"""Deliver-at-Branch auto-fulfilment.

An employee walks up to the counter, an order is rung in with order purpose
"Employee", and the jars leave the branch in their hands. There is no courier,
no delivery run and nothing for the kanban to dispatch — the order is already
delivered by the time the invoice prints. This module is what makes the system
say so.

**Why this is not "set the state to Delivered".**
Stock leaves inventory in exactly one place in this app: the Delivery Note
created on the ``Out for Delivery`` transition
(:func:`jarz_pos.services.delivery_handling.ensure_delivery_note_for_invoice`).
The Sales Invoice itself is deliberately accounting-only — ``update_stock`` is
forced to 0 at creation (``services/invoice_creation.py``) and re-forced on
every validate (``events/sales_invoice.suppress_pos_invoice_stock_update``).
So writing ``Delivered`` straight onto a fresh invoice would let real jars walk
out of the branch with the stock ledger and the bins completely untouched, and
would additionally skip:

  * the consumable Material Issue (couvert / bags) that
    ``services/consumable_deduction.deduct_consumables_on_ofd`` posts,
  * the ``custom_was_out_for_delivery`` stamp, without which — together with the
    missing Delivery Note — ``services/invoice_return`` refuses to return the
    order and the card is dead on the board.

Therefore this module runs the REAL fulfilment, in the real order, and only then
lands on ``Delivered``.

**How the OFD side effects are triggered.**
The transition is driven THROUGH ``Out for Delivery`` rather than by calling the
consumable helper by hand. ``deduct_consumables_on_ofd`` is registered as an
``on_update_after_submit`` doc event and gates itself on the invoice's state, so
it only fires when the state is actually written; and it is not the only hook on
that transition — ``stamp_out_for_delivery_flag`` and
``mint_tracking_token_on_ofd`` sit next to it. Calling one helper directly would
silently skip the other two and duplicate the gate logic. Saving the real state
reuses the whole existing chain with no duplicated logic, which is the entire
point: a branch handover and a courier dispatch consume the same stock and the
same bags, so they must go through the same code.

The board therefore sees ``Out for Delivery`` for the fraction of a second
between the two saves. That is honest — the goods genuinely were dispatched —
and the second save lands the card in ``Delivered`` before anyone can act on it.

**Failure is never fatal.** This runs after the Sales Invoice is already
submitted. Nothing in here rolls back: a rollback would take the customer's
submitted invoice with it. A failure just means the card stays in ``Recieved``
and staff drag it through the board by hand, exactly as they do today.
"""

from __future__ import annotations

from typing import Any, Optional

import frappe

from jarz_pos.constants import WS_EVENTS
from jarz_pos.services.delivery_handling import (
    ensure_delivery_note_for_invoice,
    update_submitted_sales_invoice_state,
)
from jarz_pos.utils.realtime import publish_invoice_event

#: The two aliases ``api/kanban`` reads a board state from, in its own priority
#: order. ``update_submitted_sales_invoice_state`` defaults to the FIRST one only,
#: and filters the pair down to whatever the site's Sales Invoice meta actually
#: carries — so passing both explicitly is what makes the card move regardless of
#: which alias this site has. Same call style as ``services/courier_delivery``.
STATE_FIELD_ALIASES = ("custom_sales_invoice_state", "sales_invoice_state")

#: The intermediate state whose doc events do the real work (stock via the
#: Delivery Note is created before it; consumables + the was-OFD stamp + the
#: tracking token hang off the save itself).
DISPATCH_STATE = "Out for Delivery"

#: Where a branch handover lands. Spelled correctly — unlike ``Recieved``, which
#: is live production data and stays misspelled (see tests/test_state_options_frozen).
DELIVERED_STATE = "Delivered"


def _log(*args: Any, **kwargs: Any) -> None:
    """``frappe.log_error`` that cannot itself raise.

    Not defensive padding: ``frappe.log_error`` really can throw. It routes
    through ``sentry.capture_exception``, whose first statement reads System
    Settings *outside* its own ``try``. Every call site here is explaining
    something that already happened to an already-submitted invoice; letting the
    explanation raise would be strictly worse than losing the log line.
    """
    try:
        frappe.log_error(*args, **kwargs)
    except Exception:  # pragma: no cover - the logger of last resort
        pass


def _existing_delivery_note(invoice_name: str) -> Optional[str]:
    """The submitted, non-return Delivery Note this invoice shipped on.

    Same lookup as ``services/invoice_return._original_delivery_note`` — the
    ``against_sales_invoice`` link on the DN item rows is the only reliable
    invoice→DN edge on v16 (the ``remarks`` column that used to carry it is gone).
    """
    try:
        rows = frappe.get_all(
            "Delivery Note Item",
            filters={"against_sales_invoice": invoice_name, "docstatus": 1},
            fields=["parent"],
            limit_page_length=20,
        ) or []
    except Exception:
        return None
    for row in rows:
        parent = row.get("parent")
        if not parent:
            continue
        try:
            if not int(frappe.db.get_value("Delivery Note", parent, "is_return") or 0):
                return parent
        except Exception:
            continue
    return None


def _invoice_state(inv: Any) -> str:
    """Current board state, reading the aliases in ``api/kanban``'s priority order."""
    for field in STATE_FIELD_ALIASES:
        try:
            value = inv.get(field)
        except Exception:
            value = getattr(inv, field, None)
        if value:
            return str(value).strip()
    return ""


def _add_audit_comment(invoice_name: str, comment: str) -> None:
    """Best-effort audit trail on the invoice.

    Mirrors ``api/manager._add_invoice_audit_comment`` rather than importing it:
    ``api.manager`` pulls in most of the settlement stack, and a service module
    importing an api module to write one comment is how import cycles start.
    """
    if not comment:
        return
    try:
        frappe.get_doc("Sales Invoice", invoice_name).add_comment("Comment", comment)
    except Exception:
        _log(
            frappe.get_traceback(),
            f"branch_fulfilment: audit comment failed for {invoice_name}",
        )


def _publish_state_change(inv: Any, *, invoice_name: str, delivery_note: Optional[str]) -> None:
    """Move the card on every open board without a refresh.

    Same payload shape and same event pair ``api/kanban.update_invoice_state``
    emits after a state change, so the Flutter/web board handles it with the code
    it already has. ``old_state`` is ``Recieved`` because that is the column the
    invoice was born into a moment ago.
    """
    payload = {
        "invoice_id": invoice_name,
        "old_state": "Recieved",
        "new_state": DELIVERED_STATE,
        "old_state_key": "recieved",
        "new_state_key": DELIVERED_STATE.strip().lower().replace(" ", "_"),
        "updated_by": frappe.session.user,
        "timestamp": frappe.utils.now(),
        "delivery_note": delivery_note,
        "branch_fulfilment": True,
    }
    try:
        publish_invoice_event(WS_EVENTS.INVOICE_STATE_CHANGE, payload, inv)
        publish_invoice_event(WS_EVENTS.KANBAN_UPDATE, payload, inv)
    except Exception:
        # A board that did not get the nudge redraws on its next poll/refresh.
        # It is not worth failing a completed fulfilment over.
        _log(
            frappe.get_traceback(),
            f"branch_fulfilment: realtime publish failed for {invoice_name}",
        )


def fulfil_at_branch(invoice_name: str, *, logger=None) -> dict:
    """Fulfil a submitted invoice at the counter and land it in ``Delivered``.

    Returns ``{"success", "delivery_note", "state", "error"}``. Never raises and
    never rolls back — see the module docstring.
    """
    invoice_name = str(invoice_name or "").strip()
    result: dict = {
        "success": False,
        "delivery_note": None,
        "state": None,
        "error": None,
    }

    if not invoice_name:
        result["error"] = "invoice_name is required"
        return result

    try:
        inv = frappe.get_doc("Sales Invoice", invoice_name)

        # 1) Only a submitted invoice has stock to release and a Delivery Note to
        #    hang it on. A draft is a caller bug, not an exception: report it and
        #    let the invoice flow finish.
        if int(getattr(inv, "docstatus", 0) or 0) != 1:
            result["state"] = _invoice_state(inv) or None
            result["error"] = (
                f"Invoice {invoice_name} is not submitted (docstatus="
                f"{getattr(inv, 'docstatus', None)}); cannot fulfil at branch."
            )
            return result

        # 2) Idempotency. A retried creation call, or a manual drag that beat us
        #    to it, must not post a second Delivery Note or a second consumable
        #    Material Issue. ``Delivered`` is the terminal marker for that.
        current_state = _invoice_state(inv)
        if current_state == DELIVERED_STATE:
            result["success"] = True
            result["state"] = DELIVERED_STATE
            result["delivery_note"] = _existing_delivery_note(invoice_name)
            if logger:
                logger.info(
                    f"branch_fulfilment: {invoice_name} already Delivered "
                    f"(dn={result['delivery_note']}); nothing to do"
                )
            return result

        # 3) STOCK MOVES HERE — and it must happen BEFORE the state lands on
        #    Delivered. The Delivery Note is the only thing that takes the jars
        #    out of the bin and off the ledger; if it fails we abandon the whole
        #    auto-fulfilment with the state untouched, so the card stays in
        #    Recieved and staff dispatch it by hand (which retries the DN under
        #    the normal, visible, failure-reporting path). Landing on Delivered
        #    with no DN would be the silent inventory leak this feature exists to
        #    avoid.
        dn_result = ensure_delivery_note_for_invoice(invoice_name) or {}
        dn_error = dn_result.get("error")
        if dn_error:
            result["state"] = current_state or None
            result["error"] = f"Delivery Note creation failed: {dn_error}"
            _log(
                f"branch_fulfilment: DN creation failed for {invoice_name}: {dn_error}",
                "branch_fulfilment: delivery note failed",
            )
            return result
        result["delivery_note"] = dn_result.get("delivery_note")

        # 4) + 5) Drive the transition through the real dispatch state so the
        #    existing ``on_update_after_submit`` chain fires exactly once, in the
        #    order it already runs for a courier dispatch:
        #      - deduct_consumables_on_ofd  -> couvert / colored bag / nylon bag
        #      - stamp_out_for_delivery_flag -> custom_was_out_for_delivery = 1
        #      - mint_tracking_token_on_ofd  -> /track token
        #    Reusing the hooks beats calling the helpers by hand: the gate logic
        #    lives in one place and a future hook added to the OFD transition is
        #    picked up here for free.
        dispatched = update_submitted_sales_invoice_state(
            inv, DISPATCH_STATE, field_names=STATE_FIELD_ALIASES
        )
        if not dispatched:
            # The helper only returns False when nothing was written — i.e. the
            # site's Sales Invoice meta carries neither alias. The OFD hooks
            # therefore never fired, so the consumables were NOT deducted. Say so
            # loudly instead of pretending the order is fulfilled.
            _log(
                f"Sales Invoice carries none of {STATE_FIELD_ALIASES}, so "
                f"{invoice_name} could not be moved to {DISPATCH_STATE} and its "
                "consumables were not deducted.",
                "branch_fulfilment: board state field missing",
            )

        # The consumable + stamp hooks are deliberately exception-swallowing, so a
        # silent failure there is possible. ``custom_was_out_for_delivery`` is the
        # one that must not be missing: ``services/invoice_return`` refuses to
        # return an order that never carried it, which would leave a dead card
        # nobody can unwind. Re-assert it directly rather than trust the hook.
        try:
            if not int(
                frappe.db.get_value(
                    "Sales Invoice", invoice_name, "custom_was_out_for_delivery"
                )
                or 0
            ):
                frappe.db.set_value(
                    "Sales Invoice",
                    invoice_name,
                    "custom_was_out_for_delivery",
                    1,
                    update_modified=False,
                )
                inv.custom_was_out_for_delivery = 1
        except Exception:
            _log(
                frappe.get_traceback(),
                f"branch_fulfilment: was-OFD stamp failed for {invoice_name}",
            )

        # 6) Land on the board column the counter handover actually means. Both
        #    aliases are passed explicitly — the helper defaults to the first one
        #    only, and ``api/kanban`` reads whichever the site's meta carries.
        landed = update_submitted_sales_invoice_state(
            inv, DELIVERED_STATE, field_names=STATE_FIELD_ALIASES
        )
        if not landed:
            # Nothing written and the state is not already Delivered (checked at
            # step 2) => no writable alias. The goods are out and the DN is
            # posted, but the card never moved, so report the partial outcome
            # rather than a green success the board contradicts.
            result["state"] = _invoice_state(inv) or None
            result["error"] = (
                f"Fulfilment completed (delivery note {result['delivery_note']}) but "
                f"the board state could not be set to {DELIVERED_STATE}."
            )
            return result

        result["state"] = DELIVERED_STATE
        result["success"] = True

        # 7) Nudge every open board so the card appears in Delivered without a
        #    refresh, using the same event pair api/kanban emits.
        _publish_state_change(
            inv, invoice_name=invoice_name, delivery_note=result["delivery_note"]
        )

        # 8) Audit trail: this state was not reached by a human dragging a card,
        #    and six months from now somebody will ask why.
        _add_audit_comment(
            invoice_name,
            "Auto-fulfilled at branch (Deliver at Branch policy): goods handed "
            "over at the counter. Delivery Note "
            f"{result['delivery_note'] or 'n/a'} posted, consumables deducted, "
            f"state set to {DELIVERED_STATE}.",
        )

        if logger:
            logger.info(
                f"branch_fulfilment: {invoice_name} auto-fulfilled at branch "
                f"(dn={result['delivery_note']}, state={DELIVERED_STATE})"
            )
        return result

    except Exception as exc:
        # Deliberately NO rollback. This runs inside the invoice-creation
        # transaction, after submit — rolling back here would destroy the
        # customer's invoice to undo a bookkeeping convenience.
        result["error"] = str(exc)
        _log(
            frappe.get_traceback(),
            f"branch_fulfilment: fulfil_at_branch failed for {invoice_name}",
        )
        if logger:
            try:
                logger.warning(
                    f"branch_fulfilment: fulfil_at_branch failed for {invoice_name}: {exc}"
                )
            except Exception:
                pass
        return result

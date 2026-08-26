"""Per-invoice courier transitions: Arrived, Delivered, Failed.

The Sales Invoice is already the unit of delivery work in this system — the
courier is assigned on the *invoice* (``custom_courier_party_type`` /
``custom_courier_party``), not on the Delivery Trip, and the trip is only a
batching wrapper around the same per-invoice primitives. So these three
functions stamp outcomes on the invoice and nothing else: no new document, no
new state, no accounting.

**This module posts no GL and creates no Journal Entry, Payment Entry, Delivery
Note or Courier Transaction.** Money for a delivered order moves through the
existing settlement path (``services/settlement_strategies``), which the GL audit
suite covers. ``collected_amount`` is accepted, validated and recorded as an
operational note *only* — see :func:`mark_invoice_delivered`. Putting cash logic
here would create a second, untested source of truth for the same money.

Five invariants, each of which exists because its absence has a specific,
silent failure mode:

1. **Meta assertion first.** ``update_submitted_sales_invoice_fields`` filters
   out fields the site does not have and returns ``False`` *with no error*.
   Combined with an offline queue that treats HTTP 200 as success, a courier's
   "Delivered" tap on a pre-migration deployment would simply vanish. So a
   missing field raises :class:`CourierSchemaError` rather than degrading.
2. **Every write goes through ``update_submitted_sales_invoice_fields``.** Never
   ``frappe.db.set_value`` on a submitted invoice — that skips
   ``on_update_after_submit``, and the board's realtime refresh, the consumable
   deduction and the CRM follow-up all hang off that hook.
3. **Dual idempotency.** A deterministic composite token (``"<invoice>::delivered"``)
   *plus* the caller's ``request_id``. The deterministic one survives a device
   reinstall, which is exactly when a ``request_id``-only scheme loses its
   memory and replays a whole day's queue. The ``request_id`` one covers the
   case the deterministic token cannot: a repeated *failure*, which is
   legitimately repeatable and must not double-count attempts.
4. **A failure never changes the board state.** The invoice stays
   ``Out for Delivery``; the outcome is expressed as
   ``custom_delivery_failure_reason`` + ``custom_delivery_attempt_no``. No new
   state is introduced by this project (COURIER_CONTRACTS §1).
5. **Realtime only through ``utils.realtime.publish_invoice_event``.** A bare
   ``frappe.publish_realtime`` with no user lands in the site-wide ``all`` room
   and shows one branch's orders to every branch.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from jarz_pos.constants import WS_EVENTS
from jarz_pos.services import courier_identity, delivery_leg
from jarz_pos.services.delivery_handling import update_submitted_sales_invoice_fields
from jarz_pos.utils.access_control import (
    ShiftRequiredError,
    ensure_open_shift_for_invoice,
    ensure_profile_scoped_invoice_access,
)
from jarz_pos.utils.realtime import publish_invoice_event


class CourierSchemaError(frappe.ValidationError):
    """The site's Sales Invoice schema is behind this deployment.

    A distinct class so the client can tell "run bench migrate" apart from a
    business rejection, and so the envelope wrapper below knows to let it
    propagate instead of flattening it into ``{"success": False}``.
    """


ACTION_ARRIVED = "arrived"
ACTION_DELIVERED = "delivered"
ACTION_FAILED = "failed"
#: The courier set off toward this specific customer. Not a board state and
#: not an outcome — it gates the live map and nothing else (services/delivery_leg).
ACTION_LEG_STARTED = "leg_started"
ACTION_LEG_ENDED = "leg_ended"

#: Board state a successful delivery lands on. Must be one of the seven frozen
#: options on ``Sales Invoice-custom_sales_invoice_state`` — the Kanban derives
#: its columns from that field at runtime.
DELIVERED_STATE = "Delivered"

#: Every alias the operational board state has been stored under, in the order
#: ``api.kanban._get_state_field_options`` probes them. A site carrying more than
#: one must have all of them kept in step, because different callers read
#: different ones.
_STATE_FIELD_ALIASES = (
    "custom_sales_invoice_state",
    "sales_invoice_state",
    "custom_state",
    "state",
)

#: Fields each action writes. Asserted present *and* ``allow_on_submit`` before
#: anything is written.
_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    ACTION_ARRIVED: (
        "custom_arrived_at",
        "custom_delivery_latitude",
        "custom_delivery_longitude",
        "custom_delivery_accuracy_m",
    ),
    ACTION_DELIVERED: (
        "custom_delivered_at",
        "custom_delivery_latitude",
        "custom_delivery_longitude",
        "custom_delivery_accuracy_m",
        "custom_delivery_failure_reason",
    ),
    ACTION_FAILED: (
        "custom_delivery_failure_reason",
        "custom_delivery_attempt_no",
        "custom_delivery_latitude",
        "custom_delivery_longitude",
        "custom_delivery_accuracy_m",
    ),
    ACTION_LEG_STARTED: delivery_leg.LEG_FIELDS,
    ACTION_LEG_ENDED: delivery_leg.LEG_FIELDS,
}

#: How long a ``request_id`` stays replay-protected. Long enough to cover a
#: courier out of signal for a full shift and flushing an offline queue at the
#: depot; short enough that the cache does not grow without bound.
REPLAY_TTL_SEC = 36 * 3600

_REPLAY_CACHE_PREFIX = "jarz_courier_delivery::"


# ─────────────────────────────────────────────────────────────────────────────
# Feature flag — read HERE, in the service, not in the API
# ─────────────────────────────────────────────────────────────────────────────

def courier_delivery_enabled() -> bool:
    """Master switch (``Jarz POS Settings.enable_courier_delivery_actions``).

    Off by default. Read in the service rather than the API so every entry point
    — HTTP, the staging harness, a background job — passes the same gate; a flag
    checked only in the transport layer is a flag the harness silently bypasses.
    Flipping it needs no deploy and no restart, which is what makes it a usable
    rollback lever.
    """
    try:
        return bool(
            int(
                frappe.db.get_single_value(
                    "Jarz POS Settings", "enable_courier_delivery_actions"
                )
                or 0
            )
        )
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Schema assertions
# ─────────────────────────────────────────────────────────────────────────────

def _assert_invoice_fields(fieldnames: Sequence[str]) -> None:
    """Refuse to run against a schema that would swallow the write.

    Checks existence *and* ``allow_on_submit``. Both matter: without the field
    the value is filtered out silently, and without ``allow_on_submit`` any other
    write path (Desk, plain REST) rejects it — so a field that exists without the
    flag is a half-migrated site that will behave inconsistently depending on who
    writes.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
    except Exception as exc:  # pragma: no cover - environmental
        frappe.throw(
            _("Could not read Sales Invoice metadata: {0}").format(exc),
            CourierSchemaError,
        )

    missing: List[str] = []
    not_updatable: List[str] = []
    for fieldname in fieldnames:
        field = meta.get_field(fieldname)
        if not field:
            missing.append(fieldname)
        elif not field.get("allow_on_submit"):
            not_updatable.append(fieldname)

    if missing:
        frappe.throw(
            _(
                "Sales Invoice is missing the delivery outcome fields {0}. This "
                "deployment is ahead of the site's schema — run `bench migrate`. "
                "Refusing rather than writing, because the write would be dropped "
                "silently."
            ).format(", ".join(sorted(missing))),
            CourierSchemaError,
            title=_("Schema Out Of Date"),
        )

    if not_updatable:
        frappe.throw(
            _(
                "Sales Invoice fields {0} exist but are not marked Allow on "
                "Submit, so they cannot be written on a submitted invoice. Run "
                "`bench migrate` to re-sync the Custom Field fixtures."
            ).format(", ".join(sorted(not_updatable))),
            CourierSchemaError,
            title=_("Schema Out Of Date"),
        )


def _board_state_fields() -> List[str]:
    """Whichever board-state aliases this site actually carries, in probe order."""
    try:
        meta = frappe.get_meta("Sales Invoice")
    except Exception:
        return []
    return [field for field in _STATE_FIELD_ALIASES if meta.get_field(field)]


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────

def deterministic_token(invoice_id: str, action: str) -> str:
    """The device-independent idempotency token for an invoice/action pair.

    Deliberately derived from data both the server and every device already
    agree on, so it survives an app reinstall, a new phone, or a courier handing
    the stop to a colleague mid-run — all situations where a locally generated
    ``request_id`` is a brand-new value for an action that already happened.
    """
    return f"{str(invoice_id or '').strip()}::{str(action or '').strip()}"


def _replay_key(invoice_id: str, action: str, request_id: str) -> str:
    return f"{_REPLAY_CACHE_PREFIX}{action}::{invoice_id}::{request_id}"


def _replay_lookup(invoice_id: str, action: str, request_id: str) -> Optional[Dict[str, Any]]:
    """Prior result for this exact ``request_id``, if we still remember it.

    Cache-backed, so it is best-effort by construction: a Redis flush loses the
    memory. That is tolerable only because it is the *second* guard — the
    deterministic token above is the durable one, backed by the invoice's own
    fields. The pair is what makes the design safe; either alone is not.
    """
    if not request_id:
        return None
    try:
        raw = frappe.cache().get_value(_replay_key(invoice_id, action, request_id))
    except Exception:
        return None
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except Exception:
        return None


def _replay_store(
    invoice_id: str, action: str, request_id: str, result: Dict[str, Any]
) -> None:
    if not request_id:
        return
    try:
        frappe.cache().set_value(
            _replay_key(invoice_id, action, request_id),
            json.dumps(result, default=str),
            expires_in_sec=REPLAY_TTL_SEC,
        )
    except Exception:
        # A replay-guard write failure must not undo a completed delivery; the
        # deterministic token still protects the two success paths.
        frappe.log_error(
            frappe.get_traceback(), f"courier_delivery: replay store failed for {invoice_id}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Shared preamble
# ─────────────────────────────────────────────────────────────────────────────

def _state_key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _load_and_gate(invoice_id: str, *, action_label: str) -> Any:
    """Load the invoice and run the full access gate, in the contract's order.

    ``frappe.has_permission`` → ``ensure_profile_scoped_invoice_access`` →
    ``ensure_open_shift_for_invoice``. The order is not cosmetic: the doctype
    permission is the cheapest check and the one that must never be bypassed;
    branch scoping needs a loaded document; and the shift check is the most
    situational, so asking it last means a user who fails the first two never
    sees a confusing "start a shift" prompt for an order they cannot touch at
    all.
    """
    frappe.has_permission("Sales Invoice", ptype="write", throw=True)

    inv = frappe.get_doc("Sales Invoice", invoice_id)

    ensure_profile_scoped_invoice_access(inv, action_label=action_label)
    ensure_open_shift_for_invoice(inv, action_label=action_label)

    # After the three mandated gates, never before: a courier may only act on
    # their own stop. A no-op for dispatchers and managers, who are not couriers
    # and legitimately stamp outcomes from the POS board.
    courier_identity.assert_invoice_assigned_to_courier(inv, action_label=action_label)

    return inv


def _assert_actionable(inv: Any) -> Optional[str]:
    """Return an error string when *inv* cannot take a delivery outcome."""
    if int(getattr(inv, "docstatus", 0) or 0) != 1:
        return _("Only submitted invoices can take a delivery outcome")
    if int(inv.get("is_return") or 0):
        return _("A credit note has no delivery outcome")
    if _state_key(inv.get("custom_return_status")) == "fully_returned":
        return _("This order was fully returned and can no longer be delivered")
    return None


def _geo_updates(
    latitude: Any, longitude: Any, accuracy_m: Any
) -> Dict[str, Any]:
    """Coordinate fields for the update dict, omitting anything unusable.

    Imported lazily so this module stays importable on a site where the geo lane
    has not landed.
    """
    from jarz_pos.utils.geo import is_valid_coordinate

    updates: Dict[str, Any] = {}
    if latitude not in (None, "") and longitude not in (None, ""):
        if is_valid_coordinate(latitude, longitude):
            updates["custom_delivery_latitude"] = float(latitude)
            updates["custom_delivery_longitude"] = float(longitude)
    if accuracy_m not in (None, ""):
        try:
            updates["custom_delivery_accuracy_m"] = max(0.0, float(accuracy_m))
        except (TypeError, ValueError):
            pass
    return updates


def _publish(event: str, inv: Any, payload: Dict[str, Any]) -> None:
    """Emit to the branch that owns the invoice. Never raises."""
    try:
        publish_invoice_event(event, payload, inv)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"courier_delivery: realtime publish failed ({event})"
        )


def _envelope(action: str, inv: Any, **extra: Any) -> Dict[str, Any]:
    return {
        "success": True,
        "action": action,
        "invoice_id": getattr(inv, "name", None),
        "idempotency_token": deterministic_token(getattr(inv, "name", ""), action),
        **extra,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Transitions (COURIER_CONTRACTS §5)
# ─────────────────────────────────────────────────────────────────────────────

def mark_invoice_arrived(
    invoice_id: str,
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Stamp "the courier is at the door" on an invoice.

    Records no money and does not move the board state — arrival is a sub-step
    of Out for Delivery, not a column. Its value is the timestamp: it is what
    turns "how long did that stop take?" and "how long did the customer wait?"
    into answerable questions.
    """
    invoice_id = str(invoice_id or "").strip()
    request_id = str(request_id or "").strip()

    if not invoice_id:
        return {"success": False, "error": _("invoice_id is required")}
    if not courier_delivery_enabled():
        return {"success": False, "error": _("Courier delivery actions are not enabled.")}

    replayed = _replay_lookup(invoice_id, ACTION_ARRIVED, request_id)
    if replayed:
        return {**replayed, "replayed": True}

    _assert_invoice_fields(_REQUIRED_FIELDS[ACTION_ARRIVED])

    try:
        inv = _load_and_gate(invoice_id, action_label="marking this order arrived")

        blocked = _assert_actionable(inv)
        if blocked:
            return {"success": False, "error": blocked}

        # Deterministic guard: the timestamp itself is the durable token.
        if inv.get("custom_arrived_at"):
            result = _envelope(
                ACTION_ARRIVED,
                inv,
                idempotent=True,
                arrived_at=str(inv.get("custom_arrived_at")),
                message=_("Already marked arrived"),
            )
            _replay_store(invoice_id, ACTION_ARRIVED, request_id, result)
            return result

        updates: Dict[str, Any] = {"custom_arrived_at": now_datetime()}
        updates.update(_geo_updates(latitude, longitude, accuracy_m))

        if not update_submitted_sales_invoice_fields(inv, updates):
            return {
                "success": False,
                "error": _("Could not record the arrival on {0}").format(invoice_id),
            }

        result = _envelope(
            ACTION_ARRIVED,
            inv,
            idempotent=False,
            arrived_at=str(inv.get("custom_arrived_at")),
            latitude=updates.get("custom_delivery_latitude"),
            longitude=updates.get("custom_delivery_longitude"),
        )
        _replay_store(invoice_id, ACTION_ARRIVED, request_id, result)
        _publish(WS_EVENTS.COURIER_STOP_ARRIVED, inv, dict(result))
        return result

    except (frappe.PermissionError, ShiftRequiredError, CourierSchemaError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"mark_invoice_arrived failed: {invoice_id}")
        return {"success": False, "error": str(exc)}


def mark_invoice_delivered(
    invoice_id: str,
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    collected_amount: Optional[float] = None,
    recipient_name: Optional[str] = None,
    is_mocked: bool = False,
    request_id: Optional[str] = None,
) -> dict:
    """Record a successful hand-over and move the invoice to ``Delivered``.

    ``collected_amount``, ``recipient_name`` and ``is_mocked`` are **recorded as
    an operational note, not as accounting and not as schema**. The contract
    freezes the field list at eight and none of them is a money field, so:

    * the cash a courier collected settles through the existing
      ``Courier Transaction`` / settlement path, which the GL audit covers — this
      function must not become a second way for money to move;
    * proof-of-delivery detail (who signed, whether the GPS was faked) belongs on
      ``jarz_courier``'s ``Delivery Proof``, which owns that data.

    A mocked location is escalated to the Error Log regardless, because a
    spoofed GPS at proof-of-delivery is a fraud signal and losing it is not an
    acceptable degradation.
    """
    invoice_id = str(invoice_id or "").strip()
    request_id = str(request_id or "").strip()
    recipient_name = (str(recipient_name or "").strip() or None)

    if not invoice_id:
        return {"success": False, "error": _("invoice_id is required")}
    if not courier_delivery_enabled():
        return {"success": False, "error": _("Courier delivery actions are not enabled.")}

    collected: Optional[float] = None
    if collected_amount not in (None, ""):
        try:
            collected = flt(collected_amount)
        except Exception:
            return {"success": False, "error": _("collected_amount must be a number")}
        if collected < 0:
            return {"success": False, "error": _("collected_amount cannot be negative")}

    replayed = _replay_lookup(invoice_id, ACTION_DELIVERED, request_id)
    if replayed:
        return {**replayed, "replayed": True}

    _assert_invoice_fields(_REQUIRED_FIELDS[ACTION_DELIVERED])

    try:
        inv = _load_and_gate(invoice_id, action_label="marking this order delivered")

        blocked = _assert_actionable(inv)
        if blocked:
            return {"success": False, "error": blocked}

        # Deterministic guard, durable across a device reinstall: the delivery
        # timestamp on the invoice IS the token "<invoice>::delivered" stands for.
        if inv.get("custom_delivered_at"):
            result = _envelope(
                ACTION_DELIVERED,
                inv,
                idempotent=True,
                delivered_at=str(inv.get("custom_delivered_at")),
                message=_("Already marked delivered"),
            )
            _replay_store(invoice_id, ACTION_DELIVERED, request_id, result)
            return result

        updates: Dict[str, Any] = {
            "custom_delivered_at": now_datetime(),
            # A delivered order has no outstanding failure. Cleared in the same
            # save so the two can never be observed disagreeing.
            "custom_delivery_failure_reason": None,
        }
        # A terminal outcome closes the leg by itself, in this same save. A
        # courier who forgets to end a leg must never leave a live map open on a
        # delivered order, and a second write to close it would be observable in
        # between as "delivered, still being driven to".
        if delivery_leg.leg_fields_present():
            updates.update(delivery_leg.end_updates(updates["custom_delivered_at"]))
        if not inv.get("custom_arrived_at"):
            # A courier who taps Delivered without tapping Arrived (or whose
            # Arrived is still stuck in the offline queue) still needs an arrival
            # time, or every duration report silently loses that stop.
            updates["custom_arrived_at"] = updates["custom_delivered_at"]
        updates.update(_geo_updates(latitude, longitude, accuracy_m))

        state_fields = _board_state_fields()
        for field in state_fields:
            updates[field] = DELIVERED_STATE
        if not state_fields:
            frappe.log_error(
                title="Courier: board state field missing",
                message=(
                    f"Sales Invoice carries none of {_STATE_FIELD_ALIASES}, so "
                    f"{invoice_id} was delivered but stays on its old board column."
                ),
            )

        if not update_submitted_sales_invoice_fields(inv, updates):
            return {
                "success": False,
                "error": _("Could not record the delivery on {0}").format(invoice_id),
            }

        _record_delivery_note_comment(
            inv,
            collected=collected,
            recipient_name=recipient_name,
            is_mocked=bool(is_mocked),
        )

        if is_mocked:
            frappe.log_error(
                title="Courier: mocked location at delivery",
                message=(
                    f"Invoice {invoice_id} was marked delivered with a mocked "
                    f"device location by {frappe.session.user}."
                ),
            )

        result = _envelope(
            ACTION_DELIVERED,
            inv,
            idempotent=False,
            state=DELIVERED_STATE if state_fields else None,
            delivered_at=str(inv.get("custom_delivered_at")),
            latitude=updates.get("custom_delivery_latitude"),
            longitude=updates.get("custom_delivery_longitude"),
            collected_amount=collected,
            recipient_name=recipient_name,
            is_mocked=bool(is_mocked),
            # Stated explicitly in the response so no client can mistake this
            # call for a settlement.
            posted_accounting=False,
        )
        _replay_store(invoice_id, ACTION_DELIVERED, request_id, result)
        _publish(WS_EVENTS.COURIER_STOP_DELIVERED, inv, dict(result))
        return result

    except (frappe.PermissionError, ShiftRequiredError, CourierSchemaError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"mark_invoice_delivered failed: {invoice_id}")
        return {"success": False, "error": str(exc)}


def mark_invoice_failed(
    invoice_id: str,
    *,
    failure_reason: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    accuracy_m: Optional[float] = None,
    notes: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict:
    """Record a failed delivery attempt.

    **The board state is deliberately left alone.** The order stays
    ``Out for Delivery`` and the failure is expressed through
    ``custom_delivery_failure_reason`` + ``custom_delivery_attempt_no``. A new
    state would touch the invoice state machine and all 274 references that read
    it across 44 files, for a case a reason code already describes.

    Repeated failures are legitimate (three attempts on three days is a real
    sequence), so there is no deterministic idempotency token here — only the
    caller's ``request_id`` stops an offline retry of *the same tap* from
    counting twice. A caller that omits ``request_id`` will double-count on
    retry, and that is why the API layer always supplies one.
    """
    invoice_id = str(invoice_id or "").strip()
    request_id = str(request_id or "").strip()
    failure_reason = str(failure_reason or "").strip()
    notes = (str(notes or "").strip() or None)

    if not invoice_id:
        return {"success": False, "error": _("invoice_id is required")}
    if not failure_reason:
        return {"success": False, "error": _("A failure reason is required")}
    if not courier_delivery_enabled():
        return {"success": False, "error": _("Courier delivery actions are not enabled.")}

    replayed = _replay_lookup(invoice_id, ACTION_FAILED, request_id)
    if replayed:
        return {**replayed, "replayed": True}

    _assert_invoice_fields(_REQUIRED_FIELDS[ACTION_FAILED])

    try:
        reason = _resolve_failure_reason(failure_reason)
        if not reason:
            return {
                "success": False,
                "error": _("Unknown or inactive failure reason: {0}").format(failure_reason),
            }

        inv = _load_and_gate(invoice_id, action_label="recording a failed delivery")

        blocked = _assert_actionable(inv)
        if blocked:
            return {"success": False, "error": blocked}

        state_before = {field: inv.get(field) for field in _board_state_fields()}

        attempt_no = int(flt(inv.get("custom_delivery_attempt_no")) or 0) + 1
        updates: Dict[str, Any] = {
            "custom_delivery_failure_reason": reason["name"],
            "custom_delivery_attempt_no": attempt_no,
        }
        updates.update(_geo_updates(latitude, longitude, accuracy_m))
        # The attempt is over, so the leg is over — the courier is not driving
        # toward this customer any more. Note this closes the *leg* and still
        # changes no board state: the order remains Out for Delivery and a
        # second attempt simply opens a new leg.
        if delivery_leg.leg_fields_present():
            # Explicit timestamp, not end_updates()' own default: that would read
            # a second clock and stamp this save with two different times.
            updates.update(delivery_leg.end_updates(now_datetime()))

        # Guard the frozen invariant at the point of the write, not only in a
        # test: no board-state alias may ever appear in a failure update.
        trespass = sorted(set(updates) & set(_STATE_FIELD_ALIASES))
        if trespass:  # pragma: no cover - defensive
            frappe.throw(
                _("A failed delivery must not change the invoice state ({0})").format(
                    ", ".join(trespass)
                )
            )

        if not update_submitted_sales_invoice_fields(inv, updates):
            return {
                "success": False,
                "error": _("Could not record the failed attempt on {0}").format(invoice_id),
            }

        if notes:
            _add_comment(
                inv,
                f"Delivery attempt {attempt_no} failed — {reason['label_en']}: {notes}",
            )

        result = _envelope(
            ACTION_FAILED,
            inv,
            idempotent=False,
            failure_reason=reason["name"],
            failure_label_en=reason["label_en"],
            failure_label_ar=reason["label_ar"],
            next_action=reason["next_action"],
            attempt_no=attempt_no,
            notes=notes,
            # Echoed so a client (or a test) can see the state really did not move.
            state_unchanged=state_before,
        )
        _replay_store(invoice_id, ACTION_FAILED, request_id, result)
        _publish(WS_EVENTS.COURIER_STOP_FAILED, inv, dict(result))
        return result

    except (frappe.PermissionError, ShiftRequiredError, CourierSchemaError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"mark_invoice_failed failed: {invoice_id}")
        return {"success": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
# Trip leg (services/delivery_leg) — gates the live map, changes no state
# ─────────────────────────────────────────────────────────────────────────────

def mark_invoice_leg_started(
    invoice_id: str,
    *,
    at: Any = None,
    request_id: Optional[str] = None,
) -> dict:
    """The courier set off toward **this** customer.

    Three things this is not, because each has been assumed at least once:

    * **not a board state.** The order was already ``Out for Delivery`` when the
      trip was dispatched. Nothing here touches a state alias, and the guard
      below enforces that the same way :func:`mark_invoice_failed` does.
    * **not an outcome.** No accounting, no settlement, no arrival.
    * **not deterministically idempotent.** Re-starting a leg is a legitimate,
      repeatable action — a courier who skips an order and comes back to it
      later re-opens it, and the map is expected to reopen. So there is only the
      caller's ``request_id`` to swallow an offline double-send, exactly as for
      a failed attempt.

    Starting a leg **closes every other open leg for the same courier.** That is
    the rule that makes the whole thing worth having: a courier can be driving
    toward exactly one customer, so a start elsewhere is proof the previous leg
    is over, whether or not anybody remembered to end it.
    """
    invoice_id = str(invoice_id or "").strip()
    request_id = str(request_id or "").strip()

    if not invoice_id:
        return {"success": False, "error": _("invoice_id is required")}
    if not courier_delivery_enabled():
        return {"success": False, "error": _("Courier delivery actions are not enabled.")}

    replayed = _replay_lookup(invoice_id, ACTION_LEG_STARTED, request_id)
    if replayed:
        return {**replayed, "replayed": True}

    _assert_invoice_fields(_REQUIRED_FIELDS[ACTION_LEG_STARTED])

    try:
        inv = _load_and_gate(invoice_id, action_label="starting the drive to this order")

        blocked = _assert_actionable(inv)
        if blocked:
            return {"success": False, "error": blocked}
        if inv.get("custom_delivered_at"):
            return {"success": False, "error": _("This order was already delivered")}

        started_at = at or now_datetime()
        superseded = _close_other_open_legs(inv, at=started_at)

        updates = dict(delivery_leg.start_updates(started_at))
        trespass = sorted(set(updates) & set(_STATE_FIELD_ALIASES))
        if trespass:  # pragma: no cover - defensive
            frappe.throw(
                _("Starting a leg must not change the invoice state ({0})").format(
                    ", ".join(trespass)
                )
            )

        if not update_submitted_sales_invoice_fields(inv, updates):
            return {
                "success": False,
                "error": _("Could not start the delivery leg on {0}").format(invoice_id),
            }

        result = _envelope(
            ACTION_LEG_STARTED,
            inv,
            idempotent=False,
            leg_started_at=str(inv.get(delivery_leg.LEG_STARTED_FIELD)),
            # Named rather than counted so a client can tell the courier which
            # stop it just took the map away from, instead of doing it silently.
            superseded_invoices=superseded,
        )
        _replay_store(invoice_id, ACTION_LEG_STARTED, request_id, result)
        _publish(WS_EVENTS.COURIER_LEG_STARTED, inv, dict(result))
        return result

    except (frappe.PermissionError, ShiftRequiredError, CourierSchemaError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"mark_invoice_leg_started failed: {invoice_id}")
        return {"success": False, "error": str(exc)}


def mark_invoice_leg_ended(
    invoice_id: str,
    *,
    at: Any = None,
    request_id: Optional[str] = None,
) -> dict:
    """Close this order's leg. Closes the map, changes nothing else.

    Explicitly **not** a delivery. An order whose leg ended is still
    ``Out for Delivery`` and still owes an outcome; a client that sends only
    this when a courier hands the order over leaves it out for delivery forever.
    Delivered and failed close the leg themselves, so this is only for the
    abandon case — the courier skipped the stop and is driving somewhere else.
    """
    invoice_id = str(invoice_id or "").strip()
    request_id = str(request_id or "").strip()

    if not invoice_id:
        return {"success": False, "error": _("invoice_id is required")}
    if not courier_delivery_enabled():
        return {"success": False, "error": _("Courier delivery actions are not enabled.")}

    replayed = _replay_lookup(invoice_id, ACTION_LEG_ENDED, request_id)
    if replayed:
        return {**replayed, "replayed": True}

    _assert_invoice_fields(_REQUIRED_FIELDS[ACTION_LEG_ENDED])

    try:
        inv = _load_and_gate(invoice_id, action_label="ending the drive to this order")

        if not delivery_leg.is_leg_open(inv):
            result = _envelope(
                ACTION_LEG_ENDED,
                inv,
                idempotent=True,
                leg_ended_at=str(inv.get(delivery_leg.LEG_ENDED_FIELD) or ""),
                message=_("No open leg on this order"),
            )
            _replay_store(invoice_id, ACTION_LEG_ENDED, request_id, result)
            return result

        ended_at = at or now_datetime()
        if not update_submitted_sales_invoice_fields(
            inv, delivery_leg.end_updates(ended_at)
        ):
            return {
                "success": False,
                "error": _("Could not end the delivery leg on {0}").format(invoice_id),
            }

        result = _envelope(
            ACTION_LEG_ENDED,
            inv,
            idempotent=False,
            leg_ended_at=str(inv.get(delivery_leg.LEG_ENDED_FIELD)),
        )
        _replay_store(invoice_id, ACTION_LEG_ENDED, request_id, result)
        _publish(WS_EVENTS.COURIER_LEG_ENDED, inv, dict(result))
        return result

    except (frappe.PermissionError, ShiftRequiredError, CourierSchemaError):
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"mark_invoice_leg_ended failed: {invoice_id}")
        return {"success": False, "error": str(exc)}


def _close_other_open_legs(inv: Any, *, at: Any) -> List[str]:
    """Close every other open leg this courier holds. Never raises.

    Deliberately best-effort. If closing a stale leg fails, the *new* leg must
    still open — a courier standing on a doorstep is not helped by an error
    about a different order. The cost of that failure is one extra live map,
    which the next terminal outcome closes anyway.

    Writes through ``frappe.db.set_value`` rather than the submitted-invoice
    saver on purpose: this is a bulk sweep over other documents, each save fires
    the whole ``on_update_after_submit`` chain, and the field written is a
    timestamp no hook reads. The one consumer — the public tracking read —
    queries the column directly.
    """
    party_type = str(inv.get("custom_courier_party_type") or "").strip()
    party = str(inv.get("custom_courier_party") or "").strip()
    if not (party_type and party):
        return []

    closed: List[str] = []
    try:
        others = delivery_leg.open_legs_for_courier(
            party_type, party, exclude_invoice=str(getattr(inv, "name", "") or "")
        )
        for name in others:
            try:
                frappe.db.set_value(
                    "Sales Invoice",
                    name,
                    delivery_leg.LEG_ENDED_FIELD,
                    at,
                    update_modified=False,
                )
                closed.append(name)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"delivery_leg: could not close superseded leg {name}",
                )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "delivery_leg: superseding sweep failed")
    return closed


# ─────────────────────────────────────────────────────────────────────────────
# Supporting reads / notes
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_failure_reason(code: str) -> Optional[Dict[str, Any]]:
    """Load an *active* ``Delivery Failure Reason`` by name, or ``None``."""
    try:
        row = frappe.db.get_value(
            "Delivery Failure Reason",
            str(code or "").strip(),
            ["name", "label_en", "label_ar", "next_action", "is_active"],
            as_dict=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Failed to resolve Delivery Failure Reason")
        return None

    if not row or not int(row.get("is_active") or 0):
        return None
    return {
        "name": row["name"],
        "label_en": row.get("label_en") or row["name"],
        "label_ar": row.get("label_ar") or "",
        "next_action": row.get("next_action") or "Reschedule",
    }


def list_failure_reasons() -> List[Dict[str, Any]]:
    """Active failure reasons for the courier's picker."""
    try:
        return (
            frappe.get_all(
                "Delivery Failure Reason",
                filters={"is_active": 1},
                fields=["name", "code", "label_en", "label_ar", "next_action"],
                order_by="label_en asc",
            )
            or []
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "list_failure_reasons failed")
        return []


def _add_comment(inv: Any, content: str) -> None:
    """Attach an informational comment. Best-effort; never raises."""
    try:
        frappe.get_doc(
            {
                "doctype": "Comment",
                "comment_type": "Info",
                "reference_doctype": "Sales Invoice",
                "reference_name": getattr(inv, "name", None),
                "content": content,
            }
        ).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "courier_delivery: comment insert failed")


def _record_delivery_note_comment(
    inv: Any,
    *,
    collected: Optional[float],
    recipient_name: Optional[str],
    is_mocked: bool,
) -> None:
    """Persist the non-schema delivery detail somewhere durable and visible.

    A Comment, not a custom field: the contract freezes the field list at eight,
    and none of these three is one of them. A Comment costs no schema, survives,
    and shows up in the Desk timeline where an investigator will actually look.
    """
    parts: List[str] = [f"Delivered by {frappe.session.user}"]
    if recipient_name:
        parts.append(f"received by {recipient_name}")
    if collected is not None:
        parts.append(f"collected {collected}")
    if is_mocked:
        parts.append("MOCKED DEVICE LOCATION")
    if len(parts) == 1:
        return
    _add_comment(inv, " — ".join(parts))

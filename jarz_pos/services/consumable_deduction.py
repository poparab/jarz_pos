"""
Automatic consumable stock deduction when a Sales Invoice transitions to "Out for Delivery".

Items deducted per order:
  - covier          : 1 per medium or large jar
  - colored bag     : ceil(large_qty / 5 + medium_qty / 7)
  - Nylon Inside bag: same count as colored bag

Stock is deducted from the site's consumables store (resolved per item via
``jarz_pos.utils.warehouse_utils.resolve_purchase_warehouse`` — see
``_get_warehouse`` below), NOT the invoice's POS Profile warehouse. A Material
Issue Stock Entry is created and its name stored on the invoice so it can be
cancelled if the invoice is later cancelled.

These handlers are registered in hooks.py doc_events["Sales Invoice"].

Production incident (2026-09-06), which is why this file looks the way it
does:

* 167 invoices reached "Out for Delivery" since 2026-08-26; 167 Stock Entries
  were attempted; **0 succeeded**. 169 Stock Entries were found sitting at
  ``docstatus=1`` with **zero GL Entries** — the previous ``_get_warehouse``
  resolved the invoice's POS Profile warehouse (a branch finished-goods
  store), which has never in the site's history received any Consumable-group
  stock, so the negative-stock guard refused every issue.

  The partial write is worse than "nothing posted", and the shape matters if
  you are cleaning it up: ``update_stock_ledger()`` DID write a Stock Ledger
  Entry for the first line (``covier``) on each of those 169 documents — 169
  zero-value rows summing to exactly ``-919`` — and only THEN raised during
  the valuation recompute, before ``make_gl_entries()`` ran. Nothing rolled
  it back, so the request committed the half-written document. ERPNext's
  ``update_bin_qty`` sits after the raising call, so the branch Bins never
  accumulated: they read ``-2 / -5 / -4`` against an SLE sum of ``-919``.
  Bin and ledger disagree by 908 units, and both figures are fiction.
  ``_create_material_issue``'s savepoint is what stops this recurring; the
  169 documents already on disk need their own remediation and are NOT
  repaired by deploying this file.
* ``Nylon Inside bag`` has no ``Bin`` rows anywhere (it has never been
  purchased), so even pointing at the right warehouse is not enough: a naive
  fix would just move the 100% failure rate onto that item and take the two
  good lines down with it, because ERPNext aborts the whole Stock Entry's
  ``make_sl_entries`` on the first row it cannot post. ``_create_material_issue``
  now pre-checks ``Bin.actual_qty`` per line and drops what it cannot cover
  instead of letting one dead item sink the other two.
* ``custom_consumable_stock_entry`` was populated on 0 invoices because it was
  only assigned *after* the call that raised, so
  ``reverse_consumable_deduction_on_cancel`` could never find it. It is now
  persisted the moment the Stock Entry submits successfully.
* The failure was unrecoverable: ``stamp_out_for_delivery_flag`` runs right
  after this hook and sets ``custom_was_out_for_delivery=1`` permanently — the
  very first guard this handler checks — so a bare ``frappe.log_error`` (which
  nobody watches) meant the feature ran at 0% success for 11 days completely
  unnoticed. See ``_notify_deduction_failure`` for the human-visible signal
  added alongside the log.

KNOWN AND DELIBERATE LIMITATION — read before trusting these numbers
--------------------------------------------------------------------
``_notify_deduction_failure`` fires only when NOTHING could be posted. A
partial result — a line dropped for zero stock, or clamped to the quantity on
hand — returns successfully and therefore alerts nobody. Two live consequences:

* ``Nylon Inside bag`` has never been purchased, so it is dropped on EVERY
  order until somebody stocks it. That is one ``no stock`` Error Log row per
  order (~15-18/day) on the very channel this module's own history proves
  nobody reads.
* Once ``Consumables - J`` runs low (covier 7000 on hand against ~89/day, so
  roughly late November 2026 at current volume) the clamp starts under-booking
  consumption silently. The order still ships with the consumables physically
  used; the ledger records less. That is a slower, opposite-signed version of
  the very drift this module was rewritten to stop.

Neither is fixed here. Fixing it properly means alerting on drop/clamp too,
with throttling so ~18 identical alerts a day do not simply retrain everyone to
ignore the new signal as well — which needs its own test design. The operator
action (purchase ``Nylon Inside bag`` into ``Consumables - J``) removes the
first case entirely and is the cheaper half.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Tuple

try:
    import frappe
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

_COUVERT_ITEM = "covier"
_COLORED_BAG_ITEM = "colored bag"
_NYLON_BAG_ITEM = "Nylon Inside bag"

# ``Error Log.method`` is a Data column -- varchar(140) -- and
# ``BaseDocument._validate_length`` THROWS on overflow rather than truncating.
_ERROR_LOG_TITLE_MAX = 140


def _log(title: str, message: str) -> None:
    """Write an Error Log row without ever being able to raise.

    Two traps this exists to close, both of which bit this exact module:

    1. ``frappe.log_error(a, b)`` only swaps its arguments when ``"\\n" in a``
       (see ``frappe/utils/error.py``). A single-line first argument therefore
       stays the TITLE and is written to ``Error Log.method`` -- varchar(140),
       validated with a throw, not a truncate. The natural
       ``f"Consumable deduction: {item} has {n} on hand in {wh} for {inv}..."``
       is 155 characters, so the drop path would have raised
       ``CharacterLengthExceededError`` on the very line meant to make a
       missing item survivable -- outside the savepoint, aborting the whole
       deduction. Keyword arguments make the mapping explicit and the title is
       clamped regardless.
    2. This handler is fire-and-forget: it must never raise into
       ``on_update_after_submit`` or it blocks dispatch. Logging is not worth
       an order, so a failure to log is swallowed.
    """
    if frappe is None:  # pragma: no cover - import guard
        return
    try:
        frappe.log_error(
            title=str(title or "consumable_deduction")[:_ERROR_LOG_TITLE_MAX],
            message=str(message or ""),
        )
    except Exception:
        pass

_MEDIUM_GROUPS = {"Medium", "Meduim"}  # second spelling is a known data typo
_LARGE_GROUPS = {"Large"}


# ---------------------------------------------------------------------------
# Public hook handlers
# ---------------------------------------------------------------------------

def deduct_consumables_on_ofd(doc: Any, method: Optional[str] = None) -> None:
    """Create a Material Issue SE for consumables when the invoice first goes Out for Delivery.

    Safe no-op on every subsequent save — guarded by custom_was_out_for_delivery.
    Errors are logged AND surfaced to a human (see ``_notify_deduction_failure``)
    but never re-raised, so a stock shortfall can never block the OFD state
    transition it hangs off of.
    """
    if not frappe or not doc or not getattr(doc, "name", None):
        return
    try:
        # Guard: already processed on a previous save
        if int(getattr(doc, "custom_was_out_for_delivery", 0) or 0):
            return

        current_state = str(
            getattr(doc, "custom_sales_invoice_state", None)
            or getattr(doc, "sales_invoice_state", None)
            or ""
        ).strip()
        if current_state != "Out for Delivery":
            return

        # Guard: SE already created (double-fire safety)
        existing_se = frappe.db.get_value("Sales Invoice", doc.name, "custom_consumable_stock_entry")
        if existing_se:
            return

        medium_qty, large_qty = _calc_jar_quantities(doc)
        couvert_qty = medium_qty + large_qty
        if couvert_qty == 0:
            return  # no medium/large jars in this order — nothing to deduct

        bag_qty = math.ceil(large_qty / 5 + medium_qty / 7)
        nylon_qty = bag_qty

        company = str(getattr(doc, "company", "") or "").strip()
        if not company:
            _log(
                "consumable_deduction: missing company",
                f"Consumable deduction skipped for {doc.name}: invoice has no company.",
            )
            _notify_deduction_failure(doc, reason="The invoice has no company set.")
            return

        se_name = _create_material_issue(
            invoice_name=doc.name,
            company=company,
            couvert_qty=couvert_qty,
            bag_qty=bag_qty,
            nylon_qty=nylon_qty,
        )

        if not se_name:
            _notify_deduction_failure(
                doc,
                reason=(
                    "No consumable line had enough stock in its resolved warehouse; "
                    "nothing was deducted."
                ),
            )
            return

        doc.custom_consumable_stock_entry = se_name

    except Exception as exc:
        # Both of these MUST be incapable of raising: this handler runs inside
        # on_update_after_submit, so anything escaping here blocks the Out for
        # Delivery transition -- i.e. a bookkeeping failure would stop dispatch.
        # _log swallows its own errors; _notify_deduction_failure is guarded too.
        _log(
            f"consumable_deduction: deduct failed for {getattr(doc, 'name', '?')}",
            frappe.get_traceback(),
        )
        try:
            _notify_deduction_failure(doc, reason=str(exc) or "Unexpected error during consumable deduction.")
        except Exception:
            pass


def reverse_consumable_deduction_on_cancel(doc: Any, method: Optional[str] = None) -> None:
    """Cancel the consumable Material Issue SE when its parent invoice is cancelled.

    Safe no-op if the invoice never reached Out for Delivery (no SE was created).
    Errors are logged but never re-raised.
    """
    if not frappe or not doc or not getattr(doc, "name", None):
        return
    try:
        # Read from DB — the doc object may carry a stale in-memory value
        se_name = frappe.db.get_value("Sales Invoice", doc.name, "custom_consumable_stock_entry")
        if not se_name:
            return  # invoice was cancelled before reaching OFD

        _cancel_se_if_submitted(se_name, invoice_name=doc.name)

    except Exception:
        _log(
            f"consumable_deduction: reverse failed for {getattr(doc, 'name', '?')}",
            frappe.get_traceback(),
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _calc_jar_quantities(doc: Any) -> tuple[float, float]:
    """Return (medium_qty, large_qty) from the invoice items table."""
    medium_qty = 0.0
    large_qty = 0.0
    for item in getattr(doc, "items", []):
        group = str(getattr(item, "item_group", "") or "").strip()
        qty = float(getattr(item, "qty", 0) or 0)
        if group in _MEDIUM_GROUPS:
            medium_qty += qty
        elif group in _LARGE_GROUPS:
            large_qty += qty
    return medium_qty, large_qty


def _get_warehouse(item_code: str, company: str) -> Optional[str]:
    """Resolve where THIS consumable actually lives — never the POS Profile warehouse.

    Why not the invoice's POS Profile / kanban-profile warehouse (the
    pre-2026-09-06 behaviour): that resolves to a branch finished-goods store
    (``Nasr city - J``, ``Dokki - J``, ``6th of october - J``), and confirmed
    against production Stock Ledger Entries, those stores have received
    **zero** Consumable-group stock in the site's entire history. Every one of
    167 deduction attempts since 2026-08-26 therefore tried to issue stock
    from a warehouse that never had any of it, and ERPNext's negative-stock
    guard raised inside ``update_stock_ledger()`` before a single GL Entry was
    posted — a 100% failure rate that ran silently for 11 days.

    Real consumable stock lives in ``Consumables - J``, which is exactly the
    warehouse ``Jarz POS Settings.purchase_warehouse_routes`` already maps the
    ``Consumable`` item group to (``jarz_pos.setup.purchase_setup.WAREHOUSE_ROUTES``).
    Reusing the existing purchasing resolver —
    :func:`jarz_pos.utils.warehouse_utils.resolve_purchase_warehouse` — instead
    of hardcoding ``"Consumables - J"`` keeps this in step with whatever an
    operator later reconfigures on Jarz POS Settings, and resolution is done
    per item code (this is called once per consumable line) because the three
    items could in principle be routed to different stores.

    Returns ``None`` — never raises — when no route can be resolved.
    ``resolve_purchase_warehouse`` is written for purchasing, where a blank
    warehouse must hard-stop the transaction (``frappe.throw``); here the
    caller must be able to degrade to "drop this line" without ever blocking
    the Out for Delivery transition.
    """
    item_code = str(item_code or "").strip()
    company = str(company or "").strip()
    if not item_code or not company:
        return None
    try:
        from jarz_pos.utils.warehouse_utils import resolve_purchase_warehouse

        warehouse = resolve_purchase_warehouse(item_code, company)
        return str(warehouse or "").strip() or None
    except Exception:
        return None


def _bin_actual_qty(item_code: str, warehouse: str) -> float:
    """Current on-hand qty for *item_code* in *warehouse*, or 0 on any failure."""
    try:
        value = frappe.db.get_value(
            "Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
        )
        return float(value or 0)
    except Exception:
        return 0.0


def _build_coverable_lines(
    company: str,
    requested: List[Tuple[str, float]],
    *,
    invoice_name: str,
) -> List[Dict[str, Any]]:
    """Resolve a warehouse and clamp each requested line to what stock can cover.

    Deliberately does NOT enable ``allow_negative_stock`` and does NOT set a
    branch valuation rate to force a post — branch valuation is 0.00, so
    either would post a zero-value Stock Ledger Entry and book zero COGS:
    silently wrong instead of loudly wrong. Instead, a line the warehouse
    cannot (fully) cover is clamped or dropped here, before the Stock Entry is
    ever built, so ``Nylon Inside bag`` (which has never carried a single Bin
    row) cannot take the other two consumables down with it — ERPNext aborts
    ``make_sl_entries`` for the WHOLE document on the first row it cannot
    post, so one uncoverable line previously meant zero SLEs for all three.
    """
    lines: List[Dict[str, Any]] = []
    for item_code, qty in requested:
        qty = float(qty or 0)
        if qty <= 0:
            continue

        warehouse = _get_warehouse(item_code, company)
        if not warehouse:
            _log(
                "consumable_deduction: warehouse unresolved",
                f"Consumable deduction: no source warehouse resolved for {item_code} "
                f"({invoice_name}); line dropped.",
            )
            continue

        available = _bin_actual_qty(item_code, warehouse)
        if available <= 0:
            _log(
                "consumable_deduction: no stock",
                f"Consumable deduction: {item_code} has {available} on hand in "
                f"{warehouse} for {invoice_name}; line dropped rather than "
                f"blocking the other consumables.",
            )
            continue

        covered_qty = min(qty, available)
        if covered_qty < qty:
            _log(
                "consumable_deduction: clamped to available stock",
                f"Consumable deduction: {item_code} clamped to {covered_qty} "
                f"(requested {qty}, {available} available in {warehouse}) for "
                f"{invoice_name}.",
            )

        uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
        lines.append({
            "item_code": item_code,
            "qty": covered_qty,
            "uom": uom,
            "s_warehouse": warehouse,
        })
    return lines


def _savepoint_name(invoice_name: str) -> str:
    """A savepoint identifier safe for any SQL dialect, derived from the invoice name.

    Sales Invoice names contain dashes (``ACC-SINV-2026-00123``), which are not
    safe as a bare SQL identifier, so the name is hashed rather than embedded —
    same approach as ``api.manufacturing._build_submit_savepoint_name`` and
    ``services.invoice_return``'s per-request savepoint.
    """
    token = hashlib.sha1(str(invoice_name or "").encode("utf-8")).hexdigest()[:12]
    return f"consumable_deduction_{token}"


def _create_material_issue(
    *,
    invoice_name: str,
    company: str,
    couvert_qty: float,
    bag_qty: int,
    nylon_qty: int,
) -> Optional[str]:
    """Build, insert, and submit a Material Issue Stock Entry. Returns the SE name.

    Returns ``None`` (never raises for this case) when none of the three
    requested lines could be covered by real stock in their resolved
    warehouse — see ``_build_coverable_lines``.

    Any OTHER failure during insert/submit is rolled back to a savepoint taken
    immediately before the Stock Entry is created, so a mid-batch validation
    error can never leave a submitted-but-broken Stock Entry (partial Stock
    Ledger Entries, zero GL Entries) behind — exactly the state 168 orphaned
    Stock Entries were found in on production. The exception is re-raised
    after rollback so the caller's existing log-and-alert handling still
    fires; the rollback itself is defensive so a failure to roll back can
    never mask the original error.
    """
    requested = [
        (_COUVERT_ITEM, couvert_qty),
        (_COLORED_BAG_ITEM, bag_qty),
        (_NYLON_BAG_ITEM, nylon_qty),
    ]
    lines = _build_coverable_lines(company, requested, invoice_name=invoice_name)
    if not lines:
        _log(
            "consumable_deduction: nothing coverable",
            f"Consumable deduction skipped for {invoice_name}: no requested line "
            f"could be covered by available stock.",
        )
        return None

    savepoint = _savepoint_name(invoice_name)
    try:
        frappe.db.savepoint(savepoint)
    except Exception:
        savepoint = ""

    try:
        se = frappe.new_doc("Stock Entry")
        # Stock Entry.company is reqd=1. Leaving it unset makes frappe.new_doc
        # fall back to the SESSION USER's default company, which is a different
        # question from "which company is this invoice for". Two ways that bites,
        # both ending in the same alert-only zero deduction this fix exists to
        # end: no default set for the acting user -> MandatoryError on insert;
        # or a default that differs from the invoice's company -> the warehouse
        # was routed for the invoice's company but is validated against another,
        # and ERPNext throws "Warehouse does not belong to Company". The whole
        # premise here is that the warehouse now matches -- so the company that
        # it was matched FOR has to be stated outright.
        se.company = company
        se.stock_entry_type = "Material Issue"
        se.posting_date = frappe.utils.today()
        se.set_posting_time = 1
        se.remarks = f"Auto consumable deduction for Sales Invoice {invoice_name}"

        for line in lines:
            se.append("items", line)

        se.flags.ignore_permissions = True
        se.insert()
        se.flags.ignore_permissions = True
        se.submit()

        # Persist the link the moment the Stock Entry exists, inside this same
        # guarded block. The pre-fix code set this AFTER returning from here,
        # so every one of the 167 production failures (which raised inside
        # submit()'s update_stock_ledger) never wrote it at all — which is
        # exactly why reverse_consumable_deduction_on_cancel could never find
        # an SE to reverse.
        frappe.db.set_value(
            "Sales Invoice",
            invoice_name,
            "custom_consumable_stock_entry",
            se.name,
            update_modified=False,
        )
        return se.name
    except Exception:
        if savepoint:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                # A failed rollback must not mask the original error re-raised below.
                pass
        raise


def _notify_deduction_failure(doc: Any, *, reason: str) -> None:
    """Surface a failed/partial consumable deduction to a human.

    A bare ``frappe.log_error`` is why 167/167 production attempts failed for
    11 days completely unnoticed — nobody watches the Error Log. This mirrors
    the two operator-alert conventions already established in this app rather
    than inventing a third:

    * ``services.label_stock.run_label_stock_alerts`` — one ``Notification
      Log`` row per recipient (``type="Alert"``, linked to the source
      document).
    * ``utils.realtime.publish_invoice_event`` / ``publish_to_branches`` —
      realtime events addressed to the users assigned to the invoice's branch
      POS Profile, never a bare ``publish_realtime`` (falls through to the
      site-wide ``all`` room) and never ``user="*"`` (addresses a room nobody
      joins — see the module docstring in ``utils/realtime.py``).

    Never raises, and deliberately never ``frappe.throw``: a failed or
    partial deduction must not be allowed to block the Out for Delivery
    transition it hangs off of — the order still has to go out.
    """
    if not frappe or not doc or not getattr(doc, "name", None):
        return
    try:
        from jarz_pos.utils.access_control import get_invoice_branch, get_users_for_pos_profiles
        from jarz_pos.utils.realtime import publish_invoice_event

        branch = get_invoice_branch(doc)
        recipients = get_users_for_pos_profiles([branch]) if branch else []

        subject = f"Consumable stock deduction failed: {doc.name}"
        message = f"{reason} No consumables were deducted for this delivery — check stock manually."

        for user in recipients:
            try:
                note = frappe.new_doc("Notification Log")
                note.subject = subject
                note.email_content = message
                note.for_user = user
                note.type = "Alert"
                note.document_type = "Sales Invoice"
                note.document_name = doc.name
                note.flags.ignore_permissions = True
                note.insert(ignore_permissions=True)
            except Exception:
                continue

        try:
            from jarz_pos.constants import WS_EVENTS

            event = getattr(WS_EVENTS, "CONSUMABLE_DEDUCTION_FAILED", "jarz_pos_consumable_deduction_failed")
        except Exception:
            event = "jarz_pos_consumable_deduction_failed"

        publish_invoice_event(
            event,
            {"invoice": doc.name, "reason": reason},
            doc,
        )
    except Exception:
        _log(
            f"consumable_deduction: failure alert itself failed for {getattr(doc, 'name', '?')}",
            frappe.get_traceback(),
        )


def _cancel_se_if_submitted(se_name: str, *, invoice_name: str) -> None:
    """Cancel a submitted Stock Entry; warn if already cancelled; skip if not found."""
    docstatus = frappe.db.get_value("Stock Entry", se_name, "docstatus")
    if docstatus is None:
        _log(
            "consumable_deduction: SE not found",
            f"Consumable SE {se_name} not found while reversing {invoice_name}.",
        )
        return
    if int(docstatus) == 2:
        # Already cancelled — nothing to do
        return
    if int(docstatus) != 1:
        _log(
            "consumable_deduction: unexpected SE status",
            f"Consumable SE {se_name} is in unexpected docstatus {docstatus} for {invoice_name}.",
        )
        return

    se = frappe.get_doc("Stock Entry", se_name)
    se.flags.ignore_permissions = True
    se.cancel()

"""
Automatic consumable stock deduction when a Sales Invoice transitions to "Out for Delivery".

Items deducted per order:
  - covier          : 1 per medium or large jar
  - colored bag     : ceil(large_qty / 5 + medium_qty / 7)
  - Nylon Inside bag: same count as colored bag

Stock is deducted from the warehouse linked to the invoice's kanban (POS) profile.
A Material Issue Stock Entry is created and its name stored on the invoice so it can
be cancelled if the invoice is later cancelled.

These handlers are registered in hooks.py doc_events["Sales Invoice"].
"""

from __future__ import annotations

import math
from typing import Any, Optional

try:
    import frappe
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

_COUVERT_ITEM = "covier"
_COLORED_BAG_ITEM = "colored bag"
_NYLON_BAG_ITEM = "Nylon Inside bag"

_MEDIUM_GROUPS = {"Medium", "Meduim"}  # second spelling is a known data typo
_LARGE_GROUPS = {"Large"}


# ---------------------------------------------------------------------------
# Public hook handlers
# ---------------------------------------------------------------------------

def deduct_consumables_on_ofd(doc: Any, method: Optional[str] = None) -> None:
    """Create a Material Issue SE for consumables when the invoice first goes Out for Delivery.

    Safe no-op on every subsequent save — guarded by custom_was_out_for_delivery.
    Errors are logged but never re-raised so they cannot block the OFD state transition.
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

        warehouse = _get_warehouse(doc)
        if not warehouse:
            frappe.log_error(
                f"Consumable deduction skipped for {doc.name}: could not resolve warehouse.",
                "consumable_deduction: missing warehouse",
            )
            return

        se_name = _create_material_issue(
            invoice_name=doc.name,
            warehouse=warehouse,
            couvert_qty=couvert_qty,
            bag_qty=bag_qty,
            nylon_qty=nylon_qty,
        )

        frappe.db.set_value(
            "Sales Invoice",
            doc.name,
            "custom_consumable_stock_entry",
            se_name,
            update_modified=False,
        )
        doc.custom_consumable_stock_entry = se_name

    except Exception:
        frappe.log_error(frappe.get_traceback(), f"consumable_deduction: deduct failed for {getattr(doc, 'name', '?')}")


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
        frappe.log_error(frappe.get_traceback(), f"consumable_deduction: reverse failed for {getattr(doc, 'name', '?')}")


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


def _get_warehouse(doc: Any) -> str:
    """Resolve the warehouse from the invoice's kanban (POS) profile."""
    kanban_profile = str(getattr(doc, "custom_kanban_profile", "") or "").strip()
    pos_profile = str(getattr(doc, "pos_profile", "") or "").strip()

    for profile in filter(None, [kanban_profile, pos_profile]):
        wh = str(frappe.db.get_value("POS Profile", profile, "warehouse") or "").strip()
        if wh:
            return wh
    return ""


def _create_material_issue(
    *,
    invoice_name: str,
    warehouse: str,
    couvert_qty: float,
    bag_qty: int,
    nylon_qty: int,
) -> str:
    """Build, insert, and submit a Material Issue Stock Entry. Returns the SE name."""
    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Issue"
    se.posting_date = frappe.utils.today()
    se.set_posting_time = 1
    se.remarks = f"Auto consumable deduction for Sales Invoice {invoice_name}"

    for item_code, qty in [
        (_COUVERT_ITEM, couvert_qty),
        (_COLORED_BAG_ITEM, bag_qty),
        (_NYLON_BAG_ITEM, nylon_qty),
    ]:
        uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
        se.append("items", {
            "item_code": item_code,
            "qty": qty,
            "uom": uom,
            "s_warehouse": warehouse,
        })

    se.flags.ignore_permissions = True
    se.insert()
    se.flags.ignore_permissions = True
    se.submit()
    return se.name


def _cancel_se_if_submitted(se_name: str, *, invoice_name: str) -> None:
    """Cancel a submitted Stock Entry; warn if already cancelled; skip if not found."""
    docstatus = frappe.db.get_value("Stock Entry", se_name, "docstatus")
    if docstatus is None:
        frappe.log_error(
            f"Consumable SE {se_name} not found while reversing {invoice_name}.",
            "consumable_deduction: SE not found",
        )
        return
    if int(docstatus) == 2:
        # Already cancelled — nothing to do
        return
    if int(docstatus) != 1:
        frappe.log_error(
            f"Consumable SE {se_name} is in unexpected docstatus {docstatus} for {invoice_name}.",
            "consumable_deduction: unexpected SE status",
        )
        return

    se = frappe.get_doc("Stock Entry", se_name)
    se.flags.ignore_permissions = True
    se.cancel()

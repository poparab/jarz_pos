from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe import _


def _ensure_manager_access() -> None:
    roles = set(frappe.get_roles())
    allowed = {"System Manager", "Stock Manager", "Manufacturing Manager", "Purchase Manager", "Accounts Manager"}
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


@frappe.whitelist()
def list_pos_profiles() -> list[dict[str, Any]]:
    """Return POS Profiles with their connected warehouses."""
    _ensure_manager_access()
    rows = frappe.get_all(
        "POS Profile",
        filters={"disabled": 0},
        fields=["name", "company", "warehouse"],
        order_by="name asc",
    )
    # Normalize field names
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "name": r.get("name"),
            "company": r.get("company"),
            "warehouse": r.get("warehouse"),
        })
    return out


@frappe.whitelist()
def list_item_groups(search: str | None = None, only_leaf: int = 1, limit: int = 200) -> list[dict[str, Any]]:
    _ensure_manager_access()
    filters: dict[str, Any] = {}
    if int(only_leaf or 0):
        filters["is_group"] = 0
    or_filters: list[Any] = []
    if search:
        like = f"%{search}%"
        or_filters = [["Item Group", "name", "like", like]]
    fields = ["name", "parent_item_group", "is_group"]
    return frappe.get_all("Item Group", filters=filters, or_filters=or_filters, fields=fields, order_by="name asc", limit=limit)


def _sum_bin_quantities(warehouse: str, item_codes: list[str]) -> dict[str, float]:
    if not item_codes:
        return {}
    placeholders = ",".join(["%s"] * len(item_codes))
    sql = f"""
        SELECT b.item_code, COALESCE(SUM(b.actual_qty), 0) AS qty
        FROM `tabBin` b
        WHERE b.warehouse = %s AND b.item_code IN ({placeholders})
        GROUP BY b.item_code
    """
    args = [warehouse, *item_codes]
    rows = frappe.db.sql(sql, args, as_dict=True)  # type: ignore
    out: dict[str, float] = {}
    for r in rows:
        out[str(r.get("item_code"))] = float(r.get("qty") or 0)
    return out


def _sum_reserved_from_sinv(warehouse: str, item_codes: list[str]) -> dict[str, float]:
    """Approximate 'reserved' from submitted Sales Invoices not yet delivered.

    We treat reserved as (qty - delivered_qty) for Sales Invoice Items where:
      - parent docstatus=1 and is_return=0
      - update_stock=0 (stock not affected yet)
      - sii.warehouse = target warehouse
    """
    if not item_codes:
        return {}
    placeholders = ",".join(["%s"] * len(item_codes))
    sql = f"""
        SELECT sii.item_code, COALESCE(SUM(sii.qty - COALESCE(sii.delivered_qty, 0)), 0) AS reserved
        FROM `tabSales Invoice Item` sii
        INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND COALESCE(si.is_return, 0) = 0
          AND COALESCE(si.update_stock, 0) = 0
          AND sii.warehouse = %s
          AND sii.item_code IN ({placeholders})
          AND COALESCE(sii.qty, 0) > COALESCE(sii.delivered_qty, 0)
        GROUP BY sii.item_code
    """
    args = [warehouse, *item_codes]
    rows = frappe.db.sql(sql, args, as_dict=True)  # type: ignore
    out: dict[str, float] = {}
    for r in rows:
        out[str(r.get("item_code"))] = float(r.get("reserved") or 0)
    return out


@frappe.whitelist()
def search_items_with_stock(
    source_warehouse: str,
    target_warehouse: str,
    search: str | None = None,
    item_group: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    _ensure_manager_access()
    if not source_warehouse or not target_warehouse:
        frappe.throw(_("Both source_warehouse and target_warehouse are required"))
    if source_warehouse == target_warehouse:
        frappe.throw(_("Source and Target warehouses must be different"))

    filters: dict[str, Any] = {
        "disabled": 0,
        "has_variants": 0,
    }
    or_filters: list[Any] = []
    if item_group:
        filters["item_group"] = item_group
    if search:
        like = f"%{search}%"
        or_filters = [["Item", "name", "like", like], ["Item", "item_name", "like", like]]
    # Select fields dynamically to tolerate installations without POS extension field
    fields = ["name as item_code", "item_name", "item_group", "stock_uom"]
    try:
        if frappe.db.has_column("Item", "include_item_in_pos"):
            fields.append("include_item_in_pos")
    except Exception:
        # If introspection fails, proceed without the optional column
        pass
    items = frappe.get_all("Item", filters=filters, or_filters=or_filters, fields=fields, limit=limit, order_by="modified desc")
    codes = [it["item_code"] for it in items]

    src_qty = _sum_bin_quantities(source_warehouse, codes)
    dst_qty = _sum_bin_quantities(target_warehouse, codes)
    reserved_src = _sum_reserved_from_sinv(source_warehouse, [c for c in codes])
    reserved_dst = _sum_reserved_from_sinv(target_warehouse, [c for c in codes])

    out: list[dict[str, Any]] = []
    for it in items:
        code = it["item_code"]
        out.append({
            "item_code": code,
            "item_name": it.get("item_name") or code,
            "item_group": it.get("item_group"),
            "stock_uom": it.get("stock_uom") or "Nos",
            "qty_source": float(src_qty.get(code, 0)),
            "qty_target": float(dst_qty.get(code, 0)),
            "reserved_source": float(reserved_src.get(code, 0)),
            "reserved_target": float(reserved_dst.get(code, 0)),
            # include_item_in_pos may not exist in some setups
            "pos_item": int((it.get("include_item_in_pos") if isinstance(it, dict) else None) or 0),
        })
    return out


@frappe.whitelist()
def submit_transfer(
    source_warehouse: str,
    target_warehouse: str,
    lines: Any,
    posting_date: str | None = None,
) -> dict[str, Any]:
    """Create a Stock Entry (Material Transfer) between warehouses for given items.

    lines: list[{item_code, qty}]
    """
    _ensure_manager_access()
    try:
        if isinstance(lines, str):
            import json
            lines = json.loads(lines)
    except Exception:
        frappe.throw(_("Invalid JSON for lines"))
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("lines must be a non-empty list"))

    if not source_warehouse or not target_warehouse:
        frappe.throw(_("Both source_warehouse and target_warehouse are required"))
    if source_warehouse == target_warehouse:
        frappe.throw(_("Source and Target warehouses must be different"))

    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Material Transfer"
    if posting_date:
        se.posting_date = posting_date
        se.set_posting_time = 1

    for ln in lines:
        if not isinstance(ln, dict):
            frappe.throw(_("Each line must be an object"))
        item_code = ln.get("item_code") or ln.get("item")
        qty = float(ln.get("qty") or 0)
        if not item_code or qty <= 0:
            frappe.throw(_("Invalid item or qty in lines"))
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
        se.append("items", {
            "item_code": item_code,
            "uom": stock_uom,
            "qty": qty,
            "s_warehouse": source_warehouse,
            "t_warehouse": target_warehouse,
        })

    se.flags.ignore_permissions = True
    se.insert()
    se.flags.ignore_permissions = True
    se.submit()
    frappe.db.commit()

    return {"ok": True, "stock_entry": se.name}

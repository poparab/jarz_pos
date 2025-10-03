from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _


def _ensure_manager_access() -> None:
    roles = set(frappe.get_roles())
    allowed = {"System Manager", "Stock Manager", "Manufacturing Manager", "Accounts Manager"}
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _get_uom_conversions(item_code: str) -> List[Dict[str, Any]]:
    """Return available UOM conversions for an item, including stock_uom with factor 1."""
    stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
    convs: List[Dict[str, Any]] = [{"uom": stock_uom, "conversion_factor": 1.0}]
    rows = frappe.get_all(
        "UOM Conversion Detail",
        filters={"parenttype": "Item", "parentfield": "uoms", "parent": item_code},
        fields=["uom", "conversion_factor"],
        order_by="idx asc",
    )
    for r in rows:
        try:
            u = str(r.get("uom"))
            f = float(r.get("conversion_factor") or 1)
        except Exception:
            continue
        if not any(c.get("uom") == u for c in convs):
            convs.append({"uom": u, "conversion_factor": f})
    return convs


@frappe.whitelist()
def list_warehouses(company: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_manager_access()
    if not company:
        company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
    filters: Dict[str, Any] = {"is_group": 0}
    if company:
        filters["company"] = company
    return frappe.get_all("Warehouse", filters=filters, fields=["name", "company"], order_by="name asc")


def _get_bin_qty_map(warehouse: str, item_codes: List[str]) -> Dict[str, float]:
    if not item_codes:
        return {}
    ph = ",".join(["%s"] * len(item_codes))
    sql = f"""
        SELECT b.item_code, COALESCE(SUM(b.actual_qty), 0) AS qty
        FROM `tabBin` b
        WHERE b.warehouse = %s AND b.item_code IN ({ph})
        GROUP BY b.item_code
    """
    rows = frappe.db.sql(sql, [warehouse] + item_codes, as_dict=True)  # type: ignore
    out: Dict[str, float] = {}
    for r in rows:
        out[str(r.get("item_code"))] = float(r.get("qty") or 0)
    return out


@frappe.whitelist()
def list_items_for_count(
    warehouse: str,
    search: Optional[str] = None,
    item_group: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """List items to count in a warehouse with current qty and UOM conversions.

    We include non-variant, enabled items, optionally filtered by group/search.
    """
    _ensure_manager_access()
    if not warehouse:
        frappe.throw(_("warehouse is required"))

    filters: Dict[str, Any] = {"disabled": 0, "has_variants": 0}
    if item_group:
        filters["item_group"] = item_group
    or_filters: List[Any] = []
    if search:
        like = f"%{search}%"
        or_filters = [["Item", "name", "like", like], ["Item", "item_name", "like", like]]
    fields = ["name as item_code", "item_name", "item_group", "stock_uom"]
    items = frappe.get_all("Item", filters=filters, or_filters=or_filters, fields=fields, order_by="modified desc", limit=limit)
    codes = [it["item_code"] for it in items]
    qty_map = _get_bin_qty_map(warehouse, codes)

    out: List[Dict[str, Any]] = []
    for it in items:
        code = it["item_code"]
        stock_uom = it.get("stock_uom") or "Nos"
        out.append({
            "item_code": code,
            "item_name": it.get("item_name") or code,
            "item_group": it.get("item_group"),
            "stock_uom": stock_uom,
            "current_qty": float(qty_map.get(code, 0)),
            "uoms": _get_uom_conversions(code),
        })
    return out


def _to_stock_qty(item_code: str, qty: float, uom: Optional[str]) -> float:
    if qty is None:
        return 0.0
    stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
    if not uom or uom == stock_uom:
        return float(qty)
    # find factor
    rows = frappe.get_all(
        "UOM Conversion Detail",
        filters={"parenttype": "Item", "parentfield": "uoms", "parent": item_code, "uom": uom},
        fields=["conversion_factor"],
        limit=1,
    )
    factor = float(rows[0].get("conversion_factor") if rows else 1)
    return float(qty) * factor


@frappe.whitelist()
def submit_reconciliation(
    warehouse: str,
    posting_date: Optional[str],
    lines: Any,
    enforce_all: int = 1,
) -> Dict[str, Any]:
    """Create a Stock Reconciliation for counted items.

    lines: list[{item_code, counted_qty, uom?}]
    If enforce_all=1, require that each item from list_items_for_count(warehouse) is present in lines.
    Only differences are added to the reconciliation to reduce noise.
    """
    _ensure_manager_access()
    try:
        import json
        if isinstance(lines, str):
            lines = json.loads(lines)
    except Exception:
        frappe.throw(_("Invalid JSON for lines"))
    if not isinstance(lines, list) or not lines:
        frappe.throw(_("lines must be a non-empty list"))
    if not warehouse:
        frappe.throw(_("warehouse is required"))

    # Build a map of counted qty in stock UOM
    counted: Dict[str, float] = {}
    for ln in lines:
        if not isinstance(ln, dict):
            frappe.throw(_("Each line must be an object"))
        code = ln.get("item_code") or ln.get("item")
        if not code:
            frappe.throw(_("Missing item_code in a line"))
        qty = float(ln.get("counted_qty") or ln.get("qty") or 0)
        uom = ln.get("uom")
        counted[code] = _to_stock_qty(code, qty, uom)

    # Enforce all items counted (subset: items matching the search criteria we expose)
    if int(enforce_all or 0):
        expected = list_items_for_count(warehouse=warehouse)  # type: ignore
        expected_codes = {e["item_code"] for e in expected}
        missing = [c for c in expected_codes if c not in counted]
        if missing:
            frappe.throw(_("You must count all items in this warehouse. Missing: {0}").format(", ".join(sorted(missing)[:10]) + (" ..." if len(missing) > 10 else "")))

    # Current quantities
    cur_qty_map = _get_bin_qty_map(warehouse, list(counted.keys()))

    # Create Stock Reconciliation only for differences
    sr = frappe.new_doc("Stock Reconciliation")
    if posting_date:
        sr.posting_date = posting_date
    else:
        from frappe.utils import today
        sr.posting_date = today()
    sr.set_posting_time = 1
    sr.purpose = "Stock Reconciliation"

    diffs = 0
    for code, counted_stock_qty in counted.items():
        current = float(cur_qty_map.get(code, 0))
        if abs(counted_stock_qty - current) < 1e-9:
            continue
        sr.append("items", {
            "item_code": code,
            "warehouse": warehouse,
            "qty": counted_stock_qty,
        })
        diffs += 1

    if diffs == 0:
        return {"ok": True, "stock_reconciliation": None, "message": "No differences found"}

    sr.flags.ignore_permissions = True
    sr.insert()
    sr.flags.ignore_permissions = True
    sr.submit()
    frappe.db.commit()

    return {"ok": True, "stock_reconciliation": sr.name, "differences": diffs}

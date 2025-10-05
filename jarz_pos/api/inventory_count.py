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


@frappe.whitelist()
def list_item_groups(search: Optional[str] = None) -> List[Dict[str, Any]]:
    """List item groups for filtering inventory count"""
    _ensure_manager_access()
    filters: Dict[str, Any] = {"is_group": 0}  # Only leaf nodes
    or_filters: List[Any] = []
    if search:
        like = f"%{search}%"
        or_filters = [["Item Group", "name", "like", like], ["Item Group", "item_group_name", "like", like]]
    
    fields = ["name", "item_group_name"]
    return frappe.get_all("Item Group", filters=filters, or_filters=or_filters, fields=fields, order_by="name asc", limit=100)


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


# Public helper used by both list and submit to infer a plausible valuation rate
def _resolve_item_valuation(item_code: str, warehouse: str) -> Optional[float]:
    try:
        # Latest SLE in this warehouse
        rows = frappe.get_all(
            "Stock Ledger Entry",
            filters={"item_code": item_code, "warehouse": warehouse},
            fields=["valuation_rate"],
            order_by="posting_date desc, posting_time desc, creation desc",
            limit=1,
        )
        if rows and rows[0].get("valuation_rate") is not None:
            return float(rows[0]["valuation_rate"])  # type: ignore
        # Any SLE for the item
        rows_any = frappe.get_all(
            "Stock Ledger Entry",
            filters={"item_code": item_code},
            fields=["valuation_rate"],
            order_by="posting_date desc, posting_time desc, creation desc",
            limit=1,
        )
        if rows_any and rows_any[0].get("valuation_rate") is not None:
            return float(rows_any[0]["valuation_rate"])  # type: ignore
        # last_purchase_rate
        lpr = frappe.db.get_value("Item", item_code, "last_purchase_rate")
        if lpr is not None:
            return float(lpr)
        # Buying Item Price
        ip = frappe.get_all(
            "Item Price",
            filters={"item_code": item_code, "buying": 1},
            fields=["price_list_rate"],
            order_by="modified desc",
            limit=1,
        )
        if ip and ip[0].get("price_list_rate") is not None:
            return float(ip[0]["price_list_rate"])  # type: ignore
        # Fallback: Selling Item Price (as last resort)
        ips = frappe.get_all(
            "Item Price",
            filters={"item_code": item_code, "selling": 1},
            fields=["price_list_rate"],
            order_by="modified desc",
            limit=1,
        )
        if ips and ips[0].get("price_list_rate") is not None:
            return float(ips[0]["price_list_rate"])  # type: ignore
    except Exception:
        pass
    return None


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
    fields = [
        "name as item_code",
        "item_name",
        "item_group",
        "stock_uom",
        "has_batch_no",
        "has_serial_no",
    ]
    items = frappe.get_all("Item", filters=filters, or_filters=or_filters, fields=fields, order_by="modified desc", limit=limit)
    codes = [it["item_code"] for it in items]
    qty_map = _get_bin_qty_map(warehouse, codes)

    out: List[Dict[str, Any]] = []
    for it in items:
        code = it["item_code"]
        stock_uom = it.get("stock_uom") or "Nos"
        # Attempt to provide valuation info for the UI (optional)
        val = _resolve_item_valuation(code, warehouse)  # may be None
        out.append({
            "item_code": code,
            "item_name": it.get("item_name") or code,
            "item_group": it.get("item_group"),
            "stock_uom": stock_uom,
            "has_batch_no": bool(it.get("has_batch_no") or 0),
            "has_serial_no": bool(it.get("has_serial_no") or 0),
            "current_qty": float(qty_map.get(code, 0)),
            "uoms": _get_uom_conversions(code),
            "valuation_rate": val,
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
    provided_vr: Dict[str, Optional[float]] = {}
    provided_batch: Dict[str, Optional[str]] = {}
    provided_serials: Dict[str, Optional[str]] = {}
    for ln in lines:
        if not isinstance(ln, dict):
            frappe.throw(_("Each line must be an object"))
        code = ln.get("item_code") or ln.get("item")
        if not code:
            frappe.throw(_("Missing item_code in a line"))
        qty = float(ln.get("counted_qty") or ln.get("qty") or 0)
        uom = ln.get("uom")
        counted[code] = _to_stock_qty(code, qty, uom)
        # Optional valuation_rate provided by client
        try:
            if ln.get("valuation_rate") is not None:
                provided_vr[str(code)] = float(ln.get("valuation_rate"))
            else:
                provided_vr[str(code)] = None
        except Exception:
            provided_vr[str(code)] = None
        # Optional batch/serial
        bno = ln.get("batch_no")
        provided_batch[str(code)] = str(bno) if bno else None
        sno = ln.get("serial_no") or ln.get("serial_nos")
        provided_serials[str(code)] = str(sno) if sno else None

    # Enforce all items counted (subset: items matching the search criteria we expose)
    if int(enforce_all or 0):
        expected = list_items_for_count(warehouse=warehouse)  # type: ignore
        expected_codes = {e["item_code"] for e in expected}
        missing = [c for c in expected_codes if c not in counted]
        if missing:
            frappe.throw(_("You must count all items in this warehouse. Missing: {0}").format(", ".join(sorted(missing)[:10]) + (" ..." if len(missing) > 10 else "")))

    # Current quantities
    cur_qty_map = _get_bin_qty_map(warehouse, list(counted.keys()))

    # Helper to fetch a sensible valuation rate when ERPNext requires one
    def _resolve_valuation_rate(item_code: str, wh: str) -> Optional[float]:
        # 1) Latest Stock Ledger Entry for this warehouse
        rows = frappe.get_all(
            "Stock Ledger Entry",
            filters={"item_code": item_code, "warehouse": wh},
            fields=["valuation_rate"],
            order_by="posting_date desc, posting_time desc, creation desc",
            limit=1,
        )
        if rows and rows[0].get("valuation_rate") is not None:
            try:
                return float(rows[0]["valuation_rate"])  # type: ignore
            except Exception:
                pass

        # 2) Any SLE for the item (other warehouses)
        rows_any = frappe.get_all(
            "Stock Ledger Entry",
            filters={"item_code": item_code},
            fields=["valuation_rate"],
            order_by="posting_date desc, posting_time desc, creation desc",
            limit=1,
        )
        if rows_any and rows_any[0].get("valuation_rate") is not None:
            try:
                return float(rows_any[0]["valuation_rate"])  # type: ignore
            except Exception:
                pass

        # 3) Item.last_purchase_rate
        lpr = frappe.db.get_value("Item", item_code, "last_purchase_rate")
        try:
            if lpr is not None:
                return float(lpr)
        except Exception:
            pass

        # 4) Any Buying Item Price
        ip = frappe.get_all(
            "Item Price",
            filters={"item_code": item_code, "buying": 1},
            fields=["price_list_rate"],
            order_by="modified desc",
            limit=1,
        )
        if ip and ip[0].get("price_list_rate") is not None:
            try:
                return float(ip[0]["price_list_rate"])  # type: ignore
            except Exception:
                pass
        # 5) Fallback: Selling Item Price
        ips = frappe.get_all(
            "Item Price",
            filters={"item_code": item_code, "selling": 1},
            fields=["price_list_rate"],
            order_by="modified desc",
            limit=1,
        )
        if ips and ips[0].get("price_list_rate") is not None:
            try:
                return float(ips[0]["price_list_rate"])  # type: ignore
            except Exception:
                pass

        return None

    # Create Stock Reconciliation only for differences
    sr = frappe.new_doc("Stock Reconciliation")
    # Set company from warehouse to align with stock rules across environments
    wh_company = frappe.db.get_value("Warehouse", warehouse, "company")
    if wh_company:
        sr.company = wh_company
    if posting_date:
        sr.posting_date = posting_date
    else:
        from frappe.utils import today
        sr.posting_date = today()
    sr.set_posting_time = 1
    sr.purpose = "Stock Reconciliation"

    diffs = 0
    allow_zero_val = bool(frappe.db.get_single_value("Stock Settings", "allow_zero_valuation_rate") or 0)
    for code, counted_stock_qty in counted.items():
        current = float(cur_qty_map.get(code, 0))
        if abs(counted_stock_qty - current) < 1e-9:
            continue
        # If we're increasing stock and ERPNext may require valuation_rate, attempt to provide it
        row: Dict[str, Any] = {
            "item_code": code,
            "warehouse": warehouse,
            "qty": counted_stock_qty,
        }

        is_increase = counted_stock_qty > current
        if is_increase:
            # Prefer client-provided valuation_rate if valid (> 0)
            vr = provided_vr.get(code)
            if vr is not None and float(vr) <= 0:
                vr = None
            if vr is None:
                vr = _resolve_valuation_rate(code, warehouse)
            # If zero or negative, treat as missing unless zero valuation is allowed
            if vr is not None and float(vr) <= 0:
                vr = None if not allow_zero_val else 0.0
            if vr is None and allow_zero_val:
                vr = 0.0
            if vr is None:
                # As a last resort, fail with a clear message rather than a generic ValidationError
                frappe.throw(_(
                    "Valuation Rate required for Item {0}. Please set a Buying Item Price or enable 'Allow Zero Valuation Rate' in Stock Settings."
                ).format(code))
            row["valuation_rate"] = float(vr)

        # Handle batch/serial requirements
        has_batch = bool(frappe.db.get_value("Item", code, "has_batch_no") or 0)
        has_serial = bool(frappe.db.get_value("Item", code, "has_serial_no") or 0)
        if has_serial and is_increase:
            # For increases, serial numbers are mandatory and cannot be auto-generated safely here
            serials = provided_serials.get(code)
            if not serials:
                frappe.throw(_(
                    "Serial No is required for Item {0}. Please provide serial_no/serial_nos in the submitted lines."
                ).format(code))
            row["serial_no"] = serials
        if has_batch and is_increase:
            bno = provided_batch.get(code)
            if not bno:
                # Try reuse any existing batch for this item; else create a simple batch
                ex = frappe.get_all("Batch", filters={"item": code}, fields=["name"], limit=1)
                if ex:
                    bno = ex[0]["name"]
                else:
                    from frappe.utils import today
                    b = frappe.new_doc("Batch")
                    b.item = code
                    b.batch_id = f"AUTO-{code}-{today()}"
                    b.insert(ignore_permissions=True)
                    bno = b.name
            row["batch_no"] = bno

        sr.append("items", row)
        diffs += 1

    if diffs == 0:
        return {"ok": True, "stock_reconciliation": None, "message": "No differences found"}

    try:
        sr.flags.ignore_permissions = True
        sr.insert()
        sr.flags.ignore_permissions = True
        sr.submit()
        frappe.db.commit()
    except Exception as e:
        # Log full traceback for diagnostics and raise a concise message to the client
        frappe.log_error(frappe.get_traceback(), "jarz_pos.submit_reconciliation")
        frappe.throw(_(f"Submit reconciliation failed: {e}"))

    return {"ok": True, "stock_reconciliation": sr.name, "differences": diffs}

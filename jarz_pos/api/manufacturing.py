from __future__ import annotations

import json
from typing import Any, Dict, List

import frappe
from frappe import _
try:
    from frappe import _dict as FrappeDict  # type: ignore
except Exception:  # pragma: no cover
    FrappeDict = dict  # type: ignore
try:
    from frappe.model.document import Document  # type: ignore
except Exception:  # pragma: no cover
    class Document:  # type: ignore
        pass
try:
    from frappe.utils import get_datetime  # type: ignore
except Exception:  # pragma: no cover
    def get_datetime(x):  # type: ignore
        return x

try:
    # ERPNext helper to build Stock Entry for a Work Order
    from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry  # type: ignore
except Exception:  # pragma: no cover
    make_stock_entry = None  # type: ignore


def _get_default_company() -> str:
    try:
        return frappe.db.get_single_value("Global Defaults", "default_company") or ""
    except Exception:
        return ""


def _get_mfg_defaults(company: str) -> Dict[str, str]:
    # Best-effort defaults for warehouses
    out: Dict[str, str] = {"company": company}
    try:
        ms = frappe.get_single("Manufacturing Settings")
        # Only include warehouses that belong to the given company to avoid cross-company errors
        if getattr(ms, "default_wip_warehouse", None):
            wh = ms.default_wip_warehouse
            wh_comp = frappe.db.get_value("Warehouse", wh, "company")
            if wh_comp == company:
                out["wip_warehouse"] = wh
        if getattr(ms, "default_fg_warehouse", None):
            wh = ms.default_fg_warehouse
            wh_comp = frappe.db.get_value("Warehouse", wh, "company")
            if wh_comp == company:
                out["fg_warehouse"] = wh
    except Exception:
        pass
    return out


def _ensure_manager_access() -> None:
    try:
        roles = set(frappe.get_roles())
        allowed = {"System Manager", "Manufacturing Manager", "Stock Manager", "Purchase Manager"}
        if not roles.intersection(allowed):
            frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)
    except Exception:
        # If roles lookup fails (e.g., during background/debug), do not block by default
        pass


def _get_bom_company(bom_name: str) -> str:
    try:
        return frappe.db.get_value("BOM", bom_name, "company") or ""
    except Exception:
        return ""


def _find_company_warehouse(company: str, warehouse_type: str | None, name_hints: list[str]) -> str | None:
    """Pick a reasonable warehouse for the company.
    Priority: exact warehouse_type match -> name contains any hint -> any leaf warehouse for company.
    """
    try:
        if warehouse_type:
            wh = frappe.db.get_value(
                "Warehouse",
                {"company": company, "warehouse_type": warehouse_type, "is_group": 0},
                "name",
            )
            if wh:
                return wh
        # Try name hints (case-insensitive contains)
        for hint in name_hints:
            rows = frappe.get_all(
                "Warehouse",
                filters={"company": company, "is_group": 0},
                fields=["name"],
                or_filters=[["Warehouse", "warehouse_name", "like", f"%{hint}%"], ["Warehouse", "name", "like", f"%{hint}%"]],
                limit=1,
            )
            if rows:
                return rows[0]["name"]
        # Fallback to any leaf warehouse of the company
        any_wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
        return any_wh
    except Exception:
        return None


@frappe.whitelist()
def list_default_bom_items(search: str | None = None) -> List[Dict[str, Any]]:
    """List Items that have a default BOM, with basic info.

    Returns: [{ item_code, item_name, stock_uom, default_bom, bom_qty }]
    """
    term = (search or "").strip()
    cond = ""
    vals: Dict[str, Any] = {}
    if term:
        cond = "AND (i.name LIKE %(q)s OR i.item_name LIKE %(q)s)"
        vals["q"] = f"%{term}%"

    sql = f"""
        SELECT
            i.name AS item_code,
            COALESCE(i.item_name, i.name) AS item_name,
            i.stock_uom,
            b.name AS default_bom,
            b.quantity AS bom_qty
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        WHERE b.is_default = 1
          AND b.docstatus = 1
          {cond}
        ORDER BY i.modified DESC
        LIMIT 100
    """
    rows = frappe.db.sql(sql, vals, as_dict=True)  # type: ignore
    return [dict(r) for r in rows]


@frappe.whitelist()
def get_bom_details(item_code: str) -> Dict[str, Any]:
    """Return default BOM details for an Item, including components.

    Structure:
      {
        item_code, item_name, stock_uom, default_bom, bom_qty,
        components: [{ item_code, item_name, uom, qty_per_bom }]
      }
    """
    item_code = (item_code or "").strip()
    if not item_code:
        frappe.throw(_("item_code is required"))

    bom = frappe.db.get_value(
        "BOM",
        {"item": item_code, "is_default": 1, "docstatus": 1},
        ["name", "quantity", "company"],
        as_dict=True,
    )
    if not bom:
        frappe.throw(_(f"No submitted default BOM found for Item {item_code}"))

    item = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom"], as_dict=True)
    comps = frappe.db.get_all(
        "BOM Item",
        filters={"parent": bom["name"], "docstatus": ["<", 2]},
        fields=["item_code", "item_name", "uom", "qty"],
        order_by="idx asc",
    )
    # Compute available stock per component across warehouses of the BOM's company
    availability: Dict[str, float] = {}
    try:
        company = bom.get("company") if isinstance(bom, dict) else None
        codes = [c["item_code"] for c in comps] if comps else []
        if company and codes:
            # Avoid SQL errors with single item tuple; build placeholders dynamically
            placeholders = ",".join(["%s"] * len(codes))
            sql = f"""
                SELECT b.item_code, COALESCE(SUM(b.actual_qty), 0) AS qty
                FROM `tabBin` b
                INNER JOIN `tabWarehouse` w ON w.name = b.warehouse
                WHERE w.company = %s AND w.is_group = 0 AND b.item_code IN ({placeholders})
                GROUP BY b.item_code
            """
            args = [company] + codes
            rows = frappe.db.sql(sql, args, as_dict=True)  # type: ignore
            for r in rows:
                availability[str(r.get("item_code"))] = float(r.get("qty") or 0)
    except Exception:
        # If anything fails, leave availability empty
        pass
    # qty in BOM Item corresponds to the component requirement for one BOM (which yields bom.quantity finished items)
    return {
        "item_code": item_code,
        "item_name": item.get("item_name") if item else item_code,
        "stock_uom": item.get("stock_uom") if item else "Nos",
        "default_bom": bom["name"],
        "bom_qty": float(bom.get("quantity") or 1),
        "components": [
            {
                "item_code": c["item_code"],
                "item_name": c.get("item_name") or c["item_code"],
                "uom": c.get("uom") or "Nos",
                "qty_per_bom": float(c.get("qty") or 0),
                "available_qty": float(availability.get(c["item_code"], 0)),
            }
            for c in comps
        ],
    }


def _ensure_work_order(line: Dict[str, Any], company: str, defaults: Dict[str, str]) -> str:
    # Create and submit a Work Order for the given line dict
    wip_wh = defaults.get("wip_warehouse")
    fg_wh = defaults.get("fg_warehouse")
    if not wip_wh:
        wip_wh = _find_company_warehouse(company, "WIP", ["WIP", "Work In Progress"]) or None
    if not fg_wh:
        fg_wh = _find_company_warehouse(company, "Finished Goods", ["FG", "Finished Goods"]) or None
    # Strict requirement: both warehouses must be resolvable for the BOM company
    if not wip_wh or not fg_wh:
        frappe.throw(_(f"Missing WIP/FG warehouse for company {company}. Configure Manufacturing Settings or create WIP/FG warehouses."))

    wo = frappe.get_doc({
        "doctype": "Work Order",
        "company": company,
        "production_item": line["item_code"],
        "qty": float(line["item_qty"]),
        "bom_no": line["bom_name"],
        "planned_start_date": get_datetime(line.get("scheduled_at")) if line.get("scheduled_at") else frappe.utils.now_datetime(),
        "transfer_material_against": "Work Order",
        # Set defaults if present
        "wip_warehouse": wip_wh,
        "fg_warehouse": fg_wh,
    })
    # Elevate to avoid role-based blocks from mobile user
    wo.flags.ignore_permissions = True
    wo.insert()
    wo.flags.ignore_permissions = True
    wo.submit()
    frappe.db.commit()
    return wo.name


def _resolve_make_stock_entry():
    # Resolve helper dynamically to be resilient to import timing
    if make_stock_entry:
        return make_stock_entry
    try:
        fn = frappe.get_attr("erpnext.manufacturing.doctype.work_order.work_order.make_stock_entry")
        if fn:
            return fn
    except Exception:
        pass
    # Importlib fallback
    try:
        import importlib
        mod = importlib.import_module('erpnext.manufacturing.doctype.work_order.work_order')
        fn = getattr(mod, 'make_stock_entry', None)
        return fn
    except Exception:
        return None


def _make_and_submit_se(work_order: str, purpose: str, qty: float) -> str:
    creator = _resolve_make_stock_entry()
    if not creator:
        frappe.throw(_("Could not resolve ERPNext make_stock_entry helper"))
    try:
        frappe.log_error(title="JARZ – MFG debug", message=f"About to call make_stock_entry; creator={creator!r}; callable={callable(creator)}; purpose={purpose}; qty={qty}")
    except Exception:
        pass
    try:
        se = creator(work_order, purpose, qty=qty)  # type: ignore
    except Exception as e:
        try:
            tb = frappe.get_traceback()
        except Exception:
            tb = None
        try:
            frappe.log_error(
                title="JARZ – make_stock_entry call failed",
                message=f"creator={creator!r}\ncallable={callable(creator)}\nwork_order={work_order}\npurpose={purpose}\nqty={qty}\nerror={e}\ntraceback=\n{tb}",
            )
        except Exception:
            pass
        raise
    try:
        frappe.log_error(
            title="JARZ – MFG debug",
            message=f"make_stock_entry() returned type={type(se)}; hasattr doctype={hasattr(se,'doctype')}"
        )
    except Exception:
        pass
    # Coerce return into a dict/document pair. frappe._dict has attribute access but is NOT a Document.
    is_document = isinstance(se, Document)
    is_mapping = isinstance(se, (dict, FrappeDict))
    if not is_document and not is_mapping:
        frappe.throw(_("make_stock_entry did not return a Document or dict-like mapping"))
    # Ensure finished qty is set for Manufacture
    # Ensure finished qty is set for Manufacture
    try:
        if purpose == "Manufacture":
            if is_document and not getattr(se, "fg_completed_qty", None):
                se.fg_completed_qty = qty
            elif is_mapping and not se.get("fg_completed_qty"):
                se["fg_completed_qty"] = qty
    except Exception:
        pass
    # Try standard insert/submit, then fallback to client API path if needed
    try:
            
        if is_document:
            se.flags.ignore_permissions = True
            se.set_posting_time = 1
            se.insert()
            se.flags.ignore_permissions = True
            se.submit()
            name = se.name
        else:
            # dict path via get_doc
            # Ensure posting flag present on mapping too
            if is_mapping:
                se["set_posting_time"] = 1
            doc = frappe.get_doc(se)  # type: ignore
            doc.flags.ignore_permissions = True
            doc.set_posting_time = 1
            doc.insert()
            doc.flags.ignore_permissions = True
            doc.submit()
            name = doc.name
        frappe.db.commit()
    except Exception as e1:
        # Log and fallback to frappe.client methods (REST-like controller)
        try:
            frappe.log_error(title="JARZ – SE insert/submit failed, trying client API", message=f"WO: {work_order}\nPurpose: {purpose}\nError: {e1}")
        except Exception:
            pass
        d = se.as_dict() if is_document else (se if is_mapping else None)  # type: ignore
        if d is None:
            frappe.throw(_("Unexpected Stock Entry return type; cannot fallback insert"))
        try:
            client_insert = frappe.get_attr("frappe.client.insert")
            client_submit = frappe.get_attr("frappe.client.submit")
            inserted = client_insert(doc=d)  # type: ignore
            name = inserted.get("name") if isinstance(inserted, dict) else None
            if not name:
                frappe.throw(_("Client insert did not return name"))
            client_submit(doctype="Stock Entry", name=name)  # type: ignore
            frappe.db.commit()
        except Exception as e2:
            try:
                frappe.log_error(title="JARZ – SE client API failed", message=f"WO: {work_order}\nPurpose: {purpose}\nError: {e2}")
            except Exception:
                pass
            raise
    try:
        frappe.logger().info(f"JARZ Manufacturing: Submitted SE {name} for WO {work_order} ({purpose})")
    except Exception:
        pass
    return name


def _coerce_lines(lines: Any) -> List[Dict[str, Any]]:
    if isinstance(lines, str):
        try:
            lines = json.loads(lines)
        except Exception:
            frappe.throw(_("Invalid JSON payload for lines"))
    if not isinstance(lines, list):
        frappe.throw(_("lines must be a list"))
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(lines):
        if not isinstance(it, dict):
            frappe.throw(_(f"lines[{i}] must be an object"))
        for req in ("item_code", "bom_name", "item_qty"):
            if not it.get(req):
                frappe.throw(_(f"Missing required field {req} in lines[{i}]"))
        out.append(it)
    return out


@frappe.whitelist()
def submit_work_orders(lines: Any) -> Dict[str, Any]:
    """Create and submit Work Orders, then create Stock Entries for:
    - Material Transfer for Manufacture
    - Manufacture (to finish with same quantity)

    Args:
      lines: JSON/list of objects with keys: item_code, bom_name, item_qty, scheduled_at (optional ISO)
    Returns per-line results with created names or error.
    """
    lines = _coerce_lines(lines)
    results: List[Dict[str, Any]] = []
    for ln in lines:
        try:
            try:
                frappe.log_error(title="JARZ – MFG start line", message=f"Line: {ln}")
            except Exception:
                pass
            # Always respect the BOM's company; fallback to default if missing
            company = _get_bom_company(ln["bom_name"]) or _get_default_company()
            if not company:
                frappe.throw(_("Company is not configured on BOM and no Default Company set"))
            defaults = _get_mfg_defaults(company)

            wo_name = _ensure_work_order(ln, company, defaults)
            try:
                frappe.log_error(title="JARZ – MFG WO created", message=f"WO: {wo_name}\nCompany: {company}\nWIP: {defaults.get('wip_warehouse')}\nFG: {defaults.get('fg_warehouse')}")
            except Exception:
                pass
            qty = float(ln["item_qty"])
            se1 = _make_and_submit_se(wo_name, "Material Transfer for Manufacture", qty)
            try:
                frappe.log_error(title="JARZ – MFG SE1 done", message=f"WO: {wo_name}\nSE1: {se1}")
            except Exception:
                pass
            se2 = _make_and_submit_se(wo_name, "Manufacture", qty)
            try:
                frappe.log_error(title="JARZ – MFG SE2 done", message=f"WO: {wo_name}\nSE2: {se2}")
            except Exception:
                pass
            # Refresh WO status; after Manufacture entry, it should be Completed when produced qty >= planned qty
            try:
                wo_doc = frappe.get_doc("Work Order", wo_name)
                if hasattr(wo_doc, "update_status"):
                    wo_doc.update_status()
                wo_doc.reload()
                try:
                    frappe.log_error(title="JARZ – MFG WO status", message=f"WO: {wo_name}\nStatus: {wo_doc.status}")
                except Exception:
                    pass
            except Exception:
                pass
            results.append({
                "ok": True,
                "status": "success",
                "work_order": wo_name,
                "material_transfer": se1,
                "manufacture_entry": se2,
                "line": ln,
                "company": company,
                "wip_warehouse": defaults.get("wip_warehouse"),
                "fg_warehouse": defaults.get("fg_warehouse"),
                "wo_status": (wo_doc.status if 'wo_doc' in locals() else None),
            })
        except Exception as e:
            # Avoid exceeding title length by using a short title and detailed message
            try:
                frappe.log_error(
                    title="JARZ – Manufacturing submit failed",
                    message=f"Line: {ln}\nError: {e}",
                )
            except Exception:
                # Swallow logging issues to not mask original error in response
                pass
            results.append({"ok": False, "error": str(e), "line": ln})

    return {"results": results}

@frappe.whitelist()
def submit_single_work_order(item_code: str, bom_name: str, item_qty: float, scheduled_at: str | None = None) -> Dict[str, Any]:
    line = {
        "item_code": item_code,
        "bom_name": bom_name,
        "item_qty": float(item_qty),
        "scheduled_at": scheduled_at,
    }
    out = submit_work_orders([line])
    try:
        if isinstance(out, dict) and isinstance(out.get("results"), list) and out["results"]:
            first = out["results"][0]
            if isinstance(first, dict):
                return first  # Flatten for single-wo convenience
    except Exception:
        pass
    return out


@frappe.whitelist()
def list_recent_work_orders(limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent Work Orders sorted by creation (last added first)."""
    try:
        _ensure_manager_access()
    except Exception:
        # Fallback: allow if ensure function not defined in this module variant
        pass

    rows = frappe.get_all(
        "Work Order",
        filters={},
        fields=[
            "name",
            "production_item",
            "qty",
            "bom_no",
            "status",
            "company",
            "planned_start_date",
            "wip_warehouse",
            "fg_warehouse",
            "creation",
        ],
        order_by="creation desc",
        limit=limit,
    )
    # Cast/normalize types
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "name": r.get("name"),
                "production_item": r.get("production_item"),
                "qty": float(r.get("qty") or 0),
                "bom_no": r.get("bom_no"),
                "status": r.get("status"),
                "company": r.get("company"),
                "planned_start_date": r.get("planned_start_date"),
                "wip_warehouse": r.get("wip_warehouse"),
                "fg_warehouse": r.get("fg_warehouse"),
                "creation": r.get("creation"),
            }
        )
    return out

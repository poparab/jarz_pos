from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import frappe
from frappe import _
from frappe.utils import cint, getdate
from jarz_pos.constants import DEFAULT_UOM, ROLES


def _ensure_transfer_access() -> None:
    """Gate every stock-transfer endpoint on ``ROLES.STOCK_TRANSFER``.

    That set is ``ROLES.MANAGER`` plus the line-manager tier: moving stock
    between a branch and Finished Goods is floor-supervisor work, and gating it
    on ``ROLES.MANAGER`` alone left the line manager with the drawer entry
    hidden and a "Not permitted" on every call.  Cash Transfer and the Purchase
    Invoice keep the narrower ``ROLES.MANAGER`` set — they commit money.
    """
    roles = set(frappe.get_roles())
    allowed = ROLES.STOCK_TRANSFER
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _get_singleton_value(doctype: str, field: str) -> Optional[str]:
    rows = frappe.db.sql(
        """
        SELECT value
        FROM `tabSingles`
        WHERE doctype = %s AND field = %s
        LIMIT 1
        """,
        (doctype, field),
        as_dict=True,
    )
    return rows[0].get("value") if rows else None


def _append_transfer_warehouse_option(
    out: List[Dict[str, Any]],
    *,
    name: str,
    warehouse: Optional[str],
) -> None:
    warehouse = (warehouse or "").strip()
    if not warehouse:
        return
    if any((row.get("warehouse") or "").strip() == warehouse for row in out):
        return
    company = frappe.db.get_value("Warehouse", warehouse, "company")
    out.append({
        "name": name,
        "company": company,
        "warehouse": warehouse,
    })


@frappe.whitelist()
def list_pos_profiles() -> List[Dict[str, Any]]:
    """Return stock transfer options backed by warehouses.

    The stock transfer screen is centered on POS branches, but some warehouse-only
    destinations such as Finished Goods must also be selectable.
    """
    _ensure_transfer_access()
    rows = frappe.get_all(
        "POS Profile",
        filters={"disabled": 0},
        fields=["name", "company", "warehouse"],
        order_by="name asc",
    )
    # Normalize field names
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "name": r.get("name"),
            "company": r.get("company"),
            "warehouse": r.get("warehouse"),
        })
    _append_transfer_warehouse_option(
        out,
        name=_("Finished Goods"),
        warehouse=_get_singleton_value("Manufacturing Settings", "default_fg_warehouse"),
    )
    out.sort(key=lambda row: ((row.get("name") or "").lower(), (row.get("warehouse") or "").lower()))
    return out


@frappe.whitelist()
def list_item_groups(search: Optional[str] = None, only_leaf: int = 1, limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_transfer_access()
    filters: Dict[str, Any] = {}
    if int(only_leaf or 0):
        filters["is_group"] = 0
    or_filters: List[Any] = []
    if search:
        like = f"%{search}%"
        or_filters = [["Item Group", "name", "like", like]]
    fields = ["name", "parent_item_group", "is_group"]
    return frappe.get_all("Item Group", filters=filters, or_filters=or_filters, fields=fields, order_by="name asc", limit=limit)


def _sum_bin_quantities(warehouse: str, item_codes: List[str]) -> Dict[str, float]:
    if not item_codes:
        return {}
    placeholders = ",".join(["%s"] * len(item_codes))
    sql = f"""
        SELECT b.item_code, COALESCE(SUM(b.actual_qty), 0) AS qty
        FROM `tabBin` b
        WHERE b.warehouse = %s AND b.item_code IN ({placeholders})
        GROUP BY b.item_code
    """
    args = [warehouse] + item_codes
    rows = frappe.db.sql(sql, args, as_dict=True)  # type: ignore
    out: Dict[str, float] = {}
    for r in rows:
        out[str(r.get("item_code"))] = float(r.get("qty") or 0)
    return out


def _sum_reserved_from_sinv(warehouse: str, item_codes: List[str]) -> Dict[str, float]:
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
    args = [warehouse] + item_codes
    rows = frappe.db.sql(sql, args, as_dict=True)  # type: ignore
    out: Dict[str, float] = {}
    for r in rows:
        out[str(r.get("item_code"))] = float(r.get("reserved") or 0)
    return out


@frappe.whitelist()
def search_items_with_stock(
    source_warehouse: str,
    target_warehouse: str,
    search: Optional[str] = None,
    item_group: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    _ensure_transfer_access()
    if not source_warehouse or not target_warehouse:
        frappe.throw(_("Both source_warehouse and target_warehouse are required"))
    if source_warehouse == target_warehouse:
        frappe.throw(_("Source and Target warehouses must be different"))

    filters: Dict[str, Any] = {
        "disabled": 0,
        "has_variants": 0,
    }
    or_filters: List[Any] = []
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

    out: List[Dict[str, Any]] = []
    for it in items:
        code = it["item_code"]
        out.append({
            "item_code": code,
            "item_name": it.get("item_name") or code,
            "item_group": it.get("item_group"),
            "stock_uom": it.get("stock_uom") or DEFAULT_UOM,
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
    posting_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Stock Entry (Material Transfer) between warehouses for given items.

    lines: list[{item_code, qty}]
    """
    _ensure_transfer_access()
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
        stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or DEFAULT_UOM
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


@frappe.whitelist()
def list_transfers(
    limit: Any = 30,
    page: Any = 0,
    source_warehouse: Optional[str] = None,
    target_warehouse: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """Return submitted Material Transfer stock entries — the history view.

    The screen that calls :func:`submit_transfer` had no way to show what it
    had already moved, so a repeated transfer was indistinguishable from a
    first one.  Filters are optional and combine with AND; ``search`` matches
    the Stock Entry name or any item code / item name on it.

    Warehouse filters read the *child* rows rather than the parent, because a
    Stock Entry carries its warehouses per line, not in the header.
    """
    _ensure_transfer_access()
    limit = max(1, min(cint(limit) or 30, 200))
    start = max(0, cint(page)) * limit

    filters: Dict[str, Any] = {
        "docstatus": 1,
        "stock_entry_type": "Material Transfer",
    }
    if from_date and to_date:
        filters["posting_date"] = ["between", [getdate(from_date), getdate(to_date)]]
    elif from_date:
        filters["posting_date"] = [">=", getdate(from_date)]
    elif to_date:
        filters["posting_date"] = ["<=", getdate(to_date)]

    # Narrowing by warehouse or free text means asking the child table which
    # parents qualify first. `None` = "no restriction", which is not the same
    # as an empty set ("nothing matches") — collapsing the two would silently
    # return the unfiltered list.
    allowed_parents: Optional[Set[str]] = None

    def _restrict(names: Set[str]) -> None:
        nonlocal allowed_parents
        allowed_parents = names if allowed_parents is None else (allowed_parents & names)

    child_filters: Dict[str, Any] = {}
    if source_warehouse:
        child_filters["s_warehouse"] = source_warehouse
    if target_warehouse:
        child_filters["t_warehouse"] = target_warehouse
    if child_filters:
        _restrict({
            row["parent"]
            for row in frappe.get_all(
                "Stock Entry Detail",
                filters=child_filters,
                fields=["parent"],
                limit_page_length=0,
            )
        })

    search = (search or "").strip()
    if search:
        like = f"%{search}%"
        by_name = {
            row["name"]
            for row in frappe.get_all(
                "Stock Entry",
                filters={**filters, "name": ["like", like]},
                fields=["name"],
                limit_page_length=0,
            )
        }
        by_item = {
            row["parent"]
            for row in frappe.get_all(
                "Stock Entry Detail",
                or_filters=[
                    ["item_code", "like", like],
                    ["item_name", "like", like],
                ],
                fields=["parent"],
                limit_page_length=0,
            )
        }
        _restrict(by_name | by_item)

    if allowed_parents is not None:
        if not allowed_parents:
            return {"transfers": [], "total": 0}
        filters["name"] = ["in", sorted(allowed_parents)]

    total = frappe.db.count("Stock Entry", filters=filters)
    entries = frappe.get_all(
        "Stock Entry",
        filters=filters,
        fields=[
            "name", "posting_date", "posting_time", "total_outgoing_value",
            "owner", "creation", "remarks",
        ],
        order_by="posting_date desc, posting_time desc, creation desc",
        limit_page_length=limit,
        limit_start=start,
    )

    # One query for every line on the page rather than one per entry.
    names = [e["name"] for e in entries]
    lines_by_parent: Dict[str, List[Dict[str, Any]]] = {name: [] for name in names}
    if names:
        for row in frappe.get_all(
            "Stock Entry Detail",
            filters={"parent": ["in", names]},
            fields=[
                "parent", "item_code", "item_name", "qty", "uom",
                "s_warehouse", "t_warehouse", "basic_rate", "amount",
            ],
            order_by="parent asc, idx asc",
            limit_page_length=0,
        ):
            lines_by_parent.setdefault(row["parent"], []).append(row)

    owners = {e["owner"] for e in entries if e.get("owner")}
    full_names = {
        row["name"]: row.get("full_name") or row["name"]
        for row in frappe.get_all(
            "User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"]
        )
    } if owners else {}

    out: List[Dict[str, Any]] = []
    for entry in entries:
        lines = lines_by_parent.get(entry["name"], [])
        # The header has no warehouses, so the pair shown on the card is
        # derived from the lines. A transfer built by this app is always one
        # pair; a hand-made entry may not be, hence the distinct sets.
        sources = sorted({(ln.get("s_warehouse") or "") for ln in lines} - {""})
        targets = sorted({(ln.get("t_warehouse") or "") for ln in lines} - {""})
        out.append({
            **entry,
            "owner_name": full_names.get(entry.get("owner"), entry.get("owner")),
            "source_warehouse": sources[0] if len(sources) == 1 else None,
            "target_warehouse": targets[0] if len(targets) == 1 else None,
            "source_warehouses": sources,
            "target_warehouses": targets,
            "total_qty": sum(float(ln.get("qty") or 0) for ln in lines),
            "items": lines,
        })

    return {"transfers": out, "total": total}

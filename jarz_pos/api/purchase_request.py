"""Team item requests, built on the standard ERPNext Material Request.

The cycle is: *someone on the floor notices a shortage → raises a request →
the buyer sees every open request rolled up into one buying list → buys, with
freedom to buy more, less, or not at all → the request closes itself.*

No custom request DocType exists here on purpose. ``Material Request`` with
``material_request_type = "Purchase"`` already models all of it, and — crucially
— ERPNext's own status updater
(:mod:`erpnext.accounts.doctype.purchase_invoice.purchase_invoice`, the
``status_updater`` entry keyed on ``material_request_item``) writes
``received_qty`` back onto the request and recomputes ``per_received`` whenever
a Purchase Invoice with ``update_stock=1`` carries the link. That is exactly the
shape of :func:`jarz_pos.api.purchase.create_purchase_invoice`, so partial
fulfilment tracking is free:

===========================  ==================================================
Business state               ERPNext
===========================  ==================================================
Raised by staff              submitted MR, status ``Pending``
Bought some of it            ``Partially Received`` (``per_received`` < 100)
Bought all of it             ``Received``
Rejected / no longer needed  ``Stopped``
===========================  ==================================================

There is deliberately **no approval gate**: a request is submitted the moment it
is raised. The buyer's review happens at purchase time, where they are already
adjusting quantities — an extra approval step only adds a place for requests to
get stuck.
"""
from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate
from typing import Any, Dict, List, Optional, Sequence

from jarz_pos.constants import ROLES
from jarz_pos.utils.access_control import (
    BranchAccessError,
    get_user_pos_profiles,
    is_unrestricted_user,
)
from jarz_pos.utils.warehouse_utils import resolve_request_warehouse
from jarz_pos.utils.settings_utils import single_int

#: Request statuses that still want stock. Anything else is done or abandoned.
OPEN_STATUSES = ("Pending", "Partially Received", "Ordered", "Partially Ordered")


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def _ensure_request_access() -> None:
    """Anyone who can notice a shortage may file one."""
    if not set(frappe.get_roles()).intersection(ROLES.PURCHASE_REQUEST):
        frappe.throw(_("Not permitted to raise item requests"), frappe.PermissionError)


def _ensure_review_access() -> None:
    if not set(frappe.get_roles()).intersection(ROLES.PURCHASE_REQUEST_REVIEW):
        frappe.throw(_("Not permitted to review item requests"), frappe.PermissionError)


def _can_review() -> bool:
    return is_unrestricted_user() or bool(
        set(frappe.get_roles()).intersection(ROLES.PURCHASE_REQUEST_REVIEW)
    )


def _visible_profiles() -> Optional[List[str]]:
    """Branches whose requests the caller may see.

    ``None`` means "no branch filter" — reviewers and Administrator see the whole
    queue, because a buyer purchases for every branch at once.
    """
    if _can_review():
        return None
    return get_user_pos_profiles()


def _coerce_rows(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            frappe.throw(_("Malformed items payload"))
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        frappe.throw(_("Malformed items payload"))
    return [row for row in value if isinstance(row, dict)]


def _settings_int(fieldname: str, default: int) -> int:
    """Int setting, with ``default`` applied only when nobody has ever set it.

    Was ``int(frappe.db.get_single_value(...))``. That helper casts an ``Int``
    through ``cint()``, so an unwritten field arrives as ``0``, ``int(0)``
    succeeds, and the ``except (TypeError, ValueError)`` branch carrying the
    default could never be reached. Every team item request raised without an
    explicit date was therefore dated *today* instead of the declared three days
    out — the requester got no lead time and the buyer got no warning.
    """
    return single_int("Jarz POS Settings", fieldname, default)


def _resolve_company(company: Optional[str] = None) -> str:
    resolved = (
        company
        or frappe.defaults.get_user_default("company")
        or frappe.db.get_single_value("Global Defaults", "default_company")
    )
    if not resolved:
        rows = frappe.get_all("Company", fields=["name"], limit=2)
        if len(rows) == 1:
            resolved = rows[0]["name"]
    if not resolved:
        frappe.throw(_("Default Company not set. Please configure a default Company."))
    return resolved


def _resolve_requester_profile(pos_profile: Optional[str] = None) -> Optional[str]:
    """Branch the request is filed against.

    An explicit profile must be one the caller belongs to; otherwise their first
    assigned branch is used. Reviewers with no branch assignment (e.g. a
    head-office buyer) may file an unattributed request.
    """
    profiles = get_user_pos_profiles()
    requested = (pos_profile or "").strip()
    if requested:
        if profiles and requested not in profiles and not is_unrestricted_user():
            frappe.throw(
                _("You do not have access to branch {0}.").format(requested),
                BranchAccessError,
            )
        return requested
    return profiles[0] if profiles else None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

@frappe.whitelist()
def create_request(
    items: Optional[List[Dict[str, Any]]] = None,
    schedule_date: Optional[str] = None,
    note: Optional[str] = None,
    pos_profile: Optional[str] = None,
    company: Optional[str] = None,
) -> Dict[str, Any]:
    """Raise an item request.

    ``items``: ``[{item_code, qty, uom?, warehouse?}]``. The request is submitted
    immediately — see the module docstring on why there is no approval gate.
    """
    _ensure_request_access()

    rows = _coerce_rows(items)
    if not rows:
        frappe.throw(_("At least one item is required"))

    resolved_company = _resolve_company(company)
    profile = _resolve_requester_profile(pos_profile)

    if schedule_date:
        needed_by = getdate(schedule_date)
        if needed_by < getdate(nowdate()):
            frappe.throw(_("The 'needed by' date cannot be in the past."))
    else:
        needed_by = getdate(add_days(nowdate(), _settings_int("purchase_request_schedule_days", 3)))

    doc = frappe.new_doc("Material Request")
    doc.material_request_type = "Purchase"
    doc.company = resolved_company
    doc.transaction_date = nowdate()
    doc.schedule_date = needed_by
    doc.custom_jarz_pos_profile = profile
    doc.custom_jarz_requested_by_label = _user_label(frappe.session.user)
    if note:
        doc.custom_jarz_note = note

    for row in rows:
        item_code = str(row.get("item_code") or "").strip()
        if not item_code:
            frappe.throw(_("Item code missing in row"))
        qty = flt(row.get("qty") or 0)
        if qty <= 0:
            frappe.throw(_("Quantity must be greater than zero for {0}").format(item_code))

        item = frappe.db.get_value(
            "Item", item_code, ["stock_uom", "item_name", "disabled", "is_purchase_item"], as_dict=True
        )
        if not item:
            frappe.throw(_("Item {0} not found").format(item_code))
        if int(item.get("disabled") or 0):
            frappe.throw(_("Item {0} is disabled and cannot be requested.").format(item_code))
        if not int(item.get("is_purchase_item") or 0):
            frappe.throw(_("Item {0} is not a purchasable item.").format(item_code))

        uom = str(row.get("uom") or "").strip() or item["stock_uom"]
        conversion = _conversion_factor(item_code, uom, item["stock_uom"])

        doc.append("items", {
            "item_code": item_code,
            "item_name": item.get("item_name"),
            "qty": qty,
            "uom": uom,
            "stock_uom": item["stock_uom"],
            "conversion_factor": conversion,
            "schedule_date": needed_by,
            "warehouse": resolve_request_warehouse(
                item_code, resolved_company, row.get("warehouse")
            ),
        })

    doc.flags.ignore_permissions = True
    doc.insert()
    doc.submit()
    doc.reload()

    _notify_reviewers(doc)

    return {"success": True, "request": _serialize_request(doc.as_dict())}


@frappe.whitelist()
def stop_request(name: str, reason: Optional[str] = None) -> Dict[str, Any]:
    """Reject / close a request that will not be bought.

    Uses ERPNext's own ``Stopped`` status rather than cancelling, so the request
    stays on the record with its history intact.
    """
    _ensure_review_access()
    if not name:
        frappe.throw(_("Request name is required"))

    doc = frappe.get_doc("Material Request", name)
    if doc.docstatus != 1:
        frappe.throw(_("Only an open request can be stopped."))
    if doc.status == "Stopped":
        return {"success": True, "request": _serialize_request(doc.as_dict())}

    doc.update_status("Stopped")
    if reason:
        doc.add_comment("Comment", _("Stopped: {0}").format(reason))
    doc.reload()
    return {"success": True, "request": _serialize_request(doc.as_dict())}


@frappe.whitelist()
def reopen_request(name: str) -> Dict[str, Any]:
    """Undo a stop — ERPNext calls this transition ``Submitted``."""
    _ensure_review_access()
    if not name:
        frappe.throw(_("Request name is required"))

    doc = frappe.get_doc("Material Request", name)
    if doc.status != "Stopped":
        frappe.throw(_("Only a stopped request can be reopened."))
    doc.update_status("Submitted")
    doc.reload()
    return {"success": True, "request": _serialize_request(doc.as_dict())}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_requests(
    status: Optional[str] = None,
    pos_profile: Optional[str] = None,
    mine_only: int = 0,
    limit: int = 50,
    page: int = 0,
) -> Dict[str, Any]:
    """Paginated request list, scoped to the branches the caller may see."""
    _ensure_request_access()

    limit = max(1, min(int(limit or 50), 200))
    start = max(0, int(page or 0)) * limit

    filters: Dict[str, Any] = {"material_request_type": "Purchase", "docstatus": 1}
    if status == "open":
        filters["status"] = ["in", list(OPEN_STATUSES)]
    elif status:
        filters["status"] = status
    if int(mine_only or 0):
        filters["owner"] = frappe.session.user

    visible = _visible_profiles()
    if pos_profile:
        if visible is not None and pos_profile not in visible:
            frappe.throw(
                _("You do not have access to branch {0}.").format(pos_profile),
                BranchAccessError,
            )
        filters["custom_jarz_pos_profile"] = pos_profile
    elif visible is not None:
        if not visible:
            return {"requests": [], "total": 0, "can_review": _can_review()}
        filters["custom_jarz_pos_profile"] = ["in", visible]

    rows = frappe.get_all(
        "Material Request",
        filters=filters,
        fields=_REQUEST_FIELDS,
        order_by="transaction_date desc, creation desc",
        limit_page_length=limit,
        limit_start=start,
    )
    total = frappe.db.count("Material Request", filters=filters)

    names = [r["name"] for r in rows]
    lines_by_parent: Dict[str, List[Dict[str, Any]]] = {name: [] for name in names}
    if names:
        for line in frappe.get_all(
            "Material Request Item",
            filters={"parent": ["in", names]},
            fields=_REQUEST_ITEM_FIELDS,
            order_by="parent asc, idx asc",
            limit_page_length=0,
        ):
            lines_by_parent.setdefault(line["parent"], []).append(_serialize_line(line))

    requests = []
    for row in rows:
        payload = _serialize_request(row)
        payload["items"] = lines_by_parent.get(row["name"], [])
        requests.append(payload)

    return {"requests": requests, "total": total, "can_review": _can_review()}


@frappe.whitelist()
def get_open_request_lines(company: Optional[str] = None) -> Dict[str, Any]:
    """The buying list: every outstanding request line, rolled up per item.

    This is the shape real purchasing software buys from — one row per item with
    total outstanding demand — rather than one order per requester. Without the
    roll-up a buyer placing three separate orders for the same item is the
    default behaviour, not the exception.

    Each row also carries what the buyer needs to make a call without leaving
    the screen: what is already on hand, and what the item last cost.
    """
    _ensure_request_access()
    resolved_company = _resolve_company(company)

    filters: Dict[str, Any] = {
        "material_request_type": "Purchase",
        "docstatus": 1,
        "status": ["in", list(OPEN_STATUSES)],
        "company": resolved_company,
    }
    visible = _visible_profiles()
    if visible is not None:
        if not visible:
            return {"lines": [], "company": resolved_company}
        filters["custom_jarz_pos_profile"] = ["in", visible]

    parents = frappe.get_all(
        "Material Request",
        filters=filters,
        fields=["name", "custom_jarz_pos_profile", "custom_jarz_requested_by_label",
                "custom_jarz_note", "schedule_date", "transaction_date", "owner"],
        limit_page_length=0,
    )
    if not parents:
        return {"lines": [], "company": resolved_company}

    parent_map = {p["name"]: p for p in parents}
    rows = frappe.get_all(
        "Material Request Item",
        filters={"parent": ["in", list(parent_map)]},
        fields=_REQUEST_ITEM_FIELDS,
        order_by="parent asc, idx asc",
        limit_page_length=0,
    )

    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        outstanding = flt(row.get("stock_qty") or 0) - flt(row.get("received_qty") or 0)
        if outstanding <= 0:
            continue

        parent = parent_map.get(row["parent"], {})
        item_code = row["item_code"]
        bucket = grouped.setdefault(item_code, {
            "item_code": item_code,
            "item_name": row.get("item_name") or item_code,
            "stock_uom": row.get("stock_uom"),
            "outstanding_qty": 0.0,
            "requested_qty": 0.0,
            "received_qty": 0.0,
            "earliest_needed_by": None,
            "sources": [],
        })
        bucket["outstanding_qty"] += outstanding
        bucket["requested_qty"] += flt(row.get("stock_qty") or 0)
        bucket["received_qty"] += flt(row.get("received_qty") or 0)

        needed_by = row.get("schedule_date") or parent.get("schedule_date")
        current = bucket["earliest_needed_by"]
        if needed_by and (current is None or getdate(needed_by) < getdate(current)):
            bucket["earliest_needed_by"] = needed_by

        bucket["sources"].append({
            "material_request": row["parent"],
            "material_request_item": row["name"],
            "pos_profile": parent.get("custom_jarz_pos_profile"),
            "requested_by": parent.get("custom_jarz_requested_by_label") or parent.get("owner"),
            "note": parent.get("custom_jarz_note"),
            "qty": flt(row.get("qty") or 0),
            "stock_qty": flt(row.get("stock_qty") or 0),
            "received_qty": flt(row.get("received_qty") or 0),
            "outstanding_qty": outstanding,
            "uom": row.get("uom"),
            "conversion_factor": flt(row.get("conversion_factor") or 1),
            "warehouse": row.get("warehouse"),
            "needed_by": needed_by,
        })

    if not grouped:
        return {"lines": [], "company": resolved_company}

    item_codes = list(grouped)
    on_hand = _on_hand_by_item(item_codes)
    last_rates = _last_purchase_rate_by_item(item_codes)
    # So a line bought straight off the requests list carries the same VAT it
    # would have carried had the buyer searched for the item by hand: the Item
    # master's own template, else the site default from Jarz POS Settings.
    # Company-scoped, so a default belonging to another company is not offered.
    from jarz_pos.api.purchase import _item_tax_templates_bulk

    tax_templates = _item_tax_templates_bulk(item_codes, company=resolved_company)

    lines = []
    for item_code, bucket in grouped.items():
        bucket["on_hand_qty"] = on_hand.get(item_code, 0.0)
        bucket["last_purchase_rate"] = last_rates.get(item_code, 0.0)
        bucket["item_tax_template"] = tax_templates.get(item_code)
        # Sort the per-branch breakdown by urgency so the tap-to-expand view
        # leads with whoever needs it soonest.
        bucket["sources"].sort(key=lambda s: (str(s.get("needed_by") or "9999-12-31"), s.get("pos_profile") or ""))
        lines.append(bucket)

    lines.sort(key=lambda b: (str(b.get("earliest_needed_by") or "9999-12-31"), b["item_name"]))
    return {"lines": lines, "company": resolved_company}


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

_REQUEST_FIELDS = [
    "name", "transaction_date", "schedule_date", "status", "docstatus",
    "per_ordered", "per_received", "company", "owner", "creation", "modified",
    "custom_jarz_pos_profile", "custom_jarz_requested_by_label", "custom_jarz_note",
]

_REQUEST_ITEM_FIELDS = [
    "name", "parent", "item_code", "item_name", "qty", "uom", "stock_uom",
    "conversion_factor", "stock_qty", "ordered_qty", "received_qty",
    "warehouse", "schedule_date", "idx",
]


def _serialize_request(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": doc.get("name"),
        "transaction_date": doc.get("transaction_date"),
        "schedule_date": doc.get("schedule_date"),
        "status": doc.get("status"),
        "docstatus": doc.get("docstatus"),
        "per_ordered": flt(doc.get("per_ordered") or 0),
        "per_received": flt(doc.get("per_received") or 0),
        "company": doc.get("company"),
        "pos_profile": doc.get("custom_jarz_pos_profile"),
        "requested_by": doc.get("custom_jarz_requested_by_label") or doc.get("owner"),
        "requested_by_user": doc.get("owner"),
        "note": doc.get("custom_jarz_note"),
        "creation": doc.get("creation"),
        "modified": doc.get("modified"),
        "is_mine": doc.get("owner") == frappe.session.user,
    }


def _serialize_line(row: Dict[str, Any]) -> Dict[str, Any]:
    stock_qty = flt(row.get("stock_qty") or 0)
    received = flt(row.get("received_qty") or 0)
    return {
        "name": row.get("name"),
        "item_code": row.get("item_code"),
        "item_name": row.get("item_name") or row.get("item_code"),
        "qty": flt(row.get("qty") or 0),
        "uom": row.get("uom"),
        "stock_uom": row.get("stock_uom"),
        "conversion_factor": flt(row.get("conversion_factor") or 1),
        "stock_qty": stock_qty,
        "ordered_qty": flt(row.get("ordered_qty") or 0),
        "received_qty": received,
        "outstanding_qty": max(stock_qty - received, 0.0),
        "warehouse": row.get("warehouse"),
        "schedule_date": row.get("schedule_date"),
    }


def _conversion_factor(item_code: str, uom: str, stock_uom: str) -> float:
    if not uom or uom == stock_uom:
        return 1.0
    factor = frappe.db.get_value(
        "UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor"
    )
    if not factor:
        frappe.throw(
            _("{0} has no conversion from {1} to {2}. Add it on the Item.").format(
                item_code, uom, stock_uom
            )
        )
    return flt(factor)


def _on_hand_by_item(item_codes: Sequence[str]) -> Dict[str, float]:
    if not item_codes:
        return {}
    totals: Dict[str, float] = {}
    for row in frappe.get_all(
        "Bin",
        filters={"item_code": ["in", list(item_codes)]},
        fields=["item_code", "actual_qty"],
        limit_page_length=0,
    ):
        totals[row["item_code"]] = totals.get(row["item_code"], 0.0) + flt(row.get("actual_qty") or 0)
    return totals


def _last_purchase_rate_by_item(item_codes: Sequence[str]) -> Dict[str, float]:
    """Most recent rate actually paid, per item.

    Read from submitted Purchase Invoice lines rather than
    ``Item.last_purchase_rate``, which is also written by Purchase Orders and so
    can reflect a price that was quoted but never paid.
    """
    if not item_codes:
        return {}
    rows = frappe.get_all(
        "Purchase Invoice Item",
        filters={"item_code": ["in", list(item_codes)], "docstatus": 1},
        fields=["item_code", "rate", "creation"],
        order_by="creation desc",
        limit_page_length=0,
    )
    latest: Dict[str, float] = {}
    for row in rows:
        latest.setdefault(row["item_code"], flt(row.get("rate") or 0))
    return latest


def _user_label(user: str) -> str:
    label = frappe.db.get_value("User", user, "full_name")
    return str(label or user)


def notify_fulfilment(purchase_invoice: str, request_item_names: Sequence[str]) -> None:
    """Tell requesters what was actually bought against their request.

    Called after the Purchase Invoice submits, so ``received_qty`` on the
    request lines already reflects the purchase. Closing this loop is what keeps
    staff filing requests — a request that disappears into silence stops being
    used within a week.

    Best-effort by design: the goods are already received and the accounting is
    already posted by the time this runs, so a notification failure must not
    undo any of it. It is logged, never silently swallowed.
    """
    if not request_item_names:
        return
    try:
        rows = frappe.get_all(
            "Material Request Item",
            filters={"name": ["in", list(request_item_names)]},
            fields=["parent", "item_code", "item_name", "stock_qty", "received_qty", "stock_uom"],
            limit_page_length=0,
        )
        if not rows:
            return

        parents = {r["parent"] for r in rows}
        parent_rows = frappe.get_all(
            "Material Request",
            filters={"name": ["in", list(parents)]},
            fields=["name", "owner", "status", "per_received", "custom_jarz_pos_profile"],
            limit_page_length=0,
        )
        parent_map = {p["name"]: p for p in parent_rows}

        by_parent: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_parent.setdefault(row["parent"], []).append(row)

        from jarz_pos.utils.realtime import publish_to_branches

        for parent_name, lines in by_parent.items():
            parent = parent_map.get(parent_name) or {}
            profile = parent.get("custom_jarz_pos_profile")
            payload = {
                "name": parent_name,
                "purchase_invoice": purchase_invoice,
                "status": parent.get("status"),
                "per_received": flt(parent.get("per_received") or 0),
                "lines": [
                    {
                        "item_code": line["item_code"],
                        "item_name": line.get("item_name") or line["item_code"],
                        "requested_qty": flt(line.get("stock_qty") or 0),
                        "received_qty": flt(line.get("received_qty") or 0),
                        "uom": line.get("stock_uom"),
                    }
                    for line in lines
                ],
            }
            owner = parent.get("owner")
            publish_to_branches(
                "jarz_pos_item_request_fulfilled",
                payload,
                [profile] if profile else [],
                extra_users=[owner] if owner else None,
                after_commit=True,
            )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "purchase_request: fulfilment notification failed"
        )


def _notify_reviewers(doc: Any) -> None:
    """Tell the buyers a new request landed.

    Best-effort: a notification failure must not roll back a request that is
    otherwise valid. Logged rather than swallowed silently so a broken
    notification path is visible instead of merely absent.
    """
    try:
        from jarz_pos.utils.realtime import publish_to_branches

        profile = doc.get("custom_jarz_pos_profile")
        if not profile:
            return
        publish_to_branches(
            "jarz_pos_item_request_created",
            {
                "name": doc.name,
                "pos_profile": profile,
                "requested_by": doc.get("custom_jarz_requested_by_label"),
                "item_count": len(doc.get("items") or []),
            },
            [profile],
            after_commit=True,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "purchase_request: reviewer notification failed"
        )

"""
Master Orders API for the Jarz POS mobile app.

Provides a searchable, filterable, paginated list of all POS invoices
across branches. Access restricted to Moderator, Line Manager, and Manager
roles (no regular staff).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES


def _ensure_elevated_access():
    """Raise if the current user is not at least a Moderator."""
    roles = set(frappe.get_roles(frappe.session.user))
    allowed = {ROLES.JARZ_MANAGER, ROLES.JARZ_LINE_MANAGER, ROLES.ADMINISTRATOR, ROLES.SYSTEM_MANAGER, "Moderator"}
    if not (roles & allowed):
        frappe.throw(_("Access denied"), frappe.PermissionError)


def _get_state_field() -> Optional[str]:
    """Return the state field name on Sales Invoice, if it exists."""
    try:
        meta = frappe.get_meta("Sales Invoice")
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                return candidate
    except Exception:
        pass
    return None


def _get_state_options() -> List[str]:
    """Return available state option values."""
    try:
        meta = frappe.get_meta("Sales Invoice")
        for field_name in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            field = meta.get_field(field_name)
            if field and getattr(field, "options", None):
                opts = [o.strip() for o in field.options.split("\n") if o.strip()]
                if opts:
                    return opts
    except Exception:
        pass
    return []


@frappe.whitelist(allow_guest=False)
def get_master_orders(
    search: Optional[str] = None,
    status: Optional[str] = None,
    branch: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    payment_status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """
    Return a paginated, filtered list of POS invoices.

    Args:
        search: Free-text search against invoice name, customer_name, customer phone.
        status: Filter by kanban/invoice state (e.g. "Received", "Delivered").
        branch: Filter by POS Profile / custom_kanban_profile.
        from_date: Filter invoices on or after this date (YYYY-MM-DD).
        to_date: Filter invoices on or before this date (YYYY-MM-DD).
        payment_status: Filter by doc status ("Paid", "Unpaid").
        page: 1-based page number.
        page_size: Items per page (max 100).

    Returns:
        {
            "invoices": [...],
            "total": <int>,
            "page": <int>,
            "page_size": <int>,
            "total_pages": <int>,
            "filters": {
                "states": [...],
                "branches": [...],
                "payment_statuses": [...]
            }
        }
    """
    _ensure_elevated_access()

    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 100))

    # Determine the state field
    state_field = _get_state_field()

    # Determine the branch field
    try:
        si_meta = frappe.get_meta("Sales Invoice")
        branch_field = "custom_kanban_profile" if si_meta.get_field("custom_kanban_profile") else "pos_profile"
    except Exception:
        branch_field = "pos_profile"

    # Base filters: only submitted POS invoices
    filters: Dict[str, Any] = {
        "docstatus": 1,
        "is_pos": 1,
    }

    # Status filter
    if status and status.lower() != "all" and state_field:
        # Match case-insensitively against available options
        options = _get_state_options()
        match = next((o for o in options if o.lower() == status.lower()), None)
        filters[state_field] = match or status

    # Branch filter
    if branch and branch.lower() != "all":
        filters[branch_field] = branch

    # Date range filter
    if from_date:
        filters.setdefault("posting_date", {})
        if isinstance(filters["posting_date"], dict):
            filters["posting_date"] = [">=", from_date]
        else:
            filters["posting_date"] = [">=", from_date]
    if to_date:
        if from_date:
            filters["posting_date"] = ["between", [from_date, to_date]]
        else:
            filters["posting_date"] = ["<=", to_date]

    # Payment status filter
    if payment_status and payment_status.lower() != "all":
        filters["status"] = payment_status

    # Build fields list
    fields = [
        "name",
        "customer",
        "customer_name",
        "posting_date",
        "posting_time",
        "grand_total",
        "outstanding_amount",
        "status",
        branch_field,
        "pos_profile",
    ]
    if state_field and state_field not in fields:
        fields.append(state_field)

    # Search handling - use OR conditions
    or_filters = None
    if search and search.strip():
        search_term = search.strip()
        or_filters = [
            ["name", "like", f"%{search_term}%"],
            ["customer_name", "like", f"%{search_term}%"],
            ["customer", "like", f"%{search_term}%"],
        ]

    # Count total matching records
    if or_filters:
        # For OR filters with AND filters, we need SQL
        and_conditions = []
        and_values = {}
        idx = 0
        for key, val in filters.items():
            if isinstance(val, list):
                if val[0] == "between":
                    and_conditions.append(f"`tab{key}`.`{key}` between %(v{idx}a)s and %(v{idx}b)s")
                    and_values[f"v{idx}a"] = val[1][0]
                    and_values[f"v{idx}b"] = val[1][1]
                elif val[0] in (">=", "<=", ">", "<"):
                    and_conditions.append(f"`posting_date` {val[0]} %(v{idx})s")
                    and_values[f"v{idx}"] = val[1]
                elif val[0] == "in":
                    placeholders = ", ".join([f"%(v{idx}_{j})s" for j in range(len(val[1]))])
                    and_conditions.append(f"`{key}` in ({placeholders})")
                    for j, v in enumerate(val[1]):
                        and_values[f"v{idx}_{j}"] = v
                else:
                    and_conditions.append(f"`{key}` {val[0]} %(v{idx})s")
                    and_values[f"v{idx}"] = val[1]
            else:
                and_conditions.append(f"`{key}` = %(v{idx})s")
                and_values[f"v{idx}"] = val
            idx += 1

        or_parts = []
        for of in or_filters:
            or_parts.append(f"`{of[0]}` like %(search)s")
        and_values["search"] = f"%{search.strip()}%"

        where = " AND ".join(and_conditions)
        if where:
            where += f" AND ({' OR '.join(or_parts)})"
        else:
            where = f"({' OR '.join(or_parts)})"

        total = frappe.db.sql(
            f"SELECT COUNT(*) FROM `tabSales Invoice` WHERE {where}",
            and_values,
        )[0][0]

        # Fetch paginated results
        offset = (page - 1) * page_size
        field_list = ", ".join([f"`{f}`" for f in fields])
        rows_raw = frappe.db.sql(
            f"SELECT {field_list} FROM `tabSales Invoice` WHERE {where} ORDER BY `posting_date` DESC, `posting_time` DESC LIMIT %(limit)s OFFSET %(offset)s",
            {**and_values, "limit": page_size, "offset": offset},
            as_dict=True,
        )
    else:
        total = frappe.get_count("Sales Invoice", filters=filters)

        offset = (page - 1) * page_size
        rows_raw = frappe.get_all(
            "Sales Invoice",
            filters=filters,
            fields=fields,
            order_by="posting_date desc, posting_time desc",
            limit_page_length=page_size,
            limit_start=offset,
        )

    # Normalize rows
    invoices = []
    for r in rows_raw:
        invoices.append({
            "name": r.get("name"),
            "customer": r.get("customer"),
            "customer_name": r.get("customer_name") or r.get("customer"),
            "posting_date": str(r.get("posting_date") or ""),
            "posting_time": str(r.get("posting_time") or ""),
            "grand_total": float(r.get("grand_total") or 0),
            "outstanding_amount": float(r.get("outstanding_amount") or 0),
            "payment_status": r.get("status"),
            "state": r.get(state_field) if state_field else (r.get("status") or ""),
            "branch": r.get(branch_field) or r.get("pos_profile") or "",
        })

    total_pages = max(1, -(-total // page_size))  # ceiling division

    # Gather filter options for the frontend
    branches = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or []
    states = _get_state_options()

    return {
        "invoices": invoices,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "filters": {
            "states": states,
            "branches": sorted(branches),
            "payment_statuses": ["Paid", "Unpaid", "Overdue"],
        },
    }

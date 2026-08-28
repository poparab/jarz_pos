"""Manager Dashboard APIs for Branch Live Feed.

Endpoints:
- get_transfer_target_branches: list accessible active POS Profiles without balance data for transfer pickers.
- get_manager_dashboard_summary: list accessible POS Profiles with cash account and current balance.
- get_manager_orders: recent invoices feed filtered by branch (POS Profile) or all, optional by state.
- get_manager_states: return available Sales Invoice state options (same as Kanban columns).
- update_cancelled_invoice_status_fields: update limited workflow fields on cancelled Sales Invoices.
- update_invoice_branch: reassign a submitted Sales Invoice by changing custom_kanban_profile only.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import re
from typing import List, Dict, Any, Optional, Union
import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate
from jarz_pos.constants import ACCOUNTS, ROLES, WS_EVENTS

try:
    # ERPNext helper to get account balance as of today
    from erpnext.accounts.utils import get_balance_on  # type: ignore
except Exception:
    get_balance_on = None  # type: ignore

try:
    from jarz_pos.utils.account_utils import get_pos_cash_account
except Exception:
    def get_pos_cash_account(pos_profile: str, company: str) -> str:  # type: ignore
        # Fallback: try to resolve a Cash account roughly matching the profile name
        acc = frappe.db.get_value(
            "Account",
            {"company": company, "parent_account": ["like", f"%{ACCOUNTS.CASH_IN_HAND}%"], "account_name": ["like", f"%{pos_profile}%"], "is_group": 0},
            "name",
        )
        if acc:
            return acc
        # last resort: company's default cash account
        return frappe.get_cached_value("Company", company, "default_cash_account") or "Cash"

try:
    from jarz_pos.api.notifications import notify_invoice_reassignment
except Exception:
    def notify_invoice_reassignment(*args, **kwargs):  # type: ignore
        return None

try:
    from jarz_pos.utils.invoice_utils import (
        assert_pos_profile_matches_territory,
        format_invoice_data,
        read_invoice_shipping_income,
        resolve_order_territory,
    )
except Exception:
    def format_invoice_data(invoice_doc):  # type: ignore
        return {"name": getattr(invoice_doc, "name", None)}

    def read_invoice_shipping_income(invoice_doc):  # type: ignore
        return 0.0

    def assert_pos_profile_matches_territory(*args, **kwargs):  # type: ignore
        return None

    def resolve_order_territory(*args, **kwargs):  # type: ignore
        return None

try:
    from jarz_pos.services.invoice_creation import create_pos_invoice as _create_amendment_invoice
except Exception:
    _create_amendment_invoice = None  # type: ignore

from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

from jarz_pos.utils.access_control import (
    ensure_profile_scoped_invoice_access as _shared_ensure_profile_scoped_invoice_access,
    get_user_pos_profiles,
    get_users_for_pos_profiles,
)
from jarz_pos.utils.invoice_utils import normalize_woo_order_id
from jarz_pos.utils.realtime import publish_invoice_event, publish_to_branches


# Allowed states for invoice transfer (normalized: lowercase, no extra spaces)
# These match the actual field values: "Received", "In Progress", "Ready"
# Note: "recieved" (misspelled) included for backward compatibility with existing data
_ALLOWED_TRANSFER_STATES = {"received", "recieved", "in progress", "ready", "preparing"}
_ALLOWED_AMENDMENT_STATES = _ALLOWED_TRANSFER_STATES


def _current_user_allowed_profiles() -> List[str]:
    """Return POS Profiles the current user can manage.

    Branch membership is the POS Profile User child table for *everyone*: a
    manager is added to the branches they run, exactly like a staff member.
    Only ``Administrator`` still sees every profile.

    This used to hand System Manager / POS Manager the full list while the
    Kanban board scoped the same user to their linked profiles — the manager
    feed would list an order that the board then refused to open.
    """
    return get_user_pos_profiles()


def _has_manager_dashboard_access() -> bool:
    roles = {str(role or "").strip() for role in (frappe.get_roles() or []) if str(role or "").strip()}
    allowed = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER
    return bool(roles.intersection(allowed))


def _ensure_manager_dashboard_access() -> None:
    """Ensure the current user has JARZ Manager, Line Manager, or admin-level role for dashboard access."""
    if not _has_manager_dashboard_access():
        frappe.throw(_("Not permitted: Manager Dashboard access required"), frappe.PermissionError)


def _has_shift_monitor_access() -> bool:
    roles = {str(role or "").strip() for role in (frappe.get_roles() or []) if str(role or "").strip()}
    allowed = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER
    return bool(roles.intersection(allowed))


def _ensure_shift_monitor_access() -> None:
    if not _has_shift_monitor_access():
        frappe.throw(_("Not permitted: Shift monitor access required"), frappe.PermissionError)


def _get_all_active_pos_profiles() -> List[str]:
    try:
        return frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or []
    except Exception:
        return []


def _current_user_shift_monitor_profiles() -> List[str]:
    """Branches whose shifts this user may monitor.

    The role check says the user may open the monitor at all; the branch list
    says whose shifts they see. Previously every monitor user saw every active
    profile, which contradicted the branch scoping applied everywhere else.
    """
    _ensure_shift_monitor_access()
    return _current_user_allowed_profiles()


def _coerce_shift_monitor_date(value: Optional[str], fallback: str):
    if not value:
        return getdate(fallback)
    try:
        return getdate(value)
    except Exception:
        frappe.throw(_("Invalid date: {0}").format(value))


def _normalize_shift_monitor_status(status: Optional[str]) -> str:
    normalized = str(status or "all").strip().lower()
    if normalized not in {"all", "open", "closed"}:
        frappe.throw(_("Invalid status filter: {0}").format(status))
    return normalized


def _sum_shift_amount(rows: Any, fieldname: str) -> float:
    total = 0.0
    for row in rows or []:
        if isinstance(row, dict):
            total += flt(row.get(fieldname))
        else:
            total += flt(getattr(row, fieldname, 0))
    return flt(total, 2)


def _shift_monitor_user_details(
    user: str,
    user_cache: Dict[str, Dict[str, Optional[str]]],
) -> Dict[str, Optional[str]]:
    normalized_user = str(user or "").strip()
    if not normalized_user:
        return {
            "user": None,
            "full_name": None,
            "employee": None,
            "employee_name": None,
        }

    cached = user_cache.get(normalized_user)
    if cached is not None:
        return cached

    full_name = frappe.db.get_value("User", normalized_user, "full_name")
    employee = frappe.db.get_value(
        "Employee",
        {"user_id": normalized_user},
        ["name", "employee_name"],
        as_dict=True,
    )
    cached = {
        "user": normalized_user,
        "full_name": full_name,
        "employee": employee.get("name") if employee else None,
        "employee_name": employee.get("employee_name") if employee else None,
    }
    user_cache[normalized_user] = cached
    return cached


def _find_linked_pos_closing_entry(opening_name: str) -> Optional[str]:
    linked = frappe.db.get_value("POS Opening Entry", opening_name, "pos_closing_entry")
    if linked:
        return linked
    return frappe.db.get_value(
        "POS Closing Entry",
        {"pos_opening_entry": opening_name},
        "name",
    )


def _build_shift_monitor_row(
    opening: Any,
    *,
    user_cache: Dict[str, Dict[str, Optional[str]]],
    carry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from jarz_pos.api.shift import _resolve_pos_profile_account

    opening_user = _shift_monitor_user_details(getattr(opening, "user", None), user_cache)
    opening_amount = _sum_shift_amount(getattr(opening, "balance_details", None), "opening_amount")
    cash_account = _resolve_pos_profile_account(
        getattr(opening, "company", None),
        getattr(opening, "pos_profile", None),
        None,
        None,
    )

    closing_name = _find_linked_pos_closing_entry(opening.name)
    closing = frappe.get_doc("POS Closing Entry", closing_name) if closing_name else None

    closed_by = {"user": None, "full_name": None, "employee": None, "employee_name": None}
    expected_closing_amount = None
    actual_closing_amount = None
    difference_amount = None
    difference_kind = "none"
    closed_at = None
    journal_entry = None

    if closing:
        closing_user = (
            getattr(closing, "owner", None)
            or getattr(closing, "modified_by", None)
            or getattr(opening, "modified_by", None)
        )
        closed_by = _shift_monitor_user_details(closing_user, user_cache)
        expected_closing_amount = _sum_shift_amount(
            getattr(closing, "payment_reconciliation", None),
            "expected_amount",
        )
        actual_closing_amount = _sum_shift_amount(
            getattr(closing, "payment_reconciliation", None),
            "closing_amount",
        )
        difference_amount = flt(actual_closing_amount - expected_closing_amount, 2)
        if difference_amount > 0:
            difference_kind = "surplus"
        elif difference_amount < 0:
            difference_kind = "shortage"
        closed_at = (
            getattr(closing, "period_end_date", None)
            or getattr(closing, "modified", None)
            or getattr(opening, "period_end_date", None)
        )
        journal_entry = frappe.db.get_value(
            "Journal Entry",
            {
                "user_remark": ["like", f"%{opening.name}%"],
                "docstatus": 1,
            },
            "name",
        )

    shift_status = "closed" if closing else "open"
    # Courier money this shift handed forward, and money a previous shift handed
    # to it. Read from the Courier Transaction stamps rather than from the till,
    # because none of it ever passed through the drawer on the night it was
    # carried. Computed in bulk by the caller — see get_shift_carry_stats_bulk.
    from jarz_pos.services.courier_carry import EMPTY_CARRY_STATS

    carry = carry or EMPTY_CARRY_STATS
    return {
        "pos_profile": getattr(opening, "pos_profile", None),
        "company": getattr(opening, "company", None),
        "shift_status": shift_status,
        "opening_entry": getattr(opening, "name", None),
        "closing_entry": closing_name,
        "opened_at": getattr(opening, "period_start_date", None),
        "opened_by_user": opening_user["user"],
        "opened_by_full_name": opening_user["full_name"],
        "opened_by_employee": opening_user["employee"],
        "opened_by_employee_name": opening_user["employee_name"],
        "closed_at": closed_at,
        "closed_by_user": closed_by["user"],
        "closed_by_full_name": closed_by["full_name"],
        "closed_by_employee": closed_by["employee"],
        "closed_by_employee_name": closed_by["employee_name"],
        "cash_account": cash_account,
        "opening_amount": opening_amount,
        "expected_closing_amount": expected_closing_amount,
        "actual_closing_amount": actual_closing_amount,
        "difference_amount": difference_amount,
        "difference_kind": difference_kind,
        "journal_entry": journal_entry,
        "carried_out_count": carry["carried_out_count"],
        "carried_out_amount": carry["carried_out_amount"],
        "settled_in_count": carry["settled_in_count"],
        "settled_in_amount": carry["settled_in_amount"],
    }


def _ensure_profile_scoped_invoice_access(
    inv: Any,
    *,
    action_label: str,
    extra_profiles: Optional[List[str]] = None,
) -> None:
    """Assert the current user may act on this invoice's branch.

    Holding a manager role no longer waives the branch check — a manager is
    scoped to the branches they were added to, same as anyone else.
    """
    _shared_ensure_profile_scoped_invoice_access(
        inv,
        action_label=action_label,
        extra_profiles=extra_profiles,
    )


def _get_state_field_options() -> List[str]:
    """Return list of Sales Invoice state options without reading Custom Field doc.
    Prefers 'custom_sales_invoice_state', falls back to legacy names.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        for field_name in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            field = meta.get_field(field_name)
            if field and getattr(field, 'options', None):
                options = [opt.strip() for opt in field.options.split('\n') if opt.strip()]
                if options:
                    return options
        return []
    except Exception:
        return []


def _state_key(value: Optional[str]) -> Optional[str]:
    """Normalize a state label into the websocket state-key format."""
    if not value:
        return None
    return str(value).strip().lower().replace(" ", "_")


def _match_option(value: Optional[str], options: List[str]) -> Optional[str]:
    """Return the canonical option that matches value case-insensitively."""
    cleaned_value = str(value or "").strip()
    if not cleaned_value:
        return None
    for option in options:
        if option.lower() == cleaned_value.lower():
            return option
    return None


def _get_acceptance_field_options() -> List[str]:
    """Return Acceptance Status select options from Sales Invoice metadata."""
    try:
        field = frappe.get_meta("Sales Invoice").get_field("custom_acceptance_status")
        if field and getattr(field, "options", None):
            return [opt.strip() for opt in field.options.split("\n") if opt.strip()]
    except Exception:
        pass
    return []


def _resolve_pos_profile_warehouse(pos_profile_name: str) -> str:
    """Resolve the stock source warehouse for a POS Profile."""
    warehouse = (frappe.db.get_value("POS Profile", pos_profile_name, "warehouse") or "").strip()
    if not warehouse:
        raise frappe.ValidationError(_(f"Target POS Profile {pos_profile_name} has no warehouse configured."))
    if not frappe.db.exists("Warehouse", warehouse):
        raise frappe.ValidationError(_(f"Configured warehouse {warehouse} for POS Profile {pos_profile_name} was not found."))
    return warehouse


def _validate_transfer_target_warehouse(inv: Any, target_warehouse: str) -> None:
    """Ensure the target warehouse is compatible with the Sales Invoice company."""
    invoice_company = str(inv.get("company") or "").strip()
    warehouse_company = str(frappe.db.get_value("Warehouse", target_warehouse, "company") or "").strip()
    if invoice_company and warehouse_company and invoice_company != warehouse_company:
        raise frappe.ValidationError(_(f"Target warehouse {target_warehouse} belongs to a different company."))


def _get_transfer_stock_rows(inv: Any) -> List[Any]:
    """Return invoice rows whose warehouse must follow branch reassignment."""
    stock_rows: List[Any] = []
    item_stock_cache: Dict[str, bool] = {}

    for row in list(getattr(inv, "items", []) or []):
        item_code = str(getattr(row, "item_code", "") or "").strip()
        if not item_code:
            continue
        if item_code not in item_stock_cache:
            item_stock_cache[item_code] = bool(int(frappe.db.get_value("Item", item_code, "is_stock_item") or 0))
        if item_stock_cache[item_code]:
            stock_rows.append(row)

    return stock_rows


def _find_submitted_delivery_notes(invoice_name: str) -> List[str]:
    """Return submitted Delivery Notes already linked to the Sales Invoice."""
    rows = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": invoice_name, "docstatus": 1},
        pluck="parent",
        limit_page_length=20,
    ) or []
    return sorted({row for row in rows if row})


def _find_submitted_payment_entries(invoice_name: str) -> List[str]:
    """Return submitted Payment Entries already linked to the Sales Invoice."""
    ref_rows = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice_name,
            "parenttype": "Payment Entry",
        },
        pluck="parent",
        limit_page_length=20,
    ) or []
    payment_entry_names = sorted({row for row in ref_rows if row})
    if not payment_entry_names:
        return []

    submitted = frappe.get_all(
        "Payment Entry",
        filters={"name": ["in", payment_entry_names], "docstatus": 1},
        pluck="name",
        limit_page_length=20,
    ) or []
    return sorted({row for row in submitted if row})


def _find_courier_transactions(invoice_name: str) -> List[str]:
    """Return courier transactions already linked to the Sales Invoice."""
    try:
        rows = frappe.get_all(
            "Courier Transaction",
            filters={"reference_invoice": invoice_name},
            pluck="name",
            limit_page_length=20,
        ) or []
    except Exception:
        rows = []
    return sorted({row for row in rows if row})


def _find_sales_partner_transactions(invoice_name: str) -> List[str]:
    """Return Sales Partner Transaction rows already linked to the Sales Invoice."""
    try:
        rows = frappe.get_all(
            "Sales Partner Transactions",
            filters={"reference_invoice": invoice_name},
            pluck="name",
            limit_page_length=20,
        ) or []
    except Exception:
        rows = []
    return sorted({row for row in rows if row})


def _find_active_custom_shipping_requests(invoice_name: str) -> List[str]:
    """Return active (not cancelled) Custom Shipping Requests linked to the Sales Invoice."""
    try:
        rows = frappe.get_all(
            "Custom Shipping Request",
            filters={"invoice": invoice_name, "docstatus": ["!=", 2]},
            pluck="name",
            limit_page_length=20,
        ) or []
    except Exception:
        rows = []
    return sorted({row for row in rows if row})


def _mentions_invoice(text: Optional[str], invoice_name: str) -> bool:
    """True when *text* references *invoice_name* as a whole token.

    A plain ``LIKE %name%`` cannot distinguish an invoice from its own
    amendments: ``ACC-SINV-2026-00001`` is a substring of
    ``ACC-SINV-2026-00001-1``, so the amendment's settlement Journal Entry used
    to block cancellation of the *original*. Requiring the match to end at a
    non-identifier character keeps every genuine reference while dropping that
    whole class of false positives.

    Deliberately permissive on recall: this feeds a safety blocker, so missing a
    real reference (under-blocking) is far worse than an extra hit.
    """
    if not text or not invoice_name:
        return False
    pattern = re.escape(invoice_name) + r"(?![0-9A-Za-z\-_])"
    return re.search(pattern, str(text)) is not None


def _find_submitted_journal_entries(invoice_name: str) -> List[str]:
    """Return submitted Journal Entries that already settled against the invoice."""
    journal_entry_names = set()

    # `title` and `user_remark` are matched broadly in SQL, then narrowed in
    # Python by `_mentions_invoice`. `title` is kept even though ERPNext v16
    # overwrites it on validate — historical (pre-v16) entries can still carry a
    # meaningful title, and dropping the source could only ever under-block.
    for fieldname in ("title", "user_remark"):
        try:
            rows = frappe.get_all(
                "Journal Entry",
                filters={"docstatus": 1, fieldname: ["like", f"%{invoice_name}%"]},
                fields=["name", fieldname],
                limit_page_length=50,
            ) or []
            journal_entry_names.update(
                row.get("name")
                for row in rows
                if row.get("name") and _mentions_invoice(row.get(fieldname), invoice_name)
            )
        except Exception:
            pass

    try:
        ref_rows = frappe.get_all(
            "Journal Entry Account",
            filters={
                "reference_type": "Sales Invoice",
                "reference_name": invoice_name,
                "parenttype": "Journal Entry",
            },
            pluck="parent",
            limit_page_length=20,
        ) or []
        ref_names = sorted({row for row in ref_rows if row})
        if ref_names:
            submitted = frappe.get_all(
                "Journal Entry",
                filters={"name": ["in", ref_names], "docstatus": 1},
                pluck="name",
                limit_page_length=20,
            ) or []
            journal_entry_names.update(row for row in submitted if row)
    except Exception:
        pass

    return sorted(journal_entry_names)


def _get_active_delivery_trip_name(inv: Any) -> Optional[str]:
    """Return the linked delivery trip when it is still operationally active."""
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()
    trip_name = str(getattr(inv, "custom_delivery_trip", "") or inv.get("custom_delivery_trip") or "").strip()
    if not trip_name and invoice_name:
        try:
            linked_trips = frappe.get_all(
                "Delivery Trip Invoice",
                filters={"invoice": invoice_name},
                pluck="parent",
                limit_page_length=5,
            ) or []
            trip_name = next((row for row in linked_trips if row), "")
        except Exception:
            trip_name = ""
    if not trip_name:
        return None

    try:
        trip_status = str(frappe.db.get_value("Delivery Trip", trip_name, "status") or "").strip()
    except Exception:
        return trip_name

    if not trip_status or trip_status != "Completed":
        return trip_name
    return None


def get_invoice_hard_mutation_blocker(inv: Any) -> Optional[Dict[str, Any]]:
    """Return the first downstream artifact that blocks cancel/amend mutations."""
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()
    if not invoice_name:
        return None

    delivery_notes = _find_submitted_delivery_notes(invoice_name)
    if delivery_notes:
        return {
            "mutation_block_code": "delivery_note_exists",
            "mutation_block_reason": _("This invoice already has a submitted Delivery Note and must use a corrective workflow."),
            "delivery_notes": delivery_notes,
        }

    active_trip = _get_active_delivery_trip_name(inv)
    if active_trip:
        return {
            "mutation_block_code": "delivery_trip_exists",
            "mutation_block_reason": _("This invoice is already linked to an active delivery trip and cannot be changed from this workflow."),
            "delivery_trip": active_trip,
        }

    courier_transactions = _find_courier_transactions(invoice_name)
    if courier_transactions:
        return {
            "mutation_block_code": "courier_transaction_exists",
            "mutation_block_reason": _("This invoice already has courier settlement artifacts and cannot be changed from this workflow."),
            "courier_transactions": courier_transactions,
        }

    sales_partner_transactions = _find_sales_partner_transactions(invoice_name)
    if sales_partner_transactions:
        return {
            "mutation_block_code": "sales_partner_transaction_exists",
            "mutation_block_reason": _("This invoice already has sales partner settlement artifacts and cannot be changed from this workflow."),
            "sales_partner_transactions": sales_partner_transactions,
        }

    journal_entries = _find_submitted_journal_entries(invoice_name)
    if journal_entries:
        return {
            "mutation_block_code": "journal_entry_exists",
            "mutation_block_reason": _("This invoice already has settlement journal entries and cannot be changed from this workflow."),
            "journal_entries": journal_entries,
        }

    custom_shipping_requests = _find_active_custom_shipping_requests(invoice_name)
    if custom_shipping_requests:
        return {
            "mutation_block_code": "custom_shipping_request_exists",
            "mutation_block_reason": _("This invoice is linked to an active shipping request and cannot be changed from this workflow."),
            "custom_shipping_requests": custom_shipping_requests,
        }

    return None


def get_invoice_amendment_eligibility(inv: Any) -> Dict[str, Any]:
    """Return whether a submitted POS invoice can start the ERP-first amendment flow."""
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()
    current_state = str(inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "").strip()
    normalized_state = current_state.lower()

    def _blocked(code: str, reason: str, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "can_amend": False,
            "amendment_block_code": code,
            "amendment_block_reason": reason,
        }
        payload.update(extra)
        return payload

    if not invoice_name:
        return _blocked("invoice_missing", _("Invoice was not found."))

    if int(inv.get("docstatus") or 0) != 1:
        return _blocked("invoice_not_submitted", _("Only submitted invoices can be amended."))

    if int(inv.get("is_return") or 0):
        return _blocked("return_invoice", _("Return invoices cannot be amended from this workflow."))

    if normalized_state not in _ALLOWED_AMENDMENT_STATES:
        return _blocked(
            "state_not_supported",
            _("This invoice can only be amended before dispatch while it is still in an operational prep state."),
        )

    mutation_blocker = get_invoice_hard_mutation_blocker(inv)
    if mutation_blocker:
        return _blocked(
            mutation_blocker.get("mutation_block_code") or "mutation_blocked",
            mutation_blocker.get("mutation_block_reason") or _("This invoice cannot be changed from this workflow."),
            **{
                key: value
                for key, value in mutation_blocker.items()
                if key not in {"mutation_block_code", "mutation_block_reason"}
            },
        )

    return {
        "can_amend": True,
        "amendment_block_code": None,
        "amendment_block_reason": None,
    }


#: Operational states that still allow an outright cancellation.
_DISPATCHED_STATES = {"out_for_delivery", "delivered", "completed", "cancelled"}


def get_invoice_cancellation_eligibility(inv: Any) -> Dict[str, Any]:
    """Return whether a submitted invoice can still be cancelled outright.

    Mirrors the guards inside ``jarz_pos.api.kanban.cancel_invoice`` so the
    client can explain *why* the action is unavailable instead of letting the
    user press it and receive a raw server error. Kept next to
    :func:`get_invoice_amendment_eligibility` because both are merged into the
    same card payload.

    A blocked cancel is not a dead end: everything except ``return_invoice``
    and ``invoice_not_submitted`` is a candidate for the return workflow, which
    is what ``suggest_return`` tells the client.
    """
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()

    def _blocked(code: str, reason: str, *, suggest_return: bool = False, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "can_cancel": False,
            "cancellation_block_code": code,
            "cancellation_block_reason": reason,
            "cancellation_suggests_return": suggest_return,
        }
        payload.update(extra)
        return payload

    if not invoice_name:
        return _blocked("invoice_missing", _("Invoice was not found."))

    if int(inv.get("docstatus") or 0) != 1:
        return _blocked("invoice_not_submitted", _("Only submitted invoices can be cancelled."))

    if int(inv.get("is_return") or 0):
        return _blocked("return_invoice", _("Return invoices cannot be cancelled."))

    current_state = str(
        inv.get("custom_sales_invoice_state")
        or inv.get("sales_invoice_state")
        or inv.get("custom_state")
        or inv.get("state")
        or ""
    ).strip().lower().replace(" ", "_").replace("-", "_")
    if current_state in _DISPATCHED_STATES:
        return _blocked(
            "already_dispatched",
            _("This order was already dispatched. Use the return workflow instead."),
            suggest_return=True,
        )

    mutation_blocker = get_invoice_hard_mutation_blocker(inv)
    if mutation_blocker:
        return _blocked(
            mutation_blocker.get("mutation_block_code") or "mutation_blocked",
            mutation_blocker.get("mutation_block_reason") or _("This invoice cannot be changed from this workflow."),
            suggest_return=True,
            **{
                key: value
                for key, value in mutation_blocker.items()
                if key not in {"mutation_block_code", "mutation_block_reason"}
            },
        )

    outstanding = flt(inv.get("outstanding_amount"))
    grand_total = flt(inv.get("grand_total"))
    tolerance = 0.50
    if not (outstanding >= (grand_total - tolerance) or outstanding <= tolerance):
        return _blocked(
            "partial_payment",
            _("This invoice has a partial payment; settle or refund it before cancelling."),
            suggest_return=True,
        )

    # A paid invoice cancels its Payment Entries, so the shift rules apply.
    if outstanding <= tolerance and grand_total > tolerance:
        try:
            from jarz_pos.utils.access_control import (
                find_closed_shift_covering,
                get_open_shift_for_profile,
                get_invoice_branch,
                user_requires_pos_shift,
            )

            for pe_name in _find_submitted_payment_entries(invoice_name):
                closed = find_closed_shift_covering(
                    frappe.db.get_value("Payment Entry", pe_name, "creation")
                )
                if closed:
                    return _blocked(
                        "closed_shift",
                        _(
                            "The payment for this order was booked in shift {0}, "
                            "which is already closed. Use the return workflow instead."
                        ).format(closed.get("name")),
                        suggest_return=True,
                        closed_shift=closed.get("name"),
                    )

            if user_requires_pos_shift() and not get_open_shift_for_profile(get_invoice_branch(inv)):
                return _blocked(
                    "shift_required",
                    _("Start a shift on this branch before cancelling a paid order."),
                )
        except Exception:
            # Advisory only — the authoritative guard runs inside cancel_invoice.
            pass

    return {
        "can_cancel": True,
        "cancellation_block_code": None,
        "cancellation_block_reason": None,
        "cancellation_suggests_return": False,
    }


def _derive_required_delivery_datetime(inv: Any) -> Optional[str]:
    """Derive the delivery start datetime from the invoice's stored slot fields."""
    delivery_date = str(inv.get("custom_delivery_date") or "").strip()
    delivery_time_from = str(inv.get("custom_delivery_time_from") or "").strip()
    if not delivery_date or not delivery_time_from:
        return None
    normalized_time = delivery_time_from if len(delivery_time_from) > 5 else f"{delivery_time_from}:00"
    return f"{delivery_date} {normalized_time}"


def _derive_delivery_end_datetime(inv: Any) -> Optional[str]:
    """Derive the delivery end datetime from the invoice's duration metadata."""
    start_text = _derive_required_delivery_datetime(inv)
    if not start_text:
        return None

    raw_duration = inv.get("custom_delivery_duration")
    if raw_duration in (None, ""):
        return None

    try:
        start_dt = frappe.utils.get_datetime(start_text)
        if isinstance(raw_duration, str) and ":" in raw_duration:
            parts = [int(part or 0) for part in raw_duration.split(":")]
            while len(parts) < 3:
                parts.append(0)
            duration_seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            duration_seconds = int(float(raw_duration or 0))
        if duration_seconds <= 0:
            return None
        return frappe.utils.add_to_date(start_dt, seconds=duration_seconds, as_string=True)
    except Exception:
        return None


@contextmanager
def _temporary_invoice_creation_form_context(
    *,
    required_delivery_datetime: Optional[str] = None,
    delivery_end_datetime: Optional[str] = None,
) -> Any:
    """Temporarily seed form_dict so invoice creation keeps the chosen slot duration."""
    previous_form_dict = getattr(frappe, "form_dict", None)
    next_form_dict = frappe._dict(dict(previous_form_dict or {}))
    if required_delivery_datetime:
        next_form_dict["required_delivery_datetime"] = required_delivery_datetime
    if delivery_end_datetime:
        next_form_dict["delivery_end_datetime"] = delivery_end_datetime
    frappe.form_dict = next_form_dict
    try:
        yield
    finally:
        frappe.form_dict = previous_form_dict


def _build_invoice_amendment_request_id(
    *,
    invoice_id: str,
    cart_json: Any,
    pos_profile_name: Optional[str],
    customer_name: Optional[str],
    shipping_address_name: Optional[str],
    required_delivery_datetime: Optional[str],
    delivery_end_datetime: Optional[str],
    sales_partner: Optional[str],
    payment_type: Optional[str],
    pickup: Union[bool, int, str, None],
    payment_method: Optional[str],
    suppress_shipping_income: Union[bool, int, str, None] = None,
    suppress_legacy_delivery_charges: Union[bool, int, str, None] = None,
    zero_shipping_override: Union[bool, int, str, None] = None,
    custom_delivery_income: Union[float, str, None] = None,
    provided_idempotency_key: Optional[str] = None,
) -> str:
    """Build a stable idempotency key for amendment retries of the same payload."""
    provided = str(provided_idempotency_key or "").strip()
    if provided:
        return provided

    try:
        normalized_cart = frappe.parse_json(cart_json) if isinstance(cart_json, str) else cart_json
    except Exception:
        normalized_cart = cart_json

    payload = {
        "invoice_id": invoice_id,
        "cart": normalized_cart,
        "pos_profile_name": pos_profile_name,
        "customer_name": customer_name,
        "shipping_address_name": shipping_address_name,
        "required_delivery_datetime": required_delivery_datetime,
        "delivery_end_datetime": delivery_end_datetime,
        "sales_partner": sales_partner,
        "payment_type": payment_type,
        "pickup": _is_truthy_flag(pickup),
        "payment_method": payment_method,
        "suppress_shipping_income": _is_truthy_flag(suppress_shipping_income),
        "suppress_legacy_delivery_charges": _is_truthy_flag(suppress_legacy_delivery_charges),
        "zero_shipping_override": _is_truthy_flag(zero_shipping_override),
        "custom_delivery_income": (
            None if (custom_delivery_income is None or str(custom_delivery_income).strip() == "")
            else frappe.utils.flt(custom_delivery_income)
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return f"amd-{invoice_id}-{digest[:16]}"


def _territory_default_delivery_income(territory_name: Optional[str]) -> Optional[float]:
    """Return a Territory's configured delivery income, or None when unresolvable.

    Used to tell a genuine per-order override apart from an order that simply
    charged its territory's standard rate.
    """
    name = str(territory_name or "").strip()
    if not name:
        return None
    try:
        value = frappe.db.get_value("Territory", name, "delivery_income")
    except Exception:
        return None
    return frappe.utils.flt(value) if value is not None else None


def _resolve_amendment_delivery_income(
    source_invoice: Any,
    requested: Union[float, str, None],
) -> Optional[float]:
    """Return the delivery income override to carry into the replacement invoice.

    ``requested`` follows the API contract:
      * ``None`` (key absent) → preserve what the source invoice actually charged
      * ``''`` (empty string) → explicitly clear, reverting to the territory default
      * numeric               → use it (0 = free delivery, >0 = custom amount)

    ``None`` is returned when the replacement should re-derive the income from its
    territory rather than carry a pinned override.
    """
    if requested is not None and str(requested).strip() != "":
        return frappe.utils.flt(requested)
    if requested is not None:
        return None  # explicit clear

    # Deliberately NOT source_invoice.custom_delivery_income: that column is NOT NULL
    # DEFAULT 0, so "never overridden" and "overridden to free" are the same stored 0.
    # Reading it made every amendment that omitted the key silently drop the shipping
    # income to zero. The invoice's own Shipping Income tax row is the only
    # unambiguous record of what it charged.
    actual_income = frappe.utils.flt(read_invoice_shipping_income(source_invoice))
    territory_income = _territory_default_delivery_income(source_invoice.get("territory"))
    # Charging exactly the territory rate means nothing was ever overridden. Leave it
    # unset so the replacement re-derives it — otherwise every amended order would pin
    # a stale rate, and moving the address to another territory would keep the old price.
    if territory_income is not None and abs(actual_income - territory_income) < 0.005:
        return None
    return actual_income


def _find_existing_amendment_invoice(source_invoice_id: str) -> Optional[str]:
    """Return the existing replacement invoice for a cancelled source invoice when present."""
    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters={"amended_from": source_invoice_id, "docstatus": ["!=", 2]},
            pluck="name",
            order_by="creation desc",
            limit_page_length=1,
        ) or []
    except Exception:
        rows = []
    return rows[0] if rows else None


def _add_invoice_audit_comment(invoice_name: str, comment: str) -> None:
    """Add a best-effort audit comment to an invoice."""
    if not comment:
        return
    try:
        frappe.get_doc("Sales Invoice", invoice_name).add_comment("Comment", comment)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Invoice amendment audit comment failed for {invoice_name}")


def _mark_source_invoice_as_amended(
    source_invoice_name: str,
    *,
    replacement_invoice_name: str,
    request_id: str,
    initiated_by: str,
) -> None:
    """Persist structured amendment metadata on the superseded source invoice."""
    meta = frappe.get_meta("Sales Invoice")
    reason_text = (
        f"Superseded by {replacement_invoice_name} through POS amendment flow. "
        f"Request ID: {request_id}. Initiated by: {initiated_by}."
    )
    updates: Dict[str, Any] = {}
    if meta.get_field("custom_cancellation_type"):
        updates["custom_cancellation_type"] = "Amended"
    if meta.get_field("custom_cancellation_reason"):
        updates["custom_cancellation_reason"] = reason_text
    if updates:
        frappe.db.set_value("Sales Invoice", source_invoice_name, updates, update_modified=False)


def _repoint_woocommerce_order_map(
    *,
    woo_order_id: str,
    new_invoice_name: str,
    logger: Any = None,
) -> None:
    """After amendment, point the WooCommerce Order Map at the live replacement
    invoice and clear the cached hash so the next inbound webhook re-evaluates
    the order instead of short-circuiting with "unchanged".

    Errors are swallowed so a map-update failure can never abort an already-
    successful amendment.
    """
    try:
        maps = frappe.get_all(
            "WooCommerce Order Map",
            filters={"woo_order_id": woo_order_id},
            fields=["name"],
            limit=1,
        )
        if not maps:
            return
        frappe.db.set_value(
            "WooCommerce Order Map",
            maps[0]["name"],
            {
                "erpnext_sales_invoice": new_invoice_name,
                "hash": "",
            },
            update_modified=False,
        )
    except Exception as exc:
        if logger:
            logger.warning(
                {
                    "event": "woo_order_map_repoint_failed",
                    "woo_order_id": woo_order_id,
                    "new_invoice": new_invoice_name,
                    "error": str(exc),
                }
            )


def _carry_over_invoice_notes(
    *,
    source_invoice_name: str,
    new_invoice_name: str,
    logger: Any = None,
) -> None:
    """Move operational Jarz notes from a superseded source invoice onto its live
    amendment replacement.

    Notes are standalone ``Jarz Invoice Note`` documents linked to a Sales Invoice
    via the ``sales_invoice`` field (the Kanban card's note badge + latest-note
    preview read them through that link). Because amendment cancels the source
    invoice and creates a brand-new invoice with a different name, the notes would
    otherwise stay pinned to the cancelled invoice and disappear from the live
    order. Re-pointing them here preserves the original author/timestamp metadata
    exactly — unlike re-inserting, which ``JarzInvoiceNote.validate`` would restamp
    with the amending user and ``now()``.

    Errors are swallowed so a note-carry-over failure can never abort an already-
    successful amendment.
    """
    try:
        note_names = frappe.get_all(
            "Jarz Invoice Note",
            filters={"sales_invoice": source_invoice_name},
            pluck="name",
        ) or []
        if not note_names:
            return

        # Re-derive the Kanban/POS profile from the replacement invoice so the
        # carried-over notes stay aligned with the new invoice's board column.
        invoice_values = frappe.db.get_value(
            "Sales Invoice",
            new_invoice_name,
            ["custom_kanban_profile", "pos_profile"],
            as_dict=True,
        ) or {}
        new_pos_profile = (
            invoice_values.get("custom_kanban_profile")
            or invoice_values.get("pos_profile")
            or None
        )

        for note_name in note_names:
            updates: Dict[str, Any] = {"sales_invoice": new_invoice_name}
            if new_pos_profile:
                updates["pos_profile"] = new_pos_profile
            frappe.db.set_value(
                "Jarz Invoice Note",
                note_name,
                updates,
                update_modified=False,
            )

        if logger:
            logger.info(
                {
                    "event": "invoice_notes_carried_over",
                    "source_invoice": source_invoice_name,
                    "new_invoice": new_invoice_name,
                    "note_count": len(note_names),
                }
            )
    except Exception as exc:
        if logger:
            logger.warning(
                {
                    "event": "invoice_note_carry_over_failed",
                    "source_invoice": source_invoice_name,
                    "new_invoice": new_invoice_name,
                    "error": str(exc),
                }
            )
        else:
            frappe.log_error(
                frappe.get_traceback(),
                f"Invoice note carry-over failed {source_invoice_name} -> {new_invoice_name}",
            )


def _build_invoice_amendment_response(
    *,
    request_id: str,
    source_invoice_name: str,
    replacement_invoice_name: str,
    cancelled_payment_entries: Optional[List[str]] = None,
    already_processed: bool = False,
) -> Dict[str, Any]:
    """Return the stable API response for a completed amendment orchestration."""
    replacement_invoice = frappe.get_doc("Sales Invoice", replacement_invoice_name)
    return {
        "success": True,
        "request_id": request_id,
        "source_invoice_id": source_invoice_name,
        "replacement_invoice_id": replacement_invoice_name,
        "cancelled_payment_entries": cancelled_payment_entries or [],
        "already_processed": already_processed,
        "invoice": format_invoice_data(replacement_invoice),
    }


def _run_invoice_amendment_job(
    *,
    invoice_id: str,
    request_id: str,
    cart_json: Any,
    customer_name: Optional[str] = None,
    shipping_address_name: Optional[str] = None,
    pos_profile_name: Optional[str] = None,
    required_delivery_datetime: Optional[str] = None,
    delivery_end_datetime: Optional[str] = None,
    sales_partner: Optional[str] = None,
    payment_type: Optional[str] = None,
    pickup: Union[bool, int, str, None] = None,
    payment_method: Optional[str] = None,
    initiated_by: Optional[str] = None,
    pos_profile_override: Union[bool, int, str, None] = None,
    suppress_shipping_income: Union[bool, int, None] = None,
    suppress_legacy_delivery_charges: Union[bool, int, None] = None,
    zero_shipping_override: Union[bool, int, str, None] = None,
    expected_source_grand_total: Optional[float] = None,
    expected_source_item_count: Optional[int] = None,
    custom_delivery_income: Union[float, str, None] = None,
) -> Dict[str, Any]:
    """Queueable job that supersedes a submitted invoice and recreates it from the POS payload."""
    if _create_amendment_invoice is None:
        frappe.throw(_("Invoice amendment service is unavailable."))

    logger = frappe.logger("jarz_pos.api.manager", allow_site=frappe.local.site)
    source_invoice = frappe.get_doc("Sales Invoice", invoice_id)
    existing_replacement = _find_existing_amendment_invoice(invoice_id)
    if existing_replacement:
        return _build_invoice_amendment_response(
            request_id=request_id,
            source_invoice_name=invoice_id,
            replacement_invoice_name=existing_replacement,
            already_processed=True,
        )

    eligibility = get_invoice_amendment_eligibility(source_invoice)
    if not eligibility.get("can_amend"):
        return {
            "success": False,
            "request_id": request_id,
            "error": eligibility.get("amendment_block_reason") or _("Invoice amendment is blocked."),
            "amendment_block_code": eligibility.get("amendment_block_code"),
        }

    cancelled_payment_entries: List[str] = []
    effective_customer_name = (customer_name or source_invoice.get("customer") or "").strip() or "Walking Customer"
    effective_shipping_address_name = (
        shipping_address_name
        or source_invoice.get("shipping_address_name")
        or source_invoice.get("customer_address")
        or None
    )
    effective_pos_profile = (
        pos_profile_name
        or source_invoice.get("custom_kanban_profile")
        or source_invoice.get("pos_profile")
        or ""
    ).strip()
    effective_sales_partner = (sales_partner if sales_partner is not None else source_invoice.get("sales_partner") or None)
    effective_payment_method = (
        payment_method if payment_method is not None else source_invoice.get("custom_payment_method") or None
    )
    effective_pickup = _is_truthy_flag(pickup) or _is_truthy_flag(source_invoice.get("custom_is_pickup"))
    zero_shipping_override_enabled = _is_truthy_flag(zero_shipping_override)
    effective_suppress_shipping_income = (
        True
        if zero_shipping_override_enabled
        else (bool(suppress_shipping_income) if suppress_shipping_income is not None else None)
    )
    effective_suppress_legacy_delivery_charges = (
        True
        if zero_shipping_override_enabled
        else (
            bool(suppress_legacy_delivery_charges)
            if suppress_legacy_delivery_charges is not None
            else None
        )
    )
    effective_required_delivery_datetime = required_delivery_datetime or _derive_required_delivery_datetime(source_invoice)
    effective_delivery_end_datetime = delivery_end_datetime or _derive_delivery_end_datetime(source_invoice)
    effective_custom_delivery_income = _resolve_amendment_delivery_income(
        source_invoice, custom_delivery_income
    )
    woo_order_id = source_invoice.get("woo_order_id")
    # Preserve commercial-policy / order purpose on amendment. Without this a B2B /
    # Employee / Sample replacement would silently revert to Standard accounting
    # (courier expense reappears, policy price list lost). Re-passing the purpose lets
    # the service re-resolve the policy (income/courier suppression + price list) freshly.
    effective_order_purpose = source_invoice.get("custom_order_purpose") or None
    effective_commercial_policy = source_invoice.get("custom_commercial_policy") or None
    effective_policy_reason = source_invoice.get("custom_policy_reason") or None
    initiated_by = (initiated_by or frappe.session.user or "Unknown User").strip()

    # Territory → POS profile safety check (before any DB writes)
    effective_order_territory = resolve_order_territory(
        effective_customer_name,
        shipping_address_name=effective_shipping_address_name,
    )
    assert_pos_profile_matches_territory(
        effective_customer_name,
        effective_pos_profile,
        override=_is_truthy_flag(pos_profile_override),
        territory_name=effective_order_territory,
    )

    # Advisory lock shared with the Woo amendment path (order_amendment.py).
    # Prevents POS and Woo webhook races on the same invoice.
    inv_lock_key = f"inv:{invoice_id}"
    inv_lock_acquired = False
    try:
        lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 5)", (inv_lock_key,))
        inv_lock_acquired = bool(lock_result and lock_result[0] and lock_result[0][0] == 1)
        if not inv_lock_acquired:
            return {
                "success": False,
                "request_id": request_id,
                "error": "Invoice is currently being modified by another process. Please retry.",
                "amendment_block_code": "invoice_locked",
            }
    except Exception:
        inv_lock_acquired = False

    # B2: Re-verify eligibility *after* acquiring the lock to catch races
    # (e.g. CSR created after the Flutter card was opened).
    reload_invoice = getattr(source_invoice, "reload", None)
    if callable(reload_invoice):
        reload_invoice()
    else:
        try:
            source_invoice = frappe.get_doc("Sales Invoice", invoice_id)
        except Exception:
            pass
    fresh_eligibility = get_invoice_amendment_eligibility(source_invoice)
    if not fresh_eligibility.get("can_amend"):
        if inv_lock_acquired:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (inv_lock_key,))
            except Exception:
                pass
        return {
            "success": False,
            "request_id": request_id,
            "error": fresh_eligibility.get("amendment_block_reason") or _("Invoice amendment is blocked."),
            "amendment_block_code": fresh_eligibility.get("amendment_block_code"),
        }

    # B5: Parity checks — validate submitted cart against source invoice.
    try:
        parsed_cart = frappe.parse_json(cart_json) if isinstance(cart_json, str) else (cart_json or [])
    except Exception:
        parsed_cart = []

    submitted_item_count = len(parsed_cart) if isinstance(parsed_cart, list) else 0

    def _release_invoice_lock_if_needed():
        if inv_lock_acquired:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (inv_lock_key,))
            except Exception:
                pass

    if submitted_item_count == 0:
        _release_invoice_lock_if_needed()
        return {
            "success": False,
            "request_id": request_id,
            "error": _("The submitted cart is empty. Please reload the order and try again."),
            "amendment_block_code": "empty_cart",
        }

    if isinstance(parsed_cart, list):
        malformed_bundle_rows = []
        for row in parsed_cart:
            if not isinstance(row, dict) or row.get("is_bundle") is not True:
                continue
            selected_items = row.get("selected_items")
            has_selected_children = isinstance(selected_items, dict) and any(
                isinstance(entries, list) and len(entries) > 0
                for entries in selected_items.values()
            )
            if not has_selected_children:
                malformed_bundle_rows.append(str(row.get("item_code") or "bundle"))

        if malformed_bundle_rows:
            _release_invoice_lock_if_needed()
            frappe.log_error(
                f"Invoice amendment {invoice_id} submitted bundle rows without selected children: "
                f"{', '.join(malformed_bundle_rows)}",
                "Invoice Amendment Bundle Guard",
            )
            return {
                "success": False,
                "request_id": request_id,
                "error": _(
                    "One or more bundles were submitted without their selected items. "
                    "Please reload the order and try again."
                ),
                "amendment_block_code": "bundle_selection_missing",
                "malformed_bundles": malformed_bundle_rows,
            }

    source_grand_total = float(source_invoice.get("grand_total") or 0)
    if expected_source_grand_total is not None and source_grand_total > 0:
        drift = abs(float(expected_source_grand_total) - source_grand_total) / source_grand_total
        if drift > 0.005:
            if inv_lock_acquired:
                try:
                    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (inv_lock_key,))
                except Exception:
                    pass
            return {
                "success": False,
                "request_id": request_id,
                "error": _(
                    f"The source invoice total changed since you opened the draft "
                    f"(expected {expected_source_grand_total}, actual {source_grand_total}). "
                    f"Please reload and retry."
                ),
                "amendment_block_code": "stale_source",
                "source_grand_total": source_grand_total,
            }

    # Compute submitted total (best-effort: multiply rate × qty for each row).
    # A rate-less line ({item_code, qty} with no rate/price, as the Flutter update-cart
    # payload sends) must NOT be treated as zero value, or a pure quantity INCREASE
    # (e.g. 3 → 5) would falsely trip the >50%-drop guard below. Resolve the missing rate
    # from the invoice's selling price list / Item master using the SAME helper the
    # invoice engine uses, so the guard reflects the real cart value. Genuine reductions
    # (rows that keep a real, lower rate) are unaffected — the guard stays intact.
    from jarz_pos.services.invoice_creation import _resolve_item_rate as _amend_resolve_item_rate

    _amend_price_list = source_invoice.get("selling_price_list") or None
    submitted_total = 0.0
    if isinstance(parsed_cart, list):
        for row in parsed_cart:
            if isinstance(row, dict):
                row_rate = float(row.get("rate") or row.get("price") or 0)
                row_qty = float(row.get("qty") or row.get("quantity") or 1)
                # Only resolve for plain rows; bundle rows keep their existing best-effort
                # rate (bundle pricing is not a single price-list lookup).
                if row_rate <= 0 and not row.get("is_bundle"):
                    _code = str(row.get("item_code") or "").strip()
                    if _code:
                        try:
                            row_rate = float(
                                _amend_resolve_item_rate(
                                    _code,
                                    _amend_price_list,
                                    customer=effective_customer_name,
                                )
                                or 0
                            )
                        except Exception:
                            row_rate = 0.0
                submitted_total += row_rate * row_qty

    if source_grand_total > 0 and submitted_total < 0.5 * source_grand_total:
        if inv_lock_acquired:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (inv_lock_key,))
            except Exception:
                pass
        return {
            "success": False,
            "request_id": request_id,
            "error": _(
                f"Submitted cart total ({submitted_total:.2f}) is less than 50% of the source invoice total "
                f"({source_grand_total:.2f}). If intentional, contact your manager."
            ),
            "amendment_block_code": "suspicious_diff",
            "source_grand_total": source_grand_total,
            "submitted_total": submitted_total,
        }

    # H4: When the source invoice has a woo_order_id we must also hold the
    # Woo inbound lock so a concurrent webhook cannot race the cancel-then-submit
    # window and create a duplicate invoice against the same WooCommerce order.
    woo_lock_key = f"woo-order-{woo_order_id}" if woo_order_id else None
    woo_lock_acquired = False
    if woo_lock_key:
        try:
            woo_lock_result = frappe.db.sql("SELECT GET_LOCK(%s, 5)", (woo_lock_key,))
            woo_lock_acquired = bool(
                woo_lock_result and woo_lock_result[0] and woo_lock_result[0][0] == 1
            )
            if not woo_lock_acquired:
                if inv_lock_acquired:
                    try:
                        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (inv_lock_key,))
                    except Exception:
                        pass
                return {
                    "success": False,
                    "request_id": request_id,
                    "error": "The Woo order is currently being synced. Please retry in a moment.",
                    "amendment_block_code": "woo_order_locked",
                }
        except Exception:
            woo_lock_acquired = False

    save_point = f"invoice_amendment_{hashlib.sha1(request_id.encode('utf-8')).hexdigest()[:10]}"
    try:
        frappe.db.savepoint(save_point)
    except Exception:
        save_point = ""

    try:
        # H1: Per-PE try/except so a single failed cancellation does not leave
        # previously-cancelled PEs orphaned against a still-submitted source invoice.
        payment_entries = _find_submitted_payment_entries(invoice_id)
        pe_cancel_errors: List[str] = []
        for payment_entry_name in payment_entries:
            try:
                payment_entry = frappe.get_doc("Payment Entry", payment_entry_name)
                if int(payment_entry.get("docstatus") or 0) != 1:
                    continue
                payment_entry.flags.ignore_permissions = True
                payment_entry.cancel()
                cancelled_payment_entries.append(payment_entry.name)
            except Exception as pe_exc:
                pe_cancel_errors.append(f"{payment_entry_name}: {pe_exc}")

        if pe_cancel_errors:
            raise Exception(
                f"Failed to cancel {len(pe_cancel_errors)} payment "
                f"entry/entries: {'; '.join(pe_cancel_errors)}"
            )

        if cancelled_payment_entries:
            # Cancelling a Payment Entry writes back to the invoice it paid --
            # outstanding_amount, status and therefore `modified`. This document
            # was loaded before that happened, so its timestamp is now stale and
            # `.cancel()` raises TimestampMismatchError:
            #   "has been modified after you have opened it ... Please refresh".
            #
            # The window is the whole point: an UNPAID amendment has no Payment
            # Entry to cancel, so nothing touches the invoice in between and the
            # bug never fires. Only amending a PAID order hits it, which is why
            # it survived -- the unpaid path is the one that gets exercised.
            source_invoice.reload()

        source_invoice.flags.ignore_permissions = True
        source_invoice.flags.ignore_woo_outbound = True
        source_invoice.cancel()

        with _temporary_invoice_creation_form_context(
            required_delivery_datetime=effective_required_delivery_datetime,
            delivery_end_datetime=effective_delivery_end_datetime,
        ):
            creation_result = _create_amendment_invoice(
                cart_json,
                effective_customer_name,
                effective_pos_profile,
                None,
                effective_required_delivery_datetime,
                effective_shipping_address_name,
                effective_sales_partner,
                payment_type,
                effective_pickup,
                effective_payment_method,
                amended_from=invoice_id,
                woo_order_id=woo_order_id,
                suppress_shipping_income=effective_suppress_shipping_income,
                suppress_legacy_delivery_charges=effective_suppress_legacy_delivery_charges,
                custom_delivery_income=effective_custom_delivery_income,
                order_purpose=effective_order_purpose,
                commercial_policy=effective_commercial_policy,
                policy_reason=effective_policy_reason,
            )

        replacement_invoice_name = (
            creation_result.get("invoice_name")
            or creation_result.get("name")
            or ""
        )
        if not replacement_invoice_name:
            frappe.throw(_("Invoice amendment did not return the replacement invoice name."))

        _mark_source_invoice_as_amended(
            invoice_id,
            replacement_invoice_name=replacement_invoice_name,
            request_id=request_id,
            initiated_by=initiated_by,
        )
        _add_invoice_audit_comment(
            invoice_id,
            (
                f"Invoice amended by {initiated_by}. Superseded by {replacement_invoice_name}. "
                f"Request ID: {request_id}."
            ),
        )
        _add_invoice_audit_comment(
            replacement_invoice_name,
            (
                f"Created as amendment of {invoice_id} by {initiated_by}. "
                f"Request ID: {request_id}."
            ),
        )

        # Carry operational Jarz notes forward onto the live replacement so the
        # Kanban note badge + latest-note preview survive the cancel-and-recreate
        # amendment (notes are linked to the invoice by name, which changes here).
        _carry_over_invoice_notes(
            source_invoice_name=invoice_id,
            new_invoice_name=replacement_invoice_name,
            logger=logger,
        )

        # H3: Repoint WooCommerce Order Map from the cancelled source to the live
        # replacement and clear the cached hash so the next inbound webhook
        # re-evaluates the order rather than skipping as "unchanged".
        if woo_order_id:
            _repoint_woocommerce_order_map(
                woo_order_id=woo_order_id,
                new_invoice_name=replacement_invoice_name,
                logger=logger,
            )

        logger.info(
            {
                "event": "invoice_amendment_completed",
                "source_invoice": invoice_id,
                "replacement_invoice": replacement_invoice_name,
                "request_id": request_id,
                "cancelled_payment_entries": cancelled_payment_entries,
            }
        )

        # H5: Notify all open clients so kanban boards refresh without a manual
        # page reload.  Fire-and-forget: a publish failure must never abort
        # the amendment that has already succeeded.
        try:
            from jarz_pos.utils.realtime import publish_invoice_event_by_name

            publish_invoice_event_by_name(
                "jarz_pos_invoice_amended",
                {
                    "source_invoice_id": invoice_id,
                    "replacement_invoice_id": replacement_invoice_name,
                    "request_id": request_id,
                    "timestamp": frappe.utils.now(),
                },
                replacement_invoice_name or invoice_id,
            )
        except Exception as pub_exc:
            logger.warning(
                {
                    "event": "invoice_amendment_realtime_publish_failed",
                    "source_invoice": invoice_id,
                    "replacement_invoice": replacement_invoice_name,
                    "error": str(pub_exc),
                }
            )

        return _build_invoice_amendment_response(
            request_id=request_id,
            source_invoice_name=invoice_id,
            replacement_invoice_name=replacement_invoice_name,
            cancelled_payment_entries=cancelled_payment_entries,
        )
    except Exception as exc:
        if save_point:
            frappe.db.rollback(save_point=save_point)
        logger.error(
            {
                "event": "invoice_amendment_failed",
                "source_invoice": invoice_id,
                "request_id": request_id,
                "error": str(exc),
            }
        )
        frappe.log_error(frappe.get_traceback(), "submit_invoice_amendment failed")
        return {"success": False, "request_id": request_id, "error": str(exc)}
    finally:
        # Release locks in reverse acquisition order (woo first, then inv).
        if woo_lock_acquired and woo_lock_key:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (woo_lock_key,))
            except Exception:
                pass
        if inv_lock_acquired:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (inv_lock_key,))
            except Exception:
                pass


@frappe.whitelist(allow_guest=False)
def submit_invoice_amendment(
    invoice_id: str,
    cart_json: Any = None,
    customer_name: Optional[str] = None,
    shipping_address_name: Optional[str] = None,
    pos_profile_name: Optional[str] = None,
    required_delivery_datetime: Optional[str] = None,
    delivery_end_datetime: Optional[str] = None,
    sales_partner: Optional[str] = None,
    payment_type: Optional[str] = None,
    pickup: Union[bool, int, str, None] = None,
    payment_method: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    pos_profile_override: Union[bool, int, str, None] = None,
    suppress_shipping_income: Union[bool, int, None] = None,
    suppress_legacy_delivery_charges: Union[bool, int, None] = None,
    zero_shipping_override: Union[bool, int, str, None] = None,
    expected_source_grand_total: Optional[float] = None,
    expected_source_item_count: Optional[int] = None,
    custom_delivery_income: Union[float, str, None] = None,
    reuse_source_cart: Union[bool, int, str, None] = None,
) -> Dict[str, Any]:
    """Supersede a submitted invoice and recreate it from the edited POS cart payload.

    Pass ``reuse_source_cart`` (with no ``cart_json``) for amendments that change
    only invoice-level data — delivery income, address, slot. The cart is then
    rebuilt server-side from the persisted invoice rows, which reproduces bundles
    exactly instead of relying on the client's lossy reconstruction.
    """
    invoice_id = (invoice_id or "").strip()
    if not invoice_id:
        return {"success": False, "error": "invoice_id is required"}
    if not cart_json and not _is_truthy_flag(reuse_source_cart):
        return {"success": False, "error": "cart_json is required"}

    source_invoice = frappe.get_doc("Sales Invoice", invoice_id)
    requested_profile = (pos_profile_name or "").strip()
    _ensure_profile_scoped_invoice_access(
        source_invoice,
        action_label="invoice amendment",
        extra_profiles=[requested_profile] if requested_profile else None,
    )

    if not cart_json:
        try:
            cart_json = json.dumps(build_amendment_cart_from_invoice(source_invoice))
        except Exception as rebuild_error:
            frappe.log_error(frappe.get_traceback(), f"Amendment cart rebuild failed for {invoice_id}")
            return {
                "success": False,
                "error": str(rebuild_error) or _("This order could not be rebuilt automatically."),
                "amendment_block_code": "cart_rebuild_failed",
            }

    zero_shipping_override_enabled = _is_truthy_flag(zero_shipping_override)
    if zero_shipping_override_enabled:
        suppress_shipping_income = True
        suppress_legacy_delivery_charges = True

    existing_replacement = _find_existing_amendment_invoice(invoice_id)
    request_id = _build_invoice_amendment_request_id(
        invoice_id=invoice_id,
        cart_json=cart_json,
        pos_profile_name=pos_profile_name,
        customer_name=customer_name,
        shipping_address_name=shipping_address_name,
        required_delivery_datetime=required_delivery_datetime or _derive_required_delivery_datetime(source_invoice),
        delivery_end_datetime=delivery_end_datetime or _derive_delivery_end_datetime(source_invoice),
        sales_partner=sales_partner if sales_partner is not None else source_invoice.get("sales_partner"),
        payment_type=payment_type,
        pickup=pickup,
        payment_method=payment_method if payment_method is not None else source_invoice.get("custom_payment_method"),
        suppress_shipping_income=suppress_shipping_income,
        suppress_legacy_delivery_charges=suppress_legacy_delivery_charges,
        zero_shipping_override=zero_shipping_override,
        custom_delivery_income=custom_delivery_income,
        provided_idempotency_key=idempotency_key,
    )
    if existing_replacement:
        return _build_invoice_amendment_response(
            request_id=request_id,
            source_invoice_name=invoice_id,
            replacement_invoice_name=existing_replacement,
            already_processed=True,
        )

    if int(source_invoice.get("docstatus") or 0) != 1:
        return {"success": False, "request_id": request_id, "error": "Only submitted invoices can be amended"}

    eligibility = get_invoice_amendment_eligibility(source_invoice)
    if not eligibility.get("can_amend"):
        return {
            "success": False,
            "request_id": request_id,
            "error": eligibility.get("amendment_block_reason") or _("Invoice amendment is blocked."),
            "amendment_block_code": eligibility.get("amendment_block_code"),
        }

    return frappe.enqueue(
        "jarz_pos.api.manager._run_invoice_amendment_job",
        queue="short",
        timeout=1200,
        now=True,
        job_id=request_id,
        invoice_id=invoice_id,
        request_id=request_id,
        cart_json=cart_json,
        customer_name=customer_name,
        shipping_address_name=shipping_address_name,
        pos_profile_name=pos_profile_name,
        required_delivery_datetime=required_delivery_datetime,
        delivery_end_datetime=delivery_end_datetime,
        sales_partner=sales_partner,
        payment_type=payment_type,
        pickup=pickup,
        payment_method=payment_method,
        initiated_by=frappe.session.user,
        pos_profile_override=pos_profile_override,
        suppress_shipping_income=suppress_shipping_income,
        suppress_legacy_delivery_charges=suppress_legacy_delivery_charges,
        zero_shipping_override=zero_shipping_override,
        expected_source_grand_total=float(expected_source_grand_total) if expected_source_grand_total is not None else None,
        expected_source_item_count=int(expected_source_item_count) if expected_source_item_count is not None else None,
        custom_delivery_income=custom_delivery_income,
    )


def _get_invoice_warehouse_mismatches(inv: Any, expected_warehouse: str) -> List[Dict[str, str]]:
    """Return stock rows whose warehouse no longer matches the operational branch."""
    mismatches: List[Dict[str, str]] = []
    for row in _get_transfer_stock_rows(inv):
        row_warehouse = str(getattr(row, "warehouse", "") or "").strip()
        if row_warehouse != expected_warehouse:
            mismatches.append(
                {
                    "row_name": str(getattr(row, "name", "") or "").strip(),
                    "item_code": str(getattr(row, "item_code", "") or "").strip(),
                    "warehouse": row_warehouse or "blank",
                }
            )
    return mismatches


def _is_truthy_flag(value: Union[bool, int, str, None]) -> bool:
    """Normalize common truthy flag inputs from whitelisted method arguments."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _publish_invoice_reassignment_refresh(
    invoice: Any,
    *,
    old_branch: Optional[str],
    new_branch: str,
    old_state: Optional[str],
    new_state: str,
) -> None:
    """Broadcast a board-refresh event for cross-session Kanban convergence."""
    invoice_summary = {
        "name": getattr(invoice, "name", None),
        "customer": invoice.get("customer"),
        "customer_name": invoice.get("customer_name"),
        "grand_total": invoice.get("grand_total"),
        "status": invoice.get("status"),
        "posting_date": str(invoice.get("posting_date")) if invoice.get("posting_date") else None,
        "posting_time": str(invoice.get("posting_time")) if invoice.get("posting_time") else None,
        "pos_profile": invoice.get("pos_profile"),
        "kanban_profile": new_branch,
    }
    payload = {
        "event": "invoice_reassigned",
        "invoice_id": getattr(invoice, "name", None),
        "old_profile": old_branch,
        "new_profile": new_branch,
        "old_state": old_state,
        "new_state": new_state,
        "old_state_key": None,
        "new_state_key": _state_key(new_state),
        "pos_profile": invoice.get("pos_profile"),
        "kanban_profile": new_branch,
        "acceptance_status": invoice.get("custom_acceptance_status"),
        "updated_by": frappe.session.user,
        "timestamp": frappe.utils.now(),
        "force_refresh": True,
        "invoice": invoice_summary,
    }
    try:
        # A transfer needs both sides to redraw: the branch losing the order has
        # to drop the card, the branch receiving it has to show it.
        both_branches = [b for b in (old_branch, new_branch) if b]
        publish_to_branches(WS_EVENTS.INVOICE_STATE_CHANGE, payload, both_branches)
        publish_to_branches(WS_EVENTS.KANBAN_UPDATE, payload, both_branches)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Invoice reassignment realtime publish failed for {getattr(invoice, 'name', None)}",
        )


@frappe.whitelist(allow_guest=False)
def get_transfer_target_branches() -> Dict[str, Any]:
    """Return all enabled POS Profiles as transfer targets for any staff user.

    Transfer targets are intentionally NOT scoped to the user's assigned POS
    Profiles: staff must be able to push an order to any enabled branch. The
    source-side restriction (a user may only transfer orders that belong to
    their own assigned profile) is enforced separately in update_invoice_branch
    via _ensure_profile_scoped_invoice_access.
    """
    profiles = frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or []
    return {
        "success": True,
        "branches": [{"name": profile, "title": profile} for profile in profiles],
    }


@frappe.whitelist(allow_guest=False)
def get_manager_dashboard_summary(company: Optional[str] = None) -> Dict[str, Any]:
    """Return accessible branches (POS Profiles) and their cash balances.

    Args:
        company: Optional company filter. If omitted, uses the single company of latest POS invoice or the user's default company.
    Returns:
        { success, branches: [ { name, title, cash_account, balance } ], total_balance }
    """
    _ensure_manager_dashboard_access()
    profiles = _current_user_allowed_profiles()
    if not profiles:
        return {
            "success": True,
            "branches": [],
            "total_balance": 0.0,
            "notice_code": "no_branch_assigned",
            "notice": _(
                "You are not assigned to any branch (POS Profile). Ask an "
                "administrator to add you to the branches you manage."
            ),
        }

    # Try to get company if not given
    if not company:
        try:
            # Latest POS SI company
            row = frappe.get_all("Sales Invoice", filters={"is_pos": 1}, fields=["company"], order_by="creation desc", limit=1)
            if row:
                company = row[0]["company"]
        except Exception:
            company = None
    balances: List[Dict[str, Any]] = []
    total = 0.0
    for p in profiles:
        try:
            cash_acc = get_pos_cash_account(p, company) if company else None
            bal = 0.0
            if cash_acc and get_balance_on:
                try:
                    bal = float(get_balance_on(account=cash_acc, date=frappe.utils.nowdate(), company=company))  # type: ignore
                except Exception:
                    bal = 0.0
            balances.append({
                "name": p,
                "title": p,
                "cash_account": cash_acc,
                "balance": bal,
            })
            total += bal
        except Exception:
            balances.append({"name": p, "title": p, "cash_account": None, "balance": 0.0})
    return {"success": True, "branches": balances, "total_balance": total}


@frappe.whitelist(allow_guest=False)
def get_pos_shift_monitor(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    pos_profile: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    _ensure_shift_monitor_access()

    start_date = _coerce_shift_monitor_date(from_date, nowdate())
    end_date = _coerce_shift_monitor_date(to_date, nowdate())
    if start_date > end_date:
        frappe.throw(_("From date cannot be later than To date"))

    status_filter = _normalize_shift_monitor_status(status)
    # `accessible_profiles` drives the filter picker; `profiles` narrows the query.
    accessible_profiles = _current_user_shift_monitor_profiles()
    profiles = list(accessible_profiles)
    selected_profile = str(pos_profile or "").strip()
    if selected_profile:
        if selected_profile not in accessible_profiles:
            return {
                "success": True,
                "summary": {
                    "open_count": 0,
                    "closed_count": 0,
                    "discrepancy_count": 0,
                    "discrepancy_total": 0.0,
                    "carried_out_count": 0,
                    "carried_out_total": 0.0,
                },
                "courier_outstanding": {
                    "couriers": [],
                    "total_net_balance": 0.0,
                    "transaction_count": 0,
                    "carried_count": 0,
                    "oldest_days_outstanding": 0,
                },
                "profiles": [{"name": profile, "title": profile} for profile in accessible_profiles],
                "shifts": [],
            }
        profiles = [selected_profile]

    if not profiles:
        return {
            "success": True,
            "summary": {
                "open_count": 0,
                "closed_count": 0,
                "discrepancy_count": 0,
                "discrepancy_total": 0.0,
                "carried_out_count": 0,
                "carried_out_total": 0.0,
            },
            "courier_outstanding": {
                "couriers": [],
                "total_net_balance": 0.0,
                "transaction_count": 0,
                "carried_count": 0,
                "oldest_days_outstanding": 0,
            },
            "profiles": [],
            "shifts": [],
            "notice_code": "no_branch_assigned",
            "notice": _(
                "You are not assigned to any branch (POS Profile). Ask an "
                "administrator to add you to the branches you manage."
            ),
        }

    openings = frappe.get_all(
        "POS Opening Entry",
        filters={
            "docstatus": 1,
            "pos_profile": ["in", profiles],
            "period_start_date": [
                "between",
                [f"{start_date} 00:00:00", f"{end_date} 23:59:59"],
            ],
        },
        fields=["name", "period_start_date", "period_end_date"],
        order_by="period_start_date desc, modified desc",
        limit=1000,
    )

    user_cache: Dict[str, Dict[str, Optional[str]]] = {}
    rows: List[Dict[str, Any]] = []
    discrepancy_total = 0.0
    discrepancy_count = 0
    open_count = 0
    closed_count = 0
    carried_out_total = 0.0
    carried_out_count = 0

    from jarz_pos.services.courier_carry import (
        get_carried_balances,
        get_shift_carry_stats_bulk,
    )

    courier_outstanding = get_carried_balances(profiles)
    carry_stats = get_shift_carry_stats_bulk(
        [dict(row) for row in openings]
    )

    for row in openings:
        opening = frappe.get_doc("POS Opening Entry", row["name"])
        payload = _build_shift_monitor_row(
            opening,
            user_cache=user_cache,
            carry=carry_stats.get(row["name"]),
        )
        if status_filter != "all" and payload["shift_status"] != status_filter:
            continue

        if payload["shift_status"] == "open":
            open_count += 1
        else:
            closed_count += 1

        difference_amount = flt(payload.get("difference_amount") or 0)
        if difference_amount != 0:
            discrepancy_count += 1
            discrepancy_total += difference_amount

        if int(payload.get("carried_out_count") or 0):
            carried_out_count += int(payload["carried_out_count"])
            carried_out_total += flt(payload.get("carried_out_amount") or 0)

        rows.append(payload)

    return {
        "success": True,
        "summary": {
            "open_count": open_count,
            "closed_count": closed_count,
            "discrepancy_count": discrepancy_count,
            "discrepancy_total": flt(discrepancy_total, 2),
            "carried_out_count": carried_out_count,
            "carried_out_total": flt(carried_out_total, 2),
        },
        # Money still with couriers RIGHT NOW, across every branch this user
        # monitors — deliberately not filtered by the date range, because the
        # question "who is holding our cash" is never about last week.
        "courier_outstanding": courier_outstanding,
        "filters": {
            "from_date": str(start_date),
            "to_date": str(end_date),
            "status": status_filter,
            "pos_profile": selected_profile or None,
        },
        # Filter options are every branch this user may monitor — not the
        # narrowed query set, which would collapse to the current selection.
        "profiles": [{"name": profile, "title": profile} for profile in accessible_profiles],
        "shifts": rows,
    }


@frappe.whitelist(allow_guest=False)
def get_manager_orders(branch: Optional[str] = None, state: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """Return a recent feed of POS invoices for selected branch or for all accessible branches.

    Args:
      branch: POS Profile name; when omitted or 'all', includes all accessible profiles.
      limit: Max invoices to return (default 200).
    Returns:
      { success, invoices: [ ... ] }
    """
    _ensure_manager_dashboard_access()
    limit = max(1, min(int(limit or 200), 500))
    allowed = _current_user_allowed_profiles()
    if not allowed:
        # Passing the role gate but owning no branch used to return a bare empty
        # feed, which reads as "no orders today" rather than "you were never
        # added to a branch".
        return {
            "success": True,
            "invoices": [],
            "notice_code": "no_branch_assigned",
            "notice": _(
                "You are not assigned to any branch (POS Profile). Ask an "
                "administrator to add you to the branches you manage."
            ),
        }

    profiles = allowed
    if branch and branch.lower() != "all":
        if branch in allowed:
            profiles = [branch]
        else:
            # No access to requested branch
            return {"success": True, "invoices": []}

    # Prefer filtering by custom_kanban_profile; fallback to pos_profile
    try:
        si_meta = frappe.get_meta("Sales Invoice")
        branch_filter_field = "custom_kanban_profile" if si_meta.get_field("custom_kanban_profile") else "pos_profile"
    except Exception:
        branch_filter_field = "pos_profile"

    fields = [
        "name", "customer", "customer_name", "posting_date", "posting_time", "grand_total", "net_total",
        "status", branch_filter_field, "custom_sales_invoice_state", "sales_invoice_state",
        "woo_order_id",
    ]
    # Build filters
    filters: Dict[str, Any] = {
        branch_filter_field: ["in", profiles],
        "docstatus": 1,
        "is_pos": 1,
    }
    # Optional state filter
    try:
        state_field = "custom_sales_invoice_state" if frappe.get_meta("Sales Invoice").get_field("custom_sales_invoice_state") else (
            "sales_invoice_state" if frappe.get_meta("Sales Invoice").get_field("sales_invoice_state") else None
        )
    except Exception:
        state_field = None
    if state and state.lower() != "all" and state_field:
        # Map to canonical case from options (case-insensitive)
        try:
            options = _get_state_field_options()
            match = next((opt for opt in options if opt.lower() == state.lower()), None)
            canonical = match or state
        except Exception:
            canonical = state
        filters[state_field] = canonical

    rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=fields,
        order_by="posting_date desc, posting_time desc",
        limit=limit,
    )
    # Normalize payload
    invs: List[Dict[str, Any]] = []
    for r in rows:
        invs.append({
            "name": r.get("name"),
            "woo_order_id": normalize_woo_order_id(r.get("woo_order_id")),
            "customer": r.get("customer"),
            "customer_name": r.get("customer_name") or r.get("customer"),
            "posting_date": str(r.get("posting_date")),
            "posting_time": str(r.get("posting_time")),
            "grand_total": float(r.get("grand_total") or 0),
            "net_total": float(r.get("net_total") or 0),
            "status": r.get("custom_sales_invoice_state") or r.get("sales_invoice_state") or r.get("status"),
            "branch": r.get(branch_filter_field),
        })
    return {"success": True, "invoices": invs}


@frappe.whitelist(allow_guest=False)
def get_manager_states() -> Dict[str, Any]:
    """Return available Sales Invoice states (same list used by Kanban columns)."""
    _ensure_manager_dashboard_access()
    try:
        states = _get_state_field_options()
        return {"success": True, "states": states}
    except Exception as e:
        return {"success": False, "error": str(e), "states": []}


# ---------------------------------------------------------------------------
# Employee ledger — cash advances + unpaid Employee-purpose orders
# ---------------------------------------------------------------------------
# Two unrelated things are "money one employee owes the company":
#
#   1. an HRMS ``Employee Advance`` — party is the Employee, and
#   2. an Employee-purpose POS order — party is the Customer.
#
# Employee orders are settled ON THE EMPLOYEE'S ACCOUNT: the invoice is
# submitted and deliberately left as an unpaid receivable instead of being paid
# at the till. So the two live in different party spaces yet add up to a single
# per-person balance, and a manager chasing somebody needs that one number.
# ``jarz_pos.utils.employee_link`` owns the Customer <-> Employee join; this
# section owns the money.

#: How far back the ledger looks when the caller passes no dates.
#:
#: The shift monitor defaults to *today* because a shift IS a one-day object. A
#: balance is not. An advance taken six weeks ago and still unsettled is exactly
#: the row a manager opens this screen to find, and a one-day default would hide
#: it behind a reassuringly empty table. 90 days spans the month-end settlement
#: cycle roughly three times, so nothing routine falls out of the window.
_EMPLOYEE_LEDGER_DEFAULT_DAYS = 90

#: Advance statuses that can still carry a balance. Mirrors HRMS's own
#: ``get_employee_advance_balance`` (hrms/api/__init__.py): "Claimed" and
#: "Returned" advances are fully settled and would only add noise.
_EMPLOYEE_ADVANCE_OPEN_STATUSES = ["Paid", "Partially Paid", "Unpaid"]


def _log_employee_ledger_error(summary: str) -> None:
    """Log a ledger failure without ever *becoming* the failure.

    ``frappe.log_error`` writes an Error Log row, so it can itself raise (read-only
    replica, disk full, the DocType missing mid-migrate). Called from an ``except``
    block that would turn a degraded section into a 500, so it is wrapped.
    """
    try:
        frappe.log_error(frappe.get_traceback(), summary)
    except Exception:
        pass


def _employee_ledger_currency() -> str:
    """Presentation currency for the ledger totals.

    Every amount summed here is company-currency (advances and invoices are both
    posted in it), so one currency label for the whole payload is honest. Falls
    back to EGP the way ``setup/b2b_master_data._default_currency`` does.
    """
    try:
        company = frappe.defaults.get_global_default("company")
        if company:
            currency = frappe.get_cached_value("Company", company, "default_currency")
            if currency:
                return str(currency)
    except Exception:
        pass
    return "EGP"


def _employee_ledger_branch_field() -> str:
    """Sales Invoice field that carries the operational branch.

    Same resolution ``get_manager_orders`` uses: ``custom_kanban_profile`` is the
    branch an order currently belongs to (it moves on transfer), ``pos_profile``
    is where it was created and is read-only after submit.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        return "custom_kanban_profile" if meta.get_field("custom_kanban_profile") else "pos_profile"
    except Exception:
        return "pos_profile"


def _employee_ledger_state_field() -> Optional[str]:
    """Sales Invoice field holding the Kanban state, or None when absent."""
    try:
        meta = frappe.get_meta("Sales Invoice")
        if meta.get_field("custom_sales_invoice_state"):
            return "custom_sales_invoice_state"
        if meta.get_field("sales_invoice_state"):
            return "sales_invoice_state"
    except Exception:
        pass
    return None


def _employee_ledger_employee_meta(employee_ids: List[str]) -> Dict[str, Dict[str, str]]:
    """``employee -> {employee_name, branch, user}`` for the ids in play.

    One bulk read rather than a lookup per row. Deliberately NOT filtered on
    ``status="Active"``: a resigned employee who still owes money must keep
    appearing, otherwise the balance silently leaves the report the day HR marks
    them left — which is the day chasing it actually matters.
    """
    wanted = sorted({str(e or "").strip() for e in employee_ids if str(e or "").strip()})
    if not wanted:
        return {}
    try:
        rows = frappe.get_all(
            "Employee",
            filters={"name": ["in", wanted]},
            fields=["name", "employee_name", "branch", "user_id"],
            limit_page_length=0,
        ) or []
        return {
            row["name"]: {
                "employee_name": str(row.get("employee_name") or row["name"]),
                "branch": str(row.get("branch") or ""),
                "user": str(row.get("user_id") or ""),
            }
            for row in rows
        }
    except Exception:
        _log_employee_ledger_error("Employee ledger: bulk Employee read failed")

    # Degraded path: at least label the rows, so the money stays attributable.
    try:
        from jarz_pos.utils.employee_link import employee_display_names

        return {
            emp: {"employee_name": name, "branch": "", "user": ""}
            for emp, name in (employee_display_names(wanted) or {}).items()
        }
    except Exception:
        return {}


def _employee_ledger_delivery_notes(invoice_names: List[str]) -> Dict[str, str]:
    """``invoice -> submitted Delivery Note`` in one query.

    ``_find_submitted_delivery_notes`` answers for a single invoice; the ledger
    lists hundreds, so it would be that many round trips. When an invoice has
    several notes the first by name is reported — the column is a "was it
    dispatched" hint, not an exhaustive list.
    """
    wanted = [n for n in invoice_names if n]
    if not wanted:
        return {}
    try:
        rows = frappe.get_all(
            "Delivery Note Item",
            filters={"against_sales_invoice": ["in", wanted], "docstatus": 1},
            fields=["parent", "against_sales_invoice"],
            limit_page_length=0,
        ) or []
    except Exception:
        _log_employee_ledger_error("Employee ledger: Delivery Note lookup failed")
        return {}
    mapping: Dict[str, str] = {}
    for row in rows:
        invoice = str(row.get("against_sales_invoice") or "")
        note = str(row.get("parent") or "")
        if not invoice or not note:
            continue
        current = mapping.get(invoice)
        if current is None or note < current:
            mapping[invoice] = note
    return mapping


def _employee_ledger_bucket(
    people: Dict[str, Dict[str, Any]],
    *,
    key: str,
    employee: str = "",
    employee_name: str = "",
    customer: str = "",
    user: str = "",
    branch: str = "",
) -> Dict[str, Any]:
    """Get-or-create the per-person rollup row addressed by ``key``.

    The two passes know different things — the advances pass knows the employee's
    branch and login, the orders pass knows their Customer — so later passes fill
    blanks but never overwrite a value that is already known.
    """
    row = people.get(key)
    if row is None:
        row = {
            "employee": employee,
            "employee_name": employee_name,
            "user": user,
            "branch": branch,
            "customer": customer,
            "advance_outstanding": 0.0,
            "order_outstanding": 0.0,
            "total_outstanding": 0.0,
            "advance_count": 0,
            "order_count": 0,
        }
        people[key] = row
        return row
    for field, value in (
        ("employee", employee),
        ("employee_name", employee_name),
        ("user", user),
        ("branch", branch),
        ("customer", customer),
    ):
        if value and not row.get(field):
            row[field] = value
    return row


def _empty_employee_ledger(
    *,
    filters: Dict[str, Any],
    currency: str,
    hrms: bool,
    notice_code: Optional[str] = None,
    notice: Optional[str] = None,
) -> Dict[str, Any]:
    """The ledger payload with every section empty and every total zeroed.

    Same keys as a populated response so the client never branches on shape —
    only ``notice_code`` tells it *why* it is empty.
    """
    payload: Dict[str, Any] = {
        "success": True,
        "hrms_available": hrms,
        "filters": filters,
        "summary": {
            "advance_outstanding": 0.0,
            "order_outstanding": 0.0,
            "total_outstanding": 0.0,
            "advance_count": 0,
            "order_count": 0,
            "employee_count": 0,
            "currency": currency,
            # Always present, even here, so the client can label the figure the
            # same way on every response instead of special-casing empty states.
            "outstanding_is_all_time": True,
        },
        "employees": [],
        "advances": [],
        "orders": [],
    }
    if notice_code:
        payload["notice_code"] = notice_code
        payload["notice"] = notice
    return payload


def _employee_ledger_advance_rows(
    start_date: Any,
    end_date: Any,
    selected_employee: str,
    limit: int,
) -> tuple:
    """Submitted, not-yet-settled Employee Advances in the window.

    Returns ``(rows, truncated)``. Every failure degrades to ``([], False)``:
    HRMS is not a ``required_app`` of this bench, so an absent or half-migrated
    Employee Advance table must leave the orders half of the ledger working.
    """
    filters: Dict[str, Any] = {
        "docstatus": 1,
        # Fully claimed / returned advances carry no balance and are history.
        "status": ["in", _EMPLOYEE_ADVANCE_OPEN_STATUSES],
        "posting_date": ["between", [f"{start_date} 00:00:00", f"{end_date} 23:59:59"]],
    }
    if selected_employee:
        filters["employee"] = selected_employee
    try:
        rows = frappe.get_all(
            "Employee Advance",
            filters=filters,
            fields=[
                "name",
                "employee",
                "employee_name",
                "posting_date",
                "advance_amount",
                "paid_amount",
                "claimed_amount",
                "return_amount",
                "status",
                "purpose",
                "advance_account",
                "currency",
            ],
            order_by="posting_date desc, modified desc",
            # +1 so truncation is detectable instead of guessed from a full page.
            limit_page_length=limit + 1,
        ) or []
    except Exception:
        _log_employee_ledger_error("Employee ledger: Employee Advance query failed")
        return [], False
    return rows[:limit], len(rows) > limit


def _employee_ledger_order_rows(
    start_date: Any,
    end_date: Any,
    profiles: List[str],
    customers: Optional[List[str]],
    limit: int,
    branch_field: str,
    state_field: Optional[str],
) -> tuple:
    """Submitted Employee-purpose Sales Invoices in the window, branch-scoped.

    Returns ``(rows, truncated)``. ``customers`` is ``None`` when no employee
    filter is active; an empty list means "the requested employee maps to no
    Customer", which is a real empty result rather than an unfiltered query.

    Credit notes (``is_return``) are deliberately NOT excluded: a returned staff
    order carries a negative grand total and a negative outstanding, so leaving it
    in is what makes the balance drop when the goods come back.
    """
    from jarz_pos.utils.employee_link import EMPLOYEE_ORDER_PURPOSE

    if customers is not None and not customers:
        return [], False
    try:
        if not frappe.get_meta("Sales Invoice").get_field("custom_order_purpose"):
            # Pre-fixture bench: the purpose field is not there yet, so no order
            # can be flagged as staff. Empty is correct; filtering would raise.
            return [], False
    except Exception:
        return [], False

    filters: Dict[str, Any] = {
        "docstatus": 1,
        "custom_order_purpose": EMPLOYEE_ORDER_PURPOSE,
        "posting_date": ["between", [f"{start_date} 00:00:00", f"{end_date} 23:59:59"]],
        branch_field: ["in", profiles],
    }
    if customers is not None:
        filters["customer"] = ["in", customers]

    fields = [
        "name",
        "customer",
        "customer_name",
        "posting_date",
        "grand_total",
        "outstanding_amount",
        "status",
        branch_field,
    ]
    if state_field:
        fields.append(state_field)

    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters=filters,
            fields=fields,
            order_by="posting_date desc, modified desc",
            limit_page_length=limit + 1,
        ) or []
    except Exception:
        _log_employee_ledger_error("Employee ledger: Sales Invoice query failed")
        return [], False
    return rows[:limit], len(rows) > limit


def _employee_ledger_advance_out_of_branch(
    employee_branch: str,
    known_pos_profiles: set,
    allowed_profiles: set,
) -> bool:
    """True when an advance belongs to a branch this user does not manage.

    ``Employee Advance`` carries no POS Profile, so the employee's own HR branch
    is the only attribution available — and it is blank for most staff here. An
    advance is therefore excluded ONLY when its branch actually names a POS
    Profile the caller does not run; blank or non-POS branches stay visible.

    Shared by the listing pass and the balance pass on purpose: two copies of
    this rule would eventually disagree about whose money a manager can see.
    """
    if not employee_branch:
        return False
    if employee_branch not in known_pos_profiles:
        return False
    return employee_branch not in allowed_profiles


def _employee_ledger_open_advance_rows(selected_employee: str) -> List[Dict[str, Any]]:
    """EVERY unsettled Employee Advance. No date filter, no row cap.

    This feeds the *balance*, and a balance has no window. An advance taken in
    January and never settled is precisely the row a manager opens this screen
    to find; filtering it by ``posting_date`` would drop it from a figure the
    client labels "total outstanding" — and the older and more delinquent the
    debt, the more certainly it would vanish.

    Uncapped for the same reason: truncating a list loses rows, truncating a
    balance states the wrong amount of money. The set is inherently small (only
    advances still in an open status) and only the fields the rollup needs are
    selected — the sum itself is done in Python, because ERPNext v16 rejects SQL
    functions in ``SELECT``.
    """
    filters: Dict[str, Any] = {
        "docstatus": 1,
        "status": ["in", _EMPLOYEE_ADVANCE_OPEN_STATUSES],
    }
    if selected_employee:
        filters["employee"] = selected_employee
    try:
        return frappe.get_all(
            "Employee Advance",
            filters=filters,
            fields=[
                "employee",
                "employee_name",
                "paid_amount",
                "claimed_amount",
                "return_amount",
            ],
            limit_page_length=0,
        ) or []
    except Exception:
        _log_employee_ledger_error("Employee ledger: open Employee Advance query failed")
        return []


def _employee_ledger_open_order_rows(
    profiles: List[str],
    customers: Optional[List[str]],
    branch_field: str,
) -> List[Dict[str, Any]]:
    """EVERY employee-purpose invoice still carrying an outstanding amount.

    All-time, uncapped, for the reasons in :func:`_employee_ledger_open_advance_rows`.
    A staff order unpaid since March is the whole point of the screen.

    Branch scoping still applies: "all-time" widens the DATE range, never the set
    of people whose money this user may see. Credit notes are included — their
    negative outstanding is what makes a returned order reduce the balance.
    """
    from jarz_pos.utils.employee_link import EMPLOYEE_ORDER_PURPOSE

    if customers is not None and not customers:
        return []
    try:
        if not frappe.get_meta("Sales Invoice").get_field("custom_order_purpose"):
            return []
    except Exception:
        return []

    filters: Dict[str, Any] = {
        "docstatus": 1,
        "custom_order_purpose": EMPLOYEE_ORDER_PURPOSE,
        branch_field: ["in", profiles],
        # Paid staff orders are history, not debt. Excluding them in SQL keeps
        # this query proportional to what is actually owed.
        "outstanding_amount": ["!=", 0],
    }
    if customers is not None:
        filters["customer"] = ["in", customers]

    try:
        return frappe.get_all(
            "Sales Invoice",
            filters=filters,
            fields=["customer", "customer_name", "outstanding_amount"],
            limit_page_length=0,
        ) or []
    except Exception:
        _log_employee_ledger_error("Employee ledger: open Sales Invoice query failed")
        return []


@frappe.whitelist(allow_guest=False)
def get_employee_ledger(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    branch: Optional[str] = None,
    employee: Optional[str] = None,
    limit: Union[int, str, None] = 200,
) -> Dict[str, Any]:
    """What each employee owes the company, advances and staff orders together.

    **THE DATE WINDOW DESCRIBES ACTIVITY. IT NEVER LIMITS THE BALANCE.**

    Read that twice before changing anything here, because collapsing the two
    back into one filtered query is a very natural "simplification" and it
    silently breaks the only number on the screen that matters:

    * ``advances`` and ``orders`` are the ROWS LISTED, and they honour
      ``from_date`` / ``to_date``. This is the activity feed.
    * ``summary.*_outstanding`` and ``employees[].*_outstanding`` are the
      BALANCE, computed over EVERY open item regardless of date. An advance
      taken in January, or a staff order unpaid since March, is exactly the debt
      a manager is hunting for — and under a windowed total it would contribute
      nothing while the client still labelled the figure "total outstanding".
      The older and more delinquent the debt, the more certainly it would
      disappear. ``summary.outstanding_is_all_time`` is always ``True`` so the
      client can say so out loud.

    A consequence, and it is correct: somebody can appear in ``employees`` with a
    real balance and an EMPTY advances/orders list, because all their activity
    predates the window. The rollup is driven by what is owed, not by what is
    listed. The reverse also holds — somebody whose only activity in the window
    was a paid order appears with a zero balance.

    Two sources, one balance per person:

    * **Advances** — submitted ``Employee Advance`` rows whose status still allows
      a balance. ``balance = paid_amount - (claimed_amount + return_amount)``,
      exactly as HRMS computes it in ``get_employee_advance_balance``. An advance
      that was approved but not yet disbursed (``Unpaid``, ``paid_amount`` 0) is
      listed for visibility and contributes 0 — nothing has left the company yet.
    * **Orders** — submitted Sales Invoices with ``custom_order_purpose =
      "Employee"``. Only ``outstanding_amount`` feeds the balance, which is the
      whole point of the design: a staff order settled at the till contributes 0
      while still appearing in ``orders`` as history, and one settled *on the
      employee's account* stays an unpaid receivable and shows up as debt.

    Dates default to the **last 90 days**, NOT to today like the shift monitor.
    A shift is a one-day object; an activity feed is not. Both listed sections
    are capped at ``limit`` rows (200 by default, 500 max); if either is
    truncated the payload says so via ``notice_code = "results_truncated"``
    rather than dropping rows quietly. The all-time balance queries are
    deliberately UNCAPPED — truncating a list loses rows, truncating a balance
    states the wrong amount of money.

    Branch scoping is not symmetric, because the two sources are not:

    * Orders carry the POS Profile and are hard-filtered to the branches this
      user is assigned to (managers included — only Administrator sees all).
    * ``Employee Advance`` has no POS Profile at all. It is attributed to the HR
      ``Employee.branch``, which is frequently blank on this site. So an advance
      is dropped only when its employee's branch names a POS Profile the caller
      does NOT manage; an advance nobody can attribute is shown to everybody who
      can open the ledger, because an advance nobody can see is one nobody
      chases.

    An order whose Customer resolves to no Employee is still listed and still
    counted, with ``employee = ""`` and the customer name standing in for the
    employee name. Dropping it would quietly shrink the amount owed.

    HRMS is optional on this bench: when ``Employee Advance`` does not exist the
    response carries ``hrms_available: False``, an empty ``advances`` list and a
    zeroed advance summary. It never throws.

    Args:
        from_date: Start of the ACTIVITY window (default: 89 days before today).
            Does not affect any outstanding figure.
        to_date: End of the ACTIVITY window (default: today). Likewise.
        branch: POS Profile to narrow to; omitted or "all" means every assigned
            branch. A branch the user is not assigned to returns an empty ledger
            with ``notice_code = "branch_not_permitted"``. Applies to the
            all-time balance queries too — "all-time" widens the date range,
            never the set of people whose money this user may see.
        employee: Restrict to one Employee (advances directly, orders through the
            Customer that Employee is linked to).
        limit: Max rows PER LISTED SECTION (advances, orders). Capped at 500.
            The balance is never capped.

    Returns:
        Flat dict: ``success``, ``hrms_available``, ``filters``, ``summary``,
        ``employees`` (per-person rollup, highest ``total_outstanding`` first),
        ``advances``, ``orders``, plus optional ``notice_code`` / ``notice``.

        ``summary.advance_count`` / ``order_count`` and
        ``employees[].advance_count`` / ``order_count`` count the ROWS LISTED IN
        THE WINDOW — they are activity counts, not balance counts, and a person
        with an out-of-window debt legitimately shows 0 against a non-zero
        amount. ``summary.employee_count`` is the other way round: the number of
        people who actually owe something all-time, so it can be smaller than
        ``len(employees)``.
    """
    _ensure_manager_dashboard_access()

    # Imported inside the endpoint, matching the local-import style used by
    # _build_shift_monitor_row / get_pos_shift_monitor for cross-module helpers.
    from jarz_pos.utils.employee_link import customers_for_employees, employees_for_customers, hrms_available

    try:
        limit = max(1, min(int(limit or 200), 500))
    except Exception:
        limit = 200

    end_date = _coerce_shift_monitor_date(to_date, nowdate())
    start_date = _coerce_shift_monitor_date(
        from_date, add_days(nowdate(), -(_EMPLOYEE_LEDGER_DEFAULT_DAYS - 1))
    )
    if start_date > end_date:
        frappe.throw(_("From date cannot be later than To date"))

    selected_branch = str(branch or "").strip()
    selected_employee = str(employee or "").strip()
    hrms = bool(hrms_available())
    currency = _employee_ledger_currency()
    filters_echo: Dict[str, Any] = {
        "from_date": str(start_date),
        "to_date": str(end_date),
        "branch": selected_branch or None,
        "employee": selected_employee or None,
    }

    allowed = _current_user_allowed_profiles()
    if not allowed:
        # Passing the role gate while owning no branch is a setup problem, not an
        # empty ledger — say which, exactly as the other dashboard endpoints do.
        return _empty_employee_ledger(
            filters=filters_echo,
            currency=currency,
            hrms=hrms,
            notice_code="no_branch_assigned",
            notice=_(
                "You are not assigned to any branch (POS Profile). Ask an "
                "administrator to add you to the branches you manage."
            ),
        )

    profiles = list(allowed)
    if selected_branch and selected_branch.lower() != "all":
        if selected_branch not in allowed:
            return _empty_employee_ledger(
                filters=filters_echo,
                currency=currency,
                hrms=hrms,
                notice_code="branch_not_permitted",
                notice=_("You are not assigned to branch {0}.").format(selected_branch),
            )
        profiles = [selected_branch]

    branch_field = _employee_ledger_branch_field()
    state_field = _employee_ledger_state_field()
    customer_filter: Optional[List[str]] = None
    if selected_employee:
        # One employee -> the Customer they order as. No Customer means no staff
        # orders exist for them, which is an empty list, not an unfiltered query.
        customer_filter = [
            c for c in (customers_for_employees([selected_employee]) or {}).values() if c
        ]

    # --- What is LISTED: the activity window --------------------------------
    advance_rows: List[Dict[str, Any]] = []
    advances_truncated = False
    if hrms:
        advance_rows, advances_truncated = _employee_ledger_advance_rows(
            start_date, end_date, selected_employee, limit
        )
    order_rows, orders_truncated = _employee_ledger_order_rows(
        start_date, end_date, profiles, customer_filter, limit, branch_field, state_field
    )

    # --- What is OWED: every open item, all time ----------------------------
    # Deliberately NOT date-filtered. See the docstring: the window says which
    # rows are listed, never how much is owed. These two queries are the entire
    # reason the ledger can be trusted as a balance.
    open_advance_rows = _employee_ledger_open_advance_rows(selected_employee) if hrms else []
    open_order_rows = _employee_ledger_open_order_rows(profiles, customer_filter, branch_field)

    # --- Join: Customer -> Employee, once across BOTH row sets --------------
    order_customers = sorted({
        str(r.get("customer") or "")
        for r in (list(order_rows) + list(open_order_rows))
        if r.get("customer")
    })
    customer_to_employee = employees_for_customers(order_customers) if order_customers else {}

    employee_ids = {
        str(r.get("employee") or "")
        for r in (list(advance_rows) + list(open_advance_rows))
        if r.get("employee")
    }
    employee_ids.update(str(e) for e in customer_to_employee.values() if e)
    emp_meta = _employee_ledger_employee_meta(sorted(employee_ids))

    # Customer per employee, so an employee with advances but no orders in the
    # window still shows the account their staff orders would land on.
    employee_to_customer = customers_for_employees(sorted(employee_ids)) if employee_ids else {}

    # POS Profiles that exist at all — used to tell "this employee belongs to a
    # branch I do not manage" apart from "this employee's branch is not a POS
    # branch (or is blank)", which must NOT be filtered away.
    known_pos_profiles = set(_get_all_active_pos_profiles())
    allowed_profiles = set(profiles)

    # ``_shift_monitor_user_details`` resolves a login the same way the shift
    # monitor does, so the same person is labelled identically on both screens.
    user_cache: Dict[str, Dict[str, Optional[str]]] = {}

    people: Dict[str, Dict[str, Any]] = {}

    def _person(employee_id: str, customer: str, display_name: str) -> Dict[str, Any]:
        """The rollup row for one person, keyed identically from every pass.

        Four passes write here — two balance, two listing — and they must land on
        the same row or a person's debt and their activity end up on separate
        lines. An order whose Customer resolves to no Employee keeps its own
        customer-keyed bucket rather than collapsing onto one anonymous row.
        """
        if employee_id:
            key = f"EMP:{employee_id}"
        elif customer:
            key = f"CUST:{customer}"
        else:
            key = "UNATTRIBUTED"
        meta = emp_meta.get(employee_id, {}) if employee_id else {}
        return _employee_ledger_bucket(
            people,
            key=key,
            employee=employee_id,
            employee_name=display_name,
            customer=customer or (str(employee_to_customer.get(employee_id) or "") if employee_id else ""),
            user=str(meta.get("user") or ""),
            branch=str(meta.get("branch") or ""),
        )

    # --- Balance pass 1: advances (money only, all time) --------------------
    advance_total = 0.0
    for row in open_advance_rows:
        emp = str(row.get("employee") or "")
        meta = emp_meta.get(emp, {})
        if _employee_ledger_advance_out_of_branch(
            str(meta.get("branch") or ""), known_pos_profiles, allowed_profiles
        ):
            continue
        # HRMS's own arithmetic. A negative balance (claimed beyond what was
        # paid) is left signed on purpose: it nets against the person's other
        # advances, which is the truthful "what do they owe overall".
        balance = flt(row.get("paid_amount")) - (
            flt(row.get("claimed_amount")) + flt(row.get("return_amount"))
        )
        if not balance:
            # Approved-but-undisbursed (nothing has left the company) and
            # exactly-settled advances owe nothing.
            continue
        advance_total += balance
        # The rollup names the PERSON, so the live Employee record wins here.
        # The listed advance row below keeps the name stored on the document,
        # which is what HRMS shows when you open it.
        _person(
            emp, "", str(meta.get("employee_name") or row.get("employee_name") or emp)
        )["advance_outstanding"] += balance

    # --- Balance pass 2: orders (money only, all time) ----------------------
    order_total = 0.0
    for row in open_order_rows:
        customer = str(row.get("customer") or "")
        customer_name = str(row.get("customer_name") or customer)
        emp = str(customer_to_employee.get(customer) or "")
        outstanding = flt(row.get("outstanding_amount"))
        if not outstanding:
            continue
        order_total += outstanding
        display = str(emp_meta.get(emp, {}).get("employee_name") or "") if emp else ""
        _person(emp, customer, display or customer_name)["order_outstanding"] += outstanding

    # --- Listing pass 1: advances in the window -----------------------------
    advances_payload: List[Dict[str, Any]] = []
    advance_count = 0

    for row in advance_rows:
        emp = str(row.get("employee") or "")
        meta = emp_meta.get(emp, {})
        emp_branch = str(meta.get("branch") or "")
        if _employee_ledger_advance_out_of_branch(
            emp_branch, known_pos_profiles, allowed_profiles
        ):
            continue

        emp_name = str(row.get("employee_name") or meta.get("employee_name") or emp)
        emp_user = str(meta.get("user") or "")
        if emp_user:
            details = _shift_monitor_user_details(emp_user, user_cache)
            emp_name = str(details.get("employee_name") or emp_name)

        paid = flt(row.get("paid_amount"))
        claimed = flt(row.get("claimed_amount"))
        returned = flt(row.get("return_amount"))
        # Per-row balance, for the feed. The person's TOTAL came from the
        # all-time pass above and is not touched here.
        balance = paid - (claimed + returned)

        advances_payload.append({
            "name": row.get("name"),
            "employee": emp,
            "employee_name": emp_name,
            "posting_date": str(row.get("posting_date") or ""),
            "amount": flt(row.get("advance_amount"), 2),
            "paid_amount": flt(paid, 2),
            "claimed_amount": flt(claimed, 2),
            "return_amount": flt(returned, 2),
            "balance": flt(balance, 2),
            "status": row.get("status"),
            "purpose": row.get("purpose"),
            "branch": emp_branch,
            # Employee Advance names the account the money was paid FROM; there
            # is no "paying_account" field on the DocType.
            "paying_account": row.get("advance_account"),
            "currency": str(row.get("currency") or currency),
        })

        advance_count += 1
        # Counts only. The money was already taken from the all-time pass, and
        # adding it again here would double it for anything inside the window.
        _person(emp, "", emp_name)["advance_count"] += 1

    # --- Listing pass 2: orders in the window -------------------------------
    delivery_notes = _employee_ledger_delivery_notes([str(r.get("name") or "") for r in order_rows])
    orders_payload: List[Dict[str, Any]] = []
    order_count = 0

    for row in order_rows:
        invoice = str(row.get("name") or "")
        customer = str(row.get("customer") or "")
        customer_name = str(row.get("customer_name") or customer)
        emp = str(customer_to_employee.get(customer) or "")
        meta = emp_meta.get(emp, {}) if emp else {}
        # An unlinked customer keeps its own bucket instead of collapsing every
        # unlinked order onto one anonymous row — the money stays chaseable.
        emp_name = str(meta.get("employee_name") or "") if emp else ""
        if not emp_name:
            emp_name = customer_name
        outstanding = flt(row.get("outstanding_amount"))

        orders_payload.append({
            "invoice": invoice,
            "employee": emp,
            "employee_name": emp_name,
            "customer": customer,
            "customer_name": customer_name,
            "branch": str(row.get(branch_field) or ""),
            "posting_date": str(row.get("posting_date") or ""),
            "grand_total": flt(row.get("grand_total"), 2),
            "outstanding_amount": flt(outstanding, 2),
            "status": row.get("status"),
            "state": row.get(state_field) if state_field else None,
            "delivery_note": delivery_notes.get(invoice),
        })

        order_count += 1
        # Counts only — same reason as the advances pass above.
        _person(emp, customer, emp_name)["order_count"] += 1

    employees_payload: List[Dict[str, Any]] = []
    for row in people.values():
        row["advance_outstanding"] = flt(row["advance_outstanding"], 2)
        row["order_outstanding"] = flt(row["order_outstanding"], 2)
        row["total_outstanding"] = flt(row["advance_outstanding"] + row["order_outstanding"], 2)
        employees_payload.append(row)
    # Biggest debt first; the name breaks ties so the order is stable between
    # requests (a jumping table reads as data changing when it has not).
    employees_payload.sort(key=lambda r: (-r["total_outstanding"], str(r.get("employee_name") or "")))

    # "How many people owe us money" — a balance question, so it counts balances,
    # not listed rows. Somebody whose only activity in the window was a paid
    # order is still IN `employees` (at zero) but is not one of these.
    employee_count = sum(1 for row in employees_payload if row["total_outstanding"])

    payload: Dict[str, Any] = {
        "success": True,
        "hrms_available": hrms,
        "filters": filters_echo,
        "summary": {
            # All-time, by design. See the docstring.
            "advance_outstanding": flt(advance_total, 2),
            "order_outstanding": flt(order_total, 2),
            "total_outstanding": flt(advance_total + order_total, 2),
            # Activity counts: rows listed inside the window.
            "advance_count": advance_count,
            "order_count": order_count,
            "employee_count": employee_count,
            "currency": currency,
            # Lets the client label the figure honestly instead of implying the
            # date filter applies to it.
            "outstanding_is_all_time": True,
        },
        "employees": employees_payload,
        "advances": advances_payload,
        "orders": orders_payload,
    }

    if not hrms:
        payload["notice_code"] = "hrms_unavailable"
        payload["notice"] = _(
            "HR module is not installed, so cash advances are not included. "
            "Only employee orders are shown."
        )
    elif advances_truncated or orders_truncated:
        # Truncation is reported, never silent. The wording is careful: only the
        # LIST is cut. The outstanding totals are all-time and uncapped, so they
        # remain correct — saying otherwise would send a manager chasing a
        # shortfall that does not exist.
        payload["notice_code"] = "results_truncated"
        payload["notice"] = _(
            "Showing the first {0} rows per section. The outstanding totals are "
            "complete; only this list is cut short. Narrow the date range, a "
            "branch or an employee to see the rest."
        ).format(limit)

    return payload


@frappe.whitelist(allow_guest=False)
def get_invoice_warehouse_alignment_report(
    company: Optional[str] = None,
    branch: Optional[str] = None,
    limit: Union[int, str] = 100,
) -> Dict[str, Any]:
    """List submitted POS invoices whose item warehouses no longer match the operational branch."""
    _ensure_manager_dashboard_access()
    frappe.has_permission("Sales Invoice", throw=True)

    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.ADMIN):
        frappe.throw(_("Not permitted: administrator access required"), frappe.PermissionError)

    try:
        limit_value = max(1, min(int(limit or 100), 500))
    except Exception:
        limit_value = 100

    filters: Dict[str, Any] = {"docstatus": 1, "is_pos": 1}
    if company:
        filters["company"] = company

    report_rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=["name", "company", "customer", "posting_date", "custom_kanban_profile", "pos_profile"],
        order_by="modified desc",
        limit_page_length=limit_value,
    ) or []

    misaligned_invoices: List[Dict[str, Any]] = []
    for row in report_rows:
        invoice_name = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
        if not invoice_name:
            continue

        inv = frappe.get_doc("Sales Invoice", invoice_name)
        operational_profile = inv.get("custom_kanban_profile") or inv.get("pos_profile")
        if branch and operational_profile != branch:
            continue

        submitted_delivery_notes = _find_submitted_delivery_notes(inv.name)
        if submitted_delivery_notes:
            continue

        if not operational_profile:
            misaligned_invoices.append(
                {
                    "invoice_id": inv.name,
                    "company": inv.get("company"),
                    "customer": inv.get("customer"),
                    "operational_profile": None,
                    "target_warehouse": None,
                    "delivery_notes": [],
                    "issue": "Invoice has no operational POS Profile configured.",
                    "mismatches": [],
                }
            )
            continue

        try:
            expected_warehouse = _resolve_pos_profile_warehouse(operational_profile)
            _validate_transfer_target_warehouse(inv, expected_warehouse)
        except frappe.ValidationError as validation_error:
            misaligned_invoices.append(
                {
                    "invoice_id": inv.name,
                    "company": inv.get("company"),
                    "customer": inv.get("customer"),
                    "operational_profile": operational_profile,
                    "target_warehouse": None,
                    "delivery_notes": [],
                    "issue": str(validation_error),
                    "mismatches": [],
                }
            )
            continue

        mismatches = _get_invoice_warehouse_mismatches(inv, expected_warehouse)
        if mismatches:
            misaligned_invoices.append(
                {
                    "invoice_id": inv.name,
                    "company": inv.get("company"),
                    "customer": inv.get("customer"),
                    "operational_profile": operational_profile,
                    "target_warehouse": expected_warehouse,
                    "delivery_notes": [],
                    "issue": "Invoice item warehouses do not match the operational branch warehouse.",
                    "mismatches": mismatches,
                }
            )

    return {"success": True, "count": len(misaligned_invoices), "invoices": misaligned_invoices}


@frappe.whitelist(allow_guest=False)
def repair_invoice_warehouse_alignment(
    company: Optional[str] = None,
    branch: Optional[str] = None,
    limit: Union[int, str] = 100,
    apply_changes: Union[bool, int, str, None] = False,
) -> Dict[str, Any]:
    """Dry-run or repair misaligned submitted invoices before Delivery Note creation."""
    _ensure_manager_dashboard_access()
    frappe.has_permission("Sales Invoice", throw=True)

    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.ADMIN):
        frappe.throw(_("Not permitted: administrator access required"), frappe.PermissionError)

    report = get_invoice_warehouse_alignment_report(company=company, branch=branch, limit=limit)
    apply_mode = _is_truthy_flag(apply_changes)
    if not apply_mode:
        report["mode"] = "dry_run"
        return report

    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    meta = frappe.get_meta("Sales Invoice")

    for entry in report.get("invoices", []):
        invoice_id = entry.get("invoice_id")
        if not invoice_id:
            continue

        inv = frappe.get_doc("Sales Invoice", invoice_id)
        operational_profile = inv.get("custom_kanban_profile") or inv.get("pos_profile")
        if not operational_profile:
            skipped.append({"invoice_id": invoice_id, "issue": "Invoice has no operational POS Profile configured."})
            continue

        if _find_submitted_delivery_notes(inv.name):
            skipped.append({
                "invoice_id": invoice_id,
                "issue": "Invoice already has a submitted Delivery Note; branch transfer is no longer allowed.",
            })
            continue

        try:
            target_warehouse = _resolve_pos_profile_warehouse(operational_profile)
            _validate_transfer_target_warehouse(inv, target_warehouse)
            mismatches = _get_invoice_warehouse_mismatches(inv, target_warehouse)
            if not mismatches:
                skipped.append({"invoice_id": invoice_id, "issue": "Invoice is already aligned."})
                continue

            source_warehouses = sorted({mismatch.get("warehouse") for mismatch in mismatches if mismatch.get("warehouse")})
            for row in _get_transfer_stock_rows(inv):
                current_warehouse = str(getattr(row, "warehouse", "") or "").strip()
                if current_warehouse != target_warehouse:
                    frappe.db.set_value("Sales Invoice Item", row.name, "warehouse", target_warehouse, update_modified=False)
                    row.warehouse = target_warehouse

            if meta.get_field("set_warehouse"):
                frappe.db.set_value("Sales Invoice", inv.name, "set_warehouse", target_warehouse, update_modified=True)

            try:
                source_warehouse_label = ", ".join(source_warehouses) if source_warehouses else "none"
                inv.add_comment(
                    "Edit",
                    f"Warehouse alignment repair moved item warehouses from {source_warehouse_label} to {target_warehouse} for active kanban profile {operational_profile} by {frappe.session.user}.",
                )
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Invoice warehouse alignment repair comment failed")

            frappe.db.commit()
            applied.append(
                {
                    "invoice_id": inv.name,
                    "operational_profile": operational_profile,
                    "target_warehouse": target_warehouse,
                    "repaired_rows": len(mismatches),
                }
            )
        except Exception as exc:
            frappe.db.rollback()
            skipped.append({"invoice_id": invoice_id, "issue": str(exc)})

    return {
        "success": True,
        "mode": "apply",
        "count": report.get("count", 0),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
    }


@frappe.whitelist(allow_guest=False)
def update_cancelled_invoice_status_fields(
    invoice_id: str,
    sales_invoice_state: Optional[str] = None,
    acceptance_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Update selected workflow fields on a cancelled Sales Invoice.

    This keeps cancelled documents immutable in general while allowing managers
    to correct the two Jarz workflow fields that still matter operationally.
    """
    _ensure_manager_dashboard_access()

    invoice_id = (invoice_id or "").strip()
    requested_state = (sales_invoice_state or "").strip()
    requested_acceptance = (acceptance_status or "").strip()

    if not invoice_id:
        return {"success": False, "error": "invoice_id is required"}
    if not requested_state and not requested_acceptance:
        return {"success": False, "error": "At least one field update is required"}

    try:
        inv = frappe.get_doc("Sales Invoice", invoice_id)
        frappe.has_permission("Sales Invoice", doc=inv, ptype="write", throw=True)

        if int(inv.get("docstatus") or 0) != 2:
            return {"success": False, "error": "Only cancelled Sales Invoices can be updated with this action"}

        meta = frappe.get_meta("Sales Invoice")
        updates: Dict[str, Any] = {}
        update_fragments: List[str] = []

        if requested_state:
            state_options = _get_state_field_options()
            canonical_state = _match_option(requested_state, state_options) if state_options else requested_state
            if not canonical_state:
                return {"success": False, "error": f"Invalid Sales Invoice State: {requested_state}"}

            state_fields = [
                field_name
                for field_name in ("custom_sales_invoice_state", "sales_invoice_state")
                if meta.get_field(field_name)
            ]
            if not state_fields:
                return {"success": False, "error": "Sales Invoice State field was not found on Sales Invoice"}

            if any(str(inv.get(field_name) or "").strip() != canonical_state for field_name in state_fields):
                for field_name in state_fields:
                    updates[field_name] = canonical_state
                update_fragments.append(f"Sales Invoice State = {canonical_state}")

        if requested_acceptance:
            acceptance_options = _get_acceptance_field_options()
            canonical_acceptance = _match_option(requested_acceptance, acceptance_options) if acceptance_options else requested_acceptance
            if not canonical_acceptance:
                return {"success": False, "error": f"Invalid Acceptance Status: {requested_acceptance}"}

            if not meta.get_field("custom_acceptance_status"):
                return {"success": False, "error": "Acceptance Status field was not found on Sales Invoice"}

            current_acceptance = str(inv.get("custom_acceptance_status") or "").strip()
            if current_acceptance != canonical_acceptance:
                updates["custom_acceptance_status"] = canonical_acceptance
                if meta.get_field("custom_accepted_by"):
                    updates["custom_accepted_by"] = frappe.session.user if canonical_acceptance.lower() == "accepted" else None
                if meta.get_field("custom_accepted_on"):
                    updates["custom_accepted_on"] = frappe.utils.now_datetime() if canonical_acceptance.lower() == "accepted" else None
                update_fragments.append(f"Acceptance Status = {canonical_acceptance}")

        if not updates:
            return {
                "success": True,
                "invoice_id": inv.name,
                "sales_invoice_state": inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state"),
                "acceptance_status": inv.get("custom_acceptance_status"),
                "no_change": True,
            }

        frappe.db.set_value("Sales Invoice", inv.name, updates, update_modified=True)
        inv.reload()

        try:
            inv.add_comment(
                "Edit",
                f"Cancelled invoice fields updated by {frappe.session.user}: {'; '.join(update_fragments)}",
            )
        except Exception:
            pass

        return {
            "success": True,
            "invoice_id": inv.name,
            "sales_invoice_state": inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state"),
            "acceptance_status": inv.get("custom_acceptance_status"),
            "accepted_by": inv.get("custom_accepted_by"),
            "accepted_on": inv.get("custom_accepted_on"),
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "update_cancelled_invoice_status_fields failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def update_invoice_branch(invoice_id: str, new_branch: str) -> Dict[str, Any]:
    """Reassign a submitted POS invoice by updating custom_kanban_profile.

    This is the only supported post-submit branch transfer path.

    Rules:
    - Only for submitted POS invoices (docstatus=1 and is_pos=1).
    - The target POS Profile must exist and be enabled. It does NOT have to be one
      of the current user's assigned profiles — staff may transfer to any enabled branch.
    - Only custom_kanban_profile and transfer-related Kanban workflow fields are updated.
    - pos_profile remains unchanged after submit.
    - Source-side scoping is enforced: the user may only transfer an order whose current
      profile is in their assigned POS Profiles (via _ensure_profile_scoped_invoice_access).
    - The reassignment touches modified and emits a realtime refresh event for other sessions.
    """
    try:
        frappe.logger().info(f"Transfer invoice request: {invoice_id} -> {new_branch}")
        
        if not invoice_id:
            return {"success": False, "error": "invoice_id is required"}
        if not new_branch:
            return {"success": False, "error": "new_branch is required"}

        if not frappe.db.exists("Sales Invoice", invoice_id):
            return {"success": False, "error": f"Sales Invoice {invoice_id} was not found"}

        if not frappe.db.exists("POS Profile", new_branch):
            return {"success": False, "error": f"Target POS Profile {new_branch} was not found"}

        if int(frappe.db.get_value("POS Profile", new_branch, "disabled") or 0) == 1:
            return {"success": False, "error": f"Target POS Profile {new_branch} is disabled"}

        inv = frappe.get_doc("Sales Invoice", invoice_id)
        # Source-side scoping only: the user may transfer orders that belong to their
        # assigned profiles, but the target (new_branch) may be ANY enabled profile.
        _ensure_profile_scoped_invoice_access(
            inv,
            action_label="invoice transfer",
        )
        
        frappe.logger().info(f"Invoice docstatus: {inv.get('docstatus')}, is_pos: {inv.get('is_pos')}")
        
        if int(inv.get("docstatus") or 0) != 1 or int(inv.get("is_pos") or 0) != 1:
            return {"success": False, "error": "Only submitted POS invoices can be reassigned"}
        meta = frappe.get_meta("Sales Invoice")
        if not meta.get_field("custom_kanban_profile"):
            return {"success": False, "error": "custom_kanban_profile field not found on Sales Invoice"}

        current_state = (
            inv.get("custom_sales_invoice_state")
            or inv.get("sales_invoice_state")
            or inv.get("custom_state")
            or inv.get("state")
            or "Received"
        )
        current_branch = inv.get("custom_kanban_profile") or inv.get("pos_profile")

        frappe.logger().info(f"Current state: '{current_state}'")
        frappe.logger().info(
            f"Invoice transfer validated: invoice={inv.name}, user={frappe.session.user}, old_branch={current_branch}, new_branch={new_branch}, state={current_state}"
        )
        
        # Normalize the state for comparison (strip and lowercase)
        normalized_state = str(current_state).strip().lower()
        
        frappe.logger().info(f"Normalized state: '{normalized_state}', Allowed: {_ALLOWED_TRANSFER_STATES}")
        
        # Only allow transfer from Received, In Progress, or Ready states
        if normalized_state not in _ALLOWED_TRANSFER_STATES:
            frappe.log_error(
                f"Invoice {invoice_id} transfer blocked. State: '{current_state}' (normalized: '{normalized_state}'). Allowed: {_ALLOWED_TRANSFER_STATES}",
                "Invoice Transfer State Check"
            )
            return {
                "success": False,
                "error": f"Invoice can only be transferred when state is Received, In Progress, or Ready. Current state: {current_state}",
            }

        existing_delivery_notes = _find_submitted_delivery_notes(inv.name)
        if existing_delivery_notes:
            return {
                "success": False,
                "error": "Invoice already has a submitted Delivery Note; branch transfer is no longer allowed.",
            }

        try:
            target_warehouse = _resolve_pos_profile_warehouse(new_branch)
            _validate_transfer_target_warehouse(inv, target_warehouse)
        except frappe.ValidationError as validation_error:
            return {"success": False, "error": str(validation_error)}

        stock_rows = _get_transfer_stock_rows(inv)
        source_warehouses = sorted({str(getattr(row, "warehouse", "") or "").strip() for row in stock_rows if str(getattr(row, "warehouse", "") or "").strip()})

        state_fields: List[str] = []
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                state_fields.append(candidate)

        # Only update custom_kanban_profile, NOT pos_profile
        # pos_profile is read-only after invoice submission and cannot be changed
        updates: Dict[str, Any] = {"custom_kanban_profile": new_branch}
        if meta.get_field("set_warehouse"):
            updates["set_warehouse"] = target_warehouse
        
        # Reset to Received state when transferring
        target_received = "Received"
        try:
            options = _get_state_field_options() or []
            # Prefer exact option match (case-insensitive) for Received / Recieved
            for opt in options:
                if opt.strip().lower() in {"received", "recieved"}:
                    target_received = opt.strip()
                    break
        except Exception:
            pass
        for field in state_fields:
            updates[field] = target_received

        # Reset acceptance status
        for field, value in {
            "custom_acceptance_status": "Pending",
            "custom_accepted_by": None,
            "custom_accepted_on": None,
        }.items():
            if meta.get_field(field):
                updates[field] = value

        # Handle delivery time: try to find closest matching period in new POS profile
        current_time_from = inv.get("custom_delivery_time_from")
        current_time_to = inv.get("custom_delivery_time_to") 
        current_delivery_date = inv.get("custom_delivery_date")
        
        if current_time_from and meta.get_field("custom_delivery_time_from"):
            try:
                # Get delivery periods from new POS profile
                new_profile_doc = frappe.get_doc("POS Profile", new_branch)
                delivery_periods = new_profile_doc.get("custom_delivery_periods") or []
                
                if delivery_periods:
                    # Find closest matching period based on time_from
                    from datetime import datetime, time
                    current_time = datetime.strptime(str(current_time_from), "%H:%M:%S").time() if isinstance(current_time_from, str) else current_time_from
                    
                    closest_period = None
                    min_diff = float('inf')
                    
                    for period in delivery_periods:
                        period_from = period.get("time_from")
                        if period_from:
                            period_time = datetime.strptime(str(period_from), "%H:%M:%S").time() if isinstance(period_from, str) else period_from
                            # Calculate time difference in minutes
                            diff = abs((datetime.combine(datetime.today(), current_time) - 
                                      datetime.combine(datetime.today(), period_time)).total_seconds() / 60)
                            if diff < min_diff:
                                min_diff = diff
                                closest_period = period
                    
                    if closest_period:
                        updates["custom_delivery_time_from"] = closest_period.get("time_from")
                        updates["custom_delivery_time_to"] = closest_period.get("time_to")
                        if meta.get_field("custom_delivery_duration"):
                            updates["custom_delivery_duration"] = closest_period.get("duration")
                        if meta.get_field("custom_delivery_slot_label"):
                            raw_label = closest_period.get("label") or ""
                            if raw_label:
                                updates["custom_delivery_slot_label"] = raw_label
                            else:
                                tf = closest_period.get("time_from") or ""
                                tt = closest_period.get("time_to") or ""
                                try:
                                    from datetime import datetime as _dt
                                    tf_ampm = _dt.strptime(tf.split(".")[0], "%H:%M:%S").strftime("%I:%M %p") if tf else tf
                                    tt_ampm = _dt.strptime(tt.split(".")[0], "%H:%M:%S").strftime("%I:%M %p") if tt else tt
                                    updates["custom_delivery_slot_label"] = f"{tf_ampm} - {tt_ampm}"
                                except Exception:
                                    updates["custom_delivery_slot_label"] = f"{tf} - {tt}"
            except Exception as e:
                frappe.log_error(f"Error updating delivery time during transfer: {str(e)}", "Invoice Transfer")

        # Use flags to bypass validation and permission checks for submitted invoices
        frappe.flags.ignore_permissions = True
        frappe.flags.ignore_validate = True
        
        try:
            for field, value in updates.items():
                frappe.db.set_value("Sales Invoice", inv.name, field, value, update_modified=True)
            for row in stock_rows:
                if not getattr(row, "name", None):
                    raise frappe.ValidationError(_(f"Invoice row for item {getattr(row, 'item_code', '?')} is missing a name and cannot be moved."))
                current_warehouse = str(getattr(row, "warehouse", "") or "").strip()
                if current_warehouse != target_warehouse:
                    frappe.db.set_value("Sales Invoice Item", row.name, "warehouse", target_warehouse, update_modified=False)
                    row.warehouse = target_warehouse
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(f"Error setting values during transfer: {str(e)}\nUpdates: {updates}", "Invoice Transfer")
            frappe.db.rollback()
            return {"success": False, "error": f"Failed to update invoice fields: {str(e)}"}
        finally:
            frappe.flags.ignore_permissions = False
            frappe.flags.ignore_validate = False

        inv.reload()

        try:
            source_warehouse_label = ", ".join(source_warehouses) if source_warehouses else "none"
            inv.add_comment(
                "Edit",
                f"Invoice transferred from {current_branch} to {new_branch}. Item warehouses moved from {source_warehouse_label} to {target_warehouse} by {frappe.session.user}.",
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Invoice transfer audit comment failed")

        try:
            _publish_invoice_reassignment_refresh(
                inv,
                old_branch=current_branch,
                new_branch=new_branch,
                old_state=current_state,
                new_state=target_received,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Invoice reassignment realtime publish failed during transfer")

        try:
            notify_invoice_reassignment(inv, new_branch)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "notify_invoice_reassignment failed during transfer")
        try:
            frappe.db.commit()
        except Exception:
            pass
        frappe.logger().info(
            f"Invoice transfer completed: invoice={inv.name}, user={frappe.session.user}, old_branch={current_branch}, new_branch={new_branch}, old_state={current_state}, new_state={target_received}"
        )
        return {
            "success": True,
            "invoice_id": invoice_id,
            "new_branch": new_branch,
            "new_state": target_received,
            "target_warehouse": target_warehouse,
        }
    except Exception as e:
        frappe.logger().error(f"Update Invoice Branch Error: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Manager API - Update Invoice Branch")
        return {"success": False, "error": str(e)}

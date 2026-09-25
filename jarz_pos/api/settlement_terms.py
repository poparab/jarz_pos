"""B2B settlement terms: when each shop is expected to pay its credit balance.

Endpoints (all ``allow_guest=False``):

* :func:`get_settlement_terms` -- one customer's terms + where they stand.
* :func:`save_settlement_terms` -- upsert a customer's terms.
* :func:`delete_settlement_terms` -- drop a customer's terms (back to "unscheduled").
* :func:`get_collections_due` -- every customer with something to collect.

REMINDERS, NOT ENFORCEMENT. Nothing here blocks an order; the owner asked for a
schedule, reminders and a list (2026-09-25). The rules live in the pure
``services/settlement_schedule``; this module reads rows and shapes payloads.

Credit invoices are identified EXACTLY as ``api/credit`` identifies them, by
reusing its ``_open_credit_invoices`` (the OR over the mutable payment method
and the frozen ``custom_credit_terms_days`` stamp -- see ``utils/credit_utils``).
Keying on ``custom_payment_method`` alone would lose every debt a manager has
relabelled with "Change collection method".

Access mirrors the credit screens: reading is the credit-ledger gate, writing
is the credit-payment gate (``ROLES.ADMIN | ROLES.LINE_MANAGER_TIER``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

from jarz_pos.api.credit import (
    _allowed_profiles,
    _credit_currency,
    _ensure_credit_ledger_access,
    _ensure_credit_payment_access,
    _log_credit_error,
    _open_credit_invoices,
)
from jarz_pos.constants import ROLES
from jarz_pos.services import settlement_schedule as ss

TERMS_DOCTYPE = "Jarz Settlement Terms"

_TERMS_FIELDS = [
    "name",
    "customer",
    "customer_name",
    "enabled",
    "cycle",
    "weekdays",
    "week_interval",
    "month_days",
    "interval_days",
    "anchor_date",
    "remind_days_before",
    "overdue_repeat_days",
    "responsible_user",
    "notes",
    "last_reminder_on",
    "last_reminder_kind",
    "creation",
    "modified",
]

#: Collections "due soon" look-ahead bounds (days).
_DAYS_AHEAD_DEFAULT = 7
_DAYS_AHEAD_MAX = 90

#: The approvals badge is polled every minute by every manager; the count
#: walks every open credit invoice, so it is cached per user this long.
_COUNT_CACHE_PREFIX = "jarz_pos:credit_collections_count:"
_COUNT_CACHE_SECONDS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today():
    return getdate(nowdate())


def doctype_ready() -> bool:
    """False until ``bench migrate`` has created the DocType on this site."""
    try:
        return bool(frappe.db.exists("DocType", TERMS_DOCTYPE))
    except Exception:
        return False


def _can_edit() -> bool:
    """Same role set ``credit._ensure_credit_payment_access`` enforces, as a predicate."""
    try:
        roles = {str(r or "").strip() for r in (frappe.get_roles() or []) if str(r or "").strip()}
    except Exception:
        return False
    return bool(roles.intersection(ROLES.ADMIN | ROLES.LINE_MANAGER_TIER))


def _load_terms_row(customer: str) -> Optional[Dict[str, Any]]:
    if not doctype_ready():
        return None
    rows = frappe.get_all(
        TERMS_DOCTYPE,
        filters={"customer": customer},
        fields=_TERMS_FIELDS,
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _load_terms_rows(customers: List[str]) -> Dict[str, Dict[str, Any]]:
    if not customers or not doctype_ready():
        return {}
    try:
        rows = frappe.get_all(
            TERMS_DOCTYPE,
            filters={"customer": ["in", customers]},
            fields=_TERMS_FIELDS,
            limit_page_length=0,
        )
    except Exception:
        _log_credit_error("Settlement terms: terms query failed")
        return {}
    return {str(r.get("customer")): r for r in rows or [] if r.get("customer")}


def _invoice_inputs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "name": r.get("name"),
            "posting_date": r.get("posting_date"),
            "outstanding_amount": r.get("outstanding_amount"),
        }
        for r in rows or []
    ]


def _user_full_name(user: Optional[str]) -> Optional[str]:
    if not user:
        return None
    try:
        return frappe.db.get_value("User", user, "full_name") or user
    except Exception:
        return user


def _serialize_terms(row: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    month_days = [str(d) for d in parsed.get("month_days") or []]
    if parsed.get("month_last"):
        month_days.append(ss.MONTH_LAST)
    anchor = row.get("anchor_date")
    return {
        "name": row.get("name"),
        "customer": row.get("customer"),
        "customer_name": row.get("customer_name") or row.get("customer"),
        "enabled": bool(parsed.get("enabled")),
        "cycle": parsed.get("cycle"),
        "weekdays": [ss.WEEKDAY_CODES[d] for d in parsed.get("weekdays") or []],
        "week_interval": int(parsed.get("week_interval") or 1),
        "month_days": month_days,
        "interval_days": parsed.get("interval_days"),
        "anchor_date": str(anchor) if anchor else None,
        "remind_days_before": int(parsed.get("remind_days_before") or 0),
        "overdue_repeat_days": int(parsed.get("overdue_repeat_days") or 1),
        "responsible_user": parsed.get("responsible_user"),
        "responsible_user_name": _user_full_name(parsed.get("responsible_user")),
        "notes": parsed.get("notes"),
        "last_reminder_on": str(row.get("last_reminder_on")) if row.get("last_reminder_on") else None,
        "last_reminder_kind": row.get("last_reminder_kind") or None,
        "modified": str(row.get("modified")) if row.get("modified") else None,
        "exists": True,
    }


def _terms_payload(customer: str) -> Dict[str, Any]:
    row = _load_terms_row(customer)
    parsed = ss.parse_terms(row) if row else None
    # All branches, deliberately -- the same scope get_customer_credit_profile
    # uses. What a shop owes is one debt, not one per branch.
    open_rows = _open_credit_invoices(customers=[customer])
    status = ss.compute_status(parsed, _invoice_inputs(open_rows), _today())
    customer_name = (row or {}).get("customer_name") or (
        frappe.db.get_value("Customer", customer, "customer_name") or customer
    )
    return {
        "success": True,
        "customer": customer,
        "customer_name": customer_name,
        "terms": _serialize_terms(row, parsed) if row else None,
        "description": ss.describe(parsed) if parsed else None,
        "description_ar": ss.describe(parsed, "ar") if parsed else None,
        "status": status,
        "currency": _credit_currency(),
        "can_edit": _can_edit(),
    }


def _require_customer(customer: Any) -> str:
    name = str(customer or "").strip()
    if not name:
        frappe.throw(_("customer is required"))
    if not frappe.db.exists("Customer", name):
        frappe.throw(_("Customer {0} was not found").format(name))
    frappe.has_permission("Customer", "read", doc=name, throw=True)
    return name


def invalidate_collections_count_cache() -> None:
    """Drop every cached approvals-badge count (after a terms change). Never raises."""
    try:
        frappe.cache().delete_keys(_COUNT_CACHE_PREFIX)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=False)
def get_settlement_terms(customer: str) -> Dict[str, Any]:
    """One customer's settlement terms and where they stand today.

    Returns ``{success, customer, customer_name, terms, description,
    description_ar, status, currency, can_edit}``. ``terms`` is ``null`` when
    the customer has no record (``status.state`` is then ``unscheduled`` with a
    balance, ``none`` without).
    """
    _ensure_credit_ledger_access()
    name = _require_customer(customer)
    return _terms_payload(name)


@frappe.whitelist(allow_guest=False)
def save_settlement_terms(
    customer: str,
    cycle: str,
    weekdays: Union[str, List[str], None] = None,
    week_interval: Union[int, str, None] = 1,
    month_days: Union[str, List[Any], None] = None,
    interval_days: Union[int, str, None] = None,
    anchor_date: Optional[str] = None,
    remind_days_before: Union[int, str, None] = 1,
    overdue_repeat_days: Union[int, str, None] = 2,
    responsible_user: Optional[str] = None,
    notes: Optional[str] = None,
    enabled: Union[int, str, bool, None] = 1,
) -> Dict[str, Any]:
    """Create or replace a customer's settlement terms. Same shape as the getter.

    ``weekdays`` / ``month_days`` accept a comma string (``"Thu"``,
    ``"15,last"``) or a JSON list. Fields that do not belong to the chosen cycle
    are cleared. ``anchor_date`` omitted keeps the stored one, else today.
    """
    _ensure_credit_payment_access()

    name = str(customer or "").strip()
    if not name:
        frappe.throw(_("customer is required"))

    # Validate the terms BEFORE touching the database: a bad weekday is the
    # caller's mistake whatever state the customer is in.
    try:
        values = ss.normalize_terms_input(
            {
                "cycle": cycle,
                "enabled": enabled,
                "weekdays": weekdays,
                "week_interval": week_interval,
                "month_days": month_days,
                "interval_days": interval_days,
                "anchor_date": anchor_date,
                "remind_days_before": remind_days_before,
                "overdue_repeat_days": overdue_repeat_days,
                "responsible_user": responsible_user,
                "notes": notes,
            }
        )
    except ss.SettlementTermsError as exc:
        frappe.throw(_(str(exc)), title=_("Invalid settlement terms"))
        raise  # unreachable with the real frappe.throw; keeps a mocked throw honest

    name = _require_customer(name)

    if not doctype_ready():
        frappe.throw(
            _("Settlement terms are not installed on this site yet. Ask an administrator to run the update (bench migrate).")
        )

    existing = frappe.db.get_value(TERMS_DOCTYPE, {"customer": name}, "name")
    if existing:
        doc = frappe.get_doc(TERMS_DOCTYPE, existing)
        if values["anchor_date"] is None and ss.needs_anchor(values) and doc.get("anchor_date"):
            # Re-saving a fortnightly schedule must not silently shift its grid.
            values["anchor_date"] = getdate(doc.get("anchor_date"))
    else:
        doc = frappe.new_doc(TERMS_DOCTYPE)
        doc.customer = name

    for field, value in values.items():
        doc.set(field, value)

    if existing:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)

    invalidate_collections_count_cache()
    return _terms_payload(name)


@frappe.whitelist(allow_guest=False)
def delete_settlement_terms(customer: str) -> Dict[str, Any]:
    """Remove a customer's terms. Returns the getter's shape (``terms: null``)."""
    _ensure_credit_payment_access()
    name = _require_customer(customer)
    if doctype_ready():
        existing = frappe.db.get_value(TERMS_DOCTYPE, {"customer": name}, "name")
        if existing:
            frappe.delete_doc(TERMS_DOCTYPE, existing, ignore_permissions=True)
            # The daily pass only walks existing terms, so close our tagged
            # ToDos now or they stay open forever.
            from jarz_pos.services.settlement_reminders import sync_settlement_todo

            try:
                sync_settlement_todo(name, None, [], "")
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"settlement_todo_close_failed:{name}"[:140])
    invalidate_collections_count_cache()
    return _terms_payload(name)


def _collection_entries(profiles: List[str]) -> List[Dict[str, Any]]:
    """Per-customer inputs for ``ss.build_collection_rows``, within *profiles*.

    Driven by what is OWED: a customer with terms and nothing open is in state
    ``none`` and would be dropped anyway, so only customers with an open credit
    invoice in scope are loaded.
    """
    open_rows = _open_credit_invoices(profiles=profiles)
    by_customer: Dict[str, Dict[str, Any]] = {}
    for row in open_rows or []:
        customer = str(row.get("customer") or "")
        if not customer:
            continue
        bucket = by_customer.setdefault(
            customer,
            {
                "customer": customer,
                "customer_name": row.get("customer_name") or customer,
                "invoices": [],
            },
        )
        bucket["invoices"].append(
            {
                "name": row.get("name"),
                "posting_date": row.get("posting_date"),
                "outstanding_amount": row.get("outstanding_amount"),
            }
        )
    terms_by_customer = _load_terms_rows(list(by_customer))
    entries: List[Dict[str, Any]] = []
    for customer, bucket in by_customer.items():
        row = terms_by_customer.get(customer)
        bucket["terms"] = ss.parse_terms(row) if row else None
        entries.append(bucket)
    return entries


def _days_ahead(value: Any) -> int:
    try:
        days = int(float(value)) if value not in (None, "") else _DAYS_AHEAD_DEFAULT
    except (TypeError, ValueError):
        days = _DAYS_AHEAD_DEFAULT
    return max(0, min(days, _DAYS_AHEAD_MAX))


@frappe.whitelist(allow_guest=False)
def get_collections_due(
    days_ahead: Union[int, str, None] = _DAYS_AHEAD_DEFAULT,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Every customer with an open credit balance, by urgency.

    Sorted overdue -> due_today -> due_soon -> unscheduled -> ok; customers with
    nothing open are left out. ``days_ahead`` widens "due soon" for this list
    (a row is ``due_soon`` when its next due date is within
    ``max(remind_days_before, days_ahead)`` days).

    Branch-scoped exactly like ``credit.get_credit_ledger``: the caller sees
    orders belonging to the POS Profiles they are assigned to; ``branch`` narrows
    to one of them. A shop that orders from two branches therefore shows only
    the part a branch manager is responsible for.
    """
    _ensure_credit_ledger_access()

    days = _days_ahead(days_ahead)
    selected = str(branch or "").strip()
    currency = _credit_currency()
    filters_echo = {"branch": selected or None, "days_ahead": days}

    def _empty(code: str, notice: str) -> Dict[str, Any]:
        return {
            "success": True,
            "currency": currency,
            "filters": filters_echo,
            "rows": [],
            "counts": {"overdue": 0, "due_today": 0, "due_soon": 0, "unscheduled": 0},
            "notice_code": code,
            "notice": notice,
        }

    allowed = _allowed_profiles()
    if not allowed:
        return _empty(
            "no_branch_assigned",
            _("You are not assigned to any branch (POS Profile). Ask an administrator to add you to the branches you manage."),
        )
    profiles = list(allowed)
    if selected and selected.lower() != "all":
        if selected not in allowed:
            return _empty("branch_not_permitted", _("You are not assigned to branch {0}.").format(selected))
        profiles = [selected]

    rows, counts = ss.build_collection_rows(_collection_entries(profiles), _today(), soon_days=days)
    return {
        "success": True,
        "currency": currency,
        "filters": filters_echo,
        "rows": rows,
        "counts": counts,
    }


def count_collections_needing_attention() -> int:
    """Customers in state overdue / due_today within the caller's branches.

    For the approvals badge (``api/approvals``). Cached per user for
    ``_COUNT_CACHE_SECONDS`` because the badge is polled every minute and this
    walks every open credit invoice. Raises on a real failure so the approvals
    endpoint logs and skips the queue, like every other queue there.
    """
    user = str(getattr(frappe.session, "user", "") or "")
    cache_key = f"{_COUNT_CACHE_PREFIX}{user}"
    try:
        cached = frappe.cache().get_value(cache_key)
        if cached is not None:
            return int(cached)
    except Exception:
        pass

    profiles = _allowed_profiles()
    if not profiles:
        count = 0
    else:
        rows, _counts = ss.build_collection_rows(_collection_entries(profiles), _today())
        count = sum(1 for row in rows if ss.needs_attention(row.get("state")))

    try:
        frappe.cache().set_value(cache_key, count, expires_in_sec=_COUNT_CACHE_SECONDS)
    except Exception:
        pass
    return count

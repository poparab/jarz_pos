"""Recurring Expenses API — Jarz POS ERPNext Desk page.

Answers one question: *what does this company pay every month, and did it
actually get posted?*

Three sources are unioned, deliberately without duplicating any of them:

1. **Payroll** — read live from HRMS ``Salary Structure Assignment``. Salaries
   are never copied into a Jarz table, so there is nothing to migrate when
   payroll starts being run properly.
2. **Registry** — ``Jarz Recurring Expense`` rows for non-payroll recurring
   costs (rent, utilities, telecom, SaaS, retainers, licences).
3. **Reconciliation** — actual ``GL Entry`` postings for the selected month,
   compared against what the first two say *should* have been posted.

A fourth section, ``detected``, mines GL history for expenses that recur on a
monthly cadence but are **not** registered, so they can be onboarded.

Read-only.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import flt, getdate

from jarz_pos.constants import ROLES

# Months covered by one occurrence, mirroring the DocType's own mapping.
FREQUENCY_MONTHS = {
    "Monthly": 1,
    "Quarterly": 3,
    "Semi-Annual": 6,
    "Annual": 12,
}

# Accounts that are cost-of-sales / mechanical rather than recurring overhead.
# They dominate the GL by volume and would drown out the signal in `detected`.
EXCLUDED_ACCOUNT_TYPES = {
    "Cost of Goods Sold",
    "Stock Adjustment",
    "Expenses Included In Valuation",
    "Expenses Included In Asset Valuation",
    "Round Off",
    "Chargeable",
}

# A GL account needs activity in at least this many distinct months to be
# treated as a recurring-expense candidate.
DETECTION_MIN_MONTHS = 3
DETECTION_LOOKBACK_MONTHS = 12


# ── access control ────────────────────────────────────────────────────────


def _ensure_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    allowed = {
        ROLES.JARZ_MANAGER,
        ROLES.ADMINISTRATOR,
        ROLES.SYSTEM_MANAGER,
        "Accounts Manager",
    }
    if not (roles & allowed):
        frappe.throw(
            _("Only managers can view recurring expenses."), frappe.PermissionError
        )


# ── date helpers ──────────────────────────────────────────────────────────


def _month_bounds(month: Optional[str]) -> tuple[date, date, str]:
    """Resolve a ``YYYY-MM`` string (or today) to first/last day of that month."""
    if month:
        try:
            year_s, mon_s = str(month).split("-")[:2]
            year, mon = int(year_s), int(mon_s)
            anchor = date(year, mon, 1)
        except (ValueError, TypeError):
            frappe.throw(_("Invalid month {0}. Expected format YYYY-MM.").format(month))
    else:
        today = getdate()
        anchor = date(today.year, today.month, 1)

    last_day = calendar.monthrange(anchor.year, anchor.month)[1]
    return anchor, date(anchor.year, anchor.month, last_day), anchor.strftime("%Y-%m")


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def _is_due_in_month(row: Dict[str, Any], month_start: date, month_end: date) -> bool:
    """Does this registry item have an occurrence inside the selected month?"""
    if (row.get("status") or "") != "Active":
        return False

    start = getdate(row.get("start_date")) if row.get("start_date") else None
    if not start or start > month_end:
        return False

    end = getdate(row.get("end_date")) if row.get("end_date") else None
    if end and end < month_start:
        return False

    step = FREQUENCY_MONTHS.get(row.get("frequency") or "Monthly", 1)
    if step == 1:
        return True

    # Non-monthly cadences only land on months that are a whole number of
    # periods after the start month.
    return _months_between(start, month_start) % step == 0


# ── payroll (HRMS, live) ──────────────────────────────────────────────────


def _load_payroll(company: Optional[str], month_end: date) -> Dict[str, Any]:
    """Current payroll run-rate straight from HRMS. Never cached into Jarz."""
    emp_filters: Dict[str, Any] = {"status": "Active"}
    if company:
        emp_filters["company"] = company

    employees = frappe.get_all(
        "Employee",
        filters=emp_filters,
        fields=["name", "employee_name", "designation", "department", "date_of_joining"],
        order_by="employee_name",
    )
    emp_index = {e["name"]: e for e in employees}

    # Latest submitted assignment per employee, effective on or before month end.
    assignments = frappe.get_all(
        "Salary Structure Assignment",
        filters={
            "docstatus": 1,
            "employee": ["in", list(emp_index.keys())] if emp_index else ["in", [""]],
            "from_date": ["<=", month_end],
        },
        fields=[
            "name",
            "employee",
            "employee_name",
            "salary_structure",
            "from_date",
            "base",
            "variable",
            "currency",
        ],
        order_by="employee asc, from_date desc",
    )

    latest: Dict[str, Dict[str, Any]] = {}
    for row in assignments:
        latest.setdefault(row["employee"], row)

    rows: List[Dict[str, Any]] = []
    monthly_total = 0.0
    for emp_id, assignment in latest.items():
        emp = emp_index.get(emp_id, {})
        monthly = flt(assignment.get("base")) + flt(assignment.get("variable"))
        monthly_total += monthly
        rows.append(
            {
                "employee": emp_id,
                "employee_name": assignment.get("employee_name")
                or emp.get("employee_name"),
                "designation": emp.get("designation"),
                "department": emp.get("department"),
                "salary_structure": assignment.get("salary_structure"),
                "from_date": assignment.get("from_date"),
                "base": flt(assignment.get("base")),
                "variable": flt(assignment.get("variable")),
                "monthly": monthly,
            }
        )

    rows.sort(key=lambda r: r["monthly"], reverse=True)

    missing = [
        {
            "employee": e["name"],
            "employee_name": e.get("employee_name"),
            "designation": e.get("designation"),
            "department": e.get("department"),
            "date_of_joining": e.get("date_of_joining"),
        }
        for e in employees
        if e["name"] not in latest
    ]

    return {
        "configured": bool(latest),
        "employees_total": len(employees),
        "employees_with_structure": len(latest),
        "employees_without_structure": len(missing),
        "monthly_total": monthly_total,
        "rows": rows,
        "missing": missing,
    }


def _payroll_expense_accounts(company: Optional[str]) -> List[str]:
    """Expense accounts payroll posts to, for month-level reconciliation."""
    accounts: set[str] = set()

    # Preferred source: accounts configured on Salary Components.
    try:
        comp_filters: Dict[str, Any] = {}
        if company:
            comp_filters["company"] = company
        for row in frappe.get_all(
            "Salary Component Account",
            filters=comp_filters,
            fields=["account"],
        ):
            if row.get("account"):
                accounts.add(row["account"])
    except Exception:
        # Salary Component Account is an HRMS table; tolerate its absence.
        frappe.log_error(
            frappe.get_traceback(), "recurring_expenses: salary component accounts"
        )

    if accounts:
        return sorted(accounts)

    # Fallback: expense ledgers that look like payroll accounts.
    acc_filters: Dict[str, Any] = {"root_type": "Expense", "is_group": 0}
    if company:
        acc_filters["company"] = company
    for row in frappe.get_all("Account", filters=acc_filters, fields=["name", "account_name"]):
        label = (row.get("account_name") or "").lower()
        if any(token in label for token in ("salary", "salaries", "wage", "payroll")):
            accounts.add(row["name"])

    return sorted(accounts)


# ── GL reconciliation ─────────────────────────────────────────────────────


def _gl_posted_by_account(
    accounts: List[str], month_start: date, month_end: date, company: Optional[str]
) -> Dict[str, Dict[str, Any]]:
    """Net debit posted to each account within the month."""
    if not accounts:
        return {}

    conditions = {
        "account": ["in", accounts],
        "posting_date": ["between", [month_start, month_end]],
        "is_cancelled": 0,
    }
    if company:
        conditions["company"] = company

    entries = frappe.get_all(
        "GL Entry",
        filters=conditions,
        fields=["account", "debit", "credit", "voucher_type", "voucher_no"],
    )

    posted: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        bucket = posted.setdefault(
            entry["account"], {"amount": 0.0, "count": 0, "vouchers": set()}
        )
        bucket["amount"] += flt(entry.get("debit")) - flt(entry.get("credit"))
        bucket["count"] += 1
        if entry.get("voucher_no"):
            bucket["vouchers"].add(f"{entry.get('voucher_type')}|{entry['voucher_no']}")

    for bucket in posted.values():
        bucket["vouchers"] = sorted(bucket.pop("vouchers"))

    return posted


def _reconcile_status(expected: float, actual: float) -> str:
    """Classify an account's month against what was expected."""
    if expected <= 0:
        return "Unexpected" if abs(actual) > 0.005 else "None"
    if abs(actual) < 0.005:
        return "Missing"
    ratio = actual / expected
    if ratio < 0.95:
        return "Partial"
    if ratio > 1.05:
        return "Over"
    return "Posted"


# ── detection of unregistered recurring spend ─────────────────────────────


def _detect_unregistered(
    company: Optional[str], month_end: date, registered_accounts: set[str]
) -> List[Dict[str, Any]]:
    """Expense accounts posting on a monthly cadence but absent from the registry."""
    lookback_year = month_end.year
    lookback_month = month_end.month - DETECTION_LOOKBACK_MONTHS
    while lookback_month <= 0:
        lookback_month += 12
        lookback_year -= 1
    window_start = date(lookback_year, lookback_month, 1)

    acc_filters: Dict[str, Any] = {"root_type": "Expense", "is_group": 0}
    if company:
        acc_filters["company"] = company
    accounts = frappe.get_all(
        "Account", filters=acc_filters, fields=["name", "account_name", "account_type"]
    )
    candidate_accounts = [
        a["name"]
        for a in accounts
        if (a.get("account_type") or "") not in EXCLUDED_ACCOUNT_TYPES
        and a["name"] not in registered_accounts
    ]
    if not candidate_accounts:
        return []

    gl_filters: Dict[str, Any] = {
        "account": ["in", candidate_accounts],
        "posting_date": ["between", [window_start, month_end]],
        "is_cancelled": 0,
    }
    if company:
        gl_filters["company"] = company

    entries = frappe.get_all(
        "GL Entry",
        filters=gl_filters,
        fields=["account", "posting_date", "debit", "credit"],
    )

    by_account: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        bucket = by_account.setdefault(
            entry["account"], {"months": {}, "total": 0.0, "count": 0}
        )
        key = getdate(entry["posting_date"]).strftime("%Y-%m")
        net = flt(entry.get("debit")) - flt(entry.get("credit"))
        bucket["months"][key] = bucket["months"].get(key, 0.0) + net
        bucket["total"] += net
        bucket["count"] += 1

    detected: List[Dict[str, Any]] = []
    for account, data in by_account.items():
        active_months = [m for m, amt in data["months"].items() if abs(amt) > 0.005]
        if len(active_months) < DETECTION_MIN_MONTHS or data["total"] <= 0:
            continue
        detected.append(
            {
                "account": account,
                "months_active": len(active_months),
                "total": data["total"],
                "average_monthly": data["total"] / max(len(active_months), 1),
                "entries": data["count"],
                "last_month": max(active_months),
            }
        )

    detected.sort(key=lambda r: r["total"], reverse=True)
    return detected


# ── main endpoint ─────────────────────────────────────────────────────────


@frappe.whitelist()
def get_recurring_expenses_overview(
    month: Optional[str] = None,
    company: Optional[str] = None,
) -> Dict[str, Any]:
    """Monthly recurring-expense roll-up with GL reconciliation."""
    _ensure_manager()

    month_start, month_end, month_key = _month_bounds(month)

    if not company:
        company = frappe.defaults.get_user_default("Company") or frappe.db.get_value(
            "Company", {}, "name"
        )
    currency = (
        frappe.db.get_value("Company", company, "default_currency") if company else None
    ) or frappe.defaults.get_global_default("currency")

    # ── payroll ───────────────────────────────────────────────────────────
    payroll = _load_payroll(company, month_end)

    payroll_accounts = _payroll_expense_accounts(company)
    payroll_posted_map = _gl_posted_by_account(
        payroll_accounts, month_start, month_end, company
    )
    payroll_posted = sum(b["amount"] for b in payroll_posted_map.values())
    payroll["posted_this_month"] = payroll_posted
    payroll["accounts"] = payroll_accounts
    payroll["status"] = _reconcile_status(payroll["monthly_total"], payroll_posted)

    # ── registry ──────────────────────────────────────────────────────────
    reg_filters: Dict[str, Any] = {}
    if company:
        reg_filters["company"] = company
    registry_rows = frappe.get_all(
        "Jarz Recurring Expense",
        filters=reg_filters,
        fields=[
            "name",
            "expense_name",
            "category",
            "status",
            "supplier",
            "amount",
            "currency",
            "frequency",
            "monthly_equivalent",
            "day_of_month",
            "expense_account",
            "cost_center",
            "start_date",
            "end_date",
            "auto_repeat",
            "notes",
        ],
        order_by="category asc, monthly_equivalent desc",
    )

    registry_accounts = sorted(
        {r["expense_account"] for r in registry_rows if r.get("expense_account")}
    )
    registry_posted_map = _gl_posted_by_account(
        registry_accounts, month_start, month_end, company
    )

    # An account shared by several due items can't be attributed per item —
    # surface that instead of implying a false per-item match.
    due_per_account: Dict[str, int] = {}
    expected_per_account: Dict[str, float] = {}
    for row in registry_rows:
        if _is_due_in_month(row, month_start, month_end):
            acc = row.get("expense_account")
            due_per_account[acc] = due_per_account.get(acc, 0) + 1
            expected_per_account[acc] = expected_per_account.get(acc, 0.0) + flt(
                row.get("amount")
            )

    registry: List[Dict[str, Any]] = []
    registry_monthly_total = 0.0
    expected_this_month = 0.0
    items_missing = 0
    items_posted = 0

    for row in registry_rows:
        due = _is_due_in_month(row, month_start, month_end)
        account = row.get("expense_account")
        posted_bucket = registry_posted_map.get(account, {})
        posted_amount = flt(posted_bucket.get("amount"))
        account_expected = expected_per_account.get(account, 0.0)
        status = _reconcile_status(account_expected, posted_amount) if due else "Not Due"

        if (row.get("status") or "") == "Active":
            registry_monthly_total += flt(row.get("monthly_equivalent"))
        if due:
            expected_this_month += flt(row.get("amount"))
            if status in ("Missing", "Partial"):
                items_missing += 1
            elif status in ("Posted", "Over"):
                items_posted += 1

        registry.append(
            {
                **row,
                "amount": flt(row.get("amount")),
                "monthly_equivalent": flt(row.get("monthly_equivalent")),
                "due_this_month": due,
                "posted_amount": posted_amount,
                "posted_entries": posted_bucket.get("count", 0),
                "vouchers": posted_bucket.get("vouchers", []),
                "account_expected": account_expected,
                "shared_account": due_per_account.get(account, 0) > 1,
                "gl_status": status,
            }
        )

    # ── category roll-up (payroll included as its own bucket) ─────────────
    by_category: Dict[str, Dict[str, Any]] = {}
    for row in registry:
        if (row.get("status") or "") != "Active":
            continue
        bucket = by_category.setdefault(
            row["category"], {"category": row["category"], "monthly": 0.0, "count": 0}
        )
        bucket["monthly"] += flt(row.get("monthly_equivalent"))
        bucket["count"] += 1

    if payroll["monthly_total"] > 0 or payroll["employees_total"] > 0:
        by_category["Salaries (HRMS)"] = {
            "category": "Salaries (HRMS)",
            "monthly": payroll["monthly_total"],
            "count": payroll["employees_with_structure"],
            "source": "HRMS",
        }

    categories = sorted(by_category.values(), key=lambda c: c["monthly"], reverse=True)

    total_runrate = registry_monthly_total + payroll["monthly_total"]
    total_posted = payroll_posted + sum(
        b["amount"] for b in registry_posted_map.values()
    )
    total_expected = expected_this_month + payroll["monthly_total"]

    # ── gaps worth shouting about ─────────────────────────────────────────
    gaps: List[Dict[str, str]] = []
    if payroll["employees_without_structure"]:
        gaps.append(
            {
                "severity": "critical" if not payroll["configured"] else "warning",
                "message": _(
                    "{0} of {1} active employees have no Salary Structure Assignment, so their salary is not counted in the run-rate."
                ).format(
                    payroll["employees_without_structure"], payroll["employees_total"]
                ),
            }
        )
    if not registry_rows:
        gaps.append(
            {
                "severity": "critical",
                "message": _(
                    "No recurring expenses registered yet. Rent, utilities, telecom and services are invisible until they are added."
                ),
            }
        )
    if items_missing:
        gaps.append(
            {
                "severity": "warning",
                "message": _(
                    "{0} registered expense(s) due this month have nothing posted to their account."
                ).format(items_missing),
            }
        )

    detected = _detect_unregistered(company, month_end, set(registry_accounts))

    return {
        "month": month_key,
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "company": company,
        "currency": currency,
        "summary": {
            "total_monthly_runrate": total_runrate,
            "payroll_monthly": payroll["monthly_total"],
            "registry_monthly": registry_monthly_total,
            "active_count": sum(
                1 for r in registry if (r.get("status") or "") == "Active"
            ),
            "expected_this_month": total_expected,
            "posted_this_month": total_posted,
            "variance": total_posted - total_expected,
            "items_posted": items_posted,
            "items_missing": items_missing,
        },
        "by_category": categories,
        "payroll": payroll,
        "registry": registry,
        "detected": detected,
        "gaps": gaps,
    }

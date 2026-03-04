from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, nowdate, now_datetime


from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
    make_closing_entry_from_opening,
)
from erpnext.accounts.utils import get_balance_on


def _assert_user_has_profile_access(user: str, pos_profile: str):
    has_access = frappe.db.exists(
        "POS Profile User",
        {
            "parent": pos_profile,
            "user": user,
        },
    )
    if not has_access:
        frappe.throw(_("You are not allowed to use POS Profile {0}").format(pos_profile), frappe.PermissionError)


def _get_mode_of_payment_account(mode_of_payment: str, company: str) -> str | None:
    if not mode_of_payment or not company:
        return None

    account = frappe.db.get_value(
        "Mode of Payment Account",
        {
            "parent": mode_of_payment,
            "company": company,
        },
        "default_account",
    )
    if account:
        return account

    account = frappe.db.get_value(
        "Mode of Payment Account",
        {
            "parent": mode_of_payment,
        },
        "default_account",
    )
    return account


def _get_account_balance(account: str | None, company: str) -> float:
    if not account:
        return 0.0
    try:
        return flt(get_balance_on(account=account, company=company, date=nowdate()) or 0)
    except Exception:
        return 0.0


def _ensure_mode_of_payment_account(mode_of_payment: str, company: str, default_account: str | None):
    if not mode_of_payment or not company or not default_account:
        return

    existing = frappe.db.get_value(
        "Mode of Payment Account",
        {
            "parent": mode_of_payment,
            "company": company,
        },
        ["name", "default_account"],
        as_dict=True,
    )

    if existing:
        if existing.default_account != default_account:
            frappe.db.set_value(
                "Mode of Payment Account",
                existing.name,
                "default_account",
                default_account,
            )
        return

    row = frappe.get_doc(
        {
            "doctype": "Mode of Payment Account",
            "parent": mode_of_payment,
            "parenttype": "Mode of Payment",
            "parentfield": "accounts",
            "company": company,
            "default_account": default_account,
        }
    )
    row.insert(ignore_permissions=True)


def _get_profile_primary_mode_of_payment(profile) -> str | None:
    payments = profile.get("payments") or []
    if not payments:
        return None

    for row in payments:
        if row.get("default") and row.get("mode_of_payment"):
            return row.get("mode_of_payment")

    return (payments[0] or {}).get("mode_of_payment")


def _resolve_pos_profile_account(company: str, pos_profile: str, branch: str | None, mode_of_payment: str | None) -> str | None:
    # Required by business flow: use the account named after the POS Profile.
    if frappe.db.exists("Account", {"company": company, "is_group": 0, "account_name": pos_profile}):
        return frappe.db.get_value("Account", {"company": company, "is_group": 0, "account_name": pos_profile}, "name")

    if frappe.db.exists("Account", {"company": company, "is_group": 0, "name": pos_profile}):
        return pos_profile

    return None


def _get_employee_for_user(user: str) -> dict[str, Any] | None:
    employee = frappe.db.get_value(
        "Employee",
        {"user_id": user},
        ["name", "employee_name", "branch"],
        as_dict=True,
    )
    if not employee:
        return None

    # Read shift requirement from User doctype, not Employee
    employee["require_pos_shift"] = bool(
        int(frappe.db.get_value("User", user, "custom_require_pos_shift") or 0)
    )
    return employee


def _get_latest_opening_entry(user: str, pos_profile: str | None = None) -> dict[str, Any] | None:
    filters: dict[str, Any] = {
        "user": user,
        "status": "Open",
        "docstatus": 1,
    }
    if pos_profile:
        filters["pos_profile"] = pos_profile

    entries = frappe.get_all(
        "POS Opening Entry",
        filters=filters,
        fields=["name"],
        order_by="period_start_date desc, modified desc",
        limit=1,
    )

    if not entries:
        return None

    opening = frappe.get_doc("POS Opening Entry", entries[0]["name"])
    return {
        "name": opening.name,
        "status": opening.status,
        "user": opening.user,
        "company": opening.company,
        "pos_profile": opening.pos_profile,
        "period_start_date": opening.period_start_date,
        "period_end_date": opening.period_end_date,
        "balance_details": [
            {
                "mode_of_payment": row.mode_of_payment,
                "opening_amount": flt(row.opening_amount),
            }
            for row in (opening.balance_details or [])
        ],
    }


@frappe.whitelist(allow_guest=False)
def get_active_shift(pos_profile: str | None = None):
    user = frappe.session.user
    return _get_latest_opening_entry(user=user, pos_profile=pos_profile)


@frappe.whitelist(allow_guest=False)
def get_shift_payment_methods(pos_profile: str):
    if not pos_profile:
        frappe.throw(_("POS Profile is required"))

    user = frappe.session.user
    _assert_user_has_profile_access(user, pos_profile)

    profile = frappe.get_doc("POS Profile", pos_profile)
    company = profile.company
    branch = getattr(profile, "branch", None)
    mode = _get_profile_primary_mode_of_payment(profile)
    if not mode:
        frappe.throw(_("POS Profile {0} has no payment methods configured").format(pos_profile))

    account = _resolve_pos_profile_account(company, pos_profile, branch, mode)
    if not account:
        frappe.throw(
            _("No account named as POS Profile {0} was found in company {1}.").format(pos_profile, company)
        )

    try:
        _ensure_mode_of_payment_account(mode, company, account)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "jarz_pos.shift.ensure_mode_of_payment_account",
        )

    current_balance = _get_account_balance(account, company)
    return [
        {
            "mode_of_payment": mode,
            "default_amount": current_balance,
            "account": account,
            "company": company,
            "branch": branch,
            "current_balance": current_balance,
            "suggested_opening_amount": current_balance,
        }
    ]


@frappe.whitelist(allow_guest=False)
def start_shift(pos_profile: str, opening_balances: list[dict[str, Any]] | None = None):
    user = frappe.session.user

    if not pos_profile:
        frappe.throw(_("POS Profile is required"))

    _assert_user_has_profile_access(user, pos_profile)

    existing_open = _get_latest_opening_entry(user=user)
    if existing_open:
        frappe.throw(
            _("You already have an open shift: {0}").format(existing_open["name"]),
            title=_("Shift Already Open"),
        )

    company = frappe.db.get_value("POS Profile", pos_profile, "company")
    if not company:
        frappe.throw(_("POS Profile {0} was not found").format(pos_profile))

    opening_doc = frappe.new_doc("POS Opening Entry")
    opening_doc.user = user
    opening_doc.company = company
    opening_doc.pos_profile = pos_profile
    opening_doc.period_start_date = now_datetime()
    opening_doc.posting_date = nowdate()

    rows = opening_balances or []
    if not rows:
        for method in get_shift_payment_methods(pos_profile):
            rows.append(
                {
                    "mode_of_payment": method["mode_of_payment"],
                    "opening_amount": flt(method.get("suggested_opening_amount") or 0),
                    "system_balance": flt(method.get("current_balance") or 0),
                    "account": method.get("account"),
                }
            )

    opening_differences: list[dict[str, Any]] = []
    captured_one = False
    for row in rows:
        if captured_one:
            break
        mode = (row or {}).get("mode_of_payment")
        if not mode:
            continue

        row_account = (row or {}).get("account")
        if row_account:
            try:
                _ensure_mode_of_payment_account(mode, company, row_account)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "jarz_pos.shift.ensure_mode_of_payment_account.start_shift",
                )

        confirmed_opening = flt(
            (row or {}).get("opening_amount")
            if (row or {}).get("opening_amount") is not None
            else (row or {}).get("confirmed_amount")
        )
        system_balance = flt((row or {}).get("system_balance") or 0)
        difference = flt((row or {}).get("difference") or (confirmed_opening - system_balance))

        opening_doc.append(
            "balance_details",
            {
                "mode_of_payment": mode,
                "opening_amount": confirmed_opening,
            },
        )

        opening_differences.append(
            {
                "mode_of_payment": mode,
                "account": (row or {}).get("account"),
                "system_balance": system_balance,
                "confirmed_opening_amount": confirmed_opening,
                "difference": difference,
            }
        )
        captured_one = True

    if not opening_doc.balance_details:
        frappe.throw(_("At least one opening balance row is required"))

    opening_doc.insert(ignore_permissions=True)
    opening_doc.submit()

    if opening_differences:
        lines = [
            _("Opening confirmation differences:")
        ]
        for diff in opening_differences:
            lines.append(
                _("{0} | Account: {1} | System: {2} | Confirmed: {3} | Difference: {4}").format(
                    diff["mode_of_payment"],
                    diff.get("account") or "-",
                    flt(diff.get("system_balance") or 0),
                    flt(diff.get("confirmed_opening_amount") or 0),
                    flt(diff.get("difference") or 0),
                )
            )
        opening_doc.add_comment("Comment", "\n".join(lines))

    employee = _get_employee_for_user(user)
    return {
        "opening_entry": opening_doc.name,
        "employee": employee,
        "opening_differences": opening_differences,
    }


@frappe.whitelist(allow_guest=False)
def get_shift_summary(pos_opening_entry: str):
    if not pos_opening_entry:
        frappe.throw(_("POS Opening Entry is required"))

    opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)
    if opening.user != frappe.session.user:
        frappe.throw(_("You are not allowed to access this shift"), frappe.PermissionError)

    closing_draft = make_closing_entry_from_opening(opening)

    return {
        "opening_entry": opening.name,
        "status": opening.status,
        "user": opening.user,
        "company": opening.company,
        "pos_profile": opening.pos_profile,
        "period_start_date": opening.period_start_date,
        "period_end_date": opening.period_end_date,
        "invoice_count": len(closing_draft.pos_transactions or []),
        "grand_total": flt(closing_draft.grand_total),
        "net_total": flt(closing_draft.net_total),
        "total_quantity": flt(closing_draft.total_quantity),
        "payment_reconciliation": [
            {
                "mode_of_payment": row.mode_of_payment,
                "opening_amount": flt(row.opening_amount),
                "expected_amount": flt(row.expected_amount),
                "closing_amount": flt(getattr(row, "closing_amount", 0) or 0),
                "difference": flt(getattr(row, "difference", 0) or 0),
            }
            for row in (closing_draft.payment_reconciliation or [])
        ],
    }


@frappe.whitelist(allow_guest=False)
def end_shift(pos_opening_entry: str, closing_balances: list[dict[str, Any]] | None = None):
    if not pos_opening_entry:
        frappe.throw(_("POS Opening Entry is required"))

    opening = frappe.get_doc("POS Opening Entry", pos_opening_entry)

    if opening.user != frappe.session.user:
        frappe.throw(_("You are not allowed to close this shift"), frappe.PermissionError)

    if opening.status != "Open" or opening.docstatus != 1:
        frappe.throw(_("Selected POS Opening Entry should be open."), title=_("Invalid Opening Entry"))

    closing = make_closing_entry_from_opening(opening)

    closing_map: dict[str, float] = {}
    for row in (closing_balances or []):
        mode = (row or {}).get("mode_of_payment")
        if not mode:
            continue
        closing_map[mode] = flt((row or {}).get("closing_amount") or 0)

    for row in (closing.payment_reconciliation or []):
        row.closing_amount = flt(closing_map.get(row.mode_of_payment, row.expected_amount or 0))

    closing.insert(ignore_permissions=True)
    closing.submit()

    return {
        "closing_entry": closing.name,
        "opening_entry": opening.name,
        "status": closing.status,
        "payment_reconciliation": [
            {
                "mode_of_payment": row.mode_of_payment,
                "opening_amount": flt(row.opening_amount),
                "expected_amount": flt(row.expected_amount),
                "closing_amount": flt(row.closing_amount),
                "difference": flt(row.difference),
            }
            for row in (closing.payment_reconciliation or [])
        ],
        "invoice_count": len(closing.pos_transactions or []),
        "grand_total": flt(closing.grand_total),
        "net_total": flt(closing.net_total),
        "total_quantity": flt(closing.total_quantity),
    }

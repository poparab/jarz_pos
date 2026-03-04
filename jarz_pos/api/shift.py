from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, nowdate


from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
    make_closing_entry_from_opening,
)
from erpnext.accounts.utils import get_balance_on


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

    profile = frappe.get_doc("POS Profile", pos_profile)
    company = profile.company
    branch = getattr(profile, "branch", None)
    methods = []
    for row in (profile.get("payments") or []):
        mode = row.get("mode_of_payment")
        if not mode:
            continue
        account = _get_mode_of_payment_account(mode, company)
        current_balance = _get_account_balance(account, company)
        methods.append(
            {
                "mode_of_payment": mode,
                "default_amount": flt(row.get("default_amount") or 0),
                "account": account,
                "company": company,
                "branch": branch,
                "current_balance": current_balance,
                "suggested_opening_amount": current_balance,
            }
        )

    return methods


@frappe.whitelist(allow_guest=False)
def start_shift(pos_profile: str, opening_balances: list[dict[str, Any]] | None = None):
    user = frappe.session.user

    if not pos_profile:
        frappe.throw(_("POS Profile is required"))

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
    for row in rows:
        mode = (row or {}).get("mode_of_payment")
        if not mode:
            continue

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

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt


from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
    make_closing_entry_from_opening,
)


def _get_employee_for_user(user: str) -> dict[str, Any] | None:
    employee = frappe.db.get_value(
        "Employee",
        {"user_id": user},
        ["name", "employee_name", "branch", "custom_require_pos_shift"],
        as_dict=True,
    )
    if not employee:
        return None

    employee["require_pos_shift"] = bool(int(employee.get("custom_require_pos_shift") or 0))
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
    methods = []
    for row in (profile.get("payments") or []):
        mode = row.get("mode_of_payment")
        if not mode:
            continue
        methods.append(
            {
                "mode_of_payment": mode,
                "default_amount": flt(row.get("default_amount") or 0),
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
                    "opening_amount": flt(method.get("default_amount") or 0),
                }
            )

    for row in rows:
        mode = (row or {}).get("mode_of_payment")
        if not mode:
            continue
        opening_doc.append(
            "balance_details",
            {
                "mode_of_payment": mode,
                "opening_amount": flt((row or {}).get("opening_amount") or 0),
            },
        )

    if not opening_doc.balance_details:
        frappe.throw(_("At least one opening balance row is required"))

    opening_doc.insert(ignore_permissions=True)
    opening_doc.submit()

    employee = _get_employee_for_user(user)
    return {
        "opening_entry": opening_doc.name,
        "employee": employee,
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

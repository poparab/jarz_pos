"""Jarz POS – Courier workflow API endpoints.

This module exposes courier-related operations using the refactored
delivery handling services.
"""

from __future__ import annotations

import frappe

from jarz_pos.services.delivery_handling import (
    mark_courier_outstanding as _mark_courier_outstanding,
    pay_delivery_expense as _pay_delivery_expense,
    courier_delivery_expense_only as _courier_delivery_expense_only,
    get_courier_balances as _get_courier_balances,
    settle_courier as _settle_courier,
    settle_courier_for_invoice as _settle_courier_for_invoice
)


# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------


@frappe.whitelist()  # type: ignore[attr-defined]
def mark_courier_outstanding(invoice_name: str, courier: str):
    return _mark_courier_outstanding(invoice_name, courier)


@frappe.whitelist()  # type: ignore[attr-defined]
def pay_delivery_expense(invoice_name: str, pos_profile: str):
    return _pay_delivery_expense(invoice_name, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def courier_delivery_expense_only(invoice_name: str, courier: str):
    return _courier_delivery_expense_only(invoice_name, courier)


@frappe.whitelist()  # type: ignore[attr-defined]
def get_courier_balances():
    return _get_courier_balances()


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier(courier: str, pos_profile: str | None = None):
    return _settle_courier(courier, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier_for_invoice(invoice_name: str, pos_profile: str | None = None):
    return _settle_courier_for_invoice(invoice_name, pos_profile) 
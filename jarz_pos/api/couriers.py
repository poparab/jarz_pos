"""Jarz POS – Courier workflow API endpoints.

This thin shim exposes courier-related operations under a dedicated
import path while delegating the heavy business logic to the proven
implementation located in ``jarz_pos.jarz_pos.page.custom_pos.custom_pos``.
"""

from __future__ import annotations

import frappe

from jarz_pos.jarz_pos.page.custom_pos import custom_pos as _legacy


# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------


@frappe.whitelist()  # type: ignore[attr-defined]
def mark_courier_outstanding(invoice_name: str, courier: str):
    return _legacy.mark_courier_outstanding(invoice_name, courier)


@frappe.whitelist()  # type: ignore[attr-defined]
def pay_delivery_expense(invoice_name: str, pos_profile: str):
    return _legacy.pay_delivery_expense(invoice_name, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def courier_delivery_expense_only(invoice_name: str, courier: str):
    return _legacy.courier_delivery_expense_only(invoice_name, courier)


@frappe.whitelist()  # type: ignore[attr-defined]
def get_courier_balances():
    return _legacy.get_courier_balances()


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier(courier: str, pos_profile: str | None = None):
    return _legacy.settle_courier(courier, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier_for_invoice(invoice_name: str, pos_profile: str | None = None):
    return _legacy.settle_courier_for_invoice(invoice_name, pos_profile) 
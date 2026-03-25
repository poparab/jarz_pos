"""Jarz POS – Delivery Partner API endpoints.

Provides endpoints for:
  - Listing delivery partner balances (unsettled shipping fees)
  - Settling accumulated fees via bank Payment Entry
"""
from __future__ import annotations

import frappe
from frappe.utils import now_datetime


@frappe.whitelist()
def get_delivery_partner_balances(delivery_partner: str | None = None):
    """Return unsettled shipping fee totals per Delivery Partner.

    If delivery_partner is provided, returns only that partner's balance.
    Returns list of: {delivery_partner, partner_name, order_count, total_shipping, oldest_date}
    """
    filters = {"is_partner_order": 1, "status": "Unsettled"}
    if delivery_partner:
        filters["delivery_partner"] = delivery_partner

    rows = frappe.db.sql("""
        SELECT
            ct.delivery_partner,
            dp.partner_name,
            COUNT(*) AS order_count,
            SUM(ct.shipping_amount) AS total_shipping,
            MIN(ct.date) AS oldest_date
        FROM `tabCourier Transaction` ct
        LEFT JOIN `tabDelivery Partner` dp ON dp.name = ct.delivery_partner
        WHERE ct.is_partner_order = 1
          AND ct.status = 'Unsettled'
          AND ct.delivery_partner IS NOT NULL
          {partner_filter}
        GROUP BY ct.delivery_partner
        ORDER BY total_shipping DESC
    """.format(
        partner_filter=("AND ct.delivery_partner = %(delivery_partner)s" if delivery_partner else "")
    ), {"delivery_partner": delivery_partner} if delivery_partner else {}, as_dict=True)

    return rows


@frappe.whitelist()
def get_delivery_partner_unsettled_details(delivery_partner: str):
    """Return individual unsettled Courier Transactions for a partner."""
    if not delivery_partner:
        frappe.throw("delivery_partner is required")

    return frappe.get_all(
        "Courier Transaction",
        filters={
            "delivery_partner": delivery_partner,
            "is_partner_order": 1,
            "status": "Unsettled",
        },
        fields=[
            "name", "reference_invoice", "party_type", "party",
            "amount", "shipping_amount", "date", "payment_mode",
        ],
        order_by="date asc",
    )


@frappe.whitelist()
def settle_delivery_partner(delivery_partner: str, bank_account: str | None = None):
    """Settle all unsettled shipping fees for a Delivery Partner via bank Payment Entry.

    Creates a Journal Entry:
      DR  Freight/Delivery Expense Account  (total shipping)
      CR  Bank Account                      (total shipping)

    Then marks all matched Courier Transactions as Settled.

    Returns: {success, delivery_partner, order_count, total_shipping, journal_entry}
    """
    if not delivery_partner:
        frappe.throw("delivery_partner is required")

    dp = frappe.get_doc("Delivery Partner", delivery_partner)

    # Get all unsettled partner CTs
    unsettled = frappe.get_all(
        "Courier Transaction",
        filters={
            "delivery_partner": delivery_partner,
            "is_partner_order": 1,
            "status": "Unsettled",
        },
        fields=["name", "shipping_amount", "reference_invoice"],
    )

    if not unsettled:
        return {
            "success": True,
            "delivery_partner": delivery_partner,
            "order_count": 0,
            "total_shipping": 0,
            "message": "No unsettled transactions found",
        }

    total_shipping = sum(float(ct.get("shipping_amount") or 0) for ct in unsettled)
    if total_shipping <= 0:
        frappe.throw("Total shipping amount is zero — nothing to settle")

    # Resolve bank account: explicit param > partner config > company default
    if not bank_account and dp.bank_account:
        bank_account = frappe.db.get_value("Bank Account", dp.bank_account, "account")
    if not bank_account:
        # Fallback: first submitted invoice's company default bank
        first_inv = unsettled[0].get("reference_invoice")
        if first_inv:
            company = frappe.db.get_value("Sales Invoice", first_inv, "company")
            if company:
                bank_account = frappe.db.get_value("Company", company, "default_bank_account")

    if not bank_account:
        frappe.throw("No bank account found. Set it on the Delivery Partner or pass bank_account.")

    # Resolve expense account
    settlement_account = dp.settlement_account
    if not settlement_account:
        # Fallback: freight charges account from Jarz POS Settings
        try:
            from jarz_pos.utils.account_utils import get_freight_expense_account
            first_inv = unsettled[0].get("reference_invoice")
            if first_inv:
                company = frappe.db.get_value("Sales Invoice", first_inv, "company")
                settlement_account = get_freight_expense_account(company)
        except Exception:
            pass

    if not settlement_account:
        frappe.throw("No settlement/expense account configured for this partner.")

    # Determine company from bank account
    company = frappe.db.get_value("Account", bank_account, "company")
    if not company:
        frappe.throw(f"Cannot determine company from bank account {bank_account}")

    # Create Journal Entry
    invoice_refs = ", ".join(ct.get("reference_invoice", "") for ct in unsettled if ct.get("reference_invoice"))
    je = frappe.get_doc({
        "doctype": "Journal Entry",
        "voucher_type": "Bank Entry",
        "posting_date": frappe.utils.today(),
        "company": company,
        "user_remark": f"Delivery Partner settlement: {delivery_partner} ({len(unsettled)} orders). Invoices: {invoice_refs[:500]}",
        "accounts": [
            {
                "account": settlement_account,
                "debit_in_account_currency": total_shipping,
                "credit_in_account_currency": 0,
            },
            {
                "account": bank_account,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": total_shipping,
            },
        ],
    })
    je.insert(ignore_permissions=True)
    je.submit()

    # Mark all CTs as settled
    for ct in unsettled:
        frappe.db.set_value(
            "Courier Transaction", ct["name"],
            {"status": "Settled", "journal_entry": je.name},
            update_modified=False,
        )

    frappe.db.commit()

    return {
        "success": True,
        "delivery_partner": delivery_partner,
        "partner_name": dp.partner_name,
        "order_count": len(unsettled),
        "total_shipping": total_shipping,
        "journal_entry": je.name,
    }

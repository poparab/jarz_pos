"""Jarz POS – Delivery Partner API endpoints.

Provides endpoints for:
  - Listing delivery partner balances (unsettled shipping fees)
  - Settling accumulated partner balances via one balanced Journal Entry
"""
from __future__ import annotations

import hashlib

import frappe
from frappe.utils import now_datetime

from jarz_pos.services.delivery_handling import create_partner_settlement_je


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
    """Settle every Unsettled partner Courier Transaction for a Delivery Partner.

    §5-D hybrid money model. Partner orders were booked against the partner's
    Payable ``settlement_account`` at dispatch (with a per-partner Supplier party).
    This sweep posts ONE balanced Journal Entry that nets the partner ledger back
    to zero:

      - CASH / COD CTs (amount = order total): the partner remits
        Σ(order - shipping) into ``bank_account`` ->
          DR Bank (Σ net) / CR settlement_account (Σ net) [Supplier].
      - ONLINE / prepaid CTs (amount = 0): we pay the partner Σ(shipping) ->
          DR settlement_account (Σ shipping) [Supplier] / CR Bank.

    Both portions coexist in one JE with the bank leg netted; EVERY
    settlement_account line carries the per-partner Supplier party (fixes the v16
    ValidationError crash). Idempotent via a batch ``user_remark`` tag.

    Returns: {success, delivery_partner, partner_name, order_count,
              cash_order_count, online_order_count, cash_net_total,
              online_ship_total, total_shipping, bank_account, journal_entry}
    """
    if not delivery_partner:
        frappe.throw("delivery_partner is required")

    dp = frappe.get_doc("Delivery Partner", delivery_partner)

    # Only Unsettled partner CTs for this partner.
    unsettled = frappe.get_all(
        "Courier Transaction",
        filters={
            "delivery_partner": delivery_partner,
            "is_partner_order": 1,
            "status": "Unsettled",
        },
        fields=["name", "amount", "shipping_amount", "reference_invoice"],
        order_by="date asc",
    )

    if not unsettled:
        return {
            "success": True,
            "delivery_partner": delivery_partner,
            "order_count": 0,
            "total_shipping": 0,
            "message": "No unsettled transactions found",
        }

    # Split COD (amount > 0 -> partner remits the net) from ONLINE (amount == 0 ->
    # we pay the partner the fee).
    cash_net_total = 0.0
    online_ship_total = 0.0
    cash_order_count = 0
    online_order_count = 0
    for ct in unsettled:
        amt = float(ct.get("amount") or 0)
        ship = float(ct.get("shipping_amount") or 0)
        if amt > 0.005:
            cash_net_total += (amt - ship)
            cash_order_count += 1
        else:
            online_ship_total += ship
            online_order_count += 1

    if not dp.settlement_account:
        frappe.throw(
            "Delivery Partner has no settlement_account (Payable) configured. "
            "Set it on the Delivery Partner master."
        )

    # Resolve bank ledger: explicit param > partner Bank Account > company default.
    if not bank_account and dp.bank_account:
        bank_account = frappe.db.get_value("Bank Account", dp.bank_account, "account")
    if not bank_account:
        first_inv = unsettled[0].get("reference_invoice")
        company0 = frappe.db.get_value("Sales Invoice", first_inv, "company") if first_inv else None
        if company0:
            bank_account = frappe.db.get_value("Company", company0, "default_bank_account")
    if not bank_account:
        frappe.throw("No bank account found. Set it on the Delivery Partner or pass bank_account.")

    company = frappe.db.get_value("Account", bank_account, "company")
    if not company:
        frappe.throw(f"Cannot determine company from bank account {bank_account}")

    total_shipping = round(sum(float(ct.get("shipping_amount") or 0) for ct in unsettled), 2)

    # Deterministic per-batch idempotency token (stable across retries of the same set).
    token = hashlib.sha1(
        "|".join(sorted(str(ct["name"]) for ct in unsettled)).encode("utf-8")
    ).hexdigest()[:12]

    invoice_refs = ", ".join(
        str(ct.get("reference_invoice") or "") for ct in unsettled if ct.get("reference_invoice")
    )[:400]

    je_name = create_partner_settlement_je(
        delivery_partner=delivery_partner,
        company=company,
        bank_account=bank_account,
        cash_net_total=cash_net_total,
        online_ship_total=online_ship_total,
        token=token,
        human=f"Delivery Partner settlement: {delivery_partner} ({len(unsettled)} orders). Invoices: {invoice_refs}",
    )

    # Mark all swept CTs Settled and link the JE.
    for ct in unsettled:
        frappe.db.set_value(
            "Courier Transaction", ct["name"],
            {"status": "Settled", "journal_entry": je_name},
            update_modified=False,
        )

    frappe.db.commit()

    return {
        "success": True,
        "delivery_partner": delivery_partner,
        "partner_name": dp.partner_name,
        "order_count": len(unsettled),
        "cash_order_count": cash_order_count,
        "online_order_count": online_order_count,
        "cash_net_total": round(cash_net_total, 2),
        "online_ship_total": round(online_ship_total, 2),
        "total_shipping": total_shipping,
        "bank_account": bank_account,
        "journal_entry": je_name,
    }

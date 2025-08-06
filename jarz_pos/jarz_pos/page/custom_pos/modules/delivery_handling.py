"""
Delivery Handling Module for Jarz POS

This module handles all delivery and courier-related operations,
including outstanding management, expense tracking, and settlement.
"""

import frappe
from ..utils.account_utils import get_account_for_company, _get_cash_account


@frappe.whitelist()
def mark_courier_outstanding(invoice_name: str, courier: str):
    """
    Allocate the outstanding amount of a submitted Sales Invoice to the
    company's *Courier Outstanding* account and create a *Courier Transaction*
    log entry.
    
    Args:
        invoice_name: Sales Invoice ID that courier will collect payment for.
        courier: Selected courier (link to *Courier* doctype).
    """
    # Validate input & fetch documents
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted before marking as courier outstanding.")
    if inv.outstanding_amount <= 0:
        frappe.throw("Invoice already paid – no outstanding amount to allocate.")
    
    company = inv.company
    outstanding = inv.outstanding_amount
    
    # Resolve ledger accounts
    paid_to_account = _get_courier_outstanding_account(company)
    paid_from_account = _get_receivable_account(company)
    
    # Build & submit **Payment Entry** – marks invoice paid while
    # parking the receivable in *Courier Outstanding*.
    pe = _create_payment_entry(inv, paid_from_account, paid_to_account, outstanding)
    
    # Record SHIPPING EXPENSE via separate Journal Entry
    shipping_exp = _get_delivery_expense_amount(inv)
    je_name = None
    if shipping_exp and shipping_exp > 0:
        je_name = _create_shipping_expense_journal_entry(inv, shipping_exp, paid_to_account)
    
    # Create Courier Transaction log
    ct = _create_courier_transaction(courier, inv, outstanding, shipping_exp)
    
    # Notify front-end & return (custom event)
    frappe.publish_realtime(
        "jarz_pos_courier_outstanding",
        {
            "invoice": inv.name,
            "payment_entry": pe.name,
            "journal_entry": je_name,
            "courier_transaction": ct.name,
            "shipping_amount": shipping_exp or 0,
        },
    )
    
    return {
        "payment_entry": pe.name,
        "journal_entry": je_name,
        "courier_transaction": ct.name,
        "shipping_amount": shipping_exp or 0,
    }


@frappe.whitelist()
def pay_delivery_expense(invoice_name: str, pos_profile: str):
    """
    Create (or return existing) Journal Entry for paying the courier's delivery
    expense in cash and, **atomically**, set the invoice operational state to
    "Out for delivery". This makes the endpoint idempotent – repeated calls for
    the same invoice will NOT generate duplicate Journal Entries.
    """
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")
    
    company = inv.company
    
    # Ensure the invoice is marked Out for delivery before proceeding.
    if inv.get("sales_invoice_state") != "Out for delivery":
        inv.db_set("sales_invoice_state", "Out for delivery", update_modified=False)
    
    # Determine expense amount based on invoice city
    amount = _get_delivery_expense_amount(inv)
    if amount <= 0:
        frappe.throw("No delivery expense configured for the invoice city.")
    
    # Idempotency guard – return existing submitted JE if already created
    existing_je = frappe.db.get_value(
        "Journal Entry",
        {
            "title": f"Courier Expense – {inv.name}",
            "company": company,
            "docstatus": 1,
        },
        "name",
    )
    if existing_je:
        return {"journal_entry": existing_je, "amount": amount}
    
    # Resolve ledgers for cash payment
    paid_from = _get_cash_account(pos_profile, company)
    paid_to = get_account_for_company("Freight and Forwarding Charges", company)
    
    # Build Journal Entry (credit cash-in-hand, debit expense)
    je = _create_expense_journal_entry(inv, amount, paid_from, paid_to)
    
    # Fire realtime event so other sessions update cards instantly
    frappe.publish_realtime(
        "jarz_pos_courier_expense_paid",
        {"invoice": inv.name, "journal_entry": je.name, "amount": amount},
    )
    
    return {"journal_entry": je.name, "amount": amount}


@frappe.whitelist()
def courier_delivery_expense_only(invoice_name: str, courier: str):
    """
    Record courier delivery expense to be settled later.
    Creates a **Courier Transaction** of type *Pick-Up* with **negative** amount
    and note *delivery expense only* so that the courier's outstanding balance is
    reduced by the delivery fee they will collect from us.
    """
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")
    
    # Ensure state is Out for delivery (idempotent)
    if inv.get("sales_invoice_state") != "Out for delivery":
        inv.db_set("sales_invoice_state", "Out for delivery", update_modified=False)
    
    amount = _get_delivery_expense_amount(inv)
    if amount <= 0:
        frappe.throw("No delivery expense configured for the invoice city.")
    
    # Idempotency – avoid duplicate CTs for same purpose
    existing_ct = frappe.db.get_value(
        "Courier Transaction",
        {
            "reference_invoice": inv.name,
            "type": "Pick-Up",
            "notes": ["like", "%delivery expense only%"],
        },
        "name",
    )
    if existing_ct:
        return {"courier_transaction": existing_ct, "amount": amount}
    
    # Insert Courier Transaction recording shipping expense separately (positive)
    ct = frappe.new_doc("Courier Transaction")
    ct.courier = courier
    ct.date = frappe.utils.now_datetime()
    ct.type = "Pick-Up"
    ct.reference_invoice = inv.name
    ct.amount = 0  # No principal amount involved – only shipping expense
    ct.shipping_amount = abs(amount)
    ct.notes = "delivery expense only (pay later)"
    ct.insert(ignore_permissions=True)
    
    frappe.publish_realtime(
        "jarz_pos_courier_expense_only",
        {
            "invoice": inv.name,
            "courier_transaction": ct.name,
            "shipping_amount": abs(amount),
        },
    )
    
    return {"courier_transaction": ct.name, "shipping_amount": abs(amount)}


@frappe.whitelist()
def get_courier_balances():
    """
    Return list of couriers with their current balance (= Σ amounts – Σ shipping)
    together with per-invoice breakdown for popup view.
    
    Output structure::
        [
            {
                "courier": "COURIER-0001",
                "courier_name": "FastEx",
                "balance": 1250.0,
                "details": [
                    {"invoice": "ACC-SINV-0001", "city": "Downtown", "amount": 250.0, "shipping": 10.0},
                    ...
                ]
            },
            ...
        ]
    """
    data = []
    couriers = frappe.get_all("Courier", fields=["name", "courier_name"])
    
    for c in couriers:
        rows = frappe.get_all(
            "Courier Transaction",
            filters={
                "courier": c.name,
                "status": ["!=", "Settled"],  # Exclude settled transactions
            },
            fields=["reference_invoice", "amount", "shipping_amount"]
        )
        
        total_amount = sum(float(r.amount or 0) for r in rows)
        total_shipping = sum(float(r.shipping_amount or 0) for r in rows)
        balance = total_amount - total_shipping
        
        details = []
        for r in rows:
            city = _get_invoice_city(r.reference_invoice)
            details.append({
                "invoice": r.reference_invoice,
                "city": city,
                "amount": float(r.amount or 0),
                "shipping": float(r.shipping_amount or 0)
            })
        
        data.append({
            "courier": c.name,
            "courier_name": c.courier_name or c.name,
            "balance": balance,
            "details": details,
        })
    
    # Sort by balance desc for nicer UI
    data.sort(key=lambda d: d["balance"], reverse=True)
    return data


@frappe.whitelist()
def settle_courier(courier: str, pos_profile: str | None = None):
    """
    Settle all *Unsettled* "Courier Transaction" rows for the given courier.
    
    Args:
        courier: Courier (DocType) ID.
        pos_profile: POS Profile whose cash account should be credited/used.
    
    Returns:
        dict: Summary of documents created keyed by invoice / CT.
    """
    if not courier:
        frappe.throw("Courier ID required")
    
    if not pos_profile:
        # Fallback: try to grab first enabled POS profile
        pos_profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
        if not pos_profile:
            frappe.throw("POS Profile is required to resolve Cash account")
    
    cts = frappe.get_all(
        "Courier Transaction",
        filters={"courier": courier, "status": ["!=", "Settled"]},
        fields=["name", "amount", "shipping_amount"],
    )
    
    if not cts:
        frappe.throw("No unsettled courier transactions found.")
    
    # Compute NET balance: (amount - shipping) per row
    net_balance = 0.0
    for r in cts:
        net_balance += float(r.amount or 0) - float(r.shipping_amount or 0)
    
    # Determine company (assume all CTs share same company via linked invoice or default)
    company = frappe.defaults.get_global_default("company") or frappe.db.get_single_value("Global Defaults", "default_company")
    courier_outstanding_acc = _get_courier_outstanding_account(company)
    cash_acc = _get_cash_account(pos_profile, company)
    
    je_name = None
    if abs(net_balance) > 0.005:
        je_name = _create_settlement_journal_entry(courier, net_balance, company, cash_acc, courier_outstanding_acc)
    
    # Mark all CTs as settled
    for r in cts:
        frappe.db.set_value("Courier Transaction", r.name, "status", "Settled")
    
    frappe.db.commit()
    
    # Fire realtime event
    frappe.publish_realtime(
        "jarz_pos_courier_settled",
        {"courier": courier, "journal_entry": je_name, "net_balance": net_balance},
    )
    
    return {"journal_entry": je_name, "net_balance": net_balance}


@frappe.whitelist()
def settle_courier_for_invoice(invoice_name: str, pos_profile: str | None = None):
    """Settle courier outstanding for a single invoice."""
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")
    
    # Find related courier transactions
    cts = frappe.get_all(
        "Courier Transaction",
        filters={
            "reference_invoice": invoice_name,
            "status": ["!=", "Settled"]
        },
        fields=["name", "courier", "amount", "shipping_amount"],
    )
    
    if not cts:
        frappe.throw(f"No unsettled courier transactions found for invoice {invoice_name}")
    
    # Use the courier from the first transaction (should all be same courier)
    courier = cts[0].courier
    
    # Call the main settle_courier function
    return settle_courier(courier, pos_profile)


# Helper functions

def _get_courier_outstanding_account(company: str) -> str:
    """Return the 'Courier Outstanding' ledger for the given company."""
    acc = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "account_name": ["like", "Courier Outstanding%"],
            "is_group": 0,
        },
        "name",
    )
    if acc:
        return acc
    frappe.throw(
        f"No 'Courier Outstanding' account found for company {company}.\n"
        "Please create a ledger named 'Courier Outstanding' (non-group) under Accounts Receivable."
    )


def _get_receivable_account(company):
    """Get the default receivable account for the company."""
    paid_from_account = frappe.get_value("Company", company, "default_receivable_account")
    if not paid_from_account:
        paid_from_account = frappe.get_value(
            "Account",
            {
                "account_type": "Receivable",
                "company": company,
                "is_group": 0,
            },
            "name",
        )
    if not paid_from_account:
        frappe.throw(f"No receivable account found for company {company}.")
    return paid_from_account


def _get_delivery_expense_amount(inv):
    """
    Return delivery expense amount (float) for the given invoice using its city.
    Tries to resolve city from the shipping / customer address linked to the invoice
    and then fetches the *delivery_expense* field from the **City** DocType.
    Returns ``0`` if city or expense could not be determined.
    """
    address_name = inv.get("shipping_address_name") or inv.get("customer_address")
    if not address_name:
        return 0.0
    
    try:
        addr = frappe.get_doc("Address", address_name)
    except Exception:
        return 0.0
    
    city_id = getattr(addr, "city", None)
    if not city_id:
        return 0.0
    
    try:
        expense = frappe.db.get_value("City", city_id, "delivery_expense")
        return float(expense or 0)
    except Exception:
        return 0.0


def _get_invoice_city(invoice_name):
    """Get the city name for an invoice."""
    if not invoice_name:
        return ""
    
    # Fetch shipping or customer address linked to the invoice
    si_addr = frappe.db.get_value(
        "Sales Invoice",
        invoice_name,
        ["shipping_address_name", "customer_address"],
        as_dict=True,
    )
    
    addr_name = None
    if si_addr:
        addr_name = si_addr.get("shipping_address_name") or si_addr.get("customer_address")
    
    if addr_name:
        city_id = frappe.db.get_value("Address", addr_name, "city")
        if city_id:
            city_name = frappe.db.get_value("City", city_id, "city_name")
            return city_name or city_id or ""
    
    return ""


def _create_payment_entry(inv, paid_from_account, paid_to_account, outstanding):
    """Create and submit payment entry for courier outstanding."""
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = inv.company
    pe.party_type = "Customer"
    pe.party = inv.customer
    pe.paid_from = paid_from_account  # Debtors (party account)
    pe.paid_to = paid_to_account      # Courier Outstanding (asset/receivable)
    pe.paid_amount = outstanding
    pe.received_amount = outstanding
    
    # Allocate full amount to invoice to close it
    pe.append(
        "references",
        {
            "reference_doctype": "Sales Invoice",
            "reference_name": inv.name,
            "due_date": inv.get("due_date"),
            "total_amount": inv.grand_total,
            "outstanding_amount": outstanding,
            "allocated_amount": outstanding,
        },
    )
    
    # Minimal bank fields placeholders
    pe.reference_no = f"COURIER-OUT-{inv.name}"
    pe.reference_date = frappe.utils.nowdate()
    pe.save(ignore_permissions=True)
    pe.submit()
    
    return pe


def _create_shipping_expense_journal_entry(inv, shipping_exp, paid_to_account):
    """Create journal entry for shipping expense."""
    company = inv.company
    freight_acc = get_account_for_company("Freight and Forwarding Charges", company)
    
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = frappe.utils.nowdate()
    je.company = company
    je.title = f"Courier Expense – {inv.name}"
    
    # Debit Freight Expense
    je.append(
        "accounts",
        {
            "account": freight_acc,
            "debit_in_account_currency": shipping_exp,
            "credit_in_account_currency": 0,
        },
    )
    
    # Credit Courier Outstanding (reduces receivable)
    je.append(
        "accounts",
        {
            "account": paid_to_account,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": shipping_exp,
        },
    )
    
    je.save(ignore_permissions=True)
    je.submit()
    
    return je.name


def _create_courier_transaction(courier, inv, outstanding, shipping_exp):
    """Create courier transaction log entry."""
    ct = frappe.new_doc("Courier Transaction")
    ct.courier = courier
    ct.date = frappe.utils.now_datetime()
    ct.type = "Pick-Up"
    ct.reference_invoice = inv.name
    ct.amount = outstanding
    ct.shipping_amount = shipping_exp or 0
    ct.insert(ignore_permissions=True)
    
    return ct


def _create_expense_journal_entry(inv, amount, paid_from, paid_to):
    """Create journal entry for delivery expense payment."""
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = frappe.utils.nowdate()
    je.company = inv.company
    je.title = f"Courier Expense – {inv.name}"
    
    je.append(
        "accounts",
        {
            "account": paid_from,
            "credit_in_account_currency": amount,
            "debit_in_account_currency": 0,
        },
    )
    
    je.append(
        "accounts",
        {
            "account": paid_to,
            "debit_in_account_currency": amount,
            "credit_in_account_currency": 0,
        },
    )
    
    je.save(ignore_permissions=True)
    je.submit()
    
    return je


def _create_settlement_journal_entry(courier, net_balance, company, cash_acc, courier_outstanding_acc):
    """Create journal entry for courier settlement."""
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = frappe.utils.nowdate()
    je.company = company
    je.title = f"Courier Settlement – {courier}"
    
    if net_balance > 0:
        # Courier owes us money – we RECEIVE cash
        je.append("accounts", {
            "account": cash_acc,
            "debit_in_account_currency": net_balance,
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": courier_outstanding_acc,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": net_balance,
        })
    else:
        amt = abs(net_balance)
        # We owe courier – PAY cash
        je.append("accounts", {
            "account": courier_outstanding_acc,
            "debit_in_account_currency": amt,
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": cash_acc,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": amt,
        })
    
    je.save(ignore_permissions=True)
    je.submit()
    
    return je.name

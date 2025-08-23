"""
Account Utilities for Jarz POS

This module contains utility functions for account management,
including account lookup, payment processing, and cash handling.
"""

import frappe


def get_account_for_company(account_name, company):
    """
    Get account for company with fallback options
    """
    print(f"   🔍 get_account_for_company({account_name}, {company})")
    
    # Try exact match first
    company_abbr = frappe.db.get_value("Company", company, "abbr")
    account_with_abbr = f"{account_name} - {company_abbr}"
    if frappe.db.exists("Account", account_with_abbr):
        return account_with_abbr
    
    # Fallback to searching by name and company
    acc = frappe.db.get_value("Account", {"account_name": account_name, "company": company})
    if acc:
        return acc
    
    # If still not found, throw an error
    frappe.throw(f"Could not find account '{account_name}' for company '{company}'")


def get_item_price(item_code, price_list):
    """Get item price from price list with fallback to standard rate."""
    price = frappe.db.get_value(
        "Item Price",
        {"item_code": item_code, "price_list": price_list},
        "price_list_rate",
    )
    
    if not price:
        # Fallback to item's standard selling rate if no price list rate found
        price = frappe.db.get_value("Item", item_code, "standard_selling_rate")
    
    return price


def _get_cash_account(pos_profile: str, company: str) -> str:
    """Return Cash In Hand ledger for the given POS profile."""
    # Try exact child under Cash In Hand first: "{pos_profile} - <ABBR>"
    acc = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "parent_account": ["like", "%Cash In Hand%"],
            "account_name": pos_profile,
            "is_group": 0,
        },
        "name",
    )
    
    # Fallback: partial match
    if not acc:
        acc = frappe.db.get_value(
            "Account",
            {
                "company": company,
                "parent_account": ["like", "%Cash In Hand%"],
                "account_name": ["like", f"%{pos_profile}%"],
                "is_group": 0,
            },
            "name",
        )
    
    if acc:
        return acc
    
    frappe.throw(
        f"No Cash In Hand account found for POS profile '{pos_profile}' in company {company}."
    )


# ------------------ NEW HELPER FUNCTIONS (centralized resolution) ------------------

def get_freight_expense_account(company: str) -> str:
    """Return Freight & Forwarding Charges account for company (validated)."""
    acc = get_account_for_company("Freight and Forwarding Charges", company)
    if not acc:
        frappe.throw(f"Freight and Forwarding Charges account missing for {company}")
    return acc


def get_courier_outstanding_account(company: str) -> str:
    """Return Courier Outstanding account (non-group) for company."""
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
        f"No 'Courier Outstanding' account found for company {company}. Create a ledger under Accounts Receivable."
    )


def get_pos_cash_account(pos_profile: str, company: str) -> str:
    """Public wrapper for resolving POS profile cash-in-hand child account."""
    return _get_cash_account(pos_profile, company)


def validate_account_exists(account_name: str):
    if not frappe.db.exists("Account", {"name": account_name}):
        frappe.throw(f"Account '{account_name}' does not exist.")


def get_creditors_account(company: str) -> str:
    """Resolve the company's Creditors (Payable) account.

    Strategy:
    - Company.default_payable_account
    - Exact 'Creditors - {abbr}'
    - Any non-group Account with account_type='Payable' under the company
    - Fallback to get_account_for_company('Creditors', company)
    """
    acc = frappe.get_value("Company", company, "default_payable_account")
    if acc:
        return acc

    abbr = frappe.get_value("Company", company, "abbr")
    if abbr and frappe.db.exists("Account", f"Creditors - {abbr}"):
        return f"Creditors - {abbr}"

    acc = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "account_type": "Payable",
            "is_group": 0,
        },
        "name",
    )
    if acc:
        return acc

    # Last resort: try by name + company via helper
    return get_account_for_company("Creditors", company)


@frappe.whitelist()
def create_online_payment_entry(
    invoice_name: str,
    payment_mode: str,
    pos_profile: str | None = None,
):
    """
    Create a Payment Entry for the given invoice and mark it as paid.
    
    Args:
        invoice_name (str): The name of the Sales Invoice (e.g., "ACC-SINV-2023-0001").
        payment_mode (str): The payment mode (e.g., "Instapay", "Payment Gateway").
        pos_profile (str, optional): The POS Profile name. Required for cash payments.
    
    Returns:
        dict: A dictionary containing the payment entry details.
    
    Raises:
        frappe.ValidationError: If the invoice is not submitted, or if the payment mode is invalid.
    """
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    
    # Check if invoice is submitted
    if invoice.docstatus != 1:
        frappe.throw("Invoice must be submitted before payment can be recorded.")
    
    # Determine the payment account based on the payment mode
    paid_to_account = _get_payment_account(payment_mode, invoice.company)
    
    # Get the default receivable account for the company
    paid_from_account = frappe.get_value("Company", invoice.company, "default_receivable_account")
    if not paid_from_account:
        frappe.throw("No receivable account found for the company.")
    
    # Create a new Payment Entry
    payment_entry = _create_payment_entry_document(invoice, payment_mode, paid_from_account, paid_to_account)
    
    # Save and submit the payment entry
    payment_entry.save(ignore_permissions=True)
    payment_entry.submit()
    
    # Return the created payment entry
    return payment_entry


def _get_payment_account(payment_mode, company):
    """Get the appropriate payment account based on payment mode."""
    if payment_mode in ["Instapay", "Payment Gateway"]:
        # For online payments, find a suitable bank account
        paid_to_account = frappe.db.get_value(
            "Account",
            {
                "company": company,
                "parent_account": ["like", "%Bank Accounts%"],
                "is_group": 0,
            },
            "name",
        )
        if not paid_to_account:
            frappe.throw("No suitable bank account found for the payment.")
    elif payment_mode == "Mobile Wallet":
        # For mobile wallet payments, find a suitable mobile wallet account
        paid_to_account = frappe.db.get_value(
            "Account",
            {
                "company": company,
                "account_name": ["like", "Mobile Wallet%"],
                "is_group": 0,
            },
            "name",
        )
        if not paid_to_account:
            frappe.throw("No suitable mobile wallet account found for the payment.")
    else:
        frappe.throw("Invalid payment mode. Please select a valid payment method.")
    
    return paid_to_account


def _create_payment_entry_document(invoice, payment_mode, paid_from_account, paid_to_account):
    """Create the payment entry document."""
    payment_entry = frappe.new_doc("Payment Entry")
    payment_entry.payment_type = "Receive"
    payment_entry.mode_of_payment = payment_mode
    payment_entry.company = invoice.company
    payment_entry.party_type = "Customer"
    payment_entry.party = invoice.customer
    payment_entry.paid_from = paid_from_account
    payment_entry.paid_to = paid_to_account
    payment_entry.paid_amount = invoice.grand_total
    payment_entry.received_amount = invoice.grand_total
    
    # Add a reference to the invoice in the payment entry
    payment_entry.append(
        "references",
        {
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice.name,
            "due_date": invoice.get("due_date"),
            "total_amount": invoice.grand_total,
            "outstanding_amount": invoice.grand_total,
            "allocated_amount": invoice.grand_total,
        },
    )
    
    # Set default reference number and date if not provided
    if not payment_entry.get("reference_no"):
        timestamp = frappe.utils.now_datetime().strftime("%Y%m%d-%H%M%S")
        payment_entry.reference_no = f"POS-{payment_mode.upper().replace(' ', '')}-{timestamp}"
    
    if not payment_entry.get("reference_date"):
        payment_entry.reference_date = frappe.utils.nowdate()
    
    # Stick to invoice currency
    payment_entry.source_exchange_rate = 1
    payment_entry.target_exchange_rate = 1
    
    return payment_entry

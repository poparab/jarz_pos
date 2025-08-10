"""
Delivery Utilities for Jarz POS

Handles delivery charges integration with ERPNext's Sales Taxes and Charges table.
Delivery charges are added as 'Actual' type charges to the Freight and Forwarding Charges account.
"""

import frappe
from frappe import _
from frappe.utils import flt


def get_delivery_account(company):
    """
    Get the Freight and Forwarding Charges account for the company
    Format: "Freight and Forwarding Charges - {company_abbr}"
    """
    try:
        # Get company abbreviation
        company_doc = frappe.get_doc("Company", company)
        company_abbr = company_doc.abbr
        
        # Construct account name
        account_name = f"Freight and Forwarding Charges - {company_abbr}"
        
        # Verify account exists
        if not frappe.db.exists("Account", account_name):
            # Try to find similar account
            similar_accounts = frappe.get_all("Account", 
                filters={
                    "account_name": ["like", "%Freight%"],
                    "company": company
                },
                fields=["name"])
            
            if similar_accounts:
                account_name = similar_accounts[0].name
                frappe.log_error(f"Using similar account: {account_name} for delivery charges", "Delivery Charges")
            else:
                # Try to find any expense account as fallback
                expense_accounts = frappe.get_all("Account",
                    filters={
                        "account_type": "Expense Account",
                        "company": company,
                        "is_group": 0
                    },
                    fields=["name"],
                    limit=1)
                
                if expense_accounts:
                    account_name = expense_accounts[0].name
                    frappe.log_error(f"Using fallback expense account: {account_name} for delivery charges", "Delivery Charges")
                else:
                    frappe.throw(_("No suitable account found for delivery charges in company {0}").format(company))
                
        return account_name
    except Exception as e:
        frappe.log_error(f"Error getting delivery account: {str(e)}", "Delivery Charges")
        raise


def add_delivery_charges_to_taxes(invoice_doc, delivery_charges, delivery_description="Delivery Charges"):
    """
    Add delivery charges to Sales Taxes and Charges table
    As per requirements: Type=Actual, Account=Freight and Forwarding Charges - {abbr}
    """
    if not delivery_charges or flt(delivery_charges) <= 0:
        frappe.log_error("No delivery charges to add or invalid amount", "Delivery Charges")
        return
        
    try:
        # Get the correct account
        delivery_account = get_delivery_account(invoice_doc.company)
        
        # Get cost center for the company
        cost_center = invoice_doc.cost_center or frappe.get_cached_value('Company', 
                                                                         invoice_doc.company, 
                                                                         'cost_center')
        
        # Check if taxes table exists, if not create it
        if not hasattr(invoice_doc, 'taxes'):
            invoice_doc.taxes = []
            
        # Calculate running total
        current_total = invoice_doc.net_total or 0
        for tax in invoice_doc.taxes:
            current_total += flt(tax.tax_amount)
            
        # Add delivery charge entry
        tax_row = {
            'charge_type': 'Actual',  # As specified in requirements
            'account_head': delivery_account,
            'description': delivery_description,
            'tax_amount': flt(delivery_charges),
            'total': current_total + flt(delivery_charges),  # Update running total
            'base_tax_amount': flt(delivery_charges),  # Base currency amount
            'cost_center': cost_center
        }
        
        invoice_doc.append('taxes', tax_row)
        
        frappe.log_error(f"Successfully added delivery charges: {delivery_charges} to account {delivery_account}", "Delivery Charges")
        
    except Exception as e:
        frappe.log_error(f"Error adding delivery charges: {str(e)}", "Delivery Charges")
        # Don't fail invoice for delivery charge errors - just log and continue
        pass


def validate_delivery_charges(delivery_charges):
    """
    Validate delivery charges before adding to invoice
    """
    try:
        charges = flt(delivery_charges)
        if charges < 0:
            return False, "Delivery charges cannot be negative"
        if charges > 10000:  # Reasonable upper limit
            return False, "Delivery charges seem too high (over 10,000)"
        return True, "Valid delivery charges"
    except Exception as e:
        return False, f"Invalid delivery charges format: {str(e)}"


def get_delivery_tax_summary(invoice_doc):
    """
    Get summary of delivery charges from invoice taxes
    """
    delivery_charges = 0
    delivery_entries = []
    
    try:
        if hasattr(invoice_doc, 'taxes') and invoice_doc.taxes:
            for tax in invoice_doc.taxes:
                if "freight" in (tax.description or "").lower() or "delivery" in (tax.description or "").lower():
                    delivery_charges += flt(tax.tax_amount)
                    delivery_entries.append({
                        'description': tax.description,
                        'amount': tax.tax_amount,
                        'account': tax.account_head
                    })
                    
        return {
            'total_delivery_charges': delivery_charges,
            'delivery_entries': delivery_entries,
            'has_delivery_charges': delivery_charges > 0
        }
    except Exception as e:
        frappe.log_error(f"Error getting delivery tax summary: {str(e)}", "Delivery Charges")
        return {
            'total_delivery_charges': 0,
            'delivery_entries': [],
            'has_delivery_charges': False
        }

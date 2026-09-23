"""
Delivery Utilities for Jarz POS

Handles delivery charges integration with ERPNext's Sales Taxes and Charges table.
Delivery charges are added as 'Actual' type charges to the Shipping Income account.
"""

import frappe
from frappe import _
from frappe.utils import flt


def get_delivery_account(company):
    """
    Return the account the customer's delivery charge is credited to.

    Shipping Income - {abbr}. Until 2026-09 this was Freight and Forwarding
    Charges, which netted what the customer paid for delivery against what the
    courier cost and left a negative expense in the P&L.
    """
    from jarz_pos.utils.account_utils import get_shipping_income_account

    return get_shipping_income_account(company)


def add_delivery_charges_to_taxes(invoice_doc, delivery_charges, delivery_description="Delivery Charges"):
    """
    Add delivery charges to Sales Taxes and Charges table
    Type=Actual, Account=Shipping Income - {abbr} (see get_delivery_account)
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

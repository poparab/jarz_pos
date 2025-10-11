"""
Jarz POS Custom Page - Refactored Main Module

This is the main entry point for the Jarz POS custom page.
All functionality has been split into focused modules for better maintainability.
"""

import json
import traceback

import frappe

from jarz_pos.services.delivery_handling import (
    courier_delivery_expense_only,
    get_courier_balances,
    mark_courier_outstanding,
    pay_delivery_expense,
    settle_courier,
    settle_courier_for_invoice,
)

# Import from our services structure
from jarz_pos.services.invoice_creation import create_pos_invoice
from jarz_pos.utils.account_utils import create_online_payment_entry, get_account_for_company, get_item_price

# Debug: File loaded timestamp
print(f"🔄 custom_pos.py loaded at {frappe.utils.now()}")

try:
    from erpnext.stock.stock_ledger import NegativeStockError
except ImportError:
    class NegativeStockError(Exception):
        pass


@frappe.whitelist()
def get_context(context):
    """Page context for custom POS"""
    context.title = "Jarz POS"
    return context


# Re-export main functions for backward compatibility
create_pos_invoice = create_pos_invoice
mark_courier_outstanding = mark_courier_outstanding
pay_delivery_expense = pay_delivery_expense
courier_delivery_expense_only = courier_delivery_expense_only
get_courier_balances = get_courier_balances
settle_courier = settle_courier
settle_courier_for_invoice = settle_courier_for_invoice
get_account_for_company = get_account_for_company
get_item_price = get_item_price
create_online_payment_entry = create_online_payment_entry


# Permission functions
def get_permission_query_conditions(user):
    """Permission check for accessing the page"""
    return ""


def has_permission(doc, user):
    """Check if user has permission to access the page"""
    return True

"""Debug API to check accounts"""

import frappe


@frappe.whitelist()
def check_cash_accounts():
    """Check Cash In Hand accounts for debugging"""
    
    # Get all Cash In Hand children
    accounts = frappe.get_all(
        "Account",
        filters={
            "company": "JARZ",
            "parent_account": ["like", "%Cash In Hand%"],
            "is_group": 0,
        },
        fields=["name", "account_name", "parent_account"],
    )
    
    # Check for Ahram Gardens specific
    ahram_accounts = frappe.get_all(
        "Account",
        filters={
            "company": "JARZ",
            "account_name": ["like", "%Ahram%"],
            "is_group": 0,
        },
        fields=["name", "account_name", "parent_account", "account_type"],
    )
    
    return {
        "cash_in_hand_accounts": accounts,
        "ahram_accounts": ahram_accounts,
    }

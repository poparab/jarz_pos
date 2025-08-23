# Version
__version__ = '0.0.1'

import frappe
import sys as _sys

# --- Early alias registration (must precede any submodule imports using double path) ---
# Allow imports like jarz_pos.jarz_pos.utils.*
_sys.modules.setdefault('jarz_pos.jarz_pos', _sys.modules[__name__])

# Pre-register common subpackages if already importable (ignore failures silently)
for _pkg_name in ('api', 'services', 'utils', 'events', 'doctype'):
    try:
        _pkg = __import__(f'{__name__}.{_pkg_name}', fromlist=['*'])
        _sys.modules[f'jarz_pos.jarz_pos.{_pkg_name}'] = _pkg
    except Exception:
        pass

# --- Helper to ensure custom field ---
def ensure_sales_invoice_state_field():
    """Ensure the sales_invoice_state custom field exists with proper options"""
    try:
        from frappe.custom.doctype.custom_field.custom_field import create_custom_field
        if not frappe.db.exists("Custom Field", {"dt": "Sales Invoice", "fieldname": "sales_invoice_state"}):
            create_custom_field("Sales Invoice", {
                "fieldname": "sales_invoice_state",
                "label": "State",
                "fieldtype": "Select",
                "options": "\nReceived\nIn Progress\nReady\nOut for Delivery\nDelivered\nCancelled",
                "insert_after": "status",
                "in_list_view": 1,
                "in_standard_filter": 1,
                "default": "Received"
            })
            frappe.db.commit()
            return True
        return False
    except Exception as e:
        if hasattr(frappe, 'log_error'):
            try:
                frappe.log_error(f"Error creating custom field: {str(e)}", "Kanban Setup")
            except Exception:
                print(f"[Jarz POS] Error creating custom field: {e}")
        return False

# --- Import kanban API functions (after alias setup) ---
try:
    from .api.kanban import (
        get_kanban_columns,
        get_kanban_invoices,
        update_invoice_state,
        get_invoice_details,
        get_kanban_filters,
    )
    from .api import kanban
    __all__ = [
        'get_kanban_columns',
        'get_kanban_invoices',
        'update_invoice_state',
        'get_invoice_details',
        'get_kanban_filters',
        'kanban',
        'ensure_sales_invoice_state_field'
    ]
except ImportError as e:
    # Log once; printing fallback if db/log unavailable
    try:
        if hasattr(frappe, 'log_error'):
            frappe.log_error(f"Failed to import kanban functions: {str(e)}", "Jarz POS Import Error")
        else:
            print(f"[Jarz POS] Failed to import kanban functions: {e}")
    except Exception:
        print(f"[Jarz POS] Failed to import kanban functions: {e}")

    def get_kanban_columns():
        return {"success": False, "error": "Kanban module not properly imported"}
    def get_kanban_invoices(filters=None):
        return {"success": False, "error": "Kanban module not properly imported"}
    def update_invoice_state(invoice_id, new_state):
        return {"success": False, "error": "Kanban module not properly imported"}
    def get_invoice_details(invoice_id):
        return {"success": False, "error": "Kanban module not properly imported"}
    def get_kanban_filters():
        return {"success": False, "error": "Kanban module not properly imported"}
    __all__ = [
        'get_kanban_columns',
        'get_kanban_invoices',
        'update_invoice_state',
        'get_invoice_details',
        'get_kanban_filters',
        'ensure_sales_invoice_state_field'
    ]

# Attempt to ensure custom field (safe)
try:
    if hasattr(frappe, 'db') and frappe.db:
        ensure_sales_invoice_state_field()
except Exception:
    pass

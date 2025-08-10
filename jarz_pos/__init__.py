# Version
__version__ = '0.0.1'

# Import and expose API functions at root level for global access
import frappe

def ensure_sales_invoice_state_field():
    """Ensure the sales_invoice_state custom field exists with proper options"""
    try:
        from frappe.custom.doctype.custom_field.custom_field import create_custom_field
        
        # Check if field exists
        if not frappe.db.exists("Custom Field", {"dt": "Sales Invoice", "fieldname": "sales_invoice_state"}):
            # Create the custom field
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
            frappe.log_error(f"Error creating custom field: {str(e)}", "Kanban Setup")
        return False

# Try to import kanban functions and make them available at root level
try:
    from .api.kanban import (
        get_kanban_columns,
        get_kanban_invoices, 
        update_invoice_state,
        get_invoice_details,
        get_kanban_filters,
    )
    
    # Also import the module itself for direct access
    from .api import kanban
    
    # Re-export these functions at module root to make them accessible
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
    if hasattr(frappe, 'log_error'):
        frappe.log_error(f"Failed to import kanban functions: {str(e)}", "Jarz POS Import Error")
    
    # Create fallback functions
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

# Try to ensure custom field exists on import (only if frappe is initialized)
try:
    if hasattr(frappe, 'db') and frappe.db:
        ensure_sales_invoice_state_field()
except Exception:
    # Silent fail during import - field will be created when needed
    pass

# Alias support for double-jarz import paths used by Frappe HTTP RPC
# Make `import jarz_pos.jarz_pos.api` resolve to this package's subpackages
import sys as _sys

# Register this package again under the nested name
_sys.modules.setdefault('jarz_pos.jarz_pos', _sys.modules[__name__])

# Optionally pre-register common subpackages if already imported later
try:
	from . import api as _api  # noqa: F401
	_sys.modules['jarz_pos.jarz_pos.api'] = _api
except Exception:
	pass
try:
	from . import services as _services  # noqa: F401
	_sys.modules['jarz_pos.jarz_pos.services'] = _services
except Exception:
	pass
try:
	from . import utils as _utils  # noqa: F401
	_sys.modules['jarz_pos.jarz_pos.utils'] = _utils
except Exception:
	pass
try:
	from . import events as _events  # noqa: F401
	_sys.modules['jarz_pos.jarz_pos.events'] = _events
except Exception:
	pass
try:
	from . import doctype as _doctype  # noqa: F401
	_sys.modules['jarz_pos.jarz_pos.doctype'] = _doctype
except Exception:
	pass

"""Test Kanban Setup - Verification utilities for kanban functionality"""

import frappe

@frappe.whitelist(allow_guest=False)
def test_kanban_setup():
    """Test function to verify kanban setup"""
    results = {
        "custom_field_exists": False,
        "custom_field_options": None,
        "functions_whitelisted": {},
        "test_get_columns": None,
        "test_get_filters": None,
        "frappe_user": frappe.session.user,
        "site": frappe.local.site
    }
    
    try:
        # Check if custom field exists
        custom_field = frappe.db.exists("Custom Field", {
            "dt": "Sales Invoice", 
            "fieldname": "sales_invoice_state"
        })
        results["custom_field_exists"] = bool(custom_field)
        
        if custom_field:
            # Get the field options
            field_doc = frappe.get_doc("Custom Field", custom_field)
            results["custom_field_options"] = field_doc.options
        
        # Test if functions are accessible
        from jarz_pos.api import kanban
        
        functions_to_test = [
            'get_kanban_columns',
            'get_kanban_invoices',
            'update_invoice_state',
            'get_invoice_details',
            'get_kanban_filters'
        ]
        
        for func_name in functions_to_test:
            try:
                func = getattr(kanban, func_name)
                results["functions_whitelisted"][func_name] = {
                    "exists": True,
                    "whitelisted": hasattr(func, 'whitelisted') or (hasattr(func, '__func__') and hasattr(func.__func__, 'whitelisted'))
                }
            except AttributeError:
                results["functions_whitelisted"][func_name] = {
                    "exists": False,
                    "whitelisted": False
                }
        
        # Try to call the functions
        try:
            results["test_get_columns"] = kanban.get_kanban_columns()
        except Exception as e:
            results["test_get_columns"] = f"Error: {str(e)}"
            
        try:
            results["test_get_filters"] = kanban.get_kanban_filters()
        except Exception as e:
            results["test_get_filters"] = f"Error: {str(e)}"
            
    except Exception as e:
        results["error"] = str(e)
        import traceback
        results["traceback"] = traceback.format_exc()
    
    return results

@frappe.whitelist(allow_guest=False)
def create_sales_invoice_state_field():
    """Create the sales_invoice_state custom field if it doesn't exist.

    System Manager only: this mutates the SCHEMA (inserts a Custom Field on Sales
    Invoice, which alters the table). ``allow_guest=False`` alone meant any logged
    in user — every POS cashier — could change the schema.

    Kept rather than deleted because ``sales_invoice_state`` is still read as the
    legacy fallback for ``custom_sales_invoice_state`` across api/kanban.py,
    api/manager.py, api/notifications.py and events/sales_invoice.py, so this is a
    live recovery tool rather than dead code.
    """
    frappe.only_for("System Manager")
    try:
        # Check if field already exists
        if frappe.db.exists("Custom Field", {"dt": "Sales Invoice", "fieldname": "sales_invoice_state"}):
            return {"success": True, "message": "Field already exists", "created": False}
        
        from frappe.custom.doctype.custom_field.custom_field import create_custom_field
        
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
        return {"success": True, "message": "Custom field created successfully", "created": True}
        
    except Exception as e:
        frappe.log_error(f"Error creating custom field: {str(e)}", "Kanban Setup")
        return {"success": False, "error": str(e)}

@frappe.whitelist(allow_guest=False) 
def fix_existing_invoices_state():
    """Set default state for existing POS invoices that don't have a state.

    System Manager only: this is an unbounded bulk write. It loads EVERY submitted
    POS invoice with an empty state and ``save(ignore_permissions=True)``s each one
    — across every branch, with no scoping and no limit. Any logged in user could
    fire it and rewrite the whole board.

    Kept rather than deleted for the same reason as the field creator above: the
    legacy state field is still read, so the backfill still has a purpose.
    """
    frappe.only_for("System Manager")
    try:
        # Find POS invoices without a state
        invoices = frappe.get_all(
            "Sales Invoice",
            filters={
                "is_pos": 1,
                "docstatus": 1,
                "sales_invoice_state": ["in", ["", None]]
            },
            fields=["name"]
        )
        
        count = 0
        for invoice in invoices:
            doc = frappe.get_doc("Sales Invoice", invoice.name)
            doc.sales_invoice_state = "Received"
            doc.save(ignore_permissions=True)
            count += 1
        
        frappe.db.commit()
        return {"success": True, "message": f"Updated {count} invoices with default state", "count": count}
        
    except Exception as e:
        frappe.log_error(f"Error fixing invoice states: {str(e)}", "Kanban Setup")
        return {"success": False, "error": str(e)}

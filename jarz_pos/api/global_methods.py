"""
Global whitelisted methods for Kanban functionality.
These are registered at the global scope to avoid nested module path issues.
"""
import frappe

from .kanban import (
    get_invoice_details as _get_invoice_details,
)
from .kanban import (
    get_kanban_columns as _get_kanban_columns,
)
from .kanban import (
    get_kanban_filters as _get_kanban_filters,
)
from .kanban import (
    get_kanban_invoices as _get_kanban_invoices,
)
from .kanban import (
    update_invoice_state as _update_invoice_state,
)


@frappe.whitelist(allow_guest=False)
def get_kanban_columns():
    """Get all available Kanban columns based on Sales Invoice State field options."""
    return _get_kanban_columns()

@frappe.whitelist(allow_guest=False)
def get_kanban_invoices(filters=None):
    """Get Sales Invoices organized by their state for Kanban display."""
    return _get_kanban_invoices(filters=filters)

@frappe.whitelist(allow_guest=False)
def update_invoice_state(invoice_id, new_state):
    """Update the sales_invoice_state of a Sales Invoice."""
    return _update_invoice_state(invoice_id=invoice_id, new_state=new_state)

@frappe.whitelist(allow_guest=False)
def get_invoice_details(invoice_id):
    """Get detailed information about a specific invoice."""
    return _get_invoice_details(invoice_id=invoice_id)

@frappe.whitelist(allow_guest=False)
def get_kanban_filters():
    """Get available filter options for the Kanban board."""
    return _get_kanban_filters()

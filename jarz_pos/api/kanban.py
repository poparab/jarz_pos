"""Jarz POS - Kanban board API endpoints.
This module provides API endpoints for the Sales Invoice Kanban board functionality. 
All operations are based on the 'sales_invoice_state' custom field.
"""
from __future__ import annotations
import frappe
import json
import traceback
from typing import Dict, List, Any, Optional, Union
from jarz_pos.jarz_pos.utils.invoice_utils import (
    get_address_details,
    format_invoice_data,
    apply_invoice_filters
)

# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_kanban_columns() -> Dict[str, Any]:
    """Get all available Kanban columns based on Sales Invoice State field options.
    
    Returns:
        Dict with success status and columns data
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_columns called by {0}".format(frappe.session.user))
        
        # Get the custom field definition for sales_invoice_state
        custom_field = frappe.get_doc("Custom Field", {
            "dt": "Sales Invoice",
            "fieldname": "sales_invoice_state"
        })
        
        # Parse the options and create columns
        options = custom_field.options.split('\n') if custom_field.options else []
        columns = []
        
        # Color mapping for different states
        color_map = {
            "Received": "#E3F2FD",      # Light Blue
            "Processing": "#FFF3E0",    # Light Orange
            "Preparing": "#F3E5F5",     # Light Purple
            "Out for delivery": "#E8F5E8", # Light Green
            "Completed": "#E0F2F1"      # Light Teal
        }
        
        for i, option in enumerate(options):
            option = option.strip()
            if option:
                column_id = option.lower().replace(' ', '_')
                columns.append({
                    "id": column_id,
                    "name": option,
                    "color": color_map.get(option, "#F5F5F5"),  # Default gray
                    "order": i
                })
        
        return {
            "success": True,
            "columns": columns
        }
        
    except Exception as e:
        error_msg = f"Error getting kanban columns: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Columns Error: {str(e)}", "Kanban API")
        return {
            "success": False,
            "error": error_msg
        }

@frappe.whitelist()
def get_kanban_invoices(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
    """Get Sales Invoices organized by their state for Kanban display.
    
    Args:
        filters: Filter conditions for invoice selection
        
    Returns:
        Dict with success status and invoices organized by state
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_invoices called with filters: {0}".format(filters))
        
        # Apply filters
        filter_conditions = apply_invoice_filters(filters)
        
        # Fetch all matching Sales Invoices
        fields = [
            "name", "customer", "customer_name", "territory", "posting_date",
            "posting_time", "grand_total", "net_total", "total_taxes_and_charges",
            "status", "sales_invoice_state", "required_delivery_datetime",
            "shipping_address_name", "customer_address"
        ]
        
        invoices = frappe.get_all(
            "Sales Invoice",
            filters=filter_conditions,
            fields=fields,
            order_by="posting_date desc, posting_time desc"
        )
        
        # Get address information for invoices
        invoice_addresses = {}
        for inv in invoices:
            address_name = inv.get("shipping_address_name") or inv.get("customer_address")
            invoice_addresses[inv.name] = get_address_details(address_name)
        
        # Get items for each invoice
        invoice_items = {}
        for inv in invoices:
            items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": inv.name},
                fields=["item_code", "item_name", "qty", "rate", "amount"]
            )
            invoice_items[inv.name] = items
        
        # Organize invoices by state
        kanban_data = {}
        
        # Get all possible states from custom field
        custom_field = frappe.get_doc("Custom Field", {
            "dt": "Sales Invoice",
            "fieldname": "sales_invoice_state"
        })
        all_states = custom_field.options.split('\n') if custom_field.options else []
        
        # Initialize empty lists for all states
        for state in all_states:
            state = state.strip()
            if state:
                state_key = state.lower().replace(' ', '_')
                kanban_data[state_key] = []
        
        # Organize invoices by their current state
        for inv in invoices:
            state = inv.get("sales_invoice_state") or "Received"  # Default state
            state_key = state.lower().replace(' ', '_')
            
            # Create invoice card data
            invoice_card = {
                "name": inv.name,
                "invoice_id_short": inv.name.split('-')[-1] if '-' in inv.name else inv.name,
                "customer_name": inv.customer_name or inv.customer,
                "customer": inv.customer,
                "territory": inv.territory or "",
                "required_delivery_date": inv.required_delivery_datetime,
                "status": state,  # Use the sales_invoice_state as status
                "posting_date": str(inv.posting_date),
                "grand_total": float(inv.grand_total or 0),
                "net_total": float(inv.net_total or 0),
                "total_taxes_and_charges": float(inv.total_taxes_and_charges or 0),
                "full_address": invoice_addresses.get(inv.name, ""),
                "items": invoice_items.get(inv.name, [])
            }
            
            # Add to appropriate state column
            if state_key not in kanban_data:
                kanban_data[state_key] = []
            kanban_data[state_key].append(invoice_card)
        
        return {
            "success": True,
            "data": kanban_data
        }
        
    except Exception as e:
        error_msg = f"Error getting kanban invoices: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Invoices Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return {
            "success": False,
            "error": error_msg
        }

@frappe.whitelist()
def update_invoice_state(invoice_id: str, new_state: str) -> Dict[str, Any]:
    """Update the sales_invoice_state of a Sales Invoice.
    
    Args:
        invoice_id: ID of the Sales Invoice to update
        new_state: New state value to set
        
    Returns:
        Dict with success status and message
    """
    try:
        frappe.logger().debug(f"KANBAN API: update_invoice_state - Invoice: {invoice_id}, New state: {new_state}")
        
        # Get the invoice document
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        old_state = invoice.get("sales_invoice_state")
        
        # Update the state
        invoice.sales_invoice_state = new_state
        invoice.save(ignore_permissions=True)
        
        # Publish real-time update
        frappe.publish_realtime(
            "jarz_pos_invoice_state_change",
            {
                "invoice_id": invoice_id,
                "old_state": old_state,
                "new_state": new_state,
                "updated_by": frappe.session.user,
                "timestamp": frappe.utils.now()
            },
            user="*"  # Broadcast to all users
        )
        
        return {
            "success": True,
            "message": f"Invoice {invoice_id} state updated to {new_state}"
        }
        
    except Exception as e:
        error_msg = f"Error updating invoice state: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Update Invoice State Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return {
            "success": False,
            "error": error_msg
        }

@frappe.whitelist()
def get_invoice_details(invoice_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific invoice.
    
    Args:
        invoice_id: ID of the Sales Invoice to retrieve
        
    Returns:
        Dict with success status and invoice details
    """
    try:
        frappe.logger().debug(f"KANBAN API: get_invoice_details - Invoice: {invoice_id}")
        
        # Get the invoice document
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        
        # Use utility function to format invoice data
        invoice_data = format_invoice_data(invoice)
        
        return {
            "success": True,
            "data": invoice_data
        }
        
    except Exception as e:
        error_msg = f"Error getting invoice details: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Invoice Details Error: {str(e)}", "Kanban API")
        return {
            "success": False,
            "error": error_msg
        }

@frappe.whitelist()
def get_kanban_filters() -> Dict[str, Any]:
    """Get available filter options for the Kanban board.
    
    Returns:
        Dict with success status and filter options
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_filters called")
        
        # Get unique customers from Sales Invoices
        customers = frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1, "is_pos": 1},
            fields=["customer", "customer_name"],
            distinct=True,
            order_by="customer_name"
        )
        
        customer_options = []
        for customer in customers:
            customer_options.append({
                "value": customer.customer,
                "label": customer.customer_name or customer.customer
            })
        
        # Get available states
        custom_field = frappe.get_doc("Custom Field", {
            "dt": "Sales Invoice",
            "fieldname": "sales_invoice_state"
        })
        
        state_options = []
        if custom_field.options:
            for state in custom_field.options.split('\n'):
                state = state.strip()
                if state:
                    state_options.append({
                        "value": state,
                        "label": state
                    })
        
        return {
            "success": True,
            "customers": customer_options,
            "states": state_options
        }
        
    except Exception as e:
        error_msg = f"Error getting kanban filters: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Filters Error: {str(e)}", "Kanban API")
        return {
            "success": False,
            "error": error_msg
        }

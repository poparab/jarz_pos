"""Jarz POS - Kanban board API endpoints.
This module provides API endpoints for the Sales Invoice Kanban board functionality. 
Primary state field: 'custom_sales_invoice_state' (legacy fallback: 'sales_invoice_state').
"""
from __future__ import annotations
import frappe
import json
import traceback
from typing import Dict, List, Any, Optional, Union, Tuple

# Import utility functions with fallback if they don't exist
try:
    from jarz_pos.utils.invoice_utils import (
        get_address_details,
        format_invoice_data,
        apply_invoice_filters
    )
except ImportError:
    # Fallback implementations if utils don't exist
    def get_address_details(address_name: str) -> str:
        if not address_name:
            return ""
        try:
            address_doc = frappe.get_doc("Address", address_name)
            return f"{address_doc.address_line1 or ''}, {address_doc.city or ''}".strip(", ")
        except Exception:
            return ""
    
    def format_invoice_data(invoice: frappe.Document) -> Dict[str, Any]:
        address_name = invoice.get("shipping_address_name") or invoice.get("customer_address")
        items = [{"item_code": item.item_code, "item_name": item.item_name, 
                 "qty": float(item.qty), "rate": float(item.rate), "amount": float(item.amount)}
                for item in invoice.items]
        state_val = invoice.get("custom_sales_invoice_state") or invoice.get("sales_invoice_state") or "Received"
        return {
            "name": invoice.name,
            "invoice_id_short": invoice.name.split('-')[-1] if '-' in invoice.name else invoice.name,
            "customer_name": invoice.customer_name or invoice.customer,
            "customer": invoice.customer,
            "territory": invoice.territory or "",
            "required_delivery_date": invoice.get("required_delivery_datetime"),
            "status": state_val,
            "posting_date": str(invoice.posting_date),
            "grand_total": float(invoice.grand_total or 0),
            "net_total": float(invoice.net_total or 0),
            "total_taxes_and_charges": float(invoice.total_taxes_and_charges or 0),
            "full_address": get_address_details(address_name),
            "items": items
        }
    
    def apply_invoice_filters(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
        filter_conditions = {"docstatus": 1, "is_pos": 1}
        if not filters:
            return filter_conditions
        
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except json.JSONDecodeError:
                return filter_conditions
        
        if filters.get('dateFrom'):
            filter_conditions["posting_date"] = [">=", filters['dateFrom']]
        if filters.get('dateTo'):
            if "posting_date" in filter_conditions:
                filter_conditions["posting_date"] = ["between", [filters['dateFrom'], filters['dateTo']]]
            else:
                filter_conditions["posting_date"] = ["<=", filters['dateTo']]
        if filters.get('customer'):
            filter_conditions["customer"] = filters['customer']
        if filters.get('amountFrom'):
            filter_conditions["grand_total"] = [">=", filters['amountFrom']]
        if filters.get('amountTo'):
            if "grand_total" in filter_conditions:
                filter_conditions["grand_total"] = ["between", [filters['amountFrom'], filters['amountTo']]]
            else:
                filter_conditions["grand_total"] = ["<=", filters['amountTo']]
        
        return filter_conditions

# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

# REPLACED: direct Custom Field doc fetch (requires permissions) with meta-based access
# which is available to all authenticated users and avoids 403 on restricted roles.

def _get_state_field_options() -> List[str]:
    """Return list of state options from Sales Invoice meta without reading Custom Field doc.
    Prefers 'custom_sales_invoice_state', falls back to legacy names.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        # Prefer new canonical field first
        field_names = ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]
        for field_name in field_names:
            field = meta.get_field(field_name)
            if field and getattr(field, 'options', None):
                options = [opt.strip() for opt in field.options.split('\n') if opt.strip()]
                if options:
                    frappe.logger().info(f"Found state field: {field_name} with options: {options}")
                    return options
        frappe.logger().warning("No state field found, using default states")
        return ["Received", "In Progress", "Ready", "Out for Delivery", "Delivered", "Cancelled"]
    except Exception as e:
        frappe.logger().error(f"Error getting state field options: {str(e)}")
        return ["Received", "In Progress", "Ready", "Out for Delivery", "Delivered", "Cancelled"]

# Backwards compatibility wrappers (kept in case referenced elsewhere in file)

def _get_state_custom_field():  # noqa: intentionally returns None now
    return None

def _get_allowed_states() -> List[str]:  # override previous implementation
    return _get_state_field_options()

def _state_key(label: str) -> str:
    return (label or "").strip().lower().replace(' ', '_')

# Unified success / error builders

def _success(**kwargs):
    payload = {"success": True}
    payload.update(kwargs)
    return payload

def _failure(msg: str):
    return {"success": False, "error": msg}

# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def get_kanban_columns() -> Dict[str, Any]:
    """Get all available Kanban columns based on Sales Invoice State field options.
    
    Returns:
        Dict with success status and columns data
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_columns called by {0}".format(frappe.session.user))
        options = _get_state_field_options()
        if not options:
            return _failure("Field 'sales_invoice_state' not found or has no options on Sales Invoice")
        columns = []
        # Color mapping for different states
        color_map = {
            "Received": "#E3F2FD",
            "Processing": "#FFF3E0",
            "Preparing": "#F3E5F5",
            "Out for delivery": "#E8F5E8",
            "Completed": "#E0F2F1"
        }
        for i, option in enumerate(options):
            column_id = _state_key(option)
            columns.append({
                "id": column_id,
                "name": option,
                "color": color_map.get(option, "#F5F5F5"),
                "order": i
            })
        return _success(columns=columns)
    except Exception as e:
        error_msg = f"Error getting kanban columns: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Columns Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_kanban_invoices(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
    """Get Sales Invoices organized by their state for Kanban display.
    
    Args:
        filters: Filter conditions for invoice selection
        
    Returns:
        Dict with success status and invoices organized by state
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_invoices called with filters: {0}".format(filters))
        filter_conditions = apply_invoice_filters(filters)
        filter_conditions["docstatus"] = 1
        filter_conditions["is_pos"] = 1
        
        # Fetch all matching Sales Invoices
        fields = [
            "name", "customer", "customer_name", "territory", "posting_date",
            "posting_time", "grand_total", "net_total", "total_taxes_and_charges",
            "status", "custom_sales_invoice_state", "sales_invoice_state", "required_delivery_datetime",
            "shipping_address_name", "customer_address"
        ]
        
        invoices = frappe.get_all(
            "Sales Invoice",
            filters=filter_conditions,
            fields=fields,
            order_by="posting_date desc, posting_time desc"
        )
        # Territory shipping cache
        territory_cache: Dict[str, Dict[str, float]] = {}
        def _get_territory_shipping(territory_name: str) -> Dict[str, float]:
            if not territory_name:
                return {"income": 0.0, "expense": 0.0}
            if territory_name in territory_cache:
                return territory_cache[territory_name]
            income = 0.0
            expense = 0.0
            try:
                terr = frappe.get_doc("Territory", territory_name)
                # Try multiple possible custom field names for robustness
                income_field_candidates = [
                    "shipping_income", "delivery_income", "courier_income", "shipping_income_amount"
                ]
                expense_field_candidates = [
                    "shipping_expense", "delivery_expense", "courier_expense", "shipping_expense_amount"
                ]
                for f in income_field_candidates:
                    if f in terr.as_dict():
                        try:
                            income = float(terr.get(f) or 0) ; break
                        except Exception:
                            pass
                for f in expense_field_candidates:
                    if f in terr.as_dict():
                        try:
                            expense = float(terr.get(f) or 0) ; break
                        except Exception:
                            pass
            except Exception:
                pass
            territory_cache[territory_name] = {"income": income, "expense": expense}
            return territory_cache[territory_name]
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
        
        # Get all possible states
        all_states = _get_state_field_options()
        # Initialize
        kanban_data = {}
        for state in all_states:
            state = state.strip()
            if state:
                kanban_data[_state_key(state)] = []
        
        # Organize invoices by their current state
        for inv in invoices:
            state = inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "Received"  # Default state
            state_key = state.lower().replace(' ', '_')
            terr_ship = _get_territory_shipping(inv.get("territory") or "")
            # Create invoice card data
            invoice_card = {
                "name": inv.name,
                "invoice_id_short": inv.name.split('-')[-1] if '-' in inv.name else inv.name,
                "customer_name": inv.customer_name or inv.customer,
                "customer": inv.customer,
                "territory": inv.territory or "",
                "required_delivery_date": inv.required_delivery_datetime,
                "status": state,
                "posting_date": str(inv.posting_date),
                "grand_total": float(inv.grand_total or 0),
                "net_total": float(inv.net_total or 0),
                "total_taxes_and_charges": float(inv.total_taxes_and_charges or 0),
                "full_address": invoice_addresses.get(inv.name, ""),
                "items": invoice_items.get(inv.name, []),
                "shipping_income": terr_ship.get("income", 0.0),
                "shipping_expense": terr_ship.get("expense", 0.0),
            }
            
            # Add to appropriate state column
            if state_key not in kanban_data:
                kanban_data[state_key] = []
            kanban_data[state_key].append(invoice_card)
        
        # Return unified success
        return _success(data=kanban_data)
    except Exception as e:
        error_msg = f"Error getting kanban invoices: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Invoices Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def update_invoice_state(invoice_id: str, new_state: str) -> Dict[str, Any]:
    """Update the custom_sales_invoice_state of a Sales Invoice (legacy field kept for backward compatibility).
    
    Args:
        invoice_id: ID of the Sales Invoice to update
        new_state: New state value to set
        
    Returns:
        Dict with success status and message
    """
    try:
        frappe.logger().debug(f"KANBAN API: update_invoice_state - Invoice: {invoice_id}, New state: {new_state}")
        allowed_states = _get_allowed_states()
        if not allowed_states:
            return _failure("No allowed states configured (Custom Field missing or empty)")
        if new_state not in allowed_states:
            match_ci = next((s for s in allowed_states if s.lower() == (new_state or '').lower()), None)
            if match_ci:
                new_state = match_ci
            else:
                return _failure(f"'{new_state}' is not a valid state")
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        if invoice.docstatus != 1:
            return _failure("Only submitted (docstatus=1) Sales Invoices can change state")
        old_state = invoice.get("custom_sales_invoice_state") or invoice.get("sales_invoice_state")
        if old_state == new_state:
            return _success(message="State unchanged (already set)", invoice_id=invoice_id, state=new_state)
        try:
            invoice.db_set("custom_sales_invoice_state", new_state, update_modified=True)
        except Exception:
            invoice.set("custom_sales_invoice_state", new_state)
            invoice.save(ignore_permissions=True, ignore_version=True)
        payload = {
            "invoice_id": invoice_id,
            "old_state": old_state,
            "new_state": new_state,
            "old_state_key": _state_key(old_state or "") if old_state else None,
            "new_state_key": _state_key(new_state),
            "updated_by": frappe.session.user,
            "timestamp": frappe.utils.now()
        }
        frappe.publish_realtime("jarz_pos_invoice_state_change", payload, user="*")
        frappe.publish_realtime("kanban_update", payload, user="*")
        return _success(message=f"Invoice {invoice_id} state updated", invoice_id=invoice_id, state=new_state)
    except Exception as e:
        error_msg = f"Error updating invoice state: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Update Invoice State Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_invoice_details(invoice_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific invoice.
    
    Args:
        invoice_id: ID of the Sales Invoice to retrieve
        
    Returns:
        Dict with success status and invoice details
    """
    try:
        frappe.logger().debug(f"KANBAN API: get_invoice_details - Invoice: {invoice_id}")
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        data = format_invoice_data(invoice)
        return _success(data=data)
    except Exception as e:
        error_msg = f"Error getting invoice details: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Invoice Details Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_kanban_filters() -> Dict[str, Any]:
    """Get available filter options for the Kanban board.
    
    Returns:
        Dict with success status and filter options
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_filters called")
        customers = frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1, "is_pos": 1},
            fields=["customer", "customer_name"],
            distinct=True,
            order_by="customer_name"
        )
        customer_options = [{"value": c.customer, "label": c.customer_name or c.customer} for c in customers]
        state_options = [{"value": s, "label": s} for s in _get_state_field_options()]
        return _success(customers=customer_options, states=state_options)
    except Exception as e:
        error_msg = f"Error getting kanban filters: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Filters Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

# ---------------------------------------------------------------------------
# Fallback explicit whitelist enforcement (in case of edge caching/import issues)
# ---------------------------------------------------------------------------
try:
    _kanban_funcs = [
        get_kanban_columns,
        get_kanban_invoices,
        update_invoice_state,
        get_invoice_details,
        get_kanban_filters,
    ]
    for _f in _kanban_funcs:
        if not getattr(_f, "is_whitelisted", False):
            frappe.logger().warning(f"KANBAN API: Forcing whitelist registration for {_f.__name__}")
            # Re-wrap with decorator (preserve allow_guest False)
            _wrapped = frappe.whitelist(allow_guest=False)(_f)
            globals()[_f.__name__] = _wrapped
except Exception:
    # Silent fail – we don't want import to abort
    pass

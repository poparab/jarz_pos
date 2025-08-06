"""Jarz POS - Utility functions for API endpoints.
This module provides common helper functions that are used across different API endpoints.
"""
from __future__ import annotations
import frappe
from typing import Dict, List, Any, Optional, Union


def get_address_details(address_name: str) -> str:
    """Get formatted address string from an Address document.
    
    Args:
        address_name: The name of the Address document
        
    Returns:
        A formatted address string with comma-separated components
    """
    full_address = ""
    if not address_name:
        return full_address
        
    try:
        address_doc = frappe.get_doc("Address", address_name)
        full_address = f"{address_doc.address_line1 or ''}"
        if address_doc.address_line2:
            full_address += f", {address_doc.address_line2}"
        if address_doc.city:
            full_address += f", {address_doc.city}"
        return full_address.strip(", ")
    except Exception as e:
        frappe.log_error(f"Error fetching address details: {str(e)}", "Address Utils")
        return ""


def format_invoice_data(invoice: frappe.Document) -> Dict[str, Any]:
    """Format a Sales Invoice document into a standardized dictionary format.
    
    Args:
        invoice: The Sales Invoice document
        
    Returns:
        Dictionary with standardized invoice data
    """
    # Get address information
    address_name = invoice.get("shipping_address_name") or invoice.get("customer_address")
    full_address = get_address_details(address_name)
    
    # Get items
    items = []
    for item in invoice.items:
        items.append({
            "item_code": item.item_code,
            "item_name": item.item_name,
            "qty": float(item.qty),
            "rate": float(item.rate),
            "amount": float(item.amount)
        })
    
    # Create formatted invoice data
    return {
        "name": invoice.name,
        "invoice_id_short": invoice.name.split('-')[-1] if '-' in invoice.name else invoice.name,
        "customer_name": invoice.customer_name or invoice.customer,
        "customer": invoice.customer,
        "territory": invoice.territory or "",
        "required_delivery_date": invoice.get("required_delivery_datetime"),
        "status": invoice.get("sales_invoice_state") or "Received",
        "posting_date": str(invoice.posting_date),
        "grand_total": float(invoice.grand_total or 0),
        "net_total": float(invoice.net_total or 0),
        "total_taxes_and_charges": float(invoice.total_taxes_and_charges or 0),
        "full_address": full_address,
        "items": items
    }


def apply_invoice_filters(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
    """Process and apply filters for Sales Invoice queries.
    
    Args:
        filters: Filter conditions as string (JSON) or dict
        
    Returns:
        Dictionary of filter conditions for frappe.get_all
    """
    # Base filters for POS invoices
    filter_conditions = {
        "docstatus": 1,  # Only submitted invoices
        "is_pos": 1      # Only POS invoices
    }
    
    if not filters:
        return filter_conditions
        
    # Convert JSON string to dict if needed
    if isinstance(filters, str):
        try:
            import json
            filters = json.loads(filters)
        except json.JSONDecodeError:
            frappe.log_error(f"Invalid JSON in filters: {filters}", "Filter Processing")
            return filter_conditions
    
    # Apply date filters
    if filters.get('dateFrom'):
        filter_conditions["posting_date"] = [">=", filters['dateFrom']]
        
    if filters.get('dateTo'):
        if "posting_date" in filter_conditions:
            filter_conditions["posting_date"] = ["between", [filters['dateFrom'], filters['dateTo']]]
        else:
            filter_conditions["posting_date"] = ["<=", filters['dateTo']]
            
    # Apply customer filter
    if filters.get('customer'):
        filter_conditions["customer"] = filters['customer']
        
    # Apply amount filters
    if filters.get('amountFrom'):
        filter_conditions["grand_total"] = [">=", filters['amountFrom']]
        
    if filters.get('amountTo'):
        if "grand_total" in filter_conditions:
            filter_conditions["grand_total"] = ["between", [filters['amountFrom'], filters['amountTo']]]
        else:
            filter_conditions["grand_total"] = ["<=", filters['amountTo']]
            
    return filter_conditions

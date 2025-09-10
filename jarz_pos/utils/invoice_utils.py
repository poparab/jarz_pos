"""Jarz POS - Utility functions for API endpoints.
This module provides common helper functions that are used across different API endpoints.
"""
from __future__ import annotations
import frappe
from typing import Dict, List, Any, Optional, Union


def set_invoice_fields(invoice_doc, customer_doc, pos_profile, delivery_datetime, logger):
    """Set basic fields on the Sales Invoice document."""
    logger.debug("Setting invoice fields...")
    print(f"   Setting basic invoice fields...")
    
    # Set basic invoice fields
    invoice_doc.customer = customer_doc.name
    invoice_doc.customer_name = customer_doc.customer_name
    invoice_doc.company = pos_profile.company
    invoice_doc.pos_profile = pos_profile.name
    invoice_doc.is_pos = 1
    invoice_doc.selling_price_list = pos_profile.selling_price_list
    invoice_doc.currency = pos_profile.currency
    invoice_doc.territory = customer_doc.territory or "All Territories"
    
    # Set delivery slot fields when delivery_datetime provided (new model)
    if delivery_datetime:
        try:
            dt = frappe.utils.get_datetime(delivery_datetime)
            invoice_doc.custom_delivery_date = dt.date()
            invoice_doc.custom_delivery_time_from = dt.time().strftime("%H:%M:%S")
            if not getattr(invoice_doc, "custom_delivery_duration", None):
                invoice_doc.custom_delivery_duration = 60
        except Exception:
            # Non-fatal: let hooks enforce completeness if partially provided later
            pass
    
    # Set posting date and time
    invoice_doc.posting_date = frappe.utils.today()
    invoice_doc.posting_time = frappe.utils.nowtime()
    
    logger.debug(f"Invoice fields set: customer={invoice_doc.customer}, company={invoice_doc.company}")
    print(f"   ✅ Basic fields set for customer: {invoice_doc.customer_name}")


def add_items_to_invoice(invoice_doc, processed_items, logger):
    """Add items to the Sales Invoice document following ERPNext discount logic.
    Key insight: Set price_list_rate and discount_percentage, let ERPNext compute rate and discount_amount.
    For 100% discount: ERPNext will set rate = 0.0 automatically.
    For partial discount: ERPNext will compute rate = price_list_rate * (1 - discount_percentage/100).
    """
    logger.debug(f"Adding {len(processed_items)} items to invoice (ERPNext native discount logic)...")
    print(f"   Adding {len(processed_items)} items to invoice...")

    discount_items = 0
    total_planned_discount = 0.0

    for i, item_data in enumerate(processed_items, 1):
        try:
            invoice_item = invoice_doc.append("items", {})
            invoice_item.item_code = item_data["item_code"]
            invoice_item.item_name = item_data.get("item_name", item_data["item_code"])
            invoice_item.qty = float(item_data.get("qty", 1))

            # CRITICAL: Set price_list_rate first (ERPNext needs this for discount calculations)
            if "price_list_rate" in item_data:
                invoice_item.price_list_rate = float(item_data["price_list_rate"])
                # Don't set rate - let ERPNext compute it from price_list_rate and discount_percentage
            elif "rate" in item_data:
                # Fallback: if no price_list_rate, use rate as both
                invoice_item.price_list_rate = float(item_data["rate"])
                # Still don't set rate - let ERPNext compute

            # CRITICAL: Set discount_percentage (ERPNext will compute rate and discount_amount)
            discount_pct = float(item_data.get("discount_percentage", 0) or 0)
            if discount_pct > 0:
                discount_items += 1
                invoice_item.discount_percentage = discount_pct
                # Estimate discount for logging
                price_list_rate = getattr(invoice_item, 'price_list_rate', 0) or 0
                estimated_discount = price_list_rate * invoice_item.qty * (discount_pct / 100.0)
                total_planned_discount += estimated_discount

            # Handle legacy discount_amount (convert to percentage if no percentage set)
            if discount_pct == 0 and "discount_amount" in item_data:
                discount_amt_per_unit = float(item_data["discount_amount"] or 0)
                if discount_amt_per_unit > 0:
                    price_list_rate = getattr(invoice_item, 'price_list_rate', 0) or 0
                    if price_list_rate > 0:
                        # Convert discount_amount to discount_percentage
                        computed_pct = (discount_amt_per_unit / price_list_rate) * 100.0
                        computed_pct = min(max(0.0, computed_pct), 100.0)
                        invoice_item.discount_percentage = computed_pct
                        discount_items += 1
                        total_planned_discount += discount_amt_per_unit * invoice_item.qty

            # Custom bundle flags (only set if fields exist)
            for flag_field in ["is_bundle_parent", "is_bundle_child", "bundle_code", "parent_bundle"]:
                if flag_field in item_data:
                    try:
                        setattr(invoice_item, flag_field, item_data[flag_field])
                    except Exception:
                        pass

            # UOM resolution
            if item_data.get("uom"):
                invoice_item.uom = item_data["uom"]
            else:
                try:
                    item_doc = frappe.get_doc("Item", item_data["item_code"])
                    invoice_item.uom = item_doc.stock_uom
                except Exception:
                    pass

            # Log what we set (rate will be computed by ERPNext)
            price_list_rate = getattr(invoice_item, 'price_list_rate', 0) or 0
            discount_pct = getattr(invoice_item, 'discount_percentage', 0) or 0
            print(f"      {i}. {invoice_item.item_name} x {invoice_item.qty} | price_list_rate={price_list_rate} | discount_pct={discount_pct}% (rate will be computed by ERPNext)")
            
        except Exception as e:
            error_msg = f"Error adding item {item_data.get('item_code','Unknown')}: {str(e)}"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            raise

    print(f"   ✅ All {len(processed_items)} items added successfully")
    if discount_items:
        print(f"   💸 Discount bearing lines: {discount_items}; Estimated total discount: {total_planned_discount}")
    else:
        print(f"   ℹ️ No discount lines detected in added items")


def add_delivery_charges_to_invoice(invoice_doc, delivery_charges, pos_profile, logger):
    """Add delivery charges to the Sales Invoice document.
    
    Note: Delivery charges are handled in the taxes section, not as items.
    This function is kept for compatibility but doesn't add delivery as items.
    """
    logger.debug(f"Processing {len(delivery_charges)} delivery charges...")
    print(f"   Processing {len(delivery_charges)} delivery charges...")
    
    if delivery_charges:
        # Just log the delivery charges - they're handled elsewhere in taxes
        total_delivery = sum(float(charge["amount"]) for charge in delivery_charges)
        
        logger.info(f"Delivery charges total: ${total_delivery:.2f} (handled in taxes section)")
        print(f"   📦 Delivery charges total: ${total_delivery:.2f}")
        print(f"   💡 Delivery charges are handled in taxes section, not as items")
        
        # Optionally add a note to the invoice remarks
        for i, charge in enumerate(delivery_charges, 1):
            charge_desc = charge.get("description", f"Delivery Charge - {charge.get('charge_type', 'Standard')}")
            charge_amount = float(charge["amount"])
            print(f"      {i}. {charge_desc}: ${charge_amount:.2f}")
            
    else:
        print(f"   📦 No delivery charges to process")
    
    print(f"   ✅ Delivery charges processing completed (handled in taxes)")


def verify_invoice_totals(invoice_doc, logger):
    """Verify that invoice totals are calculated correctly."""
    logger.debug("Verifying invoice totals...")
    print(f"   Verifying invoice totals...")
    
    try:
        # Calculate expected totals
        expected_net_total = sum(item.amount for item in invoice_doc.items)
        
        # Basic validation
        if abs(float(invoice_doc.net_total) - expected_net_total) > 0.01:
            error_msg = f"Net total mismatch: Expected {expected_net_total}, Got {invoice_doc.net_total}"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)
        
        logger.debug(f"Invoice totals verified: net_total={invoice_doc.net_total}, grand_total={invoice_doc.grand_total}")
        print(f"   ✅ Totals verified: Net=${invoice_doc.net_total}, Grand=${invoice_doc.grand_total}")
        
    except Exception as e:
        error_msg = f"Error verifying invoice totals: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        raise


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
    data = {
        "name": invoice.name,
        "invoice_id_short": invoice.name.split('-')[-1] if '-' in invoice.name else invoice.name,
        "customer_name": invoice.customer_name or invoice.customer,
        "customer": invoice.customer,
        "territory": invoice.territory or "",
    # New delivery slot fields
        "delivery_date": invoice.get("custom_delivery_date"),
        "delivery_time_from": invoice.get("custom_delivery_time_from"),
        "delivery_duration": invoice.get("custom_delivery_duration"),
    "delivery_slot_label": invoice.get("custom_delivery_slot_label"),
        "status": invoice.get("sales_invoice_state") or "Received",
        "posting_date": str(invoice.posting_date),
        "grand_total": float(invoice.grand_total or 0),
        "net_total": float(invoice.net_total or 0),
        "total_taxes_and_charges": float(invoice.total_taxes_and_charges or 0),
        "full_address": full_address,
        "items": items
    }
    return data


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

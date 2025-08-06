"""
Bundle Processing Module for Jarz POS

This module handles all bundle-related processing for POS invoices,
including bundle validation, child item calculation, and discount distribution.
"""

import frappe
from .discount_calculation import (
    create_main_bundle_item_with_discount,
    create_child_bundle_items_with_discounts,
    verify_bundle_discount_totals
)


def process_bundle_item(bundle_id, bundle_qty, bundle_price, selling_price_list):
    """
    Process bundle item for invoice creation.
    Args:
        bundle_id (str): The Jarz Bundle ID (e.g., 'lcp1b13gg0')
        bundle_qty (float): Quantity of bundles ordered
        bundle_price (float): Price per bundle
        selling_price_list (str): Price list to use
    Returns:
        list: List of invoice items (main bundle item + child items with discounts)  
    Logic:
    1. Add main bundle item (erpnext_item) with full discount_amount (making it free)
    2. Add child items with partial discounts so their total equals the bundle price 
    3. Result: Customer pays only the bundle price, distributed across child items   
    """
    print(f"\n🎁 PROCESSING BUNDLE ID: {bundle_id}")
    processed_items = []
    
    try:
        # Get the bundle document by name/ID
        if not frappe.db.exists("Jarz Bundle", bundle_id):
            # Not a bundle - this is an error case, should never happen
            print(f"      ❌ ERROR: Bundle ID {bundle_id} does not exist in Jarz Bundle")
            frappe.throw(f"Bundle ID '{bundle_id}' does not exist in Jarz Bundle. Cannot proceed with invoice creation.")
        
        # Get the bundle document
        bundle_doc = frappe.get_doc("Jarz Bundle", bundle_id)
        if not bundle_doc.erpnext_item:
            frappe.throw(f"Bundle {bundle_doc.bundle_name} has no ERPNext item configured")
        
        print(f"      ✅ Bundle found: {bundle_doc.bundle_name}")
        print(f"         - Bundle Price: {bundle_doc.bundle_price}")
        print(f"         - Main ERPNext Item: {bundle_doc.erpnext_item}")
        
        # Validate that the ERPNext item exists
        if not frappe.db.exists("Item", bundle_doc.erpnext_item):
            frappe.throw(f"ERPNext item '{bundle_doc.erpnext_item}' referenced by bundle does not exist")
        
        # Get bundle child items and process them
        child_items_data = _get_bundle_child_items(bundle_doc, bundle_qty, selling_price_list)
        
        # Add main bundle item with full discount
        main_item = create_main_bundle_item_with_discount(bundle_doc, bundle_qty, bundle_price)
        processed_items.append(main_item)
        
        # Add child items with proportional discounts
        child_items = create_child_bundle_items_with_discounts(child_items_data, bundle_qty, bundle_price)
        processed_items.extend(child_items)
        
        # Verify totals
        verify_bundle_discount_totals(processed_items, bundle_qty, bundle_price)
        
        return processed_items
        
    except Exception as e:
        error_msg = f"Error processing bundle {bundle_id}: {str(e)}"
        print(f"         ❌ {error_msg}")
        frappe.throw(error_msg)


def _get_bundle_child_items(bundle_doc, bundle_qty, selling_price_list):
    """Get and validate bundle child items."""
    # Get bundle child items (item groups) - use the bundle document's items field
    bundle_items = bundle_doc.items or []
    print(f"         - Bundle items from document: {len(bundle_items)} items")   
    
    for i, item in enumerate(bundle_items):
        print(f"           {i+1}. Item Group: {item.item_group}, Quantity: {item.quantity}")
    
    # Also try the database query as fallback
    if not bundle_items:
        print(f"         - No items in document, trying database query...")      
        bundle_items = frappe.get_all(
            "Jarz Bundle Item Group",
            filters={"parent": bundle_doc.name},
            fields=["item_group", "quantity"]
        )
        print(f"         - Bundle items query result: {bundle_items}")
    
    if not bundle_items:
        print(f"         - ⚠️ No bundle items found for bundle: {bundle_doc.name}")
        print(f"         - Checking if any Jarz Bundle Item Group records exist...")
        # Debug: Check if any bundle item groups exist at all
        all_bundle_groups = frappe.get_all(
            "Jarz Bundle Item Group",
            fields=["parent", "item_group", "quantity"],
            limit=5
        )
        print(f"         - Sample Jarz Bundle Item Group records: {all_bundle_groups}")
        frappe.throw(f"Bundle {bundle_doc.bundle_name} has no items configured") 
    
    print(f"         - Child item groups: {len(bundle_items)}")
    
    # Calculate total expected price for all child items
    child_items_data = []
    total_child_value = 0
    
    for bundle_item in bundle_items:
        item_group = bundle_item.item_group
        required_qty = bundle_item.quantity * bundle_qty  # Multiply by bundle quantity
        print(f"            - Processing item group: {item_group} (qty: {required_qty})")
        
        # Get items from this item group
        group_items = _get_items_from_group(item_group)
        
        if not group_items:
            frappe.throw(f"No available items found in item group '{item_group}'")
        
        selected_item = group_items[0]
        
        # Get item price from price list
        item_price = _get_item_price_from_list(selected_item.item_code, selling_price_list)
        item_total = item_price * required_qty
        total_child_value += item_total
        
        child_items_data.append({
            "item_code": selected_item.item_code,
            "item_name": selected_item.item_name,
            "qty": required_qty,
            "regular_rate": item_price,
            "regular_total": item_total,
            "uom": selected_item.stock_uom
        })
        
        print(f"               - Selected: {selected_item.item_code} @ {item_price} each = {item_total}")
    
    print(f"         - Total child items value: {total_child_value}")
    print(f"         - Number of child items found: {len(child_items_data)}")    
    
    # Debug: Print all child items details
    for i, child_item in enumerate(child_items_data, 1):
        print(f"         - Child {i}: {child_item['item_code']} (qty: {child_item['qty']}, rate: {child_item['regular_rate']}, total: {child_item['regular_total']})")
    
    return child_items_data


def _get_items_from_group(item_group):
    """Get available items from an item group."""
    group_items = frappe.get_all(
        "Item",
        filters={
            "item_group": item_group,
            "disabled": 0,
            "has_variants": 0
        },
        fields=["item_code", "item_name", "stock_uom"],
        limit=1  # Take first available item for now
    )
    
    print(f"               - Items found in group '{item_group}': {len(group_items)}")
    
    if group_items:
        print(f"               - First item: {group_items[0]}")
    else:
        print(f"               - ⚠️ No items found, checking alternative filters...")
        # Try without the has_variants filter
        group_items_alt = frappe.get_all(
            "Item",
            filters={
                "item_group": item_group,
                "disabled": 0
            },
            fields=["item_code", "item_name", "stock_uom"],
            limit=5
        )
        print(f"               - Items without has_variants filter: {len(group_items_alt)}")
        
        if group_items_alt:
            group_items = [group_items_alt[0]]  # Take the first one
            print(f"               - Using: {group_items[0]}")
        else:
            print(f"               - ❌ No items found even with alternative filters")
    
    return group_items


def _get_item_price_from_list(item_code, price_list):
    """Get item price from price list with fallback."""
    item_price = frappe.get_value(
        "Item Price",
        {
            "item_code": item_code,
            "price_list": price_list
        },
        "price_list_rate"
    ) or 0
    
    if item_price == 0:
        print(f"               ⚠️ No price found for {item_code}, using 1")
        item_price = 1  # Fallback price
    
    return item_price

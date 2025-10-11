"""
Discount Calculation Module for Jarz POS

This module handles all discount-related calculations for bundle items
and proportional discount distribution across child items.
"""

import frappe


def calculate_bundle_discounts(child_items_data, bundle_qty, bundle_price):
    """
    Calculate proportional discounts for bundle child items.

    Args:
        child_items_data: List of child items with their regular pricing
        bundle_qty: Quantity of bundles
        bundle_price: Target price per bundle

    Returns:
        tuple: (total_child_value, target_total, discount_per_item)
    """
    total_child_value = sum(item["regular_total"] for item in child_items_data)
    target_total_all_children = bundle_price * bundle_qty

    print("         - Child items processing:")
    print(f"           Original total value: ${total_child_value}")
    print(f"           Target total value: ${target_total_all_children}")
    print(f"           Required discount: ${total_child_value - target_total_all_children}")

    return total_child_value, target_total_all_children


def calculate_proportional_discount(child_item, total_child_value, target_total):
    """
    Calculate proportional discount for a single child item.

    Args:
        child_item: Dictionary containing child item data
        total_child_value: Total value of all child items before discount
        target_total: Target total value after discount

    Returns:
        float: Discount amount for this item
    """
    child_rate = float(child_item["regular_rate"])
    child_qty = float(child_item["qty"])
    child_original_total = child_rate * child_qty

    # Calculate proportional discount for each child item
    # Formula: (child_original_total / total_original) * total_discount_needed
    total_discount_needed = total_child_value - target_total
    child_discount_amount = (child_original_total / total_child_value) * total_discount_needed

    # Ensure discount doesn't exceed the item total
    child_discount_amount = min(child_discount_amount, child_original_total)
    child_discount_amount = max(0, child_discount_amount)  # No negative discounts

    return round(child_discount_amount, 2)


def calculate_item_rates_with_discount(original_rate, discount_amount, qty):
    """
    Calculate final rate and price list rate for ERPNext compliance.

    Args:
        original_rate: Original item rate before discount
        discount_amount: Total discount amount for the item
        qty: Item quantity

    Returns:
        tuple: (final_rate, price_list_rate, discount_type)
    """
    if discount_amount > 0:
        # ERPNext way: Set price_list_rate to original, rate to discounted rate
        discount_per_unit = discount_amount / qty
        discounted_rate = original_rate - discount_per_unit

        # Safety check: ensure rate doesn't go below zero (for 100% discounts)
        if discounted_rate < 0:
            discounted_rate = 0.0  # 100% discount case

        price_list_rate = original_rate
        final_rate = discounted_rate

        # Determine discount type
        if discount_per_unit >= original_rate:
            discount_type = "100%"
            print("      🎯 100% Bundle discount detected (Main Bundle Item):")
            print(f"         Original rate: ${original_rate}")
            print(f"         Discount amount: ${discount_amount}")
            print(f"         Qty: {qty}")
            print(f"         Discount per unit: ${discount_per_unit}")
            print(f"         Final rate: ${final_rate} (100% discount applied)")
            print(f"         Price list rate: ${price_list_rate}")
        else:
            discount_type = "partial"
            print("      🎯 Partial Bundle discount calculation:")
            print(f"         Original rate: ${original_rate}")
            print(f"         Discount amount: ${discount_amount}")
            print(f"         Qty: {qty}")
            print(f"         Discount per unit: ${discount_per_unit}")
            print(f"         Final discounted rate: ${final_rate}")
            print(f"         Price list rate: ${price_list_rate}")
    else:
        # Regular item without discount
        final_rate = original_rate
        price_list_rate = original_rate
        discount_type = "none"

    return final_rate, price_list_rate, discount_type


def calculate_discount_percentage(discount_amount, original_rate, qty):
    """
    Calculate discount percentage for ERPNext.

    Args:
        discount_amount: Total discount amount
        original_rate: Original rate per unit
        qty: Item quantity

    Returns:
        float: Discount percentage (0-100)
    """
    if discount_amount > 0 and original_rate > 0:
        discount_percentage = (discount_amount / (original_rate * qty)) * 100
        return min(discount_percentage, 100)  # Cap at 100%
    return 0


def apply_main_bundle_discount(item_row, discount_amount, original_rate, qty):
    """
    Apply special discount handling for main bundle items.

    Args:
        item_row: Item row dictionary to modify
        discount_amount: Discount amount to apply
        original_rate: Original rate per unit
        qty: Item quantity

    Returns:
        dict: Modified item row with discount settings
    """
    discount_percentage = calculate_discount_percentage(discount_amount, original_rate, qty)

    if discount_percentage >= 99.9:  # Essentially 100%
        item_row["discount_percentage"] = 100
        print("      🎯 Setting discount_percentage = 100% for main bundle item")

    return item_row


def create_main_bundle_item_with_discount(bundle_doc, bundle_qty, bundle_price):
    """
    Create main bundle item with full discount (makes it free).

    Args:
        bundle_doc: Bundle document
        bundle_qty: Quantity of bundles
        bundle_price: Price per bundle

    Returns:
        dict: Main bundle item dictionary
    """
    main_item_rate = float(bundle_doc.bundle_price)
    main_item_qty = float(bundle_qty)
    main_item_total = main_item_rate * main_item_qty
    main_item_discount = main_item_total  # 100% discount = full amount

    main_item = {
        "item_code": bundle_doc.erpnext_item,  # CRITICAL: Use the ERPNext item, not the bundle ID
        "qty": main_item_qty,
        "rate": main_item_rate,  # We'll adjust this to discounted rate during item addition
        "uom": frappe.get_value("Item", bundle_doc.erpnext_item, "stock_uom"),
        "discount_amount": main_item_discount,  # Full discount amount = rate * qty
        "is_bundle_item": True,
        "bundle_type": "main"
    }

    print(f"         - Main item added: {bundle_doc.erpnext_item} (from Bundle ID: {bundle_doc.name})")
    print(f"           Original Rate: ${main_item_rate}, Qty: {main_item_qty}, Total: ${main_item_total}")
    print(f"           Discount Amount: ${main_item_discount} (100% - makes item FREE)")
    print("           Final Rate: $0.00 (100% discount applied)")
    print(f"           Final Amount: ${main_item_total - main_item_discount} (should be $0.00)")

    return main_item


def create_child_bundle_items_with_discounts(child_items_data, bundle_qty, bundle_price):
    """
    Create child bundle items with proportional discounts.

    Args:
        child_items_data: List of child items data
        bundle_qty: Bundle quantity
        bundle_price: Bundle price

    Returns:
        list: List of child item dictionaries with discounts
    """
    child_items = []

    if not child_items_data:
        return _create_fallback_child_item(bundle_qty, bundle_price)

    # Calculate total values and required discounts
    total_child_value, target_total_all_children = calculate_bundle_discounts(
        child_items_data, bundle_qty, bundle_price
    )

    # Add child items with proportional discounts to match bundle price
    for i, child_item in enumerate(child_items_data, 1):
        child_discount_amount = calculate_proportional_discount(
            child_item, total_child_value, target_total_all_children
        )

        child_rate = float(child_item["regular_rate"])
        child_qty = float(child_item["qty"])
        child_original_total = child_rate * child_qty
        child_final_amount = child_original_total - child_discount_amount

        child_item_dict = {
            "item_code": child_item["item_code"],
            "qty": child_qty,
            "rate": child_rate,
            "uom": child_item["uom"],
            "discount_amount": child_discount_amount,  # Discount amount, not percentage
            "is_bundle_item": True,
            "bundle_type": "child"
        }

        child_items.append(child_item_dict)

        print(f"            - Child item {i}: {child_item['item_code']}")
        print(f"              Rate: ${child_rate}, Qty: {child_qty}, Original: ${child_original_total}")
        print(f"              Discount Amount: ${child_discount_amount:.2f}")
        print(f"              Final Amount: ${child_final_amount:.2f}")
        print(f"              Added to processed_items: {child_item_dict}")

    return child_items


def _create_fallback_child_item(bundle_qty, bundle_price):
    """Create fallback child item when no child items are configured."""
    print("         - No child items found in bundle configuration")

    # This ensures the invoice always shows something beyond just the main item
    fallback_child_rate = bundle_price  # Use full bundle price for fallback
    fallback_child_qty = bundle_qty
    fallback_child_total = fallback_child_rate * fallback_child_qty

    # No discount on fallback item since main item already has full discount
    fallback_item = {
        "item_code": "BUNDLE-FALLBACK",  # Generic fallback item
        "qty": fallback_child_qty,
        "rate": fallback_child_rate,
        "uom": "Nos",
        "discount_amount": 0.0,  # No discount on fallback item
        "is_bundle_item": True,
        "bundle_type": "child"
    }

    print(f"         - Added fallback child item: {fallback_item}")
    print(f"           Rate: ${fallback_child_rate}, Qty: {fallback_child_qty}, Total: ${fallback_child_total}")
    print("           No discount applied to fallback item")

    return [fallback_item]


def verify_bundle_discount_totals(processed_items, bundle_qty, bundle_price):
    """
    Verify that bundle discount calculations are correct.

    Args:
        processed_items: List of all processed items
        bundle_qty: Bundle quantity
        bundle_price: Bundle price

    Returns:
        dict: Verification summary
    """
    # Calculate totals for verification
    main_item_total_original = sum([
        item["qty"] * item["rate"]
        for item in processed_items if item.get("bundle_type") == "main"
    ])

    main_item_total_discount = sum([
        item.get("discount_amount", 0)
        for item in processed_items if item.get("bundle_type") == "main"
    ])

    main_item_total_final = main_item_total_original - main_item_total_discount

    child_item_total_original = sum([
        item["qty"] * item["rate"]
        for item in processed_items if item.get("bundle_type") == "child"
    ])

    child_item_total_discount = sum([
        item.get("discount_amount", 0)
        for item in processed_items if item.get("bundle_type") == "child"
    ])

    child_item_total_final = child_item_total_original - child_item_total_discount

    # Count items by type
    main_items_count = len([item for item in processed_items if item.get("bundle_type") == "main"])
    child_items_count = len([item for item in processed_items if item.get("bundle_type") == "child"])

    # Expected total should equal bundle price
    expected_total = bundle_price * bundle_qty
    actual_total = main_item_total_final + child_item_total_final

    print("         ✅ Bundle processing complete:")
    print(f"            - Total processed items: {len(processed_items)}")
    print(f"            - Main items: {main_items_count}")
    print(f"            - Child items: {child_items_count}")
    print(f"            - Main items: Original ${main_item_total_original:.2f}, Discount ${main_item_total_discount:.2f}, Final ${main_item_total_final:.2f}")
    print(f"            - Child items: Original ${child_item_total_original:.2f}, Discount ${child_item_total_discount:.2f}, Final ${child_item_total_final:.2f}")
    print(f"            - Expected total (bundle price): ${expected_total:.2f}")
    print(f"            - Actual total (after discounts): ${actual_total:.2f}")
    print(f"            - Match verification: {'✅ Perfect Match' if abs(actual_total - expected_total) < 0.01 else f'❌ Difference: ${abs(actual_total - expected_total):.2f}'}")

    # Debug: Print all processed items with detailed discount info
    print("         📋 All processed items with discount details:")
    for i, item in enumerate(processed_items, 1):
        item_type = item.get("bundle_type", "unknown")
        rate = item["rate"]
        qty = item["qty"]
        original_total = rate * qty
        discount = item.get("discount_amount", 0)
        final_total = original_total - discount
        discount_percentage = (discount / original_total * 100) if original_total > 0 else 0

        print(f"            {i}. {item['item_code']} ({item_type})")
        print(f"               Rate: ${rate}, Qty: {qty}, Original: ${original_total:.2f}")
        print(f"               Discount Amount: ${discount:.2f} ({discount_percentage:.1f}%)")
        print(f"               Final Amount: ${final_total:.2f}")

    return {
        "main_items_count": main_items_count,
        "child_items_count": child_items_count,
        "expected_total": expected_total,
        "actual_total": actual_total,
        "total_discount": main_item_total_discount + child_item_total_discount,
        "match_within_tolerance": abs(actual_total - expected_total) < 0.01
    }

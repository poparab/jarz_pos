"""
Validation Utilities for Jarz POS

This module contains all validation functions for POS invoice creation,
including cart data validation, customer validation, and POS profile validation.
"""

import frappe
from dateutil import parser
from frappe import _dict


def validate_cart_data(cart_json, logger):
    """Validate and parse cart JSON data."""
    # Validate required parameters
    if not cart_json:
        error_msg = "Cart data is required"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)

    # Parse cart JSON using Frappe best practice
    try:
        cart_items = frappe.parse_json(cart_json) if isinstance(cart_json, str) else cart_json
    except (ValueError, TypeError) as e:
        error_msg = f"Invalid cart JSON format: {e!s}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)

    # Handle responses where cart_json may be nested inside a dict (e.g., {"cart": [...]})
    if isinstance(cart_items, dict):
        if cart_items.get("cart"):
            cart_items = cart_items.get("cart")
        else:
            cart_items = [cart_items]

    # Some clients double-encode individual items; normalize everything into a list of dicts
    if isinstance(cart_items, (str, bytes)):
        try:
            cart_items = frappe.parse_json(cart_items)
        except Exception as e:
            error_msg = f"Cart data must be a JSON list: {e!s}"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)

    if not isinstance(cart_items, (list, tuple)):
        error_msg = "Cart data must be a list of items"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)

    normalized_items = []
    for idx, raw_item in enumerate(cart_items, 1):
        item = raw_item
        if isinstance(item, (str, bytes)):
            try:
                item = frappe.parse_json(item)
            except Exception as parse_error:
                error_msg = f"Invalid cart line at position {idx}: {parse_error}"
                logger.error(error_msg)
                print(f"   ❌ {error_msg}")
                frappe.throw(error_msg)
        elif hasattr(item, "as_dict"):
            item = item.as_dict()
        elif not isinstance(item, dict):
            try:
                item = dict(item)
            except Exception:
                error_msg = f"Cart line at position {idx} is not a valid item structure"
                logger.error(error_msg)
                print(f"   ❌ {error_msg}")
                frappe.throw(error_msg)

        normalized_items.append(_dict(item))

    cart_items = normalized_items
    logger.debug(f"Parsed cart: {len(cart_items)} items")
    print(f"   ✅ Cart parsed: {len(cart_items)} items")

    # Filter out shipping items - shipping should be handled separately, not as cart items
    original_count = len(cart_items)
    cart_items = [item for item in cart_items if item.get('item_code', '').upper() not in ['SHIPPING', 'DELIVERY', 'SHIPPING_FEE']]

    if len(cart_items) < original_count:
        shipping_count = original_count - len(cart_items)
        logger.info(f"Filtered out {shipping_count} shipping item(s) from cart")
        print(f"   🚚 Filtered out {shipping_count} shipping item(s) - shipping should be handled separately")
        print(f"   ✅ Remaining cart items: {len(cart_items)}")

    if not cart_items:
        error_msg = "Cart cannot be empty (after filtering out shipping items)"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)

    return cart_items


def validate_customer(customer_name, logger):
    """Validate customer exists and get customer document."""
    if not customer_name:
        error_msg = "Customer name is required"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)

    logger.debug(f"Validating customer: {customer_name}")

    # Frappe best practice: Use frappe.db.exists() for existence checks
    if not frappe.db.exists("Customer", customer_name):
        error_msg = f"Customer '{customer_name}' does not exist. Please create the customer first."
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)

    # Get customer document for validation
    try:
        customer_doc = frappe.get_doc("Customer", customer_name)
        logger.debug(f"Customer loaded: {customer_doc.customer_name}")
        print(f"   ✅ Customer validated: {customer_doc.customer_name}")
        print(f"      - Group: {customer_doc.customer_group}")
        print(f"      - Territory: {customer_doc.territory}")
        return customer_doc
    except Exception as e:
        error_msg = f"Error loading customer '{customer_name}': {e!s}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def validate_pos_profile(pos_profile_name, logger):
    """Validate POS profile and get profile document."""
    if pos_profile_name:
        logger.debug(f"Validating provided POS Profile: {pos_profile_name}")
        print(f"   Checking provided POS Profile: {pos_profile_name}")

        if not frappe.db.exists("POS Profile", pos_profile_name):
            error_msg = f"POS Profile '{pos_profile_name}' does not exist"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)

        try:
            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
            logger.debug(f"POS Profile loaded: {pos_profile.name}")
        except Exception as e:
            error_msg = f"Error loading POS Profile '{pos_profile_name}': {e!s}"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)
    else:
        logger.debug("Finding default POS Profile")
        print("   Finding default POS Profile...")

        # Frappe best practice: Use filters dict for complex queries
        pos_profile_name = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
        if not pos_profile_name:
            error_msg = "No active POS Profile found. Please create and enable a POS Profile."
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)

        try:
            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
            logger.debug(f"Default POS Profile: {pos_profile.name}")
            print(f"   Using default POS Profile: {pos_profile.name}")
        except Exception as e:
            error_msg = f"Error loading default POS Profile: {e!s}"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)

    # Validate POS Profile has required fields
    if not pos_profile.company:
        error_msg = f"POS Profile '{pos_profile.name}' has no company set"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)

    print("   ✅ POS Profile validated:")
    print(f"      - Name: {pos_profile.name}")
    print(f"      - Company: {pos_profile.company}")
    print(f"      - Price List: {pos_profile.selling_price_list}")
    print(f"      - Currency: {pos_profile.currency}")

    return pos_profile


def validate_delivery_datetime(required_delivery_datetime, logger):
    """Parse and validate delivery datetime."""
    delivery_datetime = None

    if required_delivery_datetime:
        try:
            if isinstance(required_delivery_datetime, str):
                # Parse ISO datetime string
                delivery_datetime = parser.parse(required_delivery_datetime)
                logger.debug(f"Parsed delivery datetime: {delivery_datetime}")
                print(f"   🕐 Delivery datetime parsed: {delivery_datetime}")
            else:
                delivery_datetime = required_delivery_datetime
                print(f"   🕐 Delivery datetime provided: {delivery_datetime}")

            # Validate that delivery datetime is in the future
            current_datetime = frappe.utils.now_datetime()
            if delivery_datetime <= current_datetime:
                error_msg = f"Delivery datetime must be in the future. Provided: {delivery_datetime}, Current: {current_datetime}"
                logger.error(error_msg)
                print(f"   ❌ {error_msg}")
                frappe.throw(error_msg)

            print(f"   ✅ Delivery datetime validated: {delivery_datetime}")

        except Exception as e:
            error_msg = f"Invalid delivery datetime format: {e!s}"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)
    else:
        print("   🕐 No delivery datetime provided")

    return delivery_datetime

"""
Invoice Utilities for Jarz POS

This module contains utility functions for invoice document manipulation,
including field setting, item addition, delivery charges, and verification.
"""

import frappe


def set_invoice_fields(invoice_doc, customer_doc, pos_profile, delivery_datetime, logger):
    """Set all document fields following ERPNext field order."""
    logger.debug("Setting document fields")
    try:
        # Basic identification fields first
        invoice_doc.naming_series = "ACC-SINV-.YYYY.-"
        invoice_doc.customer = customer_doc.name
        invoice_doc.company = pos_profile.company
        
        # Date and time fields
        invoice_doc.posting_date = frappe.utils.nowdate()
        invoice_doc.posting_time = frappe.utils.nowtime()
        invoice_doc.set_posting_time = 1
        
        # POS specific fields
        invoice_doc.is_pos = 1
        invoice_doc.pos_profile = pos_profile.name
        
        # Price and currency fields
        invoice_doc.selling_price_list = pos_profile.selling_price_list
        invoice_doc.currency = pos_profile.currency
        invoice_doc.price_list_currency = pos_profile.currency
        
        # Customer related fields
        invoice_doc.territory = customer_doc.territory
        invoice_doc.customer_group = customer_doc.customer_group
        
        # Delivery datetime field (custom field for delivery time slot)
        if delivery_datetime:
            _set_delivery_datetime_field(invoice_doc, delivery_datetime, logger)
        
        logger.debug("Basic fields set successfully")
        print(f"   ✅ Basic fields set")
        print(f"      - Customer: {invoice_doc.customer}")
        print(f"      - Company: {invoice_doc.company}")
        print(f"      - POS Profile: {invoice_doc.pos_profile}")
        
    except Exception as e:
        error_msg = f"Error setting document fields: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _set_delivery_datetime_field(invoice_doc, delivery_datetime, logger):
    """Set delivery datetime field with multiple fallback methods."""
    try:
        # First, try to set as a standard field if it exists
        if hasattr(invoice_doc, 'delivery_date'):
            invoice_doc.delivery_date = delivery_datetime.date()
            print(f"      - Delivery date: {invoice_doc.delivery_date}")

        # Set delivery time in remarks or custom field
        delivery_info = f"Delivery scheduled for: {delivery_datetime.strftime('%A, %B %d, %Y at %I:%M %p')}"
        # Add to remarks if remarks field exists
        if hasattr(invoice_doc, 'remarks'):
            existing_remarks = invoice_doc.remarks or ""
            if existing_remarks:
                invoice_doc.remarks = f"{existing_remarks}\n{delivery_info}"
            else:
                invoice_doc.remarks = delivery_info
            print(f"      - Delivery info added to remarks")
        
        # Set the exact custom field name for delivery datetime
        field_name = 'required_delivery_datetime'
        field_set_successfully = False
        print(f"      - 🎯 Setting delivery datetime field: {field_name}")
        
        try:
            # Method 1: Try using direct attribute assignment
            if hasattr(invoice_doc, field_name):
                setattr(invoice_doc, field_name, delivery_datetime)
                print(f"      - ✅ Set {field_name} via hasattr/setattr: {delivery_datetime}")
                field_set_successfully = True
            else:
                # Method 2: Try using frappe's set method (works with custom fields)
                invoice_doc.set(field_name, delivery_datetime)
                print(f"      - ✅ Set {field_name} via set(): {delivery_datetime}")
                field_set_successfully = True
            
            # Verify the field was set correctly
            field_value = getattr(invoice_doc, field_name, None) or invoice_doc.get(field_name)
            print(f"      - 🔍 Verification: {field_name} = {field_value}")
            
        except Exception as field_error:
            print(f"      - ❌ Failed to set {field_name}: {str(field_error)}")
            # Method 3: Try alternative field setting approaches
            try:
                # Force set using the db_set method (bypasses validation)
                invoice_doc.db_set(field_name, delivery_datetime, update_modified=False)
                print(f"      - ✅ Set {field_name} via db_set(): {delivery_datetime}")
                field_set_successfully = True
            except Exception as db_error:
                print(f"      - ❌ db_set also failed: {str(db_error)}")
                # Method 4: Try setting via the document's __dict__
                try:
                    invoice_doc.__dict__[field_name] = delivery_datetime
                    print(f"      - ✅ Set {field_name} via __dict__: {delivery_datetime}")
                    field_set_successfully = True
                except Exception as dict_error:
                    print(f"      - ❌ __dict__ assignment failed: {str(dict_error)}")
        
        if not field_set_successfully:
            print(f"      - ⚠️ Could not set any delivery datetime field")
            # As a fallback, try to get all available fields and find delivery-related ones
            _try_fallback_delivery_fields(invoice_doc, delivery_datetime)
        
        logger.debug(f"Delivery datetime field set: {field_set_successfully}")
        print(f"      - Delivery datetime field status: {'✅ Success' if field_set_successfully else '❌ Failed'}")
        
    except Exception as e:
        logger.warning(f"Could not set delivery datetime: {str(e)}")
        print(f"      ⚠️ Warning: Could not set delivery datetime: {str(e)}")


def _try_fallback_delivery_fields(invoice_doc, delivery_datetime):
    """Try to find and set delivery-related fields as fallback."""
    try:
        meta = frappe.get_meta("Sales Invoice")
        all_fields = [field.fieldname for field in meta.fields]
        delivery_fields = [f for f in all_fields if 'delivery' in f.lower() or 'required' in f.lower()]
        print(f"      - 📋 Available delivery-related fields: {delivery_fields}")
        
        # Try the first delivery-related field found
        if delivery_fields:
            first_delivery_field = delivery_fields[0]
            try:
                invoice_doc.set(first_delivery_field, delivery_datetime)
                print(f"      - ✅ Set fallback field {first_delivery_field}: {delivery_datetime}")
                return True
            except Exception as fallback_error:
                print(f"      - ❌ Fallback field {first_delivery_field} failed: {str(fallback_error)}")
        
    except Exception as meta_error:
        print(f"      - ❌ Could not get meta fields: {str(meta_error)}")
    
    return False


def add_items_to_invoice(invoice_doc, processed_items, logger):
    """Add all processed items to the invoice document."""
    logger.debug(f"Adding {len(processed_items)} items")
    
    # Debug: Check what items we have in processed_items
    print(f"   🔍 DETAILED PROCESSED_ITEMS INSPECTION:")
    for i, item_data in enumerate(processed_items, 1):
        print(f"      Item {i}: {item_data}")
    
    try:
        bundle_items_count = 0
        regular_items_count = 0
        main_bundle_items_count = 0
        child_bundle_items_count = 0
        
        for i, item_data in enumerate(processed_items, 1):
            # Check if this is a bundle item (has discount_amount)
            is_bundle_item = item_data.get("is_bundle_item", False)
            discount_amount = float(item_data.get("discount_amount", 0))
            bundle_type = item_data.get("bundle_type", "")
            original_rate = float(item_data["rate"])
            
            # Calculate final rate with discount
            final_rate, price_list_rate = _calculate_item_rates(original_rate, discount_amount, float(item_data["qty"]))
            
            # Create item row with ERPNext-compliant discount structure
            item_row = _create_item_row(item_data, final_rate, price_list_rate)
            
            # Special handling for 100% discounts (main bundle items)
            if discount_amount > 0 and bundle_type == "main":
                _handle_main_bundle_discount(item_row, discount_amount, original_rate, float(item_data["qty"]))
            
            # Track and log bundle vs regular items
            _track_and_log_item(item_data, final_rate, original_rate, discount_amount, is_bundle_item, bundle_type)
            
            # Update counters
            if is_bundle_item:
                bundle_items_count += 1
                if bundle_type == "main":
                    main_bundle_items_count += 1
                elif bundle_type == "child":
                    child_bundle_items_count += 1
            else:
                regular_items_count += 1
            
            print(f"      🔍 Adding to invoice: {item_row}")
            
            # Add item to invoice using ERPNext standard method
            invoice_item = invoice_doc.append("items", item_row)
            
            # For main bundle items with 100% discount, explicitly set discount_amount
            if discount_amount > 0 and bundle_type == "main":
                invoice_item.discount_amount = discount_amount
                print(f"      ✅ Main bundle item: Explicitly set discount_amount = ${discount_amount}")
                print(f"      ✅ Rate: ${final_rate}, Price List Rate: ${price_list_rate}")
            elif discount_amount > 0:
                print(f"      ✅ ERPNext will calculate discount from price_list_rate (${price_list_rate}) - rate (${final_rate})")
            
            logger.debug(f"Added item: {item_data['item_code']} (bundle: {is_bundle_item}, expected discount: ${discount_amount})")
            
            # Verification: Check that item was actually added
            current_items_count = len(invoice_doc.items)
            print(f"      ✅ Item added successfully, total items now: {current_items_count}")
        
        print(f"   ✅ All {len(invoice_doc.items)} items added:")
        print(f"      - Regular items: {regular_items_count}")
        print(f"      - Bundle items total: {bundle_items_count}")
        print(f"        - Bundle main items: {main_bundle_items_count}")
        print(f"        - Bundle child items: {child_bundle_items_count}")
        
        # Validation: Ensure we didn't lose any items
        if len(invoice_doc.items) != len(processed_items):
            error_msg = f"CRITICAL ERROR: Expected {len(processed_items)} items but invoice has {len(invoice_doc.items)} items!"
            print(f"   ❌ {error_msg}")
            logger.error(error_msg)
            frappe.throw(error_msg)
        else:
            print(f"   ✅ Item count validation passed: {len(invoice_doc.items)} items in invoice")
        
    except Exception as e:
        error_msg = f"Error adding items to document: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _calculate_item_rates(original_rate, discount_amount, qty):
    """Calculate final rate and price list rate for item."""
    if discount_amount > 0:
        # ERPNext way: Set price_list_rate to original, rate to discounted rate
        discount_per_unit = discount_amount / qty
        discounted_rate = original_rate - discount_per_unit
        
        # Safety check: ensure rate doesn't go below zero (for 100% discounts)
        if discounted_rate < 0:
            discounted_rate = 0.0  # 100% discount case
        
        price_list_rate = original_rate
        final_rate = discounted_rate
        
        # Special handling for 100% discount (main bundle items)
        if discount_per_unit >= original_rate:
            print(f"      🎯 100% Bundle discount detected (Main Bundle Item):")
            print(f"         Original rate: ${original_rate}")
            print(f"         Discount amount: ${discount_amount}")
            print(f"         Qty: {qty}")
            print(f"         Discount per unit: ${discount_per_unit}")
            print(f"         Final rate: ${final_rate} (100% discount applied)")
            print(f"         Price list rate: ${price_list_rate}")
        else:
            print(f"      🎯 Partial Bundle discount calculation:")
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
    
    return final_rate, price_list_rate


def _create_item_row(item_data, final_rate, price_list_rate):
    """Create item row dictionary for invoice."""
    return {
        "item_code": item_data["item_code"],
        "qty": item_data["qty"],
        "rate": final_rate,  # Use discounted rate
        "price_list_rate": price_list_rate,  # Original rate for ERPNext calculations
        "uom": item_data["uom"]
    }


def _handle_main_bundle_discount(item_row, discount_amount, original_rate, qty):
    """Handle special discount percentage for main bundle items."""
    discount_percentage = (discount_amount / (original_rate * qty)) * 100
    if discount_percentage >= 99.9:  # Essentially 100%
        item_row["discount_percentage"] = 100
        print(f"      🎯 Setting discount_percentage = 100% for main bundle item")


def _track_and_log_item(item_data, final_rate, original_rate, discount_amount, is_bundle_item, bundle_type):
    """Track and log item processing details."""
    if is_bundle_item:
        if bundle_type == "main":
            print(f"   Item (Bundle Main): {item_data['qty']} x {item_data['item_code']} @ ${final_rate} (was ${original_rate}) - Discount: ${discount_amount}")
        elif bundle_type == "child":
            print(f"   Item (Bundle Child): {item_data['qty']} x {item_data['item_code']} @ ${final_rate} (was ${original_rate}) - Discount: ${discount_amount}")
        else:
            print(f"   Item (Bundle Unknown): {item_data['qty']} x {item_data['item_code']} @ ${final_rate} (was ${original_rate}) - Discount: ${discount_amount}")
    else:
        print(f"   Item (Regular): {item_data['qty']} x {item_data['item_code']} @ ${final_rate}")


def add_delivery_charges_to_invoice(invoice_doc, delivery_charges, pos_profile, logger):
    """Add delivery charges as taxes and charges to the invoice."""
    logger.debug(f"Adding {len(delivery_charges)} delivery charges as taxes")
    try:
        for i, charge in enumerate(delivery_charges, 1):
            charge_type = charge.get('charge_type', 'Delivery')
            amount = float(charge.get('amount', 0))
            description = charge.get('description', f'{charge_type} Charge')
            
            if amount > 0:
                # Get the correct account name for the company
                freight_account = _get_freight_account(pos_profile.company, logger)
                
                # Add as tax/charge entry
                invoice_doc.append("taxes", {
                    "charge_type": "Actual",  # Fixed amount, not percentage
                    "account_head": freight_account,
                    "description": description,
                    "tax_amount": amount,
                    "total": 0,  # Will be calculated by ERPNext
                    "rate": 0,   # Not applicable for Actual type
                    "included_in_print_rate": 0,
                    "included_in_paid_amount": 0,
                })
                
                logger.debug(f"Added delivery charge: {charge_type} - ${amount}")
                print(f"   Charge {i}: {charge_type} - ${amount}")
                print(f"      - Account: {freight_account}")
                print(f"      - Type: Actual")
                print(f"      - Description: {description}")
            else:
                print(f"   Skipping charge {i}: Amount is zero")
        
        print(f"   ✅ Delivery charges added as taxes and charges")
        
    except Exception as e:
        error_msg = f"Error adding delivery charges: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _get_freight_account(company, logger):
    """Get the appropriate freight account for delivery charges."""
    freight_account = None
    
    try:
        # Get company abbreviation from ERPNext
        company_abbr = frappe.db.get_value("Company", company, "abbr")
        if company_abbr:
            # Try with company abbreviation first (most likely format)
            freight_account_with_abbr = f"Freight and Forwarding Charges - {company_abbr}"
            if frappe.db.exists("Account", freight_account_with_abbr):
                freight_account = freight_account_with_abbr
                print(f"      - Found account with abbreviation: {freight_account}")
        
        # If not found with abbreviation, try other patterns
        if not freight_account:
            account_patterns = [
                f"Freight and Forwarding Charges - {company}",
                f"Freight and Forwarding Ch - {company_abbr}",
                f"Freight and Forwarding Ch - {company}",
                "Freight and Forwarding Charges"
            ]
            for pattern in account_patterns:
                if frappe.db.exists("Account", pattern):
                    freight_account = pattern
                    print(f"      - Found account with pattern: {freight_account}")
                    break
        
        # If still no specific account found, search dynamically for any freight account
        if not freight_account:
            freight_accounts = frappe.db.sql("""
                SELECT name FROM `tabAccount`
                WHERE account_name = 'Freight and Forwarding Charges'
                AND company = %s
                AND is_group = 0
                ORDER BY name
                LIMIT 1
            """, (company,), as_dict=True)
            if freight_accounts:
                freight_account = freight_accounts[0].name
                print(f"      - Found account by search: {freight_account}")
        
    except Exception as account_error:
        logger.warning(f"Error finding freight account: {str(account_error)}")
        print(f"      - Error finding freight account: {str(account_error)}")
    
    # Fallback to a default account if freight account not found
    if not freight_account:
        company_abbr = frappe.db.get_value("Company", company, "abbr") or company
        freight_account = f"Miscellaneous Expenses - {company_abbr}"
        logger.warning(f"Using fallback account: {freight_account}")
        print(f"      - Warning: Using fallback account: {freight_account}")
    
    return freight_account


def verify_invoice_totals(invoice_doc, logger):
    """Verify invoice totals after submission."""
    print(f"   🔍 POST-SUBMISSION VERIFICATION:")
    try:
        # Reload the document to verify discount amounts persisted
        saved_invoice = frappe.get_doc("Sales Invoice", invoice_doc.name)
        print(f"   📋 Verifying {len(saved_invoice.items)} items in saved invoice:")
        
        for i, item in enumerate(saved_invoice.items, 1):
            discount_amt = getattr(item, 'discount_amount', 0) or 0
            item_amount = getattr(item, 'amount', 0) or 0
            original_amount = item.qty * item.rate
            print(f"      Item {i}: {item.item_code}")
            print(f"        Original: ${original_amount}, Discount: ${discount_amt}, Final: ${item_amount}")
            
            if discount_amt > 0:
                expected_final = original_amount - discount_amt
                discount_applied_correctly = abs(item_amount - expected_final) < 0.01
                print(f"        Discount Status: {'✅ Applied' if discount_applied_correctly else '❌ NOT Applied'}")
        
        print(f"   📊 Saved Invoice Totals:")
        print(f"      - Net Total: ${saved_invoice.net_total}")
        print(f"      - Grand Total: ${saved_invoice.grand_total}")
        
    except Exception as verification_error:
        print(f"   ⚠️ Post-submission verification failed: {str(verification_error)}")

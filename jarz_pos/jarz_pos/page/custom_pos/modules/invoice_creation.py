"""
Invoice Creation Module for Jarz POS

This module handles the main POS invoice creation logic,
including validation, document creation, and submission.
"""

import frappe
import traceback
from .bundle_processing import process_bundle_item
from ..utils.validation_utils import (
    validate_cart_data, 
    validate_customer, 
    validate_pos_profile,
    validate_delivery_datetime
)
from ..utils.invoice_utils import (
    set_invoice_fields,
    add_items_to_invoice,
    add_delivery_charges_to_invoice,
    verify_invoice_totals
)


@frappe.whitelist()
def create_pos_invoice(cart_json, customer_name, pos_profile_name=None, delivery_charges_json=None, required_delivery_datetime=None):
    """
    Create POS Sales Invoice using Frappe best practices with comprehensive logging
    
    Following Frappe/ERPNext best practices:
    - Proper error handling with frappe.throw()
    - Structured logging with frappe.log_error()
    - Document validation before save/submit
    - Handle delivery time slot for scheduled deliveries
    - Proper field setting in correct order
    """
    
    # Frappe best practice: Create logger for this module
    logger = frappe.logger("jarz_pos.custom_pos", allow_site=frappe.local.site)
    
    # Always log function entry for debugging
    logger.info(f"create_pos_invoice called with customer: {customer_name}")
    
    print("\n" + "="*100)
    print("🚀 CORE FUNCTION: create_pos_invoice")
    print("="*100)
    print(f"🕐 {frappe.utils.now()}")
    
    try:
        # STEP 1: Input Validation and Parsing
        print(f"\n1️⃣ INPUT VALIDATION:")
        logger.debug(f"Validating inputs: cart={bool(cart_json)}, customer={customer_name}")
        
        # Validate and parse cart data
        cart_items = validate_cart_data(cart_json, logger)
        
        # Parse delivery charges if provided
        delivery_charges = _parse_delivery_charges(delivery_charges_json, logger)
        
        # Parse and validate delivery datetime
        delivery_datetime = validate_delivery_datetime(required_delivery_datetime, logger)
        
        print(f"   ✅ Input validation passed")
        
        # STEP 2: Customer Validation
        print(f"\n2️⃣ CUSTOMER VALIDATION:")
        customer_doc = validate_customer(customer_name, logger)
        
        # STEP 3: POS Profile Validation
        print(f"\n3️⃣ POS PROFILE VALIDATION:")
        pos_profile = validate_pos_profile(pos_profile_name, logger)
        
        # STEP 4: Item and Bundle Processing
        print(f"\n4️⃣ ITEM AND BUNDLE PROCESSING:")
        processed_items = _process_cart_items(cart_items, pos_profile, logger)
        
        # STEP 5: Create Sales Invoice Document
        print(f"\n5️⃣ CREATING SALES INVOICE:")
        invoice_doc = _create_invoice_document(logger)
        
        # STEP 6: Set Document Fields
        print(f"\n6️⃣ SETTING DOCUMENT FIELDS:")
        set_invoice_fields(invoice_doc, customer_doc, pos_profile, delivery_datetime, logger)
        
        # STEP 7: Add Items to Document
        print(f"\n7️⃣ ADDING ITEMS:")
        add_items_to_invoice(invoice_doc, processed_items, logger)
        
        # STEP 7.5: Add Delivery Charges
        if delivery_charges:
            print(f"\n7️⃣.5️⃣ ADDING DELIVERY CHARGES:")
            add_delivery_charges_to_invoice(invoice_doc, delivery_charges, pos_profile, logger)
        
        # STEP 8: Validate and Calculate Document
        print(f"\n8️⃣ DOCUMENT VALIDATION:")
        _validate_and_calculate_document(invoice_doc, logger)
        
        # STEP 9: Save Document
        print(f"\n9️⃣ SAVING DOCUMENT:")
        _save_document(invoice_doc, delivery_datetime, logger)
        
        # STEP 10: Submit Document
        print(f"\n🔟 SUBMITTING DOCUMENT:")
        _submit_document(invoice_doc, logger)
        
        # STEP 11: Prepare Response
        print(f"\n🎯 PREPARING RESPONSE:")
        result = _prepare_response(invoice_doc, delivery_datetime, logger)
        
        print(f"\n🎉 SUCCESS! Invoice creation completed!")
        print("="*100)
        return result
        
    except Exception as e:
        # Comprehensive error logging following Frappe best practices
        _handle_invoice_creation_error(e, customer_name, pos_profile_name, logger)
        raise


def _parse_delivery_charges(delivery_charges_json, logger):
    """Parse delivery charges JSON."""
    delivery_charges = []
    if delivery_charges_json:
        try:
            delivery_charges = frappe.parse_json(delivery_charges_json) if isinstance(delivery_charges_json, str) else delivery_charges_json
            logger.debug(f"Parsed delivery charges: {len(delivery_charges)} charges")
            print(f"   📦 Delivery charges parsed: {len(delivery_charges)} charges")
            for i, charge in enumerate(delivery_charges, 1):
                print(f"      {i}. {charge.get('charge_type', 'Unknown')}: ${charge.get('amount', 0)}")
        except (ValueError, TypeError) as e:
            error_msg = f"Invalid delivery charges JSON format: {str(e)}"
            logger.error(error_msg)
            print(f"   ❌ {error_msg}")
            frappe.throw(error_msg)
    else:
        print(f"   📦 No delivery charges provided")
    return delivery_charges


def _process_cart_items(cart_items, pos_profile, logger):
    """Process all cart items including bundles."""
    logger.debug(f"Processing {len(cart_items)} cart items")
    processed_items = []  # Will contain both regular items and bundle items
    
    for i, item_data in enumerate(cart_items, 1):
        print(f"   Processing item {i}: {item_data}")
        
        # Extract item details with enhanced validation
        item_code = item_data.get("item_code")
        qty = item_data.get("qty", 1)
        rate = item_data.get("rate") or item_data.get("price", 0)
        is_bundle = item_data.get("is_bundle", False)
        
        # Enhanced logging for debugging
        print(f"      📋 Item Details:")
        print(f"         - item_code: {item_code}")
        print(f"         - qty: {qty}")
        print(f"         - rate: {rate}")
        print(f"         - is_bundle: {is_bundle} (type: {type(is_bundle)})")
        
        # Validate required fields
        if not item_code:
            logger.warning(f"Item {i} missing item_code, skipping")
            print(f"      ❌ Missing item_code, skipping")
            continue
            
        if qty <= 0:
            logger.warning(f"Item {i} has invalid quantity {qty}, using 1")
            print(f"      ⚠️ Invalid quantity {qty}, using 1")
            qty = 1
            
        if rate < 0:
            logger.warning(f"Item {i} has negative rate {rate}, using 0")
            print(f"      ⚠️ Negative rate {rate}, using 0")
            rate = 0
        
        if is_bundle:
            # Process bundle item
            bundle_items = _process_bundle_item(item_code, qty, rate, pos_profile, logger)
            processed_items.extend(bundle_items)
        else:
            # Process regular item
            regular_item = _process_regular_item(item_code, qty, rate, logger)
            processed_items.append(regular_item)
    
    if not processed_items:
        error_msg = "No valid items found in cart after processing"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)
    
    _log_processing_summary(processed_items, logger)
    return processed_items


def _process_bundle_item(item_code, qty, rate, pos_profile, logger):
    """Process a bundle item."""
    print(f"      🎁 BUNDLE DETECTED: {item_code}")
    print(f"      🔌 Passing bundle ID to process_bundle_item: {item_code}")
    
    try:
        bundle_items = process_bundle_item(item_code, qty, rate, pos_profile.selling_price_list)
        print(f"      ✅ Bundle processed: {len(bundle_items)} items added")
        return bundle_items
    except Exception as bundle_error:
        error_msg = f"Error processing bundle {item_code}: {str(bundle_error)}"
        logger.error(error_msg)
        print(f"      ❌ {error_msg}")
        # Continue with other items instead of failing the entire invoice
        return []


def _process_regular_item(item_code, qty, rate, logger):
    """Process a regular item."""
    print(f"      📦 REGULAR ITEM: {item_code}")
    
    # Validate regular item exists
    if not frappe.db.exists("Item", item_code):
        error_msg = f"Item '{item_code}' does not exist"
        logger.error(error_msg)
        print(f"         ❌ {error_msg}")
        frappe.throw(error_msg)
    
    # Get item details for regular item
    try:
        item_doc = frappe.get_doc("Item", item_code)
        logger.debug(f"Item validated: {item_doc.item_name}")
        print(f"         ✅ {item_doc.item_name} (UOM: {item_doc.stock_uom})")
        
        return {
            "item_code": item_code,
            "qty": float(qty),
            "rate": float(rate),
            "uom": item_doc.stock_uom,
            "is_bundle_item": False
        }
    except Exception as e:
        error_msg = f"Error loading item '{item_code}': {str(e)}"
        logger.error(error_msg)
        print(f"         ❌ {error_msg}")
        frappe.throw(error_msg)


def _log_processing_summary(processed_items, logger):
    """Log a detailed summary of processed items."""
    print(f"   ✅ Processing complete: {len(processed_items)} total items (including bundle items)")
    
    # Log summary of processed items
    bundle_items_count = len([item for item in processed_items if item.get("is_bundle_item", False)])
    regular_items_count = len(processed_items) - bundle_items_count
    print(f"      - Regular items: {regular_items_count}")
    print(f"      - Bundle items: {bundle_items_count}")
    
    # CRITICAL DEBUG: List all processed items before moving to validation
    print(f"   🔍 ALL PROCESSED ITEMS DETAILS:")
    total_main_items = 0
    total_child_items = 0
    total_regular_items = 0
    
    for i, item in enumerate(processed_items, 1):
        bundle_type = item.get("bundle_type", "N/A")
        is_bundle = item.get("is_bundle_item", False)
        discount = item.get("discount_amount", 0)
        
        # Count items by type
        if bundle_type == "main":
            total_main_items += 1
        elif bundle_type == "child":
            total_child_items += 1
        elif not is_bundle:
            total_regular_items += 1
        
        print(f"      Processed Item {i}: {item['item_code']} - Bundle: {is_bundle}, Type: {bundle_type}, Qty: {item['qty']}, Rate: {item['rate']}, Discount: ${discount}")
    
    print(f"   📊 PROCESSING SUMMARY:")
    print(f"      - Total processed items: {len(processed_items)}")
    print(f"      - Regular items: {total_regular_items}")
    print(f"      - Bundle main items: {total_main_items}")
    print(f"      - Bundle child items: {total_child_items}")
    
    # Validation: Ensure we have the expected structure
    expected_items_in_invoice = total_regular_items + total_main_items + total_child_items
    print(f"      - Expected items in final invoice: {expected_items_in_invoice}")
    
    if len(processed_items) != expected_items_in_invoice:
        print(f"      ⚠️ WARNING: Item count mismatch!")
    else:
        print(f"      ✅ Item counts match expected structure")


def _create_invoice_document(logger):
    """Create a new Sales Invoice document."""
    logger.debug("Creating new Sales Invoice document")
    try:
        # Frappe best practice: Use frappe.new_doc()
        invoice_doc = frappe.new_doc("Sales Invoice")
        logger.debug("Sales Invoice document created")
        print(f"   ✅ New document created")
        return invoice_doc
    except Exception as e:
        error_msg = f"Error creating Sales Invoice document: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _validate_and_calculate_document(invoice_doc, logger):
    """Validate and calculate document totals."""
    logger.debug("Running ERPNext document validation")
    try:
        # Store all discount amounts before any calculations
        print(f"   💾 STORING DISCOUNT AMOUNTS BEFORE CALCULATION...")
        stored_item_discounts = _store_discount_amounts(invoice_doc)
        
        # Let ERPNext set missing values
        print(f"   Running set_missing_values()...")
        invoice_doc.set_missing_values()
        
        # Restore discount amounts after set_missing_values
        print(f"   🔄 RESTORING DISCOUNTS AFTER set_missing_values...")
        _restore_discount_amounts(invoice_doc, stored_item_discounts)
        
        # Run initial calculation to set up the document properly
        print(f"   Running initial calculate_taxes_and_totals()...")
        invoice_doc.calculate_taxes_and_totals()
        
        # Process discount amounts after first calculation
        print(f"   🔄 PROCESSING DISCOUNT AMOUNTS AFTER FIRST CALCULATION...")
        _process_discount_amounts_after_calculation(invoice_doc, stored_item_discounts)
        
        # Run final calculation to apply all discounts and update totals
        print(f"   Running final calculate_taxes_and_totals()...")
        _final_calculation_with_discount_preservation(invoice_doc, stored_item_discounts)
        
        # Verify discount amounts are properly applied
        _verify_discount_application(invoice_doc, logger)
        
        logger.debug(f"Document validated - Total: {invoice_doc.grand_total}")
        print(f"   ✅ Document validated:")
        print(f"      - Net Total: {invoice_doc.net_total}")
        print(f"      - Grand Total: {invoice_doc.grand_total}")
        
    except Exception as e:
        error_msg = f"Error during document validation: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _store_discount_amounts(invoice_doc):
    """Store discount amounts before calculations."""
    stored_item_discounts = {}
    for i, item in enumerate(invoice_doc.items):
        discount_amt = getattr(item, 'discount_amount', 0) or 0
        if discount_amt > 0:
            stored_item_discounts[i] = discount_amt
            print(f"      Stored discount for item {i+1} ({item.item_code}): ${discount_amt}")
    print(f"   📋 Total items with discounts stored: {len(stored_item_discounts)}")
    return stored_item_discounts


def _restore_discount_amounts(invoice_doc, stored_item_discounts):
    """Restore discount amounts after set_missing_values."""
    for i, discount_amt in stored_item_discounts.items():
        if i < len(invoice_doc.items):
            current_discount = getattr(invoice_doc.items[i], 'discount_amount', 0) or 0
            if current_discount != discount_amt:
                invoice_doc.items[i].discount_amount = discount_amt
                print(f"      Restored discount for item {i+1}: ${discount_amt}")


def _process_discount_amounts_after_calculation(invoice_doc, stored_item_discounts):
    """Process discount amounts after first calculation."""
    total_items_with_discounts = 0
    total_discount_amount = 0
    
    for i, item in enumerate(invoice_doc.items):
        # Get the stored discount amount (original)
        stored_discount = stored_item_discounts.get(i, 0)
        current_discount = getattr(item, 'discount_amount', 0) or 0
        
        if stored_discount > 0:
            total_items_with_discounts += 1
            total_discount_amount += stored_discount
            
            # Check if discount was reset and restore it
            if current_discount != stored_discount:
                print(f"      Item {item.item_code}: discount was reset from ${stored_discount} to ${current_discount}, restoring...")
                item.discount_amount = float(stored_discount)
            
            # Force calculation of the item amount
            original_amount = item.qty * item.rate
            item.amount = original_amount - stored_discount
            item.net_amount = item.amount  # Ensure net_amount also reflects discount
            print(f"      Item {item.item_code}: discount ${stored_discount}, amount ${item.amount}")
    
    print(f"   📊 Discount Summary: {total_items_with_discounts} items with ${total_discount_amount} total discount")


def _final_calculation_with_discount_preservation(invoice_doc, stored_item_discounts):
    """Run final calculation while preserving discounts."""
    # Store discounts again before final calculation
    final_stored_discounts = {}
    for i, item in enumerate(invoice_doc.items):
        discount_amt = getattr(item, 'discount_amount', 0) or 0
        if discount_amt > 0:
            final_stored_discounts[i] = discount_amt
    
    invoice_doc.calculate_taxes_and_totals()
    
    # FINAL RESTORE: Ensure discounts persist after final calculation
    print(f"   🔄 FINAL DISCOUNT RESTORATION...")
    for i, discount_amt in final_stored_discounts.items():
        if i < len(invoice_doc.items):
            current_discount = getattr(invoice_doc.items[i], 'discount_amount', 0) or 0
            if current_discount != discount_amt:
                print(f"      Final restore for item {i+1}: ${discount_amt}")
                invoice_doc.items[i].discount_amount = discount_amt
                original_amount = invoice_doc.items[i].qty * invoice_doc.items[i].rate
                invoice_doc.items[i].amount = original_amount - discount_amt
                invoice_doc.items[i].net_amount = invoice_doc.items[i].amount


def _verify_discount_application(invoice_doc, logger):
    """Verify that discounts are properly applied."""
    print(f"   🔍 VERIFYING DISCOUNT APPLICATION:")
    total_discount_applied = 0
    
    for i, item in enumerate(invoice_doc.items, 1):
        item_discount = getattr(item, 'discount_amount', 0) or 0
        item_amount = getattr(item, 'amount', 0) or 0
        item_rate = getattr(item, 'rate', 0) or 0
        item_qty = getattr(item, 'qty', 0) or 0
        expected_original = item_rate * item_qty
        expected_final = expected_original - item_discount
        
        print(f"      Item {i} ({item.item_code}):")
        print(f"        Rate: ${item_rate}, Qty: {item_qty}")
        print(f"        Expected Original: ${expected_original}")
        print(f"        Discount Amount: ${item_discount}")
        print(f"        Expected Final: ${expected_final}")
        print(f"        Actual Amount: ${item_amount}")
        print(f"        ✅ Discount Applied: {'Yes' if abs(item_amount - expected_final) < 0.01 else 'No - ISSUE!'}")
        
        total_discount_applied += item_discount
    
    print(f"   📊 TOTAL DISCOUNT SUMMARY:")
    print(f"      - Total discount applied: ${total_discount_applied}")
    print(f"      - Net Total: ${invoice_doc.net_total}")
    print(f"      - Grand Total: ${invoice_doc.grand_total}")


def _save_document(invoice_doc, delivery_datetime, logger):
    """Save the invoice document."""
    logger.debug("Saving document")
    try:
        # Frappe best practice: Use insert() for new documents
        invoice_doc.insert(ignore_permissions=True)
        logger.info(f"Invoice saved: {invoice_doc.name}")
        print(f"   ✅ Document saved: {invoice_doc.name}")
        
        # Verify delivery datetime field after save
        if delivery_datetime:
            _verify_delivery_field_after_save(invoice_doc, delivery_datetime, logger)
            
    except Exception as e:
        error_msg = f"Error saving document: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _verify_delivery_field_after_save(invoice_doc, delivery_datetime, logger):
    """Verify delivery datetime field was set correctly after save."""
    print(f"\n🔍 DELIVERY FIELD VERIFICATION AFTER SAVE:")
    field_name = 'required_delivery_datetime'
    
    # Reload document to get fresh state from database
    saved_doc = frappe.get_doc("Sales Invoice", invoice_doc.name)
    
    # Check multiple ways to verify the field value
    field_value_attr = getattr(saved_doc, field_name, None)
    field_value_get = saved_doc.get(field_name)
    field_value_db = frappe.db.get_value("Sales Invoice", invoice_doc.name, field_name)
    
    print(f"   📊 Field verification for '{field_name}':")
    print(f"      - Via getattr(): {field_value_attr}")
    print(f"      - Via get(): {field_value_get}")
    print(f"      - Via db.get_value(): {field_value_db}")
    print(f"      - Expected value: {delivery_datetime}")
    
    # Determine if field was set successfully
    field_is_set = any([field_value_attr, field_value_get, field_value_db])
    
    if field_is_set:
        print(f"   ✅ Delivery datetime field verified: {field_value_attr or field_value_get or field_value_db}")
    else:
        print(f"   ❌ Delivery datetime field NOT SET - attempting correction...")
        # Try to set it again on the saved document
        try:
            saved_doc.set(field_name, delivery_datetime)
            saved_doc.save(ignore_permissions=True)
            print(f"   🔄 Re-saved document with delivery datetime")
            # Verify again
            final_value = frappe.db.get_value("Sales Invoice", invoice_doc.name, field_name)
            print(f"   🔍 Final verification: {final_value}")
        except Exception as correction_error:
            print(f"   ❌ Could not correct delivery datetime: {str(correction_error)}")
            # Don't throw error here, just log it
            logger.warning(f"Delivery datetime field could not be set: {str(correction_error)}")


def _submit_document(invoice_doc, logger):
    """Submit the invoice document."""
    logger.debug("Submitting document")
    try:
        # Frappe best practice: Submit after successful save
        invoice_doc.submit()
        logger.info(f"Invoice submitted: {invoice_doc.name}")
        print(f"   ✅ Document submitted successfully!")
        
        # Verify discount amounts persisted after submission
        verify_invoice_totals(invoice_doc, logger)
        
    except Exception as e:
        error_msg = f"Error submitting document: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _prepare_response(invoice_doc, delivery_datetime, logger):
    """Prepare the response data."""
    result = {
        "success": True,
        "invoice_name": invoice_doc.name,
        "grand_total": invoice_doc.grand_total,
        "net_total": invoice_doc.net_total,
        "customer": invoice_doc.customer,
        "items_count": len(invoice_doc.items),
        "status": invoice_doc.status,
        "docstatus": invoice_doc.docstatus,
        "posting_date": str(invoice_doc.posting_date),
        "company": invoice_doc.company
    }
    
    # Add delivery information to response if provided
    if delivery_datetime:
        result["delivery_datetime"] = delivery_datetime.isoformat()
        result["delivery_date"] = delivery_datetime.date().isoformat()
        result["delivery_time"] = delivery_datetime.time().isoformat()
        result["delivery_label"] = delivery_datetime.strftime('%A, %B %d, %Y at %I:%M %p')
        print(f"      delivery_datetime: {result['delivery_datetime']}")
        print(f"      delivery_label: {result['delivery_label']}")
    
    logger.info(f"Invoice creation successful: {invoice_doc.name}")
    print(f"   ✅ Response prepared:")
    for key, value in result.items():
        print(f"      {key}: {value}")
    
    return result


def _handle_invoice_creation_error(e, customer_name, pos_profile_name, logger):
    """Handle and log invoice creation errors."""
    error_msg = f"Error in create_pos_invoice: {str(e)}"
    logger.error(error_msg, exc_info=True)
    print(f"\n❌ FUNCTION ERROR:")
    print(f"   Type: {type(e).__name__}")
    print(f"   Message: {str(e)}")
    
    # Log full error details for debugging
    full_traceback = traceback.format_exc()
    print(f"   Traceback:")
    print(full_traceback)
    
    # Frappe best practice: Use frappe.log_error for persistent logging
    frappe.log_error(
        title=f"POS Invoice Creation Error: {type(e).__name__}",
        message=f"""
FUNCTION: create_pos_invoice
ERROR: {str(e)}
PARAMETERS:
- customer_name: {customer_name}
- pos_profile_name: {pos_profile_name}
TRACEBACK:
{full_traceback}
        """.strip()
    )
    print("="*100)

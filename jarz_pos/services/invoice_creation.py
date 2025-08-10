"""
Invoice Creation Module for Jarz POS

This module handles the main POS invoice creation logic,
including validation, document creation, and submission.
"""

import frappe
import traceback
from .bundle_processing import process_bundle_for_invoice, validate_bundle_configuration_by_item
from jarz_pos.utils.validation_utils import (
    validate_cart_data, 
    validate_customer, 
    validate_pos_profile,
    validate_delivery_datetime
)
from jarz_pos.utils.invoice_utils import (
    set_invoice_fields,
    add_items_to_invoice,
    verify_invoice_totals
)
from jarz_pos.utils.delivery_utils import add_delivery_charges_to_taxes


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
        
        # STEP 7.4: Inject Shipping (Territory Delivery Income) as Actual tax row
        print(f"\n7️⃣.4️⃣ ADDING SHIPPING (Territory Delivery Income) AS TAX:")
        try:
            territory_name = getattr(customer_doc, 'territory', None)
            if territory_name and frappe.db.exists('Territory', territory_name):
                territory_doc = frappe.get_doc('Territory', territory_name)
                shipping_income = getattr(territory_doc, 'delivery_income', 0) or 0
                print(f"   📦 Territory: {territory_name} | delivery_income: {shipping_income}")
                if shipping_income and float(shipping_income) > 0:
                    # Avoid duplicate insertion (idempotent)
                    already_added = False
                    if getattr(invoice_doc, 'taxes', None):
                        for tax in invoice_doc.taxes:
                            if (tax.get('description') or '').lower().startswith('shipping income'):
                                already_added = True
                                print("   ⚠️ Shipping income tax row already present – skipping")
                                break
                    if not already_added:
                        add_delivery_charges_to_taxes(
                            invoice_doc,
                            shipping_income,
                            delivery_description=f"Shipping Income ({territory_name})"
                        )
                        print("   ✅ Shipping income tax row appended")
                else:
                    print("   ℹ️ No positive delivery_income on territory – nothing added")
            else:
                print("   ℹ️ Customer territory not found – skipping shipping income")
        except Exception as ship_err:
            print(f"   ❌ Failed adding shipping income: {ship_err}")
            # Do not abort – continue invoice creation
        
        # STEP 7.5: Add Delivery Charges (legacy param based)
        if delivery_charges:
            print(f"\n7️⃣.5️⃣ ADDING DELIVERY CHARGES:")
            add_delivery_charges_to_taxes(invoice_doc, delivery_charges, "Delivery Charges")
        
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
        
        # Additional debug: Check if this item exists in different places
        is_erpnext_item = frappe.db.exists("Item", item_code)
        is_bundle_record = frappe.db.exists("Jarz Bundle", item_code)
        bundle_with_erpnext_item = frappe.get_all("Jarz Bundle", 
            filters={"erpnext_item": item_code}, 
            fields=["name", "bundle_name"], 
            limit=1)
        
        print(f"         - ERPNext Item exists: {is_erpnext_item}")
        print(f"         - Jarz Bundle record exists: {is_bundle_record}")
        print(f"         - Bundle with this ERPNext item: {bundle_with_erpnext_item}")
        
        if is_bundle and not bundle_with_erpnext_item and not is_bundle_record:
            print(f"         ⚠️ WARNING: is_bundle=True but no bundle found for '{item_code}' (neither as ERPNext item nor bundle ID)")
        elif is_bundle and (bundle_with_erpnext_item or is_bundle_record):
            if bundle_with_erpnext_item:
                print(f"         ✅ Bundle found by erpnext_item: {bundle_with_erpnext_item[0]['name']} ({bundle_with_erpnext_item[0]['bundle_name']})")
            elif is_bundle_record:
                bundle_doc = frappe.get_doc("Jarz Bundle", item_code)
                print(f"         ✅ Bundle found by record ID: {item_code} ({bundle_doc.bundle_name})")
        
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
    print(f"      🔌 Processing bundle using ERPNext item: {item_code}")
    
    try:
        # Validate bundle configuration using ERPNext item code
        is_valid, message, bundle_code = validate_bundle_configuration_by_item(item_code)
        if not is_valid:
            error_msg = f"Bundle validation failed for ERPNext item {item_code}: {message}"
            logger.error(error_msg)
            print(f"      ❌ {error_msg}")
            frappe.throw(error_msg)
        
        print(f"      ✅ Found bundle: {bundle_code} for ERPNext item: {item_code}")
        
        # Process bundle using ERPNext item code (not bundle record ID)
        bundle_items = process_bundle_for_invoice(item_code, qty)
        print(f"      ✅ Bundle processed: {len(bundle_items)} items added")
        return bundle_items
    except Exception as bundle_error:
        error_msg = f"Error processing bundle with ERPNext item {item_code}: {str(bundle_error)}"
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
        rate = item.get("rate", item.get("price_list_rate", 0))  # Fix: fallback to price_list_rate if rate missing
        
        # Count items by type
        if bundle_type == "main":
            total_main_items += 1
        elif bundle_type == "child":
            total_child_items += 1
        elif not is_bundle:
            total_regular_items += 1
        
        print(f"      Processed Item {i}: {item['item_code']} - Bundle: {is_bundle}, Type: {bundle_type}, Qty: {item['qty']}, Rate: {rate}, Discount: ${discount}")
    
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
    """Validate and calculate document totals using native ERPNext logic.
    No custom discount preservation - let ERPNext handle discount_percentage naturally.
    """
    logger.debug("Running ERPNext document validation (native discount logic)...")
    try:
        print(f"   📋 Pre-calculation item summary:")
        for idx, item in enumerate(invoice_doc.items, 1):
            price_list_rate = getattr(item, 'price_list_rate', 0) or 0
            discount_pct = getattr(item, 'discount_percentage', 0) or 0
            qty = getattr(item, 'qty', 0) or 0
            print(f"      {idx}. {item.item_code} | qty={qty} | price_list_rate={price_list_rate} | discount_pct={discount_pct}")

        print(f"   Running set_missing_values()...")
        invoice_doc.set_missing_values()

        print(f"   Running calculate_taxes_and_totals()...")
        invoice_doc.calculate_taxes_and_totals()

        _log_discount_diagnostics_final(invoice_doc)

        logger.debug(f"Document validated - Total: {invoice_doc.grand_total}")
        print(f"   ✅ Document validated:")
        print(f"      - Net Total: {invoice_doc.net_total}")
        print(f"      - Grand Total: {invoice_doc.grand_total}")
    except Exception as e:
        error_msg = f"Error during document validation: {str(e)}"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)


def _log_discount_diagnostics_final(invoice_doc):
    """Log final discount application after ERPNext processing."""
    print(f"   🔍 FINAL DISCOUNT DIAGNOSTICS (after ERPNext processing):")
    total_discount_amount = 0.0
    total_net_amount = 0.0
    
    for idx, item in enumerate(invoice_doc.items, 1):
        discount_amt = float(getattr(item, 'discount_amount', 0) or 0)
        disc_pct = float(getattr(item, 'discount_percentage', 0) or 0)
        qty = float(item.qty or 0)
        rate = float(item.rate or 0)
        amount = float(item.amount or 0)
        price_list_rate = float(getattr(item, 'price_list_rate', 0) or 0)
        
        # ERPNext computed values
        line_gross = price_list_rate * qty if price_list_rate > 0 else rate * qty
        line_discount_total = discount_amt * qty if discount_amt > 0 else (line_gross * disc_pct / 100.0)
        
        total_discount_amount += line_discount_total
        total_net_amount += amount
        
        print(f"      {idx}. {item.item_code}:")
        print(f"         qty={qty} | price_list_rate={price_list_rate} | rate={rate} | amount={amount}")
        print(f"         discount_pct={disc_pct}% | discount_amt={discount_amt} | line_discount_total={line_discount_total}")
        
        # Validation: check if ERPNext computed correctly
        if disc_pct == 100:
            expected_rate = 0.0
            if abs(rate - expected_rate) > 0.01:
                print(f"         ⚠️ Expected rate=0 for 100% discount, got rate={rate}")
        elif price_list_rate > 0 and disc_pct > 0:
            expected_rate = price_list_rate * (1 - disc_pct/100)
            if abs(rate - expected_rate) > 0.01:
                print(f"         ⚠️ Expected rate={expected_rate}, got rate={rate}")
    
    print(f"   💰 FINAL TOTALS:")
    print(f"      - Total discount applied: {total_discount_amount}")
    print(f"      - Net amount (sum of line amounts): {total_net_amount}")
    print(f"      - Document net_total: {invoice_doc.net_total}")
    print(f"      - Document grand_total: {invoice_doc.grand_total}")
    
    # Verify net total matches sum of line amounts
    if abs(total_net_amount - float(invoice_doc.net_total)) > 0.01:
        print(f"      ⚠️ Net total mismatch! Line sum: {total_net_amount}, Doc total: {invoice_doc.net_total}")
    else:
        print(f"      ✅ Net total verified correctly")


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

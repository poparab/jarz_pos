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


def _apply_delivery_slot_fields(invoice_doc, delivery_datetime):
    """Populate new delivery slot fields on the Sales Invoice from a datetime.

    Fields:
      - custom_delivery_date (Date)
      - custom_delivery_time_from (Time)
      - custom_delivery_duration (Duration, stored in SECONDS)

    Notes:
      - Historically code treated duration as minutes. We now standardize to seconds.
      - If a small integer value (< 1000) is detected, it's assumed to be minutes and converted to seconds.
      - Parse optional request param 'delivery_duration' if present: supports '4h', '240m', '2:30', or plain numbers.
    """
    def _parse_duration_to_seconds(raw) -> int | None:
        if raw is None:
            return None
        try:
            if isinstance(raw, (int, float)):
                val = float(raw)
                # Heuristic: treat small numbers as minutes (legacy), otherwise seconds
                return int(val * 60) if val < 1000 else int(val)
            s = str(raw).strip().lower()
            if not s:
                return None
            # HH:MM or H:MM
            if ":" in s:
                parts = s.split(":", 1)
                h = int(parts[0] or 0)
                m = int(parts[1] or 0)
                return h * 3600 + m * 60
            # Suffix-based
            if s.endswith(("hours", "hour", "hrs", "hr", "h")):
                num = float(s.rstrip("hoursr h"))
                return int(num * 3600)
            if s.endswith(("minutes", "minute", "mins", "min", "m")):
                num = float(s.rstrip("minutesin m"))
                return int(num * 60)
            # Plain number: assume minutes (legacy)
            return int(float(s) * 60)
        except Exception:
            return None

    if not delivery_datetime:
        return
    try:
        dt = frappe.utils.get_datetime(delivery_datetime)
        invoice_doc.custom_delivery_date = dt.date()
        invoice_doc.custom_delivery_time_from = dt.time().strftime("%H:%M:%S")

        # If request provided an explicit duration, parse it
        try:
            raw_duration = getattr(frappe, "form_dict", {}).get("delivery_duration")
        except Exception:
            raw_duration = None
        parsed_seconds = _parse_duration_to_seconds(raw_duration)

        current_val = getattr(invoice_doc, "custom_delivery_duration", None)
        if parsed_seconds is not None:
            invoice_doc.custom_delivery_duration = parsed_seconds
        elif current_val:
            # Normalize existing value to seconds if it looks like minutes
            try:
                cur = float(current_val)
                invoice_doc.custom_delivery_duration = int(cur * 60) if cur < 1000 else int(cur)
            except Exception:
                invoice_doc.custom_delivery_duration = 3600
        else:
            # Default: 1 hour in seconds
            invoice_doc.custom_delivery_duration = 3600
    except Exception:
        # Non-fatal; let validation hook catch missing fields if required
        pass


def _set_initial_state_for_sales_partner(invoice_doc, logger):
    """If a Sales Partner is set on the invoice, initialize the Kanban state to 'In Progress'.

    We don't assume a specific custom field name. Instead, we probe common candidates and
    set whichever exist on the Sales Invoice meta so the Kanban board (which reads these
    fields) will place the new order under the 'In Progress' column.

    Candidate fields (in priority order):
      - custom_sales_invoice_state (preferred)
      - sales_invoice_state
      - custom_state
      - state
    """
    try:
        if not getattr(invoice_doc, "sales_partner", None):
            return
        target_state = "In Progress"
        meta = frappe.get_meta("Sales Invoice")
        candidates = [
            "custom_sales_invoice_state",
            "sales_invoice_state",
            "custom_state",
            "state",
        ]
        updated = []
        for f in candidates:
            try:
                field = meta.get_field(f)
                if field:
                    value_to_set = target_state
                    # If it's a Select field, ensure option exists (case-insensitive)
                    try:
                        if getattr(field, "fieldtype", "") == "Select":
                            raw_options = getattr(field, "options", "") or ""
                            opts = [o.strip() for o in str(raw_options).split("\n") if o.strip()]
                            if opts:
                                match = next((o for o in opts if o.lower() == target_state.lower()), None)
                                if match:
                                    value_to_set = match
                                else:
                                    # Skip setting this field if 'In Progress' isn't an allowed option
                                    print(f"   ℹ️ Field '{f}' is Select without 'In Progress' option – skipping")
                                    field = None
                    except Exception:
                        pass
                    if field:
                        # Prefer using setter to ensure ORM picks change up
                        try:
                            invoice_doc.set(f, value_to_set)
                        except Exception:
                            setattr(invoice_doc, f, value_to_set)
                        updated.append(f)
            except Exception:
                # Ignore meta access issues per-field and continue
                continue
        if updated:
            logger.info(
                f"Initial Kanban state set to '{target_state}' on fields: {updated} (sales_partner present)"
            )
            print(f"   🧭 Initial state set to '{target_state}' on {updated}")
        else:
            logger.warning(
                "Sales partner present, but no known Kanban state field found to set initial state"
            )
    except Exception as e:
        try:
            logger.warning(f"Could not set initial Kanban state for sales partner: {e}")
        except Exception:
            pass


@frappe.whitelist()
def create_pos_invoice(
    cart_json,
    customer_name,
    pos_profile_name=None,
    delivery_charges_json=None,
    required_delivery_datetime=None,
    sales_partner: str | None = None,
    payment_type: str | None = None,
):
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

    print("\n" + "=" * 100)
    print("🚀 CORE FUNCTION: create_pos_invoice")
    print("=" * 100)
    print(f"🕐 {frappe.utils.now()}")

    try:
        # STEP 1: Input Validation and Parsing
        print("\n1️⃣ INPUT VALIDATION:")
        logger.debug(f"Validating inputs: cart={bool(cart_json)}, customer={customer_name}")

        # Validate and parse cart data
        cart_items = validate_cart_data(cart_json, logger)

        # Parse delivery charges if provided
        delivery_charges = _parse_delivery_charges(delivery_charges_json, logger)

        # Parse and validate delivery datetime
        delivery_datetime = validate_delivery_datetime(required_delivery_datetime, logger)

        print("   ✅ Input validation passed")

        # STEP 2: Customer Validation
        print("\n2️⃣ CUSTOMER VALIDATION:")
        customer_doc = validate_customer(customer_name, logger)

        # STEP 3: POS Profile Validation
        print("\n3️⃣ POS PROFILE VALIDATION:")
        pos_profile = validate_pos_profile(pos_profile_name, logger)

        # STEP 4: Item and Bundle Processing
        print("\n4️⃣ ITEM AND BUNDLE PROCESSING:")
        processed_items = _process_cart_items(cart_items, pos_profile, logger)

        # STEP 5: Create Sales Invoice Document
        print("\n5️⃣ CREATING SALES INVOICE:")
        invoice_doc = _create_invoice_document(logger)

        # STEP 6: Set Document Fields
        print("\n6️⃣ SETTING DOCUMENT FIELDS:")
        set_invoice_fields(invoice_doc, customer_doc, pos_profile, delivery_datetime, logger)

        # STEP 6.1: Optional Sales Partner assignment (touch-friendly picker from POS)
        if sales_partner:
            try:
                if frappe.db.exists("Sales Partner", sales_partner):
                    invoice_doc.sales_partner = sales_partner
                    print(f"   🤝 Sales Partner set: {sales_partner}")
                else:
                    print(f"   ⚠️ Sales Partner not found: {sales_partner} (ignored)")
            except Exception as sp_err:
                print(f"   ⚠️ Could not set Sales Partner: {sp_err}")

        # STEP 6.2: Initialize Kanban state to 'In Progress' when a Sales Partner is set
        _set_initial_state_for_sales_partner(invoice_doc, logger)

        # STEP 7: Add Items to Document
        print("\n7️⃣ ADDING ITEMS:")
        add_items_to_invoice(invoice_doc, processed_items, logger)

        # STEP 7.4: Inject Shipping (Territory Delivery Income) as Actual tax row
        print("\n7️⃣.4️⃣ ADDING SHIPPING (Territory Delivery Income) AS TAX:")
        try:
            territory_name = getattr(customer_doc, "territory", None)
            if territory_name and frappe.db.exists("Territory", territory_name):
                territory_doc = frappe.get_doc("Territory", territory_name)
                shipping_income = getattr(territory_doc, "delivery_income", 0) or 0
                print(f"   📦 Territory: {territory_name} | delivery_income: {shipping_income}")
                if shipping_income and float(shipping_income) > 0:
                    # Avoid duplicate insertion (idempotent)
                    already_added = False
                    if getattr(invoice_doc, "taxes", None):
                        for tax in invoice_doc.taxes:
                            if (tax.get("description") or "").lower().startswith("shipping income"):
                                already_added = True
                                print("   ⚠️ Shipping income tax row already present – skipping")
                                break
                    if not already_added:
                        add_delivery_charges_to_taxes(
                            invoice_doc,
                            shipping_income,
                            delivery_description=f"Shipping Income ({territory_name})",
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
            print("\n7️⃣.5️⃣ ADDING DELIVERY CHARGES:")
            add_delivery_charges_to_taxes(invoice_doc, delivery_charges, "Delivery Charges")

        # STEP 8: Validate and Calculate Document
        print("\n8️⃣ DOCUMENT VALIDATION:")
        _validate_and_calculate_document(invoice_doc, logger)

        # STEP 9: Save Document
        print("\n9️⃣ SAVING DOCUMENT:")
        _save_document(invoice_doc, delivery_datetime, logger)

        # STEP 10: Submit Document
        print("\n🔟 SUBMITTING DOCUMENT:")
        _submit_document(invoice_doc, logger)

        # STEP 11: If payment_type == 'online' and invoice has a sales partner, create a Payment Entry
        try:
            _maybe_register_online_payment_to_partner(invoice_doc, sales_partner, payment_type, logger)
        except Exception as pay_err:
            # Don't fail invoice creation if payment step fails; log and proceed
            print(f"   ❌ Online payment registration failed: {pay_err}")
            try:
                logger.warning(f"Online payment registration failed: {pay_err}")
            except Exception:
                pass

        # STEP 12: Prepare Response
        print("\n🎯 PREPARING RESPONSE:")
        result = _prepare_response(invoice_doc, delivery_datetime, logger)

        print("\n🎉 SUCCESS! Invoice creation completed!")
        print("=" * 100)
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
        # Set new delivery slot fields before insert if delivery datetime provided
        if delivery_datetime:
            _apply_delivery_slot_fields(invoice_doc, delivery_datetime)

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
    """Verify delivery slot fields were set correctly after save."""
    print(f"\n🔍 DELIVERY SLOT VERIFICATION AFTER SAVE:")
    # Reload document to get fresh state from database
    saved_doc = frappe.get_doc("Sales Invoice", invoice_doc.name)

    # Fetch new fields
    date_attr = getattr(saved_doc, "custom_delivery_date", None)
    time_from_attr = getattr(saved_doc, "custom_delivery_time_from", None)
    duration_attr = getattr(saved_doc, "custom_delivery_duration", None)

    print(f"   📊 custom_delivery_date: {date_attr}")
    print(f"   📊 custom_delivery_time_from: {time_from_attr}")
    print(f"   📊 custom_delivery_duration: {duration_attr}")

    if not (date_attr and time_from_attr and duration_attr):
        # Attempt to apply from provided delivery_datetime again
        try:
            _apply_delivery_slot_fields(saved_doc, delivery_datetime)
            saved_doc.save(ignore_permissions=True)
            # Reload values
            date_attr = getattr(saved_doc, "custom_delivery_date", None)
            time_from_attr = getattr(saved_doc, "custom_delivery_time_from", None)
            duration_attr = getattr(saved_doc, "custom_delivery_duration", None)
            print(f"   � Re-saved with delivery slot fields")
        except Exception as correction_error:
            print(f"   ❌ Could not set delivery slot fields: {str(correction_error)}")
            logger.warning(f"Delivery slot fields could not be set: {str(correction_error)}")


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
        try:
            result["delivery_datetime"] = delivery_datetime.isoformat()
            result["delivery_date"] = delivery_datetime.date().isoformat()
            result["delivery_time_from"] = delivery_datetime.time().isoformat()
            # Duration is stored in seconds; include both seconds and minutes for clients
            dur_seconds = int(float(getattr(invoice_doc, "custom_delivery_duration", 3600) or 3600))
            result["delivery_duration_seconds"] = dur_seconds
            result["delivery_duration_minutes"] = int(round(dur_seconds / 60))
            # Compute a human-readable label
            try:
                end_dt = frappe.utils.add_to_date(delivery_datetime, seconds=dur_seconds)
                mins = int(round(dur_seconds / 60))
                hrs = mins // 60
                rem = mins % 60
                if hrs > 0 and rem == 0:
                    dur_label = f"{hrs}h"
                elif hrs > 0:
                    dur_label = f"{hrs}h {rem}m"
                else:
                    dur_label = f"{mins}m"
                result["delivery_slot_label"] = f"{delivery_datetime.strftime('%H:%M')} - {end_dt.strftime('%H:%M')} ({dur_label})"
            except Exception:
                pass
            result["delivery_label"] = delivery_datetime.strftime('%A, %B %d, %Y at %I:%M %p')
            print(f"      delivery_datetime: {result['delivery_datetime']}")
            print(f"      delivery_label: {result['delivery_label']}")
        except Exception:
            pass
    
    logger.info(f"Invoice creation successful: {invoice_doc.name}")
    print(f"   ✅ Response prepared:")
    for key, value in result.items():
        print(f"      {key}: {value}")
    
    return result


def _get_company_receivable(company: str) -> str:
    receivable = frappe.get_cached_value("Company", company, "default_receivable_account")
    if receivable:
        return receivable
    receivable = frappe.db.get_value(
        "Account",
        {"company": company, "account_type": "Receivable", "is_group": 0},
        "name",
    )
    if not receivable:
        frappe.throw("Could not determine receivable account for company")
    return receivable


def _ensure_partner_subaccount(parent_receivable: str, company: str, partner_name: str) -> str:
    """Ensure an AR child account named exactly as sales partner exists under company receivable.

    Returns the account name (e.g., "Partner X - CmpAbbr").
    """
    # Parent format like "Accounts Receivable - CmpAbbr"; derive company abbr suffix
    company_abbr = frappe.db.get_value("Company", company, "abbr") or ""
    desired = f"{partner_name} - {company_abbr}".strip()
    if frappe.db.exists("Account", desired):
        return desired

    parent_doc = frappe.get_doc("Account", parent_receivable)
    # Create new leaf account under receivable
    acc = frappe.new_doc("Account")
    acc.account_name = partner_name
    acc.parent_account = parent_doc.name
    acc.company = company
    acc.is_group = 0
    acc.account_type = "Receivable"
    acc.insert(ignore_permissions=True)
    return acc.name


def _maybe_register_online_payment_to_partner(invoice_doc, sales_partner: str | None, payment_type: str | None, logger):
    """When payment_type == 'online', mark invoice paid and allocate to a Payment Entry whose paid_to is
    the Sales Partner subaccount under Receivables. This keeps AR by sales partner while clearing customer AR.
    """
    if not payment_type or str(payment_type).strip().lower() != "online":
        return
    try:
        # Only proceed for submitted invoices with outstanding
        if int(invoice_doc.docstatus) != 1:
            return
        outstanding = float(getattr(invoice_doc, "outstanding_amount", 0) or 0)
        if outstanding <= 0.0001:
            return
        company = invoice_doc.company
        receivable = _get_company_receivable(company)

        # Determine destination account: Sales Partner subaccount under receivable; if no partner, fallback to receivable
        if sales_partner and frappe.db.exists("Sales Partner", sales_partner):
            paid_to = _ensure_partner_subaccount(receivable, company, sales_partner)
        else:
            # Fallback: single consolidated AR account (still clears invoice)
            paid_to = receivable

        try:
            # Create Payment Entry (Receive)
            pe = frappe.new_doc("Payment Entry")
            pe.payment_type = "Receive"
            pe.party_type = "Customer"
            pe.party = invoice_doc.customer
            pe.company = company
            pe.posting_date = frappe.utils.today()
            pe.mode_of_payment = None  # optional; could be set to "Online"
            pe.paid_from = receivable
            pe.paid_to = paid_to
            pe.party_account = receivable
            pe.paid_amount = outstanding
            pe.received_amount = outstanding
            pe.append("references", {
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice_doc.name,
                "due_date": getattr(invoice_doc, "due_date", None),
                "total_amount": float(getattr(invoice_doc, "grand_total", 0) or 0),
                "outstanding_amount": outstanding,
                "allocated_amount": outstanding,
            })
            pe.flags.ignore_permissions = True
            try:
                pe.set_missing_values()
            except AttributeError:
                if not getattr(pe, 'party_account', None):
                    pe.party_account = receivable
            pe.insert(ignore_permissions=True)
            pe.submit()

            print(f"   ✅ Online Payment Entry created: {pe.name} → {paid_to}")
            try:
                logger.info(f"Online Payment Entry created: {pe.name} to {paid_to}")
            except Exception:
                pass
            return
        except Exception as pe_err:
            # Fallback: create a Journal Entry to transfer AR -> partner subaccount and knock off invoice
            print(f"   ⚠️ Payment Entry validation failed, falling back to Journal Entry: {pe_err}")
            try:
                je = frappe.new_doc("Journal Entry")
                je.voucher_type = "Journal Entry"
                je.company = company
                je.posting_date = frappe.utils.today()
                je.title = f"Online Payment – {invoice_doc.name}"
                # Debit partner AR subaccount
                je.append("accounts", {
                    "account": paid_to,
                    "debit_in_account_currency": outstanding,
                    "credit_in_account_currency": 0,
                    "party_type": None,
                    "party": None,
                })
                # Credit customer receivable with reference to SI (to close it)
                je.append("accounts", {
                    "account": receivable,
                    "credit_in_account_currency": outstanding,
                    "debit_in_account_currency": 0,
                    "party_type": "Customer",
                    "party": invoice_doc.customer,
                    "reference_type": "Sales Invoice",
                    "reference_name": invoice_doc.name,
                })
                je.flags.ignore_permissions = True
                je.insert(ignore_permissions=True)
                je.submit()
                print(f"   ✅ Journal Entry created to transfer AR: {je.name} (Deb {paid_to} / Cr {receivable})")
                try:
                    logger.info(f"JE fallback created: {je.name}")
                except Exception:
                    pass
            except Exception as je_err:
                print(f"   ❌ JE fallback failed: {je_err}")
                try:
                    logger.error(f"JE fallback failed: {je_err}")
                except Exception:
                    pass
                # Re-raise original Payment Entry error to be handled by caller
                raise pe_err
    except Exception as e:
        # Surface exception to caller to log, but do not break invoice creation
        raise


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

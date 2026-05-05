"""
Invoice Creation Module for Jarz POS

This module handles the main POS invoice creation logic,
including validation, document creation, and submission.
"""

import frappe
import traceback
from .bundle_processing import process_bundle_for_invoice, validate_bundle_configuration_by_item
from jarz_pos.services import delivery_promotions as _delivery_promotions
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
from jarz_pos.services import delivery_handling as _delivery
from jarz_pos.utils.delivery_utils import add_delivery_charges_to_taxes
from jarz_pos.utils.account_utils import (
    get_company_receivable_account,
    ensure_partner_receivable_subaccount,
    resolve_online_partner_paid_to,
)


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

        # --- Calculate duration from end_datetime (preferred) ---
        duration_set = False
        try:
            raw_end = getattr(frappe, "form_dict", {}).get("delivery_end_datetime")
            if raw_end:
                end_dt = frappe.utils.get_datetime(raw_end)
                duration_seconds = int((end_dt - dt).total_seconds())
                if duration_seconds > 0:
                    invoice_doc.custom_delivery_duration = duration_seconds
                    duration_set = True
        except Exception:
            pass

        if not duration_set:
            # Fallback: explicit delivery_duration param
            try:
                raw_duration = getattr(frappe, "form_dict", {}).get("delivery_duration")
            except Exception:
                raw_duration = None
            parsed_seconds = _parse_duration_to_seconds(raw_duration)
            if parsed_seconds is not None:
                invoice_doc.custom_delivery_duration = parsed_seconds
                duration_set = True

        if not duration_set:
            # Fallback: look up timetable slot_hours for the POS profile
            try:
                pos_profile_name = getattr(invoice_doc, "pos_profile", None)
                if pos_profile_name:
                    slot_hours = frappe.db.get_value(
                        "POS Profile Timetable",
                        {"pos_profile": pos_profile_name},
                        "slot_hours",
                    )
                    if slot_hours:
                        invoice_doc.custom_delivery_duration = int(slot_hours) * 3600
                        duration_set = True
            except Exception:
                pass

        if not duration_set:
            # Final fallback: 1 hour in seconds
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
    pickup: bool | None = None,
    payment_method: str | None = None,
):
    """
    Create POS Sales Invoice using Frappe best practices with comprehensive logging

    Following Frappe/ERPNext best practices:
    - Proper error handling with frappe.throw()
    - Structured logging with frappe.log_error()
    - Document validation before save/submit
    - Handle delivery time slot for scheduled deliveries
    - Proper field setting in correct order
    
    Args:
        payment_method: Payment method - Cash, Instapay, or Mobile Wallet
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

        # Validate payment method if provided
        if payment_method:
            allowed_methods = ["Cash", "Instapay", "Mobile Wallet", "Kashier Card", "Kashier Wallet"]
            if payment_method not in allowed_methods:
                error_msg = f"Invalid payment_method: {payment_method}. Must be one of: {', '.join(allowed_methods)}"
                logger.error(error_msg)
                print(f"   ❌ {error_msg}")
                frappe.throw(error_msg)
            print(f"   ✅ Payment method validated: {payment_method}")

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

        # STEP 6.0: Mark pickup flag if provided
        is_pickup = bool(pickup)
        if is_pickup:
            try:
                # Set the standardized custom_is_pickup field
                invoice_doc.custom_is_pickup = 1
                # Add a remark marker for backward compatibility and visibility
                try:
                    existing = (getattr(invoice_doc, "remarks", "") or "").strip()
                    marker = "[PICKUP]"
                    if marker not in existing:
                        invoice_doc.remarks = (existing + "\n" if existing else "") + marker
                except Exception:
                    pass
                print("   🚏 Pickup mode enabled – shipping suppressed")
            except Exception as _mkpu_err:
                print(f"   ⚠️ Could not mark pickup flag: {_mkpu_err}")

        # Ensure custom_kanban_profile mirrors POS profile at creation time (defensive in addition to hook)
        try:
            if getattr(invoice_doc, "pos_profile", None):
                invoice_doc.custom_kanban_profile = invoice_doc.pos_profile
            else:
                invoice_doc.custom_kanban_profile = None
        except Exception:
            # If custom field missing, don't fail invoice creation
            pass

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

        # STEP 6.3: Set Payment Method (Cash | Instapay | Mobile Wallet)
        if payment_method:
            try:
                invoice_doc.custom_payment_method = payment_method
                print(f"   💳 Payment Method set: {payment_method}")
            except Exception as pm_err:
                print(f"   ⚠️ Could not set Payment Method: {pm_err}")

        # STEP 7: Add Items to Document
        print("\n7️⃣ ADDING ITEMS:")
        add_items_to_invoice(invoice_doc, processed_items, logger)

        # STEP 7.3: Sales Partner Tax Suppression Rule
        # Business Rule (2025-09): If an invoice has a Sales Partner, it must have NO rows in
        # the "Sales Taxes and Charges" table. We therefore:
        #   1. Clear any default taxes added by POS Profile / Tax Template.
        #   2. Skip adding Shipping Income (territory delivery income) rows.
        #   3. Skip adding legacy delivery_charges rows.
        partner_tax_suppressed = False
        if getattr(invoice_doc, "sales_partner", None):
            try:
                existing_taxes = len(getattr(invoice_doc, "taxes", []) or [])
                if existing_taxes:
                    print(f"\n7️⃣.3️⃣ SALES PARTNER MODE: Clearing {existing_taxes} pre-populated tax rows")
                # Reset taxes child table fully (use set to ensure ORM awareness)
                try:
                    invoice_doc.set("taxes", [])
                except Exception:
                    invoice_doc.taxes = []  # fallback
                partner_tax_suppressed = True
                print("   ✅ Sales Partner present → all tax rows suppressed")
            except Exception as clear_err:
                print(f"   ⚠️ Could not clear existing taxes: {clear_err}")
        
        # Determine if cart includes any free-shipping bundle to suppress shipping income insertion
        free_shipping_waived = False
        try:
            # Inspect processed_items for any bundle link and query Jarz Bundle.free_shipping
            bundle_candidates = []
            for it in processed_items:
                bcode = it.get('bundle_code') or it.get('parent_bundle')
                if bcode and bcode not in bundle_candidates:
                    bundle_candidates.append(bcode)
                # Also consider ERPNext parent items that map to a bundle
                code = it.get('item_code')
                if code and not bcode:
                    try:
                        rows = frappe.get_all('Jarz Bundle', filters={'erpnext_item': code}, pluck='name')
                        for r in rows:
                            if r not in bundle_candidates:
                                bundle_candidates.append(r)
                    except Exception:
                        pass
            if bundle_candidates:
                cols = set(frappe.db.get_table_columns('Jarz Bundle') or [])
                if 'free_shipping' in cols:
                    any_free = frappe.get_all('Jarz Bundle', filters={'name': ['in', bundle_candidates], 'free_shipping': 1}, pluck='name')
                    free_shipping_waived = bool(any_free)
        except Exception:
            free_shipping_waived = False

        delivery_promotion = _delivery_promotions.DeliveryPromotionDecision()
        try:
            delivery_promotion = _delivery_promotions.resolve_delivery_promotion(
                invoice_doc,
                customer_doc=customer_doc,
                pos_profile=pos_profile,
                channel="flutter",
                is_pickup=bool(pickup),
            )
            if delivery_promotion.matched:
                _delivery_promotions.apply_delivery_promotion_audit(invoice_doc, delivery_promotion)
                print(
                    "   🎯 Delivery promotion matched: "
                    f"{delivery_promotion.rule_name} "
                    f"(merchandise_subtotal={delivery_promotion.merchandise_subtotal:.2f})"
                )
        except Exception as promo_err:
            print(f"   ⚠️ Delivery promotion resolution failed: {promo_err}")

        suppress_shipping_income = (
            partner_tax_suppressed
            or free_shipping_waived
            or bool(pickup)
            or delivery_promotion.suppress_shipping_income
        )
        suppress_legacy_delivery_charges = (
            partner_tax_suppressed
            or free_shipping_waived
            or bool(pickup)
            or delivery_promotion.suppress_legacy_delivery_charges
        )

        # STEP 7.4: Inject Shipping (Territory Delivery Income) as Actual tax row
        if not suppress_shipping_income:
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
        else:
            print("\n7️⃣.4️⃣ SKIPPED: Shipping income suppressed")
            if partner_tax_suppressed:
                print("   🤝 Sales Partner tax suppression active")
            if free_shipping_waived:
                print("   🚚 Free-shipping bundle detected")
            if bool(pickup):
                print("   🚏 Pickup mode enabled")
            if delivery_promotion.suppress_shipping_income:
                print(f"   🎯 Promotion matched: {delivery_promotion.rule_name}")

        # STEP 7.5: Add Delivery Charges (legacy param based)
        if delivery_charges and not suppress_legacy_delivery_charges:
            print("\n7️⃣.5️⃣ ADDING DELIVERY CHARGES:")
            add_delivery_charges_to_taxes(invoice_doc, delivery_charges, "Delivery Charges")
        else:
            print("\n7️⃣.5️⃣ SKIPPED: Delivery charges suppressed or not provided")
            if not delivery_charges:
                print("   ℹ️ No legacy delivery charges provided")
            if partner_tax_suppressed:
                print("   🤝 Sales Partner tax suppression active")
            if free_shipping_waived:
                print("   🚚 Free-shipping bundle detected")
            if bool(pickup):
                print("   🚏 Pickup mode enabled")
            if delivery_promotion.suppress_legacy_delivery_charges:
                print(f"   🎯 Promotion matched: {delivery_promotion.rule_name}")

        # STEP 8: Validate and Calculate Document
        print("\n8️⃣ DOCUMENT VALIDATION:")
        _validate_and_calculate_document(invoice_doc, logger)

        # STEP 8.1: Keep POS Sales Invoices accounting-only for all payment flows.
        # Business Rule: Stock movement must happen via Delivery Note on the delivery flow,
        # so Sales Invoice creation must never reduce stock directly.
        try:
            if hasattr(invoice_doc, 'update_stock'):
                invoice_doc.update_stock = 0  # int flag expected by ERPNext
                print("   🚚 Stock update disabled on POS Sales Invoice")
            else:
                print("   ℹ️ 'update_stock' field not present on Sales Invoice; skipping suppression")
        except Exception as _ustk_err:
            print(f"   ⚠️ Could not suppress stock update: {_ustk_err}")

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
        try:
            result["pickup"] = bool(pickup)
        except Exception:
            pass

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
        
        selected_items = item_data.get("selected_items") if hasattr(item_data, "get") else None

        if is_bundle:
            # Process bundle item
            bundle_items = _process_bundle_item(
                item_code,
                qty,
                rate,
                pos_profile,
                logger,
                selected_items=selected_items,
            )
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


def _process_bundle_item(item_code, qty, rate, pos_profile, logger, selected_items=None):
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
        bundle_items = process_bundle_for_invoice(
            item_code,
            qty,
            selected_items=selected_items,
        )
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
    invoice_doc.reload()

    # Fetch new fields
    date_attr = getattr(invoice_doc, "custom_delivery_date", None)
    time_from_attr = getattr(invoice_doc, "custom_delivery_time_from", None)
    duration_attr = getattr(invoice_doc, "custom_delivery_duration", None)

    print(f"   📊 custom_delivery_date: {date_attr}")
    print(f"   📊 custom_delivery_time_from: {time_from_attr}")
    print(f"   📊 custom_delivery_duration: {duration_attr}")

    if not (date_attr and time_from_attr and duration_attr):
        # Attempt to apply from provided delivery_datetime again
        try:
            _apply_delivery_slot_fields(invoice_doc, delivery_datetime)
            invoice_doc.save(ignore_permissions=True)
            # Reload the document after save to sync timestamps
            invoice_doc.reload()
            # Reload values
            date_attr = getattr(invoice_doc, "custom_delivery_date", None)
            time_from_attr = getattr(invoice_doc, "custom_delivery_time_from", None)
            duration_attr = getattr(invoice_doc, "custom_delivery_duration", None)
            print(f"   ✅ Re-saved with delivery slot fields")
        except Exception as correction_error:
            print(f"   ❌ Could not set delivery slot fields: {str(correction_error)}")
            logger.warning(f"Delivery slot fields could not be set: {str(correction_error)}")


def _submit_document(invoice_doc, logger):
    """Submit the invoice document."""
    logger.debug("Submitting document")
    try:
        # Reload document to get fresh state from database
        # Use reload() instead of get_doc() to avoid timestamp mismatch
        invoice_doc.reload()
        
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
    # Detect if tax suppression was applied (flag set earlier) or by heuristic
    partner_tax_suppressed = False
    try:
        partner_tax_suppressed = bool(getattr(getattr(invoice_doc, "flags", object()), "partner_tax_suppressed", False))
    except Exception:
        partner_tax_suppressed = False
    if not partner_tax_suppressed:
        # Heuristic: sales_partner present AND no taxes rows
        try:
            if getattr(invoice_doc, "sales_partner", None) and len(getattr(invoice_doc, "taxes", []) or []) == 0:
                partner_tax_suppressed = True
        except Exception:
            pass

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
    "company": invoice_doc.company,
    "partner_tax_suppressed": partner_tax_suppressed,
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
                result["delivery_slot_label"] = f"{delivery_datetime.strftime('%I:%M %p')} - {end_dt.strftime('%I:%M %p')} ({dur_label})"
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
        receivable = get_company_receivable_account(company)

        # Determine destination account centrally
        paid_to = resolve_online_partner_paid_to(company, sales_partner)

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
            try:
                _delivery.sales_partner_paid_out_for_delivery(invoice_doc.name, payment_mode="Online")
            except Exception as sp_err:
                print(f"   ⚠️ Sales Partner paid OFD hook failed: {sp_err}")
                try:
                    logger.warning(f"Sales Partner paid OFD hook failed: {sp_err}")
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
                try:
                    _delivery.sales_partner_paid_out_for_delivery(invoice_doc.name, payment_mode="Online")
                except Exception as sp_err:
                    print(f"   ⚠️ Sales Partner paid OFD hook (JE fallback) failed: {sp_err}")
                    try:
                        logger.warning(f"Sales Partner paid OFD hook (JE fallback) failed: {sp_err}")
                    except Exception:
                        pass
                return
            except Exception as je_err:
                print(f"   ❌ JE fallback failed: {je_err}")
                try:
                    logger.error(f"JE fallback failed: {je_err}")
                except Exception:
                    pass
                # Re-raise original Payment Entry error to be handled by caller
                raise pe_err
    except Exception:
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

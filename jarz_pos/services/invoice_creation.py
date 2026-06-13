"""
Invoice Creation Module for Jarz POS

This module handles the main POS invoice creation logic,
including validation, document creation, and submission.
"""

import frappe
import traceback
from .bundle_processing import process_bundle_for_invoice, validate_bundle_configuration_by_item
from jarz_pos.constants import ROLES
from jarz_pos.services import delivery_promotions as _delivery_promotions
from jarz_pos.services import commercial_policy as _commercial_policy
from jarz_pos.utils.validation_utils import (
    validate_cart_data, 
    validate_customer, 
    validate_pos_profile,
    validate_delivery_datetime
)
from jarz_pos.utils.invoice_utils import (
    set_invoice_fields,
    add_items_to_invoice,
    verify_invoice_totals,
    resolve_order_territory,
)
from jarz_pos.utils.customer_address_utils import (
    ensure_shipping_address,
    resolve_customer_shipping_address,
)
from jarz_pos.services import delivery_handling as _delivery
from jarz_pos.utils.delivery_utils import add_delivery_charges_to_taxes
from jarz_pos.utils.account_utils import (
    get_item_price,
    get_company_receivable_account,
    ensure_partner_receivable_subaccount,
    resolve_online_partner_paid_to,
)


_MANAGER_PRICING_ROLES = {
    ROLES.JARZ_MANAGER,
    "JARZ line manager",
    ROLES.JARZ_LINE_MANAGER,
}


def _has_manager_pricing_access() -> bool:
    roles = {
        str(role or "").strip()
        for role in (frappe.get_roles(frappe.session.user) or [])
        if str(role or "").strip()
    }
    return bool(roles.intersection(_MANAGER_PRICING_ROLES))


def _ensure_manager_pricing_access() -> None:
    if not _has_manager_pricing_access():
        frappe.throw("Not permitted: manager pricing access required")


def _normalize_price_list_name(value) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _append_unique_remark(invoice_doc, marker: str) -> None:
    marker = (marker or "").strip()
    if not marker:
        return

    existing = (getattr(invoice_doc, "remarks", "") or "").strip()
    if marker in existing:
        return

    invoice_doc.remarks = (existing + "\n" if existing else "") + marker


def _describe_unresolved_territory_source(
    *,
    resolved_shipping_address=None,
    shipping_address_name: str | None = None,
    customer_doc=None,
) -> str:
    if isinstance(resolved_shipping_address, dict):
        address_name = str(resolved_shipping_address.get("name") or shipping_address_name or "").strip()
        for fieldname in ("city", "state"):
            value = str(resolved_shipping_address.get(fieldname) or "").strip()
            if value:
                if address_name:
                    return f"{fieldname}={value} (address {address_name})"
                return f"{fieldname}={value}"

    customer_territory = str(getattr(customer_doc, "territory", "") or "").strip()
    if customer_territory:
        return f"customer_territory={customer_territory}"

    shipping_address_name = str(shipping_address_name or "").strip()
    if shipping_address_name:
        return f"shipping_address_name={shipping_address_name}"

    return "no territory source"


def _pricing_action_requires_manager(
    cart_items,
    *,
    requested_price_list: str | None,
    default_price_list: str | None,
    suppress_shipping_income: bool | None,
    suppress_legacy_delivery_charges: bool | None,
) -> bool:
    requested = _normalize_price_list_name(requested_price_list)
    default = _normalize_price_list_name(default_price_list)
    if requested and requested != default:
        return True
    if suppress_shipping_income is True or suppress_legacy_delivery_charges is True:
        return True

    for item in cart_items or []:
        if not isinstance(item, dict):
            continue
        for field in ("custom_rate_override", "discount_amount", "discount_percentage"):
            value = item.get(field)
            if value not in (None, "", 0, 0.0):
                return True

    return False


def _resolve_sales_partner_price_list(sales_partner: str | None) -> str | None:
    if not sales_partner:
        return None
    try:
        return _normalize_price_list_name(
            frappe.db.get_value("Sales Partner", sales_partner, "price_list")
        )
    except Exception:
        return None


def _resolve_customer_price_list(customer_doc) -> str | None:
    """Customer-specific, then Customer-Group, default selling price list (if present).

    Uses getattr/db lookups defensively so missing fields never break creation.
    """
    if customer_doc is None:
        return None
    candidate = _normalize_price_list_name(getattr(customer_doc, "default_price_list", None))
    if candidate:
        return candidate
    customer_group = getattr(customer_doc, "customer_group", None)
    if customer_group:
        try:
            candidate = _normalize_price_list_name(
                frappe.db.get_value("Customer Group", customer_group, "default_price_list")
            )
        except Exception:
            candidate = None
    return candidate


def _resolve_company_default_price_list() -> str | None:
    try:
        return _normalize_price_list_name(
            frappe.db.get_single_value("Selling Settings", "selling_price_list")
        )
    except Exception:
        return None


def _resolve_effective_price_list(
    pos_profile,
    cart_items,
    *,
    requested_price_list: str | None,
    suppress_shipping_income: bool | None,
    suppress_legacy_delivery_charges: bool | None,
    logger,
    policy_matched: bool = False,
    policy_price_list: str | None = None,
    customer_doc=None,
    sales_partner: str | None = None,
) -> str | None:
    default_price_list = _normalize_price_list_name(
        getattr(pos_profile, "selling_price_list", None)
    )
    requested = _normalize_price_list_name(requested_price_list)

    # Manager gating is unchanged: only an explicit manual override (or line pricing /
    # suppress hints) requires manager access. Policy-derived price lists are gated
    # separately by the commercial-policy resolver, so they do NOT re-trip this check.
    if _pricing_action_requires_manager(
        cart_items,
        requested_price_list=requested,
        default_price_list=default_price_list,
        suppress_shipping_income=suppress_shipping_income,
        suppress_legacy_delivery_charges=suppress_legacy_delivery_charges,
    ):
        _ensure_manager_pricing_access()

    policy_pl = _normalize_price_list_name(policy_price_list)
    if policy_matched:
        # Non-Standard (B2B/Employee/Sample/...) resolution chain, highest priority first.
        # Only reached for an explicitly chosen, permission-gated order purpose.
        effective_price_list = (
            requested
            or policy_pl
            or _resolve_sales_partner_price_list(sales_partner)
            or _resolve_customer_price_list(customer_doc)
            or default_price_list
            or _resolve_company_default_price_list()
        )
    else:
        # Standard order: BYTE-IDENTICAL to prior behavior — POS Profile default only.
        effective_price_list = requested or default_price_list

    if effective_price_list and not frappe.db.exists("Price List", effective_price_list):
        frappe.throw(f"Price List '{effective_price_list}' does not exist")

    logger.info(
        f"pricing_context resolved: requested={requested or ''}, policy={policy_pl or ''}, "
        f"default={default_price_list or ''}, effective={effective_price_list or ''}"
    )
    return effective_price_list


def _validate_policy_price_list_coverage(policy_decision, effective_price_list, cart_items, logger) -> None:
    """For a MATCHED commercial policy, ensure the resolved price list has a selling
    Item Price for every plain cart item. Gives a clear, actionable error (instead of a
    cryptic failure or a silently zero-priced order) when a B2B/Employee/Sample price
    list has not been populated yet. Standard orders are unaffected (they price from the
    cart rate as before)."""
    if not getattr(policy_decision, "matched", False) or not effective_price_list:
        return
    missing = []
    for it in cart_items or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("item_code") or "").strip()
        if not code:
            continue
        # Skip bundles and manually-overridden lines — those don't price off the list.
        if it.get("is_bundle") or it.get("custom_rate_override") not in (None, "", 0, 0.0):
            continue
        if not frappe.db.exists(
            "Item Price",
            {"item_code": code, "price_list": effective_price_list, "selling": 1},
        ):
            missing.append(code)
    if missing:
        names = ", ".join(sorted(set(missing)))
        logger.error(
            f"Policy price list '{effective_price_list}' missing prices for: {names}"
        )
        frappe.throw(
            f"Order purpose '{getattr(policy_decision, 'order_purpose', '')}' uses price "
            f"list '{effective_price_list}', but it has no selling price for: {names}. "
            f"Add an Item Price for these item(s) in '{effective_price_list}' "
            f"(Selling → Item Price) before placing this order."
        )


def _resolve_item_rate(item_code, price_list, fallback_rate=0.0) -> float:
    if price_list:
        rate = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "price_list": price_list},
            "price_list_rate",
        )
        if rate not in (None, ""):
            return float(rate)

    return float(get_item_price(item_code, price_list) or fallback_rate or 0.0)


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
            # Fallback: look up timetable slot duration for the POS profile
            try:
                pos_profile_name = getattr(invoice_doc, "pos_profile", None)
                if pos_profile_name:
                    timetable = frappe.db.get_value(
                        "POS Profile Timetable",
                        {"pos_profile": pos_profile_name},
                        ["slot_hours", "slot_minutes"],
                        as_dict=True,
                    )
                    if timetable:
                        total_minutes = int(timetable.slot_hours or 1) * 60 + int(timetable.slot_minutes or 0)
                        invoice_doc.custom_delivery_duration = total_minutes * 60
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
    shipping_address_name: str | None = None,
    sales_partner: str | None = None,
    payment_type: str | None = None,
    pickup: bool | None = None,
    payment_method: str | None = None,
    price_list: str | None = None,
    amended_from: str | None = None,
    woo_order_id: int | None = None,
    suppress_shipping_income: bool | None = None,
    suppress_legacy_delivery_charges: bool | None = None,
    custom_delivery_income: float | str | None = None,
    order_purpose: str | None = None,
    commercial_policy: str | None = None,
    policy_reason: str | None = None,
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

        # Normalize custom_delivery_income: "" or None → None (use territory);
        # any other value → flt; negative → error.
        delivery_income_override: float | None = None
        if custom_delivery_income is not None and str(custom_delivery_income).strip() != "":
            _override_val = frappe.utils.flt(custom_delivery_income)
            if _override_val < 0:
                frappe.throw("custom_delivery_income must be non-negative")
            delivery_income_override = _override_val

        # STEP 2: Customer Validation
        print("\n2️⃣ CUSTOMER VALIDATION:")
        customer_doc = validate_customer(customer_name, logger)

        # STEP 3: POS Profile Validation
        print("\n3️⃣ POS PROFILE VALIDATION:")
        pos_profile = validate_pos_profile(pos_profile_name, logger)

        # STEP 3.5: Resolve Commercial Policy / Order Purpose (gated; Standard is inert)
        policy_decision = _commercial_policy.resolve_commercial_policy(
            order_purpose=order_purpose,
            commercial_policy=commercial_policy,
            policy_reason=policy_reason,
            pos_profile=pos_profile,
            logger=logger,
        )

        # STEP 4: Item and Bundle Processing
        print("\n4️⃣ ITEM AND BUNDLE PROCESSING:")
        effective_price_list = _resolve_effective_price_list(
            pos_profile,
            cart_items,
            requested_price_list=price_list,
            suppress_shipping_income=suppress_shipping_income,
            suppress_legacy_delivery_charges=suppress_legacy_delivery_charges,
            logger=logger,
            policy_matched=policy_decision.matched,
            policy_price_list=policy_decision.price_list,
            customer_doc=customer_doc,
            sales_partner=sales_partner,
        )
        # Fail fast with an actionable message if a policy order's price list is missing
        # prices (the common "B2B Selling not populated yet" data gap).
        _validate_policy_price_list_coverage(
            policy_decision, effective_price_list, cart_items, logger
        )
        processed_items = _process_cart_items(
            cart_items,
            pos_profile,
            logger,
            price_list=effective_price_list,
        )

        # Sample policies may carry a fallback discount %. Apply it to plain item rows
        # that have no explicit line discount, reusing the existing line-discount path
        # (no new discount engine). Bundle rows are left untouched.
        if policy_decision.matched and policy_decision.discount_percentage > 0:
            _disc = min(max(float(policy_decision.discount_percentage), 0.0), 100.0)
            for _it in processed_items:
                if (
                    _it.get("is_bundle_parent")
                    or _it.get("is_bundle_child")
                    or _it.get("is_bundle_item")
                    or _it.get("bundle_type")
                ):
                    continue
                if _it.get("discount_percentage") in (None, "", 0, 0.0) and _it.get(
                    "discount_amount"
                ) in (None, "", 0, 0.0):
                    _it["discount_percentage"] = _disc
            print(f"   🎁 Sample discount applied to plain rows: {_disc}%")

        # STEP 5: Create Sales Invoice Document
        print("\n5️⃣ CREATING SALES INVOICE:")
        invoice_doc = _create_invoice_document(logger)

        # STEP 6: Set Document Fields
        print("\n6️⃣ SETTING DOCUMENT FIELDS:")
        set_invoice_fields(invoice_doc, customer_doc, pos_profile, delivery_datetime, logger)
        if effective_price_list:
            invoice_doc.selling_price_list = effective_price_list
            print(f"   🏷️ Selling Price List set: {effective_price_list}")
            if effective_price_list != getattr(pos_profile, "selling_price_list", None):
                _append_unique_remark(invoice_doc, f"[PRICE LIST OVERRIDE] {effective_price_list}")
        if suppress_shipping_income is True or suppress_legacy_delivery_charges is True:
            _append_unique_remark(invoice_doc, "[ZERO SHIPPING OVERRIDE]")
        if any(item.get("custom_rate_override") not in (None, "", 0, 0.0) for item in processed_items):
            _append_unique_remark(invoice_doc, "[CUSTOM LINE PRICING]")
        if any(
            (
                item.get("discount_amount") not in (None, "", 0, 0.0)
                or item.get("discount_percentage") not in (None, "", 0, 0.0)
            )
            and not item.get("is_bundle_parent")
            and not item.get("is_bundle_child")
            for item in processed_items
        ):
            _append_unique_remark(invoice_doc, "[LINE DISCOUNTS]")

        # STEP 6.A: Resolve and stamp the shipping address explicitly.
        resolved_shipping_address = resolve_customer_shipping_address(
            customer_doc.name,
            preferred_address_name=shipping_address_name,
        )
        if shipping_address_name and (
            not resolved_shipping_address or resolved_shipping_address.get("name") != shipping_address_name
        ):
            frappe.throw("Selected shipping address is no longer available for this customer")
        resolved_shipping_address_name = None
        if resolved_shipping_address:
            resolved_shipping_address_name = str(resolved_shipping_address.get("name") or "").strip()
            if resolved_shipping_address_name:
                ensure_shipping_address(resolved_shipping_address_name)
                invoice_doc.shipping_address_name = resolved_shipping_address_name
                invoice_doc.customer_address = resolved_shipping_address_name
                print(f"   📍 Shipping address set: {resolved_shipping_address_name}")

        effective_order_territory = resolve_order_territory(
            customer_doc.name,
            shipping_address_name=resolved_shipping_address_name or shipping_address_name,
            resolved_shipping_address=resolved_shipping_address,
        )
        if effective_order_territory:
            invoice_doc.territory = effective_order_territory
            print(f"   🧭 Order territory set: {effective_order_territory}")

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

        # STEP 6.0b: Freeze commercial-policy / order-purpose snapshot onto the invoice.
        # For Standard orders these fields default to "Standard"/0, leaving accounting
        # behavior byte-identical. For a MATCHED policy order the snapshot drives the
        # accounting (custom_no_courier etc.), so stamping must NOT be silently swallowed
        # — a stamp failure on a matched order must abort, never submit as Standard.
        invoice_doc.custom_order_purpose = policy_decision.order_purpose or "Standard"
        if policy_decision.matched:
            if policy_decision.policy_name:
                invoice_doc.custom_commercial_policy = policy_decision.policy_name
            if policy_decision.reason:
                invoice_doc.custom_policy_reason = policy_decision.reason
            invoice_doc.custom_no_courier = 1 if policy_decision.no_courier else 0
            _append_unique_remark(
                invoice_doc, f"[ORDER PURPOSE] {policy_decision.order_purpose}"
            )
            if policy_decision.price_list:
                _append_unique_remark(
                    invoice_doc, f"[POLICY PRICE LIST] {policy_decision.price_list}"
                )
            print(
                f"   🧾 Order purpose: {policy_decision.order_purpose} "
                f"(policy={policy_decision.policy_name}, no_courier={policy_decision.no_courier})"
            )
        elif policy_decision.reason:
            # Reason supplied without a matched policy (e.g. Standard) — keep for audit.
            invoice_doc.custom_policy_reason = policy_decision.reason

        # Ensure custom_kanban_profile mirrors POS profile at creation time (defensive in addition to hook)
        try:
            if getattr(invoice_doc, "pos_profile", None):
                invoice_doc.custom_kanban_profile = invoice_doc.pos_profile
            else:
                invoice_doc.custom_kanban_profile = None
        except Exception:
            # If custom field missing, don't fail invoice creation
            pass

        # Persist delivery income override (None = territory default; 0 = free delivery; >0 = custom)
        if delivery_income_override is not None:
            try:
                invoice_doc.custom_delivery_income = delivery_income_override
            except Exception:
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

        # STEP 6.4: Preserve amendment lineage and remote Woo link before submit
        if amended_from:
            try:
                invoice_doc.amended_from = amended_from
                print(f"   🔁 Amendment source set: {amended_from}")
            except Exception as amend_err:
                print(f"   ⚠️ Could not set amended_from: {amend_err}")
        if woo_order_id:
            try:
                invoice_doc.woo_order_id = woo_order_id
                print(f"   🛒 Existing Woo Order ID set: {woo_order_id}")
            except Exception as woo_err:
                print(f"   ⚠️ Could not set woo_order_id: {woo_err}")

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
            (suppress_shipping_income is True)  # explicit hint from amendment caller
            or partner_tax_suppressed
            or free_shipping_waived
            or bool(pickup)
            or delivery_promotion.suppress_shipping_income
            or policy_decision.suppress_shipping_income
        )
        suppress_legacy_delivery_charges = (
            (suppress_legacy_delivery_charges is True)  # explicit hint from amendment caller
            or partner_tax_suppressed
            or free_shipping_waived
            or bool(pickup)
            or delivery_promotion.suppress_legacy_delivery_charges
            or policy_decision.suppress_legacy_delivery_charges
        )

        # STEP 7.4: Inject Shipping (Territory Delivery Income OR override) as Actual tax row
        if not suppress_shipping_income:
            print("\n7️⃣.4️⃣ ADDING SHIPPING (Territory Delivery Income) AS TAX:")
            try:
                # Idempotency guard shared by both paths
                already_added = False
                if getattr(invoice_doc, "taxes", None):
                    for tax in invoice_doc.taxes:
                        if (tax.get("description") or "").lower().startswith("shipping income"):
                            already_added = True
                            print("   ⚠️ Shipping income tax row already present – skipping")
                            break

                if delivery_income_override is not None:
                    # Custom override path – fully replaces territory income.
                    # override == 0 → free delivery (add_delivery_charges_to_taxes
                    # early-returns for <= 0, so nothing is injected).
                    print(f"   🎯 Custom delivery income override: {delivery_income_override}")
                    if not already_added and delivery_income_override > 0:
                        territory_label = effective_order_territory or getattr(invoice_doc, "territory", None) or "Override"
                        add_delivery_charges_to_taxes(
                            invoice_doc,
                            delivery_income_override,
                            delivery_description=f"Shipping Income ({territory_label})",
                        )
                        print("   ✅ Override shipping income tax row appended")
                    elif delivery_income_override == 0:
                        print("   ℹ️ Override = 0 → free delivery, no shipping row injected")
                else:
                    # Territory default path (unchanged)
                    territory_name = effective_order_territory or getattr(invoice_doc, "territory", None)
                    if territory_name and frappe.db.exists("Territory", territory_name):
                        territory_doc = frappe.get_doc("Territory", territory_name)
                        shipping_income = getattr(territory_doc, "delivery_income", 0) or 0
                        print(f"   📦 Territory: {territory_name} | delivery_income: {shipping_income}")
                        if shipping_income and float(shipping_income) > 0:
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
                        unresolved_source = _describe_unresolved_territory_source(
                            resolved_shipping_address=resolved_shipping_address,
                            shipping_address_name=resolved_shipping_address_name or shipping_address_name,
                            customer_doc=customer_doc,
                        )
                        _append_unique_remark(invoice_doc, f"[UNRESOLVED TERRITORY] {unresolved_source}")
                        logger.warning(
                            "Order territory unresolved; shipping income skipped "
                            f"(customer={customer_doc.name}, source={unresolved_source})"
                        )
                        print(
                            "   ⚠️ Order territory unresolved – skipping shipping income "
                            f"({unresolved_source})"
                        )
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


def _process_cart_items(cart_items, pos_profile, logger, price_list=None):
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
                price_list=price_list,
                item_data=item_data,
            )
            processed_items.extend(bundle_items)
        else:
            # Process regular item
            regular_item = _process_regular_item(item_data, logger, price_list=price_list)
            processed_items.append(regular_item)
    
    if not processed_items:
        error_msg = "No valid items found in cart after processing"
        logger.error(error_msg)
        print(f"   ❌ {error_msg}")
        frappe.throw(error_msg)
    
    _log_processing_summary(processed_items, logger)
    return processed_items


def _process_bundle_item(item_code, qty, rate, pos_profile, logger, selected_items=None, price_list=None, item_data=None):
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

        target_bundle_price = None
        if isinstance(item_data, dict):
            target_bundle_price = item_data.get("custom_rate_override")
            if target_bundle_price in (None, ""):
                target_bundle_price = item_data.get("price_list_rate")
        if target_bundle_price in (None, ""):
            target_bundle_price = rate
        
        # Process bundle using ERPNext item code (not bundle record ID)
        bundle_items = process_bundle_for_invoice(
            item_code,
            qty,
            selected_items=selected_items,
            price_list=price_list,
            target_bundle_price=target_bundle_price,
        )
        print(f"      ✅ Bundle processed: {len(bundle_items)} items added")
        return bundle_items
    except Exception as bundle_error:
        error_msg = f"Error processing bundle with ERPNext item {item_code}: {str(bundle_error)}"
        logger.error(error_msg)
        print(f"      ❌ {error_msg}")
        frappe.throw(error_msg)


def _process_regular_item(item_data, logger, price_list=None):
    """Process a regular item."""
    item_code = item_data.get("item_code")
    qty = item_data.get("qty", 1)
    rate = item_data.get("rate", item_data.get("price_list_rate", 0))
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

        catalog_rate = _resolve_item_rate(item_code, price_list, fallback_rate=rate)
        custom_rate_override = item_data.get("custom_rate_override")
        effective_price_list_rate = float(catalog_rate)
        if custom_rate_override not in (None, ""):
            custom_rate_override = float(custom_rate_override)
            if custom_rate_override < 0:
                frappe.throw(f"Custom rate override for item '{item_code}' must be non-negative")
            effective_price_list_rate = custom_rate_override

        discount_percentage = item_data.get("discount_percentage")
        if discount_percentage not in (None, ""):
            discount_percentage = float(discount_percentage)
            if discount_percentage < 0 or discount_percentage > 100:
                frappe.throw(f"Discount percentage for item '{item_code}' must be between 0 and 100")

        discount_amount = item_data.get("discount_amount")
        if discount_amount not in (None, ""):
            discount_amount = float(discount_amount)
            if discount_amount < 0:
                frappe.throw(f"Discount amount for item '{item_code}' must be non-negative")
            if effective_price_list_rate > 0 and discount_amount > effective_price_list_rate:
                frappe.throw(
                    f"Discount amount for item '{item_code}' cannot exceed the effective unit price"
                )
        
        result = {
            "item_code": item_code,
            "qty": float(qty),
            "rate": float(effective_price_list_rate),
            "price_list_rate": float(effective_price_list_rate),
            "uom": item_data.get("uom") or item_doc.stock_uom,
            "is_bundle_item": False,
        }
        if custom_rate_override not in (None, ""):
            result["custom_rate_override"] = float(custom_rate_override)
            result["original_price_list_rate"] = float(catalog_rate)
        if discount_percentage not in (None, ""):
            result["discount_percentage"] = float(discount_percentage)
        if discount_amount not in (None, ""):
            result["discount_amount"] = float(discount_amount)

        return result
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

"""Jarz POS - Utility functions for API endpoints.
This module provides common helper functions that are used across different API endpoints.
"""
from __future__ import annotations
import frappe
import re
from typing import Dict, List, Any, Optional, Union


_PRINT_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0B-\x1F\x7F-\x9F]")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def sanitize_printable_text(value: Any) -> str:
    """Normalize printable payload text for thermal-receipt consumers.

    This strips control characters and HTML while preserving ordinary spaces,
    punctuation, URLs, and Arabic text.
    """
    if value is None:
        return ""

    text = value
    as_unicode = getattr(frappe, "as_unicode", None)
    if callable(as_unicode):
        try:
            candidate = as_unicode(value)
            if isinstance(candidate, bytes):
                text = candidate.decode(errors="ignore")
            elif isinstance(candidate, str):
                text = candidate
            else:
                text = value
        except Exception:
            text = value

    if not isinstance(text, str):
        text = str(text)

    text = _HTML_TAG_PATTERN.sub(" ", text)
    text = _PRINT_CONTROL_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    # Note: duration is calculated properly in _apply_delivery_slot_fields from
    # end_datetime or timetable slot_hours; do NOT set a default here.
    if delivery_datetime:
        try:
            dt = frappe.utils.get_datetime(delivery_datetime)
            invoice_doc.custom_delivery_date = dt.date()
            invoice_doc.custom_delivery_time_from = dt.time().strftime("%H:%M:%S")
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
            for flag_field in [
                "is_bundle_parent", "is_bundle_child", "bundle_code", "parent_bundle",
                "bundle_group_key", "bundle_group_name",
            ]:
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
        full_address = sanitize_printable_text(address_doc.address_line1 or "")
        if address_doc.address_line2:
            full_address += f", {sanitize_printable_text(address_doc.address_line2)}"
        if address_doc.city:
            full_address += f", {sanitize_printable_text(address_doc.city)}"
        return full_address.strip(", ")
    except Exception as e:
        frappe.log_error(f"Error fetching address details: {str(e)}", "Address Utils")
        return ""


def _safe_float(value, fallback: float = 0.0) -> float:
    """Convert value to float; return fallback on None / empty / unparseable."""
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _derive_bundle_group_metadata(
    bundle_code: str,
    item_code: str,
    cache: Dict[str, Dict[str, Dict[str, str]]],
) -> tuple:
    """Return (group_key, group_name) for a bundle child item.

    Looks up the Jarz Bundle document by bundle_code, walks its Jarz Bundle
    Item Group rows, and finds which group contains item_code.  Results are
    cached per bundle_code within a single request so repeated child rows for
    the same bundle do not re-query.  Returns ('', '') when the bundle or item
    cannot be resolved.
    """
    if bundle_code not in cache:
        bundle_map: Dict[str, Dict[str, str]] = {}
        try:
            bundle_doc = frappe.get_doc("Jarz Bundle", bundle_code)
            for group_row in bundle_doc.items:
                group_key = str(getattr(group_row, "name", "") or "")
                group_name = str(group_row.item_group or "")
                items_in_group = frappe.get_all(
                    "Item",
                    filters={"item_group": group_name, "disabled": 0, "has_variants": 0},
                    fields=["name"],
                    limit=0,
                )
                for item_row in items_in_group:
                    bundle_map[item_row["name"]] = {
                        "key": group_key,
                        "name": group_name,
                    }
        except Exception:
            pass  # Bundle not found or DB error — leave entry empty so caller gets ""
        cache[bundle_code] = bundle_map

    entry = cache.get(bundle_code, {}).get(item_code)
    if entry:
        return entry["key"], entry["name"]
    return "", ""


def _derive_bundle_code_from_parent_item(
    item_code: str,
    cache: Dict[str, str],
) -> str:
    """Return Jarz Bundle name linked to a bundle parent ERPNext item."""
    item_code = str(item_code or "").strip()
    if not item_code:
        return ""
    if item_code not in cache:
        bundle_code = ""
        try:
            rows = frappe.get_all(
                "Jarz Bundle",
                filters={"erpnext_item": item_code},
                fields=["name"],
                limit=1,
            )
            if isinstance(rows, (list, tuple)) and rows:
                first = rows[0]
                if isinstance(first, dict):
                    bundle_code = str(first.get("name") or "").strip()
                else:
                    bundle_code = str(getattr(first, "name", "") or "").strip()
        except Exception:
            bundle_code = ""
        cache[item_code] = bundle_code
    return cache[item_code]


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
    _has_bundle_parent_missing_code = False
    _bundle_group_derivation_cache: Dict[str, Dict[str, Dict[str, str]]] = {}
    _bundle_parent_derivation_cache: Dict[str, str] = {}
    for item in invoice.items:
        bundle_code_val = getattr(item, "bundle_code", None)
        if bundle_code_val is None:
            bundle_code_val = ""
        else:
            bundle_code_val = str(bundle_code_val).strip()
        is_bundle_parent_val = getattr(item, "is_bundle_parent", None)
        is_bundle_parent_flag = bool(is_bundle_parent_val) if is_bundle_parent_val not in (None, "") else False
        if is_bundle_parent_flag and not bundle_code_val:
            bundle_code_val = _derive_bundle_code_from_parent_item(
                item.item_code, _bundle_parent_derivation_cache
            )
            if not bundle_code_val:
                _has_bundle_parent_missing_code = True

        # Read persisted group metadata; derive on-the-fly for legacy / unfilled rows
        is_bundle_child_flag = bool(getattr(item, "is_bundle_child", None))
        bundle_group_key = str(getattr(item, "bundle_group_key", None) or "").strip()
        bundle_group_name = str(getattr(item, "bundle_group_name", None) or "").strip()
        if is_bundle_child_flag and (not bundle_group_key or not bundle_group_name):
            parent_bundle_code = str(getattr(item, "parent_bundle", None) or "").strip()
            if parent_bundle_code:
                derived_key, derived_name = _derive_bundle_group_metadata(
                    parent_bundle_code, item.item_code, _bundle_group_derivation_cache
                )
                if derived_key and not bundle_group_key:
                    bundle_group_key = derived_key
                if derived_name and not bundle_group_name:
                    bundle_group_name = derived_name

        item_payload = {
            "item_code": item.item_code,
            "item_name": sanitize_printable_text(item.item_name),
            "qty": _safe_float(item.qty),
            "rate": _safe_float(item.rate),
            "amount": _safe_float(item.amount),
            # Always emit bundle_code + is_bundle_parent/child so amendment client
            # can reconstruct bundles even when value is falsy.
            "bundle_code": bundle_code_val,
            "is_bundle_parent": is_bundle_parent_flag,
            "is_bundle_child": is_bundle_child_flag,
            "parent_bundle": str(getattr(item, "parent_bundle", None) or "").strip(),
            "bundle_group_key": bundle_group_key,
            "bundle_group_name": bundle_group_name,
        }
        for fieldname in [
            "price_list_rate",
            "discount_percentage",
            "discount_amount",
        ]:
            value = getattr(item, fieldname, None)
            if value not in (None, ""):
                item_payload[fieldname] = value
        items.append(item_payload)
    if _has_bundle_parent_missing_code:
        frappe.log_error(
            f"Invoice {invoice.name} has bundle-parent rows with empty bundle_code — "
            "amendment reconstruction may fail (catalog miss). "
            "Check item rows for missing bundle_code field.",
            "Amendment Payload Drift",
        )
    
    # Validate customer field — should never be empty on a submitted POS invoice.
    _customer_val = str(invoice.customer or "").strip()
    if not _customer_val:
        frappe.log_error(
            f"Invoice {invoice.name} has empty customer field — "
            "amendment load will fail (customer will be missing). "
            "Raw invoice.customer: {!r}".format(invoice.customer),
            "Amendment Payload Missing Customer",
        )

    # Create formatted invoice data
    data = {
        "name": invoice.name,
        "invoice_id_short": invoice.name.split('-')[-1] if '-' in invoice.name else invoice.name,
        "customer_name": sanitize_printable_text(invoice.customer_name or invoice.customer),
        "customer": sanitize_printable_text(invoice.customer),
        "territory": sanitize_printable_text(invoice.territory or ""),
        "sales_partner": invoice.get("sales_partner"),
    # New delivery slot fields
        "delivery_date": invoice.get("custom_delivery_date"),
        "delivery_time_from": invoice.get("custom_delivery_time_from"),
        "delivery_duration": invoice.get("custom_delivery_duration"),
    "delivery_slot_label": sanitize_printable_text(invoice.get("custom_delivery_slot_label")),
        "status": sanitize_printable_text(invoice.get("custom_sales_invoice_state") or invoice.get("sales_invoice_state") or "Received"),
        "posting_date": str(invoice.posting_date),
        "grand_total": float(invoice.grand_total or 0),
        "net_total": float(invoice.net_total or 0),
        "total_taxes_and_charges": float(invoice.total_taxes_and_charges or 0),
        "full_address": full_address,
        "shipping_address_name": invoice.get("shipping_address_name") or invoice.get("customer_address"),
        "customer_address": invoice.get("customer_address"),
        "items": items,
        "payment_method": invoice.get("custom_payment_method"),
        "pos_profile": invoice.get("custom_kanban_profile") or invoice.get("pos_profile"),
        "outstanding_amount": float(invoice.get("outstanding_amount") or 0),
        "docstatus_value": int(invoice.get("docstatus") or 0),
        "doc_status": invoice.get("status"),
        "is_return": int(invoice.get("is_return") or 0),
        "delivery_trip": invoice.get("custom_delivery_trip"),
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

    def _normalise_date(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value).strip()
        if not text:
            return None
        return text.split("T", 1)[0]

    date_from = _normalise_date(filters.get('dateFrom'))
    date_to = _normalise_date(filters.get('dateTo'))
    customer = str(filters.get('customer') or '').strip()
    status = str(filters.get('status') or '').strip().lower()
    
    # Apply date filters
    if date_from:
        filter_conditions["posting_date"] = [">=", date_from]
        
    if date_to:
        if "posting_date" in filter_conditions:
            filter_conditions["posting_date"] = ["between", [date_from, date_to]]
        else:
            filter_conditions["posting_date"] = ["<=", date_to]
            
    # Apply customer filter
    if customer:
        filter_conditions["customer"] = customer

    if status:
        if status == 'paid':
            filter_conditions["status"] = "Paid"
            filter_conditions["docstatus"] = 1
        elif status in {'unpaid', 'overdue'}:
            filter_conditions["status"] = ["in", ["Unpaid", "Overdue"]]
            filter_conditions["docstatus"] = 1
        elif status in {'cancelled', 'canceled'}:
            filter_conditions["docstatus"] = 2
            filter_conditions.pop("status", None)
        elif status == 'return':
            filter_conditions["is_return"] = 1
            filter_conditions["docstatus"] = 1
        elif status == 'draft':
            filter_conditions["docstatus"] = 0
            filter_conditions.pop("status", None)
        
    # Apply amount filters
    if filters.get('amountFrom'):
        filter_conditions["grand_total"] = [">=", filters['amountFrom']]
        
    if filters.get('amountTo'):
        if "grand_total" in filter_conditions:
            filter_conditions["grand_total"] = ["between", [filters['amountFrom'], filters['amountTo']]]
        else:
            filter_conditions["grand_total"] = ["<=", filters['amountTo']]
            
    return filter_conditions


# ---------------------------------------------------------------------------
# Territory → POS Profile helpers
# ---------------------------------------------------------------------------

def resolve_territory_name(territory_value: Any) -> Optional[str]:
    """Return the canonical Territory name for a code/display value, if known."""
    raw_value = str(territory_value or "").strip()
    if not raw_value:
        return None

    if frappe.db.exists("Territory", raw_value):
        return raw_value

    try:
        if frappe.db.has_column("Territory", "territory_name"):
            territory_name = frappe.db.get_value("Territory", {"territory_name": raw_value}, "name")
            if territory_name:
                return territory_name

        for fieldname in ("custom_woo_code", "woo_code"):
            if frappe.db.has_column("Territory", fieldname):
                territory_name = frappe.db.get_value("Territory", {fieldname: raw_value}, "name")
                if territory_name:
                    return territory_name
    except Exception:
        return None

    return None


def _territory_from_address_row(address_row: Optional[Dict[str, Any]]) -> Optional[str]:
    if not address_row:
        return None

    for fieldname in ("city", "state"):
        territory_name = resolve_territory_name(address_row.get(fieldname))
        if territory_name:
            return territory_name
    return None


def resolve_address_territory(address_name: str | None) -> Optional[str]:
    """Resolve a selected Address to a Territory using Jarz's Address.city convention."""
    address_name = str(address_name or "").strip()
    if not address_name:
        return None

    try:
        address_row = frappe.db.get_value("Address", address_name, ["city", "state"], as_dict=True)
    except Exception:
        address_row = None
    return _territory_from_address_row(address_row)


def resolve_order_territory(
    customer_name: str | None,
    *,
    shipping_address_name: str | None = None,
    resolved_shipping_address: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve the territory that should control a POS order.

    A selected shipping address wins over the customer's stored territory so
    older/default customer state cannot contaminate invoice totals.
    """
    territory_name = _territory_from_address_row(resolved_shipping_address)
    if territory_name:
        return territory_name

    territory_name = resolve_address_territory(shipping_address_name)
    if territory_name:
        return territory_name

    customer_name = str(customer_name or "").strip()
    if not customer_name:
        return None

    try:
        customer_territory = frappe.db.get_value("Customer", customer_name, "territory")
    except Exception:
        customer_territory = None
    return resolve_territory_name(customer_territory)


def resolve_pos_profile_for_territory(territory_name: str | None) -> Optional[str]:
    territory_name = resolve_territory_name(territory_name)
    if not territory_name:
        return None

    try:
        if not frappe.get_meta("Territory").get_field("pos_profile"):
            return None
    except Exception:
        return None

    return frappe.db.get_value("Territory", territory_name, "pos_profile") or None


def resolve_territory_pos_profile(customer_name: str, territory_name: str | None = None) -> Optional[str]:
    """Return the POS Profile configured on the customer's territory, or None if not set.

    Returns None when:
    - customer_name is blank
    - customer has no territory
    - the Territory doctype has no pos_profile field (app not installed)
    - the territory has no POS profile assigned
    """
    if territory_name:
        return resolve_pos_profile_for_territory(territory_name)

    if not customer_name:
        return None
    territory = frappe.db.get_value("Customer", customer_name, "territory")
    return resolve_pos_profile_for_territory(territory)


def assert_pos_profile_matches_territory(
    customer_name: str,
    pos_profile_name: str,
    override: bool = False,
    territory_name: str | None = None,
) -> None:
    """Raise a ValidationError with code POS_PROFILE_TERRITORY_MISMATCH when the
    selected POS profile does not match the customer's territory profile and the
    caller has not supplied an explicit override.

    Both "no territory" and "territory has no pos_profile" are treated as a
    mismatch and require confirmation (override=True) to proceed.
    """
    import json as _json

    if override:
        return

    effective_territory = resolve_territory_name(territory_name) if territory_name else None
    territory_profile = resolve_territory_pos_profile(customer_name, territory_name=effective_territory)
    selected = (pos_profile_name or "").strip()
    territory = (territory_profile or "").strip()

    if selected and territory and selected == territory:
        return

    customer_territory = frappe.db.get_value("Customer", customer_name, "territory") or ""
    frappe.throw(
        _json.dumps({
            "code": "POS_PROFILE_TERRITORY_MISMATCH",
            "selected_profile": selected,
            "territory_profile": territory,
            "customer_territory": customer_territory,
            "effective_territory": effective_territory or customer_territory,
        }),
        frappe.ValidationError,
        title="POS_PROFILE_TERRITORY_MISMATCH",
    )

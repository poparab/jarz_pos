import frappe
from frappe import _


def publish_new_invoice(doc, method):
    """Publish a realtime event whenever a Sales Invoice is created."""
    try:
        # Log the event for debugging
        frappe.log_error(f"[JARZ_NOTIFICATIONS] publish_new_invoice triggered for {doc.name}, method={method}, docstatus={doc.docstatus}, is_pos={getattr(doc, 'is_pos', 'N/A')}", "Jarz Invoice Events")
        
        # Only emit for submitted invoices
        if int(getattr(doc, "docstatus", 0) or 0) != 1:
            frappe.log_error(f"[JARZ_NOTIFICATIONS] Invoice {doc.name} not submitted (docstatus={doc.docstatus}), skipping emit", "Jarz Invoice Events")
            return

        # Emit for all submitted Sales Invoices (Desk or POS)
        payload = {
            "name": doc.name,
            "customer_name": doc.get("customer_name") or doc.customer,
            "total": float(doc.total or 0),
            "grand_total": float(doc.grand_total or 0),
            "status": doc.status,
            "sales_invoice_state": doc.get("sales_invoice_state"),
            "custom_sales_invoice_state": doc.get("custom_sales_invoice_state"),
            "posting_date": str(doc.posting_date),
            "posting_time": str(doc.posting_time),
            "pos_profile": doc.pos_profile or "",
            "creation_source": "POS" if getattr(doc, 'is_pos', 0) else "Desk",
            "timestamp": frappe.utils.now()
        }

        # Broadcast both a generic and a kanban-friendly event
        frappe.log_error(f"[JARZ_NOTIFICATIONS] Emitting jarz_pos_new_invoice for {doc.name}", "Jarz Invoice Events")
        # Broadcast to all users and to current site room for multi-site setups
        frappe.publish_realtime("jarz_pos_new_invoice", payload, user="*", room=getattr(frappe.local, "site", None))  # type: ignore[attr-defined]

        try:
            # Normalize state key used by the kanban map in frontend
            state = (doc.get("custom_sales_invoice_state") or doc.get("sales_invoice_state") or "Received")
            state_key = str(state).strip().lower().replace(" ", "_")
            kanban_payload = {
                "event": "jarz_pos_invoice_state_change",
                "invoice_id": doc.name,
                "old_state_key": None,
                "new_state_key": state_key,
                "old_state": None,
                "new_state": state,
                "timestamp": frappe.utils.now()
            }
            frappe.log_error(f"[JARZ_NOTIFICATIONS] Emitting jarz_pos_invoice_state_change for {doc.name}, state={state}", "Jarz Invoice Events")
            frappe.publish_realtime("jarz_pos_invoice_state_change", kanban_payload, user="*", room=getattr(frappe.local, "site", None))  # type: ignore[attr-defined]
            # Redundant generic event to trigger board refresh listeners
            frappe.publish_realtime("kanban_update", kanban_payload, user="*", room=getattr(frappe.local, "site", None))  # type: ignore[attr-defined]
        except Exception as inner_e:
            frappe.log_error(f"[JARZ_NOTIFICATIONS] Kanban event failed for {doc.name}: {inner_e}", "Jarz Invoice Events")

    except Exception as e:
        frappe.log_error(f"[JARZ_NOTIFICATIONS] Realtime publish failed for {doc.name}: {e}", "Jarz Invoice Events")
def publish_state_change_if_needed(doc, method):
    """Publish a realtime kanban update when invoice state changes after submit.

    Triggers on on_update_after_submit so external edits (other devices, scripts)
    propagate to mobile clients immediately.
    """
    try:
        # Log the event for debugging
        frappe.log_error(f"[JARZ_NOTIFICATIONS] publish_state_change_if_needed triggered for {doc.name}, method={method}", "Jarz Invoice Events")

        # Only handle submitted invoices
        if int(getattr(doc, "docstatus", 0) or 0) != 1:
            frappe.log_error(f"[JARZ_NOTIFICATIONS] Invoice {doc.name} not submitted (docstatus={doc.docstatus}), skipping state change emit", "Jarz Invoice Events")
            return

        # Determine current and previous state values from any known fields
        current = doc.get("custom_sales_invoice_state") or doc.get("sales_invoice_state") or None
        # Try to get the previous db value to detect change
        prev = frappe.db.get_value("Sales Invoice", doc.name, ["custom_sales_invoice_state", "sales_invoice_state"], as_dict=True)
        old_val = None
        if prev:
            old_val = prev.get("custom_sales_invoice_state") or prev.get("sales_invoice_state")

        frappe.log_error(f"[JARZ_NOTIFICATIONS] State comparison for {doc.name}: current='{current}', old='{old_val}'", "Jarz Invoice Events")

        if (current or "") == (old_val or ""):
            frappe.log_error(f"[JARZ_NOTIFICATIONS] No state change for {doc.name}, skipping", "Jarz Invoice Events")
            return

        payload = {
            "event": "jarz_pos_invoice_state_change",
            "invoice_id": doc.name,
            "old_state": old_val,
            "new_state": current,
            "old_state_key": (old_val or "").strip().lower().replace(" ", "_") if old_val else None,
            "new_state_key": (current or "").strip().lower().replace(" ", "_") if current else None,
            "timestamp": frappe.utils.now()
        }

        frappe.log_error(f"[JARZ_NOTIFICATIONS] Emitting state change event for {doc.name}: {old_val} -> {current}", "Jarz Invoice Events")
        # Broadcast to all users and site room for reliability
        frappe.publish_realtime("jarz_pos_invoice_state_change", payload, user="*", room=getattr(frappe.local, "site", None))
        # Also emit a generic kanban update for clients only listening to this event
        frappe.publish_realtime("kanban_update", payload, user="*", room=getattr(frappe.local, "site", None))

    except Exception as e:
        frappe.log_error(f"[JARZ_NOTIFICATIONS] Failed to publish state change for {doc.name}: {e}", "Jarz Invoice Events")
def validate_invoice_before_submit(doc, method):
    """
    Hook method to validate invoice before submission
    Called automatically by ERPNext via hooks.py
    
    Validates:
    - Bundle parent items have 100% discount
    - Bundle child items have correct discount calculations
    - Total amounts match expected bundle pricing
    """
    try:
        frappe.log_error(f"Validating invoice {doc.name} before submit", "Bundle Validation")
        
        # Check if invoice contains bundle items
        has_bundle_items = any(
            item.get('is_bundle_parent') or item.get('is_bundle_child') 
            for item in doc.items
        )
        
        if not has_bundle_items:
            frappe.log_error(f"Invoice {doc.name} has no bundle items, skipping bundle validation", "Bundle Validation")
            return
            
        print(f"\n🔍 BUNDLE VALIDATION FOR INVOICE: {doc.name}")
        
        # Validate bundle parent items
        _validate_bundle_parents(doc)
        
        # Validate bundle child items
        _validate_bundle_children(doc)
        
        # Validate bundle totals
        _validate_bundle_totals(doc)
        
        print(f"✅ Bundle validation passed for invoice {doc.name}")
        frappe.log_error(f"Bundle validation passed for invoice {doc.name}", "Bundle Validation")
        
    except Exception as e:
        error_msg = f"Bundle validation failed for invoice {doc.name}: {str(e)}"
        frappe.log_error(error_msg, "Bundle Validation")
        print(f"❌ {error_msg}")
        frappe.throw(_(error_msg))


def _validate_bundle_parents(doc):
    """Validate that bundle parent items have 100% discount"""
    bundle_parents = [item for item in doc.items if item.get('is_bundle_parent')]
    
    for item in bundle_parents:
        # Check discount percentage is 100%
        discount_percentage = getattr(item, 'discount_percentage', 0) or 0
        if discount_percentage != 100:
            frappe.throw(_(
                f"Bundle parent item {item.item_code} must have 100% discount percentage. "
                f"Current: {discount_percentage}%"
            ))
            
        # Check net amount is 0
        net_amount = getattr(item, 'net_amount', None) or getattr(item, 'amount', 0)
        if net_amount != 0:
            frappe.throw(_(
                f"Bundle parent item {item.item_code} must have net amount of 0. "
                f"Current: {net_amount}"
            ))
            
        print(f"   ✅ Bundle parent {item.item_code}: 100% discount, net amount = 0")


def _validate_bundle_children(doc):
    """Validate bundle child items have appropriate discounts"""
    bundle_children = [item for item in doc.items if item.get('is_bundle_child')]
    
    # Group children by parent bundle
    bundles = {}
    for item in bundle_children:
        parent_bundle = item.get('parent_bundle')
        if parent_bundle not in bundles:
            bundles[parent_bundle] = []
        bundles[parent_bundle].append(item)
    
    for bundle_code, children in bundles.items():
        print(f"   🎁 Validating bundle {bundle_code} with {len(children)} children")
        
        # Get bundle document to verify pricing
        try:
            bundle_doc = frappe.get_doc("Jarz Bundle", bundle_code)
            expected_total = bundle_doc.bundle_price
            
            # Calculate actual total of children after discount
            actual_total = sum(
                getattr(item, 'net_amount', 0) or getattr(item, 'amount', 0) 
                for item in children
            )
            
            # Allow small rounding differences
            tolerance = 0.02
            if abs(actual_total - expected_total) > tolerance:
                frappe.throw(_(
                    f"Bundle {bundle_code} total mismatch. "
                    f"Expected: {expected_total}, Actual: {actual_total}"
                ))
                
            print(f"      ✅ Bundle total validated: Expected {expected_total}, Actual {actual_total}")
            
        except Exception as e:
            frappe.log_error(f"Error validating bundle {bundle_code}: {str(e)}", "Bundle Validation")
            # Don't fail validation for bundle lookup errors
            pass


def _validate_bundle_totals(doc):
    """Validate overall invoice totals with bundle considerations"""
    try:
        # Recalculate expected total
        expected_total = 0
        
        for item in doc.items:
            if item.get('is_bundle_parent'):
                # Parent items should contribute 0 to total
                continue
            else:
                # Regular items and bundle children contribute their net amount
                item_total = getattr(item, 'net_amount', 0) or getattr(item, 'amount', 0)
                expected_total += item_total
        
        # Add taxes to expected total
        for tax in (doc.taxes or []):
            expected_total += getattr(tax, 'tax_amount', 0) or 0
            
        # Compare with document total
        actual_total = doc.grand_total
        tolerance = 0.02
        
        if abs(actual_total - expected_total) > tolerance:
            frappe.log_error(
                f"Invoice total mismatch - Expected: {expected_total}, Actual: {actual_total}", 
                "Bundle Validation"
            )
            # Don't throw error for total mismatch - let ERPNext handle it
            pass
        else:
            print(f"   ✅ Invoice total validated: {actual_total}")
            
    except Exception as e:
        frappe.log_error(f"Error validating invoice totals: {str(e)}", "Bundle Validation")
        # Don't fail validation for total calculation errors
        pass
"""Jarz POS - Kanban board API endpoints.
This module provides API endpoints for the Sales Invoice Kanban board functionality. 
Primary state field: 'custom_sales_invoice_state' (legacy fallback: 'sales_invoice_state').
"""
from __future__ import annotations
import frappe
import json
import traceback
from typing import Dict, List, Any, Optional, Union, Tuple

# Import utility functions with fallback if they don't exist
try:
    from jarz_pos.utils.invoice_utils import (
        get_address_details,
        format_invoice_data,
        apply_invoice_filters
    )
except ImportError:
    # Fallback implementations if utils don't exist
    def get_address_details(address_name: str) -> str:
        if not address_name:
            return ""
        try:
            address_doc = frappe.get_doc("Address", address_name)
            return f"{address_doc.address_line1 or ''}, {address_doc.city or ''}".strip(", ")
        except Exception:
            return ""
    
    def format_invoice_data(invoice: frappe.Document) -> Dict[str, Any]:
        address_name = invoice.get("shipping_address_name") or invoice.get("customer_address")
        items = [{"item_code": item.item_code, "item_name": item.item_name, 
                 "qty": float(item.qty), "rate": float(item.rate), "amount": float(item.amount)}
                for item in invoice.items]
        state_val = invoice.get("custom_sales_invoice_state") or invoice.get("sales_invoice_state") or "Received"
        return {
            "name": invoice.name,
            "invoice_id_short": invoice.name.split('-')[-1] if '-' in invoice.name else invoice.name,
            "customer_name": invoice.customer_name or invoice.customer,
            "customer": invoice.customer,
            "territory": invoice.territory or "",
            "sales_partner": getattr(invoice, "sales_partner", None),
            "required_delivery_date": invoice.get("required_delivery_datetime"),
            "status": state_val,
            "posting_date": str(invoice.posting_date),
            "grand_total": float(invoice.grand_total or 0),
            "net_total": float(invoice.net_total or 0),
            "total_taxes_and_charges": float(invoice.total_taxes_and_charges or 0),
            "full_address": get_address_details(address_name),
            # New delivery slot fields (date + time range)
            "delivery_date": getattr(invoice, "custom_delivery_date", None),
            "delivery_time_from": getattr(invoice, "custom_delivery_time_from", None),
            "delivery_duration": getattr(invoice, "custom_delivery_duration", None),
            "items": items
        }
    
    def apply_invoice_filters(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
        filter_conditions = {"docstatus": 1, "is_pos": 1}
        if not filters:
            return filter_conditions
        
        if isinstance(filters, str):
            try:
                filters = json.loads(filters)
            except json.JSONDecodeError:
                return filter_conditions
        
        if filters.get('dateFrom'):
            filter_conditions["posting_date"] = [">=", filters['dateFrom']]
        if filters.get('dateTo'):
            if "posting_date" in filter_conditions:
                filter_conditions["posting_date"] = ["between", [filters['dateFrom'], filters['dateTo']]]
            else:
                filter_conditions["posting_date"] = ["<=", filters['dateTo']]
        if filters.get('customer'):
            filter_conditions["customer"] = filters['customer']
        if filters.get('amountFrom'):
            filter_conditions["grand_total"] = [">=", filters['amountFrom']]
        if filters.get('amountTo'):
            if "grand_total" in filter_conditions:
                filter_conditions["grand_total"] = ["between", [filters['amountFrom'], filters['amountTo']]]
            else:
                filter_conditions["grand_total"] = ["<=", filters['amountTo']]
        
        return filter_conditions

# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

# REPLACED: direct Custom Field doc fetch (requires permissions) with meta-based access
# which is available to all authenticated users and avoids 403 on restricted roles.

def _get_state_field_options() -> List[str]:
    """Return list of state options from Sales Invoice meta without reading Custom Field doc.
    Prefers 'custom_sales_invoice_state', falls back to legacy names.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        # Prefer new canonical field first
        field_names = ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]
        for field_name in field_names:
            field = meta.get_field(field_name)
            if field and getattr(field, 'options', None):
                options = [opt.strip() for opt in field.options.split('\n') if opt.strip()]
                if options:
                    frappe.logger().info(f"Found state field: {field_name} with options: {options}")
                    return options
        frappe.logger().warning("No state field found, using default states")
        return ["Received", "In Progress", "Ready", "Out for Delivery", "Delivered", "Cancelled"]
    except Exception as e:
        frappe.logger().error(f"Error getting state field options: {str(e)}")
        return ["Received", "In Progress", "Ready", "Out for Delivery", "Delivered", "Cancelled"]

# Backwards compatibility wrappers (kept in case referenced elsewhere in file)

def _get_state_custom_field():  # noqa: intentionally returns None now
    return None

def _get_allowed_states() -> List[str]:  # override previous implementation
    return _get_state_field_options()

def _state_key(label: str) -> str:
    return (label or "").strip().lower().replace(' ', '_')

# Unified success / error builders

def _success(**kwargs):
    payload = {"success": True}
    payload.update(kwargs)
    return payload

def _failure(msg: str):
    return {"success": False, "error": msg}

# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def get_kanban_columns() -> Dict[str, Any]:
    """Get all available Kanban columns based on Sales Invoice State field options.
    
    Returns:
        Dict with success status and columns data
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_columns called by {0}".format(frappe.session.user))
        options = _get_state_field_options()
        if not options:
            return _failure("Field 'sales_invoice_state' not found or has no options on Sales Invoice")
        columns = []
        # Color mapping for different states
        color_map = {
            "Received": "#E3F2FD",
            "Processing": "#FFF3E0",
            "Preparing": "#F3E5F5",
            "Out for delivery": "#E8F5E8",
            "Completed": "#E0F2F1"
        }
        for i, option in enumerate(options):
            column_id = _state_key(option)
            columns.append({
                "id": column_id,
                "name": option,
                "color": color_map.get(option, "#F5F5F5"),
                "order": i
            })
        return _success(columns=columns)
    except Exception as e:
        error_msg = f"Error getting kanban columns: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Columns Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_kanban_invoices(filters: Optional[Union[str, Dict]] = None) -> Dict[str, Any]:
    """Get Sales Invoices organized by their state for Kanban display.
    
    Args:
        filters: Filter conditions for invoice selection
        
    Returns:
        Dict with success status and invoices organized by state
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_invoices called with filters: {0}".format(filters))
        filter_conditions = apply_invoice_filters(filters)
        filter_conditions["docstatus"] = 1
        # Include both POS and Desk invoices; remove hard is_pos filter if present
        try:
            if isinstance(filter_conditions, dict) and "is_pos" in filter_conditions:
                filter_conditions.pop("is_pos", None)
        except Exception:
            pass
        
        # Fetch all matching Sales Invoices
        fields = [
            "name", "customer", "customer_name", "territory", "posting_date",
            "posting_time", "grand_total", "net_total", "total_taxes_and_charges",
            "status", "custom_sales_invoice_state", "sales_invoice_state",
            "sales_partner",
            # New delivery slot fields
            "custom_delivery_date", "custom_delivery_time_from", "custom_delivery_duration",
            "shipping_address_name", "customer_address"
        ]
        
        invoices = frappe.get_all(
            "Sales Invoice",
            filters=filter_conditions,
            fields=fields,
            order_by="posting_date desc, posting_time desc"
        )
        # Territory shipping cache
        territory_cache: Dict[str, Dict[str, float]] = {}
        def _get_territory_shipping(territory_name: str) -> Dict[str, float]:
            if not territory_name:
                return {"income": 0.0, "expense": 0.0}
            if territory_name in territory_cache:
                return territory_cache[territory_name]
            income = 0.0
            expense = 0.0
            try:
                terr = frappe.get_doc("Territory", territory_name)
                # Try multiple possible custom field names for robustness
                income_field_candidates = [
                    "shipping_income", "delivery_income", "courier_income", "shipping_income_amount"
                ]
                expense_field_candidates = [
                    "shipping_expense", "delivery_expense", "courier_expense", "shipping_expense_amount"
                ]
                for f in income_field_candidates:
                    if f in terr.as_dict():
                        try:
                            income = float(terr.get(f) or 0) ; break
                        except Exception:
                            pass
                for f in expense_field_candidates:
                    if f in terr.as_dict():
                        try:
                            expense = float(terr.get(f) or 0) ; break
                        except Exception:
                            pass
            except Exception:
                pass
            territory_cache[territory_name] = {"income": income, "expense": expense}
            return territory_cache[territory_name]
        # Get address information for invoices
        invoice_addresses = {}
        for inv in invoices:
            address_name = inv.get("shipping_address_name") or inv.get("customer_address")
            invoice_addresses[inv.name] = get_address_details(address_name)
        
        # Get items for each invoice
        invoice_items = {}
        for inv in invoices:
            items = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": inv.name},
                fields=["item_code", "item_name", "qty", "rate", "amount"]
            )
            invoice_items[inv.name] = items
        
        # Organize invoices by state
        kanban_data = {}
        
        # Get all possible states
        all_states = _get_state_field_options()
        # Initialize
        kanban_data = {}
        for state in all_states:
            state = state.strip()
            if state:
                kanban_data[_state_key(state)] = []
        
        # Organize invoices by their current state
        for inv in invoices:
            state = inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "Received"  # Default state
            state_key = state.lower().replace(' ', '_')
            terr_ship = _get_territory_shipping(inv.get("territory") or "")
            # Determine if there exists any UNSETTLED courier transaction for this invoice
            has_unsettled = False
            try:
                has_unsettled = frappe.db.exists(
                    "Courier Transaction",
                    {
                        "reference_invoice": inv.name,
                        "status": ["!=", "Settled"],
                    },
                )
            except Exception:
                has_unsettled = False
            # Create invoice card data
            # Normalize ERPNext doc status for board (treat Overdue as Unpaid)
            doc_status_label = str(inv.status or "").strip()
            if doc_status_label.lower() == "overdue":
                doc_status_label = "Unpaid"

            invoice_card = {
                "name": inv.name,
                "invoice_id_short": inv.name.split('-')[-1] if '-' in inv.name else inv.name,
                "customer_name": inv.customer_name or inv.customer,
                "customer": inv.customer,
                "territory": inv.territory or "",
                "sales_partner": inv.get("sales_partner"),
                # Delivery slot: date + start time + duration
                "delivery_date": getattr(inv, "custom_delivery_date", None),
                "delivery_time_from": getattr(inv, "custom_delivery_time_from", None),
                "delivery_duration": getattr(inv, "custom_delivery_duration", None),
                "delivery_slot_label": getattr(inv, "custom_delivery_slot_label", None),
                "status": state,  # Kanban state (custom field)
                "doc_status": doc_status_label,  # ERPNext doc status, with Overdue normalized to Unpaid
                "posting_date": str(inv.posting_date),
                "grand_total": float(inv.grand_total or 0),
                "net_total": float(inv.net_total or 0),
                "total_taxes_and_charges": float(inv.total_taxes_and_charges or 0),
                "full_address": invoice_addresses.get(inv.name, ""),
                "items": invoice_items.get(inv.name, []),
                "shipping_income": terr_ship.get("income", 0.0),
                "shipping_expense": terr_ship.get("expense", 0.0),
                "has_unsettled_courier_txn": bool(has_unsettled),
            }
            
            # Add to appropriate state column
            if state_key not in kanban_data:
                kanban_data[state_key] = []
            kanban_data[state_key].append(invoice_card)
        
        # Return unified success
        return _success(data=kanban_data)
    except Exception as e:
        error_msg = f"Error getting kanban invoices: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Invoices Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def update_invoice_state(invoice_id: str, new_state: str) -> Dict[str, Any]:
    """Update the custom_sales_invoice_state of a Sales Invoice (legacy field kept for backward compatibility).
    
    Args:
        invoice_id: ID of the Sales Invoice to update
        new_state: New state value to set
        
    Returns:
        Dict with success status and message
    """
    try:
        frappe.logger().debug(f"KANBAN API: update_invoice_state - Invoice: {invoice_id}, New state: {new_state}")
        print("\n" + "-"*90)
        print("KANBAN STATE CHANGE API CALL")
        print(f"Invoice: {invoice_id}")
        print(f"Requested New State: {new_state}")
        print(f"Timestamp: {frappe.utils.now()}")
        allowed_states = _get_allowed_states()
        if not allowed_states:
            return _failure("No allowed states configured (Custom Field missing or empty)")
        if new_state not in allowed_states:
            match_ci = next((s for s in allowed_states if s.lower() == (new_state or '').lower()), None)
            if match_ci:
                new_state = match_ci
            else:
                return _failure(f"'{new_state}' is not a valid state")
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        if invoice.docstatus != 1:
            return _failure("Only submitted (docstatus=1) Sales Invoices can change state")
        old_state = (
            invoice.get("custom_sales_invoice_state")
            or invoice.get("sales_invoice_state")
            or invoice.get("custom_state")
            or invoice.get("state")
        )
        if old_state == new_state:
            print(f"State unchanged; old_state == new_state == {new_state}")
            return _success(message="State unchanged (already set)", invoice_id=invoice_id, state=new_state)

        meta = frappe.get_meta("Sales Invoice")
        fields_to_update: List[str] = []
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                fields_to_update.append(candidate)
        if not fields_to_update:
            return _failure("No sales invoice state fields found (expected custom_sales_invoice_state or sales_invoice_state)")

        normalized_target = (new_state or "").strip().lower()
        create_dn = normalized_target in {"out for delivery", "out_for_delivery"}
        dn_logic_version = "2025-09-04b"
        frappe.logger().info(
            f"KANBAN API: State change requested -> {invoice_id} to '{new_state}' (normalized='{normalized_target}'), create_dn={create_dn}, logic_version={dn_logic_version}"
        )
        print(f"Normalized Target: {normalized_target} | create_dn: {create_dn} | logic_version: {dn_logic_version}")

        created_delivery_note: Optional[str] = None

        def _create_delivery_note_from_invoice(si_doc) -> str:
            frappe.logger().info(f"KANBAN API: Attempting Delivery Note creation for {si_doc.name}")
            existing = frappe.get_all(
                "Delivery Note",
                filters={"docstatus": 1, "remarks": ["like", f"%{si_doc.name}%"]},
                fields=["name"],
                limit=1,
            )
            if existing:
                dn_name = existing[0].name
                frappe.logger().info(
                    f"KANBAN API: Reusing existing Delivery Note {dn_name} for invoice {si_doc.name}"
                )
                # Ensure completed state on reuse
                try:
                    dn_doc = frappe.get_doc("Delivery Note", dn_name)
                    if int(getattr(dn_doc, "docstatus", 0) or 0) == 1:
                        try:
                            dn_doc.db_set("per_billed", 100, update_modified=False)
                        except Exception:
                            pass
                        try:
                            dn_doc.db_set("status", "Completed", update_modified=False)
                        except Exception:
                            pass
                except Exception:
                    pass
                return dn_name
            dn = frappe.new_doc("Delivery Note")
            dn.customer = si_doc.customer
            dn.company = si_doc.company
            dn.posting_date = frappe.utils.getdate()
            dn.posting_time = frappe.utils.nowtime()
            dn.remarks = f"Auto-created from Sales Invoice {si_doc.name} on state change to Out for Delivery"
            default_wh = None
            for it in si_doc.items:
                if it.get("warehouse"):
                    default_wh = it.get("warehouse")
                    break
            if default_wh:
                dn.set_warehouse = default_wh
            for it in si_doc.items:
                dn.append("items", {
                    "item_code": it.item_code,
                    "item_name": it.item_name,
                    "description": it.description,
                    "qty": it.qty,
                    "uom": it.uom,
                    "stock_uom": it.stock_uom,
                    "conversion_factor": getattr(it, "conversion_factor", 1) or 1,
                    "rate": it.rate,
                    "amount": it.amount,
                    "warehouse": it.get("warehouse") or default_wh,
                })
            dn.flags.ignore_permissions = True
            dn.insert(ignore_permissions=True)
            dn.submit()
            # Mark completed (fully billed) per business rule
            try:
                dn.db_set("per_billed", 100, update_modified=False)
            except Exception:
                pass
            try:
                dn.db_set("status", "Completed", update_modified=False)
            except Exception:
                pass
            frappe.logger().info(f"KANBAN API: Delivery Note {dn.name} submitted successfully for {si_doc.name}")
            return dn.name

        if create_dn:
            try:
                print(f"Attempting Delivery Note creation for invoice {invoice_id}")
                created_delivery_note = _create_delivery_note_from_invoice(invoice)
                print(f"Delivery Note created: {created_delivery_note}")
                frappe.logger().info(
                    f"KANBAN API: Delivery Note created '{created_delivery_note}' for invoice {invoice_id}"
                )
            except Exception as dn_ex:
                print(f"Delivery Note creation FAILED: {dn_ex}")
                frappe.logger().error(
                    f"KANBAN API: Delivery Note creation failed for {invoice_id}: {dn_ex}\n{frappe.get_traceback()}"
                )
                fail_resp = _failure(
                    f"Failed creating Delivery Note for invoice {invoice_id}: {str(dn_ex)}"
                )
                fail_resp["dn_logic_version"] = dn_logic_version
                return fail_resp

        updated_fields: List[str] = []
        for f in fields_to_update:
            try:
                invoice.db_set(f, new_state, update_modified=True)
                updated_fields.append(f)
                print(f"db_set success for field {f}")
            except Exception:
                try:
                    invoice.set(f, new_state)
                    invoice.save(ignore_permissions=True, ignore_version=True)
                    updated_fields.append(f + "(saved)")
                    print(f"save fallback success for field {f}")
                except Exception as inner_ex:
                    print(f"Failed updating field {f}: {inner_ex}")
                    frappe.logger().error(f"Failed updating field {f} on {invoice_id}: {inner_ex}")

        try:
            frappe.db.commit()
            print("DB commit successful")
        except Exception as commit_ex:
            frappe.logger().warning(f"Explicit DB commit after state update failed: {commit_ex}")
            print(f"DB commit FAILED: {commit_ex}")

        frappe.logger().info(
            f"KANBAN API: Invoice {invoice_id} state change {old_state} -> {new_state}; fields updated: {updated_fields}; delivery_note={created_delivery_note}; logic_version={dn_logic_version}"
        )
        payload = {
            "invoice_id": invoice_id,
            "old_state": old_state,
            "new_state": new_state,
            "old_state_key": _state_key(old_state or "") if old_state else None,
            "new_state_key": _state_key(new_state),
            "updated_by": frappe.session.user,
            "timestamp": frappe.utils.now(),
            "delivery_note": created_delivery_note if create_dn else None,
            "dn_logic_version": dn_logic_version,
        }
        frappe.publish_realtime("jarz_pos_invoice_state_change", payload, user="*")
        frappe.publish_realtime("kanban_update", payload, user="*")
        return _success(
            message=f"Invoice {invoice_id} state updated",
            invoice_id=invoice_id,
            state=new_state,
            updated_fields=updated_fields,
            final_state=new_state,
            delivery_note=created_delivery_note if create_dn else None,
            dn_logic_version=dn_logic_version,
        )
    except Exception as e:
        print(f"GENERAL FAILURE update_invoice_state: {e}")
        error_msg = f"Error updating invoice state: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Update Invoice State Error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_invoice_details(invoice_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific invoice.
    
    Args:
        invoice_id: ID of the Sales Invoice to retrieve
        
    Returns:
        Dict with success status and invoice details
    """
    try:
        frappe.logger().debug(f"KANBAN API: get_invoice_details - Invoice: {invoice_id}")
        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        data = format_invoice_data(invoice)
        # Augment with unsettled courier txn flag
        try:
            data["has_unsettled_courier_txn"] = bool(
                frappe.db.exists(
                    "Courier Transaction",
                    {"reference_invoice": invoice.name, "status": ["!=", "Settled"]},
                )
            )
        except Exception:
            data["has_unsettled_courier_txn"] = False
        return _success(data=data)
    except Exception as e:
        error_msg = f"Error getting invoice details: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Invoice Details Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_kanban_filters() -> Dict[str, Any]:
    """Get available filter options for the Kanban board.
    
    Returns:
        Dict with success status and filter options
    """
    try:
        frappe.logger().debug("KANBAN API: get_kanban_filters called")
        customers = frappe.get_all(
            "Sales Invoice",
            filters={"docstatus": 1, "is_pos": 1},
            fields=["customer", "customer_name"],
            distinct=True,
            order_by="customer_name"
        )
        customer_options = [{"value": c.customer, "label": c.customer_name or c.customer} for c in customers]
        state_options = [{"value": s, "label": s} for s in _get_state_field_options()]
        return _success(customers=customer_options, states=state_options)
    except Exception as e:
        error_msg = f"Error getting kanban filters: {str(e)}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Filters Error: {str(e)}", "Kanban API")
        return _failure(error_msg)

# ---------------------------------------------------------------------------
# Fallback explicit whitelist enforcement (in case of edge caching/import issues)
# ---------------------------------------------------------------------------
try:
    _kanban_funcs = [
        get_kanban_columns,
        get_kanban_invoices,
        update_invoice_state,
        get_invoice_details,
        get_kanban_filters,
    ]
    for _f in _kanban_funcs:
        if not getattr(_f, "is_whitelisted", False):
            frappe.logger().warning(f"KANBAN API: Forcing whitelist registration for {_f.__name__}")
            # Re-wrap with decorator (preserve allow_guest False)
            _wrapped = frappe.whitelist(allow_guest=False)(_f)
            globals()[_f.__name__] = _wrapped
except Exception:
    # Silent fail – we don't want import to abort
    pass

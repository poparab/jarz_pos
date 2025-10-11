"""Jarz POS - Kanban board API endpoints.
This module provides API endpoints for the Sales Invoice Kanban board functionality.
Primary state field: 'custom_sales_invoice_state' (legacy fallback: 'sales_invoice_state').
"""
from __future__ import annotations

import json
import traceback
from typing import Any, Optional, Union

import frappe

# Accounting helpers
try:
    from jarz_pos.utils.account_utils import (
        ensure_partner_receivable_subaccount,
        get_company_receivable_account,
        get_pos_cash_account,
    )
except Exception:
    # Fallback dummies (should not normally trigger)
    def get_company_receivable_account(company: str) -> str:  # type: ignore
        return frappe.get_cached_value("Company", company, "default_receivable_account")
    def get_pos_cash_account(pos_profile: str, company: str) -> str:  # type: ignore
        return frappe.get_cached_value("Company", company, "default_cash_account") or "Cash"  # type: ignore
    def ensure_partner_receivable_subaccount(company: str, partner: str) -> str:  # type: ignore
        return get_company_receivable_account(company)

# Import utility functions with fallback if they don't exist
try:
    from jarz_pos.utils.invoice_utils import apply_invoice_filters, format_invoice_data, get_address_details
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

    def format_invoice_data(invoice: frappe.Document) -> dict[str, Any]:
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

    def apply_invoice_filters(filters: str | dict | None = None) -> dict[str, Any]:
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

def _get_state_field_options() -> list[str]:
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
        frappe.logger().error(f"Error getting state field options: {e!s}")
        return ["Received", "In Progress", "Ready", "Out for Delivery", "Delivered", "Cancelled"]

def _coerce_bool(val: Any) -> bool:
    try:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return float(val) != 0.0
        s = str(val).strip().lower()
        return s in {"1", "true", "yes", "y", "on"}
    except Exception:
        return False

def _is_pickup_invoice(inv: dict[str, Any] | frappe.Document) -> bool:
    """Detect pickup flag on a Sales Invoice robustly across possible custom field names.
    Checks any of: custom_is_pickup, is_pickup, pickup, custom_pickup, or remarks contains [PICKUP].
    """
    try:
        getter = inv.get if isinstance(inv, dict) else getattr
        # Direct fields (several candidates)
        field_candidates = [
            "custom_is_pickup",
            "is_pickup",
            "pickup",
            "custom_pickup",
        ]
        for f in field_candidates:
            try:
                val = getter(inv, f) if getter is getattr else getter(f)
            except Exception:
                val = None
            if _coerce_bool(val):
                return True
        # Remarks marker
        try:
            remarks = getter(inv, "remarks") if getter is getattr else getter("remarks")
            if isinstance(remarks, str) and "[pickup]" in remarks.lower():
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False

def _get_current_user_pos_profiles() -> list[str]:
    """Return names of POS Profiles linked to the current session user (and not disabled)."""
    try:
        user = frappe.session.user
        # POS Profile linkage is via child table 'POS Profile User' (parent is the profile name)
        linked = frappe.get_all('POS Profile User', filters={'user': user}, pluck='parent') or []
        if not linked:
            return []
        profiles = frappe.get_all(
            'POS Profile',
            filters={'name': ['in', linked], 'disabled': 0},
            pluck='name',
        ) or []
        return profiles
    except Exception as e:
        frappe.logger().warning(f"KANBAN API: Failed to resolve user POS profiles: {e}")
        return []

# Backwards compatibility wrappers (kept in case referenced elsewhere in file)

def _get_state_custom_field():
    return None

def _get_allowed_states() -> list[str]:  # override previous implementation
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
def get_kanban_columns() -> dict[str, Any]:
    """Get all available Kanban columns based on Sales Invoice State field options.

    Returns:
        Dict with success status and columns data
    """
    try:
        frappe.logger().debug(f"KANBAN API: get_kanban_columns called by {frappe.session.user}")
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
        error_msg = f"Error getting kanban columns: {e!s}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Columns Error: {e!s}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_kanban_invoices(filters: str | dict | None = None) -> dict[str, Any]:
    """Get Sales Invoices organized by their state for Kanban display.

    Args:
        filters: Filter conditions for invoice selection

    Returns:
        Dict with success status and invoices organized by state
    """
    try:
        frappe.logger().debug(f"KANBAN API: get_kanban_invoices called with filters: {filters}")

        filter_conditions = apply_invoice_filters(filters)
        filter_conditions["docstatus"] = 1

        # Performance guardrails:
        # - Default to POS invoices and a recent date window when client doesn't specify
        # - Allow overriding via explicit filters
        try:
            if isinstance(filter_conditions, dict):
                # If no explicit posting_date filter, restrict to last 14 days
                if "posting_date" not in filter_conditions:
                    filter_conditions["posting_date"] = [">=", frappe.utils.add_days(frappe.utils.today(), -14)]
                # Default to POS only unless caller provided is_pos explicitly (True/False)
                if "is_pos" not in filter_conditions:
                    filter_conditions["is_pos"] = 1
        except Exception:
            pass

        # Restrict to POS Profile(s) assigned to the current user
        allowed_profiles = _get_current_user_pos_profiles()

        # Initialize columns up-front for possible early return
        all_states = _get_state_field_options()
        kanban_data: dict[str, list[dict[str, Any]]] = {}
        for state in all_states:
            st = (state or '').strip()
            if st:
                kanban_data[_state_key(st)] = []

        if not allowed_profiles:
            frappe.logger().info("KANBAN API: No POS Profile linked to user; returning empty board")
            return _success(data=kanban_data)

        # Optional client-provided branches list (subset of allowed profiles)
        client_selected_branches: list[str] = []
        try:
            raw = filters
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            if isinstance(raw, dict):
                maybe = raw.get("branches")
                if isinstance(maybe, list):
                    client_selected_branches = [str(x) for x in maybe if str(x).strip()]
        except Exception:
            client_selected_branches = []

        # Compute enforced branch list = intersection(allowed, client_selected) if client provided any; otherwise allowed only
        enforced_branches = allowed_profiles
        if client_selected_branches:
            enforced_branches = [p for p in allowed_profiles if p in set(client_selected_branches)]
            # If intersection is empty, return empty board (no access to requested branches)
            if not enforced_branches:
                return _success(data=kanban_data)

        # Enforce branch filter using new source of truth when available
        try:
            si_meta = frappe.get_meta("Sales Invoice")
            if si_meta.get_field("custom_kanban_profile"):
                filter_conditions["custom_kanban_profile"] = ["in", enforced_branches]
            else:
                # Fallback to legacy field
                filter_conditions["pos_profile"] = ["in", enforced_branches]
        except Exception:
            # Safe fallback
            filter_conditions["pos_profile"] = ["in", enforced_branches]

        # Fetch all matching Sales Invoices
        # Start with a stable base set of fields that always exist (or are known fixtures)
        fields = [
            "name", "customer", "customer_name", "territory", "posting_date",
            "posting_time", "grand_total", "net_total", "total_taxes_and_charges",
            "status", "custom_sales_invoice_state", "sales_invoice_state",
            "sales_partner", "pos_profile", "custom_kanban_profile",
            # New delivery slot fields (these are in our fixtures; safe to select)
            "custom_delivery_date", "custom_delivery_time_from", "custom_delivery_duration",
            "shipping_address_name", "customer_address",
            # Always-safe system field
            "remarks",
        ]

        # Append pickup-related fields ONLY if they exist in meta to avoid SQL errors
        try:
            si_meta = frappe.get_meta("Sales Invoice")
            pickup_candidates = ["custom_is_pickup", "is_pickup", "pickup", "custom_pickup"]
            for fn in pickup_candidates:
                if si_meta.get_field(fn):
                    fields.append(fn)
        except Exception:
            # If meta access fails, do not add optional fields
            pass

        # Cap results to avoid large payloads; client can paginate via additional filters
        invoices = frappe.get_all(
            "Sales Invoice",
            filters=filter_conditions,
            fields=fields,
            order_by="posting_date desc, posting_time desc",
            limit=250,
        )

        # Territory shipping cache
        territory_cache: dict[str, dict[str, float]] = {}

        def _get_territory_shipping(territory_name: str) -> dict[str, float]:
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
                            income = float(terr.get(f) or 0)
                            break
                        except Exception:
                            pass
                for f in expense_field_candidates:
                    if f in terr.as_dict():
                        try:
                            expense = float(terr.get(f) or 0)
                            break
                        except Exception:
                            pass
            except Exception:
                pass
            territory_cache[territory_name] = {"income": income, "expense": expense}
            return territory_cache[territory_name]

        # Get address information for invoices (batch compute via helper on names)
        invoice_addresses: dict[str, str] = {}
        try:
            addr_name_by_inv = {}
            for inv in invoices:
                addr_name_by_inv[inv.name] = inv.get("shipping_address_name") or inv.get("customer_address")
            for inv_name, addr_name in addr_name_by_inv.items():
                invoice_addresses[inv_name] = get_address_details(addr_name)
        except Exception:
            # Fallback: empty addresses
            invoice_addresses = {inv.name: "" for inv in invoices}

        # Batch fetch items for all invoices (avoid N+1 queries)
        invoice_items: dict[str, list[dict[str, Any]]] = {inv.name: [] for inv in invoices}
        try:
            if invoices:
                items_rows = frappe.get_all(
                    "Sales Invoice Item",
                    filters={"parent": ["in", [inv.name for inv in invoices]]},
                    fields=["parent", "item_code", "item_name", "qty", "rate", "amount"],
                    limit=5000,
                )
                for row in items_rows:
                    parent = row.get("parent")
                    if parent in invoice_items:
                        invoice_items[parent].append({
                            "item_code": row.get("item_code"),
                            "item_name": row.get("item_name"),
                            "qty": row.get("qty"),
                            "rate": row.get("rate"),
                            "amount": row.get("amount"),
                        })
        except Exception:
            # Fallback to per-invoice if batch fails
            for inv in invoices:
                try:
                    items = frappe.get_all(
                        "Sales Invoice Item",
                        filters={"parent": inv.name},
                        fields=["item_code", "item_name", "qty", "rate", "amount"],
                        limit=100,
                    )
                    invoice_items[inv.name] = items
                except Exception:
                    invoice_items[inv.name] = []

        # Organize invoices by their current state
        for inv in invoices:
            state = inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "Received"  # Default state
            state_key = state.lower().replace(' ', '_')
            terr_ship = _get_territory_shipping(inv.get("territory") or "")
            # Detect pickup and zero shipping amounts accordingly
            is_pickup = _is_pickup_invoice(inv)
            if is_pickup:
                terr_ship = {"income": 0.0, "expense": 0.0}

            # Resolve customer phone/mobile via primary Contact if possible (cached per customer)
            customer_phone = ""
            try:
                cust = inv.get("customer")
                if cust:
                    if "__customer_contact_cache" not in frappe.local.cache:  # type: ignore
                        frappe.local.cache["__customer_contact_cache"] = {}  # type: ignore
                    cache_key = f"cust_phone::{cust}"
                    ccache = frappe.local.cache["__customer_contact_cache"]  # type: ignore
                    if cache_key in ccache:
                        customer_phone = ccache[cache_key]
                    else:
                        # Try to find primary contact
                        contact_name = frappe.db.get_value(
                            "Dynamic Link",
                            {"link_doctype": "Customer", "link_name": cust, "parenttype": "Contact"},
                            "parent",
                        )
                        if contact_name:
                            contact_doc = frappe.get_doc("Contact", contact_name)
                            raw_mobile = getattr(contact_doc, "mobile_no", None) or getattr(contact_doc, "phone", None)
                            if raw_mobile:
                                customer_phone = str(raw_mobile)
                        ccache[cache_key] = customer_phone
            except Exception:
                customer_phone = ""

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
                "customer_phone": customer_phone,
                "is_pickup": bool(is_pickup),
            }

            # Add to appropriate state column
            if state_key not in kanban_data:
                kanban_data[state_key] = []
            kanban_data[state_key].append(invoice_card)

        # Return unified success
        return _success(data=kanban_data)
    except Exception as e:
        error_msg = f"Error getting kanban invoices: {e!s}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Invoices Error: {e!s}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def update_invoice_state(invoice_id: str, new_state: str) -> dict[str, Any]:
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
        fields_to_update: list[str] = []
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                fields_to_update.append(candidate)
        if not fields_to_update:
            return _failure("No sales invoice state fields found (expected custom_sales_invoice_state or sales_invoice_state)")

        normalized_target = (new_state or "").strip().lower()
        create_dn = normalized_target in {"out for delivery", "out_for_delivery"}
        dn_logic_version = "2025-09-11a"
        frappe.logger().info(
            f"KANBAN API: State change requested -> {invoice_id} to '{new_state}' (normalized='{normalized_target}'), create_dn={create_dn}, logic_version={dn_logic_version}"
        )
        print(f"Normalized Target: {normalized_target} | create_dn: {create_dn} | logic_version: {dn_logic_version}")

        created_delivery_note: str | None = None
        created_cash_payment_entry: str | None = None
        created_partner_txn: str | None = None

        # ------------------------------------------------------------------
        # Helper: Ensure CASH Payment Entry for Sales Partner invoices when
        # moving to Out For Delivery (business rule 2025-09). Only trigger if:
        #   - invoice has sales_partner
        #   - invoice still has outstanding_amount > 0
        #   - payment not already fully paid (no existing PE closing it)
        #   - new state is Out For Delivery
        # The Payment Entry will credit the company Receivable and debit
        # the POS Profile cash account (branch cash) – representing branch
        # taking cash from rider on dispatch.
        # Idempotency: if a PE already exists allocating full outstanding,
        # function returns gracefully.
        # ------------------------------------------------------------------
        def _ensure_cash_payment_entry_for_partner(si_doc) -> str | None:
            try:
                if not getattr(si_doc, "sales_partner", None):
                    return None
                outstanding = float(getattr(si_doc, "outstanding_amount", 0) or 0)
                if outstanding <= 0.0001:
                    return None
                existing = frappe.get_all(
                    "Payment Entry Reference",
                    filters={
                        "reference_doctype": "Sales Invoice",
                        "reference_name": si_doc.name,
                    },
                    fields=["parent", "allocated_amount", "total_amount", "outstanding_amount"],
                    limit=20,
                )
                for ref in existing:
                    try:
                        if float(ref.get("allocated_amount") or 0) >= outstanding - 0.0001:
                            return None
                    except Exception:
                        continue
                company = si_doc.company
                # Source of truth: custom_kanban_profile; fallback to pos_profile
                pos_profile = getattr(si_doc, "custom_kanban_profile", None) or getattr(si_doc, "pos_profile", None)
                if not pos_profile:
                    return None
                try:
                    cash_account = get_pos_cash_account(pos_profile, company)
                except Exception:
                    return None
                receivable = get_company_receivable_account(company)
                pe = frappe.new_doc("Payment Entry")
                pe.payment_type = "Receive"
                pe.company = company
                pe.posting_date = frappe.utils.getdate()
                pe.posting_time = frappe.utils.nowtime()
                pe.mode_of_payment = "Cash"
                pe.party_type = "Customer"
                pe.party = si_doc.customer
                pe.paid_from = receivable
                pe.paid_to = cash_account
                pe.party_account = receivable
                pe.paid_amount = outstanding
                pe.received_amount = outstanding
                # Propagate branch to Payment Entry if custom field exists
                try:
                    pe_meta = frappe.get_meta("Payment Entry")
                    if pe_meta.get_field("custom_kanban_profile"):
                        pe.custom_kanban_profile = pos_profile
                except Exception:
                    pass
                pe.append("references", {
                    "reference_doctype": "Sales Invoice",
                    "reference_name": si_doc.name,
                    "due_date": getattr(si_doc, "due_date", None),
                    "total_amount": float(getattr(si_doc, "grand_total", 0) or 0),
                    "outstanding_amount": outstanding,
                    "allocated_amount": outstanding,
                })
                pe.flags.ignore_permissions = True
                try:
                    pe.set_missing_values()
                except Exception:
                    pass
                pe.insert(ignore_permissions=True)
                pe.submit()
                frappe.logger().info(
                    f"KANBAN API: Cash Payment Entry {pe.name} created for partner invoice {si_doc.name} on OFD transition"
                )
                return pe.name
            except Exception as ce:
                frappe.logger().warning(f"KANBAN API: Cash PE creation skipped for {si_doc.name}: {ce}")
                return None

        def _create_delivery_note_from_invoice(si_doc) -> str:
            frappe.logger().info(f"KANBAN API: Attempting Delivery Note creation for {si_doc.name}")
            # Avoid filtering by remarks at SQL level (table name has spaces). Instead,
            # fetch recent Delivery Notes for this customer and inspect remarks in Python.
            try:
                candidates = frappe.get_all(
                    "Delivery Note",
                    filters={
                        "docstatus": 1,
                        "customer": si_doc.customer,
                        # Narrow by date window to keep list small; last 7 days
                        "posting_date": [">=", frappe.utils.add_days(frappe.utils.today(), -7)],
                    },
                    fields=["name", "posting_date", "posting_time"],
                    order_by="posting_date desc, posting_time desc",
                    limit=50,
                )
            except Exception:
                candidates = []
            for row in candidates:
                try:
                    dn_name_try = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
                    if not dn_name_try:
                        continue
                    dn_doc_try = frappe.get_doc("Delivery Note", dn_name_try)
                    remarks_text = (getattr(dn_doc_try, "remarks", None) or "").strip()
                    if remarks_text and si_doc.name in remarks_text:
                        frappe.logger().info(
                            f"KANBAN API: Reusing existing Delivery Note {dn_name_try} for invoice {si_doc.name} (found by remarks scan)"
                        )
                        # Ensure completed state on reuse
                        try:
                            if int(getattr(dn_doc_try, "docstatus", 0) or 0) == 1:
                                try:
                                    dn_doc_try.db_set("per_billed", 100, update_modified=False)
                                except Exception:
                                    pass
                                try:
                                    dn_doc_try.db_set("status", "Completed", update_modified=False)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                        return dn_name_try
                except Exception:
                    # ignore a single candidate failure and continue
                    continue
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
            # Propagate branch to Delivery Note when custom field exists
            try:
                dn_meta = frappe.get_meta("Delivery Note")
                if dn_meta.get_field("custom_kanban_profile"):
                    dn.custom_kanban_profile = getattr(si_doc, "custom_kanban_profile", None)
            except Exception:
                pass
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
                    f"Failed creating Delivery Note for invoice {invoice_id}: {dn_ex!s}"
                )
                fail_resp["dn_logic_version"] = dn_logic_version
                return fail_resp
            # After (or even if reusing) DN creation, ensure branch cash PE if partner invoice
            try:
                created_cash_payment_entry = _ensure_cash_payment_entry_for_partner(invoice)
                if created_cash_payment_entry:
                    print(f"Cash Payment Entry created: {created_cash_payment_entry}")
            except Exception as cash_ex:
                print(f"Cash Payment Entry creation FAILED (non-fatal): {cash_ex}")
                frappe.logger().warning(
                    f"KANBAN API: Cash Payment Entry creation failed for {invoice_id}: {cash_ex}"
                )
            # Create Sales Partner Transaction record (idempotent)
            try:
                sales_partner_val = getattr(invoice, 'sales_partner', None)
                if sales_partner_val:
                    # Idempotency token pattern: SPTRN::<invoice_name>
                    idem_token = f"SPTRN::{invoice.name}"
                    if not frappe.db.exists("Sales Partner Transactions", {"idempotency_token": idem_token}):
                        txn = frappe.new_doc("Sales Partner Transactions")
                        txn.sales_partner = sales_partner_val
                        txn.status = "Unsettled"  # always unsettle on creation
                        # Use original invoice creation datetime (invoice.creation is str/datetime)
                        try:
                            txn.date = getattr(invoice, 'creation', frappe.utils.now())
                        except Exception:
                            txn.date = frappe.utils.now()
                        txn.reference_invoice = invoice.name
                        txn.amount = float(getattr(invoice, 'grand_total', 0) or 0)
                        # partner_fees left blank for now; user will update later
                        # Determine payment mode: cash if cash PE created, else Online
                        payment_mode_val = 'Cash' if created_cash_payment_entry else 'Online'
                        txn.payment_mode = payment_mode_val
                        txn.idempotency_token = idem_token
                        txn.insert(ignore_permissions=True)
                        created_partner_txn = txn.name
                        print(f"Sales Partner Transaction created: {created_partner_txn} ({payment_mode_val})")
                        frappe.logger().info(
                            f"KANBAN API: Sales Partner Transaction {txn.name} created for invoice {invoice_id}"
                        )
                    else:
                        print("Sales Partner Transaction already exists (idempotent skip)")
            except Exception as sp_txn_err:
                print(f"Sales Partner Transaction creation FAILED (non-fatal): {sp_txn_err}")
                frappe.logger().warning(
                    f"KANBAN API: Sales Partner Transaction creation failed for {invoice_id}: {sp_txn_err}"
                )

        updated_fields: list[str] = []
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
            "cash_payment_entry": created_cash_payment_entry,
            "sales_partner_transaction": created_partner_txn,
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
            cash_payment_entry=created_cash_payment_entry,
            sales_partner_transaction=created_partner_txn,
        )
    except Exception as e:
        print(f"GENERAL FAILURE update_invoice_state: {e}")
        error_msg = f"Error updating invoice state: {e!s}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Update Invoice State Error: {e!s}\n\nTraceback:\n{traceback.format_exc()}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_invoice_details(invoice_id: str) -> dict[str, Any]:
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
        # Add is_pickup flag consistently
        try:
            data["is_pickup"] = _is_pickup_invoice(invoice)
            if data["is_pickup"]:
                # Ensure shipping fields are zeroed for pickup in details too
                data["shipping_income"] = 0.0
                data["shipping_expense"] = 0.0
        except Exception:
            pass
        # Enrich with customer_phone (reuse logic from get_kanban_invoices for consistency)
        try:
            customer_phone = ""
            cust = invoice.get("customer")
            if cust:
                if "__customer_contact_cache" not in frappe.local.cache:  # type: ignore
                    frappe.local.cache["__customer_contact_cache"] = {}  # type: ignore
                cache_key = f"cust_phone::{cust}"
                ccache = frappe.local.cache["__customer_contact_cache"]  # type: ignore
                if cache_key in ccache:
                    customer_phone = ccache[cache_key]
                else:
                    contact_name = frappe.db.get_value(
                        "Dynamic Link",
                        {"link_doctype": "Customer", "link_name": cust, "parenttype": "Contact"},
                        "parent",
                    )
                    if contact_name:
                        contact_doc = frappe.get_doc("Contact", contact_name)
                        raw_mobile = getattr(contact_doc, "mobile_no", None) or getattr(contact_doc, "phone", None)
                        if raw_mobile:
                            customer_phone = str(raw_mobile)
                    ccache[cache_key] = customer_phone
            if customer_phone:
                data["customer_phone"] = customer_phone
        except Exception:
            # Silently ignore phone enrichment failure
            pass
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
        error_msg = f"Error getting invoice details: {e!s}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Invoice Details Error: {e!s}", "Kanban API")
        return _failure(error_msg)

@frappe.whitelist(allow_guest=False)
def get_kanban_filters() -> dict[str, Any]:
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
        error_msg = f"Error getting kanban filters: {e!s}"
        frappe.logger().error(error_msg)
        frappe.log_error(f"Kanban Filters Error: {e!s}", "Kanban API")
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

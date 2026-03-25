"""
API endpoints for Delivery Trip management.

Provides trip CRUD, bulk OFD transition, and trip listing.
"""

import frappe
from frappe import _
from jarz_pos.constants import WS_EVENTS
from jarz_pos.services.delivery_handling import (
    mark_courier_outstanding,
    ensure_delivery_note_for_invoice,
    _get_delivery_expense_amount,
)


# ---------------------------------------------------------------------------
# Trip creation
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def create_delivery_trip(invoice_names, party_type: str, party: str):
    """Create a Delivery Trip grouping the given invoices.

    Args:
        invoice_names: list of Sales Invoice names (JSON string or list)
        party_type: "Employee" or "Supplier"
        party: Party name (Employee or Supplier)

    Returns:
        dict with trip details
    """
    if isinstance(invoice_names, str):
        invoice_names = frappe.parse_json(invoice_names)
    if not invoice_names:
        frappe.throw(_("At least one invoice is required to create a trip"))
    if party_type not in ("Employee", "Supplier"):
        frappe.throw(_("Courier party type must be Employee or Supplier"))
    if not frappe.db.exists(party_type, party):
        frappe.throw(_("{0} '{1}' not found").format(party_type, party))

    # Validate all invoices
    for inv_name in invoice_names:
        inv = frappe.get_doc("Sales Invoice", inv_name)
        if inv.docstatus != 1:
            frappe.throw(_("Invoice {0} is not submitted").format(inv_name))

        state = (
            inv.get("custom_sales_invoice_state")
            or inv.get("sales_invoice_state")
            or ""
        ).strip().lower()
        if state in ("out for delivery", "out_for_delivery", "delivered", "cancelled"):
            frappe.throw(_("Invoice {0} is in state '{1}' and cannot be added to a trip").format(
                inv_name, state
            ))

        is_pickup = bool(getattr(inv, "custom_is_pickup", 0))
        if is_pickup:
            frappe.throw(_("Pickup invoice {0} cannot be added to a delivery trip").format(inv_name))

        existing_trip = (
            getattr(inv, "custom_delivery_trip", None)
            or frappe.db.get_value("Sales Invoice", inv_name, "custom_delivery_trip")
            or ""
        )
        if existing_trip:
            # Check if the existing trip is still active
            trip_status = frappe.db.get_value("Delivery Trip", existing_trip, "status")
            if trip_status and trip_status != "Completed":
                frappe.throw(
                    _("Invoice {0} is already in active trip {1}").format(inv_name, existing_trip)
                )

        # Check sub-territory requirement
        from jarz_pos.api.territories import territory_has_children
        inv_territory = (inv.territory or "").strip()
        inv_sub = (getattr(inv, "custom_sub_territory", None) or "").strip()
        if inv_territory and territory_has_children(inv_territory) and not inv_sub:
            frappe.throw(
                _("Invoice {0}: please select a sub-territory for '{1}' before adding to trip").format(
                    inv_name, inv_territory
                )
            )

    # Create Delivery Trip document
    trip = frappe.new_doc("Delivery Trip")
    trip.trip_date = frappe.utils.today()
    trip.courier_party_type = party_type
    trip.courier_party = party
    trip.status = "Created"

    for inv_name in invoice_names:
        inv = frappe.get_doc("Sales Invoice", inv_name)
        # Prefer persisted SI value, fall back to territory computation
        shipping_exp = float(getattr(inv, "custom_shipping_expense", 0) or 0)
        if shipping_exp <= 0:
            shipping_exp = _get_delivery_expense_amount(inv) or 0.0
            if shipping_exp > 0:
                try:
                    inv.db_set("custom_shipping_expense", shipping_exp, update_modified=False)
                except Exception:
                    pass
        trip.append("invoices", {
            "invoice": inv_name,
            "customer_name": inv.customer_name,
            "territory": inv.territory,
            "sub_territory": getattr(inv, "custom_sub_territory", None) or "",
            "grand_total": float(inv.grand_total or 0),
            "shipping_expense": shipping_exp,
            "invoice_status": (
                inv.get("custom_sales_invoice_state")
                or inv.get("sales_invoice_state")
                or "Ready"
            ),
        })

    trip.insert(ignore_permissions=True)

    # Link trip back to each Sales Invoice
    for inv_name in invoice_names:
        frappe.db.set_value(
            "Sales Invoice", inv_name, "custom_delivery_trip", trip.name,
            update_modified=True,
        )

    frappe.db.commit()

    # Publish realtime event
    frappe.publish_realtime(WS_EVENTS.TRIP_CREATED, {
        "trip": trip.name,
        "status": trip.status,
        "courier_party_type": trip.courier_party_type,
        "courier_party": trip.courier_party,
        "courier_display_name": trip.courier_display_name,
        "total_orders": trip.total_orders,
        "total_amount": float(trip.total_amount or 0),
        "total_shipping_expense": float(trip.total_shipping_expense or 0),
        "is_double_shipping": bool(trip.is_double_shipping),
        "invoices": [r.invoice for r in trip.invoices],
    }, user="*")

    return {
        "success": True,
        "trip": trip.name,
        "status": trip.status,
        "total_orders": trip.total_orders,
        "total_amount": float(trip.total_amount or 0),
        "total_shipping_expense": float(trip.total_shipping_expense or 0),
        "is_double_shipping": bool(trip.is_double_shipping),
        "double_shipping_territory": trip.double_shipping_territory,
    }


# ---------------------------------------------------------------------------
# Send trip for delivery (bulk OFD)
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def send_trip_for_delivery(trip_name: str):
    """Bulk OFD transition for all eligible invoices in a trip.

    Skips invoices with pending custom shipping requests.
    Applies double shipping multiplier if applicable.

    Returns:
        dict with processed/skipped invoices and trip status
    """
    trip = frappe.get_doc("Delivery Trip", trip_name)
    if trip.status == "Completed":
        frappe.throw(_("Trip {0} is already completed").format(trip_name))
    if trip.status == "Out for Delivery":
        frappe.throw(_("Trip {0} is already out for delivery").format(trip_name))

    processed = []
    skipped = []

    for row in trip.invoices:
        inv = frappe.get_doc("Sales Invoice", row.invoice)

        # Skip if already OFD or beyond
        current_state = (
            inv.get("custom_sales_invoice_state")
            or inv.get("sales_invoice_state")
            or ""
        ).strip().lower()
        if current_state in ("out for delivery", "out_for_delivery", "delivered"):
            processed.append({"invoice": row.invoice, "status": "already_ofd"})
            continue

        # Check pending custom shipping → skip
        override_status = (
            getattr(inv, "custom_shipping_override_status", None)
            or frappe.db.get_value("Sales Invoice", row.invoice, "custom_shipping_override_status")
            or ""
        )
        if str(override_status).strip() == "Pending":
            skipped.append({
                "invoice": row.invoice,
                "reason": "Custom shipping request pending approval",
            })
            continue

        # Compute shipping expense (with double shipping multiplier)
        # Prefer persisted SI value, fall back to territory computation
        shipping_exp = float(getattr(inv, "custom_shipping_expense", 0) or 0)
        if shipping_exp <= 0:
            shipping_exp = _get_delivery_expense_amount(inv) or 0.0
            if shipping_exp > 0:
                try:
                    inv.db_set("custom_shipping_expense", shipping_exp, update_modified=False)
                except Exception:
                    pass
        if trip.is_double_shipping:
            shipping_exp = shipping_exp * 2

        # Ensure Delivery Note exists
        dn_result = ensure_delivery_note_for_invoice(row.invoice)
        if dn_result.get("error"):
            skipped.append({
                "invoice": row.invoice,
                "reason": f"Delivery Note error: {dn_result['error']}",
            })
            continue

        # Determine if invoice needs courier outstanding (unpaid) or is already paid
        outstanding = float(frappe.db.get_value("Sales Invoice", row.invoice, "outstanding_amount") or 0)
        if outstanding > 0.0001:
            # Unpaid: create courier outstanding with trip link and shipping override
            mark_courier_outstanding(
                row.invoice,
                party_type=trip.courier_party_type,
                party=trip.courier_party,
                delivery_trip=trip.name,
                shipping_override=shipping_exp if trip.is_double_shipping else None,
            )

        # Set invoice state to Out for Delivery
        meta = frappe.get_meta("Sales Invoice")
        for field_name in ["custom_sales_invoice_state", "sales_invoice_state"]:
            if meta.get_field(field_name):
                inv.db_set(field_name, "Out for Delivery", update_modified=True)

        # Update child row status
        row.invoice_status = "Out for Delivery"
        row.shipping_expense = shipping_exp

        processed.append({
            "invoice": row.invoice,
            "status": "processed",
            "shipping_expense": shipping_exp,
            "delivery_note": dn_result.get("delivery_note"),
        })

    # Update trip status
    if processed and not skipped:
        trip.status = "Out for Delivery"
    elif processed and skipped:
        # Partial: still mark as OFD since some invoices went out
        trip.status = "Out for Delivery"
    # If all skipped, remain as Created

    trip.save(ignore_permissions=True)
    frappe.db.commit()

    frappe.publish_realtime(WS_EVENTS.TRIP_OFD, {
        "trip": trip.name,
        "status": trip.status,
        "processed": processed,
        "skipped": skipped,
        "is_double_shipping": bool(trip.is_double_shipping),
    }, user="*")

    return {
        "success": True,
        "trip": trip.name,
        "status": trip.status,
        "processed": processed,
        "skipped": skipped,
        "is_double_shipping": bool(trip.is_double_shipping),
    }


# ---------------------------------------------------------------------------
# Trip listing & details
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def get_delivery_trips(status=None, courier_party=None, date_from=None, date_to=None,
                       pos_profile=None, limit=50, offset=0):
    """List delivery trips with optional filters.

    Args:
        status: Filter by trip status
        courier_party: Filter by courier party name
        date_from: Filter trips from this date
        date_to: Filter trips up to this date
        pos_profile: Filter by POS profile (via invoice territory)
        limit: Max results
        offset: Pagination offset

    Returns:
        dict with trips list
    """
    filters = {}
    if status:
        filters["status"] = status
    if courier_party:
        filters["courier_party"] = courier_party
    if date_from:
        filters["trip_date"] = [">=", date_from]
    if date_to:
        if "trip_date" in filters:
            filters["trip_date"] = ["between", [date_from, date_to]]
        else:
            filters["trip_date"] = ["<=", date_to]

    trips = frappe.get_all(
        "Delivery Trip",
        filters=filters,
        fields=[
            "name", "trip_date", "courier_party_type", "courier_party",
            "courier_display_name", "status", "is_double_shipping",
            "double_shipping_territory", "total_orders", "total_amount",
            "total_shipping_expense",
        ],
        order_by="creation desc",
        limit_page_length=int(limit),
        limit_start=int(offset),
    )

    return {
        "success": True,
        "data": trips,
        "count": len(trips),
    }


@frappe.whitelist(allow_guest=False)
def get_trip_details(trip_name: str):
    """Get full details of a delivery trip including all invoices.

    Args:
        trip_name: Delivery Trip name

    Returns:
        dict with full trip details
    """
    if not frappe.db.exists("Delivery Trip", trip_name):
        frappe.throw(_("Delivery Trip '{0}' not found").format(trip_name))

    trip = frappe.get_doc("Delivery Trip", trip_name)

    invoices = []
    for row in trip.invoices:
        # Get fresh data from Sales Invoice for rich detail display
        si_fields = [
            "custom_sales_invoice_state", "customer", "customer_name",
            "territory", "outstanding_amount", "status",
            "shipping_address_name", "customer_address",
            "custom_payment_method", "custom_delivery_date",
            "custom_delivery_time_from", "custom_delivery_duration",
            "custom_delivery_slot_label",
        ]
        si_data = frappe.db.get_value(
            "Sales Invoice", row.invoice, si_fields, as_dict=True
        ) or {}

        inv_state = si_data.get("custom_sales_invoice_state") or row.invoice_status

        # Resolve address
        address = ""
        addr_name = si_data.get("shipping_address_name") or si_data.get("customer_address")
        if addr_name:
            try:
                addr_doc = frappe.get_doc("Address", addr_name)
                address = f"{addr_doc.address_line1 or ''}, {addr_doc.city or ''}".strip(", ")
            except Exception:
                pass

        # Resolve customer phone
        customer_phone = ""
        customer = si_data.get("customer") or ""
        if customer:
            try:
                from jarz_pos.api.kanban import _resolve_customer_phone
                customer_phone = _resolve_customer_phone(customer)
            except Exception:
                pass

        # Fetch items
        items = []
        try:
            items_rows = frappe.get_all(
                "Sales Invoice Item",
                filters={"parent": row.invoice},
                fields=["item_code", "item_name", "qty", "rate", "amount"],
                order_by="idx asc",
            )
            items = [
                {
                    "item_code": r.get("item_code"),
                    "item_name": r.get("item_name"),
                    "qty": float(r.get("qty") or 0),
                    "rate": float(r.get("rate") or 0),
                    "amount": float(r.get("amount") or 0),
                }
                for r in items_rows
            ]
        except Exception:
            pass

        invoices.append({
            "invoice": row.invoice,
            "customer_name": row.customer_name,
            "territory": row.territory,
            "sub_territory": row.sub_territory,
            "grand_total": float(row.grand_total or 0),
            "shipping_expense": float(row.shipping_expense or 0),
            "invoice_status": inv_state,
            "outstanding_amount": float(si_data.get("outstanding_amount") or 0),
            "payment_status": str(si_data.get("status") or ""),
            "payment_method": si_data.get("custom_payment_method") or "",
            "address": address,
            "customer_phone": customer_phone,
            "items": items,
            "delivery_date": str(si_data.get("custom_delivery_date") or ""),
            "delivery_time_from": str(si_data.get("custom_delivery_time_from") or ""),
            "delivery_duration": si_data.get("custom_delivery_duration"),
            "delivery_slot_label": str(si_data.get("custom_delivery_slot_label") or ""),
        })

    return {
        "success": True,
        "trip": {
            "name": trip.name,
            "trip_date": str(trip.trip_date),
            "courier_party_type": trip.courier_party_type,
            "courier_party": trip.courier_party,
            "courier_display_name": trip.courier_display_name,
            "status": trip.status,
            "is_double_shipping": bool(trip.is_double_shipping),
            "double_shipping_territory": trip.double_shipping_territory,
            "total_orders": trip.total_orders,
            "total_amount": float(trip.total_amount or 0),
            "total_shipping_expense": float(trip.total_shipping_expense or 0),
            "notes": trip.notes,
            "invoices": invoices,
        },
    }


# ---------------------------------------------------------------------------
# Trip status sync helper
# ---------------------------------------------------------------------------

def sync_trip_status(invoice_name: str):
    """Recompute and update trip status when an invoice's state changes.

    Called from update_invoice_state or doc_events when a linked invoice
    changes state (OFD → Delivered, etc.).

    Args:
        invoice_name: Sales Invoice name that changed state
    """
    trip_name = frappe.db.get_value(
        "Sales Invoice", invoice_name, "custom_delivery_trip"
    )
    if not trip_name:
        return

    trip = frappe.get_doc("Delivery Trip", trip_name)
    states = set()
    for row in trip.invoices:
        st = (
            frappe.db.get_value("Sales Invoice", row.invoice, "custom_sales_invoice_state")
            or "Ready"
        ).strip().lower()
        states.add(st)
        # Update child row status
        row.invoice_status = frappe.db.get_value(
            "Sales Invoice", row.invoice, "custom_sales_invoice_state"
        ) or row.invoice_status

    old_status = trip.status

    # Derive status: all delivered → Completed; any OFD → Out for Delivery; else Created
    if states and all(s in ("delivered",) for s in states):
        trip.status = "Completed"
    elif any(s in ("out for delivery", "out_for_delivery") for s in states):
        trip.status = "Out for Delivery"
    else:
        trip.status = "Created"

    if trip.status != old_status:
        trip.save(ignore_permissions=True)
        if trip.status == "Completed":
            frappe.publish_realtime(WS_EVENTS.TRIP_COMPLETED, {
                "trip": trip.name,
                "status": trip.status,
            }, user="*")

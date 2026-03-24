"""
API endpoints for Custom Shipping Request management.

Provides request creation, approval/rejection, and listing for managers.
"""

import frappe
from frappe import _
from jarz_pos.constants import ROLES, WS_EVENTS
from jarz_pos.services.delivery_handling import _get_delivery_expense_amount


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return ROLES.JARZ_MANAGER in roles


@frappe.whitelist(allow_guest=False)
def request_custom_shipping(invoice_name: str, amount: float, reason: str):
    """Create a Custom Shipping Request for an invoice.

    Sets the invoice's custom_shipping_override_status to 'Pending' which
    blocks OFD transition until a manager approves or rejects.

    Args:
        invoice_name: Sales Invoice name
        amount: Requested custom shipping amount
        reason: Reason for custom shipping (required)

    Returns:
        dict with request name
    """
    amount = float(amount or 0)
    reason = (reason or "").strip()

    if amount <= 0:
        frappe.throw(_("Requested amount must be greater than zero"))
    if len(reason) < 10:
        frappe.throw(_("Please provide a reason of at least 10 characters"))
    if not frappe.db.exists("Sales Invoice", invoice_name):
        frappe.throw(_("Sales Invoice {0} not found").format(invoice_name))

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw(_("Only submitted Sales Invoices can have custom shipping requests"))

    # Check for existing pending request
    existing = frappe.db.exists(
        "Custom Shipping Request",
        {"invoice": invoice_name, "docstatus": 0, "status": "Pending"},
    )
    if existing:
        frappe.throw(
            _("A custom shipping request for {0} is already pending ({1})").format(
                invoice_name, existing
            )
        )

    # Compute current territory-based shipping for reference
    original_amount = _get_delivery_expense_amount(inv) or 0.0

    # Create request
    csr = frappe.new_doc("Custom Shipping Request")
    csr.invoice = invoice_name
    csr.original_amount = original_amount
    csr.requested_amount = amount
    csr.reason = reason
    csr.requested_by = frappe.session.user
    csr.requested_on = frappe.utils.now_datetime()
    csr.requires_approval = 1
    csr.status = "Pending"
    csr.insert(ignore_permissions=True)

    # Set pending status on invoice to gate OFD
    frappe.db.set_value(
        "Sales Invoice", invoice_name,
        "custom_shipping_override_status", "Pending",
        update_modified=True,
    )

    frappe.db.commit()

    # Notify managers via realtime
    frappe.publish_realtime(WS_EVENTS.CUSTOM_SHIPPING_REQUESTED, {
        "request": csr.name,
        "invoice": invoice_name,
        "original_amount": original_amount,
        "requested_amount": amount,
        "reason": reason,
        "requested_by": frappe.session.user,
    }, user="*")

    return {
        "success": True,
        "request": csr.name,
        "original_amount": original_amount,
        "requested_amount": amount,
    }


@frappe.whitelist(allow_guest=False)
def approve_custom_shipping(request_name: str):
    """Approve a Custom Shipping Request (JARZ Manager only).

    Submits the request doc, which triggers on_submit to set the
    approved amount on the Sales Invoice.

    Args:
        request_name: Custom Shipping Request name

    Returns:
        dict with approval details
    """
    if not _is_manager():
        frappe.throw(_("Only JARZ Managers can approve custom shipping requests"))

    csr = frappe.get_doc("Custom Shipping Request", request_name)
    if csr.docstatus != 0:
        frappe.throw(_("Request {0} is not in Draft state").format(request_name))
    if csr.status != "Pending":
        frappe.throw(_("Request {0} is not pending").format(request_name))

    csr.submit()
    frappe.db.commit()

    frappe.publish_realtime(WS_EVENTS.CUSTOM_SHIPPING_APPROVED, {
        "request": csr.name,
        "invoice": csr.invoice,
        "approved_amount": float(csr.requested_amount or 0),
        "approved_by": frappe.session.user,
    }, user="*")

    return {
        "success": True,
        "request": csr.name,
        "invoice": csr.invoice,
        "approved_amount": float(csr.requested_amount or 0),
    }


@frappe.whitelist(allow_guest=False)
def reject_custom_shipping(request_name: str, rejection_reason: str = ""):
    """Reject a Custom Shipping Request (JARZ Manager only).

    Cancels the request doc, which triggers on_cancel to revert the
    override on the Sales Invoice.

    Args:
        request_name: Custom Shipping Request name
        rejection_reason: Reason for rejection

    Returns:
        dict with rejection details
    """
    if not _is_manager():
        frappe.throw(_("Only JARZ Managers can reject custom shipping requests"))

    csr = frappe.get_doc("Custom Shipping Request", request_name)
    if csr.docstatus not in (0, 1):
        frappe.throw(_("Request {0} cannot be rejected").format(request_name))

    rejection_reason = (rejection_reason or "").strip()
    if rejection_reason:
        csr.rejection_reason = rejection_reason

    if csr.docstatus == 0:
        # Draft → just cancel (save rejection + set status)
        csr.status = "Rejected"
        csr.save(ignore_permissions=True)
        # Revert override on invoice
        frappe.db.set_value(
            "Sales Invoice", csr.invoice,
            {
                "custom_shipping_override": 0,
                "custom_shipping_override_status": "Rejected",
            },
            update_modified=True,
        )
    else:
        # Submitted → cancel
        csr.cancel()

    frappe.db.commit()

    frappe.publish_realtime(WS_EVENTS.CUSTOM_SHIPPING_REJECTED, {
        "request": csr.name,
        "invoice": csr.invoice,
        "rejection_reason": rejection_reason,
        "rejected_by": frappe.session.user,
    }, user="*")

    return {
        "success": True,
        "request": csr.name,
        "invoice": csr.invoice,
    }


@frappe.whitelist(allow_guest=False)
def get_pending_custom_shipping_requests():
    """List all pending Custom Shipping Requests.

    For manager dashboard.

    Returns:
        dict with list of pending requests
    """
    requests = frappe.get_all(
        "Custom Shipping Request",
        filters={"docstatus": 0, "status": "Pending"},
        fields=[
            "name", "invoice", "customer_name", "territory",
            "original_amount", "requested_amount", "reason",
            "requested_by", "requested_on",
        ],
        order_by="creation desc",
    )

    return {
        "success": True,
        "data": requests,
        "count": len(requests),
    }

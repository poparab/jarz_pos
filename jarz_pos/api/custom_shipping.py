"""
API endpoints for Custom Shipping Request management.

Provides request creation, approval/rejection, and listing for managers.
"""

import frappe
from frappe import _
from jarz_pos.constants import ROLES, WS_EVENTS
from jarz_pos.services.delivery_handling import _get_delivery_expense_amount


def _can_approve_shipping() -> bool:
    """Who may approve or reject a custom shipping override.

    Mirrors `manager._has_manager_dashboard_access` exactly: the pending
    requests are surfaced on the Manager Dashboard, so gating the approval on a
    *narrower* set than the dashboard itself is how a line manager ends up
    looking at Approve/Reject buttons that answer 403 on every tap. The line
    manager is a floor supervisor and owns the shipping call on their branch.
    """
    roles = {
        str(role or "").strip()
        for role in (frappe.get_roles(frappe.session.user) or [])
        if str(role or "").strip()
    }
    return bool(roles.intersection(ROLES.ADMIN | ROLES.LINE_MANAGER_TIER))


def _publish_shipping_event(event: str, payload: dict, *, invoice_name: str) -> None:
    """Notify the branch that owns the order, rather than the whole site.

    A shipping override is only actionable by the branch running the order and
    the managers assigned to it — both of which are exactly the POS Profile's
    user list.
    """
    from jarz_pos.utils.realtime import publish_invoice_event_by_name

    publish_invoice_event_by_name(event, payload, invoice_name)


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
    _publish_shipping_event(WS_EVENTS.CUSTOM_SHIPPING_REQUESTED, {
        "request": csr.name,
        "invoice": invoice_name,
        "original_amount": original_amount,
        "requested_amount": amount,
        "reason": reason,
        "requested_by": frappe.session.user,
    }, invoice_name=invoice_name)

    return {
        "success": True,
        "request": csr.name,
        "original_amount": original_amount,
        "requested_amount": amount,
    }


@frappe.whitelist(allow_guest=False)
def approve_custom_shipping(request_name: str):
    """Approve a Custom Shipping Request (manager tier, incl. line manager).

    Submits the request doc, which triggers on_submit to set the
    approved amount on the Sales Invoice.

    Args:
        request_name: Custom Shipping Request name

    Returns:
        dict with approval details
    """
    if not _can_approve_shipping():
        frappe.throw(_("Only managers can approve custom shipping requests"))

    csr = frappe.get_doc("Custom Shipping Request", request_name)
    if csr.docstatus != 0:
        frappe.throw(_("Request {0} is not in Draft state").format(request_name))
    if csr.status != "Pending":
        frappe.throw(_("Request {0} is not pending").format(request_name))

    # The role gate above is the authority. The DocType's own submit permission
    # is held by JARZ Manager and System Manager only, and widening it would
    # mean naming both spellings of the line-manager role in a DocPerm row —
    # so the API authorizes and the document write follows, exactly as the
    # request path already does with insert(ignore_permissions=True).
    csr.flags.ignore_permissions = True
    csr.submit()
    frappe.db.commit()

    _publish_shipping_event(WS_EVENTS.CUSTOM_SHIPPING_APPROVED, {
        "request": csr.name,
        "invoice": csr.invoice,
        "approved_amount": float(csr.requested_amount or 0),
        "approved_by": frappe.session.user,
    }, invoice_name=csr.invoice)

    return {
        "success": True,
        "request": csr.name,
        "invoice": csr.invoice,
        "approved_amount": float(csr.requested_amount or 0),
    }


@frappe.whitelist(allow_guest=False)
def reject_custom_shipping(request_name: str, rejection_reason: str = ""):
    """Reject a Custom Shipping Request (manager tier, incl. line manager).

    Cancels the request doc, which triggers on_cancel to revert the
    override on the Sales Invoice.

    Args:
        request_name: Custom Shipping Request name
        rejection_reason: Reason for rejection

    Returns:
        dict with rejection details
    """
    if not _can_approve_shipping():
        frappe.throw(_("Only managers can reject custom shipping requests"))

    csr = frappe.get_doc("Custom Shipping Request", request_name)
    csr.flags.ignore_permissions = True
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

    _publish_shipping_event(WS_EVENTS.CUSTOM_SHIPPING_REJECTED, {
        "request": csr.name,
        "invoice": csr.invoice,
        "rejection_reason": rejection_reason,
        "rejected_by": frappe.session.user,
    }, invoice_name=csr.invoice)

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

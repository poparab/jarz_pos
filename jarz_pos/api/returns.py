"""Whitelisted endpoints for the post-dispatch return workflow.

Thin transport layer only: every rule lives in
``jarz_pos.services.invoice_return`` so the service can be exercised directly by
the staging harness without going through HTTP.

Gated to the same tier as cancellation (``ROLES.LINE_MANAGER_TIER`` — line
manager, JARZ Manager, System Manager, Administrator) plus branch scoping, so a
manager can only return their own branch's orders.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES
from jarz_pos.services import invoice_return as _returns


def _ensure_return_permission() -> None:
    roles = {str(r or "").strip().lower() for r in frappe.get_roles(frappe.session.user)}
    if roles.isdisjoint(ROLES.LINE_MANAGER_TIER_LOWER):
        frappe.throw(_("You are not permitted to return orders"), frappe.PermissionError)


@frappe.whitelist(allow_guest=False)
def get_return_preview(invoice_id: str) -> Dict[str, Any]:
    """Everything the return dialog needs. Read-only."""
    _ensure_return_permission()
    try:
        return {"success": True, **_returns.build_return_preview(invoice_id)}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "get_return_preview failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def submit_invoice_return(
    invoice_id: str,
    lines: Any,
    reason: str,
    return_type: str = "Customer Return",
    refund_mode: str = _returns.REFUND_CUSTOMER_CREDIT,
    pay_courier_for_trip: Any = True,
    notes: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Post the return document graph.

    ``lines`` is a JSON list of ``{"si_detail": <row name>, "qty": <positive>}``.
    Idempotent by ``request_id``; resubmitting the same one returns the original
    result rather than posting a second set of documents.
    """
    _ensure_return_permission()
    return _returns.run_invoice_return(
        invoice_id=invoice_id,
        lines=lines,
        reason=reason,
        return_type=return_type,
        refund_mode=refund_mode,
        pay_courier_for_trip=pay_courier_for_trip,
        notes=notes,
        request_id=request_id,
    )


@frappe.whitelist(allow_guest=False)
def dry_run_return(invoice_id: str) -> Dict[str, Any]:
    """Resolve and balance every posting a return would make, writing nothing.

    Exposed so the staging survey can sweep real production-cloned orders and
    prove account resolution works for every branch/company/courier combination
    without touching the ledger.
    """
    _ensure_return_permission()
    try:
        return {"success": True, **_returns.dry_run_return_postings(invoice_id)}
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "dry_run_return failed")
        return {"success": False, "error": str(exc)}

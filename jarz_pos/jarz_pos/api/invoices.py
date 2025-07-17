"""Jarz POS – Invoice-related API endpoints.

This module serves as the authoritative location for all invoice-level
operations (create invoice, register payment, etc.) that need to be
consumed by external clients such as the React-Native mobile app.

Internally we continue to rely on the proven logic implemented in
``jarz_pos.jarz_pos.page.custom_pos.custom_pos``.  By importing and
re-exporting those callables here we achieve a clear, dedicated import
path (``jarz_pos.jarz_pos.api.invoices``) while maintaining full
backwards-compatibility with legacy endpoints.
"""

from __future__ import annotations

import frappe

# Legacy implementation lives in the custom POS page controller
from jarz_pos.jarz_pos.page.custom_pos import custom_pos as _legacy


# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------


@frappe.whitelist()  # type: ignore[attr-defined]
def create_sales_invoice(
    cart_json: str,
    customer_name: str,
    pos_profile_name: str,
    delivery_charges_json: str | None = None,
    required_delivery_datetime: str | None = None,
):
    """Create a Sales Invoice from the provided cart JSON.

    The heavy-lifting is delegated to :pyfunc:`_legacy.create_sales_invoice`.
    The signature is preserved 1-to-1 so no client-side changes are needed.
    """
    return _legacy.create_sales_invoice(
        cart_json,
        customer_name,
        pos_profile_name,
        delivery_charges_json,
        required_delivery_datetime,
    )


@frappe.whitelist()  # type: ignore[attr-defined]
def pay_invoice(invoice_name: str, payment_mode: str, pos_profile: str | None = None):
    """Register a payment against an existing Sales Invoice."""
    return _legacy.pay_invoice(invoice_name, payment_mode, pos_profile) 
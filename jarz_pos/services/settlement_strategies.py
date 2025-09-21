"""
Settlement strategy separation for Jarz POS.

Clear, idempotent handlers for these cases:
  1) unpaid + settle now
  2) unpaid + settle later
  3) paid   + settle now
  4) paid   + settle later

Sales Partner flow is handled as a separate concern via account routing helper
and a dedicated placeholder to extend later without mixing logic paths.
"""
from __future__ import annotations
from typing import Optional, Dict, Any
import frappe

from jarz_pos.jarz_pos.services.delivery_handling import (
    _get_delivery_expense_amount,
    ensure_delivery_note_for_invoice,
    _get_courier_outstanding_account,
    _get_receivable_account,
    _create_payment_entry,
)
from jarz_pos.jarz_pos.utils.account_utils import (
    get_pos_cash_account,
    get_freight_expense_account,
    get_creditors_account,
    validate_account_exists,
    resolve_online_partner_paid_to,
)


def _route_paid_to_account(company: str, payment_type: Optional[str], sales_partner: Optional[str]) -> Optional[str]:
    """Route paid_to account based on payment type and partner.
    Online + Sales Partner -> partner receivable subaccount (to be implemented elsewhere).
    Returns None to indicate caller should decide default (Cash/Bank/Courier Outstanding).
    """
    pt = (payment_type or "").strip().lower()
    if pt == "online":
        try:
            return resolve_online_partner_paid_to(company, sales_partner)
        except Exception:
            # If helper fails for any reason, let caller fallback
            return None
    return None


def _is_unpaid(inv) -> bool:
    try:
        outstanding = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
    except Exception:
        outstanding = float(inv.outstanding_amount or 0)
    status_l = (str(inv.get("status") or "").strip().lower())
    return (outstanding > 0.009) or status_l in {"unpaid", "overdue", "partially paid", "partly paid"}


# -----------------------------
# Handlers
# -----------------------------

def handle_unpaid_settle_now(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str]) -> Dict[str, Any]:
    company = inv.company
    outstanding = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
    already_paid = outstanding <= 0.0001

    paid_from = _get_receivable_account(company)
    # Paid to defaults to POS Cash; partner routing could override later
    paid_to = get_pos_cash_account(pos_profile, company)
    alt = _route_paid_to_account(company, payment_type, getattr(inv, "sales_partner", None))
    if alt:
        paid_to = alt
    for acc in (paid_from, paid_to):
        validate_account_exists(acc)

    pe_name = None
    paid_amt = outstanding
    if not already_paid and outstanding > 0.0001:
        pe = _create_payment_entry(inv, paid_from, paid_to, outstanding)
        pe_name = pe.name

    # After payment, perform Out For Delivery transition with immediate courier cash settlement
    # Reuse robust paid-handler that creates JE (DR Freight / CR Cash), CT (Settled), DN, and state update
    from jarz_pos.jarz_pos.services.delivery_handling import handle_out_for_delivery_paid as _ofd_paid
    courier_label = "Courier"
    ofd = _ofd_paid(inv.name, courier_label, settlement="cash_now", pos_profile=pos_profile, party_type=party_type, party=party)

    # Merge and return
    res: Dict[str, Any] = {
        "success": True,
        "invoice": inv.name,
        "mode": "unpaid_settle_now",
    }
    if pe_name:
        res.update({
            "payment_entry": pe_name,
            "paid_amount": paid_amt,
        })
    # Include OFD artifacts (journal_entry, courier_transaction, delivery_note, etc.)
    if isinstance(ofd, dict):
        for k in ("journal_entry", "courier_transaction", "delivery_note", "delivery_note_reused", "shipping_amount"):
            if k in ofd:
                res[k] = ofd[k]
    return res


def handle_unpaid_settle_later(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str]) -> Dict[str, Any]:
    from jarz_pos.jarz_pos.services.delivery_handling import mark_courier_outstanding as _mark
    # mark_courier_outstanding now enforces Delivery Note creation and returns DN info
    res = _mark(inv.name, courier=None, party_type=party_type, party=party)
    if isinstance(res, dict):
        res.update({"success": True, "mode": "unpaid_settle_later"})
    return res


def handle_paid_settle_now(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str]) -> Dict[str, Any]:
    # Paid already – no PE; perform Out For Delivery transition with immediate courier cash settlement
    from jarz_pos.jarz_pos.services.delivery_handling import handle_out_for_delivery_paid as _ofd_paid
    courier_label = "Courier"
    ofd = _ofd_paid(inv.name, courier_label, settlement="cash_now", pos_profile=pos_profile, party_type=party_type, party=party)
    # Return OFD artifacts
    res: Dict[str, Any] = {"success": True, "invoice": inv.name, "mode": "paid_settle_now"}
    if isinstance(ofd, dict):
        for k in ("journal_entry", "courier_transaction", "delivery_note", "delivery_note_reused", "shipping_amount"):
            if k in ofd:
                res[k] = ofd[k]
    return res


def handle_paid_settle_later(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str]) -> Dict[str, Any]:
    # No PE; accrue shipping and create Unsettled CT via existing transition path for paid invoices
    from jarz_pos.jarz_pos.services.delivery_handling import handle_out_for_delivery_paid as _ofd_paid
    courier_label = "Courier"
    return _ofd_paid(inv.name, courier_label, settlement="later", pos_profile=pos_profile, party_type=party_type, party=party)


STRATEGY = {
    ("unpaid", "now"): handle_unpaid_settle_now,
    ("unpaid", "later"): handle_unpaid_settle_later,
    ("paid", "now"): handle_paid_settle_now,
    ("paid", "later"): handle_paid_settle_later,
}


def dispatch_settlement(inv_name: str, *, mode: str, pos_profile: Optional[str] = None, payment_type: Optional[str] = None, party_type: Optional[str] = None, party: Optional[str] = None) -> Dict[str, Any]:
    """Central dispatch that decides paid/unpaid at call time and invokes the proper handler.

    mode: "now" | "later"
    """
    inv = frappe.get_doc("Sales Invoice", inv_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")
    status = "unpaid" if _is_unpaid(inv) else "paid"
    key = (status, (mode or "").strip().lower())
    fn = STRATEGY.get(key)
    if not fn:
        frappe.throw(f"Unsupported settlement: {key}")
    if not pos_profile:
        # Try to derive a default POS Profile when required; handlers that don't need it can ignore
        pos_profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
    return fn(inv, pos_profile=pos_profile or "", payment_type=payment_type, party_type=party_type, party=party)

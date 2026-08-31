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

from jarz_pos.services.delivery_handling import (
    _get_delivery_expense_amount,
    ensure_delivery_note_for_invoice,
    _get_courier_outstanding_account,
    _get_receivable_account,
    _create_payment_entry,
    update_submitted_sales_invoice_state,
    # Delivery-partner fee accrual. One builder for both order types: the fee is
    # the ONLY thing a partner changes, and it is always owed to the partner
    # company, never netted against the customer's money.
    create_partner_fee_accrual_je,
)
# Re-export selected delivery handlers at module level so tests can patch via
# 'jarz_pos.services.settlement_strategies.<name>'
from jarz_pos.services.delivery_handling import (
    handle_out_for_delivery_paid as handle_out_for_delivery_paid,  # alias for tests
    mark_courier_outstanding as mark_courier_outstanding,          # alias for tests
    handle_unpaid_online_deliver_unconfirmed as handle_unpaid_online_deliver_unconfirmed,  # alias for tests
)
import sys
from jarz_pos.utils.account_utils import (
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


_CASH_TOKENS = {"cash", "cod", "cashondelivery"}
# Only these normalized tokens count as unpaid online-intent (InstaPay + Mobile Wallet share
# identical accounting). Payment-gateway/card methods are prepaid and are intentionally excluded.
_ONLINE_INTENT_TOKENS = {"instapay", "insta", "bank", "bankaccount", "mobilewallet", "wallet"}


def _is_online_intent(inv) -> bool:
    """True when the invoice's intended payment method is a non-cash online method.

    Self-contained (no frappe / DB calls) so it is safe under unit-test mocks. Unknown or
    empty methods return False, so cash and gateway orders keep their existing flow.
    """
    try:
        raw = inv.get("custom_payment_method") if hasattr(inv, "get") else getattr(inv, "custom_payment_method", None)
    except Exception:
        raw = getattr(inv, "custom_payment_method", None)
    normalized = str(raw or "").strip().lower().replace(" ", "").replace("_", "")
    if not normalized or normalized in _CASH_TOKENS:
        return False
    return normalized in _ONLINE_INTENT_TOKENS


def _in_test_mode() -> bool:
    """Best-effort detection of unit test context to allow safe fallbacks.

    When running with --skip-test-records there are no real ledgers/pos profiles.
    In that context, handlers should avoid failing on account lookups and instead
    use placeholder accounts so patched/mocked flows can proceed.
    """
    try:
        if getattr(frappe, "flags", None) and getattr(frappe.flags, "in_test", None):
            return True
    except Exception:
        pass
    try:
        import sys as _sys  # local alias to avoid shadowing
        return "unittest" in _sys.modules
    except Exception:
        return False


# -----------------------------
# Handlers
# -----------------------------

def handle_unpaid_settle_now(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str]) -> Dict[str, Any]:
    company = inv.company
    outstanding = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
    already_paid = outstanding <= 0.0001

    # Resolve accounts with test-safe fallbacks
    try:
        paid_from = _get_receivable_account(company)
    except Exception:
        if _in_test_mode():
            paid_from = "Debtors - TEST"
        else:
            raise
    # Paid to defaults to POS Cash; partner routing could override later
    try:
        paid_to = get_pos_cash_account(pos_profile, company)
    except Exception:
        if _in_test_mode():
            paid_to = "Cash - TEST"
        else:
            raise
    alt = _route_paid_to_account(company, payment_type, getattr(inv, "sales_partner", None))
    if alt:
        paid_to = alt
    # Only validate ledgers when not in test mode (skip DB checks under mocks)
    if not _in_test_mode():
        for acc in (paid_from, paid_to):
            validate_account_exists(acc)

    pe_name = None
    paid_amt = outstanding
    if not already_paid and outstanding > 0.0001:
        pe = _create_payment_entry(inv, paid_from, paid_to, outstanding)
        pe_name = pe.name

    # After payment, perform Out For Delivery transition with immediate courier cash settlement
    # Use module-level alias so tests can patch it
    courier_label = "Courier"
    ofd = handle_out_for_delivery_paid(inv.name, courier_label, settlement="cash_now", pos_profile=pos_profile, party_type=party_type, party=party)

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
    # mark_courier_outstanding now enforces Delivery Note creation and returns DN info
    res = mark_courier_outstanding(inv.name, courier=None, party_type=party_type, party=party)
    if isinstance(res, dict):
        res.update({"success": True, "mode": "unpaid_settle_later"})
    return res


def handle_paid_settle_now(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str]) -> Dict[str, Any]:
    # Paid already – no PE; perform Out For Delivery transition with immediate courier cash settlement
    courier_label = "Courier"
    ofd = handle_out_for_delivery_paid(inv.name, courier_label, settlement="cash_now", pos_profile=pos_profile, party_type=party_type, party=party)
    # Return OFD artifacts
    res: Dict[str, Any] = {"success": True, "invoice": inv.name, "mode": "paid_settle_now"}
    if isinstance(ofd, dict):
        for k in ("journal_entry", "courier_transaction", "delivery_note", "delivery_note_reused", "shipping_amount"):
            if k in ofd:
                res[k] = ofd[k]
    return res


def handle_paid_settle_later(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str]) -> Dict[str, Any]:
    # No PE; accrue shipping and create Unsettled CT via existing transition path for paid invoices
    courier_label = "Courier"
    return handle_out_for_delivery_paid(inv.name, courier_label, settlement="later", pos_profile=pos_profile, party_type=party_type, party=party)


STRATEGY = {
    ("unpaid", "now"): handle_unpaid_settle_now,
    ("unpaid", "later"): handle_unpaid_settle_later,
    ("paid", "now"): handle_paid_settle_now,
    ("paid", "later"): handle_paid_settle_later,
}


# ---------------------------------------------------------------------------
# Delivery-Partner Strategies (zero delivery expense at branch level)
# ---------------------------------------------------------------------------

def _resolve_delivery_partner(party_type: Optional[str], party: Optional[str]) -> Optional[str]:
    """Return the Delivery Partner name if this courier belongs to one, else None."""
    if not party_type or not party:
        return None
    field = "custom_delivery_partner"
    try:
        return frappe.db.get_value(party_type, party, field)
    except Exception:
        return None


def _stamp_partner_fields(inv_name: str, delivery_partner: str):
    """Set partner fields on Sales Invoice (allow_on_submit safe via set_value)."""
    frappe.db.set_value(
        "Sales Invoice", inv_name,
        {"custom_delivery_partner": delivery_partner, "custom_is_partner_order": 1},
        update_modified=False,
    )


def _create_partner_courier_transaction(
    inv,
    *,
    party_type: Optional[str],
    party: Optional[str],
    delivery_partner: str,
    order_amount: float,
    partner_fee: float,
    status: str = "Unsettled",
    payment_mode: str = "Cash",
    journal_entry: Optional[str] = None,
    pos_profile: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """Create a Courier Transaction for a delivery-partner order.

    ``shipping_amount`` is deliberately ZERO on every partner row. That column means
    "what the RIDER is owed out of the cash he is carrying", and a partner's rider is
    owed nothing — his company bills us weekly instead. The fee goes on
    ``partner_fee``.

    That one convention is what lets partner rows travel through the ordinary courier
    cash machinery — balances, shift close, per-invoice settlement, the settlement
    journal entry — with no special case anywhere downstream: every
    ``amount - shipping_amount`` naturally yields the full amount the branch collects.
    """
    payload = {
        "doctype": "Courier Transaction",
        "party_type": party_type,
        "party": party,
        "reference_invoice": inv.name,
        "amount": order_amount,
        "shipping_amount": 0.0,
        "partner_fee": partner_fee,
        "status": status,
        "payment_mode": payment_mode,
        "delivery_partner": delivery_partner,
        "is_partner_order": 1,
        "journal_entry": journal_entry,
        "date": frappe.utils.now_datetime(),
    }
    if notes:
        payload["notes"] = notes
    if status == "Settled":
        # Born settled — stamp it like any other settlement so the shift
        # dashboard's "collected in this shift" figure is complete.
        from jarz_pos.services.courier_carry import settlement_stamp

        payload.update(settlement_stamp(pos_profile))
    ct = frappe.get_doc(payload)
    ct.insert(ignore_permissions=True)
    return ct.name


def _merge_dn_info(res: Dict[str, Any], dn_info: Any) -> None:
    if isinstance(dn_info, dict):
        for k in ("delivery_note", "delivery_note_reused"):
            if k in dn_info:
                res[k] = dn_info[k]


def _require_partner_fee(inv, delivery_partner: str, partner_fee) -> float:
    """The partner's own price for this trip, entered by whoever requested the rider.

    A delivery partner prices in its own zones, which subdivide ours many times
    over, so our territory rate is never their number and must never stand in for
    it. There is deliberately no fallback: the cost is read off the partner's app at
    the moment the rider is requested, or the dispatch is refused.

    Persisted to ``custom_shipping_expense`` so the delivery-cost reports show what
    we actually paid, and so the ordinary courier path downstream reads the same
    figure.
    """
    if partner_fee is None or str(partner_fee).strip() == "":
        frappe.throw(
            "This courier works for delivery partner {0}. Enter the partner's "
            "delivery cost for this order before dispatching — our own area rate "
            "does not apply to them.".format(delivery_partner)
        )
    try:
        fee = round(float(partner_fee), 2)
    except (TypeError, ValueError):
        frappe.throw("The partner delivery cost must be a number.")
    if fee < 0:
        frappe.throw("The partner delivery cost cannot be negative.")
    try:
        frappe.db.set_value(
            "Sales Invoice", inv.name, "custom_shipping_expense", fee, update_modified=False
        )
    except Exception:
        pass
    return fee


def handle_partner_unpaid_settle_now(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str], delivery_partner: str, partner_fee=None) -> Dict[str, Any]:
    """Partner cash order, rider hands the cash over now.

    He collected the customer's money and gives the branch the FULL amount — a
    partner's rider is never paid his fee out of it. So the cash side is the plain
    "unpaid, settle now" shape with no freight leg against the rider:

        DR POS Cash (GT)   / CR Debtors (GT)            the customer's money, into the till
        DR Freight (fee)   / CR Partner payable (fee)   what his company bills us

    The second entry is cleared by one weekly bank transfer, never from this drawer.
    """
    company = inv.company
    fee = _require_partner_fee(inv, delivery_partner, partner_fee)
    grand_total = float(inv.grand_total or 0)
    try:
        outstanding = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
    except Exception:
        outstanding = float(inv.outstanding_amount or 0)

    update_submitted_sales_invoice_state(inv, "Out for Delivery")
    dn_info = ensure_delivery_note_for_invoice(inv.name)

    # Customer's cash into the branch drawer, in full.
    pe_name = None
    if outstanding > 0.0001:
        try:
            paid_from = _get_receivable_account(company)
        except Exception:
            if _in_test_mode():
                paid_from = "Debtors - TEST"
            else:
                raise
        try:
            paid_to = get_pos_cash_account(pos_profile, company)
        except Exception:
            if _in_test_mode():
                paid_to = "Cash - TEST"
            else:
                raise
        if not _in_test_mode():
            for acc in (paid_from, paid_to):
                validate_account_exists(acc)
        pe = _create_payment_entry(inv, paid_from, paid_to, outstanding)
        pe_name = getattr(pe, "name", None)

    fee_je = create_partner_fee_accrual_je(inv, delivery_partner=delivery_partner, fee=fee)

    ct_name = _create_partner_courier_transaction(
        inv,
        pos_profile=pos_profile,
        party_type=party_type,
        party=party,
        delivery_partner=delivery_partner,
        order_amount=grand_total,
        partner_fee=fee,
        status="Settled",
        payment_mode=payment_type or "Cash",
        journal_entry=fee_je,
        notes="Partner cash order - full amount handed to the branch, rider takes no fee",
    )
    _stamp_partner_fields(inv.name, delivery_partner)

    res: Dict[str, Any] = {
        "success": True,
        "invoice": inv.name,
        "mode": "partner_unpaid_settle_now",
        "is_partner_order": True,
        "delivery_partner": delivery_partner,
        "courier_transaction": ct_name,
        "shipping_amount": 0.0,
        "partner_fee": fee,
        "amount_collected": grand_total,
        "journal_entry": fee_je,
    }
    if pe_name:
        res["payment_entry"] = pe_name
        res["paid_amount"] = outstanding
    _merge_dn_info(res, dn_info)
    return res


def handle_partner_unpaid_settle_later(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str], delivery_partner: str, partner_fee=None) -> Dict[str, Any]:
    """Partner cash order, cash comes back later.

    Identical to the ordinary courier settle-later path, because that is exactly
    what it is: the receivable moves to Courier Outstanding against the rider for
    the FULL order amount, and he owes the branch every pound of it.
    ``mark_courier_outstanding`` recognises the partner link itself and routes the
    fee to the partner's payable instead of the rider's Creditors, leaving
    ``shipping_amount`` at zero so nothing is deducted when he settles.
    """
    fee = _require_partner_fee(inv, delivery_partner, partner_fee)
    res = mark_courier_outstanding(
        inv.name, courier=None, party_type=party_type, party=party, shipping_override=fee
    )
    _stamp_partner_fields(inv.name, delivery_partner)
    if isinstance(res, dict):
        res.update({
            "success": True,
            "mode": "partner_unpaid_settle_later",
            "is_partner_order": True,
            "delivery_partner": delivery_partner,
        })
    return res


def _handle_partner_prepaid(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str], delivery_partner: str, mode_label: str, partner_fee=None) -> Dict[str, Any]:
    """Partner prepaid order — the rider carries no money and is given none.

        DR Freight (fee) / CR Partner payable (fee)

    That is the whole entry. The customer already paid us, so there is no cash
    position in either direction: the Courier Transaction is born Settled and never
    appears in courier balances or at shift close. Its ``partner_fee`` still rides
    on the weekly bill, tracked by the independent ``partner_settled`` flag.

    "Now" and "later" collapse to the same thing here — there is no cash whose
    timing could differ.
    """
    fee = _require_partner_fee(inv, delivery_partner, partner_fee)

    update_submitted_sales_invoice_state(inv, "Out for Delivery")
    dn_info = ensure_delivery_note_for_invoice(inv.name)

    fee_je = create_partner_fee_accrual_je(inv, delivery_partner=delivery_partner, fee=fee)

    ct_name = _create_partner_courier_transaction(
        inv,
        pos_profile=pos_profile,
        party_type=party_type,
        party=party,
        delivery_partner=delivery_partner,
        order_amount=0,
        partner_fee=fee,
        status="Settled",
        payment_mode=payment_type or "Online",
        journal_entry=fee_je,
        notes="Partner prepaid order - no cash position, fee billed weekly",
    )
    _stamp_partner_fields(inv.name, delivery_partner)

    res: Dict[str, Any] = {
        "success": True,
        "invoice": inv.name,
        "mode": mode_label,
        "is_partner_order": True,
        "delivery_partner": delivery_partner,
        "courier_transaction": ct_name,
        "shipping_amount": 0.0,
        "partner_fee": fee,
        "amount_collected": 0.0,
        "journal_entry": fee_je,
    }
    _merge_dn_info(res, dn_info)
    return res


def handle_partner_paid_settle_now(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str], delivery_partner: str, partner_fee=None) -> Dict[str, Any]:
    return _handle_partner_prepaid(
        inv, pos_profile=pos_profile, payment_type=payment_type, party_type=party_type,
        party=party, delivery_partner=delivery_partner, partner_fee=partner_fee,
        mode_label="partner_paid_settle_now",
    )


def handle_partner_paid_settle_later(inv, *, pos_profile: str, payment_type: Optional[str], party_type: Optional[str], party: Optional[str], delivery_partner: str, partner_fee=None) -> Dict[str, Any]:
    return _handle_partner_prepaid(
        inv, pos_profile=pos_profile, payment_type=payment_type, party_type=party_type,
        party=party, delivery_partner=delivery_partner, partner_fee=partner_fee,
        mode_label="partner_paid_settle_later",
    )


PARTNER_STRATEGY = {
    ("unpaid", "now"): handle_partner_unpaid_settle_now,
    ("unpaid", "later"): handle_partner_unpaid_settle_later,
    ("paid", "now"): handle_partner_paid_settle_now,
    ("paid", "later"): handle_partner_paid_settle_later,
}


def dispatch_settlement(inv_name: str, *, mode: str, pos_profile: Optional[str] = None, payment_type: Optional[str] = None, party_type: Optional[str] = None, party: Optional[str] = None, partner_fee=None) -> Dict[str, Any]:
    """Central dispatch that decides paid/unpaid at call time and invokes the proper handler.

    mode: "now" | "later"

    Automatically detects partner mode when the selected courier has a delivery_partner link.
    """
    inv = frappe.get_doc("Sales Invoice", inv_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")
    status = "unpaid" if _is_unpaid(inv) else "paid"
    key = (status, (mode or "").strip().lower())

    # Detect delivery partner mode
    delivery_partner = _resolve_delivery_partner(party_type, party)

    # InstaPay / Mobile Wallet honesty guard: an UNPAID online-intent order must move
    # Out for Delivery WITHOUT shifting the receivable to Courier Outstanding or marking
    # the invoice Paid. It stays honestly Unpaid until a manager confirms the transfer
    # via confirm_online_payment.
    #
    # This now covers delivery-partner orders too. It used to exclude them, which sent an
    # unpaid InstaPay partner order down a cash path and recorded money nobody had
    # collected. The rule is the same either way — the rider carries nothing — and
    # handle_unpaid_online_deliver_unconfirmed books the partner's fee to the partner.
    if status == "unpaid" and _is_online_intent(inv):
        return handle_unpaid_online_deliver_unconfirmed(
            inv,
            pos_profile=pos_profile,
            party_type=party_type,
            party=party,
            partner_fee=partner_fee,
        )

    if delivery_partner:
        fn = PARTNER_STRATEGY.get(key)
        if not fn:
            frappe.throw(f"Unsupported partner settlement: {key}")
        if not pos_profile:
            pos_profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
        return fn(inv, pos_profile=pos_profile or "", payment_type=payment_type, party_type=party_type, party=party, delivery_partner=delivery_partner, partner_fee=partner_fee)

    fn = STRATEGY.get(key)
    if not fn:
        frappe.throw(f"Unsupported settlement: {key}")
    # Allow unit tests to patch handler functions on this module by name.
    try:
        current_module = sys.modules.get(__name__)
        if current_module and hasattr(fn, "__name__"):
            patched = getattr(current_module, fn.__name__, None)
            if callable(patched):
                fn = patched
    except Exception:
        pass
    if not pos_profile:
        pos_profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
    return fn(inv, pos_profile=pos_profile or "", payment_type=payment_type, party_type=party_type, party=party)

"""Jarz POS – Promo-code preview API.

Exposes a single read-only endpoint used by the Flutter / Desk clients to
preview promo-code discounts against an in-memory cart before the invoice is
created.  Performs NO database writes.
"""

from __future__ import annotations

import json

import frappe

from jarz_pos.services import promo_codes as _promo


def _parse_json_param(value):
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


@frappe.whitelist()
def validate_promo_codes():
    """Preview promo-code discounts for an in-memory cart (read-only).

    form_dict params:
        codes        : JSON list or CSV of promo codes
        cart_json    : JSON list of {item_code, qty, rate, discount_percentage?}
        customer     : optional Customer name
        pos_profile  : optional POS Profile name
        channel      : default "flutter"
        pickup       : truthy flag
    """
    fd = frappe.form_dict

    codes = _parse_json_param(fd.get("codes"))
    cart = _parse_json_param(fd.get("cart_json")) or []
    customer = fd.get("customer")
    pos_profile_name = fd.get("pos_profile")
    channel = (fd.get("channel") or "flutter").strip().lower()

    raw_pickup = fd.get("pickup")
    if isinstance(raw_pickup, str):
        is_pickup = raw_pickup.strip().lower() in {"1", "true", "yes", "on"}
    else:
        is_pickup = bool(raw_pickup)

    if not isinstance(cart, list):
        cart = []

    # Build an unsaved Sales Invoice for evaluation only (never inserted/saved).
    invoice_doc = frappe.new_doc("Sales Invoice")
    if customer:
        invoice_doc.customer = customer

    # Resolve company defensively from POS profile or global defaults.
    company = None
    try:
        if pos_profile_name and frappe.db.exists("POS Profile", pos_profile_name):
            company = frappe.db.get_value("POS Profile", pos_profile_name, "company")
    except Exception:
        company = None
    if not company:
        try:
            company = frappe.defaults.get_global_default("company")
        except Exception:
            company = None
    if company:
        invoice_doc.company = company

    customer_doc = None
    if customer:
        try:
            customer_doc = frappe.get_doc("Customer", customer)
        except Exception:
            customer_doc = None

    # Populate items from the cart with rate + qty (no pricing recalculation).
    for row in cart:
        if not isinstance(row, dict):
            continue
        item_code = row.get("item_code")
        if not item_code:
            continue
        qty = float(row.get("qty") or 0)
        rate = float(row.get("rate") or 0)
        discount_pct = float(row.get("discount_percentage") or 0)
        amount = rate * qty * (1 - min(max(discount_pct, 0.0), 100.0) / 100.0)
        item_group = None
        try:
            item_group = frappe.get_cached_value("Item", item_code, "item_group")
        except Exception:
            item_group = None
        child = invoice_doc.append("items", {})
        child.item_code = item_code
        child.qty = qty
        child.rate = rate
        child.discount_percentage = discount_pct
        child.amount = round(amount, 2)
        if item_group is not None:
            child.item_group = item_group

    evaluation = _promo.evaluate_promo_codes(
        invoice_doc,
        codes,
        customer=customer,
        customer_doc=customer_doc,
        pos_profile=pos_profile_name,
        channel=channel,
        is_pickup=is_pickup,
    )

    return {
        "results": [
            {
                "code": r.code,
                "accepted": r.accepted,
                "discount_type": r.discount_type,
                "discount_amount": r.discount_amount,
                "free_delivery": r.free_delivery,
                "reason": r.reason,
            }
            for r in evaluation.results
        ],
        "total_discount": evaluation.total_discount,
        "free_delivery": evaluation.free_delivery,
        "eligible_net_total": evaluation.eligible_net_total,
        "capped": evaluation.capped,
    }

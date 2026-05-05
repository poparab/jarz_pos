from __future__ import annotations

from dataclasses import dataclass

import frappe


@dataclass
class DeliveryPromotionDecision:
    matched: bool = False
    rule_name: str | None = None
    rule_type: str | None = None
    merchandise_subtotal: float = 0.0
    item_qty: float = 0.0
    suppress_shipping_income: bool = False
    suppress_legacy_delivery_charges: bool = False


def resolve_delivery_promotion(
    invoice_doc,
    customer_doc=None,
    pos_profile=None,
    *,
    channel: str = "flutter",
    is_pickup: bool = False,
):
    decision = DeliveryPromotionDecision(
        merchandise_subtotal=_get_merchandise_subtotal(invoice_doc),
        item_qty=_get_item_qty(invoice_doc),
    )

    if is_pickup and not _pickup_rules_allowed():
        return decision

    if not frappe.db.exists("DocType", "Jarz Promotion Rule"):
        return decision

    rule_names = frappe.get_all(
        "Jarz Promotion Rule",
        filters={"enabled": 1, "promotion_scope": "Delivery"},
        pluck="name",
        order_by="priority asc, creation asc",
    )

    for rule_name in rule_names:
        rule = frappe.get_doc("Jarz Promotion Rule", rule_name)
        if not _is_rule_active(rule):
            continue
        if not _rule_matches_scope(rule, invoice_doc, customer_doc, pos_profile, channel, is_pickup):
            continue
        if not _rule_matches_threshold(rule, decision):
            continue
        if (rule.rule_type or "") != "Free Delivery":
            continue

        decision.matched = True
        decision.rule_name = rule.rule_name
        decision.rule_type = rule.rule_type
        decision.suppress_shipping_income = bool(rule.apply_to_shipping_income)
        decision.suppress_legacy_delivery_charges = bool(rule.apply_to_legacy_delivery_charges)
        return decision

    return decision


def apply_delivery_promotion_audit(invoice_doc, decision: DeliveryPromotionDecision):
    if not decision.matched or not decision.rule_name:
        return

    marker = (
        f"[DELIVERY PROMO] {decision.rule_name} | "
        f"merchandise_subtotal={decision.merchandise_subtotal:.2f}"
    )
    existing = (getattr(invoice_doc, "remarks", "") or "").strip()
    if marker in existing:
        return
    invoice_doc.remarks = (existing + "\n" if existing else "") + marker


def _pickup_rules_allowed() -> bool:
    return False


def _is_rule_active(rule) -> bool:
    now_dt = frappe.utils.now_datetime()
    if getattr(rule, "active_from", None) and rule.active_from > now_dt:
        return False
    if getattr(rule, "active_to", None) and rule.active_to < now_dt:
        return False
    return True


def _rule_matches_scope(rule, invoice_doc, customer_doc, pos_profile, channel: str, is_pickup: bool) -> bool:
    if is_pickup and not bool(getattr(rule, "is_pickup_allowed", 0)):
        return False

    company = getattr(invoice_doc, "company", None)
    if getattr(rule, "company", None) and rule.company != company:
        return False

    territory = getattr(customer_doc, "territory", None) or getattr(invoice_doc, "territory", None)
    if getattr(rule, "territory", None) and rule.territory != territory:
        return False

    customer_group = getattr(customer_doc, "customer_group", None)
    if getattr(rule, "customer_group", None) and rule.customer_group != customer_group:
        return False

    profile_name = getattr(pos_profile, "name", None) or getattr(invoice_doc, "pos_profile", None)
    if getattr(rule, "pos_profile", None) and rule.pos_profile != profile_name:
        return False

    allowed_channels = {
        (row.channel or "").strip().lower()
        for row in (getattr(rule, "channels", None) or [])
        if (row.channel or "").strip()
    }
    if allowed_channels and channel.strip().lower() not in allowed_channels:
        return False

    return True


def _rule_matches_threshold(rule, decision: DeliveryPromotionDecision) -> bool:
    basis = getattr(rule, "threshold_basis", None) or "Merchandise Subtotal"
    if basis == "Item Quantity":
        metric_value = decision.item_qty
        minimum_value = float(getattr(rule, "minimum_item_qty", 0) or 0)
        maximum_value = None
    else:
        metric_value = decision.merchandise_subtotal
        minimum_value = float(getattr(rule, "minimum_threshold", 0) or 0)
        maximum_value = getattr(rule, "maximum_threshold", None)
        maximum_value = float(maximum_value) if maximum_value not in (None, "") else None

    if minimum_value and metric_value < minimum_value:
        return False

    if maximum_value is not None and metric_value > maximum_value:
        return False

    return True


def _get_item_qty(invoice_doc) -> float:
    total_qty = 0.0
    for item in getattr(invoice_doc, "items", None) or []:
        total_qty += float(getattr(item, "qty", 0) or 0)
    return total_qty


def _get_merchandise_subtotal(invoice_doc) -> float:
    subtotal = 0.0
    for item in getattr(invoice_doc, "items", None) or []:
        qty = float(getattr(item, "qty", 0) or 0)
        price_list_rate = getattr(item, "price_list_rate", None)
        if price_list_rate in (None, ""):
            price_list_rate = getattr(item, "rate", 0) or 0
        discount_pct = float(getattr(item, "discount_percentage", 0) or 0)
        discount_pct = min(max(discount_pct, 0.0), 100.0)
        subtotal += float(price_list_rate or 0) * qty * (1 - discount_pct / 100.0)
    return round(subtotal, 2)

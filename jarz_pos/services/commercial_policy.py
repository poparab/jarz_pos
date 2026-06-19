"""Commercial Policy / Order Purpose resolver for Jarz POS.

This module resolves a chosen *order purpose* (Standard, B2B Supply, Employee,
Sample - Courier, Sample - No Courier, Free Shipping Waiver) into the existing
invoice primitives the accounting engine already understands:

  - ``suppress_shipping_income`` / ``suppress_legacy_delivery_charges`` (income waiver)
  - ``no_courier`` -> stamped onto ``Sales Invoice.custom_no_courier`` (expense zeroing
    + courier-assignment block, WITHOUT overloading the ``pickup`` label)
  - ``price_list`` (agreed B2B / employee / sample pricing)
  - ``discount_percentage`` (sample fallback pricing)

Design notes:
  - ``Standard`` (or an absent purpose with no explicit policy) is COMPLETELY INERT:
    it returns a default decision with every flag False so existing behavior is
    byte-identical.
  - The decision is a snapshot resolved at invoice-creation time. The caller freezes
    it onto the Sales Invoice; the accounting engine never re-reads the policy record,
    so editing a policy later cannot retroactively alter historical invoices.

Mirrors the structure of ``jarz_pos.services.delivery_promotions``.
"""

from __future__ import annotations

from dataclasses import dataclass

import frappe


@dataclass
class CommercialPolicyDecision:
    matched: bool = False
    order_purpose: str = "Standard"
    policy_name: str | None = None
    price_list: str | None = None
    discount_percentage: float = 0.0
    suppress_shipping_income: bool = False
    suppress_legacy_delivery_charges: bool = False
    no_courier: bool = False
    reason: str | None = None


def resolve_commercial_policy(
    order_purpose: str | None = None,
    commercial_policy: str | None = None,
    policy_reason: str | None = None,
    *,
    pos_profile=None,
    logger=None,
) -> CommercialPolicyDecision:
    """Resolve the order purpose into a ``CommercialPolicyDecision``.

    Raises (frappe.throw) when a non-Standard purpose/policy is requested but the
    caller is not permitted, or when an explicit policy cannot be found.
    """
    purpose = (order_purpose or "").strip()
    policy_name = (commercial_policy or "").strip()
    decision = CommercialPolicyDecision(reason=(policy_reason or "").strip() or None)

    # Inert fast-path: Standard purpose with no explicit policy -> unchanged behavior.
    if not policy_name and purpose in ("", "Standard"):
        decision.order_purpose = "Standard"
        return decision

    # Defensive: during staged rollout the DocType may not be migrated yet. Stay inert
    # rather than break invoice creation.
    if not frappe.db.exists("DocType", "Jarz Commercial Policy"):
        decision.order_purpose = purpose or "Standard"
        if logger:
            logger.warning(
                "Commercial policy requested but DocType not present; treating as Standard"
            )
        return decision

    policy = _load_policy(policy_name, purpose, pos_profile)
    if policy is None:
        frappe.throw(
            "No enabled Commercial Policy found for order purpose "
            f"'{purpose or policy_name}'."
        )

    # If the resolved policy is itself Standard, keep behavior inert (but record purpose).
    if (policy.order_purpose or "Standard") == "Standard":
        decision.order_purpose = "Standard"
        decision.matched = False
        return decision

    _ensure_policy_permission(policy)

    decision.matched = True
    decision.policy_name = policy.name
    decision.order_purpose = policy.order_purpose or purpose or "Standard"
    decision.price_list = (getattr(policy, "price_list", None) or "").strip() or None
    decision.discount_percentage = float(getattr(policy, "discount_percentage", 0) or 0)
    decision.suppress_shipping_income = (
        getattr(policy, "shipping_income_behavior", "Normal") == "Zero"
    )
    # When customer-facing shipping income is waived, also suppress the legacy
    # delivery-charge injection path (same coupling pickup/promotions use).
    decision.suppress_legacy_delivery_charges = decision.suppress_shipping_income
    decision.no_courier = getattr(policy, "courier_behavior", "Courier") == "No Courier"

    if logger:
        logger.info(
            "commercial_policy resolved: name=%s purpose=%s price_list=%s "
            "suppress_income=%s no_courier=%s discount=%s"
            % (
                decision.policy_name,
                decision.order_purpose,
                decision.price_list or "",
                decision.suppress_shipping_income,
                decision.no_courier,
                decision.discount_percentage,
            )
        )
    return decision


def _load_policy(policy_name: str, purpose: str, pos_profile):
    """Load a policy by explicit name, else the best enabled match for the purpose."""
    if policy_name:
        if not frappe.db.exists("Jarz Commercial Policy", policy_name):
            frappe.throw(f"Commercial Policy '{policy_name}' does not exist.")
        policy = frappe.get_doc("Jarz Commercial Policy", policy_name)
        if not getattr(policy, "enabled", 0):
            frappe.throw(f"Commercial Policy '{policy_name}' is disabled.")
        return policy

    if not purpose or purpose == "Standard":
        return None

    filters = {"enabled": 1, "order_purpose": purpose}
    candidates = frappe.get_all(
        "Jarz Commercial Policy",
        filters=filters,
        fields=["name", "company", "pos_profile", "priority"],
        order_by="priority asc, creation asc",
    )
    if not candidates:
        return None

    profile_name = getattr(pos_profile, "name", None)
    company = getattr(pos_profile, "company", None)

    # Prefer the most specific scope (matching pos_profile, then company), else first.
    def _scope_ok(row):
        if row.get("pos_profile") and row.get("pos_profile") != profile_name:
            return False
        if row.get("company") and company and row.get("company") != company:
            return False
        return True

    for row in candidates:
        if _scope_ok(row):
            return frappe.get_doc("Jarz Commercial Policy", row["name"])
    return None


def _ensure_policy_permission(policy) -> None:
    """Gate non-Standard purposes. Policy ``require_role`` overrides the default,
    which is the same manager-pricing access used for manual price-list overrides."""
    require_role = (getattr(policy, "require_role", "") or "").strip()
    if require_role:
        roles = set(frappe.get_roles(frappe.session.user) or [])
        if require_role not in roles:
            frappe.throw(
                f"Not permitted: role '{require_role}' is required to apply order "
                f"purpose '{policy.order_purpose}'."
            )
        return

    # Default gate: allow a non-Standard purpose for B2B Sales Reps OR manager-pricing
    # users. Lazy import avoids a circular import (invoice_creation imports this module).
    from jarz_pos.services.invoice_creation import _has_manager_pricing_access

    roles = set(frappe.get_roles(frappe.session.user) or [])
    if "B2B Sales Rep" in roles:
        return
    if _has_manager_pricing_access():
        return

    frappe.throw(
        "Not permitted: B2B Sales Rep or manager pricing access required to apply order "
        f"purpose '{policy.order_purpose}'."
    )

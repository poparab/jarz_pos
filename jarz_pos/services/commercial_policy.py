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
    #: The goods are handed over at the counter at order time (employee pickup).
    #: The caller runs the real fulfilment side effects immediately after submit —
    #: see ``jarz_pos.services.branch_fulfilment.fulfil_at_branch``. This is NOT the
    #: same as ``no_courier``: no_courier only says nobody is paid to drive, while
    #: this says the order never travels the dispatch kanban at all.
    deliver_at_branch: bool = False
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
    # ``getattr`` default keeps a site that has not migrated the new Select yet
    # resolving to Normal rather than raising — same staged-rollout tolerance the
    # DocType-existence guard above applies.
    decision.deliver_at_branch = (
        getattr(policy, "fulfilment_behavior", "Normal") == "Deliver at Branch"
    )

    if logger:
        logger.info(
            "commercial_policy resolved: name=%s purpose=%s price_list=%s "
            "suppress_income=%s no_courier=%s deliver_at_branch=%s discount=%s"
            % (
                decision.policy_name,
                decision.order_purpose,
                decision.price_list or "",
                decision.suppress_shipping_income,
                decision.no_courier,
                decision.deliver_at_branch,
                decision.discount_percentage,
            )
        )
    return decision


#: Sentinel for :func:`reserved_price_lists`: "caller did not say what the POS Profile
#: default is" (look it up), as distinct from "the profile has no default" (``None``).
_UNSET = object()


def reserved_price_lists(
    pos_profile_name: str | None,
    default_price_list=_UNSET,
) -> dict[str, set[str]]:
    """Map every price list OWNED by an order purpose to the purposes that own it.

    WHY: the POS let a manager pick the Order Purpose and the Price List
    independently, and ``invoice_creation._resolve_effective_price_list`` resolved
    ``requested or policy_pl``, so a client-sent list silently beat the policy's own.
    Production booked Sample - Courier invoices at Standard Selling and a B2B Supply
    invoice at Standard Selling. This map is the single definition of "which lists
    belong to a purpose", shared by the cart (``api/pos.get_pos_price_lists`` marks the
    options) and the server gate (``invoice_creation`` refuses a Standard / retail order
    that borrows one), so the two can never disagree.

    A list is reserved when it is:
      * the ``price_list`` of an ENABLED non-Standard ``Jarz Commercial Policy`` whose
        ``pos_profile`` scope is empty or equals ``pos_profile_name`` — the same scope
        rule ``_load_policy`` applies, so a Nasr City-only policy does not lock a list
        on Dokki; or
      * the B2B baseline list (``setup/b2b_pricing.PRICE_LIST``) -> B2B Supply. The
        "B2B Supply" policy deliberately carries no price list (the tier comes from the
        customer), so without this entry the one list B2B orders fall back to would
        look free to every retail order.

    The POS Profile default is NEVER reserved, even if a policy points at it: it is
    what every retail order prices from, and reserving it would make Standard checkout
    impossible on that profile.

    One query for the whole map, never one per price list. Names are returned as
    stored; callers compare case-insensitively (MariaDB's collation treats
    "sample" and "Sample" as the same list).
    """
    from jarz_pos.setup.b2b_pricing import B2B_SUPPLY_PURPOSE, PRICE_LIST as B2B_PRICE_LIST

    profile = (pos_profile_name or "").strip()
    if default_price_list is _UNSET:
        default_price_list = (
            frappe.db.get_value("POS Profile", profile, "selling_price_list") if profile else None
        )
    default_key = (default_price_list or "").strip().casefold()

    reserved: dict[str, set[str]] = {}

    def _reserve(price_list, purpose) -> None:
        name = (price_list or "").strip()
        owner = (purpose or "").strip()
        if not name or not owner or owner == "Standard":
            return
        if default_key and name.casefold() == default_key:
            return
        reserved.setdefault(name, set()).add(owner)

    # Staged-rollout tolerance, same as resolve_commercial_policy: a site that has not
    # migrated the DocType has no policy-owned lists, only the B2B baseline.
    if frappe.db.exists("DocType", "Jarz Commercial Policy"):
        rows = frappe.get_all(
            "Jarz Commercial Policy",
            filters={"enabled": 1},
            fields=["price_list", "order_purpose", "pos_profile"],
            limit_page_length=0,
        )
        for row in rows or []:
            scope = (row.get("pos_profile") or "").strip()
            if scope and scope != profile:
                continue
            _reserve(row.get("price_list"), row.get("order_purpose"))

    _reserve(B2B_PRICE_LIST, B2B_SUPPLY_PURPOSE)
    return reserved


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

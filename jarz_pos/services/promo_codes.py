"""Promo-code engine for Jarz POS.

Pure evaluation + application of ``Jarz Promo Code`` records against a Sales
Invoice document.  This module is intentionally self-contained and only relies
on standard Frappe/ERPNext APIs (never on jarz_woocommerce_integration).

Public surface
--------------
* ``evaluate_promo_codes(...)``                 — pure / read-only evaluation
* ``apply_promo_evaluation_to_invoice(...)``    — mutate a doc (no save)
* ``apply_promo_codes_before_validate(doc, ..)``— before_validate hook (Woo/Desk)
* ``record_redemptions_on_submit(doc, ..)``     — on_submit hook
* ``reverse_redemptions_on_cancel(doc, ..)``    — on_cancel hook
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import frappe


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PromoCodeResult:
    code: str
    accepted: bool
    discount_type: str | None = None
    discount_amount: float = 0.0
    free_delivery: bool = False
    reason: str | None = None


@dataclass
class PromoEvaluation:
    results: list = field(default_factory=list)
    total_discount: float = 0.0
    free_delivery: bool = False
    eligible_net_total: float = 0.0
    capped: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_codes(codes) -> list[str]:
    """Uppercase, strip, dedupe (preserve first-seen order), drop empties."""
    out: list[str] = []
    seen: set[str] = set()
    if not codes:
        return out
    if isinstance(codes, str):
        # Allow a JSON list string or a CSV string
        codes = _parse_codes_value(codes)
    for raw in codes or []:
        if raw is None:
            continue
        norm = str(raw).strip().upper()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _parse_codes_value(value) -> list[str]:
    """Parse a codes parameter that may be a JSON list, CSV, or list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        # Fall back to CSV
        return [piece for piece in text.split(",")]
    return [value]


def _line_amount(item) -> float:
    """Net amount of a single invoice line (post bundle/line discount).

    Falls back to ``price_list_rate`` when ``amount``/``rate`` are not yet
    populated — this is the case at the POS inline and ``before_validate``
    apply points, which run *before* ERPNext's ``calculate_taxes_and_totals``
    computes ``rate``/``amount`` (only ``price_list_rate`` is set by then).
    Mirrors ``delivery_promotions._get_merchandise_subtotal``.
    """
    amount = getattr(item, "amount", None)
    if not amount:
        rate = getattr(item, "rate", None)
        if not rate:
            rate = getattr(item, "price_list_rate", 0)
        rate = float(rate or 0)
        qty = float(getattr(item, "qty", 0) or 0)
        discount_pct = float(getattr(item, "discount_percentage", 0) or 0)
        discount_pct = min(max(discount_pct, 0.0), 100.0)
        amount = rate * qty * (1 - discount_pct / 100.0)
    return float(amount or 0)


def _whole_order_net(invoice_doc) -> float:
    total = 0.0
    for item in getattr(invoice_doc, "items", None) or []:
        total += _line_amount(item)
    return round(total, 2)


def _item_group_for(item_code: str) -> str | None:
    if not item_code:
        return None
    try:
        return frappe.get_cached_value("Item", item_code, "item_group")
    except Exception:
        return None


def _scope_sets(promo) -> tuple[set[str], set[str]]:
    """Return (item_codes, item_groups) configured in the promo's item_scope."""
    item_codes: set[str] = set()
    item_groups: set[str] = set()
    for row in getattr(promo, "item_scope", None) or []:
        scope_type = (getattr(row, "scope_type", "") or "").strip()
        if scope_type == "Item" and getattr(row, "item_code", None):
            item_codes.add(row.item_code)
        elif scope_type == "Item Group" and getattr(row, "item_group", None):
            item_groups.add(row.item_group)
    return item_codes, item_groups


def _eligible_net_for(promo, invoice_doc) -> float:
    """Compute the eligible net for a promo based on its applies_to scope."""
    applies_to = (getattr(promo, "applies_to", "") or "Whole Order").strip()
    if applies_to == "Whole Order":
        return _whole_order_net(invoice_doc)

    item_codes, item_groups = _scope_sets(promo)
    total = 0.0
    for item in getattr(invoice_doc, "items", None) or []:
        item_code = getattr(item, "item_code", None)
        if applies_to == "Specific Items":
            if item_code in item_codes:
                total += _line_amount(item)
        elif applies_to == "Item Groups":
            group = getattr(item, "item_group", None) or _item_group_for(item_code)
            if group in item_groups:
                total += _line_amount(item)
    return round(total, 2)


def _is_active_window(promo, now_dt) -> bool:
    active_from = getattr(promo, "active_from", None)
    active_to = getattr(promo, "active_to", None)
    if active_from and active_from > now_dt:
        return False
    if active_to and active_to < now_dt:
        return False
    return True


def _allowed_channels(promo) -> set[str]:
    return {
        (row.channel or "").strip().lower()
        for row in (getattr(promo, "channels", None) or [])
        if (row.channel or "").strip()
    }


def _active_redemption_count(code: str, *, exclude_invoice=None, customer=None) -> int:
    filters = {"promo_code": code, "status": "Active"}
    if customer:
        filters["customer"] = customer
    if exclude_invoice:
        filters["sales_invoice"] = ["!=", exclude_invoice]
    try:
        return frappe.db.count("Jarz Promo Redemption", filters)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Core evaluation (pure / read-only)
# ---------------------------------------------------------------------------


def evaluate_promo_codes(
    invoice_doc,
    codes,
    *,
    customer=None,
    customer_doc=None,
    pos_profile=None,
    channel: str = "flutter",
    is_pickup: bool = False,
    now_dt=None,
) -> PromoEvaluation:
    """Evaluate a set of promo codes against ``invoice_doc``.

    PURE / read-only: performs no DB writes.  Usage-limit checks here are
    advisory and exclude the current invoice so a re-evaluation of an existing
    invoice does not count itself.
    """
    channel = (channel or "flutter").strip().lower()
    if now_dt is None:
        now_dt = frappe.utils.now_datetime()

    norm_codes = _normalize_codes(codes)
    whole_net = _whole_order_net(invoice_doc)
    invoice_company = getattr(invoice_doc, "company", None)
    invoice_name = getattr(invoice_doc, "name", None)
    if customer is None:
        customer = getattr(customer_doc, "name", None) or getattr(invoice_doc, "customer", None)

    results: list[PromoCodeResult] = []
    # Pending = passed all per-code gates; eligible for stacking/application.
    pending: list[tuple] = []  # (promo, eligible_net, PromoCodeResult)

    for code in norm_codes:
        result = PromoCodeResult(code=code, accepted=False)
        results.append(result)

        try:
            promo = frappe.get_doc("Jarz Promo Code", code)
        except Exception:
            result.reason = "unknown or disabled"
            continue

        if not promo or not getattr(promo, "enabled", 0):
            result.reason = "unknown or disabled"
            continue

        result.discount_type = getattr(promo, "discount_type", None)

        # Active window
        if not _is_active_window(promo, now_dt):
            result.reason = "code is not active for the current date"
            continue

        # Channel
        allowed = _allowed_channels(promo)
        if allowed and channel not in allowed:
            result.reason = f"not valid for channel '{channel}'"
            continue

        # Pickup
        if is_pickup and not getattr(promo, "allow_pickup", 0):
            result.reason = "not valid for pickup orders"
            continue

        # Company
        if getattr(promo, "company", None) and invoice_company and promo.company != invoice_company:
            result.reason = "not valid for this company"
            continue

        # Eligible net for this code's scope
        eligible_net = _eligible_net_for(promo, invoice_doc)
        if eligible_net <= 0:
            result.reason = "no eligible items in cart"
            continue

        # Min order amount (against the code's scoped eligible net)
        min_order = float(getattr(promo, "min_order_amount", 0) or 0)
        if min_order > 0 and eligible_net < min_order:
            result.reason = f"order below minimum ({min_order:.2f})"
            continue

        # Usage limits (advisory, read-only; exclude current invoice)
        usage_limit = int(getattr(promo, "usage_limit", 0) or 0)
        if usage_limit > 0:
            used = _active_redemption_count(code, exclude_invoice=invoice_name)
            if used >= usage_limit:
                result.reason = "usage limit reached"
                continue

        per_customer_limit = int(getattr(promo, "per_customer_limit", 0) or 0)
        if per_customer_limit > 0 and customer:
            used_c = _active_redemption_count(
                code, exclude_invoice=invoice_name, customer=customer
            )
            if used_c >= per_customer_limit:
                result.reason = "per-customer limit reached"
                continue

        pending.append((promo, eligible_net, result))

    # Stacking: if any pending code is non-stackable AND there is >1 pending
    # code, reject the non-stackable one(s).
    if len(pending) > 1:
        survivors: list[tuple] = []
        for promo, eligible_net, result in pending:
            if not int(getattr(promo, "stackable", 0) or 0):
                result.reason = "not stackable with other codes"
            else:
                survivors.append((promo, eligible_net, result))
        pending = survivors

    # Sort accepted by (priority asc, code asc) and apply on a diminishing base.
    pending.sort(
        key=lambda t: (
            int(getattr(t[0], "priority", 0) or 0),
            t[2].code,
        )
    )

    remaining_net = whole_net
    total_discount = 0.0
    free_delivery = False

    for promo, eligible_net, result in pending:
        discount_type = (getattr(promo, "discount_type", "") or "").strip()
        value = float(getattr(promo, "discount_value", 0) or 0)
        base = min(eligible_net, remaining_net)
        if base < 0:
            base = 0.0

        amount = 0.0
        if discount_type == "Free Delivery":
            result.free_delivery = True
            free_delivery = True
            amount = 0.0
        elif discount_type == "Percentage":
            amount = base * value / 100.0
            cap = float(getattr(promo, "max_discount_amount", 0) or 0)
            if cap > 0:
                amount = min(amount, cap)
        elif discount_type == "Fixed Amount":
            amount = min(value, base)
        else:
            # Unknown discount type — treat as no-op but still accepted
            amount = 0.0

        amount = max(round(float(amount or 0), 2), 0.0)
        result.accepted = True
        result.discount_amount = amount
        total_discount += amount
        remaining_net -= amount
        if remaining_net < 0:
            remaining_net = 0.0

    # Final clamp against the whole-order net
    total_discount = round(total_discount, 2)
    capped = False
    if total_discount > whole_net:
        total_discount = whole_net
        capped = True

    return PromoEvaluation(
        results=results,
        total_discount=round(total_discount, 2),
        free_delivery=free_delivery,
        eligible_net_total=whole_net,
        capped=capped,
    )


# ---------------------------------------------------------------------------
# Apply an evaluation to a document (no save)
# ---------------------------------------------------------------------------


def apply_promo_evaluation_to_invoice(invoice_doc, evaluation, *, woo_discount_total=None) -> None:
    """Mutate ``invoice_doc`` to reflect ``evaluation``.  Does NOT save."""
    total = round(float(evaluation.total_discount or 0), 2)

    # Apply discount via ERPNext's net-total discount mechanism (idempotent SET).
    invoice_doc.apply_discount_on = "Net Total"
    invoice_doc.discount_amount = total

    # Free delivery → waive any shipping/delivery tax rows.
    if evaluation.free_delivery:
        existing = getattr(invoice_doc, "taxes", None) or []
        kept = []
        for tax in existing:
            desc = (tax.get("description") if hasattr(tax, "get") else getattr(tax, "description", "")) or ""
            desc_l = desc.strip().lower()
            if desc_l.startswith("shipping income") or desc_l.startswith("delivery charges"):
                continue
            kept.append(tax)
        invoice_doc.set("taxes", kept)

    # Custom fields
    invoice_doc.custom_promo_discount_total = total
    invoice_doc.custom_promo_applied = 1

    # Mismatch detection vs Woo's declared discount
    if woo_discount_total is not None:
        woo_total = round(float(woo_discount_total or 0), 2)
        invoice_doc.custom_promo_woo_discount_total = woo_total
        rejected = [r.code for r in evaluation.results if not r.accepted]
        delta = round(total - woo_total, 2)
        if abs(delta) > 0.01 or rejected:
            invoice_doc.custom_promo_discount_mismatch = 1
            note_parts = []
            if rejected:
                note_parts.append("Rejected: " + ", ".join(rejected))
            note_parts.append(f"ERP={total:.2f} Woo={woo_total:.2f} delta={delta:.2f}")
            invoice_doc.custom_promo_mismatch_note = " | ".join(note_parts)[:140]


# ---------------------------------------------------------------------------
# Hook entrypoints
# ---------------------------------------------------------------------------


def apply_promo_codes_before_validate(doc, method=None):
    """Single apply path for Woo / Desk invoices (NOT the inline POS path).

    Runs in ``before_validate`` so the controller's subsequent
    ``calculate_taxes_and_totals`` picks up ``discount_amount``.  Never raises.
    """
    try:
        if not doc.get("custom_promo_codes"):
            return
        if doc.get("custom_promo_applied"):
            return

        codes = _parse_codes_value(doc.get("custom_promo_codes"))
        codes = _normalize_codes(codes)
        if not codes:
            return

        channel = "woo" if doc.get("woo_order_id") else "desk"
        customer = doc.get("customer")
        is_pickup = bool(doc.get("custom_is_pickup") or doc.get("is_pickup") or 0)

        evaluation = evaluate_promo_codes(
            doc,
            codes,
            customer=customer,
            channel=channel,
            is_pickup=is_pickup,
        )
        apply_promo_evaluation_to_invoice(
            doc,
            evaluation,
            woo_discount_total=doc.get("custom_promo_woo_discount_total"),
        )
    except Exception:
        frappe.log_error(
            title="promo: apply_promo_codes_before_validate failed",
            message=frappe.get_traceback(),
        )


def _per_code_discounts(doc, codes, channel):
    """Re-evaluate to attribute a discount amount per accepted code.

    Returns a mapping ``{code: PromoCodeResult}`` for accepted codes only.
    """
    try:
        evaluation = evaluate_promo_codes(
            doc,
            codes,
            customer=doc.get("customer"),
            channel=channel,
            is_pickup=bool(doc.get("custom_is_pickup") or doc.get("is_pickup") or 0),
        )
    except Exception:
        return {}
    return {r.code: r for r in evaluation.results if r.accepted}


def record_redemptions_on_submit(doc, method=None):
    """Record (idempotently) a redemption per applied code, concurrency-safe.

    Enforces usage / per-customer limits under a row lock on the promo code so
    concurrent submits cannot exceed the cap.  May ``frappe.throw`` to abort
    the submit when a cap would be exceeded.
    """
    if not doc.get("custom_promo_codes"):
        return
    if float(doc.get("custom_promo_discount_total") or 0) <= 0:
        return

    codes = _normalize_codes(_parse_codes_value(doc.get("custom_promo_codes")))
    if not codes:
        return

    channel = "woo" if doc.get("woo_order_id") else (doc.get("custom_promo_channel") or "desk")
    customer = doc.get("customer")
    woo_order_id = doc.get("woo_order_id")

    # Re-evaluate to attribute per-code discount amounts.
    per_code = _per_code_discounts(doc, codes, channel)

    for code in codes:
        result = per_code.get(code)
        if result is None:
            # Code was not accepted at evaluation time — do not record.
            continue

        # Row lock the promo code for this transaction.
        try:
            frappe.db.sql(
                "select name from `tabJarz Promo Code` where name=%s for update",
                code,
            )
        except Exception:
            # If the code no longer exists, skip silently.
            continue

        promo = frappe.get_doc("Jarz Promo Code", code)
        usage_limit = int(getattr(promo, "usage_limit", 0) or 0)
        per_customer_limit = int(getattr(promo, "per_customer_limit", 0) or 0)

        # Idempotency matches the doctype's before_insert guard, which blocks a
        # second row for (promo_code, sales_invoice) regardless of status.
        existing_name = frappe.db.exists(
            "Jarz Promo Redemption",
            {"promo_code": code, "sales_invoice": doc.name},
        )

        if existing_name:
            # Re-submit / re-run: ensure it is Active again.
            current_status = frappe.db.get_value(
                "Jarz Promo Redemption", existing_name, "status"
            )
            if current_status != "Active":
                frappe.db.set_value(
                    "Jarz Promo Redemption", existing_name, "status", "Active"
                )
        else:
            # Enforce caps counting existing Active redemptions (excluding this SI).
            if usage_limit > 0:
                used = _active_redemption_count(code, exclude_invoice=doc.name)
                if used + 1 > usage_limit:
                    frappe.throw(
                        frappe._("Promo code {0} has reached its usage limit").format(code)
                    )
            if per_customer_limit > 0 and customer:
                used_c = _active_redemption_count(
                    code, exclude_invoice=doc.name, customer=customer
                )
                if used_c + 1 > per_customer_limit:
                    frappe.throw(
                        frappe._(
                            "Promo code {0} has reached its per-customer limit"
                        ).format(code)
                    )

            redemption = frappe.new_doc("Jarz Promo Redemption")
            redemption.promo_code = code
            redemption.sales_invoice = doc.name
            redemption.customer = customer
            redemption.channel = channel
            redemption.discount_applied = round(float(result.discount_amount or 0), 2)
            if woo_order_id is not None:
                redemption.woo_order_id = str(woo_order_id)
            redemption.status = "Active"
            redemption.insert(ignore_permissions=True)

        # Recompute times_used = count of Active redemptions for this code.
        count = frappe.db.count(
            "Jarz Promo Redemption", {"promo_code": code, "status": "Active"}
        )
        frappe.db.set_value(
            "Jarz Promo Code", code, "times_used", count, update_modified=False
        )


def reverse_redemptions_on_cancel(doc, method=None):
    """Mark this invoice's redemptions as Reversed and recompute times_used."""
    try:
        names = frappe.get_all(
            "Jarz Promo Redemption",
            filters={"sales_invoice": doc.name, "status": "Active"},
            pluck="name",
        )
        if not names:
            return
        affected_codes: set[str] = set()
        for name in names:
            red = frappe.get_doc("Jarz Promo Redemption", name)
            affected_codes.add(red.promo_code)
            red.status = "Reversed"
            red.save(ignore_permissions=True)

        for code in affected_codes:
            count = frappe.db.count(
                "Jarz Promo Redemption", {"promo_code": code, "status": "Active"}
            )
            frappe.db.set_value(
                "Jarz Promo Code", code, "times_used", count, update_modified=False
            )
    except Exception:
        frappe.log_error(
            title="promo: reverse_redemptions_on_cancel failed",
            message=frappe.get_traceback(),
        )

"""
Executive Overview API — Jarz POS ERPNext Desk page.

A single management roll-up that **composes the existing analytics functions**
server-side (product, shipping, segmentation, forecasting) so the front-end
makes one call and no logic is duplicated.

Each sub-section is wrapped defensively: if one source fails, the rest of the
overview still renders.

Read-only.  Requires the JARZ Manager role.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES


def _ensure_jarz_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access the executive overview"), frappe.PermissionError)


def _default_dates() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=29)).isoformat(), today.isoformat()


def _safe(label: str, fn, default):
    """Run a sub-section loader, log + fall back on failure."""
    try:
        return fn()
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"executive_analytics: {label} failed")
        return default


@frappe.whitelist()
def get_executive_overview(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Composed executive roll-up across the four analytics domains.

    ``branch`` (optional POS Profile name) is passed to product, P&L, shipping
    and inventory. With a branch:

    * revenue, gross profit, orders and B2B / B2C revenue come from that
      branch's row of the P&L ``branches`` split (invoice net sales, actual cost
      of goods net of returns; B2B/B2C = net sales + billed shipping, the same
      basis as the company figures) — never from the P&L ``summary``, which is
      company-wide;
    * shipping expense / income / net come from the branch's invoices
      (shipping summary KPIs), not the company-wide ledger;
    * critical / watch stock are the branch's restock statuses (its warehouse,
      its own sales);
    * net profit and net margin stay company-wide (overheads are not split by
      branch) and the customer segment mix stays company-wide — both listed in
      ``scope_notes``.

    Additive keys: ``filters`` and ``scope_notes``. A ``branch`` that is not an
    existing POS Profile is refused with a ``ValidationError`` (before, every
    section swallowed it and the overview rendered as an all-zero branch).
    """
    _ensure_jarz_manager()
    from jarz_pos.api.financial_report import validate_branch

    branch = validate_branch(branch)

    if not date_from or not date_to:
        date_from, date_to = _default_dates()

    # ── Product / sales (revenue, margin, mix, trend, territory) ─────────
    def _load_product():
        from jarz_pos.api.product_analytics import get_product_analytics
        return get_product_analytics(date_from=date_from, date_to=date_to, branch=branch)

    product = _safe("product", _load_product, {})
    p_summary = product.get("summary", {}) if isinstance(product, dict) else {}
    revenue_trend = product.get("trend", []) if isinstance(product, dict) else []
    product_mix = product.get("by_product_type", []) if isinstance(product, dict) else []
    top_territories = (product.get("by_territory", []) if isinstance(product, dict) else [])[:5]

    # ── Ledger P&L (revenue, gross profit, shipping, net profit) ─────────
    # The headline money comes from the ledger-reconciled P&L, not from the
    # product breakdown: product analytics counts only classified jar/bundle
    # lines, before invoice discounts and without returns, which overstated
    # September 2026 on production by 3,844 (270,945 vs the ledger's 267,101).
    def _load_pnl():
        from jarz_pos.api.financial_report import get_profit_and_loss
        if branch:
            return get_profit_and_loss(date_from=date_from, date_to=date_to, branch=branch)
        return get_profit_and_loss(date_from=date_from, date_to=date_to)

    pnl = _safe("pnl", _load_pnl, {})
    pnl_summary = pnl.get("summary", {}) if isinstance(pnl, dict) else {}
    pnl_shipping = pnl.get("shipping", {}) if isinstance(pnl, dict) else {}
    pnl_quality = pnl.get("data_quality", {}) if isinstance(pnl, dict) else {}
    pnl_branches = pnl.get("branches", []) if isinstance(pnl, dict) else []

    branch_row = None
    if branch:
        branch_row = next((r for r in pnl_branches or [] if r.get("branch") == branch), None)

    # B2B / B2C revenue: the P&L summary is company-wide, so a branch reads its
    # own row (no row = the branch invoiced nothing in the range).
    if branch:
        b2b_revenue = float((branch_row or {}).get("b2b_revenue") or 0)
        b2c_revenue = float((branch_row or {}).get("b2c_revenue") or 0)
    else:
        b2b_revenue = float(pnl_summary.get("b2b_revenue") or 0)
        b2c_revenue = float(pnl_summary.get("b2c_revenue") or 0)

    if branch:
        if branch_row:
            total_revenue = float(branch_row.get("revenue") or 0)
            gross_profit = float(branch_row.get("gross_profit") or 0)
            orders = int(branch_row.get("orders") or 0)
        else:
            # No P&L row: the branch had no invoices, or the P&L failed; fall
            # back to the branch-filtered product breakdown.
            total_revenue = float(p_summary.get("total_revenue") or 0)
            gross_profit = float(p_summary.get("total_gross_profit") or 0)
            orders = int(p_summary.get("total_orders") or 0)
    elif pnl_summary:
        total_revenue = float(pnl_summary.get("sales") or 0)
        gross_profit = float(pnl_summary.get("gross_profit") or 0)
        orders = int(pnl_summary.get("orders") or 0)
    else:
        total_revenue = float(p_summary.get("total_revenue") or 0)
        gross_profit = float(p_summary.get("total_gross_profit") or 0)
        orders = int(p_summary.get("total_orders") or 0)
    gross_margin = round(gross_profit / total_revenue * 100, 1) if total_revenue else 0.0
    avg_order_value = round(total_revenue / orders, 2) if orders else 0.0

    # ── Shipping (courier-side KPIs: unsettled balances, pending overrides) ──
    def _load_shipping():
        from jarz_pos.api.shipping_analytics import get_summary_kpis
        if branch:
            return get_summary_kpis(from_date=date_from, to_date=date_to, branch=branch)
        return get_summary_kpis(from_date=date_from, to_date=date_to)

    shipping = _safe("shipping", _load_shipping, {})
    if branch:
        # The ledger's shipping lines are company-wide; a branch reads its own
        # invoices' courier cost and billed shipping instead.
        pnl_shipping = {}

    # ── Customer segment mix ─────────────────────────────────────────────
    def _load_segments():
        from jarz_pos.services.rfm_segmentation import get_segment_summary
        # Drop pseudo rows (Regular): they overlap the real segments.
        return [r for r in get_segment_summary() if not r.get("pseudo")]

    segment_mix = _safe("segments", _load_segments, [])
    total_customers = sum(int(r.get("count") or 0) for r in segment_mix)

    # ── Inventory alerts ─────────────────────────────────────────────────
    def _load_inventory():
        if branch:
            # The branch's own restock statuses, in the alert-list row shape.
            from jarz_pos.api.inventory_analytics import get_restock

            restock = get_restock(branch).get("restock", [])

            def _as_alert(r):
                return {
                    "item_code": r.get("item_code"),
                    "item_name": r.get("item_name"),
                    "item_group": r.get("item_group"),
                    "daily_velocity": r.get("velocity_30d"),
                    "stock_on_hand": r.get("stock"),
                    "days_remaining": r.get("days_cover"),
                }

            return {
                "critical": [_as_alert(r) for r in restock if r.get("status") == "critical"],
                "watch_list": [_as_alert(r) for r in restock if r.get("status") == "watch"],
            }
        from jarz_pos.services.demand_forecasting import build_alert_data, get_settings
        return build_alert_data(get_settings())

    inv = _safe("inventory", _load_inventory, {})
    critical_items = inv.get("critical", []) if isinstance(inv, dict) else []
    watch_items = inv.get("watch_list", []) if isinstance(inv, dict) else []

    # ── Consolidated alerts (shipping + inventory critical) ──────────────
    def _load_ship_alerts():
        from jarz_pos.api.shipping_analytics import get_alerts_data
        if branch:
            return get_alerts_data(from_date=date_from, to_date=date_to, branch=branch)
        return get_alerts_data(from_date=date_from, to_date=date_to)

    # Incomplete books first: they qualify every number below them.
    alerts: List[Dict[str, str]] = []
    warnings = pnl_quality.get("warnings") or []
    if "cogs_incomplete" in warnings:
        alerts.append({
            "type": "warning",
            "message": (
                f"Cost of goods is posted for only <b>{pnl_quality.get('cogs_coverage_pct', 0)}%</b> "
                f"of orders in this period, so gross profit is overstated."
            ),
        })
    if "shipping_expense_incomplete" in warnings:
        alerts.append({
            "type": "warning",
            "message": (
                f"Only <b>{pnl_quality.get('shipping_expense_coverage_pct', 0)}%</b> of the courier "
                f"cost recorded on orders has reached the ledger in this period."
            ),
        })
    alerts.extend(_safe("ship_alerts", _load_ship_alerts, []) or [])

    for item in critical_items[:8]:
        days = item.get("days_remaining")
        alerts.append({
            "type": "danger",
            "message": (
                f"Stock alert: <b>{item.get('item_name') or item.get('item_code')}</b> "
                f"has <b>{int(days or 0)} days</b> of stock left "
                f"({float(item.get('daily_velocity') or 0):.1f}/day)"
            ),
        })

    # ── Headline KPIs ────────────────────────────────────────────────────
    kpis = {
        "total_revenue": round(total_revenue, 2),
        "total_orders": orders,
        "gross_profit": round(gross_profit, 2),
        "gross_margin": gross_margin,
        "avg_order_value": avg_order_value,
        "shipping_expense": float(pnl_shipping.get("expense", shipping.get("total_expense")) or 0),
        "delivery_income": float(pnl_shipping.get("income", shipping.get("total_income")) or 0),
        "net_shipping_pl": float(pnl_shipping.get("net", shipping.get("net_pl")) or 0),
        "net_profit": float(pnl_summary.get("net_profit") or 0),
        "net_margin": float(pnl_summary.get("net_margin_pct") or 0),
        "b2b_revenue": b2b_revenue,
        "b2c_revenue": b2c_revenue,
        "avg_cost_per_order": float(shipping.get("avg_cost_per_order") or 0),
        "unsettled_courier_total": float(shipping.get("unsettled_courier_total") or 0),
        "pending_overrides": int(shipping.get("pending_csr_count") or 0),
        "total_customers": total_customers,
        "critical_stock": len(critical_items),
        "watch_stock": len(watch_items),
    }

    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "kpis": kpis,
        "revenue_trend": revenue_trend,
        "product_mix": product_mix,
        "segment_mix": segment_mix,
        "top_territories": top_territories,
        "alerts": alerts,
        "filters": {"date_from": date_from, "date_to": date_to, "branch": branch},
        "scope_notes": (
            ["net_profit_company_wide", "segment_mix_company_wide", "unsettled_courier_total_current"]
            if branch
            else []
        ),
    }

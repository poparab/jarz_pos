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
) -> Dict[str, Any]:
    """Composed executive roll-up across the four analytics domains."""
    _ensure_jarz_manager()

    if not date_from or not date_to:
        date_from, date_to = _default_dates()

    # ── Product / sales (revenue, margin, mix, trend, territory) ─────────
    def _load_product():
        from jarz_pos.api.product_analytics import get_product_analytics
        return get_product_analytics(date_from=date_from, date_to=date_to)

    product = _safe("product", _load_product, {})
    p_summary = product.get("summary", {}) if isinstance(product, dict) else {}
    revenue_trend = product.get("trend", []) if isinstance(product, dict) else []
    product_mix = product.get("by_product_type", []) if isinstance(product, dict) else []
    top_territories = (product.get("by_territory", []) if isinstance(product, dict) else [])[:5]

    total_revenue = float(p_summary.get("total_revenue") or 0)
    gross_profit = float(p_summary.get("total_gross_profit") or 0)
    gross_margin = round(gross_profit / total_revenue * 100, 1) if total_revenue else 0.0

    # ── Shipping P&L ─────────────────────────────────────────────────────
    def _load_shipping():
        from jarz_pos.api.shipping_analytics import get_summary_kpis
        return get_summary_kpis(from_date=date_from, to_date=date_to)

    shipping = _safe("shipping", _load_shipping, {})

    # ── Customer segment mix ─────────────────────────────────────────────
    def _load_segments():
        from jarz_pos.services.rfm_segmentation import get_segment_summary
        return get_segment_summary()

    segment_mix = _safe("segments", _load_segments, [])
    total_customers = sum(int(r.get("count") or 0) for r in segment_mix)

    # ── Inventory alerts ─────────────────────────────────────────────────
    def _load_inventory():
        from jarz_pos.services.demand_forecasting import build_alert_data, get_settings
        return build_alert_data(get_settings())

    inv = _safe("inventory", _load_inventory, {})
    critical_items = inv.get("critical", []) if isinstance(inv, dict) else []
    watch_items = inv.get("watch_list", []) if isinstance(inv, dict) else []

    # ── Consolidated alerts (shipping + inventory critical) ──────────────
    def _load_ship_alerts():
        from jarz_pos.api.shipping_analytics import get_alerts_data
        return get_alerts_data(from_date=date_from, to_date=date_to)

    alerts: List[Dict[str, str]] = list(_safe("ship_alerts", _load_ship_alerts, []) or [])

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
        "total_orders": int(p_summary.get("total_orders") or 0),
        "gross_profit": round(gross_profit, 2),
        "gross_margin": gross_margin,
        "avg_order_value": float(p_summary.get("avg_order_value") or 0),
        "shipping_expense": float(shipping.get("total_expense") or 0),
        "delivery_income": float(shipping.get("total_income") or 0),
        "net_shipping_pl": float(shipping.get("net_pl") or 0),
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
    }

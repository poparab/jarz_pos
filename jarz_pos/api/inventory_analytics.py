"""
Inventory Intelligence API — Jarz POS ERPNext Desk page.

Surfaces the demand-forecasting / velocity engine that already runs weekly
(``jarz_pos.services.demand_forecasting``).  Velocity, trend and days-of-stock
are **precomputed** on the Item DocType, so most panels are a current-state
snapshot; the date range only scopes the "top sellers in range" panel.

Reuses ``demand_forecasting.build_alert_data`` / ``get_settings`` for the four
alert lists (critical / watch / slow movers / overstocked) so the thresholds
stay consistent with the nightly email digest.

Read-only.  Requires the JARZ Manager role.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES

# Canonical display order for velocity trend buckets.
TREND_ORDER = ["Accelerating", "Stable", "Declining", "New Item", "No Sales", "Unrated"]


def _ensure_jarz_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access inventory analytics"), frappe.PermissionError)


def _default_dates() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=29)).isoformat(), today.isoformat()


def _trend_sort_key(trend: str) -> int:
    try:
        return TREND_ORDER.index(trend)
    except ValueError:
        return len(TREND_ORDER)


@frappe.whitelist()
def get_inventory_analytics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregated inventory / velocity analytics.

    Response shape:
    {
      "period": {"date_from": str, "date_to": str},
      "summary": {"total_stock_items", "critical_count", "watch_count",
                  "slow_count", "overstock_count", "total_stock_value"},
      "alerts": {"critical": [...], "watch_list": [...],
                 "slow_movers": [...], "overstocked": [...]},
      "velocity_distribution": [{"trend": str, "count": int}],
      "top_movers": [{"item_code", "item_name", "item_group",
                      "velocity_30d", "velocity_60d", "trend",
                      "days_of_stock", "stock_on_hand"}],
      "top_sold_in_range": [{"item_code", "item_name", "item_group",
                             "qty", "revenue"}]
    }
    """
    _ensure_jarz_manager()

    if not date_from or not date_to:
        date_from, date_to = _default_dates()

    # ── Alert lists (reuse the forecasting engine) ───────────────────────
    try:
        from jarz_pos.services.demand_forecasting import build_alert_data, get_settings
        alerts = build_alert_data(get_settings())
    except Exception:
        frappe.log_error(frappe.get_traceback(), "inventory_analytics: build_alert_data failed")
        alerts = {"critical": [], "watch_list": [], "slow_movers": [], "overstocked": []}

    # ── Headline counts + total stock value ──────────────────────────────
    total_stock_items = frappe.db.count("Item", {"is_stock_item": 1, "disabled": 0})

    stock_value_row = frappe.db.sql(
        """
        SELECT COALESCE(SUM(b.actual_qty * i.valuation_rate), 0) AS val
        FROM `tabBin` b
        JOIN `tabItem` i ON i.name = b.item_code
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
        """,
        as_dict=True,
    )
    total_stock_value = round(float(stock_value_row[0]["val"] or 0), 2) if stock_value_row else 0.0

    summary = {
        "total_stock_items": int(total_stock_items or 0),
        "critical_count": len(alerts.get("critical", [])),
        "watch_count": len(alerts.get("watch_list", [])),
        "slow_count": len(alerts.get("slow_movers", [])),
        "overstock_count": len(alerts.get("overstocked", [])),
        "total_stock_value": total_stock_value,
    }

    # ── Velocity-trend distribution ──────────────────────────────────────
    dist_rows = frappe.db.sql(
        """
        SELECT
            COALESCE(NULLIF(jarz_velocity_trend, ''), 'Unrated') AS trend,
            COUNT(*) AS count
        FROM `tabItem`
        WHERE is_stock_item = 1 AND disabled = 0
        GROUP BY trend
        """,
        as_dict=True,
    )
    velocity_distribution = sorted(
        [{"trend": r["trend"], "count": int(r["count"] or 0)} for r in dist_rows],
        key=lambda r: _trend_sort_key(r["trend"]),
    )

    # ── Top movers by 60-day velocity ────────────────────────────────────
    top_movers = frappe.db.sql(
        """
        SELECT
            i.name                AS item_code,
            i.item_name,
            i.item_group,
            i.jarz_velocity_30d   AS velocity_30d,
            i.jarz_velocity_60d   AS velocity_60d,
            i.jarz_velocity_trend AS trend,
            i.jarz_days_of_stock  AS days_of_stock,
            COALESCE(b.stock, 0)  AS stock_on_hand
        FROM `tabItem` i
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock
            FROM `tabBin` GROUP BY item_code
        ) b ON b.item_code = i.name
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
          AND i.jarz_velocity_60d > 0
        ORDER BY i.jarz_velocity_60d DESC
        LIMIT 15
        """,
        as_dict=True,
    )
    for r in top_movers:
        r["velocity_30d"] = round(float(r["velocity_30d"] or 0), 3)
        r["velocity_60d"] = round(float(r["velocity_60d"] or 0), 3)
        r["days_of_stock"] = int(r["days_of_stock"] or 0)
        r["stock_on_hand"] = round(float(r["stock_on_hand"] or 0), 2)

    # ── Top sellers in the selected range (date-scoped) ──────────────────
    top_sold_in_range = frappe.db.sql(
        """
        SELECT
            sii.item_code,
            sii.item_name,
            sii.item_group,
            SUM(sii.qty)            AS qty,
            COALESCE(SUM(sii.amount), 0) AS revenue
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
        GROUP BY sii.item_code, sii.item_name, sii.item_group
        ORDER BY qty DESC
        LIMIT 15
        """,
        {"fd": date_from, "td": date_to},
        as_dict=True,
    )
    for r in top_sold_in_range:
        r["qty"] = round(float(r["qty"] or 0), 2)
        r["revenue"] = round(float(r["revenue"] or 0), 2)

    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "summary": summary,
        "alerts": {
            "critical": alerts.get("critical", []),
            "watch_list": alerts.get("watch_list", []),
            "slow_movers": alerts.get("slow_movers", []),
            "overstocked": alerts.get("overstocked", []),
        },
        "velocity_distribution": velocity_distribution,
        "top_movers": top_movers,
        "top_sold_in_range": top_sold_in_range,
    }

"""
Customer Analytics API — Jarz POS ERPNext Desk page.

Surfaces the RFM segmentation that already runs nightly
(``jarz_pos.services.rfm_segmentation``).  Segment counts and per-customer RFM
metrics are **current-state** (stored on the Customer DocType); revenue figures
are **date-scoped** through ``tabSales Invoice``.

Data sources:
  tabCustomer       — customer_segment, rfm_recency_days, rfm_frequency_count,
                      rfm_avg_order_value (populated by the nightly RFM job)
  tabSales Invoice  — grand_total, posting_date (docstatus=1, is_return=0)

Read-only.  Requires the JARZ Manager role.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES

# Canonical display order for the 8 RFM segments + fallbacks.
SEGMENT_ORDER = [
    "Champion",
    "Loyal",
    "Potential Loyalist",
    "New Customer",
    "At Risk",
    "Can't Lose Them",
    "Lost",
    "One-Time",
    "Unclassified",
]

# Segments surfaced in the "needs attention" list.
AT_RISK_SEGMENTS = ["At Risk", "Can't Lose Them"]


def _ensure_jarz_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access customer analytics"), frappe.PermissionError)


def _default_dates() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=29)).isoformat(), today.isoformat()


def _segment_sort_key(segment: str) -> int:
    try:
        return SEGMENT_ORDER.index(segment)
    except ValueError:
        return len(SEGMENT_ORDER)


@frappe.whitelist()
def get_customer_analytics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregated customer / RFM analytics for the given date range.

    Response shape:
    {
      "period": {"date_from": str, "date_to": str},
      "summary": {
        "total_customers": int, "active_in_period": int, "new_customers": int,
        "returning_customers": int, "repeat_rate": float,
        "champions": int, "at_risk": int, "lost": int,
        "period_revenue": float, "period_orders": int, "avg_order_value": float
      },
      "segment_distribution": [{"segment": str, "count": int}],
      "segment_table": [{"segment", "customers", "active_customers",
                         "orders", "revenue", "avg_recency",
                         "avg_frequency", "avg_aov"}],
      "top_customers": [{"customer", "customer_name", "segment",
                         "recency_days", "orders", "revenue"}],
      "at_risk_customers": [{"customer", "customer_name", "segment",
                             "recency_days", "frequency", "avg_aov", "territory"}],
      "acquisition_trend": [{"date": str, "new_customers": int}]
    }
    """
    _ensure_jarz_manager()

    if not date_from or not date_to:
        date_from, date_to = _default_dates()

    params = {"fd": date_from, "td": date_to}

    # ── Snapshot: customer count + avg RFM metrics per segment ───────────
    snapshot_rows = frappe.db.sql(
        """
        SELECT
            COALESCE(customer_segment, 'Unclassified') AS segment,
            COUNT(*)                     AS customers,
            AVG(rfm_recency_days)        AS avg_recency,
            AVG(rfm_frequency_count)     AS avg_frequency,
            AVG(rfm_avg_order_value)     AS avg_aov
        FROM `tabCustomer`
        WHERE disabled = 0
        GROUP BY segment
        """,
        as_dict=True,
    )
    snapshot_map = {r["segment"]: r for r in snapshot_rows}

    # ── Date-scoped revenue per segment ──────────────────────────────────
    revenue_rows = frappe.db.sql(
        """
        SELECT
            COALESCE(c.customer_segment, 'Unclassified') AS segment,
            COUNT(*)                        AS orders,
            COALESCE(SUM(si.grand_total), 0) AS revenue,
            COUNT(DISTINCT si.customer)     AS active_customers
        FROM `tabSales Invoice` si
        JOIN `tabCustomer` c ON c.name = si.customer
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
        GROUP BY segment
        """,
        params,
        as_dict=True,
    )
    revenue_map = {r["segment"]: r for r in revenue_rows}

    # ── Merge snapshot + revenue into one ordered segment table ──────────
    all_segments = set(snapshot_map) | set(revenue_map)
    segment_table = []
    for seg in sorted(all_segments, key=_segment_sort_key):
        snap = snapshot_map.get(seg, {})
        rev = revenue_map.get(seg, {})
        segment_table.append({
            "segment": seg,
            "customers": int(snap.get("customers") or 0),
            "active_customers": int(rev.get("active_customers") or 0),
            "orders": int(rev.get("orders") or 0),
            "revenue": round(float(rev.get("revenue") or 0), 2),
            "avg_recency": round(float(snap.get("avg_recency") or 0), 1),
            "avg_frequency": round(float(snap.get("avg_frequency") or 0), 1),
            "avg_aov": round(float(snap.get("avg_aov") or 0), 2),
        })

    segment_distribution = [
        {"segment": s["segment"], "count": s["customers"]}
        for s in segment_table if s["customers"] > 0
    ]

    # ── Top customers by revenue in range ────────────────────────────────
    top_customers = frappe.db.sql(
        """
        SELECT
            si.customer,
            c.customer_name,
            COALESCE(c.customer_segment, 'Unclassified') AS segment,
            c.rfm_recency_days              AS recency_days,
            COUNT(*)                        AS orders,
            COALESCE(SUM(si.grand_total), 0) AS revenue
        FROM `tabSales Invoice` si
        JOIN `tabCustomer` c ON c.name = si.customer
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
        GROUP BY si.customer, c.customer_name, c.customer_segment, c.rfm_recency_days
        ORDER BY revenue DESC
        LIMIT 20
        """,
        params,
        as_dict=True,
    )
    for r in top_customers:
        r["revenue"] = round(float(r["revenue"] or 0), 2)
        r["orders"] = int(r["orders"] or 0)
        r["recency_days"] = int(r["recency_days"] or 0)

    # ── Customers needing attention (current snapshot) ───────────────────
    at_risk_customers = frappe.db.sql(
        """
        SELECT
            name           AS customer,
            customer_name,
            customer_segment AS segment,
            rfm_recency_days   AS recency_days,
            rfm_frequency_count AS frequency,
            rfm_avg_order_value AS avg_aov,
            territory
        FROM `tabCustomer`
        WHERE disabled = 0
          AND customer_segment IN %(segs)s
        ORDER BY rfm_avg_order_value DESC
        LIMIT 50
        """,
        {"segs": tuple(AT_RISK_SEGMENTS)},
        as_dict=True,
    )
    for r in at_risk_customers:
        r["recency_days"] = int(r["recency_days"] or 0)
        r["frequency"] = int(r["frequency"] or 0)
        r["avg_aov"] = round(float(r["avg_aov"] or 0), 2)

    # ── New-customer acquisition in range (first-ever order in window) ────
    acquisition_trend = frappe.db.sql(
        """
        SELECT f.first_date AS date, COUNT(*) AS new_customers
        FROM (
            SELECT customer, MIN(posting_date) AS first_date
            FROM `tabSales Invoice`
            WHERE docstatus = 1 AND is_return = 0 AND customer IS NOT NULL
            GROUP BY customer
        ) f
        WHERE f.first_date BETWEEN %(fd)s AND %(td)s
        GROUP BY f.first_date
        ORDER BY f.first_date
        """,
        params,
        as_dict=True,
    )
    for r in acquisition_trend:
        r["date"] = str(r["date"])
        r["new_customers"] = int(r["new_customers"] or 0)

    new_customers = sum(r["new_customers"] for r in acquisition_trend)

    # ── Period totals ────────────────────────────────────────────────────
    totals = frappe.db.sql(
        """
        SELECT
            COUNT(*)                        AS orders,
            COALESCE(SUM(grand_total), 0)    AS revenue,
            COUNT(DISTINCT customer)        AS active
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND posting_date BETWEEN %(fd)s AND %(td)s
        """,
        params,
        as_dict=True,
    )[0]

    period_orders = int(totals["orders"] or 0)
    period_revenue = round(float(totals["revenue"] or 0), 2)
    active_in_period = int(totals["active"] or 0)
    avg_order_value = round(period_revenue / period_orders, 2) if period_orders else 0.0
    returning_customers = max(0, active_in_period - new_customers)
    repeat_rate = round(returning_customers / active_in_period * 100, 1) if active_in_period else 0.0

    def _seg_count(seg: str) -> int:
        return int(snapshot_map.get(seg, {}).get("customers") or 0)

    total_customers = sum(int(r.get("customers") or 0) for r in snapshot_rows)

    summary = {
        "total_customers": total_customers,
        "active_in_period": active_in_period,
        "new_customers": new_customers,
        "returning_customers": returning_customers,
        "repeat_rate": repeat_rate,
        "champions": _seg_count("Champion"),
        "at_risk": _seg_count("At Risk") + _seg_count("Can't Lose Them"),
        "lost": _seg_count("Lost"),
        "period_revenue": period_revenue,
        "period_orders": period_orders,
        "avg_order_value": avg_order_value,
    }

    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "summary": summary,
        "segment_distribution": segment_distribution,
        "segment_table": segment_table,
        "top_customers": top_customers,
        "at_risk_customers": at_risk_customers,
        "acquisition_trend": acquisition_trend,
    }

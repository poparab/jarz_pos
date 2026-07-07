"""
B2B Sales & Clients Analytics API — Jarz POS ERPNext Desk / mobile dashboard.

Aggregates the B2B commercial funnel into a single management payload:

  Sales    — Sales Invoice ``docstatus=1 AND is_return=0 AND
             custom_order_purpose='B2B Supply'`` scoped to the date range.
  Clients  — Customer ``customer_group IN ('B2B','Distributor')`` (current
             state), with RFM fields (customer_segment, rfm_recency_days,
             rfm_frequency_count, rfm_avg_order_value) driving the at-risk feed.
  Pipeline — Lead + Opportunity aggregated by ``custom_b2b_stage``.
  Reorder  — Customers whose ``custom_predicted_next_order`` is due (mirrors
             ``crm.get_reorder_due``).
  Convert  — Opportunities in range vs. those converted to a Customer
             (``Customer.custom_source_opportunity``).

Every sub-query is wrapped defensively (try/except + frappe.log_error): a broken
section returns its empty default so the page never 500s. All returned values are
JSON-serializable primitives (Decimals -> float, dates -> ISO strings).

Read-only. Requires the JARZ Manager role.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES

# ── Customer groups that count as B2B clients ────────────────────────────
_B2B_GROUPS = ("B2B", "Distributor")

# ── Canonical B2B pipeline stage order (mirrors custom_b2b_stage options) ─
_STAGE_ORDER = [
    "Lead",
    "Qualify",
    "Sample",
    "Approved",
    "Trial",
    "Check-up",
    "Active",
    "Lost/On-hold",
]

# Stages considered "open" (still in-flight) for pipeline_open_value.
_CLOSED_STAGES = {"Active", "Lost/On-hold"}

# Customer RFM segments surfaced as "at risk".
_AT_RISK_SEGMENTS = ("At Risk", "Can't Lose Them")

_B2B_PURPOSE = "B2B Supply"


def _ensure_jarz_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access B2B analytics"), frappe.PermissionError)


def _default_dates() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=29)).isoformat(), today.isoformat()


def _safe(label: str, fn, default):
    """Run a sub-section loader, log + fall back on failure."""
    try:
        return fn()
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"b2b_analytics: {label} failed")
        return default


@frappe.whitelist()
def get_b2b_analytics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Composed B2B sales + clients + pipeline analytics for the date range.

    See the shared analytics contract ("B2B Sales & Clients") for the exact
    return shape. Every section defaults to empty/zero on failure.
    """
    _ensure_jarz_manager()

    if not date_from or not date_to:
        date_from, date_to = _default_dates()

    params = {"fd": date_from, "td": date_to}

    # ── Sales sections ───────────────────────────────────────────────────
    sales_totals = _safe(
        "sales_totals", lambda: _sales_totals(params),
        {"revenue": 0.0, "orders": 0, "active_clients": 0, "cost": 0.0},
    )
    revenue_trend = _safe("revenue_trend", lambda: _revenue_trend(params), [])
    top_clients = _safe("top_clients", lambda: _top_clients(params), [])
    revenue_by_policy = _safe("revenue_by_policy", lambda: _revenue_by_policy(params), [])
    revenue_by_territory = _safe("revenue_by_territory", lambda: _revenue_by_territory(params), [])

    # ── Clients sections ─────────────────────────────────────────────────
    clients = _safe(
        "clients", lambda: _clients(params),
        {"total_b2b_clients": 0, "new_clients": 0, "clients_by_group": []},
    )
    at_risk = _safe(
        "at_risk", lambda: _at_risk_clients(params),
        {"at_risk_clients": [], "at_risk_count": 0},
    )

    # ── Pipeline / reorder / conversion ──────────────────────────────────
    pipeline = _safe(
        "pipeline", _pipeline,
        {"pipeline_by_stage": [], "pipeline_open_value": 0.0},
    )
    reorder = _safe(
        "reorder", _reorder_due,
        {"reorder_due": [], "reorder_due_count": 0},
    )
    conversion = _safe(
        "conversion", lambda: _conversion(params),
        {"opportunities": 0, "won": 0, "conversion_rate": 0.0},
    )

    # ── Assemble summary ─────────────────────────────────────────────────
    revenue = float(sales_totals.get("revenue") or 0)
    orders = int(sales_totals.get("orders") or 0)
    cost = float(sales_totals.get("cost") or 0)
    gross_profit = round(revenue - cost, 2)
    gross_margin_pct = round(gross_profit / revenue * 100, 1) if revenue else 0.0
    avg_order_value = round(revenue / orders, 2) if orders else 0.0

    summary = {
        "b2b_revenue": round(revenue, 2),
        "b2b_orders": orders,
        "active_clients": int(sales_totals.get("active_clients") or 0),
        "new_clients": int(clients.get("new_clients") or 0),
        "avg_order_value": avg_order_value,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "reorder_due_count": int(reorder.get("reorder_due_count") or 0),
        "at_risk_count": int(at_risk.get("at_risk_count") or 0),
        "total_b2b_clients": int(clients.get("total_b2b_clients") or 0),
        "pipeline_open_value": round(float(pipeline.get("pipeline_open_value") or 0), 2),
    }

    alerts = _safe(
        "alerts",
        lambda: _build_alerts(summary, pipeline, conversion),
        [],
    )

    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "summary": summary,
        "pipeline_by_stage": pipeline.get("pipeline_by_stage", []),
        "revenue_trend": revenue_trend,
        "top_clients": top_clients,
        "revenue_by_policy": revenue_by_policy,
        "revenue_by_territory": revenue_by_territory,
        "clients_by_group": clients.get("clients_by_group", []),
        "reorder_due": reorder.get("reorder_due", []),
        "at_risk_clients": at_risk.get("at_risk_clients", []),
        "conversion": conversion,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------
def _sales_totals(params: Dict[str, str]) -> Dict[str, Any]:
    """Headline B2B revenue / order count / distinct clients + BOM cost."""
    row = frappe.db.sql(
        """
        SELECT
            COUNT(*)                          AS orders,
            COALESCE(SUM(grand_total), 0)     AS revenue,
            COUNT(DISTINCT customer)          AS active_clients
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND custom_order_purpose = %(purpose)s
          AND posting_date BETWEEN %(fd)s AND %(td)s
        """,
        {**params, "purpose": _B2B_PURPOSE},
        as_dict=True,
    )[0]

    # BOM-based cost (default+active BOM per item, qty-weighted). Wrapped so a
    # missing BOM table / column never breaks the revenue headline.
    cost = 0.0
    try:
        cost_row = frappe.db.sql(
            """
            SELECT COALESCE(SUM(sii.qty * COALESCE(b.total_cost, 0)), 0) AS total_cost
            FROM `tabSales Invoice Item` sii
            JOIN `tabSales Invoice` si ON si.name = sii.parent
            LEFT JOIN `tabBOM` b
                   ON b.item = sii.item_code
                  AND b.is_active = 1
                  AND b.is_default = 1
            WHERE si.docstatus = 1
              AND si.is_return = 0
              AND si.custom_order_purpose = %(purpose)s
              AND si.posting_date BETWEEN %(fd)s AND %(td)s
            """,
            {**params, "purpose": _B2B_PURPOSE},
            as_dict=True,
        )[0]
        cost = float(cost_row.get("total_cost") or 0)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "b2b_analytics: sales cost failed")
        cost = 0.0

    return {
        "revenue": float(row.get("revenue") or 0),
        "orders": int(row.get("orders") or 0),
        "active_clients": int(row.get("active_clients") or 0),
        "cost": cost,
    }


def _revenue_trend(params: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = frappe.db.sql(
        """
        SELECT
            posting_date,
            COALESCE(SUM(grand_total), 0) AS revenue,
            COUNT(*)                      AS orders
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND custom_order_purpose = %(purpose)s
          AND posting_date BETWEEN %(fd)s AND %(td)s
        GROUP BY posting_date
        ORDER BY posting_date ASC
        """,
        {**params, "purpose": _B2B_PURPOSE},
        as_dict=True,
    )
    return [
        {
            "posting_date": str(r["posting_date"]),
            "revenue": round(float(r["revenue"] or 0), 2),
            "orders": int(r["orders"] or 0),
        }
        for r in rows
    ]


def _top_clients(params: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = frappe.db.sql(
        """
        SELECT
            si.customer,
            c.customer_name,
            COALESCE(c.customer_segment, 'Unclassified') AS segment,
            COUNT(*)                          AS orders,
            COALESCE(SUM(si.grand_total), 0)  AS revenue,
            MAX(si.posting_date)              AS last_order_date
        FROM `tabSales Invoice` si
        LEFT JOIN `tabCustomer` c ON c.name = si.customer
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.custom_order_purpose = %(purpose)s
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
        GROUP BY si.customer, c.customer_name, c.customer_segment
        ORDER BY revenue DESC
        LIMIT 20
        """,
        {**params, "purpose": _B2B_PURPOSE},
        as_dict=True,
    )
    return [
        {
            "customer": r["customer"],
            "customer_name": r["customer_name"] or r["customer"],
            "revenue": round(float(r["revenue"] or 0), 2),
            "orders": int(r["orders"] or 0),
            "last_order_date": str(r["last_order_date"]) if r["last_order_date"] else None,
            "segment": r["segment"],
        }
        for r in rows
    ]


def _revenue_by_policy(params: Dict[str, str]) -> List[Dict[str, Any]]:
    """Revenue grouped by the commercial policy applied to each B2B invoice."""
    rows = frappe.db.sql(
        """
        SELECT
            COALESCE(NULLIF(custom_commercial_policy, ''), '(No Policy)') AS policy,
            COUNT(*)                          AS order_count,
            COALESCE(SUM(grand_total), 0)     AS revenue
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND custom_order_purpose = %(purpose)s
          AND posting_date BETWEEN %(fd)s AND %(td)s
        GROUP BY policy
        ORDER BY revenue DESC
        """,
        {**params, "purpose": _B2B_PURPOSE},
        as_dict=True,
    )
    return [
        {
            "policy": r["policy"],
            "order_count": int(r["order_count"] or 0),
            "revenue": round(float(r["revenue"] or 0), 2),
        }
        for r in rows
    ]


def _revenue_by_territory(params: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = frappe.db.sql(
        """
        SELECT
            COALESCE(NULLIF(territory, ''), 'Unassigned') AS territory,
            COUNT(*)                          AS orders,
            COALESCE(SUM(grand_total), 0)     AS revenue
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND custom_order_purpose = %(purpose)s
          AND posting_date BETWEEN %(fd)s AND %(td)s
        GROUP BY territory
        ORDER BY revenue DESC
        """,
        {**params, "purpose": _B2B_PURPOSE},
        as_dict=True,
    )
    return [
        {
            "territory": r["territory"],
            "revenue": round(float(r["revenue"] or 0), 2),
            "orders": int(r["orders"] or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
def _clients(params: Dict[str, str]) -> Dict[str, Any]:
    """Client counts + per-group revenue for B2B/Distributor customer groups."""
    total_b2b_clients = frappe.db.sql(
        """
        SELECT COUNT(*) AS n
        FROM `tabCustomer`
        WHERE disabled = 0
          AND customer_group IN %(groups)s
        """,
        {"groups": _B2B_GROUPS},
        as_dict=True,
    )[0]["n"]

    new_clients = frappe.db.sql(
        """
        SELECT COUNT(*) AS n
        FROM `tabCustomer`
        WHERE customer_group IN %(groups)s
          AND DATE(creation) BETWEEN %(fd)s AND %(td)s
        """,
        {"groups": _B2B_GROUPS, **params},
        as_dict=True,
    )[0]["n"]

    # Client count per group.
    count_rows = frappe.db.sql(
        """
        SELECT customer_group, COUNT(*) AS client_count
        FROM `tabCustomer`
        WHERE disabled = 0
          AND customer_group IN %(groups)s
        GROUP BY customer_group
        """,
        {"groups": _B2B_GROUPS},
        as_dict=True,
    )
    # Revenue per group in range (B2B Supply invoices).
    rev_rows = frappe.db.sql(
        """
        SELECT c.customer_group, COALESCE(SUM(si.grand_total), 0) AS revenue
        FROM `tabSales Invoice` si
        JOIN `tabCustomer` c ON c.name = si.customer
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.custom_order_purpose = %(purpose)s
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND c.customer_group IN %(groups)s
        GROUP BY c.customer_group
        """,
        {**params, "purpose": _B2B_PURPOSE, "groups": _B2B_GROUPS},
        as_dict=True,
    )
    rev_map = {r["customer_group"]: float(r["revenue"] or 0) for r in rev_rows}

    clients_by_group = [
        {
            "customer_group": r["customer_group"],
            "client_count": int(r["client_count"] or 0),
            "revenue": round(rev_map.get(r["customer_group"], 0.0), 2),
        }
        for r in sorted(count_rows, key=lambda x: -int(x["client_count"] or 0))
    ]

    return {
        "total_b2b_clients": int(total_b2b_clients or 0),
        "new_clients": int(new_clients or 0),
        "clients_by_group": clients_by_group,
    }


def _at_risk_clients(params: Dict[str, str]) -> Dict[str, Any]:
    """B2B/Distributor customers currently flagged At Risk / Can't Lose Them,
    with their in-range revenue."""
    rows = frappe.db.sql(
        """
        SELECT
            c.name                              AS customer,
            c.customer_name,
            c.customer_segment                  AS segment,
            c.rfm_recency_days                  AS recency_days,
            COALESCE(rev.revenue, 0)            AS revenue
        FROM `tabCustomer` c
        LEFT JOIN (
            SELECT customer, COALESCE(SUM(grand_total), 0) AS revenue
            FROM `tabSales Invoice`
            WHERE docstatus = 1
              AND is_return = 0
              AND custom_order_purpose = %(purpose)s
              AND posting_date BETWEEN %(fd)s AND %(td)s
            GROUP BY customer
        ) rev ON rev.customer = c.name
        WHERE c.disabled = 0
          AND c.customer_group IN %(groups)s
          AND c.customer_segment IN %(segs)s
        ORDER BY c.rfm_avg_order_value DESC
        LIMIT 50
        """,
        {**params, "purpose": _B2B_PURPOSE, "groups": _B2B_GROUPS, "segs": _AT_RISK_SEGMENTS},
        as_dict=True,
    )
    at_risk_clients = [
        {
            "customer": r["customer"],
            "customer_name": r["customer_name"] or r["customer"],
            "segment": r["segment"],
            "recency_days": int(r["recency_days"] or 0),
            "revenue": round(float(r["revenue"] or 0), 2),
        }
        for r in rows
    ]
    return {"at_risk_clients": at_risk_clients, "at_risk_count": len(at_risk_clients)}


# ---------------------------------------------------------------------------
# Pipeline (Lead + Opportunity)
# ---------------------------------------------------------------------------
def _pipeline() -> Dict[str, Any]:
    """Aggregate Lead + Opportunity records by custom_b2b_stage.

    Leads contribute count only (no monetary value); Opportunities contribute
    both count and summed ``opportunity_amount``. pipeline_open_value is the sum
    of opportunity value for stages that are neither Active nor Lost/On-hold.
    """
    agg: Dict[str, Dict[str, float]] = {
        s: {"count": 0, "value": 0.0} for s in _STAGE_ORDER
    }

    # Leads (count only).
    try:
        lead_rows = frappe.db.sql(
            """
            SELECT custom_b2b_stage AS stage, COUNT(*) AS cnt
            FROM `tabLead`
            WHERE custom_b2b_stage IS NOT NULL AND custom_b2b_stage != ''
            GROUP BY custom_b2b_stage
            """,
            as_dict=True,
        )
        for r in lead_rows:
            stage = r["stage"]
            agg.setdefault(stage, {"count": 0, "value": 0.0})
            agg[stage]["count"] += int(r["cnt"] or 0)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "b2b_analytics: pipeline leads failed")

    # Opportunities (count + value).
    pipeline_open_value = 0.0
    try:
        opp_rows = frappe.db.sql(
            """
            SELECT
                custom_b2b_stage AS stage,
                COUNT(*)         AS cnt,
                COALESCE(SUM(opportunity_amount), 0) AS value
            FROM `tabOpportunity`
            WHERE custom_b2b_stage IS NOT NULL AND custom_b2b_stage != ''
            GROUP BY custom_b2b_stage
            """,
            as_dict=True,
        )
        for r in opp_rows:
            stage = r["stage"]
            value = float(r["value"] or 0)
            agg.setdefault(stage, {"count": 0, "value": 0.0})
            agg[stage]["count"] += int(r["cnt"] or 0)
            agg[stage]["value"] += value
            if stage not in _CLOSED_STAGES:
                pipeline_open_value += value
    except Exception:
        frappe.log_error(frappe.get_traceback(), "b2b_analytics: pipeline opportunities failed")

    # Ordered output: canonical stages first, then any unexpected extras.
    ordered_stages = list(_STAGE_ORDER) + [s for s in agg if s not in _STAGE_ORDER]
    pipeline_by_stage = [
        {
            "stage": s,
            "count": int(agg[s]["count"]),
            "value": round(float(agg[s]["value"]), 2),
        }
        for s in ordered_stages
    ]

    return {
        "pipeline_by_stage": pipeline_by_stage,
        "pipeline_open_value": round(pipeline_open_value, 2),
    }


# ---------------------------------------------------------------------------
# Reorder-due (mirrors crm.get_reorder_due, remapped to contract keys)
# ---------------------------------------------------------------------------
def _reorder_due() -> Dict[str, Any]:
    """Customers whose predicted next order date is due (<= today)."""
    from frappe.utils import today as _today, getdate, date_diff

    if not frappe.get_meta("Customer").get_field("custom_predicted_next_order"):
        return {"reorder_due": [], "reorder_due_count": 0}

    today = _today()
    fields = ["name", "customer_name", "custom_predicted_next_order"]
    if frappe.get_meta("Customer").get_field("custom_last_order_date"):
        fields.append("custom_last_order_date")

    filters = {"custom_predicted_next_order": ["<=", today], "disabled": 0}
    if frappe.get_meta("Customer").get_field("customer_type"):
        filters["customer_type"] = "Company"

    rows = frappe.get_all(
        "Customer",
        filters=filters,
        fields=fields,
        order_by="custom_predicted_next_order asc",
        limit_page_length=0,
    )

    out = []
    for r in rows:
        last_order = r.get("custom_last_order_date")
        expected = r.get("custom_predicted_next_order")
        days_since = None
        if last_order:
            try:
                days_since = int(date_diff(today, getdate(last_order)))
            except Exception:
                days_since = None
        out.append(
            {
                "customer": r.get("name"),
                "customer_name": r.get("customer_name") or r.get("name"),
                "last_order_date": str(last_order) if last_order else None,
                "days_since": days_since,
                "expected_reorder_date": str(expected) if expected else None,
            }
        )

    return {"reorder_due": out, "reorder_due_count": len(out)}


# ---------------------------------------------------------------------------
# Conversion (opportunities -> customers)
# ---------------------------------------------------------------------------
def _conversion(params: Dict[str, str]) -> Dict[str, Any]:
    """Opportunities created in range vs. those converted to a Customer.

    ``won`` counts range opportunities that a Customer points at via
    ``custom_source_opportunity``.
    """
    opportunities = frappe.db.sql(
        """
        SELECT COUNT(*) AS n
        FROM `tabOpportunity`
        WHERE custom_b2b_stage IS NOT NULL AND custom_b2b_stage != ''
          AND DATE(creation) BETWEEN %(fd)s AND %(td)s
        """,
        params,
        as_dict=True,
    )[0]["n"]

    won = frappe.db.sql(
        """
        SELECT COUNT(*) AS n
        FROM `tabOpportunity` o
        WHERE o.custom_b2b_stage IS NOT NULL AND o.custom_b2b_stage != ''
          AND DATE(o.creation) BETWEEN %(fd)s AND %(td)s
          AND EXISTS (
              SELECT 1 FROM `tabCustomer` c
              WHERE c.custom_source_opportunity = o.name
          )
        """,
        params,
        as_dict=True,
    )[0]["n"]

    opportunities = int(opportunities or 0)
    won = int(won or 0)
    conversion_rate = round(won / opportunities * 100, 1) if opportunities else 0.0

    return {"opportunities": opportunities, "won": won, "conversion_rate": conversion_rate}


# ---------------------------------------------------------------------------
# Alerts feed
# ---------------------------------------------------------------------------
def _build_alerts(
    summary: Dict[str, Any],
    pipeline: Dict[str, Any],
    conversion: Dict[str, Any],
) -> List[Dict[str, str]]:
    alerts: List[Dict[str, str]] = []

    reorder_due = int(summary.get("reorder_due_count") or 0)
    if reorder_due:
        alerts.append({
            "type": "danger" if reorder_due >= 5 else "warning",
            "message": (
                f"<b>{reorder_due}</b> B2B client(s) are overdue for a reorder — "
                f"follow up before they churn"
            ),
        })

    at_risk = int(summary.get("at_risk_count") or 0)
    if at_risk:
        alerts.append({
            "type": "warning",
            "message": (
                f"<b>{at_risk}</b> B2B client(s) are flagged At Risk / Can't Lose Them"
            ),
        })

    conv_rate = float(conversion.get("conversion_rate") or 0)
    opps = int(conversion.get("opportunities") or 0)
    if opps >= 5 and conv_rate < 25:
        alerts.append({
            "type": "warning",
            "message": (
                f"Opportunity conversion is low at <b>{conv_rate:.0f}%</b> "
                f"({conversion.get('won', 0)}/{opps}) — review stalled deals"
            ),
        })

    open_value = float(summary.get("pipeline_open_value") or 0)
    if open_value > 0:
        alerts.append({
            "type": "info",
            "message": (
                f"Open pipeline value stands at <b>EGP {open_value:,.0f}</b>"
            ),
        })

    # Highlight the largest stalled stage (excluding Active / Lost).
    try:
        stalled = [
            s for s in pipeline.get("pipeline_by_stage", [])
            if s.get("stage") not in _CLOSED_STAGES and int(s.get("count") or 0) > 0
        ]
        if stalled:
            biggest = max(stalled, key=lambda x: int(x.get("count") or 0))
            if int(biggest.get("count") or 0) >= 5:
                alerts.append({
                    "type": "info",
                    "message": (
                        f"<b>{biggest['count']}</b> account(s) sitting in the "
                        f"<b>{biggest['stage']}</b> stage — keep the pipeline moving"
                    ),
                })
    except Exception:
        pass

    return alerts

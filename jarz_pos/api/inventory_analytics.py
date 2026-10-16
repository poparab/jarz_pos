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

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from jarz_pos.constants import ROLES

# Canonical display order for velocity trend buckets.
TREND_ORDER = ["Accelerating", "Stable", "Declining", "New Item", "No Sales", "Unrated"]

# Restock list ordering: the most urgent first.
_STATUS_ORDER = {"critical": 0, "watch": 1, "overstock": 2, "slow": 3}
# A suggested reorder brings cover up to this many times the watch threshold
# (14 days x 2 = 28 days with the default settings).
_TARGET_COVER_FACTOR = 2
_DEFAULT_THRESHOLDS = {
	"critical": 5,
	"watch": 14,
	"overstock": 90,
	"slow": 30,
	"multiplier": 1.0,
}


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


def _thresholds() -> Dict[str, Any]:
	"""Alert thresholds from Jarz Forecast Settings, plus the active seasonal multiplier."""
	try:
		from jarz_pos.services.demand_forecasting import (
			get_active_seasonal_multiplier,
			get_settings,
		)

		settings = get_settings()
		multiplier, _season = get_active_seasonal_multiplier(settings)
		return {
			"critical": int(settings.critical_days_threshold or 5),
			"watch": int(settings.watch_days_threshold or 14),
			"overstock": int(settings.overstock_days_threshold or 90),
			"slow": int(settings.slow_mover_days or 30),
			"multiplier": float(multiplier or 1.0),
		}
	except Exception:
		frappe.log_error(frappe.get_traceback(), "inventory_analytics: forecast settings unreadable")
		return dict(_DEFAULT_THRESHOLDS)


def _velocity_rows(warehouse: Optional[str], branch: Optional[str], window_days: int) -> List[Dict[str, Any]]:
	"""Per-item stock (Bin, optionally one warehouse) and live sales velocity.

	One set-based query. Sales are counted in stock units (``stock_qty``) from
	submitted, non-return invoices, optionally only the branch's
	(``si.pos_profile``). An item is a candidate when it sold within the window,
	or when it is a finished-product size (Small / Medium / Large groups) with
	stock on hand; raw materials that never sell over the counter belong to the
	materials report, not the restock list.
	"""
	from jarz_pos.api.product_analytics import _ALL_SIZE_GROUPS

	now = getdate(today())
	params = {
		"d30": add_days(now, -30),
		"d60": add_days(now, -60),
		"dwin": add_days(now, -max(60, int(window_days or 0))),
		"warehouse": warehouse,
		"branch": branch,
		"groups": _ALL_SIZE_GROUPS,
	}
	wh_cond = "WHERE warehouse = %(warehouse)s" if warehouse else ""
	br_cond = "AND si.pos_profile = %(branch)s" if branch else ""
	return frappe.db.sql(
		f"""
        SELECT
            i.name                          AS item_code,
            i.item_name,
            i.item_group,
            i.stock_uom                     AS uom,
            i.default_material_request_type AS replenishment_type,
            COALESCE(b.stock, 0)            AS stock,
            COALESCE(b.stock_value, 0)      AS stock_value,
            COALESCE(s.qty_30d, 0)          AS qty_30d,
            COALESCE(s.qty_60d, 0)          AS qty_60d,
            s.last_sale
        FROM `tabItem` i
        LEFT JOIN (
            SELECT item_code, SUM(actual_qty) AS stock, SUM(stock_value) AS stock_value
            FROM `tabBin` {wh_cond}
            GROUP BY item_code
        ) b ON b.item_code = i.name
        LEFT JOIN (
            SELECT
                sii.item_code,
                SUM(CASE WHEN si.posting_date >= %(d30)s THEN sii.stock_qty ELSE 0 END) AS qty_30d,
                SUM(CASE WHEN si.posting_date >= %(d60)s THEN sii.stock_qty ELSE 0 END) AS qty_60d,
                MAX(si.posting_date) AS last_sale
            FROM `tabSales Invoice Item` sii
            JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE si.docstatus = 1
              AND si.is_return = 0
              AND si.posting_date >= %(dwin)s
              {br_cond}
            GROUP BY sii.item_code
        ) s ON s.item_code = i.name
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
          AND (
              s.item_code IS NOT NULL
              OR (COALESCE(b.stock, 0) > 0 AND i.item_group IN %(groups)s)
          )
        """,
		params,
		as_dict=True,
	)


def _live_trend(v30: float, v60: float) -> str:
	"""Same rule as ``demand_forecasting.calculate_velocity``."""
	if v60 == 0 and v30 == 0:
		return "No Sales"
	if v60 == 0:
		return "New Item"
	if v30 > v60 * 1.15:
		return "Accelerating"
	if v30 < v60 * 0.85:
		return "Declining"
	return "Stable"


def build_restock(rows: List[Dict[str, Any]], th: Dict[str, Any]) -> List[Dict[str, Any]]:
	"""Classify velocity rows into the actionable restock list.

	- ``critical`` / ``watch``: selling, and cover (stock / (velocity_30d x
	  seasonal multiplier)) is at or under the critical / watch threshold. No
	  stock at all while selling is critical with ``days_cover`` 0.
	- ``overstock``: selling, cover above the overstock threshold.
	- ``slow``: stock on hand, nothing sold in 30 days and no sale within the
	  slow-mover window. ``days_cover`` is None (no velocity to divide by).
	Rows with no status (healthy cover, or no stock and no sales) are left out.
	``suggested_qty`` (critical / watch only) brings cover up to
	``watch_days x _TARGET_COVER_FACTOR``; negative stock counts as zero.
	"""
	multiplier = float(th.get("multiplier") or 1.0)
	slow_cutoff = getdate(add_days(getdate(today()), -int(th["slow"])))
	target_days = int(th["watch"]) * _TARGET_COVER_FACTOR

	out: List[Dict[str, Any]] = []
	for r in rows:
		stock = float(r.get("stock") or 0)
		stock_value = float(r.get("stock_value") or 0)
		v30 = round(float(r.get("qty_30d") or 0) / 30.0, 3)
		v60 = round(float(r.get("qty_60d") or 0) / 60.0, 3)
		effective = v30 * multiplier
		last_sale = r.get("last_sale")

		status = None
		days_cover = None
		if effective > 0:
			days_cover = 0.0 if stock <= 0 else round(stock / effective, 1)
			if days_cover <= th["critical"]:
				status = "critical"
			elif days_cover <= th["watch"]:
				status = "watch"
			elif days_cover > th["overstock"]:
				status = "overstock"
		elif stock > 0 and (not last_sale or getdate(last_sale) < slow_cutoff):
			status = "slow"

		if not status:
			continue

		suggested = 0
		if status in ("critical", "watch"):
			suggested = max(0, int(math.ceil(effective * target_days - max(stock, 0.0))))

		out.append(
			{
				"item_code": r.get("item_code"),
				"item_name": r.get("item_name"),
				"item_group": r.get("item_group"),
				"stock": round(stock, 3),
				"uom": r.get("uom"),
				"velocity_30d": v30,
				"days_cover": days_cover,
				"status": status,
				"suggested_qty": suggested,
				# Extra context (additive): ledger value, live trend, last sale.
				"stock_value": round(stock_value, 2),
				"valuation_rate": round(stock_value / stock, 4) if stock > 0 else 0.0,
				"velocity_60d": v60,
				"trend": _live_trend(v30, v60),
				"replenishment_type": r.get("replenishment_type"),
				"last_sale": str(last_sale) if last_sale else None,
			}
		)

	out.sort(
		key=lambda x: (
			_STATUS_ORDER.get(x["status"], 9),
			x["days_cover"] if x["days_cover"] is not None else float("inf"),
			-x["stock_value"],
		)
	)
	return out


def get_restock(branch: Optional[str] = None) -> Dict[str, Any]:
	"""Restock list for one branch (its POS Profile warehouse + its own sales) or all.

	Returns ``{"restock": [...], "rows": [...], "thresholds": {...}, "warehouse": str|None}``.
	``rows`` are the raw velocity rows (every candidate, not only actionable
	ones). Reused by the executive overview for branch-scoped alert counts.
	"""
	from jarz_pos.api.reports import branch_warehouse

	branch = (branch or "").strip() or None
	warehouse = branch_warehouse(branch)
	th = _thresholds()
	rows = _velocity_rows(warehouse, branch, th["slow"])
	return {
		"restock": build_restock(rows, th),
		"rows": rows,
		"thresholds": th,
		"warehouse": warehouse,
	}


def _alerts_from_restock(restock: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
	"""The four legacy alert lists, rebuilt from a branch's restock rows.

	Same row keys as ``demand_forecasting.build_alert_data`` so existing
	clients keep rendering them; velocity and cover are the branch's own.
	"""
	critical: List[Dict[str, Any]] = []
	watch: List[Dict[str, Any]] = []
	slow: List[Dict[str, Any]] = []
	over: List[Dict[str, Any]] = []
	for r in restock:
		base = {
			"item_code": r["item_code"],
			"item_name": r["item_name"],
			"item_group": r["item_group"],
			"replenishment_type": r.get("replenishment_type"),
		}
		if r["status"] in ("critical", "watch", "overstock"):
			row = dict(
				base,
				daily_velocity=r["velocity_30d"],
				stock_on_hand=r["stock"],
				days_remaining=r["days_cover"],
			)
			if r["status"] == "critical":
				critical.append(row)
			elif r["status"] == "watch":
				watch.append(row)
			else:
				row["stock_value"] = round(r["stock_value"], 0)
				over.append(row)
		elif r["status"] == "slow":
			slow.append(dict(base, stock_on_hand=r["stock"], trend=r.get("trend")))
	slow.sort(key=lambda x: -float(x.get("stock_on_hand") or 0))
	over.sort(key=lambda x: -float(x.get("stock_value") or 0))
	return {"critical": critical, "watch_list": watch, "slow_movers": slow, "overstocked": over[:20]}


def _branch_top_movers(rows: List[Dict[str, Any]], multiplier: float) -> List[Dict[str, Any]]:
	"""Top 15 by the branch's own 60-day velocity, in the ``top_movers`` shape."""
	movers = []
	for r in rows:
		v60 = round(float(r.get("qty_60d") or 0) / 60.0, 3)
		if v60 <= 0:
			continue
		v30 = round(float(r.get("qty_30d") or 0) / 30.0, 3)
		stock = float(r.get("stock") or 0)
		effective = v60 * (multiplier or 1.0)
		movers.append(
			{
				"item_code": r.get("item_code"),
				"item_name": r.get("item_name"),
				"item_group": r.get("item_group"),
				"velocity_30d": v30,
				"velocity_60d": v60,
				"trend": _live_trend(v30, v60),
				"days_of_stock": int(round(stock / effective)) if effective > 0 else 999,
				"stock_on_hand": round(stock, 2),
			}
		)
	movers.sort(key=lambda m: -m["velocity_60d"])
	return movers[:15]


@frappe.whitelist()
def get_inventory_analytics(
	date_from: Optional[str] = None,
	date_to: Optional[str] = None,
	branch: Optional[str] = None,
) -> Dict[str, Any]:
	"""Aggregated inventory / velocity analytics.

	``branch`` (optional POS Profile name): stock is that branch's warehouse
	(POS Profile ``warehouse``), velocity is that branch's own sales, and the
	alert lists, summary counts, top movers and top sellers follow it. The
	velocity-trend distribution stays company-wide (it is the stored Item
	master trend); ``scope_notes`` says so. Without a branch every existing
	figure is computed exactly as before.

	Additive keys: ``restock`` (see ``build_restock``), ``restock_thresholds``,
	``filters`` and ``scope_notes``.

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

	from jarz_pos.api.reports import branch_warehouse

	branch = (branch or "").strip() or None
	# Throws a clear ValidationError for a branch with no warehouse.
	warehouse = branch_warehouse(branch)

	# ── Restock list (live velocity x stock, per branch when given) ──────
	th = _thresholds()
	try:
		velocity_rows = _velocity_rows(warehouse, branch, th["slow"])
		restock = build_restock(velocity_rows, th)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "inventory_analytics: restock failed")
		velocity_rows, restock = [], []

	# ── Alert lists (reuse the forecasting engine) ───────────────────────
	if branch:
		# The engine's stored velocity and stock are company-wide; a branch
		# gets the same four lists rebuilt from its own restock rows.
		alerts = _alerts_from_restock(restock)
	else:
		try:
			from jarz_pos.services.demand_forecasting import build_alert_data, get_settings

			alerts = build_alert_data(get_settings())
		except Exception:
			frappe.log_error(frappe.get_traceback(), "inventory_analytics: build_alert_data failed")
			alerts = {"critical": [], "watch_list": [], "slow_movers": [], "overstocked": []}

	# ── Headline counts + total stock value ──────────────────────────────
	wh_cond = "AND b.warehouse = %(warehouse)s" if warehouse else ""
	stock_value_row = frappe.db.sql(
		f"""
        -- The ledger's own value per bin, not qty x Item.valuation_rate: the
        -- item-master rate is a stale default and priced production stock at
        -- 62,696 against a ledger value of 164,813 (2026-09-27).
        SELECT
            COALESCE(SUM(b.stock_value), 0) AS val,
            COUNT(DISTINCT CASE WHEN b.actual_qty <> 0 THEN b.item_code END) AS items
        FROM `tabBin` b
        JOIN `tabItem` i ON i.name = b.item_code
        WHERE i.is_stock_item = 1
          AND i.disabled = 0
          {wh_cond}
        """,
		{"warehouse": warehouse},
		as_dict=True,
	)
	total_stock_value = round(float(stock_value_row[0]["val"] or 0), 2) if stock_value_row else 0.0

	if warehouse:
		# A branch counts the items it actually holds, not the whole item master.
		total_stock_items = int(stock_value_row[0].get("items") or 0) if stock_value_row else 0
	else:
		total_stock_items = frappe.db.count("Item", {"is_stock_item": 1, "disabled": 0})

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
	if branch:
		top_movers = _branch_top_movers(velocity_rows, th["multiplier"])
	else:
		top_movers = _company_top_movers()

	# ── Top sellers in the selected range (date-scoped) ──────────────────
	br_cond = "AND si.pos_profile = %(branch)s" if branch else ""
	top_sold_in_range = frappe.db.sql(
		f"""
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
          {br_cond}
        GROUP BY sii.item_code, sii.item_name, sii.item_group
        ORDER BY qty DESC
        LIMIT 15
        """,
		{"fd": date_from, "td": date_to, "branch": branch},
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
		"restock": restock,
		"restock_thresholds": {
			"critical_days": th["critical"],
			"watch_days": th["watch"],
			"overstock_days": th["overstock"],
			"slow_mover_days": th["slow"],
			"seasonal_multiplier": th["multiplier"],
			"target_cover_days": th["watch"] * _TARGET_COVER_FACTOR,
		},
		"filters": {
			"date_from": date_from,
			"date_to": date_to,
			"branch": branch,
			"warehouse": warehouse,
		},
		"scope_notes": ["velocity_distribution_company_wide"] if branch else [],
	}


def _company_top_movers() -> List[Dict[str, Any]]:
	"""Top 15 by the stored company-wide 60-day velocity (unchanged query)."""
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
	return top_movers

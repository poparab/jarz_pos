"""
Shipping Analytics API for the Jarz POS Shipping Dashboard.

Provides aggregated shipping cost, revenue, courier, and territory data
for use by the shipping-analytics Frappe page.

All endpoints require the JARZ Manager role.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import date_diff, get_first_day, get_last_day, getdate, nowdate

from jarz_pos.constants import ROLES
from jarz_pos.utils.invoice_utils import get_area_label, get_woo_order_ids

# What the customer was actually billed for delivery on invoice ``si``: its
# ``Shipping Income (...)`` tax row. No row means the order billed nothing.
# Never ``custom_delivery_income`` (NOT NULL DEFAULT 0, so "never overridden"
# and "free" look alike) and never the Territory rate: falling back to the
# territory invented income for orders that billed zero, overstating
# September 2026 on production by 10,555 (23,310 shown vs 12,755 billed).
# Returns are excluded throughout, so every figure here is per order placed.
_BILLED_SHIPPING_INCOME = """(
    SELECT COALESCE(SUM(stc.base_tax_amount), 0)
    FROM `tabSales Taxes and Charges` stc
    WHERE stc.parent = si.name
      AND stc.parenttype = 'Sales Invoice'
      AND (stc.description LIKE 'Shipping Income%%' OR stc.account_head LIKE 'Shipping Income%%')
)"""


# Optional branch (POS Profile) scoping. Each is spliced in only when a branch
# is given, so without one every query is exactly what it was.
# Invoices: the invoice's own POS Profile.
_SI_BRANCH = "AND si.pos_profile = %(branch)s"
_PLAIN_BRANCH = "AND pos_profile = %(branch)s"
# Courier Transactions and Custom Shipping Requests carry no branch of their
# own; they belong to the branch of the invoice they reference.
_CT_BRANCH = """AND EXISTS (
    SELECT 1 FROM `tabSales Invoice` bsi
    WHERE bsi.name = ct.reference_invoice AND bsi.pos_profile = %(branch)s
)"""
_CSR_BRANCH = """AND EXISTS (
    SELECT 1 FROM `tabSales Invoice` bsi
    WHERE bsi.name = csr.invoice AND bsi.pos_profile = %(branch)s
)"""


def _norm_branch(branch):
	return (branch or "").strip() or None


def _unsettled_scope(as_of, params):
	"""``(WHERE clause, reference date)`` for courier balances still owed.

	``as_of`` falsy: rows unsettled right now. Otherwise the balance at the end
	of that day: rows dated on or before it that were unsettled then. A Settled
	row with no ``settled_at`` (legacy) is taken as settled on its own ``date``,
	so it can never surface as owed. ``modified`` is NOT a settlement time: any
	later save (a note, a carry-over) moved it, which turned settled rows into
	phantom balances. The settling Journal Entry is not used either:
	``Courier Transaction.journal_entry`` does not reliably name the settlement.
	Sets ``params["as_of_end"]`` when ``as_of`` is given.
	"""
	today = getdate(nowdate())
	if not as_of:
		return "ct.status = 'Unsettled'", today
	as_of_date = getdate(as_of)
	params["as_of_end"] = f"{as_of_date} 23:59:59"
	scope = """ct.date <= %(as_of_end)s
          AND (
              ct.status = 'Unsettled'
              OR (ct.status = 'Settled' AND COALESCE(ct.settled_at, ct.date) > %(as_of_end)s)
          )"""
	return scope, min(as_of_date, today)


def _when(branch, condition):
	return condition if branch else ""


def _party_label(party_type, party):
	"""A courier's name for display. Couriers are Employees (or Suppliers), and
	the raw link value is an id: the dashboard showed "HR-EMP-000002" beside a
	colleague shown by name."""
	if not party:
		return party
	field = {"Employee": "employee_name", "Supplier": "supplier_name"}.get(party_type or "")
	if not field:
		return party
	try:
		return frappe.db.get_value(party_type, party, field) or party
	except Exception:
		return party


def _ensure_jarz_manager():
	roles = set(frappe.get_roles(frappe.session.user))
	if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
		frappe.throw(_("Only JARZ Manager can access shipping analytics"), frappe.PermissionError)


def _parse_dates(from_date, to_date):
	today = getdate(nowdate())
	fd = getdate(from_date) if from_date else get_first_day(today)
	td = getdate(to_date) if to_date else get_last_day(today)
	return str(fd), str(td)


def _safe(label, fn, default):
	"""Run a sub-section loader, log + fall back on failure so one broken
	section never 500s the whole composed response."""
	try:
		return fn()
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"shipping_analytics: {label} failed")
		return default


@frappe.whitelist()
def get_shipping_analytics(from_date=None, to_date=None, branch=None):
	"""Composed shipping dashboard payload.

	Assembles the twelve existing shipping helpers into the single dict the
	mobile Shipping Analytics screen consumes. Each sub-call is wrapped
	defensively: a failure returns that key's empty default instead of failing
	the entire response.

	``branch`` (optional POS Profile name) scopes every invoice-based section to
	that branch's invoices, and courier transactions / override requests to the
	branch of the invoice they reference. ``double_shipping_impact`` reads
	Delivery Trips, which carry no branch, and stays company-wide.

	``unsettled_courier_balances`` is the balance owed **as of ``to_date``**
	(end of day): transactions dated on or before it that were not yet settled
	then. ``unsettled_as_of`` (new key) names that date.
	``summary_kpis.unsettled_courier_total`` is computed on the same as-of date
	(and branch), so the KPI equals the table's total; the standalone
	``get_summary_kpis`` without ``as_of`` still returns today's balance.
	"""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	return {
		"summary_kpis": _safe(
			"summary_kpis",
			lambda: get_summary_kpis(from_date=fd, to_date=td, branch=branch, as_of=td),
			{},
		),
		"alerts": _safe("alerts", lambda: get_alerts_data(from_date=fd, to_date=td, branch=branch), []),
		"cost_by_territory": _safe(
			"cost_by_territory",
			lambda: get_cost_by_territory(from_date=fd, to_date=td, branch=branch),
			[],
		),
		"cost_by_sub_territory": _safe(
			"cost_by_sub_territory",
			lambda: get_cost_by_sub_territory(from_date=fd, to_date=td, branch=branch),
			[],
		),
		"cost_by_pos_profile": _safe(
			"cost_by_pos_profile",
			lambda: get_cost_by_pos_profile(from_date=fd, to_date=td, branch=branch),
			[],
		),
		"cost_by_courier": _safe(
			"cost_by_courier",
			lambda: get_cost_by_courier(from_date=fd, to_date=td, branch=branch),
			[],
		),
		"custom_shipping_breakdown": _safe(
			"custom_shipping_breakdown",
			lambda: get_custom_shipping_breakdown(from_date=fd, to_date=td, branch=branch),
			{
				"summary": {"total": 0, "approved": 0, "rejected": 0, "pending": 0, "approval_rate": 0},
				"rows": [],
			},
		),
		"double_shipping_impact": _safe(
			"double_shipping_impact",
			lambda: get_double_shipping_impact(from_date=fd, to_date=td),
			{"trips": [], "total_double_trips": 0, "total_extra_cost": 0.0},
		),
		"daily_trend": _safe(
			"daily_trend", lambda: get_daily_trend(from_date=fd, to_date=td, branch=branch), []
		),
		"pickup_vs_delivery_split": _safe(
			"pickup_vs_delivery_split",
			lambda: get_pickup_vs_delivery_split(from_date=fd, to_date=td, branch=branch),
			{"pickup": 0, "delivery": 0},
		),
		"unsettled_courier_balances": _safe(
			"unsettled_courier_balances",
			lambda: get_unsettled_courier_balances(as_of=td, branch=branch),
			[],
		),
		"unsettled_as_of": td,
		"pickup_delivery_trend": _safe(
			"pickup_delivery_trend",
			lambda: get_pickup_delivery_trend(from_date=fd, to_date=td, branch=branch),
			[],
		),
		"filters": {"from_date": fd, "to_date": td, "branch": branch},
	}


@frappe.whitelist()
def get_summary_kpis(from_date=None, to_date=None, branch=None, as_of=None):
	"""Eight headline KPIs covering the selected date range.

	``unsettled_courier_total`` is today's balance unless ``as_of`` (optional
	date) is given, in which case it is the balance owed at the end of that day,
	on exactly the rules of ``get_unsettled_courier_balances(as_of=...)``.
	``unsettled_as_of`` (additive) echoes the date used, or null for today.
	"""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)
	params = {"fd": fd, "td": td, "branch": branch}
	unsettled_scope, _reference = _unsettled_scope(as_of, params)

	row = frappe.db.sql(
		f"""
        SELECT
            COUNT(*)                                                                    AS total_orders,
            SUM(CASE WHEN si.custom_is_pickup = 0 THEN 1 ELSE 0 END)                   AS delivery_orders,
            SUM(CASE WHEN si.custom_is_pickup = 1 THEN 1 ELSE 0 END)                   AS pickup_orders,
            COALESCE(SUM(si.custom_shipping_expense), 0)                                AS total_expense,
            COALESCE(SUM({_BILLED_SHIPPING_INCOME}), 0)                                  AS total_income,
            COALESCE(AVG(CASE WHEN si.custom_is_pickup = 0
                               AND si.custom_shipping_expense > 0
                              THEN si.custom_shipping_expense END), 0)                  AS avg_cost_per_order
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {_when(branch, _SI_BRANCH)}
    """,
		params,
		as_dict=True,
	)[0]

	if branch:
		pending_csr = frappe.db.sql(
			f"""
            SELECT COUNT(*) AS n
            FROM `tabCustom Shipping Request` csr
            WHERE csr.status = 'Pending'
              {_CSR_BRANCH}
        """,
			params,
			as_dict=True,
		)[0]["n"]
	else:
		pending_csr = frappe.db.count("Custom Shipping Request", {"status": "Pending"})

	# Today's balance, or (with ``as_of``) the same scope the balances table uses.
	unsettled_row = frappe.db.sql(
		f"""
        SELECT COALESCE(SUM(ct.shipping_amount), 0) AS total
        FROM `tabCourier Transaction` ct
        WHERE {unsettled_scope}
          {_when(branch, _CT_BRANCH)}
    """,
		params,
		as_dict=True,
	)[0]

	return {
		"total_orders": int(row.total_orders or 0),
		"delivery_orders": int(row.delivery_orders or 0),
		"pickup_orders": int(row.pickup_orders or 0),
		"total_expense": float(row.total_expense or 0),
		"total_income": float(row.total_income or 0),
		"net_pl": float((row.total_income or 0) - (row.total_expense or 0)),
		"avg_cost_per_order": float(row.avg_cost_per_order or 0),
		"pending_csr_count": int(pending_csr or 0),
		"unsettled_courier_total": float(unsettled_row.total or 0),
		"unsettled_as_of": str(getdate(as_of)) if as_of else None,
	}


@frappe.whitelist()
def get_cost_by_territory(from_date=None, to_date=None, branch=None):
	"""Shipping expense, income, order count and average cost grouped by territory."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	rows = frappe.db.sql(
		f"""
        SELECT
            si.territory,
            COUNT(*)                                                                    AS order_count,
            COALESCE(SUM(si.custom_shipping_expense), 0)                                AS total_expense,
            COALESCE(SUM({_BILLED_SHIPPING_INCOME}), 0)                                  AS total_income,
            COALESCE(AVG(CASE WHEN si.custom_shipping_expense > 0
                              THEN si.custom_shipping_expense END), 0)                  AS avg_cost
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND si.territory IS NOT NULL
          AND si.territory != ''
          {_when(branch, _SI_BRANCH)}
        GROUP BY si.territory
        ORDER BY SUM(si.custom_shipping_expense) DESC
    """,
		{"fd": fd, "td": td, "branch": branch},
		as_dict=True,
	)

	for r in rows:
		r["total_expense"] = float(r["total_expense"] or 0)
		r["total_income"] = float(r["total_income"] or 0)
		r["avg_cost"] = float(r["avg_cost"] or 0)
		r["order_count"] = int(r["order_count"] or 0)
		r["net_pl"] = r["total_income"] - r["total_expense"]
		# Grouped on the raw territory, labelled with the Arabic name: the
		# record is *named* by its Woo area code, so the raw value would put
		# "EGNASRCITY" on the chart axis.
		r["territory"] = get_area_label(r["territory"])

	return rows


@frappe.whitelist()
def get_cost_by_sub_territory(from_date=None, to_date=None, branch=None):
	"""Shipping expense grouped by sub-territory (top 20 by cost)."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	rows = frappe.db.sql(
		f"""
        SELECT
            COALESCE(custom_sub_territory, '(None)')                                    AS sub_territory,
            COUNT(*)                                                                    AS order_count,
            COALESCE(SUM(custom_shipping_expense), 0)                                   AS total_expense,
            COALESCE(AVG(CASE WHEN custom_shipping_expense > 0
                              THEN custom_shipping_expense END), 0)                     AS avg_cost
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND posting_date BETWEEN %(fd)s AND %(td)s
          {_when(branch, _PLAIN_BRANCH)}
        GROUP BY custom_sub_territory
        ORDER BY total_expense DESC
        LIMIT 20
    """,
		{"fd": fd, "td": td, "branch": branch},
		as_dict=True,
	)

	for r in rows:
		r["total_expense"] = float(r["total_expense"] or 0)
		r["avg_cost"] = float(r["avg_cost"] or 0)
		r["order_count"] = int(r["order_count"] or 0)

	return rows


@frappe.whitelist()
def get_cost_by_pos_profile(from_date=None, to_date=None, branch=None):
	"""Shipping expense and income grouped by POS Profile (branch).

	With ``branch`` the list holds only that branch's row.
	"""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	rows = frappe.db.sql(
		f"""
        SELECT
            COALESCE(si.pos_profile, '(No Profile)')                                    AS branch,
            COUNT(*)                                                                    AS order_count,
            COALESCE(SUM(si.custom_shipping_expense), 0)                                AS total_expense,
            COALESCE(SUM({_BILLED_SHIPPING_INCOME}), 0)                                  AS total_income,
            COALESCE(AVG(CASE WHEN si.custom_shipping_expense > 0
                              THEN si.custom_shipping_expense END), 0)                  AS avg_cost
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {_when(branch, _SI_BRANCH)}
        GROUP BY si.pos_profile
        ORDER BY SUM(si.custom_shipping_expense) DESC
    """,
		{"fd": fd, "td": td, "branch": branch},
		as_dict=True,
	)

	for r in rows:
		r["total_expense"] = float(r["total_expense"] or 0)
		r["total_income"] = float(r["total_income"] or 0)
		r["avg_cost"] = float(r["avg_cost"] or 0)
		r["order_count"] = int(r["order_count"] or 0)

	return rows


@frappe.whitelist()
def get_cost_by_courier(from_date=None, to_date=None, branch=None):
	"""Shipping amounts per courier, split by settled vs unsettled."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	rows = frappe.db.sql(
		f"""
        SELECT
            ct.party_type,
            ct.party,
            ct.status,
            COUNT(*)                               AS order_count,
            COALESCE(SUM(ct.shipping_amount), 0)   AS total_amount
        FROM `tabCourier Transaction` ct
        WHERE DATE(ct.date) BETWEEN %(fd)s AND %(td)s
          {_when(branch, _CT_BRANCH)}
        GROUP BY ct.party_type, ct.party, ct.status
        ORDER BY total_amount DESC
    """,
		{"fd": fd, "td": td, "branch": branch},
		as_dict=True,
	)

	couriers = {}
	for r in rows:
		key = (r["party_type"], r["party"])
		if key not in couriers:
			couriers[key] = {
				"party_type": r["party_type"],
				"party": _party_label(r["party_type"], r["party"]),
				"settled": 0.0,
				"unsettled": 0.0,
				"order_count": 0,
			}
		amount = float(r["total_amount"] or 0)
		if r["status"] == "Settled":
			couriers[key]["settled"] += amount
		else:
			couriers[key]["unsettled"] += amount
		couriers[key]["order_count"] += int(r["order_count"] or 0)

	return sorted(couriers.values(), key=lambda x: x["settled"] + x["unsettled"], reverse=True)


@frappe.whitelist()
def get_custom_shipping_breakdown(from_date=None, to_date=None, branch=None):
	"""Custom Shipping Request list with delta analysis and approval rate."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)
	params = {"fd": fd, "td": td, "branch": branch}
	csr_branch = _when(branch, _CSR_BRANCH)

	rows = frappe.db.sql(
		f"""
        SELECT
            csr.name,
            csr.invoice,
            csr.territory,
            csr.original_amount,
            csr.requested_amount,
            (csr.requested_amount - csr.original_amount)   AS delta,
            csr.status,
            csr.reason,
            csr.requested_by,
            csr.creation
        FROM `tabCustom Shipping Request` csr
        WHERE DATE(csr.creation) BETWEEN %(fd)s AND %(td)s
          {csr_branch}
        ORDER BY csr.creation DESC
        LIMIT 200
    """,
		params,
		as_dict=True,
	)

	total = len(rows)
	approved = sum(1 for r in rows if r["status"] == "Approved")
	rejected = sum(1 for r in rows if r["status"] == "Rejected")
	pending = sum(1 for r in rows if r["status"] == "Pending")

	# Money view of the overrides. A request replaces the order's courier cost
	# (the area's standard rate, `original_amount`) with `requested_amount`;
	# only APPROVED ones changed what was paid. An increase is extra cost, a
	# decrease a saving. Computed over every request in range, not the 200
	# rows returned for listing.
	money = frappe.db.sql(
		f"""
        SELECT
            COUNT(*)                                                                   AS n,
            COALESCE(SUM(GREATEST(csr.requested_amount - csr.original_amount, 0)), 0)  AS extra,
            COALESCE(SUM(GREATEST(csr.original_amount - csr.requested_amount, 0)), 0)  AS saved,
            COALESCE(SUM(csr.original_amount), 0)                                      AS standard,
            SUM(csr.requested_amount > csr.original_amount)                            AS increases,
            SUM(csr.requested_amount < csr.original_amount)                            AS decreases,
            MAX(CASE WHEN csr.original_amount > 0
                     THEN (csr.requested_amount - csr.original_amount) / csr.original_amount END) AS max_pct
        FROM `tabCustom Shipping Request` csr
        WHERE csr.status = 'Approved'
          AND DATE(csr.creation) BETWEEN %(fd)s AND %(td)s
          {csr_branch}
    """,
		params,
		as_dict=True,
	)[0]
	pending_extra = frappe.db.sql(
		f"""
        SELECT COALESCE(SUM(csr.requested_amount - csr.original_amount), 0) AS x
        FROM `tabCustom Shipping Request` csr
        WHERE csr.status = 'Pending' AND DATE(csr.creation) BETWEEN %(fd)s AND %(td)s
          {csr_branch}
    """,
		params,
		as_dict=True,
	)[0]["x"]
	delivery_orders = frappe.db.sql(
		f"""
        SELECT COUNT(*) AS n FROM `tabSales Invoice`
        WHERE docstatus = 1 AND is_return = 0 AND IFNULL(custom_is_pickup, 0) = 0
          AND posting_date BETWEEN %(fd)s AND %(td)s
          {_when(branch, _PLAIN_BRANCH)}
    """,
		params,
		as_dict=True,
	)[0]["n"]
	area_rows = frappe.db.sql(
		f"""
        SELECT csr.territory,
               COUNT(*)                                                             AS count,
               COALESCE(SUM(csr.requested_amount - csr.original_amount), 0)         AS net,
               COALESCE(SUM(GREATEST(csr.requested_amount - csr.original_amount, 0)), 0) AS extra
        FROM `tabCustom Shipping Request` csr
        WHERE csr.status = 'Approved' AND DATE(csr.creation) BETWEEN %(fd)s AND %(td)s
          {csr_branch}
        GROUP BY csr.territory
        ORDER BY net DESC
        LIMIT 5
    """,
		params,
		as_dict=True,
	)
	extra = float(money.get("extra") or 0)
	saved = float(money.get("saved") or 0)
	standard = float(money.get("standard") or 0)
	increases = int(money.get("increases") or 0)
	money_summary = {
		"approved_extra": round(extra, 2),
		"approved_saved": round(saved, 2),
		"net_effect": round(extra - saved, 2),
		"net_effect_pct": round((extra - saved) / standard * 100, 1) if standard else 0.0,
		"increases": increases,
		"decreases": int(money.get("decreases") or 0),
		"avg_increase": round(extra / increases, 2) if increases else 0.0,
		"max_increase_pct": round(float(money.get("max_pct") or 0) * 100, 1),
		"pending_extra": round(float(pending_extra or 0), 2),
		"delivery_orders": int(delivery_orders or 0),
		"exception_rate_pct": round(int(money.get("n") or 0) / delivery_orders * 100, 1)
		if delivery_orders
		else 0.0,
		"large_overrides": sum(
			1
			for r in rows
			if r["status"] == "Approved"
			and r["original_amount"]
			and (float(r["requested_amount"] or 0) - float(r["original_amount"] or 0))
			> float(r["original_amount"]) * 0.5
		),
	}
	by_area = [
		{
			"territory": get_area_label(r["territory"]),
			"count": int(r["count"] or 0),
			"net": round(float(r["net"] or 0), 2),
			"extra": round(float(r["extra"] or 0), 2),
		}
		for r in area_rows
	]

	woo_ids = get_woo_order_ids([r.get("invoice") for r in rows])

	for r in rows:
		r["woo_order_id"] = woo_ids.get(r.get("invoice"))
		r["territory"] = get_area_label(r.get("territory"))
		r["original_amount"] = float(r["original_amount"] or 0)
		r["requested_amount"] = float(r["requested_amount"] or 0)
		r["delta"] = float(r["delta"] or 0)
		r["is_large_override"] = r["original_amount"] > 0 and r["delta"] > r["original_amount"] * 0.5
		r["creation"] = str(r["creation"])[:19] if r["creation"] else ""

	return {
		"rows": rows,
		"summary": {
			"total": total,
			"approved": approved,
			"rejected": rejected,
			"pending": pending,
			"approval_rate": round(approved / total * 100, 1) if total > 0 else 0,
			**money_summary,
		},
		"by_area": by_area,
	}


@frappe.whitelist()
def get_double_shipping_impact(from_date=None, to_date=None):
	"""Delivery trips where double shipping was applied, with extra cost breakdown."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)

	rows = frappe.db.sql(
		"""
        SELECT
            dt.name,
            dt.trip_date,
            dt.courier_party_type,
            dt.courier_party,
            dt.double_shipping_territory,
            dt.total_orders,
            dt.total_shipping_expense
        FROM `tabDelivery Trip` dt
        WHERE dt.is_double_shipping = 1
          AND dt.trip_date BETWEEN %(fd)s AND %(td)s
        ORDER BY dt.trip_date DESC
    """,
		{"fd": fd, "td": td},
		as_dict=True,
	)

	total_extra = 0.0
	for r in rows:
		r["total_shipping_expense"] = float(r["total_shipping_expense"] or 0)
		r["total_orders"] = int(r["total_orders"] or 0)
		r["double_shipping_territory"] = get_area_label(r.get("double_shipping_territory"))
		r["trip_date"] = str(r["trip_date"]) if r["trip_date"] else ""
		# Double shipping doubles the cost; extra = half the total paid
		r["extra_cost"] = round(r["total_shipping_expense"] / 2, 2)
		total_extra += r["extra_cost"]

	return {
		"trips": rows,
		"total_double_trips": len(rows),
		"total_extra_cost": round(total_extra, 2),
	}


@frappe.whitelist()
def get_daily_trend(from_date=None, to_date=None, branch=None):
	"""Daily order count, shipping expense, and delivery income for trend charts."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	rows = frappe.db.sql(
		f"""
        SELECT
            si.posting_date,
            COUNT(*)                                        AS order_count,
            COALESCE(SUM(si.custom_shipping_expense), 0)    AS total_expense,
            COALESCE(SUM({_BILLED_SHIPPING_INCOME}), 0)     AS total_income
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {_when(branch, _SI_BRANCH)}
        GROUP BY si.posting_date
        ORDER BY si.posting_date ASC
    """,
		{"fd": fd, "td": td, "branch": branch},
		as_dict=True,
	)

	for r in rows:
		r["posting_date"] = str(r["posting_date"])
		r["order_count"] = int(r["order_count"] or 0)
		r["total_expense"] = float(r["total_expense"] or 0)
		r["total_income"] = float(r["total_income"] or 0)

	return rows


@frappe.whitelist()
def get_pickup_vs_delivery_split(from_date=None, to_date=None, branch=None):
	"""Pickup vs delivery order counts for the donut chart."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	rows = frappe.db.sql(
		f"""
        SELECT
            custom_is_pickup,
            COUNT(*) AS order_count
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND posting_date BETWEEN %(fd)s AND %(td)s
          {_when(branch, _PLAIN_BRANCH)}
        GROUP BY custom_is_pickup
    """,
		{"fd": fd, "td": td, "branch": branch},
		as_dict=True,
	)

	result = {"pickup": 0, "delivery": 0}
	for r in rows:
		if r["custom_is_pickup"]:
			result["pickup"] = int(r["order_count"])
		else:
			result["delivery"] = int(r["order_count"])
	return result


@frappe.whitelist()
def get_unsettled_courier_balances(as_of=None, branch=None):
	"""Courier balances still owed, with age in days.

	Without ``as_of``: every transaction unsettled right now (unchanged).

	With ``as_of`` (a date): the balance as it stood at the end of that day —
	transactions dated on or before it that were either still unsettled, or
	settled only after it. When a settled row has no ``settled_at`` (legacy
	rows) its own ``date`` stands in, i.e. it is treated as settled when it was
	raised: such a row can be under-reported as owed, never invented. (Its
	``modified`` time used to stand in, which made any later save look like an
	unpaid balance until that save.) ``days_aged`` is measured to the earlier
	of ``as_of`` and today.

	Known limit (accepted): a settlement that was later **reversed** cannot be
	told apart from a row that was never settled. A Courier Transaction has only
	Unsettled/Settled and no history; ``courier_carry`` reverses by flipping
	``status`` back to Unsettled (keeping the old ``settled_at``) and a re-settle
	overwrites ``settled_at``, so nothing records when it was settled the first
	time or when that was undone. The as-of view reads each row by its current
	status and ``settled_at``: a row settled before ``as_of`` and reversed
	after it shows as owed at ``as_of``.

	``branch`` (optional POS Profile) keeps transactions whose reference
	invoice belongs to that branch.
	"""
	_ensure_jarz_manager()
	branch = _norm_branch(branch)
	params = {"branch": branch}
	scope, reference = _unsettled_scope(as_of, params)

	rows = frappe.db.sql(
		f"""
        SELECT
            ct.party_type,
            ct.party,
            COUNT(*)                               AS order_count,
            COALESCE(SUM(ct.shipping_amount), 0)   AS total_owed,
            MIN(ct.date)                           AS oldest_date
        FROM `tabCourier Transaction` ct
        WHERE {scope}
          {_when(branch, _CT_BRANCH)}
        GROUP BY ct.party_type, ct.party
        ORDER BY total_owed DESC
    """,
		params,
		as_dict=True,
	)

	for r in rows:
		r["party"] = _party_label(r["party_type"], r["party"])
		r["total_owed"] = float(r["total_owed"] or 0)
		r["order_count"] = int(r["order_count"] or 0)
		r["oldest_date"] = str(r["oldest_date"]) if r["oldest_date"] else None
		r["days_aged"] = max(0, int(date_diff(reference, r["oldest_date"]))) if r["oldest_date"] else 0

	return rows


@frappe.whitelist()
def get_alerts_data(from_date=None, to_date=None, branch=None):
	"""
	Return structured alert conditions for the Discrepancies panel.
	Each item: {type: 'danger'|'warning'|'info', message: str}

	``branch`` (optional POS Profile) scopes every alert except the
	double-shipping count (Delivery Trips carry no branch).
	"""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)
	params = {"fd": fd, "td": td, "branch": branch}

	alerts = []

	# 1. Territories where delivery expense exceeds income (losing money)
	# Use subquery so the outer WHERE can reference aliases without the
	# MySQL 1247 "reference to group function" error.
	losing = frappe.db.sql(
		f"""
        SELECT territory, expense, income
        FROM (
            SELECT si.territory,
                   COALESCE(SUM(si.custom_shipping_expense), 0) AS expense,
                   COALESCE(SUM({_BILLED_SHIPPING_INCOME}), 0) AS income
            FROM `tabSales Invoice` si
            WHERE si.docstatus = 1
              AND si.is_return = 0
              AND si.posting_date BETWEEN %(fd)s AND %(td)s
              AND si.territory IS NOT NULL AND si.territory != ''
              {_when(branch, _SI_BRANCH)}
            GROUP BY si.territory
        ) sub
        WHERE expense > 0 AND income < expense
        ORDER BY (expense - income) DESC
    """,
		params,
		as_dict=True,
	)

	# One alert for all of them: a line per area buried the page (13 alerts
	# for September 2026) and repeated the territory chart right below it.
	if losing:
		losses = [
			(get_area_label(t["territory"]), float(t["expense"] or 0) - float(t["income"] or 0))
			for t in losing
		]
		worst = ", ".join(f"{name} ({loss:,.0f})" for name, loss in losses[:3])
		alerts.append(
			{
				"type": "danger",
				"message": (
					f"<b>{len(losses)}</b> areas cost more to deliver than customers paid: "
					f"<b>EGP {sum(l for _, l in losses):,.0f}</b> lost. Worst: {worst}"
				),
			}
		)

	# 2. Couriers with unsettled balance older than 7 days
	# Subquery avoids MySQL 1247 error when referencing the MIN() alias in HAVING.
	today = getdate(nowdate())
	aged = frappe.db.sql(
		f"""
        SELECT party_type, party, total, oldest_date
        FROM (
            SELECT ct.party_type, ct.party,
                   COALESCE(SUM(ct.shipping_amount), 0) AS total,
                   MIN(ct.date)                          AS oldest_date
            FROM `tabCourier Transaction` ct
            WHERE ct.status = 'Unsettled'
              {_when(branch, _CT_BRANCH)}
            GROUP BY ct.party_type, ct.party
        ) sub
        WHERE DATEDIFF(CURDATE(), oldest_date) > 7
        ORDER BY DATEDIFF(CURDATE(), oldest_date) DESC
    """,
		params,
		as_dict=True,
	)

	for c in aged:
		days = int(date_diff(today, c["oldest_date"])) if c["oldest_date"] else 0
		alerts.append(
			{
				"type": "warning",
				"message": (
					f"Courier <b>{_party_label(c['party_type'], c['party'])}</b> has "
					f"<b>EGP {float(c['total'] or 0):,.0f}</b> unsettled for <b>{days} days</b>"
				),
			}
		)

	# 3. Pending Custom Shipping Requests older than 24 hours
	old_csrs = frappe.db.sql(
		f"""
        SELECT csr.name, csr.invoice, csr.territory,
               TIMESTAMPDIFF(HOUR, csr.creation, NOW()) AS hours_pending
        FROM `tabCustom Shipping Request` csr
        WHERE csr.status = 'Pending'
          AND csr.creation < DATE_SUB(NOW(), INTERVAL 24 HOUR)
          {_when(branch, _CSR_BRANCH)}
        ORDER BY csr.creation ASC
    """,
		params,
		as_dict=True,
	)

	for csr in old_csrs:
		hours = int(csr["hours_pending"] or 0)
		alerts.append(
			{
				"type": "warning",
				"message": (
					f"Override request <b>{csr['name']}</b> on invoice <b>{csr['invoice']}</b>"
					f" ({get_area_label(csr['territory'])}) has been pending for <b>{hours}h</b> — needs manager decision"
				),
			}
		)

	# 4. Large approved overrides in range (delta > 50% of original), as ONE
	# alert: a line per invoice listed ten near-identical rows (Sep 2026: 63
	# such overrides). The overrides card below breaks them down by area.
	large = frappe.db.sql(
		f"""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(csr.requested_amount - csr.original_amount), 0) AS extra,
               MAX((csr.requested_amount - csr.original_amount) / csr.original_amount) AS max_pct
        FROM `tabCustom Shipping Request` csr
        WHERE csr.status = 'Approved'
          AND DATE(csr.creation) BETWEEN %(fd)s AND %(td)s
          AND csr.original_amount > 0
          AND (csr.requested_amount - csr.original_amount) > csr.original_amount * 0.5
          {_when(branch, _CSR_BRANCH)}
    """,
		params,
		as_dict=True,
	)[0]

	if int(large.get("n") or 0):
		alerts.append(
			{
				"type": "info",
				"message": (
					f"<b>{int(large['n'])}</b> approved overrides raised the courier cost more than 50% "
					f"above the area rate: <b>EGP {float(large['extra'] or 0):,.0f}</b> extra "
					f"(largest +{float(large['max_pct'] or 0) * 100:,.0f}%)"
				),
			}
		)

	# 5. Double shipping trip count in range
	double_count = frappe.db.count(
		"Delivery Trip",
		{
			"is_double_shipping": 1,
			"trip_date": ["between", [fd, td]],
		},
	)
	if double_count:
		alerts.append(
			{
				"type": "info",
				"message": (
					f"<b>{double_count}</b> trip(s) used double-shipping in this period. "
					f"Consider mixing territories across trips to avoid the 2× multiplier."
				),
			}
		)

	return alerts


@frappe.whitelist()
def get_pickup_delivery_trend(from_date=None, to_date=None, branch=None):
	"""Daily delivery vs pickup order counts for a stacked trend chart."""
	_ensure_jarz_manager()
	fd, td = _parse_dates(from_date, to_date)
	branch = _norm_branch(branch)

	rows = frappe.db.sql(
		f"""
        SELECT
            posting_date,
            SUM(CASE WHEN custom_is_pickup = 1 THEN 1 ELSE 0 END) AS pickup,
            SUM(CASE WHEN custom_is_pickup = 0 THEN 1 ELSE 0 END) AS delivery
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND is_return = 0
          AND posting_date BETWEEN %(fd)s AND %(td)s
          {_when(branch, _PLAIN_BRANCH)}
        GROUP BY posting_date
        ORDER BY posting_date ASC
    """,
		{"fd": fd, "td": td, "branch": branch},
		as_dict=True,
	)

	for r in rows:
		r["posting_date"] = str(r["posting_date"])
		r["pickup"] = int(r["pickup"] or 0)
		r["delivery"] = int(r["delivery"] or 0)

	return rows

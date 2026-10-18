"""Numbers for the B2B home screen: one row per account at the Active stage.

The B2B mode opens on this list (the pipeline Kanban is one tap away). Each
row is an Active Lead/Opportunity card resolved to its Customer, carrying the
handful of figures a rep or manager wants at a glance: what the account bought
recently, when it last ordered, whether it is due to reorder, and what it owes.

Everything money-related is read from submitted "B2B Supply" Sales Invoices
of the linked Customer in ONE grouped query, so the screen costs the same for
10 accounts or 100. Retail and sample invoices are left out, matching the
B2B analytics. Returns (credit notes) are netted into the billed figures but
never counted as orders. The reorder forecast comes from the Customer fields kept
by ``crm.reorder_forecast`` and is shown as-is, never recomputed here.

The pure helpers (``period_bounds``, ``build_row``, ``build_summary``) take
plain dicts so they are unit-tested without a site.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import frappe

ACTIVE_STAGE = "Active"
#: Sales Invoice.custom_order_purpose of a B2B sale (same as b2b_analytics).
B2B_PURPOSE = "B2B Supply"
DEFAULT_PERIOD_DAYS = 30
MAX_PERIOD_DAYS = 365

#: A reorder predicted within this many days counts as "due soon".
DUE_SOON_DAYS = 3
#: An unpaid invoice older than this many days flags the account.
OLD_UNPAID_DAYS = 30

#: Customer forecast fields, read only when the site has them.
_FORECAST_FIELDS = (
	"custom_predicted_next_order",
	"custom_avg_order_cycle_days",
	"custom_avg_basket_value",
)


def clamp_period_days(value: Any) -> int:
	try:
		days = int(value)
	except (TypeError, ValueError):
		return DEFAULT_PERIOD_DAYS
	return max(1, min(days, MAX_PERIOD_DAYS))


def period_bounds(today: date, period_days: int) -> tuple[date, date]:
	"""(start of the current window, start of the previous window), inclusive.

	A 30-day window ending today starts 29 days ago; the previous window is the
	30 days before that, so the two never overlap.
	"""
	start = today - timedelta(days=period_days - 1)
	return start, start - timedelta(days=period_days)


def _as_date(value: Any) -> date | None:
	if value is None or value == "":
		return None
	if isinstance(value, date):
		return value
	try:
		return date.fromisoformat(str(value)[:10])
	except ValueError:
		return None


def _num(value: Any) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _round(value: float) -> float:
	return round(float(value or 0), 2)


def reorder_status(predicted: date | None, today: date) -> str | None:
	"""``overdue`` / ``due_soon`` / ``ok``, or None when there is no forecast."""
	if predicted is None:
		return None
	if predicted <= today:
		return "overdue"
	if (predicted - today).days <= DUE_SOON_DAYS:
		return "due_soon"
	return "ok"


def change_pct(current: float, previous: float) -> float | None:
	"""Percent change, or None when there is no previous figure to compare with."""
	if not previous:
		return None
	return round((current - previous) / abs(previous) * 100, 1)


def build_row(
	card: dict[str, Any],
	stats: dict[str, Any] | None,
	forecast: dict[str, Any] | None,
	today: date,
) -> dict[str, Any]:
	"""One account row from its pipeline card, invoice stats and forecast (pure)."""
	stats = stats or {}
	forecast = forecast or {}

	orders = int(stats.get("orders") or 0)
	billed = _num(stats.get("billed"))
	period_billed = _num(stats.get("period_billed"))
	prev_period_billed = _num(stats.get("prev_period_billed"))
	outstanding = _num(stats.get("outstanding"))
	last_order = _as_date(stats.get("last_order_date"))
	oldest_unpaid = _as_date(stats.get("oldest_unpaid_date"))
	predicted = _as_date(forecast.get("custom_predicted_next_order"))
	cycle = forecast.get("custom_avg_order_cycle_days")

	status = reorder_status(predicted, today)
	oldest_unpaid_days = (today - oldest_unpaid).days if oldest_unpaid and outstanding > 0 else None

	alerts = []
	if status == "overdue":
		alerts.append("reorder_overdue")
	if oldest_unpaid_days is not None and oldest_unpaid_days > OLD_UNPAID_DAYS:
		alerts.append("old_unpaid")
	if int(card.get("label_alert") or 0) > 0:
		alerts.append("labels")
	if not card.get("customer"):
		alerts.append("no_customer")

	return {
		"doctype": card.get("doctype"),
		"name": card.get("name"),
		"title": card.get("title"),
		"customer": card.get("customer"),
		"customer_name": card.get("customer_name"),
		"owner": card.get("owner"),
		"mobile_no": card.get("mobile_no"),
		"territory": card.get("territory"),
		"orders": orders,
		"billed": _round(billed),
		"avg_order_value": _round(billed / orders) if orders else 0.0,
		"period_orders": int(stats.get("period_orders") or 0),
		"period_billed": _round(period_billed),
		"prev_period_billed": _round(prev_period_billed),
		"period_change_pct": change_pct(period_billed, prev_period_billed),
		"first_order_date": str(_as_date(stats.get("first_order_date")) or "") or None,
		"last_order_date": str(last_order) if last_order else None,
		"days_since_last_order": (today - last_order).days if last_order else None,
		"avg_order_cycle_days": _round(_num(cycle)) if cycle else None,
		"predicted_next_order": str(predicted) if predicted else None,
		"reorder_status": status,
		"outstanding": _round(outstanding),
		"oldest_unpaid_days": oldest_unpaid_days,
		"label_alert": int(card.get("label_alert") or 0),
		"last_journey_date": card.get("last_journey_date"),
		"last_journey_note": card.get("last_journey_note"),
		"next_action_date": card.get("next_action_date"),
		"next_action": card.get("next_action"),
		"alerts": alerts,
	}


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
	"""Totals across the account rows (pure)."""
	period_billed = sum(_num(r.get("period_billed")) for r in rows)
	prev_period_billed = sum(_num(r.get("prev_period_billed")) for r in rows)
	return {
		"accounts": len(rows),
		"period_orders": sum(int(r.get("period_orders") or 0) for r in rows),
		"period_billed": _round(period_billed),
		"prev_period_billed": _round(prev_period_billed),
		"period_change_pct": change_pct(period_billed, prev_period_billed),
		"outstanding": _round(sum(_num(r.get("outstanding")) for r in rows)),
		# Overdue or due within DUE_SOON_DAYS: the same set the app's filter lists.
		"reorder_due": sum(1 for r in rows if r.get("reorder_status") in ("overdue", "due_soon")),
		"needs_attention": sum(1 for r in rows if r.get("alerts")),
	}


def dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""One card per Customer, so two Active cards on one account never count twice.

	When a Lead and an Opportunity both resolve to the same Customer, the
	Opportunity is kept (it is the later step of the same account); otherwise
	the first card wins. Cards with no Customer are all kept (pure).
	"""
	kept: dict[str, int] = {}
	out: list[dict[str, Any]] = []
	for card in cards:
		customer = card.get("customer")
		if not customer:
			out.append(card)
			continue
		if customer not in kept:
			kept[customer] = len(out)
			out.append(card)
		elif card.get("doctype") == "Opportunity" and out[kept[customer]].get("doctype") != "Opportunity":
			out[kept[customer]] = card
	return out


def sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Biggest recent buyers first, then lifetime value, then name."""
	return sorted(
		rows,
		key=lambda r: (
			-_num(r.get("period_billed")),
			-_num(r.get("billed")),
			str(r.get("title") or "").lower(),
		),
	)


def invoice_stats(
	customers: list[str], period_start: date, prev_start: date, today: date | None = None
) -> dict[str, dict]:
	"""Per-customer figures in one grouped query over submitted B2B invoices."""
	customers = sorted({c for c in customers if c})
	if not customers:
		return {}
	purpose_clause = ""
	if frappe.get_meta("Sales Invoice").has_field("custom_order_purpose"):
		purpose_clause = "AND custom_order_purpose = %(purpose)s"
	rows = frappe.db.sql(
		f"""
		SELECT
			customer,
			SUM(CASE WHEN is_return = 0 THEN 1 ELSE 0 END) AS orders,
			SUM(grand_total) AS billed,
			SUM(CASE WHEN posting_date >= %(period_start)s AND is_return = 0 THEN 1 ELSE 0 END)
				AS period_orders,
			SUM(CASE WHEN posting_date >= %(period_start)s THEN grand_total ELSE 0 END)
				AS period_billed,
			SUM(CASE WHEN posting_date >= %(prev_start)s AND posting_date < %(period_start)s
				THEN grand_total ELSE 0 END) AS prev_period_billed,
			MIN(CASE WHEN is_return = 0 THEN posting_date END) AS first_order_date,
			MAX(CASE WHEN is_return = 0 THEN posting_date END) AS last_order_date,
			SUM(outstanding_amount) AS outstanding,
			MIN(CASE WHEN outstanding_amount > 0 THEN posting_date END) AS oldest_unpaid_date
		FROM `tabSales Invoice`
		WHERE docstatus = 1 AND customer IN %(customers)s AND posting_date <= %(today)s
			{purpose_clause}
		GROUP BY customer
		""",
		{
			"customers": tuple(customers),
			"period_start": period_start,
			"prev_start": prev_start,
			"purpose": B2B_PURPOSE,
			"today": today or date.today(),
		},
		as_dict=True,
	)
	return {r["customer"]: r for r in rows or []}


def customer_details(customers: list[str]) -> dict[str, dict]:
	"""Display name, territory and forecast fields for each customer."""
	customers = sorted({c for c in customers if c})
	if not customers:
		return {}
	meta = frappe.get_meta("Customer")
	fields = ["name", "customer_name", "territory", "mobile_no"]
	fields = [f for f in fields if f == "name" or meta.has_field(f)]
	fields += [f for f in _FORECAST_FIELDS if meta.has_field(f)]
	rows = frappe.get_all(
		"Customer",
		filters={"name": ["in", customers]},
		fields=fields,
		limit_page_length=0,
	)
	return {r["name"]: r for r in rows or []}

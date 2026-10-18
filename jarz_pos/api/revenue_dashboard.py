"""Revenue dashboard: today, yesterday, the last 7 days, month-to-date and any period.

Endpoint: ``jarz_pos.api.revenue_dashboard.get_revenue_dashboard`` (read-only,
JARZ Manager or Administrator).

The numbers are the Profit & Loss report's invoice basis, so the two agree:

* only submitted (``docstatus = 1``) Sales Invoices; credit notes
  (``is_return = 1``) are included as negatives;
* ``net_sales`` = ``SUM(base_net_total)`` (after discounts, before tax and
  delivery fees), ``shipping_income`` = the ``Shipping Income`` tax rows
  (``financial_report._shipping_rows``), ``total_revenue`` = the two together;
* the B2B / B2C split is ``financial_report.channel_for_purpose`` on
  ``custom_order_purpose``; ``by_source`` uses ``ops_reports._channel_sql``;
* invoices are limited to the default company, exactly like the P&L report.

``branch`` (a POS Profile) applies to every section, the snapshot included.

The fetchers are thin set-based SQL; every ``build_*`` function is pure (rows
in, payload out) so it can be tested without a site. Money is rounded with
Python ``round`` (``ops_reports._r``): ``flt(x, 2)`` goes through the precision
lookup, which raises without a site.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, nowdate

from jarz_pos.api.financial_report import (
	CHANNEL_B2B,
	CHANNEL_B2C,
	CHANNEL_SAMPLES,
	CHANNEL_STAFF,
	CHANNELS,
	NO_BRANCH_LABEL,
	_accounts,
	_default_company,
	_shipping_rows,
	branch_condition,
	bucket_key,
	bucket_keys,
	channel_for_purpose,
	granularity_for,
	validate_branch,
)
from jarz_pos.api.ops_reports import _channel_sql, _f, _r, _si_col
from jarz_pos.constants import ROLES

# ── constants ─────────────────────────────────────────────────────────────

MAX_RANGE_DAYS = 400
TOP_N = 10

#: ``custom_order_purpose`` value that ``channel_for_purpose`` maps to B2B.
B2B_PURPOSE = "B2B Supply"

#: ``by_source`` row order (``ops_reports._channel_sql`` values).
SOURCE_ORDER: tuple[str, ...] = ("pos", "woo", "talabat", "partner", "b2b", "staff", "samples")

#: Snapshot periods, in response order.
SNAPSHOT_KEYS: tuple[str, ...] = ("today", "yesterday", "last_7_days", "month_to_date")

UNSPECIFIED_METHOD = "Unspecified"


# ── access / dates ────────────────────────────────────────────────────────


def _ensure_jarz_manager() -> None:
	roles = set(frappe.get_roles(frappe.session.user))
	if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
		frappe.throw(_("Only JARZ Manager can access the revenue dashboard"), frappe.PermissionError)


def _parse_date(value: Any, today: date) -> date:
	if value in (None, ""):
		return today
	try:
		return getdate(value)
	except Exception:
		frappe.throw(_("Invalid date: {0}").format(value), frappe.ValidationError)
		raise  # unreachable; frappe.throw raises


def _parse_range(date_from: Any, date_to: Any, today: date) -> tuple[date, date]:
	"""``today .. today`` by default; a reversed range is swapped; max 400 days."""
	fd = _parse_date(date_from, today)
	td = _parse_date(date_to, today)
	if fd > td:
		fd, td = td, fd
	if (td - fd).days + 1 > MAX_RANGE_DAYS:
		frappe.throw(
			_("The date range cannot be longer than {0} days").format(MAX_RANGE_DAYS),
			frappe.ValidationError,
		)
	return fd, td


# ── windows (pure) ────────────────────────────────────────────────────────


def previous_window(fd: date, td: date) -> tuple[date, date]:
	"""The same-length window ending the day before ``fd``."""
	days = (td - fd).days + 1
	ptd = fd - timedelta(days=1)
	return ptd - timedelta(days=days - 1), ptd


def snapshot_windows(today: date) -> dict[str, tuple[date, date, date, date]]:
	"""``{key: (from, to, compare_from, compare_to)}`` for the four snapshot cards.

	* today / yesterday compare with the same weekday one week earlier;
	* last_7_days (today-6 .. today) compares with today-13 .. today-7;
	* month_to_date (1st .. today) compares with the 1st of the previous month
	  .. the same day-of-month there, clamped to that month's last day.
	"""
	t = today
	yesterday = t - timedelta(days=1)
	first = date(t.year, t.month, 1)
	prev_last = first - timedelta(days=1)
	prev_first = date(prev_last.year, prev_last.month, 1)
	prev_to = date(
		prev_first.year,
		prev_first.month,
		min(t.day, calendar.monthrange(prev_first.year, prev_first.month)[1]),
	)
	return {
		"today": (t, t, t - timedelta(days=7), t - timedelta(days=7)),
		"yesterday": (yesterday, yesterday, yesterday - timedelta(days=7), yesterday - timedelta(days=7)),
		"last_7_days": (t - timedelta(days=6), t, t - timedelta(days=13), t - timedelta(days=7)),
		"month_to_date": (first, t, prev_first, prev_to),
	}


def snapshot_span(today: date) -> tuple[date, date]:
	"""The one window that covers every snapshot period and its comparison."""
	windows = snapshot_windows(today).values()
	start = min(min(w[0], w[2]) for w in windows)
	end = max(max(w[1], w[3]) for w in windows)
	return start, end


def merge_windows(windows: Iterable[tuple[date, date]]) -> list[tuple[date, date]]:
	"""Merge overlapping or touching ``(from, to)`` windows (pure)."""
	out: list[tuple[date, date]] = []
	for start, end in sorted(windows):
		if out and start <= out[-1][1] + timedelta(days=1):
			if end > out[-1][1]:
				out[-1] = (out[-1][0], end)
		else:
			out.append((start, end))
	return out


# ── small pure helpers ────────────────────────────────────────────────────


def pct_change(cur: Any, prev: Any) -> float | None:
	"""``(cur - prev) / |prev| * 100`` to 1 dp; ``None`` when ``prev`` is 0."""
	p = _r(prev)
	if p == 0:
		return None
	return _r((_r(cur) - p) / abs(p) * 100, 1)


def share_pct(part: Any, whole: Any) -> float | None:
	"""``part / whole * 100`` to 1 dp; ``None`` when ``whole`` is 0."""
	w = _r(whole)
	if w == 0:
		return None
	return _r(_f(part) / w * 100, 1)


def aov(order_net: Any, orders: Any) -> float | None:
	"""Average order value of non-return invoices; ``None`` with no orders."""
	n = int(orders or 0)
	return _r(_f(order_net) / n) if n else None


def _day(value: Any) -> date:
	return getdate(value)


def _blank_totals() -> dict[str, Any]:
	return {
		"net": 0.0,
		"orders": 0,
		"order_net": 0.0,
		"returns": 0,
		"returns_value": 0.0,
		"discount": 0.0,
		"rows": 0,
		"channel_net": {c: 0.0 for c in CHANNELS},
		"channel_orders": {c: 0 for c in CHANNELS},
		"channel_order_net": {c: 0.0 for c in CHANNELS},
	}


def aggregate(rows: Iterable[dict[str, Any]], fd: date, td: date) -> dict[str, Any]:
	"""Sum the daily invoice rows that fall inside ``fd .. td`` (pure).

	Rows: ``purpose, posting_date, net, orders, order_net, returns,
	returns_value, discount`` (``source`` is ignored here).
	"""
	acc = _blank_totals()
	for r in rows or []:
		if not fd <= _day(r.get("posting_date")) <= td:
			continue
		c = channel_for_purpose(r.get("purpose"))
		net = _f(r.get("net"))
		orders = int(r.get("orders") or 0)
		order_net = _f(r.get("order_net"))
		acc["rows"] += 1
		acc["net"] += net
		acc["orders"] += orders
		acc["order_net"] += order_net
		acc["returns"] += int(r.get("returns") or 0)
		acc["returns_value"] += _f(r.get("returns_value"))
		acc["discount"] += _f(r.get("discount"))
		acc["channel_net"][c] += net
		acc["channel_orders"][c] += orders
		acc["channel_order_net"][c] += order_net
	return acc


def _other(values: dict[str, Any]) -> Any:
	return values[CHANNEL_STAFF] + values[CHANNEL_SAMPLES]


# ── builders (pure) ───────────────────────────────────────────────────────


def build_snapshot(daily_rows: list[dict[str, Any]], today: date) -> dict[str, dict[str, Any]]:
	out: dict[str, dict[str, Any]] = {}
	windows = snapshot_windows(today)
	for key in SNAPSHOT_KEYS:
		f, t, cf, ct = windows[key]
		cur = aggregate(daily_rows, f, t)
		cmp = aggregate(daily_rows, cf, ct)
		net = _r(cur["net"])
		compare_net = _r(cmp["net"])
		out[key] = {
			"label_from": f.isoformat(),
			"label_to": t.isoformat(),
			"net_sales": net,
			"b2c": _r(cur["channel_net"][CHANNEL_B2C]),
			"b2b": _r(cur["channel_net"][CHANNEL_B2B]),
			"other": _r(_other(cur["channel_net"])),
			"orders": int(cur["orders"]),
			"b2c_orders": int(cur["channel_orders"][CHANNEL_B2C]),
			"b2b_orders": int(cur["channel_orders"][CHANNEL_B2B]),
			"aov": aov(cur["order_net"], cur["orders"]),
			"compare_net_sales": compare_net,
			"compare_orders": int(cmp["orders"]),
			"change_pct": pct_change(net, compare_net),
			"compare_from": cf.isoformat(),
			"compare_to": ct.isoformat(),
		}
	return out


def daily_net(daily_rows: list[dict[str, Any]], fd: date, td: date) -> list[tuple[date, float]]:
	"""Net sales for every day in ``fd .. td``, zero-filled, in date order (pure)."""
	by_day: dict[date, float] = {}
	d = fd
	while d <= td:
		by_day[d] = 0.0
		d += timedelta(days=1)
	for r in daily_rows or []:
		day = _day(r.get("posting_date"))
		if day in by_day:
			by_day[day] += _f(r.get("net"))
	return sorted(by_day.items())


def best_and_worst_day(
	daily_rows: list[dict[str, Any]], fd: date, td: date
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
	"""Highest and lowest net-sales day in the range, quiet (zero) days included.

	Ties go to the earliest day. Both are ``None`` when the range has no
	invoices at all (there is no best or worst of nothing).
	"""
	if not any(fd <= _day(r.get("posting_date")) <= td for r in daily_rows or []):
		return None, None
	days = daily_net(daily_rows, fd, td)
	best = worst = days[0]
	for day in days[1:]:
		if _r(day[1]) > _r(best[1]):
			best = day
		if _r(day[1]) < _r(worst[1]):
			worst = day
	return (
		{"date": best[0].isoformat(), "net_sales": _r(best[1])},
		{"date": worst[0].isoformat(), "net_sales": _r(worst[1])},
	)


def build_summary(
	*,
	daily_rows: list[dict[str, Any]],
	fd: date,
	td: date,
	shipping_rows: list[dict[str, Any]],
	cancelled: dict[str, Any],
	customers: dict[str, Any],
) -> dict[str, Any]:
	cur = aggregate(daily_rows, fd, td)
	pfd, ptd = previous_window(fd, td)
	prev = aggregate(daily_rows, pfd, ptd)

	net = _r(cur["net"])
	b2c = _r(cur["channel_net"][CHANNEL_B2C])
	b2b = _r(cur["channel_net"][CHANNEL_B2B])
	shipping_income = _r(sum(_f(r.get("amount")) for r in shipping_rows or []))
	days = (td - fd).days + 1
	best, worst = best_and_worst_day(daily_rows, fd, td)

	prev_net = _r(prev["net"])
	prev_b2c = _r(prev["channel_net"][CHANNEL_B2C])
	prev_b2b = _r(prev["channel_net"][CHANNEL_B2B])
	prev_orders = int(prev["orders"])

	return {
		"net_sales": net,
		"b2c": b2c,
		"b2b": b2b,
		"staff": _r(cur["channel_net"][CHANNEL_STAFF]),
		"samples": _r(cur["channel_net"][CHANNEL_SAMPLES]),
		"shipping_income": shipping_income,
		"total_revenue": _r(net + shipping_income),
		"orders": int(cur["orders"]),
		"b2c_orders": int(cur["channel_orders"][CHANNEL_B2C]),
		"b2b_orders": int(cur["channel_orders"][CHANNEL_B2B]),
		"other_orders": int(_other(cur["channel_orders"])),
		"aov": aov(cur["order_net"], cur["orders"]),
		"b2c_aov": aov(cur["channel_order_net"][CHANNEL_B2C], cur["channel_orders"][CHANNEL_B2C]),
		"b2b_aov": aov(cur["channel_order_net"][CHANNEL_B2B], cur["channel_orders"][CHANNEL_B2B]),
		"b2b_share_pct": share_pct(b2b, net),
		"returns": int(cur["returns"]),
		# Credit notes carry negative net; the contract wants a positive magnitude.
		"returns_value": _r(-cur["returns_value"]),
		"cancelled": int((cancelled or {}).get("cancelled") or 0),
		"cancelled_value": _r((cancelled or {}).get("cancelled_value")),
		"discount_total": _r(cur["discount"]),
		"customers": int((customers or {}).get("customers") or 0),
		"new_customers": int((customers or {}).get("new_customers") or 0),
		"days": days,
		"daily_average": _r(net / days),
		"best_day": best,
		"worst_day": worst,
		"previous": {
			"date_from": pfd.isoformat(),
			"date_to": ptd.isoformat(),
			"net_sales": prev_net,
			"b2c": prev_b2c,
			"b2b": prev_b2b,
			"orders": prev_orders,
			"change_pct": pct_change(net, prev_net),
			"b2c_change_pct": pct_change(b2c, prev_b2c),
			"b2b_change_pct": pct_change(b2b, prev_b2b),
			"orders_change_pct": pct_change(cur["orders"], prev_orders),
		},
	}


def build_trend(
	daily_rows: list[dict[str, Any]], fd: date, td: date, granularity: str
) -> list[dict[str, Any]]:
	"""One zero-filled row per bucket of the range (pure)."""
	buckets: dict[str, dict[str, Any]] = {
		k: {"b2c": 0.0, "b2b": 0.0, "other": 0.0, "net_sales": 0.0, "orders": 0}
		for k in bucket_keys(fd, td, granularity)
	}
	for r in daily_rows or []:
		day = _day(r.get("posting_date"))
		if not fd <= day <= td:
			continue
		b = buckets.get(bucket_key(day, granularity))
		if b is None:
			continue
		c = channel_for_purpose(r.get("purpose"))
		net = _f(r.get("net"))
		b["net_sales"] += net
		b["orders"] += int(r.get("orders") or 0)
		if c in (CHANNEL_B2C, CHANNEL_B2B):
			b[c] += net
		else:
			b["other"] += net
	return [
		{
			"date": k,
			"b2c": _r(b["b2c"]),
			"b2b": _r(b["b2b"]),
			"other": _r(b["other"]),
			"net_sales": _r(b["net_sales"]),
			"orders": int(b["orders"]),
		}
		for k, b in buckets.items()
	]


def build_by_source(daily_rows: list[dict[str, Any]], fd: date, td: date) -> list[dict[str, Any]]:
	"""Net sales and orders per ``_channel_sql`` source, in contract order (pure).

	Rows with neither orders nor net sales are dropped; ``share_pct`` is of the
	range's net sales (the sum of every source).
	"""
	acc: dict[str, dict[str, Any]] = {}
	for r in daily_rows or []:
		if not fd <= _day(r.get("posting_date")) <= td:
			continue
		s = (r.get("source") or "pos").strip().lower()
		a = acc.setdefault(s, {"net_sales": 0.0, "orders": 0})
		a["net_sales"] += _f(r.get("net"))
		a["orders"] += int(r.get("orders") or 0)
	total = sum(a["net_sales"] for a in acc.values())
	order = list(SOURCE_ORDER) + sorted(s for s in acc if s not in SOURCE_ORDER)
	out = []
	for s in order:
		a = acc.get(s)
		if not a or not (a["orders"] or _r(a["net_sales"])):
			continue
		out.append(
			{
				"source": s,
				"net_sales": _r(a["net_sales"]),
				"orders": int(a["orders"]),
				"share_pct": share_pct(a["net_sales"], total),
			}
		)
	return out


def build_by_payment(dim_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Non-return invoices per ``custom_payment_method``, biggest first (pure)."""
	acc: dict[str, dict[str, Any]] = {}
	for r in dim_rows or []:
		orders = int(r.get("orders") or 0)
		order_net = _f(r.get("order_net"))
		if not orders and not _r(order_net):
			continue  # a group of credit notes only
		m = (r.get("method") or "").strip() or UNSPECIFIED_METHOD
		a = acc.setdefault(m, {"net_sales": 0.0, "orders": 0})
		a["net_sales"] += order_net
		a["orders"] += orders
	total = sum(a["net_sales"] for a in acc.values())
	rows = [
		{
			"method": m,
			"net_sales": _r(a["net_sales"]),
			"orders": int(a["orders"]),
			"share_pct": share_pct(a["net_sales"], total),
		}
		for m, a in acc.items()
	]
	rows.sort(key=lambda x: (-x["net_sales"], x["method"]))
	return rows


def build_by_branch(dim_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Net sales (returns netted) per POS Profile, biggest first (pure)."""
	acc: dict[str, dict[str, Any]] = {}
	for r in dim_rows or []:
		name = r.get("branch") or NO_BRANCH_LABEL
		a = acc.setdefault(name, {"net_sales": 0.0, CHANNEL_B2C: 0.0, CHANNEL_B2B: 0.0, "orders": 0})
		net = _f(r.get("net"))
		a["net_sales"] += net
		a["orders"] += int(r.get("orders") or 0)
		c = channel_for_purpose(r.get("purpose"))
		if c in (CHANNEL_B2C, CHANNEL_B2B):
			a[c] += net
	total = sum(a["net_sales"] for a in acc.values())
	rows = [
		{
			"branch": name,
			"net_sales": _r(a["net_sales"]),
			"b2c": _r(a[CHANNEL_B2C]),
			"b2b": _r(a[CHANNEL_B2B]),
			"orders": int(a["orders"]),
			"share_pct": share_pct(a["net_sales"], total),
		}
		for name, a in acc.items()
		if a["orders"] or _r(a["net_sales"])
	]
	rows.sort(key=lambda x: (-x["net_sales"], x["branch"]))
	return rows


def build_by_hour(dim_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Always 24 rows (0..23) of non-return orders and their net sales (pure)."""
	hours = {h: {"orders": 0, "net_sales": 0.0} for h in range(24)}
	for r in dim_rows or []:
		try:
			h = int(r.get("hour"))
		except (TypeError, ValueError):
			continue
		if 0 <= h <= 23:
			hours[h]["orders"] += int(r.get("orders") or 0)
			hours[h]["net_sales"] += _f(r.get("order_net"))
	return [
		{"hour": h, "orders": int(v["orders"]), "net_sales": _r(v["net_sales"])} for h, v in hours.items()
	]


def build_fulfilment(dim_rows: list[dict[str, Any]]) -> dict[str, int]:
	"""Non-return orders split by ``custom_is_pickup`` (pure)."""
	delivery = pickup = 0
	for r in dim_rows or []:
		orders = int(r.get("orders") or 0)
		try:
			is_pickup = int(r.get("is_pickup") or 0)
		except (TypeError, ValueError):
			is_pickup = 0
		if is_pickup:
			pickup += orders
		else:
			delivery += orders
	return {"delivery_orders": delivery, "pickup_orders": pickup}


def build_top_products(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Top items by net sales (returns netted), at most ``TOP_N`` (pure)."""
	out = [
		{
			"item_code": r.get("item_code"),
			"item_name": r.get("item_name") or r.get("item_code"),
			"qty": _r(r.get("qty"), 3),
			"net_sales": _r(r.get("net_sales")),
		}
		for r in rows or []
		if r.get("item_code")
	]
	out.sort(key=lambda x: (-x["net_sales"], str(x["item_code"])))
	return out[:TOP_N]


def build_top_b2b_customers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Top B2B customers by net sales (returns netted), at most ``TOP_N`` (pure)."""
	out = [
		{
			"customer": r.get("customer"),
			"customer_name": r.get("customer_name") or r.get("customer"),
			"net_sales": _r(r.get("net_sales")),
			"orders": int(r.get("orders") or 0),
		}
		for r in rows or []
		if r.get("customer")
	]
	out.sort(key=lambda x: (-x["net_sales"], str(x["customer"])))
	return out[:TOP_N]


def build_dashboard(
	*,
	today: date,
	fd: date,
	td: date,
	branch: str | None,
	generated_at: str,
	daily_rows: list[dict[str, Any]],
	shipping_rows: list[dict[str, Any]],
	cancelled: dict[str, Any],
	dim_rows: list[dict[str, Any]],
	product_rows: list[dict[str, Any]],
	b2b_customer_rows: list[dict[str, Any]],
	customers: dict[str, Any],
) -> dict[str, Any]:
	"""Assemble the payload (pure).

	``daily_rows`` must cover the snapshot span and ``previous_window(fd, td)``
	.. ``td``; every other row set covers ``fd .. td`` only.
	"""
	granularity = granularity_for(fd, td)
	return {
		"generated_at": generated_at,
		"today": today.isoformat(),
		"filters": {"date_from": fd.isoformat(), "date_to": td.isoformat(), "branch": branch or None},
		"snapshot": build_snapshot(daily_rows, today),
		"summary": build_summary(
			daily_rows=daily_rows,
			fd=fd,
			td=td,
			shipping_rows=shipping_rows,
			cancelled=cancelled,
			customers=customers,
		),
		"granularity": granularity,
		"trend": build_trend(daily_rows, fd, td, granularity),
		"by_source": build_by_source(daily_rows, fd, td),
		"by_payment": build_by_payment(dim_rows),
		"by_branch": build_by_branch(dim_rows),
		"by_hour": build_by_hour(dim_rows),
		"fulfilment": build_fulfilment(dim_rows),
		"top_products": build_top_products(product_rows),
		"top_b2b_customers": build_top_b2b_customers(b2b_customer_rows),
	}


# ── fetchers (thin SQL) ───────────────────────────────────────────────────


def _cond(company: str | None, branch: str | None) -> str:
	"""Constant company/branch filter on alias ``si``; values are bound parameters."""
	cond = "AND si.company = %(company)s" if company else ""
	return cond + " " + branch_condition(branch)


def _params(fd: date, td: date, company: str | None, branch: str | None, **extra) -> dict[str, Any]:
	params: dict[str, Any] = {"fd": fd, "td": td, "company": company, "branch": branch}
	params.update(extra)
	return params


def _daily_rows(fd: date, td: date, company: str | None, branch: str | None) -> list[dict[str, Any]]:
	"""Per (order purpose, source, day): net (returns netted), orders, returns, discount.

	Discount = invoice-wide ``base_discount_amount`` plus every line's
	``discount_amount * qty`` (price list minus selling rate). Promo codes and
	bundle pricing are recorded on the lines, so the invoice field alone reads 0.
	"""
	cond = _cond(company, branch)
	source = _channel_sql("si")
	# nosemgrep: frappe-sql-format-injection -- only constant fragments, schema-probed column names and constant alias/param names interpolated; values are bound parameters
	return frappe.db.sql(
		f"""
        SELECT si.custom_order_purpose AS purpose,
               {source} AS sale_source,
               si.posting_date AS posting_date,
               SUM(si.base_net_total) AS net,
               SUM(CASE WHEN si.is_return = 0 THEN 1 ELSE 0 END) AS orders,
               SUM(CASE WHEN si.is_return = 0 THEN si.base_net_total ELSE 0 END) AS order_net,
               SUM(CASE WHEN si.is_return = 1 THEN 1 ELSE 0 END) AS returns,
               SUM(CASE WHEN si.is_return = 1 THEN si.base_net_total ELSE 0 END) AS returns_value,
               SUM(CASE WHEN si.is_return = 0
                        THEN si.base_discount_amount + IFNULL((
                            SELECT SUM(GREATEST(sii.discount_amount, 0) * sii.qty) * IFNULL(si.conversion_rate, 1)
                            FROM `tabSales Invoice Item` sii
                            WHERE sii.parent = si.name AND sii.parenttype = 'Sales Invoice'
                        ), 0)
                        ELSE 0 END) AS discount
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY si.custom_order_purpose, sale_source, si.posting_date
        """,
		_params(fd, td, company, branch),
		as_dict=True,
	)


def _cancelled(fd: date, td: date, company: str | None, branch: str | None) -> dict[str, Any]:
	"""Cancelled non-return invoices in range, amendments excluded (as daily close)."""
	cond = _cond(company, branch)
	cancel_type = _si_col("custom_cancellation_type", "si", "''")
	# nosemgrep: frappe-sql-format-injection -- only constant fragments, schema-probed column names and constant alias/param names interpolated; values are bound parameters
	rows = frappe.db.sql(
		f"""
        SELECT COUNT(*) AS cancelled, COALESCE(SUM(si.base_net_total), 0) AS cancelled_value
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 2
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND IFNULL({cancel_type}, '') != 'Amended'
          AND si.name NOT IN (
              SELECT a.amended_from FROM `tabSales Invoice` a
              WHERE IFNULL(a.amended_from, '') != ''
          )
          {cond}
        """,
		_params(fd, td, company, branch),
		as_dict=True,
	)
	return rows[0] if rows else {}


def _dimension_rows(fd: date, td: date, company: str | None, branch: str | None) -> list[dict[str, Any]]:
	"""Per (branch, purpose, payment method, hour, pickup) for the range.

	One query feeds by_branch, by_payment, by_hour and fulfilment. Aliases are
	deliberately unlike any Sales Invoice column: MariaDB resolves a GROUP BY
	name against the table before the select list.
	"""
	cond = _cond(company, branch)
	method = _si_col("custom_payment_method", "si", "''")
	pickup = _si_col("custom_is_pickup", "si", "0")
	# nosemgrep: frappe-sql-format-injection -- only constant fragments, schema-probed column names and constant alias/param names interpolated; values are bound parameters
	return frappe.db.sql(
		f"""
        SELECT COALESCE(NULLIF(si.pos_profile, ''), %(nb)s) AS branch_label,
               si.custom_order_purpose AS order_purpose,
               TRIM(IFNULL({method}, '')) AS pay_method,
               HOUR(si.posting_time) AS order_hour,
               IFNULL({pickup}, 0) AS pickup_flag,
               SUM(si.base_net_total) AS net,
               SUM(CASE WHEN si.is_return = 0 THEN 1 ELSE 0 END) AS orders,
               SUM(CASE WHEN si.is_return = 0 THEN si.base_net_total ELSE 0 END) AS order_net
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY branch_label, order_purpose, pay_method, order_hour, pickup_flag
        """,
		_params(fd, td, company, branch, nb=NO_BRANCH_LABEL),
		as_dict=True,
	)


def _product_rows(fd: date, td: date, company: str | None, branch: str | None) -> list[dict[str, Any]]:
	"""Top items by line net amount, credit-note lines netted."""
	cond = _cond(company, branch)
	# nosemgrep: frappe-sql-format-injection -- only constant fragments, an integer constant and constant alias/param names interpolated; values are bound parameters
	return frappe.db.sql(
		f"""
        SELECT sii.item_code AS item_code,
               MAX(sii.item_name) AS item_name,
               SUM(sii.qty) AS qty,
               SUM(sii.base_net_amount) AS net_sales
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si
          ON si.name = sii.parent AND sii.parenttype = 'Sales Invoice'
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY sii.item_code
        ORDER BY net_sales DESC, sii.item_code
        LIMIT {int(TOP_N)}
        """,
		_params(fd, td, company, branch),
		as_dict=True,
	)


def _b2b_customer_rows(fd: date, td: date, company: str | None, branch: str | None) -> list[dict[str, Any]]:
	"""Top B2B Supply customers by net sales, credit notes netted."""
	cond = _cond(company, branch)
	# nosemgrep: frappe-sql-format-injection -- only constant fragments, an integer constant and constant alias/param names interpolated; values are bound parameters
	return frappe.db.sql(
		f"""
        SELECT si.customer AS customer,
               MAX(si.customer_name) AS customer_name,
               SUM(si.base_net_total) AS net_sales,
               SUM(CASE WHEN si.is_return = 0 THEN 1 ELSE 0 END) AS orders
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND TRIM(IFNULL(si.custom_order_purpose, '')) = %(b2b_purpose)s
          {cond}
        GROUP BY si.customer
        ORDER BY net_sales DESC, si.customer
        LIMIT {int(TOP_N)}
        """,
		_params(fd, td, company, branch, b2b_purpose=B2B_PURPOSE),
		as_dict=True,
	)


def _customer_counts(fd: date, td: date, company: str | None, branch: str | None) -> dict[str, Any]:
	"""Distinct customers with a non-return invoice in range, and how many are new.

	"New" = the customer's first-ever submitted non-return invoice (any branch)
	falls inside the range. The first-invoice subquery only looks at customers
	present in the range.
	"""
	cond = _cond(company, branch)
	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
	rows = frappe.db.sql(
		f"""
        SELECT COUNT(*) AS customers,
               COALESCE(SUM(CASE WHEN fx.first_date >= %(fd)s THEN 1 ELSE 0 END), 0) AS new_customers
        FROM (
            SELECT DISTINCT si.customer AS customer
            FROM `tabSales Invoice` si
            WHERE si.docstatus = 1
              AND si.is_return = 0
              AND si.posting_date BETWEEN %(fd)s AND %(td)s
              AND IFNULL(si.customer, '') != ''
              {cond}
        ) c
        LEFT JOIN (
            SELECT x.customer AS customer, MIN(x.posting_date) AS first_date
            FROM `tabSales Invoice` x
            WHERE x.docstatus = 1
              AND x.is_return = 0
              AND x.customer IN (
                  SELECT si.customer
                  FROM `tabSales Invoice` si
                  WHERE si.docstatus = 1
                    AND si.is_return = 0
                    AND si.posting_date BETWEEN %(fd)s AND %(td)s
                    {cond}
              )
            GROUP BY x.customer
        ) fx ON fx.customer = c.customer
        """,
		_params(fd, td, company, branch),
		as_dict=True,
	)
	return rows[0] if rows else {}


# ── endpoint ──────────────────────────────────────────────────────────────


@frappe.whitelist()
def get_revenue_dashboard(
	date_from: str | None = None,
	date_to: str | None = None,
	branch: str | None = None,
) -> dict[str, Any]:
	"""Daily revenue dashboard: snapshot cards, the selected range and its breakdowns.

	``date_from`` / ``date_to`` default to today .. today (site timezone); the
	range may not exceed 400 days. ``branch`` is an optional POS Profile and
	filters every section, the snapshot included. See the module docstring for
	the definitions shared with the Profit & Loss report.
	"""
	_ensure_jarz_manager()
	today = getdate(nowdate())
	fd, td = _parse_range(date_from, date_to, today)
	branch = validate_branch(branch)
	company = _default_company()
	shipping_account, _freight = _accounts(company)

	# One daily query covers the snapshot span and the selected range with its
	# previous window; it only splits in two when those windows are disjoint.
	pfd, _ptd = previous_window(fd, td)
	daily_rows: list[dict[str, Any]] = []
	for start, end in merge_windows([snapshot_span(today), (pfd, td)]):
		daily_rows.extend(_daily_rows(start, end, company, branch) or [])
	for r in daily_rows:
		r["source"] = r.get("sale_source")

	dim_rows = [
		{
			"branch": r.get("branch_label"),
			"purpose": r.get("order_purpose"),
			"method": r.get("pay_method"),
			"hour": r.get("order_hour"),
			"is_pickup": r.get("pickup_flag"),
			"net": r.get("net"),
			"orders": r.get("orders"),
			"order_net": r.get("order_net"),
		}
		for r in (_dimension_rows(fd, td, company, branch) or [])
	]

	return build_dashboard(
		today=today,
		fd=fd,
		td=td,
		branch=branch,
		generated_at=now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
		daily_rows=daily_rows,
		shipping_rows=_shipping_rows(fd, td, company, shipping_account, branch) or [],
		cancelled=_cancelled(fd, td, company, branch),
		dim_rows=dim_rows,
		product_rows=_product_rows(fd, td, company, branch) or [],
		b2b_customer_rows=_b2b_customer_rows(fd, td, company, branch) or [],
		customers=_customer_counts(fd, td, company, branch),
	)

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

Comparisons are like-for-like. A range shorter than a week compares with the
same weekdays one week earlier, a longer one with the same-length window just
before it; when the range ends today, the comparison's last day is cut at the
current time ("today so far" vs "the same day last week up to now"). The
``attention`` list names only what needs a look: cancellations, returns, a
branch's B2C or a source's sales well down on the comparison, unusual
discounting.

``trend``, ``top_products`` and ``top_b2b_customers`` are no longer shown by
the daily-first layout; they stay in the payload for app builds up to 1.0.0+659.

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

#: ``attention``: a branch (B2C) or a source is flagged at this % change or worse,
#: when its comparison had at least ``DROP_MIN_BASE`` of sales; "high" at ``DROP_HIGH_PCT``.
DROP_PCT = -25.0
DROP_HIGH_PCT = -50.0
DROP_MIN_BASE = 500.0

#: Sources that never raise a drop: B2B orders are lumpy, staff and samples are not sales.
NO_DROP_SOURCES: frozenset[str] = frozenset({"b2b", "staff", "samples"})

#: Discounts are flagged at ``DISCOUNT_MIN`` or more when their share of gross sales is
#: at least ``DISCOUNT_RATIO`` x the comparison's and ``DISCOUNT_MIN_POINTS`` higher.
DISCOUNT_MIN = 300.0
DISCOUNT_RATIO = 1.5
DISCOUNT_MIN_POINTS = 2.0


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


def compare_window(fd: date, td: date) -> tuple[date, date]:
	"""The like-for-like comparison of ``fd .. td``.

	Under 7 days: the same weekdays one week earlier (a Friday against the last
	Friday, not against Thursday). 7 days or more: ``previous_window``.
	"""
	if (td - fd).days + 1 < 7:
		return fd - timedelta(days=7), td - timedelta(days=7)
	return previous_window(fd, td)


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


def _in_window(r: dict[str, Any], fd: date, td: date, cut_day: date | None) -> bool:
	"""Row inside ``fd .. td``; on ``cut_day`` only rows posted up to the cut-off."""
	day = _day(r.get("posting_date"))
	if not fd <= day <= td:
		return False
	return not (cut_day is not None and day == cut_day and int(r.get("late") or 0))


def aggregate(
	rows: Iterable[dict[str, Any]], fd: date, td: date, cut_day: date | None = None
) -> dict[str, Any]:
	"""Sum the daily invoice rows that fall inside ``fd .. td`` (pure).

	Rows: ``purpose, posting_date, net, orders, order_net, returns,
	returns_value, discount`` and optionally ``late`` (posted after the cut-off
	time); ``source`` and ``branch`` are ignored here. On ``cut_day`` the late
	rows are left out.
	"""
	acc = _blank_totals()
	for r in rows or []:
		if not _in_window(r, fd, td, cut_day):
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


def cut_for(window_to: date, compare_to: date, today: date | None, cutoff: str | None) -> date | None:
	"""The comparison day to cut at ``cutoff`` (only when the window ends today)."""
	return compare_to if cutoff and window_to == today else None


def _hhmm(cutoff: str | None) -> str | None:
	return cutoff[:5] if cutoff else None


def _other(values: dict[str, Any]) -> Any:
	return values[CHANNEL_STAFF] + values[CHANNEL_SAMPLES]


# ── builders (pure) ───────────────────────────────────────────────────────


def build_snapshot(
	daily_rows: list[dict[str, Any]], today: date, cutoff: str | None = None
) -> dict[str, dict[str, Any]]:
	"""The four cards; a card ending today compares up to ``cutoff`` (``HH:MM:SS``)."""
	out: dict[str, dict[str, Any]] = {}
	windows = snapshot_windows(today)
	for key in SNAPSHOT_KEYS:
		f, t, cf, ct = windows[key]
		cut_day = cut_for(t, ct, today, cutoff)
		cur = aggregate(daily_rows, f, t)
		cmp = aggregate(daily_rows, cf, ct, cut_day)
		net = _r(cur["net"])
		compare_net = _r(cmp["net"])
		cur_aov = aov(cur["order_net"], cur["orders"])
		cmp_aov = aov(cmp["order_net"], cmp["orders"])
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
			"aov": cur_aov,
			"compare_net_sales": compare_net,
			"compare_orders": int(cmp["orders"]),
			"change_pct": pct_change(net, compare_net),
			"compare_from": cf.isoformat(),
			"compare_to": ct.isoformat(),
			"compare_aov": cmp_aov,
			"orders_change_pct": pct_change(cur["orders"], cmp["orders"]),
			"aov_change_pct": pct_change(cur_aov, cmp_aov) if cur_aov is not None else None,
			"compare_cut_at": _hhmm(cutoff) if cut_day else None,
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
	today: date | None = None,
	cutoff: str | None = None,
) -> dict[str, Any]:
	cur = aggregate(daily_rows, fd, td)
	pfd, ptd = compare_window(fd, td)
	cut_day = cut_for(td, ptd, today, cutoff)
	prev = aggregate(daily_rows, pfd, ptd, cut_day)

	net = _r(cur["net"])
	b2c = _r(cur["channel_net"][CHANNEL_B2C])
	b2b = _r(cur["channel_net"][CHANNEL_B2B])
	shipping_income = _r(sum(_f(r.get("amount")) for r in shipping_rows or []))
	days = (td - fd).days + 1
	# Today is still in progress: it would always be the "worst day" of a range ending today.
	last_whole_day = td - timedelta(days=1) if today == td and fd < td else td
	best, worst = best_and_worst_day(daily_rows, fd, last_whole_day)

	prev_net = _r(prev["net"])
	prev_b2c = _r(prev["channel_net"][CHANNEL_B2C])
	prev_b2b = _r(prev["channel_net"][CHANNEL_B2B])
	prev_orders = int(prev["orders"])
	cur_aov = aov(cur["order_net"], cur["orders"])
	prev_aov = aov(prev["order_net"], prev["orders"])

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
		"aov": cur_aov,
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
			"aov": prev_aov,
			"aov_change_pct": pct_change(cur_aov, prev_aov) if cur_aov is not None else None,
			"discount_total": _r(prev["discount"]),
			"cut_at": _hhmm(cutoff) if cut_day else None,
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


def _source_of(r: dict[str, Any]) -> str:
	return (r.get("source") or "pos").strip().lower()


def _branch_of(r: dict[str, Any]) -> str:
	return r.get("branch") or NO_BRANCH_LABEL


def totals_by(
	daily_rows: list[dict[str, Any]], key, fd: date, td: date, cut_day: date | None = None
) -> dict[str, dict[str, float]]:
	"""``{key(row): {"net", "b2c"}}`` over the window (pure)."""
	acc: dict[str, dict[str, float]] = {}
	for r in daily_rows or []:
		if not _in_window(r, fd, td, cut_day):
			continue
		a = acc.setdefault(key(r), {"net": 0.0, "b2c": 0.0})
		net = _f(r.get("net"))
		a["net"] += net
		if channel_for_purpose(r.get("purpose")) == CHANNEL_B2C:
			a["b2c"] += net
	return acc


def build_by_source(
	daily_rows: list[dict[str, Any]],
	fd: date,
	td: date,
	compare: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
	"""Net sales and orders per ``_channel_sql`` source, in contract order (pure).

	Rows with neither orders nor net sales are dropped; ``share_pct`` is of the
	range's net sales (the sum of every source). With ``compare`` (``totals_by``
	over the comparison window) each row also carries ``compare_net_sales`` and
	``change_pct``, and a source that sold only in the comparison is kept at 0.
	"""
	acc: dict[str, dict[str, Any]] = {}
	for r in daily_rows or []:
		if not fd <= _day(r.get("posting_date")) <= td:
			continue
		a = acc.setdefault(_source_of(r), {"net_sales": 0.0, "orders": 0})
		a["net_sales"] += _f(r.get("net"))
		a["orders"] += int(r.get("orders") or 0)
	total = sum(a["net_sales"] for a in acc.values())
	names = set(acc) | {s for s, c in (compare or {}).items() if _r(c["net"])}
	order = list(SOURCE_ORDER) + sorted(s for s in names if s not in SOURCE_ORDER)
	out = []
	for s in order:
		a = acc.get(s) or {"net_sales": 0.0, "orders": 0}
		c = (compare or {}).get(s)
		has_compare = bool(c and _r(c["net"]))
		if not (a["orders"] or _r(a["net_sales"]) or has_compare):
			continue
		row = {
			"source": s,
			"net_sales": _r(a["net_sales"]),
			"orders": int(a["orders"]),
			"share_pct": share_pct(a["net_sales"], total),
		}
		if compare is not None:
			prev = _r(c["net"]) if c else 0.0
			row["compare_net_sales"] = prev
			row["change_pct"] = pct_change(a["net_sales"], prev)
		out.append(row)
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


def build_by_branch(
	dim_rows: list[dict[str, Any]], compare: dict[str, dict[str, float]] | None = None
) -> list[dict[str, Any]]:
	"""Net sales (returns netted) per POS Profile, biggest first (pure).

	With ``compare`` (``totals_by`` branch over the comparison window) each row
	also carries ``compare_net_sales`` / ``change_pct`` and the B2C pair
	``compare_b2c`` / ``b2c_change_pct``; a branch that sold only in the
	comparison is kept at 0.
	"""
	acc: dict[str, dict[str, Any]] = {}
	for name, c in (compare or {}).items():
		if _r(c["net"]):
			acc.setdefault(name, {"net_sales": 0.0, CHANNEL_B2C: 0.0, CHANNEL_B2B: 0.0, "orders": 0})
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
	rows = []
	for name, a in acc.items():
		c = (compare or {}).get(name)
		if not (a["orders"] or _r(a["net_sales"]) or (c and _r(c["net"]))):
			continue
		row = {
			"branch": name,
			"net_sales": _r(a["net_sales"]),
			"b2c": _r(a[CHANNEL_B2C]),
			"b2b": _r(a[CHANNEL_B2B]),
			"orders": int(a["orders"]),
			"share_pct": share_pct(a["net_sales"], total),
		}
		if compare is not None:
			prev, prev_b2c = (_r(c["net"]), _r(c["b2c"])) if c else (0.0, 0.0)
			row["compare_net_sales"] = prev
			row["change_pct"] = pct_change(a["net_sales"], prev)
			row["compare_b2c"] = prev_b2c
			row["b2c_change_pct"] = pct_change(a[CHANNEL_B2C], prev_b2c)
		rows.append(row)
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


def _drop(change_pct: float | None, base: float) -> str | None:
	"""``"high"`` / ``"medium"`` for a flagged drop, else ``None``."""
	if change_pct is None or base < DROP_MIN_BASE or change_pct > DROP_PCT:
		return None
	return "high" if change_pct <= DROP_HIGH_PCT else "medium"


def discount_pct(discount: Any, net: Any) -> float | None:
	"""Discount as a % of gross (net + discount); ``None`` with no sales."""
	return share_pct(discount, _f(net) + _f(discount))


def build_attention(
	summary: dict[str, Any], by_source: list[dict[str, Any]], by_branch: list[dict[str, Any]]
) -> list[dict[str, Any]]:
	"""What needs a look in the range, most actionable first (pure).

	Kinds: ``cancelled`` / ``returns`` (count, value); ``branch_drop`` (a
	branch's B2C sales vs the comparison); ``source_drop``; ``discount_high``.
	Clients phrase each kind themselves.
	"""
	out: list[dict[str, Any]] = []
	for kind in ("cancelled", "returns"):
		if summary.get(kind):
			out.append(
				{
					"kind": kind,
					"severity": "medium",
					"count": int(summary[kind]),
					"value": _r(summary.get(f"{kind}_value")),
				}
			)
	drops = []
	for b in by_branch or []:
		sev = _drop(b.get("b2c_change_pct"), _f(b.get("compare_b2c")))
		if sev:
			drops.append(
				{
					"kind": "branch_drop",
					"severity": sev,
					"name": b["branch"],
					"net_sales": _r(b.get("b2c")),
					"compare_net_sales": _r(b.get("compare_b2c")),
					"change_pct": b["b2c_change_pct"],
				}
			)
	for s in by_source or []:
		if s["source"] in NO_DROP_SOURCES:
			continue
		sev = _drop(s.get("change_pct"), _f(s.get("compare_net_sales")))
		if sev:
			drops.append(
				{
					"kind": "source_drop",
					"severity": sev,
					"name": s["source"],
					"net_sales": _r(s.get("net_sales")),
					"compare_net_sales": _r(s.get("compare_net_sales")),
					"change_pct": s["change_pct"],
				}
			)
	drops.sort(key=lambda x: (x["kind"] != "branch_drop", x["change_pct"], x["name"]))
	out.extend(drops)

	prev = summary.get("previous") or {}
	disc = _f(summary.get("discount_total"))
	cur_pct = discount_pct(disc, summary.get("net_sales"))
	prev_pct = discount_pct(prev.get("discount_total"), prev.get("net_sales")) or 0.0
	if (
		disc >= DISCOUNT_MIN
		and cur_pct is not None
		and cur_pct >= prev_pct * DISCOUNT_RATIO
		and cur_pct - prev_pct >= DISCOUNT_MIN_POINTS
	):
		out.append(
			{
				"kind": "discount_high",
				"severity": "medium",
				"value": _r(disc),
				"discount_pct": cur_pct,
				"compare_discount_pct": prev_pct,
			}
		)
	return out


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
	cutoff: str | None = None,
) -> dict[str, Any]:
	"""Assemble the payload (pure).

	``daily_rows`` must cover the snapshot span and ``compare_window(fd, td)``
	.. ``td``; every other row set covers ``fd .. td`` only. ``cutoff``
	(``HH:MM:SS``, the time of the request) cuts the comparison of anything
	ending today; the rows' ``late`` flag is relative to it.
	"""
	granularity = granularity_for(fd, td)
	cfd, ctd = compare_window(fd, td)
	cut_day = cut_for(td, ctd, today, cutoff)
	summary = build_summary(
		daily_rows=daily_rows,
		fd=fd,
		td=td,
		shipping_rows=shipping_rows,
		cancelled=cancelled,
		customers=customers,
		today=today,
		cutoff=cutoff,
	)
	by_source = build_by_source(daily_rows, fd, td, totals_by(daily_rows, _source_of, cfd, ctd, cut_day))
	by_branch = build_by_branch(dim_rows, totals_by(daily_rows, _branch_of, cfd, ctd, cut_day))
	return {
		"generated_at": generated_at,
		"today": today.isoformat(),
		"filters": {"date_from": fd.isoformat(), "date_to": td.isoformat(), "branch": branch or None},
		"snapshot": build_snapshot(daily_rows, today, cutoff),
		"summary": summary,
		"attention": build_attention(summary, by_source, by_branch),
		"granularity": granularity,
		"trend": build_trend(daily_rows, fd, td, granularity),
		"by_source": by_source,
		"by_payment": build_by_payment(dim_rows),
		"by_branch": by_branch,
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


def _daily_rows(
	fd: date, td: date, company: str | None, branch: str | None, cutoff: str = "23:59:59"
) -> list[dict[str, Any]]:
	"""Per (purpose, source, branch, day, late): net (returns netted), orders, returns, discount.

	``late`` = posted after ``cutoff`` (``HH:MM:SS``), so a comparison day can be
	cut at the time of the request without another query.

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
               COALESCE(NULLIF(si.pos_profile, ''), %(nb)s) AS branch_label,
               si.posting_date AS posting_date,
               CASE WHEN si.posting_time > %(cutoff)s THEN 1 ELSE 0 END AS late_flag,
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
        GROUP BY si.custom_order_purpose, sale_source, branch_label, si.posting_date, late_flag
        """,
		_params(fd, td, company, branch, nb=NO_BRANCH_LABEL, cutoff=cutoff),
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

	now = now_datetime()
	cutoff = now.strftime("%H:%M:%S")

	# One daily query covers the snapshot span and the selected range with its
	# comparison window; it only splits in two when those windows are disjoint.
	cfd, _ctd = compare_window(fd, td)
	daily_rows: list[dict[str, Any]] = []
	for start, end in merge_windows([snapshot_span(today), (cfd, td)]):
		daily_rows.extend(_daily_rows(start, end, company, branch, cutoff=cutoff) or [])
	for r in daily_rows:
		r["source"] = r.get("sale_source")
		r["branch"] = r.get("branch_label")
		r["late"] = r.get("late_flag")

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
		generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
		daily_rows=daily_rows,
		shipping_rows=_shipping_rows(fd, td, company, shipping_account, branch) or [],
		cancelled=_cancelled(fd, td, company, branch),
		dim_rows=dim_rows,
		product_rows=_product_rows(fd, td, company, branch) or [],
		b2b_customer_rows=_b2b_customer_rows(fd, td, company, branch) or [],
		customers=_customer_counts(fd, td, company, branch),
		cutoff=cutoff,
	)

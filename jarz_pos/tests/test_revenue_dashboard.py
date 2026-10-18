"""Revenue dashboard: the builders are pure (rows in, payload out), so no site is needed.

The fixtures use the same row shapes the fetchers return. The whitelisted entry
is exercised with ``frappe`` patched the way ``TestBranchValidation`` in
``test_api_financial_report`` does it.
"""

import unittest
from datetime import date, datetime

from jarz_pos.api import financial_report as fr
from jarz_pos.api import revenue_dashboard as rd

SHIP = "Shipping Income - J"
FREIGHT = "Freight and Forwarding Charges - J"


def d(
	purpose,
	day,
	net,
	orders=1,
	order_net=None,
	returns=0,
	returns_value=0.0,
	discount=0.0,
	source="pos",
	branch=None,
	late=0,
):
	"""A ``_daily_rows`` row, after the entry renames ``sale_source`` / ``branch_label`` / ``late_flag``."""
	return {
		"purpose": purpose,
		"source": source,
		"branch": branch,
		"late": late,
		"posting_date": day,
		"net": net,
		"orders": orders,
		"order_net": net if order_net is None else order_net,
		"returns": returns,
		"returns_value": returns_value,
		"discount": discount,
	}


def october_range_rows():
	"""Range 2026-10-01..03 plus its comparison, the same weekdays a week earlier (09-24..26)."""
	return [
		d("Standard", date(2026, 10, 1), 1000, orders=5),
		# A credit note: negative net, no order, counted as a return.
		d("Standard", date(2026, 10, 1), -100, orders=0, order_net=0, returns=1, returns_value=-100),
		d("B2B Supply", date(2026, 10, 2), 500, orders=2, discount=20, source="b2b"),
		d("Employee", date(2026, 10, 2), 50, orders=1, source="staff"),
		d("Sample - Courier", date(2026, 10, 3), 0, orders=1, source="samples"),
		# comparison window
		d("Standard", date(2026, 9, 25), 725, orders=3),
		# outside both windows
		d("Standard", date(2026, 9, 27), 9999, orders=9),
		d("Standard", date(2026, 9, 23), 9999, orders=9),
	]


def summary(rows=None, fd=date(2026, 10, 1), td=date(2026, 10, 3), **kw):
	return rd.build_summary(
		daily_rows=october_range_rows() if rows is None else rows,
		fd=fd,
		td=td,
		shipping_rows=kw.get("shipping_rows", [{"purpose": "Standard", "posting_date": fd, "amount": 60}]),
		cancelled=kw.get("cancelled", {"cancelled": 2, "cancelled_value": 123.4567}),
		customers=kw.get("customers", {"customers": 7, "new_customers": 3}),
		today=kw.get("today"),
		cutoff=kw.get("cutoff"),
	)


# ── windows ───────────────────────────────────────────────────────────────


class TestWindows(unittest.TestCase):
	def test_snapshot_comparisons(self):
		w = rd.snapshot_windows(date(2026, 10, 18))
		self.assertEqual(list(w), ["today", "yesterday", "last_7_days", "month_to_date"])
		# today compares with the same weekday last week, not with yesterday
		self.assertEqual(
			w["today"], (date(2026, 10, 18), date(2026, 10, 18), date(2026, 10, 11), date(2026, 10, 11))
		)
		self.assertEqual(
			w["yesterday"], (date(2026, 10, 17), date(2026, 10, 17), date(2026, 10, 10), date(2026, 10, 10))
		)
		self.assertEqual(
			w["last_7_days"], (date(2026, 10, 12), date(2026, 10, 18), date(2026, 10, 5), date(2026, 10, 11))
		)
		self.assertEqual(
			w["month_to_date"], (date(2026, 10, 1), date(2026, 10, 18), date(2026, 9, 1), date(2026, 9, 18))
		)

	def test_month_to_date_clamps_on_the_31st(self):
		w = rd.snapshot_windows(date(2026, 10, 31))
		self.assertEqual(
			w["month_to_date"], (date(2026, 10, 1), date(2026, 10, 31), date(2026, 9, 1), date(2026, 9, 30))
		)
		w = rd.snapshot_windows(date(2026, 3, 31))
		self.assertEqual(w["month_to_date"][2:], (date(2026, 2, 1), date(2026, 2, 28)))

	def test_month_to_date_in_january_compares_with_december(self):
		w = rd.snapshot_windows(date(2026, 1, 15))
		self.assertEqual(w["month_to_date"][2:], (date(2025, 12, 1), date(2025, 12, 15)))

	def test_snapshot_span_covers_every_window(self):
		self.assertEqual(rd.snapshot_span(date(2026, 10, 18)), (date(2026, 9, 1), date(2026, 10, 18)))
		# Early in a month the 13-day look-back still sits inside the previous month.
		self.assertEqual(rd.snapshot_span(date(2026, 3, 1)), (date(2026, 2, 1), date(2026, 3, 1)))

	def test_previous_window_is_the_same_length_ending_the_day_before(self):
		self.assertEqual(
			rd.previous_window(date(2026, 10, 1), date(2026, 10, 18)), (date(2026, 9, 13), date(2026, 9, 30))
		)
		self.assertEqual(
			rd.previous_window(date(2026, 10, 18), date(2026, 10, 18)),
			(date(2026, 10, 17), date(2026, 10, 17)),
		)
		self.assertEqual(
			rd.previous_window(date(2026, 3, 1), date(2026, 3, 31)), (date(2026, 1, 29), date(2026, 2, 28))
		)

	def test_compare_window_is_like_for_like(self):
		# A day or a few days: the same weekdays one week earlier.
		self.assertEqual(
			rd.compare_window(date(2026, 10, 18), date(2026, 10, 18)),
			(date(2026, 10, 11), date(2026, 10, 11)),
		)
		self.assertEqual(
			rd.compare_window(date(2026, 10, 13), date(2026, 10, 18)), (date(2026, 10, 6), date(2026, 10, 11))
		)
		# A week or more: the same-length window just before it.
		self.assertEqual(
			rd.compare_window(date(2026, 10, 12), date(2026, 10, 18)), (date(2026, 10, 5), date(2026, 10, 11))
		)
		self.assertEqual(
			rd.compare_window(date(2026, 10, 1), date(2026, 10, 18)), (date(2026, 9, 13), date(2026, 9, 30))
		)

	def test_merge_windows(self):
		a = (date(2026, 9, 1), date(2026, 10, 18))
		self.assertEqual(rd.merge_windows([a, (date(2026, 10, 17), date(2026, 10, 18))]), [a])
		# touching windows merge into one query
		self.assertEqual(
			rd.merge_windows([(date(2026, 8, 1), date(2026, 8, 31)), a]),
			[(date(2026, 8, 1), date(2026, 10, 18))],
		)
		# disjoint windows stay apart (two queries, never a scan of the gap)
		far = (date(2025, 1, 1), date(2025, 1, 31))
		self.assertEqual(rd.merge_windows([a, far]), [far, a])

	def test_parse_range_limit(self):
		today = date(2026, 10, 18)
		self.assertEqual(rd._parse_range(None, None, today), (today, today))
		self.assertEqual(
			rd._parse_range("2026-10-18", "2026-10-01", today), (date(2026, 10, 1), date(2026, 10, 18))
		)
		# exactly 400 days is allowed
		self.assertEqual(rd._parse_range("2025-09-14", "2026-10-18", today)[0], date(2025, 9, 14))


# ── small helpers ─────────────────────────────────────────────────────────


class TestHelpers(unittest.TestCase):
	def test_pct_change(self):
		self.assertEqual(rd.pct_change(150, 100), 50.0)
		self.assertEqual(rd.pct_change(50, 100), -50.0)
		self.assertEqual(rd.pct_change(50, -100), 150.0)
		self.assertIsNone(rd.pct_change(5, 0))
		self.assertIsNone(rd.pct_change(0, 0))
		self.assertIsNone(rd.pct_change(5, 0.001))

	def test_share_and_aov(self):
		self.assertIsNone(rd.share_pct(5, 0))
		self.assertEqual(rd.share_pct(1, 3), 33.3)
		self.assertIsNone(rd.aov(100, 0))
		self.assertEqual(rd.aov(100, 3), 33.33)


# ── summary ───────────────────────────────────────────────────────────────


class TestSummary(unittest.TestCase):
	def test_channel_split_and_b2b_share(self):
		s = summary()
		self.assertEqual(s["net_sales"], 1450.0)
		self.assertEqual((s["b2c"], s["b2b"], s["staff"], s["samples"]), (900.0, 500.0, 50.0, 0.0))
		self.assertEqual(s["b2b_share_pct"], 34.5)
		self.assertEqual(s["shipping_income"], 60.0)
		self.assertEqual(s["total_revenue"], 1510.0)
		self.assertEqual(
			(s["orders"], s["b2c_orders"], s["b2b_orders"], s["other_orders"]),
			(9, 5, 2, 2),
		)
		self.assertEqual(s["discount_total"], 20.0)

	def test_returns_are_netted_and_counted(self):
		s = summary()
		# The credit note is inside net_sales and b2c, and reported as a positive figure.
		self.assertEqual(s["b2c"], 900.0)
		self.assertEqual(s["returns"], 1)
		self.assertEqual(s["returns_value"], 100.0)
		# ...but it is not an order, and AOV is on non-return invoices only.
		self.assertEqual(s["aov"], 172.22)  # (1000 + 500 + 50 + 0) / 9
		self.assertEqual(s["b2c_aov"], 200.0)
		self.assertEqual(s["b2b_aov"], 250.0)

	def test_aov_is_null_with_no_orders(self):
		only_return = [
			d("Standard", date(2026, 10, 1), -80, orders=0, order_net=0, returns=1, returns_value=-80),
		]
		s = summary(only_return)
		self.assertIsNone(s["aov"])
		self.assertIsNone(s["b2c_aov"])
		self.assertIsNone(s["b2b_aov"])
		self.assertEqual(s["returns_value"], 80.0)

		empty = summary([], shipping_rows=[], cancelled={}, customers={})
		self.assertIsNone(empty["aov"])
		self.assertIsNone(empty["b2b_share_pct"])
		self.assertIsNone(empty["best_day"])
		self.assertIsNone(empty["worst_day"])
		self.assertEqual(empty["total_revenue"], 0.0)
		self.assertEqual((empty["cancelled"], empty["customers"], empty["new_customers"]), (0, 0, 0))

	def test_pass_through_counts_are_rounded(self):
		s = summary()
		self.assertEqual((s["cancelled"], s["cancelled_value"]), (2, 123.46))
		self.assertEqual((s["customers"], s["new_customers"]), (7, 3))

	def test_days_average_and_best_worst_day(self):
		s = summary()
		self.assertEqual(s["days"], 3)
		self.assertEqual(s["daily_average"], 483.33)
		self.assertEqual(s["best_day"], {"date": "2026-10-01", "net_sales": 900.0})
		# 2026-10-03 only has a zero-value sample order: zero days are candidates.
		self.assertEqual(s["worst_day"], {"date": "2026-10-03", "net_sales": 0.0})

	def test_quiet_day_is_the_worst_day(self):
		rows = [d("Standard", date(2026, 10, 1), 100), d("Standard", date(2026, 10, 3), 300)]
		s = summary(rows)
		self.assertEqual(s["best_day"], {"date": "2026-10-03", "net_sales": 300.0})
		self.assertEqual(s["worst_day"], {"date": "2026-10-02", "net_sales": 0.0})

	def test_today_in_progress_is_not_the_worst_day(self):
		rows = [
			d("Standard", date(2026, 10, 1), 100),
			d("Standard", date(2026, 10, 2), 300),
			d("Standard", date(2026, 10, 3), 20),
		]
		s = summary(rows, today=date(2026, 10, 3))
		self.assertEqual(s["worst_day"], {"date": "2026-10-01", "net_sales": 100.0})
		self.assertEqual(s["net_sales"], 420.0)
		# A one-day range keeps its only day.
		s = summary(rows, fd=date(2026, 10, 3), today=date(2026, 10, 3))
		self.assertEqual(s["worst_day"]["date"], "2026-10-03")

	def test_ties_go_to_the_earliest_day(self):
		rows = [d("Standard", date(2026, 10, 1), 100), d("Standard", date(2026, 10, 2), 100)]
		s = summary(rows, td=date(2026, 10, 2))
		self.assertEqual(s["best_day"]["date"], "2026-10-01")
		self.assertEqual(s["worst_day"]["date"], "2026-10-01")

	def test_previous_window(self):
		p = summary()["previous"]
		self.assertEqual((p["date_from"], p["date_to"]), ("2026-09-24", "2026-09-26"))
		self.assertEqual((p["net_sales"], p["b2c"], p["b2b"], p["orders"]), (725.0, 725.0, 0.0, 3))
		self.assertEqual(p["change_pct"], 100.0)
		self.assertEqual(p["b2c_change_pct"], 24.1)
		# 0 base: no percentage, not +inf and not 100%
		self.assertIsNone(p["b2b_change_pct"])
		self.assertEqual(p["orders_change_pct"], 200.0)
		self.assertEqual((p["aov"], p["aov_change_pct"]), (241.67, -28.7))
		self.assertEqual(p["discount_total"], 0.0)
		self.assertIsNone(p["cut_at"])

	def test_a_range_ending_today_compares_up_to_now(self):
		today = date(2026, 10, 18)
		rows = [
			d("Standard", today, 400, orders=2),
			d("Standard", date(2026, 10, 11), 300, orders=3),
			d("Standard", date(2026, 10, 11), 500, orders=4, late=1),  # after 14:05 last week
		]
		p = summary(rows, fd=today, td=today, today=today, cutoff="14:05:00")["previous"]
		self.assertEqual((p["net_sales"], p["orders"], p["cut_at"]), (300.0, 3, "14:05"))
		self.assertEqual(p["change_pct"], 33.3)
		# A range that ended before today compares whole days.
		p = summary(rows, fd=today, td=today, today=date(2026, 10, 19), cutoff="14:05:00")["previous"]
		self.assertEqual((p["net_sales"], p["cut_at"]), (800.0, None))

	def test_pct_change_null_when_previous_is_empty(self):
		rows = [d("Standard", date(2026, 10, 1), 100)]
		p = summary(rows)["previous"]
		self.assertEqual(p["net_sales"], 0.0)
		self.assertIsNone(p["change_pct"])
		self.assertIsNone(p["orders_change_pct"])


# ── snapshot ──────────────────────────────────────────────────────────────


def snapshot_rows():
	return [
		d("Standard", date(2026, 10, 18), 300, orders=2),
		d("B2B Supply", date(2026, 10, 18), 200, orders=1),
		d("Employee", date(2026, 10, 18), 40, orders=1),
		d("Standard", date(2026, 10, 11), 270, orders=3),
		d("Standard", date(2026, 10, 12), 100, orders=1),
		d("Standard", date(2026, 10, 5), 60, orders=1),
		d("Standard", date(2026, 9, 18), 100, orders=1),
		d("Standard", date(2026, 9, 19), 999, orders=9),
	]


class TestSnapshot(unittest.TestCase):
	def test_today_compares_with_same_weekday_last_week(self):
		t = rd.build_snapshot(snapshot_rows(), date(2026, 10, 18))["today"]
		self.assertEqual(
			t,
			{
				"label_from": "2026-10-18",
				"label_to": "2026-10-18",
				"net_sales": 540.0,
				"b2c": 300.0,
				"b2b": 200.0,
				"other": 40.0,
				"orders": 4,
				"b2c_orders": 2,
				"b2b_orders": 1,
				"aov": 135.0,
				"compare_net_sales": 270.0,
				"compare_orders": 3,
				"change_pct": 100.0,
				"compare_from": "2026-10-11",
				"compare_to": "2026-10-11",
				"compare_aov": 90.0,
				"orders_change_pct": 33.3,
				"aov_change_pct": 50.0,
				"compare_cut_at": None,
			},
		)

	def test_cards_ending_today_are_cut_at_the_time(self):
		rows = [
			*snapshot_rows(),
			d("Standard", date(2026, 10, 11), 900, orders=2, late=1),
			d("Standard", date(2026, 10, 10), 50, orders=1, late=1),
		]
		snap = rd.build_snapshot(rows, date(2026, 10, 18), cutoff="14:05:00")
		t = snap["today"]
		self.assertEqual(
			(t["compare_net_sales"], t["compare_orders"], t["compare_cut_at"]), (270.0, 3, "14:05")
		)
		# Yesterday is a whole day, so last week's whole day is the comparison.
		y = snap["yesterday"]
		self.assertEqual((y["compare_net_sales"], y["compare_cut_at"]), (50.0, None))
		# Last 7 days ends today: only its last comparison day (10-11) is cut.
		w = snap["last_7_days"]
		self.assertEqual((w["compare_net_sales"], w["compare_cut_at"]), (380.0, "14:05"))
		self.assertEqual(snap["month_to_date"]["compare_cut_at"], "14:05")

	def test_a_quiet_yesterday(self):
		y = rd.build_snapshot(snapshot_rows(), date(2026, 10, 18))["yesterday"]
		self.assertEqual((y["label_from"], y["compare_from"]), ("2026-10-17", "2026-10-10"))
		self.assertEqual((y["net_sales"], y["orders"]), (0.0, 0))
		self.assertIsNone(y["aov"])
		self.assertIsNone(y["change_pct"])

	def test_last_7_days(self):
		w = rd.build_snapshot(snapshot_rows(), date(2026, 10, 18))["last_7_days"]
		self.assertEqual((w["label_from"], w["label_to"]), ("2026-10-12", "2026-10-18"))
		self.assertEqual((w["net_sales"], w["orders"]), (640.0, 5))
		self.assertEqual((w["compare_from"], w["compare_to"]), ("2026-10-05", "2026-10-11"))
		self.assertEqual((w["compare_net_sales"], w["compare_orders"]), (330.0, 4))
		self.assertEqual(w["change_pct"], 93.9)

	def test_month_to_date(self):
		m = rd.build_snapshot(snapshot_rows(), date(2026, 10, 18))["month_to_date"]
		self.assertEqual((m["net_sales"], m["orders"]), (970.0, 9))
		# 2026-09-19 is past the same day-of-month and stays out.
		self.assertEqual((m["compare_net_sales"], m["compare_orders"]), (100.0, 1))
		self.assertEqual(m["change_pct"], 870.0)

	def test_month_to_date_on_the_31st(self):
		rows = [
			d("Standard", date(2026, 10, 31), 50),
			d("Standard", date(2026, 9, 30), 100),
			d("Standard", date(2026, 9, 1), 25),
			d("Standard", date(2026, 8, 31), 999),
		]
		m = rd.build_snapshot(rows, date(2026, 10, 31))["month_to_date"]
		self.assertEqual((m["compare_from"], m["compare_to"]), ("2026-09-01", "2026-09-30"))
		self.assertEqual(m["compare_net_sales"], 125.0)
		self.assertEqual(m["change_pct"], -60.0)


# ── trend and breakdowns ──────────────────────────────────────────────────


def trend_row(day, b2c=0.0, b2b=0.0, other=0.0, net_sales=0.0, orders=0):
	return {"date": day, "b2c": b2c, "b2b": b2b, "other": other, "net_sales": net_sales, "orders": orders}


class TestTrend(unittest.TestCase):
	def test_zero_filled_daily_trend(self):
		rows = [
			d("Standard", date(2026, 10, 1), 100),
			d("B2B Supply", date(2026, 10, 3), 50),
			d("Employee", date(2026, 10, 3), 10),
			d("Standard", date(2026, 10, 6), 999),
		]
		t = rd.build_trend(rows, date(2026, 10, 1), date(2026, 10, 5), "day")
		self.assertEqual([r["date"] for r in t], [f"2026-10-0{i}" for i in range(1, 6)])
		self.assertEqual(t[0], trend_row("2026-10-01", b2c=100.0, net_sales=100.0, orders=1))
		self.assertEqual(t[1], trend_row("2026-10-02"))
		self.assertEqual(t[2], trend_row("2026-10-03", b2b=50.0, other=10.0, net_sales=60.0, orders=2))
		self.assertEqual(sum(r["net_sales"] for r in t), 160.0)

	def test_wide_range_buckets_follow_the_pl_report(self):
		fd, td = date(2026, 8, 1), date(2026, 9, 29)
		payload = rd.build_dashboard(
			today=td,
			fd=fd,
			td=td,
			branch=None,
			generated_at="x",
			daily_rows=[d("Standard", date(2026, 9, 27), 70)],
			shipping_rows=[],
			cancelled={},
			dim_rows=[],
			product_rows=[],
			b2b_customer_rows=[],
			customers={},
		)
		self.assertEqual(payload["granularity"], "week")
		self.assertEqual([r["date"] for r in payload["trend"]], fr.bucket_keys(fd, td, "week"))
		week = next(r for r in payload["trend"] if r["date"] == "2026-09-21")
		self.assertEqual(week["net_sales"], 70.0)


class TestBySource(unittest.TestCase):
	def test_order_share_and_empty_rows(self):
		day = date(2026, 10, 1)
		rows = [
			d("Sample - Courier", day, 0, orders=1, source="samples"),
			d("B2B Supply", day, 50, source="b2b"),
			d("Standard", day, 100, source="pos"),
			d("Standard", day, 50, source="woo"),
			d("Standard", day, 0, orders=0, source="talabat"),
			d("Standard", date(2026, 10, 9), 500, source="partner"),  # out of range
		]
		out = rd.build_by_source(rows, day, day)
		self.assertEqual([r["source"] for r in out], ["pos", "woo", "b2b", "samples"])
		self.assertEqual(out[0], {"source": "pos", "net_sales": 100.0, "orders": 1, "share_pct": 50.0})
		self.assertEqual(out[-1]["share_pct"], 0.0)

	def test_compare_adds_change_and_keeps_lost_sources(self):
		day, prev = date(2026, 10, 18), date(2026, 10, 11)
		rows = [
			d("Standard", day, 150, source="pos"),
			d("Standard", prev, 100, source="pos"),
			d("Standard", prev, 800, source="talabat"),
			d("Standard", prev, 0, orders=1, source="woo"),
		]
		compare = rd.totals_by(rows, rd._source_of, prev, prev)
		out = rd.build_by_source(rows, day, day, compare)
		self.assertEqual(
			out,
			[
				{
					"source": "pos",
					"net_sales": 150.0,
					"orders": 1,
					"share_pct": 100.0,
					"compare_net_sales": 100.0,
					"change_pct": 50.0,
				},
				# Talabat sold nothing today: kept at 0 so the drop is visible.
				{
					"source": "talabat",
					"net_sales": 0.0,
					"orders": 0,
					"share_pct": 0.0,
					"compare_net_sales": 800.0,
					"change_pct": -100.0,
				},
			],
		)


class TestBreakdowns(unittest.TestCase):
	def test_by_payment(self):
		dims = [
			{"method": "Cash", "orders": 3, "order_net": 300, "net": 300},
			{"method": "", "orders": 1, "order_net": 50, "net": 50},
			{"method": None, "orders": 1, "order_net": 25, "net": 25},
			{"method": "Instapay", "orders": 2, "order_net": 500, "net": 500},
			{"method": "Card", "orders": 0, "order_net": 0, "net": -40},  # credit notes only
		]
		out = rd.build_by_payment(dims)
		self.assertEqual(
			out,
			[
				{"method": "Instapay", "net_sales": 500.0, "orders": 2, "share_pct": 57.1},
				{"method": "Cash", "net_sales": 300.0, "orders": 3, "share_pct": 34.3},
				{"method": "Unspecified", "net_sales": 75.0, "orders": 2, "share_pct": 8.6},
			],
		)

	def test_by_branch(self):
		dims = [
			{"branch": "Dokki", "purpose": "Standard", "net": 300, "orders": 3},
			{"branch": "Dokki", "purpose": "B2B Supply", "net": 200, "orders": 1},
			{"branch": "Nasr City", "purpose": "Standard", "net": 600, "orders": 4},
			{"branch": None, "purpose": "Employee", "net": 10, "orders": 1},
			{"branch": "Dokki", "purpose": "Standard", "net": -50, "orders": 0},
		]
		out = rd.build_by_branch(dims)
		self.assertEqual([r["branch"] for r in out], ["Nasr City", "Dokki", fr.NO_BRANCH_LABEL])
		self.assertEqual(
			out[1],
			{
				"branch": "Dokki",
				"net_sales": 450.0,
				"b2c": 250.0,
				"b2b": 200.0,
				"orders": 4,
				"share_pct": 42.5,
			},
		)
		self.assertEqual(out[0]["share_pct"], 56.6)
		self.assertEqual((out[2]["b2c"], out[2]["b2b"], out[2]["share_pct"]), (0.0, 0.0, 0.9))

	def test_by_branch_compare(self):
		dims = [
			{"branch": "Dokki", "purpose": "Standard", "net": 300, "orders": 3},
			{"branch": "Dokki", "purpose": "B2B Supply", "net": 700, "orders": 1},
		]
		prev = date(2026, 10, 11)
		compare = rd.totals_by(
			[
				d("Standard", prev, 600, branch="Dokki"),
				d("Standard", prev, 250, branch="Zamalek"),
				d("Employee", prev, 40),
			],
			rd._branch_of,
			prev,
			prev,
		)
		out = {r["branch"]: r for r in rd.build_by_branch(dims, compare)}
		keys = ("compare_net_sales", "change_pct", "compare_b2c", "b2c_change_pct")
		self.assertEqual(
			{k: out["Dokki"][k] for k in keys},
			{"compare_net_sales": 600.0, "change_pct": 66.7, "compare_b2c": 600.0, "b2c_change_pct": -50.0},
		)
		# Zamalek sold only last week; staff sales count in net but not in B2C.
		self.assertEqual((out["Zamalek"]["net_sales"], out["Zamalek"]["b2c_change_pct"]), (0.0, -100.0))
		nb = out[fr.NO_BRANCH_LABEL]
		self.assertEqual((nb["compare_net_sales"], nb["compare_b2c"]), (40.0, 0.0))

	def test_by_hour_is_always_24_rows(self):
		self.assertEqual([r["hour"] for r in rd.build_by_hour([])], list(range(24)))
		dims = [
			{"hour": 9, "orders": 2, "order_net": 100},
			{"hour": 9, "orders": 1, "order_net": 50},
			{"hour": None, "orders": 1, "order_net": 999},
			{"hour": "14", "orders": 1, "order_net": 20},
			{"hour": 23, "orders": 1, "order_net": 10},
		]
		out = rd.build_by_hour(dims)
		self.assertEqual(len(out), 24)
		self.assertEqual(out[9], {"hour": 9, "orders": 3, "net_sales": 150.0})
		self.assertEqual(out[14]["orders"], 1)
		self.assertEqual(out[23]["net_sales"], 10.0)
		self.assertEqual(sum(r["orders"] for r in out), 5)

	def test_fulfilment(self):
		dims = [
			{"is_pickup": 0, "orders": 3},
			{"is_pickup": 1, "orders": 2},
			{"is_pickup": None, "orders": 1},
		]
		self.assertEqual(rd.build_fulfilment(dims), {"delivery_orders": 4, "pickup_orders": 2})

	def test_top_products(self):
		def product(i):
			return {"item_code": f"I{i:02d}", "item_name": None, "qty": i, "net_sales": i * 10}

		rows = [product(i) for i in range(1, 13)]
		rows.append({"item_code": "RET", "item_name": "Returned", "qty": -1, "net_sales": -5})
		out = rd.build_top_products(rows)
		self.assertEqual(len(out), 10)
		self.assertEqual(out[0], {"item_code": "I12", "item_name": "I12", "qty": 12.0, "net_sales": 120.0})
		self.assertNotIn("RET", [r["item_code"] for r in out])

	def test_top_b2b_customers(self):
		rows = [
			{"customer": "C1", "customer_name": "Cafe One", "net_sales": 100, "orders": 1},
			{"customer": "C2", "customer_name": None, "net_sales": 300.004, "orders": 3},
		]
		out = rd.build_top_b2b_customers(rows)
		self.assertEqual(out[0], {"customer": "C2", "customer_name": "C2", "net_sales": 300.0, "orders": 3})
		self.assertEqual(out[1]["customer_name"], "Cafe One")


class TestAttention(unittest.TestCase):
	def _summary(self, **kw):
		base = {
			"net_sales": 5000.0,
			"cancelled": 0,
			"cancelled_value": 0.0,
			"returns": 0,
			"returns_value": 0.0,
			"discount_total": 0.0,
			"previous": {"net_sales": 5000.0, "discount_total": 0.0},
		}
		base.update(kw)
		return base

	def test_nothing_off_is_an_empty_list(self):
		self.assertEqual(rd.build_attention(self._summary(), [], []), [])

	def test_kinds_in_order(self):
		s = self._summary(
			cancelled=2, cancelled_value=310.456, returns=1, returns_value=80.0, discount_total=600.0
		)
		by_source = [
			{"source": "talabat", "net_sales": 200.0, "compare_net_sales": 1000.0, "change_pct": -80.0},
			{"source": "woo", "net_sales": 700.0, "compare_net_sales": 1000.0, "change_pct": -30.0},
			# b2b is lumpy: never a drop
			{"source": "b2b", "net_sales": 0.0, "compare_net_sales": 5000.0, "change_pct": -100.0},
			# below the base: not worth a flag
			{"source": "partner", "net_sales": 0.0, "compare_net_sales": 400.0, "change_pct": -100.0},
			# a small dip
			{"source": "pos", "net_sales": 900.0, "compare_net_sales": 1000.0, "change_pct": -10.0},
		]
		by_branch = [
			{"branch": "Dokki", "b2c": 400.0, "compare_b2c": 1000.0, "b2c_change_pct": -60.0},
			{"branch": "Nasr City", "b2c": 1000.0, "compare_b2c": 1000.0, "b2c_change_pct": 0.0},
		]
		out = rd.build_attention(s, by_source, by_branch)
		self.assertEqual(
			[(a["kind"], a.get("name"), a["severity"]) for a in out],
			[
				("cancelled", None, "medium"),
				("returns", None, "medium"),
				("branch_drop", "Dokki", "high"),
				("source_drop", "talabat", "high"),
				("source_drop", "woo", "medium"),
				("discount_high", None, "medium"),
			],
		)
		self.assertEqual(out[0], {"kind": "cancelled", "severity": "medium", "count": 2, "value": 310.46})
		self.assertEqual(
			out[2],
			{
				"kind": "branch_drop",
				"severity": "high",
				"name": "Dokki",
				"net_sales": 400.0,
				"compare_net_sales": 1000.0,
				"change_pct": -60.0,
			},
		)
		self.assertEqual(
			out[-1],
			{
				"kind": "discount_high",
				"severity": "medium",
				"value": 600.0,
				"discount_pct": 10.7,
				"compare_discount_pct": 0.0,
			},
		)

	def test_discount_is_flagged_only_when_unusual(self):
		# 10.7% vs 9.1% last week: not 1.5x.
		s = self._summary(discount_total=600.0, previous={"net_sales": 5000.0, "discount_total": 500.0})
		self.assertEqual(rd.build_attention(s, [], []), [])
		# Below the floor.
		s = self._summary(net_sales=1000.0, discount_total=200.0)
		self.assertEqual(rd.build_attention(s, [], []), [])
		# 1.5x but under 2 points: 3.0% vs 1.96%.
		s = self._summary(
			net_sales=9700.0, discount_total=300.0, previous={"net_sales": 9800.0, "discount_total": 196.0}
		)
		self.assertEqual(rd.build_attention(s, [], []), [])


class TestDashboardShape(unittest.TestCase):
	def test_top_level_keys_in_contract_order(self):
		today = date(2026, 10, 18)
		payload = rd.build_dashboard(
			today=today,
			fd=today,
			td=today,
			branch="Dokki",
			generated_at="2026-10-18 14:05:00",
			daily_rows=snapshot_rows(),
			shipping_rows=[],
			cancelled={},
			dim_rows=[],
			product_rows=[],
			b2b_customer_rows=[],
			customers={},
		)
		self.assertEqual(
			list(payload),
			[
				"generated_at",
				"today",
				"filters",
				"snapshot",
				"summary",
				"attention",
				"granularity",
				"trend",
				"by_source",
				"by_payment",
				"by_branch",
				"by_hour",
				"fulfilment",
				"top_products",
				"top_b2b_customers",
			],
		)
		self.assertEqual(
			payload["filters"],
			{"date_from": "2026-10-18", "date_to": "2026-10-18", "branch": "Dokki"},
		)
		self.assertEqual(payload["today"], "2026-10-18")
		# The selected range (today) agrees with the snapshot's today card.
		self.assertEqual(payload["summary"]["net_sales"], payload["snapshot"]["today"]["net_sales"])
		self.assertEqual(payload["granularity"], "day")
		self.assertEqual(len(payload["trend"]), 1)
		self.assertEqual(payload["attention"], [])
		self.assertEqual(payload["by_source"][0]["compare_net_sales"], 270.0)


# ── whitelisted entry ─────────────────────────────────────────────────────


def _fake_frappe(roles=("JARZ Manager",)):
	"""A MagicMock ``frappe`` whose ``throw`` raises the real exception class."""
	from unittest.mock import MagicMock

	import frappe as real

	fake = MagicMock()
	fake.ValidationError = real.ValidationError
	fake.DoesNotExistError = real.DoesNotExistError
	fake.PermissionError = real.PermissionError
	fake.session.user = "someone@example.com"
	fake.get_roles.return_value = list(roles)

	def _throw(msg, exc=real.ValidationError, *a, **kw):
		raise exc(msg)

	fake.throw.side_effect = _throw
	fake.db.sql.return_value = []
	return fake


class TestEndpoint(unittest.TestCase):
	def _patch(self, fake, **extra):
		from unittest.mock import MagicMock, patch

		return patch.multiple(
			rd,
			frappe=fake,
			_=lambda s, *a, **k: s,
			nowdate=MagicMock(return_value="2026-10-18"),
			**extra,
		)

	def test_non_manager_is_refused(self):
		import frappe as real

		fake = _fake_frappe(roles=("Sales User", "Line Manager"))
		with self._patch(fake), self.assertRaises(real.PermissionError):
			rd.get_revenue_dashboard()
		fake.db.sql.assert_not_called()

	def test_range_over_400_days_is_refused(self):
		import frappe as real

		fake = _fake_frappe()
		with self._patch(fake), self.assertRaises(real.ValidationError):
			rd.get_revenue_dashboard(date_from="2025-09-13", date_to="2026-10-18")  # 401 days
		fake.db.sql.assert_not_called()

	def _fetchers(self):
		from unittest.mock import MagicMock

		return {
			"now_datetime": MagicMock(return_value=datetime(2026, 10, 18, 14, 5, 0)),
			"validate_branch": MagicMock(side_effect=lambda b: b or None),
			"_default_company": MagicMock(return_value="J"),
			"_accounts": MagicMock(return_value=(SHIP, FREIGHT)),
			"_daily_rows": MagicMock(return_value=[]),
			"_shipping_rows": MagicMock(return_value=[]),
			"_cancelled": MagicMock(return_value={}),
			"_dimension_rows": MagicMock(return_value=[]),
			"_product_rows": MagicMock(return_value=[]),
			"_b2b_customer_rows": MagicMock(return_value=[]),
			"_customer_counts": MagicMock(return_value={}),
		}

	def test_default_range_is_today_and_one_daily_query(self):
		m = self._fetchers()
		with self._patch(_fake_frappe(), **m):
			out = rd.get_revenue_dashboard()
		self.assertEqual(
			out["filters"],
			{"date_from": "2026-10-18", "date_to": "2026-10-18", "branch": None},
		)
		self.assertEqual(out["generated_at"], "2026-10-18 14:05:00")
		# Today compares with the same weekday last week, up to the same time.
		self.assertEqual(out["summary"]["previous"]["date_from"], "2026-10-11")
		self.assertEqual(out["summary"]["previous"]["cut_at"], "14:05")
		# The snapshot span (2026-09-01..) already holds today and last week.
		m["_daily_rows"].assert_called_once_with(
			date(2026, 9, 1), date(2026, 10, 18), "J", None, cutoff="14:05:00"
		)
		m["_shipping_rows"].assert_called_once_with(date(2026, 10, 18), date(2026, 10, 18), "J", SHIP, None)
		self.assertEqual(len(out["by_hour"]), 24)
		self.assertEqual(out["fulfilment"], {"delivery_orders": 0, "pickup_orders": 0})

	def test_an_old_range_fetches_its_own_window(self):
		m = self._fetchers()
		with self._patch(_fake_frappe(), **m):
			rd.get_revenue_dashboard(date_from="2025-01-01", date_to="2025-01-31", branch="Dokki")
		calls = [c.args for c in m["_daily_rows"].call_args_list]
		self.assertEqual(
			calls,
			[
				(date(2024, 12, 1), date(2025, 1, 31), "J", "Dokki"),
				(date(2026, 9, 1), date(2026, 10, 18), "J", "Dokki"),
			],
		)
		self.assertEqual([c.kwargs for c in m["_daily_rows"].call_args_list], [{"cutoff": "14:05:00"}] * 2)
		m["validate_branch"].assert_called_once_with("Dokki")

	def test_daily_rows_feed_the_payload(self):
		m = self._fetchers()
		m["_daily_rows"].return_value = [
			{
				"purpose": "B2B Supply",
				"sale_source": "b2b",
				"branch_label": "Dokki",
				"posting_date": date(2026, 10, 18),
				"late_flag": 0,
				"net": 400,
				"orders": 2,
				"order_net": 400,
				"returns": 0,
				"returns_value": 0,
				"discount": 0,
			}
		]
		with self._patch(_fake_frappe(), **m):
			out = rd.get_revenue_dashboard()
		self.assertEqual(out["summary"]["b2b"], 400.0)
		self.assertEqual(out["summary"]["b2b_share_pct"], 100.0)
		self.assertEqual(out["snapshot"]["today"]["b2b_orders"], 2)
		self.assertEqual(
			out["by_source"],
			[
				{
					"source": "b2b",
					"net_sales": 400.0,
					"orders": 2,
					"share_pct": 100.0,
					"compare_net_sales": 0.0,
					"change_pct": None,
				}
			],
		)
		self.assertEqual(out["attention"], [])


class TestQueries(unittest.TestCase):
	"""Every value is a bound parameter; only constant fragments are spliced in."""

	def _capture(self):
		from unittest.mock import MagicMock, patch

		fake = MagicMock()
		seen = []

		def _sql(q, params=None, as_dict=False, **kw):
			seen.append((" ".join(q.split()), params))
			return []

		fake.db.sql.side_effect = _sql
		return patch.object(rd, "frappe", fake), seen

	def test_fetchers_bind_company_and_branch(self):
		ctx, seen = self._capture()
		fd, td = date(2026, 10, 1), date(2026, 10, 18)
		with ctx:
			self.assertEqual(rd._daily_rows(fd, td, "J", "Dokki"), [])
			self.assertEqual(rd._cancelled(fd, td, "J", "Dokki"), {})
			self.assertEqual(rd._dimension_rows(fd, td, "J", "Dokki"), [])
			self.assertEqual(rd._product_rows(fd, td, "J", "Dokki"), [])
			self.assertEqual(rd._b2b_customer_rows(fd, td, "J", "Dokki"), [])
			self.assertEqual(rd._customer_counts(fd, td, "J", "Dokki"), {})
		self.assertEqual(len(seen), 6)
		for q, params in seen:
			self.assertNotIn("Dokki", q)
			self.assertIn("pos_profile = %(branch)s", q)
			self.assertIn("si.company = %(company)s", q)
			self.assertEqual((params["branch"], params["company"]), ("Dokki", "J"))
			self.assertIn("si.docstatus", q)

		daily, cancelled, dims, products, b2b, customers = (q for q, _p in seen)
		self.assertIn("THEN si.base_discount_amount + IFNULL((", daily)
		self.assertIn("SUM(GREATEST(sii.discount_amount, 0) * sii.qty)", daily)
		self.assertIn("WHERE sii.parent = si.name", daily)
		# The cut-off is a bound time, so last week's day can be cut at "now".
		self.assertIn("CASE WHEN si.posting_time > %(cutoff)s THEN 1 ELSE 0 END AS late_flag", daily)
		self.assertEqual(seen[0][1]["cutoff"], "23:59:59")
		self.assertIn("si.docstatus = 2", cancelled)
		self.assertIn("!= 'Amended'", cancelled)
		# The hour comes from SQL: a Frappe Time value arrives as a timedelta.
		self.assertIn("HOUR(si.posting_time)", dims)
		self.assertIn("LIMIT 10", products)
		self.assertIn("SUM(sii.base_net_amount)", products)
		self.assertIn("= %(b2b_purpose)s", b2b)
		self.assertEqual(seen[4][1]["b2b_purpose"], "B2B Supply")
		self.assertIn("MIN(x.posting_date)", customers)
		self.assertIn("x.customer IN (", customers)

	def test_no_branch_no_company_means_no_filter(self):
		ctx, seen = self._capture()
		with ctx:
			rd._daily_rows(date(2026, 10, 1), date(2026, 10, 1), None, None)
		self.assertNotIn("%(branch)s", seen[0][0])
		self.assertNotIn("%(company)s", seen[0][0])

	def test_group_by_aliases_do_not_shadow_columns(self):
		# MariaDB resolves a GROUP BY name against the table first; Sales Invoice
		# has ``source`` and may have ``branch``, so those names must not be used.
		ctx, seen = self._capture()
		with ctx:
			rd._daily_rows(date(2026, 10, 1), date(2026, 10, 1), None, None)
			rd._dimension_rows(date(2026, 10, 1), date(2026, 10, 1), None, None)
		for q, _p in seen:
			group_by = q.split("GROUP BY", 1)[1]
			self.assertNotRegex(group_by, r"(^|[\s,])(source|branch|hour)([\s,]|$)")


if __name__ == "__main__":
	unittest.main()

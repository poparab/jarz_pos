"""Ops reports (daily close, cash over/short, fulfilment SLA): pure builders.

The ``build_*`` functions take grouped rows and return the payload, so no site
is needed. The SQL that feeds them is exercised on a real stack only.
"""

import unittest
from datetime import date, datetime, timedelta

from jarz_pos.api import ops_reports as ops

FILTERS = {"date_from": "2026-10-01", "date_to": "2026-10-02", "branch": None, "scoped_to": None}


class TestStats(unittest.TestCase):
	def test_median_odd_even_empty(self):
		self.assertEqual(ops.median([3, 1, 2]), 2)
		self.assertEqual(ops.median([1, 2, 3, 4]), 2.5)
		self.assertIsNone(ops.median([]))

	def test_p90_nearest_rank(self):
		self.assertEqual(ops.percentile(list(range(1, 11)), 90), 9)
		self.assertEqual(ops.percentile([5.0], 90), 5.0)
		self.assertIsNone(ops.percentile([], 90))


class TestSlotWindow(unittest.TestCase):
	def test_full_slot(self):
		start, end = ops.slot_window(date(2026, 10, 1), timedelta(hours=14), 7200)
		self.assertEqual(start, datetime(2026, 10, 1, 14, 0))
		self.assertEqual(end, datetime(2026, 10, 1, 16, 0))

	def test_string_time_and_no_duration(self):
		start, end = ops.slot_window("2026-10-01", "09:30:00", None)
		self.assertEqual(start, datetime(2026, 10, 1, 9, 30))
		self.assertIsNone(end)

	def test_date_only_is_whole_day(self):
		start, end = ops.slot_window("2026-10-01", None, None)
		self.assertEqual(start, datetime(2026, 10, 1))
		self.assertEqual(end, datetime(2026, 10, 2))

	def test_no_slot(self):
		self.assertEqual(ops.slot_window(None, "10:00", 3600), (None, None))


class TestRemark(unittest.TestCase):
	def test_close_and_open(self):
		self.assertEqual(
			ops.parse_discrepancy_remark(
				"Shift cash discrepancy for POS-OPE-2026-00012 → POS-CLO-2026-00009. Expected 10, confirmed 5, difference -5"
			),
			("POS-OPE-2026-00012", "POS-CLO-2026-00009"),
		)
		self.assertEqual(
			ops.parse_discrepancy_remark("Shift cash discrepancy for POS-OPE-1 → Opening. Expected 1"),
			("POS-OPE-1", "Opening"),
		)
		self.assertEqual(ops.parse_discrepancy_remark("manual fix"), (None, None))


class TestDailyClose(unittest.TestCase):
	def test_totals_channels_payments(self):
		inv = [
			{
				"branch": "Dokki",
				"channel": "pos",
				"hour": 13,
				"orders": 2,
				"return_count": 0,
				"net_sales": 200.0,
				"gross_net": 200.0,
				"returns_net": 0.0,
				"grand_total": 230.0,
				"outstanding": 0,
			},
			{
				"branch": "Dokki",
				"channel": "woo",
				"hour": 20,
				"orders": 1,
				"return_count": 1,
				"net_sales": 50.0,
				"gross_net": 100.0,
				"returns_net": -50.0,
				"grand_total": 60.0,
				"outstanding": 0,
			},
			{
				"branch": "Nasr",
				"channel": "samples",
				"hour": 9,
				"orders": 1,
				"return_count": 0,
				"net_sales": 0.0,
				"gross_net": 0.0,
				"returns_net": 0.0,
				"grand_total": 0.0,
				"outstanding": 0,
			},
		]
		out = ops.build_daily_close(
			FILTERS,
			inv,
			[{"branch": "Dokki", "amount": 30.0}],
			[{"branch": "Nasr", "cancelled": 2}],
			[
				{"branch": "Dokki", "mode": "Cash", "amount": 200.0},
				{"branch": "Dokki", "mode": "InstaPay", "amount": 60.0},
			],
		)
		t = out["totals"]
		self.assertEqual(t["orders"], 4)
		self.assertEqual(t["net_sales"], 250.0)
		self.assertEqual(t["returns"], 50.0)
		self.assertEqual(t["cancelled"], 2)
		self.assertEqual(t["shipping_income"], 30.0)
		self.assertEqual(t["avg_order_value"], 75.0)
		chans = [c["channel"] for c in out["channels"]]
		self.assertEqual(chans[:5], ["pos", "woo", "talabat", "b2b", "staff"])
		self.assertIn("samples", chans)  # extra channel carries an order
		self.assertNotIn("partner", chans)
		self.assertEqual(len(out["by_hour"]), 24)
		self.assertEqual(out["by_hour"][20]["net_sales"], 50.0)
		self.assertEqual(out["payments"][0], {"mode": "Cash", "amount": 200.0})
		dokki = next(b for b in out["branches"] if b["branch"] == "Dokki")
		self.assertEqual(dokki["shipping_income"], 30.0)
		self.assertEqual(len(dokki["payments"]), 2)


class TestCashOverShort(unittest.TestCase):
	def test_over_short_split(self):
		shifts = [
			{
				"closing": "C2",
				"opening": "O2",
				"branch": "Dokki",
				"user": "a@x",
				"full_name": "A",
				"opened": datetime(2026, 10, 2, 9),
				"closed": datetime(2026, 10, 2, 23),
				"expected": 1000,
				"counted": 950,
				"difference": -50,
			},
			{
				"closing": "C1",
				"opening": "O1",
				"branch": "Dokki",
				"user": "b@x",
				"full_name": "B",
				"opened": datetime(2026, 10, 1, 9),
				"closed": datetime(2026, 10, 1, 23),
				"expected": 500,
				"counted": 520,
				"difference": 20,
			},
			{
				"closing": "C0",
				"opening": "O0",
				"branch": "Nasr",
				"user": "b@x",
				"full_name": "B",
				"opened": datetime(2026, 10, 1, 9),
				"closed": datetime(2026, 10, 1, 22),
				"expected": 100,
				"counted": 100,
				"difference": 0,
			},
		]
		ledger = [
			{
				"voucher_no": "JE1",
				"posting_date": date(2026, 10, 2),
				"credit": 0,
				"debit": 50,
				"user_remark": "Shift cash discrepancy for O2 → C2. Expected 1000",
			},
			{
				"voucher_no": "JE2",
				"posting_date": date(2026, 10, 2),
				"credit": 50,
				"debit": 0,
				"user_remark": "Shift cash discrepancy for O2 → Opening. Expected 0",
			},
			{
				"voucher_no": "JE3",
				"posting_date": date(2026, 9, 30),
				"credit": 5,
				"debit": 0,
				"user_remark": "Shift cash discrepancy for O1 → Opening.",
			},
		]
		out = ops.build_cash_over_short(
			FILTERS, date(2026, 10, 1), shifts, ledger, {"O2": "Dokki", "O1": "Dokki"}, None
		)
		t = out["totals"]
		self.assertEqual(
			(t["shifts"], t["over"], t["short"], t["net"], t["shifts_with_diff"]), (3, 20.0, 50.0, -30.0, 2)
		)
		b = {r["user"]: r for r in out["cashiers"]}
		self.assertEqual(b["b@x"]["shifts"], 2)
		self.assertEqual(out["shifts"][0]["name"], "C2")
		self.assertEqual(out["shifts"][0]["opening_difference"], 50.0)
		self.assertEqual(out["shifts"][1]["opening_difference"], 5.0)  # mapped from the day before
		self.assertEqual(out["ledger"]["net"], 0.0)  # JE3 is before the range
		self.assertEqual(out["ledger"]["at_open_net"], 50.0)

	def test_scope_hides_other_branch_ledger(self):
		ledger = [
			{
				"voucher_no": "JE1",
				"posting_date": date(2026, 10, 1),
				"credit": 0,
				"debit": 10,
				"user_remark": "Shift cash discrepancy for O9 → C9.",
			}
		]
		out = ops.build_cash_over_short(FILTERS, date(2026, 10, 1), [], ledger, {"O9": "Nasr"}, ["Dokki"])
		self.assertEqual(out["ledger"]["entries"], 0)


class TestFulfilmentSla(unittest.TestCase):
	def _row(self, name, **kw):
		base = {
			"name": name,
			"customer": "C",
			"customer_name": "Cust",
			"docstatus": 1,
			"branch": "Dokki",
			"state": "Delivered",
			"purpose": "Standard",
			"is_pickup": 0,
			"delivery_date": None,
			"time_from": None,
			"duration": None,
			"slot_label": None,
			"delivered_at": None,
			"cancellation_type": None,
			"cancellation_reason": None,
			"creation": datetime(2026, 10, 1, 12, 0),
		}
		base.update(kw)
		return base

	def test_rules(self):
		rows = [
			# no slot, delivered after 60 min -> on time
			self._row("A", delivered_at=datetime(2026, 10, 1, 13, 0)),
			# no slot, delivered after 120 min -> 30 min late
			self._row("B", delivered_at=datetime(2026, 10, 1, 14, 0)),
			# slot 18:00-20:00, delivered 20:15 -> 15 min late, clock starts at 18:00
			self._row(
				"C",
				delivery_date=date(2026, 10, 1),
				time_from=timedelta(hours=18),
				duration=7200,
				delivered_at=datetime(2026, 10, 1, 20, 15),
			),
			# cancelled with notes
			self._row(
				"D",
				docstatus=2,
				state="Cancelled",
				cancellation_reason="Customer changed mind\nNotes: called twice",
			),
			# pickup: counted, not timed
			self._row("E", is_pickup=1, delivered_at=datetime(2026, 10, 1, 18, 0)),
			# still out for delivery, past deadline -> open late
			self._row("F", state="Out for Delivery", creation=datetime(2026, 10, 2, 9, 0)),
		]
		out = ops.build_fulfilment_sla(
			FILTERS,
			date(2026, 10, 1),
			date(2026, 10, 2),
			rows,
			{"A": datetime(2026, 10, 1, 12, 30)},
			{},
			{"B"},
			90,
			datetime(2026, 10, 2, 12, 0),
		)
		t = out["totals"]
		self.assertEqual(t["orders"], 6)
		self.assertEqual(t["cancelled"], 1)
		self.assertEqual(t["returned"], 1)
		self.assertEqual(t["late_orders"], 3)
		self.assertEqual(t["open_late_orders"], 1)
		self.assertEqual(t["median_to_dispatch_min"], 30.0)
		self.assertEqual(t["median_to_deliver_min"], 120.0)  # 60, 120, 135
		late = out["late"]
		self.assertEqual([r["invoice"] for r in late], ["F", "B", "C"])
		self.assertEqual(late[1]["minutes"], 30.0)
		self.assertEqual(late[2]["minutes"], 15.0)
		self.assertEqual(late[2]["slot"], "2026-10-01 18:00-20:00")
		self.assertIsNone(late[0]["delivered"])
		self.assertEqual(out["cancellations"], [{"reason": "Customer changed mind", "count": 1}])
		self.assertEqual([d["date"] for d in out["by_day"]], ["2026-10-01", "2026-10-02"])


if __name__ == "__main__":
	unittest.main()

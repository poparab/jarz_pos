"""B2B home-screen account rows (pure helpers, no site needed)."""

from __future__ import annotations

import unittest
from datetime import date

from jarz_pos.services import b2b_active_accounts as active

TODAY = date(2026, 10, 18)


def _card(**kw):
	base = {"doctype": "Lead", "name": "CRM-LEAD-1", "title": "Orbt", "customer": "Orbt", "label_alert": 0}
	base.update(kw)
	return base


class TestPeriod(unittest.TestCase):
	def test_windows_do_not_overlap(self):
		start, prev = active.period_bounds(TODAY, 30)
		self.assertEqual(start, date(2026, 9, 19))
		self.assertEqual(prev, date(2026, 8, 20))
		self.assertEqual((start - prev).days, 30)

	def test_clamp(self):
		self.assertEqual(active.clamp_period_days("7"), 7)
		self.assertEqual(active.clamp_period_days(0), 1)
		self.assertEqual(active.clamp_period_days(9999), active.MAX_PERIOD_DAYS)
		self.assertEqual(active.clamp_period_days("x"), active.DEFAULT_PERIOD_DAYS)


class TestBuildRow(unittest.TestCase):
	def test_figures(self):
		stats = {
			"orders": 4,
			"billed": 4000,
			"period_orders": 2,
			"period_billed": 3000,
			"prev_period_billed": 1500,
			"first_order_date": date(2026, 6, 1),
			"last_order_date": date(2026, 10, 15),
			"outstanding": 500,
			"oldest_unpaid_date": date(2026, 10, 1),
		}
		forecast = {"custom_predicted_next_order": date(2026, 10, 20), "custom_avg_order_cycle_days": 6.5}
		row = active.build_row(_card(), stats, forecast, TODAY)
		self.assertEqual(row["avg_order_value"], 1000.0)
		self.assertEqual(row["period_change_pct"], 100.0)
		self.assertEqual(row["days_since_last_order"], 3)
		self.assertEqual(row["reorder_status"], "due_soon")
		self.assertEqual(row["oldest_unpaid_days"], 17)
		self.assertEqual(row["alerts"], [])

	def test_alerts(self):
		stats = {"orders": 1, "billed": 100, "outstanding": 100, "oldest_unpaid_date": "2026-08-01"}
		forecast = {"custom_predicted_next_order": "2026-10-10"}
		row = active.build_row(_card(label_alert=2), stats, forecast, TODAY)
		self.assertEqual(row["reorder_status"], "overdue")
		self.assertEqual(row["alerts"], ["reorder_overdue", "old_unpaid", "labels"])

	def test_paid_up_account_has_no_unpaid_age(self):
		stats = {"orders": 1, "billed": 100, "outstanding": 0, "oldest_unpaid_date": "2026-08-01"}
		row = active.build_row(_card(), stats, None, TODAY)
		self.assertIsNone(row["oldest_unpaid_days"])
		self.assertIsNone(row["reorder_status"])

	def test_account_without_customer(self):
		row = active.build_row(_card(customer=None), None, None, TODAY)
		self.assertEqual(row["orders"], 0)
		self.assertEqual(row["avg_order_value"], 0.0)
		self.assertIsNone(row["period_change_pct"])
		self.assertEqual(row["alerts"], ["no_customer"])


class TestSummaryAndSort(unittest.TestCase):
	def test_summary(self):
		rows = [
			{"period_orders": 2, "period_billed": 300, "prev_period_billed": 100, "outstanding": 50,
			 "reorder_status": "overdue", "alerts": ["reorder_overdue"]},
			{"period_orders": 1, "period_billed": 100, "prev_period_billed": 100, "outstanding": 0,
			 "reorder_status": "ok", "alerts": []},
		]
		s = active.build_summary(rows)
		self.assertEqual(s["accounts"], 2)
		self.assertEqual(s["period_orders"], 3)
		self.assertEqual(s["period_billed"], 400)
		self.assertEqual(s["period_change_pct"], 100.0)
		self.assertEqual(s["reorder_due"], 1)
		self.assertEqual(s["needs_attention"], 1)

	def test_reorder_due_counts_due_soon_like_the_app_filter(self):
		rows = [{"reorder_status": "overdue"}, {"reorder_status": "due_soon"}, {"reorder_status": "ok"}, {}]
		self.assertEqual(active.build_summary(rows)["reorder_due"], 2)

	def test_sort_recent_buyers_first(self):
		rows = [
			{"title": "b", "period_billed": 0, "billed": 900},
			{"title": "a", "period_billed": 50, "billed": 10},
			{"title": "c", "period_billed": 0, "billed": 900},
		]
		self.assertEqual([r["title"] for r in active.sort_rows(rows)], ["a", "b", "c"])


class TestDedupeCards(unittest.TestCase):
	def test_lead_and_opportunity_on_one_customer_keep_opportunity(self):
		cards = [
			{"doctype": "Lead", "name": "L-1", "customer": "C-1"},
			{"doctype": "Lead", "name": "L-2", "customer": None},
			{"doctype": "Opportunity", "name": "O-1", "customer": "C-1"},
			{"doctype": "Lead", "name": "L-3", "customer": None},
		]
		out = active.dedupe_cards(cards)
		self.assertEqual([c["name"] for c in out], ["O-1", "L-2", "L-3"])

	def test_two_leads_on_one_customer_keep_first(self):
		cards = [
			{"doctype": "Lead", "name": "L-1", "customer": "C-1"},
			{"doctype": "Lead", "name": "L-2", "customer": "C-1"},
		]
		self.assertEqual([c["name"] for c in active.dedupe_cards(cards)], ["L-1"])

	def test_summary_counts_a_shared_customer_once(self):
		cards = active.dedupe_cards(
			[
				{"doctype": "Lead", "name": "L-1", "customer": "C-1"},
				{"doctype": "Opportunity", "name": "O-1", "customer": "C-1"},
			]
		)
		stats = {"period_billed": 500, "outstanding": 200}
		rows = [active.build_row(c, stats, None, date(2026, 10, 18)) for c in cards]
		summary = active.build_summary(rows)
		self.assertEqual(summary["accounts"], 1)
		self.assertEqual(summary["period_billed"], 500)
		self.assertEqual(summary["outstanding"], 200)


if __name__ == "__main__":
	unittest.main()

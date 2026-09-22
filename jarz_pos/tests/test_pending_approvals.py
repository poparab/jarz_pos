"""Tests for the side-menu pending-approvals counts (api/approvals.py).

What they pin down:

* a queue the caller cannot act on is absent, not zero — the client shows
  exactly what the server returns;
* one broken queue never hides the others;
* the expense count excludes rejected drafts, and carries the oldest pending
  month so the screen can open where the request actually is.
"""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


if "frappe" not in sys.modules:
	fake_frappe = types.ModuleType("frappe")

	def fake_whitelist(*args, **kwargs):
		def decorator(func):
			return func

		if args and callable(args[0]) and len(args) == 1 and not kwargs:
			return args[0]
		return decorator

	fake_frappe._ = lambda message: message
	fake_frappe.whitelist = fake_whitelist
	fake_frappe.db = SimpleNamespace(count=lambda *a, **k: 0)
	fake_frappe.get_all = lambda *a, **k: []
	fake_frappe.get_roles = lambda *a, **k: []
	fake_frappe.log_error = lambda *a, **k: None
	fake_frappe.get_traceback = lambda *a, **k: ""
	fake_frappe.session = SimpleNamespace(user="manager@jarz.test")

	sys.modules["frappe"] = fake_frappe

from jarz_pos.api import approvals


def _queues(**builders):
	"""Replace the queue table with the given key -> builder callables."""
	return patch.object(approvals, "_QUEUES", list(builders.items()))


class TestGetPendingApprovals(unittest.TestCase):
	def test_ineligible_when_no_queue_applies(self):
		with _queues(expenses=lambda: None, custom_shipping=lambda: None):
			result = approvals.get_pending_approvals()
		self.assertEqual(result, {"eligible": False, "total": 0, "queues": []})

	def test_lists_only_actionable_queues_and_sums_them(self):
		with _queues(
			expenses=lambda: {"count": 2, "oldest_month": "2026-08"},
			employee_advances=lambda: None,
			payment_receipts=lambda: {"count": 5},
			custom_shipping=lambda: {"count": 0},
		):
			result = approvals.get_pending_approvals()
		self.assertTrue(result["eligible"])
		self.assertEqual(result["total"], 7)
		self.assertEqual(
			[q["key"] for q in result["queues"]],
			["expenses", "payment_receipts", "custom_shipping"],
		)
		self.assertEqual(result["queues"][0]["oldest_month"], "2026-08")

	def test_a_failing_queue_does_not_hide_the_others(self):
		def boom():
			raise RuntimeError("db down")

		with _queues(expenses=boom, payment_receipts=lambda: {"count": 3}):
			with patch.object(approvals, "_log") as log:
				result = approvals.get_pending_approvals()
		log.assert_called_once()
		self.assertEqual(result["total"], 3)
		self.assertEqual([q["key"] for q in result["queues"]], ["payment_receipts"])

	def test_a_failing_queue_keeps_the_client_polling(self):
		def boom():
			raise RuntimeError("db down")

		with _queues(expenses=boom):
			with patch.object(approvals, "_log"):
				result = approvals.get_pending_approvals()
		self.assertTrue(result["eligible"])


class TestExpensesQueue(unittest.TestCase):
	def _run(self, *, is_manager, count, rows):
		fake_expenses = SimpleNamespace(_is_manager=lambda: is_manager)
		calls = {}

		def fake_count(doctype, filters=None):
			calls["filters"] = filters
			return count

		with patch.dict(sys.modules, {"jarz_pos.api.expenses": fake_expenses}), \
			patch.object(approvals.frappe.db, "count", side_effect=fake_count, create=True), \
			patch.object(approvals.frappe, "get_all", return_value=rows, create=True):
			return approvals._expenses_queue(), calls

	def test_non_manager_gets_no_expense_queue(self):
		result, _ = self._run(is_manager=False, count=4, rows=[])
		self.assertIsNone(result)

	def test_counts_pending_approval_only_not_rejected_drafts(self):
		result, calls = self._run(
			is_manager=True, count=1, rows=[{"expense_month": "2026-08"}]
		)
		self.assertEqual(calls["filters"], {"docstatus": 0, "status": "Pending Approval"})
		self.assertEqual(result, {"count": 1, "oldest_month": "2026-08"})

	def test_no_month_lookup_when_nothing_is_pending(self):
		result, _ = self._run(is_manager=True, count=0, rows=[{"expense_month": "2026-01"}])
		self.assertEqual(result, {"count": 0, "oldest_month": None})


class TestMonthOf(unittest.TestCase):
	def test_date_and_month_strings(self):
		self.assertEqual(approvals._month_of("2026-08-14"), "2026-08")
		self.assertEqual(approvals._month_of("2026-08"), "2026-08")
		self.assertIsNone(approvals._month_of(None))
		self.assertIsNone(approvals._month_of(""))


if __name__ == "__main__":
	unittest.main()

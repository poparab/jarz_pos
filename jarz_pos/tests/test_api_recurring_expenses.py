"""Unit tests for the recurring-expenses roll-up logic.

These cover the pure decision functions — month resolution, cadence matching
and GL reconciliation classification — so they run without a site or database.
"""

from datetime import date
import io
import unittest
from unittest.mock import MagicMock, patch


class TestMonthBounds(unittest.TestCase):
	def test_explicit_month_resolves_to_first_and_last_day(self):
		from jarz_pos.api.recurring_expenses import _month_bounds

		start, end, key = _month_bounds("2026-02")
		self.assertEqual(start, date(2026, 2, 1))
		self.assertEqual(end, date(2026, 2, 28))
		self.assertEqual(key, "2026-02")

	def test_leap_february_gets_29_days(self):
		from jarz_pos.api.recurring_expenses import _month_bounds

		_, end, _key = _month_bounds("2024-02")
		self.assertEqual(end, date(2024, 2, 29))

	def test_31_day_month(self):
		from jarz_pos.api.recurring_expenses import _month_bounds

		start, end, _key = _month_bounds("2026-12")
		self.assertEqual(start, date(2026, 12, 1))
		self.assertEqual(end, date(2026, 12, 31))

	def test_missing_month_falls_back_to_current_month(self):
		from jarz_pos.api import recurring_expenses

		with patch.object(recurring_expenses, "getdate", return_value=date(2026, 8, 14)):
			start, end, key = recurring_expenses._month_bounds(None)
		self.assertEqual(start, date(2026, 8, 1))
		self.assertEqual(end, date(2026, 8, 31))
		self.assertEqual(key, "2026-08")

	def test_malformed_month_is_rejected(self):
		from jarz_pos.api import recurring_expenses

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = ValueError("rejected")
		with patch.object(recurring_expenses, "frappe", mock_frappe):
			with self.assertRaises(ValueError):
				recurring_expenses._month_bounds("not-a-month")


class TestDueInMonth(unittest.TestCase):
	MONTH_START = date(2026, 8, 1)
	MONTH_END = date(2026, 8, 31)

	def _due(self, **overrides):
		from jarz_pos.api.recurring_expenses import _is_due_in_month

		row = {
			"status": "Active",
			"frequency": "Monthly",
			"start_date": date(2026, 1, 1),
			"end_date": None,
		}
		row.update(overrides)
		return _is_due_in_month(row, self.MONTH_START, self.MONTH_END)

	def test_active_monthly_item_is_always_due(self):
		self.assertTrue(self._due())

	def test_paused_item_is_never_due(self):
		self.assertFalse(self._due(status="Paused"))

	def test_ended_item_is_never_due(self):
		self.assertFalse(self._due(status="Ended"))

	def test_item_starting_after_the_month_is_not_due(self):
		self.assertFalse(self._due(start_date=date(2026, 9, 1)))

	def test_item_that_ended_before_the_month_is_not_due(self):
		self.assertFalse(self._due(end_date=date(2026, 7, 31)))

	def test_item_ending_inside_the_month_is_still_due(self):
		self.assertTrue(self._due(end_date=date(2026, 8, 15)))

	def test_quarterly_lands_only_on_multiples_of_three_months(self):
		# Start Feb 2026 -> due Feb, May, Aug, Nov.
		self.assertTrue(self._due(frequency="Quarterly", start_date=date(2026, 2, 1)))
		# Start Mar 2026 -> due Mar, Jun, Sep, Dec (not Aug).
		self.assertFalse(self._due(frequency="Quarterly", start_date=date(2026, 3, 1)))

	def test_annual_lands_only_on_the_anniversary_month(self):
		self.assertTrue(self._due(frequency="Annual", start_date=date(2024, 8, 10)))
		self.assertFalse(self._due(frequency="Annual", start_date=date(2024, 7, 10)))

	def test_semi_annual_cadence(self):
		self.assertTrue(self._due(frequency="Semi-Annual", start_date=date(2026, 2, 1)))
		self.assertFalse(self._due(frequency="Semi-Annual", start_date=date(2026, 1, 1)))

	def test_row_without_start_date_is_not_due(self):
		self.assertFalse(self._due(start_date=None))


class TestReconcileStatus(unittest.TestCase):
	def _status(self, expected, actual):
		from jarz_pos.api.recurring_expenses import _reconcile_status

		return _reconcile_status(expected, actual)

	def test_nothing_expected_and_nothing_posted(self):
		self.assertEqual(self._status(0, 0), "None")

	def test_nothing_expected_but_something_posted(self):
		self.assertEqual(self._status(0, 500), "Unexpected")

	def test_expected_but_nothing_posted(self):
		self.assertEqual(self._status(1000, 0), "Missing")

	def test_exact_match_is_posted(self):
		self.assertEqual(self._status(1000, 1000), "Posted")

	def test_within_five_percent_tolerance_is_posted(self):
		self.assertEqual(self._status(1000, 980), "Posted")
		self.assertEqual(self._status(1000, 1040), "Posted")

	def test_materially_under_is_partial(self):
		self.assertEqual(self._status(1000, 400), "Partial")

	def test_materially_over_is_over(self):
		self.assertEqual(self._status(1000, 1500), "Over")


class TestMonthsBetween(unittest.TestCase):
	def test_same_month_is_zero(self):
		from jarz_pos.api.recurring_expenses import _months_between

		self.assertEqual(_months_between(date(2026, 5, 1), date(2026, 5, 20)), 0)

	def test_spans_year_boundary(self):
		from jarz_pos.api.recurring_expenses import _months_between

		self.assertEqual(_months_between(date(2025, 11, 1), date(2026, 2, 1)), 3)


class TestMonthlyEquivalent(unittest.TestCase):
	"""The DocType normalises every cadence to a monthly run-rate."""

	def _monthly(self, amount, frequency):
		from jarz_pos.doctype.jarz_recurring_expense.jarz_recurring_expense import (
			FREQUENCY_MONTHS,
		)

		return amount / FREQUENCY_MONTHS[frequency]

	def test_monthly_is_unchanged(self):
		self.assertEqual(self._monthly(1200, "Monthly"), 1200)

	def test_quarterly_is_divided_by_three(self):
		self.assertEqual(self._monthly(1200, "Quarterly"), 400)

	def test_semi_annual_is_divided_by_six(self):
		self.assertEqual(self._monthly(1200, "Semi-Annual"), 200)

	def test_annual_is_divided_by_twelve(self):
		self.assertEqual(self._monthly(1200, "Annual"), 100)

	def test_doctype_and_api_frequency_maps_agree(self):
		from jarz_pos.api.recurring_expenses import FREQUENCY_MONTHS as api_map
		from jarz_pos.doctype.jarz_recurring_expense.jarz_recurring_expense import (
			FREQUENCY_MONTHS as doctype_map,
		)

		self.assertEqual(api_map, doctype_map)


class TestNamingSeriesFallback(unittest.TestCase):
	"""API and Data Import inserts never get the form's client-side default."""

	def _before_insert(self, naming_series):
		from jarz_pos.doctype.jarz_recurring_expense.jarz_recurring_expense import (
			JarzRecurringExpense,
		)

		doc = MagicMock()
		doc.naming_series = naming_series
		JarzRecurringExpense.before_insert(doc)
		return doc.naming_series

	def test_missing_series_is_filled_in(self):
		from jarz_pos.doctype.jarz_recurring_expense.jarz_recurring_expense import (
			DEFAULT_NAMING_SERIES,
		)

		self.assertEqual(self._before_insert(None), DEFAULT_NAMING_SERIES)
		self.assertEqual(self._before_insert(""), DEFAULT_NAMING_SERIES)

	def test_explicit_series_is_left_alone(self):
		self.assertEqual(self._before_insert("CUSTOM-.#####"), "CUSTOM-.#####")

	def test_default_matches_the_doctype_field_options(self):
		import json
		import os

		from jarz_pos.doctype.jarz_recurring_expense import jarz_recurring_expense as mod

		path = os.path.join(
			os.path.dirname(mod.__file__), "jarz_recurring_expense.json"
		)
		with io.open(path, encoding="utf-8") as handle:
			schema = json.load(handle)

		field = next(
			f for f in schema["fields"] if f["fieldname"] == "naming_series"
		)
		# A read-only Data field silently drops its default on non-form inserts.
		self.assertEqual(field["fieldtype"], "Select")
		self.assertFalse(field.get("read_only"))
		self.assertEqual(field["options"], mod.DEFAULT_NAMING_SERIES)


if __name__ == "__main__":
	unittest.main()

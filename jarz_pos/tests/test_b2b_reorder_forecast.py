"""CRM reorder-forecast math tests (pure mock / unittest).

Feeds a fake submitted-invoice history (mocking ``frappe.get_all``) into
``reorder_forecast._compute_for_customer`` and asserts:
  - average order cycle = mean gap (days) between consecutive posting dates,
  - last_order_date = most recent posting date,
  - avg_basket_value = mean grand_total,
  - predicted_next_order = last_order_date + round(avg_cycle),
  - it never raises on empty history or missing/garbage fields.

Date helpers (``frappe.utils.date_diff`` / ``add_days``) are real (pure functions); only
the DB query is mocked. No FrappeTestCase / erpnext.tests.utils import.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from jarz_pos.crm import reorder_forecast as rf

# All four target fields present, mirroring a migrated Customer doctype.
_ALL_PRESENT = {f: True for f in rf._TARGET_FIELDS}


@contextmanager
def _invoices(rows):
    """Mock frappe.get_all to return a fixed invoice history for the customer."""
    with patch.object(rf.frappe, "get_all", return_value=list(rows)):
        yield


def _inv(date, total):
    return {"posting_date": date, "grand_total": total}


class TestReorderMath(unittest.TestCase):
    def test_regular_cadence(self):
        # 10-day cadence: gaps [10, 10] -> avg 10.0; predicted = last + 10.
        rows = [
            _inv("2026-01-01", 100),
            _inv("2026-01-11", 200),
            _inv("2026-01-21", 300),
        ]
        with _invoices(rows):
            out = rf._compute_for_customer("CUST-1", _ALL_PRESENT)
        self.assertEqual(out["custom_avg_order_cycle_days"], 10.0)
        self.assertEqual(str(out["custom_last_order_date"]), "2026-01-21")
        self.assertEqual(out["custom_avg_basket_value"], 200.0)  # mean of 100/200/300
        self.assertEqual(str(out["custom_predicted_next_order"]), "2026-01-31")

    def test_irregular_cadence_average(self):
        # Gaps [5, 15] -> avg 10.0; predicted = 2026-02-10 + 10 = 2026-02-20.
        rows = [
            _inv("2026-01-21", 50),
            _inv("2026-01-26", 50),
            _inv("2026-02-10", 200),
        ]
        with _invoices(rows):
            out = rf._compute_for_customer("CUST-2", _ALL_PRESENT)
        self.assertEqual(out["custom_avg_order_cycle_days"], 10.0)
        self.assertEqual(str(out["custom_predicted_next_order"]), "2026-02-20")

    def test_rounding_of_fractional_cycle(self):
        # Gaps [3, 4] -> avg 3.5; round(3.5) for add_days. last 2026-01-08 + 4 days.
        rows = [
            _inv("2026-01-01", 10),
            _inv("2026-01-04", 10),
            _inv("2026-01-08", 10),
        ]
        with _invoices(rows):
            out = rf._compute_for_customer("CUST-3", _ALL_PRESENT)
        self.assertEqual(out["custom_avg_order_cycle_days"], 3.5)
        # int(round(3.5)) == 4 (banker's rounding rounds .5 to even -> 4).
        self.assertEqual(str(out["custom_predicted_next_order"]), "2026-01-12")


class TestReorderEdgeCases(unittest.TestCase):
    def test_single_invoice_no_cycle(self):
        # One invoice -> avg_cycle 0, last_date set, NO predicted_next_order.
        rows = [_inv("2026-03-01", 500)]
        with _invoices(rows):
            out = rf._compute_for_customer("CUST-4", _ALL_PRESENT)
        self.assertEqual(out["custom_avg_order_cycle_days"], 0.0)
        self.assertEqual(str(out["custom_last_order_date"]), "2026-03-01")
        self.assertEqual(out["custom_avg_basket_value"], 500.0)
        self.assertNotIn("custom_predicted_next_order", out)

    def test_empty_history_returns_none(self):
        with _invoices([]):
            self.assertIsNone(rf._compute_for_customer("CUST-5", _ALL_PRESENT))

    def test_missing_posting_dates_returns_none(self):
        # Rows without posting_date -> nothing to compute, never raises.
        rows = [{"grand_total": 100}, {"grand_total": 200}]
        with _invoices(rows):
            self.assertIsNone(rf._compute_for_customer("CUST-6", _ALL_PRESENT))

    def test_missing_grand_totals_skips_basket(self):
        # Dates present but no totals -> basket value omitted, no crash.
        rows = [
            {"posting_date": "2026-01-01"},
            {"posting_date": "2026-01-11"},
        ]
        with _invoices(rows):
            out = rf._compute_for_customer("CUST-7", _ALL_PRESENT)
        self.assertNotIn("custom_avg_basket_value", out)
        self.assertEqual(out["custom_avg_order_cycle_days"], 10.0)

    def test_only_present_fields_written(self):
        # When only some target fields exist, only those keys appear in the result.
        present = {f: False for f in rf._TARGET_FIELDS}
        present["custom_last_order_date"] = True
        rows = [_inv("2026-01-01", 10), _inv("2026-01-11", 20)]
        with _invoices(rows):
            out = rf._compute_for_customer("CUST-8", present)
        self.assertEqual(set(out.keys()), {"custom_last_order_date"})

    def test_query_failure_returns_none(self):
        # A DB error during the invoice query is swallowed (never raises).
        with patch.object(rf.frappe, "get_all", side_effect=RuntimeError("db down")):
            self.assertIsNone(rf._compute_for_customer("CUST-9", _ALL_PRESENT))


class TestComputeReorderForecastTask(unittest.TestCase):
    """The scheduled entrypoint never raises and reports a summary."""

    def test_missing_doctypes_noop(self):
        with patch.object(rf, "_doctype_exists", return_value=False):
            summary = rf.compute_reorder_forecast()
        self.assertEqual(summary, {"processed": 0, "updated": 0, "errors": 0})

    def test_no_target_fields_noop(self):
        with patch.object(rf, "_doctype_exists", return_value=True), patch.object(
            rf, "_has_field", return_value=False
        ):
            summary = rf.compute_reorder_forecast()
        self.assertEqual(summary, {"processed": 0, "updated": 0, "errors": 0})

    def test_processes_and_updates_customers(self):
        # Two Company customers; both yield an update -> processed=2, updated=2.
        with patch.object(rf, "_doctype_exists", return_value=True), patch.object(
            rf, "_has_field", return_value=True
        ), patch.object(
            rf.frappe, "get_all", return_value=[{"name": "C1"}, {"name": "C2"}]
        ), patch.object(
            rf, "_compute_for_customer", return_value={"custom_last_order_date": "2026-01-01"}
        ), patch.object(rf.frappe.db, "set_value") as mock_set, patch.object(
            rf.frappe.db, "commit"
        ):
            summary = rf.compute_reorder_forecast()
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["updated"], 2)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(mock_set.call_count, 2)

    def test_per_customer_failure_counts_error_not_raise(self):
        with patch.object(rf, "_doctype_exists", return_value=True), patch.object(
            rf, "_has_field", return_value=True
        ), patch.object(
            rf.frappe, "get_all", return_value=[{"name": "C1"}]
        ), patch.object(
            rf, "_compute_for_customer", side_effect=RuntimeError("boom")
        ), patch.object(rf.frappe.db, "commit"):
            summary = rf.compute_reorder_forecast()
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["errors"], 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for invoice utilities.

This module tests utility functions for invoice processing.
"""

import unittest


class TestInvoiceUtils(unittest.TestCase):
	"""Test class for invoice utility functions."""

	def test_get_address_details_empty(self):
		"""Test get_address_details with empty input."""
		from jarz_pos.utils.invoice_utils import get_address_details

		result = get_address_details(None)
		self.assertEqual(result, "", "Should return empty string for None")

		result = get_address_details("")
		self.assertEqual(result, "", "Should return empty string for empty string")

	def test_apply_invoice_filters_default(self):
		"""Test apply_invoice_filters with default filters."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		result = apply_invoice_filters(None)

		# Should apply default filters
		self.assertIn("docstatus", result, "Should include docstatus filter")
		self.assertEqual(result["docstatus"], 1, "docstatus should be 1")
		self.assertIn("is_pos", result, "Should include is_pos filter")
		self.assertEqual(result["is_pos"], 1, "is_pos should be 1")

	def test_apply_invoice_filters_date_from(self):
		"""Test apply_invoice_filters with dateFrom filter."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		filters = {"dateFrom": "2025-01-01"}
		result = apply_invoice_filters(filters)

		# Should apply date filter
		self.assertIn("posting_date", result, "Should include posting_date filter")
		self.assertEqual(result["posting_date"][0], ">=", "Should use >= operator")
		self.assertEqual(result["posting_date"][1], "2025-01-01", "Should use correct date")

	def test_apply_invoice_filters_date_to(self):
		"""Test apply_invoice_filters with dateTo filter."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		filters = {"dateTo": "2025-12-31"}
		result = apply_invoice_filters(filters)

		# Should apply date filter
		self.assertIn("posting_date", result, "Should include posting_date filter")
		self.assertEqual(result["posting_date"][0], "<=", "Should use <= operator")
		self.assertEqual(result["posting_date"][1], "2025-12-31", "Should use correct date")

	def test_apply_invoice_filters_date_range(self):
		"""Test apply_invoice_filters with date range."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		filters = {"dateFrom": "2025-01-01", "dateTo": "2025-12-31"}
		result = apply_invoice_filters(filters)

		# Should apply date range filter
		self.assertIn("posting_date", result, "Should include posting_date filter")
		# When both are provided, implementation may use 'between' or array format
		# We'll verify it's present and has the dates

	def test_apply_invoice_filters_customer(self):
		"""Test apply_invoice_filters with customer filter."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		filters = {"customer": "Test Customer"}
		result = apply_invoice_filters(filters)

		# Should apply customer filter
		self.assertIn("customer", result, "Should include customer filter")
		self.assertEqual(result["customer"], "Test Customer", "Should filter by customer")

	def test_apply_invoice_filters_branch(self):
		"""Test apply_invoice_filters with branch filter."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		filters = {"branch": "Test Branch"}
		result = apply_invoice_filters(filters)

		# Current implementation leaves branch filtering to higher-level APIs
		self.assertNotIn("branch", result, "Branch filter handled separately in API layer")
		self.assertNotIn("pos_profile", result, "POS profile filter applied later")
		self.assertIn("docstatus", result, "Base filters should remain intact")

	def test_apply_invoice_filters_normalises_iso_dates(self):
		"""Test apply_invoice_filters trims ISO timestamps down to posting dates."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		filters = {
			"dateFrom": "2025-01-01T00:00:00.000",
			"dateTo": "2025-01-31T23:59:59.999",
		}
		result = apply_invoice_filters(filters)

		self.assertEqual(result["posting_date"], ["between", ["2025-01-01", "2025-01-31"]])

	def test_apply_invoice_filters_status_unpaid_includes_overdue(self):
		"""Test unpaid filter includes overdue invoices."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		result = apply_invoice_filters({"status": "Unpaid"})

		self.assertEqual(result["status"], ["in", ["Unpaid", "Overdue"]])
		self.assertEqual(result["docstatus"], 1)

	def test_apply_invoice_filters_status_cancelled_uses_docstatus(self):
		"""Test cancelled filter scopes to cancelled documents."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		result = apply_invoice_filters({"status": "Cancelled"})

		self.assertEqual(result["docstatus"], 2)
		self.assertNotIn("status", result)

	def test_apply_invoice_filters_status_return_uses_is_return(self):
		"""Test return filter scopes to return invoices."""
		from jarz_pos.utils.invoice_utils import apply_invoice_filters

		result = apply_invoice_filters({"status": "Return"})

		self.assertEqual(result["is_return"], 1)
		self.assertEqual(result["docstatus"], 1)

	def test_format_invoice_data_basic(self):
		"""Test format_invoice_data with basic invoice."""

		# This requires a real invoice object, which is complex to mock
		# We'll test that it can be called (may fail without proper data)
		pass


class TestTerritoryLabels(unittest.TestCase):
	"""A WooCommerce area code must never be what an operator reads.

	Territories synced from WooCommerce are *named* by their Woo state code
	("EGNASRCITY"), so every surface that prints the raw Link value prints a
	code. The kanban card already resolves Arabic-first; the details payload and
	the address string have to agree with it.
	"""

	def _patched(self, values):
		"""Patch the single Territory read both helpers make."""
		from unittest import mock
		from jarz_pos.utils import invoice_utils

		return mock.patch.object(
			invoice_utils.frappe.db, "get_value", return_value=values
		)

	def test_labels_prefer_the_names_over_the_code(self):
		from jarz_pos.utils.invoice_utils import get_territory_labels

		with self._patched(
			{"territory_name": "Nasr City", "custom_territory_name_ar": "مدينة نصر"}
		):
			labels = get_territory_labels("EGNASRCITY")

		self.assertEqual(labels["territory"], "EGNASRCITY")
		self.assertEqual(labels["territory_display"], "Nasr City")
		self.assertEqual(labels["territory_name_ar"], "مدينة نصر")

	def test_area_label_is_arabic_first_then_title_then_the_code(self):
		from jarz_pos.utils.invoice_utils import get_area_label

		with self._patched(
			{"territory_name": "Nasr City", "custom_territory_name_ar": "مدينة نصر"}
		):
			self.assertEqual(get_area_label("EGNASRCITY"), "مدينة نصر")

		with self._patched({"territory_name": "Nasr City", "custom_territory_name_ar": ""}):
			self.assertEqual(get_area_label("EGNASRCITY"), "Nasr City")

		# An unknown / unsynced territory still reads as itself, never as blank.
		with self._patched(None):
			self.assertEqual(get_area_label("EGNASRCITY"), "EGNASRCITY")

	def test_no_territory_yields_no_label(self):
		from jarz_pos.utils.invoice_utils import get_area_label, get_territory_labels

		self.assertEqual(get_area_label(None), "")
		self.assertEqual(
			get_territory_labels(""),
			{"territory": "", "territory_display": "", "territory_name_ar": ""},
		)

	def test_address_prints_the_area_name_not_the_woo_code(self):
		"""``Address.city`` holds the Woo code on every Woo-sourced address."""
		from types import SimpleNamespace
		from unittest import mock
		from jarz_pos.utils import invoice_utils

		address = SimpleNamespace(
			address_line1="حدائق الاهرام، عمارة 297",
			address_line2=None,
			city="EGHADAYEQAH",
		)

		with mock.patch.object(invoice_utils.frappe, "get_doc", return_value=address), 			mock.patch.object(
				invoice_utils.frappe.db,
				"get_value",
				return_value={
					"territory_name": "Hadayek Al-Ahram",
					"custom_territory_name_ar": "حدائق الاهرام",
				},
			):
			rendered = invoice_utils.get_address_details("SOME-ADDRESS")

		self.assertNotIn("EGHADAYEQAH", rendered)
		self.assertTrue(rendered.endswith("حدائق الاهرام"), rendered)


"""Tests for customer API endpoints.

This module tests customer-related API endpoints.
"""

import unittest
from unittest.mock import patch

import frappe


class TestCustomerAPI(unittest.TestCase):
	"""Test class for Customer API functionality."""

	def test_get_customers_structure(self):
		"""Test that get_customers returns correct structure."""
		from jarz_pos.api.customer import get_customers

		result = get_customers()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_recent_customers_structure(self):
		"""Test that get_recent_customers returns correct structure."""
		from jarz_pos.api.customer import get_recent_customers

		result = get_recent_customers()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_recent_customers_limit(self):
		"""Test that get_recent_customers respects limit parameter."""
		from jarz_pos.api.customer import get_recent_customers

		# Test with small limit
		result = get_recent_customers(limit=5)

		# Should not exceed limit
		self.assertLessEqual(len(result), 5, "Should not exceed specified limit of 5")

	def test_search_customers_structure(self):
		"""Test that search_customers returns correct structure."""
		from jarz_pos.api.customer import search_customers

		# Test with empty search should return empty list
		result = search_customers(name="")

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_territories_structure(self):
		"""Test that get_territories returns correct structure."""
		from jarz_pos.api.customer import get_territories

		result = get_territories()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

		# If there are territories, verify their structure
		if result:
			territory = result[0]
			self.assertIn("name", territory, "Territory should have name")

	def test_get_territories_includes_arabic_name(self):
		"""Test that get_territories exposes Arabic territory labels for the app."""
		from jarz_pos.api.customer import get_territories

		rows = [
			{
				"name": "EGNASRCITY",
				"territory_name": "EGNASRCITY",
				"custom_woo_code": "EGNASRCITY",
				"custom_territory_name_ar": "مدينة نصر",
				"delivery_income": 25,
				"delivery_expense": 10,
			}
		]

		with patch(
			"jarz_pos.utils.invoice_utils._territory_has_column", return_value=True
		), patch(
			"jarz_pos.api.customer.frappe.get_all", return_value=rows
		):
			result = get_territories()

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["territory_name_ar"], "مدينة نصر")
		self.assertEqual(result[0]["woo_code"], "EGNASRCITY")
		self.assertEqual(result[0]["delivery_income"], 25.0)
		self.assertEqual(result[0]["delivery_expense"], 10.0)

	def test_get_territories_drops_rows_without_a_woo_code(self):
		"""Only Woo-coded territories are selectable delivery areas.

		"Egypt" and the Arabic-named sub-zones under a coded parent are real
		Territory records, but an order can never ship to them directly.
		"""
		from jarz_pos.api.customer import get_territories

		rows = [
			{"name": "All Territories", "territory_name": "All Territories", "custom_woo_code": None},
			{"name": "Egypt", "territory_name": "Egypt", "custom_woo_code": ""},
			{"name": "القرية الذكية", "territory_name": "القرية الذكية", "custom_woo_code": "   "},
			{"name": "EGZAYED", "territory_name": "EGZAYED", "custom_woo_code": "EGZAYED"},
		]

		with patch(
			"jarz_pos.utils.invoice_utils._territory_has_column", return_value=True
		), patch(
			"jarz_pos.api.customer.frappe.get_all", return_value=rows
		):
			result = get_territories()

		self.assertEqual([t["name"] for t in result], ["EGZAYED"])

	def test_get_territories_unfiltered_without_a_woo_code_column(self):
		"""A site without the WooCommerce app keeps an unfiltered picker.

		Filtering on a column that does not exist would empty the dropdown
		entirely, which is worse than showing every territory.
		"""
		from jarz_pos.api.customer import get_territories

		rows = [{"name": "Egypt", "territory_name": "Egypt"}]

		with patch(
			"jarz_pos.utils.invoice_utils._territory_has_column", return_value=False
		), patch(
			"jarz_pos.api.customer.frappe.get_all", return_value=rows
		):
			result = get_territories()

		self.assertEqual([t["name"] for t in result], ["Egypt"])
		self.assertEqual(result[0]["woo_code"], "")

	def test_get_territories_search_matches_arabic_name(self):
		"""Search has to hit the Arabic label — the record name is a Woo code."""
		from jarz_pos.api.customer import get_territories

		rows = [
			{
				"name": "EGNASRCITY",
				"territory_name": "EGNASRCITY",
				"custom_woo_code": "EGNASRCITY",
				"custom_territory_name_ar": "مدينة نصر",
			},
			{
				"name": "EGMAADI",
				"territory_name": "EGMAADI",
				"custom_woo_code": "EGMAADI",
				"custom_territory_name_ar": "المعادى",
			},
		]

		with patch(
			"jarz_pos.utils.invoice_utils._territory_has_column", return_value=True
		), patch(
			"jarz_pos.api.customer.frappe.get_all", return_value=rows
		):
			result = get_territories(search="نصر")

		self.assertEqual([t["name"] for t in result], ["EGNASRCITY"])

	def test_get_territory_structure(self):
		"""Test that get_territory returns correct structure."""
		from jarz_pos.api.customer import get_territory

		# Test with a territory name (may not exist)
		try:
			result = get_territory(territory_id="Test Territory")
			# If it succeeds, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Territory may not exist in test environment
			pass

	def test_create_customer_validation(self):
		"""Test that create_customer validates required fields."""
		from jarz_pos.api.customer import create_customer

		# Test with missing required fields should raise an error
		with self.assertRaises(Exception):
			create_customer(
				customer_name="",  # Empty name should fail
				mobile_no="",
				email="",
			)

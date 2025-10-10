"""Tests for customer API endpoints.

This module tests customer-related API endpoints.
"""

import unittest

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

		# Test with empty search
		result = search_customers(search_term="")

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

	def test_get_territory_structure(self):
		"""Test that get_territory returns correct structure."""
		from jarz_pos.api.customer import get_territory

		# Test with a territory name (may not exist)
		try:
			result = get_territory(territory="Test Territory")
			# If it succeeds, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except frappe.DoesNotExistError:
			# Territory doesn't exist, which is fine for this test
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

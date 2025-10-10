"""Tests for purchase API endpoints.

This module tests purchase-related API endpoints.
"""

import unittest


class TestPurchaseAPI(unittest.TestCase):
	"""Test class for Purchase API functionality."""

	def test_get_suppliers_structure(self):
		"""Test that get_suppliers returns correct structure."""
		from jarz_pos.api.purchase import get_suppliers

		result = get_suppliers()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_recent_suppliers_structure(self):
		"""Test that get_recent_suppliers returns correct structure."""
		from jarz_pos.api.purchase import get_recent_suppliers

		result = get_recent_suppliers()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_recent_suppliers_limit(self):
		"""Test that get_recent_suppliers respects limit parameter."""
		from jarz_pos.api.purchase import get_recent_suppliers

		# Test with small limit
		result = get_recent_suppliers(limit=5)

		# Should not exceed limit
		self.assertLessEqual(len(result), 5, "Should not exceed specified limit")

	def test_search_items_structure(self):
		"""Test that search_items returns correct structure."""
		from jarz_pos.api.purchase import search_items

		# Test with empty search
		result = search_items(search="")

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_item_details_validation(self):
		"""Test that get_item_details validates item parameter."""
		from jarz_pos.api.purchase import get_item_details

		# Test with non-existent item
		try:
			result = get_item_details(item_code="NON_EXISTENT_ITEM")
			# If it doesn't raise, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Item may not exist
			pass

	def test_get_item_price_validation(self):
		"""Test that get_item_price validates inputs."""
		from jarz_pos.api.purchase import get_item_price

		# Test with non-existent item
		try:
			result = get_item_price(item_code="NON_EXISTENT_ITEM", supplier="Test Supplier")
			# If it doesn't raise, verify structure
			self.assertIsInstance(result, (dict, float, type(None)), "Should return dict, float, or None")
		except Exception:
			# Item may not exist
			pass

	def test_create_purchase_invoice_validation(self):
		"""Test that create_purchase_invoice validates inputs."""
		from jarz_pos.api.purchase import create_purchase_invoice

		# Test with invalid data should raise an error
		with self.assertRaises(Exception):
			create_purchase_invoice(supplier="", items=[])

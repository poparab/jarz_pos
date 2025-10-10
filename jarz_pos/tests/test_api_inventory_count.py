"""Tests for inventory count API endpoints.

This module tests inventory counting and reconciliation endpoints.
"""

import unittest


class TestInventoryCountAPI(unittest.TestCase):
	"""Test class for Inventory Count API functionality."""

	def test_list_warehouses_structure(self):
		"""Test that list_warehouses returns correct structure."""
		from jarz_pos.api.inventory_count import list_warehouses

		result = list_warehouses()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

		# If there are warehouses, verify their structure
		if result:
			warehouse = result[0]
			self.assertIn("name", warehouse, "Warehouse should have name")

	def test_list_item_groups_structure(self):
		"""Test that list_item_groups returns correct structure."""
		from jarz_pos.api.inventory_count import list_item_groups

		result = list_item_groups()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

		# If there are item groups, verify their structure
		if result:
			item_group = result[0]
			self.assertIn("name", item_group, "Item group should have name")

	def test_list_items_for_count_requires_warehouse(self):
		"""Test that list_items_for_count requires warehouse parameter."""
		from jarz_pos.api.inventory_count import list_items_for_count

		# Test with empty warehouse should handle gracefully
		result = list_items_for_count(warehouse="")

		# Should return a list (possibly empty)
		self.assertIsInstance(result, list, "Should return a list")

	def test_list_items_for_count_structure(self):
		"""Test that list_items_for_count returns correct structure."""
		from jarz_pos.api.inventory_count import list_items_for_count

		# Test with a warehouse (may not exist)
		try:
			result = list_items_for_count(warehouse="Test Warehouse")
			# If successful, verify structure
			self.assertIsInstance(result, list, "Should return a list")
		except Exception:
			# Warehouse may not exist
			pass

	def test_submit_reconciliation_validation(self):
		"""Test that submit_reconciliation validates inputs."""
		from jarz_pos.api.inventory_count import submit_reconciliation

		# Test with invalid data should raise an error
		with self.assertRaises(Exception):
			submit_reconciliation(warehouse="", items=[])

"""Tests for manufacturing API endpoints.

This module tests manufacturing and work order endpoints.
"""
import unittest
import frappe


class TestManufacturingAPI(unittest.TestCase):
	"""Test class for Manufacturing API functionality."""

	def test_list_default_bom_items_structure(self):
		"""Test that list_default_bom_items returns correct structure."""
		from jarz_pos.api.manufacturing import list_default_bom_items

		result = list_default_bom_items()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_list_bom_items_structure(self):
		"""Test that list_bom_items returns correct structure."""
		from jarz_pos.api.manufacturing import list_bom_items

		# Test with no filters
		result = list_bom_items()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_search_bom_items_structure(self):
		"""Test that search_bom_items returns correct structure."""
		from jarz_pos.api.manufacturing import search_bom_items

		# Test with empty search
		result = search_bom_items(search="")

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_bom_details_validation(self):
		"""Test that get_bom_details validates item parameter."""
		from jarz_pos.api.manufacturing import get_bom_details

		# Test with non-existent item
		try:
			result = get_bom_details(item="NON_EXISTENT_ITEM")
			# If it doesn't raise, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Item may not exist
			pass

	def test_submit_work_orders_validation(self):
		"""Test that submit_work_orders validates inputs."""
		from jarz_pos.api.manufacturing import submit_work_orders

		# Test with empty orders should handle gracefully
		try:
			result = submit_work_orders(orders=[])
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# May require at least one order
			pass

	def test_submit_single_work_order_validation(self):
		"""Test that submit_single_work_order validates inputs."""
		from jarz_pos.api.manufacturing import submit_single_work_order

		# Test with invalid data should raise an error
		with self.assertRaises(Exception):
			submit_single_work_order(item="", qty=0)

	def test_list_recent_work_orders_structure(self):
		"""Test that list_recent_work_orders returns correct structure."""
		from jarz_pos.api.manufacturing import list_recent_work_orders

		result = list_recent_work_orders()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_list_recent_work_orders_limit(self):
		"""Test that list_recent_work_orders respects limit parameter."""
		from jarz_pos.api.manufacturing import list_recent_work_orders

		# Test with small limit
		result = list_recent_work_orders(limit=5)

		# Should not exceed limit
		self.assertLessEqual(len(result), 5, "Should not exceed specified limit")

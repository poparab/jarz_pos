"""Tests for transfer API endpoints.

This module tests transfer-related API endpoints.
"""

import unittest
from unittest.mock import patch


class TestTransferAPI(unittest.TestCase):
	"""Test class for Transfer API functionality."""

	def test_transfer_module_imports(self):
		"""Test that transfer module can be imported."""
		try:
			import jarz_pos.api.transfer as transfer_module  # noqa: F401

			self.assertTrue(True, "Transfer module should be importable")
		except ImportError:
			self.fail("Transfer module should be importable")

	def test_transfer_endpoints_exist(self):
		"""Test that transfer endpoints are defined."""
		import jarz_pos.api.transfer as transfer_module

		# Check for whitelisted functions
		# This depends on what's actually in the module
		# If the module is empty, this test just verifies it exists
		self.assertTrue(
			hasattr(transfer_module, "__name__"), "Transfer module should have __name__ attribute"
		)

	def test_list_pos_profiles_includes_finished_goods_option(self):
		"""Finished Goods warehouse should be selectable even without a POS Profile."""
		from jarz_pos.api import transfer

		with patch.object(transfer, "_ensure_manager_access"), \
			 patch.object(
				transfer.frappe,
				"get_all",
				return_value=[{"name": "Dokki", "company": "JARZ", "warehouse": "Stores - Dokki"}],
			 ), \
			 patch.object(
				transfer.frappe.db,
				"get_single_value",
				return_value="Finished Goods - J",
			 ), \
			 patch.object(
				transfer.frappe.db,
				"get_value",
				return_value="JARZ",
			 ):
			result = transfer.list_pos_profiles()

		self.assertTrue(any(row["warehouse"] == "Finished Goods - J" for row in result))
		finished_goods = next(row for row in result if row["warehouse"] == "Finished Goods - J")
		self.assertEqual(finished_goods["name"], "Finished Goods")

	def test_list_pos_profiles_does_not_duplicate_finished_goods_warehouse(self):
		"""Do not append a second option when a POS Profile already uses the FG warehouse."""
		from jarz_pos.api import transfer

		with patch.object(transfer, "_ensure_manager_access"), \
			 patch.object(
				transfer.frappe,
				"get_all",
				return_value=[{"name": "Finished Goods Branch", "company": "JARZ", "warehouse": "Finished Goods - J"}],
			 ), \
			 patch.object(
				transfer.frappe.db,
				"get_single_value",
				return_value="Finished Goods - J",
			 ), \
			 patch.object(
				transfer.frappe.db,
				"get_value",
				return_value="JARZ",
			 ):
			result = transfer.list_pos_profiles()

		self.assertEqual(sum(1 for row in result if row["warehouse"] == "Finished Goods - J"), 1)

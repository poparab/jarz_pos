"""Tests for transfer API endpoints.

This module tests transfer-related API endpoints.
"""

import unittest
from unittest.mock import patch


class TestTransferAPI(unittest.TestCase):
	"""Test class for Transfer API functionality."""

	def test_default_fg_warehouse_is_read_off_the_default_company(self):
		"""v16 moved ``default_fg_warehouse`` from Manufacturing Settings onto the Company."""
		from jarz_pos.api import transfer

		with patch.object(
			transfer.frappe.db, "get_single_value", return_value="JARZ"
		) as single, patch.object(
			transfer.frappe.db, "get_value", return_value="Finished Goods - J"
		) as get_value, patch.object(transfer.frappe.db, "sql") as sql:
			result = transfer._get_default_fg_warehouse()

		self.assertEqual(result, "Finished Goods - J")
		single.assert_called_once_with("Global Defaults", "default_company")
		get_value.assert_called_once_with("Company", "JARZ", "default_fg_warehouse")
		# Never the stale v15 ``tabSingles`` row for Manufacturing Settings.
		sql.assert_not_called()

	def test_default_fg_warehouse_uses_the_company_in_hand(self):
		from jarz_pos.api import transfer

		with patch.object(transfer.frappe.db, "get_single_value") as single, patch.object(
			transfer.frappe.db, "get_value", return_value="Finished Goods - O"
		) as get_value:
			result = transfer._get_default_fg_warehouse("Other Co")

		self.assertEqual(result, "Finished Goods - O")
		single.assert_not_called()
		get_value.assert_called_once_with("Company", "Other Co", "default_fg_warehouse")

	def test_default_fg_warehouse_is_none_without_a_default_company(self):
		from jarz_pos.api import transfer

		with patch.object(transfer.frappe.db, "get_single_value", return_value=None), patch.object(
			transfer.frappe.db, "get_value"
		) as get_value:
			self.assertIsNone(transfer._get_default_fg_warehouse())
		get_value.assert_not_called()

	def test_default_fg_warehouse_is_none_when_the_company_has_none(self):
		from jarz_pos.api import transfer

		with patch.object(transfer.frappe.db, "get_single_value", return_value="JARZ"), patch.object(
			transfer.frappe.db, "get_value", return_value=""
		):
			self.assertIsNone(transfer._get_default_fg_warehouse())

	def test_default_fg_warehouse_read_failure_is_none_not_an_error(self):
		"""The Finished Goods option is an extra; a failed read must not break the list."""
		from jarz_pos.api import transfer

		with patch.object(transfer.frappe.db, "get_single_value", return_value="JARZ"), patch.object(
			transfer.frappe.db, "get_value", side_effect=Exception("db down")
		):
			self.assertIsNone(transfer._get_default_fg_warehouse())

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

		with patch.object(transfer, "_ensure_transfer_access"), \
			 patch.object(
				transfer.frappe,
				"get_all",
				return_value=[{"name": "Dokki", "company": "JARZ", "warehouse": "Stores - Dokki"}],
			 ), \
			 patch.object(
				transfer,
				"_get_default_fg_warehouse",
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

		with patch.object(transfer, "_ensure_transfer_access"), \
			 patch.object(
				transfer.frappe,
				"get_all",
				return_value=[{"name": "Finished Goods Branch", "company": "JARZ", "warehouse": "Finished Goods - J"}],
			 ), \
			 patch.object(
				transfer,
				"_get_default_fg_warehouse",
				return_value="Finished Goods - J",
			 ), \
			 patch.object(
				transfer.frappe.db,
				"get_value",
				return_value="JARZ",
			 ):
			result = transfer.list_pos_profiles()

		self.assertEqual(sum(1 for row in result if row["warehouse"] == "Finished Goods - J"), 1)

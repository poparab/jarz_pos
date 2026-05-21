"""Tests for the kanban API module.
This module contains tests for the kanban API endpoints.
"""

import unittest
from unittest.mock import patch
import frappe
from jarz_pos.api.kanban import (
	_build_invoice_search_or_filters,
	get_kanban_columns,
	get_kanban_invoices,
	get_invoice_details,
	get_kanban_filters,
)
from jarz_pos.utils.invoice_utils import get_address_details, apply_invoice_filters


class TestKanbanAPI(unittest.TestCase):
	"""Test class for Kanban API functionality."""

	@classmethod
	def setUpClass(cls):
		"""Set up test environment before all tests."""
		# Ensure the sales_invoice_state custom field exists
		cls.ensure_custom_field_exists()

	@classmethod
	def ensure_custom_field_exists(cls):
		"""Ensure the sales_invoice_state custom field exists."""
		try:
			# Check if the custom field exists
			custom_field = frappe.db.exists(
				"Custom Field", {"dt": "Sales Invoice", "fieldname": "sales_invoice_state"}
			)

			if not custom_field:
				# Create the custom field if it doesn't exist
				field = frappe.get_doc(
					{
						"doctype": "Custom Field",
						"dt": "Sales Invoice",
						"fieldname": "sales_invoice_state",
						"label": "Sales Invoice State",
						"fieldtype": "Select",
						"options": "Received\nProcessing\nPreparing\nOut for delivery\nCompleted",
						"insert_after": "status",
						"allow_on_submit": 1,
					}
				)
				field.insert(ignore_permissions=True)
		except Exception as e:
			frappe.log_error(f"Error ensuring custom field exists: {str(e)}", "Kanban Test")

	def test_get_kanban_columns(self):
		"""Test the get_kanban_columns endpoint."""
		result = get_kanban_columns()
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("columns", result, "Should include columns key")
		self.assertTrue(len(result["columns"]) > 0, "Should return at least one column")

	def test_apply_invoice_filters(self):
		"""Test the apply_invoice_filters utility function."""
		# Test empty filters
		result = apply_invoice_filters(None)
		self.assertEqual(result["docstatus"], 1, "Should have docstatus=1")
		self.assertEqual(result["is_pos"], 1, "Should have is_pos=1")

		# Test with date filter
		result = apply_invoice_filters({"dateFrom": "2025-01-01"})
		self.assertEqual(result["posting_date"][0], ">=", "Should have >= operator")
		self.assertEqual(result["posting_date"][1], "2025-01-01", "Should have correct date")

	def test_build_invoice_search_or_filters(self):
		"""Search helper should cover invoice identifiers and matched customer ids."""
		result = _build_invoice_search_or_filters("Ali", customer_ids=["CUST-1"])

		self.assertIn({"name": ["like", "%Ali%"]}, result)
		self.assertIn({"customer_name": ["like", "%Ali%"]}, result)
		self.assertIn({"customer": ["like", "%Ali%"]}, result)
		self.assertIn({"customer": ["in", ["CUST-1"]]}, result)

	@patch("jarz_pos.api.kanban._sort_kanban_columns", side_effect=lambda data: data)
	@patch("jarz_pos.api.kanban._get_active_payment_receipt_map", return_value={})
	@patch("jarz_pos.api.kanban._find_customer_search_matches", return_value=["CUST-1"])
	@patch("jarz_pos.api.kanban._get_state_field_options", return_value=["Received"])
	@patch("jarz_pos.api.kanban._get_current_user_pos_profiles", return_value=["Main"])
	@patch("jarz_pos.api.kanban.frappe.get_meta")
	@patch("jarz_pos.api.kanban.frappe.get_all")
	def test_get_kanban_invoices_combines_branch_scope_with_search(
		self,
		mock_get_all,
		mock_get_meta,
		_mock_profiles,
		_mock_states,
		_mock_customer_matches,
		_mock_receipts,
		_mock_sort,
	):
		"""Kanban invoice query should combine enforced branches with search OR filters."""

		class _MetaStub:
			def get_field(self, fieldname):
				return fieldname == "custom_kanban_profile"

		captured = {}

		def _fake_get_all(doctype, **kwargs):
			if doctype == "Sales Invoice":
				captured.update(kwargs)
			return []

		mock_get_all.side_effect = _fake_get_all
		mock_get_meta.return_value = _MetaStub()

		result = get_kanban_invoices({"searchTerm": "Ali", "branches": ["Main"]})

		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertEqual(
			captured["filters"]["custom_kanban_profile"],
			["in", ["Main"]],
			"Search should still respect enforced branch scope",
		)
		self.assertIn(
			{"name": ["like", "%Ali%"]},
			captured["or_filters"],
			"Search should include invoice name matching",
		)
		self.assertIn(
			{"customer": ["in", ["CUST-1"]]},
			captured["or_filters"],
			"Search should include matched customer ids",
		)

	def test_get_address_details(self):
		"""Test the get_address_details utility function."""
		# Test empty address
		result = get_address_details(None)
		self.assertEqual(result, "", "Should return empty string for None")

		# Additional tests would require creating a test address document

	def test_format_invoice_data(self):
		"""Test the format_invoice_data utility function."""
		# This test requires a mock invoice object, which is more complex to set up
		pass

	def test_get_kanban_invoices(self):
		"""Test the get_kanban_invoices endpoint."""
		result = get_kanban_invoices()
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("data", result, "Should include data key")
		self.assertIsInstance(result["data"], dict, "Invoices data should be a dictionary")

	def test_get_invoice_details_validation(self):
		"""Test the get_invoice_details with non-existent invoice."""
		# Test with non-existent invoice
		try:
			result = get_invoice_details(invoice_id="NON_EXISTENT_INV")
			# If it doesn't raise, verify it returns error structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Expected to fail with non-existent invoice
			pass

	def test_get_kanban_filters(self):
		"""Test the get_kanban_filters endpoint."""
		result = get_kanban_filters()
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("customers", result, "Should include customers key")
		self.assertIn("states", result, "Should include states key")

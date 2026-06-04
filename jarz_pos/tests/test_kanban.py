"""Tests for the kanban API module.
This module contains tests for the kanban API endpoints.
"""

import unittest
from unittest.mock import patch
import frappe
from jarz_pos.api.kanban import (
	_build_invoice_search_or_filters,
	_sort_kanban_columns,
	get_kanban_columns,
	get_kanban_invoices,
	get_invoice_details,
	get_invoice_notes,
	add_invoice_note,
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

	def test_sort_kanban_columns_orders_received_by_posting_datetime_desc(self):
		"""Received should use newest posting datetime first, not delivery slot ordering."""
		data = {
			"received": [
				{
					"name": "INV-EARLY",
					"posting_date": "2026-06-01",
					"posting_time": "09:15:00",
					"creation": "2026-06-01 09:10:00",
					"delivery_date": "2026-06-03",
					"delivery_time_from": "08:00:00",
				},
				{
					"name": "INV-LATE",
					"posting_date": "2026-06-01",
					"posting_time": "18:45:00",
					"creation": "2026-06-01 18:40:00",
					"delivery_date": "2026-06-01",
					"delivery_time_from": "07:00:00",
				},
			],
			"in_progress": [],
		}

		result = _sort_kanban_columns(data)

		self.assertEqual(
			[card["name"] for card in result["received"]],
			["INV-LATE", "INV-EARLY"],
			"Received should sort by posting datetime descending",
		)

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

	@patch("jarz_pos.api.kanban._serialize_invoice_note_row", side_effect=lambda row: row)
	@patch("jarz_pos.api.kanban._ensure_invoice_detail_access")
	@patch("jarz_pos.api.kanban.frappe.get_doc")
	@patch("jarz_pos.api.kanban.frappe.get_all")
	@patch("jarz_pos.api.kanban.frappe.has_permission")
	def test_get_invoice_notes_returns_custom_notes_only(
		self,
		_mock_permission,
		mock_get_all,
		mock_get_doc,
		_mock_access,
		_mock_serialize,
	):
		"""Invoice notes endpoint should return only Jarz Invoice Note rows."""

		mock_get_doc.return_value = frappe._dict(name="ACC-SINV-0001")
		mock_get_all.return_value = [
			{
				"name": "JIN-00001",
				"sales_invoice": "ACC-SINV-0001",
				"note": "Call customer first",
				"added_by": "tester@example.com",
				"added_by_full_name": "Tester",
				"added_on": "2026-06-04 10:00:00",
			}
		]

		result = get_invoice_notes("ACC-SINV-0001")

		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("note_count"), 1)
		self.assertEqual(result["data"][0]["name"], "JIN-00001")
		mock_get_all.assert_called_once()
		self.assertEqual(mock_get_all.call_args.kwargs["filters"]["sales_invoice"], "ACC-SINV-0001")

	@patch("jarz_pos.api.kanban.frappe.publish_realtime")
	@patch("jarz_pos.api.kanban.frappe.utils.now", return_value="2026-06-04 11:00:00")
	@patch("jarz_pos.api.kanban._get_invoice_note_counts", return_value={"ACC-SINV-0001": 2})
	@patch("jarz_pos.api.kanban._serialize_invoice_note_row", side_effect=lambda row: row)
	@patch("jarz_pos.api.kanban.frappe.db.commit")
	@patch("jarz_pos.api.kanban._ensure_invoice_detail_access")
	@patch("jarz_pos.api.kanban.frappe.get_doc")
	@patch("jarz_pos.api.kanban.frappe.has_permission")
	def test_add_invoice_note_creates_custom_note_and_broadcasts_count(
		self,
		_mock_permission,
		mock_get_doc,
		_mock_access,
		_mock_commit,
		_mock_serialize,
		mock_note_counts,
		_mock_now,
		mock_publish_realtime,
	):
		"""Adding a note should create Jarz Invoice Note and emit invoice_note_added."""

		invoice_doc = frappe._dict(name="ACC-SINV-0001")
		note_doc = frappe._dict(
			name="JIN-00002",
			sales_invoice="ACC-SINV-0001",
			pos_profile="Main",
			note="Leave at reception",
			added_by="tester@example.com",
			added_by_full_name="Tester",
			added_on="2026-06-04 10:59:00",
		)

		def _fake_get_doc(*args, **kwargs):
			if args and args[0] == "Sales Invoice":
				return invoice_doc
			if args and isinstance(args[0], dict):
				note_doc.insert = lambda ignore_permissions=True: note_doc
				return note_doc
			raise AssertionError(f"Unexpected get_doc args: {args!r} {kwargs!r}")

		mock_get_doc.side_effect = _fake_get_doc

		result = add_invoice_note("ACC-SINV-0001", "  Leave at reception  ")

		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("note_count"), 2)
		self.assertEqual(result["data"]["note"], "Leave at reception")
		mock_note_counts.assert_called_once_with(["ACC-SINV-0001"])
		mock_publish_realtime.assert_called_once()
		payload = mock_publish_realtime.call_args.args[1]
		self.assertEqual(payload["event"], "invoice_note_added")
		self.assertEqual(payload["invoice_id"], "ACC-SINV-0001")
		self.assertEqual(payload["note_count"], 2)

	@patch("jarz_pos.api.kanban._sort_kanban_columns", side_effect=lambda data: data)
	@patch("jarz_pos.api.kanban._get_invoice_note_counts", return_value={"ACC-SINV-0001": 3})
	@patch("jarz_pos.api.kanban._get_active_payment_receipt_map", return_value={})
	@patch("jarz_pos.api.kanban._get_state_field_options", return_value=["Received"])
	@patch("jarz_pos.api.kanban._get_current_user_pos_profiles", return_value=["Main"])
	@patch("jarz_pos.api.kanban._resolve_customer_phone", return_value="")
	@patch("jarz_pos.api.kanban._get_territory_shipping_values", return_value={"income": 0.0, "expense": 0.0})
	@patch("jarz_pos.api.kanban._is_pickup_invoice", return_value=False)
	@patch("jarz_pos.api.kanban.frappe.db.exists", return_value=False)
	@patch("jarz_pos.api.kanban.frappe.get_meta")
	@patch("jarz_pos.api.kanban.frappe.get_all")
	def test_get_kanban_invoices_includes_note_count_on_cards(
		self,
		mock_get_all,
		mock_get_meta,
		_mock_exists,
		_mock_pickup,
		_mock_shipping,
		_mock_phone,
		_mock_profiles,
		_mock_states,
		_mock_receipts,
		_mock_note_counts,
		_mock_sort,
	):
		"""Kanban board payload should surface note_count for each invoice card."""

		class _MetaStub:
			def get_field(self, fieldname):
				return fieldname == "custom_kanban_profile"

		mock_get_meta.return_value = _MetaStub()

		invoice = frappe._dict(
			name="ACC-SINV-0001",
			customer_name="Alice",
			customer="CUST-1",
			territory="Metro",
			status="Unpaid",
			posting_date="2026-06-04",
			posting_time="09:00:00",
			creation="2026-06-04 08:55:00",
			grand_total=100,
			net_total=90,
			total_taxes_and_charges=10,
			outstanding_amount=100,
			custom_sales_invoice_state="Received",
			docstatus=1,
			is_return=0,
			modified="2026-06-04 09:00:00",
		)

		def _fake_get_all(doctype, **kwargs):
			if doctype == "Sales Invoice":
				return [invoice]
			return []

		mock_get_all.side_effect = _fake_get_all

		result = get_kanban_invoices()

		self.assertTrue(result.get("success"))
		self.assertEqual(result["data"]["received"][0]["note_count"], 3)

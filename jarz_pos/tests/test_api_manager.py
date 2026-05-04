"""Tests for manager API endpoints.

This module tests manager dashboard and order management API endpoints.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _FakeInvoice:
	"""Minimal Sales Invoice stub for manager API tests."""

	def __init__(self, **data):
		self._data = dict(data)
		self.name = self._data["name"]

	def get(self, key, default=None):
		return self._data.get(key, default)

	def reload(self):
		return None


class TestManagerAPI(unittest.TestCase):
	"""Test class for Manager API functionality."""

	def test_get_manager_dashboard_summary_structure(self):
		"""Test that get_manager_dashboard_summary returns correct structure."""
		from jarz_pos.api.manager import get_manager_dashboard_summary

		result = get_manager_dashboard_summary()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("branches", result, "Should include branches")
		self.assertIn("total_balance", result, "Should include total_balance")

		# Verify branches structure
		self.assertIsInstance(result["branches"], list, "Branches should be a list")

		# If there are branches, verify their structure
		for branch in result["branches"]:
			self.assertIn("name", branch, "Branch should have name")
			self.assertIn("title", branch, "Branch should have title")
			self.assertIn("cash_account", branch, "Branch should have cash_account")
			self.assertIn("balance", branch, "Branch should have balance")

	def test_get_manager_orders_structure(self):
		"""Test that get_manager_orders returns correct structure."""
		from jarz_pos.api.manager import get_manager_orders

		result = get_manager_orders()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("invoices", result, "Should include invoices")

		# Verify invoices structure
		self.assertIsInstance(result["invoices"], list, "Invoices should be a list")

	def test_get_manager_orders_limit(self):
		"""Test that get_manager_orders respects limit parameter."""
		from jarz_pos.api.manager import get_manager_orders

		# Test with small limit
		result = get_manager_orders(limit=5)

		# Should not exceed limit
		self.assertLessEqual(len(result["invoices"]), 5, "Should not exceed specified limit of 5")

	def test_get_manager_orders_invoice_structure(self):
		"""Test individual invoice structure in get_manager_orders."""
		from jarz_pos.api.manager import get_manager_orders

		result = get_manager_orders(limit=1)

		# If there are invoices, verify their structure
		if result["invoices"]:
			invoice = result["invoices"][0]
			self.assertIn("name", invoice, "Invoice should have name")
			self.assertIn("customer", invoice, "Invoice should have customer")
			self.assertIn("customer_name", invoice, "Invoice should have customer_name")
			self.assertIn("posting_date", invoice, "Invoice should have posting_date")
			self.assertIn("posting_time", invoice, "Invoice should have posting_time")
			self.assertIn("grand_total", invoice, "Invoice should have grand_total")
			self.assertIn("net_total", invoice, "Invoice should have net_total")
			self.assertIn("status", invoice, "Invoice should have status")
			self.assertIn("branch", invoice, "Invoice should have branch")

	def test_get_manager_states_structure(self):
		"""Test that get_manager_states returns correct structure."""
		from jarz_pos.api.manager import get_manager_states

		result = get_manager_states()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("states", result, "Should include states")

		# Verify states structure
		self.assertIsInstance(result["states"], list, "States should be a list")

	def test_update_invoice_branch_validation(self):
		"""Test that update_invoice_branch validates inputs."""
		from jarz_pos.api.manager import update_invoice_branch

		with patch("jarz_pos.api.manager._ensure_manager_dashboard_access"):
			# Test with empty invoice_id
			result = update_invoice_branch(invoice_id="", new_branch="Test Branch")

			# Should return error
			self.assertFalse(result.get("success"), "Should return success=False for empty invoice_id")
			self.assertIn("error", result, "Should include error message")

			# Test with empty new_branch
			result = update_invoice_branch(invoice_id="TEST-INV-001", new_branch="")

			# Should return error
			self.assertFalse(result.get("success"), "Should return success=False for empty new_branch")
			self.assertIn("error", result, "Should include error message")

	def test_update_invoice_branch_updates_custom_kanban_profile_only(self):
		"""Submitted invoice reassignment should update custom_kanban_profile and publish refresh events."""
		from jarz_pos.api.manager import update_invoice_branch

		invoice = _FakeInvoice(
			name="INV-001",
			docstatus=1,
			is_pos=1,
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="In Progress",
			status="Paid",
			customer="CUST-001",
			customer_name="Test Customer",
			grand_total=250.0,
			posting_date="2026-05-03",
			posting_time="12:00:00",
		)

		meta = MagicMock()
		meta.get_field.side_effect = lambda fieldname: object() if fieldname in {
			"custom_kanban_profile",
			"custom_sales_invoice_state",
			"custom_acceptance_status",
			"custom_accepted_by",
			"custom_accepted_on",
		} else None

		mock_frappe = MagicMock()
		mock_frappe.flags = SimpleNamespace(ignore_permissions=False, ignore_validate=False)
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.utils.now.return_value = "2026-05-03 12:30:00"
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: 0 if (doctype, name, field) == ("POS Profile", "Nasr city", "disabled") else None
		)

		def _set_value(_doctype, _name, field, value, update_modified=True):
			invoice._data[field] = value

		mock_frappe.db.set_value.side_effect = _set_value
		mock_frappe.get_doc.side_effect = lambda doctype, name: invoice if doctype == "Sales Invoice" else MagicMock()
		mock_frappe.get_meta.return_value = meta

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"), \
			 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Nasr city"]), \
			 patch("jarz_pos.api.manager.notify_invoice_reassignment") as mock_notify, \
			 patch("jarz_pos.api.manager._get_state_field_options", return_value=["Received", "In Progress", "Ready"]):
			result = update_invoice_branch(invoice_id="INV-001", new_branch="Nasr city")

		self.assertTrue(result.get("success"))
		self.assertEqual(invoice.get("pos_profile"), "Dokki")
		self.assertEqual(invoice.get("custom_kanban_profile"), "Nasr city")
		self.assertEqual(invoice.get("custom_sales_invoice_state"), "Received")
		self.assertEqual(invoice.get("custom_acceptance_status"), "Pending")
		self.assertIsNone(invoice.get("custom_accepted_by"))
		self.assertIsNone(invoice.get("custom_accepted_on"))
		self.assertNotIn("pos_profile", [call.args[2] for call in mock_frappe.db.set_value.call_args_list])
		self.assertTrue(all(call.kwargs.get("update_modified") is True for call in mock_frappe.db.set_value.call_args_list))
		mock_notify.assert_called_once_with(invoice, "Nasr city")

		events = [call.args[0] for call in mock_frappe.publish_realtime.call_args_list]
		self.assertEqual(events, ["jarz_pos_invoice_state_change", "kanban_update"])
		payload = mock_frappe.publish_realtime.call_args_list[0].args[1]
		self.assertEqual(payload["event"], "invoice_reassigned")
		self.assertEqual(payload["old_profile"], "Dokki")
		self.assertEqual(payload["new_profile"], "Nasr city")
		self.assertEqual(payload["pos_profile"], "Dokki")
		self.assertEqual(payload["kanban_profile"], "Nasr city")
		self.assertIsNone(payload["old_state_key"])
		self.assertEqual(payload["new_state_key"], "received")
		self.assertTrue(payload["force_refresh"])

	def test_update_invoice_branch_rejects_disabled_target_profile(self):
		"""Disabled POS Profiles should fail with a specific validation error."""
		from jarz_pos.api.manager import update_invoice_branch

		mock_frappe = MagicMock()
		mock_frappe.flags = SimpleNamespace(ignore_permissions=False, ignore_validate=False)
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: 1 if (doctype, name, field) == ("POS Profile", "Closed Branch", "disabled") else None
		)

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"):
			result = update_invoice_branch(invoice_id="INV-001", new_branch="Closed Branch")

		self.assertFalse(result.get("success"))
		self.assertIn("disabled", result.get("error", "").lower())

	def test_update_cancelled_invoice_status_fields_rejects_non_cancelled_invoice(self):
		"""Only cancelled Sales Invoices should go through the correction endpoint."""
		from jarz_pos.api.manager import update_cancelled_invoice_status_fields

		invoice = _FakeInvoice(
			name="INV-OPEN-001",
			docstatus=1,
			custom_sales_invoice_state="Ready",
			custom_acceptance_status="Pending",
		)

		mock_frappe = MagicMock()
		mock_frappe.get_doc.return_value = invoice
		mock_frappe.has_permission.return_value = True

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
				 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"):
			result = update_cancelled_invoice_status_fields(
				invoice_id="INV-OPEN-001",
				sales_invoice_state="Cancelled",
			)

		self.assertFalse(result.get("success"))
		self.assertIn("cancelled", result.get("error", "").lower())

	def test_update_cancelled_invoice_status_fields_updates_state_and_acceptance(self):
		"""Managers can correct the Jarz workflow fields on cancelled invoices only."""
		from jarz_pos.api.manager import update_cancelled_invoice_status_fields

		invoice = _FakeInvoice(
			name="INV-CANCELLED-001",
			docstatus=2,
			custom_sales_invoice_state="Cancelled",
			sales_invoice_state="Cancelled",
			custom_acceptance_status="Pending",
			custom_accepted_by=None,
			custom_accepted_on=None,
		)
		invoice.add_comment = MagicMock()

		def _get_field(fieldname):
			if fieldname == "custom_sales_invoice_state":
				return SimpleNamespace(options="Received\nIn Progress\nReady\nOut for Delivery\nDelivered\nCancelled")
			if fieldname == "sales_invoice_state":
				return SimpleNamespace(options="Received\nIn Progress\nReady\nOut for Delivery\nDelivered\nCancelled")
			if fieldname == "custom_acceptance_status":
				return SimpleNamespace(options="Pending\nAccepted")
			if fieldname in {"custom_accepted_by", "custom_accepted_on"}:
				return object()
			return None

		meta = MagicMock()
		meta.get_field.side_effect = _get_field

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.utils.now_datetime.return_value = "2026-05-04 15:00:00"
		mock_frappe.get_doc.return_value = invoice
		mock_frappe.get_meta.return_value = meta
		mock_frappe.has_permission.return_value = True

		def _set_value(_doctype, _name, values, update_modified=True):
			invoice._data.update(values)

		mock_frappe.db.set_value.side_effect = _set_value

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
				 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"):
			result = update_cancelled_invoice_status_fields(
				invoice_id="INV-CANCELLED-001",
				sales_invoice_state="Ready",
				acceptance_status="Accepted",
			)

		self.assertTrue(result.get("success"))
		self.assertEqual(invoice.get("custom_sales_invoice_state"), "Ready")
		self.assertEqual(invoice.get("sales_invoice_state"), "Ready")
		self.assertEqual(invoice.get("custom_acceptance_status"), "Accepted")
		self.assertEqual(invoice.get("custom_accepted_by"), "manager@example.com")
		self.assertEqual(invoice.get("custom_accepted_on"), "2026-05-04 15:00:00")
		mock_frappe.db.set_value.assert_called_once()
		invoice.add_comment.assert_called_once()


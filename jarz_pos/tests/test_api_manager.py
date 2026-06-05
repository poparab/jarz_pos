"""Tests for manager API endpoints.

This module tests manager dashboard and order management API endpoints.
"""

from contextlib import nullcontext
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _FakeInvoice:
	"""Minimal Sales Invoice stub for manager API tests."""

	def __init__(self, **data):
		self._data = dict(data)
		self.name = self._data["name"]

	def __getattr__(self, key):
		if key in self._data:
			return self._data[key]
		raise AttributeError(key)

	def get(self, key, default=None):
		return self._data.get(key, default)

	def reload(self):
		return None


def _raise_frappe(message, exc=None, title=None):
	if exc and isinstance(exc, type) and issubclass(exc, Exception):
		raise exc(message)
	raise Exception(message)


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

	def test_get_transfer_target_branches_returns_assigned_active_profiles_for_staff(self):
		"""Assigned staff should only receive active POS Profiles linked to their user."""
		from jarz_pos.api.manager import get_transfer_target_branches

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"

		def _get_all(doctype, **kwargs):
			if doctype == "Has Role":
				return [{"role": "Sales User"}]
			if doctype == "POS Profile User":
				return ["Dokki", "Closed Branch", "Nasr city"]
			if doctype == "POS Profile":
				return ["Dokki", "Nasr city"]
			return []

		mock_frappe.get_all.side_effect = _get_all

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			result = get_transfer_target_branches()

		self.assertEqual(
			result,
			{
				"success": True,
				"branches": [
					{"name": "Dokki", "title": "Dokki"},
					{"name": "Nasr city", "title": "Nasr city"},
				],
			},
		)

	def test_get_transfer_target_branches_returns_minimal_shape_for_admin_scope(self):
		"""Admin-scoped users should receive the same lightweight picker structure."""
		from jarz_pos.api.manager import get_transfer_target_branches

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"

		def _get_all(doctype, **kwargs):
			if doctype == "Has Role":
				return [{"role": "System Manager"}]
			if doctype == "POS Profile":
				return ["Dokki", "Nasr city"]
			return []

		mock_frappe.get_all.side_effect = _get_all

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			result = get_transfer_target_branches()

		self.assertTrue(result.get("success"))
		self.assertEqual(
			result.get("branches"),
			[
				{"name": "Dokki", "title": "Dokki"},
				{"name": "Nasr city", "title": "Nasr city"},
			],
		)
		for branch in result["branches"]:
			self.assertEqual(set(branch.keys()), {"name", "title"})

	def test_get_transfer_target_branches_does_not_require_manager_dashboard_access(self):
		"""Transfer branch picker should work for assigned staff without manager dashboard access."""
		from jarz_pos.api.manager import get_transfer_target_branches

		with patch("jarz_pos.api.manager._has_manager_dashboard_access", return_value=False), \
				 patch("jarz_pos.api.manager._ensure_manager_dashboard_access", side_effect=AssertionError("dashboard gate should not run")), \
				 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki"]):
			result = get_transfer_target_branches()

		self.assertEqual(
			result,
			{"success": True, "branches": [{"name": "Dokki", "title": "Dokki"}]},
		)

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

	def test_get_invoice_amendment_eligibility_rejects_sales_partner_transactions(self):
		"""Amendment flow must stop once Sales Partner settlement artifacts exist."""
		from jarz_pos.api.manager import get_invoice_amendment_eligibility

		invoice = _FakeInvoice(
			name="INV-SPT-001",
			docstatus=1,
			is_return=0,
			custom_sales_invoice_state="Ready",
		)

		mock_frappe = MagicMock()
		mock_frappe.get_all.side_effect = lambda doctype, **kwargs: {
			"Delivery Note Item": [],
			"Delivery Trip Invoice": [],
			"Courier Transaction": [],
			"Sales Partner Transactions": ["SPT-001"],
			"Journal Entry": [],
			"Journal Entry Account": [],
		}.get(doctype, [])
		mock_frappe.db.get_value.return_value = None

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			result = get_invoice_amendment_eligibility(invoice)

		self.assertFalse(result.get("can_amend"))
		self.assertEqual(result.get("amendment_block_code"), "sales_partner_transaction_exists")
		self.assertEqual(result.get("sales_partner_transactions"), ["SPT-001"])

	def test_get_invoice_amendment_eligibility_rejects_submitted_journal_entries(self):
		"""Amendment flow must stop once settlement Journal Entries exist."""
		from jarz_pos.api.manager import get_invoice_amendment_eligibility

		invoice = _FakeInvoice(
			name="INV-JE-001",
			docstatus=1,
			is_return=0,
			custom_sales_invoice_state="Ready",
		)

		def _get_all(doctype, **kwargs):
			if doctype == "Journal Entry" and kwargs.get("filters", {}).get("docstatus") == 1:
				return ["JE-001"]
			return []

		mock_frappe = MagicMock()
		mock_frappe.get_all.side_effect = _get_all
		mock_frappe.db.get_value.return_value = None

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			result = get_invoice_amendment_eligibility(invoice)

		self.assertFalse(result.get("can_amend"))
		self.assertEqual(result.get("amendment_block_code"), "journal_entry_exists")
		self.assertEqual(result.get("journal_entries"), ["JE-001"])

	def test_submit_invoice_amendment_uses_queueable_job_path(self):
		"""Public amendment submit should execute the queueable job entrypoint with a stable request id."""
		from jarz_pos.api.manager import submit_invoice_amendment

		source_invoice = _FakeInvoice(
			name="INV-AMD-001",
			docstatus=1,
			is_return=0,
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
			sales_partner=None,
			custom_payment_method="Cash",
		)

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_roles.return_value = ["JARZ Manager"]
		mock_frappe.get_doc.return_value = source_invoice
		mock_frappe.has_permission.return_value = True
		mock_frappe.enqueue.return_value = {"success": True, "request_id": "amd-INV-AMD-001-1234"}

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"), \
			 patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None), \
			 patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}):
			result = submit_invoice_amendment(invoice_id="INV-AMD-001", cart_json="[]")

		self.assertTrue(result.get("success"))
		self.assertTrue(mock_frappe.enqueue.called)
		self.assertEqual(mock_frappe.enqueue.call_args.args[0], "jarz_pos.api.manager._run_invoice_amendment_job")
		self.assertEqual(mock_frappe.enqueue.call_args.kwargs["queue"], "short")
		self.assertTrue(mock_frappe.enqueue.call_args.kwargs["now"])
		self.assertTrue(mock_frappe.enqueue.call_args.kwargs["job_id"].startswith("amd-INV-AMD-001-"))

	def test_submit_invoice_amendment_normalizes_zero_shipping_override(self):
		"""Zero-shipping override should force suppression flags on the amendment job."""
		from jarz_pos.api.manager import submit_invoice_amendment

		source_invoice = _FakeInvoice(
			name="INV-AMD-ZERO-001",
			docstatus=1,
			is_return=0,
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
			sales_partner=None,
			custom_payment_method="Cash",
		)

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_roles.return_value = ["JARZ Manager"]
		mock_frappe.get_doc.return_value = source_invoice
		mock_frappe.has_permission.return_value = True
		mock_frappe.enqueue.return_value = {"success": True, "request_id": "amd-INV-AMD-ZERO-001-1234"}

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"), \
			 patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None), \
			 patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}):
			result = submit_invoice_amendment(
				invoice_id="INV-AMD-ZERO-001",
				cart_json="[]",
				zero_shipping_override=1,
			)

		self.assertTrue(result.get("success"))
		self.assertTrue(mock_frappe.enqueue.called)
		self.assertTrue(mock_frappe.enqueue.call_args.kwargs["suppress_shipping_income"])
		self.assertTrue(mock_frappe.enqueue.call_args.kwargs["suppress_legacy_delivery_charges"])
		self.assertEqual(mock_frappe.enqueue.call_args.kwargs["zero_shipping_override"], 1)

	def test_submit_invoice_amendment_returns_existing_replacement_idempotently(self):
		"""Repeated submit requests should return the existing replacement invoice instead of reprocessing."""
		from jarz_pos.api.manager import submit_invoice_amendment

		source_invoice = _FakeInvoice(
			name="INV-AMD-002",
			docstatus=1,
			is_return=0,
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
		)
		replacement_invoice = _FakeInvoice(name="INV-AMD-002-1")

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_roles.return_value = ["JARZ Manager"]
		mock_frappe.has_permission.return_value = True
		mock_frappe.get_doc.side_effect = lambda doctype, name: source_invoice if name == "INV-AMD-002" else replacement_invoice

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"), \
			 patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value="INV-AMD-002-1"), \
			 patch("jarz_pos.api.manager.format_invoice_data", return_value={"name": "INV-AMD-002-1"}):
			result = submit_invoice_amendment(invoice_id="INV-AMD-002", cart_json="[]")

		self.assertTrue(result.get("success"))
		self.assertTrue(result.get("already_processed"))
		self.assertEqual(result.get("replacement_invoice_id"), "INV-AMD-002-1")
		mock_frappe.enqueue.assert_not_called()

	def test_submit_invoice_amendment_allows_staff_with_assigned_pos_profile(self):
		"""Staff linked to the invoice POS Profile should be able to submit amendments."""
		from jarz_pos.api.manager import submit_invoice_amendment

		source_invoice = _FakeInvoice(
			name="INV-AMD-STAFF-001",
			docstatus=1,
			is_return=0,
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
			sales_partner=None,
			custom_payment_method="Cash",
		)

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.get_roles.return_value = ["Sales User"]
		mock_frappe.get_doc.return_value = source_invoice
		mock_frappe.enqueue.return_value = {"success": True, "request_id": "amd-INV-AMD-STAFF-001-1234"}

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
				 patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None), \
				 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki"]), \
				 patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}):
			result = submit_invoice_amendment(
				invoice_id="INV-AMD-STAFF-001",
				cart_json="[]",
				pos_profile_name="Dokki",
			)

		self.assertTrue(result.get("success"))
		mock_frappe.enqueue.assert_called_once()
		mock_frappe.has_permission.assert_not_called()

	def test_submit_invoice_amendment_rejects_staff_without_profile_access(self):
		"""Staff should not amend invoices outside their assigned POS Profiles."""
		from jarz_pos.api.manager import submit_invoice_amendment

		source_invoice = _FakeInvoice(
			name="INV-AMD-STAFF-002",
			docstatus=1,
			is_return=0,
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
		)

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.get_roles.return_value = ["Sales User"]
		mock_frappe.get_doc.return_value = source_invoice
		mock_frappe.PermissionError = PermissionError
		mock_frappe.throw.side_effect = PermissionError("Not permitted")

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
				 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Nasr city"]):
			with self.assertRaises(PermissionError):
				submit_invoice_amendment(
					invoice_id="INV-AMD-STAFF-002",
					cart_json="[]",
					pos_profile_name="Dokki",
				)

		mock_frappe.enqueue.assert_not_called()

	def test_run_invoice_amendment_job_cancels_payment_entries_before_recreate(self):
		"""The amendment job should cancel linked payment entries before recreating the replacement invoice."""
		from jarz_pos.api.manager import _run_invoice_amendment_job

		source_invoice = _FakeInvoice(
			name="INV-AMD-003",
			docstatus=1,
			is_return=0,
			customer="CUST-003",
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
			custom_payment_method="Cash",
			woo_order_id=14500,
		)
		source_invoice.flags = SimpleNamespace(ignore_permissions=False, ignore_woo_outbound=False)
		source_invoice.cancel = MagicMock()
		source_invoice.add_comment = MagicMock()

		replacement_invoice = _FakeInvoice(name="INV-AMD-003-1")
		replacement_invoice.add_comment = MagicMock()

		payment_entry = MagicMock()
		payment_entry.name = "PE-001"
		payment_entry.get.side_effect = lambda fieldname, default=None: 1 if fieldname == "docstatus" else default
		payment_entry.cancel = MagicMock()
		payment_entry.flags = SimpleNamespace(ignore_permissions=False)

		meta = MagicMock()
		meta.get_field.return_value = None

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.local.site = "frontend"
		mock_frappe.get_doc.side_effect = lambda doctype, name: {
			("Sales Invoice", "INV-AMD-003"): source_invoice,
			("Sales Invoice", "INV-AMD-003-1"): replacement_invoice,
			("Payment Entry", "PE-001"): payment_entry,
		}[(doctype, name)]
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.get_meta.return_value = meta
		mock_frappe.parse_json.return_value = [{"item_code": "ITEM-001", "qty": 1, "rate": 10}]
		mock_frappe.db.savepoint = MagicMock()
		mock_frappe.db.rollback = MagicMock()
		mock_frappe.db.sql.return_value = [[1]]
		mock_frappe.db.set_value = MagicMock()

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None), \
			 patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}), \
			 patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None), \
			 patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=["PE-001"]), \
			 patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", return_value=nullcontext()), \
			 patch("jarz_pos.api.manager._create_amendment_invoice", return_value={"invoice_name": "INV-AMD-003-1"}) as mock_create_invoice, \
			 patch("jarz_pos.api.manager.format_invoice_data", return_value={"name": "INV-AMD-003-1"}):
			result = _run_invoice_amendment_job(
				invoice_id="INV-AMD-003",
				request_id="amd-INV-AMD-003-1234",
				cart_json='[{"item_code":"ITEM-001","qty":1,"rate":10}]',
			)

		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("cancelled_payment_entries"), ["PE-001"])
		payment_entry.cancel.assert_called_once()
		source_invoice.cancel.assert_called_once()
		self.assertEqual(mock_create_invoice.call_args.kwargs["amended_from"], "INV-AMD-003")
		self.assertEqual(mock_create_invoice.call_args.kwargs["woo_order_id"], 14500)

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
		"""Submitted invoice reassignment should move operational branch and stock rows together."""
		from jarz_pos.api.manager import update_invoice_branch

		item_row_1 = SimpleNamespace(name="SII-001", item_code="ITEM-001", warehouse="Stores - Dokki")
		item_row_2 = SimpleNamespace(name="SII-002", item_code="SERVICE-001", warehouse="")

		invoice = _FakeInvoice(
			name="INV-001",
			docstatus=1,
			is_pos=1,
			company="Jarz",
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="In Progress",
			status="Paid",
			customer="CUST-001",
			customer_name="Test Customer",
			grand_total=250.0,
			posting_date="2026-05-03",
			posting_time="12:00:00",
			items=[item_row_1, item_row_2],
		)
		invoice.add_comment = MagicMock()

		meta = MagicMock()
		meta.get_field.side_effect = lambda fieldname: object() if fieldname in {
			"custom_kanban_profile",
			"set_warehouse",
			"custom_sales_invoice_state",
			"custom_acceptance_status",
			"custom_accepted_by",
			"custom_accepted_on",
		} else None

		mock_frappe = MagicMock()
		mock_frappe.flags = SimpleNamespace(ignore_permissions=False, ignore_validate=False)
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_roles.return_value = ["JARZ Manager"]
		mock_frappe.utils.now.return_value = "2026-05-03 12:30:00"
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.get_all.return_value = []
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "disabled"): 0,
				("POS Profile", "Nasr city", "warehouse"): "Stores - Nasr city",
				("Warehouse", "Stores - Nasr city", "company"): "Jarz",
				("Item", "ITEM-001", "is_stock_item"): 1,
				("Item", "SERVICE-001", "is_stock_item"): 0,
			}.get((doctype, name, field))
		)

		def _set_value(_doctype, _name, field, value, update_modified=True):
			if _doctype == "Sales Invoice":
				invoice._data[field] = value
			if _doctype == "Sales Invoice Item" and _name == "SII-001" and field == "warehouse":
				item_row_1.warehouse = value
			if _doctype == "Sales Invoice Item" and _name == "SII-002" and field == "warehouse":
				item_row_2.warehouse = value

		mock_frappe.db.set_value.side_effect = _set_value
		mock_frappe.get_doc.side_effect = lambda doctype, name: invoice if doctype == "Sales Invoice" else MagicMock()
		mock_frappe.get_meta.return_value = meta
		mock_frappe.has_permission.return_value = True

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"), \
			 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Nasr city"]), \
			 patch("jarz_pos.api.manager.notify_invoice_reassignment") as mock_notify, \
			 patch("jarz_pos.api.manager._get_state_field_options", return_value=["Received", "In Progress", "Ready"]):
			result = update_invoice_branch(invoice_id="INV-001", new_branch="Nasr city")

		self.assertTrue(result.get("success"))
		self.assertEqual(invoice.get("pos_profile"), "Dokki")
		self.assertEqual(invoice.get("custom_kanban_profile"), "Nasr city")
		self.assertEqual(invoice.get("set_warehouse"), "Stores - Nasr city")
		self.assertEqual(invoice.get("custom_sales_invoice_state"), "Received")
		self.assertEqual(invoice.get("custom_acceptance_status"), "Pending")
		self.assertIsNone(invoice.get("custom_accepted_by"))
		self.assertIsNone(invoice.get("custom_accepted_on"))
		self.assertEqual(item_row_1.warehouse, "Stores - Nasr city")
		self.assertEqual(item_row_2.warehouse, "")
		self.assertNotIn("pos_profile", [call.args[2] for call in mock_frappe.db.set_value.call_args_list])
		self.assertEqual(result.get("target_warehouse"), "Stores - Nasr city")
		invoice.add_comment.assert_called_once()
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

	def test_update_invoice_branch_allows_staff_with_assigned_profiles(self):
		"""Staff assigned to both source and target POS Profiles should be able to transfer invoices."""
		from jarz_pos.api.manager import update_invoice_branch

		item_row = SimpleNamespace(name="SII-020", item_code="ITEM-001", warehouse="Stores - Dokki")
		invoice = _FakeInvoice(
			name="INV-STAFF-TRANSFER-001",
			docstatus=1,
			is_pos=1,
			company="Jarz",
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
			items=[item_row],
		)
		invoice.add_comment = MagicMock()

		meta = MagicMock()
		meta.get_field.side_effect = lambda fieldname: object() if fieldname in {
			"custom_kanban_profile",
			"set_warehouse",
			"custom_sales_invoice_state",
			"custom_acceptance_status",
			"custom_accepted_by",
			"custom_accepted_on",
		} else None

		mock_frappe = MagicMock()
		mock_frappe.flags = SimpleNamespace(ignore_permissions=False, ignore_validate=False)
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.get_roles.return_value = ["Sales User"]
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.get_all.return_value = []
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "disabled"): 0,
				("POS Profile", "Nasr city", "warehouse"): "Stores - Nasr city",
				("Warehouse", "Stores - Nasr city", "company"): "Jarz",
				("Item", "ITEM-001", "is_stock_item"): 1,
			}.get((doctype, name, field))
		)

		def _set_value(_doctype, _name, field, value, update_modified=True):
			if _doctype == "Sales Invoice":
				invoice._data[field] = value
			if _doctype == "Sales Invoice Item" and _name == "SII-020" and field == "warehouse":
				item_row.warehouse = value

		mock_frappe.db.set_value.side_effect = _set_value
		mock_frappe.get_doc.side_effect = lambda doctype, name: invoice if doctype == "Sales Invoice" else MagicMock()
		mock_frappe.get_meta.return_value = meta

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
				 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki", "Nasr city"]), \
				 patch("jarz_pos.api.manager.notify_invoice_reassignment"), \
				 patch("jarz_pos.api.manager._get_state_field_options", return_value=["Received", "In Progress", "Ready"]):
			result = update_invoice_branch(invoice_id="INV-STAFF-TRANSFER-001", new_branch="Nasr city")

		self.assertTrue(result.get("success"))
		self.assertEqual(invoice.get("custom_kanban_profile"), "Nasr city")
		self.assertEqual(item_row.warehouse, "Stores - Nasr city")
		mock_frappe.has_permission.assert_not_called()

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

	def test_update_invoice_branch_rejects_target_profile_without_warehouse(self):
		"""Transfer must fail when the target POS Profile cannot provide a warehouse."""
		from jarz_pos.api.manager import update_invoice_branch

		invoice = _FakeInvoice(
			name="INV-002",
			docstatus=1,
			is_pos=1,
			company="Jarz",
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
			items=[SimpleNamespace(name="SII-010", item_code="ITEM-001", warehouse="Stores - Dokki")],
		)

		meta = MagicMock()
		meta.get_field.side_effect = lambda fieldname: object() if fieldname in {"custom_kanban_profile", "custom_sales_invoice_state"} else None

		mock_frappe = MagicMock()
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.get_roles.return_value = ["JARZ Manager"]
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.get_all.return_value = []
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "disabled"): 0,
				("POS Profile", "Nasr city", "warehouse"): None,
			}.get((doctype, name, field))
		)
		mock_frappe.get_doc.return_value = invoice
		mock_frappe.get_meta.return_value = meta
		mock_frappe.has_permission.return_value = True
		mock_frappe.ValidationError = Exception

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"), \
			 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Nasr city"]):
			result = update_invoice_branch(invoice_id="INV-002", new_branch="Nasr city")

		self.assertFalse(result.get("success"))
		self.assertIn("warehouse", result.get("error", "").lower())

	def test_update_invoice_branch_rejects_existing_delivery_note(self):
		"""Transfer must stop once a submitted Delivery Note already exists."""
		from jarz_pos.api.manager import update_invoice_branch

		invoice = _FakeInvoice(
			name="INV-003",
			docstatus=1,
			is_pos=1,
			company="Jarz",
			pos_profile="Dokki",
			custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready",
			items=[SimpleNamespace(name="SII-011", item_code="ITEM-001", warehouse="Stores - Dokki")],
		)

		meta = MagicMock()
		meta.get_field.side_effect = lambda fieldname: object() if fieldname == "custom_kanban_profile" else None

		mock_frappe = MagicMock()
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.get_roles.return_value = ["JARZ Manager"]
		mock_frappe.db.exists.side_effect = lambda doctype, name: True
		mock_frappe.get_all.return_value = ["DN-001"]
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "disabled"): 0,
			}.get((doctype, name, field))
		)
		mock_frappe.get_doc.return_value = invoice
		mock_frappe.get_meta.return_value = meta
		mock_frappe.has_permission.return_value = True

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"), \
			 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Nasr city"]):
			result = update_invoice_branch(invoice_id="INV-003", new_branch="Nasr city")

		self.assertFalse(result.get("success"))
		self.assertIn("delivery note", result.get("error", "").lower())

	def test_get_invoice_warehouse_alignment_report_lists_misaligned_invoices(self):
		"""Admin report should surface submitted invoices whose stock rows still point to the old warehouse."""
		from jarz_pos.api.manager import get_invoice_warehouse_alignment_report

		invoice = _FakeInvoice(
			name="INV-004",
			docstatus=1,
			is_pos=1,
			company="Jarz",
			customer="CUST-004",
			pos_profile="Dokki",
			custom_kanban_profile="Nasr city",
			items=[SimpleNamespace(name="SII-020", item_code="ITEM-001", warehouse="Stores - Dokki")],
		)

		mock_frappe = MagicMock()
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.get_roles.return_value = ["System Manager"]
		mock_frappe.has_permission.return_value = True
		mock_frappe.db.exists.side_effect = lambda doctype, name: True

		def _get_all(doctype, **kwargs):
			if doctype == "Sales Invoice":
				return [{"name": "INV-004"}]
			if doctype == "Delivery Note Item":
				return []
			return []

		mock_frappe.get_all.side_effect = _get_all
		mock_frappe.get_doc.side_effect = lambda doctype, name: invoice if doctype == "Sales Invoice" else MagicMock()
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "warehouse"): "Stores - Nasr city",
				("Warehouse", "Stores - Nasr city", "company"): "Jarz",
				("Item", "ITEM-001", "is_stock_item"): 1,
			}.get((doctype, name, field))
		)

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"):
			result = get_invoice_warehouse_alignment_report(limit=25)

		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("count"), 1)
		self.assertEqual(result["invoices"][0]["invoice_id"], "INV-004")
		self.assertEqual(result["invoices"][0]["target_warehouse"], "Stores - Nasr city")
		self.assertEqual(result["invoices"][0]["mismatches"][0]["warehouse"], "Stores - Dokki")

	def test_repair_invoice_warehouse_alignment_apply_updates_eligible_invoice(self):
		"""Apply mode should repair eligible invoices that are still misaligned and have no Delivery Note."""
		from jarz_pos.api.manager import repair_invoice_warehouse_alignment

		item_row = SimpleNamespace(name="SII-021", item_code="ITEM-001", warehouse="Stores - Dokki")
		invoice = _FakeInvoice(
			name="INV-005",
			docstatus=1,
			is_pos=1,
			company="Jarz",
			customer="CUST-005",
			pos_profile="Dokki",
			custom_kanban_profile="Nasr city",
			items=[item_row],
		)
		invoice.add_comment = MagicMock()

		meta = MagicMock()
		meta.get_field.side_effect = lambda fieldname: object() if fieldname == "set_warehouse" else None

		mock_frappe = MagicMock()
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.get_roles.return_value = ["System Manager"]
		mock_frappe.has_permission.return_value = True
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.ValidationError = Exception
		mock_frappe.db.exists.side_effect = lambda doctype, name: True

		def _get_all(doctype, **kwargs):
			if doctype == "Sales Invoice":
				return [{"name": "INV-005"}]
			if doctype == "Delivery Note Item":
				return []
			return []

		def _set_value(doctype, name, field, value, update_modified=True):
			if doctype == "Sales Invoice Item" and name == "SII-021" and field == "warehouse":
				item_row.warehouse = value
			if doctype == "Sales Invoice" and name == "INV-005" and field == "set_warehouse":
				invoice._data[field] = value

		mock_frappe.get_all.side_effect = _get_all
		mock_frappe.get_doc.side_effect = lambda doctype, name: invoice if doctype == "Sales Invoice" else MagicMock()
		mock_frappe.get_meta.return_value = meta
		mock_frappe.db.set_value.side_effect = _set_value
		mock_frappe.db.get_value.side_effect = (
			lambda doctype, name, field: {
				("POS Profile", "Nasr city", "warehouse"): "Stores - Nasr city",
				("Warehouse", "Stores - Nasr city", "company"): "Jarz",
				("Item", "ITEM-001", "is_stock_item"): 1,
			}.get((doctype, name, field))
		)

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._ensure_manager_dashboard_access"):
			result = repair_invoice_warehouse_alignment(limit=25, apply_changes=1)

		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("mode"), "apply")
		self.assertEqual(result.get("applied_count"), 1)
		self.assertEqual(item_row.warehouse, "Stores - Nasr city")
		self.assertEqual(invoice.get("set_warehouse"), "Stores - Nasr city")
		invoice.add_comment.assert_called_once()

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

	def test_get_pos_shift_monitor_denies_line_manager_only_access(self):
		"""Line-manager-only users must not access the shift monitor page."""
		from jarz_pos.api.manager import get_pos_shift_monitor

		mock_frappe = MagicMock()
		mock_frappe.get_roles.return_value = ["JARZ line manager"]
		mock_frappe.throw.side_effect = _raise_frappe

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			with self.assertRaises(PermissionError):
				get_pos_shift_monitor()

	def test_get_pos_shift_monitor_returns_open_and_closed_shift_rows(self):
		"""Shift monitor should aggregate open and closed rows with discrepancy totals."""
		from jarz_pos.api.manager import get_pos_shift_monitor

		opening_closed = SimpleNamespace(
			name="POS-OPE-0001",
			user="opener@example.com",
			company="JARZ",
			pos_profile="Dokki",
			period_start_date="2026-06-05 08:00:00",
			balance_details=[SimpleNamespace(opening_amount=1000)],
		)
		closing = SimpleNamespace(
			name="POS-CLO-0001",
			owner="closer@example.com",
			period_end_date="2026-06-05 16:00:00",
			payment_reconciliation=[
				SimpleNamespace(expected_amount=1400, closing_amount=1450),
			],
		)
		opening_open = SimpleNamespace(
			name="POS-OPE-0002",
			user="second@example.com",
			company="JARZ",
			pos_profile="Nasr city",
			period_start_date="2026-06-05 09:00:00",
			balance_details=[SimpleNamespace(opening_amount=750)],
		)

		mock_frappe = MagicMock()
		mock_frappe.get_roles.return_value = ["JARZ Manager"]
		mock_frappe.throw.side_effect = _raise_frappe
		mock_frappe.get_all.return_value = [
			{"name": "POS-OPE-0002"},
			{"name": "POS-OPE-0001"},
		]

		def _get_doc(doctype, name):
			if doctype == "POS Opening Entry" and name == "POS-OPE-0001":
				return opening_closed
			if doctype == "POS Opening Entry" and name == "POS-OPE-0002":
				return opening_open
			if doctype == "POS Closing Entry" and name == "POS-CLO-0001":
				return closing
			raise AssertionError(f"Unexpected get_doc lookup: {doctype} {name}")

		def _db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "POS Opening Entry" and name == "POS-OPE-0001" and fieldname == "pos_closing_entry":
				return "POS-CLO-0001"
			if doctype == "POS Opening Entry" and name == "POS-OPE-0002" and fieldname == "pos_closing_entry":
				return None
			if doctype == "POS Closing Entry":
				return None
			if doctype == "User" and name == "opener@example.com" and fieldname == "full_name":
				return "Omar Opener"
			if doctype == "User" and name == "second@example.com" and fieldname == "full_name":
				return "Nada Starter"
			if doctype == "User" and name == "closer@example.com" and fieldname == "full_name":
				return "Sara Closer"
			if doctype == "Employee" and isinstance(name, dict) and name.get("user_id") == "opener@example.com":
				return {"name": "EMP-OPEN", "employee_name": "Omar"}
			if doctype == "Employee" and isinstance(name, dict) and name.get("user_id") == "second@example.com":
				return {"name": "EMP-START", "employee_name": "Nada"}
			if doctype == "Employee" and isinstance(name, dict) and name.get("user_id") == "closer@example.com":
				return {"name": "EMP-CLOSE", "employee_name": "Sara"}
			if doctype == "Journal Entry" and isinstance(name, dict) and fieldname == "name":
				return "JE-SHIFT-0001"
			return None

		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.db.get_value.side_effect = _db_get_value

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._current_user_shift_monitor_profiles", return_value=["Dokki", "Nasr city"]), \
			 patch("jarz_pos.api.manager._get_all_active_pos_profiles", return_value=["Dokki", "Nasr city"]), \
			 patch("jarz_pos.api.shift._resolve_pos_profile_account", side_effect=["Dokki - J", "Nasr city - J"]):
			result = get_pos_shift_monitor(from_date="2026-06-05", to_date="2026-06-05")

		self.assertTrue(result.get("success"))
		self.assertEqual(result["summary"]["open_count"], 1)
		self.assertEqual(result["summary"]["closed_count"], 1)
		self.assertEqual(result["summary"]["discrepancy_count"], 1)
		self.assertEqual(result["summary"]["discrepancy_total"], 50.0)
		self.assertEqual(len(result["shifts"]), 2)

		closed_row = next(row for row in result["shifts"] if row["opening_entry"] == "POS-OPE-0001")
		self.assertEqual(closed_row["shift_status"], "closed")
		self.assertEqual(closed_row["closing_entry"], "POS-CLO-0001")
		self.assertEqual(closed_row["opened_by_full_name"], "Omar Opener")
		self.assertEqual(closed_row["closed_by_full_name"], "Sara Closer")
		self.assertEqual(closed_row["cash_account"], "Dokki - J")
		self.assertEqual(closed_row["opening_amount"], 1000.0)
		self.assertEqual(closed_row["expected_closing_amount"], 1400.0)
		self.assertEqual(closed_row["actual_closing_amount"], 1450.0)
		self.assertEqual(closed_row["difference_amount"], 50.0)
		self.assertEqual(closed_row["difference_kind"], "surplus")
		self.assertEqual(closed_row["journal_entry"], "JE-SHIFT-0001")

		open_row = next(row for row in result["shifts"] if row["opening_entry"] == "POS-OPE-0002")
		self.assertEqual(open_row["shift_status"], "open")
		self.assertIsNone(open_row["closing_entry"])
		self.assertIsNone(open_row["closed_at"])
		self.assertEqual(open_row["cash_account"], "Nasr city - J")

	def test_get_pos_shift_monitor_applies_closed_status_filter(self):
		"""Closed filter should exclude open shifts from the response."""
		from jarz_pos.api.manager import get_pos_shift_monitor

		opening = SimpleNamespace(
			name="POS-OPE-0001",
			user="opener@example.com",
			company="JARZ",
			pos_profile="Dokki",
			period_start_date="2026-06-05 08:00:00",
			balance_details=[SimpleNamespace(opening_amount=1000)],
		)
		closing = SimpleNamespace(
			name="POS-CLO-0001",
			owner="closer@example.com",
			period_end_date="2026-06-05 16:00:00",
			payment_reconciliation=[SimpleNamespace(expected_amount=1200, closing_amount=1200)],
		)

		mock_frappe = MagicMock()
		mock_frappe.get_roles.return_value = ["System Manager"]
		mock_frappe.throw.side_effect = _raise_frappe
		mock_frappe.get_all.return_value = [{"name": "POS-OPE-0001"}]
		mock_frappe.get_doc.side_effect = lambda doctype, name: opening if doctype == "POS Opening Entry" else closing
		mock_frappe.db.get_value.side_effect = lambda doctype, name, fieldname, *args, **kwargs: {
			("POS Opening Entry", "POS-OPE-0001", "pos_closing_entry"): "POS-CLO-0001",
			("User", "opener@example.com", "full_name"): "Omar Opener",
			("User", "closer@example.com", "full_name"): "Sara Closer",
		}.get((doctype, name, fieldname))

		with patch("jarz_pos.api.manager.frappe", mock_frappe), \
			 patch("jarz_pos.api.manager._current_user_shift_monitor_profiles", return_value=["Dokki"]), \
			 patch("jarz_pos.api.manager._get_all_active_pos_profiles", return_value=["Dokki"]), \
			 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"):
			result = get_pos_shift_monitor(status="closed")

		self.assertEqual(len(result["shifts"]), 1)
		self.assertEqual(result["shifts"][0]["shift_status"], "closed")
		self.assertEqual(result["summary"]["open_count"], 0)
		self.assertEqual(result["summary"]["closed_count"], 1)


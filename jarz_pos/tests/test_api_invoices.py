"""Tests for invoice API endpoints.

This module tests invoice creation and management endpoints.
"""

import unittest
from unittest.mock import patch


class TestInvoiceAPI(unittest.TestCase):
	"""Test class for Invoice API functionality."""

	@patch("jarz_pos.utils.invoice_utils.resolve_order_territory", return_value=None)
	@patch("jarz_pos.utils.invoice_utils.assert_pos_profile_matches_territory")
	@patch("jarz_pos.api.invoices._create_invoice")
	@patch("jarz_pos.api.invoices.frappe")
	def test_create_pos_invoice_forwards_price_list_and_zero_shipping_flags(
		self,
		mock_frappe,
		mock_create_invoice,
		mock_assert_profile,
		mock_resolve_order_territory,
	):
		"""Public API wrapper should forward price list and shipping suppression flags."""
		from jarz_pos.api.invoices import create_pos_invoice

		mock_frappe.session.user = "manager@example.com"
		mock_frappe.local.site = "frontend"
		mock_frappe.local.request.method = "POST"
		mock_frappe.form_dict = {
			"cart_json": '[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			"customer_name": "Test Customer",
			"pos_profile_name": "Main POS",
			"payment_method": "Cash",
			"price_list": "B2B Selling",
			"zero_shipping_override": "1",
		}
		mock_create_invoice.return_value = {"success": True, "invoice_name": "INV-0001"}

		result = create_pos_invoice()

		self.assertEqual(result, {"success": True, "invoice_name": "INV-0001"})
		mock_resolve_order_territory.assert_called_once_with("Test Customer", shipping_address_name=None)
		mock_assert_profile.assert_called_once_with("Test Customer", "Main POS", override=False, territory_name=None)
		# Assert the kwargs this test is about rather than the whole call.
		# The exact-call form rotted silently: the API grew custom_delivery_income,
		# order_purpose, commercial_policy, policy_reason, promo_codes and channel
		# over several releases and this assertion was never updated, because the
		# module ran nowhere -- site-less it errors on BranchAccessError, and it
		# was absent from the CI module list until 2026-09-04.
		mock_create_invoice.assert_called_once()
		self.assertEqual(mock_create_invoice.call_args.args, ())
		forwarded = mock_create_invoice.call_args.kwargs
		for key, expected in {
			"cart_json": '[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			"customer_name": "Test Customer",
			"pos_profile_name": "Main POS",
			"delivery_charges_json": None,
			"required_delivery_datetime": None,
			"shipping_address_name": None,
			"sales_partner": None,
			"payment_type": None,
			"pickup": False,
			"payment_method": "Cash",
			"price_list": "B2B Selling",
			"suppress_shipping_income": True,
			"suppress_legacy_delivery_charges": True,
		}.items():
			self.assertIn(key, forwarded)
			self.assertEqual(forwarded[key], expected, f"{key} was not forwarded intact")

	@patch("jarz_pos.utils.invoice_utils.resolve_order_territory", return_value=None)
	@patch("jarz_pos.utils.invoice_utils.assert_pos_profile_matches_territory")
	@patch("jarz_pos.api.invoices._create_invoice")
	@patch("jarz_pos.api.invoices.frappe")
	def test_create_pos_invoice_honors_explicit_suppression_flags(
		self,
		mock_frappe,
		mock_create_invoice,
		mock_assert_profile,
		mock_resolve_order_territory,
	):
		"""Explicit suppression flags should be forwarded even without zero_shipping_override."""
		from jarz_pos.api.invoices import create_pos_invoice

		mock_frappe.session.user = "manager@example.com"
		mock_frappe.local.site = "frontend"
		mock_frappe.local.request.method = "POST"
		mock_frappe.form_dict = {
			"cart_json": '[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			"customer_name": "Test Customer",
			"pos_profile_name": "Main POS",
			"suppress_shipping_income": "1",
			"suppress_legacy_delivery_charges": "true",
		}
		mock_create_invoice.return_value = {"success": True, "invoice_name": "INV-0002"}

		create_pos_invoice()

		mock_resolve_order_territory.assert_called_once_with("Test Customer", shipping_address_name=None)
		mock_assert_profile.assert_called_once_with("Test Customer", "Main POS", override=False, territory_name=None)
		# Subset assertion for the same reason as the test above.
		mock_create_invoice.assert_called_once()
		self.assertEqual(mock_create_invoice.call_args.args, ())
		forwarded = mock_create_invoice.call_args.kwargs
		for key, expected in {
			"cart_json": '[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			"customer_name": "Test Customer",
			"pos_profile_name": "Main POS",
			"delivery_charges_json": None,
			"required_delivery_datetime": None,
			"shipping_address_name": None,
			"sales_partner": None,
			"payment_type": None,
			"pickup": False,
			"payment_method": None,
			"price_list": None,
			"suppress_shipping_income": True,
			"suppress_legacy_delivery_charges": True,
		}.items():
			self.assertIn(key, forwarded)
			self.assertEqual(forwarded[key], expected, f"{key} was not forwarded intact")

	def _forwarded_employee_payment(self, form_dict):
		"""Run the public wrapper and return the kwargs it handed the service."""
		from jarz_pos.api.invoices import create_pos_invoice

		with patch("jarz_pos.utils.invoice_utils.resolve_order_territory", return_value=None), \
			 patch("jarz_pos.utils.invoice_utils.assert_pos_profile_matches_territory"), \
			 patch("jarz_pos.api.invoices._guard_branch_sale"), \
			 patch("jarz_pos.api.invoices._create_invoice") as mock_create_invoice, \
			 patch("jarz_pos.api.invoices.frappe") as mock_frappe:
			mock_frappe.session.user = "cashier@example.com"
			mock_frappe.local.site = "frontend"
			mock_frappe.local.request.method = "POST"
			mock_frappe.form_dict = form_dict
			mock_create_invoice.return_value = {"success": True, "invoice_name": "INV-EMP-0001"}

			create_pos_invoice()

		mock_create_invoice.assert_called_once()
		self.assertEqual(mock_create_invoice.call_args.args, ())
		return mock_create_invoice.call_args.kwargs

	def test_create_pos_invoice_forwards_employee_payment(self):
		"""The service is whitelisted and validates; the wrapper must forward the raw value."""
		forwarded = self._forwarded_employee_payment({
			"cart_json": '[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			"customer_name": "STAFF-Mona",
			"pos_profile_name": "Nasr City",
			"order_purpose": "Employee",
			"employee_payment": "CASH",
		})
		self.assertIn("employee_payment", forwarded)
		self.assertEqual(forwarded["employee_payment"], "CASH")
		self.assertEqual(forwarded["order_purpose"], "Employee")

	def test_create_pos_invoice_employee_payment_defaults_to_none(self):
		"""An older client that never sends the key must reach the service as credit (None)."""
		forwarded = self._forwarded_employee_payment({
			"cart_json": '[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			"customer_name": "STAFF-Mona",
			"pos_profile_name": "Nasr City",
			"order_purpose": "Employee",
			"payment_method": "Cash",
		})
		self.assertIn("employee_payment", forwarded)
		self.assertIsNone(forwarded["employee_payment"])
		# payment_method=Cash is forwarded as a label only — it never implies cash.
		self.assertEqual(forwarded["payment_method"], "Cash")

	def test_api_modules_present(self):
		"""Test that invoice API modules can be imported."""
		import importlib

		invoices = importlib.import_module("jarz_pos.api.invoices")
		couriers = importlib.import_module("jarz_pos.api.couriers")

		self.assertTrue(hasattr(invoices, "create_pos_invoice"), "Should have create_pos_invoice")
		self.assertTrue(hasattr(couriers, "get_courier_balances"), "Should have get_courier_balances")

	def test_create_pos_invoice_validation(self):
		"""Test that create_pos_invoice validates required fields."""
		from jarz_pos.api.invoices import create_pos_invoice

		# Test with missing required fields should raise an error
		with self.assertRaises(Exception):
			create_pos_invoice(
				customer="",
				pos_profile="",
				cart_items=[],
			)

	def test_create_pos_invoice_empty_cart(self):
		"""Test that create_pos_invoice handles empty cart."""
		from jarz_pos.api.invoices import create_pos_invoice

		# Test with empty cart should raise an error
		with self.assertRaises(Exception):
			create_pos_invoice(
				customer="Test Customer",
				pos_profile="Test Profile",
				cart_items=[],
			)

	def test_pay_invoice_validation(self):
		"""Test that pay_invoice validates inputs."""
		from jarz_pos.api.invoices import pay_invoice

		# Test with invalid invoice
		try:
			result = pay_invoice(invoice_name="NON_EXISTENT_INV")
			# If it doesn't raise, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Expected to fail with non-existent invoice
			pass

	def test_get_invoice_settlement_preview_validation(self):
		"""Test that get_invoice_settlement_preview validates inputs."""
		from jarz_pos.api.invoices import get_invoice_settlement_preview

		# Test with invalid invoice
		try:
			result = get_invoice_settlement_preview(invoice_name="NON_EXISTENT_INV")
			# If it doesn't raise, verify structure
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Expected to fail with non-existent invoice
			pass


class TestPayInvoiceIdempotency(unittest.TestCase):
	"""Two taps on Pay must not post two Payment Entries.

	``pay_invoice`` takes ``FOR UPDATE`` on the Sales Invoice row, which
	serialises the two callers but does NOT refresh either transaction's
	snapshot: MariaDB runs at REPEATABLE READ and Frappe sets no isolation
	level. A plain read of the existing payments therefore answers "none" to the
	second caller even after the first has committed, and it posts a duplicate.
	"""

	def _frappe(self, mock_frappe, *, existing_rows=None, sql_error=None):
		invoice = type("Inv", (), {})()
		invoice.name = "ACC-SINV-0001"
		invoice.docstatus = 1
		invoice.company = "Test Company"
		invoice.customer = "CUST-1"
		invoice.outstanding_amount = 480.0
		mock_frappe.get_doc.return_value = invoice

		def _sql(query, *args, **kwargs):
			if "tabPayment Entry" in query:
				if sql_error is not None:
					raise sql_error
				return list(existing_rows or [])
			return []

		mock_frappe.db.sql.side_effect = _sql
		mock_frappe.db.get_value.return_value = 480.0
		mock_frappe.throw.side_effect = lambda msg, *a, **k: (_ for _ in ()).throw(Exception(msg))
		return mock_frappe

	@patch("jarz_pos.api.invoices._clear_awaiting_payment_flag")
	@patch("jarz_pos.api.invoices.ensure_open_shift_for_invoice")
	@patch("jarz_pos.api.invoices.ensure_profile_scoped_invoice_access")
	@patch("jarz_pos.api.invoices.frappe")
	def test_an_existing_payment_is_looked_up_with_a_locking_read(
		self, mock_frappe, _scope, _shift, _flag
	):
		from jarz_pos.api.invoices import pay_invoice

		self._frappe(mock_frappe, existing_rows=[
			{"name": "ACC-PAY-0001", "posting_date": "2026-09-16", "paid_amount": 480.0},
		])

		result = pay_invoice("ACC-SINV-0001", "cash", pos_profile="Dokki")

		self.assertEqual(result["payment_entry"], "ACC-PAY-0001")
		self.assertIn("idempotent", result["note"])
		# The check that protects the money has to read the latest committed
		# rows, not this transaction's snapshot.
		queries = [c[0][0] for c in mock_frappe.db.sql.call_args_list if "tabPayment Entry" in c[0][0]]
		self.assertTrue(queries, "the existing-payment check never ran")
		self.assertIn("FOR UPDATE", queries[-1].upper())
		mock_frappe.new_doc.assert_not_called()

	@patch("jarz_pos.api.invoices._clear_awaiting_payment_flag")
	@patch("jarz_pos.api.invoices.ensure_open_shift_for_invoice")
	@patch("jarz_pos.api.invoices.ensure_profile_scoped_invoice_access")
	@patch("jarz_pos.api.invoices.frappe")
	def test_a_failed_check_refuses_instead_of_posting_blind(
		self, mock_frappe, _scope, _shift, _flag
	):
		"""The moment this check is most likely to fail -- a lock wait, a
		deadlock -- is the moment two people are paying the same order, which is
		the case it exists to stop. It used to log and carry on."""
		from jarz_pos.api.invoices import pay_invoice

		self._frappe(mock_frappe, sql_error=RuntimeError("lock wait timeout"))

		with self.assertRaises(Exception) as exc:
			pay_invoice("ACC-SINV-0001", "cash", pos_profile="Dokki")

		self.assertIn("try again", str(exc.exception).lower())
		mock_frappe.new_doc.assert_not_called()


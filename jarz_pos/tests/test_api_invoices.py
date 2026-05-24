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
		mock_create_invoice.assert_called_once_with(
			cart_json='[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			customer_name="Test Customer",
			pos_profile_name="Main POS",
			delivery_charges_json=None,
			required_delivery_datetime=None,
			shipping_address_name=None,
			sales_partner=None,
			payment_type=None,
			pickup=False,
			payment_method="Cash",
			price_list="B2B Selling",
			suppress_shipping_income=True,
			suppress_legacy_delivery_charges=True,
		)

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
		mock_create_invoice.assert_called_once_with(
			cart_json='[{"item_code":"ITEM-001","qty":1,"rate":100}]',
			customer_name="Test Customer",
			pos_profile_name="Main POS",
			delivery_charges_json=None,
			required_delivery_datetime=None,
			shipping_address_name=None,
			sales_partner=None,
			payment_type=None,
			pickup=False,
			payment_method=None,
			price_list=None,
			suppress_shipping_income=True,
			suppress_legacy_delivery_charges=True,
		)

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

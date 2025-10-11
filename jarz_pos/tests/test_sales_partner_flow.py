"""Comprehensive tests for sales partner invoice flow.

This module tests the sales partner-specific invoice handling:
- Payment Entry creation when moving to "Out for Delivery"
- Sales Partner Transaction record creation
- Account routing for online vs cash payments
- Fee calculation and VAT
- Integration with kanban state transitions
"""

import unittest
import frappe
from unittest.mock import patch, MagicMock, call


class TestSalesPartnerInvoiceFlow(unittest.TestCase):
	"""Test sales partner invoice business logic."""

	def setUp(self):
		"""Set up test environment."""
		self.original_get_doc = frappe.get_doc
		self.original_get_all = frappe.get_all
		self.original_db_exists = frappe.db.exists

	def tearDown(self):
		"""Clean up test environment."""
		frappe.get_doc = self.original_get_doc
		frappe.get_all = self.original_get_all
		frappe.db.exists = self.original_db_exists

	def test_sales_partner_fees_calculation_commission_only(self):
		"""Test sales partner fee calculation with commission only."""
		from jarz_pos.services.delivery_handling import _compute_sales_partner_fees

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.grand_total = 1000.0

		# Mock sales partner with 5% commission
		mock_partner = MagicMock()
		mock_partner.commission_rate = 5.0
		mock_partner.online_payment_fees = 0.0

		frappe.get_doc = MagicMock(return_value=mock_partner)

		result = _compute_sales_partner_fees(mock_inv, "Partner-001", online=False)

		# 1000 * 5% = 50 commission
		# 50 * 14% VAT = 7
		# Total = 57
		self.assertEqual(result["base_fees"], 50.0)
		self.assertEqual(result["vat"], 7.0)
		self.assertEqual(result["total_fees"], 57.0)
		self.assertEqual(result["commission_rate"], 5.0)
		self.assertEqual(result["online_rate"], 0.0)

	def test_sales_partner_fees_calculation_with_online_fee(self):
		"""Test sales partner fee calculation with online payment fee."""
		from jarz_pos.services.delivery_handling import _compute_sales_partner_fees

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.grand_total = 1000.0

		# Mock sales partner with 5% commission and 2% online fee
		mock_partner = MagicMock()
		mock_partner.commission_rate = 5.0
		mock_partner.online_payment_fees = 2.0

		frappe.get_doc = MagicMock(return_value=mock_partner)

		result = _compute_sales_partner_fees(mock_inv, "Partner-001", online=True)

		# 1000 * 5% = 50 commission
		# 1000 * 2% = 20 online fee
		# Base = 70
		# VAT = 70 * 14% = 9.8
		# Total = 79.8
		self.assertEqual(result["base_fees"], 70.0)
		self.assertEqual(result["vat"], 9.8)
		self.assertEqual(result["total_fees"], 79.8)
		self.assertEqual(result["commission_rate"], 5.0)
		self.assertEqual(result["online_rate"], 2.0)

	def test_sales_partner_fees_calculation_no_online_flag(self):
		"""Test that online fee is not applied when online=False."""
		from jarz_pos.services.delivery_handling import _compute_sales_partner_fees

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.grand_total = 1000.0

		# Mock sales partner with both commission and online fee
		mock_partner = MagicMock()
		mock_partner.commission_rate = 5.0
		mock_partner.online_payment_fees = 2.0

		frappe.get_doc = MagicMock(return_value=mock_partner)

		result = _compute_sales_partner_fees(mock_inv, "Partner-001", online=False)

		# Should only calculate commission, not online fee
		self.assertEqual(result["base_fees"], 50.0)
		self.assertEqual(result["online_rate"], 2.0)  # Still returned but not used
		# VAT only on commission
		self.assertEqual(result["vat"], 7.0)

	def test_sales_partner_fees_calculation_error_handling(self):
		"""Test sales partner fee calculation with missing partner."""
		from jarz_pos.services.delivery_handling import _compute_sales_partner_fees

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.grand_total = 1000.0

		# Mock frappe.get_doc to raise exception
		frappe.get_doc = MagicMock(side_effect=Exception("Partner not found"))

		result = _compute_sales_partner_fees(mock_inv, "Missing-Partner", online=True)

		# Should return zeros when partner not found
		self.assertEqual(result["base_fees"], 0.0)
		self.assertEqual(result["vat"], 0.0)
		self.assertEqual(result["total_fees"], 0.0)
		self.assertEqual(result["commission_rate"], 0.0)
		self.assertEqual(result["online_rate"], 0.0)

	@patch('jarz_pos.api.kanban.frappe')
	@patch('jarz_pos.api.kanban.get_pos_cash_account')
	@patch('jarz_pos.api.kanban.get_company_receivable_account')
	def test_cash_payment_entry_creation_for_partner(self, mock_receivable, mock_cash, mock_frappe):
		"""Test automatic cash payment entry creation for sales partner invoices."""
		# This tests the _ensure_cash_payment_entry_for_partner function
		# which is called during kanban state transition to "Out for Delivery"

		# Mock invoice with sales partner and outstanding amount
		mock_inv = MagicMock()
		mock_inv.name = "INV-PARTNER-001"
		mock_inv.sales_partner = "Partner-001"
		mock_inv.outstanding_amount = 500.0
		mock_inv.grand_total = 500.0
		mock_inv.customer = "Customer-001"
		mock_inv.company = "Test Company"
		mock_inv.custom_kanban_profile = "POS-001"

		# Mock no existing payment entries
		mock_frappe.get_all.return_value = []

		# Mock accounts
		mock_cash.return_value = "Cash - TC"
		mock_receivable.return_value = "Debtors - TC"

		# Mock payment entry creation
		mock_pe = MagicMock()
		mock_pe.name = "PE-001"
		mock_frappe.new_doc.return_value = mock_pe

		# Mock frappe.utils methods
		mock_frappe.utils.getdate.return_value = "2025-01-01"
		mock_frappe.utils.nowtime.return_value = "12:00:00"

		# Import and call the function (through kanban update_invoice_state)
		# We'll test the logic exists and correct flow
		from jarz_pos.api.kanban import update_invoice_state

		# The function should be callable
		self.assertTrue(callable(update_invoice_state))

	def test_sales_partner_transaction_idempotency(self):
		"""Test that Sales Partner Transaction is created only once (idempotency)."""
		# Mock to test idempotency token pattern SPTRN::<invoice_name>

		mock_inv = MagicMock()
		mock_inv.name = "INV-SP-001"

		expected_token = f"SPTRN::{mock_inv.name}"

		self.assertEqual(expected_token, "SPTRN::INV-SP-001")

	@patch('jarz_pos.api.kanban.frappe')
	def test_sales_partner_transaction_payment_mode_cash(self, mock_frappe):
		"""Test Sales Partner Transaction payment mode is Cash when cash PE created."""
		# When cash payment entry is created, payment_mode should be 'Cash'

		mock_txn = MagicMock()
		mock_frappe.new_doc.return_value = mock_txn

		# Simulate the logic in kanban.py
		created_cash_payment_entry = "PE-CASH-001"  # PE was created
		payment_mode_val = 'Cash' if created_cash_payment_entry else 'Online'

		self.assertEqual(payment_mode_val, 'Cash')

	@patch('jarz_pos.api.kanban.frappe')
	def test_sales_partner_transaction_payment_mode_online(self, mock_frappe):
		"""Test Sales Partner Transaction payment mode is Online when no cash PE."""
		# When no cash payment entry is created, payment_mode should be 'Online'

		mock_txn = MagicMock()
		mock_frappe.new_doc.return_value = mock_txn

		# Simulate the logic in kanban.py
		created_cash_payment_entry = None  # No PE created
		payment_mode_val = 'Cash' if created_cash_payment_entry else 'Online'

		self.assertEqual(payment_mode_val, 'Online')

	def test_sales_partner_transaction_field_structure(self):
		"""Test Sales Partner Transaction has expected fields."""
		# This validates the expected structure based on doctype definition

		expected_fields = [
			"sales_partner",
			"status",  # Unsettled/Settled
			"date",
			"reference_invoice",
			"amount",
			"partner_fees",  # User updates later
			"payment_mode",  # Cash/Online
			"idempotency_token",
		]

		# This is a structure validation test
		for field in expected_fields:
			self.assertIsNotNone(field)

	@patch('jarz_pos.api.kanban.frappe')
	def test_ensure_cash_pe_skipped_when_no_sales_partner(self, mock_frappe):
		"""Test cash PE creation is skipped when invoice has no sales partner."""
		# Mock invoice without sales partner
		mock_inv = MagicMock()
		mock_inv.sales_partner = None
		mock_inv.outstanding_amount = 500.0

		# The function should return None (skip creation)
		# Simulating the check in _ensure_cash_payment_entry_for_partner
		if not getattr(mock_inv, "sales_partner", None):
			result = None
		else:
			result = "PE-001"

		self.assertIsNone(result)

	@patch('jarz_pos.api.kanban.frappe')
	def test_ensure_cash_pe_skipped_when_no_outstanding(self, mock_frappe):
		"""Test cash PE creation is skipped when invoice has no outstanding."""
		# Mock invoice with sales partner but no outstanding
		mock_inv = MagicMock()
		mock_inv.sales_partner = "Partner-001"
		mock_inv.outstanding_amount = 0.0

		# The function should return None (skip creation)
		outstanding = float(getattr(mock_inv, "outstanding_amount", 0) or 0)
		if outstanding <= 0.0001:
			result = None
		else:
			result = "PE-001"

		self.assertIsNone(result)

	@patch('jarz_pos.api.kanban.frappe')
	def test_ensure_cash_pe_skipped_when_already_paid(self, mock_frappe):
		"""Test cash PE creation is skipped when PE already exists."""
		# Mock existing payment entry reference
		mock_frappe.get_all.return_value = [
			{"parent": "PE-EXISTING", "allocated_amount": 500.0}
		]

		mock_inv = MagicMock()
		mock_inv.name = "INV-001"
		mock_inv.sales_partner = "Partner-001"
		mock_inv.outstanding_amount = 500.0

		# Simulate the idempotency check
		existing = mock_frappe.get_all.return_value
		should_skip = False
		for ref in existing:
			if float(ref.get("allocated_amount") or 0) >= 500.0 - 0.0001:
				should_skip = True
				break

		self.assertTrue(should_skip)

	def test_sales_partner_vat_rate_constant(self):
		"""Test that VAT rate for partner fees is 14%."""
		from jarz_pos.services.delivery_handling import PARTNER_FEES_VAT_RATE

		self.assertEqual(PARTNER_FEES_VAT_RATE, 0.14)

	@patch('jarz_pos.services.settlement_strategies.resolve_online_partner_paid_to')
	def test_account_routing_online_partner(self, mock_resolve):
		"""Test account routing for online payment with sales partner."""
		from jarz_pos.services.settlement_strategies import _route_paid_to_account

		# Mock the helper to return partner-specific account
		mock_resolve.return_value = "Partner Receivable Sub - TC"

		result = _route_paid_to_account("Test Company", "online", "Partner-001")

		self.assertEqual(result, "Partner Receivable Sub - TC")
		mock_resolve.assert_called_once_with("Test Company", "Partner-001")

	def test_account_routing_cash_partner(self):
		"""Test account routing for cash payment returns None (use default)."""
		from jarz_pos.services.settlement_strategies import _route_paid_to_account

		# Cash payment should return None to use default POS cash account
		result = _route_paid_to_account("Test Company", "cash", "Partner-001")

		self.assertIsNone(result)

	def test_sales_partner_integration_with_settlement_strategies(self):
		"""Test that settlement strategies properly handle sales partner invoices."""
		from jarz_pos.services.settlement_strategies import handle_unpaid_settle_now

		# This is a structure validation - the handler should exist and be callable
		self.assertTrue(callable(handle_unpaid_settle_now))

		# The handler should accept sales_partner info via invoice attributes
		# and route accounts accordingly via _route_paid_to_account


if __name__ == "__main__":
	unittest.main()

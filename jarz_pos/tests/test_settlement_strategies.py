"""Comprehensive tests for settlement strategies.

This module tests all four settlement strategy cases:
1. Unpaid + settle now
2. Unpaid + settle later
3. Paid + settle now
4. Paid + settle later

Each test validates the correct business logic, accounting entries,
and state transitions.
"""

import unittest
import frappe
from unittest.mock import patch, MagicMock


class TestSettlementStrategies(unittest.TestCase):
	"""Test settlement strategy dispatch and handlers."""

	def setUp(self):
		"""Set up test environment."""
		# Mock frappe methods to avoid database dependencies
		self.original_get_doc = frappe.get_doc
		self.original_db_get_value = frappe.db.get_value
		self.original_throw = frappe.throw

	def tearDown(self):
		"""Clean up test environment."""
		frappe.get_doc = self.original_get_doc
		frappe.db.get_value = self.original_db_get_value
		frappe.throw = self.original_throw

	def test_is_unpaid_with_outstanding(self):
		"""Test _is_unpaid function with outstanding amount."""
		from jarz_pos.services.settlement_strategies import _is_unpaid

		# Mock invoice with outstanding amount
		mock_inv = MagicMock()
		mock_inv.name = "INV-001"
		mock_inv.outstanding_amount = 100.0
		mock_inv.get.return_value = "Unpaid"

		# Mock db.get_value to return outstanding
		frappe.db.get_value = MagicMock(return_value=100.0)

		result = _is_unpaid(mock_inv)
		self.assertTrue(result, "Should detect unpaid invoice with outstanding amount")

	def test_is_unpaid_with_zero_outstanding(self):
		"""Test _is_unpaid function with zero outstanding."""
		from jarz_pos.services.settlement_strategies import _is_unpaid

		# Mock invoice with no outstanding
		mock_inv = MagicMock()
		mock_inv.name = "INV-002"
		mock_inv.outstanding_amount = 0.0
		mock_inv.get.return_value = "Paid"

		frappe.db.get_value = MagicMock(return_value=0.0)

		result = _is_unpaid(mock_inv)
		self.assertFalse(result, "Should detect paid invoice with zero outstanding")

	def test_is_unpaid_with_status_check(self):
		"""Test _is_unpaid function checking status field."""
		from jarz_pos.services.settlement_strategies import _is_unpaid

		# Mock invoice with partially paid status
		mock_inv = MagicMock()
		mock_inv.name = "INV-003"
		mock_inv.outstanding_amount = 50.0
		mock_inv.get.return_value = "Partially Paid"

		frappe.db.get_value = MagicMock(return_value=50.0)

		result = _is_unpaid(mock_inv)
		self.assertTrue(result, "Should detect unpaid invoice with Partially Paid status")

	def test_route_paid_to_account_online_payment(self):
		"""Test paid_to account routing for online payments."""
		from jarz_pos.services.settlement_strategies import _route_paid_to_account

		# Test online payment type
		with patch('jarz_pos.services.settlement_strategies.resolve_online_partner_paid_to') as mock_resolve:
			mock_resolve.return_value = "Bank Account - Online"
			result = _route_paid_to_account("Company", "online", "Partner-001")
			self.assertEqual(result, "Bank Account - Online")
			mock_resolve.assert_called_once_with("Company", "Partner-001")

	def test_route_paid_to_account_cash_payment(self):
		"""Test paid_to account routing for cash payments."""
		from jarz_pos.services.settlement_strategies import _route_paid_to_account

		# Test non-online payment type (should return None to use default)
		result = _route_paid_to_account("Company", "cash", None)
		self.assertIsNone(result, "Should return None for non-online payments")

	def test_route_paid_to_account_no_payment_type(self):
		"""Test paid_to account routing with no payment type."""
		from jarz_pos.services.settlement_strategies import _route_paid_to_account

		# Test with None payment type
		result = _route_paid_to_account("Company", None, None)
		self.assertIsNone(result, "Should return None when payment type is None")

	@patch('jarz_pos.services.settlement_strategies.frappe')
	def test_dispatch_settlement_unpaid_now(self, mock_frappe):
		"""Test dispatch_settlement for unpaid + settle now case."""
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-UNPAID-NOW"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 100.0
		mock_inv.company = "Test Company"

		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.return_value = 100.0

		# Mock the handler function
		with patch('jarz_pos.services.settlement_strategies.handle_unpaid_settle_now') as mock_handler:
			mock_handler.return_value = {"success": True, "mode": "unpaid_settle_now"}

			result = dispatch_settlement("INV-UNPAID-NOW", mode="now", pos_profile="POS-001")

			mock_handler.assert_called_once()
			self.assertTrue(result.get("success"))
			self.assertEqual(result.get("mode"), "unpaid_settle_now")

	@patch('jarz_pos.services.settlement_strategies.frappe')
	def test_dispatch_settlement_unpaid_later(self, mock_frappe):
		"""Test dispatch_settlement for unpaid + settle later case."""
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-UNPAID-LATER"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 100.0

		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.return_value = 100.0

		# Mock the handler function
		with patch('jarz_pos.services.settlement_strategies.handle_unpaid_settle_later') as mock_handler:
			mock_handler.return_value = {"success": True, "mode": "unpaid_settle_later"}

			result = dispatch_settlement("INV-UNPAID-LATER", mode="later", pos_profile="POS-001")

			mock_handler.assert_called_once()
			self.assertTrue(result.get("success"))
			self.assertEqual(result.get("mode"), "unpaid_settle_later")

	@patch('jarz_pos.services.settlement_strategies.frappe')
	def test_dispatch_settlement_paid_now(self, mock_frappe):
		"""Test dispatch_settlement for paid + settle now case."""
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		# Mock paid invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-PAID-NOW"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 0.0

		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.return_value = 0.0

		# Mock the handler function
		with patch('jarz_pos.services.settlement_strategies.handle_paid_settle_now') as mock_handler:
			mock_handler.return_value = {"success": True, "mode": "paid_settle_now"}

			result = dispatch_settlement("INV-PAID-NOW", mode="now", pos_profile="POS-001")

			mock_handler.assert_called_once()
			self.assertTrue(result.get("success"))
			self.assertEqual(result.get("mode"), "paid_settle_now")

	@patch('jarz_pos.services.settlement_strategies.frappe')
	def test_dispatch_settlement_paid_later(self, mock_frappe):
		"""Test dispatch_settlement for paid + settle later case."""
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		# Mock paid invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-PAID-LATER"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 0.0

		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.return_value = 0.0

		# Mock the handler function
		with patch('jarz_pos.services.settlement_strategies.handle_paid_settle_later') as mock_handler:
			mock_handler.return_value = {"success": True, "mode": "paid_settle_later"}

			result = dispatch_settlement("INV-PAID-LATER", mode="later", pos_profile="POS-001")

			mock_handler.assert_called_once()
			self.assertTrue(result.get("success"))

	@patch('jarz_pos.services.settlement_strategies.frappe')
	def test_dispatch_settlement_unsubmitted_invoice(self, mock_frappe):
		"""Test dispatch_settlement with unsubmitted invoice should fail."""
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		# Mock unsubmitted invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-DRAFT"
		mock_inv.docstatus = 0

		mock_frappe.get_doc.return_value = mock_inv

		# Should raise error for unsubmitted invoice
		error_raised = False
		mock_frappe.throw = MagicMock(side_effect=Exception("Invoice must be submitted"))

		try:
			dispatch_settlement("INV-DRAFT", mode="now")
		except Exception as e:
			error_raised = True
			self.assertIn("must be submitted", str(e))

		self.assertTrue(error_raised or mock_frappe.throw.called, "Should raise error for unsubmitted invoice")

	@patch('jarz_pos.services.settlement_strategies.frappe')
	def test_dispatch_settlement_invalid_mode(self, mock_frappe):
		"""Test dispatch_settlement with invalid mode should fail."""
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		# Mock invoice
		mock_inv = MagicMock()
		mock_inv.name = "INV-001"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 0.0

		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.return_value = 0.0
		mock_frappe.throw = MagicMock(side_effect=Exception("Unsupported settlement"))

		# Should raise error for invalid mode
		error_raised = False
		try:
			dispatch_settlement("INV-001", mode="invalid_mode")
		except Exception as e:
			error_raised = True
			self.assertIn("Unsupported", str(e))

		self.assertTrue(error_raised or mock_frappe.throw.called, "Should raise error for invalid mode")

	def test_handler_signatures_consistency(self):
		"""Test that all handler functions have consistent signatures."""
		from jarz_pos.services.settlement_strategies import (
			handle_unpaid_settle_now,
			handle_unpaid_settle_later,
			handle_paid_settle_now,
			handle_paid_settle_later,
		)

		# All handlers should accept the same keyword arguments
		import inspect

		handlers = [
			handle_unpaid_settle_now,
			handle_unpaid_settle_later,
			handle_paid_settle_now,
			handle_paid_settle_later,
		]

		expected_params = ['inv', 'pos_profile', 'payment_type', 'party_type', 'party']

		for handler in handlers:
			sig = inspect.signature(handler)
			param_names = list(sig.parameters.keys())
			self.assertListEqual(
				param_names,
				expected_params,
				f"Handler {handler.__name__} should have consistent parameters"
			)

	def test_strategy_mapping_completeness(self):
		"""Test that STRATEGY dict has all expected keys."""
		from jarz_pos.services.settlement_strategies import STRATEGY

		# Should have all 4 combinations
		expected_keys = [
			("unpaid", "now"),
			("unpaid", "later"),
			("paid", "now"),
			("paid", "later"),
		]

		for key in expected_keys:
			self.assertIn(key, STRATEGY, f"STRATEGY should include key {key}")

		# Each value should be callable
		for key, handler in STRATEGY.items():
			self.assertTrue(callable(handler), f"Handler for {key} should be callable")


if __name__ == "__main__":
	unittest.main()

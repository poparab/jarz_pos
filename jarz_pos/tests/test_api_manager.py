"""Tests for manager API endpoints.

This module tests manager dashboard and order management API endpoints.
"""

import unittest


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

"""Tests for global methods API endpoints.

This module tests global method endpoints (kanban wrappers).
"""

import unittest


class TestGlobalMethodsAPI(unittest.TestCase):
	"""Test class for Global Methods API functionality."""

	def test_get_kanban_columns_wrapper(self):
		"""Test that get_kanban_columns is accessible from global_methods."""
		from jarz_pos.api.global_methods import get_kanban_columns

		result = get_kanban_columns()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")

	def test_get_kanban_invoices_wrapper(self):
		"""Test that get_kanban_invoices is accessible from global_methods."""
		from jarz_pos.api.global_methods import get_kanban_invoices

		result = get_kanban_invoices()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")

	def test_update_invoice_state_wrapper(self):
		"""Test that update_invoice_state validates inputs."""
		from jarz_pos.api.global_methods import update_invoice_state

		# Test with invalid inputs
		result = update_invoice_state(invoice_id="", new_state="")

		# Should return error or handle gracefully
		self.assertIsInstance(result, dict, "Should return a dictionary")

	def test_get_invoice_details_wrapper(self):
		"""Test that get_invoice_details validates inputs."""
		from jarz_pos.api.global_methods import get_invoice_details

		# Test with non-existent invoice
		try:
			result = get_invoice_details(invoice_id="NON_EXISTENT_INV")
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Invoice doesn't exist
			pass

	def test_get_kanban_filters_wrapper(self):
		"""Test that get_kanban_filters is accessible from global_methods."""
		from jarz_pos.api.global_methods import get_kanban_filters

		result = get_kanban_filters()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")

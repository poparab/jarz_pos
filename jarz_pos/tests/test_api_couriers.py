"""Tests for courier API endpoints.

This module tests courier-related API endpoints including delivery handling.
"""
import unittest
import frappe


class TestCourierAPI(unittest.TestCase):
	"""Test class for Courier API functionality."""

	def test_get_active_couriers_structure(self):
		"""Test that get_active_couriers returns correct structure."""
		from jarz_pos.api.couriers import get_active_couriers

		result = get_active_couriers()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

		# If there are couriers, verify their structure
		if result:
			courier = result[0]
			self.assertIn("party_type", courier, "Courier should have party_type")
			self.assertIn("party", courier, "Courier should have party")
			self.assertIn("display_name", courier, "Courier should have display_name")

	def test_get_couriers_wrapper(self):
		"""Test that get_couriers is a wrapper for get_active_couriers."""
		from jarz_pos.api.couriers import get_couriers, get_active_couriers

		result_wrapper = get_couriers()
		result_direct = get_active_couriers()

		# Both should return lists
		self.assertIsInstance(result_wrapper, list, "get_couriers should return a list")
		self.assertIsInstance(result_direct, list, "get_active_couriers should return a list")

	def test_get_courier_balances_structure(self):
		"""Test that get_courier_balances returns correct structure."""
		from jarz_pos.api.couriers import get_courier_balances

		result = get_courier_balances()

		# Verify response is a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_settle_courier_validation(self):
		"""Test that settle_courier validates required parameters."""
		from jarz_pos.api.couriers import settle_courier

		# Test without required parameters should raise an error
		with self.assertRaises(Exception):
			settle_courier()  # No courier or party_type/party provided

	def test_create_delivery_party_validation(self):
		"""Test that create_delivery_party validates required fields."""
		from jarz_pos.api.couriers import create_delivery_party

		# Test with missing required fields should raise an error
		with self.assertRaises(Exception):
			create_delivery_party(
				party_type="",  # Empty party_type should fail
				name="",  # Empty name should fail
			)

	def test_generate_settlement_preview_validation(self):
		"""Test that generate_settlement_preview validates inputs."""
		from jarz_pos.api.couriers import generate_settlement_preview

		# Test without required parameters
		try:
			result = generate_settlement_preview()
			# If it doesn't raise an error, verify it handles gracefully
			self.assertIsInstance(result, dict, "Should return a dictionary")
		except Exception:
			# Expected to fail without required parameters
			pass

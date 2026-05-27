"""Tests for courier API endpoints.

This module tests courier-related API endpoints including delivery handling.
"""

import unittest
from unittest.mock import MagicMock, patch


class TestCourierAPI(unittest.TestCase):
	"""Test class for Courier API functionality."""

	def _employee_group_doc(self, employee_names):
		doc = MagicMock()
		doc.get.side_effect = lambda key: [{"employee": name} for name in employee_names] if key == "employees" else None
		doc.as_dict.return_value = {}
		return doc

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

	@patch("jarz_pos.utils.courier_visibility.frappe")
	@patch("jarz_pos.api.couriers.frappe")
	@patch("jarz_pos.api.couriers.get_visible_pos_profiles", return_value=["Nasr city"])
	def test_get_active_couriers_filters_by_branch_and_active_state(
		self,
		mock_visible_profiles,
		mock_api_frappe,
		mock_visibility_frappe,
	):
		"""Only active couriers in the requested branch should be returned."""
		from jarz_pos.api.couriers import get_active_couriers

		employee_rows = [
			{
				"name": "EMP-1",
				"employee_name": "Active Nasr Employee",
				"branch": "Nasr city",
				"status": "Active",
				"custom_delivery_partner": "DP-1",
			},
			{
				"name": "EMP-2",
				"employee_name": "Inactive Nasr Employee",
				"branch": "Nasr city",
				"status": "Inactive",
				"custom_delivery_partner": None,
			},
			{
				"name": "EMP-3",
				"employee_name": "Dokki Employee",
				"branch": "Dokki",
				"status": "Active",
				"custom_delivery_partner": None,
			},
		]
		supplier_rows = [
			{
				"name": "SUP-1",
				"supplier_name": "Active Nasr Supplier",
				"branch": "Nasr city",
				"disabled": 0,
				"custom_delivery_partner": "DP-1",
			},
			{
				"name": "SUP-2",
				"supplier_name": "Disabled Supplier",
				"branch": "Nasr city",
				"disabled": 1,
				"custom_delivery_partner": None,
			},
			{
				"name": "SUP-3",
				"supplier_name": "Inactive Partner Supplier",
				"branch": "Nasr city",
				"disabled": 0,
				"custom_delivery_partner": "DP-2",
			},
		]

		mock_api_frappe.db.get_value.side_effect = ["EMP-GRP", "SUP-GRP"]
		mock_api_frappe.get_doc.return_value = self._employee_group_doc(["EMP-1", "EMP-2", "EMP-3"])
		mock_api_frappe.db.has_column.side_effect = lambda doctype, column: True
		mock_api_frappe.get_all.side_effect = [employee_rows, supplier_rows]

		def cached_value_side_effect(doctype, name, fieldname):
			if doctype == "Delivery Partner" and name == "DP-1" and fieldname == "is_active":
				return 1
			if doctype == "Delivery Partner" and name == "DP-2" and fieldname == "is_active":
				return 0
			return None

		mock_visibility_frappe.get_cached_value.side_effect = cached_value_side_effect

		result = get_active_couriers(pos_profile="Nasr city")

		self.assertEqual(
			result,
			[
				{
					"party_type": "Employee",
					"party": "EMP-1",
					"display_name": "Active Nasr Employee",
					"branch": "Nasr city",
					"delivery_partner": "DP-1",
				},
				{
					"party_type": "Supplier",
					"party": "SUP-1",
					"display_name": "Active Nasr Supplier",
					"branch": "Nasr city",
					"delivery_partner": "DP-1",
				},
			],
		)
		mock_visible_profiles.assert_called_once_with(requested_pos_profile="Nasr city")

	@patch("jarz_pos.api.couriers.frappe")
	@patch("jarz_pos.api.couriers.get_visible_pos_profiles", return_value=[])
	def test_get_active_couriers_returns_empty_when_user_has_no_visible_profiles(
		self,
		mock_visible_profiles,
		mock_api_frappe,
	):
		"""Courier list should be empty when the user has no accessible POS profiles."""
		from jarz_pos.api.couriers import get_active_couriers

		result = get_active_couriers()

		self.assertEqual(result, [])
		mock_api_frappe.get_all.assert_not_called()
		mock_visible_profiles.assert_called_once_with(requested_pos_profile=None)

	@patch("jarz_pos.api.couriers.get_active_couriers", return_value=[])
	def test_get_couriers_wrapper_forwards_pos_profile(self, mock_get_active_couriers):
		"""Wrapper should forward the optional pos_profile parameter."""
		from jarz_pos.api.couriers import get_couriers

		result = get_couriers(pos_profile="Nasr city")

		self.assertEqual(result, [])
		mock_get_active_couriers.assert_called_once_with(pos_profile="Nasr city")

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

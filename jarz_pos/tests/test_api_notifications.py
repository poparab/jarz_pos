"""Tests for notification API endpoints.

This module tests notification and polling API endpoints.
"""

import unittest


class TestNotificationAPI(unittest.TestCase):
	"""Test class for Notification API functionality."""

	def test_get_recent_invoices_structure(self):
		"""Test that get_recent_invoices returns correct structure."""
		from jarz_pos.api.notifications import get_recent_invoices

		result = get_recent_invoices()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("new_invoices", result, "Should include new_invoices")
		self.assertIn("modified_invoices", result, "Should include modified_invoices")
		self.assertIsInstance(result.get("new_invoices"), list, "new_invoices should be a list")
		self.assertIsInstance(result.get("modified_invoices"), list, "modified_invoices should be a list")
		self.assertIn("timestamp", result, "Should include timestamp")
		self.assertIn("total_count", result, "Should include total_count")

	def test_get_recent_invoices_minutes_parameter(self):
		"""Test that get_recent_invoices respects minutes parameter."""
		from jarz_pos.api.notifications import get_recent_invoices

		# Test with different minutes values
		result_5 = get_recent_invoices(minutes=5)
		result_60 = get_recent_invoices(minutes=60)

		# Both should return valid structures
		self.assertTrue(result_5.get("success"), "Should return success=True for 5 minutes")
		self.assertTrue(result_60.get("success"), "Should return success=True for 60 minutes")
		self.assertEqual(result_5.get("minutes_checked"), 5, "Should echo minutes parameter")
		self.assertEqual(result_60.get("minutes_checked"), 60, "Should echo minutes parameter")

	def test_check_for_updates_structure(self):
		"""Test that check_for_updates returns correct structure."""
		from jarz_pos.api.notifications import check_for_updates

		result = check_for_updates()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("has_updates", result, "Should include has_updates")
		self.assertIn("last_check", result, "Should include last_check")
		self.assertIn("current_time", result, "Should include current_time")

	def test_test_websocket_emission_structure(self):
		"""Test that test_websocket_emission returns correct structure."""
		from jarz_pos.api.notifications import test_websocket_emission

		result = test_websocket_emission()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("timestamp", result, "Should include timestamp")
		self.assertIn("events_sent", result, "Should include events_sent")
		self.assertIn("test_invoice_id", result, "Should include test_invoice_id")

		# Verify events_sent is a list
		self.assertIsInstance(result["events_sent"], list, "events_sent should be a list")

	def test_get_websocket_debug_info_structure(self):
		"""Test that get_websocket_debug_info returns correct structure."""
		from jarz_pos.api.notifications import get_websocket_debug_info

		result = get_websocket_debug_info()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("timestamp", result, "Should include timestamp")

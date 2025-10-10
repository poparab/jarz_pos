"""Tests for delivery slots API endpoints.

This module tests delivery slot management endpoints.
"""

import unittest


class TestDeliverySlotsAPI(unittest.TestCase):
	"""Test class for Delivery Slots API functionality."""

	def test_get_available_delivery_slots_structure(self):
		"""Test that get_available_delivery_slots returns correct structure."""
		from jarz_pos.api.delivery_slots import get_available_delivery_slots

		result = get_available_delivery_slots()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("slots", result, "Should include slots")

		# Verify slots is a list
		self.assertIsInstance(result["slots"], list, "Slots should be a list")

	def test_get_available_delivery_slots_date_parameter(self):
		"""Test that get_available_delivery_slots accepts date parameter."""
		from jarz_pos.api.delivery_slots import get_available_delivery_slots

		# Test with a specific date
		result = get_available_delivery_slots(date="2025-01-01")

		# Should return valid structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")

	def test_get_next_available_slot_structure(self):
		"""Test that get_next_available_slot returns correct structure."""
		from jarz_pos.api.delivery_slots import get_next_available_slot

		result = get_next_available_slot()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")

		# Should include either a slot or indicate none available
		if result.get("slot"):
			slot = result["slot"]
			self.assertIsInstance(slot, dict, "Slot should be a dictionary")
		else:
			self.assertIn("message", result, "Should include message if no slot available")

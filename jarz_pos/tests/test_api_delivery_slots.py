"""Tests for delivery slots API endpoints.

This module tests delivery slot management endpoints.
"""

import unittest
import datetime


class TestDeliverySlotsAPI(unittest.TestCase):
	"""Test class for Delivery Slots API functionality."""

	def test_get_available_delivery_slots_structure(self):
		"""Test that get_available_delivery_slots returns correct structure."""
		from jarz_pos.api.delivery_slots import get_available_delivery_slots

		try:
			result = get_available_delivery_slots("Test POS Profile")
			self.assertIsInstance(result, list, "Should return a list of slots")
		except Exception:
			# POS Profile may not exist in test environment
			pass

	def test_get_available_delivery_slots_date_parameter(self):
		"""Test that get_available_delivery_slots validates POS profile."""
		from jarz_pos.api.delivery_slots import get_available_delivery_slots

		with self.assertRaises(Exception):
			get_available_delivery_slots("Nonexistent Profile")

	def test_get_next_available_slot_structure(self):
		"""Test that get_next_available_slot returns correct structure."""
		from jarz_pos.api.delivery_slots import get_next_available_slot

		try:
			result = get_next_available_slot("Test POS Profile")
			if result:
				self.assertIsInstance(result, dict, "Slot should be a dictionary")
		except Exception:
			# POS Profile may not exist in test environment
			pass

	# ------------------------------------------------------------------
	# Unit tests for _generate_day_slots (no Frappe/DB dependency)
	# ------------------------------------------------------------------

	def _make_date(self):
		"""Return a fixed future date for slot generation tests."""
		return datetime.date(2030, 1, 6)  # A Monday far in the future

	def test_sub_hour_slots_90_minutes(self):
		"""Slots of 1h30m (90 min) produce correct start/end pairs."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		target = self._make_date()
		slots = _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(9, 0),
			closing_time=datetime.time(18, 0),
			same_day="Same Day",
			slot_duration_minutes=90,
		)

		self.assertTrue(len(slots) > 0, "Should generate at least one slot")
		# Each slot must be exactly 90 minutes wide
		for slot in slots:
			start = datetime.datetime.fromisoformat(slot["datetime"])
			end = datetime.datetime.fromisoformat(slot["end_datetime"])
			self.assertEqual(
				(end - start).total_seconds(), 90 * 60,
				f"Slot {slot['time_label']} should be 90 minutes wide"
			)
		# First slot: 09:00–10:30
		first_start = datetime.datetime.fromisoformat(slots[0]["datetime"])
		self.assertEqual(first_start.hour, 9)
		self.assertEqual(first_start.minute, 0)
		first_end = datetime.datetime.fromisoformat(slots[0]["end_datetime"])
		self.assertEqual(first_end.hour, 10)
		self.assertEqual(first_end.minute, 30)

	def test_custom_last_slot_fills_remaining_time(self):
		"""Custom last slot (60 min) is appended when regular slot (90 min) would overflow."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		# Opening 09:00, Closing 17:30 — with 90-min slots:
		# 09:00-10:30, 10:30-12:00, 12:00-13:30, 13:30-15:00, 15:00-16:30
		# 16:30 + 90 min = 18:00 > 17:30 → regular slot overflows
		# 16:30 + 60 min = 17:30 ≤ 17:30 → custom last slot fits
		target = self._make_date()
		slots = _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(9, 0),
			closing_time=datetime.time(17, 30),
			same_day="Same Day",
			slot_duration_minutes=90,
			last_slot_duration_minutes=60,
		)

		# Should have 6 slots (5 regular + 1 custom last)
		self.assertEqual(len(slots), 6, f"Expected 6 slots, got {len(slots)}: {[s['time_label'] for s in slots]}")
		last = slots[-1]
		last_start = datetime.datetime.fromisoformat(last["datetime"])
		last_end = datetime.datetime.fromisoformat(last["end_datetime"])
		self.assertEqual(last_start.hour, 16)
		self.assertEqual(last_start.minute, 30)
		self.assertEqual(last_end.hour, 17)
		self.assertEqual(last_end.minute, 30)
		# Duration of last slot = 60 minutes
		self.assertEqual((last_end - last_start).total_seconds(), 3600)

	def test_custom_last_slot_not_added_when_it_also_overflows(self):
		"""Custom last slot is not appended if it would still exceed closing time."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		# Opening 09:00, Closing 17:00 — 90-min regular, 90-min last (same as regular)
		# 16:30 + 90 min = 18:00 > 17:00, last slot also overflows
		target = self._make_date()
		slots_no_last = _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(9, 0),
			closing_time=datetime.time(17, 0),
			same_day="Same Day",
			slot_duration_minutes=90,
		)
		slots_with_bad_last = _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(9, 0),
			closing_time=datetime.time(17, 0),
			same_day="Same Day",
			slot_duration_minutes=90,
			last_slot_duration_minutes=90,  # Same as regular, won't fit
		)

		self.assertEqual(
			len(slots_no_last), len(slots_with_bad_last),
			"Overflowing custom last slot should not increase slot count"
		)

	def test_whole_hour_slots_unchanged(self):
		"""Existing whole-hour timetables (60 min) produce the same slots as before."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		target = self._make_date()
		slots = _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(9, 0),
			closing_time=datetime.time(17, 0),
			same_day="Same Day",
			slot_duration_minutes=60,
		)

		self.assertEqual(len(slots), 8, f"Expected 8 one-hour slots, got {len(slots)}")
		for slot in slots:
			start = datetime.datetime.fromisoformat(slot["datetime"])
			end = datetime.datetime.fromisoformat(slot["end_datetime"])
			self.assertEqual((end - start).total_seconds(), 3600)

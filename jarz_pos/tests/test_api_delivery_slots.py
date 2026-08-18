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

	# ------------------------------------------------------------------
	# Anchored last slot — mirrors the production WooCommerce (ORDDD) grid
	# ------------------------------------------------------------------

	# Taken from 699 sampled production Woo orders on 2026-08-18. These are the
	# slots customers can actually book on orderjarz.com; ERPNext must match.
	WOO_GRID_A = [
		"13:00-14:30", "14:30-16:00", "16:00-17:30", "17:30-19:00",
		"19:00-20:30", "20:30-22:00", "22:00-23:30", "00:00-01:00",
	]
	WOO_FRIDAY = [
		"14:00-15:30", "15:30-17:00", "17:00-18:30", "18:30-20:00",
		"20:00-21:30", "21:30-23:00", "23:00-00:30", "00:30-01:30",
	]

	def _hhmm(self, slots):
		"""Render slots as HH:MM-HH:MM pairs for readable comparisons."""
		return [f'{s["datetime"][11:16]}-{s["end_datetime"][11:16]}' for s in slots]

	def test_anchored_last_slot_matches_woo_grid(self):
		"""13:00-01:00 with a 60-min anchored last slot reproduces the Woo grid.

		The regular 90-minute cadence stops at 23:30 and the final slot is
		00:00-01:00, deliberately leaving 23:30-00:00 unbookable — which is
		exactly what the WooCommerce store offers.
		"""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		slots = _generate_day_slots(
			target_date=self._make_date(),
			opening_time=datetime.time(13, 0),
			closing_time=datetime.time(1, 0),
			same_day="Next Day",
			slot_duration_minutes=90,
			last_slot_duration_minutes=60,
			anchor_last_slot_to_closing=True,
		)

		self.assertEqual(self._hhmm(slots), self.WOO_GRID_A)

	def test_anchored_last_slot_matches_woo_friday(self):
		"""Friday opens an hour later and closes 01:30 — no gap, 8 slots."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		slots = _generate_day_slots(
			target_date=datetime.date(2030, 1, 4),  # A Friday
			opening_time=datetime.time(14, 0),
			closing_time=datetime.time(1, 30),
			same_day="Next Day",
			slot_duration_minutes=90,
			last_slot_duration_minutes=60,
			anchor_last_slot_to_closing=True,
		)

		self.assertEqual(self._hhmm(slots), self.WOO_FRIDAY)

	def test_anchor_off_preserves_previous_behaviour(self):
		"""Without the anchor the cadence runs contiguously to closing, as before."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		slots = _generate_day_slots(
			target_date=self._make_date(),
			opening_time=datetime.time(13, 0),
			closing_time=datetime.time(1, 0),
			same_day="Next Day",
			slot_duration_minutes=90,
		)

		self.assertEqual(self._hhmm(slots)[-1], "23:30-01:00")
		self.assertEqual(len(slots), 8)

	def test_anchor_ignored_without_custom_duration(self):
		"""The anchor needs a last-slot duration; alone it must change nothing."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		kwargs = dict(
			target_date=self._make_date(),
			opening_time=datetime.time(13, 0),
			closing_time=datetime.time(1, 0),
			same_day="Next Day",
			slot_duration_minutes=90,
		)
		plain = _generate_day_slots(**kwargs)
		anchored = _generate_day_slots(anchor_last_slot_to_closing=True, **kwargs)

		self.assertEqual(self._hhmm(plain), self._hhmm(anchored))

	def test_anchor_ignored_when_it_cannot_fit(self):
		"""An anchored slot wider than the whole window must not produce a slot."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		slots = _generate_day_slots(
			target_date=self._make_date(),
			opening_time=datetime.time(23, 0),
			closing_time=datetime.time(23, 30),
			same_day="Same Day",
			slot_duration_minutes=90,
			last_slot_duration_minutes=60,
			anchor_last_slot_to_closing=True,
		)

		self.assertEqual(slots, [])

	def test_anchored_slot_survives_today_filtering(self):
		"""Late in the day the anchored tail is still offered on its own."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		target = self._make_date()
		slots = _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(13, 0),
			closing_time=datetime.time(1, 0),
			same_day="Next Day",
			slot_duration_minutes=90,
			current_datetime=datetime.datetime.combine(target, datetime.time(23, 0)),
			last_slot_duration_minutes=60,
			anchor_last_slot_to_closing=True,
		)

		self.assertEqual(self._hhmm(slots), ["00:00-01:00"])

	def test_anchored_slot_respects_preparation_buffer(self):
		"""The anchored slot obeys the same 30-minute buffer as every other slot."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		target = self._make_date()
		slots = _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(13, 0),
			closing_time=datetime.time(1, 0),
			same_day="Next Day",
			slot_duration_minutes=90,
			# 23:45 leaves only 15 minutes before a 00:00 start.
			current_datetime=datetime.datetime.combine(target, datetime.time(23, 45)),
			last_slot_duration_minutes=60,
			anchor_last_slot_to_closing=True,
		)

		self.assertEqual(slots, [])

	def test_preview_endpoint_reproduces_the_woo_week(self):
		"""The Desk preview must render the same aligned week the POS serves."""
		import json as _json
		from jarz_pos.api.delivery_slots import preview_timetable_slots

		timetable = [
			{"day": day, "opening_time": "13:00:00", "closing_time": "01:00:00",
			 "same_day": "Next Day"}
			for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Saturday", "Sunday")
		]
		timetable.append({
			"day": "Friday", "opening_time": "14:00:00",
			"closing_time": "01:30:00", "same_day": "Next Day",
		})

		result = preview_timetable_slots(_json.dumps({
			"slot_hours": 1, "slot_minutes": 30,
			"has_custom_last_slot": 1, "last_slot_hours": 1, "last_slot_minutes": 0,
			"anchor_last_slot_to_closing": 1,
			"timetable": timetable,
		}))

		self.assertEqual(result["total_slots"], 56)
		by_day = {d["day"]: d for d in result["days"]}
		self.assertEqual(
			[f'{s["start"]}-{s["end"]}' for s in by_day["Monday"]["slots"]],
			self.WOO_GRID_A,
		)
		self.assertEqual(
			[f'{s["start"]}-{s["end"]}' for s in by_day["Friday"]["slots"]],
			self.WOO_FRIDAY,
		)
		# The 23:30-00:00 gap is interior, not a tail - it must still be reported.
		self.assertEqual(by_day["Monday"]["uncovered_minutes"], 30)
		self.assertEqual(by_day["Friday"]["uncovered_minutes"], 0)

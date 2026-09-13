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

	def _today_slots(self, now, target=None):
		"""The Woo grid A day, filtered against ``now`` the way the POS sees it."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		target = target or self._make_date()
		return _generate_day_slots(
			target_date=target,
			opening_time=datetime.time(13, 0),
			closing_time=datetime.time(1, 0),
			same_day="Next Day",
			slot_duration_minutes=90,
			current_datetime=now,
			last_slot_duration_minutes=60,
			anchor_last_slot_to_closing=True,
		)

	def _at(self, hh, mm, days=0):
		return datetime.datetime.combine(
			self._make_date() + datetime.timedelta(days=days), datetime.time(hh, mm)
		)

	def _current(self, slots):
		return [self._hhmm([s])[0] for s in slots if s["is_current"]]

	def test_anchored_slot_survives_today_filtering(self):
		"""Late in the day the anchored tail is still offered after the running slot."""
		slots = self._today_slots(self._at(23, 0))

		self.assertEqual(self._hhmm(slots), ["22:00-23:30", "00:00-01:00"])
		self.assertEqual(self._current(slots), ["22:00-23:30"])

	def test_no_preparation_buffer_2159_still_gets_the_2200_slot(self):
		"""Order 17343: at 21:41 (and even 21:59) the next slot is 22:00, not 00:00."""
		for now in (self._at(21, 41), self._at(21, 59)):
			slots = self._today_slots(now)
			upcoming = [s for s in slots if not s["is_current"]]

			self.assertEqual(self._hhmm(upcoming)[0], "22:00-23:30", now)
			self.assertEqual(self._current(slots), ["20:30-22:00"], now)

	def test_a_slot_starting_exactly_now_is_upcoming_not_current(self):
		slots = self._today_slots(self._at(22, 0))

		self.assertEqual(self._hhmm(slots)[0], "22:00-23:30")
		self.assertEqual(self._current(slots), [])

	def test_the_running_slot_is_offered_until_it_ends(self):
		"""At 22:10 the 22:00 slot is still bookable, flagged as current."""
		slots = self._today_slots(self._at(22, 10))

		self.assertEqual(self._hhmm(slots), ["22:00-23:30", "00:00-01:00"])
		self.assertEqual(self._current(slots), ["22:00-23:30"])

	def test_the_unbookable_gap_has_no_current_slot(self):
		"""At 23:45 nothing is running; 00:00 is simply the next slot."""
		slots = self._today_slots(self._at(23, 45))

		self.assertEqual(self._hhmm(slots), ["00:00-01:00"])
		self.assertEqual(self._current(slots), [])

	def test_yesterdays_after_midnight_slot_is_current_past_midnight(self):
		"""At 00:20 the 00:00-01:00 tail of yesterday's day is the running slot."""
		slots = self._today_slots(self._at(0, 20, days=1))

		self.assertEqual(self._hhmm(slots), ["00:00-01:00"])
		self.assertEqual(self._current(slots), ["00:00-01:00"])
		self.assertEqual(slots[0]["business_date"], self._make_date().isoformat())

	def test_day_labels_follow_the_site_clock_not_the_container(self):
		"""At 00:20 site time the running after-midnight slot is Today, not Tomorrow."""
		from unittest import mock
		from jarz_pos.api import delivery_slots

		now = self._at(0, 20, days=1)
		with mock.patch.object(delivery_slots.frappe.utils, "now_datetime", return_value=now):
			slots = self._today_slots(now)
			upcoming = self._today_slots(now, target=now.date())

		self.assertEqual(slots[0]["day_label"], "Today")
		self.assertTrue(slots[0]["label"].startswith("Today, "))
		self.assertEqual(upcoming[0]["day_label"], "Today")

	def _friday_slots(self, now):
		"""The production Friday grid (14:00-01:30), filtered against ``now``."""
		from jarz_pos.api.delivery_slots import _generate_day_slots

		return _generate_day_slots(
			target_date=datetime.date(2030, 1, 4),  # A Friday
			opening_time=datetime.time(14, 0),
			closing_time=datetime.time(1, 30),
			same_day="Next Day",
			slot_duration_minutes=90,
			current_datetime=now,
			last_slot_duration_minutes=60,
			anchor_last_slot_to_closing=True,
		)

	def _with_clock(self, now, fn):
		from unittest import mock
		from jarz_pos.api import delivery_slots

		with mock.patch.object(delivery_slots.frappe.utils, "now_datetime", return_value=now):
			return fn()

	def test_friday_night_tail_is_today_past_midnight(self):
		"""Saturday 00:10: the running 23:00 slot and the 00:30 default are Today.

		Both used to read "Friday", sat above "Today" in both pickers, and the
		label was saved onto the invoice.
		"""
		now = datetime.datetime(2030, 1, 5, 0, 10)
		slots = self._with_clock(now, lambda: self._friday_slots(now))

		self.assertEqual(self._hhmm(slots), ["23:00-00:30", "00:30-01:30"])
		self.assertEqual(self._current(slots), ["23:00-00:30"])
		self.assertEqual([s["day_label"] for s in slots], ["Today", "Today"])
		self.assertTrue(all(s["label"].startswith("Today, ") for s in slots))
		# Dates and the business day are unchanged: only the label moved.
		self.assertEqual([s["date"] for s in slots], ["2030-01-04", "2030-01-05"])
		self.assertEqual({s["business_date"] for s in slots}, {"2030-01-04"})

	def test_friday_night_tail_at_exactly_midnight(self):
		"""00:00:00 is already Saturday: nothing of Friday's tail reads Friday."""
		now = datetime.datetime(2030, 1, 5, 0, 0, 0)
		friday = self._with_clock(now, lambda: self._friday_slots(now))
		grid_a = self._with_clock(
			now, lambda: self._today_slots(now, target=datetime.date(2030, 1, 4))
		)

		self.assertEqual(self._hhmm(friday), ["23:00-00:30", "00:30-01:30"])
		self.assertEqual([s["day_label"] for s in friday], ["Today", "Today"])
		# A tail slot starting exactly at midnight is upcoming, and still Today.
		self.assertEqual(self._hhmm(grid_a), ["00:00-01:00"])
		self.assertEqual(self._current(grid_a), [])
		self.assertEqual(grid_a[0]["day_label"], "Today")

	def test_before_midnight_the_tail_keeps_its_business_day(self):
		"""At 23:59:59 Friday is still today; the labels are not touched."""
		now = datetime.datetime(2030, 1, 4, 23, 59, 59)
		slots = self._with_clock(now, lambda: self._friday_slots(now))

		self.assertEqual([s["day_label"] for s in slots], ["Today", "Today"])

	def test_unfiltered_slots_keep_business_day_labels(self):
		"""The timetable preview passes no clock, so nothing is relabelled."""
		from jarz_pos.api.delivery_slots import _build_slot

		now = datetime.datetime(2030, 1, 5, 0, 10)
		slot = self._with_clock(now, lambda: _build_slot(
			datetime.date(2030, 1, 4),
			datetime.datetime(2030, 1, 5, 0, 30),
			datetime.datetime(2030, 1, 5, 1, 30),
		))

		self.assertEqual(slot["day_label"], "Friday")

	def test_the_picker_never_lists_a_weekday_above_today(self):
		"""Through the endpoint: past midnight on Saturday the list starts with Today."""
		from types import SimpleNamespace
		from unittest import mock
		from jarz_pos.api import delivery_slots

		config = SimpleNamespace(
			name="TT", slot_hours=1, slot_minutes=30, has_custom_last_slot=1,
			last_slot_hours=1, last_slot_minutes=0, anchor_last_slot_to_closing=1,
		)

		def timing(day, opening, closing):
			return SimpleNamespace(
				day=day, opening_time=opening, closing_time=closing, same_day="Next Day",
				get=lambda key, default=None: "Next Day" if key == "same_day" else default,
			)

		timings = [
			timing(day, datetime.timedelta(hours=13), datetime.timedelta(hours=1))
			for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Saturday", "Sunday")
		]
		timings.append(
			timing("Friday", datetime.timedelta(hours=14), datetime.timedelta(hours=1, minutes=30))
		)

		with mock.patch("jarz_pos.utils.validation_utils.assert_pos_profile_enabled"), \
			mock.patch.object(delivery_slots.frappe, "get_value", return_value=config), \
			mock.patch.object(delivery_slots.frappe, "get_all", return_value=timings), \
			mock.patch.object(
				delivery_slots.frappe.utils, "now_datetime",
				return_value=datetime.datetime(2030, 1, 5, 0, 10),
			):
			slots = delivery_slots.get_available_delivery_slots("Test POS Profile")

		labels = [s["day_label"] for s in slots]
		self.assertEqual(labels[:3], ["Today", "Today", "Today"])
		self.assertNotIn("Friday", labels[: labels.index("Tomorrow")])
		self.assertTrue(slots[1]["is_default"])

	def test_an_ended_day_offers_nothing(self):
		self.assertEqual(self._today_slots(self._at(1, 0, days=1)), [])

	def test_available_slots_default_skips_the_running_slot(self):
		"""The running slot sorts first but the default is the next one."""
		from types import SimpleNamespace
		from unittest import mock
		from jarz_pos.api import delivery_slots

		target = self._make_date()
		config = SimpleNamespace(
			name="TT", slot_hours=1, slot_minutes=30, has_custom_last_slot=1,
			last_slot_hours=1, last_slot_minutes=0, anchor_last_slot_to_closing=1,
		)
		timings = [
			SimpleNamespace(
				day=day, opening_time=datetime.timedelta(hours=13),
				closing_time=datetime.timedelta(hours=1), same_day="Next Day",
				get=lambda key, default=None, _d=day: "Next Day" if key == "same_day" else default,
			)
			for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
		]

		with mock.patch("jarz_pos.utils.validation_utils.assert_pos_profile_enabled"), \
			mock.patch.object(delivery_slots.frappe, "get_value", return_value=config), \
			mock.patch.object(delivery_slots.frappe, "get_all", return_value=timings), \
			mock.patch.object(
				delivery_slots.frappe.utils, "now_datetime",
				return_value=datetime.datetime.combine(target, datetime.time(22, 10)),
			):
			slots = delivery_slots.get_available_delivery_slots("Test POS Profile")
			next_slot = delivery_slots.get_next_available_slot("Test POS Profile")

		self.assertTrue(slots[0]["is_current"])
		self.assertFalse(slots[0]["is_default"])
		self.assertEqual(self._hhmm(slots[:2]), ["22:00-23:30", "00:00-01:00"])
		self.assertTrue(slots[1]["is_default"])
		self.assertEqual(sum(1 for s in slots if s["is_default"]), 1)
		self.assertEqual(self._hhmm([next_slot]), ["00:00-01:00"])

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

	# ------------------------------------------------------------------
	# The stored delivery window must be a slot the timetable really sells
	# ------------------------------------------------------------------

	def test_anchored_slot_dates_the_real_start_not_the_business_day(self):
		"""A 13:00-01:00 day ends after midnight; ``date`` must say so.

		The kanban reschedule dialog posts ``date`` + ``time`` verbatim, so a
		business-day ``date`` on the anchored last slot moved the order a full
		day early.
		"""
		from jarz_pos.api.delivery_slots import _build_slot

		slot = _build_slot(
			datetime.date(2030, 1, 6),
			datetime.datetime(2030, 1, 7, 0, 0),
			datetime.datetime(2030, 1, 7, 1, 0),
		)

		self.assertEqual(slot["date"], "2030-01-07")
		self.assertEqual(slot["time"], "00:00:00")
		self.assertEqual(slot["business_date"], "2030-01-06")

	def _normalize(self, start, end=None, slots=None, now=None, explicit=None):
		"""Run normalize_delivery_window against a fixed grid and clock."""
		from unittest import mock
		from jarz_pos.api import delivery_slots

		grid = [
			{"datetime": "2030-01-07T00:00:00", "end_datetime": "2030-01-07T01:00:00"},
			{"datetime": "2030-01-07T13:00:00", "end_datetime": "2030-01-07T14:30:00"},
		] if slots is None else slots

		with mock.patch.object(
			delivery_slots, "get_available_delivery_slots", side_effect=lambda _p: list(grid)
		), mock.patch.object(
			delivery_slots.frappe.utils,
			"now_datetime",
			return_value=now or datetime.datetime(2030, 1, 6, 22, 19, 21),
		):
			return delivery_slots.normalize_delivery_window(
				"Test POS Profile", start, end, explicit=explicit
			)

	def test_a_slot_that_has_passed_snaps_to_the_next_one(self):
		"""The 16906 case: a cart left open submits a slot that already started."""
		start, end, note = self._normalize(
			datetime.datetime(2030, 1, 6, 22, 0), datetime.datetime(2030, 1, 6, 23, 30)
		)

		self.assertEqual(note, "snapped")
		self.assertEqual(start, datetime.datetime(2030, 1, 7, 0, 0))
		self.assertEqual(end, datetime.datetime(2030, 1, 7, 1, 0))

	def test_a_deliberately_picked_running_slot_is_kept(self):
		"""Staff chose the slot in progress; it must not be snapped to the next one."""
		grid = [
			{"datetime": "2030-01-06T22:00:00", "end_datetime": "2030-01-06T23:30:00", "is_current": True},
			{"datetime": "2030-01-07T00:00:00", "end_datetime": "2030-01-07T01:00:00"},
		]
		start, end, note = self._normalize(
			datetime.datetime(2030, 1, 6, 22, 0), slots=grid, explicit=True
		)

		self.assertEqual(note, "matched")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 22, 0))
		self.assertEqual(end, datetime.datetime(2030, 1, 6, 23, 30))

	RUNNING_GRID = [
		{"datetime": "2030-01-06T21:00:00", "end_datetime": "2030-01-06T22:30:00", "is_current": True},
		{"datetime": "2030-01-06T22:30:00", "end_datetime": "2030-01-07T00:00:00"},
	]

	def _running(self, hh, mm, explicit, slots=None):
		"""Submit the 21:00 start of the running slot at ``hh:mm``."""
		return self._normalize(
			datetime.datetime(2030, 1, 6, 21, 0), datetime.datetime(2030, 1, 6, 22, 30),
			slots=self.RUNNING_GRID if slots is None else slots,
			now=datetime.datetime(2030, 1, 6, hh, mm), explicit=explicit,
		)

	def test_an_aged_default_on_the_running_slot_snaps_to_the_next_one(self):
		"""Cart opened 20:50 with the 21:00 default, submitted 21:20.

		The app's stale-slot refresh failed (or the device clock ran behind), so
		the start still says 21:00. The POS says it pre-selected the slot: book
		the next slot, 22:30, not the running one.
		"""
		start, end, note = self._running(21, 20, explicit=False)

		self.assertEqual(note, "snapped")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 22, 30))
		self.assertEqual(end, datetime.datetime(2030, 1, 7, 0, 0))

	def test_an_explicit_pick_of_the_running_slot_is_kept(self):
		start, end, note = self._running(21, 20, explicit=True)

		self.assertEqual(note, "matched")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 21, 0))
		self.assertEqual(end, datetime.datetime(2030, 1, 6, 22, 30))

	def test_a_client_that_sends_no_flag_keeps_the_running_slot(self):
		"""An app without the patch cannot say it was a pick; it keeps what it had."""
		start, end, note = self._running(21, 20, explicit=None)

		self.assertEqual(note, "matched")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 21, 0))
		self.assertEqual(end, datetime.datetime(2030, 1, 6, 22, 30))

	def test_a_default_that_just_started_is_kept_within_the_grace(self):
		"""Sent at 20:59:58 on the device, handled at 21:00:01 on the server.

		The app saw an upcoming slot and sent no pick; the server sees it running.
		Within the grace period that is the boundary, not a stale cart.
		"""
		from jarz_pos.api import delivery_slots

		self.assertEqual(delivery_slots.RUNNING_SLOT_GRACE_MINUTES, 5)
		for hh, mm in ((21, 0), (21, 4), (21, 5)):
			start, _end, note = self._running(hh, mm, explicit=False)

			self.assertEqual(note, "matched", (hh, mm))
			self.assertEqual(start, datetime.datetime(2030, 1, 6, 21, 0), (hh, mm))

		start, _end, note = self._running(21, 6, explicit=False)
		self.assertEqual(note, "snapped")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 22, 30))

	def test_with_nothing_later_the_running_slot_is_kept_not_invented(self):
		"""No slot left to snap to: the running slot beats "now + 5 minutes"."""
		only_running = self.RUNNING_GRID[:1]
		start, end, note = self._running(21, 20, explicit=False, slots=only_running)

		self.assertEqual(note, "matched")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 21, 0))
		self.assertEqual(end, datetime.datetime(2030, 1, 6, 22, 30))

	def test_the_flag_is_irrelevant_for_a_slot_that_has_not_started(self):
		for explicit in (None, False, True):
			start, _end, note = self._normalize(
				datetime.datetime(2030, 1, 6, 22, 30), slots=self.RUNNING_GRID,
				now=datetime.datetime(2030, 1, 6, 21, 20), explicit=explicit,
			)

			self.assertEqual(note, "matched", explicit)
			self.assertEqual(start, datetime.datetime(2030, 1, 6, 22, 30), explicit)

	def test_the_explicit_flag_is_read_from_the_request(self):
		from unittest import mock
		from jarz_pos.services import invoice_creation

		cases = [
			(None, None), ("", None), ("  ", None),
			("0", False), (0, False), (False, False), ("false", False),
			("1", True), (1, True), (True, True), ("true", True), (" Yes ", True),
		]
		for raw, expected in cases:
			form = {} if raw is None else {"delivery_slot_explicit": raw}
			with mock.patch.object(invoice_creation.frappe, "form_dict", form, create=True):
				self.assertIs(invoice_creation._requested_delivery_slot_explicit(), expected, raw)

	def test_invoice_creation_forwards_the_explicit_flag(self):
		from unittest import mock
		from jarz_pos.services import invoice_creation
		from jarz_pos.api import delivery_slots

		start = datetime.datetime(2030, 1, 6, 21, 0)
		for raw, expected in ((None, None), ("1", True), ("0", False)):
			form = {} if raw is None else {"delivery_slot_explicit": raw}
			with mock.patch.object(invoice_creation.frappe, "form_dict", form, create=True), \
				mock.patch.object(
					delivery_slots, "normalize_delivery_window",
					return_value=(start, None, "matched"),
				) as normalize:
				invoice_creation._normalize_delivery_window("Test POS Profile", start, mock.Mock())

			self.assertIs(normalize.call_args.kwargs["explicit"], expected, raw)

	def test_an_amendment_keeping_its_own_window_counts_as_explicit(self):
		"""Editing an order's address while its slot runs must not move the order."""
		import frappe
		from jarz_pos.api import manager

		source = frappe._dict(custom_delivery_date="2030-01-06", custom_delivery_time_from="21:00:00")

		self.assertTrue(manager._is_source_delivery_start("2030-01-06 21:00:00", source))
		self.assertTrue(manager._is_source_delivery_start("2030-01-06 21:00:37", source))
		self.assertFalse(manager._is_source_delivery_start("2030-01-06 22:30:00", source))
		self.assertFalse(manager._is_source_delivery_start(None, source))
		self.assertFalse(manager._is_source_delivery_start("2030-01-06 21:00:00", frappe._dict()))

		decide = manager._amendment_delivery_slot_explicit
		# Its own start is explicit whatever the client sent.
		for flag in (None, "", "0", 0, "1"):
			self.assertIs(decide(flag, "2030-01-06 21:00:00", source), True, flag)
		# A different start carries the client's flag, including "not sent".
		self.assertIsNone(decide(None, "2030-01-06 22:30:00", source))
		self.assertIsNone(decide("", "2030-01-06 22:30:00", source))
		self.assertIs(decide("0", "2030-01-06 22:30:00", source), False)
		self.assertIs(decide(0, "2030-01-06 22:30:00", source), False)
		self.assertIs(decide("1", "2030-01-06 22:30:00", source), True)

	def test_the_amendment_form_context_writes_the_flag_it_is_given(self):
		"""The job's argument decides, not whatever the request happened to carry."""
		import frappe
		from jarz_pos.api import manager

		previous = getattr(frappe, "form_dict", None)
		try:
			for request_flag in (None, "1", "0"):
				frappe.form_dict = frappe._dict(
					{} if request_flag is None else {"delivery_slot_explicit": request_flag}
				)
				for given, expected in ((True, 1), (False, 0), (None, None)):
					with manager._temporary_invoice_creation_form_context(
						required_delivery_datetime="2030-01-06 22:30:00",
						delivery_slot_explicit=given,
					):
						self.assertEqual(
							frappe.form_dict.get("delivery_slot_explicit"), expected,
							(request_flag, given),
						)
				self.assertEqual(
					frappe.form_dict.get("delivery_slot_explicit"), request_flag, "restored"
				)
		finally:
			frappe.form_dict = previous

	def test_submit_invoice_amendment_passes_the_flag_to_the_job(self):
		"""Declared on the endpoint, so it survives the job leaving the request."""
		from types import SimpleNamespace
		from unittest.mock import MagicMock, patch
		from jarz_pos.api import manager

		source = SimpleNamespace(
			name="INV-SLOT-001", docstatus=1, pos_profile="Dokki", custom_kanban_profile="Dokki",
			custom_sales_invoice_state="Ready", sales_partner=None, custom_payment_method="Cash",
			custom_delivery_date="2030-01-06", custom_delivery_time_from="21:00:00",
			custom_delivery_duration=5400,
		)
		source.get = lambda key, default=None: getattr(source, key, default)

		for flag in ("1", "0", None):
			mock_frappe = MagicMock()
			mock_frappe.session.user = "manager@example.com"
			mock_frappe.get_doc.return_value = source
			mock_frappe.enqueue.return_value = {"success": True}
			with patch("jarz_pos.api.manager.frappe", mock_frappe), \
				patch("jarz_pos.api.manager._ensure_profile_scoped_invoice_access"), \
				patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None), \
				patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}):
				manager.submit_invoice_amendment(
					invoice_id="INV-SLOT-001", cart_json="[]", delivery_slot_explicit=flag,
				)

			self.assertEqual(
				mock_frappe.enqueue.call_args.kwargs["delivery_slot_explicit"], flag, flag
			)

	def test_a_passed_off_grid_start_never_snaps_onto_the_running_slot(self):
		grid = [
			{"datetime": "2030-01-06T20:30:00", "end_datetime": "2030-01-06T22:00:00", "is_current": True},
			{"datetime": "2030-01-06T22:00:00", "end_datetime": "2030-01-06T23:30:00"},
		]
		start, _end, note = self._normalize(
			datetime.datetime(2030, 1, 6, 19, 0), slots=grid,
			now=datetime.datetime(2030, 1, 6, 21, 41),
		)

		self.assertEqual(note, "snapped")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 22, 0))

	def test_a_real_slot_keeps_its_own_end_when_none_was_sent(self):
		"""A missing end must not fall back to the timetable's default length."""
		start, end, note = self._normalize(datetime.datetime(2030, 1, 7, 13, 0))

		self.assertEqual(note, "matched")
		self.assertEqual(end, datetime.datetime(2030, 1, 7, 14, 30))

	def test_seconds_on_a_stored_time_do_not_defeat_the_match(self):
		"""Time fields keep seconds; the grid is generated on whole minutes."""
		_start, end, note = self._normalize(datetime.datetime(2030, 1, 7, 13, 0, 45))

		self.assertEqual(note, "matched")
		self.assertEqual(end, datetime.datetime(2030, 1, 7, 14, 30))

	def test_a_future_off_grid_start_is_respected(self):
		"""An amendment on an older grid still gets the window it asked for."""
		start, end, note = self._normalize(
			datetime.datetime(2030, 1, 8, 17, 15), datetime.datetime(2030, 1, 8, 18, 45)
		)

		self.assertEqual(note, "kept")
		self.assertEqual(start, datetime.datetime(2030, 1, 8, 17, 15))
		self.assertEqual(end, datetime.datetime(2030, 1, 8, 18, 45))

	def test_an_impossible_end_is_dropped_rather_than_stored(self):
		"""An end before its start would render as a negative delivery window."""
		_start, end, note = self._normalize(
			datetime.datetime(2030, 1, 8, 17, 15), datetime.datetime(2030, 1, 8, 16, 0)
		)

		self.assertEqual(note, "kept")
		self.assertIsNone(end)

	def test_no_slots_left_is_reported_not_invented(self):
		"""With nothing left to offer, this module refuses to make a window up."""
		start, _end, note = self._normalize(datetime.datetime(2030, 1, 6, 22, 0), slots=[])

		self.assertEqual(note, "unresolved")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 22, 0))

	def test_a_failing_slot_lookup_never_takes_the_order_down(self):
		"""Slot normalisation is advisory; an exception must not block a sale."""
		from unittest import mock
		from jarz_pos.api import delivery_slots

		with mock.patch.object(
			delivery_slots,
			"get_available_delivery_slots",
			side_effect=RuntimeError("no timetable"),
		):
			start, _end, note = delivery_slots.normalize_delivery_window(
				"Test POS Profile", datetime.datetime(2030, 1, 6, 22, 0)
			)

		self.assertEqual(note, "unresolved")
		self.assertEqual(start, datetime.datetime(2030, 1, 6, 22, 0))


class TestRescheduleRefusesEndedSlot(unittest.TestCase):
	"""update_invoice_delivery_slot must not store an order in a finished window."""

	NOW = datetime.datetime(2030, 1, 6, 22, 30)

	def _assert(self, date, time, duration):
		from unittest import mock
		from jarz_pos.api import invoices

		with mock.patch.object(invoices.frappe.utils, "now_datetime", return_value=self.NOW):
			return invoices._assert_delivery_slot_not_ended(date, time, duration)

	def test_a_slot_that_ended_is_refused(self):
		import frappe

		with self.assertRaises(frappe.ValidationError) as ctx:
			self._assert("2030-01-06", "20:30:00", 90 * 60)  # ended 22:00

		self.assertIn("ended at 10:00 PM", str(ctx.exception))

	def test_a_slot_ending_exactly_now_is_refused(self):
		import frappe

		with self.assertRaises(frappe.ValidationError):
			self._assert("2030-01-06", "21:00:00", 90 * 60)  # ends 22:30

	def test_the_running_slot_is_still_allowed(self):
		self._assert("2030-01-06", "22:00:00", 90 * 60)  # ends 23:30

	def test_a_future_slot_is_allowed(self):
		self._assert("2030-01-07", "00:00:00", "3600")

	def test_an_unparseable_window_is_refused(self):
		import frappe

		with self.assertRaises(frappe.ValidationError):
			self._assert("not-a-date", "", 3600)

	def test_the_endpoint_refuses_before_touching_the_invoice(self):
		import frappe
		from unittest import mock
		from jarz_pos.api import invoices

		with mock.patch.object(invoices.frappe.utils, "now_datetime", return_value=self.NOW), \
			mock.patch.object(invoices.frappe, "get_doc") as get_doc:
			with self.assertRaises(frappe.ValidationError) as ctx:
				invoices.update_invoice_delivery_slot(
					"ACC-SINV-TEST", "2030-01-06", "20:30:00", 90 * 60, "Today, 08:30 PM - 10:00 PM"
				)

		get_doc.assert_not_called()
		self.assertNotIn("Failed to update delivery slot", str(ctx.exception))

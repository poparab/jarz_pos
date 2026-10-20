"""Stale Check Out -> Check In, the board's hints about it, and phone approval.

The HRMS PWA offers "Check Out" the morning after a forgotten check-out, and
staff press it on arrival (production, 10-20 Oct 2026: 18 Oct 16:03 IN, then
19 Oct 12:37 OUT + IN). What matters here:

* the 16 h (after an IN) and 4 h (after an OUT) boundaries, pinned on both
  sides -- one minute either way decides whether a real departure is rewritten;
* the rewrite never raises on its own failures (it runs inside a check-in), and
  the ONLY refusal is a second Check Out within 10 minutes of a Check In;
* a Shared Device phone is never approved as one person's phone.
"""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.services import attendance as attendance_service
from jarz_pos.services import branch_access as branch_access_service
from jarz_pos.services import checkin_device as dev
from jarz_pos.services import checkin_log_type as lt

ARRIVAL = datetime(2026, 10, 19, 12, 37)
TOKEN = "AbCdEfGhIjKlMnOpQrStUv"


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def _prev(time, log_type="IN", shift_start=None):
	return {"name": "PREV", "time": time, "log_type": log_type, "shift_start": shift_start}


def _run(previous, log_type="OUT", when=ARRIVAL, columns=True):
	"""Run the hook with the lookup and the schema check mocked; real frappe.throw."""
	doc = _Doc(employee="HR-EMP-1", time=when, log_type=log_type, name=None)
	with (
		patch.object(lt, "_columns_ready", return_value=columns),
		patch.object(lt, "_is_live_press", return_value=True),
		patch.object(lt, "_previous_checkin", return_value=previous) as lookup,
	):
		lt.correct_log_type(doc)
	return doc, lookup


# ---------------------------------------------------------------------------
# Task 1: the conversion rules
# ---------------------------------------------------------------------------


class TestStaleInConverts(unittest.TestCase):
	def test_an_out_twenty_hours_after_an_in_becomes_an_in(self):
		doc, _ = _run(_prev(datetime(2026, 10, 18, 16, 3), "IN", datetime(2026, 10, 18, 16, 0)))
		self.assertEqual(doc.log_type, "IN")
		self.assertEqual(doc.custom_original_log_type, "OUT")
		self.assertEqual(
			doc.custom_log_type_note,
			"Pressed Check Out at 12:37 but the last check-in was 20 h earlier (18 Oct 16:03), "
			"so this was recorded as a Check In. The shift on 18 Oct has no check-out.",
		)

	def test_exactly_sixteen_hours_keeps_the_out(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(hours=16), "IN"))
		self.assertEqual(doc.log_type, "OUT")
		self.assertIsNone(doc.get("custom_original_log_type"))
		self.assertIsNone(doc.get("custom_log_type_note"))

	def test_one_minute_past_sixteen_hours_converts(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(hours=16, minutes=1), "IN"))
		self.assertEqual(doc.log_type, "IN")

	def test_a_normal_shift_end_keeps_the_out(self):
		# 12.5 h, the longest real shift; and the one 13.6 h day on record.
		for hours in (9, 12.5, 13.6):
			doc, _ = _run(_prev(ARRIVAL - timedelta(hours=hours), "IN"))
			self.assertEqual(doc.log_type, "OUT", hours)

	def test_the_note_falls_back_to_the_wall_clock_day_without_a_shift(self):
		doc, _ = _run(_prev(datetime(2026, 10, 18, 16, 3), "IN", None))
		self.assertIn("The shift on 18 Oct has no check-out.", doc.custom_log_type_note)


class TestOutAfterOutConverts(unittest.TestCase):
	def test_an_out_five_hours_after_an_out_is_a_new_shift(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(hours=5), "OUT"))
		self.assertEqual(doc.log_type, "IN")
		self.assertEqual(doc.custom_original_log_type, "OUT")
		self.assertIn("already a Check Out 5 h earlier", doc.custom_log_type_note)

	def test_exactly_four_hours_keeps_the_out(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(hours=4), "OUT"))
		self.assertEqual(doc.log_type, "OUT")

	def test_one_minute_past_four_hours_converts(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(hours=4, minutes=1), "OUT"))
		self.assertEqual(doc.log_type, "IN")

	def test_a_quick_double_tap_of_out_at_shift_end_stays_out(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(minutes=2), "OUT"))
		self.assertEqual(doc.log_type, "OUT")


class TestNoPreviousConverts(unittest.TestCase):
	def test_nothing_in_seven_days_becomes_an_in(self):
		doc, _ = _run(None)
		self.assertEqual(doc.log_type, "IN")
		self.assertEqual(doc.custom_original_log_type, "OUT")
		self.assertIn("no check-in in the 7 days", doc.custom_log_type_note)


class TestKeptAsIs(unittest.TestCase):
	def test_a_blank_previous_log_type_keeps_the_out(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(hours=20), None))
		self.assertEqual(doc.log_type, "OUT")
		self.assertIsNone(doc.get("custom_original_log_type"))

	def test_an_in_press_is_never_touched_or_looked_up(self):
		doc, lookup = _run(None, log_type="IN")
		self.assertEqual(doc.log_type, "IN")
		self.assertIsNone(doc.get("custom_original_log_type"))
		lookup.assert_not_called()

	def test_a_blank_press_is_never_touched(self):
		doc, lookup = _run(None, log_type=None)
		self.assertIsNone(doc.log_type)
		lookup.assert_not_called()


class TestSecondPressRefusal(unittest.TestCase):
	def test_out_seconds_after_an_in_is_refused(self):
		with self.assertRaises(lt.CheckOutTooSoonError) as ctx:
			_run(_prev(ARRIVAL - timedelta(seconds=40), "IN"))
		self.assertIn("12:36", str(ctx.exception))
		self.assertTrue(issubclass(lt.CheckOutTooSoonError, frappe.ValidationError))

	def test_nine_minutes_is_still_refused(self):
		with self.assertRaises(lt.CheckOutTooSoonError):
			_run(_prev(ARRIVAL - timedelta(minutes=9, seconds=59), "IN"))

	def test_exactly_ten_minutes_is_a_real_departure(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(minutes=10), "IN"))
		self.assertEqual(doc.log_type, "OUT")

	def test_a_quick_out_after_an_out_is_not_refused(self):
		doc, _ = _run(_prev(ARRIVAL - timedelta(seconds=30), "OUT"))
		self.assertEqual(doc.log_type, "OUT")


class TestSchemaTolerance(unittest.TestCase):
	def test_missing_columns_leave_the_press_alone(self):
		doc, lookup = _run(None, columns=False)
		self.assertEqual(doc.log_type, "OUT")
		self.assertIsNone(doc.get("custom_original_log_type"))
		lookup.assert_not_called()

	def test_columns_ready_needs_both_fields(self):
		meta = MagicMock()
		meta.has_field.side_effect = lambda field: field == lt.ORIGINAL_LOG_TYPE_FIELD
		with patch.object(lt, "frappe") as fr:
			fr.get_meta.return_value = meta
			self.assertFalse(lt._columns_ready())
			meta.has_field.side_effect = lambda field: True
			self.assertTrue(lt._columns_ready())

	def test_unmigrated_site_end_to_end(self):
		doc = _Doc(employee="HR-EMP-1", time=ARRIVAL, log_type="OUT", name=None)
		meta = MagicMock()
		meta.has_field.return_value = False
		with patch.object(lt, "frappe") as fr:
			fr.get_meta.return_value = meta
			lt.correct_log_type(doc)
			fr.db.get_all.assert_not_called()
		self.assertEqual(doc.log_type, "OUT")


class TestNeverRaisesOnItsOwnFailures(unittest.TestCase):
	def test_a_failed_lookup_is_swallowed_and_the_press_kept(self):
		doc = _Doc(employee="HR-EMP-1", time=ARRIVAL, log_type="OUT", name=None)
		with (
			patch.object(lt, "_columns_ready", return_value=True),
			patch.object(lt, "_is_live_press", return_value=True),
			patch.object(lt, "_previous_checkin", side_effect=RuntimeError("db gone")),
			patch.object(lt, "frappe") as fr,
		):
			fr.log_error.side_effect = RuntimeError("log_error raised too")
			lt.correct_log_type(doc)  # must not raise
		self.assertEqual(doc.log_type, "OUT")

	def test_a_failed_note_leaves_the_press_exactly_as_it_was(self):
		doc = _Doc(employee="HR-EMP-1", time=ARRIVAL, log_type="OUT", name=None)
		with (
			patch.object(lt, "_columns_ready", return_value=True),
			patch.object(lt, "_is_live_press", return_value=True),
			patch.object(lt, "_previous_checkin", return_value=None),
			patch.object(lt, "note_for", side_effect=RuntimeError("boom")),
			patch.object(lt, "_safe_log"),
		):
			lt.correct_log_type(doc)
		self.assertEqual(doc.log_type, "OUT")
		self.assertIsNone(doc.get("custom_original_log_type"))
		self.assertIsNone(doc.get("custom_log_type_note"))

	def test_a_failed_meta_read_is_swallowed(self):
		doc = _Doc(employee="HR-EMP-1", time=ARRIVAL, log_type="OUT", name=None)
		with patch.object(lt, "frappe") as fr:
			fr.get_meta.side_effect = RuntimeError("db gone")
			lt.correct_log_type(doc)
		self.assertEqual(doc.log_type, "OUT")


class TestLivePressOnly(unittest.TestCase):
	"""Desk backfills, imports and patches are stored exactly as entered."""

	def _live(self, when, now=ARRIVAL, **flags):
		with (
			patch.object(lt, "now_datetime", return_value=now),
			patch.object(lt.frappe, "flags", SimpleNamespace(**flags)),
		):
			return lt._is_live_press(when)

	def test_a_press_made_now_is_live(self):
		self.assertTrue(self._live(ARRIVAL))
		self.assertTrue(self._live(ARRIVAL - timedelta(minutes=30)))
		self.assertTrue(self._live(ARRIVAL + timedelta(minutes=5)))  # phone clock a little ahead

	def test_a_backfill_is_not(self):
		self.assertFalse(self._live(ARRIVAL - timedelta(minutes=31)))
		self.assertFalse(self._live(ARRIVAL - timedelta(days=2)))

	def test_import_patch_and_migrate_are_not(self):
		for flag in ("in_import", "in_patch", "in_migrate"):
			self.assertFalse(self._live(ARRIVAL, **{flag: True}), flag)

	def test_a_backfilled_out_is_neither_converted_nor_refused(self):
		doc = _Doc(employee="HR-EMP-1", time=ARRIVAL, log_type="OUT", name=None)
		with (
			patch.object(lt, "_columns_ready", return_value=True),
			patch.object(lt, "_is_live_press", return_value=False),
			patch.object(
				lt, "_previous_checkin", return_value=_prev(ARRIVAL - timedelta(minutes=1))
			) as lookup,
		):
			lt.correct_log_type(doc)  # an OUT 1 min after an IN: would be refused live
		self.assertEqual(doc.log_type, "OUT")
		self.assertIsNone(doc.get("custom_original_log_type"))
		lookup.assert_not_called()


class TestLookupShape(unittest.TestCase):
	def test_latest_earlier_row_within_seven_days_excluding_itself(self):
		with patch.object(lt, "frappe") as fr:
			fr.db.get_all.return_value = [{"name": "P", "time": ARRIVAL, "log_type": "IN"}]
			row = lt._previous_checkin("HR-EMP-1", ARRIVAL, "EMP-CKIN-1")
		self.assertEqual(row["name"], "P")
		args, kwargs = fr.db.get_all.call_args
		self.assertEqual(args[0], "Employee Checkin")
		self.assertEqual(kwargs["order_by"], "time desc")
		self.assertEqual(kwargs["limit"], 1)
		filters = kwargs["filters"]
		self.assertIn(["employee", "=", "HR-EMP-1"], filters)
		self.assertIn(["time", "<", ARRIVAL], filters)
		self.assertIn(["time", ">=", ARRIVAL - timedelta(days=7)], filters)
		self.assertIn(["name", "!=", "EMP-CKIN-1"], filters)
		self.assertIn("log_type", kwargs["fields"])

	def test_an_unnamed_doc_adds_no_name_filter_and_none_when_empty(self):
		with patch.object(lt, "frappe") as fr:
			fr.db.get_all.return_value = []
			self.assertIsNone(lt._previous_checkin("HR-EMP-1", ARRIVAL, None))
		filters = fr.db.get_all.call_args[1]["filters"]
		self.assertFalse(any(f[0] == "name" for f in filters))


# ---------------------------------------------------------------------------
# Task 2: the attendance board
# ---------------------------------------------------------------------------


def _group(*rows, day="2026-10-19"):
	return attendance_service.group_checkins(
		[{"name": f"C{i}", "employee": "HR-EMP-1", **row} for i, row in enumerate(rows)]
	)[("HR-EMP-1", day)]


_TODAY = datetime(2026, 10, 20).date()


def _cell(group, date_str="2026-10-19", today=_TODAY):
	return attendance_service.build_cell(
		date_str=date_str,
		shift_type="S",
		shift_location="L",
		scheduled_start="12:30",
		scheduled_end="01:00",
		group=group,
		day_off_row=None,
		is_holiday=False,
		is_cover=False,
		exempt=False,
		grace=10,
		today=today,
	)


def _at(hour, minute=0, second=0):
	return datetime(2026, 10, 19, hour, minute, second)


class TestCheckoutCorrected(unittest.TestCase):
	def test_the_new_field_marks_the_day(self):
		group = _group(
			{
				"time": _at(12, 37),
				"log_type": "IN",
				"custom_original_log_type": "OUT",
				"custom_log_type_note": "Pressed Check Out at 12:37 ...",
			},
			{"time": _at(21, 16), "log_type": "OUT"},
		)
		cell = _cell(group)
		self.assertTrue(cell["checkout_corrected"])
		self.assertEqual(cell["log_type_note"], "Pressed Check Out at 12:37 ...")

	def test_historical_first_row_out_marks_the_day(self):
		group = _group(
			{"time": _at(12, 37, 5), "log_type": "OUT"},
			{"time": _at(12, 37, 40), "log_type": "IN"},
			{"time": _at(21, 16), "log_type": "OUT"},
		)
		cell = _cell(group)
		self.assertTrue(cell["checkout_corrected"])
		self.assertIsNone(cell["log_type_note"])

	def test_first_means_chronological_not_read_order(self):
		group = _group(
			{"time": _at(21, 16), "log_type": "IN"},
			{"time": _at(12, 37), "log_type": "OUT"},
		)
		self.assertTrue(_cell(group)["checkout_corrected"])

	def test_an_ordinary_day_is_not_marked(self):
		group = _group({"time": _at(12, 30), "log_type": "IN"}, {"time": _at(21, 0), "log_type": "OUT"})
		self.assertFalse(_cell(group)["checkout_corrected"])

	def test_no_checkins_means_no_marks(self):
		cell = _cell(None)
		self.assertFalse(cell["checkout_corrected"])
		self.assertFalse(cell["missed_checkout"])
		self.assertIsNone(cell["log_type_note"])

	def test_the_earliest_note_wins(self):
		group = _group(
			{"time": _at(20), "log_type": "IN", "custom_log_type_note": "later"},
			{"time": _at(13), "log_type": "IN", "custom_log_type_note": "earlier"},
		)
		self.assertEqual(_cell(group)["log_type_note"], "earlier")


class TestMissedCheckout(unittest.TestCase):
	def test_a_past_day_with_one_in_missed_its_checkout(self):
		self.assertTrue(_cell(_group({"time": _at(16, 3), "log_type": "IN"}))["missed_checkout"])

	def test_a_past_day_ending_on_out_did_not(self):
		group = _group({"time": _at(12, 30), "log_type": "IN"}, {"time": _at(21), "log_type": "OUT"})
		self.assertFalse(_cell(group)["missed_checkout"])

	def test_a_past_day_ending_on_in_did(self):
		group = _group(
			{"time": _at(12, 30), "log_type": "IN"},
			{"time": _at(15), "log_type": "OUT"},
			{"time": _at(16), "log_type": "IN"},
		)
		self.assertTrue(_cell(group)["missed_checkout"])

	def test_today_is_never_missed_yet(self):
		cell = _cell(_group({"time": _at(16, 3), "log_type": "IN"}), today=datetime(2026, 10, 19).date())
		self.assertFalse(cell["missed_checkout"])

	def test_a_lone_out_is_not_a_missed_checkout(self):
		self.assertFalse(_cell(_group({"time": _at(11, 30), "log_type": "OUT"}))["missed_checkout"])

	def test_last_nights_shift_is_not_missed_before_six(self):
		group = _group({"time": _at(16, 3), "log_type": "IN"})
		for now, expected in (
			(datetime(2026, 10, 20, 0, 30), False),  # 16:00-01:00 shift still running
			(datetime(2026, 10, 20, 5, 59), False),
			(datetime(2026, 10, 20, 6, 0), True),
			(datetime(2026, 10, 20, 14, 0), True),
		):
			self.assertEqual(
				attendance_service.missed_checkout_for(group, "2026-10-19", _TODAY, now=now), expected, now
			)

	def test_two_days_back_is_missed_even_in_the_small_hours(self):
		group = _group({"time": datetime(2026, 10, 18, 16), "log_type": "IN"}, day="2026-10-18")
		self.assertTrue(
			attendance_service.missed_checkout_for(
				group, "2026-10-18", _TODAY, now=datetime(2026, 10, 20, 0, 30)
			)
		)

	def test_blank_log_types_fall_back_to_an_odd_count(self):
		self.assertTrue(_cell(_group({"time": _at(13), "log_type": ""}))["missed_checkout"])
		group = _group({"time": _at(13), "log_type": ""}, {"time": _at(21), "log_type": ""})
		self.assertFalse(_cell(group)["missed_checkout"])


class TestOvernightTailGrouping(unittest.TestCase):
	"""An unmatched small-hours OUT closes the previous day's shift (Hassan, 15 Oct 00:42)."""

	def _groups(self, *rows):
		return attendance_service.group_checkins(
			[{"name": f"C{i}", "employee": "HR-EMP-1", **row} for i, row in enumerate(rows)]
		)

	def test_unmatched_out_after_midnight_joins_yesterday(self):
		groups = self._groups(
			{"time": datetime(2026, 10, 14, 18, 30), "log_type": "IN"},
			{"time": datetime(2026, 10, 15, 0, 42), "log_type": "OUT"},
		)
		self.assertEqual(list(groups), [("HR-EMP-1", "2026-10-14")])
		cell = _cell(groups[("HR-EMP-1", "2026-10-14")], date_str="2026-10-14")
		self.assertFalse(cell["missed_checkout"])
		self.assertFalse(cell["checkout_corrected"])

	def test_a_matched_out_keeps_its_shift_day(self):
		groups = self._groups(
			{
				"time": datetime(2026, 10, 15, 0, 42),
				"log_type": "OUT",
				"shift_start": datetime(2026, 10, 14, 12, 30),
			},
		)
		self.assertEqual(list(groups), [("HR-EMP-1", "2026-10-14")])

	def test_a_morning_in_stays_on_its_own_day(self):
		groups = self._groups({"time": datetime(2026, 10, 15, 5, 30), "log_type": "IN"})
		self.assertEqual(list(groups), [("HR-EMP-1", "2026-10-15")])

	def test_a_converted_out_stays_on_its_own_day(self):
		groups = self._groups(
			{"time": datetime(2026, 10, 15, 2, 0), "log_type": "OUT", "custom_original_log_type": "OUT"},
		)
		self.assertEqual(list(groups), [("HR-EMP-1", "2026-10-15")])

	def test_a_daytime_unmatched_out_stays_on_its_own_day(self):
		groups = self._groups({"time": datetime(2026, 10, 15, 21, 0), "log_type": "OUT"})
		self.assertEqual(list(groups), [("HR-EMP-1", "2026-10-15")])


class TestBoardPlumbing(unittest.TestCase):
	def test_the_day_board_copies_the_new_keys(self):
		for key in ("checkout_corrected", "missed_checkout", "log_type_note", "device_flags"):
			self.assertIn(key, attendance_service._DAY_ROW_KEYS)

	def test_the_new_columns_are_read_only_when_present(self):
		def fetch(present):
			meta = MagicMock()
			meta.has_field.side_effect = lambda field: field in present
			with patch.object(attendance_service, "frappe") as fr:
				fr.get_meta.return_value = meta
				fr.get_all.return_value = []
				attendance_service._fetch_checkins(
					["HR-EMP-1"], datetime(2026, 10, 19).date(), datetime(2026, 10, 19).date()
				)
				return fr.get_all.call_args[1]["fields"]

		fields = fetch(set(lt.CHECKIN_LOG_TYPE_FIELDS))
		self.assertIn("custom_original_log_type", fields)
		self.assertIn("custom_log_type_note", fields)
		fields = fetch(set())
		self.assertNotIn("custom_original_log_type", fields)
		self.assertNotIn("custom_log_type_note", fields)

	def test_employee_detail_rows_carry_the_original_and_the_note(self):
		row = {
			"name": "C1",
			"employee": "HR-EMP-1",
			"time": _at(12, 37),
			"log_type": "IN",
			"custom_original_log_type": "OUT",
			"custom_log_type_note": "note",
		}
		plain = {"name": "C2", "employee": "HR-EMP-1", "time": _at(21, 16), "log_type": "OUT"}
		grid = {
			"employees": [
				{
					"employee": "HR-EMP-1",
					"employee_name": "Ali",
					"designation": None,
					"department": None,
					"shift_locations": [],
					"is_courier": False,
					"exempt": False,
				}
			],
			"cells": {"HR-EMP-1": {"2026-10-19": {"date": "2026-10-19", "status": "present"}}},
			"groups": {("HR-EMP-1", "2026-10-19"): {"rows": [row, plain]}},
			"scope": {"configured": True, "unrestricted": True, "locations": None},
			"grace_minutes": 15,
		}
		with (
			patch.object(attendance_service, "hrms_available", return_value=True),
			patch.object(attendance_service, "build_grid", return_value=grid),
			patch.object(attendance_service, "shift_location_map", return_value={}),
		):
			data = attendance_service.get_employee("HR-EMP-1", from_date="2026-10-19", to_date="2026-10-19")
		by_name = {r["name"]: r for r in data["checkins"]}
		self.assertEqual(by_name["C1"]["original_log_type"], "OUT")
		self.assertEqual(by_name["C1"]["log_type_note"], "note")
		self.assertEqual(by_name["C1"]["log_type"], "IN")
		self.assertIsNone(by_name["C2"]["original_log_type"])
		self.assertIsNone(by_name["C2"]["log_type_note"])


# ---------------------------------------------------------------------------
# Task 3: approving a New Device phone
# ---------------------------------------------------------------------------


class _Thrown(Exception):
	pass


def _fake_throw(msg, exc=None, title=None, **kwargs):
	raise (exc if isinstance(exc, type) and issubclass(exc, Exception) else _Thrown)(str(msg))


def _ck(name, time, flag, token, shift_start=None):
	return {
		"name": name,
		"time": time,
		"shift_start": shift_start,
		"custom_device_flag": flag,
		"custom_device_token": token,
	}


def _approve(day_rows, flagged_rows=(), registered_elsewhere=None, used_by_colleague=None):
	"""Run ``checkin_device.approve_device`` with the DB mocked; return (result, fr)."""
	meta = MagicMock()
	meta.has_field.return_value = True

	def get_all(doctype, filters=None, **kwargs):
		# The day read filters with a list; the "every row with this token" read with a dict.
		if isinstance(filters, list):
			return [dict(r) for r in day_rows]
		return [dict(r) for r in flagged_rows]

	def get_value(doctype, name, field=None, *args, **kwargs):
		if doctype == "Employee" and isinstance(name, dict):
			return registered_elsewhere
		if doctype == "Employee" and field == "employee_name":
			return "Hussien"
		return None

	with (
		patch.object(dev, "frappe") as fr,
		patch.object(dev, "_approver_name", return_value="Owner Manager"),
		patch.object(dev, "now_datetime", return_value=datetime(2026, 10, 20, 11, 5)),
	):
		fr.get_meta.return_value = meta
		fr.throw.side_effect = _fake_throw
		fr.db.get_all.side_effect = get_all
		fr.db.get_value.side_effect = get_value
		fr.db.exists.return_value = used_by_colleague
		result = dev.approve_device("HR-EMP-1", "2026-10-19")
		return result, fr


class TestApproveDevice(unittest.TestCase):
	def test_the_latest_new_device_of_the_shift_day_is_registered_and_cleared(self):
		day_rows = [
			# 00:30 on the 20th belongs to the 19th's shift: it is the latest.
			_ck("C4", datetime(2026, 10, 20, 0, 30), dev.FLAG_NEW, TOKEN, datetime(2026, 10, 19, 12, 30)),
			_ck("C2", _at(12, 37), dev.FLAG_NEW, "OlderToken12345", datetime(2026, 10, 19, 12, 30)),
			# The 18th's shift: not this day.
			_ck(
				"C1", datetime(2026, 10, 18, 16, 3), dev.FLAG_NEW, "Other1234567", datetime(2026, 10, 18, 16)
			),
		]
		flagged = [
			{"name": "C4", "custom_device_flag_detail": "Different phone."},
			{"name": "C9", "custom_device_flag_detail": None},
		]
		result, fr = _approve(day_rows, flagged)

		employee_call = fr.db.set_value.call_args_list[0]
		self.assertEqual(employee_call[0][0], "Employee")
		self.assertEqual(employee_call[0][1], "HR-EMP-1")
		self.assertEqual(employee_call[0][2][dev.EMPLOYEE_DEVICE_FIELD], TOKEN)
		self.assertEqual(employee_call[0][2][dev.EMPLOYEE_REGISTERED_ON_FIELD], datetime(2026, 10, 20, 11, 5))
		self.assertFalse(employee_call[1]["update_modified"])

		checkin_calls = fr.db.set_value.call_args_list[1:]
		self.assertEqual([c[0][1] for c in checkin_calls], ["C4", "C9"])
		for call in checkin_calls:
			self.assertEqual(call[0][0], "Employee Checkin")
			self.assertEqual(call[0][2]["custom_device_flag"], "")
			self.assertTrue(
				call[0][2]["custom_device_flag_detail"].endswith(
					"Approved as this employee's phone by Owner Manager on 20 Oct 2026."
				)
			)
		self.assertTrue(checkin_calls[0][0][2]["custom_device_flag_detail"].startswith("Different phone."))

		# The "clear" read is scoped to this employee, this token, New Device only.
		clear_filters = fr.db.get_all.call_args_list[1][1]["filters"]
		self.assertEqual(
			clear_filters,
			{"employee": "HR-EMP-1", "custom_device_token": TOKEN, "custom_device_flag": dev.FLAG_NEW},
		)

		self.assertEqual(
			result,
			{
				"employee": "HR-EMP-1",
				"employee_name": "Hussien",
				"device": "…" + TOKEN[-6:],
				"approved_by": "Owner Manager",
				"approved_on": "2026-10-20 11:05:00",
				"cleared": 2,
			},
		)
		self.assertNotIn(TOKEN, str(result))

	def test_a_shared_device_is_refused(self):
		day_rows = [_ck("C2", _at(12, 37), dev.FLAG_SHARED, TOKEN, _at(12, 30))]
		with self.assertRaises(dev.DeviceApprovalError) as ctx:
			_approve(day_rows)
		self.assertIn("another employee", str(ctx.exception))

	def test_a_new_device_token_that_was_also_shared_that_day_is_refused(self):
		day_rows = [
			_ck("C3", _at(21), dev.FLAG_NEW, TOKEN, _at(12, 30)),
			_ck("C2", _at(12, 37), dev.FLAG_SHARED, TOKEN, _at(12, 30)),
		]
		with self.assertRaises(dev.DeviceApprovalError):
			_approve(day_rows)

	def test_a_phone_registered_to_somebody_else_is_refused(self):
		day_rows = [_ck("C3", _at(21), dev.FLAG_NEW, TOKEN, _at(12, 30))]
		with self.assertRaises(dev.DeviceApprovalError) as ctx:
			_approve(day_rows, registered_elsewhere="HR-EMP-7")
		self.assertIn("already registered", str(ctx.exception))

	def test_a_phone_a_colleague_used_after_this_employee_is_refused(self):
		# A checked in first (New Device); B then used it (B's row is the Shared one).
		day_rows = [_ck("C3", _at(12, 37), dev.FLAG_NEW, TOKEN, _at(12, 30))]
		with self.assertRaises(dev.DeviceApprovalError) as ctx:
			_approve(day_rows, used_by_colleague="EMP-CKIN-9")
		self.assertIn("another employee", str(ctx.exception))

	def test_the_colleague_check_is_by_token_and_other_employees(self):
		day_rows = [_ck("C3", _at(21), dev.FLAG_NEW, TOKEN, _at(12, 30))]
		_, fr = _approve(day_rows)
		fr.db.exists.assert_called_once_with(
			"Employee Checkin", {"custom_device_token": TOKEN, "employee": ("!=", "HR-EMP-1")}
		)

	def test_an_unmatched_small_hours_out_belongs_to_the_night_before(self):
		# 00:40 on the 20th, HRMS matched no shift: the board files it under the 19th.
		tail = {**_ck("C5", datetime(2026, 10, 20, 0, 40), dev.FLAG_NEW, TOKEN), "log_type": "OUT"}
		result, _ = _approve([tail], [{"name": "C5", "custom_device_flag_detail": None}])
		self.assertEqual(result["cleared"], 1)

	def test_a_converted_small_hours_press_stays_on_its_own_day(self):
		row = {
			**_ck("C5", datetime(2026, 10, 20, 0, 40), dev.FLAG_NEW, TOKEN),
			"log_type": "IN",
			"custom_original_log_type": "OUT",
		}
		with self.assertRaises(dev.DeviceApprovalError):
			_approve([row])

	def test_nothing_to_approve_is_a_friendly_error(self):
		with self.assertRaises(dev.DeviceApprovalError) as ctx:
			_approve([])
		self.assertIn("no new phone to approve", str(ctx.exception))

	def test_a_new_device_on_another_shift_day_is_not_approvable_here(self):
		day_rows = [_ck("C1", datetime(2026, 10, 18, 16, 3), dev.FLAG_NEW, TOKEN, datetime(2026, 10, 18, 16))]
		with self.assertRaises(dev.DeviceApprovalError):
			_approve(day_rows)

	def test_nothing_is_written_when_refused(self):
		with patch.object(dev, "frappe") as fr:
			meta = MagicMock()
			meta.has_field.return_value = True
			fr.get_meta.return_value = meta
			fr.throw.side_effect = _fake_throw
			fr.db.get_all.return_value = [_ck("C2", _at(12, 37), dev.FLAG_SHARED, TOKEN, _at(12, 30))]
			with self.assertRaises(dev.DeviceApprovalError):
				dev.approve_device("HR-EMP-1", "2026-10-19")
			fr.db.set_value.assert_not_called()


class TestApprovePermission(unittest.TestCase):
	def _can(self, roles, user="someone@x.com"):
		with (
			patch.object(branch_access_service, "_session_user", return_value=user),
			patch.object(branch_access_service, "_roles", return_value=set(roles)),
		):
			return attendance_service.can_approve_device()

	def test_managers_may_approve(self):
		self.assertTrue(self._can({"JARZ Manager"}))
		self.assertTrue(self._can({"System Manager"}))
		self.assertTrue(self._can(set(), user="Administrator"))

	def test_line_managers_may_not(self):
		self.assertFalse(self._can({"JARZ line manager"}))
		self.assertFalse(self._can({"jarz line manager"}))
		self.assertFalse(self._can({"POS User"}))

	def test_a_failing_role_read_is_no(self):
		with patch.object(branch_access_service, "can_manage_all", side_effect=RuntimeError("boom")):
			self.assertFalse(attendance_service.can_approve_device())

	def test_the_endpoint_refuses_a_line_manager_before_touching_anything(self):
		from jarz_pos.api import attendance as attendance_api

		with (
			patch.object(attendance_api, "frappe") as api_frappe,
			patch.object(attendance_service, "ensure_attendance_access"),
			patch.object(attendance_service, "can_approve_device", return_value=False),
			patch.object(attendance_service, "ensure_employee_in_scope") as scope,
			patch.object(dev, "approve_device") as approve,
		):
			api_frappe.throw.side_effect = _fake_throw
			api_frappe.PermissionError = frappe.PermissionError
			with self.assertRaises(frappe.PermissionError):
				attendance_api.approve_device("HR-EMP-1", "2026-10-19")
			scope.assert_not_called()
			approve.assert_not_called()

	def test_the_endpoint_checks_scope_then_approves(self):
		from jarz_pos.api import attendance as attendance_api

		with (
			patch.object(attendance_api, "frappe") as api_frappe,
			patch.object(attendance_service, "ensure_attendance_access"),
			patch.object(attendance_service, "can_approve_device", return_value=True),
			patch.object(attendance_service, "ensure_employee_in_scope") as scope,
			patch.object(dev, "approve_device", return_value={"cleared": 1}) as approve,
		):
			api_frappe.throw.side_effect = _fake_throw
			api_frappe.db.exists.return_value = True
			data = attendance_api.approve_device(" HR-EMP-1 ", "2026-10-19")
		scope.assert_called_once_with("HR-EMP-1")
		approve.assert_called_once_with("HR-EMP-1", "2026-10-19")
		self.assertTrue(data["success"])

	def test_the_bootstrap_carries_the_flag(self):
		with (
			patch.object(attendance_service, "allowed_shift_locations", return_value=None),
			patch.object(attendance_service, "hrms_available", return_value=False),
			patch.object(attendance_service, "scope_payload", return_value={}),
			patch.object(attendance_service, "grace_minutes", return_value=15),
			patch.object(attendance_service, "checkin_enforced", return_value=True),
			patch.object(attendance_service, "can_approve_device", return_value=True),
		):
			payload = attendance_service.bootstrap()
		self.assertIs(payload["can_approve_device"], True)

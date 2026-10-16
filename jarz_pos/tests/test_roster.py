"""Shift distribution: hours arithmetic, overtime credit, and the check-in gate.

Three things here are worth more than the rest:

* **Shift length across midnight.** Every branch shift in this business ends
  after midnight (12:30 -> 01:00). A naive ``end - start`` reports that
  twelve-and-a-half hour shift as *minus* eleven and a half, which would then
  flow straight into the payroll hours column as a negative number.
* **Overtime credit.** A courier's overtime hour is paid as two, a dispatcher's
  as one. Getting the multiplier the wrong way round is a silent payroll error
  that nobody notices until somebody is underpaid.
* **The check-in gate.** It is the one piece here that can stop a real person
  working. Each allow-path is pinned individually, because the cost of a false
  refusal at 07:00 is somebody standing outside a branch they are rostered at.
"""

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from frappe.utils import getdate

from jarz_pos.api import roster as roster_api
from jarz_pos.constants import ROLES
from jarz_pos.services import roster as roster_service
from jarz_pos.events import employee_checkin as checkin_guard


class TestShiftLengthHours(unittest.TestCase):
	"""Hours must be right on both sides of midnight."""

	def test_plain_daytime_shift(self):
		self.assertEqual(roster_service.shift_length_hours(timedelta(hours=10), timedelta(hours=19)), 9.0)

	def test_branch_opening_shift(self):
		# 12:30 -> 21:30
		self.assertEqual(
			roster_service.shift_length_hours(
				timedelta(hours=12, minutes=30), timedelta(hours=21, minutes=30)
			),
			9.0,
		)

	def test_closing_shift_crosses_midnight(self):
		# 16:00 -> 01:00 is nine hours, not minus fifteen.
		self.assertEqual(roster_service.shift_length_hours(timedelta(hours=16), timedelta(hours=1)), 9.0)

	def test_full_day_cover_shift(self):
		# 12:30 -> 01:00, the shift one person stretches onto when their
		# colleague is off. Twelve and a half hours, not minus eleven and a half.
		self.assertEqual(
			roster_service.shift_length_hours(timedelta(hours=12, minutes=30), timedelta(hours=1)),
			12.5,
		)

	def test_courier_twelve_hour_shift(self):
		self.assertEqual(roster_service.shift_length_hours(timedelta(hours=13), timedelta(hours=1)), 12.0)

	def test_nasr_city_courier_ten_hour_shift(self):
		self.assertEqual(
			roster_service.shift_length_hours(
				timedelta(hours=12, minutes=30), timedelta(hours=22, minutes=30)
			),
			10.0,
		)

	def test_accepts_string_times(self):
		"""Frappe hands back a Time field as timedelta, but fixtures use strings."""
		self.assertEqual(roster_service.shift_length_hours("16:00:00", "01:00:00"), 9.0)

	def test_missing_times_are_zero_not_an_exception(self):
		self.assertEqual(roster_service.shift_length_hours(None, timedelta(hours=1)), 0.0)
		self.assertEqual(roster_service.shift_length_hours(timedelta(hours=1), None), 0.0)

	def test_identical_start_and_end_is_a_full_day(self):
		"""A shift that ends when it starts wraps; it is not a zero-hour shift."""
		self.assertEqual(roster_service.shift_length_hours(timedelta(hours=9), timedelta(hours=9)), 24.0)


class TestOvertimeCredit(unittest.TestCase):
	"""Courier overtime counts double; everyone else's counts once."""

	def _multiplier(
		self,
		designation,
		scheduled=(),
		employee="HR-EMP-00001",
		courier_mult=2.0,
		default_mult=1.0,
	):
		with (
			patch.object(roster_service, "frappe") as mock_frappe,
			patch.object(roster_service, "single_float") as mock_float,
			patch.object(roster_service, "_scheduled_shift_types", return_value=list(scheduled)),
		):
			mock_frappe.db.get_single_value.return_value = "courier,driver,delivery"
			mock_float.side_effect = lambda _dt, field, default: (
				courier_mult if "courier" in field else default_mult
			)
			return roster_service.overtime_multiplier(employee, designation)

	def test_courier_hour_is_credited_twice(self):
		self.assertEqual(self._multiplier("Courier"), 2.0)

	def test_dispatcher_hour_is_credited_once(self):
		self.assertEqual(self._multiplier("Dispatcher"), 1.0)

	def test_designation_match_is_a_substring(self):
		"""Real Designation records drift; the match must survive the drift."""
		self.assertEqual(self._multiplier("Courier - Nasr City"), 2.0)
		self.assertEqual(self._multiplier("Delivery Rider"), 2.0)

	def test_match_is_case_insensitive(self):
		self.assertEqual(self._multiplier("COURIER"), 2.0)

	def test_shift_type_identifies_a_courier_when_designation_is_blank(self):
		"""The live case: every Employee record here has designation unset.

		Classifying on designation alone paid all four couriers' overtime at
		the dispatcher rate — half what they are owed — and nothing about the
		result looked wrong.
		"""
		self.assertEqual(self._multiplier(None, scheduled=["Courier Nasr City"]), 2.0)
		self.assertEqual(
			self._multiplier(None, scheduled=["Courier 6Oct-Dokki", "Courier 6Oct-Dokki - Friday"]),
			2.0,
		)

	def test_branch_shift_types_stay_on_the_dispatcher_rate(self):
		for scheduled in (
			["Branch Opening"],
			["Branch Closing", "Branch Closing - Friday"],
			["Branch Cover Full Day"],
			["Factory"],
		):
			self.assertEqual(self._multiplier(None, scheduled=scheduled), 1.0, scheduled)

	def test_no_designation_and_no_schedule_is_not_a_courier(self):
		self.assertEqual(self._multiplier(None), 1.0)
		self.assertEqual(self._multiplier(""), 1.0)

	def test_courier_classification_does_not_leak_to_similar_words(self):
		with patch.object(roster_service, "frappe") as mock_frappe:
			mock_frappe.db.get_single_value.return_value = "courier,driver,delivery"
			self.assertFalse(roster_service.is_courier_designation("Cashier"))
			self.assertFalse(roster_service.is_courier_designation("Branch Manager"))
			self.assertTrue(roster_service.is_courier_designation("Driver"))


class TestTimeTextKeepsMidnight(unittest.TestCase):
	"""A Time field of midnight is ``timedelta(0)``, which is FALSY.

	``str(value or "")`` therefore erases it. The Friday courier shift really
	does end at 00:00, and it came back from staging with a blank end time
	while its computed length stayed correct — so the picker would have shown
	"14:30 → " with nothing after the arrow.
	"""

	def test_midnight_survives(self):
		self.assertEqual(roster_service._time_text(timedelta(0)), "0:00:00")

	def test_a_real_time_is_unchanged(self):
		self.assertEqual(roster_service._time_text(timedelta(hours=16)), "16:00:00")

	def test_only_none_means_absent(self):
		self.assertEqual(roster_service._time_text(None), "")

	def test_a_shift_ending_at_midnight_still_measures_correctly(self):
		# 14:00 -> 00:00 is ten hours.
		self.assertEqual(roster_service.shift_length_hours(timedelta(hours=14), timedelta(0)), 10.0)


class TestRosterAccessGate(unittest.TestCase):
	"""The roster is line-manager work, and the whole tier must clear it."""

	def _has_access(self, roles):
		with patch.object(roster_service, "frappe") as mock_frappe:
			mock_frappe.get_roles.return_value = roles
			return roster_service.has_roster_access()

	def test_whole_line_manager_tier_is_admitted(self):
		for role in ROLES.LINE_MANAGER_TIER:
			self.assertTrue(self._has_access([role]), role)

	def test_jarz_manager_is_admitted(self):
		self.assertTrue(self._has_access(["JARZ Manager"]))

	def test_rank_and_file_is_refused(self):
		for role in ("POS User", "Sales User", "Accounts User", "Employee"):
			self.assertFalse(self._has_access([role]), role)

	def test_no_roles_is_refused(self):
		self.assertFalse(self._has_access([]))


class TestHistoryStaysReadable(unittest.TestCase):
	"""A past month must still report the hours somebody worked.

	HRMS runs ``mark_expired_shift_assignments_as_inactive`` daily, so every
	assignment whose end date has passed flips to ``Inactive``. Reading with the
	Active-only filter the WRITES use made all history disappear: last month's
	calendar came back empty and its payroll hours read zero — for the one month
	anybody actually needs to pay.
	"""

	def test_read_filter_admits_expired_assignments(self):
		self.assertIn("Inactive", roster_service.READABLE_ASSIGNMENT_STATUSES)
		self.assertIn("Active", roster_service.READABLE_ASSIGNMENT_STATUSES)

	def test_write_filter_stays_active_only(self):
		# Breaking or extending an already-expired assignment is never right.
		self.assertEqual(
			roster_service.ACTIVE_ASSIGNMENT_FILTERS,
			{"docstatus": 1, "status": "Active"},
		)

	def test_cancelled_assignments_are_excluded_from_both(self):
		"""Inactive means "already happened"; cancelled is docstatus 2."""
		self.assertEqual(roster_service.ACTIVE_ASSIGNMENT_FILTERS["docstatus"], 1)
		self.assertNotIn("Cancelled", roster_service.READABLE_ASSIGNMENT_STATUSES)


class TestUnbranchedLocations(unittest.TestCase):
	"""The Factory has no POS Profile and never will.

	It is a production site, not a sales branch, so branch scoping could not
	express it and its three staff were invisible to every manager except
	Administrator — nobody could roster the factory at all, and the screen gave
	no hint that a quarter of the workforce was missing.
	"""

	def _unbranched(self, roles, locations, claimed):
		with patch.object(roster_service, "frappe") as mock_frappe:
			mock_frappe.get_roles.return_value = roles

			def fake_get_all(doctype, **kwargs):
				if doctype == "POS Profile":
					return list(claimed)
				return list(locations)

			mock_frappe.get_all.side_effect = fake_get_all
			return roster_service._unbranched_locations()

	LOCATIONS = ["Factory", "Nasr City", "Dokki", "6th of October"]
	CLAIMED = ["Nasr City", "Dokki", "6th of October"]

	def test_manager_tier_reaches_the_factory(self):
		self.assertEqual(self._unbranched(["JARZ Manager"], self.LOCATIONS, self.CLAIMED), {"Factory"})

	def test_system_manager_reaches_it_too(self):
		self.assertEqual(self._unbranched(["System Manager"], self.LOCATIONS, self.CLAIMED), {"Factory"})

	def test_a_bare_branch_line_manager_does_not(self):
		"""A branch supervisor runs a branch, not the factory."""
		self.assertEqual(self._unbranched(["JARZ line manager"], self.LOCATIONS, self.CLAIMED), set())

	def test_rank_and_file_never_does(self):
		self.assertEqual(self._unbranched(["POS User"], self.LOCATIONS, self.CLAIMED), set())

	def test_mapping_the_factory_later_removes_it_from_this_set(self):
		"""Self-healing: give the Factory a POS Profile and normal scoping takes over."""
		self.assertEqual(self._unbranched(["JARZ Manager"], self.LOCATIONS, self.LOCATIONS), set())

	def test_a_failed_lookup_widens_nothing(self):
		with patch.object(roster_service, "frappe") as mock_frappe:
			mock_frappe.get_roles.return_value = ["JARZ Manager"]
			mock_frappe.get_all.side_effect = Exception("db down")
			self.assertEqual(roster_service._unbranched_locations(), set())


class TestMonthBounds(unittest.TestCase):
	def test_february_in_a_leap_year(self):
		start, end = roster_service.month_bounds("2028-02")
		self.assertEqual((start, end), (date(2028, 2, 1), date(2028, 2, 29)))

	def test_thirty_day_month(self):
		start, end = roster_service.month_bounds("2026-09")
		self.assertEqual((start, end), (date(2026, 9, 1), date(2026, 9, 30)))

	def test_thirty_one_day_month(self):
		start, end = roster_service.month_bounds("2026-12")
		self.assertEqual((start, end), (date(2026, 12, 1), date(2026, 12, 31)))


class TestCheckinGate(unittest.TestCase):
	"""The gate that decides whether somebody may clock in.

	Every test states which of the two outcomes it expects and why, because a
	false refusal here has a person standing outside a locked branch.
	"""

	def _run(
		self,
		shift=None,
		enforcement=True,
		exempt=False,
		roster_managed=True,
		assignment_today=None,
		assignment_yesterday=None,
		day_off=None,
		at="2026-09-10 09:00:00",
		within_tail=False,
		has_day_off=False,
		within_horizon=True,
	):
		doc = MagicMock()
		doc.employee = "HR-EMP-00001"
		doc.time = at
		doc.shift = shift

		thrown = {}

		def fake_throw(msg, title=None, exc=None):
			thrown["msg"] = str(msg)
			raise AssertionError("BLOCKED")

		with (
			patch.object(checkin_guard, "_enforcement_enabled", return_value=enforcement),
			patch.object(checkin_guard, "_is_exempt", return_value=exempt),
			patch.object(checkin_guard, "_is_roster_managed", return_value=roster_managed),
			patch.object(
				checkin_guard,
				"_assignment_on",
				side_effect=lambda _e, d: assignment_today
				if str(d) == str(getdate(at))
				else assignment_yesterday,
			),
			patch.object(checkin_guard, "_is_within_overnight_tail", return_value=within_tail),
			patch.object(checkin_guard, "_has_day_off", return_value=has_day_off),
			patch.object(checkin_guard, "_is_within_rostered_horizon", return_value=within_horizon),
			patch.object(checkin_guard, "_enforce_location") as mock_location,
			patch.object(checkin_guard, "_explanation", return_value=day_off or "not rostered"),
			patch.object(checkin_guard, "frappe") as mock_frappe,
		):
			mock_frappe.throw.side_effect = fake_throw
			mock_frappe.ValidationError = Exception
			try:
				checkin_guard.enforce_roster_on_checkin(doc)
				return {"blocked": False, "location_checked": mock_location.called}
			except AssertionError:
				return {"blocked": True, "message": thrown.get("msg")}

	def test_on_shift_is_allowed(self):
		"""HRMS matched a shift, so it already applied the geofence itself."""
		result = self._run(shift="Branch Opening")
		self.assertFalse(result["blocked"])

	def test_not_rostered_is_blocked(self):
		result = self._run(shift=None)
		self.assertTrue(result["blocked"])

	def test_rostered_today_but_off_window_is_allowed_with_a_location_check(self):
		"""Arriving early is HRMS's business, not the roster's.

		But HRMS skipped the geofence because it had no shift to hang it on, so
		the gate applies it against the branch the roster does know about.
		"""
		result = self._run(shift=None, assignment_today={"shift_location": "Nasr City"})
		self.assertFalse(result["blocked"])
		self.assertTrue(result["location_checked"])

	def test_yesterdays_shift_still_lets_you_clock_out_inside_its_tail(self):
		"""Branch shifts end after midnight; a late check-out must not be refused."""
		result = self._run(
			shift=None,
			at="2026-09-10 00:40:00",
			assignment_yesterday={"shift_type": "Branch Closing", "shift_location": "Dokki"},
			within_tail=True,
		)
		self.assertFalse(result["blocked"])

	def test_yesterdays_shift_does_NOT_excuse_a_midday_checkin(self):
		"""The bug the staging end-to-end run caught, pinned.

		Marking one day off carves it out of a longer assignment range and
		leaves the day BEFORE it still rostered. Accepting "any assignment
		dated yesterday" therefore waved through a 13:00 check-in on somebody's
		day off — which silently disabled the entire gate, since a day off
		almost always sits between two worked days.
		"""
		result = self._run(
			shift=None,
			at="2026-09-10 13:00:00",
			assignment_yesterday={"shift_type": "Branch Closing", "shift_location": "Dokki"},
			within_tail=False,
		)
		self.assertTrue(result["blocked"])

	def test_enforcement_switched_off_lets_everyone_through(self):
		result = self._run(shift=None, enforcement=False)
		self.assertFalse(result["blocked"])

	def test_exempt_employee_is_let_through(self):
		"""The escape hatch for a rota mistake at 07:00."""
		result = self._run(shift=None, exempt=True)
		self.assertFalse(result["blocked"])

	def test_employee_outside_the_roster_system_is_untouched(self):
		"""Head office has logins and no rota; they must keep working."""
		result = self._run(shift=None, roster_managed=False)
		self.assertFalse(result["blocked"])

	def test_an_explicit_day_off_blocks(self):
		result = self._run(shift=None, has_day_off=True)
		self.assertTrue(result["blocked"])

	def test_a_lapsed_roster_horizon_does_NOT_lock_the_company_out(self):
		"""Shift Schedules generate a finite horizon — currently to 2026-11-19.

		The morning after it lapses is not a decision anybody made about
		anybody, and blocking there would refuse every employee in the company
		at once over a housekeeping job nobody was watching.
		"""
		result = self._run(shift=None, within_horizon=False)
		self.assertFalse(result["blocked"])

	def test_a_hole_INSIDE_the_horizon_still_blocks(self):
		result = self._run(shift=None, within_horizon=True)
		self.assertTrue(result["blocked"])

	def test_an_explicit_day_off_outranks_the_horizon_escape_hatch(self):
		"""A granted day off is a decision, so it blocks wherever it falls."""
		result = self._run(shift=None, has_day_off=True, within_horizon=False)
		self.assertTrue(result["blocked"])


class TestOvernightTail(unittest.TestCase):
	"""Exactly when yesterday's shift may still claim today's check-in.

	This is the narrow allowance that keeps a late clock-out working. It must
	stay narrow: widening it to "any assignment dated yesterday" is what
	silently switched the whole gate off.
	"""

	def _tail(self, start, end, at, grace=0, shift_type="Branch Closing"):
		with patch.object(checkin_guard, "frappe") as mock_frappe:
			mock_frappe.db.get_value.return_value = {
				"start_time": start,
				"end_time": end,
				"allow_check_out_after_shift_end_time": grace,
			}
			return checkin_guard._is_within_overnight_tail({"shift_type": shift_type}, at)

	def test_inside_the_tail_of_a_closing_shift(self):
		# 16:00 -> 01:00, checking out at 00:40.
		self.assertTrue(self._tail(timedelta(hours=16), timedelta(hours=1), "2026-09-10 00:40:00"))

	def test_exactly_at_the_shift_end_still_counts(self):
		self.assertTrue(self._tail(timedelta(hours=16), timedelta(hours=1), "2026-09-10 01:00:00"))

	def test_past_the_tail_does_not_count(self):
		self.assertFalse(self._tail(timedelta(hours=16), timedelta(hours=1), "2026-09-10 01:30:00"))

	def test_the_grace_period_extends_it(self):
		self.assertTrue(self._tail(timedelta(hours=16), timedelta(hours=1), "2026-09-10 01:30:00", grace=60))

	def test_a_midday_checkin_is_never_a_tail(self):
		self.assertFalse(self._tail(timedelta(hours=16), timedelta(hours=1), "2026-09-10 13:00:00"))

	def test_a_daytime_shift_has_no_tail_at_all(self):
		# The factory runs 10:00 -> 19:00 and can never claim the next morning.
		self.assertFalse(self._tail(timedelta(hours=10), timedelta(hours=19), "2026-09-10 09:00:00"))

	def test_a_shift_with_no_times_claims_nothing(self):
		self.assertFalse(self._tail(None, None, "2026-09-10 00:10:00"))

	def test_an_assignment_with_no_shift_type_claims_nothing(self):
		self.assertFalse(checkin_guard._is_within_overnight_tail({}, "2026-09-10 00:10:00"))


class TestRosteredHorizon(unittest.TestCase):
	"""Where the roster is silent rather than negative."""

	def _within(self, rows, day):
		with patch.object(checkin_guard, "frappe") as mock_frappe:
			mock_frappe.get_all.return_value = rows
			return checkin_guard._is_within_rostered_horizon("HR-EMP-00001", day)

	SPAN = [
		{"start_date": "2026-08-21", "end_date": "2026-09-10"},
		{"start_date": "2026-09-11", "end_date": "2026-11-19"},
	]

	def test_a_date_inside_the_span_is_covered(self):
		self.assertTrue(self._within(self.SPAN, "2026-09-15"))

	def test_the_day_after_the_horizon_is_not_covered(self):
		self.assertFalse(self._within(self.SPAN, "2026-11-20"))

	def test_the_last_rostered_day_is_still_covered(self):
		self.assertTrue(self._within(self.SPAN, "2026-11-19"))

	def test_before_a_new_joiner_started_is_not_covered(self):
		self.assertFalse(self._within(self.SPAN, "2026-08-01"))

	def test_an_open_ended_assignment_keeps_the_strict_behaviour(self):
		rows = [{"start_date": "2026-08-21", "end_date": None}]
		self.assertTrue(self._within(rows, "2027-05-01"))

	def test_an_employee_with_no_assignments_at_all_is_not_covered(self):
		self.assertFalse(self._within([], "2026-09-15"))

	def test_a_failed_probe_never_turns_somebody_away(self):
		with patch.object(checkin_guard, "frappe") as mock_frappe:
			mock_frappe.get_all.side_effect = Exception("db down")
			self.assertFalse(checkin_guard._is_within_rostered_horizon("HR-EMP-00001", "2026-09-15"))


class TestCheckinExplanation(unittest.TestCase):
	"""The two refusals need different fixes, so they need different messages."""

	def _explain(self, off_row):
		with patch.object(checkin_guard, "frappe") as mock_frappe:
			mock_frappe.db.get_value.return_value = off_row
			mock_frappe._ = lambda s: s
			return checkin_guard._explanation("HR-EMP-00001", date(2026, 9, 10))

	def test_marked_off_says_so(self):
		message = self._explain({"off_type": "Vacation", "covered_by_name": None})
		self.assertIn("marked off", message.lower())

	def test_marked_off_names_the_colleague_covering(self):
		message = self._explain({"off_type": "Weekly Off", "covered_by_name": "Ahmed Samir"})
		self.assertIn("Ahmed Samir", message)

	def test_unrostered_points_at_the_manager_not_at_a_day_off(self):
		message = self._explain(None)
		self.assertIn("roster", message.lower())
		self.assertNotIn("marked off", message.lower())


class TestEnforcementDefaultsOn(unittest.TestCase):
	"""An unwritten Check must not read as "gate off".

	``get_single_value`` casts a Check through ``cint()``, so a field nobody has
	ever saved comes back as 0. If the gate read it that way it would be
	silently disabled on exactly the sites that had never configured it -- and a
	disabled gate looks identical to a correctly-rostered company right up until
	somebody clocks in from home.
	"""

	def test_unset_setting_defaults_to_enabled(self):
		with patch.object(checkin_guard, "single_flag") as mock_flag:
			mock_flag.return_value = True
			self.assertTrue(checkin_guard._enforcement_enabled())
			_doctype, field, default = mock_flag.call_args[0]
			self.assertEqual(field, "roster_enforce_checkin")
			self.assertTrue(default, "the gate must default ON when never configured")

	def test_a_broken_read_fails_closed_rather_than_open(self):
		"""If the setting cannot be read we do not block people out of work."""
		with patch.object(checkin_guard, "single_flag", side_effect=Exception("no db")):
			self.assertFalse(checkin_guard._enforcement_enabled())


class TestHoursSummaryArithmetic(unittest.TestCase):
	"""Overtime is the gap between the shift worked and a normal day."""

	def _month_payload(self, designation, standard, multiplier, shift_hours):
		days = {}
		for index, hours in enumerate(shift_hours, start=1):
			key = f"2026-09-{index:02d}"
			days[key] = {
				"date": key,
				"shift_type": "X" if hours else None,
				"hours": hours,
				"shift_location": "Nasr City",
				"day_off": None if hours else {"off_type": "Weekly Off"},
				"is_holiday": False,
			}
		return {
			"hrms_available": True,
			"month": "2026-09",
			"month_start": "2026-09-01",
			"month_end": "2026-09-30",
			"scope": {},
			"employees": [
				{
					"employee": "HR-EMP-00001",
					"employee_name": "Test Person",
					"designation": designation,
					"shift_locations": ["Nasr City"],
					"standard_hours": standard,
					"is_courier": designation == "Courier",
					"overtime_multiplier": multiplier,
					"days": days,
				}
			],
		}

	def _summarise(self, payload):
		with patch.object(roster_service, "get_month", return_value=payload):
			return roster_service.month_hours("2026-09")

	def test_dispatcher_cover_day_credits_overtime_at_one(self):
		# Two normal 9h days plus one 12.5h cover day: 3.5h overtime, credited 3.5.
		payload = self._month_payload("Dispatcher", 9.0, 1.0, [9.0, 9.0, 12.5])
		row = self._summarise(payload)["rows"][0]
		self.assertEqual(row["worked_hours"], 30.5)
		self.assertEqual(row["overtime_hours"], 3.5)
		self.assertEqual(row["credited_overtime_hours"], 3.5)
		self.assertEqual(row["credited_hours"], 30.5)
		self.assertEqual(row["cover_days"], 1)

	def test_courier_overtime_is_credited_at_two(self):
		# A 10h courier who works a 12h day: 2h overtime, credited as 4.
		payload = self._month_payload("Courier", 10.0, 2.0, [10.0, 12.0])
		row = self._summarise(payload)["rows"][0]
		self.assertEqual(row["worked_hours"], 22.0)
		self.assertEqual(row["overtime_hours"], 2.0)
		self.assertEqual(row["credited_overtime_hours"], 4.0)
		# 20 base + 4 credited overtime, not the 22 hours actually stood there.
		self.assertEqual(row["credited_hours"], 24.0)

	def test_days_off_are_counted_and_never_negative(self):
		payload = self._month_payload("Dispatcher", 9.0, 1.0, [9.0, 0.0, 9.0])
		row = self._summarise(payload)["rows"][0]
		self.assertEqual(row["off_days"], 1)
		self.assertEqual(row["worked_days"], 2)
		self.assertEqual(row["overtime_hours"], 0.0)

	def test_a_short_day_never_produces_negative_overtime(self):
		"""Somebody moved onto a shorter shift owes nothing back."""
		payload = self._month_payload("Dispatcher", 9.0, 1.0, [6.0])
		row = self._summarise(payload)["rows"][0]
		self.assertEqual(row["overtime_hours"], 0.0)
		self.assertEqual(row["base_hours"], 6.0)
		self.assertEqual(row["credited_hours"], 6.0)


# ---------------------------------------------------------------------------
# Rota plan wizard
# ---------------------------------------------------------------------------


class _PlanError(Exception):
	"""Stands in for ``frappe.throw`` so validation failures are catchable."""


def _raise(message=None, *args, **kwargs):
	raise _PlanError(message)


PLAN_TODAY = date(2026, 10, 12)
# Monday 2 Nov 2026 .. Sunday 8 Nov 2026: one week, one of each weekday.
WEEK_START = date(2026, 11, 2)
WEEK_END = date(2026, 11, 8)
FRIDAY_DATE = date(2026, 11, 6)
KNOWN_TYPES = {"Morning", "Morning - Friday", "Evening", "Full Day"}


def _types():
	return roster_service._ShiftTypes(known=KNOWN_TYPES)


def _row(employee, shift_type="Morning", location="Nasr City", off_days=(), covers=(), variant=True):
	return {
		"employee": employee,
		"shift_type": shift_type,
		"shift_location": location,
		"off_days": list(off_days),
		"use_friday_variant": variant,
		"covers": [
			{"weekday": weekday, "covered_by": by, "cover_shift_type": "Full Day"} for weekday, by in covers
		],
	}


def _payload(rows, start=WEEK_START, end=WEEK_END, replace_manual=False):
	return {
		"start": str(start),
		"end": str(end),
		"replace_manual": replace_manual,
		"rows": rows,
	}


def _state(**kwargs):
	kwargs.setdefault("employee_names", {"A": "Ali", "B": "Omar", "C": "Mona", "X": "Ziad"})
	return roster_service._PlanState(**kwargs)


def _full_week_rota(location="Nasr City"):
	return {weekday: ("Morning", location) for weekday in roster_service.WEEKDAYS}


class _PlanTestCase(unittest.TestCase):
	"""Patches ``frappe.throw`` to raise and ``_`` to identity for each test."""

	def setUp(self):
		self.assertEqual(WEEK_START.weekday(), 0)
		frappe_patch = patch.object(roster_service, "frappe")
		self.mock_frappe = frappe_patch.start()
		self.mock_frappe.throw.side_effect = _raise
		self.addCleanup(frappe_patch.stop)
		translate_patch = patch.object(roster_service, "_", side_effect=lambda text: text)
		translate_patch.start()
		self.addCleanup(translate_patch.stop)

	def normalize(self, rows, **kwargs):
		return roster_service._normalize_plan(_payload(rows, **kwargs), _types(), today=PLAN_TODAY)

	def compute(self, rows, state=None, **kwargs):
		plan = self.normalize(rows, **kwargs)
		return roster_service._compute_plan(plan, state or _state())

	@staticmethod
	def codes(response):
		return [issue["code"] for issue in response["issues"]]


class TestRotaPlanExpansion(_PlanTestCase):
	"""Weekdays become days, and Friday picks up its own shift type."""

	def test_friday_variant_on(self):
		plan = self.normalize([_row("A", off_days=["Sunday"])])
		row = plan["rows"][0]
		self.assertEqual(row["friday_shift_type"], "Morning - Friday")
		days = dict(roster_service._expand_plan(row, WEEK_START, WEEK_END))
		self.assertEqual(days[FRIDAY_DATE], "Morning - Friday")
		self.assertEqual(days[WEEK_START], "Morning")
		self.assertIsNone(days[WEEK_END])

	def test_friday_variant_off(self):
		plan = self.normalize([_row("A", off_days=["Sunday"], variant=False)])
		row = plan["rows"][0]
		self.assertIsNone(row["friday_shift_type"])
		days = dict(roster_service._expand_plan(row, WEEK_START, WEEK_END))
		self.assertEqual(days[FRIDAY_DATE], "Morning")

	def test_variant_used_for_planned_shift_on_friday_off(self):
		plan = self.normalize([_row("A", off_days=["Friday"])])
		row = plan["rows"][0]
		self.assertEqual(roster_service._planned_shift(row, "Friday"), "Morning - Friday")
		self.assertEqual(roster_service._planned_shift(row, "Monday"), "Morning")

	def test_weekday_names_are_canonicalised(self):
		plan = self.normalize([_row("A", off_days=["friday", "Sun"])])
		self.assertEqual(plan["rows"][0]["off_days"], ["Friday", "Sunday"])


class TestRotaPlanRanges(unittest.TestCase):
	"""One assignment per run, not one per day."""

	def _entries(self, pattern):
		return [
			(WEEK_START + timedelta(days=i), shift, "Nasr City") for i, shift in enumerate(pattern) if shift
		]

	def test_plain_week_is_one_range(self):
		ranges = roster_service._contiguous_ranges(self._entries(["Morning"] * 7))
		self.assertEqual(ranges, [(WEEK_START, WEEK_END, "Morning", "Nasr City")])

	def test_off_day_splits_the_range(self):
		pattern = ["Morning", "Morning", "Morning", None, "Morning", "Morning", "Morning"]
		ranges = roster_service._contiguous_ranges(self._entries(pattern))
		self.assertEqual(
			[(r[0], r[1]) for r in ranges],
			[(WEEK_START, date(2026, 11, 4)), (date(2026, 11, 6), WEEK_END)],
		)

	def test_friday_variant_splits_the_range(self):
		pattern = ["Morning"] * 4 + ["Morning - Friday", "Morning", None]
		ranges = roster_service._contiguous_ranges(self._entries(pattern))
		self.assertEqual(
			ranges,
			[
				(WEEK_START, date(2026, 11, 5), "Morning", "Nasr City"),
				(FRIDAY_DATE, FRIDAY_DATE, "Morning - Friday", "Nasr City"),
				(date(2026, 11, 7), date(2026, 11, 7), "Morning", "Nasr City"),
			],
		)

	def test_location_change_splits_the_range(self):
		entries = [
			(WEEK_START, "Morning", "Nasr City"),
			(WEEK_START + timedelta(days=1), "Morning", "Dokki"),
		]
		self.assertEqual(len(roster_service._contiguous_ranges(entries)), 2)


class TestRotaPlanManualDays(_PlanTestCase):
	"""Which days count as "changed by hand" -- the days a plan keeps."""

	def _manual(self, state, planned=("A",)):
		days = roster_service._days_between(WEEK_START, WEEK_END)
		return roster_service._manual_days("A", days, state, set(planned))

	def _linked_week(self):
		return {
			"A": [
				{
					"name": "SA-1",
					"shift_type": "Morning",
					"shift_location": "Nasr City",
					"start_date": WEEK_START,
					"end_date": WEEK_END,
					"shift_schedule_assignment": "SSA-1",
				}
			]
		}

	def test_hand_made_day_off_is_manual(self):
		state = _state(
			assignments=self._linked_week(),
			day_offs=[{"name": "D1", "employee": "A", "off_date": WEEK_START, "from_plan": 0}],
		)
		manual = self._manual(state)
		self.assertEqual(manual[WEEK_START]["kind"], roster_service.KIND_DAY_OFF)

	def test_plan_day_off_is_not_manual(self):
		state = _state(
			assignments=self._linked_week(),
			day_offs=[{"name": "D1", "employee": "A", "off_date": WEEK_START, "from_plan": 1}],
		)
		self.assertEqual(self._manual(state), {})

	def test_unlinked_assignment_matching_rota_is_not_manual(self):
		# The tail a single-day edit splits off loses its schedule link but
		# still says exactly what the rota says.
		assignments = self._linked_week()
		assignments["A"][0]["shift_schedule_assignment"] = None
		state = _state(assignments=assignments, rota={"A": _full_week_rota()})
		self.assertEqual(self._manual(state), {})

	def test_unlinked_assignment_differing_from_rota_is_manual(self):
		assignments = self._linked_week()
		assignments["A"].append(
			{
				"name": "SA-2",
				"shift_type": "Evening",
				"shift_location": "Nasr City",
				"start_date": date(2026, 11, 5),
				"end_date": date(2026, 11, 5),
				"shift_schedule_assignment": None,
			}
		)
		state = _state(assignments=assignments, rota={"A": _full_week_rota()})
		manual = self._manual(state)
		self.assertEqual(list(manual), [date(2026, 11, 5)])
		self.assertEqual(manual[date(2026, 11, 5)]["kind"], roster_service.KIND_SHIFT)
		self.assertEqual(manual[date(2026, 11, 5)]["current"], "Evening")

	def test_no_rota_means_no_shift_manual_days(self):
		assignments = self._linked_week()
		assignments["A"][0]["shift_schedule_assignment"] = None
		assignments["A"][0]["shift_type"] = "Evening"
		self.assertEqual(self._manual(_state(assignments=assignments)), {})

	def test_emptied_day_inside_horizon_is_manual(self):
		assignments = {
			"A": [
				{
					"name": "SA-1",
					"shift_type": "Morning",
					"shift_location": "Nasr City",
					"start_date": WEEK_START,
					"end_date": date(2026, 11, 7),
					"shift_schedule_assignment": "SSA-1",
				}
			]
		}
		state = _state(
			assignments=assignments,
			rota={"A": _full_week_rota()},
			rota_horizon={"A": date(2026, 12, 31)},
		)
		manual = self._manual(state)
		self.assertEqual(manual[WEEK_END]["kind"], roster_service.KIND_EMPTY)

	def test_day_past_horizon_is_not_empty(self):
		state = _state(rota={"A": _full_week_rota()}, rota_horizon={"A": date(2026, 11, 1)})
		self.assertEqual(self._manual(state), {})

	def test_hand_made_cover_is_manual(self):
		state = _state(
			assignments=self._linked_week(),
			day_offs=[
				{"name": "D1", "employee": "X", "off_date": FRIDAY_DATE, "covered_by": "A", "from_plan": 0}
			],
		)
		manual = self._manual(state)
		self.assertEqual(manual[FRIDAY_DATE]["kind"], roster_service.KIND_COVER)
		self.assertEqual(manual[FRIDAY_DATE]["current"], "Covering Ziad")

	def test_plan_cover_of_replanned_person_is_not_manual(self):
		state = _state(
			assignments=self._linked_week(),
			day_offs=[
				{"name": "D1", "employee": "C", "off_date": FRIDAY_DATE, "covered_by": "A", "from_plan": 1}
			],
		)
		self.assertEqual(self._manual(state, planned=("A", "C")), {})


class TestRotaPlanPreview(_PlanTestCase):
	"""Counts, kept days and the issue list."""

	def test_counts_for_a_simple_week(self):
		response = self.compute([_row("A", off_days=["Friday"], covers=[("Friday", "B")])])["response"]
		row = response["rows"][0]
		self.assertEqual((row["shift_days"], row["day_offs"], row["covers"]), (6, 1, 1))
		self.assertEqual(response["summary"]["errors"], 0)
		self.assertTrue(response["dry_run"])
		self.assertFalse(response["applied"])

	def test_manual_days_are_kept_and_not_counted(self):
		state = _state(day_offs=[{"name": "D1", "employee": "A", "off_date": WEEK_START, "from_plan": 0}])
		response = self.compute([_row("A")], state=state)["response"]
		row = response["rows"][0]
		self.assertEqual(row["shift_days"], 6)
		self.assertEqual(row["manual_days"], [{"date": str(WEEK_START), "current": "Off", "kind": "day_off"}])
		self.assertEqual(response["summary"]["manual_kept"], 1)
		self.assertEqual(response["summary"]["manual_replaced"], 0)

	def test_replace_manual_rewrites_manual_days(self):
		state = _state(day_offs=[{"name": "D1", "employee": "A", "off_date": WEEK_START, "from_plan": 0}])
		computed = self.compute([_row("A")], state=state, replace_manual=True)
		row = computed["response"]["rows"][0]
		self.assertEqual(row["shift_days"], 7)
		self.assertEqual(len(row["manual_days"]), 1)
		self.assertEqual(computed["response"]["summary"]["manual_kept"], 0)
		self.assertEqual(computed["response"]["summary"]["manual_replaced"], 1)
		self.assertEqual(computed["rows"]["A"]["kept"], set())

	def test_cover_is_off_when_coverer_planned_off(self):
		rows = [
			_row("A", off_days=["Friday"], covers=[("Friday", "B")]),
			_row("B", off_days=["Friday"]),
		]
		response = self.compute(rows)["response"]
		self.assertIn("cover_is_off", self.codes(response))
		self.assertGreaterEqual(response["summary"]["errors"], 1)
		self.assertEqual(response["rows"][0]["covers"], 0)

	def test_cover_is_off_when_outside_coverer_has_a_day_off(self):
		state = _state(day_offs=[{"name": "D1", "employee": "X", "off_date": FRIDAY_DATE, "from_plan": 0}])
		response = self.compute([_row("A", off_days=["Friday"], covers=[("Friday", "X")])], state=state)[
			"response"
		]
		self.assertEqual(self.codes(response), ["cover_is_off"])

	def test_cover_double(self):
		rows = [
			_row("A", off_days=["Friday"], covers=[("Friday", "B")]),
			_row("C", off_days=["Friday"], covers=[("Friday", "B")]),
			_row("B"),
		]
		response = self.compute(rows)["response"]
		doubles = [i for i in response["issues"] if i["code"] == "cover_double"]
		self.assertEqual({i["employee"] for i in doubles}, {"A", "C"})
		self.assertTrue(all(i["severity"] == "error" for i in doubles))

	def test_cover_day_manual_skips_the_cover(self):
		state = _state(
			assignments={
				"B": [
					{
						"name": "SA-B",
						"shift_type": "Evening",
						"shift_location": "Nasr City",
						"start_date": FRIDAY_DATE,
						"end_date": FRIDAY_DATE,
						"shift_schedule_assignment": None,
					}
				]
			},
			rota={"B": _full_week_rota()},
		)
		rows = [_row("A", off_days=["Friday"], covers=[("Friday", "B")]), _row("B")]
		response = self.compute(rows, state=state)["response"]
		self.assertEqual(self.codes(response), ["cover_day_manual"])
		self.assertEqual(response["rows"][0]["covers"], 0)
		self.assertEqual(response["summary"]["warnings"], 1)

	def test_same_day_off_once_per_pair_on_first_date(self):
		rows = [
			_row("A", off_days=["Wednesday"]),
			_row("C", off_days=["Wednesday"]),
		]
		two_weeks_end = WEEK_END + timedelta(days=7)
		response = self.compute(rows, end=two_weeks_end)["response"]
		same = [i for i in response["issues"] if i["code"] == "same_day_off"]
		self.assertEqual(len(same), 1)
		self.assertEqual(same[0]["date"], "2026-11-04")
		self.assertEqual(same[0]["severity"], "warning")

	def test_same_day_off_ignored_across_branches_or_when_both_covered(self):
		rows = [
			_row("A", off_days=["Wednesday"]),
			_row("C", location="Dokki", off_days=["Wednesday"]),
		]
		self.assertNotIn("same_day_off", self.codes(self.compute(rows)["response"]))
		rows = [
			_row("A", off_days=["Wednesday"], covers=[("Wednesday", "B")]),
			_row("C", off_days=["Wednesday"], covers=[("Wednesday", "X")]),
		]
		self.assertNotIn("same_day_off", self.codes(self.compute(rows)["response"]))

	def test_no_friday_variant_is_info(self):
		response = self.compute([_row("A", shift_type="Evening")])["response"]
		issues = [i for i in response["issues"] if i["code"] == "no_friday_variant"]
		self.assertEqual(len(issues), 1)
		self.assertEqual(issues[0]["severity"], "info")
		self.assertEqual(issues[0]["date"], str(FRIDAY_DATE))
		self.assertEqual(response["summary"]["errors"], 0)

	def test_no_friday_variant_silent_when_friday_is_off(self):
		response = self.compute([_row("A", shift_type="Evening", off_days=["Friday"])])["response"]
		self.assertNotIn("no_friday_variant", self.codes(response))


class TestRotaPlanValidation(_PlanTestCase):
	"""Hard errors: thrown before anything is read or written."""

	def assertRefused(self, rows, **kwargs):
		with self.assertRaises(_PlanError):
			self.normalize(rows, **kwargs)

	def test_start_in_the_past(self):
		self.assertRefused([_row("A")], start=PLAN_TODAY - timedelta(days=1))

	def test_today_is_allowed(self):
		plan = self.normalize([_row("A")], start=PLAN_TODAY, end=PLAN_TODAY)
		self.assertEqual(plan["start"], PLAN_TODAY)

	def test_end_before_start(self):
		self.assertRefused([_row("A")], start=WEEK_END, end=WEEK_START)

	def test_more_than_62_days(self):
		self.assertRefused([_row("A")], end=WEEK_START + timedelta(days=62))

	def test_62_days_is_allowed(self):
		self.normalize([_row("A")], end=WEEK_START + timedelta(days=61))

	def test_cover_weekday_must_be_an_off_day(self):
		self.assertRefused([_row("A", off_days=["Sunday"], covers=[("Friday", "B")])])

	def test_duplicate_employee(self):
		self.assertRefused([_row("A"), _row("A", shift_type="Evening")])

	def test_cannot_cover_yourself(self):
		self.assertRefused([_row("A", off_days=["Friday"], covers=[("Friday", "A")])])

	def test_unknown_shift_type(self):
		self.assertRefused([_row("A", shift_type="Night")])

	def test_unknown_cover_shift_type(self):
		row = _row("A", off_days=["Friday"], covers=[("Friday", "B")])
		row["covers"][0]["cover_shift_type"] = "Night"
		self.assertRefused([row])

	def test_off_every_day(self):
		self.assertRefused([_row("A", off_days=list(roster_service.WEEKDAYS))])

	def test_blank_cover_is_ignored(self):
		row = _row("A", off_days=["Friday"])
		row["covers"] = [{"weekday": "Friday", "covered_by": "", "cover_shift_type": None}]
		plan = self.normalize([row])
		self.assertEqual(plan["rows"][0]["covers"], {})


class TestRotaPlanApply(_PlanTestCase):
	"""``apply_plan`` must refuse a plan its own preview calls broken."""

	def _patches(self, state):
		stack = [
			patch.object(roster_service, "hrms_available", return_value=True),
			patch.object(roster_service, "_today", return_value=PLAN_TODAY),
			patch.object(roster_service, "_ShiftTypes", return_value=_types()),
			patch.object(roster_service, "_load_plan_state", return_value=state),
			patch.object(roster_service, "_apply_row"),
			patch.object(
				roster_service,
				"set_day_off",
				return_value={"day_off": "D-NEW", "cover_shift_location": "Nasr City"},
			),
		]
		mocks = [p.start() for p in stack]
		for p in stack:
			self.addCleanup(p.stop)
		return mocks[4], mocks[5]

	def test_apply_throws_on_error_issue(self):
		apply_row, set_day_off = self._patches(_state())
		rows = [
			_row("A", off_days=["Friday"], covers=[("Friday", "B")]),
			_row("C", off_days=["Friday"], covers=[("Friday", "B")]),
			_row("B"),
		]
		with self.assertRaises(_PlanError):
			roster_service.apply_plan(_payload(rows))
		apply_row.assert_not_called()
		set_day_off.assert_not_called()
		self.mock_frappe.db.savepoint.assert_not_called()

	def test_apply_writes_rows_then_plan_day_offs(self):
		apply_row, set_day_off = self._patches(_state())
		rows = [_row("A", off_days=["Friday"], covers=[("Friday", "B")]), _row("B")]
		result = roster_service.apply_plan(_payload(rows))
		self.assertTrue(result["applied"])
		self.assertFalse(result["dry_run"])
		self.assertEqual(apply_row.call_count, 2)
		set_day_off.assert_called_once()
		args, kwargs = set_day_off.call_args
		self.assertEqual(args[:3], ("A", FRIDAY_DATE, "Weekly Off"))
		self.assertEqual(kwargs["covered_by"], "B")
		self.assertEqual(kwargs["cover_shift_type"], "Full Day")
		self.assertEqual(kwargs["original_shift_type"], "Morning - Friday")
		self.assertEqual(kwargs["original_shift_location"], "Nasr City")
		self.assertTrue(kwargs["from_plan"])
		self.assertEqual(
			result["created_covers"],
			[
				{
					"employee": "A",
					"covered_by": "B",
					"date": str(FRIDAY_DATE),
					"day_off": "D-NEW",
					"cover_shift_location": "Nasr City",
				}
			],
		)

	def test_failed_row_aborts_the_whole_plan(self):
		"""All or nothing: a failure throws (Frappe rolls the request back)."""
		apply_row, set_day_off = self._patches(_state())
		apply_row.side_effect = [None, RuntimeError("overlap")]
		rows = [_row("B"), _row("A", off_days=["Friday"])]
		with self.assertRaises(_PlanError) as caught:
			roster_service.apply_plan(_payload(rows))
		self.assertIn("Ali", str(caught.exception))
		self.assertIn("overlap", str(caught.exception))
		set_day_off.assert_not_called()
		self.mock_frappe.db.savepoint.assert_not_called()
		self.mock_frappe.db.rollback.assert_not_called()

	def test_failed_day_off_aborts_with_name_and_date(self):
		_apply_row, set_day_off = self._patches(_state())
		set_day_off.side_effect = RuntimeError("linked check-in")
		with self.assertRaises(_PlanError) as caught:
			roster_service.apply_plan(_payload([_row("A", off_days=["Friday"])]))
		message = str(caught.exception)
		self.assertIn("Ali", message)
		self.assertIn(str(FRIDAY_DATE), message)
		self.mock_frappe.db.savepoint.assert_not_called()

	def test_response_has_no_failed_list(self):
		self._patches(_state())
		result = roster_service.apply_plan(_payload([_row("A")]))
		self.assertNotIn("failed", result)

	def test_people_touched_outside_the_plan_are_checked_before_writing(self):
		state = _state(
			day_offs=[
				{"name": "D1", "employee": "A", "off_date": FRIDAY_DATE, "covered_by": "X", "from_plan": 1}
			]
		)
		apply_row, _set_day_off = self._patches(state)
		check = MagicMock(side_effect=_PlanError("outside"))
		with self.assertRaises(_PlanError):
			roster_service.apply_plan(_payload([_row("A")]), check_employee=check)
		check.assert_called_once_with("X")
		apply_row.assert_not_called()


class TestRotaPlanTouchedOutside(_PlanTestCase):
	"""People whose day changes although they are not in the payload."""

	def test_coverer_of_dropped_plan_day_off(self):
		state = _state(
			day_offs=[
				{"name": "D1", "employee": "A", "off_date": FRIDAY_DATE, "covered_by": "X", "from_plan": 1},
				{"name": "D2", "employee": "C", "off_date": FRIDAY_DATE, "covered_by": "B", "from_plan": 1},
			]
		)
		response = self.compute([_row("A"), _row("B")], state=state)["response"]
		# D2 belongs to C, who is not re-planned, so it is not dropped.
		self.assertEqual(response["touched_outside"], ["X"])

	def test_replace_manual_adds_hand_cover_absentee_and_hand_coverer(self):
		state = _state(
			day_offs=[
				{"name": "D1", "employee": "X", "off_date": WEEK_START, "covered_by": "A", "from_plan": 0},
				{"name": "D2", "employee": "A", "off_date": FRIDAY_DATE, "covered_by": "C", "from_plan": 0},
			]
		)
		kept = self.compute([_row("A")], state=state)["response"]
		self.assertEqual(kept["touched_outside"], [])
		replaced = self.compute([_row("A")], state=state, replace_manual=True)["response"]
		self.assertEqual(replaced["touched_outside"], ["C", "X"])


class TestRotaPlanAfterEnd(_PlanTestCase):
	"""After ``end`` nothing may block HRMS's first generated range.

	Invariant: once the cuts are written and (end, horizon] is filled, no
	assignment of this person reaches past ``horizon`` and none is
	open-ended, so ``create_shifts(horizon + 1)`` starts on empty days.
	"""

	NOV_END = date(2026, 11, 30)

	def _final_segments(self, assignments, cuts, fills):
		segments = []
		for assignment in assignments:
			cut = cuts.get(assignment["name"])
			if cut is None:
				segments.append((assignment["start_date"], assignment["end_date"]))
				continue
			if cut["head_end"]:
				segments.append((assignment["start_date"], cut["head_end"]))
			segments.extend(cut["keep_runs"])
		segments.extend((s, e) for s, e, _t, _l in roster_service._contiguous_ranges(fills))
		return segments

	def _run(self, assignments, start, end, old_rota, old_after, pattern, off_after=(), protected=()):
		cuts = {}
		for assignment in assignments:
			cut = roster_service._plan_cut(assignment, start, end, set(), old_rota, set(protected))
			if cut:
				cuts[assignment["name"]] = cut
		remaining = roster_service._remaining_after_end(assignments, cuts, end)
		horizon = roster_service._plan_horizon(end, old_after, remaining, off_after)
		fills = roster_service._fill_entries(end, horizon, pattern, "Nasr City", remaining, off_after)
		segments = self._final_segments(assignments, cuts, fills)
		for seg_start, seg_end in segments:
			self.assertIsNotNone(seg_end, "an open-ended assignment survived the plan")
			self.assertLessEqual(seg_end, horizon, f"{seg_start}..{seg_end} is past horizon {horizon}")
		return cuts, horizon, fills

	@staticmethod
	def _assignment(name, shift_type, first, last, link=None):
		return {
			"name": name,
			"employee": "A",
			"shift_type": shift_type,
			"shift_location": "Nasr City",
			"start_date": first,
			"end_date": last,
			"shift_schedule_assignment": link,
		}

	def test_no_rota_and_open_ended_assignment(self):
		assignments = [self._assignment("SA-1", "Morning", date(2026, 10, 1), None)]
		pattern = roster_service._row_pattern("Evening", ["Friday"], None)
		cuts, horizon, fills = self._run(assignments, WEEK_START, WEEK_END, {}, [], pattern)
		self.assertEqual(cuts["SA-1"]["head_end"], date(2026, 11, 1))
		self.assertEqual(cuts["SA-1"]["keep_runs"], [])
		self.assertEqual(horizon, WEEK_END)
		self.assertEqual(fills, [])

	def test_open_ended_assignment_starting_after_end_is_removed(self):
		assignments = [self._assignment("SA-1", "Morning", date(2026, 12, 1), None)]
		pattern = roster_service._row_pattern("Evening", [], None)
		cuts, horizon, _fills = self._run(assignments, WEEK_START, WEEK_END, {}, [], pattern)
		self.assertEqual(cuts["SA-1"], {"name": "SA-1", "head_end": None, "keep_runs": []})
		self.assertEqual(horizon, WEEK_END)

	def test_hand_rostered_december(self):
		old_rota = _full_week_rota()
		assignments = [
			self._assignment("SA-1", "Morning", date(2026, 11, 1), self.NOV_END, "SSA-OLD"),
			# Hand edit: differs from the old rota, so it stays.
			self._assignment("SA-2", "Evening", date(2026, 12, 1), date(2026, 12, 5)),
			# Fragment _break_assignment split off the old rota (unlinked, matches it).
			self._assignment("SA-3", "Morning", date(2026, 12, 6), date(2026, 12, 9)),
			# Generated by the old schedule.
			self._assignment("SA-4", "Morning", date(2026, 12, 10), date(2026, 12, 31), "SSA-OLD"),
		]
		pattern = roster_service._row_pattern("Evening", ["Friday"], None)
		cuts, horizon, fills = self._run(
			assignments, WEEK_START, self.NOV_END, old_rota, [date(2026, 12, 31)], pattern
		)
		self.assertNotIn("SA-2", cuts)
		self.assertEqual(cuts["SA-3"]["keep_runs"], [])
		self.assertEqual(cuts["SA-4"]["keep_runs"], [])
		self.assertEqual(horizon, date(2026, 12, 31))
		filled = {day: shift for day, shift, _loc in fills}
		# Tuesday 8 Dec was left on the old shift before; now it is the new rota.
		self.assertEqual(filled[date(2026, 12, 8)], "Evening")
		self.assertFalse(any(date(2026, 12, 1) <= d <= date(2026, 12, 5) for d in filled))
		self.assertNotIn(date(2026, 12, 11), filled)  # a Friday: off in the new rota

	def test_horizon_covers_hand_edits_and_day_offs_past_old_horizon(self):
		assignments = [self._assignment("SA-1", "Evening", date(2027, 1, 3), date(2027, 1, 4))]
		pattern = roster_service._row_pattern("Morning", [], None)
		cuts, horizon, fills = self._run(
			assignments,
			WEEK_START,
			WEEK_END,
			_full_week_rota(),
			[date(2026, 11, 30)],
			pattern,
			off_after=[date(2027, 1, 10)],
		)
		self.assertEqual(cuts, {})
		self.assertEqual(horizon, date(2027, 1, 10))
		filled = {day for day, _s, _l in fills}
		self.assertNotIn(date(2027, 1, 3), filled)
		self.assertNotIn(date(2027, 1, 10), filled)
		self.assertIn(date(2027, 1, 9), filled)

	def test_cover_day_after_end_is_kept_even_when_it_matches_the_rota(self):
		assignments = [self._assignment("SA-1", "Morning", date(2026, 11, 20), date(2026, 11, 20))]
		pattern = roster_service._row_pattern("Morning", [], None)
		cuts, horizon, _fills = self._run(
			assignments,
			WEEK_START,
			WEEK_END,
			_full_week_rota(),
			[],
			pattern,
			protected=[date(2026, 11, 20)],
		)
		self.assertEqual(cuts, {})
		self.assertEqual(horizon, date(2026, 11, 20))

	def test_kept_in_period_day_survives_the_cut(self):
		assignment = self._assignment("SA-1", "Evening", WEEK_START, WEEK_END)
		cut = roster_service._plan_cut(
			assignment, WEEK_START, WEEK_END, {FRIDAY_DATE}, _full_week_rota(), set()
		)
		self.assertEqual(cut["head_end"], None)
		self.assertEqual(cut["keep_runs"], [(FRIDAY_DATE, FRIDAY_DATE)])

	def test_horizon_refuses_open_ended_leftover(self):
		with self.assertRaises(_PlanError):
			roster_service._plan_horizon(WEEK_END, [], [(date(2026, 12, 1), None)], [])


class TestCoverPreviousLocation(unittest.TestCase):
	"""Undoing a cover puts the coverer back at THEIR branch, not the one covered."""

	def test_set_day_off_records_the_coverers_previous_branch(self):
		def fake_assignment_on(employee, _day):
			if employee == "ABSENT":
				return MagicMock(name_="SA-1", shift_type="Branch Opening", shift_location="Nasr City")
			return MagicMock(shift_type="Branch Closing", shift_location="Dokki")

		doc = MagicMock()
		with (
			patch.object(roster_service, "frappe") as mock_frappe,
			patch.object(roster_service, "_", side_effect=lambda t: t),
			patch.object(roster_service, "_assignment_on", side_effect=fake_assignment_on),
			patch.object(roster_service, "_break_assignment"),
			patch.object(roster_service, "set_shift_for_day", return_value={"shift_location": "Nasr City"}),
		):
			mock_frappe.new_doc.return_value = doc
			roster_service.set_day_off(
				"ABSENT", "2026-11-06", covered_by="COVER", cover_shift_type="Full Day"
			)
		self.assertEqual(doc.cover_previous_shift_location, "Dokki")
		self.assertEqual(doc.cover_shift_location, "Nasr City")

	def _clear(self, previous_location):
		doc = MagicMock()
		doc.name = "D1"
		doc.original_shift_type = None
		doc.covered_by = "COVER"
		doc.cover_previous_shift_type = "Branch Closing"
		doc.cover_shift_location = "Nasr City"
		doc.get.side_effect = lambda key, default=None: (
			previous_location if key == "cover_previous_shift_location" else default
		)
		with (
			patch.object(roster_service, "frappe") as mock_frappe,
			patch.object(roster_service, "_", side_effect=lambda t: t),
			patch.object(roster_service, "_release_pos_access"),
			patch.object(roster_service, "set_shift_for_day") as write,
		):
			mock_frappe.db.exists.return_value = "D1"
			mock_frappe.get_doc.return_value = doc
			roster_service.clear_day_off("ABSENT", "2026-11-06")
		return write.call_args

	def test_clear_day_off_restores_to_previous_branch(self):
		args, kwargs = self._clear("Dokki")
		self.assertEqual(args[0], "COVER")
		self.assertEqual(args[2], "Branch Closing")
		self.assertEqual(args[3], "Dokki")
		self.assertFalse(kwargs["clear_conflicting_day_off"])

	def test_clear_day_off_falls_back_for_old_rows(self):
		args, _kwargs = self._clear(None)
		self.assertEqual(args[3], "Nasr City")

	def test_restore_coverer_uses_previous_branch(self):
		record = {
			"covered_by": "COVER",
			"off_date": FRIDAY_DATE,
			"cover_previous_shift_type": "Branch Closing",
			"cover_previous_shift_location": "Dokki",
			"cover_shift_location": "Nasr City",
		}
		with patch.object(roster_service, "set_shift_for_day") as write:
			roster_service._restore_coverer(record)
		write.assert_called_once_with(
			"COVER", FRIDAY_DATE, "Branch Closing", "Dokki", clear_conflicting_day_off=False
		)

	def test_restore_coverer_with_no_previous_shift_empties_the_day(self):
		record = {"covered_by": "COVER", "off_date": FRIDAY_DATE, "cover_shift_location": "Nasr City"}
		with patch.object(roster_service, "set_shift_for_day") as write:
			roster_service._restore_coverer(record)
		self.assertIsNone(write.call_args[0][2])


class TestPlanApiGuards(unittest.TestCase):
	"""dry_run parsing and the stricter scope test for re-planned people."""

	def test_only_explicit_false_applies(self):
		for value in (0, "0", False, "false", "False", "no", " NO "):
			self.assertTrue(roster_api._wants_apply(value), value)
		for value in (None, "", "null", "none", "off", 1, "1", True, "true", "yes", "maybe"):
			self.assertFalse(roster_api._wants_apply(value), value)

	def _home(self, schedules, recent, allowed=frozenset({"Nasr City"})):
		def get_all(doctype, **_kwargs):
			if doctype == "Shift Schedule Assignment":
				return [{"shift_location": loc} for loc in schedules]
			return [{"shift_location": loc} for loc in recent]

		with (
			patch.object(roster_api, "frappe") as mock_frappe,
			patch.object(roster_api, "_", side_effect=lambda t: t),
			patch.object(roster_api, "nowdate", return_value="2026-10-12"),
			patch.object(roster_api.roster_service, "allowed_shift_locations", return_value=set(allowed)),
		):
			mock_frappe.throw.side_effect = _raise
			mock_frappe.get_all.side_effect = get_all
			mock_frappe.db.get_value.return_value = "Ali"
			roster_api._ensure_plan_home_in_scope("A")

	def test_standing_rota_outside_scope_is_refused_by_name(self):
		with self.assertRaises(_PlanError) as caught:
			self._home(["Nasr City", "Dokki"], ["Nasr City"])
		self.assertIn("Ali's standing rota is at Dokki", str(caught.exception))

	def test_standing_rota_inside_scope_passes(self):
		self._home(["Nasr City"], [])

	def test_no_rota_needs_a_recent_assignment_in_scope(self):
		self._home([], ["Nasr City"])
		with self.assertRaises(_PlanError):
			self._home([], ["Dokki"])
		with self.assertRaises(_PlanError):
			self._home([], [])

	def test_unrestricted_caller_skips_the_check(self):
		with (
			patch.object(roster_api.roster_service, "allowed_shift_locations", return_value=None),
			patch.object(roster_api, "frappe") as mock_frappe,
		):
			roster_api._ensure_plan_home_in_scope("A")
			mock_frappe.get_all.assert_not_called()


if __name__ == "__main__":
	unittest.main()

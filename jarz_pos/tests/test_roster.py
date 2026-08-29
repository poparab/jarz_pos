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

from jarz_pos.constants import ROLES
from jarz_pos.services import roster as roster_service
from jarz_pos.events import employee_checkin as checkin_guard


class TestShiftLengthHours(unittest.TestCase):
    """Hours must be right on both sides of midnight."""

    def test_plain_daytime_shift(self):
        self.assertEqual(
            roster_service.shift_length_hours(timedelta(hours=10), timedelta(hours=19)), 9.0
        )

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
        self.assertEqual(
            roster_service.shift_length_hours(timedelta(hours=16), timedelta(hours=1)), 9.0
        )

    def test_full_day_cover_shift(self):
        # 12:30 -> 01:00, the shift one person stretches onto when their
        # colleague is off. Twelve and a half hours, not minus eleven and a half.
        self.assertEqual(
            roster_service.shift_length_hours(timedelta(hours=12, minutes=30), timedelta(hours=1)),
            12.5,
        )

    def test_courier_twelve_hour_shift(self):
        self.assertEqual(
            roster_service.shift_length_hours(timedelta(hours=13), timedelta(hours=1)), 12.0
        )

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
        self.assertEqual(
            roster_service.shift_length_hours(timedelta(hours=9), timedelta(hours=9)), 24.0
        )


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
        with patch.object(roster_service, "frappe") as mock_frappe, patch.object(
            roster_service, "single_float"
        ) as mock_float, patch.object(
            roster_service, "_scheduled_shift_types", return_value=list(scheduled)
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
        self.assertEqual(
            self._multiplier(None, scheduled=["Courier Nasr City"]), 2.0
        )
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
        self.assertEqual(
            roster_service._time_text(timedelta(hours=16)), "16:00:00"
        )

    def test_only_none_means_absent(self):
        self.assertEqual(roster_service._time_text(None), "")

    def test_a_shift_ending_at_midnight_still_measures_correctly(self):
        # 14:00 -> 00:00 is ten hours.
        self.assertEqual(
            roster_service.shift_length_hours(timedelta(hours=14), timedelta(0)), 10.0
        )


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
    ):
        doc = MagicMock()
        doc.employee = "HR-EMP-00001"
        doc.time = "2026-09-10 09:00:00"
        doc.shift = shift

        thrown = {}

        def fake_throw(msg, title=None, exc=None):
            thrown["msg"] = str(msg)
            raise AssertionError("BLOCKED")

        with patch.object(checkin_guard, "_enforcement_enabled", return_value=enforcement), patch.object(
            checkin_guard, "_is_exempt", return_value=exempt
        ), patch.object(
            checkin_guard, "_is_roster_managed", return_value=roster_managed
        ), patch.object(
            checkin_guard,
            "_assignment_on",
            side_effect=lambda _e, d: assignment_today
            if str(d) == "2026-09-10"
            else assignment_yesterday,
        ), patch.object(
            checkin_guard, "_enforce_location"
        ) as mock_location, patch.object(
            checkin_guard, "_explanation", return_value=day_off or "not rostered"
        ), patch.object(
            checkin_guard, "frappe"
        ) as mock_frappe:
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

    def test_yesterdays_shift_still_lets_you_clock_out(self):
        """Branch shifts end after midnight; a late check-out must not be refused."""
        result = self._run(shift=None, assignment_yesterday={"shift_location": "Dokki"})
        self.assertFalse(result["blocked"])

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


if __name__ == "__main__":
    unittest.main()

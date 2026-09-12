"""Attendance reporting: status derivation, day grouping and the rate arithmetic.

Four things here are worth more than the rest:

* **The status a day gets.** Eight strings, and the Flutter client switches on
  them. Getting one wrong does not error -- it reports a person who was at work
  as absent, or the whole company as absent on the day the roster horizon runs
  out.
* **Which day a check-in belongs to.** Every branch shift crosses midnight, so
  a check-out at 00:50 carries tomorrow's wall-clock date. Grouping on the wall
  clock splits one shift across two cells and invents an unexplained arrival on
  a day nobody was rostered.
* **The grace boundary.** One minute either side of it is the difference
  between "present" and a lateness record against a real person, so both sides
  of the boundary are pinned, not just the middle.
* **The rate arithmetic.** A month with nothing rostered in it is a real month
  (a new joiner, an exempt employee), and a division there takes the whole
  screen down for everybody else on it.
"""

import unittest
from datetime import timedelta
from unittest.mock import patch

from frappe.utils import getdate

from jarz_pos.services import attendance as attendance_service
from jarz_pos.services import roster as roster_service
from jarz_pos.utils import settings_utils

TODAY = "2026-09-15"


def _cell(status, **kwargs):
    """A minimal cell for the totals arithmetic, without the grid machinery."""
    cell = {
        "status": status,
        "worked_hours": None,
        "late_minutes": None,
    }
    cell.update(kwargs)
    return cell


class TestStatusVocabulary(unittest.TestCase):
    """The eight strings are a contract the Flutter client switches on.

    A rename here does not fail anywhere -- it renders as a blank chip on a
    client nobody is going to rebuild, so the literals are pinned.
    """

    def test_the_enum_is_exactly_the_contract(self):
        self.assertEqual(
            list(attendance_service.STATUSES),
            [
                "present",
                "late",
                "late_unmatched",
                "absent",
                "pending",
                "off",
                "holiday",
                "not_rostered",
            ],
        )

    def test_a_showed_up_day_is_never_counted_as_a_no_show(self):
        for status in attendance_service.SHOWED_STATUSES:
            self.assertIn(status, attendance_service.JUDGED_STATUSES, status)
        self.assertNotIn("pending", attendance_service.JUDGED_STATUSES)


class TestGraceSetting(unittest.TestCase):
    """The grace period must tell "never written" apart from a deliberate 0.

    ``get_single_value`` casts an Int through ``cint()``, so a field nobody has
    touched reads back as 0 and every arrival one second past the hour would be
    graded late -- with the declared default of 15 unreachable forever. The
    reader is ``single_int``, and both ends of that distinction are pinned here.
    """

    def _grace(self, stored):
        """``stored`` is the raw tabSingles value; ``None`` = never written."""
        with patch.object(settings_utils, "raw_single_value", return_value=stored):
            return attendance_service.grace_minutes()

    def test_never_written_falls_back_to_fifteen(self):
        self.assertEqual(self._grace(None), 15)
        self.assertEqual(self._grace(""), 15)

    def test_a_deliberate_zero_is_honoured(self):
        """"No grace at all" is a policy somebody may choose."""
        self.assertEqual(self._grace("0"), 0)

    def test_an_operator_value_wins(self):
        self.assertEqual(self._grace("30"), 30)

    def test_a_negative_grace_is_clamped_not_thrown(self):
        # A typo in Desk must not 500 a read-only screen, and a negative grace
        # would grade an on-time arrival as late.
        self.assertEqual(self._grace("-5"), 0)

    def test_the_field_is_ours_not_the_hrms_one(self):
        self.assertEqual(attendance_service.GRACE_FIELD, "attendance_late_grace_minutes")
        self.assertEqual(attendance_service.DEFAULT_GRACE_MINUTES, 15)


class TestGraceBoundary(unittest.TestCase):
    """Exactly at the grace is present; one minute past it is late."""

    def _status(self, late_minutes, grace=15):
        return attendance_service.derive_status(
            group={"count": 1},
            rostered=True,
            day_off=False,
            is_holiday=False,
            exempt=False,
            late_minutes=late_minutes,
            grace=grace,
            is_past=True,
        )

    def test_exactly_on_the_grace_is_present(self):
        self.assertEqual(self._status(15), "present")

    def test_one_minute_past_the_grace_is_late(self):
        self.assertEqual(self._status(16), "late")

    def test_arriving_early_is_present(self):
        self.assertEqual(self._status(-20), "present")

    def test_a_zero_grace_makes_one_minute_late(self):
        self.assertEqual(self._status(0, grace=0), "present")
        self.assertEqual(self._status(1, grace=0), "late")


class TestStatusDerivation(unittest.TestCase):
    """One case per status in the enum, including the two that are not absences."""

    def _status(
        self,
        group=None,
        rostered=True,
        day_off=False,
        is_holiday=False,
        exempt=False,
        late_minutes=None,
        is_past=True,
    ):
        return attendance_service.derive_status(
            group=group,
            rostered=rostered,
            day_off=day_off,
            is_holiday=is_holiday,
            exempt=exempt,
            late_minutes=late_minutes,
            grace=15,
            is_past=is_past,
        )

    def test_present(self):
        self.assertEqual(self._status(group={"count": 2}, late_minutes=3), "present")

    def test_late(self):
        self.assertEqual(self._status(group={"count": 2}, late_minutes=45), "late")

    def test_late_unmatched_is_an_arrival_not_a_gap(self):
        """A punch HRMS matched to no shift: offshift=1, shift_start NULL.

        That is somebody who turned up outside their window, usually long after
        it closed. Reading it as "no data" would hide the one arrival a manager
        most wants to see.
        """
        self.assertEqual(self._status(group={"count": 1}, late_minutes=None), "late_unmatched")

    def test_absent_needs_a_real_assignment_and_a_past_date(self):
        self.assertEqual(self._status(rostered=True, is_past=True), "absent")

    def test_pending_when_the_day_has_not_finished_happening(self):
        self.assertEqual(self._status(rostered=True, is_past=False), "pending")

    def test_off(self):
        self.assertEqual(self._status(rostered=False, day_off=True), "off")

    def test_holiday(self):
        self.assertEqual(self._status(rostered=False, is_holiday=True), "holiday")

    def test_not_rostered(self):
        self.assertEqual(self._status(rostered=False), "not_rostered")

    def test_an_empty_group_is_not_evidence(self):
        self.assertEqual(self._status(group={"count": 0}, rostered=True), "absent")

    def test_evidence_outranks_the_rota(self):
        """Somebody who clocked in on their day off is graded on the clock.

        The screen must never report "off" for a person who was standing in the
        branch; the anomaly is the point.
        """
        self.assertEqual(
            self._status(group={"count": 1}, day_off=True, late_minutes=2), "present"
        )


class TestPastHorizonIsNotAbsent(unittest.TestCase):
    """The roster horizon ends 2026-11-19; the day after is nobody's fault.

    Shift Schedules generate a finite span. Reporting every date past it as
    ``absent`` would paint the entire company absent from one morning onwards,
    over a housekeeping job nobody was watching -- and the screen would look
    like a mass walkout rather than like an ungenerated rota.
    """

    def _status(self, rostered, is_past):
        return attendance_service.derive_status(
            group=None,
            rostered=rostered,
            day_off=False,
            is_holiday=False,
            exempt=False,
            late_minutes=None,
            grace=15,
            is_past=is_past,
        )

    def test_past_the_horizon_reads_not_rostered_even_in_the_past(self):
        self.assertEqual(self._status(rostered=False, is_past=True), "not_rostered")

    def test_absent_requires_an_assignment_covering_the_date(self):
        self.assertEqual(self._status(rostered=True, is_past=True), "absent")
        self.assertNotEqual(self._status(rostered=False, is_past=True), "absent")

    def test_a_not_rostered_day_never_reaches_the_denominator(self):
        totals = attendance_service.totals_for([_cell("not_rostered")] * 30)
        self.assertEqual(totals["rostered_days"], 0)
        self.assertEqual(totals["absent_days"], 0)


class TestExemptEmployee(unittest.TestCase):
    """``custom_roster_checkin_exempt`` means "no clock-in obligation".

    Such a person never checks in, so every past rostered day of theirs would
    otherwise report ``absent`` -- a month of invented absences for somebody
    nobody expects to see on a clock, dragging the branch attendance rate down
    with it.
    """

    def _status(self, group=None, is_past=True, day_off=False, is_holiday=False):
        return attendance_service.derive_status(
            group=group,
            rostered=True,
            day_off=day_off,
            is_holiday=is_holiday,
            exempt=True,
            late_minutes=2 if group else None,
            grace=15,
            is_past=is_past,
        )

    def test_no_checkin_is_never_absent(self):
        self.assertEqual(self._status(), "not_rostered")

    def test_no_checkin_is_not_pending_either(self):
        """Today and tomorrow read the same as yesterday: nothing is expected."""
        self.assertEqual(self._status(is_past=False), "not_rostered")

    def test_a_real_checkin_is_still_graded(self):
        self.assertEqual(self._status(group={"count": 2}), "present")

    def test_a_day_off_still_shows_as_off(self):
        """Off is a fact about the rota, not about the clock."""
        self.assertEqual(self._status(day_off=True), "off")
        self.assertEqual(self._status(is_holiday=True), "holiday")


class TestCheckinGrouping(unittest.TestCase):
    """A check-in belongs to its shift's day, not to the wall clock.

    Every branch shift here runs 16:00 -> 01:00, so the check-out that closes
    it is stamped the following calendar day. Grouping on the wall clock cuts
    one shift in half and reports its tail as an unexplained arrival on a day
    the person was not rostered at all.
    """

    CLOSING = [
        {
            "name": "CI-1",
            "employee": "HR-EMP-1",
            "time": "2026-09-10 16:05:00",
            "log_type": "IN",
            "shift": "Branch Closing",
            "shift_start": "2026-09-10 16:00:00",
            "offshift": 0,
        },
        {
            "name": "CI-2",
            "employee": "HR-EMP-1",
            "time": "2026-09-11 00:50:00",
            "log_type": "",
            "shift": "Branch Closing",
            "shift_start": "2026-09-10 16:00:00",
            "offshift": 0,
        },
    ]

    def _groups(self, rows):
        return attendance_service.group_checkins(rows)

    def test_the_midnight_checkout_stays_on_the_shifts_day(self):
        groups = self._groups(self.CLOSING)
        self.assertEqual(list(groups.keys()), [("HR-EMP-1", "2026-09-10")])

    def test_both_punches_land_in_one_group(self):
        group = self._groups(self.CLOSING)[("HR-EMP-1", "2026-09-10")]
        self.assertEqual(group["count"], 2)
        self.assertEqual(group["first_in"].hour, 16)

    def test_a_shiftless_punch_falls_back_to_the_wall_clock(self):
        """No shift_start is the ``late_unmatched`` case, not a missing day."""
        groups = self._groups(
            [
                {
                    "name": "CI-9",
                    "employee": "HR-EMP-1",
                    "time": "2026-09-12 23:10:00",
                    "shift": None,
                    "shift_start": None,
                    "offshift": 1,
                }
            ]
        )
        self.assertEqual(list(groups.keys()), [("HR-EMP-1", "2026-09-12")])
        self.assertIsNone(groups[("HR-EMP-1", "2026-09-12")]["shift_start"])

    def test_log_type_is_never_trusted_for_pairing(self):
        """The live Shift Types do not set determine_check_in_and_check_out.

        ``log_type`` is blank on most rows here, so first-and-last is the only
        pairing available; a row labelled OUT arriving first is still the
        arrival.
        """
        rows = [dict(r) for r in self.CLOSING]
        rows[0]["log_type"] = "OUT"
        rows[1]["log_type"] = "IN"
        group = self._groups(rows)[("HR-EMP-1", "2026-09-10")]
        self.assertEqual(group["first_in"].hour, 16)
        self.assertEqual(group["last"].hour, 0)

    def test_a_row_with_no_time_is_dropped_not_fatal(self):
        self.assertEqual(self._groups([{"employee": "HR-EMP-1", "time": None}]), {})


class TestLatenessAndWorkedHours(unittest.TestCase):
    """Lateness is signed; a lone punch is an arrival with no departure."""

    def _group(self, first, last=None, shift_start=None, count=None):
        return attendance_service.group_checkins(
            [
                {
                    "employee": "E",
                    "time": t,
                    "shift_start": shift_start,
                    "offshift": 0,
                }
                for t in ([first] if last is None else [first, last])
            ]
        )[("E", str(getdate(shift_start or first)))]

    def test_lateness_is_first_in_minus_shift_start(self):
        group = self._group("2026-09-10 16:22:00", shift_start="2026-09-10 16:00:00")
        self.assertEqual(attendance_service.late_minutes_for(group), 22)

    def test_arriving_early_is_negative_not_zero(self):
        group = self._group("2026-09-10 15:45:00", shift_start="2026-09-10 16:00:00")
        self.assertEqual(attendance_service.late_minutes_for(group), -15)

    def test_no_shift_start_means_there_is_nothing_to_be_late_against(self):
        group = self._group("2026-09-10 23:00:00")
        self.assertIsNone(attendance_service.late_minutes_for(group))

    def test_worked_hours_span_first_to_last_across_midnight(self):
        group = self._group(
            "2026-09-10 16:05:00", "2026-09-11 00:50:00", shift_start="2026-09-10 16:00:00"
        )
        self.assertEqual(attendance_service.worked_hours_for(group), 8.75)

    def test_a_single_punch_has_no_worked_hours(self):
        """0.0 would read as "clocked in and left immediately"."""
        group = self._group("2026-09-10 16:05:00", shift_start="2026-09-10 16:00:00")
        self.assertIsNone(attendance_service.worked_hours_for(group))


class TestTimeRenderingKeepsMidnight(unittest.TestCase):
    """A Time field of midnight is ``timedelta(0)``, which is FALSY.

    ``str(value or "")`` erases it, and the Friday courier shift really does end
    at 00:00 -- it came back from staging with a blank end time once already,
    which would render as "14:30 -> " on the attendance cell.
    """

    def test_midnight_renders_as_a_time(self):
        self.assertEqual(attendance_service._hhmm(timedelta(0)), "00:00")

    def test_a_real_time_is_truncated_to_hours_and_minutes(self):
        self.assertEqual(attendance_service._hhmm(timedelta(hours=16, minutes=30)), "16:30")
        self.assertEqual(attendance_service._hhmm("01:00:00"), "01:00")

    def test_only_none_means_no_time(self):
        self.assertIsNone(attendance_service._hhmm(None))

    def test_the_catalogue_carries_midnight_through(self):
        with patch.object(
            roster_service,
            "shift_catalog",
            return_value=[
                {"shift_type": "Courier Friday", "start_time": "14:30:00", "end_time": "0:00:00"}
            ],
        ):
            self.assertEqual(
                attendance_service.shift_time_map()["Courier Friday"], ("14:30", "00:00")
            )


class TestRateArithmetic(unittest.TestCase):
    """A month with nothing rostered in it is a real month, not a crash."""

    def test_zero_rostered_days_does_not_divide(self):
        totals = attendance_service.totals_for([_cell("off"), _cell("not_rostered")])
        self.assertEqual(totals["attendance_rate"], 0.0)
        self.assertEqual(totals["punctuality_rate"], 0.0)

    def test_punctuality_is_zero_when_nobody_showed(self):
        """1.0 would read as a perfect record for a month of pure absence."""
        totals = attendance_service.totals_for([_cell("absent")] * 4)
        self.assertEqual(totals["attendance_rate"], 0.0)
        self.assertEqual(totals["punctuality_rate"], 0.0)

    def test_attendance_counts_the_unmatched_arrival_as_having_shown_up(self):
        totals = attendance_service.totals_for(
            [_cell("present"), _cell("present"), _cell("late"), _cell("late_unmatched"), _cell("absent")]
        )
        self.assertEqual(totals["rostered_days"], 5)
        self.assertEqual(totals["attendance_rate"], 0.8)
        self.assertEqual(totals["punctuality_rate"], 0.5)

    def test_pending_days_never_dilute_the_rate(self):
        """Mid-month the rate must not fall a little further every day."""
        totals = attendance_service.totals_for([_cell("present")] + [_cell("pending")] * 15)
        self.assertEqual(totals["rostered_days"], 1)
        self.assertEqual(totals["attendance_rate"], 1.0)

    def test_total_late_minutes_sum_only_the_positive_ones(self):
        """A week of early starts must not cancel out an hour of lateness."""
        totals = attendance_service.totals_for(
            [
                _cell("late", late_minutes=60),
                _cell("present", late_minutes=-30),
                _cell("present", late_minutes=-45),
            ]
        )
        self.assertEqual(totals["late_minutes"], 60)

    def test_worked_hours_ignore_the_days_with_no_pair(self):
        totals = attendance_service.totals_for(
            [_cell("present", worked_hours=8.75), _cell("present", worked_hours=None)]
        )
        self.assertEqual(totals["worked_hours"], 8.75)


class TestDayTotals(unittest.TestCase):
    """The live board counts people who have not arrived YET as rostered."""

    def test_pending_is_inside_rostered_on_the_day_board(self):
        totals = attendance_service.day_totals_for(
            [_cell("present"), _cell("late"), _cell("absent"), _cell("pending")]
        )
        self.assertEqual(totals["rostered"], 4)
        self.assertEqual(totals["pending"], 1)

    def test_off_and_not_rostered_are_outside_it(self):
        totals = attendance_service.day_totals_for(
            [_cell("off"), _cell("holiday"), _cell("not_rostered")]
        )
        self.assertEqual(totals["rostered"], 0)
        self.assertEqual(totals["off"], 1)

    def test_an_unmatched_arrival_has_its_own_counter(self):
        """It used to have none, so a branch header did not add up to its rows.

        The row that went missing from the arithmetic was always the same one:
        the person who turned up outside their window, i.e. the row a manager
        most needs to ask about.
        """
        totals = attendance_service.day_totals_for([_cell("late_unmatched")])
        self.assertEqual(totals["late_unmatched"], 1)
        self.assertEqual(totals["rostered"], 1)
        self.assertEqual((totals["late"], totals["present"]), (0, 0))


class TestTotalsAreInternallyConsistent(unittest.TestCase):
    """A header nobody can reconcile with its own rows is a header nobody acts on.

    These two identities are the whole reason the ``late_unmatched`` and
    ``pending`` counters exist. They are asserted over a pile containing every
    status in the enum, so a status added later without a counter fails here
    rather than silently unbalancing a screen.
    """

    EVERY_STATUS = [_cell(status) for status in attendance_service.STATUSES]

    def _assert_month_identity(self, totals):
        self.assertEqual(
            totals["rostered_days"],
            totals["present_days"]
            + totals["late_days"]
            + totals["late_unmatched_days"]
            + totals["absent_days"],
        )

    def test_the_month_identity_holds_over_every_status(self):
        self._assert_month_identity(attendance_service.totals_for(self.EVERY_STATUS))

    def test_the_month_identity_holds_over_a_lopsided_pile(self):
        cells = (
            [_cell("present")] * 7
            + [_cell("late")] * 3
            + [_cell("late_unmatched")] * 2
            + [_cell("absent")]
            + [_cell("pending")] * 4
            + [_cell("off")] * 2
            + [_cell("holiday"), _cell("not_rostered")]
        )
        totals = attendance_service.totals_for(cells)
        self._assert_month_identity(totals)
        self.assertEqual(totals["rostered_days"], 13)
        self.assertEqual(totals["pending_days"], 4)

    def test_pending_is_reported_but_stays_out_of_rostered(self):
        totals = attendance_service.totals_for([_cell("pending")] * 5)
        self.assertEqual(totals["pending_days"], 5)
        self.assertEqual(totals["rostered_days"], 0)

    def test_the_day_identity_holds_over_every_status(self):
        totals = attendance_service.day_totals_for(self.EVERY_STATUS)
        self.assertEqual(
            totals["rostered"],
            totals["present"]
            + totals["late"]
            + totals["late_unmatched"]
            + totals["absent"]
            + totals["pending"],
        )

    def test_the_attendance_rate_is_verifiable_from_the_printed_counters(self):
        """The numerator must be reconstructible from the keys beside it."""
        totals = attendance_service.totals_for(
            [_cell("present"), _cell("late"), _cell("late_unmatched"), _cell("absent")]
        )
        showed = totals["present_days"] + totals["late_days"] + totals["late_unmatched_days"]
        self.assertEqual(totals["attendance_rate"], round(showed / totals["rostered_days"], 4))
        self.assertEqual(totals["punctuality_rate"], round(totals["present_days"] / showed, 4))


class TestDayOrdering(unittest.TestCase):
    """Worst first, and the unattributable bucket never sits at the top."""

    def _rows(self, statuses_and_lateness):
        rows = [
            {"status": s, "late_minutes": m, "employee_name": f"E{i}"}
            for i, (s, m) in enumerate(statuses_and_lateness)
        ]
        rows.sort(key=attendance_service._day_sort_key)
        return [r["status"] for r in rows]

    def test_the_action_list_comes_first(self):
        self.assertEqual(
            self._rows(
                [
                    ("present", 2),
                    ("off", None),
                    ("pending", None),
                    ("absent", None),
                    ("late_unmatched", None),
                    ("late", 20),
                ]
            ),
            ["late", "late_unmatched", "absent", "pending", "present", "off"],
        )

    def test_the_worst_lateness_leads(self):
        rows = [
            {"status": "late", "late_minutes": 12, "employee_name": "A"},
            {"status": "late", "late_minutes": 95, "employee_name": "B"},
            {"status": "late", "late_minutes": 40, "employee_name": "C"},
        ]
        rows.sort(key=attendance_service._day_sort_key)
        self.assertEqual([r["late_minutes"] for r in rows], [95, 40, 12])

    def test_the_null_branch_bucket_sorts_last(self):
        """``None`` means "no branch resolved" -- a data problem, not a place.

        A naive sort on ``or ""`` puts it FIRST, so the unattributable rows
        would head the screen every morning above the real branches.
        """
        buckets = [
            {"shift_location": "Nasr City"},
            {"shift_location": None},
            {"shift_location": "6th of October"},
            {"shift_location": "Dokki"},
        ]
        buckets.sort(key=attendance_service._branch_sort_key)
        self.assertEqual(
            [b["shift_location"] for b in buckets],
            ["6th of October", "Dokki", "Nasr City", None],
        )


class TestGeoOk(unittest.TestCase):
    """``False`` is an accusation; it is only ever returned after a measurement."""

    BRANCH = {"checkin_radius": 150, "latitude": 30.0444, "longitude": 31.2357}

    def test_inside_the_radius(self):
        self.assertTrue(attendance_service._geo_ok([(30.0444, 31.2357)], self.BRANCH))

    def test_outside_the_radius(self):
        self.assertFalse(attendance_service._geo_ok([(30.2000, 31.2357)], self.BRANCH))

    def test_no_coordinates_is_unknown_not_a_breach(self):
        self.assertIsNone(attendance_service._geo_ok([], self.BRANCH))

    def test_no_branch_is_unknown(self):
        self.assertIsNone(attendance_service._geo_ok([(30.0, 31.0)], None))

    def test_a_non_positive_radius_means_do_not_measure(self):
        """Mirrors the HRMS escape hatch the check-in gate already honours."""
        self.assertIsNone(
            attendance_service._geo_ok(
                [(30.0, 31.0)], {"checkin_radius": 0, "latitude": 30.0, "longitude": 31.0}
            )
        )


class TestTriStatesNeverCoalesce(unittest.TestCase):
    """``None`` is not ``False``, and ``None`` is not ``0.0``. The client relies on it.

    ``geo_ok: null`` renders "no location recorded"; ``geo_ok: false`` renders
    "outside the branch fence" -- an accusation about a named person. Coalescing
    the two would have the screen assert something the data never said, and the
    coalesce is a one-character change (``or False``) that nothing else catches.

    ``worked_hours: null`` renders "—"; ``0.0`` renders "0.0 h", which reads as
    "clocked in and left immediately". Each is pinned with ``assertIsNone`` plus
    an explicit ``is not`` against the value it must never become, because
    ``assertEqual(None, 0.0)`` would not fail on a coalesced ``0``.
    """

    def _cell(self, checkins, location_row=None):
        group = attendance_service.group_checkins(checkins) if checkins else {}
        return attendance_service.build_cell(
            date_str="2026-09-10",
            shift_type="Branch Opening",
            shift_location="Nasr City",
            scheduled_start="12:30",
            scheduled_end="21:30",
            group=next(iter(group.values()), None),
            day_off_row=None,
            is_holiday=False,
            is_cover=False,
            exempt=False,
            grace=15,
            today=getdate(TODAY),
            location_row=location_row,
        )

    ARRIVAL = {
        "name": "CI-1",
        "employee": "HR-EMP-1",
        "time": "2026-09-10 12:31:00",
        "shift": "Branch Opening",
        "shift_start": "2026-09-10 12:30:00",
        "offshift": 0,
        "latitude": 30.0444,
        "longitude": 31.2357,
    }
    BRANCH = {"checkin_radius": 150, "latitude": 30.0444, "longitude": 31.2357}

    def test_a_lone_checkin_has_null_worked_hours_not_zero(self):
        cell = self._cell([self.ARRIVAL])
        self.assertIsNone(cell["worked_hours"])
        self.assertIsNot(cell["worked_hours"], 0.0)

    def test_a_pair_of_checkins_does_report_hours(self):
        second = dict(self.ARRIVAL, name="CI-2", time="2026-09-10 21:31:00")
        self.assertEqual(self._cell([self.ARRIVAL, second])["worked_hours"], 9.0)

    def test_a_day_with_no_checkin_has_null_worked_hours(self):
        self.assertIsNone(self._cell([])["worked_hours"])

    def test_a_missing_coordinate_is_null_geo_not_false(self):
        naked = dict(self.ARRIVAL, latitude=None, longitude=None)
        cell = self._cell([naked], location_row=self.BRANCH)
        self.assertIsNone(cell["geo_ok"])
        self.assertIsNot(cell["geo_ok"], False)

    def test_an_unconfigured_radius_is_null_geo_not_false(self):
        cell = self._cell(
            [self.ARRIVAL],
            location_row={"checkin_radius": 0, "latitude": 30.0444, "longitude": 31.2357},
        )
        self.assertIsNone(cell["geo_ok"])
        self.assertIsNot(cell["geo_ok"], False)

    def test_a_real_breach_is_still_false(self):
        """The accusation must survive: null-everywhere would be just as wrong."""
        far = dict(self.ARRIVAL, latitude=30.2000)
        self.assertIs(self._cell([far], location_row=self.BRANCH)["geo_ok"], False)
        self.assertIs(self._cell([self.ARRIVAL], location_row=self.BRANCH)["geo_ok"], True)


class AttendanceGridCase(unittest.TestCase):
    """Shared driver for the tests that need a real grid built."""

    SCOPE = {"configured": True, "unrestricted": True, "locations": None}

    def _grid(
        self,
        assignments=(),
        day_offs=(),
        schedules=(),
        employees=(),
        checkins=(),
        start="2026-09-10",
        end="2026-09-10",
        allowed=None,
        holidays=None,
        grace=15,
        today=TODAY,
        employee=None,
        shift_location=None,
        shift_times=None,
        locations=None,
    ):
        captured = {}

        def fake_get_all(doctype, **kwargs):
            captured.setdefault(doctype, []).append(kwargs)
            table = {
                "Shift Assignment": assignments,
                roster_service.DAY_OFF_DOCTYPE: day_offs,
                "Shift Schedule Assignment": schedules,
                "Employee": employees,
                attendance_service.CHECKIN_DOCTYPE: checkins,
            }.get(doctype, ())
            return [dict(row) for row in table]

        with patch.object(attendance_service, "frappe") as mock_frappe, patch.object(
            attendance_service, "allowed_shift_locations", return_value=allowed
        ), patch.object(
            attendance_service, "scope_payload", return_value=dict(self.SCOPE)
        ), patch.object(
            attendance_service, "grace_minutes", return_value=grace
        ), patch.object(
            attendance_service, "_holidays", return_value=dict(holidays or {})
        ), patch.object(
            attendance_service, "_is_courier", return_value=False
        ), patch.object(
            attendance_service, "_employee_exempt_field_exists", return_value=True
        ), patch.object(
            attendance_service, "shift_time_map", return_value=dict(shift_times or {})
        ), patch.object(
            attendance_service, "shift_location_map", return_value=dict(locations or {})
        ):
            mock_frappe.get_all.side_effect = fake_get_all
            grid = attendance_service.build_grid(
                start,
                end,
                shift_location=shift_location,
                employee=employee,
                today=today,
            )
        grid["captured"] = captured
        return grid


ALI = {
    "name": "HR-EMP-1",
    "employee_name": "Ali",
    "designation": None,
    "department": "Operations",
    "status": "Active",
    "custom_roster_checkin_exempt": 0,
}
SARA = {
    "name": "HR-EMP-2",
    "employee_name": "Sara",
    "designation": None,
    "department": "Operations",
    "status": "Active",
    "custom_roster_checkin_exempt": 0,
}


def _assignment(employee, shift_type, location, start, end):
    return {
        "name": f"SA-{employee}-{start}",
        "employee": employee,
        "shift_type": shift_type,
        "shift_location": location,
        "start_date": start,
        "end_date": end,
    }


class TestGridBuildsCells(AttendanceGridCase):
    """One pass over roster plus check-ins must produce the contract cell."""

    def test_a_punctual_day(self):
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10")
            ],
            employees=[ALI],
            checkins=[
                {
                    "name": "CI-1",
                    "employee": "HR-EMP-1",
                    "time": "2026-09-10 12:33:00",
                    "log_type": "IN",
                    "shift": "Branch Opening",
                    "shift_start": "2026-09-10 12:30:00",
                    "offshift": 0,
                }
            ],
            shift_times={"Branch Opening": ("12:30", "21:30")},
        )
        cell = grid["cells"]["HR-EMP-1"]["2026-09-10"]
        self.assertEqual(cell["status"], "present")
        self.assertEqual(
            (cell["late_minutes"], cell["checkin_count"], cell["scheduled_end"]),
            (3, 1, "21:30"),
        )

    def test_a_rostered_past_day_with_no_punch_is_absent(self):
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10")
            ],
            employees=[ALI],
        )
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-10"]["status"], "absent")

    def test_a_future_rostered_day_is_pending(self):
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-20", "2026-09-20")
            ],
            employees=[ALI],
            start="2026-09-20",
            end="2026-09-20",
        )
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-20"]["status"], "pending")

    def test_a_day_off_carries_its_reason_and_its_cover(self):
        grid = self._grid(
            assignments=[
                _assignment(
                    "HR-EMP-2", "Branch Cover Full Day", "Nasr City", "2026-09-10", "2026-09-10"
                )
            ],
            day_offs=[
                {
                    "name": "OFF-1",
                    "employee": "HR-EMP-1",
                    "off_date": "2026-09-10",
                    "off_type": "Weekly Off",
                    "shift_location": "Nasr City",
                    "covered_by": "HR-EMP-2",
                    "covered_by_name": "Sara",
                }
            ],
            employees=[ALI, SARA],
        )
        off_cell = grid["cells"]["HR-EMP-1"]["2026-09-10"]
        cover_cell = grid["cells"]["HR-EMP-2"]["2026-09-10"]
        self.assertEqual(
            (off_cell["status"], off_cell["day_off"]["covered_by_name"], off_cell["is_cover"]),
            ("off", "Sara", False),
        )
        self.assertTrue(cover_cell["is_cover"])

    def test_a_holiday_is_not_an_absence(self):
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Factory", "Factory", "2026-09-10", "2026-09-10")
            ],
            employees=[ALI],
            holidays={"HR-EMP-1": {"2026-09-10"}},
        )
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-10"]["status"], "holiday")

    def test_an_offshift_punch_reads_late_unmatched(self):
        """shift=None, offshift=1, shift_start NULL -- an arrival past the window."""
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10")
            ],
            employees=[ALI],
            checkins=[
                {
                    "name": "CI-9",
                    "employee": "HR-EMP-1",
                    "time": "2026-09-10 23:40:00",
                    "log_type": "",
                    "shift": None,
                    "shift_start": None,
                    "offshift": 1,
                }
            ],
        )
        cell = grid["cells"]["HR-EMP-1"]["2026-09-10"]
        self.assertEqual(cell["status"], "late_unmatched")
        self.assertTrue(cell["offshift"])
        self.assertIsNone(cell["late_minutes"])

    def test_an_exempt_employee_is_never_reported_absent(self):
        exempt = dict(ALI, custom_roster_checkin_exempt=1)
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10")
            ],
            employees=[exempt],
        )
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-10"]["status"], "not_rostered")


class TestGridMidnightShift(AttendanceGridCase):
    """A 16:00 -> 01:00 shift owns the punches on both sides of midnight."""

    CLOSING = [
        {
            "name": "CI-1",
            "employee": "HR-EMP-1",
            "time": "2026-09-10 16:04:00",
            "log_type": "IN",
            "shift": "Branch Closing",
            "shift_start": "2026-09-10 16:00:00",
            "offshift": 0,
        },
        {
            "name": "CI-2",
            "employee": "HR-EMP-1",
            "time": "2026-09-11 00:49:00",
            "log_type": "",
            "shift": "Branch Closing",
            "shift_start": "2026-09-10 16:00:00",
            "offshift": 0,
        },
    ]

    def _cells(self):
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Branch Closing", "Dokki", "2026-09-10", "2026-09-10")
            ],
            employees=[ALI],
            checkins=self.CLOSING,
            start="2026-09-10",
            end="2026-09-11",
            shift_times={"Branch Closing": ("16:00", "01:00")},
        )
        return grid["cells"]["HR-EMP-1"]

    def test_the_group_attaches_to_the_shifts_date(self):
        cells = self._cells()
        self.assertEqual(cells["2026-09-10"]["status"], "present")
        self.assertEqual(cells["2026-09-10"]["checkin_count"], 2)

    def test_the_next_day_is_not_an_unexplained_arrival(self):
        """Grouping on the wall clock would put the 00:49 punch here."""
        cells = self._cells()
        self.assertEqual(cells["2026-09-11"]["status"], "not_rostered")
        self.assertEqual(cells["2026-09-11"]["checkin_count"], 0)

    def test_the_hours_cross_midnight_without_going_negative(self):
        self.assertEqual(self._cells()["2026-09-10"]["worked_hours"], 8.75)


class TestGridBranchScoping(AttendanceGridCase):
    """Unrestricted, a restricted set, and the empty set are three outcomes."""

    ASSIGNMENTS = [
        _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10"),
        _assignment("HR-EMP-2", "Branch Opening", "Dokki", "2026-09-10", "2026-09-10"),
    ]

    def _visible(self, allowed, shift_location=None):
        grid = self._grid(
            assignments=self.ASSIGNMENTS,
            employees=[ALI, SARA],
            allowed=allowed,
            shift_location=shift_location,
        )
        return [row["employee"] for row in grid["employees"]]

    def test_unrestricted_sees_every_branch(self):
        self.assertEqual(self._visible(None), ["HR-EMP-1", "HR-EMP-2"])

    def test_a_restricted_manager_sees_only_their_own(self):
        self.assertEqual(self._visible({"Nasr City"}), ["HR-EMP-1"])

    def test_an_empty_scope_sees_nobody(self):
        """Scope resolved to "no branches" must not silently mean "all"."""
        self.assertEqual(self._visible(set()), [])

    def test_the_client_filter_narrows_within_the_scope(self):
        self.assertEqual(self._visible(None, shift_location="Dokki"), ["HR-EMP-2"])


class TestGridReadsHistory(AttendanceGridCase):
    """HRMS flips every past assignment Inactive; the read must still see it.

    ``mark_expired_shift_assignments_as_inactive`` runs nightly, so an
    Active-only read makes last month's attendance vanish entirely -- the month
    somebody is actually querying comes back as a grid of ``not_rostered``.
    """

    def _filters(self):
        grid = self._grid(
            assignments=[
                dict(
                    _assignment(
                        "HR-EMP-1", "Branch Opening", "Nasr City", "2026-06-01", "2026-06-30"
                    ),
                    status="Inactive",
                )
            ],
            employees=[ALI],
            start="2026-06-10",
            end="2026-06-10",
        )
        return grid, grid["captured"]["Shift Assignment"][0]["filters"]

    def test_the_read_filter_admits_inactive(self):
        _, filters = self._filters()
        self.assertEqual(
            filters["status"], ("in", roster_service.READABLE_ASSIGNMENT_STATUSES)
        )

    def test_cancelled_assignments_stay_excluded(self):
        _, filters = self._filters()
        self.assertEqual(filters["docstatus"], 1)

    def test_a_past_month_still_reports_its_days(self):
        grid, _ = self._filters()
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-06-10"]["status"], "absent")


class TestMonthPayload(unittest.TestCase):
    """The calendar envelope, and totals that agree with their own rows."""

    def _month(self, cells_by_employee, employees):
        grid = {
            "employees": employees,
            "cells": cells_by_employee,
            "groups": {},
            "scope": {"configured": True, "unrestricted": True, "locations": None},
            "grace_minutes": 15,
            "start": getdate("2026-09-01"),
            "end": getdate("2026-09-30"),
        }
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service, "build_grid", return_value=grid
        ):
            return attendance_service.get_month(month="2026-09")

    EMPLOYEES = [
        {
            "employee": "HR-EMP-2",
            "employee_name": "Sara",
            "designation": None,
            "department": None,
            "shift_locations": ["Dokki"],
            "is_courier": False,
            "exempt": False,
        },
        {
            "employee": "HR-EMP-1",
            "employee_name": "Ali",
            "designation": None,
            "department": None,
            "shift_locations": ["Nasr City"],
            "is_courier": False,
            "exempt": False,
        },
    ]
    CELLS = {
        "HR-EMP-1": {
            "2026-09-01": _cell("present", worked_hours=9.0),
            "2026-09-02": _cell("late", late_minutes=30, worked_hours=8.0),
            "2026-09-03": _cell("late_unmatched"),
            "2026-09-04": _cell("pending"),
        },
        "HR-EMP-2": {
            "2026-09-01": _cell("absent"),
            "2026-09-02": _cell("off"),
            "2026-09-03": _cell("pending"),
            "2026-09-04": _cell("pending"),
        },
    }

    def test_the_envelope_names_the_month(self):
        data = self._month(self.CELLS, self.EMPLOYEES)
        self.assertEqual(
            (data["month"], data["month_start"], data["month_end"]),
            ("2026-09", "2026-09-01", "2026-09-30"),
        )

    def test_rows_are_ordered_by_name(self):
        data = self._month(self.CELLS, self.EMPLOYEES)
        self.assertEqual([r["employee_name"] for r in data["employees"]], ["Ali", "Sara"])

    def test_the_grand_total_is_the_sum_of_the_rows(self):
        data = self._month(self.CELLS, self.EMPLOYEES)
        self.assertEqual(data["totals"]["employees"], 2)
        self.assertEqual(data["totals"]["rostered_days"], 4)
        self.assertEqual(data["totals"]["worked_hours"], 17.0)
        self.assertEqual(data["totals"]["late_minutes"], 30)

    def test_each_row_carries_its_own_totals(self):
        data = self._month(self.CELLS, self.EMPLOYEES)
        ali = next(r for r in data["employees"] if r["employee"] == "HR-EMP-1")
        self.assertEqual(ali["totals"]["attendance_rate"], 1.0)
        self.assertEqual(ali["totals"]["punctuality_rate"], round(1 / 3, 4))

    def test_the_new_counters_are_on_both_levels(self):
        """A row total and the grand total must expose the same keys.

        The Day/Month tabs read them interchangeably, and a key present on one
        level only is a null on the other.
        """
        data = self._month(self.CELLS, self.EMPLOYEES)
        for key in ("late_unmatched_days", "pending_days"):
            self.assertIn(key, data["totals"], key)
            for row in data["employees"]:
                self.assertIn(key, row["totals"], f"{row['employee']}.{key}")

    def test_every_totals_block_adds_up(self):
        data = self._month(self.CELLS, self.EMPLOYEES)
        for totals in [data["totals"]] + [row["totals"] for row in data["employees"]]:
            self.assertEqual(
                totals["rostered_days"],
                totals["present_days"]
                + totals["late_days"]
                + totals["late_unmatched_days"]
                + totals["absent_days"],
            )

    def test_the_rows_pending_days_sum_to_the_grand_total(self):
        data = self._month(self.CELLS, self.EMPLOYEES)
        self.assertEqual(data["totals"]["pending_days"], 3)
        self.assertEqual(
            sum(row["totals"]["pending_days"] for row in data["employees"]),
            data["totals"]["pending_days"],
        )


class TestDayPayload(unittest.TestCase):
    """The day board: branch buckets, the null bucket last, worst rows first."""

    def _day(self):
        cells = {
            "HR-EMP-1": {
                "2026-09-10": _cell(
                    "late", date="2026-09-10", late_minutes=42, shift_location="Nasr City"
                )
            },
            "HR-EMP-2": {
                "2026-09-10": _cell(
                    "present", date="2026-09-10", late_minutes=1, shift_location="Nasr City"
                )
            },
            "HR-EMP-3": {"2026-09-10": _cell("absent", date="2026-09-10", shift_location=None)},
            "HR-EMP-4": {
                "2026-09-10": _cell(
                    "late_unmatched", date="2026-09-10", shift_location="Dokki"
                )
            },
            "HR-EMP-5": {"2026-09-10": _cell("pending", date="2026-09-10", shift_location="Dokki")},
        }
        employees = [
            {
                "employee": name,
                "employee_name": name,
                "designation": None,
                "department": None,
                "shift_locations": [],
                "is_courier": False,
                "exempt": False,
            }
            for name in cells
        ]
        grid = {
            "employees": employees,
            "cells": cells,
            "groups": {},
            "scope": {"configured": True, "unrestricted": True, "locations": None},
            "grace_minutes": 15,
            "start": getdate("2026-09-10"),
            "end": getdate("2026-09-10"),
        }
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service, "build_grid", return_value=grid
        ):
            return attendance_service.get_day(date="2026-09-10")

    def test_the_null_bucket_is_always_last(self):
        data = self._day()
        self.assertEqual(
            [b["shift_location"] for b in data["branches"]], ["Dokki", "Nasr City", None]
        )

    def test_rows_inside_a_branch_lead_with_the_late_ones(self):
        data = self._day()
        nasr = next(b for b in data["branches"] if b["shift_location"] == "Nasr City")
        self.assertEqual([r["status"] for r in nasr["rows"]], ["late", "present"])

    def test_branch_totals_and_the_grand_total_agree(self):
        data = self._day()
        self.assertEqual(data["totals"]["rostered"], 5)
        self.assertEqual(
            sum(b["totals"]["rostered"] for b in data["branches"]), data["totals"]["rostered"]
        )

    def test_every_branch_header_adds_up_to_its_own_rows(self):
        """The identity the ``late_unmatched`` counter exists to make checkable."""
        data = self._day()
        for bucket in data["branches"] + [{"totals": data["totals"], "shift_location": "ALL"}]:
            totals = bucket["totals"]
            self.assertEqual(
                totals["rostered"],
                totals["present"]
                + totals["late"]
                + totals["late_unmatched"]
                + totals["absent"]
                + totals["pending"],
                bucket["shift_location"],
            )

    def test_a_row_does_not_repeat_its_branch(self):
        """The bucket carries the location; repeating it invites the two to drift."""
        data = self._day()
        self.assertNotIn("shift_location", data["branches"][0]["rows"][0])

    def test_every_row_carries_its_own_date(self):
        """The branch day crosses midnight, so a row's date is the SHIFT's date.

        A client stamping the envelope's date onto every row mislabels exactly
        the night-shift rows -- the ones somebody opens the detail sheet for.
        """
        data = self._day()
        for bucket in data["branches"]:
            for row in bucket["rows"]:
                self.assertEqual(row["date"], "2026-09-10", row["employee"])

    def test_the_row_date_is_copied_from_the_cell_not_from_the_envelope(self):
        """Pins the mechanism, with a cell deliberately disagreeing with the header.

        The server must read a row's date off the cell it came from. If it ever
        starts echoing the envelope instead, the two are indistinguishable on
        every ordinary day and wrong on exactly the night-shift rows.
        """
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service,
            "build_grid",
            return_value={
                "employees": [
                    {
                        "employee": "HR-EMP-9",
                        "employee_name": "Night",
                        "designation": None,
                        "department": None,
                        "shift_locations": [],
                        "is_courier": False,
                        "exempt": False,
                    }
                ],
                "cells": {
                    "HR-EMP-9": {
                        "2026-09-10": _cell(
                            "present", date="2026-09-09", shift_location="Dokki"
                        )
                    }
                },
                "groups": {},
                "scope": {"configured": True, "unrestricted": True, "locations": None},
                "grace_minutes": 15,
                "start": getdate("2026-09-10"),
                "end": getdate("2026-09-10"),
            },
        ):
            data = attendance_service.get_day(date="2026-09-10")
        self.assertEqual(data["date"], "2026-09-10")
        self.assertEqual(data["branches"][0]["rows"][0]["date"], "2026-09-09")


class TestSummaryGroupings(unittest.TestCase):
    """Branch, employee and day must roll the same cells to the same totals."""

    CELLS = {
        "HR-EMP-1": {
            "2026-09-01": _cell("present", shift_location="Nasr City", worked_hours=9.0),
            "2026-09-02": _cell("late", shift_location="Nasr City", late_minutes=20, worked_hours=8.0),
            "2026-09-03": _cell("late_unmatched", shift_location="Nasr City"),
        },
        "HR-EMP-2": {
            "2026-09-01": _cell("absent", shift_location="Dokki"),
            "2026-09-02": _cell("present", shift_location="Dokki", worked_hours=9.0),
            "2026-09-03": _cell("pending", shift_location="Dokki"),
        },
    }
    EMPLOYEES = [
        {
            "employee": "HR-EMP-1",
            "employee_name": "Ali",
            "designation": None,
            "department": None,
            "shift_locations": ["Nasr City"],
            "is_courier": False,
            "exempt": False,
        },
        {
            "employee": "HR-EMP-2",
            "employee_name": "Sara",
            "designation": None,
            "department": None,
            "shift_locations": ["Dokki"],
            "is_courier": False,
            "exempt": False,
        },
    ]

    def _summary(self, group_by):
        grid = {
            "employees": self.EMPLOYEES,
            "cells": self.CELLS,
            "groups": {},
            "scope": {"configured": True, "unrestricted": True, "locations": None},
            "grace_minutes": 15,
            "start": getdate("2026-09-01"),
            "end": getdate("2026-09-02"),
        }
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service, "build_grid", return_value=grid
        ):
            return attendance_service.get_summary(
                from_date="2026-09-01", to_date="2026-09-02", group_by=group_by
            )

    def test_every_grouping_reports_the_same_grand_total(self):
        totals = [self._summary(g)["totals"] for g in ("branch", "employee", "day")]
        self.assertEqual({t["rostered_days"] for t in totals}, {5})
        self.assertEqual({t["worked_hours"] for t in totals}, {26.0})
        self.assertEqual({t["employees"] for t in totals}, {2})
        self.assertEqual({t["late_unmatched_days"] for t in totals}, {1})
        self.assertEqual({t["pending_days"] for t in totals}, {1})

    def test_rows_sum_to_the_total_in_every_grouping(self):
        for group_by in ("branch", "employee", "day"):
            data = self._summary(group_by)
            self.assertEqual(
                sum(r["rostered_days"] for r in data["rows"]),
                data["totals"]["rostered_days"],
                group_by,
            )

    def test_branch_rows_carry_the_branch_and_no_employee(self):
        rows = self._summary("branch")["rows"]
        self.assertEqual([r["key"] for r in rows], ["Dokki", "Nasr City"])
        self.assertIsNone(rows[0]["employee"])

    def test_employee_rows_carry_the_employee(self):
        rows = self._summary("employee")["rows"]
        self.assertEqual([r["employee"] for r in rows], ["HR-EMP-1", "HR-EMP-2"])
        self.assertEqual(rows[0]["employees"], 1)

    def test_day_rows_are_in_date_order(self):
        rows = self._summary("day")["rows"]
        self.assertEqual(
            [r["key"] for r in rows], ["2026-09-01", "2026-09-02", "2026-09-03"]
        )
        self.assertEqual(rows[0]["employees"], 2)

    def test_a_day_key_is_a_bare_iso_date(self):
        """This is an Arabic-first UI, so the client formats dates, not the server.

        A server-rendered date follows the SERVER's locale, and a formatted key
        cannot be parsed back into a date to drill into.
        """
        for row in self._summary("day")["rows"]:
            self.assertRegex(row["key"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertEqual(getdate(row["key"]).isoformat(), row["key"])
            self.assertEqual(row["label"], row["key"])

    def test_only_the_branch_grouping_translates_its_label(self):
        """"No branch" is a display string; an employee id and a date are not."""
        employee_rows = self._summary("employee")["rows"]
        self.assertEqual([r["label"] for r in employee_rows], ["Ali", "Sara"])
        self.assertEqual(self._summary("branch")["rows"][0]["label"], "Dokki")

    def test_average_lateness_does_not_divide_by_zero(self):
        rows = self._summary("branch")["rows"]
        dokki = next(r for r in rows if r["key"] == "Dokki")
        nasr = next(r for r in rows if r["key"] == "Nasr City")
        self.assertEqual(dokki["avg_late_minutes"], 0.0)
        self.assertEqual(nasr["avg_late_minutes"], 20.0)

    def test_every_row_and_total_adds_up(self):
        for group_by in ("branch", "employee", "day"):
            data = self._summary(group_by)
            for totals in data["rows"] + [data["totals"]]:
                self.assertEqual(
                    totals["rostered_days"],
                    totals["present_days"]
                    + totals["late_days"]
                    + totals["late_unmatched_days"]
                    + totals["absent_days"],
                    f"{group_by}:{totals.get('key')}",
                )

    def test_the_new_counters_are_on_rows_and_totals(self):
        for group_by in ("branch", "employee", "day"):
            data = self._summary(group_by)
            for key in ("late_unmatched_days", "pending_days"):
                self.assertIn(key, data["totals"], f"{group_by}:{key}")
                for row in data["rows"]:
                    self.assertIn(key, row, f"{group_by}:{row['key']}:{key}")

    def test_an_unknown_grouping_falls_back_to_branch(self):
        self.assertEqual(self._summary("department")["group_by"], "branch")


class TestEmployeePayload(unittest.TestCase):
    """The per-person tab, and the scope block it used to be missing.

    Without ``scope`` this tab could not tell "your branch scope resolved to no
    branches at all" apart from "this person has no days in this range". Both
    render as an empty screen, and they need opposite fixes -- one is a POS
    Profile mapping somebody never filled in, the other is a different date.
    """

    SCOPE = {"configured": True, "unrestricted": False, "locations": ["Nasr City"]}

    def _employee(self, hrms=True):
        grid = {
            "employees": [
                {
                    "employee": "HR-EMP-1",
                    "employee_name": "Ali",
                    "designation": None,
                    "department": "Operations",
                    "shift_locations": ["Nasr City"],
                    "is_courier": False,
                    "exempt": False,
                }
            ],
            "cells": {
                "HR-EMP-1": {
                    "2026-09-02": _cell(
                        "late", date="2026-09-02", shift_location="Nasr City", late_minutes=30
                    ),
                    "2026-09-01": _cell(
                        "present", date="2026-09-01", shift_location="Nasr City", worked_hours=9.0
                    ),
                }
            },
            "groups": {},
            "scope": dict(self.SCOPE),
            "grace_minutes": 15,
            "start": getdate("2026-09-01"),
            "end": getdate("2026-09-02"),
        }
        with patch.object(
            attendance_service, "hrms_available", return_value=hrms
        ), patch.object(
            attendance_service, "build_grid", return_value=grid
        ), patch.object(
            attendance_service, "scope_payload", return_value=dict(self.SCOPE)
        ), patch.object(
            attendance_service, "grace_minutes", return_value=15
        ):
            return attendance_service.get_employee(
                "HR-EMP-1", from_date="2026-09-01", to_date="2026-09-02"
            )

    def test_the_scope_block_is_present(self):
        self.assertEqual(self._employee()["scope"], self.SCOPE)

    def test_the_scope_block_survives_the_no_hrms_degrade(self):
        """The empty screen still has to explain itself."""
        data = self._employee(hrms=False)
        self.assertIn("scope", data)
        self.assertIn("unrestricted", data["scope"])

    def test_days_are_ascending_by_date(self):
        self.assertEqual(
            [d["date"] for d in self._employee()["days"]], ["2026-09-01", "2026-09-02"]
        )

    def test_the_totals_carry_the_new_counters(self):
        totals = self._employee()["totals"]
        self.assertIn("late_unmatched_days", totals)
        self.assertIn("pending_days", totals)
        self.assertEqual(totals["rostered_days"], 2)


class TestRangeDefaults(unittest.TestCase):
    """A half-given range must not silently become a one-day report."""

    def test_only_a_from_date_runs_to_the_end_of_its_month(self):
        self.assertEqual(
            attendance_service._range_bounds("2026-09-10", None),
            (getdate("2026-09-10"), getdate("2026-09-30")),
        )

    def test_only_a_to_date_runs_from_the_start_of_its_month(self):
        self.assertEqual(
            attendance_service._range_bounds(None, "2026-09-10"),
            (getdate("2026-09-01"), getdate("2026-09-10")),
        )

    def test_a_reversed_pair_is_swapped_not_refused(self):
        self.assertEqual(
            attendance_service._range_bounds("2026-09-30", "2026-09-01"),
            (getdate("2026-09-01"), getdate("2026-09-30")),
        )


class TestAccessGate(unittest.TestCase):
    """Attendance is the roster's tier: if you set the rota you read the clock."""

    def _has_access(self, roles):
        with patch.object(roster_service, "frappe") as mock_frappe:
            mock_frappe.get_roles.return_value = roles
            return attendance_service.has_attendance_access()

    def test_the_line_manager_tier_is_admitted(self):
        self.assertTrue(self._has_access(["JARZ line manager"]))
        self.assertTrue(self._has_access(["jarz line manager"]))

    def test_the_manager_tier_is_admitted(self):
        self.assertTrue(self._has_access(["JARZ Manager"]))
        self.assertTrue(self._has_access(["System Manager"]))

    def test_rank_and_file_is_refused(self):
        for role in ("POS User", "Employee", "Accounts User"):
            self.assertFalse(self._has_access([role]), role)

    def test_no_roles_is_refused(self):
        self.assertFalse(self._has_access([]))


class TestEmployeeScopeCheck(unittest.TestCase):
    """``get_employee`` names a person the CLIENT chose, so it is re-checked."""

    def _check(self, allowed, assignment_locations, schedule_locations=()):
        thrown = {}

        def fake_get_all(doctype, **kwargs):
            if doctype == "Shift Assignment":
                thrown["assignment_filters"] = kwargs.get("filters")
                return [{"shift_location": loc} for loc in assignment_locations]
            return [{"shift_location": loc} for loc in schedule_locations]

        def fake_throw(msg, exc=None):
            raise PermissionError(str(msg))

        with patch.object(attendance_service, "frappe") as mock_frappe, patch.object(
            attendance_service, "allowed_shift_locations", return_value=allowed
        ):
            mock_frappe.get_all.side_effect = fake_get_all
            mock_frappe.throw.side_effect = fake_throw
            mock_frappe.db.get_value.return_value = "Ali"
            try:
                attendance_service.ensure_employee_in_scope("HR-EMP-1")
                return {"refused": False, "filters": thrown.get("assignment_filters")}
            except PermissionError:
                return {"refused": True, "filters": thrown.get("assignment_filters")}

    def test_an_unrestricted_caller_is_never_refused(self):
        self.assertFalse(self._check(None, [])["refused"])

    def test_somebody_at_your_branch_is_allowed(self):
        self.assertFalse(self._check({"Nasr City"}, ["Nasr City"])["refused"])

    def test_somebody_at_another_branch_is_refused(self):
        self.assertTrue(self._check({"Nasr City"}, ["Dokki"])["refused"])

    def test_the_schedule_location_counts_too(self):
        self.assertFalse(
            self._check({"Factory"}, [], schedule_locations=["Factory"])["refused"]
        )

    def test_history_does_not_lock_a_manager_out_of_their_own_staff(self):
        """HRMS has flipped every past assignment Inactive by now.

        An Active-only scope check would answer "that person is not at your
        branch" for somebody who has worked there all year.
        """
        filters = self._check({"Nasr City"}, ["Nasr City"])["filters"]
        self.assertEqual(
            filters["status"], ("in", roster_service.READABLE_ASSIGNMENT_STATUSES)
        )


class TestHrmsAbsentDegrades(unittest.TestCase):
    """No HRMS is an explained empty answer, never a stack trace.

    A bench without HRMS still has to migrate and still has to serve the POS,
    and a 500 on this screen would be read as "the server is down".
    """

    def _call(self, fn, **kwargs):
        with patch.object(attendance_service, "hrms_available", return_value=False), patch.object(
            attendance_service, "scope_payload", return_value={
                "configured": False, "unrestricted": True, "locations": None
            }
        ), patch.object(attendance_service, "grace_minutes", return_value=15):
            return fn(**kwargs)

    def test_every_read_answers_with_a_notice(self):
        for fn, kwargs in (
            (attendance_service.get_month, {"month": "2026-09"}),
            (attendance_service.get_day, {"date": "2026-09-10"}),
            (attendance_service.get_employee, {"employee": "HR-EMP-1"}),
            (attendance_service.get_summary, {}),
        ):
            data = self._call(fn, **kwargs)
            self.assertFalse(data["hrms_available"], fn.__name__)
            self.assertTrue(data["notice"], fn.__name__)

    def test_the_containers_are_present_and_empty(self):
        month = self._call(attendance_service.get_month, month="2026-09")
        day = self._call(attendance_service.get_day, date="2026-09-10")
        self.assertEqual(month["employees"], [])
        self.assertEqual(day["branches"], [])
        self.assertEqual(day["totals"]["rostered"], 0)

    def test_the_summary_still_carries_its_numeric_keys(self):
        totals = self._call(attendance_service.get_summary)["totals"]
        for key in (
            "rostered_days",
            "late_unmatched_days",
            "pending_days",
            "attendance_rate",
            "avg_late_minutes",
            "employees",
        ):
            self.assertIn(key, totals)

    def test_every_read_still_carries_its_scope(self):
        """An empty screen with no scope block cannot explain why it is empty."""
        for fn, kwargs in (
            (attendance_service.get_month, {"month": "2026-09"}),
            (attendance_service.get_day, {"date": "2026-09-10"}),
            (attendance_service.get_employee, {"employee": "HR-EMP-1"}),
            (attendance_service.get_summary, {}),
        ):
            self.assertIn("scope", self._call(fn, **kwargs), fn.__name__)



# ---------------------------------------------------------------------------
# Release blockers (pre-production review, 2026-09-12)
# ---------------------------------------------------------------------------


class _Refused(Exception):
    """Stands in for frappe.throw so a refusal is observable without a site."""


class TestRangeIsCapped(unittest.TestCase):
    """An unbounded range was one request away from taking production down.

    ``build_grid`` materialises employees x days cells with no ceiling of its
    own. A slipped year digit -- 1026-01-01 to 2026-12-31 -- across ~40 staff is
    ~14.6M dicts on a 4 GB box that also takes POS orders. The limit is counted
    inclusively, so both edges are pinned.
    """

    def _bounds(self, from_date, to_date, max_days):
        def fake_throw(msg, exc=None):
            raise _Refused(str(msg))

        with patch.object(attendance_service, "frappe") as mock_frappe:
            mock_frappe.throw.side_effect = fake_throw
            try:
                return attendance_service._range_bounds(from_date, to_date, max_days)
            except _Refused:
                return "refused"

    def test_summary_accepts_exactly_its_limit(self):
        # 2026-06-01 .. 2026-09-01 inclusive is 93 days.
        self.assertNotEqual(
            self._bounds("2026-06-01", "2026-09-01", attendance_service.MAX_SUMMARY_RANGE_DAYS),
            "refused",
        )

    def test_summary_refuses_one_day_more(self):
        self.assertEqual(
            self._bounds("2026-06-01", "2026-09-02", attendance_service.MAX_SUMMARY_RANGE_DAYS),
            "refused",
        )

    def test_employee_accepts_a_full_year(self):
        # 2025-09-13 .. 2026-09-13 inclusive is 366 days.
        self.assertNotEqual(
            self._bounds("2025-09-13", "2026-09-13", attendance_service.MAX_EMPLOYEE_RANGE_DAYS),
            "refused",
        )

    def test_employee_refuses_a_year_and_a_day(self):
        self.assertEqual(
            self._bounds("2025-09-12", "2026-09-13", attendance_service.MAX_EMPLOYEE_RANGE_DAYS),
            "refused",
        )

    def test_a_slipped_year_digit_is_refused(self):
        """The exact failure that was reported."""
        self.assertEqual(
            self._bounds("1026-01-01", "2026-12-31", attendance_service.MAX_EMPLOYEE_RANGE_DAYS),
            "refused",
        )

    def test_a_reversed_over_long_pair_is_still_refused(self):
        """Swapping a reversed pair must not smuggle the span past the check."""
        self.assertEqual(
            self._bounds("2026-12-31", "1026-01-01", attendance_service.MAX_SUMMARY_RANGE_DAYS),
            "refused",
        )

    def test_the_default_month_is_never_refused(self):
        self.assertNotEqual(
            self._bounds(None, None, attendance_service.MAX_SUMMARY_RANGE_DAYS), "refused"
        )

    def test_both_endpoints_pass_their_limit(self):
        """A cap that exists but is never passed protects nothing."""
        seen = []

        def fake_bounds(from_date, to_date, max_days=None):
            seen.append(max_days)
            raise _Refused("stop")

        with patch.object(attendance_service, "_range_bounds", side_effect=fake_bounds):
            for fn, kwargs in (
                (attendance_service.get_summary, {}),
                (attendance_service.get_employee, {"employee": "HR-EMP-1"}),
            ):
                with self.assertRaises(_Refused):
                    fn(**kwargs)
        self.assertEqual(
            seen,
            [
                attendance_service.MAX_SUMMARY_RANGE_DAYS,
                attendance_service.MAX_EMPLOYEE_RANGE_DAYS,
            ],
        )

    def test_build_grid_has_its_own_backstop(self):
        """A future caller that forgets the cap must not reach a single query."""

        def fake_throw(msg, exc=None):
            raise _Refused(str(msg))

        with patch.object(attendance_service, "frappe") as mock_frappe, patch.object(
            attendance_service, "allowed_shift_locations"
        ) as allowed:
            mock_frappe.throw.side_effect = fake_throw
            with self.assertRaises(_Refused):
                attendance_service.build_grid("1026-01-01", "2026-12-31")
            mock_frappe.get_all.assert_not_called()
            allowed.assert_not_called()


SHARED_DAYS = [
    _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10"),
    _assignment("HR-EMP-1", "Branch Opening", "Dokki", "2026-09-11", "2026-09-11"),
]


def _punch(name, day, lat, lng):
    return {
        "name": name,
        "employee": "HR-EMP-1",
        "time": f"{day} 12:40:00",
        "log_type": "IN",
        "shift": "Branch Opening",
        "shift_start": f"{day} 12:30:00",
        "offshift": 0,
        "latitude": lat,
        "longitude": lng,
    }


SHARED_PUNCHES = [
    _punch("CI-NASR", "2026-09-10", 30.05, 31.34),
    _punch("CI-DOKKI", "2026-09-11", 30.03, 31.21),
]
FENCES = {
    "Nasr City": {"latitude": 30.05, "longitude": 31.34, "checkin_radius": 200},
    "Dokki": {"latitude": 30.03, "longitude": 31.21, "checkin_radius": 200},
}


class TestOtherBranchDaysAreRedacted(AttendanceGridCase):
    """Scope is decided per day, not per person.

    The Nasr City manager can see Ali, because Ali worked a shift at Nasr City.
    That must not hand them Ali's arrival times and raw GPS from the days Ali
    worked at Dokki -- with a zero or mis-set radius, that GPS is a home address.
    """

    def _shared(self, allowed):
        return self._grid(
            assignments=SHARED_DAYS,
            employees=[ALI],
            checkins=SHARED_PUNCHES,
            start="2026-09-10",
            end="2026-09-11",
            allowed=allowed,
            shift_times={"Branch Opening": ("12:30", "21:30")},
            locations=FENCES,
        )

    def test_the_own_branch_day_is_intact(self):
        cell = self._shared({"Nasr City"})["cells"]["HR-EMP-1"]["2026-09-10"]
        self.assertEqual(
            (cell["shift_location"], cell["checkin_count"]), ("Nasr City", 1)
        )
        self.assertIsNotNone(cell["first_in"])

    def test_the_other_branch_day_carries_no_clock_and_no_location(self):
        cell = self._shared({"Nasr City"})["cells"]["HR-EMP-1"]["2026-09-11"]
        self.assertEqual(cell["status"], "not_rostered")
        for key in (
            "shift_location", "shift_type", "first_in", "last_out", "late_minutes",
            "worked_hours", "geo_ok", "day_off", "scheduled_start", "scheduled_end",
        ):
            self.assertIsNone(cell[key], key)
        self.assertEqual(cell["checkin_count"], 0)
        self.assertFalse(cell["is_cover"])

    def test_the_raw_punches_for_that_day_are_gone_too(self):
        """``get_employee`` builds its coordinate list from these groups."""
        groups = self._shared({"Nasr City"})["groups"]
        self.assertIn(("HR-EMP-1", "2026-09-10"), groups)
        self.assertNotIn(("HR-EMP-1", "2026-09-11"), groups)

    def test_an_unrestricted_caller_still_sees_both_days(self):
        grid = self._shared(None)
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-11"]["shift_location"], "Dokki")
        self.assertIn(("HR-EMP-1", "2026-09-11"), grid["groups"])

    def test_the_other_branch_day_does_not_count_in_this_managers_totals(self):
        cells = self._shared({"Nasr City"})["cells"]["HR-EMP-1"].values()
        self.assertEqual(attendance_service.totals_for(cells)["rostered_days"], 1)

    def test_a_day_off_at_another_branch_leaks_no_detail(self):
        grid = self._grid(
            assignments=[SHARED_DAYS[0]],
            day_offs=[{
                "name": "JRDO-1",
                "employee": "HR-EMP-1",
                "off_date": "2026-09-11",
                "off_type": "Sick",
                "shift_location": "Dokki",
                "covered_by": "HR-EMP-2",
                "covered_by_name": "Sara",
            }],
            employees=[ALI],
            start="2026-09-10",
            end="2026-09-11",
            allowed={"Nasr City"},
        )
        cell = grid["cells"]["HR-EMP-1"]["2026-09-11"]
        self.assertIsNone(cell["day_off"])
        self.assertEqual(cell["status"], "not_rostered")

    def test_a_punch_on_a_day_with_no_branch_is_not_shown_to_a_branch_manager(self):
        """No branch means no branch can claim it -- the conservative reading."""
        grid = self._grid(
            assignments=[SHARED_DAYS[0]],
            employees=[ALI],
            checkins=[_punch("CI-NOWHERE", "2026-09-11", 29.9, 31.0)],
            start="2026-09-10",
            end="2026-09-11",
            allowed={"Nasr City"},
        )
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-11"]["checkin_count"], 0)
        self.assertNotIn(("HR-EMP-1", "2026-09-11"), grid["groups"])

    def test_get_employee_returns_no_coordinates_from_the_other_branch(self):
        """Through the real grid, not a canned one."""
        grid = self._shared({"Nasr City"})
        grid.pop("captured", None)
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service, "build_grid", return_value=grid
        ), patch.object(
            attendance_service, "shift_location_map", return_value=dict(FENCES)
        ):
            data = attendance_service.get_employee(
                "HR-EMP-1", from_date="2026-09-10", to_date="2026-09-11"
            )
        self.assertEqual([c["name"] for c in data["checkins"]], ["CI-NASR"])
        self.assertEqual(
            [b["shift_location"] for b in data["by_branch"] if b["rostered_days"]],
            ["Nasr City"],
        )


class TestCheckinReadFailureIsNotAbsence(unittest.TestCase):
    """A failed read used to return [] -- which renders every rostered person absent."""

    def test_a_read_that_fails_twice_raises(self):
        with patch.object(attendance_service, "frappe") as mock_frappe:
            mock_frappe.get_all.side_effect = RuntimeError("table is locked")
            with self.assertRaises(RuntimeError):
                attendance_service._fetch_checkins(
                    ["HR-EMP-1"], getdate("2026-09-10"), getdate("2026-09-10")
                )

    def test_a_missing_optional_field_still_falls_back(self):
        """The minimal-field retry exists for older HRMS schemas and must stay."""
        calls = []

        def fake_get_all(doctype, **kwargs):
            calls.append(kwargs["fields"])
            if len(calls) == 1:
                raise RuntimeError("Unknown column 'shift_actual_end'")
            return [{"name": "CI-1"}]

        with patch.object(attendance_service, "frappe") as mock_frappe:
            mock_frappe.get_all.side_effect = fake_get_all
            rows = attendance_service._fetch_checkins(
                ["HR-EMP-1"], getdate("2026-09-10"), getdate("2026-09-10")
            )
        self.assertEqual(rows, [{"name": "CI-1"}])
        self.assertEqual(calls[1], attendance_service.CHECKIN_FIELDS_MINIMAL)


class TestRefusalDoesNotEnumerateEmployees(unittest.TestCase):
    """An out-of-scope caller must not learn which ids exist or whose they are.

    Existence used to be checked first, so a bad id answered "No such employee"
    and a real one answered "<name> is not at one of your branches". This drives
    ``api/attendance.py`` itself, which nothing else in the suite calls.
    """

    def _api(self, exists, locations):
        from jarz_pos.api import attendance as attendance_api

        def fake_throw(msg, exc=None):
            raise _Refused(str(msg))

        with patch.object(attendance_api, "frappe") as api_frappe, patch.object(
            attendance_service, "frappe"
        ) as svc_frappe, patch.object(
            attendance_service, "ensure_attendance_access"
        ), patch.object(
            attendance_service, "allowed_shift_locations", return_value={"Nasr City"}
        ), patch.object(
            attendance_service, "NOT_AVAILABLE_MESSAGE", return_value="not available"
        ), patch.object(
            attendance_service, "get_employee", return_value={}
        ):
            api_frappe.throw.side_effect = fake_throw
            svc_frappe.throw.side_effect = fake_throw
            api_frappe.db.exists.return_value = exists
            svc_frappe.get_all.side_effect = lambda doctype, **kw: [
                {"shift_location": loc} for loc in locations
            ]
            svc_frappe.db.get_value.return_value = "Ali"
            try:
                attendance_api.get_employee("HR-EMP-9")
                return "allowed"
            except _Refused as refusal:
                return str(refusal)

    def test_a_missing_id_and_another_branchs_id_read_the_same(self):
        self.assertEqual(self._api(exists=False, locations=[]), "not available")
        self.assertEqual(self._api(exists=True, locations=["Dokki"]), "not available")

    def test_the_refusal_never_carries_the_name(self):
        self.assertNotIn("Ali", self._api(exists=True, locations=["Dokki"]))

    def test_an_in_scope_employee_is_served(self):
        self.assertEqual(self._api(exists=True, locations=["Nasr City"]), "allowed")


class TestUnmatchedTailOfAnotherBranchsNight(AttendanceGridCase):
    """A check-out past the window is dated by the clock, not by its shift.

    Ali closes a Dokki night shift (16:00 -> 01:00) on the 10th and opens at Nasr
    City on the 11th. He clocks out at 02:30 -- past the window, so HRMS matches
    no shift and the punch carries the 11th. The 11th is a Nasr City day. Without
    this, the Nasr City manager gets that punch's coordinates.
    """

    NIGHT_THEN_DAY = [
        _assignment("HR-EMP-1", "Night Close", "Dokki", "2026-09-10", "2026-09-10"),
        _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-11", "2026-09-11"),
    ]
    TIMES = {"Night Close": ("16:00", "01:00"), "Branch Opening": ("12:30", "21:30")}

    @staticmethod
    def _unmatched(name, when):
        return {
            "name": name,
            "employee": "HR-EMP-1",
            "time": when,
            "log_type": "OUT",
            "shift": None,
            "shift_start": None,
            "offshift": 1,
            "latitude": 29.99,
            "longitude": 31.10,
        }

    def _run(self, allowed, assignments=None, checkins=None, start="2026-09-10"):
        return self._grid(
            assignments=assignments or self.NIGHT_THEN_DAY,
            employees=[ALI],
            checkins=checkins or [self._unmatched("CI-TAIL", "2026-09-11 02:30:00")],
            start=start,
            end="2026-09-11",
            allowed=allowed,
            shift_times=self.TIMES,
            locations=FENCES,
        )

    def test_the_tail_is_hidden_from_the_next_days_branch(self):
        grid = self._run({"Nasr City"})
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-11"]["checkin_count"], 0)
        self.assertNotIn(("HR-EMP-1", "2026-09-11"), grid["groups"])

    def test_an_unrestricted_caller_still_sees_it(self):
        grid = self._run(None)
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-11"]["checkin_count"], 1)

    def test_the_tail_of_the_callers_own_night_is_kept(self):
        both_nasr = [
            _assignment("HR-EMP-1", "Night Close", "Nasr City", "2026-09-10", "2026-09-10"),
            _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-11", "2026-09-11"),
        ]
        grid = self._run({"Nasr City"}, assignments=both_nasr)
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-11"]["checkin_count"], 1)

    def test_a_genuinely_late_unmatched_arrival_on_the_callers_day_is_kept(self):
        """After the scheduled start it is an arrival for this day, not a tail."""
        grid = self._run(
            {"Nasr City"}, checkins=[self._unmatched("CI-LATE", "2026-09-11 23:10:00")]
        )
        cell = grid["cells"]["HR-EMP-1"]["2026-09-11"]
        self.assertEqual((cell["status"], cell["checkin_count"]), ("late_unmatched", 1))

    def test_an_early_unmatched_punch_on_the_first_day_of_the_range_is_hidden(self):
        """The previous day is outside the range, so it cannot be attributed."""
        grid = self._run({"Nasr City"}, start="2026-09-11")
        self.assertEqual(grid["cells"]["HR-EMP-1"]["2026-09-11"]["checkin_count"], 0)


class TestOverlappingAssignmentsRedactTheDay(AttendanceGridCase):
    """Two branches on one day: the day is redacted whichever row is read last.

    Keeping only the last assignment read made attribution depend on database
    order, so a day could read as Nasr City while holding Dokki's punches.
    """

    TWO = [
        _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10"),
        _assignment("HR-EMP-1", "Branch Closing", "Dokki", "2026-09-10", "2026-09-10"),
    ]

    def _cell(self, assignments):
        grid = self._grid(
            assignments=assignments,
            employees=[ALI],
            checkins=[_punch("CI-DOKKI", "2026-09-10", 30.03, 31.21)],
            allowed={"Nasr City"},
            locations=FENCES,
        )
        return grid["cells"]["HR-EMP-1"]["2026-09-10"], grid["groups"]

    def test_nasr_city_read_last(self):
        cell, groups = self._cell(list(reversed(self.TWO)))
        self.assertEqual((cell["status"], cell["checkin_count"]), ("not_rostered", 0))
        self.assertEqual(groups, {})

    def test_dokki_read_last(self):
        cell, groups = self._cell(self.TWO)
        self.assertEqual((cell["status"], cell["checkin_count"]), ("not_rostered", 0))
        self.assertEqual(groups, {})


class TestRedactedDaysAreNotUnmatchedPeople(AttendanceGridCase):
    """A redacted day has no branch, and must be skipped -- not bucketed as one.

    Bucketing it put every shared colleague working elsewhere into the "No
    branch" group, which the app explains as people who could not be matched to
    a branch. Managers would chase a data fault that does not exist, every day.
    """

    def _shared_grid(self):
        grid = self._grid(
            assignments=[
                _assignment("HR-EMP-1", "Branch Opening", "Nasr City", "2026-09-10", "2026-09-10"),
                _assignment("HR-EMP-1", "Branch Opening", "Dokki", "2026-09-11", "2026-09-11"),
                _assignment("HR-EMP-2", "Branch Opening", "Nasr City", "2026-09-11", "2026-09-11"),
            ],
            employees=[ALI, SARA],
            start="2026-09-10",
            end="2026-09-11",
            allowed={"Nasr City"},
        )
        grid.pop("captured", None)
        return grid

    def test_the_day_board_has_no_phantom_no_branch_bucket(self):
        grid = self._shared_grid()
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service, "build_grid", return_value=grid
        ):
            data = attendance_service.get_day(date="2026-09-11")
        self.assertEqual([b["shift_location"] for b in data["branches"]], ["Nasr City"])
        self.assertEqual(
            [r["employee"] for r in data["branches"][0]["rows"]], ["HR-EMP-2"]
        )
        self.assertEqual(data["totals"]["rostered"], 1)

    def test_the_branch_summary_has_no_phantom_no_branch_row(self):
        grid = self._shared_grid()
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service, "build_grid", return_value=grid
        ):
            data = attendance_service.get_summary(
                from_date="2026-09-10", to_date="2026-09-11", group_by="branch"
            )
        self.assertEqual([r["key"] for r in data["rows"]], ["Nasr City"])

    def test_the_employee_tab_has_no_empty_no_branch_row(self):
        grid = self._shared_grid()
        with patch.object(attendance_service, "hrms_available", return_value=True), patch.object(
            attendance_service, "build_grid", return_value=grid
        ), patch.object(attendance_service, "shift_location_map", return_value={}):
            data = attendance_service.get_employee(
                "HR-EMP-1", from_date="2026-09-10", to_date="2026-09-11"
            )
        self.assertEqual([b["shift_location"] for b in data["by_branch"]], ["Nasr City"])

    def test_other_branch_names_are_not_listed_for_the_employee(self):
        grid = self._shared_grid()
        ali = next(r for r in grid["employees"] if r["employee"] == "HR-EMP-1")
        self.assertEqual(ali["shift_locations"], ["Nasr City"])


if __name__ == "__main__":
    unittest.main()

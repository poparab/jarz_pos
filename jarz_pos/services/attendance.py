"""Attendance reporting: who actually turned up, derived from raw check-ins.

This module answers "was this person here, and were they on time?" for a month,
a day, one employee, or a rolled-up summary. It is READ-ONLY. It creates no
records, submits nothing, and deliberately does not touch HRMS's own attendance
machinery.

Why nothing here reads an Attendance record
-------------------------------------------
Auto-attendance is switched OFF on both staging and production, so the
``Attendance`` DocType is never produced: the table is empty and will stay
empty. Turning auto-attendance on to fill it is not a reporting change -- it
starts a nightly job that writes Absent rows for every employee it cannot pair,
feeds leave balances and payroll, and would back-fill months of history the
moment it ran. A screen that only needs to *display* who was late must not carry
that blast radius, so everything below is derived, on read, from three sources
that already exist:

* **Employee Checkin** -- the raw clock events. ``EmployeeCheckin.validate``
  calls ``fetch_shift()`` unconditionally, so ``shift``, ``shift_start`` and
  ``shift_end`` are stamped on every row even with auto-attendance off. That is
  what makes lateness computable at all.
* **Shift Assignment** -- what the person was *asked* to work. Read with
  ``status in ("Active", "Inactive")`` because HRMS flips every past assignment
  to Inactive nightly; an Active-only read makes all history vanish.
* **Jarz Roster Day Off** + the shift type's holiday list -- the two reasons a
  missing check-in is not a missing person.

Lateness, and why the grace period is ours
------------------------------------------
HRMS computes lateness in ``ShiftType.mark_attendance`` as "first IN minus
``shift_start`` exceeds ``late_entry_grace_period``". That field is **unset on
every Shift Type here**, because Desk only exposes it once auto-attendance is
enabled -- so reusing it would grade everybody who arrived one second after
their shift start as late. The same arithmetic is therefore applied against our
own ``attendance_late_grace_minutes`` setting, read through ``single_int`` so
that a deliberate 0 ("no grace at all") stays distinguishable from a field
nobody has ever written.

Pairing IN and OUT
------------------
The live Shift Types do not set ``determine_check_in_and_check_out``, and
``log_type`` is blank on most rows in practice, so it cannot be trusted to say
which event is an arrival. The first check-in of a day-group is taken as the
arrival and the last as the departure regardless of ``log_type``;
``checkin_count`` still counts every row, so a day with four punches is visibly
a day with four punches. A day with a single check-in has no departure and no
``worked_hours`` -- reporting the arrival time as the departure time would
manufacture a zero-hour day that reads as "clocked in and immediately left".

Which day a check-in belongs to
-------------------------------
Every branch shift here crosses midnight (16:00 -> 01:00), so a check-out logged
at 00:40 has a wall-clock date one day after the shift it belongs to. Grouping
on the wall clock would split one shift across two calendar cells and report the
second half as an unexplained arrival. Rows are therefore grouped by the date of
their **``shift_start``**, falling back to the wall clock only for a row HRMS
could not match to any shift at all.

The four statuses that are not "present or absent"
--------------------------------------------------
* ``late_unmatched`` -- somebody checked in, but HRMS matched no shift
  (``offshift=1``, ``shift_start`` NULL). That is an arrival *outside* the shift
  window, which is a different fact from "no data": it usually means they turned
  up long after their shift ended. It is counted as having shown up, but never
  as punctual.
* ``not_rostered`` -- no Shift Assignment covers the date. This is the state past
  the end of the generated roster horizon (currently 2026-11-19 on both servers).
  Reporting those days as ``absent`` would paint the entire company absent for
  every day after the horizon lapses, which is a housekeeping fact about the
  Shift Schedule generator and not a fact about any person.
* ``pending`` -- rostered, but the day has not finished happening yet. Only a
  date strictly in the past can be ``absent``.
* ``off`` / ``holiday`` -- a decision somebody made, or the holiday list; both
  outrank "no check-in".

The check-in-exempt employee
----------------------------
``Employee.custom_roster_checkin_exempt`` is the escape hatch the roster gate
uses to let somebody work without the roster policing them. Such a person carries
no obligation to clock in at all, so a day of theirs with **no check-in** is
reported ``not_rostered`` -- for the past and the future alike -- and therefore
drops out of ``rostered_days`` entirely. Reporting them ``absent`` would invent a
month of absences for a person nobody expects to see on a clock, and reporting
them ``present`` would invent attendance nobody recorded. A day on which an
exempt employee *did* check in is graded exactly like anybody else's, because
then there is real evidence to grade. Their ``off`` and ``holiday`` days are
unaffected: those are facts about the rota, not about the clock.

What counts as a rostered day
-----------------------------
``rostered_days`` is the number of days that carry a clock-in obligation **and
have already been judged**: ``present + late + late_unmatched + absent``.
``pending`` is excluded on purpose -- including it would make the attendance rate
of an in-progress month fall a little further every day simply because the month
is not over. ``off``, ``holiday`` and ``not_rostered`` are excluded because none
of them is a day anybody was asked to turn up for. ``pending_days`` is still
*reported*, so the screen can say how much of the month has not happened yet
without that number leaking into a rate.

Every status that can land in a cell has a counter in the totals, which makes
each block checkable against itself:

* month / employee / summary: ``rostered_days == present_days + late_days +
  late_unmatched_days + absent_days``
* day board: ``rostered == present + late + late_unmatched + absent + pending``

Those two identities are the point of the counters, not decoration. A header
that cannot be reconciled with the rows printed under it is a header nobody can
act on, and the row that used to go missing from the arithmetic was always the
``late_unmatched`` one -- the person who turned up outside their window, which
is the single row a manager most needs to ask about.
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_datetime, get_first_day, get_last_day, getdate

from jarz_pos.events.employee_checkin import EMPLOYEE_EXEMPT_FIELD
from jarz_pos.services import roster as roster_service
from jarz_pos.utils.settings_utils import single_flag, single_int

SETTINGS = "Jarz POS Settings"

#: Grace before an arrival counts as late, in minutes. NOT HRMS's
#: ``late_entry_grace_period``: that field is UI-gated behind auto-attendance and
#: is unset on every Shift Type on these servers, so reusing it would grade an
#: arrival one second past the hour as late.
GRACE_FIELD = "attendance_late_grace_minutes"
DEFAULT_GRACE_MINUTES = 15

CHECKIN_DOCTYPE = "Employee Checkin"

#: Longest range each read accepts, counted inclusively (end - start + 1).
#:
#: ``build_grid`` materialises one cell per employee per day, and expands every
#: open-ended assignment day by day, so the cost is employees x days with no
#: ceiling of its own. A slipped year digit (1026-01-01 -> 2026-12-31) across
#: ~40 staff is ~14.6M dicts -- on the 4 GB production box that also takes POS
#: orders, the OOM killer then picks a gunicorn worker or MariaDB. The app's
#: range pickers enforce the same numbers so a manager never meets the refusal.
MAX_SUMMARY_RANGE_DAYS = 93
MAX_EMPLOYEE_RANGE_DAYS = 366
#: Backstop inside ``build_grid`` itself, for any future caller that forgets.
MAX_GRID_DAYS = 366

#: Fields that are safe to read from Employee Checkin WITHOUT auto-attendance.
#: ``shift_start``/``shift_end`` are stamped by ``fetch_shift()`` in validate, so
#: they are present even though no Attendance record ever is.
CHECKIN_FIELDS = [
    "name",
    "employee",
    "time",
    "log_type",
    "shift",
    "shift_start",
    "shift_end",
    "offshift",
    "latitude",
    "longitude",
    "device_id",
]

#: Read even on a bench whose HRMS predates the geolocation fields.
CHECKIN_FIELDS_MINIMAL = ["name", "employee", "time", "log_type", "shift", "shift_start"]

STATUS_PRESENT = "present"
STATUS_LATE = "late"
STATUS_LATE_UNMATCHED = "late_unmatched"
STATUS_ABSENT = "absent"
STATUS_PENDING = "pending"
STATUS_OFF = "off"
STATUS_HOLIDAY = "holiday"
STATUS_NOT_ROSTERED = "not_rostered"

#: The exact strings the Flutter client switches on. Order is the order the
#: client renders its legend in; it is part of the contract.
STATUSES: Tuple[str, ...] = (
    STATUS_PRESENT,
    STATUS_LATE,
    STATUS_LATE_UNMATCHED,
    STATUS_ABSENT,
    STATUS_PENDING,
    STATUS_OFF,
    STATUS_HOLIDAY,
    STATUS_NOT_ROSTERED,
)

#: Days that carried an obligation AND have already been settled one way or the
#: other. This is the denominator of ``attendance_rate``; see the module
#: docstring for why ``pending`` is not in it.
JUDGED_STATUSES = (STATUS_PRESENT, STATUS_LATE, STATUS_LATE_UNMATCHED, STATUS_ABSENT)

#: Days somebody actually turned up, however late and however unmatched.
SHOWED_STATUSES = (STATUS_PRESENT, STATUS_LATE, STATUS_LATE_UNMATCHED)

#: Ordering for the day board: the rows a manager has to act on first.
DAY_ROW_RANK = {
    STATUS_LATE: 0,
    STATUS_LATE_UNMATCHED: 1,
    STATUS_ABSENT: 2,
    STATUS_PENDING: 3,
    STATUS_PRESENT: 4,
    STATUS_OFF: 5,
    STATUS_HOLIDAY: 6,
    STATUS_NOT_ROSTERED: 7,
}


# ---------------------------------------------------------------------------
# Access -- all of it borrowed from the roster, deliberately
# ---------------------------------------------------------------------------


def has_attendance_access() -> bool:
    """Same tier as the roster: if you may set the rota you may read the clock.

    A narrower gate would be a dead screen. The attendance view is reached from
    the same manager dashboard as the roster, and every person who can decide
    who works tomorrow is the person who needs to know who failed to turn up
    today.
    """
    return roster_service.has_roster_access()


def ensure_attendance_access() -> None:
    if not has_attendance_access():
        frappe.throw(_("Not permitted: attendance access required"), frappe.PermissionError)


def allowed_shift_locations() -> Optional[Set[str]]:
    """Branch scope, identical to the roster's. ``None`` means unrestricted."""
    return roster_service.allowed_shift_locations()


def scope_payload(allowed: Optional[Set[str]]) -> Dict[str, Any]:
    return {
        "configured": roster_service.roster_scope_configured(),
        "unrestricted": allowed is None,
        "locations": None if allowed is None else sorted(allowed),
    }


def NOT_AVAILABLE_MESSAGE() -> str:
    return _("That employee is not available to you.")


def ensure_employee_in_scope(employee: str) -> None:
    """Refuse to report on somebody outside the caller's branches.

    The read equivalent of ``api/roster._ensure_employee_in_scope``, with one
    difference that matters: it accepts ``Inactive`` assignments. A branch
    manager pulling last quarter's attendance for one of their own staff would
    otherwise be told the person is not at their branch, because HRMS has since
    flipped every one of that employee's past assignments to Inactive.
    """
    allowed = allowed_shift_locations()
    if allowed is None:
        return

    locations: Set[str] = set()
    try:
        for row in frappe.get_all(
            "Shift Assignment",
            filters={
                "employee": employee,
                "docstatus": 1,
                "status": ("in", roster_service.READABLE_ASSIGNMENT_STATUSES),
            },
            fields=["shift_location"],
        ):
            if row.get("shift_location"):
                locations.add(row["shift_location"])
    except Exception:
        pass
    try:
        for row in frappe.get_all(
            "Shift Schedule Assignment",
            filters={"employee": employee, "enabled": 1},
            fields=["shift_location"],
        ):
            if row.get("shift_location"):
                locations.add(row["shift_location"])
    except Exception:
        pass

    if not locations.intersection(allowed):
        # Deliberately says nothing about the employee: naming them would let an
        # out-of-scope caller confirm which ids exist and read their names.
        frappe.throw(NOT_AVAILABLE_MESSAGE(), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def grace_minutes() -> int:
    """Minutes of grace before an arrival is late.

    Read through ``single_int`` rather than ``get_single_value``: the latter
    casts an Int through ``cint()``, so a field nobody has ever written reads as
    ``0`` and every arrival one second past the hour would be graded late, with
    the declared default of 15 unreachable. A deliberate 0 is honoured as 0 --
    "no grace" is a real policy somebody may choose.
    """
    try:
        value = single_int(SETTINGS, GRACE_FIELD, DEFAULT_GRACE_MINUTES)
    except Exception:
        return DEFAULT_GRACE_MINUTES
    # A negative grace would grade an on-time arrival as late; clamp rather than
    # throw, because this is a read path and a typo in Desk must not 500 it.
    return max(0, int(value))


def checkin_enforced() -> bool:
    """Whether the roster gate is refusing unrostered check-ins right now.

    Surfaced on the bootstrap because it changes how an ``absent`` should be
    read: with the gate off, a missing check-in may simply mean somebody clocked
    in from the wrong place and was not stopped.
    """
    try:
        return single_flag(SETTINGS, "roster_enforce_checkin", True)
    except Exception:
        return True


def hrms_available() -> bool:
    return roster_service.hrms_available()


def _hrms_notice() -> str:
    return _("HRMS is not installed on this site, so there is no attendance to show.")


# ---------------------------------------------------------------------------
# Small coercions -- every one of them a trap this codebase has already hit
# ---------------------------------------------------------------------------


def _hhmm(value: Any) -> Optional[str]:
    """Render a Shift Type time as ``HH:mm``, keeping midnight visible.

    A Frappe ``Time`` field comes back as ``datetime.timedelta``, and midnight is
    ``timedelta(0)`` -- which is FALSY. ``str(value or "")`` therefore erases the
    end time of the Friday courier shift, which really does end at 00:00. Only
    ``None`` means "no time here".
    """
    parsed = roster_service._as_timedelta(value)
    if parsed is None:
        return None
    total = int(parsed.total_seconds()) % 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _dt_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return get_datetime(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _distance_m(lat1: Any, lng1: Any, lat2: Any, lng2: Any) -> Optional[float]:
    """Metres between two points, or ``None`` when they cannot be measured.

    Uses HRMS's own ``get_distance_between_coordinates`` so that ``geo_ok``
    reports exactly what ``EmployeeCheckin.validate_distance_from_shift_location``
    and our own ``events/employee_checkin._enforce_location`` enforce -- a report
    that disagreed with the gate would be worse than no report. The in-app
    haversine is the fallback for a bench without HRMS; neither is written a
    second time here.
    """
    a_lat, a_lng = _float_or_none(lat1), _float_or_none(lng1)
    b_lat, b_lng = _float_or_none(lat2), _float_or_none(lng2)
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    try:
        from hrms.hr.utils import get_distance_between_coordinates

        return float(get_distance_between_coordinates(a_lat, a_lng, b_lat, b_lng))
    except Exception:
        from jarz_pos.utils.geo import distance_m_or_none

        return distance_m_or_none(a_lat, a_lng, b_lat, b_lng)


def _geo_ok(coords: List[Tuple[Any, Any]], location_row: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Whether every located check-in of the day sat inside the branch radius.

    ``None`` -- not False -- whenever the question cannot be answered: no
    coordinates on the check-in, no branch resolved, or a branch with no radius
    configured. ``False`` is an accusation and must only be returned when a
    distance was actually measured and actually exceeded. A non-positive radius
    means "do not measure", matching HRMS's own escape hatch.
    """
    if not coords or not location_row:
        return None
    radius = flt(location_row.get("checkin_radius") or 0)
    if radius <= 0:
        return None

    measured = False
    for lat, lng in coords:
        distance = _distance_m(
            location_row.get("latitude"), location_row.get("longitude"), lat, lng
        )
        if distance is None:
            continue
        measured = True
        if distance > radius:
            return False
    return True if measured else None


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------


def shift_time_map() -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """``{shift_type: (start "HH:mm", end "HH:mm")}`` from the roster catalogue."""
    out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    try:
        catalog = roster_service.shift_catalog()
    except Exception:
        return out
    for row in catalog:
        out[row["shift_type"]] = (
            _hhmm(row.get("start_time")),
            _hhmm(row.get("end_time")),
        )
    return out


def shift_location_map() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rows = roster_service.shift_locations()
    except Exception:
        return out
    for row in rows:
        out[row["shift_location"]] = row
    return out


def _holidays(employees: List[str], start: date_cls, end: date_cls) -> Dict[str, Set[str]]:
    try:
        return roster_service._holidays_by_employee(employees, start, end)
    except Exception:
        # A holiday list that cannot be resolved must not take the whole month
        # read down; the worst outcome is a Friday showing as not_rostered.
        return {}


def _is_courier(employee: str, designation: Optional[str]) -> bool:
    try:
        return roster_service.is_courier(employee, designation)
    except Exception:
        return False


def _employee_exempt_field_exists() -> bool:
    try:
        return bool(frappe.get_meta("Employee").get_field(EMPLOYEE_EXEMPT_FIELD))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Check-in grouping
# ---------------------------------------------------------------------------


def group_checkins(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Fold raw check-ins into one group per ``(employee, shift day)``.

    The day a group belongs to is the date of its ``shift_start``, NOT the wall
    clock of the punch. Every branch shift here crosses midnight, so a check-out
    at 00:40 carries a wall-clock date one day after the shift it closes;
    grouping on the wall clock would split one shift in half and report the tail
    as an unexplained arrival on a day the person was not even rostered.

    A row HRMS could not match to any shift has no ``shift_start`` and falls back
    to its wall-clock date -- that is the ``late_unmatched`` case, an arrival
    outside the window rather than an absence.
    """
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows or []:
        employee = row.get("employee")
        if not employee or not row.get("time"):
            continue
        try:
            when = get_datetime(row.get("time"))
        except Exception:
            continue
        if when is None:
            continue

        shift_start = None
        if row.get("shift_start"):
            try:
                shift_start = get_datetime(row.get("shift_start"))
            except Exception:
                shift_start = None

        day_key = str(getdate(shift_start or when))
        key = (employee, day_key)
        group = groups.get(key)
        if group is None:
            group = {
                "employee": employee,
                "date": day_key,
                "count": 0,
                "first_in": None,
                "last": None,
                "shift_start": None,
                "shift": None,
                "offshift": False,
                "coords": [],
                "rows": [],
            }
            groups[key] = group

        group["count"] += 1
        group["rows"].append(row)
        if group["first_in"] is None or when < group["first_in"]:
            group["first_in"] = when
        if group["last"] is None or when > group["last"]:
            group["last"] = when
        if shift_start is not None and (
            group["shift_start"] is None or shift_start < group["shift_start"]
        ):
            group["shift_start"] = shift_start
        if row.get("shift") and not group["shift"]:
            group["shift"] = row["shift"]
        if row.get("offshift"):
            group["offshift"] = True

        lat = _float_or_none(row.get("latitude"))
        lng = _float_or_none(row.get("longitude"))
        if lat is not None and lng is not None:
            group["coords"].append((lat, lng))

    return groups


def late_minutes_for(group: Optional[Dict[str, Any]]) -> Optional[int]:
    """Signed minutes between the arrival and the scheduled start.

    Negative means early, which is information a manager wants and which the
    contract carries as a signed number on the cell. ``None`` when HRMS matched
    no shift, because there is then nothing to be late *against*.
    """
    if not group or group.get("shift_start") is None or group.get("first_in") is None:
        return None
    delta = group["first_in"] - group["shift_start"]
    return int(round(delta.total_seconds() / 60.0))


def worked_hours_for(group: Optional[Dict[str, Any]]) -> Optional[float]:
    """Last punch minus first, in hours. ``None`` for a lone check-in.

    A single punch is an arrival with no departure. Reporting 0.0 would read as
    "clocked in and left immediately", which is a claim about somebody's day
    that the data does not make.
    """
    if not group or group.get("count", 0) < 2:
        return None
    if group.get("first_in") is None or group.get("last") is None:
        return None
    return round((group["last"] - group["first_in"]).total_seconds() / 3600.0, 2)


# ---------------------------------------------------------------------------
# Status derivation -- the heart of the module
# ---------------------------------------------------------------------------


def derive_status(
    *,
    group: Optional[Dict[str, Any]],
    rostered: bool,
    day_off: bool,
    is_holiday: bool,
    exempt: bool,
    late_minutes: Optional[int],
    grace: int,
    is_past: bool,
) -> str:
    """Pick one of the eight contract statuses for one employee-day.

    Evidence outranks expectation. Somebody who clocked in is graded on the
    clock even if the rota says they were off -- the alternative is a screen
    that reports "off" for a person who was standing in the branch, which is the
    one thing a manager would never forgive it for.
    """
    if group and group.get("count", 0) > 0:
        if late_minutes is None:
            # A punch HRMS could not attach to any shift: arrived outside the
            # window, not "no data".
            return STATUS_LATE_UNMATCHED
        return STATUS_LATE if late_minutes > grace else STATUS_PRESENT

    if day_off:
        return STATUS_OFF
    if is_holiday:
        return STATUS_HOLIDAY
    if not rostered:
        # Includes every date past the end of the generated roster horizon. See
        # the module docstring: that is a fact about the Shift Schedule
        # generator, never about the person.
        return STATUS_NOT_ROSTERED
    if exempt:
        # No clock-in obligation, so silence is not absence.
        return STATUS_NOT_ROSTERED
    if not is_past:
        return STATUS_PENDING
    return STATUS_ABSENT


def build_cell(
    *,
    date_str: str,
    shift_type: Optional[str],
    shift_location: Optional[str],
    scheduled_start: Optional[str],
    scheduled_end: Optional[str],
    group: Optional[Dict[str, Any]],
    day_off_row: Optional[Dict[str, Any]],
    is_holiday: bool,
    is_cover: bool,
    exempt: bool,
    grace: int,
    today: date_cls,
    location_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One calendar cell, in the exact shape the contract fixes."""
    late = late_minutes_for(group)
    status = derive_status(
        group=group,
        rostered=bool(shift_type),
        day_off=bool(day_off_row),
        is_holiday=bool(is_holiday),
        exempt=bool(exempt),
        late_minutes=late,
        grace=grace,
        is_past=getdate(date_str) < today,
    )

    count = int(group.get("count", 0)) if group else 0
    return {
        "date": date_str,
        "status": status,
        "shift_type": shift_type,
        "shift_location": shift_location,
        "scheduled_start": scheduled_start,
        "scheduled_end": scheduled_end,
        "first_in": _dt_text(group.get("first_in")) if group else None,
        "last_out": _dt_text(group.get("last")) if (group and count >= 2) else None,
        "late_minutes": late,
        "worked_hours": worked_hours_for(group),
        "checkin_count": count,
        "offshift": bool(group.get("shift_start") is None) if group else False,
        "geo_ok": _geo_ok(group.get("coords") if group else [], location_row),
        "day_off": (
            {
                "off_type": day_off_row.get("off_type"),
                "covered_by": day_off_row.get("covered_by"),
                "covered_by_name": day_off_row.get("covered_by_name"),
            }
            if day_off_row
            else None
        ),
        "is_cover": bool(is_cover),
    }


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------


def totals_for(cells: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll a pile of cells into the contract's totals block.

    ``late_minutes`` here is the sum of POSITIVE lateness only. Cell-level
    lateness is signed so an early arrival is visible, but summing the signed
    values would let a week of early starts cancel out a day of being an hour
    late and report a punctual month.

    Every status that can appear in a cell has a counter, so the block can be
    checked against itself:

        rostered_days == present_days + late_days + late_unmatched_days + absent_days

    That invariant is what makes ``attendance_rate`` verifiable from the numbers
    printed next to it. ``late_unmatched_days`` and ``pending_days`` were added
    after the client shipped: without them a header could not be reconciled with
    its own rows, and the two statuses that explain the gap were invisible.
    ``pending_days`` is reported but deliberately stays OUT of ``rostered_days``
    -- see the module docstring.
    """
    rostered = present = late = late_unmatched = absent = pending = off = showed = 0
    worked = 0.0
    late_total = 0

    for cell in cells:
        status = cell.get("status")
        if status in JUDGED_STATUSES:
            rostered += 1
        if status in SHOWED_STATUSES:
            showed += 1
        if status == STATUS_PRESENT:
            present += 1
        elif status == STATUS_LATE:
            late += 1
        elif status == STATUS_LATE_UNMATCHED:
            late_unmatched += 1
        elif status == STATUS_ABSENT:
            absent += 1
        elif status == STATUS_PENDING:
            pending += 1
        elif status == STATUS_OFF:
            off += 1

        worked += flt(cell.get("worked_hours") or 0)
        minutes = cell.get("late_minutes")
        if minutes and minutes > 0:
            late_total += int(minutes)

    return {
        "rostered_days": rostered,
        "present_days": present,
        "late_days": late,
        "late_unmatched_days": late_unmatched,
        "absent_days": absent,
        "pending_days": pending,
        "off_days": off,
        "worked_hours": round(worked, 2),
        "late_minutes": late_total,
        # Zero rostered days is a real month for a new joiner or an exempt
        # employee. Dividing would 500 the whole screen for everybody else on it.
        "attendance_rate": round(showed / rostered, 4) if rostered else 0.0,
        # 0.0 when nobody showed: with no arrivals there is no punctuality to
        # claim, and 1.0 would read as a perfect record.
        "punctuality_rate": round(present / showed, 4) if showed else 0.0,
    }


def day_totals_for(cells: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """The counters the day board shows.

    ``rostered`` counts everybody who owed a shift today, ``pending`` included --
    a live board is watched mid-shift, and hiding the people who have not arrived
    yet is exactly the number a manager is looking for. So, here:

        rostered == present + late + late_unmatched + absent + pending

    ``late_unmatched`` is an added key. Without it a branch header did not add up
    to the rows printed underneath it, and the missing person was always the one
    who had turned up outside their window -- the row a manager most needs to ask
    about.
    """
    counts = {
        "rostered": 0,
        "present": 0,
        "late": 0,
        "late_unmatched": 0,
        "absent": 0,
        "pending": 0,
        "off": 0,
    }
    for cell in cells:
        status = cell.get("status")
        if status in JUDGED_STATUSES or status == STATUS_PENDING:
            counts["rostered"] += 1
        if status == STATUS_PRESENT:
            counts["present"] += 1
        elif status == STATUS_LATE:
            counts["late"] += 1
        elif status == STATUS_LATE_UNMATCHED:
            counts["late_unmatched"] += 1
        elif status == STATUS_ABSENT:
            counts["absent"] += 1
        elif status == STATUS_PENDING:
            counts["pending"] += 1
        elif status == STATUS_OFF:
            counts["off"] += 1
    return counts


# ---------------------------------------------------------------------------
# The grid: one pass over roster + check-ins for an arbitrary date range
# ---------------------------------------------------------------------------


def _date_range(start: date_cls, end: date_cls) -> Iterable[date_cls]:
    return roster_service._daterange(start, end)


def _fetch_checkins(employees: List[str], start: date_cls, end: date_cls) -> List[Dict[str, Any]]:
    """Raw check-ins covering the range, padded on both sides.

    The window runs from the day BEFORE ``start`` to the day AFTER ``end``
    because a shift on either boundary can own punches on the neighbouring
    calendar day: a 16:00 -> 01:00 shift starting on the last of the month is
    clocked out of on the first of the next one. Groups outside the range are
    discarded after grouping, not before.
    """
    if not employees:
        return []
    window = [
        f"{add_days(start, -1)} 00:00:00",
        f"{add_days(end, 2)} 00:00:00",
    ]
    filters = {"employee": ("in", employees), "time": ("between", window)}
    try:
        return frappe.get_all(
            CHECKIN_DOCTYPE, filters=filters, fields=CHECKIN_FIELDS, order_by="time asc"
        )
    except Exception:
        try:
            return frappe.get_all(
                CHECKIN_DOCTYPE,
                filters=filters,
                fields=CHECKIN_FIELDS_MINIMAL,
                order_by="time asc",
            )
        except Exception:
            # Returning [] here would render every rostered person as absent --
            # a confident, wrong report. Fail the request instead, so the
            # screen shows that it could not load.
            try:
                frappe.log_error(title="Attendance: Employee Checkin read failed")
            except Exception:
                pass
            raise


def build_grid(
    start: date_cls,
    end: date_cls,
    shift_location: Optional[str] = None,
    employee: Optional[str] = None,
    today: Optional[date_cls] = None,
) -> Dict[str, Any]:
    """Every employee in scope, with one cell per day of the range.

    The roster half mirrors ``roster_service.get_month`` deliberately: same
    readable-status filter, same "an employee belongs to whichever branches
    their shifts point at" rule, same exclusion of non-Active staff. Two screens
    disagreeing about who works where would be worse than either being wrong.
    """
    start = getdate(start)
    end = getdate(end)
    _refuse_long_range(start, end, MAX_GRID_DAYS)
    today = getdate(today) if today else getdate()
    allowed = allowed_shift_locations()

    assignment_filters: Dict[str, Any] = {
        "docstatus": 1,
        "status": ("in", roster_service.READABLE_ASSIGNMENT_STATUSES),
        "start_date": ("<=", end),
    }
    day_off_filters: Dict[str, Any] = {"off_date": ("between", [start, end])}
    if employee:
        assignment_filters["employee"] = employee
        day_off_filters["employee"] = employee

    assignments = frappe.get_all(
        "Shift Assignment",
        filters=assignment_filters,
        or_filters=[["end_date", ">=", start], ["end_date", "is", "not set"]],
        fields=["name", "employee", "shift_type", "shift_location", "start_date", "end_date"],
    )

    day_offs = frappe.get_all(
        roster_service.DAY_OFF_DOCTYPE,
        filters=day_off_filters,
        fields=[
            "name",
            "employee",
            "off_date",
            "off_type",
            "shift_location",
            "covered_by",
            "covered_by_name",
        ],
    )

    # A cover is recorded on the OTHER person's day-off row, so it has to be
    # indexed separately from the day-off itself -- otherwise the colleague who
    # absorbed the day looks like an ordinary rostered day and the screen cannot
    # explain why they are on a twelve-hour shift.
    covers: Set[Tuple[str, str]] = set()
    for row in day_offs:
        if row.get("covered_by"):
            covers.add((row["covered_by"], str(getdate(row["off_date"]))))

    locations_by_employee: Dict[str, Set[str]] = {}
    for row in assignments:
        if row.get("shift_location"):
            locations_by_employee.setdefault(row["employee"], set()).add(row["shift_location"])
    for row in day_offs:
        if row.get("shift_location"):
            locations_by_employee.setdefault(row["employee"], set()).add(row["shift_location"])

    candidates: Set[str] = {row["employee"] for row in assignments}
    candidates.update(row["employee"] for row in day_offs)
    if employee:
        candidates.add(employee)
    try:
        for row in frappe.get_all(
            "Shift Schedule Assignment",
            filters=(
                {"enabled": 1, "employee": employee} if employee else {"enabled": 1}
            ),
            fields=["employee", "shift_location"],
        ):
            candidates.add(row["employee"])
            if row.get("shift_location"):
                locations_by_employee.setdefault(row["employee"], set()).add(row["shift_location"])
    except Exception:
        pass

    def in_scope(name: str) -> bool:
        locs = locations_by_employee.get(name, set())
        if shift_location and shift_location not in locs:
            return False
        if allowed is None:
            return True
        # Somebody with no Shift Location at all is invisible to a branch-scoped
        # manager on purpose: an unlocated employee is geofenced nowhere, so no
        # branch can claim them.
        return bool(locs.intersection(allowed))

    names = sorted(n for n in candidates if in_scope(n))

    employee_rows: Dict[str, Dict[str, Any]] = {}
    if names:
        fields = ["name", "employee_name", "designation", "department", "status"]
        if _employee_exempt_field_exists():
            fields.append(EMPLOYEE_EXEMPT_FIELD)
        for row in frappe.get_all("Employee", filters={"name": ("in", names)}, fields=fields):
            employee_rows[row["name"]] = row

    # Resigned staff keep their historic assignments. They are excluded for the
    # same reason the roster excludes them -- except when one is asked for by
    # name, because "why does last month's report not contain the person who
    # left in it" is a question with no good answer.
    names = [
        n
        for n in names
        if n == employee or (employee_rows.get(n, {}).get("status") == "Active")
    ]

    shift_times = shift_time_map()
    locations = shift_location_map()
    holidays = _holidays(names, start, end)
    groups = group_checkins(_fetch_checkins(names, start, end))

    roster_cells: Dict[str, Dict[str, Dict[str, Any]]] = {n: {} for n in names}
    lookup = set(names)
    for row in assignments:
        name = row["employee"]
        if name not in lookup:
            continue
        first = max(getdate(row["start_date"]), start)
        last = min(getdate(row["end_date"]), end) if row.get("end_date") else end
        for day in _date_range(first, last):
            roster_cells[name][str(day)] = row

    day_off_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in day_offs:
        day_off_index[(row["employee"], str(getdate(row["off_date"])))] = row

    grace = grace_minutes()
    employees_payload: List[Dict[str, Any]] = []
    cells_by_employee: Dict[str, Dict[str, Dict[str, Any]]] = {}
    # (employee, day) pairs that belong to a branch outside the caller's scope.
    redacted: Set[Tuple[str, str]] = set()

    for name in names:
        row = employee_rows.get(name, {})
        designation = row.get("designation")
        exempt = bool(row.get(EMPLOYEE_EXEMPT_FIELD))
        employee_holidays = holidays.get(name, set())

        cells: Dict[str, Dict[str, Any]] = {}
        for day in _date_range(start, end):
            key = str(day)
            assignment = roster_cells.get(name, {}).get(key) or {}
            shift_type = assignment.get("shift_type")
            resolved_location = assignment.get("shift_location")
            day_off_row = day_off_index.get((name, key))
            if not resolved_location and day_off_row:
                resolved_location = day_off_row.get("shift_location")
            scheduled = shift_times.get(shift_type or "", (None, None))

            # Scope is decided PER DAY, not per person. ``in_scope`` admits an
            # employee if any of their shifts ever touched one of the caller's
            # branches -- right for who appears on the screen, wrong for what
            # it shows about them. Somebody who covered one day at Nasr City
            # would otherwise hand the Nasr City manager a year of their
            # arrival times and raw GPS at Dokki; with a zero or mis-set
            # radius, that GPS is a home address. A day this caller cannot
            # attribute to their own branch -- including a day with no branch
            # at all -- is reported as a blank, unrostered day and carries no
            # clock, no coordinates and no day-off detail.
            if allowed is not None and resolved_location not in allowed:
                redacted.add((name, key))
                cells[key] = build_cell(
                    date_str=key,
                    shift_type=None,
                    shift_location=None,
                    scheduled_start=None,
                    scheduled_end=None,
                    group=None,
                    day_off_row=None,
                    is_holiday=False,
                    is_cover=False,
                    exempt=exempt,
                    grace=grace,
                    today=today,
                    location_row=None,
                )
                continue

            cells[key] = build_cell(
                date_str=key,
                shift_type=shift_type,
                shift_location=resolved_location,
                scheduled_start=scheduled[0],
                scheduled_end=scheduled[1],
                group=groups.get((name, key)),
                day_off_row=day_off_row,
                is_holiday=key in employee_holidays,
                is_cover=(name, key) in covers,
                exempt=exempt,
                grace=grace,
                today=today,
                location_row=locations.get(resolved_location) if resolved_location else None,
            )

        cells_by_employee[name] = cells
        employees_payload.append(
            {
                "employee": name,
                "employee_name": row.get("employee_name") or name,
                "designation": designation,
                "department": row.get("department"),
                "shift_locations": sorted(locations_by_employee.get(name, set())),
                "is_courier": _is_courier(name, designation),
                "exempt": exempt,
            }
        )

    # The raw check-in groups feed ``get_employee``'s coordinate list, so the
    # redacted days have to leave here too, not just their cells.
    if redacted:
        groups = {pair: group for pair, group in groups.items() if pair not in redacted}

    return {
        "employees": employees_payload,
        "cells": cells_by_employee,
        "groups": groups,
        "scope": scope_payload(allowed),
        "grace_minutes": grace,
        "start": start,
        "end": end,
    }


# ---------------------------------------------------------------------------
# Endpoint payloads
# ---------------------------------------------------------------------------


def month_bounds(month: Optional[str]) -> Tuple[date_cls, date_cls]:
    return roster_service.month_bounds(month)


def bootstrap() -> Dict[str, Any]:
    """Everything the screen needs before it can draw anything."""
    allowed = allowed_shift_locations()
    payload: Dict[str, Any] = {
        "hrms_available": hrms_available(),
        "shift_locations": roster_service.shift_locations() if hrms_available() else [],
        "scope": scope_payload(allowed),
        "grace_minutes": grace_minutes(),
        "statuses": list(STATUSES),
        "checkin_enforced": checkin_enforced(),
        "notice": None,
    }
    if not payload["hrms_available"]:
        payload["notice"] = _hrms_notice()
    return payload


def get_month(
    month: Optional[str] = None, shift_location: Optional[str] = None
) -> Dict[str, Any]:
    """The attendance calendar: people down, days across."""
    start, end = month_bounds(month)
    if not hrms_available():
        return {
            "hrms_available": False,
            "notice": _hrms_notice(),
            "month": f"{start.year:04d}-{start.month:02d}",
            "month_start": str(start),
            "month_end": str(end),
            "scope": scope_payload(None),
            "grace_minutes": grace_minutes(),
            "employees": [],
            "totals": totals_for([]),
        }

    grid = build_grid(start, end, shift_location=shift_location)
    employees: List[Dict[str, Any]] = []
    every_cell: List[Dict[str, Any]] = []

    for row in grid["employees"]:
        cells = grid["cells"].get(row["employee"], {})
        every_cell.extend(cells.values())
        employees.append(
            {
                "employee": row["employee"],
                "employee_name": row["employee_name"],
                "designation": row["designation"],
                "department": row["department"],
                "shift_locations": row["shift_locations"],
                "is_courier": row["is_courier"],
                "days": cells,
                "totals": totals_for(cells.values()),
            }
        )

    employees.sort(key=lambda r: (r["employee_name"] or "").lower())
    totals = totals_for(every_cell)
    totals["employees"] = len(employees)

    return {
        "hrms_available": True,
        "month": f"{start.year:04d}-{start.month:02d}",
        "month_start": str(start),
        "month_end": str(end),
        "scope": grid["scope"],
        "grace_minutes": grid["grace_minutes"],
        "employees": employees,
        "totals": totals,
    }


#: Keys the day board copies straight off a cell. ``shift_location`` is dropped
#: -- the bucket carries it, and repeating it invites the two to drift.
#:
#: ``date`` is NOT dropped, even though the envelope also carries one. A row's
#: date is the date of the SHIFT it belongs to, and the branch day here genuinely
#: crosses midnight, so a night-shift row can legitimately name a different
#: calendar day from the header. A client stamping the envelope's date onto every
#: row would mislabel exactly those rows -- and they are the ones somebody opens
#: the detail sheet for.
_DAY_ROW_KEYS = (
    "date",
    "status",
    "shift_type",
    "scheduled_start",
    "scheduled_end",
    "first_in",
    "last_out",
    "late_minutes",
    "worked_hours",
    "checkin_count",
    "offshift",
    "geo_ok",
    "day_off",
    "is_cover",
)


def _day_sort_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    """Worst first: late (longest first), then unmatched, absent, pending, present."""
    rank = DAY_ROW_RANK.get(row.get("status"), 99)
    lateness = -int(row.get("late_minutes") or 0) if row.get("status") == STATUS_LATE else 0
    return (rank, lateness, (row.get("employee_name") or "").lower())


def _branch_sort_key(bucket: Dict[str, Any]) -> Tuple[int, str]:
    """Named branches alphabetically; the ``null`` bucket is ALWAYS last.

    ``None`` there means "no branch could be resolved", which is a data problem
    rather than a place. Sorting it in with the names (or first, which is what a
    naive sort on ``or ""`` does) puts the unattributable rows above the real
    branches at the top of the screen every morning.
    """
    location = bucket.get("shift_location")
    return (1, "") if location is None else (0, str(location).lower())


def get_day(date: Optional[str] = None, shift_location: Optional[str] = None) -> Dict[str, Any]:
    """One day, grouped by branch, ordered by what needs attention."""
    day = getdate(date) if date else getdate()
    if not hrms_available():
        return {
            "hrms_available": False,
            "notice": _hrms_notice(),
            "date": str(day),
            "scope": scope_payload(None),
            "grace_minutes": grace_minutes(),
            "branches": [],
            "totals": day_totals_for([]),
        }

    grid = build_grid(day, day, shift_location=shift_location)
    buckets: Dict[Optional[str], Dict[str, Any]] = {}
    every_cell: List[Dict[str, Any]] = []

    for employee_row in grid["employees"]:
        cell = grid["cells"].get(employee_row["employee"], {}).get(str(day))
        if not cell:
            continue
        every_cell.append(cell)
        location = cell.get("shift_location")
        bucket = buckets.setdefault(location, {"shift_location": location, "rows": [], "_cells": []})
        bucket["_cells"].append(cell)
        row = {
            "employee": employee_row["employee"],
            "employee_name": employee_row["employee_name"],
            "designation": employee_row["designation"],
            "is_courier": employee_row["is_courier"],
        }
        row.update({key: cell.get(key) for key in _DAY_ROW_KEYS})
        bucket["rows"].append(row)

    branches: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        bucket["rows"].sort(key=_day_sort_key)
        branches.append(
            {
                "shift_location": bucket["shift_location"],
                "totals": day_totals_for(bucket["_cells"]),
                "rows": bucket["rows"],
            }
        )
    branches.sort(key=_branch_sort_key)

    return {
        "hrms_available": True,
        "date": str(day),
        "scope": grid["scope"],
        "grace_minutes": grid["grace_minutes"],
        "branches": branches,
        "totals": day_totals_for(every_cell),
    }


def get_employee(
    employee: str, from_date: Optional[str] = None, to_date: Optional[str] = None
) -> Dict[str, Any]:
    """One person's range, day by day, with the raw check-ins behind it.

    The raw rows are returned alongside the derived days because every dispute
    about a derived status ends with somebody asking what the clock actually
    recorded, and making them open Desk to find out defeats the screen.

    Carries the same ``scope`` block as the other three reads. Without it this
    tab could not tell "your branch scope resolved to nothing" apart from "this
    person has no days in this range" -- two states that render identically as
    an empty screen and need opposite fixes (one is a POS Profile mapping, the
    other is a different date range).
    """
    start, end = _range_bounds(from_date, to_date, MAX_EMPLOYEE_RANGE_DAYS)
    if not hrms_available():
        return {
            "hrms_available": False,
            "notice": _hrms_notice(),
            "employee": employee,
            "employee_name": employee,
            "designation": None,
            "department": None,
            "is_courier": False,
            "shift_locations": [],
            "from_date": str(start),
            "to_date": str(end),
            "scope": scope_payload(None),
            "grace_minutes": grace_minutes(),
            "days": [],
            "totals": totals_for([]),
            "by_branch": [],
            "checkins": [],
        }

    grid = build_grid(start, end, employee=employee)
    meta = next(
        (row for row in grid["employees"] if row["employee"] == employee),
        {
            "employee": employee,
            "employee_name": frappe.db.get_value("Employee", employee, "employee_name")
            or employee,
            "designation": None,
            "department": None,
            "shift_locations": [],
            "is_courier": False,
        },
    )
    cells = grid["cells"].get(employee, {})
    days = [cells[key] for key in sorted(cells.keys())]

    by_branch: Dict[Optional[str], Dict[str, Any]] = {}
    for cell in days:
        location = cell.get("shift_location")
        bucket = by_branch.setdefault(location, {"shift_location": location, "_cells": []})
        bucket["_cells"].append(cell)

    branch_rows = []
    for bucket in by_branch.values():
        totals = totals_for(bucket["_cells"])
        branch_rows.append(
            {
                "shift_location": bucket["shift_location"],
                "rostered_days": totals["rostered_days"],
                "present_days": totals["present_days"],
                "late_days": totals["late_days"],
                "absent_days": totals["absent_days"],
                "worked_hours": totals["worked_hours"],
            }
        )
    branch_rows.sort(key=_branch_sort_key)

    locations = shift_location_map()
    checkins: List[Dict[str, Any]] = []
    for (name, day_key), group in grid["groups"].items():
        if name != employee or day_key not in cells:
            continue
        location_row = locations.get(cells[day_key].get("shift_location") or "")
        for raw in group["rows"]:
            lat = _float_or_none(raw.get("latitude"))
            lng = _float_or_none(raw.get("longitude"))
            checkins.append(
                {
                    "name": raw.get("name"),
                    "time": _dt_text(raw.get("time")),
                    "log_type": raw.get("log_type") or None,
                    "shift": raw.get("shift") or None,
                    "offshift": bool(raw.get("offshift")),
                    "latitude": lat,
                    "longitude": lng,
                    "geo_ok": _geo_ok([(lat, lng)] if lat is not None and lng is not None else [], location_row),
                }
            )
    checkins.sort(key=lambda r: r.get("time") or "")

    return {
        "hrms_available": True,
        "employee": employee,
        "employee_name": meta["employee_name"],
        "designation": meta["designation"],
        "department": meta["department"],
        "is_courier": meta["is_courier"],
        "shift_locations": meta["shift_locations"],
        "from_date": str(start),
        "to_date": str(end),
        "scope": grid["scope"],
        "grace_minutes": grid["grace_minutes"],
        "days": days,
        "totals": totals_for(days),
        "by_branch": branch_rows,
        "checkins": checkins,
    }


def _range_bounds(
    from_date: Optional[str],
    to_date: Optional[str],
    max_days: Optional[int] = None,
) -> Tuple[date_cls, date_cls]:
    """Default to the current month; tolerate a reversed pair by swapping it.

    A reversed pair is a slip worth forgiving; an over-long one is not, because
    serving it can take the server down (see ``MAX_SUMMARY_RANGE_DAYS``). It is
    refused rather than silently truncated: a report quietly covering less than
    the manager asked for would be read as the whole period.
    """
    if from_date and to_date:
        start, end = getdate(from_date), getdate(to_date)
    elif from_date:
        start = getdate(from_date)
        end = getdate(get_last_day(start))
    elif to_date:
        end = getdate(to_date)
        start = getdate(get_first_day(end))
    else:
        today = getdate()
        start, end = getdate(get_first_day(today)), getdate(get_last_day(today))
    if end < start:
        start, end = end, start
    if max_days is not None:
        _refuse_long_range(start, end, max_days)
    return start, end


def _refuse_long_range(start: date_cls, end: date_cls, max_days: int) -> None:
    span = (getdate(end) - getdate(start)).days + 1
    if span > max_days:
        frappe.throw(
            _("That range covers {0} days. Choose a range of {1} days or fewer.").format(
                span, max_days
            )
        )


GROUP_BY_CHOICES = ("branch", "employee", "day")


def get_summary(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    group_by: str = "branch",
    shift_location: Optional[str] = None,
) -> Dict[str, Any]:
    """The same cells, rolled up three different ways.

    All three groupings share one pass over one grid, so a branch total, an
    employee total and a day total for the same range are arithmetically the
    same numbers -- three separate queries would drift the moment one of them
    filtered differently.

    Guarantees on ``key`` and ``label``:

    * ``group_by="day"`` -> ``key`` is a bare ISO ``YYYY-MM-DD`` and ``label`` is
      the same string. The client renders the date itself. This is an
      Arabic-first UI: a date formatted on the server follows the server's
      locale, not the app's, and a formatted key cannot be parsed back into a
      date to drill into.
    * ``group_by="branch"`` -> ``key`` is the Shift Location name, or ``None``
      for the unattributable bucket; ``label`` is a display string and IS
      translated ("No branch").
    * ``group_by="employee"`` -> ``key`` is the employee id and ``label`` is
      their name.
    """
    group_by = (group_by or "branch").strip().lower()
    if group_by not in GROUP_BY_CHOICES:
        group_by = "branch"
    start, end = _range_bounds(from_date, to_date, MAX_SUMMARY_RANGE_DAYS)

    if not hrms_available():
        empty = totals_for([])
        empty["avg_late_minutes"] = 0.0
        empty["employees"] = 0
        return {
            "hrms_available": False,
            "notice": _hrms_notice(),
            "from_date": str(start),
            "to_date": str(end),
            "group_by": group_by,
            "scope": scope_payload(None),
            "grace_minutes": grace_minutes(),
            "rows": [],
            "totals": empty,
        }

    grid = build_grid(start, end, shift_location=shift_location)
    meta_by_employee = {row["employee"]: row for row in grid["employees"]}

    buckets: Dict[Any, Dict[str, Any]] = {}
    every_cell: List[Dict[str, Any]] = []
    all_employees: Set[str] = set()

    for name, cells in grid["cells"].items():
        meta = meta_by_employee.get(name, {})
        for key in sorted(cells.keys()):
            cell = cells[key]
            every_cell.append(cell)
            all_employees.add(name)

            if group_by == "branch":
                bucket_key = cell.get("shift_location")
                label = bucket_key or _("No branch")
                row_employee = None
                row_location = bucket_key
            elif group_by == "employee":
                bucket_key = name
                label = meta.get("employee_name") or name
                row_employee = name
                row_location = (meta.get("shift_locations") or [None])[0]
            else:
                # ``key`` is the cell's own ISO date, already "YYYY-MM-DD".
                # Formatting it here would be wrong twice over: this is an
                # Arabic-first UI, so a server-rendered date would not follow the
                # app's locale, and a formatted key cannot be parsed back. The
                # label is the same bare string for the same reason -- the client
                # formats it.
                bucket_key = str(getdate(key))
                label = bucket_key
                row_employee = None
                row_location = None

            bucket = buckets.setdefault(
                bucket_key,
                {
                    "key": bucket_key,
                    "label": label,
                    "employee": row_employee,
                    "shift_location": row_location,
                    "_cells": [],
                    "_employees": set(),
                },
            )
            bucket["_cells"].append(cell)
            bucket["_employees"].add(name)

    rows: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        totals = totals_for(bucket["_cells"])
        totals["avg_late_minutes"] = (
            round(totals["late_minutes"] / totals["late_days"], 2) if totals["late_days"] else 0.0
        )
        row = {
            "key": bucket["key"],
            "label": bucket["label"],
            "employee": bucket["employee"],
            "shift_location": bucket["shift_location"],
            "employees": len(bucket["_employees"]),
        }
        row.update(totals)
        rows.append(row)

    if group_by == "day":
        rows.sort(key=lambda r: str(r["key"]))
    elif group_by == "branch":
        rows.sort(key=_branch_sort_key)
    else:
        rows.sort(key=lambda r: (r["label"] or "").lower())

    totals = totals_for(every_cell)
    totals["avg_late_minutes"] = (
        round(totals["late_minutes"] / totals["late_days"], 2) if totals["late_days"] else 0.0
    )
    totals["employees"] = len(all_employees)

    return {
        "hrms_available": True,
        "from_date": str(start),
        "to_date": str(end),
        "group_by": group_by,
        "scope": grid["scope"],
        "grace_minutes": grid["grace_minutes"],
        "rows": rows,
        "totals": totals,
    }

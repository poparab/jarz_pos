"""Shift distribution: the mechanics behind the monthly roster screen.

The screen this serves is a month calendar of branch staff. A line manager
opens it, sees who is on which shift on which day, marks somebody off, and
names the colleague who absorbs the day on a longer shift. Everything it writes
lands in **HRMS Shift Assignments**, because that is the record the geofenced
check-in already reads -- so a change made here is a change to who can clock in
where, with no second source of truth to drift.

Why this module re-implements HRMS's own ``hrms/api/roster.py`` mechanics
---------------------------------------------------------------------------
``hrms.api.roster`` is gated on ``frappe.has_permission``, which is NOT bypassed
by ``frappe.flags.ignore_permissions`` -- only ``Administrator`` short-circuits
it. Its callers therefore need HR Manager or HR User, and our line managers hold
neither. The three ways out, and why this one:

* Grant line managers the HR roles -- hands them the entire HR module
  (salaries, contracts, appraisals) to edit a rota. Far too wide.
* Add Custom DocPerm rows for ``JARZ line manager`` on Shift Assignment -- a
  single custom row on a DocType REPLACES the standard permission set for every
  other role on it, so this would quietly revoke HR Manager's own access to
  Shift Assignment. That failure mode has already bitten this codebase once.
* Re-implement the break/insert mechanics here, running the writes with
  ``ignore_permissions`` and making *our* role gate the authority. Same pattern
  as the custom-shipping approval path. This is what the module does.

``_break_assignment`` / ``_insert_assignment`` mirror upstream ``break_shift`` /
``insert_shift`` step for step, including dropping the
``shift_schedule_assignment`` link on the tail half of a broken assignment.
Deviating there would let a Shift Schedule re-generate a day this module had
already carved out.

The check-in consequence
------------------------
HRMS resolves a check-in's shift by looking for a submitted Active Shift
Assignment covering the moment. When it finds none it sets ``offshift=1`` and
the geofence lookup then finds no location and **returns clean** -- so an
unrostered person could check in from anywhere, silently. That inversion is why
``events/employee_checkin`` refuses a check-in on a day this module has emptied.
Removing a shift here is a real access change, not a display change.
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_first_day, get_last_day, getdate

from jarz_pos.constants import ROLES
from jarz_pos.utils.settings_utils import single_float

SETTINGS = "Jarz POS Settings"

DAY_OFF_DOCTYPE = "Jarz Roster Day Off"

#: Filters a Shift Assignment must match to count as "this person works today".
#: Mirrors ``EmployeeCheckin.validate_distance_from_shift_location`` exactly --
#: if these two disagree, the roster will show a shift the geofence does not
#: honour, or refuse a check-in the roster says is fine. Used for every WRITE:
#: an expired assignment must never be broken or extended.
ACTIVE_ASSIGNMENT_FILTERS = {"docstatus": 1, "status": "Active"}

#: Statuses the month READ accepts. Deliberately wider than the write filter.
#:
#: HRMS runs ``mark_expired_shift_assignments_as_inactive`` daily, so every
#: assignment whose end date has passed flips to ``Inactive``. Reading with the
#: Active-only filter therefore makes all history vanish: last month's calendar
#: comes back empty and the payroll hours for the month somebody actually wants
#: to pay for read zero. ``Inactive`` here means "already happened", not
#: "cancelled" -- cancelled is ``docstatus=2``, which is still excluded.
READABLE_ASSIGNMENT_STATUSES = ("Active", "Inactive")

#: Link on POS Profile that maps a POS branch to its HR Shift Location. Seeded
#: by ``setup/roster_setup.py``; see ``allowed_shift_locations``.
POS_PROFILE_LOCATION_FIELD = "custom_shift_location"

#: Per-employee override for "what a normal day is for this person", in hours.
#: Unset (0) means derive it from their Shift Schedule -- see ``standard_hours``.
EMPLOYEE_STANDARD_HOURS_FIELD = "custom_roster_standard_hours"

#: Default length of a normal working day when neither the employee override
#: nor a Shift Schedule can answer. Dispatchers are on 9h, which is the most
#: common case here, so it is the least surprising floor.
DEFAULT_STANDARD_HOURS = 9.0

#: Overtime credit multipliers. A courier's overtime hour counts double; a
#: dispatcher's counts once. Both are settings so payroll can move them without
#: a deploy.
DEFAULT_COURIER_OT_MULTIPLIER = 2.0
DEFAULT_STANDARD_OT_MULTIPLIER = 1.0

#: Designation substrings that mark somebody as a courier for overtime purposes.
#: Substring rather than exact match because real Designation records drift
#: ("Courier", "Courier - Nasr City", "Delivery Courier").
DEFAULT_COURIER_DESIGNATIONS = "courier,driver,delivery"


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def has_roster_access() -> bool:
    """True for the line-manager tier and above.

    Deliberately the same set as the Manager Dashboard rather than a narrower
    roster-specific one: the roster is surfaced on that dashboard, and a gate
    narrower than the screen that links to it is always a dead button.
    """
    roles = {str(r or "").strip() for r in (frappe.get_roles() or []) if str(r or "").strip()}
    return bool(roles.intersection(ROLES.ADMIN | ROLES.LINE_MANAGER_TIER))


def ensure_roster_access() -> None:
    if not has_roster_access():
        frappe.throw(_("Not permitted: shift distribution access required"), frappe.PermissionError)


def is_unrestricted() -> bool:
    """Administrator alone sees every branch regardless of POS Profile.

    Matches ``utils.access_control.get_user_pos_profiles``: branch membership is
    the POS Profile User child table for *everyone*, managers included. That was
    a deliberate decision and is not reversed here.
    """
    return frappe.session.user == "Administrator"


def allowed_shift_locations() -> Optional[Set[str]]:
    """Shift Locations this user may roster, or ``None`` meaning "all".

    Resolution order:

    1. ``Administrator`` -> ``None`` (everything).
    2. The Shift Locations mapped from the caller's POS Profiles, PLUS any
       location no POS Profile claims at all when the caller holds the manager
       tier -- see ``_unbranched_locations``.
    3. If **no** POS Profile anywhere carries a mapping, ``None`` with the
       caller's screen shown a notice.

    Step 3 exists because the mapping is seeded on migrate by name matching and
    can legitimately come up empty on a site whose Shift Locations are named
    differently from its POS Profiles. Without the fallback the feature would
    ship dead -- every line manager would open an empty month and have no way to
    discover that a field they have never heard of needs filling in. The
    fallback widens a *view* for users who already hold the line-manager tier,
    and ``roster_scope_configured`` reports it so the screen can say so.
    """
    if is_unrestricted():
        return None

    if not _pos_profile_location_field_exists():
        return None

    from jarz_pos.utils.access_control import get_user_pos_profiles

    profiles = get_user_pos_profiles() or []
    mapped = {
        loc
        for loc in (
            frappe.db.get_value("POS Profile", p, POS_PROFILE_LOCATION_FIELD) for p in profiles
        )
        if loc
    }

    allowed = set(mapped) | _unbranched_locations()
    if allowed:
        return allowed

    if not _any_pos_profile_mapped():
        return None

    return set()


def _unbranched_locations() -> Set[str]:
    """Shift Locations no POS Profile claims, for the manager tier only.

    The Factory is the case that forced this. It is a production site, not a
    sales branch, so it has no POS Profile and never will -- which meant branch
    scoping could not express it and its three staff were invisible to every
    manager except Administrator. Nobody could roster the factory at all, while
    the screen gave no hint that a quarter of the workforce was missing.

    Creating a dummy POS Profile to unlock it would be worse: a POS Profile is
    a real branch everywhere else in this app (order routing, the Kanban board,
    cash drawers), so inventing one would leak a phantom branch into all of
    them.

    A location that belongs to no branch belongs to the company, so the manager
    tier sees it and a *branch* line manager does not. This does not reverse the
    "branch membership is the POS Profile User table for everyone" decision --
    that rule is about which branch's ORDERS you may touch, and an unbranched HR
    location has no orders. Adding a POS Profile mapping later removes the
    location from this set automatically.
    """
    roles = {str(r or "").strip() for r in (frappe.get_roles() or []) if str(r or "").strip()}
    if not roles.intersection(ROLES.MANAGER | ROLES.ADMIN):
        return set()

    try:
        claimed = {
            loc
            for loc in frappe.get_all(
                "POS Profile",
                filters={POS_PROFILE_LOCATION_FIELD: ("is", "set")},
                pluck=POS_PROFILE_LOCATION_FIELD,
            )
            if loc
        }
        return {loc for loc in frappe.get_all("Shift Location", pluck="name") if loc not in claimed}
    except Exception:
        return set()


def roster_scope_configured() -> bool:
    """Whether POS Profile -> Shift Location mapping has been set up at all."""
    return _pos_profile_location_field_exists() and _any_pos_profile_mapped()


def _pos_profile_location_field_exists() -> bool:
    try:
        return bool(frappe.get_meta("POS Profile").get_field(POS_PROFILE_LOCATION_FIELD))
    except Exception:
        return False


def _any_pos_profile_mapped() -> bool:
    try:
        return bool(
            frappe.get_all(
                "POS Profile",
                filters={POS_PROFILE_LOCATION_FIELD: ("is", "set")},
                limit=1,
            )
        )
    except Exception:
        return False


def hrms_available() -> bool:
    """True when HRMS's shift tables are present.

    Every entry point degrades to an explained empty answer rather than a stack
    trace when HRMS is absent, exactly as ``utils.employee_link`` does -- a
    bench without HRMS still has to migrate and still has to serve the POS.
    """
    try:
        return bool(frappe.db.exists("DocType", "Shift Assignment"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Shift catalogue and hours
# ---------------------------------------------------------------------------


def _as_timedelta(value: Any) -> Optional[timedelta]:
    """Frappe returns a ``Time`` field as ``timedelta``, not ``time``.

    Reading one as if it were a ``datetime.time`` (``.hour``) raises, and
    comparing it against one silently never matches -- both have bitten this
    codebase before, so every read of a Shift Type time goes through here.
    """
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    try:
        parts = str(value).split(":")
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        seconds = int(float(parts[2])) if len(parts) > 2 else 0
        return timedelta(hours=hours, minutes=minutes, seconds=seconds)
    except Exception:
        return None


def _time_text(value: Any) -> str:
    """Render a Time field for the client, keeping midnight visible.

    ``str(value or "")`` loses ``timedelta(0)`` because it is falsy -- the trap
    that once shipped every midnight delivery slot as no slot at all. Only
    ``None`` means "no time here".
    """
    if value is None:
        return ""
    return str(value)


def shift_length_hours(start_time: Any, end_time: Any) -> float:
    """Length of a shift in hours, correct across midnight.

    Every branch shift here ends after midnight (12:30 -> 01:00), so a naive
    ``end - start`` would report a *negative* twelve-and-a-half hour shift as
    minus eleven and a half. Wrapping to the next day is the normal case, not
    the edge case.
    """
    start = _as_timedelta(start_time)
    end = _as_timedelta(end_time)
    if start is None or end is None:
        return 0.0
    delta = end - start
    if delta.total_seconds() <= 0:
        delta += timedelta(days=1)
    return round(delta.total_seconds() / 3600.0, 4)


def shift_catalog() -> List[Dict[str, Any]]:
    """Every Shift Type, with its computed length.

    The screen's shift picker is built from this rather than from a hard-coded
    list of "9h / 10h / 12h", so adding a shift type in Desk is enough to make
    it assignable. The lengths are computed, never stored, so a Shift Type whose
    hours are edited cannot leave a stale number behind on the roster.
    """
    if not hrms_available():
        return []

    rows = frappe.get_all(
        "Shift Type",
        fields=["name", "start_time", "end_time", "color", "holiday_list"],
        order_by="name",
    )
    catalog: List[Dict[str, Any]] = []
    for row in rows:
        catalog.append(
            {
                "shift_type": row["name"],
                # `or ""` would be wrong here: a Time field comes back as a
                # timedelta, and midnight is timedelta(0), which is FALSY. The
                # 6 Oct/Dokki Friday courier shift ends at 00:00 and would come
                # out with a blank end time, rendering as "14:30 -> " in the
                # picker while its computed length stayed correct.
                "start_time": _time_text(row.get("start_time")),
                "end_time": _time_text(row.get("end_time")),
                "hours": shift_length_hours(row.get("start_time"), row.get("end_time")),
                "color": row.get("color") or None,
                "holiday_list": row.get("holiday_list") or None,
            }
        )
    return catalog


def _shift_hours_map() -> Dict[str, float]:
    return {row["shift_type"]: row["hours"] for row in shift_catalog()}


def shift_locations() -> List[Dict[str, Any]]:
    if not hrms_available():
        return []
    try:
        rows = frappe.get_all(
            "Shift Location",
            fields=["name", "checkin_radius", "latitude", "longitude"],
            order_by="name",
        )
    except Exception:
        return []
    return [
        {
            "shift_location": r["name"],
            "checkin_radius": r.get("checkin_radius") or 0,
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Overtime classification
# ---------------------------------------------------------------------------


def _courier_designation_tokens() -> List[str]:
    raw = frappe.db.get_single_value(SETTINGS, "roster_courier_designations")
    text = str(raw or "").strip() or DEFAULT_COURIER_DESIGNATIONS
    tokens = [t.strip().lower() for t in text.replace("\n", ",").split(",")]
    return [t for t in tokens if t]


def is_courier_designation(designation: Optional[str]) -> bool:
    text = str(designation or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in _courier_designation_tokens())


def is_courier(employee: Optional[str], designation: Optional[str] = None) -> bool:
    """Whether this person's overtime is paid at the courier rate.

    Designation alone is not enough, and on this site it answers nothing at all:
    every Active Employee on staging and production has ``designation`` unset,
    so a designation-only test classified all four couriers as dispatchers and
    silently paid their overtime at half rate -- the exact number this feature
    exists to get right.

    The shift they are scheduled on is the signal that actually carries the
    fact. The courier shift types are named for the job ("Courier Nasr City",
    "Courier 6Oct-Dokki"), so the same token list matches them. Designation is
    still checked first, so filling it in later takes precedence and needs no
    code change.
    """
    if is_courier_designation(designation):
        return True
    if not employee:
        return False
    tokens = _courier_designation_tokens()
    return any(
        any(token in shift_type.lower() for token in tokens)
        for shift_type in _scheduled_shift_types(employee)
    )


def overtime_multiplier(employee: Optional[str], designation: Optional[str] = None) -> float:
    """Credit per overtime hour: 2x for couriers, 1x for everyone else.

    The rule came from the owner directly -- a courier's extra hour is paid as
    two, a dispatcher's as one -- and it is applied to *rostered* overtime (the
    gap between the shift they were put on and their normal day), not to
    clock-derived hours. Auto-attendance is switched off on both servers, so
    check-in pairs are not a reliable source for a payroll number; the roster is.
    """
    if is_courier(employee, designation):
        return single_float(SETTINGS, "roster_courier_overtime_multiplier", DEFAULT_COURIER_OT_MULTIPLIER)
    return single_float(SETTINGS, "roster_default_overtime_multiplier", DEFAULT_STANDARD_OT_MULTIPLIER)


def standard_hours(employee: str, employee_row: Optional[Dict[str, Any]] = None) -> float:
    """The length of a normal day for this person.

    Order: explicit per-employee override, then the shift type on their enabled
    Shift Schedule Assignment (which *is* the definition of their normal shift),
    then the site default. Derivation beats a stored number here because a
    schedule change should move the overtime baseline with it.
    """
    row = employee_row
    if row is None:
        fields = ["name", "designation"]
        if _employee_standard_hours_field_exists():
            fields.append(EMPLOYEE_STANDARD_HOURS_FIELD)
        row = frappe.db.get_value("Employee", employee, fields, as_dict=True) or {}

    override = flt(row.get(EMPLOYEE_STANDARD_HOURS_FIELD) or 0)
    if override > 0:
        return float(override)

    scheduled = _scheduled_shift_hours(employee)
    if scheduled > 0:
        return scheduled

    return single_float(SETTINGS, "roster_default_standard_hours", DEFAULT_STANDARD_HOURS)


def _employee_standard_hours_field_exists() -> bool:
    try:
        return bool(frappe.get_meta("Employee").get_field(EMPLOYEE_STANDARD_HOURS_FIELD))
    except Exception:
        return False


def _scheduled_shift_types(employee: str) -> List[str]:
    """Shift types behind this employee's enabled schedules.

    Cached per request: it is read once for the overtime baseline and again for
    the courier test, for every employee in the month, and the month read is
    already the heaviest query on this screen.
    """
    cache = getattr(frappe.local, "_jarz_roster_sched_types", None)
    if cache is None:
        cache = {}
        frappe.local._jarz_roster_sched_types = cache
    if employee in cache:
        return cache[employee]

    shift_types: List[str] = []
    try:
        schedules = frappe.get_all(
            "Shift Schedule Assignment",
            filters={"employee": employee, "enabled": 1},
            pluck="shift_schedule",
        )
        for schedule in schedules:
            shift_type = frappe.db.get_value("Shift Schedule", schedule, "shift_type")
            if shift_type:
                shift_types.append(shift_type)
    except Exception:
        shift_types = []

    cache[employee] = shift_types
    return shift_types


def _scheduled_shift_hours(employee: str) -> float:
    """Hours of the shift type behind this employee's enabled schedule.

    Returns the *shortest* when several are enabled: a person with both a
    weekday and a Friday schedule has two baselines, and taking the shorter one
    means a cover day is never accidentally scored as zero overtime.
    """
    hours_map = _shift_hours_map()
    lengths = [
        hours_map[shift_type]
        for shift_type in _scheduled_shift_types(employee)
        if hours_map.get(shift_type)
    ]
    return min(lengths) if lengths else 0.0


# ---------------------------------------------------------------------------
# Assignment primitives -- upstream break/insert, run under our own gate
# ---------------------------------------------------------------------------


def _create_assignment(
    employee: str,
    company: str,
    shift_type: str,
    start_date: Any,
    end_date: Any,
    status: str = "Active",
    shift_location: Optional[str] = None,
) -> str:
    """``hrms...create_shift_assignment`` with permissions bypassed.

    Upstream saves and submits under the caller's own rights, which a line
    manager does not have on Shift Assignment. The API gate in
    ``api/roster.py`` is the authority instead; see the module docstring for
    why widening the DocPerms was rejected.

    ``owner`` is left as the real session user on purpose -- impersonating
    Administrator would have made the writes go through unmodified upstream
    code, but every Shift Assignment would then be attributed to Administrator
    and the roster would lose its audit trail.
    """
    assignment = frappe.new_doc("Shift Assignment")
    assignment.employee = employee
    assignment.company = company
    assignment.shift_type = shift_type
    assignment.start_date = getdate(start_date)
    assignment.end_date = getdate(end_date) if end_date else None
    assignment.status = status
    assignment.shift_location = shift_location
    assignment.flags.ignore_permissions = True
    assignment.save(ignore_permissions=True)
    assignment.submit()
    return assignment.name


def _assignment_on(employee: str, on_date: Any) -> Optional[frappe._dict]:
    """The Active submitted assignment covering ``on_date``, if any."""
    if not hrms_available():
        return None
    day = getdate(on_date)
    rows = frappe.get_all(
        "Shift Assignment",
        filters=dict(
            ACTIVE_ASSIGNMENT_FILTERS,
            employee=employee,
            start_date=("<=", day),
        ),
        or_filters=[["end_date", ">=", day], ["end_date", "is", "not set"]],
        fields=[
            "name",
            "employee",
            "company",
            "shift_type",
            "shift_location",
            "start_date",
            "end_date",
            "status",
            "shift_schedule_assignment",
        ],
        order_by="start_date desc",
        limit=1,
    )
    return frappe._dict(rows[0]) if rows else None


def _break_assignment(assignment_name: str, on_date: Any) -> None:
    """Carve one day out of an assignment range.

    Mirrors upstream ``hrms.api.roster.break_shift``: shorten the head, then
    re-create the tail from the day after. The ``shift_schedule_assignment``
    link is deliberately NOT carried onto the tail, exactly as upstream does --
    keeping it would let the parent Shift Schedule regenerate the very day that
    was just carved out.
    """
    doc = frappe.get_doc("Shift Assignment", assignment_name)
    day = getdate(on_date)

    if doc.end_date and getdate(doc.end_date) < day:
        frappe.throw(_("Cannot change a shift after its end date."))
    if getdate(doc.start_date) > day:
        frappe.throw(_("Cannot change a shift before its start date."))

    employee = doc.employee
    company = doc.company
    shift_type = doc.shift_type
    status = doc.status
    end_date = doc.end_date
    shift_location = doc.shift_location

    if getdate(doc.start_date) == day:
        doc.flags.ignore_permissions = True
        doc.cancel()
        doc.delete(ignore_permissions=True)
    else:
        doc.end_date = add_days(day, -1)
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)

    if not end_date or getdate(end_date) > day:
        _create_assignment(
            employee, company, shift_type, add_days(day, 1), end_date, status, shift_location
        )


def _insert_assignment(
    employee: str,
    company: str,
    shift_type: str,
    start_date: Any,
    end_date: Any,
    status: str = "Active",
    shift_location: Optional[str] = None,
) -> None:
    """Add an assignment, merging with identical neighbours.

    Mirrors upstream ``insert_shift``. The merge matters for more than tidiness:
    without it a month of single-day edits leaves thirty separate assignment
    rows per person, and ``validate_overlapping_shifts`` has to scan all of them
    on every subsequent write.
    """
    start = getdate(start_date)
    end = getdate(end_date) if end_date else None

    filters = {
        "doctype": "Shift Assignment",
        "employee": employee,
        "company": company,
        "shift_type": shift_type,
        "status": status,
        "shift_location": shift_location,
        "docstatus": ("!=", 2),
    }
    prev_shift = frappe.db.exists(dict({"end_date": add_days(start, -1)}, **filters))
    next_shift = frappe.db.exists(dict({"start_date": add_days(end, 1)}, **filters)) if end else None

    if prev_shift:
        if next_shift:
            end = frappe.db.get_value("Shift Assignment", next_shift, "end_date")
            frappe.db.set_value("Shift Assignment", next_shift, "docstatus", 2)
            frappe.delete_doc("Shift Assignment", next_shift, ignore_permissions=True)
        frappe.db.set_value("Shift Assignment", prev_shift, "end_date", end or None)
    elif next_shift:
        frappe.db.set_value("Shift Assignment", next_shift, "start_date", start)
    else:
        _create_assignment(employee, company, shift_type, start, end, status, shift_location)


def _employee_company(employee: str) -> str:
    company = frappe.db.get_value("Employee", employee, "company")
    if not company:
        frappe.throw(_("Employee {0} has no Company set.").format(employee))
    return company


def _schedule_location(employee: str) -> Optional[str]:
    """The branch this employee's baseline schedule geofences them to.

    Used as the fallback location when a day is assigned to somebody who has no
    assignment on that date at all. Getting this wrong is not cosmetic: the
    Shift Location is what the check-in radius is measured against, and an
    assignment created without one silently disables the geofence for that day.
    """
    try:
        rows = frappe.get_all(
            "Shift Schedule Assignment",
            filters={"employee": employee, "enabled": 1},
            fields=["shift_location"],
            limit=1,
        )
    except Exception:
        return None
    return (rows[0].get("shift_location") if rows else None) or None


# ---------------------------------------------------------------------------
# Day-level operations
# ---------------------------------------------------------------------------


def set_shift_for_day(
    employee: str,
    on_date: Any,
    shift_type: Optional[str],
    shift_location: Optional[str] = None,
    clear_conflicting_day_off: bool = True,
) -> Dict[str, Any]:
    """Put one employee on one shift for exactly one day.

    ``shift_type=None`` empties the day instead. Emptying is what a day off
    does underneath, and -- because an empty day now refuses check-ins -- it is
    the one operation here that can lock a real person out, so callers reach it
    through ``set_day_off`` (which records why) rather than by passing None.

    Rostering somebody onto a day they are marked off DISCARDS that day-off
    record. The two states contradict each other, and leaving both would be
    read as "off" by the check-in gate while the calendar showed a shift --
    the person would be refused at the door by a screen that said they were
    working. ``clear_day_off`` passes ``False`` here because it deletes the
    record itself once both people are restored.
    """
    day = getdate(on_date)
    if clear_conflicting_day_off and shift_type:
        _discard_day_off_record(employee, day)
    existing = _assignment_on(employee, day)

    resolved_location = (
        shift_location
        or (existing.shift_location if existing else None)
        or _schedule_location(employee)
    )

    if existing and existing.shift_type == shift_type and existing.shift_location == resolved_location:
        return {"changed": False, "shift_type": shift_type, "shift_location": resolved_location}

    previous_shift_type = existing.shift_type if existing else None
    previous_location = existing.shift_location if existing else None

    if existing:
        _break_assignment(existing.name, day)

    if shift_type:
        _insert_assignment(
            employee,
            _employee_company(employee),
            shift_type,
            day,
            day,
            "Active",
            resolved_location,
        )

    return {
        "changed": True,
        "shift_type": shift_type,
        "shift_location": resolved_location,
        "previous_shift_type": previous_shift_type,
        "previous_shift_location": previous_location,
    }


def _discard_day_off_record(employee: str, day: Any) -> None:
    """Delete a day-off row without touching any Shift Assignment.

    Distinct from ``clear_day_off``, which also restores both people's shifts.
    Here the caller is already writing the shift, so restoring would fight it.
    """
    name = frappe.db.exists(DAY_OFF_DOCTYPE, {"employee": employee, "off_date": getdate(day)})
    if name:
        frappe.delete_doc(DAY_OFF_DOCTYPE, name, ignore_permissions=True)


def set_day_off(
    employee: str,
    off_date: Any,
    off_type: str = "Weekly Off",
    covered_by: Optional[str] = None,
    cover_shift_type: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark somebody off, optionally moving a colleague onto a cover shift.

    The two halves are one operation on purpose. A branch day that loses one of
    its two overlapping shifts is not covered by shortening the rota -- the
    remaining person has to stretch onto the full-day shift, and doing that as a
    separate second action leaves a window where the branch is rostered
    half-open. Recording the pair together is also what makes the undo exact.
    """
    day = getdate(off_date)
    original = _assignment_on(employee, day)

    cover_previous: Optional[frappe._dict] = None
    if covered_by:
        if not cover_shift_type:
            frappe.throw(_("Pick the shift the covering colleague moves onto."))
        cover_previous = _assignment_on(covered_by, day)

    # Empty the day first. If the cover step then fails, the employee is off and
    # nobody is covering -- visible on the calendar as a hole and reported by
    # the coverage warning, which is a far safer failure than the reverse
    # (somebody on a 12h cover shift for a colleague who is still rostered).
    if original:
        _break_assignment(original.name, day)

    cover_result: Optional[Dict[str, Any]] = None
    if covered_by:
        cover_result = set_shift_for_day(
            covered_by,
            day,
            cover_shift_type,
            (cover_previous.shift_location if cover_previous else None)
            or (original.shift_location if original else None),
        )

    doc = frappe.new_doc(DAY_OFF_DOCTYPE)
    doc.employee = employee
    doc.off_date = day
    doc.off_type = off_type or "Weekly Off"
    doc.shift_location = original.shift_location if original else _schedule_location(employee)
    doc.original_shift_type = original.shift_type if original else None
    doc.original_shift_location = original.shift_location if original else None
    doc.covered_by = covered_by or None
    doc.cover_shift_type = cover_shift_type if covered_by else None
    doc.cover_previous_shift_type = cover_previous.shift_type if cover_previous else None
    doc.cover_shift_location = (cover_result or {}).get("shift_location") if covered_by else None
    doc.notes = notes or None
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)

    return {
        "day_off": doc.name,
        "employee": employee,
        "off_date": str(day),
        "covered_by": covered_by,
        "cover_shift_type": cover_shift_type if covered_by else None,
    }


def clear_day_off(employee: str, off_date: Any) -> Dict[str, Any]:
    """Undo a day off, putting both people back on what they were on.

    Restoration reads the shift types recorded on the day-off row rather than
    recomputing them from the Shift Schedule. The schedule may have been edited
    since, and Friday sits on a different shift type from the rest of the week,
    so recomputation would quietly restore the wrong shift.
    """
    day = getdate(off_date)
    name = frappe.db.exists(DAY_OFF_DOCTYPE, {"employee": employee, "off_date": day})
    if not name:
        frappe.throw(_("{0} is not marked off on {1}.").format(employee, day))

    doc = frappe.get_doc(DAY_OFF_DOCTYPE, name)

    restored_employee = None
    if doc.original_shift_type:
        set_shift_for_day(
            employee,
            day,
            doc.original_shift_type,
            doc.original_shift_location,
            clear_conflicting_day_off=False,
        )
        restored_employee = doc.original_shift_type

    restored_cover = None
    if doc.covered_by:
        # A cover with nothing recorded beforehand means the colleague had no
        # shift that day, so putting them "back" means emptying the day again --
        # not leaving them on the 12h cover shift they were lent.
        set_shift_for_day(
            doc.covered_by,
            day,
            doc.cover_previous_shift_type or None,
            doc.cover_shift_location,
            clear_conflicting_day_off=False,
        )
        restored_cover = doc.cover_previous_shift_type

    doc.flags.ignore_permissions = True
    doc.delete(ignore_permissions=True)

    return {
        "employee": employee,
        "off_date": str(day),
        "restored_shift_type": restored_employee,
        "cover_restored_shift_type": restored_cover,
    }


# ---------------------------------------------------------------------------
# Month read
# ---------------------------------------------------------------------------


def month_bounds(month: Optional[str]) -> Tuple[date_cls, date_cls]:
    """First and last day of ``YYYY-MM`` (or of the current month)."""
    anchor = getdate(f"{month}-01") if month else getdate()
    return getdate(get_first_day(anchor)), getdate(get_last_day(anchor))


def _daterange(start: date_cls, end: date_cls):
    current = start
    while current <= end:
        yield current
        current = add_days(current, 1)


def _holidays_by_employee(
    employees: List[str], start: date_cls, end: date_cls
) -> Dict[str, Set[str]]:
    """Holiday dates per employee, cached by holiday list.

    The factory is the case that makes this worth carrying: it is Friday-off
    while every branch works seven days, so without holidays the calendar would
    show factory staff as unrostered on Fridays -- which, now that an empty day
    refuses check-ins, would read as a fault rather than as a day off.
    """
    from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

    per_list: Dict[str, Set[str]] = {}
    out: Dict[str, Set[str]] = {}
    for employee in employees:
        try:
            holiday_list = get_holiday_list_for_employee(employee, raise_exception=False, as_on=end)
        except Exception:
            holiday_list = None
        if not holiday_list:
            continue
        if holiday_list not in per_list:
            rows = frappe.get_all(
                "Holiday",
                filters={"parent": holiday_list, "holiday_date": ("between", [start, end])},
                pluck="holiday_date",
            )
            per_list[holiday_list] = {str(getdate(d)) for d in rows}
        out[employee] = per_list[holiday_list]
    return out


def get_month(
    month: Optional[str] = None,
    shift_location: Optional[str] = None,
) -> Dict[str, Any]:
    """The whole calendar the screen draws: people down, days across.

    Returns one row per employee in scope with a per-date cell carrying the
    shift, its length, the branch it geofences to, and any day off. Cells are
    keyed by ISO date rather than by index so a partial month, a month with 28
    days and a month with 31 all render from the same shape.
    """
    if not hrms_available():
        return {
            "hrms_available": False,
            "notice": _("HRMS is not installed on this site, so there is no roster to show."),
            "employees": [],
            "shift_catalog": [],
            "shift_locations": [],
        }

    start, end = month_bounds(month)
    allowed = allowed_shift_locations()

    assignments = frappe.get_all(
        "Shift Assignment",
        filters={
            "docstatus": 1,
            "status": ("in", READABLE_ASSIGNMENT_STATUSES),
            "start_date": ("<=", end),
        },
        or_filters=[["end_date", ">=", start], ["end_date", "is", "not set"]],
        fields=[
            "name",
            "employee",
            "shift_type",
            "shift_location",
            "start_date",
            "end_date",
        ],
    )

    day_offs = frappe.get_all(
        DAY_OFF_DOCTYPE,
        filters={"off_date": ("between", [start, end])},
        fields=[
            "name",
            "employee",
            "off_date",
            "off_type",
            "shift_location",
            "covered_by",
            "covered_by_name",
            "cover_shift_type",
            "notes",
        ],
    )

    # An employee belongs to whichever branches their shifts point at. Reading
    # it from the assignments rather than from Employee.branch is deliberate:
    # the Shift Location is what the geofence actually measures against, so it
    # is the only branch value that can be wrong in a way that matters.
    locations_by_employee: Dict[str, Set[str]] = {}
    for row in assignments:
        if row.get("shift_location"):
            locations_by_employee.setdefault(row["employee"], set()).add(row["shift_location"])
    for row in day_offs:
        if row.get("shift_location"):
            locations_by_employee.setdefault(row["employee"], set()).add(row["shift_location"])

    candidates: Set[str] = {row["employee"] for row in assignments}
    candidates.update(row["employee"] for row in day_offs)
    try:
        for row in frappe.get_all(
            "Shift Schedule Assignment",
            filters={"enabled": 1},
            fields=["employee", "shift_location"],
        ):
            candidates.add(row["employee"])
            if row.get("shift_location"):
                locations_by_employee.setdefault(row["employee"], set()).add(row["shift_location"])
    except Exception:
        pass

    def in_scope(employee: str) -> bool:
        locs = locations_by_employee.get(employee, set())
        if shift_location and shift_location not in locs:
            return False
        if allowed is None:
            return True
        # Somebody with no Shift Location at all is invisible to a
        # branch-scoped manager on purpose -- an unlocated employee is not
        # geofenced anywhere, so no branch can claim them.
        return bool(locs.intersection(allowed))

    employee_names = sorted(e for e in candidates if in_scope(e))

    employee_rows: Dict[str, Dict[str, Any]] = {}
    if employee_names:
        fields = ["name", "employee_name", "designation", "department", "company", "status"]
        if _employee_standard_hours_field_exists():
            fields.append(EMPLOYEE_STANDARD_HOURS_FIELD)
        for row in frappe.get_all(
            "Employee", filters={"name": ("in", employee_names)}, fields=fields
        ):
            employee_rows[row["name"]] = row

    # Resigned staff keep their historic assignments; showing them on next
    # month's rota would invite somebody to roster a person who has left.
    employee_names = [e for e in employee_names if (employee_rows.get(e, {}).get("status") == "Active")]

    hours_map = _shift_hours_map()
    holidays = _holidays_by_employee(employee_names, start, end)

    cells: Dict[str, Dict[str, Dict[str, Any]]] = {e: {} for e in employee_names}
    for row in assignments:
        employee = row["employee"]
        if employee not in cells:
            continue
        first = max(getdate(row["start_date"]), start)
        last = min(getdate(row["end_date"]), end) if row.get("end_date") else end
        for day in _daterange(first, last):
            cells[employee][str(day)] = {
                "shift_type": row["shift_type"],
                "shift_location": row.get("shift_location"),
                "hours": hours_map.get(row["shift_type"], 0.0),
                "assignment": row["name"],
            }

    day_off_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in day_offs:
        key = (row["employee"], str(getdate(row["off_date"])))
        day_off_index[key] = row

    employees_payload: List[Dict[str, Any]] = []
    for employee in employee_names:
        row = employee_rows.get(employee, {})
        designation = row.get("designation")
        baseline = standard_hours(employee, row)
        multiplier = overtime_multiplier(employee, designation)
        employee_holidays = holidays.get(employee, set())

        days: Dict[str, Any] = {}
        for day in _daterange(start, end):
            key = str(day)
            cell = dict(cells[employee].get(key) or {})
            off = day_off_index.get((employee, key))
            cell["date"] = key
            cell["is_holiday"] = key in employee_holidays
            cell["day_off"] = (
                {
                    "name": off["name"],
                    "off_type": off.get("off_type"),
                    "covered_by": off.get("covered_by"),
                    "covered_by_name": off.get("covered_by_name"),
                    "cover_shift_type": off.get("cover_shift_type"),
                    "notes": off.get("notes"),
                }
                if off
                else None
            )
            cell.setdefault("shift_type", None)
            cell.setdefault("shift_location", None)
            cell.setdefault("hours", 0.0)
            days[key] = cell

        employees_payload.append(
            {
                "employee": employee,
                "employee_name": row.get("employee_name") or employee,
                "designation": designation,
                "department": row.get("department"),
                "shift_locations": sorted(locations_by_employee.get(employee, set())),
                "standard_hours": baseline,
                "is_courier": is_courier(employee, designation),
                "overtime_multiplier": multiplier,
                "days": days,
            }
        )

    return {
        "hrms_available": True,
        "month": f"{start.year:04d}-{start.month:02d}",
        "month_start": str(start),
        "month_end": str(end),
        "employees": employees_payload,
        "shift_catalog": shift_catalog(),
        "shift_locations": shift_locations(),
        "scope": {
            "configured": roster_scope_configured(),
            "locations": sorted(allowed) if allowed is not None else None,
            "unrestricted": allowed is None,
        },
        "uncovered": _uncovered_days(day_offs, employee_names),
    }


def _uncovered_days(day_offs: List[Dict[str, Any]], visible: List[str]) -> List[Dict[str, Any]]:
    """Days somebody is off with nobody named as covering.

    Surfaced because this is the mistake the screen exists to prevent: a branch
    whose second shift was removed and never handed to anyone reads as a normal
    calendar until the morning it opens short-staffed.
    """
    visible_set = set(visible)
    return [
        {
            "employee": row["employee"],
            "off_date": str(getdate(row["off_date"])),
            "off_type": row.get("off_type"),
            "shift_location": row.get("shift_location"),
        }
        for row in day_offs
        if row["employee"] in visible_set and not row.get("covered_by")
    ]


# ---------------------------------------------------------------------------
# Hours and overtime
# ---------------------------------------------------------------------------


def month_hours(
    month: Optional[str] = None,
    shift_location: Optional[str] = None,
) -> Dict[str, Any]:
    """Per-employee hours for a month, with overtime credited at their rate.

    Overtime is **rostered** overtime: the gap between the shift somebody was
    actually put on and the length of their normal day. It is not derived from
    check-in pairs, because auto-attendance is deliberately off on both servers
    -- Attendance records are never produced, and raw check-ins carry no
    reliable in/out pairing to subtract. The roster is the only complete record
    of who was asked to work how long, so it is what payroll gets.

    A courier's overtime hour is credited as two, a dispatcher's as one. Both
    ``worked_hours`` (real clock hours) and ``credited_hours`` (what payroll
    pays for) are returned, because the difference between them is exactly the
    thing somebody will query.
    """
    month_data = get_month(month=month, shift_location=shift_location)
    if not month_data.get("hrms_available"):
        return month_data

    rows: List[Dict[str, Any]] = []
    for employee in month_data["employees"]:
        baseline = flt(employee["standard_hours"])
        multiplier = flt(employee["overtime_multiplier"])

        worked_hours = 0.0
        base_hours = 0.0
        overtime_hours = 0.0
        worked_days = 0
        off_days = 0
        cover_days = 0
        by_shift: Dict[str, Dict[str, Any]] = {}

        for cell in employee["days"].values():
            if cell.get("day_off"):
                off_days += 1
            shift_type = cell.get("shift_type")
            if not shift_type:
                continue

            hours = flt(cell.get("hours"))
            worked_days += 1
            worked_hours += hours
            day_base = min(hours, baseline) if baseline > 0 else hours
            day_overtime = max(0.0, hours - baseline) if baseline > 0 else 0.0
            base_hours += day_base
            overtime_hours += day_overtime
            if day_overtime > 0:
                cover_days += 1

            bucket = by_shift.setdefault(shift_type, {"shift_type": shift_type, "days": 0, "hours": 0.0})
            bucket["days"] += 1
            bucket["hours"] = round(bucket["hours"] + hours, 2)

        credited_overtime = round(overtime_hours * multiplier, 2)
        rows.append(
            {
                "employee": employee["employee"],
                "employee_name": employee["employee_name"],
                "designation": employee["designation"],
                "shift_locations": employee["shift_locations"],
                "is_courier": employee["is_courier"],
                "standard_hours": baseline,
                "overtime_multiplier": multiplier,
                "worked_days": worked_days,
                "off_days": off_days,
                "cover_days": cover_days,
                "worked_hours": round(worked_hours, 2),
                "base_hours": round(base_hours, 2),
                "overtime_hours": round(overtime_hours, 2),
                "credited_overtime_hours": credited_overtime,
                "credited_hours": round(base_hours + credited_overtime, 2),
                "by_shift_type": sorted(by_shift.values(), key=lambda b: b["shift_type"]),
            }
        )

    rows.sort(key=lambda r: (r["employee_name"] or "").lower())
    return {
        "hrms_available": True,
        "month": month_data["month"],
        "month_start": month_data["month_start"],
        "month_end": month_data["month_end"],
        "scope": month_data["scope"],
        "rows": rows,
        "totals": {
            "employees": len(rows),
            "worked_hours": round(sum(r["worked_hours"] for r in rows), 2),
            "overtime_hours": round(sum(r["overtime_hours"] for r in rows), 2),
            "credited_hours": round(sum(r["credited_hours"] for r in rows), 2),
        },
    }

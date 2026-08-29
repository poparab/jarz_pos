"""Refuse a check-in from somebody the roster says is not working.

The hole this closes
--------------------
HRMS resolves a check-in's shift by looking for a submitted Active Shift
Assignment covering that moment. When it finds none it sets ``offshift=1``, and
``validate_distance_from_shift_location`` then looks up the location *of that
shift*, finds nothing, and **returns clean**. So the person with no shift is the
one person the geofence does not apply to: they can check in from home, from
another city, from anywhere, and the record is accepted.

That means removing somebody's shift -- which is exactly what marking a day off
does -- would otherwise *loosen* their access instead of removing it. This
handler inverts that: no shift, no check-in.

Why the rule is "not rostered on this day **or** the day before"
----------------------------------------------------------------
Every branch shift here crosses midnight (12:30 -> 01:00). A check-out logged at
00:40 carries yesterday's shift, and a check-out logged slightly past the shift
end resolves to no shift at all. Blocking purely on "no assignment dated today"
would therefore refuse people trying to clock *out* of a shift they genuinely
worked. Accepting an assignment on either day costs nothing -- somebody actually
off has an assignment on neither -- and removes that whole class of false
refusal.

What this deliberately does NOT do
----------------------------------
It does not police *when* within a rostered day somebody may check in. Arriving
outside the shift window is HRMS's business and its grace periods already
express it; tightening that here would change clock-in behaviour for every
employee on the site, which is a much larger blast radius than the roster
feature asked for. What it does add for that case is the geofence HRMS skips:
an off-window check-in on a rostered day is still measured against the branch
the roster puts that person at.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate

from jarz_pos.utils.settings_utils import single_flag

SETTINGS = "Jarz POS Settings"

#: Per-employee escape hatch. A rota mistake at 07:00 must not be able to lock a
#: real person out of their own shift with no way back except a developer, so a
#: manager can exempt somebody from the roster gate without turning it off for
#: the whole company.
EMPLOYEE_EXEMPT_FIELD = "custom_roster_checkin_exempt"


class RosterCheckinBlocked(frappe.ValidationError):
    """Raised when the roster says this person is not working right now."""


def enforce_roster_on_checkin(doc, method: Optional[str] = None) -> None:
    """``Employee Checkin.validate`` hook.

    Runs *after* HRMS's own ``validate`` -- Frappe composes app hooks to run
    after the controller method -- so ``doc.shift`` and ``doc.offshift`` are
    already resolved and can be trusted here.
    """
    if not _enforcement_enabled():
        return
    if not (doc and getattr(doc, "employee", None) and getattr(doc, "time", None)):
        return

    # HRMS matched a real shift: the person is working and the geofence has
    # already been applied against that shift's location. Nothing to add.
    if getattr(doc, "shift", None):
        return

    if _is_exempt(doc.employee):
        return
    if not _is_roster_managed(doc.employee):
        return

    day = getdate(doc.time)

    # An explicit day off is a decision somebody made, and it outranks
    # everything below -- including the horizon escape hatch.
    if _has_day_off(doc.employee, day):
        frappe.throw(
            _explanation(doc.employee, day),
            title=_("Not On Shift"),
            exc=RosterCheckinBlocked,
        )

    assignment = _assignment_on(doc.employee, day)
    if assignment:
        # Rostered today, but outside the shift window. HRMS skipped the
        # geofence because it had no shift to hang it on; apply it against the
        # branch the roster does know about.
        _enforce_location(doc, assignment.get("shift_location"))
        return

    # Yesterday's shift may still be running: every branch shift here ends after
    # midnight, so a 00:40 check-out belongs to the previous day. This is a
    # NARROW allowance, restricted to a shift that genuinely crosses midnight
    # and only while its tail is still open.
    #
    # It was once "any assignment dated yesterday", which quietly disabled the
    # whole gate. Marking a single day off carves that day out of a longer
    # range and leaves the day BEFORE it still rostered -- so someone off on the
    # 9th still had an assignment covering the 8th, and a 13:00 check-in on
    # their day off was waved straight through. The end-to-end run on staging
    # caught it; nothing in the unit tests could, because the bug lived in the
    # shape of real assignment ranges.
    previous = _assignment_on(doc.employee, add_days(day, -1))
    if previous and _is_within_overnight_tail(previous, doc.time):
        _enforce_location(doc, previous.get("shift_location"))
        return

    # The roster simply has not been drawn this far. Shift Schedules generate a
    # finite horizon -- staging and production are currently written out to
    # 2026-11-19 -- and the day after it lapses is not a decision anybody made
    # about anybody. Blocking there would lock every employee in the company
    # out of check-in on the same morning, over a housekeeping job nobody was
    # watching. A genuine hole INSIDE the horizon still blocks, and an explicit
    # day off was already refused above, so nothing real is let through here.
    if not _is_within_rostered_horizon(doc.employee, day):
        return

    frappe.throw(
        _explanation(doc.employee, day),
        title=_("Not On Shift"),
        exc=RosterCheckinBlocked,
    )


def _has_day_off(employee: str, day: Any) -> bool:
    try:
        return bool(
            frappe.db.exists("Jarz Roster Day Off", {"employee": employee, "off_date": day})
        )
    except Exception:
        return False


def _is_within_rostered_horizon(employee: str, day: Any) -> bool:
    """Whether ``day`` falls inside the span the roster actually covers.

    Outside that span the roster is silent rather than negative: a date before
    somebody's first assignment (a new joiner) or after the last one the Shift
    Schedule has generated says nothing about whether they are meant to work.

    An assignment with no end date makes the horizon open-ended, so a site that
    rosters that way keeps the strict behaviour.
    """
    try:
        rows = frappe.get_all(
            "Shift Assignment",
            filters={"employee": employee, "docstatus": 1},
            fields=["start_date", "end_date"],
        )
    except Exception:
        # Cannot tell -- do not turn somebody away on a failed probe.
        return False
    if not rows:
        return False

    starts = [getdate(r["start_date"]) for r in rows if r.get("start_date")]
    if not starts or getdate(day) < min(starts):
        return False

    if any(not r.get("end_date") for r in rows):
        return True

    ends = [getdate(r["end_date"]) for r in rows if r.get("end_date")]
    return bool(ends) and getdate(day) <= max(ends)


def _is_within_overnight_tail(assignment: dict, when: Any) -> bool:
    """True while yesterday's cross-midnight shift is still running.

    Two conditions, both required:

    * the shift type actually wraps past midnight (``end_time <= start_time``),
      so a daytime shift can never claim the following morning; and
    * the check-in's time of day is still inside that tail, plus the Shift
      Type's own ``allow_check_out_after_shift_end_time`` grace -- HRMS's number,
      not one invented here, so somebody clocking out late is judged by the same
      window the rest of the system uses.
    """
    shift_type = assignment.get("shift_type")
    if not shift_type:
        return False

    row = frappe.db.get_value(
        "Shift Type",
        shift_type,
        ["start_time", "end_time", "allow_check_out_after_shift_end_time"],
        as_dict=True,
    )
    if not row:
        return False

    from jarz_pos.services.roster import _as_timedelta

    start = _as_timedelta(row.get("start_time"))
    end = _as_timedelta(row.get("end_time"))
    if start is None or end is None:
        return False
    if end > start:
        # A shift that ends after it starts finishes on its own day and has no
        # tail to claim.
        return False

    moment = get_datetime(when)
    time_of_day = timedelta(
        hours=moment.hour, minutes=moment.minute, seconds=moment.second
    )
    grace = timedelta(minutes=cint(row.get("allow_check_out_after_shift_end_time")))
    return time_of_day <= end + grace


def _enforcement_enabled() -> bool:
    """Master switch, defaulting ON.

    Read through ``single_flag`` rather than ``get_single_value`` because that
    helper casts through ``cint()``: on a site where the field has never been
    written, an unset Check reads as ``0`` and the gate would be silently off --
    the exact failure this feature cannot afford, since "nothing happened" looks
    identical to "everybody is correctly rostered".
    """
    try:
        return single_flag(SETTINGS, "roster_enforce_checkin", True)
    except Exception:
        return False


def _is_exempt(employee: str) -> bool:
    try:
        if not frappe.get_meta("Employee").get_field(EMPLOYEE_EXEMPT_FIELD):
            return False
        return bool(frappe.db.get_value("Employee", employee, EMPLOYEE_EXEMPT_FIELD))
    except Exception:
        return False


def _is_roster_managed(employee: str) -> bool:
    """Only people the roster actually governs are gated.

    Office staff, the owner, and anyone else who has never been put on a shift
    schedule keep working exactly as before. Without this, switching the feature
    on would immediately lock out every employee who has a login but no rota --
    which is most of head office.
    """
    try:
        if frappe.get_all(
            "Shift Schedule Assignment",
            filters={"employee": employee, "enabled": 1},
            limit=1,
        ):
            return True
        return bool(
            frappe.get_all(
                "Shift Assignment",
                filters={"employee": employee, "docstatus": 1},
                limit=1,
            )
        )
    except Exception:
        # A probe that cannot answer must not be the reason somebody is turned
        # away at the door.
        return False


def _assignment_on(employee: str, day: Any) -> Optional[dict]:
    try:
        rows = frappe.get_all(
            "Shift Assignment",
            filters={
                "employee": employee,
                "docstatus": 1,
                "status": "Active",
                "start_date": ("<=", day),
            },
            or_filters=[["end_date", ">=", day], ["end_date", "is", "not set"]],
            fields=["name", "shift_type", "shift_location"],
            limit=1,
        )
    except Exception:
        return None
    return rows[0] if rows else None


def _explanation(employee: str, day: Any) -> str:
    """Say which of the two reasons applies, because they need different fixes.

    "You are off today" is answered by the employee going home; "you are not on
    the roster" is answered by a manager fixing the rota. A single generic
    message would send half of them to the wrong person.
    """
    try:
        off = frappe.db.get_value(
            "Jarz Roster Day Off",
            {"employee": employee, "off_date": day},
            ["off_type", "covered_by_name"],
            as_dict=True,
        )
    except Exception:
        off = None

    if off:
        base = _("You are marked off today ({0}), so you cannot check in.").format(
            _(off.get("off_type") or "Day Off")
        )
        if off.get("covered_by_name"):
            base += " " + _("{0} is covering this day.").format(off["covered_by_name"])
        return base + " " + _("Speak to your manager if this is wrong.")

    return _(
        "You are not on the roster today, so you cannot check in. "
        "Ask your manager to add you to the shift schedule."
    )


def _enforce_location(doc, shift_location: Optional[str]) -> None:
    """Apply the branch radius that HRMS skipped for an off-window check-in.

    Mirrors ``EmployeeCheckin.validate_distance_from_shift_location`` including
    its escape hatches: the global geolocation setting still governs, and a
    location with a non-positive radius still means "do not measure".
    """
    if not shift_location:
        return
    try:
        if not frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"):
            return
    except Exception:
        return

    if not (doc.get("latitude") or doc.get("longitude")):
        frappe.throw(_("Latitude and longitude values are required for checking in."))

    try:
        radius, latitude, longitude = frappe.db.get_value(
            "Shift Location", shift_location, ["checkin_radius", "latitude", "longitude"]
        )
    except Exception:
        return
    if not radius or radius <= 0:
        return

    from hrms.hr.utils import get_distance_between_coordinates

    distance = get_distance_between_coordinates(latitude, longitude, doc.latitude, doc.longitude)
    if distance > radius:
        frappe.throw(
            _("You must be within {0} meters of your shift location to check in.").format(radius),
            title=_("Too Far From Branch"),
        )

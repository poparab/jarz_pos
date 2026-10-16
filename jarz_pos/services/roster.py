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
import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate

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
		for loc in (frappe.db.get_value("POS Profile", p, POS_PROFILE_LOCATION_FIELD) for p in profiles)
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


def shift_locations(include_pos_access: bool = False) -> List[Dict[str, Any]]:
	"""Every Shift Location, optionally with the POS branch it maps to.

	``include_pos_access`` adds ``pos_profile`` (the enabled POS Profile whose
	``custom_shift_location`` is this location, or None -- the Factory has
	none) and ``pos_manageable`` (whether the caller may give day access
	there). The roster sheet uses the pair to decide whether to offer "also
	give POS access for this day"; offering it where the grant can only be
	refused would be a tick box that always fails.
	"""
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

	profile_by_location: Dict[str, str] = {}
	manageable: Set[str] = set()
	if include_pos_access:
		try:
			from jarz_pos.services import branch_access

			profile_by_location = branch_access.profiles_by_shift_location()
			manageable = set(branch_access.manageable_profiles())
		except Exception:
			# The tick box is optional; a lookup failure hides it rather than
			# taking the whole roster screen down with it.
			profile_by_location, manageable = {}, set()

	out: List[Dict[str, Any]] = []
	for r in rows:
		item = {
			"shift_location": r["name"],
			"checkin_radius": r.get("checkin_radius") or 0,
			"latitude": r.get("latitude"),
			"longitude": r.get("longitude"),
		}
		if include_pos_access:
			profile = profile_by_location.get(r["name"])
			item["pos_profile"] = profile
			item["pos_manageable"] = bool(profile and profile in manageable)
		out.append(item)
	return out


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
		hours_map[shift_type] for shift_type in _scheduled_shift_types(employee) if hours_map.get(shift_type)
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
	shift_schedule_assignment: Optional[str] = None,
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

	``shift_schedule_assignment`` links the range to the standing rota that
	produced it. The rota plan sets it so a later run can tell "the plan put
	this here" from "somebody edited this day by hand"; single-day edits leave
	it empty for exactly that reason.
	"""
	assignment = frappe.new_doc("Shift Assignment")
	assignment.employee = employee
	assignment.company = company
	assignment.shift_type = shift_type
	assignment.start_date = getdate(start_date)
	assignment.end_date = getdate(end_date) if end_date else None
	assignment.status = status
	assignment.shift_location = shift_location
	if shift_schedule_assignment:
		assignment.shift_schedule_assignment = shift_schedule_assignment
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
		_create_assignment(employee, company, shift_type, add_days(day, 1), end_date, status, shift_location)


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
		shift_location or (existing.shift_location if existing else None) or _schedule_location(employee)
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


def _release_pos_access(day_off: str) -> None:
	"""Wind down the POS day access a cover was given, before the day off goes.

	The grant links to the day-off row, and Frappe refuses to delete a record
	that something still links to -- so without this, ticking "also give POS
	access" on a cover made that day off impossible to undo or overwrite.
	Imported here because ``branch_access`` imports this module.
	"""
	from jarz_pos.services import branch_access

	branch_access.release_for_day_off(day_off)


def _discard_day_off_record(employee: str, day: Any) -> None:
	"""Delete a day-off row without touching any Shift Assignment.

	Distinct from ``clear_day_off``, which also restores both people's shifts.
	Here the caller is already writing the shift, so restoring would fight it.
	"""
	name = frappe.db.exists(DAY_OFF_DOCTYPE, {"employee": employee, "off_date": getdate(day)})
	if name:
		_release_pos_access(name)
		frappe.delete_doc(DAY_OFF_DOCTYPE, name, ignore_permissions=True)


def set_day_off(
	employee: str,
	off_date: Any,
	off_type: str = "Weekly Off",
	covered_by: Optional[str] = None,
	cover_shift_type: Optional[str] = None,
	notes: Optional[str] = None,
	original_shift_type: Optional[str] = None,
	original_shift_location: Optional[str] = None,
	from_plan: bool = False,
) -> Dict[str, Any]:
	"""Mark somebody off, optionally moving a colleague onto a cover shift.

	The two halves are one operation on purpose. A branch day that loses one of
	its two overlapping shifts is not covered by shortening the rota -- the
	remaining person has to stretch onto the full-day shift, and doing that as a
	separate second action leaves a window where the branch is rostered
	half-open. Recording the pair together is also what makes the undo exact.

	The cover is rostered at the ABSENT person's location first, then at the
	coverer's own location that day, then (inside ``set_shift_for_day``) at the
	coverer's schedule location. The order used to be the other way round:
	the coverer's home branch won, so a Dokki cashier covering Nasr City was
	rostered at Dokki and the geofence refused their check-in at the branch
	they had actually been sent to. A cover exists to fill the absent person's
	branch, so that branch is the answer whenever it is known.

	``original_shift_type`` / ``original_shift_location`` are used only when
	the day holds no assignment. The rota plan empties the period before it
	writes day-offs, so without them its day-off rows would record "nothing to
	restore" and clearing one later would leave the person unrostered instead
	of putting them back on the shift the plan gave them. ``from_plan`` marks
	the row as plan-made so the next plan run may replace it.
	"""
	day = getdate(off_date)
	original = _assignment_on(employee, day)
	fallback_shift_type = None if original else (original_shift_type or None)
	fallback_location = None if original else (original_shift_location or None)

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
			(original.shift_location if original else fallback_location)
			or (cover_previous.shift_location if cover_previous else None),
		)

	doc = frappe.new_doc(DAY_OFF_DOCTYPE)
	doc.employee = employee
	doc.off_date = day
	doc.off_type = off_type or "Weekly Off"
	doc.shift_location = (
		original.shift_location if original else (fallback_location or _schedule_location(employee))
	)
	doc.original_shift_type = original.shift_type if original else fallback_shift_type
	doc.original_shift_location = original.shift_location if original else fallback_location
	doc.covered_by = covered_by or None
	doc.cover_shift_type = cover_shift_type if covered_by else None
	doc.cover_previous_shift_type = cover_previous.shift_type if cover_previous else None
	# Where the coverer worked before the cover. The cover itself is rostered
	# at the ABSENT person's branch, so restoring to cover_shift_location would
	# leave a Dokki cashier who covered Nasr City geofenced to Nasr City.
	doc.cover_previous_shift_location = cover_previous.shift_location if cover_previous else None
	doc.cover_shift_location = (cover_result or {}).get("shift_location") if covered_by else None
	doc.notes = notes or None
	if from_plan:
		doc.from_plan = 1
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)

	return {
		"day_off": doc.name,
		"employee": employee,
		"off_date": str(day),
		"covered_by": covered_by,
		"cover_shift_type": cover_shift_type if covered_by else None,
		# Where the cover actually landed -- the roster API gives day POS
		# access at this branch, so it must be the resolved value, not a guess.
		"cover_shift_location": doc.cover_shift_location,
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
		# Back to the coverer's OWN branch: the cover shift sat at the absent
		# person's branch. Rows written before the field existed fall back.
		set_shift_for_day(
			doc.covered_by,
			day,
			doc.cover_previous_shift_type or None,
			doc.get("cover_previous_shift_location") or doc.cover_shift_location,
			clear_conflicting_day_off=False,
		)
		restored_cover = doc.cover_previous_shift_type

	_release_pos_access(doc.name)
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


def _holidays_by_employee(employees: List[str], start: date_cls, end: date_cls) -> Dict[str, Set[str]]:
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
		for row in frappe.get_all("Employee", filters={"name": ("in", employee_names)}, fields=fields):
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
				# The branch an unrostered day falls back to on the server. The
				# client needs the same answer to label "also give POS access"
				# with the branch the grant will really land on.
				"schedule_location": _schedule_location(employee),
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


# ---------------------------------------------------------------------------
# Rota plan wizard
# ---------------------------------------------------------------------------
#
# The month screen edits one day at a time. At the start of a month a line
# manager wants to say, once: "Ali works Morning at Nasr City, is off on
# Friday, and Omar covers him". The plan writes that for the chosen period AND
# makes it Ali's standing rota (HRMS Shift Schedule Assignment), so the months
# after keep coming without anybody re-entering it.
#
# Days somebody already edited by hand are kept unless the manager says
# otherwise, and the two passes are split on purpose: every person's shifts are
# written first (pass 1), and only then are day-offs and covers laid on top
# (pass 2), so a cover always lands on the coverer's FINAL day rather than on a
# shift their own row is about to rewrite.

#: HRMS "Assignment Rule Day" values, indexed by ``date.weekday()``.
WEEKDAYS: Tuple[str, ...] = (
	"Monday",
	"Tuesday",
	"Wednesday",
	"Thursday",
	"Friday",
	"Saturday",
	"Sunday",
)
WEEKDAY_ABBR = {
	"Monday": "Mon",
	"Tuesday": "Tue",
	"Wednesday": "Wed",
	"Thursday": "Thu",
	"Friday": "Fri",
	"Saturday": "Sat",
	"Sunday": "Sun",
}
FRIDAY = "Friday"

#: Friday runs on a different shift type ("Branch Closing - Friday") because
#: the branches open later. The variant is found by this naming convention.
FRIDAY_SUFFIX = " - Friday"

#: Longest period a plan may write. Two months covers "rest of this month plus
#: next"; anything longer belongs to the standing rota, which the plan also
#: sets, rather than to explicit assignments.
PLAN_MAX_DAYS = 62

SCHEDULE_FREQUENCY = "Every Week"

ISSUE_ERROR = "error"
ISSUE_WARNING = "warning"
ISSUE_INFO = "info"

KIND_SHIFT = "shift"
KIND_DAY_OFF = "day_off"
KIND_COVER = "cover"
KIND_EMPTY = "empty"


def _today() -> date_cls:
	"""Today, behind a seam so tests can pin the clock."""
	return getdate()


def _as_date(value: Any) -> Optional[date_cls]:
	if value is None or value == "":
		return None
	return getdate(value)


def _weekday(day: date_cls) -> str:
	return WEEKDAYS[day.weekday()]


def _days_between(start: date_cls, end: date_cls) -> List[date_cls]:
	"""Inclusive day list. Pure ``timedelta`` so the plan maths needs no site."""
	out: List[date_cls] = []
	current = start
	while current <= end:
		out.append(current)
		current = current + timedelta(days=1)
	return out


def _text(value: Any) -> Optional[str]:
	text = str(value or "").strip()
	return text or None


def _flag(value: Any, default: bool = False) -> bool:
	"""Read a JSON/form boolean. ``bool("false")`` is True, so text is parsed."""
	if value is None or value == "":
		return default
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return value != 0
	return str(value).strip().lower() in ("1", "true", "yes", "on")


def _canonical_weekday(value: Any) -> Optional[str]:
	text = str(value or "").strip().lower()
	if not text:
		return None
	for name in WEEKDAYS:
		if text in (name.lower(), WEEKDAY_ABBR[name].lower()):
			return name
	return None


class _ShiftTypes:
	"""Shift Type existence and the Friday-variant lookup, cached per call.

	A plan for a whole branch asks "does X exist" and "is there an X - Friday"
	for the same handful of names hundreds of times. ``known`` short-circuits
	the database entirely -- that is what the tests use.
	"""

	def __init__(self, known: Optional[Iterable[str]] = None):
		self._known: Optional[Set[str]] = set(known) if known is not None else None
		self._cache: Dict[str, bool] = {}

	def exists(self, name: Optional[str]) -> bool:
		if not name:
			return False
		if name not in self._cache:
			if self._known is not None:
				self._cache[name] = name in self._known
			else:
				self._cache[name] = bool(frappe.db.exists("Shift Type", name))
		return self._cache[name]

	def friday_variant(self, name: Optional[str]) -> Optional[str]:
		"""``"<name> - Friday"`` when it exists; never a variant of a variant."""
		if not name or name.endswith(FRIDAY_SUFFIX):
			return None
		candidate = f"{name}{FRIDAY_SUFFIX}"
		return candidate if self.exists(candidate) else None


def _date_runs(days: Iterable[date_cls]) -> List[Tuple[date_cls, date_cls]]:
	"""Group dates into (first, last) runs of consecutive days."""
	runs: List[Tuple[date_cls, date_cls]] = []
	for day in sorted(set(days)):
		if runs and runs[-1][1] + timedelta(days=1) == day:
			runs[-1] = (runs[-1][0], day)
		else:
			runs.append((day, day))
	return runs


def _contiguous_ranges(
	entries: Iterable[Tuple[date_cls, str, Optional[str]]],
) -> List[Tuple[date_cls, date_cls, str, Optional[str]]]:
	"""Turn per-day (date, shift_type, location) into assignment ranges.

	One Shift Assignment per run of consecutive days with the same shift and
	branch, not one per day: thirty single-day rows per person per month is
	what ``_insert_assignment``'s merge exists to avoid, and every one of them
	is scanned by HRMS's overlap check on each later write.
	"""
	ranges: List[Tuple[date_cls, date_cls, str, Optional[str]]] = []
	for day, shift_type, location in sorted(entries, key=lambda e: e[0]):
		if ranges:
			first, last, prev_type, prev_location = ranges[-1]
			if last + timedelta(days=1) == day and prev_type == shift_type and prev_location == location:
				ranges[-1] = (first, day, shift_type, location)
				continue
		ranges.append((day, day, shift_type, location))
	return ranges


def _row_pattern(
	shift_type: str, off_days: Iterable[str], friday_shift_type: Optional[str]
) -> Dict[str, Optional[str]]:
	"""Weekday -> shift type, or None on an off day."""
	off = set(off_days)
	pattern: Dict[str, Optional[str]] = {}
	for weekday in WEEKDAYS:
		if weekday in off:
			pattern[weekday] = None
		elif weekday == FRIDAY and friday_shift_type:
			pattern[weekday] = friday_shift_type
		else:
			pattern[weekday] = shift_type
	return pattern


def _planned_shift(row: Dict[str, Any], weekday: str) -> str:
	"""The shift this row would work on ``weekday`` if it were not off.

	Recorded on plan day-offs as ``original_shift_type`` so that clearing one
	later puts the person on the shift the plan meant, Friday variant included.
	"""
	if weekday == FRIDAY and row.get("friday_shift_type"):
		return row["friday_shift_type"]
	return row["shift_type"]


def _expand_plan(row: Dict[str, Any], start: date_cls, end: date_cls) -> List[Tuple[date_cls, Optional[str]]]:
	"""Every day of the period with the shift the plan puts there (None = off)."""
	return [(day, row["pattern"][_weekday(day)]) for day in _days_between(start, end)]


def _parse_plan_date(value: Any, label: str) -> date_cls:
	if not value:
		frappe.throw(_("{0} is required.").format(label))
	try:
		return getdate(value)
	except Exception:
		frappe.throw(_("{0} is not a valid date: {1}").format(label, value))


def _weekday_list(value: Any) -> List[str]:
	if value is None or value == "":
		return []
	if isinstance(value, str):
		value = [v for v in value.replace(";", ",").split(",")]
	if not isinstance(value, (list, tuple)):
		frappe.throw(_("Days off must be a list of weekdays."))
	out: List[str] = []
	for item in value:
		weekday = _canonical_weekday(item)
		if not weekday:
			frappe.throw(_("Unknown weekday: {0}").format(item))
		if weekday not in out:
			out.append(weekday)
	return sorted(out, key=WEEKDAYS.index)


def _normalize_plan(plan: Any, types: _ShiftTypes, today: Optional[date_cls] = None) -> Dict[str, Any]:
	"""Validate the payload into a plan, or throw before anything is written.

	Everything here is a hard error rather than an issue: a plan with a
	missing shift or a cover on a working day cannot be previewed meaningfully,
	so the manager is stopped at the first one instead of shown a half-plan.
	"""
	if isinstance(plan, str):
		try:
			plan = json.loads(plan)
		except Exception:
			frappe.throw(_("Invalid JSON payload."))
	if not isinstance(plan, dict):
		frappe.throw(_("The plan must be an object."))

	start = _parse_plan_date(plan.get("start"), _("Start date"))
	end = _parse_plan_date(plan.get("end"), _("End date"))
	today = today or _today()
	if start < today:
		frappe.throw(_("The plan cannot start in the past."))
	if end < start:
		frappe.throw(_("The end date is before the start date."))
	if (end - start).days + 1 > PLAN_MAX_DAYS:
		frappe.throw(_("A plan can cover at most {0} days.").format(PLAN_MAX_DAYS))

	raw_rows = plan.get("rows")
	if not isinstance(raw_rows, list) or not raw_rows:
		frappe.throw(_("Pick at least one person."))

	rows: List[Dict[str, Any]] = []
	seen: Set[str] = set()
	for raw in raw_rows:
		if not isinstance(raw, dict):
			frappe.throw(_("Malformed plan row."))
		employee = _text(raw.get("employee"))
		if not employee:
			frappe.throw(_("Employee is required."))
		if employee in seen:
			frappe.throw(_("{0} is in the plan twice.").format(employee))
		seen.add(employee)

		shift_type = _text(raw.get("shift_type"))
		if not shift_type:
			frappe.throw(_("Pick a shift for {0}.").format(employee))
		if not types.exists(shift_type):
			frappe.throw(_("Shift type {0} does not exist.").format(shift_type))

		location = _text(raw.get("shift_location"))
		if not location:
			# An assignment without a location disables the geofence for that
			# day, so a plan row without one is refused rather than guessed.
			frappe.throw(_("Pick a branch for {0}.").format(employee))

		off_days = _weekday_list(raw.get("off_days"))
		if len(off_days) >= len(WEEKDAYS):
			frappe.throw(_("{0} cannot be off every day.").format(employee))

		covers: Dict[str, Dict[str, Any]] = {}
		raw_covers = raw.get("covers") or []
		if not isinstance(raw_covers, list):
			frappe.throw(_("Covers must be a list."))
		for raw_cover in raw_covers:
			if not isinstance(raw_cover, dict):
				frappe.throw(_("Malformed cover."))
			weekday = _canonical_weekday(raw_cover.get("weekday"))
			if not weekday:
				frappe.throw(_("Unknown weekday: {0}").format(raw_cover.get("weekday")))
			covered_by = _text(raw_cover.get("covered_by"))
			if not covered_by:
				# A blank picker means "nobody covers"; not an error.
				continue
			if weekday not in off_days:
				frappe.throw(_("{0} is not a day off for {1}.").format(_(weekday), employee))
			if weekday in covers:
				frappe.throw(_("{0} has two covers on {1}.").format(employee, _(weekday)))
			if covered_by == employee:
				frappe.throw(_("{0} cannot cover their own day off.").format(employee))
			cover_shift_type = _text(raw_cover.get("cover_shift_type"))
			if not cover_shift_type:
				frappe.throw(_("Pick the cover shift for {0}.").format(covered_by))
			if not types.exists(cover_shift_type):
				frappe.throw(_("Shift type {0} does not exist.").format(cover_shift_type))
			covers[weekday] = {
				"weekday": weekday,
				"covered_by": covered_by,
				"cover_shift_type": cover_shift_type,
			}

		use_variant = _flag(raw.get("use_friday_variant"), True)
		friday_shift_type = types.friday_variant(shift_type) if use_variant else None
		rows.append(
			{
				"employee": employee,
				"shift_type": shift_type,
				"shift_location": location,
				"off_days": off_days,
				"use_friday_variant": use_variant,
				"friday_shift_type": friday_shift_type,
				"covers": covers,
				"pattern": _row_pattern(shift_type, off_days, friday_shift_type),
			}
		)

	return {
		"start": start,
		"end": end,
		"replace_manual": _flag(plan.get("replace_manual"), False),
		"grant_pos_access": _flag(plan.get("grant_pos_access"), False),
		"rows": rows,
	}


class _PlanState:
	"""What the database holds for the people in a plan, read once.

	The preview and the apply must agree on which days are "manual", so both
	compute from one snapshot of this shape. Built from plain dicts so the plan
	maths can be tested without a site.

	* ``assignments``: employee -> Active assignments overlapping the period.
	* ``day_offs``: day-off rows in the period where a plan person is the
	  absentee OR the coverer.
	* ``rota``: employee -> weekday -> (shift_type, shift_location) from their
	  enabled Shift Schedule Assignments.
	* ``rota_horizon``: employee -> the latest ``create_shifts_after`` of those
	  schedules, i.e. how far HRMS has already generated shifts.
	"""

	def __init__(
		self,
		employee_names: Optional[Dict[str, str]] = None,
		assignments: Optional[Dict[str, List[Dict[str, Any]]]] = None,
		day_offs: Optional[List[Dict[str, Any]]] = None,
		rota: Optional[Dict[str, Dict[str, Tuple[str, Optional[str]]]]] = None,
		rota_horizon: Optional[Dict[str, Optional[date_cls]]] = None,
	):
		self.employee_names = dict(employee_names or {})
		self.rota = dict(rota or {})
		self.rota_horizon = {k: _as_date(v) for k, v in (rota_horizon or {}).items()}

		self.assignments: Dict[str, List[Dict[str, Any]]] = {}
		for employee, rows in (assignments or {}).items():
			normalised = []
			for row in rows:
				item = dict(row)
				item["start_date"] = _as_date(item.get("start_date"))
				item["end_date"] = _as_date(item.get("end_date"))
				normalised.append(item)
			normalised.sort(key=lambda r: r["start_date"], reverse=True)
			self.assignments[employee] = normalised

		self.day_offs: List[Dict[str, Any]] = []
		self._own_off: Dict[Tuple[str, date_cls], Dict[str, Any]] = {}
		self._covering: Dict[Tuple[str, date_cls], List[Dict[str, Any]]] = {}
		for row in day_offs or []:
			item = dict(row)
			item["off_date"] = _as_date(item.get("off_date"))
			item["from_plan"] = int(item.get("from_plan") or 0)
			self.day_offs.append(item)
			self._own_off[(item["employee"], item["off_date"])] = item
			if item.get("covered_by"):
				self._covering.setdefault((item["covered_by"], item["off_date"]), []).append(item)

	def name(self, employee: Optional[str]) -> str:
		return self.employee_names.get(employee) or employee or ""

	def own_day_off(self, employee: str, day: date_cls) -> Optional[Dict[str, Any]]:
		return self._own_off.get((employee, day))

	def covering(self, employee: str, day: date_cls) -> List[Dict[str, Any]]:
		return self._covering.get((employee, day), [])

	def assignment_on(self, employee: str, day: date_cls) -> Optional[Dict[str, Any]]:
		for row in self.assignments.get(employee, []):
			if (
				row["start_date"]
				and row["start_date"] <= day
				and (row["end_date"] is None or row["end_date"] >= day)
			):
				return row
		return None


def _rota_matches(assignment: Dict[str, Any], expected: Optional[Tuple[str, Optional[str]]]) -> bool:
	if not expected:
		return False
	shift_type, location = expected
	if assignment.get("shift_type") != shift_type:
		return False
	return not location or assignment.get("shift_location") == location


def _manual_entry(
	day: date_cls, current: str, kind: str, record: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
	return {"date": day, "current": current, "kind": kind, "record": record}


def _manual_days(
	employee: str,
	days: Iterable[date_cls],
	state: _PlanState,
	planned: Set[str],
) -> Dict[date_cls, Dict[str, Any]]:
	"""Days in the period that somebody shaped by hand, for one person.

	Used identically by preview and apply, so what the manager is shown as
	"kept" is exactly what the apply keeps. See the contract for the rules;
	two refinements beyond it, both to stop false alarms:

	* An unlinked assignment that matches the standing rota is NOT manual. It
	  is normally the tail of a range a single-day edit split -- upstream
	  ``break_shift`` drops the schedule link on the tail -- and flagging it
	  would list half a month of untouched days as "changed by hand".
	* A day only counts as ``empty`` inside the horizon HRMS has already
	  generated. Past it the day is empty because the scheduler has not got
	  there yet, not because anybody emptied it.

	A plan-made cover of somebody who is NOT in this plan is also kept (as
	``cover``): this run does not rewrite that person's day-off, so wiping the
	coverer's day would leave the day-off claiming a cover that no longer
	exists.
	"""
	rota = state.rota.get(employee) or {}
	horizon = state.rota_horizon.get(employee)
	out: Dict[date_cls, Dict[str, Any]] = {}
	for day in days:
		own = state.own_day_off(employee, day)
		if own:
			if not own.get("from_plan"):
				out[day] = _manual_entry(day, _("Off"), KIND_DAY_OFF, own)
			continue

		covering = state.covering(employee, day)
		kept_cover = next(
			(c for c in covering if not c.get("from_plan") or c.get("employee") not in planned),
			None,
		)
		if kept_cover:
			out[day] = _manual_entry(
				day,
				_("Covering {0}").format(state.name(kept_cover.get("employee"))),
				KIND_COVER,
				kept_cover,
			)
			continue
		if covering:
			# A plan-made cover of somebody this run re-plans: rewritten.
			continue

		assignment = state.assignment_on(employee, day)
		expected = rota.get(_weekday(day))
		if assignment:
			if (
				rota
				and not assignment.get("shift_schedule_assignment")
				and not _rota_matches(assignment, expected)
			):
				out[day] = _manual_entry(day, assignment.get("shift_type") or "", KIND_SHIFT)
			continue

		if expected and horizon and day <= horizon:
			out[day] = _manual_entry(day, _("Empty"), KIND_EMPTY)
	return out


def _issue(day: Optional[date_cls], code: str, severity: str, message: str) -> Dict[str, Any]:
	return {"date": str(day) if day else None, "code": code, "severity": severity, "message": message}


def _cover_conflict(
	covered_by: str,
	day: date_cls,
	infos: Dict[str, Dict[str, Any]],
	state: _PlanState,
	replace_manual: bool,
	outside_manual: Dict[str, Dict[date_cls, Dict[str, Any]]],
	planned: Set[str],
	period: List[date_cls],
) -> Optional[str]:
	"""Why the coverer cannot take ``day``: an issue code, or None if they can.

	A coverer in the plan is judged by their NEW plan (their old day-offs are
	about to be replaced); one outside it by what the database holds, since
	this run does not touch their days.
	"""
	info = infos.get(covered_by)
	if info is not None:
		manual = info["manual"].get(day) if not replace_manual else None
		if manual:
			return "cover_is_off" if manual["kind"] == KIND_DAY_OFF else "cover_day_manual"
		if info["row"]["pattern"][_weekday(day)] is None:
			return "cover_is_off"
		return None

	if state.own_day_off(covered_by, day):
		return "cover_is_off"
	if covered_by not in outside_manual:
		outside_manual[covered_by] = _manual_days(covered_by, period, state, planned)
	if outside_manual[covered_by].get(day):
		return "cover_day_manual"
	return None


def _compute_plan(plan: Dict[str, Any], state: _PlanState) -> Dict[str, Any]:
	"""The whole preview, plus the per-row working data apply needs.

	Pure: reads only ``plan`` and ``state``. Returns
	``{"response": <contract shape>, "rows": {employee: info}}``.
	"""
	start: date_cls = plan["start"]
	end: date_cls = plan["end"]
	replace_manual: bool = plan["replace_manual"]
	rows: List[Dict[str, Any]] = plan["rows"]
	planned = {row["employee"] for row in rows}
	period = _days_between(start, end)

	first_by_weekday: Dict[str, date_cls] = {}
	for day in period:
		first_by_weekday.setdefault(_weekday(day), day)

	infos: Dict[str, Dict[str, Any]] = {}
	for row in rows:
		employee = row["employee"]
		manual = _manual_days(employee, period, state, planned)
		infos[employee] = {
			"row": row,
			"manual": manual,
			"kept": set() if replace_manual else set(manual),
			"days": _expand_plan(row, start, end),
			"offs": [],
			"issues": [],
			"shift_days": 0,
			"day_offs": 0,
			"covers": 0,
		}

	outside_manual: Dict[str, Dict[date_cls, Dict[str, Any]]] = {}
	tally: Dict[Tuple[str, date_cls], List[str]] = {}

	for row in rows:
		employee = row["employee"]
		info = infos[employee]
		for day, shift_type in info["days"]:
			if day in info["kept"]:
				continue
			if shift_type:
				info["shift_days"] += 1
				continue

			info["day_offs"] += 1
			weekday = _weekday(day)
			entry = {"date": day, "planned_shift": _planned_shift(row, weekday), "cover": None}
			cover = row["covers"].get(weekday)
			if cover:
				coverer = cover["covered_by"]
				conflict = _cover_conflict(
					coverer, day, infos, state, replace_manual, outside_manual, planned, period
				)
				if conflict == "cover_is_off":
					info["issues"].append(
						_issue(
							day,
							"cover_is_off",
							ISSUE_ERROR,
							_("{0} is off on {1} and cannot cover.").format(state.name(coverer), day),
						)
					)
				elif conflict == "cover_day_manual":
					info["issues"].append(
						_issue(
							day,
							"cover_day_manual",
							ISSUE_WARNING,
							_("{0}'s day on {1} was set by hand; cover skipped.").format(
								state.name(coverer), day
							),
						)
					)
				else:
					entry["cover"] = cover
					info["covers"] += 1
					tally.setdefault((coverer, day), []).append(employee)
			info["offs"].append(entry)

	for (coverer, day), absentees in sorted(tally.items(), key=lambda kv: (kv[0][1], kv[0][0])):
		if len(absentees) < 2:
			continue
		for employee in absentees:
			infos[employee]["issues"].append(
				_issue(
					day,
					"cover_double",
					ISSUE_ERROR,
					_("{0} covers two people on {1}.").format(state.name(coverer), day),
				)
			)

	for index, first in enumerate(rows):
		for second in rows[index + 1 :]:
			if first["shift_location"] != second["shift_location"]:
				continue
			for weekday in WEEKDAYS:
				if weekday not in first["off_days"] or weekday not in second["off_days"]:
					continue
				if weekday not in first_by_weekday:
					continue
				if first["covers"].get(weekday) and second["covers"].get(weekday):
					continue
				infos[first["employee"]]["issues"].append(
					_issue(
						first_by_weekday[weekday],
						"same_day_off",
						ISSUE_WARNING,
						_("{0} and {1} are both off on {2}.").format(
							state.name(first["employee"]), state.name(second["employee"]), _(weekday)
						),
					)
				)

	for row in rows:
		if (
			row["use_friday_variant"]
			and not row["friday_shift_type"]
			and FRIDAY not in row["off_days"]
			and not row["shift_type"].endswith(FRIDAY_SUFFIX)
		):
			infos[row["employee"]]["issues"].append(
				_issue(
					first_by_weekday.get(FRIDAY),
					"no_friday_variant",
					ISSUE_INFO,
					_("No {0} shift; Fridays use {1}.").format(
						f"{row['shift_type']}{FRIDAY_SUFFIX}", row["shift_type"]
					),
				)
			)

	out_rows: List[Dict[str, Any]] = []
	all_issues: List[Dict[str, Any]] = []
	manual_total = 0
	for row in rows:
		employee = row["employee"]
		info = infos[employee]
		info["issues"].sort(key=lambda i: (i["date"] or "", i["code"]))
		manual_total += len(info["manual"])
		out_rows.append(
			{
				"employee": employee,
				"employee_name": state.name(employee),
				"shift_type": row["shift_type"],
				"friday_shift_type": row["friday_shift_type"],
				"shift_location": row["shift_location"],
				"shift_days": info["shift_days"],
				"day_offs": info["day_offs"],
				"covers": info["covers"],
				"manual_days": [
					{"date": str(day), "current": m["current"], "kind": m["kind"]}
					for day, m in sorted(info["manual"].items())
				],
				"issues": info["issues"],
			}
		)
		for issue in info["issues"]:
			all_issues.append(dict(issue, employee=employee, employee_name=state.name(employee)))

	response = {
		"success": True,
		"dry_run": True,
		"applied": False,
		"start": str(start),
		"end": str(end),
		"summary": {
			"people": len(rows),
			"shift_days": sum(r["shift_days"] for r in out_rows),
			"day_offs": sum(r["day_offs"] for r in out_rows),
			"covers": sum(r["covers"] for r in out_rows),
			"manual_kept": 0 if replace_manual else manual_total,
			"manual_replaced": manual_total if replace_manual else 0,
			"errors": sum(1 for i in all_issues if i["severity"] == ISSUE_ERROR),
			"warnings": sum(1 for i in all_issues if i["severity"] == ISSUE_WARNING),
		},
		"rows": out_rows,
		"issues": all_issues,
		"touched_outside": _touched_outside(rows, infos, state, replace_manual),
	}
	return {"response": response, "rows": infos}


def _touched_outside(
	rows: List[Dict[str, Any]],
	infos: Dict[str, Dict[str, Any]],
	state: _PlanState,
	replace_manual: bool,
) -> List[str]:
	"""People NOT in the plan whose day the apply would change.

	Their names never appear in the payload, so the API's per-row scope check
	would never see them: the coverer of a dropped plan day-off is put back on
	their own shift (``_restore_coverer``), and with ``replace_manual`` a hand
	cover's absentee has their record edited (``_detach_cover``). Covers named
	in the payload are not listed -- the API checks those already.
	"""
	planned = {row["employee"] for row in rows}
	touched: Set[str] = set()
	for record in state.day_offs:
		if (
			record.get("from_plan")
			and record.get("employee") in planned
			and record.get("covered_by")
			and record["covered_by"] not in planned
		):
			touched.add(record["covered_by"])
	if replace_manual:
		for info in infos.values():
			for manual in info["manual"].values():
				record = manual.get("record") or {}
				if manual["kind"] == KIND_DAY_OFF and record.get("covered_by"):
					touched.add(record["covered_by"])
				elif manual["kind"] == KIND_COVER and record.get("employee"):
					touched.add(record["employee"])
	return sorted(touched - planned)


# -- reads ------------------------------------------------------------------


def _schedule_shape(schedule: Optional[str]) -> Optional[Tuple[str, List[str]]]:
	"""(shift_type, weekdays) of a Shift Schedule, or None."""
	if not schedule:
		return None
	shift_type = frappe.db.get_value("Shift Schedule", schedule, "shift_type")
	if not shift_type:
		return None
	days = frappe.get_all(
		"Assignment Rule Day",
		filters={"parent": schedule, "parenttype": "Shift Schedule"},
		pluck="day",
	)
	return shift_type, [d for d in days if d in WEEKDAYS]


def _standing_rota(
	employees: List[str],
) -> Tuple[
	Dict[str, Dict[str, Tuple[str, Optional[str]]]],
	Dict[str, Optional[date_cls]],
	Dict[str, List[Dict[str, Any]]],
]:
	"""Each person's enabled standing rota as a weekday pattern.

	Returns (pattern, horizon, schedules) per employee. A person normally has
	two schedules -- the week and the Friday variant -- so the pattern is the
	union of them.
	"""
	rota: Dict[str, Dict[str, Tuple[str, Optional[str]]]] = {}
	horizon: Dict[str, Optional[date_cls]] = {}
	schedules: Dict[str, List[Dict[str, Any]]] = {}
	if not employees:
		return rota, horizon, schedules
	try:
		ssas = frappe.get_all(
			"Shift Schedule Assignment",
			filters={"enabled": 1, "employee": ("in", list(employees))},
			fields=["name", "employee", "shift_schedule", "shift_location", "create_shifts_after"],
			order_by="creation asc",
		)
	except Exception:
		return rota, horizon, schedules

	shapes: Dict[str, Optional[Tuple[str, List[str]]]] = {}
	for ssa in ssas:
		schedule = ssa.get("shift_schedule")
		if schedule not in shapes:
			try:
				shapes[schedule] = _schedule_shape(schedule)
			except Exception:
				shapes[schedule] = None
		shape = shapes[schedule]
		if not shape:
			continue
		employee = ssa["employee"]
		shift_type, days = shape
		location = ssa.get("shift_location") or None
		pattern = rota.setdefault(employee, {})
		for day in days:
			pattern.setdefault(day, (shift_type, location))
		after = _as_date(ssa.get("create_shifts_after"))
		if after and (horizon.get(employee) is None or after > horizon[employee]):
			horizon[employee] = after
		schedules.setdefault(employee, []).append(
			{"shift_type": shift_type, "days": days, "shift_location": location}
		)
	return rota, horizon, schedules


def _load_plan_state(employees: Iterable[str], start: date_cls, end: date_cls) -> _PlanState:
	employees = sorted({e for e in employees if e})
	if not employees:
		return _PlanState()

	names = {
		row["name"]: row.get("employee_name")
		for row in frappe.get_all(
			"Employee", filters={"name": ("in", employees)}, fields=["name", "employee_name"]
		)
	}

	assignments: Dict[str, List[Dict[str, Any]]] = {}
	for row in frappe.get_all(
		"Shift Assignment",
		filters=dict(ACTIVE_ASSIGNMENT_FILTERS, employee=("in", employees), start_date=("<=", end)),
		or_filters=[["end_date", ">=", start], ["end_date", "is", "not set"]],
		fields=[
			"name",
			"employee",
			"shift_type",
			"shift_location",
			"start_date",
			"end_date",
			"shift_schedule_assignment",
		],
	):
		assignments.setdefault(row["employee"], []).append(row)

	fields = [
		"name",
		"employee",
		"off_date",
		"covered_by",
		"from_plan",
		"cover_shift_type",
		"cover_previous_shift_type",
		"cover_previous_shift_location",
		"cover_shift_location",
	]
	day_offs: Dict[str, Dict[str, Any]] = {}
	for link_field in ("employee", "covered_by"):
		for row in frappe.get_all(
			DAY_OFF_DOCTYPE,
			filters={link_field: ("in", employees), "off_date": ("between", [start, end])},
			fields=fields,
		):
			day_offs[row["name"]] = row

	rota, horizon, _schedules = _standing_rota(employees)
	return _PlanState(names, assignments, list(day_offs.values()), rota, horizon)


def _plan_people(plan: Dict[str, Any]) -> Set[str]:
	people: Set[str] = set()
	for row in plan["rows"]:
		people.add(row["employee"])
		people.update(c["covered_by"] for c in row["covers"].values())
	return people


def plan_preview(plan: Any) -> Dict[str, Any]:
	"""Dry run: what ``apply_plan`` would write, and what stands in its way.

	Writes nothing. Hard validation errors throw; everything softer comes back
	as ``issues`` so the manager can see all of them at once.
	"""
	if not hrms_available():
		frappe.throw(_("HRMS is not installed on this site."))
	normalized = _normalize_plan(plan, _ShiftTypes())
	state = _load_plan_state(_plan_people(normalized), normalized["start"], normalized["end"])
	return _compute_plan(normalized, state)["response"]


# -- writes -----------------------------------------------------------------


def _message_mark() -> int:
	return len(getattr(frappe.local, "message_log", None) or [])


def _message_reset(mark: int) -> None:
	"""Drop msgprints a caught failure queued, so the client is not shown an
	error dialog for a row the response already reports as failed."""
	log = getattr(frappe.local, "message_log", None)
	if log is not None and len(log) > mark:
		del log[mark:]


def _delete_assignment(name: str) -> None:
	doc = frappe.get_doc("Shift Assignment", name)
	doc.flags.ignore_permissions = True
	if doc.docstatus == 1:
		doc.cancel()
	doc.delete(ignore_permissions=True)


def _plan_cut(
	assignment: Dict[str, Any],
	start: date_cls,
	end: date_cls,
	kept: Set[date_cls],
	old_rota: Dict[str, Tuple[str, Optional[str]]],
	protected_after: Set[date_cls],
) -> Optional[Dict[str, Any]]:
	"""Decide what happens to one assignment when the plan replaces the rota.

	Pure. Returns None when the assignment stays as it is, otherwise
	``{"name", "head_end", "keep_runs"}``: shorten it to end on ``head_end``
	(None = delete it), then re-create ``keep_runs`` unlinked.

	The plan REPLACES the standing rota, so after ``end`` only hand edits
	survive. Anything else left there blocks HRMS: the new schedule starts
	generating at ``create_shifts_after + 1``, and one leftover day in its
	way makes ``create_shifts`` hit the overlap check and fail on every run.
	So after ``end``:

	* an open-ended assignment is cut at ``end`` with no tail -- it is the
	  old "works every day from now on" and the new rota takes over;
	* a day linked to any schedule is the old rota's output and goes;
	* an unlinked day that matches the old rota is also old-rota output --
	  ``_break_assignment`` drops the link on the tail it splits off -- and
	  goes too;
	* an unlinked day that differs from the old rota is a hand edit, and a day
	  this person covers somebody on (``protected_after``) is a cover; both stay.
	"""
	a_start = _as_date(assignment.get("start_date"))
	a_end = _as_date(assignment.get("end_date"))
	if a_end is not None and a_end < start:
		return None

	link = assignment.get("shift_schedule_assignment")
	keep_days: List[date_cls] = []
	changed = False

	low = max(a_start, start)
	high = min(a_end, end) if a_end else end
	if low <= high:
		for day in _days_between(low, high):
			if day in kept:
				keep_days.append(day)
			else:
				changed = True

	if a_end is None:
		changed = True
	elif a_end > end:
		for day in _days_between(max(a_start, end + timedelta(days=1)), a_end):
			hand_edit = not link and not _rota_matches(assignment, old_rota.get(_weekday(day)))
			if day in protected_after or hand_edit:
				keep_days.append(day)
			else:
				changed = True

	if not changed:
		return None
	return {
		"name": assignment.get("name"),
		"head_end": start - timedelta(days=1) if a_start < start else None,
		"keep_runs": _date_runs(keep_days),
	}


def _remaining_after_end(
	assignments: Iterable[Dict[str, Any]],
	cuts: Dict[str, Dict[str, Any]],
	end: date_cls,
) -> List[Tuple[date_cls, Optional[date_cls]]]:
	"""What is left of the assignments after ``end`` once ``cuts`` are applied."""
	after = end + timedelta(days=1)
	out: List[Tuple[date_cls, Optional[date_cls]]] = []
	for assignment in assignments:
		cut = cuts.get(assignment.get("name"))
		if cut is None:
			segments = [(_as_date(assignment["start_date"]), _as_date(assignment.get("end_date")))]
		else:
			segments = list(cut["keep_runs"])
		for seg_start, seg_end in segments:
			if seg_end is None or seg_end >= after:
				out.append((max(seg_start, after), seg_end))
	return out


def _plan_horizon(
	end: date_cls,
	old_create_after: Iterable[Any],
	remaining: Iterable[Tuple[date_cls, Optional[date_cls]]],
	off_dates_after: Iterable[Any],
) -> date_cls:
	"""The last day the plan writes explicitly; HRMS takes over the day after.

	It must be on or after every day that still holds something for this
	person, or the new schedule's first generated range would overlap it.
	"""
	horizon = end
	for value in old_create_after:
		value = _as_date(value)
		if value and value > horizon:
			horizon = value
	for _seg_start, seg_end in remaining:
		if seg_end is None:
			# _plan_cut never leaves one; refuse rather than block HRMS forever.
			frappe.throw(_("An open-ended shift is left after the plan."))
		if seg_end > horizon:
			horizon = seg_end
	for value in off_dates_after:
		value = _as_date(value)
		if value and value > horizon:
			horizon = value
	return horizon


def _fill_entries(
	end: date_cls,
	horizon: date_cls,
	pattern: Dict[str, Optional[str]],
	location: Optional[str],
	remaining: Iterable[Tuple[date_cls, Optional[date_cls]]],
	off_dates_after: Iterable[Any],
) -> List[Tuple[date_cls, str, Optional[str]]]:
	"""New-rota days for (end, horizon], skipping days something already holds."""
	if horizon <= end:
		return []
	occupied: Set[date_cls] = {_as_date(v) for v in off_dates_after if v}
	for seg_start, seg_end in remaining:
		occupied.update(_days_between(seg_start, min(seg_end or horizon, horizon)))
	entries = []
	for day in _days_between(end + timedelta(days=1), horizon):
		shift_type = pattern[_weekday(day)]
		if shift_type and day not in occupied:
			entries.append((day, shift_type, location))
	return entries


def _apply_cut(assignment: Dict[str, Any], cut: Dict[str, Any]) -> None:
	"""Write one ``_plan_cut`` decision."""
	employee = assignment["employee"]
	company = assignment.get("company") or _employee_company(employee)
	shift_type = assignment["shift_type"]
	status = assignment.get("status") or "Active"
	location = assignment.get("shift_location")

	if cut["head_end"]:
		doc = frappe.get_doc("Shift Assignment", assignment["name"])
		doc.end_date = cut["head_end"]
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
	else:
		_delete_assignment(assignment["name"])

	for run_start, run_end in cut["keep_runs"]:
		_create_assignment(employee, company, shift_type, run_start, run_end, status, location)


def _write_ranges(
	employee: str,
	company: str,
	entries: Iterable[Tuple[date_cls, str, Optional[str]]],
	ssa_by_shift: Optional[Dict[str, str]] = None,
) -> int:
	"""Write per-day entries as contiguous assignments; returns how many."""
	count = 0
	for run_start, run_end, shift_type, location in _contiguous_ranges(entries):
		_create_assignment(
			employee,
			company,
			shift_type,
			run_start,
			run_end,
			"Active",
			location,
			shift_schedule_assignment=(ssa_by_shift or {}).get(shift_type),
		)
		count += 1
	return count


def _shift_schedule_name(shift_type: str, days: List[str]) -> str:
	base = "{0} ({1})".format(shift_type, ",".join(WEEKDAY_ABBR[d] for d in days))
	name, suffix = base, 2
	while frappe.db.exists("Shift Schedule", name):
		name = f"{base} {suffix}"
		suffix += 1
	return name


def _find_or_create_shift_schedule(shift_type: str, days: List[str]) -> str:
	"""``hrms...get_or_insert_shift_schedule`` with permissions bypassed.

	Same matching (submitted, same shift type, frequency and set of days) so
	the plan reuses schedules HR already made. The name is readable --
	"Morning (Sat,Sun,Mon)" -- instead of upstream's random string, because a
	line manager reading an SSA in Desk should see what it means.
	"""
	wanted = sorted(set(days), key=WEEKDAYS.index)
	for name in frappe.get_all(
		"Shift Schedule",
		filters={"shift_type": shift_type, "frequency": SCHEDULE_FREQUENCY, "docstatus": 1},
		pluck="name",
	):
		existing = frappe.get_all(
			"Assignment Rule Day",
			filters={"parent": name, "parenttype": "Shift Schedule"},
			pluck="day",
		)
		if sorted(set(existing)) == sorted(wanted):
			return name

	doc = frappe.get_doc(
		{
			"doctype": "Shift Schedule",
			"name": _shift_schedule_name(shift_type, wanted),
			"shift_type": shift_type,
			"frequency": SCHEDULE_FREQUENCY,
			"repeat_on_days": [{"day": day} for day in wanted],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


def _insert_schedule_assignment(
	employee: str, company: str, schedule: str, location: Optional[str], create_after: date_cls
) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Shift Schedule Assignment",
			"employee": employee,
			"company": company,
			"shift_schedule": schedule,
			"shift_location": location,
			"shift_status": "Active",
			"enabled": 1,
			"create_shifts_after": create_after,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _restore_coverer(record: Dict[str, Any]) -> None:
	"""Put a coverer who is NOT being re-planned back on their own day.

	Mirrors the cover half of ``clear_day_off``. Without it, deleting or
	replacing a day-off would leave its coverer on the 12h cover shift for a
	colleague who is no longer off.
	"""
	set_shift_for_day(
		record["covered_by"],
		record["off_date"],
		record.get("cover_previous_shift_type") or None,
		record.get("cover_previous_shift_location") or record.get("cover_shift_location"),
		clear_conflicting_day_off=False,
	)


def _drop_plan_day_off(record: Dict[str, Any], planned: Set[str]) -> None:
	if not frappe.db.exists(DAY_OFF_DOCTYPE, record["name"]):
		return
	if record.get("covered_by") and record["covered_by"] not in planned:
		_restore_coverer(record)
	_release_pos_access(record["name"])
	frappe.delete_doc(DAY_OFF_DOCTYPE, record["name"], ignore_permissions=True)


def _detach_cover(record: Dict[str, Any]) -> None:
	"""Strip the cover off somebody else's day-off; the absence stays.

	Used when ``replace_manual`` rewrites a day this person was covering by
	hand. The absentee is still off, now visibly uncovered on the month
	screen, rather than recorded as covered by somebody who is not there.
	"""
	if not frappe.db.exists(DAY_OFF_DOCTYPE, record["name"]):
		return
	_release_pos_access(record["name"])
	frappe.db.set_value(
		DAY_OFF_DOCTYPE,
		record["name"],
		{
			"covered_by": None,
			"covered_by_name": None,
			"cover_shift_type": None,
			"cover_previous_shift_type": None,
			"cover_previous_shift_location": None,
			"cover_shift_location": None,
		},
	)


def _apply_row(
	info: Dict[str, Any],
	plan: Dict[str, Any],
	state: _PlanState,
	planned: Set[str],
) -> None:
	"""Pass 1 for one person: clear, rewrite the period, set the standing rota."""
	row = info["row"]
	employee = row["employee"]
	location = row["shift_location"]
	start: date_cls = plan["start"]
	end: date_cls = plan["end"]
	kept: Set[date_cls] = info["kept"]
	company = _employee_company(employee)

	# 1. Plan-made day-offs are the previous run's output; this run replaces them.
	for record in state.day_offs:
		if not record.get("from_plan"):
			continue
		if record["employee"] == employee or (
			record.get("covered_by") == employee and record["employee"] in planned
		):
			_drop_plan_day_off(record, planned)

	# 4. Replace manual days when asked (before emptying, so restores land first).
	if plan["replace_manual"]:
		for day, manual in info["manual"].items():
			record = manual.get("record")
			if manual["kind"] == KIND_DAY_OFF and record:
				if record.get("covered_by") and record["covered_by"] not in planned:
					_restore_coverer(record)
				_discard_day_off_record(employee, day)
			elif manual["kind"] == KIND_COVER and record:
				_detach_cover(record)

	old_ssas = frappe.get_all(
		"Shift Schedule Assignment",
		filters={"employee": employee, "enabled": 1},
		fields=["name", "create_shifts_after"],
	)
	after_end = end + timedelta(days=1)
	off_dates_after = frappe.get_all(
		DAY_OFF_DOCTYPE,
		filters={"employee": employee, "off_date": (">=", after_end)},
		pluck="off_date",
	)
	protected_after = {
		_as_date(d)
		for d in frappe.get_all(
			DAY_OFF_DOCTYPE,
			filters={"covered_by": employee, "off_date": (">=", after_end)},
			pluck="off_date",
		)
	}

	# 2 + 5a. Empty the period except kept days, and clear what the old rota
	# left after it (see ``_plan_cut``).
	assignments = frappe.get_all(
		"Shift Assignment",
		filters=dict(ACTIVE_ASSIGNMENT_FILTERS, employee=employee),
		or_filters=[["end_date", ">=", start], ["end_date", "is", "not set"]],
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
	)
	old_rota = state.rota.get(employee) or {}
	cuts: Dict[str, Dict[str, Any]] = {}
	for assignment in assignments:
		cut = _plan_cut(assignment, start, end, kept, old_rota, protected_after)
		if cut:
			cuts[assignment["name"]] = cut
			_apply_cut(assignment, cut)

	for ssa in old_ssas:
		frappe.db.set_value("Shift Schedule Assignment", ssa["name"], "enabled", 0)

	# 5b. The new standing rota: one schedule per distinct shift type.
	days_by_shift: Dict[str, List[str]] = {}
	for weekday in WEEKDAYS:
		shift_type = row["pattern"][weekday]
		if shift_type:
			days_by_shift.setdefault(shift_type, []).append(weekday)
	ssa_by_shift: Dict[str, str] = {}
	for shift_type, days in days_by_shift.items():
		schedule = _find_or_create_shift_schedule(shift_type, days)
		ssa_by_shift[shift_type] = _insert_schedule_assignment(employee, company, schedule, location, end)

	# 3. Write the period, linked to the new rota.
	_write_ranges(
		employee,
		company,
		[(day, st, location) for day, st in info["days"] if st and day not in kept],
		ssa_by_shift,
	)

	# 5c. Fill (end, horizon] from the new rota around what is left there, then
	# let HRMS continue from the day after horizon. Nothing of this person's
	# is left after horizon, so HRMS's first generated range cannot collide.
	remaining = _remaining_after_end(assignments, cuts, end)
	horizon = _plan_horizon(end, [s.get("create_shifts_after") for s in old_ssas], remaining, off_dates_after)
	_write_ranges(
		employee,
		company,
		_fill_entries(end, horizon, row["pattern"], location, remaining, off_dates_after),
		ssa_by_shift,
	)
	for name in ssa_by_shift.values():
		frappe.db.set_value("Shift Schedule Assignment", name, "create_shifts_after", horizon)


def _plan_failed(employee_name: str, day: Optional[date_cls], exc: Exception, mark: int) -> None:
	"""Re-throw a write failure with the person and day it happened on.

	The original message (an HRMS overlap, a check-in already linked) is kept,
	but the client is shown one message instead of the raw one plus ours.
	"""
	_message_reset(mark)
	detail = str(exc) or exc.__class__.__name__
	if day:
		frappe.throw(
			_("Plan not saved: {0} on {1}: {2}").format(employee_name, day, detail),
			title=_("Plan not saved"),
		)
	frappe.throw(_("Plan not saved: {0}: {1}").format(employee_name, detail), title=_("Plan not saved"))


def apply_plan(plan: Any, check_employee: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
	"""Write the plan, all or nothing. Refuses when the preview has any error.

	All or nothing because a half-applied plan is worse than none: one person
	on the new rota and their coverer still on the old one is exactly the
	branch-left-half-open state the wizard exists to prevent, and the manager
	cannot see it from a "3 failed" line. Any failure re-throws with the
	person and day, and Frappe rolls the whole request back.

	Pass 1 rewrites each person's period and standing rota. Pass 2 then lays
	day-offs and covers on top through ``set_day_off`` -- the same path the
	month screen uses -- so clearing a plan day-off later restores both people
	exactly like any other day-off.

	``check_employee`` is called, before any write, for every person OUTSIDE
	the plan whose day the apply would change (``touched_outside``); the API
	passes its branch-scope check.
	"""
	if not hrms_available():
		frappe.throw(_("HRMS is not installed on this site."))
	normalized = _normalize_plan(plan, _ShiftTypes())
	state = _load_plan_state(_plan_people(normalized), normalized["start"], normalized["end"])
	computed = _compute_plan(normalized, state)
	response = computed["response"]
	errors = [i for i in response["issues"] if i["severity"] == ISSUE_ERROR]
	if errors:
		frappe.throw(
			_("Fix the errors first: {0}").format(errors[0]["message"]),
			title=_("Plan has errors"),
		)
	if check_employee:
		for employee in response["touched_outside"]:
			check_employee(employee)

	planned = {row["employee"] for row in normalized["rows"]}

	for row in normalized["rows"]:
		employee = row["employee"]
		mark = _message_mark()
		try:
			_apply_row(computed["rows"][employee], normalized, state, planned)
		except Exception as exc:
			_plan_failed(state.name(employee), None, exc, mark)

	created_covers: List[Dict[str, Any]] = []
	for row in normalized["rows"]:
		employee = row["employee"]
		info = computed["rows"][employee]
		for off in info["offs"]:
			cover = off["cover"]
			mark = _message_mark()
			try:
				result = set_day_off(
					employee,
					off["date"],
					"Weekly Off",
					covered_by=cover["covered_by"] if cover else None,
					cover_shift_type=cover["cover_shift_type"] if cover else None,
					original_shift_type=off["planned_shift"],
					original_shift_location=row["shift_location"],
					from_plan=True,
				)
			except Exception as exc:
				_plan_failed(state.name(employee), off["date"], exc, mark)
			if cover:
				created_covers.append(
					{
						"employee": employee,
						"covered_by": cover["covered_by"],
						"date": str(off["date"]),
						"day_off": result.get("day_off"),
						"cover_shift_location": result.get("cover_shift_location"),
					}
				)

	response["dry_run"] = False
	response["applied"] = True
	response["created_covers"] = created_covers
	return response


# -- defaults for the wizard --------------------------------------------------


def _standing_shift_type(schedules: List[Dict[str, Any]]) -> Optional[str]:
	"""The person's main (non-Friday) standing shift: the one with most days."""
	plain = [s for s in schedules if not s["shift_type"].endswith(FRIDAY_SUFFIX)]
	if plain:
		return max(plain, key=lambda s: len(s["days"]))["shift_type"]
	if schedules:
		return schedules[0]["shift_type"][: -len(FRIDAY_SUFFIX)]
	return None


def _common_recent_shift(
	rows: List[Dict[str, Any]], window_start: date_cls, window_end: date_cls, types: _ShiftTypes
) -> Optional[str]:
	"""Most-worked shift type in the window, Friday variants folded into their base."""
	days_by_type: Dict[str, int] = {}
	for row in rows:
		shift_type = row.get("shift_type")
		if not shift_type:
			continue
		if shift_type.endswith(FRIDAY_SUFFIX):
			base = shift_type[: -len(FRIDAY_SUFFIX)]
			if types.exists(base):
				shift_type = base
		a_start = max(_as_date(row["start_date"]), window_start)
		a_end = min(_as_date(row["end_date"]), window_end) if row.get("end_date") else window_end
		if a_end < a_start:
			continue
		days_by_type[shift_type] = days_by_type.get(shift_type, 0) + (a_end - a_start).days + 1
	if not days_by_type:
		return None
	return max(sorted(days_by_type), key=lambda k: days_by_type[k])


def _recent_plan_covers(employees: List[str], today: date_cls) -> Dict[Tuple[str, str], Dict[str, Any]]:
	"""The latest plan-made cover per (employee, weekday)."""
	if not employees:
		return {}
	try:
		rows = frappe.get_all(
			DAY_OFF_DOCTYPE,
			filters={
				"employee": ("in", employees),
				"from_plan": 1,
				"covered_by": ("is", "set"),
				"off_date": (">=", today - timedelta(days=120)),
			},
			fields=["employee", "off_date", "covered_by", "covered_by_name", "cover_shift_type"],
			order_by="off_date desc",
		)
	except Exception:
		# ``from_plan`` arrives with a migrate; before it the wizard still opens.
		return {}
	out: Dict[Tuple[str, str], Dict[str, Any]] = {}
	for row in rows:
		weekday = _weekday(_as_date(row["off_date"]))
		out.setdefault(
			(row["employee"], weekday),
			{
				"weekday": weekday,
				"covered_by": row["covered_by"],
				"covered_by_name": row.get("covered_by_name") or row["covered_by"],
				"cover_shift_type": row.get("cover_shift_type"),
			},
		)
	return out


def plan_defaults(shift_location: Optional[str] = None) -> Dict[str, Any]:
	"""Pre-fill for the wizard: each person's current week as the plan would see it.

	Read from the standing rota first, because that is what the plan replaces,
	and from the last two months of actual shifts for anybody without one.
	Scope follows ``get_month``: an employee belongs to the branches their
	shifts and schedules point at.
	"""
	today = _today()
	out: Dict[str, Any] = {
		"suggested_start": str(getdate(get_first_day(add_months(today, 1)))),
		"employees": [],
	}
	if not hrms_available():
		out["hrms_available"] = False
		return out
	out["hrms_available"] = True

	allowed = allowed_shift_locations()
	window_start = today - timedelta(days=60)
	window_end = today + timedelta(days=31)

	locations: Dict[str, Set[str]] = {}
	schedule_location: Dict[str, str] = {}
	try:
		for row in frappe.get_all(
			"Shift Schedule Assignment",
			filters={"enabled": 1},
			fields=["employee", "shift_location"],
			order_by="creation asc",
		):
			locations.setdefault(row["employee"], set())
			if row.get("shift_location"):
				locations[row["employee"]].add(row["shift_location"])
				schedule_location.setdefault(row["employee"], row["shift_location"])
	except Exception:
		pass

	recent_by_employee: Dict[str, List[Dict[str, Any]]] = {}
	for row in frappe.get_all(
		"Shift Assignment",
		filters={
			"docstatus": 1,
			"status": ("in", READABLE_ASSIGNMENT_STATUSES),
			"start_date": ("<=", window_end),
		},
		or_filters=[["end_date", ">=", window_start], ["end_date", "is", "not set"]],
		fields=["employee", "shift_type", "shift_location", "start_date", "end_date"],
		order_by="start_date desc",
	):
		recent_by_employee.setdefault(row["employee"], []).append(row)
		locations.setdefault(row["employee"], set())
		if row.get("shift_location"):
			locations[row["employee"]].add(row["shift_location"])

	def in_scope(employee: str) -> bool:
		locs = locations.get(employee, set())
		if shift_location and shift_location not in locs:
			return False
		if allowed is None:
			return True
		return bool(locs.intersection(allowed))

	candidates = sorted(e for e in locations if in_scope(e))
	if not candidates:
		return out

	employee_rows = {
		row["name"]: row
		for row in frappe.get_all(
			"Employee",
			filters={"name": ("in", candidates), "status": "Active"},
			fields=["name", "employee_name", "designation"],
		)
	}
	employees = [e for e in candidates if e in employee_rows]
	rota, _horizon, schedules = _standing_rota(employees)
	covers = _recent_plan_covers(employees, today)
	types = _ShiftTypes()

	payload: List[Dict[str, Any]] = []
	for employee in employees:
		row = employee_rows[employee]
		recent = recent_by_employee.get(employee, [])
		location = schedule_location.get(employee) or next(
			(r["shift_location"] for r in recent if r.get("shift_location")), None
		)
		shift_type = _standing_shift_type(schedules.get(employee, [])) or _common_recent_shift(
			recent, window_start, window_end, types
		)
		pattern = rota.get(employee) or {}
		off_days = [w for w in WEEKDAYS if w not in pattern] if pattern else []
		variant = types.friday_variant(shift_type) if shift_type else None
		payload.append(
			{
				"employee": employee,
				"employee_name": row.get("employee_name") or employee,
				"designation": row.get("designation"),
				"is_courier": is_courier(employee, row.get("designation")),
				"shift_location": location,
				"shift_type": shift_type,
				"off_days": off_days,
				"use_friday_variant": bool(variant),
				"friday_shift_type": variant,
				"covers": [covers[(employee, w)] for w in off_days if (employee, w) in covers],
				"has_standing_rota": bool(pattern),
			}
		)

	payload.sort(key=lambda r: (r["shift_location"] or "￿", (r["employee_name"] or "").lower()))
	out["employees"] = payload
	return out

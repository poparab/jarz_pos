"""Let chosen employees check in at whichever branch they are standing in.

Why this exists
---------------
The geofence measures a check-in against ONE place: the Shift Location on that
day's Shift Assignment. HRMS does it for an on-shift press
(``EmployeeCheckin.validate_distance_from_shift_location``) and our roster gate
does it for an off-window press (``events/employee_checkin._enforce_location``).
That is right for branch staff and wrong for a manager who visits another
branch before reaching their own: they stand inside a real branch and are told
they are too far from "their" one.

The rule for a flagged employee
-------------------------------
Still geofenced -- never "check in from anywhere". The press is accepted when
it is inside the radius of ANY Shift Location, and refused otherwise. The roster
gate is untouched: a flagged person with no shift that day is still refused,
because "where may you check in" and "are you working today" are separate
questions, and the second already has its own escape hatch
(``custom_roster_checkin_exempt``).

The flag WIDENS HRMS's rule, it never replaces it: ``widen_rostered_check``
first applies the ordinary single-branch rule, and only a press that rule
would refuse -- the rostered branch was measured and was too far -- is
re-measured against every branch. Everything HRMS lets through unmeasured (no
shift location, a radius of 0, an off-shift press) stays let through, so the
flag can never lock out somebody who could check in without it.

A location with a non-positive radius means "do not measure" in HRMS. Here it
cannot prove presence, so it is skipped as a candidate; if NO location has a
radius the check is a no-op, exactly as HRMS behaves for everybody else.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

#: Employee Check. Set from Desk on the Employee form.
ANY_BRANCH_FIELD = "custom_checkin_any_branch"


def any_branch_field_exists() -> bool:
	try:
		return bool(frappe.get_meta("Employee").get_field(ANY_BRANCH_FIELD))
	except Exception:
		return False


def is_any_branch(employee: str | None) -> bool:
	"""Whether this employee may check in at any branch. Never raises."""
	if not employee or not any_branch_field_exists():
		return False
	try:
		return bool(frappe.db.get_value("Employee", employee, ANY_BRANCH_FIELD))
	except Exception:
		return False


def measurable_locations() -> list[dict[str, Any]]:
	"""Every Shift Location that can actually prove presence (radius > 0)."""
	try:
		rows = frappe.get_all(
			"Shift Location",
			fields=["name", "checkin_radius", "latitude", "longitude"],
			order_by="name",
		)
	except Exception:
		return []
	return [r for r in rows if (r.get("checkin_radius") or 0) > 0]


def location_within_radius(latitude: Any, longitude: Any, locations: list[dict[str, Any]]) -> str | None:
	"""The nearest location whose radius contains the point, or ``None``."""
	from hrms.hr.utils import get_distance_between_coordinates

	best: tuple[float, str] | None = None
	for row in locations:
		distance = get_distance_between_coordinates(row["latitude"], row["longitude"], latitude, longitude)
		if distance <= row["checkin_radius"] and (best is None or distance < best[0]):
			best = (distance, row["name"])
	return best[1] if best else None


def widen_rostered_check(doc, shift_location: str | None) -> str | None:
	"""HRMS's single-branch rule, widened to every branch only when it fails.

	Same order and escape hatches as
	``EmployeeCheckin.validate_distance_from_shift_location``: geolocation off
	-> pass; no coordinates -> refuse; no rostered location or a radius of 0 ->
	pass unmeasured; inside the rostered radius -> pass. Only a measured miss
	falls through to ``enforce_any_branch``.
	"""
	try:
		if not frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"):
			return None
	except Exception:
		return None

	if not (doc.get("latitude") or doc.get("longitude")):
		frappe.throw(_("Latitude and longitude values are required for checking in."))

	if not shift_location:
		return None
	try:
		row = frappe.db.get_value(
			"Shift Location", shift_location, ["checkin_radius", "latitude", "longitude"], as_dict=True
		)
	except Exception:
		return None
	if not row or (row.get("checkin_radius") or 0) <= 0:
		return None
	if location_within_radius(doc.latitude, doc.longitude, [dict(row, name=shift_location)]):
		return shift_location
	return enforce_any_branch(doc)


def enforce_any_branch(doc) -> str | None:
	"""Geofence ``doc`` against every branch. Returns the branch it matched.

	Mirrors HRMS's own escape hatches: the global geolocation setting still
	governs, and missing coordinates are still refused.
	"""
	try:
		if not frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"):
			return None
	except Exception:
		return None

	if not (doc.get("latitude") or doc.get("longitude")):
		frappe.throw(_("Latitude and longitude values are required for checking in."))

	locations = measurable_locations()
	if not locations:
		return None

	matched = location_within_radius(doc.latitude, doc.longitude, locations)
	if matched:
		return matched

	from hrms.hr.doctype.employee_checkin.employee_checkin import CheckinRadiusExceededError

	frappe.throw(
		_("You must be within {0} meters of one of the branches to check in.").format(
			int(min(r["checkin_radius"] for r in locations))
		),
		title=_("Too Far From Branch"),
		exc=CheckinRadiusExceededError,
	)

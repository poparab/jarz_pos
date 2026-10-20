"""Attendance reporting API -- who turned up, and who was late.

Thin transport over :mod:`jarz_pos.services.attendance`, mirroring
``api/roster.py`` exactly: the role gate, the branch-scope check and payload
coercion live here, and everything that decides what a check-in *means* lives in
the service.

Read-only by design
-------------------
Nothing here writes attendance, and nothing must. Every number is derived on
read from Employee Checkin, Shift Assignment and the day-off records; nothing is
stored, so a wrong rule is fixed by a deploy rather than by unpicking a month of
written Attendance rows. Enabling HRMS auto-attendance would turn that on its
head -- see the service module docstring.

The one write, ``approve_device``, does not touch attendance either: it
registers an employee's phone (``Employee.custom_checkin_device``) and clears
the New Device flags it explains. It is POST-only and narrower than the rest of
the module -- managers (``attendance_service.can_approve_device``), not the
whole line-manager tier.

The gate is the authority
-------------------------
``_ensure_access`` admits the line-manager tier, the same set as the roster.
Branch scoping is applied inside the service for the list endpoints, but
``get_employee`` names one person the *client* chose, so it re-checks that
person against the caller's branches before answering -- without it, a branch
manager could read any employee in the company by posting a different id.
"""

from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import getdate

from jarz_pos.services import attendance as attendance_service
from jarz_pos.services import checkin_device as checkin_device_service


def _ensure_access() -> None:
	attendance_service.ensure_attendance_access()


def _clean(value: Any) -> str | None:
	text = str(value or "").strip()
	return text or None


def _coerce_date(value: Any, label: str = "date") -> str | None:
	"""``None`` stays ``None`` (the service applies the default); junk throws."""
	if not value:
		return None
	try:
		return str(getdate(value))
	except Exception:
		frappe.throw(_("{0} is not a valid date: {1}").format(label, value))


def _coerce_month(value: Any) -> str | None:
	"""Validate ``YYYY-MM`` here so the error names the month, not the first.

	``month_bounds`` builds ``f"{month}-01"`` and lets ``getdate`` fail, which
	surfaces to the client as a complaint about a date string it never sent.
	"""
	month = _clean(value)
	if not month:
		return None
	try:
		year, part = month.split("-")
		if len(year) != 4 or not (1 <= int(part) <= 12):
			raise ValueError(month)
		int(year)
	except Exception:
		frappe.throw(_("Month must look like YYYY-MM, not {0}.").format(month))
	return month


@frappe.whitelist()
def get_bootstrap() -> dict[str, Any]:
	"""Branches, scope, grace and the status vocabulary, in one call.

	The status list is served rather than hard-coded in the client so that a
	status added here cannot silently render as a blank chip on an app nobody
	has updated.
	"""
	_ensure_access()
	data = attendance_service.bootstrap()
	data["success"] = True
	return data


@frappe.whitelist()
def get_month(month: str | None = None, shift_location: str | None = None) -> dict[str, Any]:
	"""The attendance calendar for a month: one row per employee."""
	_ensure_access()
	data = attendance_service.get_month(month=_coerce_month(month), shift_location=_clean(shift_location))
	data["success"] = True
	return data


@frappe.whitelist()
def get_day(date: str | None = None, shift_location: str | None = None) -> dict[str, Any]:
	"""One day, grouped by branch, worst rows first."""
	_ensure_access()
	data = attendance_service.get_day(
		date=_coerce_date(date, _("Date")), shift_location=_clean(shift_location)
	)
	data["success"] = True
	return data


@frappe.whitelist()
def get_employee(
	employee: str,
	from_date: str | None = None,
	to_date: str | None = None,
) -> dict[str, Any]:
	"""One person's attendance over a range, with their raw check-ins.

	The scope re-check is not redundant with the one inside the service: the
	list endpoints filter a set the server chose, while this endpoint answers
	about an employee id the caller supplied.
	"""
	_ensure_access()
	employee = _clean(employee)
	if not employee:
		frappe.throw(_("Employee is required."))

	# Scope BEFORE existence, with the same message for both: checking existence
	# first answered "No such employee" for a bad id and "not at your branch"
	# for a real one, which let any line manager enumerate employee ids.
	attendance_service.ensure_employee_in_scope(employee)
	if not frappe.db.exists("Employee", employee):
		frappe.throw(attendance_service.NOT_AVAILABLE_MESSAGE(), frappe.PermissionError)

	data = attendance_service.get_employee(
		employee=employee,
		from_date=_coerce_date(from_date, _("From date")),
		to_date=_coerce_date(to_date, _("To date")),
	)
	data["success"] = True
	return data


@frappe.whitelist(methods=["POST"])
def approve_device(employee: str, date: str) -> dict[str, Any]:
	"""Approve the New Device phone behind ``employee``'s check-ins on shift day ``date``.

	Registers the latest New Device token of that day as the employee's phone
	and clears the New Device flag on every check-in carrying it. Never
	approves a Shared Device. Gate: attendance access, THEN the manager check,
	THEN branch scope (same message for a missing id as for an out-of-scope
	one, like ``get_employee``).
	"""
	_ensure_access()
	if not attendance_service.can_approve_device():
		frappe.throw(
			_("Only a JARZ Manager can approve a new phone."),
			frappe.PermissionError,
		)
	employee = _clean(employee)
	if not employee:
		frappe.throw(_("Employee is required."))
	day = _coerce_date(date, _("Date"))
	if not day:
		frappe.throw(_("Date is required."))

	attendance_service.ensure_employee_in_scope(employee)
	if not frappe.db.exists("Employee", employee):
		frappe.throw(attendance_service.NOT_AVAILABLE_MESSAGE(), frappe.PermissionError)

	data = checkin_device_service.approve_device(employee, day)
	data["success"] = True
	return data


@frappe.whitelist()
def get_summary(
	from_date: str | None = None,
	to_date: str | None = None,
	group_by: str = "branch",
	shift_location: str | None = None,
) -> dict[str, Any]:
	"""Totals rolled up by branch, by employee or by day."""
	_ensure_access()
	data = attendance_service.get_summary(
		from_date=_coerce_date(from_date, _("From date")),
		to_date=_coerce_date(to_date, _("To date")),
		group_by=_clean(group_by) or "branch",
		shift_location=_clean(shift_location),
	)
	data["success"] = True
	return data

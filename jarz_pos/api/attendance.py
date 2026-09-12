"""Attendance reporting API -- who turned up, and who was late.

Thin transport over :mod:`jarz_pos.services.attendance`, mirroring
``api/roster.py`` exactly: the role gate, the branch-scope check and payload
coercion live here, and everything that decides what a check-in *means* lives in
the service.

Read-only by design
-------------------
There is no write endpoint in this module and there must not be one. Every
number here is derived on read from Employee Checkin, Shift Assignment and the
day-off records; nothing is stored, so a wrong rule is fixed by a deploy rather
than by unpicking a month of written Attendance rows. Enabling HRMS
auto-attendance would turn that on its head -- see the service module docstring.

The gate is the authority
-------------------------
``_ensure_access`` admits the line-manager tier, the same set as the roster.
Branch scoping is applied inside the service for the list endpoints, but
``get_employee`` names one person the *client* chose, so it re-checks that
person against the caller's branches before answering -- without it, a branch
manager could read any employee in the company by posting a different id.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import getdate

from jarz_pos.services import attendance as attendance_service


def _ensure_access() -> None:
    attendance_service.ensure_attendance_access()


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _coerce_date(value: Any, label: str = "date") -> Optional[str]:
    """``None`` stays ``None`` (the service applies the default); junk throws."""
    if not value:
        return None
    try:
        return str(getdate(value))
    except Exception:
        frappe.throw(_("{0} is not a valid date: {1}").format(label, value))


def _coerce_month(value: Any) -> Optional[str]:
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
def get_bootstrap() -> Dict[str, Any]:
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
def get_month(
    month: Optional[str] = None, shift_location: Optional[str] = None
) -> Dict[str, Any]:
    """The attendance calendar for a month: one row per employee."""
    _ensure_access()
    data = attendance_service.get_month(
        month=_coerce_month(month), shift_location=_clean(shift_location)
    )
    data["success"] = True
    return data


@frappe.whitelist()
def get_day(date: Optional[str] = None, shift_location: Optional[str] = None) -> Dict[str, Any]:
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
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    """One person's attendance over a range, with their raw check-ins.

    The scope re-check is not redundant with the one inside the service: the
    list endpoints filter a set the server chose, while this endpoint answers
    about an employee id the caller supplied.
    """
    _ensure_access()
    employee = _clean(employee)
    if not employee:
        frappe.throw(_("Employee is required."))
    if not frappe.db.exists("Employee", employee):
        frappe.throw(_("No such employee: {0}").format(employee))

    attendance_service.ensure_employee_in_scope(employee)

    data = attendance_service.get_employee(
        employee=employee,
        from_date=_coerce_date(from_date, _("From date")),
        to_date=_coerce_date(to_date, _("To date")),
    )
    data["success"] = True
    return data


@frappe.whitelist()
def get_summary(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    group_by: str = "branch",
    shift_location: Optional[str] = None,
) -> Dict[str, Any]:
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

"""Shift distribution API -- the month calendar the branch managers work in.

Thin transport over :mod:`jarz_pos.services.roster`. Everything that decides
*what* a roster change means lives in the service; what lives here is the role
gate, the branch-scope check, and payload coercion.

The gate is the authority
-------------------------
The writes underneath run with ``ignore_permissions`` because line managers do
not hold HR Manager or HR User and must not be given them (see the service
module for why widening the DocPerms is worse than it looks). That makes
``_ensure_write_access`` the only thing standing between a caller and somebody
else's rota, so every mutating endpoint calls it *and* re-checks that each
employee named in the payload sits inside the caller's own branches -- including
the covering colleague, who is a second employee the caller could otherwise
reach across a branch boundary.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import getdate

from jarz_pos.services import roster as roster_service


def _ensure_access() -> None:
    roster_service.ensure_roster_access()


def _coerce_date(value: Any, label: str = "date") -> str:
    if not value:
        frappe.throw(_("{0} is required.").format(label))
    try:
        return str(getdate(value))
    except Exception:
        frappe.throw(_("{0} is not a valid date: {1}").format(label, value))


def _clean(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _ensure_employee_in_scope(employee: str) -> None:
    """Refuse to touch somebody outside the caller's branches.

    Checked per employee rather than once per request because a cover names two
    people, and the second one is supplied by the client. Without this a
    manager at one branch could hand their colleague's shift to a courier at
    another simply by posting a different employee id.
    """
    allowed = roster_service.allowed_shift_locations()
    if allowed is None:
        return

    locations = set()
    for row in frappe.get_all(
        "Shift Assignment",
        filters={"employee": employee, "docstatus": 1, "status": "Active"},
        fields=["shift_location"],
    ):
        if row.get("shift_location"):
            locations.add(row["shift_location"])
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
        name = frappe.db.get_value("Employee", employee, "employee_name") or employee
        frappe.throw(
            _("{0} is not at one of your branches.").format(name),
            frappe.PermissionError,
        )


def _ensure_employee_active(employee: str) -> None:
    status = frappe.db.get_value("Employee", employee, "status")
    if not status:
        frappe.throw(_("No such employee: {0}").format(employee))
    if status != "Active":
        frappe.throw(_("{0} is not an active employee.").format(employee))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_bootstrap() -> Dict[str, Any]:
    """Everything the screen needs before it can draw a month.

    Returned as one call so the calendar does not open on three spinners. The
    ``can_manage`` flag lets the client render a read-only month for a role that
    may look but not edit, rather than showing controls that 403 on tap.
    """
    _ensure_access()
    allowed = roster_service.allowed_shift_locations()
    return {
        "success": True,
        "hrms_available": roster_service.hrms_available(),
        "can_manage": True,
        "shift_catalog": roster_service.shift_catalog(),
        "shift_locations": roster_service.shift_locations(),
        "off_types": ["Weekly Off", "Vacation", "Sick", "Unpaid", "Other"],
        "scope": {
            "configured": roster_service.roster_scope_configured(),
            "unrestricted": allowed is None,
            "locations": None if allowed is None else sorted(allowed),
        },
    }


@frappe.whitelist()
def get_month(month: Optional[str] = None, shift_location: Optional[str] = None) -> Dict[str, Any]:
    """The calendar: one row per employee, one cell per day."""
    _ensure_access()
    data = roster_service.get_month(month=_clean(month), shift_location=_clean(shift_location))
    data["success"] = True
    return data


@frappe.whitelist()
def get_month_hours(
    month: Optional[str] = None, shift_location: Optional[str] = None
) -> Dict[str, Any]:
    """Per-employee hours and overtime for the month, ready for payroll."""
    _ensure_access()
    data = roster_service.month_hours(month=_clean(month), shift_location=_clean(shift_location))
    data["success"] = True
    return data


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@frappe.whitelist()
def assign_shift(
    employee: str,
    date: str,
    shift_type: str,
    shift_location: Optional[str] = None,
) -> Dict[str, Any]:
    """Put one employee on one shift for one day.

    Used for both "swap Ali from Opening to Closing" and "move Ali to the other
    branch for a day" -- the second is the same operation with a different
    ``shift_location``, and it is the location that decides where their phone
    has to be to clock in.
    """
    _ensure_access()
    employee = _clean(employee)
    if not employee:
        frappe.throw(_("Employee is required."))
    day = _coerce_date(date, _("Date"))
    shift_type = _clean(shift_type)
    if not shift_type:
        frappe.throw(_("Shift type is required."))

    _ensure_employee_active(employee)
    _ensure_employee_in_scope(employee)

    result = roster_service.set_shift_for_day(employee, day, shift_type, _clean(shift_location))
    result["success"] = True
    result["employee"] = employee
    result["date"] = day
    return result


@frappe.whitelist()
def set_day_off(
    employee: str,
    date: str,
    off_type: str = "Weekly Off",
    covered_by: Optional[str] = None,
    cover_shift_type: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark somebody off and, in the same step, name who covers the day."""
    _ensure_access()
    employee = _clean(employee)
    if not employee:
        frappe.throw(_("Employee is required."))
    day = _coerce_date(date, _("Date"))
    covered_by = _clean(covered_by)

    _ensure_employee_active(employee)
    _ensure_employee_in_scope(employee)
    if covered_by:
        _ensure_employee_active(covered_by)
        _ensure_employee_in_scope(covered_by)

    result = roster_service.set_day_off(
        employee=employee,
        off_date=day,
        off_type=_clean(off_type) or "Weekly Off",
        covered_by=covered_by,
        cover_shift_type=_clean(cover_shift_type),
        notes=_clean(notes),
    )
    result["success"] = True
    return result


@frappe.whitelist()
def clear_day_off(employee: str, date: str) -> Dict[str, Any]:
    """Undo a day off, restoring both the person and whoever covered them."""
    _ensure_access()
    employee = _clean(employee)
    if not employee:
        frappe.throw(_("Employee is required."))
    day = _coerce_date(date, _("Date"))

    _ensure_employee_in_scope(employee)

    result = roster_service.clear_day_off(employee, day)
    result["success"] = True
    return result


@frappe.whitelist()
def bulk_assign(payload: Any) -> Dict[str, Any]:
    """Apply many single-day changes in one request.

    The screen edits a month at a time, and a manager filling a rota makes
    dozens of taps; sending them one by one turns a rota into dozens of round
    trips over a branch's mobile connection. Each row is applied independently
    and reported on its own, so one bad row (a resigned employee, a branch the
    caller cannot reach) does not discard the rest of the manager's work.
    """
    _ensure_access()
    rows = payload
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except Exception:
            frappe.throw(_("Invalid JSON payload."))
    if not isinstance(rows, list):
        frappe.throw(_("Payload must be a list of changes."))

    applied: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            failed.append({"index": index, "error": _("Malformed row")})
            continue

        # One savepoint per row. Frappe wraps the whole request in a single
        # transaction, so without this a row that throws half-way through a
        # break/insert pair would leave that pair torn AND poison every change
        # already applied above it in the same request.
        savepoint = f"roster_bulk_{index}"
        frappe.db.savepoint(savepoint)
        try:
            employee = _clean(row.get("employee"))
            day = _coerce_date(row.get("date"), _("Date"))
            if not employee:
                frappe.throw(_("Employee is required."))

            _ensure_employee_active(employee)
            _ensure_employee_in_scope(employee)

            action = str(row.get("action") or "assign").strip().lower()
            if action == "day_off":
                covered_by = _clean(row.get("covered_by"))
                if covered_by:
                    _ensure_employee_active(covered_by)
                    _ensure_employee_in_scope(covered_by)
                result = roster_service.set_day_off(
                    employee=employee,
                    off_date=day,
                    off_type=_clean(row.get("off_type")) or "Weekly Off",
                    covered_by=covered_by,
                    cover_shift_type=_clean(row.get("cover_shift_type")),
                    notes=_clean(row.get("notes")),
                )
            elif action == "clear_day_off":
                result = roster_service.clear_day_off(employee, day)
            else:
                shift_type = _clean(row.get("shift_type"))
                if not shift_type:
                    frappe.throw(_("Shift type is required."))
                result = roster_service.set_shift_for_day(
                    employee, day, shift_type, _clean(row.get("shift_location"))
                )
            applied.append({"index": index, "employee": employee, "date": day, "result": result})
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            failed.append(
                {
                    "index": index,
                    "employee": row.get("employee"),
                    "date": row.get("date"),
                    "error": str(exc),
                }
            )

    return {
        "success": True,
        "applied": applied,
        "failed": failed,
        "applied_count": len(applied),
        "failed_count": len(failed),
    }

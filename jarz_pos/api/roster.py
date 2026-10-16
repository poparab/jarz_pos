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
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate

from jarz_pos.services import branch_access as branch_access_service
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


def _clean(value: Any) -> str | None:
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
def get_bootstrap() -> dict[str, Any]:
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
		"shift_locations": roster_service.shift_locations(include_pos_access=True),
		"off_types": ["Weekly Off", "Vacation", "Sick", "Unpaid", "Other"],
		"scope": {
			"configured": roster_service.roster_scope_configured(),
			"unrestricted": allowed is None,
			"locations": None if allowed is None else sorted(allowed),
		},
	}


@frappe.whitelist()
def get_month(month: str | None = None, shift_location: str | None = None) -> dict[str, Any]:
	"""The calendar: one row per employee, one cell per day."""
	_ensure_access()
	data = roster_service.get_month(month=_clean(month), shift_location=_clean(shift_location))
	data["success"] = True
	return data


@frappe.whitelist()
def get_month_hours(month: str | None = None, shift_location: str | None = None) -> dict[str, Any]:
	"""Per-employee hours and overtime for the month, ready for payroll."""
	_ensure_access()
	data = roster_service.month_hours(month=_clean(month), shift_location=_clean(shift_location))
	data["success"] = True
	return data


def _ensure_location_allowed(shift_location: str | None) -> None:
	"""Refuse a branch outside the caller's scope.

	The plan names its branch per person, so without this a manager could
	roster their own staff onto -- and give them POS access at -- a branch
	they do not run.
	"""
	if not shift_location:
		return
	allowed = roster_service.allowed_shift_locations()
	if allowed is not None and shift_location not in allowed:
		frappe.throw(
			_("{0} is not one of your branches.").format(shift_location),
			frappe.PermissionError,
		)


@frappe.whitelist()
def get_plan_defaults(shift_location: str | None = None) -> dict[str, Any]:
	"""Pre-fill for the rota plan wizard: everybody's current week."""
	_ensure_access()
	shift_location = _clean(shift_location)
	_ensure_location_allowed(shift_location)
	data = roster_service.plan_defaults(shift_location=shift_location)
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
	shift_location: str | None = None,
	grant_pos_access: Any = 0,
) -> dict[str, Any]:
	"""Put one employee on one shift for one day.

	Used for both "swap Ali from Opening to Closing" and "move Ali to the other
	branch for a day" -- the second is the same operation with a different
	``shift_location``, and it is the location that decides where their phone
	has to be to clock in.

	``grant_pos_access`` also gives them POS access at that branch for the day.
	It is best-effort: the shift is saved whatever happens to the grant, and
	``pos_access`` in the response says what did.
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
	result["pos_access"] = None
	if branch_access_service.as_flag(grant_pos_access):
		result["pos_access"] = branch_access_service.grant_for_roster(
			employee,
			day,
			result.get("shift_location"),
			source=branch_access_service.SOURCE_SHIFT,
			notes=_("Rostered on {0}.").format(shift_type),
		)
	return result


@frappe.whitelist()
def set_day_off(
	employee: str,
	date: str,
	off_type: str = "Weekly Off",
	covered_by: str | None = None,
	cover_shift_type: str | None = None,
	notes: str | None = None,
	grant_pos_access: Any = 0,
) -> dict[str, Any]:
	"""Mark somebody off and, in the same step, name who covers the day.

	``grant_pos_access`` gives the COVERING colleague POS access at the branch
	the cover landed on, for that day. Best-effort, like ``assign_shift``: the
	day off and the cover are saved whatever happens to the grant.
	"""
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
	result["pos_access"] = None
	if branch_access_service.as_flag(grant_pos_access):
		if covered_by:
			result["pos_access"] = branch_access_service.grant_for_roster(
				covered_by,
				day,
				result.get("cover_shift_location"),
				source=branch_access_service.SOURCE_COVER,
				roster_day_off=result.get("day_off"),
				notes=_("Covering for {0}.").format(
					frappe.db.get_value("Employee", employee, "employee_name") or employee
				),
			)
		else:
			result["pos_access"] = {
				"requested": True,
				"granted": False,
				"status": None,
				"already_member": False,
				"reason": _("Nobody is covering this day, so there is nobody to give POS access to."),
				"pos_profile": None,
				"day_access": None,
			}
	return result


@frappe.whitelist()
def clear_day_off(employee: str, date: str) -> dict[str, Any]:
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
def bulk_assign(payload: Any) -> dict[str, Any]:
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

	applied: list[dict[str, Any]] = []
	failed: list[dict[str, Any]] = []

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


def _coerce_plan(payload: Any) -> dict[str, Any]:
	plan = payload
	if isinstance(plan, str):
		try:
			plan = json.loads(plan)
		except Exception:
			frappe.throw(_("Invalid JSON payload."))
	if not isinstance(plan, dict):
		frappe.throw(_("The plan must be an object."))
	plan = dict(plan)
	plan["start"] = _coerce_date(plan.get("start"), _("Start date"))
	plan["end"] = _coerce_date(plan.get("end"), _("End date"))
	rows = plan.get("rows")
	if not isinstance(rows, list) or not rows:
		frappe.throw(_("Pick at least one person."))
	return plan


#: How far back an assignment still makes a branch somebody's "home" for the
#: plan's scope test. Matches the window ``plan_defaults`` lists people from.
PLAN_HOME_LOOKBACK_DAYS = 60


def _ensure_plan_home_in_scope(employee: str) -> None:
	"""Stricter scope test for a person whose standing rota the plan replaces.

	``_ensure_employee_in_scope`` passes anybody who has EVER had an Active
	assignment at one of the caller's branches. That is fine for one day's
	edit, but the plan disables the person's standing rota and writes a new
	one, so a manager who once borrowed a Dokki cashier for a day could
	otherwise rewrite that cashier's whole Dokki rota. So:

	* every enabled Shift Schedule Assignment must sit at one of the caller's
	  branches; one outside is refused by name;
	* with no standing rota, an Active assignment in the last 60 days (or
	  ahead) must be at one of them.
	"""
	allowed = roster_service.allowed_shift_locations()
	if allowed is None:
		return
	name = frappe.db.get_value("Employee", employee, "employee_name") or employee

	try:
		schedule_locations = [
			row.get("shift_location")
			for row in frappe.get_all(
				"Shift Schedule Assignment",
				filters={"employee": employee, "enabled": 1},
				fields=["shift_location"],
			)
		]
	except Exception:
		schedule_locations = []
	for location in schedule_locations:
		if location and location not in allowed:
			frappe.throw(
				_("{0}'s standing rota is at {1}, outside your branches.").format(name, location),
				frappe.PermissionError,
			)
	if any(schedule_locations):
		return

	since = add_days(nowdate(), -PLAN_HOME_LOOKBACK_DAYS)
	recent = {
		row.get("shift_location")
		for row in frappe.get_all(
			"Shift Assignment",
			filters={"employee": employee, "docstatus": 1, "status": "Active"},
			or_filters=[["end_date", ">=", since], ["end_date", "is", "not set"]],
			fields=["shift_location"],
		)
		if row.get("shift_location")
	}
	if not recent.intersection(allowed):
		frappe.throw(
			_("{0} is not at one of your branches.").format(name),
			frappe.PermissionError,
		)


def _check_plan_scope(plan: dict[str, Any]) -> None:
	"""Every person, coverer and branch in the plan must be the caller's.

	The service writes with ``ignore_permissions``, so this is the whole
	authorisation for a request that can rewrite a branch's month. People the
	plan replaces get the stricter home test; coverers, whose own rota is not
	replaced, get the usual one.
	"""
	checked = set()

	def check(employee: str | None, planned: bool = False) -> None:
		if not employee:
			return
		if employee not in checked:
			checked.add(employee)
			_ensure_employee_active(employee)
			_ensure_employee_in_scope(employee)
		if planned:
			_ensure_plan_home_in_scope(employee)

	for row in plan["rows"]:
		if not isinstance(row, dict):
			frappe.throw(_("Malformed plan row."))
		check(_clean(row.get("employee")), planned=True)
		_ensure_location_allowed(_clean(row.get("shift_location")))
		for cover in row.get("covers") or []:
			if isinstance(cover, dict):
				check(_clean(cover.get("covered_by")))


def _wants_apply(dry_run: Any) -> bool:
	"""Only an explicit "no dry run" writes.

	``as_flag`` would read "", "null" or "none" as false, and a client that
	sends one of those by accident must get a preview, never a rewritten month.
	"""
	if dry_run is False:
		return True
	if isinstance(dry_run, (int, float)) and not isinstance(dry_run, bool):
		return dry_run == 0
	if isinstance(dry_run, str):
		return dry_run.strip().lower() in ("0", "false", "no")
	return False


@frappe.whitelist()
def apply_plan(payload: Any, dry_run: Any = 1) -> dict[str, Any]:
	"""Preview (``dry_run=1``, the default) or write a rota plan.

	The default is the preview so that a client which forgets the flag gets a
	harmless answer instead of a rewritten month. ``grant_pos_access`` in the
	payload gives every coverer POS day access at the branch the cover landed
	on; best-effort, as in ``set_day_off`` -- a refused grant never undoes the
	rota, it is listed under ``pos_access_failed``.
	"""
	_ensure_access()
	plan = _coerce_plan(payload)
	_check_plan_scope(plan)

	if not _wants_apply(dry_run):
		result = roster_service.plan_preview(plan)
		# People outside the plan whose day would change are checked here too,
		# so the manager learns about a refusal at preview, not at Apply.
		for employee in result.get("touched_outside") or []:
			_ensure_employee_in_scope(employee)
		result["success"] = True
		return result

	result = roster_service.apply_plan(plan, check_employee=_ensure_employee_in_scope)
	result["success"] = True
	result["pos_access"] = []
	result["pos_access_failed"] = []
	if not branch_access_service.as_flag(plan.get("grant_pos_access")):
		return result

	names: dict[str, str] = {}
	for cover in result.get("created_covers") or []:
		employee = cover["employee"]
		if employee not in names:
			names[employee] = frappe.db.get_value("Employee", employee, "employee_name") or employee
		try:
			grant = branch_access_service.grant_for_roster(
				cover["covered_by"],
				cover["date"],
				cover.get("cover_shift_location"),
				source=branch_access_service.SOURCE_COVER,
				roster_day_off=cover.get("day_off"),
				notes=_("Covering for {0}.").format(names[employee]),
			)
		except Exception as exc:
			grant = None
			reason = str(exc)
		else:
			reason = (grant or {}).get("reason")
		result["pos_access"].append(dict(cover, result=grant))
		if not grant or not (grant.get("granted") or grant.get("already_member")):
			result["pos_access_failed"].append(
				{"covered_by": cover["covered_by"], "date": cover["date"], "reason": reason}
			)
	return result

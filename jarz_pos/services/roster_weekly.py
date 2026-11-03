"""Repeat one day's roster change every week, for good.

A manager editing "Belal, Sunday" usually means *every* Sunday from now on,
not just the one they tapped. Doing that by hand is a dozen taps up to the
roster horizon and is still wrong afterwards, because HRMS keeps generating
Sundays from the person's standing rota (their Shift Schedule Assignments)
and puts the old shift straight back.

So a weekly change has two halves, applied together:

1. **Every same weekday that already exists** -- from the chosen day up to the
   last day HRMS has generated -- is rewritten with the ordinary single-day
   operations (``set_shift_for_day`` / ``set_day_off``), so the calendar, the
   check-in gate and the day-off records all agree immediately.
2. **The standing rota is re-patterned** so HRMS carries the change on from
   the day after that horizon: the weekday leaves the schedule that held it
   and, for a shift change, joins a schedule for the new shift. A day off with
   a cover re-patterns the coverer too, so the cover also repeats forever.

The horizon rule is the same one the plan wizard lives by: a new schedule must
start after every day that already holds something for that person, or HRMS's
first generated range overlaps it -- and HRMS only logs an overlap, so one
clash stops that schedule generating forever.

One-off edits are kept. A day that somebody shaped by hand (a one-off day off,
a cover of a colleague, a swapped shift, an emptied day) is skipped and listed,
unless the manager asks to replace those too. The preview and the apply share
``_compute`` so what the preview lists as kept is exactly what the apply keeps.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date as date_cls
from datetime import timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import getdate

from jarz_pos.services import roster

ACTION_ASSIGN = "assign"
ACTION_DAY_OFF = "day_off"

WEEKLY_OFF = "Weekly Off"

#: How far past the horizon day-off records are written. They follow the
#: records already on file (the standing rota's run to 2027-12-14), but a
#: single mistyped far-future record must not turn one request into thousands.
DAY_OFF_REACH = timedelta(weeks=60)

WARN_OLD_COVERER = "old_coverer_still_rostered"
WARN_UNCOVERED = "cover_kept_own_days"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _schedule_rows(employee: str) -> list[frappe._dict]:
	"""Enabled standing-rota rows, each with its shift type and weekdays."""
	rows = frappe.get_all(
		"Shift Schedule Assignment",
		filters={"enabled": 1, "employee": employee},
		fields=["name", "company", "shift_schedule", "shift_location", "create_shifts_after"],
		order_by="creation asc",
	)
	out: list[frappe._dict] = []
	for row in rows:
		shape = roster._schedule_shape(row.get("shift_schedule"))
		if not shape:
			continue
		row = frappe._dict(row)
		row.shift_type, row.days = shape
		row.shift_location = row.get("shift_location") or None
		out.append(row)
	return out


def _load_person(employee: str) -> frappe._dict:
	rows = _schedule_rows(employee)
	pattern: dict[str, tuple[str, str | None]] = {}
	generated: date_cls | None = None
	for row in rows:
		for day in row.days:
			pattern.setdefault(day, (row.shift_type, row.shift_location))
		after = roster._as_date(row.get("create_shifts_after"))
		if after and (generated is None or after > generated):
			generated = after

	open_ended = frappe.db.count(
		"Shift Assignment",
		filters=dict(roster.ACTIVE_ASSIGNMENT_FILTERS, employee=employee, end_date=("is", "not set")),
	)
	last = frappe.db.sql(
		"""select max(end_date) from `tabShift Assignment`
		where employee = %s and docstatus = 1 and status = 'Active'""",
		(employee,),
	)
	return frappe._dict(
		employee=employee,
		name=frappe.db.get_value("Employee", employee, "employee_name") or employee,
		rows=rows,
		pattern=pattern,
		generated=generated,
		last_assignment=roster._as_date(last[0][0]) if last and last[0] else None,
		open_ended=bool(open_ended),
	)


def _records(filters: dict[str, Any]) -> list[frappe._dict]:
	return [
		frappe._dict(r)
		for r in frappe.get_all(
			roster.DAY_OFF_DOCTYPE,
			filters=filters,
			fields=[
				"name",
				"employee",
				"employee_name",
				"off_date",
				"off_type",
				"from_plan",
				"original_shift_type",
				"original_shift_location",
				"shift_location",
				"covered_by",
				"cover_shift_type",
				"cover_previous_shift_type",
				"cover_previous_shift_location",
				"cover_shift_location",
			],
		)
	]


def _own_days_off(employee: str, start: date_cls, weekday: str) -> dict[date_cls, frappe._dict]:
	out = {}
	for record in _records({"employee": employee, "off_date": (">=", start)}):
		day = getdate(record.off_date)
		if roster._weekday(day) == weekday:
			out[day] = record
	return out


def _covering(employee: str, start: date_cls, weekday: str) -> dict[date_cls, list[frappe._dict]]:
	out: dict[date_cls, list[frappe._dict]] = {}
	for record in _records({"covered_by": employee, "off_date": (">=", start)}):
		day = getdate(record.off_date)
		if roster._weekday(day) == weekday:
			out.setdefault(day, []).append(record)
	return out


def _last_day_off_on_file() -> date_cls | None:
	row = frappe.db.sql(f"select max(off_date) from `tab{roster.DAY_OFF_DOCTYPE}`")
	return roster._as_date(row[0][0]) if row and row[0] else None


def _weekly_dates(start: date_cls, end: date_cls) -> list[date_cls]:
	out = []
	day = start
	while day <= end:
		out.append(day)
		day = day + timedelta(days=7)
	return out


def _horizon(person: frappe._dict, start: date_cls) -> date_cls:
	"""The last day that can already hold something for this person.

	Anything written explicitly must stop here, and the new schedule starts the
	day after. It covers both what HRMS generated and any explicit assignment
	written past that (a cover far ahead), so the new schedule never lands on
	a day that is already taken.
	"""
	return max(d for d in (person.generated, person.last_assignment, start) if d)


def _label(entry: tuple[str, str | None] | None) -> str:
	if not entry:
		return _("Off")
	shift_type, location = entry
	return f"{shift_type} @ {location}" if location else shift_type


# ---------------------------------------------------------------------------
# One-off detection
# ---------------------------------------------------------------------------


def _kept_entry(
	person: frappe._dict, day: date_cls, kind: str, current: str, records: list | None = None
) -> dict[str, Any]:
	return {
		"date": day,
		"employee": person.employee,
		"employee_name": person.name,
		"kind": kind,
		"current": current,
		"records": records or [],
	}


def _one_off(
	person: frappe._dict,
	day: date_cls,
	weekday: str,
	own: frappe._dict | None,
	covering: list[frappe._dict],
	explicit_through: date_cls,
	ignore_cover_of: str | None = None,
) -> dict[str, Any] | None:
	"""What makes this day a one-off for this person, or None.

	Same rules as the plan wizard's ``_manual_days``, judged against the
	person's standing weekly pattern:

	* a day off on a weekday their rota normally works is a one-off (leave,
	  sick day); a day off on a weekday their rota never works IS the pattern;
	* covering a colleague is always listed -- it is somebody else's day too;
	* an assignment not linked to a schedule and not matching the pattern is a
	  hand-made swap (an unlinked one that matches is the tail of a split);
	* an empty day the rota should have filled was emptied by hand.

	``ignore_cover_of`` skips the cover of the person whose day off is being
	repeated: their coverer covering them is the relationship being rewritten.
	"""
	expected = person.pattern.get(weekday)
	if own:
		# Leave or sickness booked on their usual day off is still somebody's
		# decision about that date, not the weekly pattern.
		booked = (own.get("off_type") or WEEKLY_OFF) != WEEKLY_OFF
		if booked or (expected and not own.get("from_plan")):
			return _kept_entry(person, day, roster.KIND_DAY_OFF, own.get("off_type") or _("Off"), [own])
		return None

	others = [r for r in covering if r.employee != ignore_cover_of]
	if others:
		names = ", ".join(r.employee_name or r.employee for r in others)
		return _kept_entry(person, day, roster.KIND_COVER, _("Covering {0}").format(names), others)

	if day > explicit_through:
		return None
	assignment = roster._assignment_on(person.employee, day)
	if assignment:
		if ignore_cover_of and any(r.employee == ignore_cover_of for r in covering):
			return None
		if (
			person.pattern
			and not assignment.get("shift_schedule_assignment")
			and not roster._rota_matches(assignment, expected)
		):
			return _kept_entry(person, day, roster.KIND_SHIFT, assignment.get("shift_type") or "")
		return None
	if expected and person.generated and day <= person.generated:
		return _kept_entry(person, day, roster.KIND_EMPTY, _("Empty"))
	return None


# ---------------------------------------------------------------------------
# The plan both preview and apply use
# ---------------------------------------------------------------------------


def _coerce(change: dict[str, Any]) -> dict[str, Any]:
	employee = roster._text(change.get("employee"))
	if not employee:
		frappe.throw(_("Employee is required."))
	start = roster._as_date(change.get("date"))
	if not start:
		frappe.throw(_("Date is required."))
	if start < roster._today():
		frappe.throw(_("A weekly change must start today or later."))
	action = str(change.get("action") or ACTION_ASSIGN).strip().lower()
	if action not in (ACTION_ASSIGN, ACTION_DAY_OFF):
		frappe.throw(_("Unknown action {0}.").format(action))

	out = {
		"employee": employee,
		"start": start,
		"weekday": roster._weekday(start),
		"action": action,
		"replace_manual": roster._flag(change.get("replace_manual")),
		"shift_type": None,
		"shift_location": None,
		"off_type": roster._text(change.get("off_type")) or "Weekly Off",
		"covered_by": None,
		"cover_shift_type": None,
		"notes": roster._text(change.get("notes")),
	}
	if action == ACTION_ASSIGN:
		out["shift_type"] = roster._text(change.get("shift_type"))
		if not out["shift_type"]:
			frappe.throw(_("Shift type is required."))
		out["shift_location"] = roster._text(change.get("shift_location"))
	else:
		out["covered_by"] = roster._text(change.get("covered_by"))
		if out["covered_by"]:
			if out["covered_by"] == employee:
				frappe.throw(_("An employee cannot cover their own day off."))
			out["cover_shift_type"] = roster._text(change.get("cover_shift_type"))
			if not out["cover_shift_type"]:
				frappe.throw(_("Pick the shift the covering colleague moves onto."))

	# Refused here, before the preview, rather than half-way through the apply
	# when the new schedule is inserted.
	for shift_type in (out["shift_type"], out["cover_shift_type"]):
		if shift_type and not frappe.db.exists("Shift Type", shift_type):
			frappe.throw(_("Shift type {0} does not exist.").format(shift_type))
	if out["shift_location"] and not frappe.db.exists("Shift Location", out["shift_location"]):
		frappe.throw(_("Branch {0} does not exist.").format(out["shift_location"]))
	return out


def _check_person(person: frappe._dict, start: date_cls) -> None:
	if person.open_ended:
		frappe.throw(
			_("{0} has a shift with no end date. Give it an end date before repeating a change weekly.").format(
				person.name
			)
		)
	if person.generated and start > person.generated:
		frappe.throw(
			_("{0}'s rota is only generated up to {1}. Pick a day on or before it.").format(
				person.name, person.generated
			)
		)


def _compute(change: dict[str, Any]) -> dict[str, Any]:
	start, weekday = change["start"], change["weekday"]
	replace = change["replace_manual"]

	person = _load_person(change["employee"])
	_check_person(person, start)
	through = _horizon(person, start)

	coverer = None
	cover_through = None
	if change["covered_by"]:
		coverer = _load_person(change["covered_by"])
		_check_person(coverer, start)
		cover_through = max(_horizon(coverer, start), through)

	before = person.pattern.get(weekday)
	location = before[1] if before else None
	home = location or roster._schedule_location(person.employee)

	own = _own_days_off(person.employee, start, weekday)
	covering = _covering(person.employee, start, weekday)

	if change["action"] == ACTION_ASSIGN:
		target = (change["shift_type"], change["shift_location"] or home)
		last = max([through, *own.keys(), *covering.keys()])
	else:
		target = None
		explicit_end = cover_through or through
		on_file = _last_day_off_on_file()
		if on_file:
			on_file = min(on_file, start + DAY_OFF_REACH)
		last = max(d for d in (explicit_end, on_file) if d)

	c_own: dict[date_cls, frappe._dict] = {}
	c_covering: dict[date_cls, list[frappe._dict]] = {}
	if coverer:
		c_own = _own_days_off(coverer.employee, start, weekday)
		c_covering = _covering(coverer.employee, start, weekday)

	ops: list[dict[str, Any]] = []
	kept: list[dict[str, Any]] = []
	old_coverers: dict[str, list[date_cls]] = {}
	uncovered: list[date_cls] = []
	touched: set[str] = set()

	for day in _weekly_dates(start, last):
		rec = own.get(day)
		one_off = _one_off(person, day, weekday, rec, covering.get(day, []), through)
		if one_off and not replace:
			kept.append(one_off)
			continue
		detach = list(one_off["records"]) if one_off and one_off["kind"] == roster.KIND_COVER else []

		op = {"date": day, "own": rec, "detach": detach, "cover": False, "coverer_own": None}
		if rec and rec.covered_by and rec.covered_by != change["covered_by"]:
			old_coverers.setdefault(rec.covered_by, []).append(day)

		if coverer:
			c_rec = c_own.get(day)
			c_one_off = _one_off(
				coverer,
				day,
				weekday,
				c_rec,
				c_covering.get(day, []),
				cover_through,
				ignore_cover_of=person.employee,
			)
			if c_one_off and not replace:
				kept.append(c_one_off)
				uncovered.append(day)
			else:
				op["cover"] = True
				op["coverer_own"] = c_rec
				if c_one_off and c_one_off["kind"] == roster.KIND_COVER:
					op["detach"].extend(c_one_off["records"])
		for record in op["detach"]:
			touched.add(record.employee)
		ops.append(op)

	warnings: list[dict[str, Any]] = []
	for employee, days in old_coverers.items():
		name = frappe.db.get_value("Employee", employee, "employee_name") or employee
		warnings.append(
			{
				"code": WARN_OLD_COVERER,
				"employee": employee,
				"dates": [str(d) for d in days],
				"message": _(
					"{0} covered {1} on {2}s and stays on that cover shift. Change {0}'s {2} too if they are no longer needed."
				).format(name, person.name, _(weekday)),
			}
		)
	if uncovered:
		warnings.append(
			{
				"code": WARN_UNCOVERED,
				"employee": coverer.employee,
				"dates": [str(d) for d in uncovered],
				"message": _("{0} keeps their own plans on {1} day(s), so {2} is off with nobody covering then.").format(
					coverer.name, len(uncovered), person.name
				),
			}
		)

	standing = [
		{
			"employee": person.employee,
			"employee_name": person.name,
			"weekday": weekday,
			"before": _label(before),
			"after": _label(target),
		}
	]
	cover_target = None
	if coverer:
		cover_target = (change["cover_shift_type"], home)
		standing.append(
			{
				"employee": coverer.employee,
				"employee_name": coverer.name,
				"weekday": weekday,
				"before": _label(coverer.pattern.get(weekday)),
				"after": _label(cover_target),
			}
		)

	return {
		"change": change,
		"person": person,
		"coverer": coverer,
		"through": through,
		"cover_through": cover_through,
		"home": home,
		"target": target,
		"cover_target": cover_target,
		"ops": ops,
		"kept": kept,
		"warnings": warnings,
		"standing": standing,
		"touched": touched,
	}


def _summary(plan: dict[str, Any], applied: bool) -> dict[str, Any]:
	ops = plan["ops"]
	return {
		"dry_run": not applied,
		"applied": applied,
		"employee": plan["person"].employee,
		"employee_name": plan["person"].name,
		"action": plan["change"]["action"],
		"weekday": plan["change"]["weekday"],
		"start": str(plan["change"]["start"]),
		"through": str(ops[-1]["date"]) if ops else None,
		"rota_from": str(plan["through"] + timedelta(days=1)),
		"change_count": len(ops),
		"dates": [str(op["date"]) for op in ops],
		"replace_manual": plan["change"]["replace_manual"],
		"kept": [
			{k: (str(v) if k == "date" else v) for k, v in entry.items() if k != "records"}
			for entry in plan["kept"]
		],
		"kept_count": len(plan["kept"]),
		"warnings": plan["warnings"],
		"standing": plan["standing"],
		"touched_outside": sorted(plan["touched"]),
	}


def preview(change: dict[str, Any]) -> dict[str, Any]:
	return _summary(_compute(_coerce(change)), applied=False)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _discard(record: frappe._dict) -> None:
	if frappe.db.exists(roster.DAY_OFF_DOCTYPE, record.name):
		roster._release_pos_access(record.name)
		frappe.delete_doc(roster.DAY_OFF_DOCTYPE, record.name, ignore_permissions=True)


def _detach_cover(record: frappe._dict) -> None:
	"""Take the cover off a colleague's day off; their day off itself stays."""
	if not frappe.db.exists(roster.DAY_OFF_DOCTYPE, record.name):
		return
	roster._release_pos_access(record.name)
	frappe.db.set_value(
		roster.DAY_OFF_DOCTYPE,
		record.name,
		{
			"covered_by": None,
			"covered_by_name": None,
			"cover_shift_type": None,
			"cover_shift_location": None,
			"cover_previous_shift_type": None,
			"cover_previous_shift_location": None,
		},
	)


def _insert_day_off_ahead(
	plan: dict[str, Any],
	day: date_cls,
	original: tuple[str | None, str | None],
	cover_previous: tuple[str | None, str | None],
) -> None:
	"""A day-off row past every horizon, where no assignment exists yet.

	Nothing is broken or rostered here: the re-patterned schedules generate
	the right shifts when HRMS gets this far. The row still records the cover
	and what to restore, exactly as ``set_day_off`` would have.
	"""
	change = plan["change"]
	doc = frappe.new_doc(roster.DAY_OFF_DOCTYPE)
	doc.employee = change["employee"]
	doc.off_date = day
	doc.off_type = change["off_type"]
	doc.shift_location = plan["home"]
	doc.original_shift_type = original[0]
	doc.original_shift_location = original[1]
	doc.covered_by = change["covered_by"]
	doc.cover_shift_type = change["cover_shift_type"]
	doc.cover_previous_shift_type = cover_previous[0]
	doc.cover_previous_shift_location = cover_previous[1]
	doc.cover_shift_location = plan["home"]
	doc.notes = change["notes"]
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)


def _write_day_off(plan: dict[str, Any], op: dict[str, Any]) -> None:
	change = plan["change"]
	day = op["date"]
	rec = op["own"]
	before = plan["person"].pattern.get(change["weekday"])
	original = (
		(rec.original_shift_type, rec.original_shift_location)
		if rec
		else ((before[0], before[1]) if before else (None, None))
	)
	if any(r.covered_by == change["employee"] for r in op["detach"]):
		# They were on a colleague's cover that day. Undoing this day off
		# must put them back on their own week, not on that cover.
		original = before or (None, None)
	cover_previous = None
	if rec and rec.covered_by and rec.covered_by == change["covered_by"]:
		# The same colleague already covered: their "previous" is what they
		# were on before that cover, not the cover shift they are on now.
		cover_previous = (rec.cover_previous_shift_type, rec.cover_previous_shift_location)

	if rec:
		_discard(rec)
	for record in op["detach"]:
		_detach_cover(record)

	if op["cover"] and day > plan["cover_through"]:
		if op["coverer_own"]:
			_discard(op["coverer_own"])
		if cover_previous is None:
			cover_previous = plan["coverer"].pattern.get(change["weekday"]) or (None, None)
		_insert_day_off_ahead(plan, day, original, cover_previous)
		return

	result = roster.set_day_off(
		employee=change["employee"],
		off_date=day,
		off_type=change["off_type"],
		covered_by=change["covered_by"] if op["cover"] else None,
		cover_shift_type=change["cover_shift_type"] if op["cover"] else None,
		notes=change["notes"],
		original_shift_type=original[0],
		original_shift_location=original[1] or plan["home"],
	)
	if op["cover"] and cover_previous is not None:
		frappe.db.set_value(
			roster.DAY_OFF_DOCTYPE,
			result["day_off"],
			{
				"cover_previous_shift_type": cover_previous[0],
				"cover_previous_shift_location": cover_previous[1],
			},
		)


def _write_shift(plan: dict[str, Any], op: dict[str, Any]) -> None:
	day = op["date"]
	for record in op["detach"]:
		_detach_cover(record)
	if day <= plan["through"]:
		# Discards a day-off row on that date itself.
		shift_type, location = plan["target"]
		roster.set_shift_for_day(plan["change"]["employee"], day, shift_type, location)
	elif op["own"]:
		# Past the horizon the new schedule generates the shift; only the
		# day-off row that would contradict it has to go.
		_discard(op["own"])


def _repattern(
	person: frappe._dict, weekday: str, target: tuple[str, str | None] | None, through: date_cls
) -> dict[str, Any]:
	"""Move ``weekday`` in the person's standing rota to ``target`` (None = off).

	Old rows are disabled with ``db.set_value``, as the plan wizard does, so
	their already-generated assignments stay untouched. The row that held the
	weekday is recreated without it, keeping its own ``create_shifts_after``:
	it carries on generating the other days exactly where it was. The weekday
	joins a matching schedule only when that one is already at ``through``;
	otherwise it gets a schedule of its own starting after ``through``, which
	is the one start date guaranteed not to overlap anything.
	"""
	rows = person.rows
	holders = [r for r in rows if weekday in r.days]
	if (
		target
		and holders
		and (holders[0].shift_type, holders[0].shift_location) == target
		and (roster._as_date(holders[0].get("create_shifts_after")) or through) >= through
	):
		# Already right AND already generated as far as the explicit writes go.
		# A row behind ``through`` would regenerate the days just written.
		return {"changed": False}
	if not target and not holders:
		return {"changed": False}

	company = roster._employee_company(person.employee)
	created: list[str] = []
	for row in holders:
		frappe.db.set_value("Shift Schedule Assignment", row.name, "enabled", 0)
		rest = [d for d in row.days if d != weekday]
		if rest:
			schedule = roster._find_or_create_shift_schedule(row.shift_type, rest)
			created.append(
				roster._insert_schedule_assignment(
					person.employee,
					row.company or company,
					schedule,
					row.shift_location,
					roster._as_date(row.get("create_shifts_after")),
				)
			)

	if target:
		shift_type, location = target
		mate = next(
			(
				r
				for r in rows
				if weekday not in r.days
				and r.shift_type == shift_type
				and r.shift_location == location
				and roster._as_date(r.get("create_shifts_after")) == through
			),
			None,
		)
		days = [weekday]
		if mate:
			frappe.db.set_value("Shift Schedule Assignment", mate.name, "enabled", 0)
			days = sorted(set(mate.days) | {weekday}, key=roster.WEEKDAYS.index)
		schedule = roster._find_or_create_shift_schedule(shift_type, days)
		created.append(
			roster._insert_schedule_assignment(
				person.employee, (mate.company if mate else None) or company, schedule, location, through
			)
		)
	return {"changed": True, "created": created}


def apply(
	change: dict[str, Any], check_employee: Callable[[str], None] | None = None
) -> dict[str, Any]:
	"""Write a weekly change. All-or-nothing."""
	plan = _compute(_coerce(change))
	if check_employee:
		for employee in plan["touched"]:
			check_employee(employee)

	change = plan["change"]
	weekday = change["weekday"]
	mark = roster._message_mark()
	savepoint = "roster_weekly_change"
	frappe.db.savepoint(savepoint)
	try:
		for op in plan["ops"]:
			if change["action"] == ACTION_ASSIGN:
				_write_shift(plan, op)
			else:
				_write_day_off(plan, op)

		if change["action"] == ACTION_ASSIGN:
			_repattern(plan["person"], weekday, plan["target"], plan["through"])
		else:
			_repattern(plan["person"], weekday, None, plan["through"])
			if plan["coverer"]:
				_repattern(plan["coverer"], weekday, plan["cover_target"], plan["cover_through"])
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	# HRMS msgprints a line per assignment it creates; the summary says it all.
	roster._message_reset(mark)

	return _summary(plan, applied=True)

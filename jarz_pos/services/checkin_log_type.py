"""Turn a stale "Check Out" into the "Check In" the employee actually meant.

The problem
-----------
The HRMS PWA (``/hrms``, upstream code this app does not fork) decides which
button to show from the employee's LAST Employee Checkin: last row IN -> it
offers "Check Out". So when somebody forgets to check out at the end of a
shift, the next day the PWA greets them with "Check Out"; they press it on
arrival, notice, and press "Check In" seconds later. Production (10-20 Oct
2026) has exactly that shape, e.g. 18 Oct 16:03 IN, then 19 Oct 12:37 OUT +
12:37 IN, then 19 Oct 21:16 OUT. The OUT at 12:37 is not a departure from the
18th's shift, it is an arrival.

The rules (see ``decide``)
--------------------------
Judged against the employee's latest check-in strictly BEFORE this one, looking
back at most ``LOOKBACK`` (7 days). Only an incoming ``OUT`` is ever touched.

* previous is IN, more than ``STALE_IN_AFTER`` (16 h) earlier -> IN. The longest
  real shift here is ~12.5 h (one 13.6 h day exists), so an OUT 16 h after the
  IN cannot be the end of that shift: yesterday's shift was never closed.
* previous is OUT, more than ``NEW_SHIFT_AFTER_OUT`` (4 h) earlier -> IN. Two
  departures in a row hours apart cannot both be departures; the second is a
  new shift starting.
* no check-in at all in the lookback window -> IN.
* otherwise the OUT stands (a normal departure, a quick double tap of OUT at the
  end of a shift, or a previous row with a blank log_type).

A conversion is recorded on ``custom_original_log_type`` ("OUT") and
``custom_log_type_note`` (a sentence a manager can read), so nothing is
rewritten silently.

The one refusal
---------------
After a conversion the PWA shows "Check Out" again (the last row is now an IN),
and a confused employee presses it a second time. An OUT less than
``DOUBLE_PRESS_WINDOW`` (10 min) after an IN is refused with a friendly
message instead of being stored as a 30-second shift. That refusal is the ONLY
thing in this module allowed to raise; every other failure is logged and the
check-in goes through untouched.

Live presses only
-----------------
The rules judge a press against the clock, so they apply only to a check-in
stamped within ``LIVE_PRESS_WINDOW`` of the server's now, and never during a
data import, patch or migrate. A manager keying a missing pair in Desk, a Data
Import or a biometric backfill is stored exactly as entered.

Ordering
--------
Frappe's ``Document.insert`` runs ``before_insert`` before
``run_before_save_methods`` (``before_validate`` -> ``validate``), so HRMS's own
validate (duplicate check, ``fetch_shift``, geofence) and our roster gate
(``events/employee_checkin.enforce_roster_on_checkin``) all see the corrected
log_type.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

CHECKIN_DOCTYPE = "Employee Checkin"

LOG_IN = "IN"
LOG_OUT = "OUT"

ORIGINAL_LOG_TYPE_FIELD = "custom_original_log_type"
LOG_TYPE_NOTE_FIELD = "custom_log_type_note"

#: Read by the attendance service when the columns exist on the site.
CHECKIN_LOG_TYPE_FIELDS = (ORIGINAL_LOG_TYPE_FIELD, LOG_TYPE_NOTE_FIELD)

#: An OUT this long after an IN cannot close that shift (longest real one ~13.6 h).
STALE_IN_AFTER = timedelta(hours=16)
#: An OUT this long after another OUT is a new shift starting.
NEW_SHIFT_AFTER_OUT = timedelta(hours=4)
#: How far back the previous check-in is looked for.
LOOKBACK = timedelta(days=7)
#: An OUT this soon after an IN is a second press, not a departure.
DOUBLE_PRESS_WINDOW = timedelta(minutes=10)
#: A check-in stamped further than this from now is a backfill, not a press.
LIVE_PRESS_WINDOW = timedelta(minutes=30)

ACTION_KEEP = "keep"
ACTION_CONVERT = "convert"
ACTION_REFUSE = "refuse"

REASON_STALE_IN = "stale_in"
REASON_AFTER_OUT = "after_out"
REASON_NO_PREVIOUS = "no_previous"


class CheckOutTooSoonError(frappe.ValidationError):
	"""Check Out pressed within minutes of a Check In: the person is on shift."""


def _safe_log(title: str) -> None:
	"""``frappe.log_error`` can itself raise (e.g. DB gone); never let it."""
	try:
		frappe.log_error(title=title)
	except Exception:
		pass


def _norm(log_type: Any) -> str:
	return str(log_type or "").strip().upper()


def _clock(moment: datetime) -> str:
	return moment.strftime("%H:%M")


def _day_label(moment: Any) -> str:
	"""``18 Oct`` -- no leading zero, no year (the board already shows the year)."""
	day = getdate(moment)
	return f"{day.day} {day.strftime('%b')}"


def _stamp(moment: datetime) -> str:
	return f"{_day_label(moment)} {_clock(moment)}"


def _whole_hours(delta: timedelta) -> int:
	return int(delta.total_seconds() // 3600)


# ---------------------------------------------------------------------------
# The decision (pure)
# ---------------------------------------------------------------------------


def decide(when: Any, previous: dict[str, Any] | None) -> tuple[str, str | None]:
	"""What to do with an incoming OUT at ``when``.

	Returns ``(action, reason)``: ``action`` is one of ``ACTION_KEEP``,
	``ACTION_CONVERT``, ``ACTION_REFUSE``; ``reason`` is set for a conversion.
	Boundaries are strict: exactly 16 h after an IN, or exactly 4 h after an OUT,
	keeps the OUT; exactly 10 min after an IN is a real (if short) departure.
	"""
	if not previous:
		return ACTION_CONVERT, REASON_NO_PREVIOUS

	prev_time = get_datetime(previous.get("time")) if previous.get("time") else None
	if prev_time is None:
		return ACTION_KEEP, None
	gap = get_datetime(when) - prev_time
	prev_type = _norm(previous.get("log_type"))

	if prev_type == LOG_IN:
		if gap > STALE_IN_AFTER:
			return ACTION_CONVERT, REASON_STALE_IN
		if gap < DOUBLE_PRESS_WINDOW:
			return ACTION_REFUSE, None
		return ACTION_KEEP, None
	if prev_type == LOG_OUT:
		if gap > NEW_SHIFT_AFTER_OUT:
			return ACTION_CONVERT, REASON_AFTER_OUT
		return ACTION_KEEP, None
	# Blank / unknown previous log_type: no basis to second-guess the press.
	return ACTION_KEEP, None


def note_for(reason: str, when: Any, previous: dict[str, Any] | None) -> str:
	"""The sentence stored on ``custom_log_type_note`` for a conversion."""
	when = get_datetime(when)
	if reason == REASON_NO_PREVIOUS or not previous:
		return (
			f"Pressed Check Out at {_clock(when)} but there was no check-in in the "
			f"{LOOKBACK.days} days before it, so this was recorded as a Check In."
		)

	prev_time = get_datetime(previous.get("time"))
	hours = _whole_hours(when - prev_time)
	if reason == REASON_STALE_IN:
		# The shift the open IN belongs to: HRMS's shift_start date when it
		# matched one, else the wall clock -- the same rule the board groups by.
		shift_day = previous.get("shift_start") or prev_time
		return (
			f"Pressed Check Out at {_clock(when)} but the last check-in was {hours} h "
			f"earlier ({_stamp(prev_time)}), so this was recorded as a Check In. "
			f"The shift on {_day_label(shift_day)} has no check-out."
		)
	return (
		f"Pressed Check Out at {_clock(when)} but the last punch was already a Check Out "
		f"{hours} h earlier ({_stamp(prev_time)}), so this was recorded as a Check In "
		"for a new shift."
	)


def refusal_message(previous: dict[str, Any]) -> str:
	clock = _clock(get_datetime(previous.get("time")))
	message = _("You checked in at {0}. You are on shift now — press Check Out when your shift ends.")
	return message.format(clock)


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


def correct_log_type(doc, method: str | None = None) -> None:
	"""``Employee Checkin.before_insert`` hook.

	Never raises on its own failures; raises ``CheckOutTooSoonError`` only for
	the deliberate second-press refusal.
	"""
	try:
		refusal = _correct(doc)
	except Exception:
		_safe_log("Check-in log type correction failed")
		return
	if refusal:
		frappe.throw(refusal, exc=CheckOutTooSoonError, title=_("Already Checked In"))


def _columns_ready() -> bool:
	meta = frappe.get_meta(CHECKIN_DOCTYPE)
	return all(meta.has_field(field) for field in CHECKIN_LOG_TYPE_FIELDS)


def _previous_checkin(employee: str, when: datetime, name: str | None) -> dict[str, Any] | None:
	"""The employee's latest check-in strictly before ``when``, within ``LOOKBACK``.

	One indexed read, ``limit 1``: this runs on every check-in.
	"""
	filters: list[list[Any]] = [
		["employee", "=", employee],
		["time", "<", when],
		["time", ">=", when - LOOKBACK],
	]
	if name:
		filters.append(["name", "!=", name])
	rows = frappe.db.get_all(
		CHECKIN_DOCTYPE,
		filters=filters,
		fields=["name", "time", "log_type", "shift_start"],
		order_by="time desc",
		limit=1,
	)
	return rows[0] if rows else None


def _is_live_press(when: datetime) -> bool:
	"""A press made just now -- not an import, a patch, or a Desk backfill."""
	flags = getattr(frappe, "flags", None)
	if flags is not None and any(
		getattr(flags, name, False) for name in ("in_import", "in_patch", "in_migrate")
	):
		return False
	return abs(when - now_datetime()) <= LIVE_PRESS_WINDOW


def _correct(doc) -> str | None:
	"""Apply the rules to ``doc``. Returns a refusal message, or ``None``."""
	if _norm(doc.get("log_type")) != LOG_OUT:
		return None
	employee = doc.get("employee")
	raw_time = doc.get("time")
	if not employee or not raw_time:
		return None
	if not _columns_ready():
		return None  # schema not migrated yet: leave every press as it was

	when = get_datetime(raw_time)
	if when is None:
		return None
	if not _is_live_press(when):
		return None

	previous = _previous_checkin(employee, when, doc.get("name"))
	action, reason = decide(when, previous)

	if action == ACTION_CONVERT:
		# The note first: if it fails, the doc is left exactly as pressed.
		note = note_for(reason, when, previous)
		original = doc.get("log_type")
		doc.log_type = LOG_IN
		doc.custom_original_log_type = original
		doc.custom_log_type_note = note
		return None
	if action == ACTION_REFUSE and previous:
		return refusal_message(previous)
	return None

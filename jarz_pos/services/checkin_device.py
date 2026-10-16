"""Flag (never refuse) Employee Check-ins that look like buddy punching.

The problem
-----------
A check-in is only as honest as the phone it came from. An employee who hands
their login to a colleague can have that colleague clock them in from somewhere
else, and nothing in HRMS notices: the geofence measures where the PHONE is, not
who is holding it.

Why a cookie and not a MAC address
----------------------------------
A browser cannot read a MAC address, and the HRMS PWA (``/hrms``, upstream code
this app does not fork) only sends employee, log_type, time, latitude and
longitude. So the device is identified server-side by our own long-lived cookie,
``jarz_device``: a random token minted the first time a browser checks in, kept
for five years, HttpOnly so page scripts cannot read or copy it. Frappe's logout
deletes only a fixed list of cookies (sid, user_id, full_name, user_image,
user_lang, system_user), so the token survives logging out and back in.

The policy: FLAG ONLY
---------------------
Nothing here ever blocks a check-in. A false refusal strands a real person
outside a branch, while a missed flag costs a manager one more look; the owner
chose the cheap failure. Every code path therefore swallows its own exceptions.

The rules (see ``classify``)
----------------------------
* Employee has no registered device -> register this one, no flag.
* Registered and the same -> no flag.
* Registered and different -> ``New Device``. NOT re-registered automatically:
  a manager approves it by pasting the new token into
  ``Employee.custom_checkin_device``, or clears the field to re-register on the
  next check-in.
* The same token checked in a DIFFERENT employee on the same calendar day ->
  ``Shared Device``, which outranks ``New Device`` because it is the stronger
  signal. A shared phone is deliberately not registered to anyone.

A check-in the signed-in user does not own (a manager keying one in on the Desk
for somebody else) is skipped: stamping the manager's browser onto the employee
would register the wrong phone.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

import frappe
from frappe.utils import add_days, getdate, now_datetime

COOKIE = "jarz_device"
COOKIE_YEARS = 5

EMPLOYEE_DEVICE_FIELD = "custom_checkin_device"
EMPLOYEE_REGISTERED_ON_FIELD = "custom_checkin_device_registered_on"

FLAG_NEW = "New Device"
FLAG_SHARED = "Shared Device"
FLAG_NONE_AVAILABLE = "No Device"

#: Worst first. The attendance board lists flags in this order.
FLAG_ORDER = (FLAG_SHARED, FLAG_NEW, FLAG_NONE_AVAILABLE)

#: Read by the attendance service when the columns exist on the site.
CHECKIN_FLAG_FIELDS = ("custom_device_flag", "custom_device_flag_detail")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

#: How many trailing characters of a device token a flag detail may show.
TOKEN_VISIBLE_TAIL = 6

#: A token as older details printed it: ``token (<full token>)``. Anchored on the
#: word "token" so an employee name in parentheses is never touched.
_DETAIL_TOKEN_RE = re.compile(r"(token \()([A-Za-z0-9_-]{8,64})(\))")


def mask_token(token: Any) -> str:
	"""``…`` plus the last few characters: enough to tell two phones apart, not
	enough to forge the cookie. The full value stays on the check-in's
	``custom_device_token`` field (Desk only) for the manager who approves it."""
	value = str(token or "")
	if len(value) <= TOKEN_VISIBLE_TAIL:
		return "…" if value else ""
	return "…" + value[-TOKEN_VISIBLE_TAIL:]


def mask_tokens_in_detail(detail: Any) -> Any:
	"""Mask a full token inside a stored flag detail.

	New details are written masked; this covers rows stamped before that, which
	the attendance service still reads. Anything that is not a string is
	returned unchanged.
	"""
	if not isinstance(detail, str) or not detail:
		return detail
	return _DETAIL_TOKEN_RE.sub(lambda m: m.group(1) + mask_token(m.group(2)) + m.group(3), detail)


def _safe_log(title: str) -> None:
	"""``frappe.log_error`` can itself raise (e.g. DB gone); never let it."""
	try:
		frappe.log_error(title=title)
	except Exception:
		pass


def stamp_device(doc, method: str | None = None) -> None:
	"""``Employee Checkin.before_insert`` hook. Never raises."""
	try:
		_stamp(doc)
	except Exception:
		_safe_log("Check-in device fingerprint failed")


def _request():
	return getattr(frappe.local, "request", None)


def classify(
	*,
	token: str,
	registered: str | None,
	others_today: list[dict[str, Any]],
) -> str | None:
	"""Pure decision: which flag (if any) this check-in earns."""
	if others_today:
		return FLAG_SHARED
	if registered and registered != token:
		return FLAG_NEW
	return None


def _read_or_issue_token(request) -> str | None:
	"""The browser's token, minting (and re-setting, to slide expiry) as needed.

	``None`` only when there is no way to remember a device on this request.
	"""
	token = None
	try:
		raw = request.cookies.get(COOKIE)
		if raw and _TOKEN_RE.match(raw):
			token = raw
	except Exception:
		token = None

	manager = getattr(frappe.local, "cookie_manager", None)
	if manager is None:
		return token  # can read but cannot refresh; None if there was no cookie

	if not token:
		token = secrets.token_urlsafe(16)
	manager.set_cookie(
		COOKIE,
		token,
		expires=datetime.now() + timedelta(days=365 * COOKIE_YEARS),
		httponly=True,
		samesite="Lax",
	)
	return token


def _others_same_day(employee: str, token: str, when: Any) -> list[dict[str, Any]]:
	day = getdate(when)
	rows = frappe.get_all(
		"Employee Checkin",
		filters={
			"custom_device_token": token,
			"employee": ("!=", employee),
			"time": ("between", [f"{day} 00:00:00", f"{add_days(day, 1)} 00:00:00"]),
		},
		fields=["employee", "employee_name", "time"],
		order_by="time asc",
		limit_page_length=5,
	)
	return rows or []


def _detail_for_shared(others: list[dict[str, Any]]) -> str:
	today = getdate()
	parts = []
	seen = set()
	for row in others:
		# One line per colleague: a check-in and check-out from the same phone
		# is still one shared person, not two.
		if row.get("employee") in seen:
			continue
		seen.add(row.get("employee"))
		when = row.get("time")
		try:
			stamp = when if hasattr(when, "strftime") else datetime.fromisoformat(str(when))
			clock = stamp.strftime("%H:%M")
			day = "today" if getdate(stamp) == today else str(getdate(stamp))
		except Exception:
			clock, day = str(when), ""
		label = row.get("employee") or ""
		if row.get("employee_name"):
			label = f"{label} ({row['employee_name']})"
		parts.append(f"{label} at {clock} {day}".strip())
	return "Same phone checked in " + "; ".join(parts)


def _stamp(doc) -> None:
	request = _request()
	if request is None or not getattr(doc, "employee", None) or not getattr(doc, "time", None):
		return

	if not frappe.get_meta("Employee Checkin").has_field("custom_device_token"):
		return  # schema not migrated yet
	if not frappe.get_meta("Employee").has_field(EMPLOYEE_DEVICE_FIELD):
		return

	row = frappe.db.get_value("Employee", doc.employee, ["user_id", EMPLOYEE_DEVICE_FIELD], as_dict=True)
	if not row or not row.get("user_id") or row.get("user_id") != frappe.session.user:
		return  # keyed in for somebody else; this browser is not their phone

	token = _read_or_issue_token(request)
	if not token:
		doc.custom_device_flag = FLAG_NONE_AVAILABLE
		doc.custom_device_flag_detail = "No device cookie could be read or set for this check-in."
		return

	doc.custom_device_token = token
	if not doc.get("device_id"):
		doc.device_id = token[:12]
	doc.custom_client_ip = (getattr(frappe.local, "request_ip", None) or "")[:140]
	try:
		doc.custom_user_agent = (request.headers.get("User-Agent") or "")[:500]
	except Exception:
		pass

	registered = row.get(EMPLOYEE_DEVICE_FIELD)
	others = _others_same_day(doc.employee, token, doc.time)
	flag = classify(token=token, registered=registered, others_today=others)

	if flag == FLAG_SHARED:
		doc.custom_device_flag = flag
		doc.custom_device_flag_detail = _detail_for_shared(others)
	elif flag == FLAG_NEW:
		doc.custom_device_flag = flag
		doc.custom_device_flag_detail = (
			f"Different phone from the one registered for this employee (device "
			f"token ({mask_token(token)})). To approve it, copy the Device Token from "
			"this check-in into the employee's Check-in Device."
		)
	elif not registered:
		frappe.db.set_value(
			"Employee",
			doc.employee,
			{
				EMPLOYEE_DEVICE_FIELD: token,
				EMPLOYEE_REGISTERED_ON_FIELD: now_datetime(),
			},
			update_modified=False,
		)

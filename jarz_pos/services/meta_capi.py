"""Delivered chat orders -> Meta Conversions API, as a standard ``Purchase``.

Staff take orders from WhatsApp / Instagram / Messenger chats and enter them in
the POS app (``custom_order_channel = 'flutter'``). Meta never sees those sales,
so the ads that started the chat look like they sold nothing. This module sends
each delivered chat order to the Jarz dataset once, after a person approves it.

Website checkouts are NOT sent from here: the Facebook-for-WooCommerce plugin
already reports them, and sending them twice would double-count revenue.

Flow (all state lives on ``Meta CAPI Event``, one row per invoice):

1. ``queue_delivered_orders`` (hourly) finds delivered chat orders and creates a
   row: ``Pending Approval``, ``Held`` (above the hold amount), ``Approved``
   (approval mode "Held only" and nothing to hold) or ``Skipped`` (no valid
   Egyptian mobile, or the order root was already queued).
2. A ``Meta CAPI Manager`` approves or rejects rows from the list or the form.
3. ``send_approved_events`` (hourly, or "Send now") posts approved rows to Meta.
   In test mode they go with the test event code and stay ``Approved`` so the
   live switch sends them for real; out of test mode they become ``Sent``.

Privacy: nothing personal is stored on the log. The phone, name and customer id
are read from the invoice at send time, normalised and SHA-256 hashed before
they leave the server. Email is never sent (POS orders carry placeholder ones).

Layout: pure functions first (tested without a site), then the scheduler jobs
(never raise), then the whitelisted API used by the Desk buttons.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe import _

SETTINGS_DOCTYPE = "Meta CAPI Settings"
EVENT_DOCTYPE = "Meta CAPI Event"
MANAGER_ROLE = "Meta CAPI Manager"
CUSTOMER_EXCLUDE_FIELD = "custom_exclude_from_meta"

GRAPH_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"
DEFAULT_DATASET_ID = "1296894931563065"
DEFAULT_HOLD_ABOVE = 2500.0

APPROVAL_EVERY_ORDER = "Every order"
APPROVAL_HELD_ONLY = "Held only"

STATUS_PENDING = "Pending Approval"
STATUS_HELD = "Held"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_SENT = "Sent"
STATUS_FAILED = "Failed"
STATUS_EXPIRED = "Expired"
STATUS_SKIPPED = "Skipped"
AWAITING_DECISION = (STATUS_PENDING, STATUS_HELD)

# Customer groups that are never retail chat buyers, even on a flutter order.
DEFAULT_EXCLUDED_GROUPS = (
	"B2B",
	"Employee",
	"Commercial",
	"Distributor",
	"Government",
	"Non Profit",
	"Sample",
)

# Meta refuses an event whose event_time is more than 7 days old. Stop a little
# short so a send that starts just inside the window cannot land just outside.
MAX_EVENT_AGE = timedelta(days=7) - timedelta(minutes=30)
MAX_ATTEMPTS = 3
MAX_QUEUE_PER_RUN = 500
MAX_SEND_PER_RUN = 500
BATCH_SIZE = 50
HTTP_TIMEOUT = 30

_AMENDMENT_SUFFIX = re.compile(r"-\d+$")
_EG_MOBILE = re.compile(r"01[0125]\d{8}")


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def _get(settings: Any, key: str, default: Any = None) -> Any:
	"""Read *key* from a dict or a Document; blank / missing -> *default*."""
	try:
		value = settings.get(key) if hasattr(settings, "get") else getattr(settings, key, None)
	except Exception:
		value = None
	return default if value in (None, "") else value


def event_id_for(invoice_name: str, amended_from: str | None = None) -> str:
	"""The order's stable id: the invoice name without its amendment suffix.

	``ACC-SINV-2026-18128-1`` (amended from ``ACC-SINV-2026-18128``) is the same
	order, so it must reuse the original's id or Meta counts it twice. Only an
	amended invoice carries the suffix; an original's trailing digits are its
	serial number and stay.
	"""
	name = (invoice_name or "").strip()
	return _AMENDMENT_SUFFIX.sub("", name) if amended_from else name


def normalize_eg_mobile(raw: Any) -> str | None:
	"""An Egyptian mobile in Meta's format (``2010XXXXXXXX``), or None.

	Accepts 01XXXXXXXXX, 1XXXXXXXXX, 201XXXXXXXXX, +20 / 0020 prefixes, with any
	spaces or dashes. Landlines and anything else return None.
	"""
	digits = re.sub(r"\D", "", str(raw or ""))
	if digits.startswith("0020"):
		digits = digits[2:]
	if digits.startswith("20") and len(digits) == 12:
		local = "0" + digits[2:]
	elif digits.startswith("0") and len(digits) == 11:
		local = digits
	elif digits.startswith("1") and len(digits) == 10:
		local = "0" + digits
	else:
		return None
	if not _EG_MOBILE.fullmatch(local):
		return None
	return "20" + local[1:]


def sha256(value: str) -> str:
	return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_name_part(value: Any) -> str | None:
	"""Lowercased letters only (Arabic kept), or None when nothing usable is left.

	A part that contains a digit is dropped whole: staff sometimes type the phone
	or an order note into the name, and hashing that would only add noise.
	"""
	text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
	if not text or any(ch.isdigit() for ch in text):
		return None
	letters = "".join(ch for ch in text if ch.isalpha())
	return letters or None


def split_name(full_name: Any) -> tuple[str | None, str | None]:
	"""(first, last) normalised name parts; last is None for a one-word name."""
	parts = [p for p in (normalize_name_part(w) for w in str(full_name or "").split()) if p]
	if not parts:
		return None, None
	return parts[0], (parts[-1] if len(parts) > 1 else None)


def build_user_data(phone: str, customer_id: str, customer_name: Any) -> dict[str, list[str]]:
	"""Hashed match keys. *phone* must already be normalised."""
	first, last = split_name(customer_name)
	data: dict[str, list[str]] = {
		"ph": [sha256(phone)],
		"country": [sha256("eg")],
		"external_id": [sha256(str(customer_id).strip().lower())],
	}
	if first:
		data["fn"] = [sha256(first)]
	if last:
		data["ln"] = [sha256(last)]
	return data


def to_unix(dt: datetime, tz_name: str) -> int:
	"""Seconds since the epoch for a naive site-time datetime."""
	from zoneinfo import ZoneInfo

	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=ZoneInfo(tz_name or "Africa/Cairo"))
	return int(dt.timestamp())


def build_event(
	*,
	event_id: str,
	event_time: int,
	value: float,
	user_data: dict[str, list[str]],
) -> dict[str, Any]:
	"""One CAPI ``Purchase`` from a chat order."""
	return {
		"event_name": "Purchase",
		"event_time": int(event_time),
		"event_id": event_id,
		"action_source": "chat",
		"user_data": user_data,
		"custom_data": {
			"currency": "EGP",
			"value": round(float(value), 2),
			"order_id": event_id,
		},
	}


def initial_status(
	grand_total: float, phone: str | None, approval_mode: str, hold_above: float
) -> tuple[str, str]:
	"""(status, reason) for a newly queued order."""
	if not phone:
		return STATUS_SKIPPED, "No valid Egyptian mobile on the order or the customer"
	if float(grand_total or 0) <= 0:
		return STATUS_SKIPPED, "Order value is zero"
	if hold_above and float(grand_total) > float(hold_above):
		return STATUS_HELD, f"Above {_fmt_amount(hold_above)} EGP"
	if approval_mode == APPROVAL_HELD_ONLY:
		return STATUS_APPROVED, ""
	return STATUS_PENDING, ""


def is_expired(event_time: datetime, now: datetime) -> bool:
	return (now - event_time) > MAX_EVENT_AGE


def parse_groups(text: Any) -> tuple[str, ...]:
	"""Customer groups from the settings box: one per line or comma separated."""
	groups = [g.strip() for g in re.split(r"[\n,]", str(text or "")) if g.strip()]
	return tuple(dict.fromkeys(groups)) or DEFAULT_EXCLUDED_GROUPS


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
	for i in range(0, len(items), size):
		yield items[i : i + size]


def _fmt_amount(value: Any) -> str:
	number = float(value)
	return f"{number:,.0f}" if number == int(number) else f"{number:,.2f}"


def error_text(body: Any, status_code: Any = None) -> str:
	"""A short, token-free description of a failed Graph API response."""
	try:
		err = (body or {}).get("error") or {}
		parts = [str(err.get("message") or "").strip()]
		if err.get("error_user_msg"):
			parts.append(str(err["error_user_msg"]).strip())
		if err.get("code"):
			parts.append("code {}".format(err["code"]))
		text = " | ".join(p for p in parts if p)
	except Exception:
		text = ""
	if not text:
		text = f"HTTP {status_code}" if status_code else "Unknown error"
	return text[:1000]


# ---------------------------------------------------------------------------
# Settings and Graph API
# ---------------------------------------------------------------------------


def load_settings() -> dict[str, Any]:
	try:
		doc = frappe.get_single(SETTINGS_DOCTYPE)
	except Exception:
		doc = {}
	return {
		"doc": doc,
		"enabled": int(_get(doc, "enabled", 0) or 0),
		"test_mode": int(_get(doc, "test_mode", 1) or 0),
		"test_event_code": str(_get(doc, "test_event_code", "") or "").strip(),
		"dataset_id": str(_get(doc, "dataset_id", DEFAULT_DATASET_ID)).strip(),
		"api_version": str(_get(doc, "api_version", DEFAULT_API_VERSION)).strip(),
		"start_date": _get(doc, "start_date"),
		"approval_mode": _get(doc, "approval_mode", APPROVAL_EVERY_ORDER),
		"hold_above": float(_get(doc, "hold_above", DEFAULT_HOLD_ABOVE) or 0),
		"excluded_groups": parse_groups(_get(doc, "excluded_customer_groups", "")),
	}


def _access_token(settings: dict[str, Any]) -> str | None:
	doc = settings.get("doc")
	if not doc or not hasattr(doc, "get_password"):
		return None
	try:
		return doc.get_password("access_token", raise_exception=False) or None
	except Exception:
		return None


def _post_events(settings: dict[str, Any], token: str, events: list[dict[str, Any]]) -> tuple[bool, Any]:
	"""POST a batch. (ok, response body or error text). The token is never logged."""
	import requests

	url = "{}/{}/{}/events".format(GRAPH_URL, settings["api_version"], settings["dataset_id"])
	form = {"data": json.dumps(events), "access_token": token}
	if settings["test_mode"]:
		form["test_event_code"] = settings["test_event_code"]
	try:
		resp = requests.post(url, data=form, timeout=HTTP_TIMEOUT)
	except Exception as exc:  # network error: name the type only, never the request
		return False, f"Network error ({type(exc).__name__})"
	try:
		body = resp.json()
	except Exception:
		body = {}
	if resp.status_code == 200 and int(body.get("events_received") or 0) == len(events):
		return True, body
	return False, error_text(body, resp.status_code)


# ---------------------------------------------------------------------------
# Scheduler jobs (never raise)
# ---------------------------------------------------------------------------


def _queue_since(settings: dict[str, Any], backfill_days: int | None) -> datetime:
	from frappe.utils import get_datetime, now_datetime

	floor = now_datetime() - MAX_EVENT_AGE
	if backfill_days:
		return max(floor, now_datetime() - timedelta(days=backfill_days))
	if settings.get("start_date"):
		return max(floor, get_datetime(settings["start_date"]))
	return floor


def _candidate_orders(settings: dict[str, Any], since: datetime) -> list[Any]:
	"""Delivered chat orders inside the window that have no log row yet."""
	has = frappe.db.has_column
	for column in ("custom_order_channel", "custom_order_purpose", "custom_sales_invoice_state"):
		if not has("Sales Invoice", column):
			return []
	partner = (
		"AND IFNULL(si.custom_is_partner_order, 0) = 0"
		if has("Sales Invoice", "custom_is_partner_order")
		else ""
	)
	excluded = (
		f"AND IFNULL(c.{CUSTOMER_EXCLUDE_FIELD}, 0) = 0" if has("Customer", CUSTOMER_EXCLUDE_FIELD) else ""
	)
	return frappe.db.sql(
		f"""
        SELECT si.name, si.amended_from, si.customer, si.customer_name, si.grand_total, si.creation,
               si.contact_mobile, c.mobile_no AS customer_mobile
        FROM `tabSales Invoice` si
        INNER JOIN `tabCustomer` c ON c.name = si.customer
        WHERE si.docstatus = 1
          AND IFNULL(si.is_return, 0) = 0
          AND si.grand_total > 0
          AND si.custom_order_channel = 'flutter'
          AND si.custom_order_purpose = 'Standard'
          AND si.custom_sales_invoice_state = 'Delivered'
          AND IFNULL(si.sales_partner, '') = ''
          {partner}
          AND c.customer_type = 'Individual'
          AND IFNULL(c.customer_group, '') NOT IN %(groups)s
          {excluded}
          AND si.creation >= %(since)s
          AND NOT EXISTS (SELECT 1 FROM `tabMeta CAPI Event` e WHERE e.invoice = si.name)
        ORDER BY si.creation ASC
        LIMIT %(limit)s
        """,
		{"groups": settings["excluded_groups"], "since": since, "limit": MAX_QUEUE_PER_RUN},
		as_dict=True,
	)


def _queue_one(order: Any, settings: dict[str, Any]) -> str:
	event_id = event_id_for(order.name, order.amended_from)
	phone = normalize_eg_mobile(order.contact_mobile) or normalize_eg_mobile(order.customer_mobile)
	status, reason = initial_status(
		order.grand_total, phone, settings["approval_mode"], settings["hold_above"]
	)
	earlier = frappe.db.get_value(EVENT_DOCTYPE, {"event_id": event_id}, "invoice")
	if earlier:
		status, reason = STATUS_SKIPPED, f"Same order already queued as {earlier}"
	frappe.get_doc(
		{
			"doctype": EVENT_DOCTYPE,
			"invoice": order.name,
			"event_id": event_id,
			"customer": order.customer,
			"order_value": order.grand_total,
			"event_time": order.creation,
			"status": status,
			"status_reason": reason,
		}
	).insert(ignore_permissions=True)
	return status


def queue_delivered_orders(backfill_days: int | None = None) -> dict[str, Any]:
	"""Hourly: one log row per newly delivered chat order. Never raises."""
	result: dict[str, Any] = {"enabled": 0, "queued": 0, "errors": 0, "by_status": {}}
	try:
		settings = load_settings()
		result["enabled"] = settings["enabled"]
		if not settings["enabled"] and not backfill_days:
			return result
		for order in _candidate_orders(settings, _queue_since(settings, backfill_days)):
			try:
				frappe.db.savepoint("meta_capi_queue")
				status = _queue_one(order, settings)
				result["queued"] += 1
				result["by_status"][status] = result["by_status"].get(status, 0) + 1
			except Exception:
				result["errors"] += 1
				frappe.db.rollback(save_point="meta_capi_queue")
				frappe.log_error(frappe.get_traceback(), f"meta_capi: queue {order.name} failed")
		frappe.db.commit()
	except Exception:
		result["errors"] += 1
		try:
			frappe.log_error(frappe.get_traceback(), "meta_capi: queue_delivered_orders failed")
		except Exception:
			pass
	return result


def _event_for_row(row: Any, tz_name: str) -> dict[str, Any] | None:
	"""The CAPI event for a log row, rebuilt from the invoice. None if no phone."""
	inv = frappe.db.get_value(
		"Sales Invoice", row.invoice, ["customer", "customer_name", "contact_mobile"], as_dict=True
	)
	if not inv:
		return None
	phone = normalize_eg_mobile(inv.contact_mobile) or normalize_eg_mobile(
		frappe.db.get_value("Customer", inv.customer, "mobile_no")
	)
	if not phone:
		return None
	return build_event(
		event_id=row.event_id,
		event_time=to_unix(row.event_time, tz_name),
		value=row.order_value,
		user_data=build_user_data(phone, inv.customer, inv.customer_name),
	)


def _mark(name: str, values: dict[str, Any]) -> None:
	frappe.db.set_value(EVENT_DOCTYPE, name, values, update_modified=True)


def send_approved_events() -> dict[str, Any]:
	"""Hourly: post approved rows to Meta. Never raises.

	A batch that Meta refuses is retried one event at a time, so one bad row
	cannot hold back the rest. A row fails for good after ``MAX_ATTEMPTS``.
	"""
	from frappe.utils import get_datetime, get_system_timezone, now_datetime

	result: dict[str, Any] = {"enabled": 0, "sent": 0, "test_sent": 0, "failed": 0, "expired": 0, "errors": 0}
	try:
		settings = load_settings()
		result["enabled"] = settings["enabled"]
		if not settings["enabled"]:
			return result
		token = _access_token(settings)
		if not token:
			result["errors"] += 1
			result["error"] = "No access token saved"
			return result
		if settings["test_mode"] and not settings["test_event_code"]:
			result["errors"] += 1
			result["error"] = "Test mode is on but there is no test event code"
			return result

		test_filter = "AND e.test_sent_at IS NULL" if settings["test_mode"] else ""
		rows = frappe.db.sql(
			f"""
            SELECT e.name, e.invoice, e.event_id, e.event_time, e.order_value, e.attempts
            FROM `tabMeta CAPI Event` e
            WHERE (e.status = %(approved)s OR (e.status = %(failed)s AND IFNULL(e.attempts, 0) < %(max)s))
              {test_filter}
            ORDER BY e.event_time ASC
            LIMIT %(limit)s
            """,
			{
				"approved": STATUS_APPROVED,
				"failed": STATUS_FAILED,
				"max": MAX_ATTEMPTS,
				"limit": MAX_SEND_PER_RUN,
			},
			as_dict=True,
		)

		now = now_datetime()
		tz_name = get_system_timezone()
		ready: list[tuple[Any, dict[str, Any]]] = []
		for row in rows:
			if is_expired(get_datetime(row.event_time), now):
				_mark(row.name, {"status": STATUS_EXPIRED, "status_reason": "Older than Meta's 7-day limit"})
				result["expired"] += 1
				continue
			event = _event_for_row(row, tz_name)
			if not event:
				_mark(
					row.name,
					{"status": STATUS_SKIPPED, "status_reason": "No valid Egyptian mobile at send time"},
				)
				continue
			ready.append((row, event))

		for batch in chunks(ready, BATCH_SIZE):
			ok, body = _post_events(settings, token, [event for _row, event in batch])
			if ok:
				for row, _event in batch:
					_record_success(row, body, settings, now, result)
				continue
			for row, event in batch:
				one_ok, one_body = _post_events(settings, token, [event])
				if one_ok:
					_record_success(row, one_body, settings, now, result)
				else:
					_record_failure(row, one_body, result)
		frappe.db.commit()
	except Exception:
		result["errors"] += 1
		try:
			frappe.log_error(frappe.get_traceback(), "meta_capi: send_approved_events failed")
		except Exception:
			pass
	return result


def _record_success(
	row: Any, body: Any, settings: dict[str, Any], now: datetime, result: dict[str, Any]
) -> None:
	trace = str((body or {}).get("fbtrace_id") or "")[:140]
	if settings["test_mode"]:
		_mark(row.name, {"test_sent_at": now, "fbtrace_id": trace, "last_error": ""})
		result["test_sent"] += 1
	else:
		_mark(
			row.name,
			{
				"status": STATUS_SENT,
				"sent_at": now,
				"fbtrace_id": trace,
				"last_error": "",
				"status_reason": "",
			},
		)
		result["sent"] += 1


def _record_failure(row: Any, message: Any, result: dict[str, Any]) -> None:
	attempts = int(row.attempts or 0) + 1
	_mark(
		row.name,
		{"status": STATUS_FAILED, "attempts": attempts, "last_error": str(message)[:1000]},
	)
	result["failed"] += 1


# ---------------------------------------------------------------------------
# Whitelisted API (Desk buttons)
# ---------------------------------------------------------------------------


def _require_manager() -> None:
	roles = set(frappe.get_roles())
	if not roles & {MANAGER_ROLE, "System Manager"}:
		frappe.throw(_("Only a Meta CAPI Manager can do this."), frappe.PermissionError)


def _names(names: Any) -> list[str]:
	if isinstance(names, str):
		names = json.loads(names) if names.strip().startswith("[") else [names]
	return [str(n) for n in (names or []) if n]


@frappe.whitelist(methods=["POST"])
def approve_events(names: Any) -> dict[str, Any]:
	"""Approve rows that are waiting for a decision. Other rows are left alone."""
	_require_manager()
	from frappe.utils import now_datetime

	done, skipped = 0, 0
	for name in _names(names):
		status = frappe.db.get_value(EVENT_DOCTYPE, name, "status", for_update=True)
		if status not in AWAITING_DECISION:
			skipped += 1
			continue
		_mark(
			name,
			{"status": STATUS_APPROVED, "decided_by": frappe.session.user, "decided_at": now_datetime()},
		)
		done += 1
	frappe.db.commit()
	return {"approved": done, "skipped": skipped}


@frappe.whitelist(methods=["POST"])
def reject_events(names: Any, reason: str | None = None) -> dict[str, Any]:
	"""Reject rows that are waiting for a decision; they are never sent."""
	_require_manager()
	from frappe.utils import now_datetime

	done, skipped = 0, 0
	for name in _names(names):
		status = frappe.db.get_value(EVENT_DOCTYPE, name, "status", for_update=True)
		if status not in AWAITING_DECISION:
			skipped += 1
			continue
		_mark(
			name,
			{
				"status": STATUS_REJECTED,
				"status_reason": (reason or "").strip()[:500],
				"decided_by": frappe.session.user,
				"decided_at": now_datetime(),
			},
		)
		done += 1
	frappe.db.commit()
	return {"rejected": done, "skipped": skipped}


@frappe.whitelist(methods=["POST"])
def check_token() -> dict[str, Any]:
	"""Ask Meta whether the saved token can read the dataset. Never echoes it."""
	_require_manager()
	import requests

	settings = load_settings()
	token = _access_token(settings)
	if not token:
		return {"ok": False, "message": _("No access token saved.")}
	url = "{}/{}/{}".format(GRAPH_URL, settings["api_version"], settings["dataset_id"])
	try:
		resp = requests.get(
			url,
			params={"fields": "id,name"},
			headers={"Authorization": f"Bearer {token}"},
			timeout=HTTP_TIMEOUT,
		)
		body = resp.json()
	except Exception as exc:
		return {"ok": False, "message": f"Network error ({type(exc).__name__})"}
	if resp.status_code == 200 and body.get("id"):
		return {
			"ok": True,
			"message": _("Token works for dataset {0} ({1}).").format(body["id"], body.get("name", "")),
		}
	return {"ok": False, "message": error_text(body, resp.status_code)}


@frappe.whitelist(methods=["POST"])
def send_test_event() -> dict[str, Any]:
	"""One synthetic Purchase to the Test Events tab. Needs a test event code."""
	_require_manager()
	from frappe.utils import now_datetime

	settings = load_settings()
	token = _access_token(settings)
	if not token:
		return {"ok": False, "message": _("No access token saved.")}
	if not settings["test_event_code"]:
		return {"ok": False, "message": _("Add the test event code from Events Manager first.")}
	settings = dict(settings, test_mode=1)
	stamp = now_datetime()
	event = build_event(
		event_id="jarz-test-{}".format(stamp.strftime("%Y%m%d%H%M%S")),
		event_time=int(stamp.timestamp()),
		value=1,
		user_data={"country": [sha256("eg")], "external_id": [sha256("jarz-test")]},
	)
	ok, body = _post_events(settings, token, [event])
	if ok:
		return {"ok": True, "message": _("Meta received the test event. Check the Test Events tab.")}
	return {"ok": False, "message": str(body)}


@frappe.whitelist(methods=["POST"])
def run_queue_now(backfill_days: int | None = None) -> dict[str, Any]:
	"""Queue now. ``backfill_days`` (max 7) looks back past the start date."""
	_require_manager()
	days = min(int(backfill_days or 0), 7) or None
	return queue_delivered_orders(backfill_days=days)


@frappe.whitelist(methods=["POST"])
def run_send_now() -> dict[str, Any]:
	"""Send approved rows now instead of waiting for the hourly job."""
	_require_manager()
	return send_approved_events()


@frappe.whitelist()
def summary(days: int = 14) -> list[dict[str, Any]]:
	"""Per-day counts for the settings page and the daily report."""
	_require_manager()
	return daily_rows(int(days or 14))


def daily_rows(days: int = 14, from_date: Any = None, to_date: Any = None) -> list[dict[str, Any]]:
	from frappe.utils import add_days, getdate, nowdate

	to_date = getdate(to_date or nowdate())
	from_date = getdate(from_date or add_days(to_date, -(days - 1)))
	return frappe.db.sql(
		"""
        SELECT DATE(event_time) AS day,
               COUNT(*) AS orders,
               SUM(status = 'Pending Approval') AS pending,
               SUM(status = 'Held') AS held,
               SUM(status = 'Approved') AS approved,
               SUM(status = 'Rejected') AS rejected,
               SUM(status = 'Sent') AS sent,
               SUM(status = 'Failed') AS failed,
               SUM(status = 'Expired') AS expired,
               SUM(status = 'Skipped') AS skipped,
               SUM(IF(status = 'Sent', order_value, 0)) AS sent_value
        FROM `tabMeta CAPI Event`
        WHERE DATE(event_time) BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY DATE(event_time)
        ORDER BY day DESC
        """,
		{"from_date": from_date, "to_date": to_date},
		as_dict=True,
	)

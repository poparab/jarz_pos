"""Follow-up cycle for delivered retail orders ("Jarz Follow-up Touch").

After a retail order is delivered the customer gets a short, fixed sequence of
messages and calls (3 hours, day 2, 7, 30, 32, 45, 50). Each step is one
``Jarz Follow-up Touch`` row that a person works through the follow-up screen.

Layout of this module
---------------------
* **Pure planning / decision functions** (no database): :func:`plan_touches`,
  :func:`decide_log_effects`, :func:`render_template`, :func:`template_key`.
  They are what the unit tests pin.
* **Scheduler jobs**: :func:`generate_touches` (hourly) and
  :func:`send_daily_sender_push` (12:00). Both never raise: a failed follow-up
  run must not break the scheduler tick it shares with unrelated jobs.
* **API-facing operations**: :func:`log_touch`, :func:`skip_touch`,
  :func:`get_due_touches`, :func:`get_followup_stats`.

Retail scope is the one defined in ``rfm_segmentation`` (no B2B, no samples or
staff orders, website orders only while the segmentation setting says so).
"""

from __future__ import annotations

import datetime as _dt
import secrets
from collections.abc import Iterable
from typing import Any, Optional

import frappe

SETTINGS_DOCTYPE = "Jarz Follow-up Settings"
TOUCH_DOCTYPE = "Jarz Follow-up Touch"
CUSTOMER_OPT_OUT_FIELD = "custom_followup_opt_out"

MESSAGE = "Message"
CALL = "Call"

REACHED = "Reached"
NOT_REACHED = "Not Reached"
OUTCOMES = ("Happy", "Quality", "Delivery", "No Reply", "Opted Out")

#: Hard cap on invoices turned into cycles by one hourly run. A backlog (first
#: day after switching on, or after an outage) drains over a few runs instead
#: of holding one scheduler worker.
MAX_INVOICES_PER_RUN = 300
#: Only orders delivered inside this many days start a cycle: a cycle for an
#: order delivered a fortnight ago would open with a stale "3 hours" message.
MAX_DELIVERED_AGE_DAYS = 7

DEFAULT_QUIET_START = _dt.time(22, 0)
DEFAULT_QUIET_END = _dt.time(11, 0)
DEFAULT_CALL_START = _dt.time(12, 0)
DEFAULT_CALL_END = _dt.time(16, 0)

#: (step, channel, relative_offset_hours | day offset, audience rule, skip_if_reached)
#: Rules: "all" | "high" | "repeat" | "repeat_high".
_PLAN = (
	("3h", MESSAGE, ("hours", 3), "all", None),
	("3h", CALL, ("hours", 3), "high", None),
	("day2", CALL, ("days", 2), "all", "3h"),
	("day7", MESSAGE, ("days", 7), "all", None),
	("day30", CALL, ("days", 30), "repeat", None),
	("day32", MESSAGE, ("days", 32), "all", "day30"),
	("day45", CALL, ("days", 45), "repeat_high", None),
	("day50", MESSAGE, ("days", 50), "all", "day45"),
)

REASON_NEW_ORDER = "New order"
REASON_QUALITY = "Quality complaint"
REASON_OPT_OUT = "Opted out"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _get(settings: Any, key: str, default: Any = None) -> Any:
	"""Read *key* from a dict or a Document; blank / missing -> *default*."""
	try:
		if isinstance(settings, dict):
			value = settings.get(key)
		elif hasattr(settings, "get"):
			value = settings.get(key)
		else:
			value = getattr(settings, key, None)
	except Exception:
		value = None
	return default if value in (None, "") else value


def _to_time(value: Any, default: _dt.time) -> _dt.time:
	"""Frappe hands a Time field back as timedelta, 'HH:MM:SS' or time."""
	if value in (None, ""):
		return default
	if isinstance(value, _dt.datetime):
		return value.time().replace(microsecond=0)
	if isinstance(value, _dt.time):
		return value.replace(microsecond=0)
	if isinstance(value, _dt.timedelta):
		secs = int(value.total_seconds()) % 86400
		return _dt.time(secs // 3600, (secs % 3600) // 60, secs % 60)
	try:
		parts = [int(float(p)) for p in str(value).strip().split(":")]
		parts += [0] * (3 - len(parts))
		return _dt.time(parts[0], parts[1], parts[2])
	except Exception:
		return default


def _to_datetime(value: Any) -> _dt.datetime:
	if isinstance(value, _dt.datetime):
		return value
	if isinstance(value, _dt.date):
		return _dt.datetime.combine(value, _dt.time(0, 0))
	text = str(value).strip().replace("T", " ")
	for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
		try:
			return _dt.datetime.strptime(text, fmt)
		except ValueError:
			continue
	from frappe.utils import get_datetime

	return get_datetime(value)


def _at(day: _dt.date, t: _dt.time) -> _dt.datetime:
	return _dt.datetime.combine(day, t)


def _in_quiet(t: _dt.time, quiet_start: _dt.time, quiet_end: _dt.time) -> bool:
	if quiet_start == quiet_end:
		return False
	if quiet_start < quiet_end:
		return quiet_start <= t < quiet_end
	return t >= quiet_start or t < quiet_end  # wraps midnight (22:00 - 11:00)


def shift_message_time(due: _dt.datetime, quiet_start: _dt.time, quiet_end: _dt.time) -> _dt.datetime:
	"""Move a message out of quiet hours to the next ``quiet_end``."""
	t = due.time()
	if not _in_quiet(t, quiet_start, quiet_end):
		return due
	if quiet_start > quiet_end and t >= quiet_start:
		return _at(due.date() + _dt.timedelta(days=1), quiet_end)
	return _at(due.date(), quiet_end)


def shift_call_time(due: _dt.datetime, win_start: _dt.time, win_end: _dt.time) -> _dt.datetime:
	"""Move a call into the call window (next window start when outside)."""
	t = due.time()
	if t < win_start:
		return _at(due.date(), win_start)
	if t >= win_end:
		return _at(due.date() + _dt.timedelta(days=1), win_start)
	return due


def plan_touches(delivered_at: Any, order_value: Any, is_repeat: bool, settings: Any) -> list[dict[str, Any]]:
	"""The touches one delivered order produces. Pure.

	*settings* is a dict or Document with ``high_value_order_min``,
	``quiet_start``, ``quiet_end``, ``call_window_start``, ``call_window_end``.
	Returns ``[{step, channel, due_at, skip_if_reached}]`` ordered by due time.
	"""
	delivered = _to_datetime(delivered_at)
	value = float(order_value or 0)
	high_min = float(_get(settings, "high_value_order_min", 1000) or 0)
	is_high = high_min > 0 and value >= high_min
	quiet_start = _to_time(_get(settings, "quiet_start"), DEFAULT_QUIET_START)
	quiet_end = _to_time(_get(settings, "quiet_end"), DEFAULT_QUIET_END)
	call_start = _to_time(_get(settings, "call_window_start"), DEFAULT_CALL_START)
	call_end = _to_time(_get(settings, "call_window_end"), DEFAULT_CALL_END)

	out: list[dict[str, Any]] = []
	for step, channel, (unit, amount), rule, skip in _PLAN:
		if rule == "high" and not is_high:
			continue
		if rule == "repeat" and not is_repeat:
			continue
		if rule == "repeat_high" and not (is_repeat and is_high):
			continue

		if unit == "hours":
			due = delivered + _dt.timedelta(hours=amount)
		else:
			# Day-N touches are due on the calendar day, at the start of the
			# window their channel is allowed in.
			day = delivered.date() + _dt.timedelta(days=amount)
			due = _at(day, quiet_end if channel == MESSAGE else call_start)

		if channel == MESSAGE:
			due = shift_message_time(due, quiet_start, quiet_end)
		else:
			due = shift_call_time(due, call_start, call_end)
		out.append({"step": step, "channel": channel, "due_at": due, "skip_if_reached": skip})

	out.sort(key=lambda r: (r["due_at"], r["channel"]))
	return out


def template_key(step: str, channel: str) -> str:
	"""Settings field holding the text for *step* + *channel*."""
	return ("tpl_call_" if channel == CALL else "tpl_msg_") + str(step)


def render_template(template: str | None, first_name: str = "", order_no: Any = "", offer: str = "") -> str:
	"""Fill the placeholders by plain replace (a stray brace cannot break it)."""
	text = str(template or "")
	for key, value in (("{first_name}", first_name), ("{order_no}", order_no), ("{offer}", offer)):
		text = text.replace(key, str(value or ""))
	return " ".join(text.split())


def first_name_of(customer_name: str | None) -> str:
	parts = str(customer_name or "").split()
	return parts[0] if parts else ""


def decide_log_effects(
	channel: str, step: str, reached: str | None, outcome: str | None, apology_percent: Any = 0
) -> dict[str, Any]:
	"""Validate a call/message result and say what it sets in motion. Pure.

	Raises ``ValueError`` (message is user-facing) for an impossible result.
	"""
	reached = str(reached or "").strip()
	outcome = str(outcome or "").strip()
	if reached not in ("", REACHED, NOT_REACHED):
		raise ValueError("Reached must be Reached or Not Reached")
	if outcome and outcome not in OUTCOMES:
		raise ValueError("Unknown outcome: %s" % outcome)
	if channel != CALL and reached:
		raise ValueError("Reached only applies to calls")
	if channel == CALL and not reached:
		raise ValueError("Say whether the call reached the customer")
	if reached == NOT_REACHED and outcome not in ("", "No Reply"):
		raise ValueError("A call that did not reach the customer can only end as No Reply")

	cancel_reason = None
	if outcome == "Quality":
		cancel_reason = REASON_QUALITY
	elif outcome == "Opted Out":
		cancel_reason = REASON_OPT_OUT
	return {
		"skip_followers_of": step if reached == REACHED else None,
		"cancel_all_reason": cancel_reason,
		"create_task": outcome == "Quality",
		"create_apology_code": outcome == "Delivery" and float(apology_percent or 0) > 0,
		"opt_out": outcome == "Opted Out",
	}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def load_settings() -> dict[str, Any]:
	"""Follow-up settings as a plain dict, with the segmentation threshold."""
	from jarz_pos.services import rfm_segmentation as rfm

	try:
		doc = frappe.get_single(SETTINGS_DOCTYPE)
	except Exception:
		doc = {}
	out: dict[str, Any] = {
		"enabled": int(_get(doc, "enabled", 0) or 0),
		"start_date": _get(doc, "start_date", "2026-10-15"),
		"offer_text": _get(doc, "offer_text", ""),
		"quality_escalation_user": _get(doc, "quality_escalation_user"),
		"apology_discount_percent": float(_get(doc, "apology_discount_percent", 0) or 0),
		"apology_code_valid_days": int(_get(doc, "apology_code_valid_days", 30) or 30),
		"quiet_start": _to_time(_get(doc, "quiet_start"), DEFAULT_QUIET_START),
		"quiet_end": _to_time(_get(doc, "quiet_end"), DEFAULT_QUIET_END),
		"call_window_start": _to_time(_get(doc, "call_window_start"), DEFAULT_CALL_START),
		"call_window_end": _to_time(_get(doc, "call_window_end"), DEFAULT_CALL_END),
		"high_value_order_min": rfm.high_value_threshold(),
		"sender_users": [],
		"templates": {},
	}
	for step, channel, _o, _r, _s in _PLAN:
		key = template_key(step, channel)
		out["templates"][key] = _get(doc, key, "") or ""
	try:
		out["sender_users"] = [
			r.get("user") if hasattr(r, "get") else r.user
			for r in (doc.get("sender_users") or [])
			if (r.get("user") if hasattr(r, "get") else getattr(r, "user", None))
		]
	except Exception:
		out["sender_users"] = []
	return out


# ---------------------------------------------------------------------------
# Generator (hourly)
# ---------------------------------------------------------------------------


def _since_datetime(start_date: Any) -> _dt.datetime:
	from frappe.utils import now_datetime

	floor = now_datetime() - _dt.timedelta(days=MAX_DELIVERED_AGE_DAYS)
	return max(floor, _to_datetime(start_date))


def _cancel_pending(where: str, params: dict[str, Any], reason: str) -> None:
	# nosemgrep: frappe-sql-format-injection -- where is a literal fragment supplied by in-module callers; all values are bound parameters
	frappe.db.sql(
		f"""
        UPDATE `tabJarz Follow-up Touch`
        SET status = 'Cancelled', cancel_reason = %(reason)s, modified = NOW()
        WHERE status = 'Pending' AND {where}
        """,
		dict(params, reason=reason),
	)


def _earlier_retail_orders(customer: str, inv: Any, scope: str) -> int:
	# nosemgrep: frappe-sql-format-injection -- scope is rfm.retail_scope_sql output (constants only); all values are bound parameters
	row = frappe.db.sql(
		f"""
        SELECT COUNT(*) FROM `tabSales Invoice` si
        INNER JOIN `tabCustomer` c ON c.name = si.customer
        WHERE si.customer = %(customer)s AND si.name != %(inv)s AND {scope}
          AND (si.posting_date < %(pd)s OR (si.posting_date = %(pd)s AND si.creation < %(cr)s))
        """,
		{"customer": customer, "inv": inv.name, "pd": inv.posting_date, "cr": inv.creation},
	)
	return int(row[0][0] or 0) if row else 0


def _insert_cycle(inv: Any, settings: dict[str, Any], scope: str) -> int:
	earlier = _earlier_retail_orders(inv.customer, inv, scope)
	is_repeat = earlier >= 1
	plan = plan_touches(inv.delivered_at, inv.grand_total, is_repeat, settings)

	# A newer order replaces the old cycle: do not chase someone who just bought.
	_cancel_pending(
		"customer = %(customer)s AND IFNULL(invoice, '') != %(inv)s",
		{"customer": inv.customer, "inv": inv.name},
		REASON_NEW_ORDER,
	)

	high_min = float(settings.get("high_value_order_min") or 0)
	band = "High" if high_min > 0 and float(inv.grand_total or 0) >= high_min else "Low"
	for item in plan:
		frappe.get_doc(
			{
				"doctype": TOUCH_DOCTYPE,
				"customer": inv.customer,
				"phone": inv.mobile_no or "",
				"invoice": inv.name,
				"woo_order_id": int(inv.woo_order_id or 0),
				"cycle_start": inv.delivered_at,
				"step": item["step"],
				"channel": item["channel"],
				"due_at": item["due_at"],
				"status": "Pending",
				"skip_if_reached": item["skip_if_reached"],
				"customer_type": "Repeat" if is_repeat else "New",
				"value_band": band,
				"order_value": float(inv.grand_total or 0),
				"lifetime_orders": earlier + 1,
			}
		).insert(ignore_permissions=True)
	return len(plan)


def _cancel_superseded(scope_inv_conds: list[str]) -> None:
	"""Cancel Pending touches whose customer ordered again since the touch's order."""
	newer = " AND ".join(scope_inv_conds)
	# nosemgrep: frappe-sql-format-injection -- newer is built by rfm.retail_invoice_conditions (constants only, no user input)
	frappe.db.sql(
		f"""
        UPDATE `tabJarz Follow-up Touch` t
        INNER JOIN `tabSales Invoice` cur ON cur.name = t.invoice
        SET t.status = 'Cancelled', t.cancel_reason = %(reason)s, t.modified = NOW()
        WHERE t.status = 'Pending'
          AND EXISTS (
              SELECT 1 FROM `tabSales Invoice` n
              WHERE n.customer = t.customer AND n.name != t.invoice AND {newer}
                AND (n.posting_date > cur.posting_date
                     OR (n.posting_date = cur.posting_date AND n.creation > cur.creation))
          )
        """,
		{"reason": REASON_NEW_ORDER},
	)


def generate_touches() -> dict[str, Any]:
	"""Hourly: open a cycle for every newly delivered retail order. Never raises.

	Idempotent: an invoice that already has any touch is never planned again, so
	re-running (or two overlapping runs) cannot duplicate a cycle.
	"""
	result: dict[str, Any] = {"enabled": 0, "invoices": 0, "touches": 0, "errors": 0}
	try:
		settings = load_settings()
		result["enabled"] = settings["enabled"]
		if not settings["enabled"]:
			return result

		from jarz_pos.services import rfm_segmentation as rfm

		if not frappe.db.has_column("Sales Invoice", "custom_sales_invoice_state"):
			return result

		website_only = rfm.website_only_enabled()
		scope = rfm.retail_scope_sql(website_only)
		opt_out = ""
		if frappe.db.has_column("Customer", CUSTOMER_OPT_OUT_FIELD):
			opt_out = "AND IFNULL(c.%s, 0) = 0" % CUSTOMER_OPT_OUT_FIELD
		delivered_expr = (
			"COALESCE(si.custom_delivered_at, si.modified)"
			if frappe.db.has_column("Sales Invoice", "custom_delivered_at")
			else "si.modified"
		)
		woo_col = "si.woo_order_id" if frappe.db.has_column("Sales Invoice", "woo_order_id") else "0"

		# nosemgrep: frappe-sql-format-injection -- woo_col, delivered_expr, scope and opt_out are fixed fragments from has_column checks and module constants; values are bound
		invoices = frappe.db.sql(
			f"""
            SELECT si.name, si.customer, si.grand_total, si.posting_date, si.creation,
                   {woo_col} AS woo_order_id, {delivered_expr} AS delivered_at, c.mobile_no
            FROM `tabSales Invoice` si
            INNER JOIN `tabCustomer` c ON c.name = si.customer
            WHERE si.custom_sales_invoice_state = 'Delivered'
              AND {delivered_expr} >= %(since)s
              -- Index-friendly floor, and it stops an old order whose
              -- custom_delivered_at is empty from starting a cycle just because
              -- a later save bumped `modified`.
              AND si.posting_date >= DATE_SUB(DATE(%(since)s), INTERVAL 2 DAY)
              AND {scope} {opt_out}
              AND NOT EXISTS (SELECT 1 FROM `tabJarz Follow-up Touch` t WHERE t.invoice = si.name)
            ORDER BY {delivered_expr} ASC
            LIMIT %(limit)s
            """,
			{"since": _since_datetime(settings["start_date"]), "limit": MAX_INVOICES_PER_RUN},
			as_dict=True,
		)

		for inv in invoices:
			try:
				frappe.db.savepoint("followup_invoice")
				result["touches"] += _insert_cycle(inv, settings, scope)
				result["invoices"] += 1
			except Exception:
				result["errors"] += 1
				frappe.db.rollback(save_point="followup_invoice")
				frappe.log_error(frappe.get_traceback(), "followups: cycle for %s failed" % inv.name)

		try:
			_cancel_superseded(rfm.retail_invoice_conditions(website_only, "n"))
		except Exception:
			result["errors"] += 1
			frappe.log_error(frappe.get_traceback(), "followups: supersede sweep failed")

		frappe.db.commit()
	except Exception:
		result["errors"] += 1
		try:
			frappe.log_error(frappe.get_traceback(), "followups: generate_touches failed")
		except Exception:
			pass
	return result


# ---------------------------------------------------------------------------
# Working a touch
# ---------------------------------------------------------------------------


def _load_pending(name: str):
	if not name or not frappe.db.exists(TOUCH_DOCTYPE, name):
		frappe.throw("Follow-up not found: %s" % name)
	doc = frappe.get_doc(TOUCH_DOCTYPE, name, for_update=True)
	if doc.status != "Pending":
		frappe.throw("This follow-up is already %s" % doc.status)
	return doc


def _create_apology_code(doc: Any, settings: dict[str, Any]) -> str | None:
	"""A single-use percentage code for the customer. None when it cannot be made."""
	from frappe.utils import add_days, now_datetime

	percent = min(float(settings.get("apology_discount_percent") or 0), 100.0)
	if percent <= 0:
		return None
	alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to dictate by phone
	for _ in range(5):
		code = "SORRY-" + "".join(secrets.choice(alphabet) for _ in range(6))
		if not frappe.db.exists("Jarz Promo Code", code):
			break
	else:
		return None
	now = now_datetime()
	frappe.get_doc(
		{
			"doctype": "Jarz Promo Code",
			"code": code,
			"title": "Apology - %s" % (doc.customer_name or doc.customer),
			"enabled": 1,
			"discount_type": "Percentage",
			"discount_value": percent,
			"applies_to": "Whole Order",
			"usage_limit": 1,
			"per_customer_limit": 1,
			"active_from": now,
			"active_to": add_days(now, int(settings.get("apology_code_valid_days") or 30)),
			"description": "Delivery apology for %s (order %s). Created from follow-up %s."
			% (doc.customer, doc.woo_order_id or doc.invoice, doc.name),
		}
	).insert(ignore_permissions=True)
	return code


def _create_quality_task(doc: Any, settings: dict[str, Any], user: str, notes: str | None) -> str | None:
	"""A Task Board card for the quality owner, due today. None when not possible."""
	from frappe.utils import nowdate

	from jarz_pos.services import task_board as tb

	assignee = settings.get("quality_escalation_user")
	if not assignee:
		frappe.log_error(
			"Quality complaint on follow-up %s but no quality_escalation_user is set." % doc.name,
			"followups: no quality owner",
		)
		return None
	title = "شكوى جودة — %s — طلب %s" % (
		doc.customer_name or doc.customer,
		doc.woo_order_id or doc.invoice or "",
	)
	body = "\n".join(
		p
		for p in (
			"العميل: %s" % (doc.customer_name or doc.customer),
			"الموبايل: %s" % doc.phone if doc.phone else "",
			"الطلب: %s" % (doc.woo_order_id or doc.invoice or ""),
			"المتابعة: %s (%s)" % (doc.name, doc.step),
			"الملاحظات: %s" % notes if notes else "",
		)
		if p
	)
	task = frappe.get_doc(
		{
			"doctype": tb.TASK_DOCTYPE,
			"title": " ".join(title.split())[: tb.TITLE_MAX_LENGTH],
			"description": body,
			"status": tb.STATUS_TODO,
			"priority": tb.PRIORITY_HIGH,
			"assigned_to": assignee,
			"created_by": user,
			"due_date": nowdate(),
		}
	)
	task.insert(ignore_permissions=True)
	try:  # history + push are best effort: the card exists either way
		from jarz_pos.api import tasks as tasks_api

		tasks_api._history(task.name, tb.EV_CREATED, "Created from follow-up %s" % doc.name, user)
		tasks_api._notify(task, tb.N_ASSIGNED, user, {"due_date": nowdate()})
	except Exception:
		frappe.log_error(frappe.get_traceback(), "followups: task history/notify failed")
	return task.name


def log_touch(
	name: str,
	reached: str | None = None,
	outcome: str | None = None,
	notes: str | None = None,
	user: str | None = None,
) -> dict[str, Any]:
	"""Record the result of a touch and apply its consequences."""
	from frappe.utils import now_datetime

	user = user or frappe.session.user
	doc = _load_pending(name)
	settings = load_settings()
	try:
		effects = decide_log_effects(
			doc.channel, doc.step, reached, outcome, settings.get("apology_discount_percent")
		)
	except ValueError as exc:
		frappe.throw(str(exc))

	doc.status = "Done"
	doc.reached = (reached or "").strip()
	doc.outcome = (outcome or "").strip()
	doc.notes = (notes or "").strip()
	doc.done_by = user
	doc.done_at = now_datetime()

	if effects["create_apology_code"]:
		doc.apology_code = _create_apology_code(doc, settings)
	if effects["create_task"]:
		doc.escalation_task = _create_quality_task(doc, settings, user, doc.notes)
	doc.save(ignore_permissions=True)

	if effects["skip_followers_of"]:
		frappe.db.sql(
			"""
            UPDATE `tabJarz Follow-up Touch`
            SET status = 'Skipped', cancel_reason = %(reason)s, modified = NOW()
            WHERE status = 'Pending' AND invoice = %(inv)s AND skip_if_reached = %(step)s AND name != %(name)s
            """,
			{
				"reason": "Reached on %s call" % doc.step,
				"inv": doc.invoice,
				"step": effects["skip_followers_of"],
				"name": doc.name,
			},
		)
	if effects["cancel_all_reason"]:
		_cancel_pending(
			"customer = %(customer)s AND name != %(name)s",
			{"customer": doc.customer, "name": doc.name},
			effects["cancel_all_reason"],
		)
	if effects["opt_out"] and frappe.db.has_column("Customer", CUSTOMER_OPT_OUT_FIELD):
		frappe.db.set_value("Customer", doc.customer, CUSTOMER_OPT_OUT_FIELD, 1, update_modified=False)

	return _touch_row(doc.as_dict(), _customer_info([doc.customer]).get(doc.customer, {}), settings)


def skip_touch(name: str, reason: str | None = None, user: str | None = None) -> dict[str, Any]:
	doc = _load_pending(name)
	doc.status = "Skipped"
	doc.cancel_reason = (reason or "Skipped").strip()[:140]
	doc.done_by = user or frappe.session.user
	doc.done_at = frappe.utils.now_datetime()
	doc.save(ignore_permissions=True)
	return _touch_row(doc.as_dict(), _customer_info([doc.customer]).get(doc.customer, {}), load_settings())


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _customer_info(customers: Iterable[str]) -> dict[str, dict[str, Any]]:
	names = sorted({c for c in customers if c})
	if not names:
		return {}
	fields = ["name", "customer_name", "mobile_no", "territory", "customer_segment"]
	if frappe.db.has_column("Customer", "rfm_is_regular"):
		fields.append("rfm_is_regular")
	rows = frappe.get_all("Customer", filters={"name": ["in", names]}, fields=fields)
	return {r["name"]: r for r in rows}


def _touch_row(t: dict[str, Any], cust: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
	from frappe.utils import getdate, nowdate

	from jarz_pos.utils.phone import whatsapp_msisdn

	phone = t.get("phone") or cust.get("mobile_no") or ""
	customer_name = t.get("customer_name") or cust.get("customer_name") or t.get("customer")
	order_no = t.get("woo_order_id") or t.get("invoice") or ""
	# nosemgrep: frappe-ssti -- render_template here is this module's own str.replace placeholder filler, not Jinja
	text = render_template(
		settings.get("templates", {}).get(template_key(t.get("step"), t.get("channel")), ""),
		first_name_of(customer_name),
		order_no,
		settings.get("offer_text") or "",
	)
	due_at = t.get("due_at")
	is_overdue = bool(due_at) and getdate(due_at) < getdate(nowdate())
	return {
		"name": t.get("name"),
		"customer": t.get("customer"),
		"customer_name": customer_name,
		"phone": phone,
		"whatsapp_msisdn": whatsapp_msisdn(phone),
		"message_text": text,
		"step": t.get("step"),
		"channel": t.get("channel"),
		"status": t.get("status"),
		"due_at": str(due_at) if due_at else None,
		"is_overdue": is_overdue,
		"customer_type": t.get("customer_type"),
		"value_band": t.get("value_band"),
		"order_value": float(t.get("order_value") or 0),
		"invoice": t.get("invoice"),
		"woo_order_id": int(t.get("woo_order_id") or 0),
		"lifetime_orders": int(t.get("lifetime_orders") or 0),
		"customer_segment": cust.get("customer_segment"),
		"rfm_is_regular": int(cust.get("rfm_is_regular") or 0),
		"territory": cust.get("territory"),
		"reached": t.get("reached") or "",
		"outcome": t.get("outcome") or "",
		"notes": t.get("notes") or "",
		"apology_code": t.get("apology_code"),
		"escalation_task": t.get("escalation_task"),
	}


def count_due_today() -> int:
	from frappe.utils import add_days, nowdate

	end = "%s 00:00:00" % add_days(nowdate(), 1)
	return int(
		frappe.db.sql(
			"SELECT COUNT(*) FROM `tabJarz Follow-up Touch` WHERE status = 'Pending' AND due_at < %s", (end,)
		)[0][0]
		or 0
	)


def get_due_touches(user: str | None = None) -> dict[str, Any]:
	"""Pending touches due by the end of today, oldest first, with a summary."""
	from frappe.utils import add_days, nowdate

	end = "%s 00:00:00" % add_days(nowdate(), 1)
	rows = frappe.get_all(
		TOUCH_DOCTYPE,
		filters={"status": "Pending", "due_at": ["<", end]},
		fields=[
			"name",
			"customer",
			"customer_name",
			"phone",
			"invoice",
			"woo_order_id",
			"step",
			"channel",
			"status",
			"due_at",
			"customer_type",
			"value_band",
			"order_value",
			"lifetime_orders",
		],
		order_by="due_at asc, name asc",
		limit_page_length=1000,
	)
	cust = _customer_info(r["customer"] for r in rows)
	settings = load_settings()
	items = [_touch_row(r, cust.get(r["customer"], {}), settings) for r in rows]
	summary = {
		"due": len(items),
		"overdue": sum(1 for i in items if i["is_overdue"]),
		"calls": sum(1 for i in items if i["channel"] == CALL),
		"messages": sum(1 for i in items if i["channel"] == MESSAGE),
	}
	return {"items": items, "summary": summary}


def get_followup_stats(from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
	"""Outcome numbers for touches due in the range (default: last 30 days)."""
	from frappe.utils import add_days, getdate, nowdate

	from jarz_pos.services import rfm_segmentation as rfm

	to_d = getdate(to_date) if to_date else getdate(nowdate())
	from_d = getdate(from_date) if from_date else add_days(to_d, -29)
	params = {"from_d": "%s 00:00:00" % from_d, "to_d": "%s 00:00:00" % add_days(to_d, 1)}

	by_status = {
		r[0]: int(r[1])
		for r in frappe.db.sql(
			# nosemgrep: string-concat-in-list -- one SQL statement split over lines on purpose, not a list
			"SELECT status, COUNT(*) FROM `tabJarz Follow-up Touch` "
			"WHERE due_at >= %(from_d)s AND due_at < %(to_d)s GROUP BY status",
			params,
		)
	}
	by_outcome = {
		r[0]: int(r[1])
		for r in frappe.db.sql(
			# nosemgrep: string-concat-in-list -- one SQL statement split over lines on purpose, not a list
			"SELECT outcome, COUNT(*) FROM `tabJarz Follow-up Touch` "
			"WHERE status = 'Done' AND IFNULL(outcome, '') != '' "
			"AND due_at >= %(from_d)s AND due_at < %(to_d)s GROUP BY outcome",
			params,
		)
	}
	calls = frappe.db.sql(
		"SELECT COUNT(*), SUM(reached = 'Reached') FROM `tabJarz Follow-up Touch` "
		"WHERE status = 'Done' AND channel = 'Call' AND due_at >= %(from_d)s AND due_at < %(to_d)s",
		params,
	)
	calls_done = int(calls[0][0] or 0) if calls else 0
	calls_reached = int(calls[0][1] or 0) if calls else 0

	newer = " AND ".join(rfm.retail_invoice_conditions(rfm.website_only_enabled(), "n"))
	# nosemgrep: frappe-sql-format-injection -- newer is built by rfm.retail_invoice_conditions (constants only, no user input); dates are bound parameters
	touched, reordered = frappe.db.sql(
		f"""
        SELECT COUNT(DISTINCT t.customer),
               COUNT(DISTINCT CASE WHEN EXISTS (
                   SELECT 1 FROM `tabSales Invoice` n
                   WHERE n.customer = t.customer AND {newer}
                     AND n.creation > t.done_at AND n.creation <= DATE_ADD(t.done_at, INTERVAL 30 DAY)
               ) THEN t.customer END)
        FROM `tabJarz Follow-up Touch` t
        WHERE t.status = 'Done' AND t.done_at IS NOT NULL
          AND t.due_at >= %(from_d)s AND t.due_at < %(to_d)s
        """,
		params,
	)[0]
	touched, reordered = int(touched or 0), int(reordered or 0)
	return {
		"from_date": str(from_d),
		"to_date": str(to_d),
		"total": sum(by_status.values()),
		"by_status": by_status,
		"by_outcome": by_outcome,
		"calls_done": calls_done,
		"calls_reached": calls_reached,
		"reach_rate": round(calls_reached / calls_done, 4) if calls_done else 0,
		"touched_customers": touched,
		"reordered_customers": reordered,
		"reorder_rate": round(reordered / touched, 4) if touched else 0,
	}


# ---------------------------------------------------------------------------
# Daily push (12:00)
# ---------------------------------------------------------------------------


def send_daily_sender_push() -> dict[str, Any]:
	"""12:00: tell each sender how many follow-ups wait for them. Never raises."""
	result: dict[str, Any] = {"status": "skipped", "count": 0, "recipients": 0}
	try:
		settings = load_settings()
		if not settings["enabled"]:
			return result
		senders = sorted({u for u in settings["sender_users"] if u})
		count = count_due_today()
		result["count"] = count
		result["recipients"] = len(senders)
		if not senders or count <= 0:
			return result

		from jarz_pos.api import notifications as n

		if n.outbound_alerts_suppressed("followup_daily"):
			result["status"] = "suppressed_test_run"
			return result
		data = {
			"type": "followup_daily",
			"notification_id": "followup-%s" % frappe.utils.nowdate(),
			"title": "Jarz",
			"body": "عندك %s متابعة النهارده" % count,
			"count": str(count),
		}
		tokens, platforms = n._get_token_targets_for_users(senders)
		vapid = n._get_vapid_subscriptions_for_users(senders)
		sent = 0
		if tokens:
			sent += int(n._send_fcm_notifications(tokens, data, platforms=platforms).get("success_count", 0))
		if vapid:
			sent += int(n._send_vapid_notifications(vapid, data).get("success_count", 0))
		result.update({"status": "sent" if sent else "no_delivery", "success_count": sent})
	except Exception:
		result["status"] = "failed_exception"
		try:
			frappe.log_error(frappe.get_traceback(), "followups: daily push failed")
		except Exception:
			pass
	return result

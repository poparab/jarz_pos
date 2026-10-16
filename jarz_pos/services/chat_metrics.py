"""Chat inbox metrics: volume, response time, draft quality and AI cost.

``drafts`` and ``as_is_rate`` count real drafts only (``is_planted = 0``):
a planted draft is a test of the moderator, not a suggestion, and counting it
would skew the as-is rate in exactly the direction the test exists to catch.
Planted results are reported apart in ``planted``.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable
from statistics import median
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc


def first_response_minutes(rows: Iterable[dict[str, Any]]) -> list[float]:
	"""Minutes from each customer turn to the next outbound reply.

	``rows`` are messages ordered by (conversation, timestamp). A customer turn
	starts at the first inbound message after an outbound one (or the first
	message of the thread); reactions do not count. Unanswered turns are left out.
	"""
	out: list[float] = []
	current_conv = None
	waiting_from: _dt.datetime | None = None
	for row in rows:
		conv = row.get("conversation")
		if conv != current_conv:
			current_conv = conv
			waiting_from = None
		if row.get("message_type") == "reaction":
			continue
		ts = cc.to_dt(row.get("timestamp"))
		if ts is None:
			continue
		if row.get("direction") == "Inbound":
			if waiting_from is None:
				waiting_from = ts
		elif waiting_from is not None:
			out.append(max(0.0, (ts - waiting_from).total_seconds() / 60.0))
			waiting_from = None
	return out


def _range(from_date: Any, to_date: Any) -> tuple[_dt.date, _dt.date]:
	today = frappe.utils.getdate(frappe.utils.nowdate())
	end = frappe.utils.getdate(to_date) if to_date else today
	start = frappe.utils.getdate(from_date) if from_date else end - _dt.timedelta(days=6)
	if start > end:
		start, end = end, start
	return start, end


def get_metrics(from_date: Any = None, to_date: Any = None) -> dict[str, Any]:
	start, end = _range(from_date, to_date)
	low = _dt.datetime.combine(start, _dt.time.min)
	high = _dt.datetime.combine(end, _dt.time(23, 59, 59))
	params = {"low": low, "high": high}

	counts = frappe.db.sql(
		"""
		select count(distinct conversation) as conversations,
			sum(case when direction = 'Inbound' then 1 else 0 end) as inbound,
			sum(case when direction = 'Outbound' then 1 else 0 end) as outbound
		from `tabJarz Chat Message`
		where timestamp between %(low)s and %(high)s
		""",
		params,
		as_dict=True,
	)
	row = counts[0] if counts else {}

	msgs = frappe.db.sql(
		"""
		select conversation, direction, message_type, timestamp
		from `tabJarz Chat Message`
		where timestamp between %(low)s and %(high)s
		order by conversation, timestamp, creation
		""",
		params,
		as_dict=True,
	)
	responses = first_response_minutes(msgs)

	from jarz_pos.services.chat_settings import get_chat_settings

	settings = get_chat_settings()
	alert_before = cc.now() - _dt.timedelta(minutes=int(settings.get("alert_after_minutes") or 10))
	open_rows = frappe.db.sql(
		"""
		select count(*) as unanswered,
			sum(case when waiting_since <= %(alert_before)s then 1 else 0 end) as over_alert
		from `tabJarz Chat Conversation`
		where status = 'Open' and waiting_since is not null
		""",
		{"alert_before": alert_before},
		as_dict=True,
	)
	open_row = open_rows[0] if open_rows else {}

	draft_rows = frappe.db.sql(
		"""
		select status, ifnull(is_planted, 0) as is_planted, ifnull(planted_caught, 0) as caught,
			count(*) as n, coalesce(sum(cost_usd), 0) as cost
		from `tabJarz Chat Draft`
		where creation between %(low)s and %(high)s
		group by status, ifnull(is_planted, 0), ifnull(planted_caught, 0)
		""",
		params,
		as_dict=True,
	)
	drafts = {"total": 0, "sent_as_is": 0, "sent_edited": 0, "rejected": 0, "superseded": 0, "blocked": 0}
	planted = {"total": 0, "caught": 0}
	cost = 0.0
	key = {
		"Sent As-Is": "sent_as_is",
		"Sent Edited": "sent_edited",
		"Rejected": "rejected",
		"Superseded": "superseded",
		"Blocked": "blocked",
	}
	for d in draft_rows:
		cost += float(d.cost or 0)
		n = int(d.n or 0)
		if int(d.is_planted):
			if d.status in ("Sent As-Is", "Sent Edited", "Rejected", "Blocked"):
				planted["total"] += n
				if int(d.caught):
					planted["caught"] += n
			if d.status == "Blocked":
				drafts["blocked"] += n
			continue
		if d.status in ("Held", "Failed"):
			continue
		drafts["total"] += n
		if d.status in key:
			drafts[key[d.status]] += n
	reviewed = drafts["sent_as_is"] + drafts["sent_edited"]
	as_is_rate = round(drafts["sent_as_is"] / reviewed, 4) if reviewed else None

	sent_rows = frappe.db.sql(
		"""
		select sent_by as user, count(*) as sent
		from `tabJarz Chat Message`
		where direction = 'Outbound' and sent_via = 'App' and ifnull(sent_by, '') != ''
			and timestamp between %(low)s and %(high)s
		group by sent_by
		""",
		params,
		as_dict=True,
	)
	review_rows = frappe.db.sql(
		"""
		select reviewed_by as user,
			sum(case when status = 'Sent As-Is' then 1 else 0 end) as as_is,
			sum(case when status = 'Sent Edited' then 1 else 0 end) as edited,
			sum(case when status = 'Rejected' then 1 else 0 end) as rejected
		from `tabJarz Chat Draft`
		where ifnull(is_planted, 0) = 0 and ifnull(reviewed_by, '') != ''
			and creation between %(low)s and %(high)s
		group by reviewed_by
		""",
		params,
		as_dict=True,
	)
	by_user: dict[str, dict[str, Any]] = {}
	for r in sent_rows:
		by_user.setdefault(r.user, {"sent": 0, "as_is": 0, "edited": 0, "rejected": 0})["sent"] = int(
			r.sent or 0
		)
	for r in review_rows:
		entry = by_user.setdefault(r.user, {"sent": 0, "as_is": 0, "edited": 0, "rejected": 0})
		entry["as_is"] = int(r.as_is or 0)
		entry["edited"] = int(r.edited or 0)
		entry["rejected"] = int(r.rejected or 0)
	by_moderator = [
		{"user": user, "full_name": cc.full_name(user), **values}
		for user, values in sorted(by_user.items(), key=lambda kv: -kv[1]["sent"])
	]

	return {
		"from_date": str(start),
		"to_date": str(end),
		"conversations": int(row.get("conversations") or 0),
		"inbound_messages": int(row.get("inbound") or 0),
		"outbound_messages": int(row.get("outbound") or 0),
		"median_first_response_minutes": round(median(responses), 1) if responses else None,
		"unanswered_open": int(open_row.get("unanswered") or 0),
		"waiting_over_alert": int(open_row.get("over_alert") or 0),
		"drafts": drafts,
		"as_is_rate": as_is_rate,
		"planted": planted,
		"cost_usd": round(cost, 4),
		"by_moderator": by_moderator,
	}

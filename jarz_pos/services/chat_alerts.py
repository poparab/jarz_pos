"""Push moderators when a customer has waited too long for a reply.

Runs every minute (``hooks.scheduler_events["cron"]["* * * * *"]``). A
conversation qualifies when it is Open, has waited at least
``alert_after_minutes``, was last alerted at least ``alert_repeat_minutes`` ago
(or never), and has been alerted fewer than :data:`MAX_ALERTS` times.

The alert fields are written *before* the push goes out, so a slow FCM call
cannot make the next minute's run alert the same conversation twice.

Never raises: a scheduler job that throws every minute floods the Error Log.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

MAX_ALERTS = 6
MAX_PER_RUN = 20


def alert_text(contact_name: Any, preview: Any, minutes: int, channel: Any = None) -> tuple[str, str]:
	"""``(title, body)`` for one waiting conversation."""
	title = f"Customer waiting {max(1, int(minutes))} min"
	who = str(contact_name or "").strip() or str(channel or "Customer")
	text = " ".join(str(preview or "").split())
	if len(text) > 100:
		text = text[:99] + "…"
	body = f"{who}: {text}" if text else who
	return title, body


def _due_conversations(now: _dt.datetime, after: int, repeat: int) -> list[dict[str, Any]]:
	waited_before = now - _dt.timedelta(minutes=after)
	repeat_before = now - _dt.timedelta(minutes=repeat)
	return frappe.db.sql(
		"""
		select name, channel, contact_name, last_message_preview, waiting_since,
			assigned_to, ifnull(alert_count, 0) as alert_count
		from `tabJarz Chat Conversation`
		where status = 'Open'
			and waiting_since is not null
			and waiting_since <= %(waited_before)s
			and (alert_sent_at is null or alert_sent_at <= %(repeat_before)s)
			and ifnull(alert_count, 0) < %(max_alerts)s
		order by waiting_since asc
		limit %(limit)s
		""",
		{
			"waited_before": waited_before,
			"repeat_before": repeat_before,
			"max_alerts": MAX_ALERTS,
			"limit": MAX_PER_RUN,
		},
		as_dict=True,
	)


def _alert_recipients(assigned_to: str | None) -> list[str]:
	users = set(cc.moderator_users(alerts_only=True))
	if not users:
		try:
			from jarz_pos.api import notifications as n
			from jarz_pos.constants import ROLES

			users.update(n._get_users_with_roles([ROLES.MODERATOR]))
		except Exception:
			pass
	if assigned_to:
		users.add(assigned_to)
	users.discard("Guest")
	return sorted(u for u in users if u)


def check_waiting() -> dict[str, Any]:
	"""Scheduler entry point. Returns a small summary for the console."""
	result: dict[str, Any] = {"status": "skipped", "alerted": 0, "sent": 0}
	try:
		if not frappe.db.table_exists("Jarz Chat Conversation"):
			return result
		from jarz_pos.services.chat_settings import get_chat_settings

		settings = get_chat_settings(refresh=True)
		if not settings.get("enabled"):
			return result
		now = cc.now()
		rows = _due_conversations(now, settings["alert_after_minutes"], settings["alert_repeat_minutes"])
		if not rows:
			result["status"] = "none_due"
			return result

		from jarz_pos.api import notifications as n

		suppressed = n.outbound_alerts_suppressed("chat_waiting")
		for row in rows:
			try:
				frappe.db.set_value(
					cc.CONVERSATION,
					row.name,
					{"alert_sent_at": now, "alert_count": int(row.alert_count or 0) + 1},
					update_modified=False,
				)
				frappe.db.commit()
				result["alerted"] += 1
				if suppressed:
					continue
				waited = cc.to_dt(row.waiting_since) or now
				minutes = int((now - waited).total_seconds() // 60)
				title, body = alert_text(row.contact_name, row.last_message_preview, minutes, row.channel)
				data = {
					"type": n.CHAT_WAITING_NOTIFICATION_TYPE,
					"notification_id": f"chat-{row.name}",
					"conversation": row.name,
					"title": title,
					"body": body,
					"route": f"/chats/{row.name}",
				}
				users = _alert_recipients(row.assigned_to)
				if not users:
					continue
				tokens, platforms = n._get_token_targets_for_users(users)
				vapid = n._get_vapid_subscriptions_for_users(users)
				if tokens:
					result["sent"] += int(
						n._send_fcm_notifications(tokens, data, platforms=platforms).get("success_count", 0)
					)
				if vapid:
					result["sent"] += int(n._send_vapid_notifications(vapid, data).get("success_count", 0))
			except Exception:
				cc.safe_log_error(
					"chat: waiting alert failed", f"conversation={row.get('name')}\n{frappe.get_traceback()}"
				)
		result["status"] = "suppressed_test_run" if suppressed else "done"
	except Exception:
		result["status"] = "failed_exception"
		cc.safe_log_error("chat: check_waiting failed")
	return result

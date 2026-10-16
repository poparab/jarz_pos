"""Send a moderator's reply to the customer, on whichever channel the thread lives.

Only ever called from a human action (``api/chat.send_message`` /
``send_draft``): nothing in the inbox sends on its own.

The routing rule is pure (:func:`decide_send_route`) so the window logic is
tested without Meta or a database:

* **WhatsApp**: free text only inside the 24-hour window. After it a template
  message is needed, which this inbox does not send.
* **Messenger / Instagram DM**: inside 24 hours a normal reply; from 24 hours to
  7 days a reply tagged ``HUMAN_AGENT``; after 7 days nothing.
* **Comments**: the first reply is a private (DM) reply, which Meta allows once
  per comment within 7 days; later replies, or any reply once the private window
  has closed, go public under the comment.

The outbound row is written ``queued`` and committed *before* the Graph call,
so an echo webhook arriving mid-call finds it, and a failure is kept as a
``failed`` row with Meta's error instead of vanishing.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import frappe
from frappe import _

from jarz_pos.services import chat_common as cc

MAX_TEXT = 4096
CHANNEL_MAX_TEXT = {cc.MESSENGER: 2000, cc.INSTAGRAM: 1000}
HUMAN_AGENT_DAYS = 7
DM_HOURS = 24

ROUTE_WA = "wa_text"
ROUTE_DM = "dm"
ROUTE_PRIVATE = "private_reply"
ROUTE_PUBLIC = "public_reply"


def decide_send_route(
	channel: str,
	now: _dt.datetime,
	window_expires_at: Any,
	last_inbound_at: Any,
	private_reply_sent: Any,
) -> dict[str, Any]:
	"""``{ok, route, tag, error}``. ``error`` is a code, see :func:`_route_error`."""
	expires = cc.to_dt(window_expires_at)
	last_in = cc.to_dt(last_inbound_at)
	if channel == cc.WHATSAPP:
		if expires is not None and now < expires:
			return {"ok": True, "route": ROUTE_WA, "tag": None, "error": None}
		return {"ok": False, "route": None, "tag": None, "error": "wa_window_closed"}
	if channel in (cc.MESSENGER, cc.INSTAGRAM):
		if last_in is None:
			return {"ok": False, "route": None, "tag": None, "error": "no_inbound"}
		age = now - last_in
		if age < _dt.timedelta(hours=DM_HOURS):
			return {"ok": True, "route": ROUTE_DM, "tag": None, "error": None}
		if age < _dt.timedelta(days=HUMAN_AGENT_DAYS):
			return {"ok": True, "route": ROUTE_DM, "tag": "HUMAN_AGENT", "error": None}
		return {"ok": False, "route": None, "tag": None, "error": "dm_window_closed"}
	if channel in cc.COMMENT_CHANNELS:
		if not int(private_reply_sent or 0) and expires is not None and now < expires:
			return {"ok": True, "route": ROUTE_PRIVATE, "tag": None, "error": None}
		return {"ok": True, "route": ROUTE_PUBLIC, "tag": None, "error": None}
	return {"ok": False, "route": None, "tag": None, "error": "unknown_channel"}


def _route_error(code: str | None) -> str:
	if code == "wa_window_closed":
		return _("The 24-hour WhatsApp window has closed. A template message is needed.")
	if code == "dm_window_closed":
		return _("The 7-day reply window has closed. The customer has to message first.")
	if code == "no_inbound":
		return _("The customer has not messaged this page yet, so a reply cannot be sent.")
	return _("This conversation's channel cannot be replied to from the inbox.")


def text_limit(channel: str) -> int:
	return min(MAX_TEXT, CHANNEL_MAX_TEXT.get(channel, MAX_TEXT))


def validate_text(channel: str, text: Any) -> str:
	clean = str(text or "").strip()
	if not clean:
		frappe.throw(_("The message is empty."))
	limit = text_limit(channel)
	if len(clean) > limit:
		frappe.throw(
			_("The message is too long: {0} characters, the limit here is {1}.").format(len(clean), limit)
		)
	return clean


def _call_meta(conv: Any, route: str, tag: str | None, text: str) -> str:
	from jarz_pos.services import chat_meta_client as meta

	if route == ROUTE_WA:
		return meta.wa_send_text(conv.external_id, text)
	if route == ROUTE_DM:
		if conv.channel == cc.INSTAGRAM:
			return meta.ig_send(conv.external_id, text, tag=tag)
		return meta.messenger_send(conv.external_id, text, tag=tag)
	instagram = conv.channel == cc.INSTAGRAM_COMMENT
	if route == ROUTE_PRIVATE:
		return meta.comment_private_reply(conv.external_id, text, instagram=instagram)
	return meta.comment_public_reply(conv.external_id, text, instagram=instagram)


def send_text(conversation: str, text: str, user: str, draft: str | None = None) -> dict[str, Any]:
	"""Send *text* on *conversation* as *user*. Returns the Message shape.

	Throws (after recording a ``failed`` row) when Meta refuses; the caller's
	draft is untouched in that case, so the moderator can retry.
	"""
	conv = frappe.db.get_value(
		cc.CONVERSATION,
		conversation,
		[
			"name",
			"channel",
			"external_id",
			"status",
			"assigned_to",
			"window_expires_at",
			"last_inbound_at",
			"private_reply_sent",
		],
		as_dict=True,
	)
	if not conv:
		frappe.throw(_("Conversation {0} not found.").format(conversation), frappe.DoesNotExistError)

	clean = validate_text(conv.channel, text)
	at = cc.now()
	route = decide_send_route(
		conv.channel, at, conv.window_expires_at, conv.last_inbound_at, conv.private_reply_sent
	)
	if not route["ok"]:
		frappe.throw(_route_error(route["error"]))

	msg = frappe.get_doc(
		{
			"doctype": cc.MESSAGE,
			"conversation": conv.name,
			"direction": "Outbound",
			"message_type": "comment" if conv.channel in cc.COMMENT_CHANNELS else "text",
			"body": clean,
			"sent_via": "App",
			"sent_by": user,
			"draft": draft,
			"status": "queued",
			"timestamp": at,
		}
	)
	msg.insert(ignore_permissions=True)
	frappe.db.commit()

	try:
		external_id = _call_meta(conv, route["route"], route["tag"], clean)
	except Exception as exc:
		error = str(exc)[:500] or type(exc).__name__
		frappe.db.set_value(cc.MESSAGE, msg.name, {"status": "failed", "error": error}, update_modified=True)
		frappe.db.commit()
		cc.publish_chat_update(conv.name, "message", message=cc.load_message(msg.name))
		frappe.db.commit()
		frappe.throw(_("Sending failed: {0}").format(error))

	frappe.db.set_value(
		cc.MESSAGE, msg.name, {"status": "sent", "external_message_id": external_id}, update_modified=True
	)
	updates: dict[str, Any] = {
		"status": "Answered",
		"waiting_since": None,
		"alert_sent_at": None,
		"alert_count": 0,
		"unread_count": 0,
		"last_outbound_at": at,
		"last_message_at": at,
		"last_message_preview": cc.preview(clean),
		"last_direction": "Outbound",
	}
	if not conv.assigned_to:
		updates["assigned_to"] = user
	if route["route"] == ROUTE_PRIVATE:
		updates["private_reply_sent"] = 1
	frappe.db.set_value(cc.CONVERSATION, conv.name, updates, update_modified=True)
	cc.supersede_live_drafts(conv.name, keep=(draft,))

	message = cc.load_message(msg.name)
	cc.publish_chat_update(conv.name, "message", message=message)
	return message

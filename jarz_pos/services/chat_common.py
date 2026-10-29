"""Shared helpers for the chat inbox: shapes, recipients and the realtime event.

Every endpoint and service returns the contract shapes (ConversationSummary,
Message, Draft, CustomerCard) built here, so the Flutter client sees one form
whichever path produced the data. Datetimes are always
``"YYYY-MM-DD HH:MM:SS"`` strings in the site time zone, never objects.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable
from typing import Any

import frappe

from jarz_pos.constants import ROLES, WS_EVENTS

CONVERSATION = "Jarz Chat Conversation"
MESSAGE = "Jarz Chat Message"
DRAFT = "Jarz Chat Draft"

WHATSAPP = "WhatsApp"
INSTAGRAM = "Instagram"
MESSENGER = "Messenger"
FACEBOOK_COMMENT = "Facebook Comment"
INSTAGRAM_COMMENT = "Instagram Comment"
CHANNELS = (WHATSAPP, INSTAGRAM, MESSENGER, FACEBOOK_COMMENT, INSTAGRAM_COMMENT)
COMMENT_CHANNELS = (FACEBOOK_COMMENT, INSTAGRAM_COMMENT)

#: Draft statuses that are still "live" for a conversation.
LIVE_DRAFT_STATUSES = ("Pending", "Held")

SUMMARY_FIELDS = (
	"name",
	"channel",
	"external_id",
	"contact_name",
	"phone",
	"customer",
	"status",
	"assigned_to",
	"intent",
	"is_complaint",
	"last_message_at",
	"last_inbound_at",
	"last_message_preview",
	"last_direction",
	"unread_count",
	"waiting_since",
	"window_expires_at",
	"private_reply_sent",
	"last_order",
)

MESSAGE_FIELDS = (
	"name",
	"conversation",
	"direction",
	"message_type",
	"body",
	"media_file",
	"media_mime",
	"transcript",
	"status",
	"sent_via",
	"sent_by",
	"timestamp",
	"error",
	"external_message_id",
	"draft",
)

DRAFT_FIELDS = (
	"name",
	"conversation",
	"status",
	"draft_text",
	"template_text",
	"upsell_item",
	"upsell_text",
	"intent",
	"is_complaint",
	"needs_human_reason",
	"warnings",
	"creation",
	"for_message",
	"is_planted",
	"real_draft",
	"planted_error",
)

PREVIEW_CHARS = 140


# ── small utilities ─────────────────────────────────────────────────────


def now() -> _dt.datetime:
	return frappe.utils.now_datetime()


def to_dt(value: Any) -> _dt.datetime | None:
	if value in (None, ""):
		return None
	if isinstance(value, _dt.datetime):
		return value
	try:
		return frappe.utils.get_datetime(value)
	except Exception:
		return None


def dt_str(value: Any) -> str | None:
	"""``"YYYY-MM-DD HH:MM:SS"`` or ``None``. Drops microseconds."""
	parsed = to_dt(value)
	if parsed is None:
		return None
	return parsed.strftime("%Y-%m-%d %H:%M:%S")


def window_open(window_expires_at: Any, at: _dt.datetime | None = None) -> bool:
	expires = to_dt(window_expires_at)
	if expires is None:
		return False
	return (at or now()) < expires


def voice_text(transcript: Any) -> str:
	"""``[voice note] «words»`` for a transcribed voice note, one line, or ``""``."""
	words = " ".join(str(transcript or "").replace("«", "").replace("»", "").split())
	return f"[voice note] «{words}»" if words else ""


def preview(text: Any, message_type: str | None = None, transcript: Any = None) -> str:
	body = " ".join(str(text or "").split())
	if not body and str(message_type or "") == "audio":
		body = voice_text(transcript)
	if not body:
		body = {
			"image": "[image]",
			"audio": "[voice note]",
			"video": "[video]",
			"document": "[document]",
			"sticker": "[sticker]",
			"location": "[location]",
			"reaction": "[reaction]",
			"unsupported": "[unsupported message]",
		}.get(str(message_type or ""), "")
	return body[:PREVIEW_CHARS]


def split_warnings(value: Any) -> list[str]:
	if not value:
		return []
	if isinstance(value, list | tuple):
		return [str(v) for v in value if str(v or "").strip()]
	return [line.strip() for line in str(value).splitlines() if line.strip()]


_NAME_CACHE: dict[str, str] = {}


def full_name(user: str | None) -> str | None:
	if not user:
		return None
	key = f"{getattr(frappe.local, 'site', '')}:{user}"
	if key in _NAME_CACHE:
		return _NAME_CACHE[key]
	try:
		name = frappe.db.get_value("User", user, "full_name") or user
	except Exception:
		name = user
	if len(_NAME_CACHE) > 500:
		_NAME_CACHE.clear()
	_NAME_CACHE[key] = name
	return name


def _customer_name(customer: str | None) -> str | None:
	if not customer:
		return None
	try:
		return frappe.db.get_value("Customer", customer, "customer_name") or customer
	except Exception:
		return customer


def _item_name(item_code: str | None) -> str | None:
	if not item_code:
		return None
	try:
		return frappe.db.get_value("Item", item_code, "item_name") or item_code
	except Exception:
		return item_code


# ── serializers ─────────────────────────────────────────────────────────


def pending_draft_conversations(names: Iterable[str]) -> set[str]:
	names = [n for n in names if n]
	if not names:
		return set()
	try:
		rows = frappe.get_all(
			DRAFT,
			filters={"conversation": ["in", names], "status": "Pending"},
			pluck="conversation",
			ignore_permissions=True,
		)
	except Exception:
		return set()
	return set(rows or [])


def serialize_summary(
	row: Any, has_pending_draft: bool | None = None, at: _dt.datetime | None = None
) -> dict[str, Any]:
	"""ConversationSummary from a ``frappe._dict`` / Document carrying SUMMARY_FIELDS."""
	get = row.get
	name = get("name")
	if has_pending_draft is None:
		has_pending_draft = name in pending_draft_conversations([name])
	return {
		"name": name,
		"channel": get("channel"),
		"contact_name": get("contact_name") or None,
		"phone": get("phone") or None,
		"customer": get("customer") or None,
		"customer_name": _customer_name(get("customer")),
		"status": get("status") or "Open",
		"assigned_to": get("assigned_to") or None,
		"assigned_to_name": full_name(get("assigned_to")),
		"intent": get("intent") or None,
		"is_complaint": bool(get("is_complaint")),
		"last_message_at": dt_str(get("last_message_at")),
		"last_message_preview": get("last_message_preview") or "",
		"last_direction": get("last_direction") or None,
		"unread_count": int(get("unread_count") or 0),
		"waiting_since": dt_str(get("waiting_since")),
		"window_expires_at": dt_str(get("window_expires_at")),
		"window_open": window_open(get("window_expires_at"), at),
		"has_pending_draft": bool(has_pending_draft),
		"last_order": get("last_order") or None,
	}


def load_summary(name: str) -> dict[str, Any] | None:
	try:
		row = frappe.db.get_value(CONVERSATION, name, list(SUMMARY_FIELDS), as_dict=True)
	except Exception:
		return None
	return serialize_summary(row) if row else None


def serialize_message(row: Any) -> dict[str, Any]:
	get = row.get
	media_file = str(get("media_file") or "")
	return {
		"name": get("name"),
		"direction": get("direction"),
		"message_type": get("message_type") or "text",
		"body": get("body") or "",
		"media_url": media_file
		if media_file.startswith("/private/files/") or media_file.startswith("/files/")
		else None,
		"media_mime": get("media_mime") or None,
		"transcript": get("transcript") or None,
		"status": get("status") or None,
		"sent_via": get("sent_via") or None,
		"sent_by": get("sent_by") or None,
		"sent_by_name": full_name(get("sent_by")),
		"timestamp": dt_str(get("timestamp")),
		"error": get("error") or None,
	}


def load_message(name: str) -> dict[str, Any] | None:
	try:
		row = frappe.db.get_value(MESSAGE, name, list(MESSAGE_FIELDS), as_dict=True)
	except Exception:
		return None
	return serialize_message(row) if row else None


def serialize_draft(row: Any) -> dict[str, Any]:
	"""Draft shape. ``is_planted`` / ``planted_error`` are NEVER exposed: a planted
	draft must look exactly like a real one or the check measures nothing."""
	get = row.get
	return {
		"name": get("name"),
		"status": get("status"),
		"draft_text": get("draft_text") or "",
		"upsell_item": get("upsell_item") or None,
		"upsell_item_name": _item_name(get("upsell_item")),
		"upsell_text": get("upsell_text") or None,
		"intent": get("intent") or None,
		"is_complaint": bool(get("is_complaint")),
		"needs_human_reason": get("needs_human_reason") or None,
		"warnings": split_warnings(get("warnings")),
		"created_at": dt_str(get("creation")),
		"for_message": get("for_message") or None,
	}


def load_draft_row(name: str) -> Any:
	try:
		return frappe.db.get_value(DRAFT, name, list(DRAFT_FIELDS), as_dict=True)
	except Exception:
		return None


def load_draft(name: str | None) -> dict[str, Any] | None:
	if not name:
		return None
	row = load_draft_row(name)
	return serialize_draft(row) if row else None


def pending_draft(conversation: str) -> dict[str, Any] | None:
	try:
		rows = frappe.get_all(
			DRAFT,
			filters={"conversation": conversation, "status": "Pending"},
			fields=list(DRAFT_FIELDS),
			order_by="creation desc",
			limit_page_length=1,
			ignore_permissions=True,
		)
	except Exception:
		return None
	return serialize_draft(rows[0]) if rows else None


def customer_card(customer: str | None) -> dict[str, Any] | None:
	"""CustomerCard: identity plus order aggregates. Never raises."""
	if not customer:
		return None
	try:
		cust = frappe.db.get_value(
			"Customer", customer, ["name", "customer_name", "mobile_no", "territory"], as_dict=True
		)
	except Exception:
		cust = None
	if not cust:
		return None
	card = {
		"name": cust.name,
		"customer_name": cust.customer_name or cust.name,
		"mobile_no": cust.mobile_no or None,
		"territory": cust.territory or None,
		"total_orders": 0,
		"last_order_date": None,
		"last_order_total": None,
		"lifetime_value": 0.0,
		"recent_items": [],
	}
	try:
		agg = frappe.db.sql(
			"""
			select count(*) as orders, coalesce(sum(grand_total), 0) as ltv
			from `tabSales Invoice`
			where customer = %s and docstatus = 1 and ifnull(is_return, 0) = 0
			""",
			(customer,),
			as_dict=True,
		)
		if agg:
			card["total_orders"] = int(agg[0].orders or 0)
			card["lifetime_value"] = float(agg[0].ltv or 0)
		last = frappe.get_all(
			"Sales Invoice",
			filters={"customer": customer, "docstatus": 1, "is_return": 0},
			fields=["name", "posting_date", "grand_total"],
			order_by="posting_date desc, creation desc",
			limit_page_length=5,
			ignore_permissions=True,
		)
		if last:
			card["last_order_date"] = str(last[0].posting_date) if last[0].posting_date else None
			card["last_order_total"] = float(last[0].grand_total or 0)
			items = frappe.get_all(
				"Sales Invoice Item",
				filters={"parent": ["in", [r.name for r in last]], "parenttype": "Sales Invoice"},
				fields=["item_name", "parent", "idx"],
				order_by="creation desc, idx asc",
				limit_page_length=40,
				ignore_permissions=True,
			)
			seen: list[str] = []
			for it in items:
				label = str(it.item_name or "").strip()
				if label and label not in seen:
					seen.append(label)
				if len(seen) >= 5:
					break
			card["recent_items"] = seen
	except Exception:
		pass
	return card


def top_items(customer: str | None, limit: int = 3) -> list[str]:
	"""The customer's most-ordered item names (aggregate only, for the AI)."""
	if not customer:
		return []
	try:
		rows = frappe.db.sql(
			"""
			select sii.item_name, sum(sii.qty) as qty
			from `tabSales Invoice Item` sii
			join `tabSales Invoice` si on si.name = sii.parent
			where si.customer = %s and si.docstatus = 1 and ifnull(si.is_return, 0) = 0
			group by sii.item_name
			order by qty desc
			limit %s
			""",
			(customer, int(limit)),
			as_dict=True,
		)
		return [str(r.item_name) for r in rows if r.item_name]
	except Exception:
		return []


def supersede_live_drafts(conversation: str, keep: Iterable[str | None] = ()) -> list[str]:
	"""Mark every Pending / Held draft of *conversation* Superseded, except *keep*.

	Called whenever an outbound message goes out (app send or phone echo): the
	customer has been answered, so an older suggestion must not be sent on top.
	"""
	keep_set = {k for k in keep if k}
	try:
		names = frappe.get_all(
			DRAFT,
			filters={"conversation": conversation, "status": ["in", list(LIVE_DRAFT_STATUSES)]},
			pluck="name",
			ignore_permissions=True,
		)
	except Exception:
		return []
	changed = []
	for name in names or []:
		if name in keep_set:
			continue
		frappe.db.set_value(DRAFT, name, "status", "Superseded", update_modified=True)
		changed.append(name)
	return changed


# ── recipients and realtime ─────────────────────────────────────────────


def moderator_users(alerts_only: bool = False) -> list[str]:
	from jarz_pos.services.chat_settings import get_chat_settings

	users = []
	for row in get_chat_settings().get("moderators") or []:
		if alerts_only and not row.get("receive_alerts"):
			continue
		users.append(row["user"])
	return users


def chat_recipients() -> list[str]:
	"""Realtime audience: Settings moderators plus every Moderator / JARZ Manager / line manager."""
	users = set(moderator_users())
	try:
		from jarz_pos.api import notifications as n

		users.update(
			n._get_users_with_roles([ROLES.MODERATOR, ROLES.JARZ_MANAGER, *sorted(ROLES.LINE_MANAGER_TIER)])
		)
	except Exception:
		pass
	users.discard("Guest")
	return sorted(u for u in users if u)


def publish_chat_update(
	conversation: str,
	kind: str,
	message: dict | None = None,
	draft: dict | None = None,
	summary: dict | None = None,
) -> None:
	"""Emit ``jarz_chat_update`` to each recipient's own room, after commit.

	One room per user (``user=<email>``): ``user="*"`` and list rooms are dead
	in Frappe. ``after_commit`` so a client that refetches on the event reads
	the committed row. Never raises.
	"""
	try:
		payload = {
			"conversation": conversation,
			"kind": kind,
			"summary": summary if summary is not None else load_summary(conversation),
			"message": message,
			"draft": draft,
		}
		for user in chat_recipients():
			try:
				frappe.publish_realtime(WS_EVENTS.CHAT_UPDATE, payload, user=user, after_commit=True)
			except Exception:
				pass
	except Exception:
		safe_log_error("chat: realtime publish failed")


def safe_log_error(title: str, message: str | None = None) -> None:
	"""``frappe.log_error`` that can never raise into the caller.

	Callers pass a traceback or a short masked note, never a raw webhook body.
	"""
	try:
		frappe.log_error(
			title=title[:140],
			message=message if message is not None else frappe.get_traceback(),
		)
	except Exception:
		pass

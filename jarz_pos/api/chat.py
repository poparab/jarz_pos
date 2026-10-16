"""Chat inbox API for moderators: conversations, AI drafts, replies, metrics.

URL prefix: ``/api/method/jarz_pos.api.chat.<function>``

Every endpoint's first statement is :func:`_gate` (CHAT_ROLES, Administrator
bypass). Reads and writes after the gate use ``ignore_permissions``: the gate
*is* the permission model for the inbox, and the DocType permissions only
govern Desk.

Nothing here sends a message on its own. ``send_message`` and ``send_draft``
are the only paths to a customer, and both are a moderator's tap.

Server-side rules the client cannot skip:

* a complaint draft cannot be sent unedited;
* a planted draft sent unedited is **blocked** (never sent), and the real draft
  takes its place;
* any outbound message supersedes the conversation's live drafts.

Endpoint names avoid "create": the Flutter offline queue replays any failed
POST whose path contains it.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from jarz_pos.constants import CHAT_ROLES, ROLES
from jarz_pos.services import chat_common as cc

_DRAFT_LOCK_SECONDS = 60
_MAX_LIST = 100
_MAX_MESSAGES = 200


def _gate(roles=CHAT_ROLES) -> None:
	if frappe.session.user == ROLES.ADMINISTRATOR:
		return
	frappe.only_for(list(roles))


def _int(value: Any, default: int, maximum: int) -> int:
	try:
		number = int(value)
	except (TypeError, ValueError):
		number = default
	if number <= 0:
		number = default
	return min(number, maximum)


def _conversation_or_throw(name: Any) -> Any:
	name = str(name or "").strip()
	row = frappe.db.get_value(cc.CONVERSATION, name, list(cc.SUMMARY_FIELDS), as_dict=True) if name else None
	if not row:
		frappe.throw(_("Conversation {0} not found.").format(name or "?"), frappe.DoesNotExistError)
	return row


def _draft_or_throw(name: Any) -> Any:
	row = cc.load_draft_row(str(name or "").strip()) if name else None
	if not row:
		frappe.throw(_("Draft {0} not found.").format(name or "?"), frappe.DoesNotExistError)
	return row


def _claim(key: str) -> bool:
	"""Short Redis claim so two moderators cannot act on one draft at once."""
	try:
		# nosemgrep: frappe-cache-breaks-multitenancy -- key goes through cache.make_key(), which adds the per-site prefix
		cache = frappe.cache()
		return bool(cache.set(cache.make_key(key), 1, ex=_DRAFT_LOCK_SECONDS, nx=True))
	except Exception:
		return True


def _release(key: str) -> None:
	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(key))
	except Exception:
		pass


def _promote_real_draft(real: str | None) -> dict[str, Any] | None:
	"""Held real draft -> Pending, returned as the next draft to review."""
	if not real:
		return None
	status = frappe.db.get_value(cc.DRAFT, real, "status")
	if status != "Held":
		return None
	frappe.db.set_value(cc.DRAFT, real, "status", "Pending", update_modified=True)
	return cc.load_draft(real)


def _review_fields(user: str) -> dict[str, Any]:
	return {"reviewed_by": user, "reviewed_at": cc.now()}


# ── reads ───────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["GET", "POST"])
def get_conversations(
	status: str | None = "Open",
	channel: str | None = None,
	search: str | None = None,
	mine: int | str | None = 0,
	limit: int | str | None = 30,
	before: str | None = None,
) -> dict[str, Any]:
	"""Inbox list, newest activity first. ``before`` is a ``last_message_at`` cursor."""
	_gate()
	limit_n = _int(limit, 30, _MAX_LIST)
	filters: list[list[Any]] = []
	status = str(status or "Open").strip()
	if status.lower() != "all":
		if status not in ("Open", "Answered", "Closed"):
			frappe.throw(_("Unknown status filter: {0}").format(status))
		filters.append([cc.CONVERSATION, "status", "=", status])
	if channel:
		if channel not in cc.CHANNELS:
			frappe.throw(_("Unknown channel: {0}").format(channel))
		filters.append([cc.CONVERSATION, "channel", "=", channel])
	if str(mine or "0") in ("1", "true", "True"):
		filters.append([cc.CONVERSATION, "assigned_to", "=", frappe.session.user])
	if before:
		filters.append([cc.CONVERSATION, "last_message_at", "<", before])

	or_filters: list[list[Any]] = []
	term = str(search or "").strip()
	if term:
		like = f"%{term}%"
		or_filters = [
			[cc.CONVERSATION, "contact_name", "like", like],
			[cc.CONVERSATION, "phone", "like", like],
			[cc.CONVERSATION, "last_message_preview", "like", like],
			[cc.CONVERSATION, "external_id", "like", like],
		]
		try:
			from jarz_pos.utils.phone import phone_search_terms

			for extra in phone_search_terms(term):
				if extra and extra != term:
					or_filters.append([cc.CONVERSATION, "phone", "like", f"%{extra}%"])
		except Exception:
			pass

	rows = frappe.get_all(
		cc.CONVERSATION,
		filters=filters,
		or_filters=or_filters or None,
		fields=list(cc.SUMMARY_FIELDS),
		order_by="last_message_at desc, modified desc",
		limit_page_length=limit_n + 1,
		ignore_permissions=True,
	)
	has_more = len(rows) > limit_n
	rows = rows[:limit_n]
	pending = cc.pending_draft_conversations([r.name for r in rows])
	at = cc.now()
	return {
		"conversations": [cc.serialize_summary(r, r.name in pending, at) for r in rows],
		"has_more": has_more,
		"open_count": frappe.db.count(cc.CONVERSATION, {"status": "Open"}),
	}


@frappe.whitelist(methods=["GET", "POST"])
def get_conversation(name: str, limit: int | str | None = 50, before: str | None = None) -> dict[str, Any]:
	"""One thread: summary, messages (oldest to newest), the Pending draft, the customer card.

	Opening a thread marks it read (``unread_count`` = 0) and, on WhatsApp,
	sends blue ticks for the newest inbound message in the background.
	"""
	_gate()
	conv = _conversation_or_throw(name)
	limit_n = _int(limit, 50, _MAX_MESSAGES)
	filters: dict[str, Any] = {"conversation": conv.name}
	if before:
		filters["timestamp"] = ["<", before]
	rows = frappe.get_all(
		cc.MESSAGE,
		filters=filters,
		fields=list(cc.MESSAGE_FIELDS),
		order_by="timestamp desc, creation desc",
		limit_page_length=limit_n + 1,
		ignore_permissions=True,
	)
	has_more = len(rows) > limit_n
	rows = list(reversed(rows[:limit_n]))

	if int(conv.unread_count or 0) > 0 and not before:
		frappe.db.set_value(cc.CONVERSATION, conv.name, "unread_count", 0, update_modified=False)
		# nosemgrep: whitelisted-side-effect-on-get -- get_conversation is called with GET by the Flutter chat client (chat_repository.dart) and marks the thread read; moving it to POST would break the app
		frappe.db.commit()  # a GET is rolled back by Frappe unless committed
		if conv.channel == cc.WHATSAPP:
			wamid = next(
				(
					r.external_message_id
					for r in reversed(rows)
					if r.direction == "Inbound" and r.external_message_id
				),
				None,
			)
			if wamid:
				try:
					# nosemgrep: whitelisted-side-effect-on-get -- get_conversation is called with GET by the Flutter chat client (chat_repository.dart); enqueue is the mark-read side effect of opening a thread
					frappe.enqueue(
						"jarz_pos.services.chat_meta_client.wa_mark_read",
						queue="short",
						timeout=60,
						wamid=wamid,
					)
				except Exception:
					pass
		cc.publish_chat_update(conv.name, "status")
		# nosemgrep: whitelisted-side-effect-on-get -- get_conversation is called with GET by the Flutter chat client (chat_repository.dart); commit persists the mark-read side effect
		frappe.db.commit()

	return {
		"conversation": cc.load_summary(conv.name),
		"messages": [cc.serialize_message(r) for r in rows],
		"draft": cc.pending_draft(conv.name),
		"customer": cc.customer_card(conv.customer),
		"has_more": has_more,
	}


@frappe.whitelist(methods=["GET", "POST"])
def get_order_prefill(conversation: str) -> dict[str, Any]:
	"""What the order screen needs to start an order from this chat."""
	_gate()
	conv = _conversation_or_throw(conversation)
	customer = None
	if conv.customer:
		row = frappe.db.get_value(
			"Customer", conv.customer, ["name", "customer_name", "mobile_no"], as_dict=True
		)
		if row:
			customer = {"name": row.name, "customer_name": row.customer_name, "mobile_no": row.mobile_no}
	phone = conv.phone or (customer or {}).get("mobile_no") or None
	return {"customer": customer, "phone": phone, "contact_name": conv.contact_name or None}


@frappe.whitelist(methods=["GET", "POST"])
def get_chat_metrics(from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
	"""Inbox metrics for a date range (default: the last 7 days)."""
	_gate()
	from jarz_pos.services.chat_metrics import get_metrics

	return get_metrics(from_date, to_date)


@frappe.whitelist(methods=["GET", "POST"])
def get_unread_total() -> dict[str, Any]:
	"""Drawer badge: open conversations and total unread messages."""
	_gate()
	open_count = frappe.db.count(cc.CONVERSATION, {"status": "Open"})
	rows = frappe.db.sql(
		"""
		select coalesce(sum(unread_count), 0)
		from `tabJarz Chat Conversation`
		where status != 'Closed'
		"""
	)
	return {"open_count": int(open_count or 0), "unread_total": int((rows[0][0] if rows else 0) or 0)}


# ── replies and drafts ──────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def send_message(conversation: str, text: str) -> dict[str, Any]:
	"""Send a free-typed reply. Supersedes any live draft of the conversation."""
	_gate()
	conv = _conversation_or_throw(conversation)
	from jarz_pos.services.chat_send import send_text

	message = send_text(conv.name, text, frappe.session.user)
	return {"message": message}


@frappe.whitelist(methods=["POST"])
def send_draft(draft: str, final_text: str) -> dict[str, Any]:
	"""Send a draft, as written or edited, and record how it was reviewed.

	Returns ``{message, draft, planted, caught, next_draft}``. ``message`` is
	null when a planted draft was blocked; ``next_draft`` is then the real one.
	"""
	_gate()
	row = _draft_or_throw(draft)
	lock = f"jarz_chat_draft:{row.name}"
	if not _claim(lock):
		frappe.throw(_("Someone else is already acting on this draft."))
	try:
		return _send_draft(row, final_text)
	finally:
		_release(lock)


def _send_draft(row: Any, final_text: Any) -> dict[str, Any]:
	from jarz_pos.services.chat_drafts import (
		complaint_requires_edit,
		decide_sent_status,
		planted_caught,
		planted_wrong_value,
	)
	from jarz_pos.services.chat_send import send_text

	status = frappe.db.get_value(cc.DRAFT, row.name, "status")
	if status != "Pending":
		frappe.throw(_("This draft is no longer pending ({0}).").format(status or "?"))
	final = str(final_text or "").strip()
	if not final:
		frappe.throw(_("The message is empty."))
	if complaint_requires_edit(row.is_complaint, row.draft_text, final):
		frappe.throw(_("Complaints need a personal reply: edit the draft before sending."))

	user = frappe.session.user
	as_is = str(row.draft_text or "").strip() == final

	if int(row.is_planted or 0) and as_is:
		# The planted wrong price went out unread: block it, never send it.
		frappe.db.set_value(
			cc.DRAFT,
			row.name,
			{"status": "Blocked", "planted_caught": 0, "final_text": final, **_review_fields(user)},
			update_modified=True,
		)
		next_draft = _promote_real_draft(row.real_draft)
		cc.publish_chat_update(row.conversation, "draft", draft=next_draft)
		return {
			"message": None,
			"draft": cc.load_draft(row.name),
			"planted": True,
			"caught": False,
			"planted_error": row.planted_error,
			"next_draft": next_draft,
		}

	message = send_text(row.conversation, final, user, draft=row.name)
	sent_status, distance = decide_sent_status(row.draft_text, final)
	values: dict[str, Any] = {
		"status": sent_status,
		"final_text": final,
		"edit_distance": distance,
		**_review_fields(user),
	}
	caught = False
	planted = bool(int(row.is_planted or 0))
	if planted:
		caught = planted_caught(final, planted_wrong_value(row.planted_error))
		values["planted_caught"] = 1 if caught else 0
		if row.real_draft:
			frappe.db.set_value(cc.DRAFT, row.real_draft, "status", "Superseded", update_modified=True)
	frappe.db.set_value(cc.DRAFT, row.name, values, update_modified=True)
	return {
		"message": message,
		"draft": cc.load_draft(row.name),
		"planted": planted,
		"caught": caught,
		# Revealed only after the review is done, so the moderator learns what was planted.
		"planted_error": row.planted_error if planted else None,
		"next_draft": None,
	}


@frappe.whitelist(methods=["POST"])
def reject_draft(draft: str, reason: str | None = None) -> dict[str, Any]:
	"""Reject a draft. Rejecting a planted draft counts as catching it."""
	_gate()
	row = _draft_or_throw(draft)
	lock = f"jarz_chat_draft:{row.name}"
	if not _claim(lock):
		frappe.throw(_("Someone else is already acting on this draft."))
	try:
		status = frappe.db.get_value(cc.DRAFT, row.name, "status")
		if status != "Pending":
			frappe.throw(_("This draft is no longer pending ({0}).").format(status or "?"))
		values: dict[str, Any] = {
			"status": "Rejected",
			"reject_reason": str(reason or "").strip()[:1000] or None,
			**_review_fields(frappe.session.user),
		}
		next_draft = None
		if int(row.is_planted or 0):
			values["planted_caught"] = 1
		frappe.db.set_value(cc.DRAFT, row.name, values, update_modified=True)
		if int(row.is_planted or 0):
			next_draft = _promote_real_draft(row.real_draft)
		cc.publish_chat_update(row.conversation, "draft", draft=next_draft)
		planted = bool(int(row.is_planted or 0))
		return {
			"draft": cc.load_draft(row.name),
			"next_draft": next_draft,
			"planted": planted,
			"planted_error": row.planted_error if planted else None,
		}
	finally:
		_release(lock)


@frappe.whitelist(methods=["POST"])
def regenerate_draft(conversation: str, hint: str | None = None) -> dict[str, Any]:
	"""Ask the AI for a fresh draft now (synchronous). ``hint`` steers it."""
	_gate()
	conv = _conversation_or_throw(conversation)
	from jarz_pos.services.chat_drafts import generate_draft

	name = generate_draft(conv.name, hint=str(hint or "").strip() or None)
	return {"draft": cc.load_draft(name)}


# ── conversation state ──────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def assign_conversation(conversation: str, user: str | None = None) -> dict[str, Any]:
	"""Assign to *user* (empty = the caller). The assignee must be able to use the inbox."""
	_gate()
	conv = _conversation_or_throw(conversation)
	target = str(user or "").strip() or frappe.session.user
	if target != frappe.session.user:
		if not frappe.db.get_value("User", target, "enabled"):
			frappe.throw(_("User {0} is not an enabled user.").format(target))
		if target != ROLES.ADMINISTRATOR and not set(frappe.get_roles(target)) & set(CHAT_ROLES):
			frappe.throw(_("{0} does not have access to the chat inbox.").format(target))
	frappe.db.set_value(cc.CONVERSATION, conv.name, "assigned_to", target, update_modified=True)
	summary = cc.load_summary(conv.name)
	cc.publish_chat_update(conv.name, "assigned", summary=summary)
	return {"conversation": summary}


@frappe.whitelist(methods=["POST"])
def set_conversation_status(conversation: str, status: str) -> dict[str, Any]:
	"""Close a conversation, or reopen it."""
	_gate()
	conv = _conversation_or_throw(conversation)
	status = str(status or "").strip()
	if status not in ("Open", "Closed"):
		frappe.throw(_("Status must be Open or Closed."))
	if status == "Closed":
		values: dict[str, Any] = {
			"status": "Closed",
			"waiting_since": None,
			"alert_sent_at": None,
			"alert_count": 0,
			"unread_count": 0,
		}
	else:
		waiting = conv.last_inbound_at if conv.last_direction == "Inbound" else None
		values = {"status": "Open", "waiting_since": waiting, "alert_sent_at": None, "alert_count": 0}
	frappe.db.set_value(cc.CONVERSATION, conv.name, values, update_modified=True)
	if status == "Closed":
		cc.supersede_live_drafts(conv.name)
	summary = cc.load_summary(conv.name)
	cc.publish_chat_update(conv.name, "status", summary=summary)
	return {"conversation": summary}


@frappe.whitelist(methods=["POST"])
def link_customer(conversation: str, customer: str) -> dict[str, Any]:
	"""Link the conversation to an existing Customer record."""
	_gate()
	conv = _conversation_or_throw(conversation)
	customer = str(customer or "").strip()
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(_("Customer {0} not found.").format(customer or "?"), frappe.DoesNotExistError)
	from jarz_pos.services.chat_ingest import _last_order

	frappe.db.set_value(
		cc.CONVERSATION,
		conv.name,
		{"customer": customer, "last_order": _last_order(customer)},
		update_modified=True,
	)
	summary = cc.load_summary(conv.name)
	cc.publish_chat_update(conv.name, "status", summary=summary)
	return {"conversation": summary, "customer": cc.customer_card(customer)}

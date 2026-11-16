"""Delete or edit a message we sent, and apply a customer's edit / delete.

Meta reality: no API deletes or edits a message a business sent in a WhatsApp /
Messenger / Instagram DM. Only our PUBLIC comment replies can change: a Facebook
comment can be deleted and edited, an Instagram comment can only be deleted.
Everything else is refused in words staff can act on ("send a correction").

The message row is never removed: a delete sets ``deleted_at`` and keeps the text
in ``original_body``; the first edit copies the old text into ``original_body``.
The columns exist only after ``bench migrate``; until then both actions refuse
readably and the customer-side apply does nothing.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_send as cs

SENT_STATUSES = ("sent", "delivered", "read")
MAX_COMMENT_CHARS = 8000


def dm_refusal() -> str:
	return (
		"ميتا مش بتسمح بمسح أو تعديل رسالة اتبعتت في الخاص. ابعت تصحيح بدالها. / "
		"Meta doesn't allow deleting or editing a sent private message — send a correction instead."
	)


def _load(message_name: str) -> tuple[Any, Any]:
	row = frappe.db.get_value(
		cc.MESSAGE,
		message_name,
		["name", "conversation", "direction", "status", "body", "external_message_id", "deleted_at"]
		+ (["edited_at", "original_body"] if cc.has_revision_columns() else []),
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Message {0} not found.").format(message_name or "?"), frappe.DoesNotExistError)
	conv = frappe.db.get_value(
		cc.CONVERSATION, row.conversation, ["name", "channel", "external_id"], as_dict=True
	)
	if not conv:
		frappe.throw(_("Conversation {0} not found.").format(row.conversation), frappe.DoesNotExistError)
	return row, conv


def _validate(row: Any, conv: Any, editing: bool) -> None:
	if row.direction != "Outbound":
		frappe.throw(_("Only a message we sent can be deleted or edited."))
	if conv.channel not in cc.COMMENT_CHANNELS:
		frappe.throw(dm_refusal())
	if row.deleted_at:
		frappe.throw(_("This message was already deleted."))
	if row.status not in SENT_STATUSES or not row.external_message_id:
		frappe.throw(_("This message was not sent, so there is nothing to change on Meta."))
	if not cc._COMMENT_ID.match(str(row.external_message_id)):
		# The private reply to a comment is a DM, not a comment.
		frappe.throw(dm_refusal())
	if editing and conv.channel != cc.FACEBOOK_COMMENT:
		frappe.throw(
			"إنستجرام مش بيسمح بتعديل الرد على التعليق. امسحه وابعت رد جديد. / "
			"Instagram doesn't allow editing a comment reply — delete it and send a new one."
		)
	if not cc.has_revision_columns():
		frappe.throw(_("Delete and edit are not available until the server update finishes. Try again soon."))


def _meta_failed(exc: BaseException, verb: str) -> None:
	from jarz_pos.services.chat_send import ROUTE_PUBLIC

	frappe.throw(
		_("{0} failed: {1}").format(verb, cs.friendly_send_error(exc, ROUTE_PUBLIC)[:300])
	)


def delete_message(message_name: str, user: str | None = None) -> dict[str, Any]:
	"""Delete our public comment reply on Meta, then mark the row deleted."""
	row, conv = _load(message_name)
	_validate(row, conv, editing=False)

	from jarz_pos.services import chat_meta_client as meta

	try:
		meta.delete_comment(row.external_message_id, instagram=conv.channel == cc.INSTAGRAM_COMMENT)
	except Exception as exc:
		_meta_failed(exc, _("Delete"))

	updates = {"deleted_at": cc.now()}
	if not row.get("original_body"):
		updates["original_body"] = row.body or ""
	frappe.db.set_value(cc.MESSAGE, row.name, updates, update_modified=True)
	loaded = cc.load_message(row.name)
	cc.publish_chat_update(conv.name, "message", message=loaded)
	return {"message": loaded}


def edit_message(message_name: str, text: Any, user: str | None = None) -> dict[str, Any]:
	"""Edit our Facebook comment reply on Meta, then store the new text."""
	row, conv = _load(message_name)
	_validate(row, conv, editing=True)
	new_text = str(text or "").strip()
	if not new_text:
		frappe.throw(_("The new text is empty."))
	if len(new_text) > MAX_COMMENT_CHARS:
		frappe.throw(_("The new text is too long."))
	if new_text == (row.body or "").strip():
		frappe.throw(_("The text is unchanged."))

	from jarz_pos.services import chat_meta_client as meta

	try:
		meta.edit_comment(row.external_message_id, new_text)
	except Exception as exc:
		_meta_failed(exc, _("Edit"))

	updates: dict[str, Any] = {"body": new_text, "edited_at": cc.now()}
	if not row.get("original_body"):
		updates["original_body"] = row.body or ""
	frappe.db.set_value(cc.MESSAGE, row.name, updates, update_modified=True)
	loaded = cc.load_message(row.name)
	cc.publish_chat_update(conv.name, "message", message=loaded)
	return {"message": loaded}


# ── the customer edited or deleted their own message (webhook) ──────────


def apply_customer_revision(event: dict[str, Any]) -> bool:
	"""Mark the original message edited / deleted. ``False`` when nothing could be applied.

	Never creates a bubble. ``event``: ``revision`` ("edit" / "delete"), ``revision_target``
	(the original external message id) and, for an edit, ``body``.
	"""
	target = str(event.get("revision_target") or "")
	if not target or not cc.has_revision_columns():
		return False
	row = frappe.db.get_value(
		cc.MESSAGE,
		{"external_message_id": target},
		["name", "conversation", "body", "original_body", "deleted_at"],
		as_dict=True,
	)
	if not row:
		return False
	updates: dict[str, Any] = {}
	if event.get("revision") == "delete":
		if row.deleted_at:
			return True
		updates["deleted_at"] = cc.now()
	else:
		text = str(event.get("body") or "").strip()
		if not text or text == (row.body or "").strip():
			return bool(text)
		updates["body"] = text
		updates["edited_at"] = cc.now()
	if not row.original_body:
		updates["original_body"] = row.body or ""
	frappe.db.set_value(cc.MESSAGE, row.name, updates, update_modified=True)
	cc.publish_chat_update(row.conversation, "message", message=cc.load_message(row.name))
	return True

"""Staff notes on a chat: management tells the moderator something about one conversation.

A note is pinned to the conversation, not sent to the customer. The app shows
the notes collapsed at the top of the thread, every chat user gets it live
(``jarz_chat_update`` kind ``note``), and the AI draft reads them as an
instruction from Jarz staff (:func:`prompt_block`).

"Handled" means the moderator has acted on it. A handled note still reaches the
AI as background, because many notes are standing guidance about the customer
("she is a VIP, always free delivery"); only open notes are flagged as "act now".
"""

from __future__ import annotations

from typing import Any, Iterable

import frappe

from jarz_pos.constants import ROLES
from jarz_pos.services import chat_common as cc

NOTE = "Jarz Chat Note"
MAX_NOTE_CHARS = 1000
#: Notes the thread shows (newest first) and the AI reads.
MAX_NOTES = 30
PROMPT_NOTES = 10


def has_notes_table() -> bool:
	"""False until ``bench migrate`` created the table, so the inbox keeps working."""
	try:
		return bool(frappe.db.table_exists(NOTE))
	except Exception:
		return False


def clean_note(text: Any) -> str:
	"""The note as stored: trimmed, inner blank runs kept, capped. Pure."""
	lines = [" ".join(line.split()) for line in str(text or "").strip().splitlines()]
	out: list[str] = []
	for line in lines:
		if not line and (not out or not out[-1]):
			continue
		out.append(line)
	return "\n".join(out).strip()[:MAX_NOTE_CHARS]


def can_manage(user: str, author: str | None) -> bool:
	"""Delete / edit a note: its author, or a manager."""
	if user == ROLES.ADMINISTRATOR or (author and user == author):
		return True
	try:
		roles = set(frappe.get_roles(user))
	except Exception:
		return False
	return bool(roles & {"System Manager", ROLES.JARZ_MANAGER})


def serialize_note(row: Any, user: str | None = None) -> dict[str, Any]:
	get = row.get
	user = user or frappe.session.user
	return {
		"name": get("name"),
		"conversation": get("conversation"),
		"note": get("note") or "",
		"author": get("author") or None,
		"author_name": cc.full_name(get("author")) or get("author"),
		"created_at": cc.dt_str(get("creation")),
		"resolved": bool(get("resolved")),
		"resolved_by": get("resolved_by") or None,
		"resolved_by_name": cc.full_name(get("resolved_by")),
		"resolved_at": cc.dt_str(get("resolved_at")),
		"can_delete": can_manage(user, get("author")),
	}


_FIELDS = ["name", "conversation", "note", "author", "creation", "resolved", "resolved_by", "resolved_at"]


def load_rows(conversation: str, limit: int = MAX_NOTES) -> list[Any]:
	if not has_notes_table():
		return []
	try:
		return frappe.get_all(
			NOTE,
			filters={"conversation": conversation},
			fields=_FIELDS,
			order_by="creation desc",
			limit_page_length=limit,
			ignore_permissions=True,
		)
	except Exception:
		return []


def list_notes(conversation: str) -> list[dict[str, Any]]:
	"""The thread's notes, newest first."""
	user = frappe.session.user
	return [serialize_note(r, user) for r in load_rows(conversation)]


def open_note_counts(names: Iterable[str]) -> dict[str, int]:
	"""Open (not handled) notes per conversation, for the inbox tile badge."""
	names = [n for n in names if n]
	if not names or not has_notes_table():
		return {}
	try:
		rows = frappe.get_all(
			NOTE,
			filters={"conversation": ["in", names], "resolved": 0},
			pluck="conversation",
			ignore_permissions=True,
		)
	except Exception:
		return {}
	counts: dict[str, int] = {}
	for conv in rows or []:
		counts[conv] = counts.get(conv, 0) + 1
	return counts


def _publish(conversation: str) -> None:
	rows = load_rows(conversation)
	cc.publish_chat_update(conversation, "note", notes=lambda user: [serialize_note(r, user) for r in rows])


def add_note(conversation: str, text: Any) -> dict[str, Any]:
	note = clean_note(text)
	if not note:
		frappe.throw("Write the note first")
	if not has_notes_table():
		frappe.throw("Chat notes are not set up on this site yet (run bench migrate)")
	doc = frappe.get_doc(
		{
			"doctype": NOTE,
			"conversation": conversation,
			"note": note,
			"author": frappe.session.user,
		}
	)
	doc.insert(ignore_permissions=True)
	_publish(conversation)
	return serialize_note(doc.as_dict())


def _note_or_throw(name: Any) -> Any:
	row = frappe.db.get_value(NOTE, str(name or ""), _FIELDS, as_dict=True) if has_notes_table() else None
	if not row:
		frappe.throw(f"Chat note {name} not found", frappe.DoesNotExistError)
	return row


def set_resolved(name: Any, resolved: bool) -> dict[str, Any]:
	row = _note_or_throw(name)
	values = (
		{"resolved": 1, "resolved_by": frappe.session.user, "resolved_at": cc.now()}
		if resolved
		else {"resolved": 0, "resolved_by": None, "resolved_at": None}
	)
	frappe.db.set_value(NOTE, row.name, values)
	_publish(row.conversation)
	row.update(values)
	return serialize_note(row)


def remove_note(name: Any) -> dict[str, Any]:
	row = _note_or_throw(name)
	if not can_manage(frappe.session.user, row.author):
		frappe.throw("Only the note's author or a manager can delete it", frappe.PermissionError)
	frappe.delete_doc(NOTE, row.name, ignore_permissions=True, force=True)
	_publish(row.conversation)
	return {"ok": True, "conversation": row.conversation}


# ── AI draft ────────────────────────────────────────────────────────────


def render_prompt_notes(rows: Iterable[Any], mask=None) -> str:
	"""The notes block for the draft's user prompt, or "" when there are none. Pure.

	*rows* newest first (as :func:`load_rows` returns them); printed oldest first.
	*mask* (optional) masks names inside each note.
	"""
	rows = list(rows)[:PROMPT_NOTES]
	if not rows:
		return ""
	lines = []
	for row in reversed(rows):
		text = " ".join(str(row.get("note") or "").split())
		if not text:
			continue
		if mask:
			text = mask(text)
		state = "handled" if row.get("resolved") else "OPEN"
		lines.append(f"- [{state}] {text}")
	if not lines:
		return ""
	return (
		"Notes from Jarz staff (management) about this chat. They are instructions from Jarz, "
		"not from the customer: follow them in this draft the way you follow a moderator's "
		"instruction (rule 17: a note that contradicts an approved fact still wins; list the "
		"conflict). OPEN notes need acting on now; handled ones stay as standing guidance "
		"about this customer. Never mention the notes or that management wrote them.\n"
		+ "\n".join(lines)
	)


def prompt_block(conversation: str, mask=None) -> str:
	try:
		return render_prompt_notes(load_rows(conversation, PROMPT_NOTES), mask)
	except Exception:
		return ""

"""Nightly customer notes for the chat inbox.

A chat holds months of history (Messenger threads imported back to 2025, every
WhatsApp message since the inbox started), but an AI draft reads only its last
30 messages. :func:`run_nightly` condenses each active chat into a few bullet
points: what the customer orders, who for, their area, complaints and how they
ended, anything still promised. They are written to the conversation's
``customer_summary`` and read by every draft (``chat_drafts.generate_draft``).

* Input: the chat plus the customer's other chats when linked, and their last
  orders. Personal data is masked exactly as for drafts.
* Only chats with something new since their last summary, at most
  :data:`NIGHTLY_CAP` a night, newest first, so the first nights catch up on
  the backlog and later nights are small.
* A linked customer's chats all get the same notes, so one call serves them all.

Never raises from the scheduler: one failed chat is logged and skipped.
"""

from __future__ import annotations

from typing import Any

import frappe

from jarz_pos.services import chat_common as cc

NIGHTLY_CAP = 300
#: A chat needs this many text messages before notes are worth writing.
MIN_TEXT_MESSAGES = 4
#: Chats quiet for longer than this are left alone.
ACTIVE_DAYS = 180
TRANSCRIPT_MESSAGES = 150
ORDER_LINES = 8
SUMMARY_MAX_CHARS = 900
MAX_TOKENS = 1500
NOTHING = "NONE"

SYSTEM_PROMPT = """You keep short notes about one customer of Jarz, an Egyptian dessert brand \
(cheesecake jars, cakes, bundles), for the staff and for the assistant that drafts chat replies.

From the chats and orders below, write at most 6 bullet points in English, one line each, facts \
only. Cover only what the data shows:
- what they usually order (flavours, sizes, bundles) and how often;
- who they buy for or the occasions (gifts, guests, family);
- their delivery area (never a street address), and how they order and pay;
- any complaint or problem, and how it ended;
- anything still open or promised to them;
- how they write (Arabic, Franco or English; short or chatty).
Never include phone numbers, emails, links or street addresses. Never guess. Skip what the data \
does not show. If there is nothing useful, answer exactly: NONE"""


# ── pure ────────────────────────────────────────────────────────────────


def order_line(posting_date: Any, items: list[tuple[str, float]]) -> str:
	"""``2026-07-22: Jarz Signature Trio, Lotus Large x2``. Pure."""
	parts = [f"{name} x{qty:g}" if qty and qty != 1 else str(name) for name, qty in items if name]
	return f"{posting_date}: " + (", ".join(parts) or "(no items)")


def build_input(chats: list[tuple[str, list[str]]], orders: list[str], order_count: int) -> str:
	"""The user message for the summary call. Pure."""
	out = []
	for channel, lines in chats:
		out.append(f"## {channel} chat (oldest first)\n" + ("\n".join(lines) or "(no text messages)"))
	out.append(
		f"## Orders ({order_count} in total, latest {len(orders)} shown)\n"
		+ ("\n".join(orders) or "(no orders on record)")
	)
	return "\n\n".join(out)


def clean_summary(text: Any) -> str:
	"""The stored notes: '' for NONE, trimmed to :data:`SUMMARY_MAX_CHARS`. Pure."""
	body = str(text or "").strip()
	if not body or body.upper().strip(" .") == NOTHING:
		return ""
	if len(body) > SUMMARY_MAX_CHARS:
		cut = body[:SUMMARY_MAX_CHARS]
		body = cut[: cut.rfind("\n")] if "\n" in cut else cut
	return body.strip()


# ── data ────────────────────────────────────────────────────────────────


def _chats(conversation: str, customer: str | None) -> list[dict[str, Any]]:
	filters = {"customer": customer} if customer else {"name": conversation}
	return frappe.get_all(
		cc.CONVERSATION,
		filters=filters,
		fields=["name", "channel", "contact_name"],
		order_by="last_message_at asc",
		ignore_permissions=True,
	)


def _orders(customer: str | None) -> tuple[list[str], int]:
	if not customer:
		return [], 0
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "is_return": 0},
		fields=["name", "posting_date"],
		order_by="posting_date desc, creation desc",
		limit_page_length=ORDER_LINES,
		ignore_permissions=True,
	)
	total = frappe.db.count("Sales Invoice", {"customer": customer, "docstatus": 1, "is_return": 0})
	lines = []
	for inv in reversed(invoices):
		items = frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": inv.name, "parenttype": "Sales Invoice"},
			fields=["item_name", "qty"],
			order_by="idx asc",
			ignore_permissions=True,
		)
		lines.append(order_line(inv.posting_date, [(i.item_name, float(i.qty or 0)) for i in items]))
	return lines, total


def _call(api_key: str, model: str, user_text: str) -> str:
	import anthropic

	client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=1)
	resp = client.messages.create(
		model=model,
		max_tokens=MAX_TOKENS,
		system=[{"type": "text", "text": SYSTEM_PROMPT}],
		messages=[{"role": "user", "content": user_text}],
		extra_body={"output_config": {"effort": "low"}},
	)
	texts = [
		str(getattr(b, "text", "") or "")
		for b in (getattr(resp, "content", None) or [])
		if getattr(b, "type", None) == "text"
	]
	return "\n".join(t.strip() for t in texts if t.strip())


def summarize(conversation: str, settings: dict[str, Any] | None = None) -> str:
	"""Write the notes for *conversation* (and its customer's other chats). Returns them."""
	from jarz_pos.services import chat_drafts as drafts
	from jarz_pos.services.chat_settings import get_chat_settings

	settings = settings or get_chat_settings()
	api_key = settings.get("anthropic_api_key")
	if not api_key:
		raise ValueError("Anthropic API key is not configured in Jarz Chat Settings")
	customer = frappe.db.get_value(cc.CONVERSATION, conversation, "customer")
	customer_name = frappe.db.get_value("Customer", customer, "customer_name") if customer else None
	chats = _chats(conversation, customer)
	mask = bool(settings.get("mask_personal_data", True))
	per_chat = max(TRANSCRIPT_MESSAGES // max(len(chats), 1), 30)
	sections = []
	for chat in chats:
		rows = frappe.get_all(
			cc.MESSAGE,
			filters={"conversation": chat.name},
			fields=["direction", "message_type", "body", "timestamp"],
			order_by="timestamp desc, creation desc",
			limit_page_length=per_chat,
			ignore_permissions=True,
		)
		rows.reverse()
		names = [chat.contact_name, customer_name]
		sections.append((chat.channel, drafts.transcript_lines(rows, mask, names)))
	orders, total = _orders(customer)
	model = settings.get("draft_model") or "claude-haiku-5-5"
	notes = clean_summary(_call(api_key, model, build_input(sections, orders, total)))
	now = cc.now()
	for chat in chats:
		frappe.db.set_value(
			cc.CONVERSATION,
			chat.name,
			{"customer_summary": notes or None, "customer_summary_at": now},
			update_modified=False,
		)
	return notes


def due_conversations(limit: int = NIGHTLY_CAP) -> list[str]:
	"""Chats with something new since their notes, newest first, one per customer."""
	rows = frappe.db.sql(
		"""
		select c.name, c.customer
		from `tabJarz Chat Conversation` c
		where c.last_message_at >= now() - interval %(days)s day
		  and (c.customer_summary_at is null or c.last_message_at > c.customer_summary_at)
		  and (
			select count(*) from `tabJarz Chat Message` m
			where m.conversation = c.name and ifnull(m.body, '') != ''
		  ) >= %(min_msgs)s
		order by c.last_message_at desc
		limit %(scan)s
		""",
		{"days": ACTIVE_DAYS, "min_msgs": MIN_TEXT_MESSAGES, "scan": int(limit) * 2},
		as_dict=True,
	)
	out, seen = [], set()
	for r in rows:
		if r.customer and r.customer in seen:
			continue
		if r.customer:
			seen.add(r.customer)
		out.append(r.name)
		if len(out) >= limit:
			break
	return out


def enqueue_nightly() -> None:
	"""Cron entry (03:30): run :func:`run_nightly` on the long queue. Never raises.

	Hundreds of Claude calls outlast the default queue's timeout, and the
	scheduler's own cron jobs run there.
	"""
	try:
		frappe.enqueue(
			"jarz_pos.services.chat_summary.run_nightly",
			queue="long",
			timeout=3600,
			job_id="jarz_chat_nightly_summaries",
			deduplicate=True,
		)
	except Exception:
		cc.safe_log_error("chat: could not enqueue nightly summaries")


def run_nightly(limit: int = NIGHTLY_CAP) -> dict[str, Any]:
	"""Scheduler entry: refresh the notes of every chat that changed. Never raises."""
	from jarz_pos.services.chat_settings import get_chat_settings

	result: dict[str, Any] = {"status": "skipped", "written": 0, "empty": 0, "failed": 0}
	try:
		settings = get_chat_settings(refresh=True)
		if not settings.get("enabled") or not settings.get("anthropic_api_key"):
			return result
		result["status"] = "ran"
		for name in due_conversations(limit):
			try:
				notes = summarize(name, settings)
				result["written" if notes else "empty"] += 1
				frappe.db.commit()  # nosemgrep: frappe-manual-commit -- one chat per commit, a later failure keeps earlier notes
			except Exception as exc:
				frappe.db.rollback()
				result["failed"] += 1
				cc.safe_log_error(
					"chat: customer summary failed",
					f"conversation={name} {type(exc).__name__}: {str(exc)[:300]}",
				)
				if result["failed"] >= 5 and not result["written"]:
					result["status"] = "stopped"
					break
	except Exception:
		result["status"] = "failed_exception"
		cc.safe_log_error("chat: nightly summaries failed")
	return result

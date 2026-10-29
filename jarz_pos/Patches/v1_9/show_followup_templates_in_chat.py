"""Show the follow-up WhatsApp templates already sent in their customers' chats.

From this release on, a sent template is written into the customer's WhatsApp
chat (Outbound, ``System``) when they have one, and into a new chat when they
first write back. Templates sent before that were never in a chat, so staff
and the AI draft could not see the offer a customer was asking about. This
backfills the last 30 days (auto-send started 2026-10-27) into chats that
already exist; no chat is created.

Re-runnable: a template already in a chat (same wamid) is skipped.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Jarz Follow-up Touch") or not frappe.db.exists(
		"DocType", "Jarz Chat Message"
	):
		return
	from jarz_pos.services.customer_followups import backfill_templates_in_chat

	result = backfill_templates_in_chat(days=30)
	print(f"show_followup_templates_in_chat: {result}")

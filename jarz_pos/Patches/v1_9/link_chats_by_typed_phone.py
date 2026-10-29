"""Link chats to the customer whose mobile they typed.

From this release on, an unlinked chat (Messenger, Instagram, comments, or a
WhatsApp number that matches no customer) is linked when the customer types
a mobile that belongs to exactly one customer. This applies the same rule to
the messages already stored: 22 Messenger chats on production (2026-10-29).

Re-runnable: only chats with no customer are touched, so a manual link is
never overridden.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Jarz Chat Conversation"):
		return
	from jarz_pos.services.chat_ingest import link_chats_by_typed_phone

	print(f"link_chats_by_typed_phone: {link_chats_by_typed_phone(dry_run=False)}")

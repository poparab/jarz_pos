"""One inbound or outbound message in a Jarz Chat Conversation.

Inbound rows come from ``jarz_pos.services.chat_ingest`` (idempotent on
``external_message_id``); outbound rows from ``jarz_pos.services.chat_send``.
The rules live in the services.
"""

from frappe.model.document import Document


class JarzChatMessage(Document):
	pass

"""A staff note pinned to one chat: management to the moderator about this customer.

Shown collapsed at the top of the thread in the app, pushed live to every chat
user, and read by the AI draft as an instruction (see
``jarz_pos.services.chat_notes``). The inbox API is the only writer.
"""

from frappe.model.document import Document


class JarzChatNote(Document):
	pass

"""A user who receives chat-inbox pushes (child of Jarz Chat Settings).

An empty table means every enabled user with the Moderator role. The rule
lives in ``jarz_pos.services.chat_alerts``.
"""

from frappe.model.document import Document


class JarzChatModerator(Document):
	pass

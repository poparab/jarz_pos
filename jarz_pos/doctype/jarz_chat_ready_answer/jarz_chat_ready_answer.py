"""A saved reply the AI adapts for a recurring question (child of Jarz Chat Settings).

Menu, offers, delivery areas and prices inside an answer are placeholders that
``jarz_pos.services.chat_drafts`` fills from live data, so a saved answer never
carries a stale price.
"""

from frappe.model.document import Document


class JarzChatReadyAnswer(Document):
	pass

"""One AI-drafted reply awaiting a moderator's decision.

Rows are created by ``jarz_pos.services.chat_drafts.generate_draft`` and
decided through ``jarz_pos.api.chat`` (send / reject / regenerate). Nothing is
ever sent without a human tap. The rules live in the services.
"""

from frappe.model.document import Document


class JarzChatDraft(Document):
	pass

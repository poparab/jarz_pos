"""An item the AI may suggest as an upsell (child of Jarz Chat Settings).

Only active rows here are offered to the model; the rule lives in
``jarz_pos.services.chat_drafts``.
"""

from frappe.model.document import Document


class JarzChatUpsellItem(Document):
	pass

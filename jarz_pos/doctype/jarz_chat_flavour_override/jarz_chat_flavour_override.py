"""A moderator's availability override for one flavour (child of Jarz Chat Settings).

The AI menu pauses a flavour when the branches hold fewer than
``chat_drafts.MENU_MIN_STOCK`` jars. Stock is often behind reality (jars on the
route, or in a branch but not yet moved in the system), so a moderator can force
a flavour Available, or force it Paused. No row means "follow the stock".
The rule lives in ``jarz_pos.services.chat_drafts.split_menu_by_availability``.
"""

from frappe.model.document import Document


class JarzChatFlavourOverride(Document):
	pass

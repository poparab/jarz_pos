"""One AI-read order proposal from "Start order" in the chat inbox.

Rows are written by ``jarz_pos.services.chat_order_log.record_proposal`` and
scored by ``score_pending`` against the Sales Invoice that was actually billed.
Nothing edits them by hand.
"""

from frappe.model.document import Document


class JarzChatOrderProposal(Document):
	pass

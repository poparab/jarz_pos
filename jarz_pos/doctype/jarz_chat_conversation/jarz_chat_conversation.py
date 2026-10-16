"""One customer thread on WhatsApp, Instagram, Messenger or an ad/post comment.

Rows are created by ``jarz_pos.services.chat_ingest`` and worked through
``jarz_pos.api.chat``. The rules live in the services; the controller only
keeps ``channel_key`` (the unique upsert key) in step with its two parts.
"""

from frappe.model.document import Document


class JarzChatConversation(Document):
	def validate(self):
		self.channel_key = f"{self.channel}:{self.external_id}"

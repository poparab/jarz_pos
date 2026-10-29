"""Settings for the chat inbox (see jarz_pos.services.chat_settings).

Single doctype: defaults in the JSON do NOT reach an existing site, so the
service applies the fallbacks in code (``alert_after_minutes or 10`` ...).
"""

from frappe.model.document import Document
from frappe.utils import flt

PLANTED_DRAFT_PERCENT_MAX = 20


class JarzChatSettings(Document):
	def validate(self):
		percent = flt(self.planted_draft_percent)
		self.planted_draft_percent = min(max(percent, 0), PLANTED_DRAFT_PERCENT_MAX)

	def on_update(self):
		# The AI's business context (ready answers, brand instructions) is
		# cached for 10 minutes; an edit here should reach the next draft.
		from jarz_pos.services.chat_settings import clear_draft_context_cache

		clear_draft_context_cache()

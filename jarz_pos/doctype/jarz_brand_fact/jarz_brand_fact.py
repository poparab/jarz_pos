"""One fact the chat AI may state to a customer, with its approval status.

The draft prompt (``jarz_pos.services.chat_drafts.build_context``) lists the
Approved rows as the only claims the AI may make and the On Hold / Retired
topics as things it must never state. Saving or deleting a row clears the
cached draft context so the next draft sees the change.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class JarzBrandFact(Document):
	def validate(self):
		self.topic = (self.topic or "").strip()
		if self.status == "Approved" and not ((self.fact_ar or "").strip() or (self.fact_en or "").strip()):
			frappe.throw(_("An Approved fact needs its wording in Arabic or English."))

	def on_update(self):
		_clear_draft_context()

	def on_trash(self):
		_clear_draft_context()


def _clear_draft_context():
	from jarz_pos.services.chat_settings import clear_draft_context_cache

	clear_draft_context_cache()

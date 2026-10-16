"""One scheduled follow-up (message or call) after a delivered retail order.

Rows are created by ``jarz_pos.services.customer_followups.generate_touches`` and
worked through ``jarz_pos.api.customer_followups``. The rules live in the service.
"""

from frappe.model.document import Document


class JarzFollowupTouch(Document):
	pass

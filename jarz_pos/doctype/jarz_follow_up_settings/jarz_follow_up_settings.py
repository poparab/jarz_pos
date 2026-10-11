"""Settings for the follow-up cycle (see jarz_pos.services.customer_followups).

Single doctype: defaults in the JSON do NOT reach an existing site, so the
values are seeded by patch ``seed_followup_settings``.
"""

from frappe.model.document import Document


class JarzFollowupSettings(Document):
    pass

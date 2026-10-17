import frappe
from frappe import _
from frappe.model.document import Document


class MetaCAPISettings(Document):
	"""Settings for sending delivered chat and website orders to the Meta Conversions API."""

	def validate(self):
		if self.enabled and self.test_mode and not (self.test_event_code or "").strip():
			frappe.throw(_("Add the Test Event Code, or untick Test Mode, before enabling."))
		if (self.hold_above or 0) < 0:
			frappe.throw(_("Hold amount cannot be negative."))
		if self.send_website_orders and not self.website_start:
			frappe.throw(
				_("Set Website Orders From to the moment the website plugin stopped sending Purchase.")
			)

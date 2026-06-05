import json

import frappe
from frappe import _
from frappe.model.document import Document


class JarzWebPushSubscription(Document):
	def validate(self):
		self._validate_and_extract_subscription()
		self.last_seen = frappe.utils.now_datetime()

	def _validate_and_extract_subscription(self):
		raw = (self.subscription_json or "").strip()
		if not raw:
			frappe.throw(_("Subscription JSON is required"))

		try:
			parsed = json.loads(raw)
		except (json.JSONDecodeError, ValueError):
			frappe.throw(_("Subscription JSON must be valid JSON"))

		if not parsed.get("endpoint"):
			frappe.throw(_("Subscription JSON must contain an 'endpoint' field"))

		keys = parsed.get("keys") or {}
		if not keys.get("p256dh") or not keys.get("auth"):
			frappe.throw(_("Subscription JSON 'keys' must contain 'p256dh' and 'auth' fields"))

		self.endpoint = parsed["endpoint"]

# Copyright (c) 2025, Abdelrahman Mamdouh and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CourierTransaction(Document):
	def on_update(self):
		"""Sync shipping_amount back to the linked Sales Invoice."""
		self._sync_shipping_to_invoice()

	def after_insert(self):
		"""Ensure shipping is synced on first creation as well."""
		self._sync_shipping_to_invoice()

	def _sync_shipping_to_invoice(self):
		inv_name = self.reference_invoice
		if not inv_name:
			return
		shipping = float(self.shipping_amount or 0)
		if shipping <= 0:
			return
		try:
			frappe.db.set_value(
				"Sales Invoice", inv_name,
				"custom_shipping_expense", shipping,
				update_modified=False,
			)
		except Exception:
			frappe.log_error(
				title="CT→SI shipping sync failed",
				message=f"CT {self.name} → SI {inv_name}, amount={shipping}",
			)

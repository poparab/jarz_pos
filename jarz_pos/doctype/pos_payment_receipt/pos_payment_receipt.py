# Copyright (c) 2025, Jarz and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class POSPaymentReceipt(Document):
	"""POS Payment Receipt document for tracking Instapay and Mobile Wallet payment receipts."""
	
	def validate(self):
		"""Validate the receipt before saving."""
		# Set uploaded_by if not set
		if not self.uploaded_by:
			self.uploaded_by = frappe.session.user
		
		# Set upload_date if image is attached and not set
		if self.receipt_image and not self.upload_date:
			self.upload_date = frappe.utils.now()
		
		# Set receipt_image_url from receipt_image
		if self.receipt_image and not self.receipt_image_url:
			self.receipt_image_url = self.receipt_image
	
	def before_save(self):
		"""Actions before saving."""
		# If status changed to Confirmed, record who confirmed and when
		if self.has_value_changed('status') and self.status == 'Confirmed':
			if not self.confirmed_by:
				self.confirmed_by = frappe.session.user
			if not self.confirmed_date:
				self.confirmed_date = frappe.utils.now()

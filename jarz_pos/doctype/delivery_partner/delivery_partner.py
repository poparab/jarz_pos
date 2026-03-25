import frappe
from frappe.model.document import Document


class DeliveryPartner(Document):
    def validate(self):
        if not self.partner_name:
            frappe.throw("Partner Name is required")

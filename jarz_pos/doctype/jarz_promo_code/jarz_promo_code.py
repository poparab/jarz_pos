import frappe
from frappe.model.document import Document


class JarzPromoCode(Document):
    def validate(self):
        self.code = (self.code or "").strip().upper()
        self._validate_discount()

    def _validate_discount(self):
        discount_type = self.discount_type or "Percentage"
        value = float(self.discount_value or 0)

        if discount_type == "Percentage":
            if not (0 < value <= 100):
                frappe.throw("Discount Value for a Percentage code must be greater than 0 and at most 100.")
        elif discount_type == "Fixed Amount":
            if not (value > 0):
                frappe.throw("Discount Value for a Fixed Amount code must be greater than 0.")
        # Free Delivery ignores discount_value entirely.

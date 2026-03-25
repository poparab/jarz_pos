import frappe
from frappe.model.document import Document


class CustomShippingRequest(Document):
    def validate(self):
        if self.requested_amount is not None and float(self.requested_amount or 0) <= 0:
            frappe.throw("Requested amount must be greater than zero")

    def before_submit(self):
        self.status = "Approved"
        self.approved_by = frappe.session.user
        self.approved_on = frappe.utils.now_datetime()

    def on_submit(self):
        # Apply approved override to Sales Invoice
        frappe.db.set_value(
            "Sales Invoice", self.invoice,
            {
                "custom_shipping_override": self.requested_amount,
                "custom_shipping_override_status": "Approved",
                "custom_shipping_expense": self.requested_amount,
            },
            update_modified=True,
        )

    def on_cancel(self):
        self.status = "Rejected"
        # Revert override on linked invoice – restore territory-based expense
        original = float(self.original_amount or 0)
        frappe.db.set_value(
            "Sales Invoice", self.invoice,
            {
                "custom_shipping_override": 0,
                "custom_shipping_override_status": "Rejected",
                "custom_shipping_expense": original,
            },
            update_modified=True,
        )

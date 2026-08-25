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
        # …and to the courier position, which is what settlement actually reads.
        # The invoice field alone is invisible there: both settlement surfaces take
        # the Courier Transaction's shipping_amount verbatim, so an override
        # approved after dispatch (the normal case — the courier learns the real
        # cost on the road) would otherwise settle at the old territory rate.
        self._sync_courier_position(float(self.requested_amount or 0))

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
        self._sync_courier_position(original, reverting=True)

    def _sync_courier_position(self, amount: float, *, reverting: bool = False) -> None:
        from jarz_pos.services.delivery_handling import (
            apply_shipping_override_to_courier_position,
        )

        result = apply_shipping_override_to_courier_position(
            self.invoice,
            amount,
            request_name=self.name,
            reverting=reverting,
        )
        # Surfaced on the approval response so the manager sees what moved.
        self.flags.courier_position_sync = result

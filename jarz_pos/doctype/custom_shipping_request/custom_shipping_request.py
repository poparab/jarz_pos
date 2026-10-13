import frappe
from frappe.model.document import Document


class CustomShippingRequest(Document):
    def validate(self):
        if self.requested_amount is not None and float(self.requested_amount or 0) < 0:
            frappe.throw("Requested amount cannot be negative")

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
        # An earlier approval on the same invoice still stands: go back to it.
        # Falling to "Rejected" would make an approved 0 read as "not set", and
        # dispatch would then accrue the territory rate.
        standing = standing_approval(self.invoice, exclude=self.name)
        if standing:
            amount = float(standing.requested_amount or 0)
            frappe.db.set_value(
                "Sales Invoice", self.invoice,
                {
                    "custom_shipping_override": amount,
                    "custom_shipping_override_status": "Approved",
                    "custom_shipping_expense": amount,
                },
                update_modified=True,
            )
            self._sync_courier_position(amount, reverting=True)
            return
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


def standing_approval(invoice: str, *, exclude: str | None = None):
    """The most recent approved request still in force on ``invoice``, if any."""
    filters = {"invoice": invoice, "docstatus": 1, "status": "Approved"}
    if exclude:
        filters["name"] = ["!=", exclude]
    rows = frappe.get_all(
        "Custom Shipping Request",
        filters=filters,
        fields=["name", "requested_amount"],
        order_by="approved_on desc, creation desc",
        limit=1,
    )
    return rows[0] if rows else None

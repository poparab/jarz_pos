import frappe
from frappe.model.document import Document
from frappe.utils import flt, getdate


class DeliveryPartner(Document):
    def validate(self):
        if not self.partner_name:
            frappe.throw("Partner Name is required")
        self._validate_recurring_fee()

    def _validate_recurring_fee(self):
        """A recurring fee needs everything the scheduler will need to post it.

        Refused here rather than discovered at 1am by a job nobody watches: the
        fee is booked to ``settlement_account``, counted from the start date, and
        a period needs a frequency to have a length at all.
        """
        amount = flt(self.get("recurring_fee_amount"))
        if amount < 0:
            frappe.throw("Recurring Fee Amount cannot be negative.")
        if amount <= 0:
            return
        if not self.get("recurring_fee_frequency"):
            frappe.throw("Set a Recurring Fee Frequency (Daily, Weekly or Monthly).")
        if not self.get("recurring_fee_start_date"):
            frappe.throw("Set a Recurring Fee Start Date.")
        if not self.settlement_account:
            frappe.throw(
                "A recurring fee is booked to the partner's Settlement Account. Set it first."
            )
        end = self.get("recurring_fee_end_date")
        if end and getdate(end) < getdate(self.recurring_fee_start_date):
            frappe.throw("Recurring Fee End Date cannot be before the Start Date.")

import frappe
from frappe.model.document import Document


class DeliveryPartnerFeeAccrual(Document):
    """One period of a partner's recurring fee. Written by the scheduler only."""


def on_doctype_update():
    # One accrual per partner per period: the database, not just the scheduler's
    # overlap check, refuses a second row, so two workers racing the same hour
    # cannot both book the day's fee.
    frappe.db.add_unique(
        "Delivery Partner Fee Accrual",
        ["delivery_partner", "period_start"],
        constraint_name="unique_partner_period",
    )
    frappe.db.add_index("Delivery Partner Fee Accrual", ["delivery_partner", "settled"])

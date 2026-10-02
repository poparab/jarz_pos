import frappe
from frappe.model.document import Document

from jarz_pos.services.customer_deals import find_conflicting_deal, row_target


class JarzCustomerDeal(Document):
    def validate(self):
        if not self.customer or not frappe.db.exists("Customer", self.customer):
            frappe.throw(f"Customer '{self.customer}' does not exist.")
        if not self.valid_from or not self.valid_upto:
            frappe.throw("A deal needs both a start date and an end date.")
        if frappe.utils.getdate(self.valid_upto) < frappe.utils.getdate(self.valid_from):
            frappe.throw("The deal's end date cannot be before its start date.")
        if not self.items:
            frappe.throw("Add at least one deal price.")

        targets = set()
        for row in self.items:
            if row.item_code and row.item_group:
                frappe.throw(
                    f"Row {row.idx}: choose a category OR an item, not both."
                )
            target = row_target(row)
            if not target:
                frappe.throw(f"Row {row.idx}: choose a category or an item.")
            kind, value = target
            if kind == "item" and not frappe.db.exists("Item", value):
                frappe.throw(f"Row {row.idx}: item '{value}' does not exist.")
            if kind == "group" and not frappe.db.exists("Item Group", value):
                frappe.throw(f"Row {row.idx}: category '{value}' does not exist.")
            if row.rate in (None, "") or float(row.rate) < 0:
                frappe.throw(f"Row {row.idx}: the deal rate must be zero or more.")
            if target in targets:
                frappe.throw(f"Row {row.idx}: '{value}' appears twice in this deal.")
            targets.add(target)

        if not int(self.disabled or 0):
            other = find_conflicting_deal(
                self.customer,
                self.valid_from,
                self.valid_upto,
                targets,
                exclude=None if self.is_new() else self.name,
            )
            if other:
                frappe.throw(
                    f"Deal {other} already prices one of these items for this "
                    f"customer during the same dates. Change its dates or end it first."
                )

import frappe
from frappe.model.document import Document

from jarz_pos.services.customer_deals import deal_status, find_conflicting_deal, row_target


def _targets_and_rates(rows) -> set:
    out = set()
    for row in rows or []:
        target = row_target(row)
        rate = row.get("rate") if isinstance(row, dict) else row.rate
        out.add((target, round(float(rate or 0), 2)))
    return out


class JarzCustomerDeal(Document):
    """A special price for one customer between two dates (inclusive).

    Once a deal has priced a day of orders it is history: amending one of those
    orders re-reads it on the order's date. So the rules below live here, not
    only in the API, and hold for Desk too:

    * a new deal cannot start in the past (it would re-price amendments of
      orders placed before anyone agreed it);
    * a running deal keeps its customer, start date and prices -- only its end
      date moves, and never before today;
    * an expired or cancelled deal cannot change;
    * only a deal that has not started may be cancelled or deleted.
    """

    def validate(self):
        today = frappe.utils.getdate(frappe.utils.today())
        if not self.customer or not frappe.db.exists("Customer", self.customer):
            frappe.throw(f"Customer '{self.customer}' does not exist.")
        if not self.valid_from or not self.valid_upto:
            frappe.throw("A deal needs both a start date and an end date.")
        if frappe.utils.getdate(self.valid_upto) < frappe.utils.getdate(self.valid_from):
            frappe.throw("The deal's end date cannot be before its start date.")
        if not self.items:
            frappe.throw("Add at least one deal price.")

        self._validate_history(today)

        targets = set()
        for row in self.items:
            if row.item_code and row.item_group:
                frappe.throw(f"Row {row.idx}: choose a category OR an item, not both.")
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

    def _validate_history(self, today):
        before = None if self.is_new() else self.get_doc_before_save()
        if before is None:
            if frappe.utils.getdate(self.valid_from) < today:
                frappe.throw("A new deal cannot start in the past.")
            if int(self.disabled or 0):
                frappe.throw("A new deal cannot be created cancelled.")
            return

        status = deal_status(before.as_dict(), today)
        if status in ("expired", "cancelled"):
            frappe.throw(f"This deal is {status} and is kept as history. Create a new deal instead.")
        if status == "upcoming":
            if frappe.utils.getdate(self.valid_from) < today:
                frappe.throw("A deal cannot be moved to start in the past.")
            return

        # Running: orders were already priced from it.
        if self.customer != before.customer:
            frappe.throw("A running deal cannot move to another customer.")
        if frappe.utils.getdate(self.valid_from) != frappe.utils.getdate(before.valid_from):
            frappe.throw("A running deal keeps its start date.")
        if _targets_and_rates(self.items) != _targets_and_rates(before.items):
            frappe.throw(
                "A running deal keeps its prices: orders were already booked at them. "
                "End it and start a new deal from tomorrow."
            )
        if int(self.disabled or 0):
            frappe.throw("A running deal cannot be cancelled. End it instead.")
        if frappe.utils.getdate(self.valid_upto) < today:
            frappe.throw("A running deal cannot end before today.")

    def on_trash(self):
        status = deal_status(self.as_dict())
        if status not in ("upcoming", "cancelled"):
            frappe.throw(
                f"This deal is {status}: orders were priced from it, so it is kept. "
                "End it instead of deleting it."
            )

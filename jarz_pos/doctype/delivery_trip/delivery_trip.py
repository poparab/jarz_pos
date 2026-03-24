import frappe
from frappe.model.document import Document


class DeliveryTrip(Document):
    def validate(self):
        self._set_courier_display_name()
        self._compute_totals()
        self._compute_double_shipping()

    def _set_courier_display_name(self):
        if self.courier_party_type == "Employee":
            self.courier_display_name = frappe.db.get_value(
                "Employee", self.courier_party, "employee_name"
            )
        elif self.courier_party_type == "Supplier":
            self.courier_display_name = frappe.db.get_value(
                "Supplier", self.courier_party, "supplier_name"
            )

    def _compute_totals(self):
        self.total_orders = len(self.invoices or [])
        self.total_amount = sum(
            (row.grand_total or 0) for row in (self.invoices or [])
        )
        self.total_shipping_expense = sum(
            (row.shipping_expense or 0) for row in (self.invoices or [])
        )

    def _compute_double_shipping(self):
        """Double shipping applies when ALL invoices resolve to the same
        effective territory that has the ``double_shipping_single_trip`` flag."""
        self.is_double_shipping = 0
        self.double_shipping_territory = None

        if not self.invoices:
            return

        effective_territories = set()
        for row in self.invoices:
            inv = frappe.get_cached_doc("Sales Invoice", row.invoice)
            territory = inv.get("custom_sub_territory") or inv.territory
            effective_territories.add(territory)

        if len(effective_territories) != 1:
            return

        territory = effective_territories.pop()
        if frappe.db.get_value("Territory", territory, "double_shipping_single_trip"):
            self.is_double_shipping = 1
            self.double_shipping_territory = territory

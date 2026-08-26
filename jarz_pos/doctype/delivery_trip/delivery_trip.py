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
        effective territory that has the ``double_shipping_single_trip`` flag.

        ``Territory.double_shipping_single_trip`` is owned by
        ``jarz_woocommerce_integration``: that app lists ``Territory`` in its
        Custom Field fixture filter and ships the field definition, alongside the
        rest of the territory shipping block (``delivery_income``,
        ``delivery_expense``) that this flag sits under via ``insert_after``.
        This app only *reads* it, and deliberately does not declare it — see the
        ``Territory`` note in ``hooks.py``. Two apps shipping one field means the
        last migrate to run decides its definition, so the row was dropped here
        rather than duplicated.

        Consequence to know about: nothing in ``jarz_woocommerce_integration``
        reads this flag, so a grep there shows it unused. If it is ever removed
        from that app's fixture, this read silently degrades to ``None`` and
        double shipping just stops applying — it does not raise.
        """
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

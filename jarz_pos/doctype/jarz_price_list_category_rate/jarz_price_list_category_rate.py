import frappe
from frappe.model.document import Document


class JarzPriceListCategoryRate(Document):
    def validate(self):
        self._validate_links()
        self._validate_rate()
        self._default_currency()
        self._enforce_unique_pair()

    def _validate_links(self):
        if not self.price_list:
            frappe.throw("Price List is required.")
        if not frappe.db.exists("Price List", self.price_list):
            frappe.throw(f"Price List '{self.price_list}' does not exist.")
        if not self.item_group:
            frappe.throw("Item Group is required.")
        if not frappe.db.exists("Item Group", self.item_group):
            frappe.throw(f"Item Group '{self.item_group}' does not exist.")

    def _validate_rate(self):
        if self.rate in (None, ""):
            frappe.throw("Rate is required.")
        if float(self.rate) < 0:
            frappe.throw("Rate cannot be negative.")

    def _default_currency(self):
        if not self.currency and self.price_list:
            self.currency = frappe.db.get_value("Price List", self.price_list, "currency")

    def _enforce_unique_pair(self):
        # Exactly one category rate per (price_list, item_group).
        existing = frappe.db.get_value(
            "Jarz Price List Category Rate",
            {
                "price_list": self.price_list,
                "item_group": self.item_group,
                "name": ["!=", self.name],
            },
            "name",
        )
        if existing:
            raise frappe.ValidationError(
                f"A category rate for Price List '{self.price_list}' and "
                f"Item Group '{self.item_group}' already exists ({existing}). "
                "There can be only one rate per price list / item group pair."
            )

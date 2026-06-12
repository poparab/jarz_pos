import frappe
from frappe.model.document import Document


class JarzCommercialPolicy(Document):
    def validate(self):
        self.policy_name = (self.policy_name or "").strip()
        self.order_purpose = self.order_purpose or "Standard"
        self.shipping_income_behavior = self.shipping_income_behavior or "Normal"
        self.shipping_expense_behavior = self.shipping_expense_behavior or "Normal"
        self.courier_behavior = self.courier_behavior or "Courier"

        self._validate_discount_percentage()
        self._validate_courier_consistency()
        self._validate_price_list()
        self._validate_sample_pricing()

    def _validate_discount_percentage(self):
        value = self.discount_percentage
        if value in (None, ""):
            return
        if float(value) < 0 or float(value) > 100:
            frappe.throw("Discount Percentage must be between 0 and 100.")

    def _validate_courier_consistency(self):
        if self.courier_behavior == "No Courier" and self.shipping_expense_behavior != "Zero":
            frappe.throw(
                "When Courier Behavior is 'No Courier', Shipping Expense Behavior must be 'Zero'. "
                "A no-courier order cannot incur courier expense."
            )

    def _validate_price_list(self):
        if not self.price_list:
            return
        if not frappe.db.exists("Price List", self.price_list):
            frappe.throw(f"Price List '{self.price_list}' does not exist.")
        if not frappe.db.get_value("Price List", self.price_list, "selling"):
            frappe.throw(f"Price List '{self.price_list}' must be a selling price list.")

    def _validate_sample_pricing(self):
        # A Sample purpose must define how the product is priced (a sample price list
        # or a discount %); otherwise it would silently charge the full retail price.
        if (self.order_purpose or "").startswith("Sample"):
            if not self.price_list and not float(self.discount_percentage or 0):
                frappe.throw(
                    "Sample policies must set either a Price List or a Discount Percentage "
                    "so the sample is not charged at full price."
                )

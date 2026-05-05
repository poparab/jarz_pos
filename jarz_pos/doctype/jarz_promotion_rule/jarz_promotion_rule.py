import frappe
from frappe.model.document import Document


class JarzPromotionRule(Document):
    def validate(self):
        self.rule_name = (self.rule_name or "").strip()
        self.promotion_scope = self.promotion_scope or "Delivery"
        self.rule_type = self.rule_type or "Free Delivery"
        self.threshold_basis = self.threshold_basis or "Merchandise Subtotal"

        self._normalize_optional_numbers()

        self._sync_currency()
        self._validate_active_window()
        self._validate_thresholds()
        self._validate_channels()

    def _normalize_optional_numbers(self):
        for fieldname in ("minimum_threshold", "maximum_threshold", "minimum_item_qty"):
            value = getattr(self, fieldname, None)
            if value in (None, ""):
                continue
            if float(value or 0) <= 0:
                setattr(self, fieldname, None)

    def _sync_currency(self):
        if self.company:
            company_currency = frappe.db.get_value("Company", self.company, "default_currency")
            if company_currency:
                self.currency = company_currency
        elif not self.currency:
            default_currency = frappe.db.get_single_value("Global Defaults", "default_currency")
            if default_currency:
                self.currency = default_currency

    def _validate_active_window(self):
        if self.active_from and self.active_to and self.active_to < self.active_from:
            frappe.throw("Active To must be on or after Active From.")

    def _validate_thresholds(self):
        if self.rule_type != "Free Delivery":
            frappe.throw("Jarz Promotion Rule currently supports only the 'Free Delivery' rule type.")

        if self.threshold_basis not in {"Merchandise Subtotal", "Item Quantity"}:
            frappe.throw("Threshold Basis must be either 'Merchandise Subtotal' or 'Item Quantity'.")

        if self.minimum_threshold and self.maximum_threshold:
            if float(self.maximum_threshold) < float(self.minimum_threshold):
                frappe.throw("Maximum Threshold must be greater than or equal to Minimum Threshold.")

        if not self.apply_to_shipping_income and not self.apply_to_legacy_delivery_charges:
            frappe.throw("At least one delivery target must be enabled for the rule to have an effect.")

    def _validate_channels(self):
        seen_channels = set()
        for row in self.channels or []:
            channel = (row.channel or "").strip()
            if not channel:
                frappe.throw("Channel rows must define a channel value.")
            if channel in seen_channels:
                frappe.throw(f"Channel '{channel}' is duplicated in this rule.")
            seen_channels.add(channel)

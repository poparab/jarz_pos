import frappe
from frappe.model.document import Document


class JarzPromoRedemption(Document):
    def before_insert(self):
        # Enforce one active redemption per (promo_code, sales_invoice).
        # TODO: add a DB-level composite UNIQUE index on (promo_code, sales_invoice)
        #       via a migration patch for hard enforcement under concurrency.
        if not (self.promo_code and self.sales_invoice):
            return

        duplicate = frappe.db.exists(
            "Jarz Promo Redemption",
            {
                "promo_code": self.promo_code,
                "sales_invoice": self.sales_invoice,
                "name": ["!=", self.name],
            },
        )
        if duplicate:
            frappe.throw(
                f"Promo Code '{self.promo_code}' is already redeemed on Sales Invoice "
                f"'{self.sales_invoice}'.",
                frappe.DuplicateEntryError,
            )

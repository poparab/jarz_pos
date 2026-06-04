# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document


def _clean_note_text(value: str | None) -> str:
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in raw.split("\n")]
    return "\n".join(lines).strip()


class JarzInvoiceNote(Document):
    """Operational notes attached to Sales Invoices for Kanban users."""

    def validate(self) -> None:
        self.note = _clean_note_text(getattr(self, "note", None))
        if not self.note:
            frappe.throw("Note cannot be empty")

        if not getattr(self, "sales_invoice", None):
            frappe.throw("Sales Invoice is required")

        user = frappe.session.user or getattr(self, "added_by", None)
        if user:
            self.added_by = user
            self.added_by_full_name = (
                getattr(self, "added_by_full_name", None)
                or frappe.utils.get_fullname(user)
                or user
            )

        if not getattr(self, "added_on", None):
            self.added_on = frappe.utils.now()

        try:
            invoice_values = frappe.db.get_value(
                "Sales Invoice",
                self.sales_invoice,
                ["custom_kanban_profile", "pos_profile"],
                as_dict=True,
            ) or {}
            self.pos_profile = (
                invoice_values.get("custom_kanban_profile")
                or invoice_values.get("pos_profile")
                or getattr(self, "pos_profile", None)
            )
        except Exception:
            pass


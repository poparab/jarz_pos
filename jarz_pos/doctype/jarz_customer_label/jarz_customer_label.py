"""Jarz Customer Label -- one printed label design belonging to one B2B customer.

The document itself holds only the *policy* (who it belongs to, whether we print
it, the safety floor and batch size). Every quantity on it is a cached read of
``Jarz Label Movement``, recomputed by ``services.label_stock.refresh_label`` --
never a counter this controller increments, which is how such counters drift.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# Kept in step with the ``naming_series`` field's options in the DocType JSON.
DEFAULT_NAMING_SERIES = "JLBL-.#####"


class JarzCustomerLabel(Document):
    def before_insert(self):
        # The form fills naming_series client-side, but API inserts and Data
        # Import do not -- leaving those paths to fail autoname with "Naming
        # Series mandatory". Fill it here so every path names alike.
        if not self.naming_series:
            self.naming_series = DEFAULT_NAMING_SERIES

    def validate(self):
        self.label_title = (self.label_title or "Default").strip() or "Default"

        if float(self.labels_per_unit or 0) <= 0:
            self.labels_per_unit = 1

        for field in ("min_stock_qty", "reorder_qty"):
            if int(self.get(field) or 0) < 0:
                frappe.throw(_("{0} cannot be negative.").format(_(self.meta.get_label(field))))

        self._validate_unique_title()
        self._validate_single_catch_all()

    def _validate_unique_title(self):
        """One label design per (customer, title): the pair is how a human names it."""
        if not self.customer:
            return
        clash = frappe.db.exists(
            self.doctype,
            {
                "customer": self.customer,
                "label_title": self.label_title,
                "name": ["!=", self.name or ""],
            },
        )
        if clash:
            frappe.throw(
                _("{0} already has a label called {1}.").format(
                    frappe.bold(self.customer), frappe.bold(self.label_title)
                )
            )

    def _validate_single_catch_all(self):
        """At most one tracked label per customer may omit ``applies_to_item_group``.

        Consumption assigns each invoice row to the label whose item group matches,
        falling back to the label with no group at all. Two such fallbacks would
        make that assignment ambiguous, and the invoice would silently draw down
        whichever one happened to be created first -- so the ambiguity is refused
        here rather than guessed at later.
        """
        if not self.customer or (self.applies_to_item_group or "").strip():
            return
        if not (int(self.enabled or 0) and int(self.we_print or 0)):
            return

        others = frappe.get_all(
            self.doctype,
            filters={
                "customer": self.customer,
                "enabled": 1,
                "we_print": 1,
                "name": ["!=", self.name or ""],
            },
            fields=["name", "label_title", "applies_to_item_group"],
        )
        for row in others:
            if not (row.get("applies_to_item_group") or "").strip():
                frappe.throw(
                    _(
                        "{0} already has a catch-all label ({1}) with no Item Group. "
                        "Set an Item Group on this one so each invoice line belongs "
                        "to exactly one label."
                    ).format(frappe.bold(self.customer), frappe.bold(row.get("label_title")))
                )

    def on_update(self):
        # Turning we_print off (or retiring the design) changes the status even
        # though no label moved, so recompute rather than leave a stale "Reorder
        # Now" on a customer who now supplies their own.
        from jarz_pos.services import label_stock

        label_stock.refresh_label(self.name)

    def on_trash(self):
        """Refuse to delete a label that has history -- disable it instead."""
        if frappe.db.exists("Jarz Label Movement", {"label": self.name}):
            frappe.throw(
                _(
                    "This label has stock movements and cannot be deleted. "
                    "Untick Enabled to retire it instead."
                )
            )

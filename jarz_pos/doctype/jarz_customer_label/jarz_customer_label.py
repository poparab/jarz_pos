"""Jarz Customer Label -- one printed label design: one customer, one flavour.

Every flavour has its own artwork and every size its own sheet layout, so the
tracked unit is (customer, flavour item) -- the Item already encodes the size
through its Item Group (Medium / Large). The document holds only *policy*
(where the labels live, the safety floor, the batch size in sheets); every
quantity and cost on it is a cached read of ``Jarz Label Movement``, recomputed
by ``services.label_stock.refresh_label`` -- never a counter this controller
increments, which is how such counters drift.
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
        self._require_flavour_when_tracked()
        self._default_title_from_item()

        if float(self.labels_per_unit or 0) <= 0:
            self.labels_per_unit = 1

        for field in ("min_stock_qty", "labels_per_sheet", "default_print_sheets"):
            if int(self.get(field) or 0) < 0:
                frappe.throw(_("{0} cannot be negative.").format(_(self.meta.get_label(field))))

        self._validate_unique_flavour()

    def _require_flavour_when_tracked(self):
        """A tracked label without a flavour cannot be matched to any invoice line.

        ``we_print = 0`` records (the customers who bring their own labels) may
        omit the item -- they exist only so the customer shows on the board as
        "Customer prints" and are never consumed from.
        """
        if int(self.we_print or 0) and not (self.item or "").strip():
            frappe.throw(
                _(
                    "Pick the flavour (Item) this label is printed for. "
                    "Every flavour has its own label design, so one record "
                    "tracks exactly one flavour."
                )
            )

    def _default_title_from_item(self):
        title = (self.label_title or "").strip()
        if not title:
            if self.item:
                title = (
                    frappe.db.get_value("Item", self.item, "item_name") or self.item
                )
            else:
                title = "Default"
        self.label_title = title

    def _validate_unique_flavour(self):
        """One label record per (customer, flavour): the ledger key must be unambiguous.

        Two records for the same flavour would leave consumption free to pick
        either, splitting one flavour's history across two ledgers.
        """
        if not self.customer or not (self.item or "").strip():
            return
        clash = frappe.db.exists(
            self.doctype,
            {
                "customer": self.customer,
                "item": self.item,
                "name": ["!=", self.name or ""],
            },
        )
        if clash:
            frappe.throw(
                _("{0} already tracks a label for {1} ({2}).").format(
                    frappe.bold(self.customer), frappe.bold(self.item), clash
                )
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

"""Jarz Label Print Order -- one batch of labels sent to the print house.

Its job is twofold: promise a date (``expected_ready_date``, walked forward over
working days so the weekly rest day is never counted as printing time), and stop
the daily alert nagging about a shortage somebody has already acted on.

Receiving is what actually moves stock. Setting the status to ``Received`` --
from Desk or from the mobile app -- posts the ``Print Received`` movement exactly
once, so both paths behave identically and neither can double-credit a batch.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# Kept in step with the ``naming_series`` field's options in the DocType JSON.
DEFAULT_NAMING_SERIES = "JLPO-.#####"

#: Statuses from which a batch can still land or be called off.
OPEN_STATUSES = ("Requested", "Printing", "Ready")


class JarzLabelPrintOrder(Document):
    def before_insert(self):
        if not self.naming_series:
            self.naming_series = DEFAULT_NAMING_SERIES
        if not self.requested_by:
            self.requested_by = frappe.session.user

    def validate(self):
        from jarz_pos.services import label_stock

        if int(self.qty or 0) <= 0:
            frappe.throw(_("Qty Ordered must be greater than zero."))

        if not self.requested_on:
            self.requested_on = frappe.utils.today()

        self.expected_ready_date = label_stock.expected_ready_date(self.requested_on)

        if self.status == "Received":
            if not self.received_on:
                self.received_on = frappe.utils.today()
            if not int(self.received_qty or 0):
                # Partial receipts are recorded by editing received_qty before
                # flipping the status; defaulting to the ordered quantity keeps
                # the common case one click.
                self.received_qty = int(self.qty or 0)
        elif self.status == "Cancelled":
            self.received_qty = 0
            self.received_on = None

    def on_update(self):
        self._post_receipt_once()
        self._refresh_label()

    def after_insert(self):
        # A new open order can flip a label from "Reorder Now" to "On Order".
        self._refresh_label()

    def _post_receipt_once(self):
        """Post the stock-in movement the first time this order reads Received."""
        from jarz_pos.services import label_stock

        if self.status != "Received":
            return
        qty = int(self.received_qty or 0)
        if qty <= 0:
            return
        if frappe.db.exists("Jarz Label Movement", {"print_order": self.name}):
            return  # already credited

        label_stock.post_movement(
            label=self.label,
            movement_type="Print Received",
            qty=qty,
            posting_date=self.received_on or frappe.utils.today(),
            print_order=self.name,
            reference_doctype=self.doctype,
            reference_name=self.name,
            remarks=f"Print batch {self.name} received",
            refresh=False,
        )

    def _refresh_label(self):
        from jarz_pos.services import label_stock

        if self.label:
            label_stock.refresh_label(self.label)

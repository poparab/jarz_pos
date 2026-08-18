"""Jarz Label Print Order -- one batch of label sheets sent to the print house.

Ordering happens in SHEETS (the print house's unit: 21 Medium labels or 18
Large per sheet) while stock is counted in labels, so this document owns the
conversion: ``qty = qty_sheets x labels-per-sheet`` for the design's size.

Its other two jobs: promise a date (``expected_ready_date``, walked forward
over working days so the weekly rest day is never counted as printing time),
and stop the daily alert nagging about a shortage somebody has already acted on.

Money: ``total_cost`` (net of VAT) is what the printer charges for the batch.
On receive it becomes the batch's ``cost_per_label`` and prices the stock-in
movement, which is what later drains into COGS as the labels are consumed.
The supplier's Purchase Invoice itself is created by ``api.labels`` (see
``bill_print_order``) and linked here -- the PI debits the Labels Inventory
asset, so ``billing_status`` is the flag that says whether the GL actually
has this batch's cost yet.

Receiving is what moves stock. Setting the status to ``Received`` -- from Desk
or from the mobile app -- posts the ``Print Received`` movement exactly once,
so both paths behave identically and neither can double-credit a batch.
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

        self._derive_label_qty()

        if int(self.qty or 0) <= 0:
            frappe.throw(_("Order at least one sheet."))

        if not self.requested_on:
            self.requested_on = frappe.utils.today()

        self.expected_ready_date = label_stock.expected_ready_date(self.requested_on)
        self.billing_status = "Billed" if self.purchase_invoice else "Unbilled"

        if self.status == "Received":
            if not self.received_on:
                self.received_on = frappe.utils.today()
            if not int(self.received_qty or 0):
                # Partial receipts are recorded by editing received_qty before
                # flipping the status; defaulting to the ordered quantity keeps
                # the common case one click.
                self.received_qty = int(self.qty or 0)
            self._derive_cost_per_label()
        elif self.status == "Cancelled":
            self.received_qty = 0
            self.received_on = None

    def _derive_label_qty(self):
        """Sheets are the ordering unit; labels are the stock unit.

        When sheets are given they are authoritative and ``qty`` is recomputed
        every save (the sheet size may have been corrected). A legacy/direct
        ``qty`` with no sheets is kept as-is so a Desk user typing labels is not
        fought with.
        """
        from jarz_pos.services import label_stock

        sheets = int(self.qty_sheets or 0)
        if sheets < 0:
            frappe.throw(_("Sheets cannot be negative."))
        if sheets:
            row = frappe.db.get_value(
                "Jarz Customer Label",
                self.label,
                ["size", "labels_per_sheet"],
                as_dict=True,
            ) or {}
            self.qty = sheets * label_stock.labels_per_sheet_for(row)

    def _derive_cost_per_label(self):
        """Batch cost over labels actually received -- the rate stock books in at.

        Uses received_qty, not ordered qty: a printer that delivered short still
        charged for what they delivered, and the per-label cost must reflect the
        labels that really exist.
        """
        total = float(self.total_cost or 0)
        received = int(self.received_qty or 0)
        self.cost_per_label = round(total / received, 4) if total > 0 and received > 0 else 0

    def on_update(self):
        self._post_receipt_once()
        self._refresh_label()

    def after_insert(self):
        # A new open order can flip a label from "Reorder Now" to "On Order".
        self._refresh_label()

    def _post_receipt_once(self):
        """Post the stock-in movement the first time this order reads Received.

        ``post_gl=False`` is deliberate and load-bearing: the batch's value
        reaches the GL through the supplier's Purchase Invoice (which debits
        Labels Inventory directly), so mirroring the receipt into a Journal
        Entry as well would double-count the asset. The movement still CARRIES
        the value -- that is what keeps ledger value == account balance.
        """
        from jarz_pos.services import label_stock

        if self.status != "Received":
            return
        qty = int(self.received_qty or 0)
        if qty <= 0:
            return
        if frappe.db.exists("Jarz Label Movement", {"print_order": self.name}):
            return  # already credited

        sheets_note = f" ({self.qty_sheets} sheet(s))" if int(self.qty_sheets or 0) else ""
        label_stock.post_movement(
            label=self.label,
            movement_type="Print Received",
            qty=qty,
            posting_date=self.received_on or frappe.utils.today(),
            print_order=self.name,
            reference_doctype=self.doctype,
            reference_name=self.name,
            unit_cost=float(self.cost_per_label or 0),
            post_gl=False,
            remarks=f"Print batch {self.name} received{sheets_note}",
            refresh=False,
        )

    def _refresh_label(self):
        from jarz_pos.services import label_stock

        if self.label:
            label_stock.refresh_label(self.label)

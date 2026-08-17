"""Jarz Label Movement -- the append-only ledger behind every label count.

``qty`` is a *signed* change: ``Print Received`` adds, ``Consumed`` and
``Scrapped`` remove, ``Adjustment`` and ``Opening`` may do either. On-hand is
always ``SUM(qty)`` over this table, so a corrected count is a new row rather
than an edit of an old one.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

# Kept in step with the ``naming_series`` field's options in the DocType JSON.
DEFAULT_NAMING_SERIES = "JLMV-.#####"

#: Movement types whose sign is not the poster's choice.
FIXED_SIGN = {"Print Received": 1, "Consumed": -1, "Scrapped": -1}


class JarzLabelMovement(Document):
    def before_insert(self):
        if not self.naming_series:
            self.naming_series = DEFAULT_NAMING_SERIES

    def validate(self):
        if not self.posting_date:
            self.posting_date = frappe.utils.today()

        qty = int(self.qty or 0)
        if qty == 0:
            frappe.throw(_("Qty Change cannot be zero."))

        # Normalise rather than reject: a human typing "50" against Consumed
        # means fifty labels used, not fifty gained.
        sign = FIXED_SIGN.get(self.movement_type)
        if sign:
            self.qty = sign * abs(qty)

        if frappe.utils.getdate(self.posting_date) > frappe.utils.getdate(frappe.utils.today()):
            frappe.throw(_("Posting Date cannot be in the future."))

    def on_update(self):
        # Frappe runs on_update on insert too (via run_post_save_methods), so
        # this one hook covers both a new row and an edited one. A separate
        # after_insert would only refresh the same label twice.
        self._refresh_label()

    def on_trash(self):
        # Deleting a row changes the balance, so the cached columns have to be
        # rebuilt -- but the label is gone by the time on_trash runs during a
        # cascade delete, and refresh_label already no-ops on a missing label.
        self._refresh_label()

    def _refresh_label(self):
        from jarz_pos.services import label_stock

        if self.label:
            label_stock.refresh_label(self.label)

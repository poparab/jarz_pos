"""One reviewable crossing between the branch that shipped and the delivery territory.

Why this exists: on staging 1,285 submitted invoices (~EGP 520k) carry a
``pos_profile`` that is not the ``pos_profile`` their ``Territory`` points at,
and the crossings are systematic (Nasr city <-> Dokki <-> 6th of October, both
directions). The owner's ruling is that **the branch that shipped is the fact**
-- so nothing here corrects an invoice, blocks an order, or changes how a
profile is chosen. This document is purely the catch: it records the crossing
with enough snapshot detail that a human can fix the *Territory* afterwards.

Rows are written by ``jarz_pos.services.territory_exceptions`` and are unique
per ``(sales_invoice, exception_type)``. Frappe cannot express a composite
unique key in the DocType JSON, so ``validate`` enforces it -- and the service
checks before inserting, so this guard only ever fires against a race or a
hand-made duplicate.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

#: Kept in step with the ``status`` Select options in the DocType JSON and with
#: ``services.territory_exceptions``.
STATUS_OPEN = "Open"
STATUS_TERRITORY_CORRECTED = "Territory Corrected"
STATUS_ACCEPTED = "Accepted"
STATUS_IGNORED = "Ignored"

#: Every status that means "a human is done with this row".
RESOLVED_STATUSES = (STATUS_TERRITORY_CORRECTED, STATUS_ACCEPTED, STATUS_IGNORED)


class JarzTerritoryException(Document):
    def validate(self):
        self._default_status()
        self._reject_duplicate()
        self._stamp_resolution()

    def _default_status(self):
        if not (self.status or "").strip():
            self.status = STATUS_OPEN

    def _reject_duplicate(self):
        """One row per (invoice, exception type) -- the whole point is idempotency.

        A second row for the same pair would show a reviewer the same crossing
        twice and make "how many are open" meaningless.
        """
        if not self.sales_invoice or not self.exception_type:
            return

        clash = frappe.db.exists(
            self.doctype,
            {
                "sales_invoice": self.sales_invoice,
                "exception_type": self.exception_type,
                "name": ["!=", self.name or ""],
            },
        )
        if clash:
            frappe.throw(
                _("{0} is already recorded against {1} ({2}).").format(
                    frappe.bold(self.exception_type),
                    frappe.bold(self.sales_invoice),
                    clash,
                ),
                frappe.DuplicateEntryError,
            )

    def _stamp_resolution(self):
        """Keep ``resolved_on``/``resolved_by`` honest however the status was set.

        Both fields are read-only on the form, so a manager who picks a status
        from the list view or the Desk form would otherwise leave them empty and
        the audit trail would say nobody ever closed anything.
        """
        if (self.status or STATUS_OPEN) in RESOLVED_STATUSES:
            if not self.resolved_on:
                self.resolved_on = frappe.utils.now_datetime()
            if not self.resolved_by:
                self.resolved_by = frappe.session.user
        else:
            # Re-opened: the previous resolution no longer describes this row.
            self.resolved_on = None
            self.resolved_by = None

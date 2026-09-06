"""One reviewable WooCommerce order sitting in a non-terminal ops state.

Why this exists: the POS ops pipeline stopped being driven around 2026-06-06
(see ``jarz_pos.scripts.backlog_migration``), and WooCommerce orders kept
arriving, kept being invoiced, kept being delivered in real life -- while the
ERPNext side stayed frozen in an early state with the cash never booked. That
script is a supervised, manual migration that requires WooCommerce outbound to
be disarmed before it can run at all (re-pushing a status to a live order
would fire a duplicate completed-order email at a real customer). It is
deliberately NOT something a daily job may drive.

This DocType is the unattended half of the story: a plain record of the
symptom -- a Woo-linked invoice that has not reached a terminal ops state
long after it should have -- written by
``jarz_pos.services.woo_backlog_watch``. Rows are unique per
``sales_invoice``. Frappe cannot express that as a DocType-level unique
constraint on its own, so ``validate`` enforces it here -- the service already
checks before inserting, so this guard only ever fires against a race or a
hand-made duplicate.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

#: Kept in step with the ``status`` Select options in the DocType JSON and with
#: ``services.woo_backlog_watch``.
STATUS_OPEN = "Open"
STATUS_RESOLVED = "Resolved"


class JarzWooBacklogException(Document):
    def validate(self):
        self._default_status()
        self._reject_duplicate()
        self._stamp_resolution()

    def _default_status(self):
        if not (self.status or "").strip():
            self.status = STATUS_OPEN

    def _reject_duplicate(self):
        """One row per Sales Invoice -- there is only one kind of exception here."""
        if not self.sales_invoice:
            return

        clash = frappe.db.exists(
            self.doctype,
            {
                "sales_invoice": self.sales_invoice,
                "name": ["!=", self.name or ""],
            },
        )
        if clash:
            frappe.throw(
                _("{0} already has an open backlog exception ({1}).").format(
                    frappe.bold(self.sales_invoice), clash
                ),
                frappe.DuplicateEntryError,
            )

    def _stamp_resolution(self):
        """Keep ``resolved_on``/``resolved_by`` honest however ``status`` was set."""
        if (self.status or STATUS_OPEN) == STATUS_RESOLVED:
            if not self.resolved_on:
                self.resolved_on = frappe.utils.now_datetime()
            if not self.resolved_by:
                self.resolved_by = frappe.session.user
        else:
            # Re-opened: the previous resolution no longer describes this row.
            self.resolved_on = None
            self.resolved_by = None

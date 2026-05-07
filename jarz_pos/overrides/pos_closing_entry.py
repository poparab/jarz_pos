from __future__ import annotations

import frappe

from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
    POSClosingEntry as ERPNextPOSClosingEntry,
)


class POSClosingEntry(ERPNextPOSClosingEntry):
    def update_opening_entry(self, for_cancel: bool = False):
        opening_entry = frappe.get_doc("POS Opening Entry", self.pos_opening_entry)
        opening_entry.pos_closing_entry = self.name if not for_cancel else None
        opening_entry.flags.ignore_permissions = True
        opening_entry.set_status()
        opening_entry.save(ignore_permissions=True)
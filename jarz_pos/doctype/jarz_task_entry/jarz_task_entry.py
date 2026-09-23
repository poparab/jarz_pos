# Copyright (c) 2026, Abdelrahman Mamdouh and contributors
# For license information, please see license.txt

"""One line in a task's timeline: a Log, a Comment, or a server-written History row.

Entries are immutable. The timeline is the audit trail of who did what to a
task -- including why it was sent back or reopened -- so an entry that could be
edited or deleted afterwards would make it worthless. Only Administrator (and a
migrate) may rewrite or delete one.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

from jarz_pos.services import task_board as tb


def _may_rewrite() -> bool:
    return frappe.session.user == "Administrator" or bool(
        getattr(frappe.flags, "in_migrate", False)
    )


class JarzTaskEntry(Document):
    def validate(self):
        if not self.is_new() and not _may_rewrite():
            frappe.throw(
                frappe._("Task log entries cannot be edited once saved."),
                frappe.PermissionError,
            )
        if self.kind not in (tb.KIND_LOG, tb.KIND_COMMENT, tb.KIND_HISTORY):
            self.kind = tb.KIND_LOG
        if not self.author:
            self.author = frappe.session.user

    def on_trash(self):
        if frappe.session.user != "Administrator":
            frappe.throw(
                frappe._("Task log entries cannot be deleted."),
                frappe.PermissionError,
            )

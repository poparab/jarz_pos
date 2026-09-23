# Copyright (c) 2026, Abdelrahman Mamdouh and contributors
# For license information, please see license.txt

"""A card on the Task Board.

The rules -- who may create, edit, move or see a task -- live in
``jarz_pos.services.task_board`` and are enforced by ``jarz_pos.api.tasks``,
which is the only intended writer. This controller only keeps the record
well-formed, so a System Manager saving one in Desk cannot leave it without a
creator, a status or a title.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

from jarz_pos.services import task_board as tb


class JarzTask(Document):
    def validate(self):
        self.title = " ".join(str(self.title or "").split())[: tb.TITLE_MAX_LENGTH]
        if not self.title:
            frappe.throw(frappe._("Title is required"))
        if self.status not in tb.STATUSES:
            self.status = tb.STATUS_TODO
        if self.priority not in tb.PRIORITIES:
            self.priority = tb.PRIORITY_NORMAL
        if not self.created_by:
            self.created_by = frappe.session.user
        for row in self.get("subtasks") or []:
            row.title = " ".join(str(row.title or "").split())[: tb.TITLE_MAX_LENGTH]
            if not row.created_by:
                row.created_by = frappe.session.user

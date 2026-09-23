"""One change to who may open the POS at which branch.

Branch access itself is the ``POS Profile User`` child table, and a child row
carries no history: once it is deleted there is no trace it ever existed, and
the parent's Version log is empty because the service writes the rows directly
rather than saving the POS Profile. That silence is why this record exists --
the owner asked that every change say who made it, when, for whom, at which
branch and why.

It is an audit trail, so it is append-only. DocPerm grants nobody create, write
or delete; ``jarz_pos.services.branch_access`` inserts with
``ignore_permissions``. The controller also refuses edits and deletes, because
``ignore_permissions`` would otherwise let any later server code rewrite
history as easily as it wrote it.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


def _full_name(user: str | None) -> str | None:
    if not user:
        return None
    return frappe.db.get_value("User", user, "full_name") or user


class JarzBranchAccessLog(Document):
    def before_insert(self):
        """Freeze the names as they were at the moment of the change.

        Stored rather than fetched because a log that re-reads names later
        would silently rename history when a user record is edited.
        ``changed_by`` falls back to the session user so a caller that forgets
        it still records an author instead of failing the whole change on a
        mandatory-field error.
        """
        if not self.changed_by:
            self.changed_by = frappe.session.user
        self.user_full_name = _full_name(self.user)
        self.changed_by_name = _full_name(self.changed_by)

    def validate(self):
        if not self.is_new():
            frappe.throw(
                _("Branch access log entries cannot be edited."),
                title=_("Read Only"),
            )

    def on_trash(self):
        """Only Administrator may delete, and only as a deliberate data repair.

        Managers already have no delete DocPerm; this also stops server code
        running with ignore_permissions from quietly pruning the trail.
        """
        if frappe.session.user != "Administrator":
            frappe.throw(
                _("Branch access log entries cannot be deleted."),
                title=_("Read Only"),
            )

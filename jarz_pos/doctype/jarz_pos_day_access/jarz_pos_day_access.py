"""A one-day POS access grant for one user at one branch.

Permanent branch access is membership of the ``POS Profile User`` child table,
and that table has no notion of time. A manager who lets someone cover a branch
for a day would otherwise have to remember to take the access away again, and
nothing would show that the row was only ever meant to be temporary. This record
carries the intent -- which day, and whether this grant is what put the row
there -- so the hourly job in ``jarz_pos.services.branch_access`` can end it
without touching anyone's permanent access.

``row_added`` is the load-bearing field. It is 1 only when THIS grant inserted
the membership row. When the user was already a member it stays 0, so ending
the grant removes nothing; otherwise expiring a day grant would revoke access
the person had long before it.

Lifecycle::

    Scheduled --> Active --> Ended
        |           |
        +-----------+--> Cancelled

Ended and Cancelled are final. The service moves a grant along this path; the
controller refuses any other move so a bug or a hand edit in Desk cannot bring a
finished grant back to life with its membership row long since deleted.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

OPEN_STATUSES = ("Scheduled", "Active")
FINAL_STATUSES = ("Ended", "Cancelled")

ALLOWED_TRANSITIONS = {
    "Scheduled": {"Scheduled", "Active", "Cancelled"},
    "Active": {"Active", "Ended", "Cancelled"},
}


def _full_name(user: str | None) -> str | None:
    if not user:
        return None
    return frappe.db.get_value("User", user, "full_name") or user


class JarzPOSDayAccess(Document):
    def before_insert(self):
        if not self.granted_by:
            self.granted_by = frappe.session.user

    def validate(self):
        self.fill_names()
        self.validate_status_transition()
        self.validate_not_duplicate()

    def fill_names(self):
        """Store the names rather than fetch them, so the record reads the same
        after a user is renamed. Refreshed only when the link changes."""
        if self.user and (not self.user_full_name or self.has_value_changed("user")):
            self.user_full_name = _full_name(self.user)
        if self.granted_by and (not self.granted_by_name or self.has_value_changed("granted_by")):
            self.granted_by_name = _full_name(self.granted_by)

    def validate_status_transition(self):
        """Keep the grant on its lifecycle path.

        Administrator is exempt on purpose: that is the identity data repairs
        run under, and the hourly scheduler job runs as Administrator too, so
        the service is never blocked by its own guard. Everyone else -- a
        manager's API call, a Desk edit -- is held to the path.
        """
        if frappe.session.user == "Administrator":
            return

        if self.is_new():
            if self.status not in OPEN_STATUSES:
                frappe.throw(
                    _("A new day access must start as Scheduled or Active, not {0}.").format(
                        frappe.bold(self.status)
                    ),
                    title=_("Invalid Status"),
                )
            return

        before = self.get_doc_before_save()
        previous = before.status if before else None
        if not previous:
            return

        if previous in FINAL_STATUSES:
            frappe.throw(
                _("This day access is already {0} and can no longer be changed.").format(
                    frappe.bold(previous)
                ),
                title=_("Day Access Closed"),
            )

        allowed = ALLOWED_TRANSITIONS.get(previous, {previous})
        if self.status not in allowed:
            frappe.throw(
                _("A day access cannot move from {0} to {1}.").format(
                    frappe.bold(previous), frappe.bold(self.status)
                ),
                title=_("Invalid Status"),
            )

    def validate_not_duplicate(self):
        """At most one open grant per (user, branch, day).

        Two open grants for the same day would both believe they own the
        membership row; the second would record row_added=0 because the first
        had just inserted it, and whichever ended last would decide whether the
        row survived. The pair is refused here with a readable message rather
        than a unique index, because finished grants for the same day are
        legitimate history (a grant cancelled and then re-issued).
        """
        if self.status not in OPEN_STATUSES:
            return
        if not (self.user and self.pos_profile and self.access_date):
            return

        existing = frappe.db.get_value(
            "Jarz POS Day Access",
            {
                "user": self.user,
                "pos_profile": self.pos_profile,
                "access_date": getdate(self.access_date),
                "status": ("in", OPEN_STATUSES),
                "name": ("!=", self.name or ""),
            },
            ["name", "status"],
            as_dict=True,
        )
        if existing:
            frappe.throw(
                _("{0} already has a {1} day access to {2} on {3} ({4}).").format(
                    frappe.bold(self.user_full_name or self.user),
                    existing.status.lower(),
                    frappe.bold(self.pos_profile),
                    frappe.bold(frappe.format(getdate(self.access_date), {"fieldtype": "Date"})),
                    existing.name,
                ),
                title=_("Already Granted"),
            )

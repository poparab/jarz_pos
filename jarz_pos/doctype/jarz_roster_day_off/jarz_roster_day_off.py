"""One employee, one calendar day, deliberately not working.

This exists because the *absence* of a Shift Assignment cannot answer the two
questions the roster screen has to answer. HRMS models "off" as a hole in the
roster, and a hole is indistinguishable from "this person was never rostered in
the first place" -- so an operator cannot tell a granted day off from a
scheduling mistake, and nothing records who agreed to it or who is covering.

The record also carries the **restore data**. Setting a day off breaks the
employee's Shift Assignment for that date, and moving the covering colleague
onto a longer shift rewrites theirs. Undo therefore has to put two people back,
and it can only do that if what they were on beforehand was written down at the
time. Recomputing it later from the Shift Schedule is not equivalent: the
schedule may have been edited in between, and a Friday sits on a different
shift type from the rest of the week.

Enforcement lives in ``jarz_pos.events.employee_checkin`` -- the presence of a
row here is what turns a check-in away. See ``jarz_pos.services.roster`` for the
mechanics that keep this record and the HRMS Shift Assignments in step.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class JarzRosterDayOff(Document):
    def validate(self):
        self.validate_not_duplicate()
        self.validate_cover_is_not_self()
        self.stamp_author()

    def validate_not_duplicate(self):
        """One row per (employee, date).

        Enforced here rather than by a unique index because the pair is the
        business key but neither column alone is, and a duplicate is a normal
        user action (tapping "off" twice) that deserves a readable message
        rather than an IntegrityError.
        """
        if not (self.employee and self.off_date):
            return

        existing = frappe.db.exists(
            "Jarz Roster Day Off",
            {
                "employee": self.employee,
                "off_date": getdate(self.off_date),
                "name": ("!=", self.name or ""),
            },
        )
        if existing:
            frappe.throw(
                _("{0} is already marked off on {1}.").format(
                    frappe.bold(self.employee_name or self.employee),
                    frappe.bold(frappe.format(getdate(self.off_date), {"fieldtype": "Date"})),
                ),
                title=_("Already Off"),
            )

    def validate_cover_is_not_self(self):
        """Nobody covers for themselves.

        Worth a guard rather than trusting the UI: the covering employee is
        picked from a list that legitimately contains everyone on the branch,
        and letting the pair collapse would make the swap below break and
        re-insert the same person's shift on the same day -- which HRMS answers
        with an opaque OverlappingShiftError.
        """
        if self.covered_by and self.covered_by == self.employee:
            frappe.throw(_("An employee cannot cover their own day off."))

    def stamp_author(self):
        if not self.created_by_user:
            self.created_by_user = frappe.session.user
            self.created_by_name = frappe.db.get_value("User", frappe.session.user, "full_name")

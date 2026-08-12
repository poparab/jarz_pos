# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""Journey notes: the dated field diary a B2B rep keeps on a prospect.

One row per real-world touch — a visit that dropped samples, a call where the
manager asked to be rung back next week, a WhatsApp reply. The point is the
DATE and the PERSON: ``entry_date`` is when the touch happened, and
``next_action_date`` + ``contact_person`` are who to chase and when.

The next-action date is not decoration. When it is set, the note stamps the
referenced Lead/Opportunity's ``custom_next_followup_date`` and re-opens the
follow-up loop (``custom_followup_done = 0``), which is exactly what the daily
``jarz_pos.crm.follow_ups`` passes and the app's "My follow-ups" feed read. So
a rep writing "call the manager Thursday" here gets the reminder for free
instead of having to also drive the stage editor.

Every side effect is guarded: a missing custom field or a ToDo hiccup must
never stop the note itself from being saved.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

# Reference types a journey note may hang off. Kept in lockstep with the
# ``reference_doctype`` Select options.
REFERENCE_DOCTYPES = ("Lead", "Opportunity", "Customer")

# Reference types that carry the CRM follow-up fields. A Customer has no
# ``custom_next_followup_date``, so a next-action date on a customer note is
# recorded but drives no reminder pass.
_FOLLOWUP_DOCTYPES = ("Lead", "Opportunity")


def _clean_text(value):
    """Normalise line endings and strip trailing whitespace per line."""
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in raw.split("\n")).strip()


def _has_field(doctype, fieldname):
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


class JarzJourneyNote(Document):
    """A dated visit/call note on a Lead, Opportunity or Customer."""

    def validate(self):
        self.note = _clean_text(getattr(self, "note", None))
        if not self.note:
            frappe.throw("Note cannot be empty")

        if getattr(self, "reference_doctype", None) not in REFERENCE_DOCTYPES:
            frappe.throw(
                "Reference Type must be one of: " + ", ".join(REFERENCE_DOCTYPES)
            )
        if not getattr(self, "reference_name", None):
            frappe.throw("Reference is required")
        if not frappe.db.exists(self.reference_doctype, self.reference_name):
            frappe.throw(
                f"{self.reference_doctype} '{self.reference_name}' not found."
            )

        if not getattr(self, "entry_date", None):
            self.entry_date = frappe.utils.today()

        # A blank type reads as "some touch happened"; default it rather than
        # letting the timeline show an empty chip.
        if not getattr(self, "entry_type", None):
            self.entry_type = "Visit"

        for field in ("next_action", "contact_person", "contact_role", "contact_phone"):
            value = getattr(self, field, None)
            if value:
                setattr(self, field, _clean_text(value))

        user = frappe.session.user
        if user and not getattr(self, "logged_by", None):
            self.logged_by = user
        if getattr(self, "logged_by", None) and not getattr(self, "logged_by_name", None):
            try:
                self.logged_by_name = (
                    frappe.utils.get_fullname(self.logged_by) or self.logged_by
                )
            except Exception:
                self.logged_by_name = self.logged_by

    def on_update(self):
        # Covers insert too (Frappe runs on_update for the "save" action, which
        # an insert is), so there is deliberately no after_insert hook here --
        # having both just ran the same idempotent propagation twice per insert.
        self.sync_followup()

    def on_trash(self):
        """A deleted note takes its reminder with it."""
        self._close_reminder_todos()

    # ------------------------------------------------------------------
    # Follow-up propagation
    # ------------------------------------------------------------------
    def sync_followup(self):
        """Push ``next_action_date`` onto the referenced record's follow-up fields.

        Only ever moves the reminder EARLIER or sets one where there was none:
        a note asking to call back on the 20th must not push a reminder already
        scheduled for the 14th out of the way. Never raises — the note is the
        record of truth, the reminder is a convenience.
        """
        next_date = getattr(self, "next_action_date", None)
        if not next_date:
            # The rep cleared the date on an edit: retire the reminder rather
            # than leaving a ToDo nagging about a promise that no longer exists.
            self._close_reminder_todos()
            return
        if getattr(self, "reference_doctype", None) not in _FOLLOWUP_DOCTYPES:
            return

        doctype = self.reference_doctype
        name = self.reference_name

        try:
            if _has_field(doctype, "custom_next_followup_date"):
                current = frappe.db.get_value(
                    doctype, name, "custom_next_followup_date"
                )
                if not current or frappe.utils.getdate(next_date) < frappe.utils.getdate(
                    current
                ):
                    frappe.db.set_value(
                        doctype,
                        name,
                        "custom_next_followup_date",
                        next_date,
                        update_modified=False,
                    )
            # Re-open the loop regardless: a rep who just wrote a next action has
            # by definition not finished following up, and the reminder passes
            # skip anything flagged done.
            if _has_field(doctype, "custom_followup_done"):
                frappe.db.set_value(
                    doctype, name, "custom_followup_done", 0, update_modified=False
                )
        except Exception:
            frappe.log_error(
                title="Jarz Journey Note: follow-up stamp failed",
                message=frappe.get_traceback(),
            )

        self._ensure_reminder_todo(next_date)

    @property
    def _todo_marker(self):
        """The tag that identifies THIS note's reminder among a record's ToDos.

        Uses the app-wide ``[jarz:<kind>]`` scheme from ``crm.follow_ups`` with a
        per-note kind, so one convention covers every reminder this app opens:
        the note finds its own by the full tag, while ``close_all_jarz_todos``
        can still sweep them by the shared ``[jarz:`` prefix without touching an
        Assignment Rule's ToDo.
        """
        from jarz_pos.crm.follow_ups import todo_marker

        return todo_marker(f"journey:{self.name}")

    def _reminder_description(self):
        who = (getattr(self, "contact_person", None) or "").strip()
        action = (getattr(self, "next_action", None) or "").strip()
        parts = [
            self._todo_marker,
            f"Follow up on {self.reference_doctype.lower()} {self.reference_name}",
        ]
        if who:
            parts.append(f"with {who}")
        if action:
            parts.append(f"- {action}")
        return " ".join(parts)

    def _reminder_owner(self):
        """Who the reminder lands on.

        The rep who WROTE the note, first: they are the one who promised to call
        back, and the "My follow-ups" feed is keyed on ``allocated_to``. The
        record's owner is only the fallback -- most of this catalog was bulk
        imported, so its owner is usually Administrator, whose feed nobody reads.
        """
        for candidate in (
            getattr(self, "logged_by", None),
            frappe.db.get_value(self.reference_doctype, self.reference_name, "owner"),
            frappe.session.user,
        ):
            if candidate and frappe.db.exists("User", candidate):
                return candidate
        return None

    def _open_reminder_todos(self):
        """Open ToDos that are THIS note's reminder. Guarded -> []."""
        try:
            return frappe.get_all(
                "ToDo",
                filters={
                    "reference_type": self.reference_doctype,
                    "reference_name": self.reference_name,
                    "status": "Open",
                    "description": ["like", f"%{self._todo_marker}%"],
                },
                pluck="name",
            )
        except Exception:
            return []

    def _close_reminder_todos(self):
        """Close this note's reminder ToDos. Never raises."""
        try:
            for todo in self._open_reminder_todos():
                frappe.db.set_value("ToDo", todo, "status", "Closed")
        except Exception:
            frappe.log_error(
                title="Jarz Journey Note: closing reminder ToDo failed",
                message=frappe.get_traceback(),
            )

    def _ensure_reminder_todo(self, next_date):
        """Create or re-date THIS note's reminder ToDo. Never raises.

        Deliberately NOT ``jarz_pos.crm.follow_ups._ensure_todo``. That helper
        dedups against ANY open ToDo on the reference, and this site runs an
        Assignment Rule that opens one on every Lead the moment it is created --
        2319 of them on staging. So every journey reminder was silently
        swallowed by an unrelated "Automatic Assignment" ToDo dated today,
        leaving the rep's actual promise ("ring the owner Thursday") with no
        feed entry at all. Found by running the suite against staging; a fresh
        CI site has no Assignment Rule, so CI could never have caught it.

        Tagging the description with :attr:`_todo_marker` gives each note's
        reminder its own identity, so it is created once and then RE-DATED in
        place when the rep moves the date, rather than duplicated or skipped.
        """
        try:
            description = self._reminder_description()
            existing = self._open_reminder_todos()
            if existing:
                for todo in existing:
                    frappe.db.set_value(
                        "ToDo",
                        todo,
                        {"date": next_date, "description": description},
                        update_modified=False,
                    )
                return

            todo = {
                "doctype": "ToDo",
                "description": description,
                "reference_type": self.reference_doctype,
                "reference_name": self.reference_name,
                "status": "Open",
                "date": next_date,
            }
            owner = self._reminder_owner()
            if owner:
                todo["allocated_to"] = owner
                todo["owner"] = owner
            if _has_field("ToDo", "priority"):
                todo["priority"] = "Medium"

            frappe.get_doc(todo).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                title="Jarz Journey Note: reminder ToDo failed",
                message=frappe.get_traceback(),
            )

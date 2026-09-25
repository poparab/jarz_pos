"""Jarz Settlement Terms -- one record per B2B customer: how they settle credit.

A schedule plus reminders, never enforcement. The rules (what a cycle means,
when an invoice falls due, what is overdue) live in the pure
``jarz_pos.services.settlement_schedule`` module; this controller only makes
sure a stored record is one that module can read, using the SAME normaliser
``api.settlement_terms.save_settlement_terms`` uses, so a Desk edit and an app
edit refuse exactly the same things.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, nowdate

from jarz_pos.services import settlement_schedule as ss

#: Fields ``normalize_terms_input`` owns (and rewrites) on every save.
_NORMALIZED_FIELDS = (
    "cycle",
    "enabled",
    "weekdays",
    "week_interval",
    "month_days",
    "interval_days",
    "anchor_date",
    "remind_days_before",
    "overdue_repeat_days",
    "responsible_user",
    "notes",
)


class JarzSettlementTerms(Document):
    def validate(self):
        try:
            values = ss.normalize_terms_input({f: self.get(f) for f in _NORMALIZED_FIELDS})
        except ss.SettlementTermsError as exc:
            frappe.throw(_(str(exc)), title=_("Invalid settlement terms"))
            return

        if values["anchor_date"] is None and ss.needs_anchor(values):
            # "Counted from when the terms were agreed" is the only default a
            # person would guess; the day of saving is the closest record of it.
            values["anchor_date"] = getdate(nowdate())

        user = values.get("responsible_user")
        if user:
            enabled = frappe.db.get_value("User", user, "enabled")
            if enabled is None:
                frappe.throw(_("User {0} does not exist.").format(user))
            if not int(enabled or 0):
                frappe.throw(_("User {0} is disabled; choose someone who can receive reminders.").format(user))

        for field, value in values.items():
            self.set(field, value)

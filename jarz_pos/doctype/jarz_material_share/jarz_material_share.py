# Copyright (c) 2026, Jarz and contributors
# For license information, please see license.txt

"""One pack of sales material, sent once, to one person.

The row IS the link: ``token`` is the only credential, and the set of materials
recorded here is the only thing that maps a public URL back to a lead. Two
consequences shape the controller.

**The token is minted here and never anywhere else.** ``before_insert`` is the
one place it is set, so no caller can pass one in and no code path can produce
a share whose URL was chosen rather than drawn from the CSPRNG.

**Engagement is written by anonymous visitors.** :meth:`record_view` is reached
from a guest request, so it takes the narrowest possible route: ``db.set_value``
on three counters with ``update_modified=False``. No ``save()``, so no
validation, no version rows and no chance a hook meant for the rep's context
runs as Guest.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from jarz_pos.services import materials as materials_service
from jarz_pos.utils.phone import whatsapp_msisdn


class JarzMaterialShare(Document):
    """A minted, trackable link to a set of sales materials."""

    def before_insert(self):
        self.token = materials_service.new_token()
        if not self.sent_by:
            user = frappe.session.user
            self.sent_by = user if user and user != "Guest" else None
        if not self.sent_on:
            self.sent_on = now_datetime()

    def validate(self):
        self.contact_name = (self.contact_name or "").strip() or None
        self.contact_phone = (self.contact_phone or "").strip() or None
        if not self.items:
            frappe.throw("Pick at least one material to send.")
        seen = set()
        deduped = []
        for row in self.items:
            if not row.material or row.material in seen:
                continue
            seen.add(row.material)
            deduped.append(row)
        self.items = deduped
        for index, row in enumerate(self.items, start=1):
            row.idx = index

    @property
    def public_url(self) -> str:
        return materials_service.share_url(self.token)

    @property
    def msisdn(self) -> str:
        return whatsapp_msisdn(self.contact_phone)

    def is_expired(self) -> bool:
        if not self.expires_on:
            return False
        try:
            return get_datetime(self.expires_on) < now_datetime()
        except Exception:
            return False

    def record_view(self) -> None:
        """Count one opening of this link. Safe to call from a guest request."""
        now = now_datetime()
        values = {
            "view_count": (self.view_count or 0) + 1,
            "last_viewed_on": now,
        }
        first = not self.first_viewed_on
        if first:
            values["first_viewed_on"] = now
        frappe.db.set_value(self.doctype, self.name, values, update_modified=False)
        self.view_count = values["view_count"]
        self.last_viewed_on = now
        if first:
            self.first_viewed_on = now
            self._log_first_open()

    def _log_first_open(self) -> None:
        """Write the rep's diary entry the first time the customer opens it.

        This is the signal the whole link-instead-of-attachment design buys:
        with files in a chat, "did they even look?" is unanswerable. Best
        effort by construction -- a diary that cannot be written must never
        turn the customer's page into a 500.
        """
        if self.reference_doctype not in ("Lead", "Opportunity", "Customer"):
            return
        try:
            titles = [(row.title or row.material) for row in (self.items or [])]
            listed = "\n".join(f"- {title}" for title in titles)
            note = frappe.get_doc(
                {
                    "doctype": "Jarz Journey Note",
                    "reference_doctype": self.reference_doctype,
                    "reference_name": self.reference_name,
                    "entry_type": "WhatsApp",
                    "contact_person": self.contact_name,
                    "contact_phone": self.contact_phone,
                    "note": (
                        "Opened the material link that was sent"
                        + (f" to {self.contact_name}" if self.contact_name else "")
                        + ".\n"
                        + listed
                    ),
                    # Attribute the entry to the rep who sent it, not to Guest:
                    # this is a line in THEIR diary about THEIR prospect.
                    "logged_by": self.sent_by,
                }
            )
            note.insert(ignore_permissions=True)
        except Exception:
            frappe.logger("jarz_materials", allow_site=True).error(
                f"first-open journey note failed for {self.name}", exc_info=True
            )

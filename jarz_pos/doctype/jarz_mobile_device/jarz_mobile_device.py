from __future__ import annotations

import json

import frappe
from frappe.model.document import Document  # type: ignore[import]


class JarzMobileDevice(Document):
    """Stores registered mobile devices for push notifications."""

    def before_save(self) -> None:
        # Ensure last_seen captures the most recent registration/update time
        self.last_seen = frappe.utils.now_datetime()
        # Normalise optional POS profile payloads to compact JSON strings
        if self.pos_profiles:
            try:
                if isinstance(self.pos_profiles, (list, tuple)):
                    self.pos_profiles = frappe.as_json(list(self.pos_profiles))
                else:
                    json.loads(self.pos_profiles)
            except Exception:
                self.pos_profiles = None

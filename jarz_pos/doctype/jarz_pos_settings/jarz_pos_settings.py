# Copyright (c) 2025, Jarz and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class JarzPOSSettings(Document):
    """Singleton settings document for the Jarz POS plugin.

    All account names, price lists, groups, and receipt configuration
    live here so they can be edited from the Desk UI without touching
    constants.py or redeploying code.
    """
    pass


# ---------------------------------------------------------------------------
# Public helper – importable by any backend module
# ---------------------------------------------------------------------------

def get_jarz_settings() -> "JarzPOSSettings":
    """Return the cached Jarz POS Settings singleton.

    On the very first call after migration (before the document has been
    saved from the UI) the defaults defined in the doctype JSON are used
    automatically by Frappe.
    """
    return frappe.get_cached_doc("Jarz POS Settings")

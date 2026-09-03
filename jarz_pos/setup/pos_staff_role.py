"""Guarantee the ``Jarz POS Staff`` Role record exists before the schema syncs.

``POS Payment Receipt`` grants this role create+write so a dispatch-desk staffer
can attach and re-attach the InstaPay/wallet proof of transfer. A DocPerm row is
a Link to Role, so on any site where the record is missing the DocType sync
would fail link validation and take the whole migrate with it.

Deliberately wired into ``before_migrate``: the DocType JSON is imported during
the schema updates, which run BEFORE ``after_migrate`` — an ensure there would
arrive after the failure it is meant to prevent.

Create-only and self-swallowing, like the other seeders: it must never be the
reason a migrate stops.
"""

from __future__ import annotations

import frappe

ROLE_NAME = "Jarz POS Staff"


def ensure_pos_staff_role() -> None:
	try:
		if frappe.db.exists("Role", ROLE_NAME):
			return
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": ROLE_NAME,
				# POS staff work from the mobile app against whitelisted
				# endpoints; the Desk is not part of the job.
				"desk_access": 0,
				"disabled": 0,
			}
		).insert(ignore_permissions=True)
		frappe.logger("jarz_pos.setup", allow_site=True).info(
			f"Created Role '{ROLE_NAME}'"
		)
	except Exception:
		frappe.logger("jarz_pos.setup", allow_site=True).error(
			f"Failed to ensure Role '{ROLE_NAME}'", exc_info=True
		)

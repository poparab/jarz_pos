# Copyright (c) 2026, Abdelrahman Mamdouh and contributors
# For license information, please see license.txt

"""Task Board settings: the full-access list.

Anyone on ``full_access_users`` sees and may act on every task on the board,
whatever their roles (Administrator always does). Seeded once, only while the
list is empty, by ``jarz_pos.setup.task_board_setup``.

The list is self-guarding: the DocPerm lets any System Manager open this
Single, and without a check any of them could add themselves and read every
manager-to-manager task. So only Administrator, or someone ALREADY on the
stored list, may change who is on it (migrate/install keep working for the
seeder).
"""

import frappe
from frappe.model.document import Document


def stored_full_access_users():
	"""The list as it is in the database right now (not as submitted)."""
	rows = frappe.db.sql(
		"""
		SELECT `user` FROM `tabJarz Task Settings User`
		WHERE parent = %s AND parenttype = %s AND IFNULL(`user`, '') != ''
		""",
		("Jarz Task Settings", "Jarz Task Settings"),
	)
	return sorted({r[0] for r in rows or [] if r and r[0]})


def may_change_full_access(user, stored_users, flags) -> bool:
	"""Pure rule: Administrator, a current member, or a migrate/install."""
	if user == "Administrator":
		return True
	if getattr(flags, "in_migrate", False) or getattr(flags, "in_install", False):
		return True
	return bool(user) and user in set(stored_users or [])


class JarzTaskSettings(Document):
	def validate(self):
		# One row per user: a duplicate is harmless to the rules but confusing
		# to whoever maintains the list.
		seen = set()
		rows = []
		for row in self.get("full_access_users") or []:
			if not row.user or row.user in seen:
				continue
			seen.add(row.user)
			rows.append(row)
		self.set("full_access_users", rows)

		stored = stored_full_access_users()
		if sorted(seen) != stored and not may_change_full_access(
			frappe.session.user, stored, frappe.flags
		):
			frappe.throw(
				frappe._(
					"Only Administrator or a user already on the full-access list can change it."
				),
				frappe.PermissionError,
			)

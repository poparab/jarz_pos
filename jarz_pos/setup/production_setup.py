"""Production floor setup (Production Board, Stage 2).

Idempotent, create-only configuration seeding for the production-floor role.
Safe to run on every ``bench migrate`` via the ``after_migrate`` hook: every
record is checked with ``frappe.db.exists`` before insert, inserts use
``ignore_permissions=True``, and no existing record is ever overwritten. Each
unit is wrapped so that a single failure logs and the rest continue.

This module must import cleanly with NO top-level frappe calls.

Why a bespoke role at all: an operator needs to *read* Work Orders, BOMs and
Items to run the board, but must never reach the Desk. ``desk_access=0`` is the
whole point of ``Production Operator`` — the API gates in
``api/manufacturing.py`` decide what they may do, this decides what they may
see, and neither grants a login to ERPNext itself.
"""

import frappe

LOGGER_NAME = "production_setup"

ROLE_NAME = "Production Operator"
ROLE_PROFILE_NAME = "Production Floor"

# Read-only, non-negotiable. Every write the floor performs goes through a
# whitelisted endpoint that runs with ``ignore_permissions=True`` after its own
# role check, so handing the role direct write access on Work Order would only
# widen the blast radius of a mistake without enabling anything.
DOCPERM_TARGETS = ("Work Order", "BOM", "Item")


def _logger():
	return frappe.logger(LOGGER_NAME, allow_site=True)


def _ensure_production_role(log):
	"""Ensure the ``Production Operator`` Role exists, with no desk access."""
	try:
		if frappe.db.exists("Role", ROLE_NAME):
			log["existing"].append(f"Role: {ROLE_NAME}")
			return
		doc = frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": ROLE_NAME,
				# Floor tablets talk to the API only; the Desk is not for them.
				"desk_access": 0,
				"disabled": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		log["created"].append(f"Role: {ROLE_NAME}")
	except Exception:
		_logger().error(f"Failed to ensure Role '{ROLE_NAME}'", exc_info=True)


def _ensure_production_role_profile(log):
	"""Bundle the role into a Role Profile so new operators are one pick away."""
	try:
		if not frappe.db.exists("Role", ROLE_NAME):
			_logger().warning(
				f"Skipping Role Profile '{ROLE_PROFILE_NAME}': Role '{ROLE_NAME}' does not exist"
			)
			return
		if frappe.db.exists("Role Profile", ROLE_PROFILE_NAME):
			log["existing"].append(f"Role Profile: {ROLE_PROFILE_NAME}")
			return
		doc = frappe.get_doc(
			{
				"doctype": "Role Profile",
				"role_profile": ROLE_PROFILE_NAME,
				"roles": [{"role": ROLE_NAME}],
			}
		)
		doc.insert(ignore_permissions=True)
		log["created"].append(f"Role Profile: {ROLE_PROFILE_NAME}")
	except Exception:
		_logger().error(
			f"Failed to ensure Role Profile '{ROLE_PROFILE_NAME}'", exc_info=True
		)


def _custom_docperm_roles(doctype):
	return set(frappe.get_all("Custom DocPerm", filters={"parent": doctype}, pluck="role"))


def _standard_docperm_roles(doctype):
	return set(frappe.get_all("DocPerm", filters={"parent": doctype}, pluck="role"))


def _repair_displaced_standard_perms(doctype, log):
	"""Undo the damage described in ``_ensure_production_docperms``.

	Sites that migrated with the earlier version of this seeder have exactly one
	Custom DocPerm row per target doctype — ours — and therefore no working
	permissions for anybody else. Detect that precise signature and clear the
	table so the ensure path below can rebuild it correctly.

	The signature has to be exact. If any role other than ``Production Operator``
	already has a Custom DocPerm row, a human customised this doctype and their
	rows are the real permission set; wiping them would be a second outage on top
	of the first.
	"""
	custom_roles = _custom_docperm_roles(doctype)
	if not custom_roles:
		return
	if custom_roles - {ROLE_NAME}:
		# Somebody else's customisation is in play — hands off.
		return
	if not _standard_docperm_roles(doctype) - {ROLE_NAME}:
		# Nothing was displaced (doctype ships no other roles); leave it alone.
		return

	from frappe.permissions import reset_perms

	reset_perms(doctype)
	log["repaired"].append(doctype)


def _ensure_production_docperms(log):
	"""Grant the role read-only access to the doctypes the board renders.

	Create-only and per-doctype guarded: an admin who has since tightened or
	widened one of these by hand keeps their change, and one failure does not
	stop the others.

	``setup_custom_perms`` is the load-bearing call. Frappe resolves permissions
	in ``get_valid_perms`` by discarding *every* standard DocPerm for any doctype
	that has at least one Custom DocPerm row. So inserting a bare row here — as
	this function used to — does not add a permission, it replaces the doctype's
	entire permission set with the single row we just wrote. That is how one
	read-only grant for the floor role silently revoked Item, BOM and Work Order
	from System Manager and everyone else, surfacing as "Insufficient Permission
	for Item" on any Desk form with an Item link field.

	``setup_custom_perms`` copies the standard rows into Custom DocPerm first
	(a no-op once they are there), so ours is genuinely additive. This is what
	``frappe.permissions.add_permission`` does, and what the Role Permissions
	Manager does; we insert by hand only to pin every flag explicitly.
	"""
	if not frappe.db.exists("Role", ROLE_NAME):
		_logger().warning(f"Skipping Custom DocPerms: Role '{ROLE_NAME}' does not exist")
		return

	for doctype in DOCPERM_TARGETS:
		try:
			if not frappe.db.exists("DocType", doctype):
				# ERPNext not installed / stripped site — not our problem to fix.
				_logger().warning(f"Skipping Custom DocPerm: DocType '{doctype}' not found")
				continue

			_repair_displaced_standard_perms(doctype, log)

			if frappe.db.exists(
				"Custom DocPerm", {"parent": doctype, "role": ROLE_NAME, "permlevel": 0}
			):
				log["existing"].append(f"Custom DocPerm: {ROLE_NAME} on {doctype}")
				continue

			# Preserve the doctype's standard permissions before adding to them.
			from frappe.permissions import setup_custom_perms

			setup_custom_perms(doctype)

			doc = frappe.get_doc(
				{
					"doctype": "Custom DocPerm",
					"parent": doctype,
					"parenttype": "DocType",
					"parentfield": "permissions",
					"role": ROLE_NAME,
					"permlevel": 0,
					"read": 1,
					"write": 0,
					"create": 0,
					"delete": 0,
					"submit": 0,
					"cancel": 0,
					"amend": 0,
					"report": 0,
					"export": 0,
					"import": 0,
					"share": 0,
					"print": 0,
					"email": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			log["created"].append(f"Custom DocPerm: {ROLE_NAME} read on {doctype}")
		except Exception:
			_logger().error(
				f"Failed to ensure Custom DocPerm for '{ROLE_NAME}' on '{doctype}'",
				exc_info=True,
			)
		finally:
			# Permissions are cached per doctype; a repaired table that nobody
			# re-reads is still an outage until the next restart.
			try:
				frappe.clear_cache(doctype=doctype)
			except Exception:
				_logger().error(f"Failed to clear permission cache for '{doctype}'", exc_info=True)


def ensure_production_setup():
	"""Idempotently seed production-floor roles and permissions."""
	log = {"created": [], "existing": [], "repaired": []}
	logger = _logger()

	try:
		# Order matters: the profile and the docperms both reference the role.
		_ensure_production_role(log)
		_ensure_production_role_profile(log)
		_ensure_production_docperms(log)

		if log["repaired"]:
			# Loud on purpose: this means the site had been running with the
			# standard permissions on these doctypes revoked.
			logger.error(
				"Production setup RESTORED displaced standard permissions on: "
				+ ", ".join(log["repaired"])
			)

		if log["created"]:
			logger.info("Production setup created: " + "; ".join(log["created"]))
		else:
			logger.info("Production setup: nothing new to create")

		if log["existing"]:
			logger.info("Production setup already present: " + "; ".join(log["existing"]))
	except Exception:
		# Never let setup seeding break a migrate.
		logger.error("ensure_production_setup failed unexpectedly", exc_info=True)

	return log

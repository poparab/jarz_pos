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


def _ensure_production_docperms(log):
	"""Grant the role read-only access to the doctypes the board renders.

	Create-only and per-doctype guarded: an admin who has since tightened or
	widened one of these by hand keeps their change, and one failure does not
	stop the others.
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
			if frappe.db.exists(
				"Custom DocPerm", {"parent": doctype, "role": ROLE_NAME, "permlevel": 0}
			):
				log["existing"].append(f"Custom DocPerm: {ROLE_NAME} on {doctype}")
				continue
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


def ensure_production_setup():
	"""Idempotently seed production-floor roles and permissions."""
	log = {"created": [], "existing": []}
	logger = _logger()

	try:
		# Order matters: the profile and the docperms both reference the role.
		_ensure_production_role(log)
		_ensure_production_role_profile(log)
		_ensure_production_docperms(log)

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

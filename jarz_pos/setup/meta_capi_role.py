"""The role allowed to approve chat orders for the Meta Conversions API."""

import frappe

ROLE_NAME = "Meta CAPI Manager"


def ensure_meta_capi_role() -> None:
	"""Create the role before DocPerm rows that name it are synced."""
	try:
		if frappe.db.exists("Role", ROLE_NAME):
			return
		frappe.get_doc({"doctype": "Role", "role_name": ROLE_NAME, "desk_access": 1, "disabled": 0}).insert(
			ignore_permissions=True
		)
	except Exception:
		frappe.logger("jarz_pos.setup", allow_site=True).error("ensure_meta_capi_role failed", exc_info=True)

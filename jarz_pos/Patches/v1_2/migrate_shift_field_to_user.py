import frappe


def execute():
    """Migrate custom_require_pos_shift from Employee to User doctype."""
    fieldname = "custom_require_pos_shift"

    # Remove the old Employee-based field if it exists
    old_cf = f"Employee-{fieldname}"
    if frappe.db.exists("Custom Field", old_cf):
        frappe.delete_doc("Custom Field", old_cf, force=True)
        try:
            frappe.clear_cache(doctype="Employee")
        except Exception:
            pass

    # Create on User if not already there
    new_cf = f"User-{fieldname}"
    if frappe.db.exists("Custom Field", new_cf):
        return

    doc = frappe.get_doc(
        {
            "doctype": "Custom Field",
            "dt": "User",
            "fieldname": fieldname,
            "label": "Require POS Shift",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "role_profile_name",
            "description": "If enabled, this user must start/end a POS shift before using POS.",
        }
    )
    doc.insert(ignore_permissions=True)

    try:
        frappe.clear_cache(doctype="User")
    except Exception:
        pass

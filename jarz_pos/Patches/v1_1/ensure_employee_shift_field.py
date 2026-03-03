import frappe


def execute():
    """Ensure Employee has custom_require_pos_shift custom field."""
    fieldname = "custom_require_pos_shift"
    custom_field_name = f"Employee-{fieldname}"

    if frappe.db.exists("Custom Field", custom_field_name):
        return

    insert_after = "branch"
    if not frappe.db.has_column("Employee", insert_after):
        insert_after = "reports_to"

    doc = frappe.get_doc(
        {
            "doctype": "Custom Field",
            "dt": "Employee",
            "fieldname": fieldname,
            "label": "Require POS Shift",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": insert_after,
            "description": "If enabled, this employee must start/end POS shift before using POS.",
        }
    )
    doc.insert(ignore_permissions=True)

    try:
        frappe.clear_cache(doctype="Employee")
    except Exception:
        pass

import frappe


def execute():
    """Ensure bundle metadata fields exist on Sales Invoice Item."""
    fields = [
        {
            "fieldname": "is_bundle_parent",
            "label": "Is Bundle Parent",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "discount_percentage",
        },
        {
            "fieldname": "is_bundle_child",
            "label": "Is Bundle Child",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "is_bundle_parent",
        },
        {
            "fieldname": "bundle_code",
            "label": "Bundle Code",
            "fieldtype": "Data",
            "insert_after": "is_bundle_child",
        },
        {
            "fieldname": "parent_bundle",
            "label": "Parent Bundle",
            "fieldtype": "Data",
            "insert_after": "bundle_code",
        },
        {
            "fieldname": "bundle_group_key",
            "label": "Bundle Group Key",
            "fieldtype": "Data",
            "insert_after": "parent_bundle",
        },
        {
            "fieldname": "bundle_group_name",
            "label": "Bundle Group Name",
            "fieldtype": "Data",
            "insert_after": "bundle_group_key",
        },
    ]

    for field in fields:
        fieldname = field["fieldname"]
        if frappe.db.exists(
            "Custom Field",
            {"dt": "Sales Invoice Item", "fieldname": fieldname},
        ):
            continue

        frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Sales Invoice Item",
                **field,
            }
        ).insert(ignore_permissions=True)

    frappe.clear_cache(doctype="Sales Invoice Item")
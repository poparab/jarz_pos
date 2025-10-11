import frappe


def execute():
    """Create the required_delivery_datetime custom field for Sales Invoice"""

    # Check if the custom field already exists
    existing_field = frappe.db.get_value("Custom Field", {
        "dt": "Sales Invoice",
        "fieldname": "required_delivery_datetime"
    }, "name")

    if existing_field:
        print(f"Custom field already exists: {existing_field}")
        # Delete the existing field to recreate it with correct settings
        frappe.delete_doc("Custom Field", existing_field)
        print("Deleted existing custom field")

    # Create the custom field with proper settings
    custom_field = frappe.get_doc({
        "doctype": "Custom Field",
        "dt": "Sales Invoice",
        "label": "Required Delivery Datetime",
        "fieldname": "required_delivery_datetime",
        "fieldtype": "Datetime",
        "insert_after": "due_date",
        "allow_on_submit": 1,  # Allow editing after submission
        "no_copy": 1,  # Don't copy when duplicating
        "print_hide": 0,  # Show in print
        "reqd": 0,  # Not required
        "hidden": 0,  # Not hidden
        "read_only": 0,  # Not read-only
        "description": "Required delivery date and time for this order"
    })

    custom_field.insert(ignore_permissions=True)
    print(f"Created custom field: {custom_field.name}")

    # Commit the changes
    frappe.db.commit()
    print("Custom field created successfully")

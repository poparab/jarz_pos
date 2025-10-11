import frappe


def execute():
    """Create the sales_invoice_state custom field for Sales Invoice"""

    fieldname = "sales_invoice_state"
    doctype = "Sales Invoice"

    existing = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname}, "name")  # type: ignore[attr-defined]

    if existing:
        print(f"Custom Field {fieldname} already exists: {existing}")
        return

    options = "Received\nProcessing\nPreparing\nOut for delivery\nCompleted"

    cf = frappe.get_doc({  # type: ignore[attr-defined]
        "doctype": "Custom Field",
        "dt": doctype,
        "label": "Sales Invoice State",
        "fieldname": fieldname,
        "fieldtype": "Select",
        "options": options,
        "insert_after": "required_delivery_datetime" or "due_date",
        "allow_on_submit": 1,
        "no_copy": 1,
        "print_hide": 0,
        "reqd": 0,
        "hidden": 0,
        "read_only": 0,
        "default": "Received",
        "description": "Operational state of the invoice for delivery workflow"
    })

    cf.insert(ignore_permissions=True)  # type: ignore[attr-defined]
    frappe.db.commit()  # type: ignore[attr-defined]

    print(f"✅ Created custom field {cf.name} on {doctype}")

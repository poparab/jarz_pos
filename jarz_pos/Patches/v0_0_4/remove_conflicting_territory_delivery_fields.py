import frappe

def execute():
    # Remove any existing Territory custom fields that will be recreated by fixtures
    fields = frappe.get_all(
        "Custom Field",
        filters={"dt": "Territory", "fieldname": ["in", ["delivery_income", "delivery_expense"]]},
        pluck="name",
    )
    for name in fields:
        try:
            frappe.delete_doc("Custom Field", name, force=1, ignore_permissions=True)
        except Exception:
            frappe.db.rollback()
    frappe.db.commit()

import frappe


def execute():
    if not frappe.db.exists("Custom Field", {"dt":"Address", "fieldname":"gps_location"}):
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": "Address",
            "label": "GPS Location",
            "fieldname": "gps_location",
            "fieldtype": "Data",
            "insert_after": "city",
        }).insert(ignore_permissions=True)

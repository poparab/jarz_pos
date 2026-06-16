import frappe


@frappe.whitelist()
def get_context(context):
	context.title = "Inventory Intelligence"
	return context

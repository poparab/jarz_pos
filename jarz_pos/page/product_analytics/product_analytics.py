import frappe


@frappe.whitelist()
def get_context(context):
	context.title = "Product Analytics"
	return context

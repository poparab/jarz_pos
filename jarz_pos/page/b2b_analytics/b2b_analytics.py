import frappe


@frappe.whitelist()
def get_context(context):
	context.title = "B2B Sales & Clients"
	return context

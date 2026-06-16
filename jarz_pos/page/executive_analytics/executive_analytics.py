import frappe


@frappe.whitelist()
def get_context(context):
	context.title = "Executive Overview"
	return context

import frappe


@frappe.whitelist()
def get_context(context):
	context.title = "Revenue Dashboard"
	return context

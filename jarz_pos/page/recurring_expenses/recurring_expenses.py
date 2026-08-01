import frappe


@frappe.whitelist()
def get_context(context):
	context.title = "Recurring Expenses"
	return context

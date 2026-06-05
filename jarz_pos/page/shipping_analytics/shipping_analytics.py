import frappe


@frappe.whitelist()
def get_context(context):
    context.title = "Shipping Analytics"
    return context

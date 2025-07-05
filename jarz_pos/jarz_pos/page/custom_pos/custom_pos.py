import frappe


@frappe.whitelist()
def get_context(context):
    """Page context for custom POS"""
    context.title = "Jarz POS"
    return context


def get_permission_query_conditions(user):
    """Permission check for accessing the page"""
    return ""


def has_permission(doc, user):
    """Check if user has permission to access the page"""
    return True
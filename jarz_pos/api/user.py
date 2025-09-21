import frappe


@frappe.whitelist(allow_guest=False)
def get_current_user_roles():
    """
    Return the current session user's roles and a convenience boolean for JARZ Manager.

    Response shape:
    {
        "user": "user@example.com",
        "full_name": "Full Name",
        "roles": ["Role 1", "Role 2", ...],
        "is_jarz_manager": true|false
    }
    """
    user = frappe.session.user
    roles = frappe.get_roles(user)
    full_name = frappe.db.get_value("User", user, "full_name") if user else None
    return {
        "user": user,
        "full_name": full_name,
        "roles": roles,
        "is_jarz_manager": "JARZ Manager" in roles,
    }

import frappe


def remove_conflicting_territory_delivery_fields():
    """
    Remove existing Custom Fields in Territory that conflict with fixtures:
    - delivery_income
    - delivery_expense
    Returns a dict with counts of deleted docs.
    """

    to_delete = frappe.get_all(
        "Custom Field",
        filters={
            "dt": "Territory",
            "fieldname": ["in", ["delivery_income", "delivery_expense"]],
        },
        pluck="name",
    )

    deleted = []
    for name in to_delete:
        try:
            frappe.delete_doc("Custom Field", name, force=1, ignore_permissions=True)
            deleted.append(name)
        except Exception:
            frappe.db.rollback()

    if deleted:
        frappe.db.commit()
    return {"deleted": deleted, "count": len(deleted)}


def before_migrate():
    """Hook: called via hooks.before_migrate to ensure fixtures import succeeds."""
    try:
        remove_conflicting_territory_delivery_fields()
    except Exception:
        # Best-effort cleanup; ignore errors so migrate proceeds
        pass

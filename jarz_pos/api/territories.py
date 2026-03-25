"""
API endpoints for Territory / Sub-territory management.

Provides sub-territory lookup and assignment to Sales Invoices.
"""

import frappe
from frappe import _


@frappe.whitelist(allow_guest=False)
def get_sub_territories(territory_name: str):
    """Return child territories of the given territory with delivery costs.

    Args:
        territory_name: Parent territory name to look up children for.

    Returns:
        dict with ``success`` flag and ``data`` list of child territory dicts.
    """
    if not territory_name or not frappe.db.exists("Territory", territory_name):
        return {"success": False, "message": f"Territory '{territory_name}' not found"}

    children = frappe.get_all(
        "Territory",
        filters={"parent_territory": territory_name},
        fields=["name", "territory_name", "delivery_income", "delivery_expense"],
        order_by="territory_name asc",
    )

    return {
        "success": True,
        "data": [
            {
                "name": c.name,
                "territory_name": c.territory_name,
                "delivery_income": float(c.delivery_income or 0),
                "delivery_expense": float(c.delivery_expense or 0),
            }
            for c in children
        ],
    }


@frappe.whitelist(allow_guest=False)
def set_invoice_sub_territory(invoice_name: str, sub_territory: str):
    """Assign a sub-territory to a Sales Invoice.

    Validates that the sub-territory is a valid child of the invoice's territory.

    Args:
        invoice_name: Sales Invoice name.
        sub_territory: Territory name to assign as sub-territory.

    Returns:
        dict with success flag and updated shipping expense.
    """
    if not frappe.db.exists("Sales Invoice", invoice_name):
        frappe.throw(_("Sales Invoice {0} not found").format(invoice_name))

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw(_("Only submitted Sales Invoices can have sub-territories assigned"))

    invoice_territory = (inv.territory or "").strip()
    if not invoice_territory:
        frappe.throw(_("Invoice has no territory set"))

    if not frappe.db.exists("Territory", sub_territory):
        frappe.throw(_("Sub-territory '{0}' not found").format(sub_territory))

    # Validate parent-child relationship
    parent_of_sub = frappe.db.get_value("Territory", sub_territory, "parent_territory")
    if parent_of_sub != invoice_territory:
        frappe.throw(
            _("'{0}' is not a sub-territory of '{1}'").format(sub_territory, invoice_territory)
        )

    # Set sub-territory on invoice
    frappe.db.set_value(
        "Sales Invoice", invoice_name, "custom_sub_territory", sub_territory, update_modified=True
    )

    # Return the delivery expense from the sub-territory
    expense = float(frappe.db.get_value("Territory", sub_territory, "delivery_expense") or 0)
    income = float(frappe.db.get_value("Territory", sub_territory, "delivery_income") or 0)

    # Update custom_shipping_expense on the SI so all downstream reads use it,
    # unless there is an approved custom shipping override in place.
    override_status = (
        frappe.db.get_value("Sales Invoice", invoice_name, "custom_shipping_override_status") or ""
    )
    if override_status != "Approved" and expense > 0:
        frappe.db.set_value(
            "Sales Invoice", invoice_name, "custom_shipping_expense", expense, update_modified=False
        )

    frappe.db.commit()

    return {
        "success": True,
        "sub_territory": sub_territory,
        "delivery_expense": expense,
        "delivery_income": income,
    }


def territory_has_children(territory_name: str) -> bool:
    """Check if a territory has any child territories.

    Args:
        territory_name: Territory name to check.

    Returns:
        True if territory has children, False otherwise.
    """
    if not territory_name:
        return False
    return bool(
        frappe.db.exists("Territory", {"parent_territory": territory_name})
    )

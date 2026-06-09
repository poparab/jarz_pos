"""
Post-migrate utility: ensure Jarz Forecast Settings shortcut exists
in the JARZ POS workspace and DocType permissions are current.
"""
import frappe


WORKSPACE_NAME = "JARZ POS"
SHORTCUT_LINK = "Jarz Forecast Settings"

SHORTCUTS_TO_ADD = [
    {
        "label": "Inventory Forecast",
        "link_to": "Jarz Forecast Settings",
        "type": "DocType",
        "icon": "fa fa-bar-chart",
        "color": "#27ae60",
    },
]


def ensure_forecast_workspace_shortcuts():
    """
    Idempotent: add Jarz Forecast Settings shortcut to JARZ POS workspace
    if it isn't already there. Called from after_migrate hook.
    """
    if not frappe.db.exists("Workspace", WORKSPACE_NAME):
        return

    ws = frappe.get_doc("Workspace", WORKSPACE_NAME)
    existing_links = {s.link_to for s in (ws.shortcuts or [])}

    changed = False
    for sc in SHORTCUTS_TO_ADD:
        if sc["link_to"] not in existing_links:
            ws.append("shortcuts", sc)
            changed = True

    if changed:
        ws.flags.ignore_permissions = True
        ws.save()
        frappe.db.commit()
        frappe.logger().info(
            "[Forecast] Added Inventory Forecast shortcut to JARZ POS workspace"
        )

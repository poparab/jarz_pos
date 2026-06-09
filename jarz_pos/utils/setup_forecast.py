"""
Post-migrate utility: ensure the JARZ POS workspace exists with the right shortcuts
including Jarz Forecast Settings. Idempotent — safe to run multiple times.
"""
import frappe


WORKSPACE_NAME = "JARZ POS"

# Full workspace definition — used when creating from scratch
WORKSPACE_DEFINITION = {
    "name": WORKSPACE_NAME,
    "module": "jarz pos",
    "category": "Modules",
    "public": 1,
    "icon": "fa fa-shopping-cart",
    "color": "#FF6B35",
    "sequence_id": 1,
}

# All shortcuts that belong in the workspace (order matters)
ALL_SHORTCUTS = [
    {
        "label": "Sales Invoice List",
        "link_to": "Sales Invoice",
        "type": "DocType",
        "icon": "fa fa-file-text",
        "color": "#3498db",
    },
    {
        "label": "POS Profile",
        "link_to": "POS Profile",
        "type": "DocType",
        "icon": "fa fa-cog",
        "color": "#e74c3c",
    },
    {
        "label": "Shipping Analytics",
        "link_to": "shipping-analytics",
        "type": "Page",
        "icon": "fa fa-truck",
        "color": "#FF6B35",
    },
    {
        "label": "Inventory Forecast",
        "link_to": "Jarz Forecast Settings",
        "type": "DocType",
        "icon": "fa fa-bar-chart",
        "color": "#27ae60",
    },
]


def ensure_jarz_pos_workspace():
    """
    Create or update the JARZ POS workspace.
    - Creates it if it doesn't exist.
    - Adds any missing shortcuts if it already exists.
    Called from after_migrate hook.
    """
    if not frappe.db.exists("Workspace", WORKSPACE_NAME):
        _create_workspace()
    else:
        _patch_workspace_shortcuts()


# Alias used by hooks.py
ensure_forecast_workspace_shortcuts = ensure_jarz_pos_workspace


def _create_workspace():
    """Create the JARZ POS workspace from scratch."""
    ws = frappe.new_doc("Workspace")
    ws.update(WORKSPACE_DEFINITION)
    for sc in ALL_SHORTCUTS:
        ws.append("shortcuts", sc)
    ws.flags.ignore_permissions = True
    ws.insert()
    frappe.db.commit()
    frappe.logger().info("[Forecast] Created JARZ POS workspace with all shortcuts")


def _patch_workspace_shortcuts():
    """Add any missing shortcuts to an existing workspace."""
    ws = frappe.get_doc("Workspace", WORKSPACE_NAME)
    existing_links = {s.link_to for s in (ws.shortcuts or [])}

    changed = False
    for sc in ALL_SHORTCUTS:
        if sc["link_to"] not in existing_links:
            ws.append("shortcuts", sc)
            changed = True

    if changed:
        ws.flags.ignore_permissions = True
        ws.save()
        frappe.db.commit()
        frappe.logger().info(
            "[Forecast] Patched JARZ POS workspace — added missing shortcuts"
        )

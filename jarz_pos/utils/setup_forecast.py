"""
Post-migrate utility: ensure the JARZ POS workspace exists with all shortcuts.
Handles Frappe v15 workspace format where shortcuts are stored as JSON in the
`content` field (not as child-table records).
Idempotent — safe to run multiple times.
"""
import json
import frappe


WORKSPACE_NAME = "JARZ POS"

# Shortcut block entries for the workspace content (Frappe v15 format)
# Each shortcut_name maps to a DocType name or Page name.
SHORTCUT_BLOCKS = [
    {"id": "jarz_sc_01", "type": "shortcut", "data": {"shortcut_name": "Sales Invoice",         "col": 3}},
    {"id": "jarz_sc_02", "type": "shortcut", "data": {"shortcut_name": "POS Profile",            "col": 3}},
    {"id": "jarz_sc_03", "type": "shortcut", "data": {"shortcut_name": "Jarz Forecast Settings", "col": 3}},
]


def ensure_jarz_pos_workspace():
    """
    Create or update the JARZ POS workspace.
    - Creates it from scratch if it doesn't exist.
    - Adds Jarz Forecast Settings shortcut if the workspace exists but lacks it.
    Called from after_migrate hook.
    """
    if not frappe.db.exists("Workspace", WORKSPACE_NAME):
        _create_workspace()
    else:
        _patch_workspace_shortcuts()


# Alias used by older hook registration
ensure_forecast_workspace_shortcuts = ensure_jarz_pos_workspace


def _create_workspace():
    """Create the JARZ POS workspace with all shortcuts (Frappe v15 content-JSON format)."""
    ws = frappe.new_doc("Workspace")
    ws.name   = WORKSPACE_NAME
    ws.title  = WORKSPACE_NAME
    ws.label  = WORKSPACE_NAME   # mandatory in Frappe v15
    ws.module = "jarz pos"
    ws.public = 1
    ws.is_hidden  = 0
    ws.hide_custom = 0
    ws.sequence_id = 100
    ws.icon        = "shopping-cart"
    ws.content     = json.dumps(SHORTCUT_BLOCKS)
    ws.flags.ignore_mandatory  = True
    ws.flags.ignore_permissions = True
    ws.insert(set_name=WORKSPACE_NAME)
    frappe.db.commit()
    frappe.logger().info("[Forecast] Created JARZ POS workspace with shortcuts")


def _patch_workspace_shortcuts():
    """Add Jarz Forecast Settings shortcut to an existing workspace if absent."""
    content_raw = frappe.db.get_value("Workspace", WORKSPACE_NAME, "content") or "[]"
    try:
        blocks = json.loads(content_raw)
    except (ValueError, TypeError):
        blocks = []

    existing_shortcuts = {
        b["data"].get("shortcut_name")
        for b in blocks
        if b.get("type") == "shortcut" and isinstance(b.get("data"), dict)
    }

    to_add = [b for b in SHORTCUT_BLOCKS if b["data"]["shortcut_name"] not in existing_shortcuts]
    if not to_add:
        return

    blocks.extend(to_add)
    frappe.db.set_value(
        "Workspace", WORKSPACE_NAME, "content", json.dumps(blocks),
        update_modified=False
    )
    frappe.db.commit()
    added = [b["data"]["shortcut_name"] for b in to_add]
    frappe.logger().info("[Forecast] Patched JARZ POS workspace — added: %s", added)

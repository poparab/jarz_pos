"""
Post-migrate utility: ensure the JARZ POS workspace exists with all shortcuts.

Handles the Frappe v15/v16 workspace format where the layout lives as JSON
blocks in the ``content`` field while the shortcut definitions live in the
``shortcuts`` child table.  Both must agree for a shortcut to render, so this
helper keeps the two in sync.

Idempotent and defensive — any failure is logged, never raised, so a cosmetic
workspace issue can never break ``bench migrate`` / deployment.
"""
import json
import frappe


WORKSPACE_NAME = "JARZ POS"

# Full shortcut definitions: each becomes a row in the ``shortcuts`` child
# table AND a ``shortcut`` block in the ``content`` JSON.  Order here is the
# display order.  ``link_to`` is a DocType name or a Page route.
SHORTCUTS = [
    {"label": "Executive Overview",      "type": "Page",    "link_to": "executive-analytics",    "color": "#FF6B35", "icon": "dashboard"},
    {"label": "Product Analytics",       "type": "Page",    "link_to": "product-analytics",      "color": "#7B61FF", "icon": "box"},
    {"label": "Shipping Analytics",      "type": "Page",    "link_to": "shipping-analytics",     "color": "#FF6B35", "icon": "truck"},
    {"label": "Customer Analytics",      "type": "Page",    "link_to": "customer-analytics",     "color": "#2980b9", "icon": "users"},
    {"label": "Inventory Intelligence",  "type": "Page",    "link_to": "inventory-analytics",    "color": "#16a085", "icon": "stock"},
    {"label": "Sales Invoice",           "type": "DocType", "link_to": "Sales Invoice",          "color": "#3498db", "icon": "file"},
    {"label": "POS Profile",             "type": "DocType", "link_to": "POS Profile",            "color": "#e74c3c", "icon": "setting-gear"},
    {"label": "Jarz Forecast Settings",  "type": "DocType", "link_to": "Jarz Forecast Settings", "color": "#27ae60", "icon": "bar-chart"},
]


def ensure_jarz_pos_workspace():
    """Create or sync the JARZ POS workspace. Called from the after_migrate hook."""
    try:
        if not frappe.db.exists("Workspace", WORKSPACE_NAME):
            _create_workspace()
        else:
            _sync_workspace()
        frappe.db.commit()
    except Exception:
        # Never let a cosmetic workspace problem fail a migration / deploy.
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "ensure_jarz_pos_workspace failed")


# Alias used by the existing hook registration in hooks.py (after_migrate).
ensure_forecast_workspace_shortcuts = ensure_jarz_pos_workspace


def _content_blocks(labels):
    """Build the ``content`` JSON: a header + one shortcut block per label."""
    blocks = [{
        "id": "jarz_hdr",
        "type": "header",
        "data": {"text": "<span class=\"h4\"><b>JARZ POS</b></span>", "col": 12},
    }]
    for i, label in enumerate(labels):
        blocks.append({
            "id": f"jarz_sc_{i:02d}",
            "type": "shortcut",
            "data": {"shortcut_name": label, "col": 3},
        })
    return blocks


def _append_shortcut(ws, sc):
    ws.append("shortcuts", {
        "label": sc["label"],
        "type": sc["type"],
        "link_to": sc["link_to"],
        "color": sc.get("color"),
        "icon": sc.get("icon"),
    })


def _create_workspace():
    """Create the JARZ POS workspace with all shortcuts."""
    ws = frappe.new_doc("Workspace")
    ws.name = WORKSPACE_NAME
    ws.title = WORKSPACE_NAME
    ws.label = WORKSPACE_NAME          # mandatory in v15+
    ws.module = "jarz pos"
    ws.public = 1
    ws.is_hidden = 0
    ws.hide_custom = 0
    ws.sequence_id = 100
    ws.icon = "shopping-cart"
    for sc in SHORTCUTS:
        _append_shortcut(ws, sc)
    ws.content = json.dumps(_content_blocks([s["label"] for s in SHORTCUTS]))
    ws.flags.ignore_mandatory = True
    ws.flags.ignore_permissions = True
    ws.insert(set_name=WORKSPACE_NAME)
    frappe.logger().info("[Analytics] Created JARZ POS workspace with %s shortcuts", len(SHORTCUTS))


def _sync_workspace():
    """Add any missing shortcuts (child rows + content blocks) to an existing workspace."""
    ws = frappe.get_doc("Workspace", WORKSPACE_NAME)
    changed = False

    # 1. Ensure each shortcut exists in the child table.
    existing = {s.label for s in ws.shortcuts}
    for sc in SHORTCUTS:
        if sc["label"] not in existing:
            _append_shortcut(ws, sc)
            changed = True

    # 2. Ensure each shortcut is referenced in the content layout.
    try:
        blocks = json.loads(ws.content or "[]")
    except (ValueError, TypeError):
        blocks = []

    referenced = {
        b["data"].get("shortcut_name")
        for b in blocks
        if b.get("type") == "shortcut" and isinstance(b.get("data"), dict)
    }
    next_idx = len(blocks)
    for sc in SHORTCUTS:
        if sc["label"] not in referenced:
            blocks.append({
                "id": f"jarz_sc_x{next_idx:02d}",
                "type": "shortcut",
                "data": {"shortcut_name": sc["label"], "col": 3},
            })
            next_idx += 1
            changed = True

    if not changed:
        return

    ws.content = json.dumps(blocks)
    ws.flags.ignore_mandatory = True
    ws.flags.ignore_permissions = True
    ws.save()
    frappe.logger().info("[Analytics] Synced JARZ POS workspace shortcuts")

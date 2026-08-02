"""Post-migrate utility: add the Production SOP shortcut to the JARZ POS workspace.

The ``workspaces`` list in ``hooks.py`` only applies when a workspace is first
created, so on every site that already has the JARZ POS workspace a new
shortcut has to be appended after the fact.  Same two-place dance as
``setup_forecast``: a row in the ``shortcuts`` child table **and** a matching
block in the ``content`` JSON, because a shortcut missing from either one does
not render.

Idempotent and defensive — any failure is logged, never raised, so a cosmetic
workspace problem can never break ``bench migrate`` or a deploy.
"""

from __future__ import annotations

import json

import frappe

# Same workspace ``setup_forecast`` maintains.  Defined locally rather than
# imported so the two modules stay independently runnable.
WORKSPACE_NAME = "JARZ POS"

SHORTCUTS = [
    {
        "label": "Production SOPs",
        "type": "DocType",
        "link_to": "Jarz SOP",
        "color": "#e67e22",
        "icon": "list",
    },
]


def ensure_production_workspace_shortcuts():
    """Append the production shortcuts to the existing workspace.

    Registered from the ``after_migrate`` hook.  If the workspace does not
    exist yet, ``setup_forecast`` is asked to create it first — that helper is
    itself idempotent, so ordering between the two hooks does not matter.
    """
    try:
        if not frappe.db.exists("Workspace", WORKSPACE_NAME):
            from jarz_pos.utils.setup_forecast import ensure_jarz_pos_workspace

            ensure_jarz_pos_workspace()

        if not frappe.db.exists("Workspace", WORKSPACE_NAME):
            frappe.log_error(
                title="ensure_production_workspace_shortcuts skipped",
                message=f"Workspace {WORKSPACE_NAME} does not exist",
            )
            return

        if _sync_shortcuts():
            frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "ensure_production_workspace_shortcuts failed")


def _sync_shortcuts() -> bool:
    """Add any missing shortcut rows and content blocks. Returns True if saved."""
    ws = frappe.get_doc("Workspace", WORKSPACE_NAME)
    changed = False

    existing = {s.label for s in (ws.shortcuts or [])}
    for shortcut in SHORTCUTS:
        if shortcut["label"] in existing:
            continue
        ws.append(
            "shortcuts",
            {
                "label": shortcut["label"],
                "type": shortcut["type"],
                "link_to": shortcut["link_to"],
                "color": shortcut.get("color"),
                "icon": shortcut.get("icon"),
            },
        )
        changed = True

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
    for shortcut in SHORTCUTS:
        if shortcut["label"] in referenced:
            continue
        blocks.append(
            {
                "id": f"jarz_prod_sc{next_idx:02d}",
                "type": "shortcut",
                "data": {"shortcut_name": shortcut["label"], "col": 3},
            }
        )
        next_idx += 1
        changed = True

    if not changed:
        return False

    ws.content = json.dumps(blocks)
    ws.flags.ignore_mandatory = True
    ws.flags.ignore_permissions = True
    ws.save()
    frappe.logger().info("[Production] Synced JARZ POS workspace SOP shortcuts")
    return True

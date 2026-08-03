"""Canonical builder for the JARZ POS desk workspace.

Everything Jarz adds to the Desk — the analytics pages, the POS and delivery
masters, the settings singles — hangs off this one workspace, so the Jarz entry
in the v16 sidebar is a real landing page rather than a stub.

It replaces two earlier helpers. ``setup_forecast`` created the workspace and
``setup_production`` appended one more shortcut to it; neither ever removed
anything and neither ever wrote the ``links`` child table, which is why the
page had drifted into a flat strip of eleven tiles and no card sections at all,
while ~25 Jarz DocTypes were reachable only by typing their name into awesomebar.

This module rebuilds ``shortcuts``, ``links`` and ``content`` from the
declarations below on every ``bench migrate``. That is deliberate: the
declarations are the source of truth, so a target removed here disappears from
the Desk on the next deploy instead of lingering. The trade-off is that manual
edits made through the workspace editor are overwritten — change the lists in
this file instead.

Every target is existence-checked before it is emitted, and a card whose links
all vanish is dropped with it. That is what lets one definition serve sites
mid-rollout, where a DocType exists in code but has not yet been migrated onto
that particular site.

Idempotent and defensive: any failure is logged and swallowed, because a
cosmetic workspace problem must never fail ``bench migrate`` or a deploy.
"""

from __future__ import annotations

import json

import frappe

WORKSPACE_NAME = "JARZ POS"

# ``Workspace`` autonames on ``field:label``, so the label IS the document name
# and the route (``/desk/jarz-pos``). Leave it alone unless you intend to rename
# the workspace and break every existing bookmark and the ``app_home`` hook.
WORKSPACE_LABEL = "JARZ POS"
WORKSPACE_MODULE = "jarz pos"
WORKSPACE_ICON = "shopping-cart"

# Sorts the workspace to the top of the sidebar. It sat at 100 — below every
# stock ERPNext and HR workspace — which is the whole reason it read as missing.
WORKSPACE_SEQUENCE = 1

# Tiles across the top of the page. ``type`` is DocType | Page | Report.
SHORTCUTS = [
    {"label": "Executive Overview", "type": "Page", "link_to": "executive-analytics", "color": "#FF6B35", "icon": "dashboard"},
    {"label": "Product Analytics", "type": "Page", "link_to": "product-analytics", "color": "#7B61FF", "icon": "box"},
    {"label": "Shipping Analytics", "type": "Page", "link_to": "shipping-analytics", "color": "#FF6B35", "icon": "truck"},
    {"label": "Customer Analytics", "type": "Page", "link_to": "customer-analytics", "color": "#2980b9", "icon": "users"},
    {"label": "Inventory Intelligence", "type": "Page", "link_to": "inventory-analytics", "color": "#16a085", "icon": "stock"},
    {"label": "B2B Sales & Clients", "type": "Page", "link_to": "b2b-analytics", "color": "#2980b9", "icon": "users"},
    {"label": "Recurring Expenses", "type": "Page", "link_to": "recurring-expenses", "color": "#8e44ad", "icon": "payments"},
    {"label": "Sales Invoice", "type": "DocType", "link_to": "Sales Invoice", "color": "#3498db", "icon": "file"},
    {"label": "POS Profile", "type": "DocType", "link_to": "POS Profile", "color": "#e74c3c", "icon": "setting-gear"},
    {"label": "Jarz POS Settings", "type": "DocType", "link_to": "Jarz POS Settings", "color": "#34495e", "icon": "setting-gear"},
    {"label": "Jarz Forecast Settings", "type": "DocType", "link_to": "Jarz Forecast Settings", "color": "#27ae60", "icon": "bar-chart"},
    {"label": "Production SOPs", "type": "DocType", "link_to": "Jarz SOP", "color": "#e67e22", "icon": "list"},
]

# Card sections rendered under a "Reports & Masters" header, three to a row.
# ``link_type`` is DocType | Page | Report.
#
# ``custom-pos`` is deliberately absent: the Page record exists but carries no
# title, script or content, so linking it would render a tile that opens a blank
# screen.
CARDS = [
    {
        "label": "Dashboards",
        "links": [
            {"label": "Executive Overview", "link_type": "Page", "link_to": "executive-analytics"},
            {"label": "Product Analytics", "link_type": "Page", "link_to": "product-analytics"},
            {"label": "Shipping Analytics", "link_type": "Page", "link_to": "shipping-analytics"},
            {"label": "Customer Analytics", "link_type": "Page", "link_to": "customer-analytics"},
            {"label": "Inventory Intelligence", "link_type": "Page", "link_to": "inventory-analytics"},
            {"label": "B2B Sales & Clients", "link_type": "Page", "link_to": "b2b-analytics"},
            {"label": "Recurring Expenses", "link_type": "Page", "link_to": "recurring-expenses"},
        ],
    },
    {
        "label": "POS Operations",
        "links": [
            {"label": "Sales Invoice", "link_type": "DocType", "link_to": "Sales Invoice"},
            {"label": "POS Profile", "link_type": "DocType", "link_to": "POS Profile"},
            {"label": "POS Profile Timetable", "link_type": "DocType", "link_to": "POS Profile Timetable"},
            {"label": "POS Payment Receipt", "link_type": "DocType", "link_to": "POS Payment Receipt"},
            {"label": "Jarz Invoice Note", "link_type": "DocType", "link_to": "Jarz Invoice Note"},
            {"label": "POS Opening Entry", "link_type": "DocType", "link_to": "POS Opening Entry"},
            {"label": "POS Closing Entry", "link_type": "DocType", "link_to": "POS Closing Entry"},
        ],
    },
    {
        "label": "Delivery & Couriers",
        "links": [
            {"label": "Delivery Partner", "link_type": "DocType", "link_to": "Delivery Partner"},
            {"label": "Delivery Trip", "link_type": "DocType", "link_to": "Delivery Trip"},
            {"label": "Courier Transaction", "link_type": "DocType", "link_to": "Courier Transaction"},
            {"label": "Custom Shipping Request", "link_type": "DocType", "link_to": "Custom Shipping Request"},
            {"label": "Sales Partner Transactions", "link_type": "DocType", "link_to": "Sales Partner Transactions"},
            {"label": "City", "link_type": "DocType", "link_to": "City"},
            {"label": "Territory", "link_type": "DocType", "link_to": "Territory"},
        ],
    },
    {
        "label": "Catalog & Inventory",
        "links": [
            {"label": "Jarz Bundle", "link_type": "DocType", "link_to": "Jarz Bundle"},
            {"label": "Item", "link_type": "DocType", "link_to": "Item"},
            {"label": "Item Group", "link_type": "DocType", "link_to": "Item Group"},
            {"label": "Warehouse Count Profile", "link_type": "DocType", "link_to": "Warehouse Count Profile"},
        ],
    },
    {
        "label": "Promotions",
        "links": [
            {"label": "Jarz Promo Code", "link_type": "DocType", "link_to": "Jarz Promo Code"},
            {"label": "Jarz Promo Redemption", "link_type": "DocType", "link_to": "Jarz Promo Redemption"},
            {"label": "Jarz Promotion Rule", "link_type": "DocType", "link_to": "Jarz Promotion Rule"},
        ],
    },
    {
        "label": "B2B & Pricing",
        "links": [
            {"label": "Jarz Commercial Policy", "link_type": "DocType", "link_to": "Jarz Commercial Policy"},
            {"label": "Jarz Price List Category Rate", "link_type": "DocType", "link_to": "Jarz Price List Category Rate"},
            {"label": "Price List", "link_type": "DocType", "link_to": "Price List"},
            {"label": "Customer", "link_type": "DocType", "link_to": "Customer"},
        ],
    },
    {
        "label": "CRM & Leads",
        "links": [
            {"label": "Lead", "link_type": "DocType", "link_to": "Lead"},
            {"label": "Opportunity", "link_type": "DocType", "link_to": "Opportunity"},
            {"label": "Jarz Lead Category", "link_type": "DocType", "link_to": "Jarz Lead Category"},
        ],
    },
    {
        "label": "Production",
        "links": [
            {"label": "Jarz SOP", "link_type": "DocType", "link_to": "Jarz SOP"},
            {"label": "Jarz SOP Execution Log", "link_type": "DocType", "link_to": "Jarz SOP Execution Log"},
            {"label": "Work Order", "link_type": "DocType", "link_to": "Work Order"},
        ],
    },
    {
        "label": "Expenses",
        "links": [
            {"label": "Jarz Expense Request", "link_type": "DocType", "link_to": "Jarz Expense Request"},
            {"label": "Jarz Recurring Expense", "link_type": "DocType", "link_to": "Jarz Recurring Expense"},
        ],
    },
    {
        "label": "Settings",
        "links": [
            {"label": "Jarz POS Settings", "link_type": "DocType", "link_to": "Jarz POS Settings"},
            {"label": "Jarz Forecast Settings", "link_type": "DocType", "link_to": "Jarz Forecast Settings"},
            {"label": "Jarz Segmentation Settings", "link_type": "DocType", "link_to": "Jarz Segmentation Settings"},
            {"label": "Custom Settings", "link_type": "DocType", "link_to": "Custom Settings"},
            {"label": "Jarz Mobile Device", "link_type": "DocType", "link_to": "Jarz Mobile Device"},
            {"label": "Jarz Web Push Subscription", "link_type": "DocType", "link_to": "Jarz Web Push Subscription"},
        ],
    },
]

# ``link_type`` / shortcut ``type`` -> the DocType holding those records.
_TARGET_DOCTYPE = {"DocType": "DocType", "Page": "Page", "Report": "Report"}


def ensure_jarz_workspace():
    """Create or rebuild the JARZ POS workspace. Called from ``after_migrate``."""
    try:
        shortcuts = [s for s in SHORTCUTS if _target_exists(s["type"], s["link_to"])]
        cards = _resolve_cards()

        if not shortcuts and not cards:
            # Nothing to point at — jarz_pos is installed but not migrated yet.
            # Leave whatever is already there rather than blanking the page.
            return

        is_new = not frappe.db.exists("Workspace", WORKSPACE_NAME)
        ws = _load_or_new()
        ws.label = WORKSPACE_LABEL
        ws.title = WORKSPACE_LABEL
        ws.module = WORKSPACE_MODULE
        ws.icon = WORKSPACE_ICON
        ws.public = 1
        ws.is_hidden = 0
        ws.hide_custom = 0
        ws.sequence_id = WORKSPACE_SEQUENCE

        ws.set("shortcuts", [])
        for sc in shortcuts:
            ws.append("shortcuts", {
                "label": sc["label"],
                "type": sc["type"],
                "link_to": sc["link_to"],
                "color": sc.get("color"),
                "icon": sc.get("icon"),
            })

        ws.set("links", [])
        for card in cards:
            ws.append("links", {
                "type": "Card Break",
                "label": card["label"],
                "link_count": len(card["links"]),
                "hidden": 0,
                "onboard": 0,
            })
            for link in card["links"]:
                ws.append("links", {
                    "type": "Link",
                    "label": link["label"],
                    "link_type": link["link_type"],
                    "link_to": link["link_to"],
                    "hidden": 0,
                    "onboard": 0,
                    "is_query_report": 0,
                    "dependencies": "",
                })

        ws.content = json.dumps(_content_blocks(shortcuts, cards))
        ws.flags.ignore_mandatory = True
        ws.flags.ignore_permissions = True
        ws.flags.ignore_links = True

        if is_new:
            ws.insert(set_name=WORKSPACE_NAME)
        else:
            ws.save()

        frappe.db.commit()
        frappe.logger("jarz_workspace").info(
            "[Workspace] Rebuilt %s: %s shortcuts, %s cards",
            WORKSPACE_NAME, len(shortcuts), len(cards),
        )
    except Exception:
        # Never let a cosmetic workspace problem fail a migration / deploy.
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "ensure_jarz_workspace failed")


def _load_or_new():
    if frappe.db.exists("Workspace", WORKSPACE_NAME):
        return frappe.get_doc("Workspace", WORKSPACE_NAME)
    ws = frappe.new_doc("Workspace")
    ws.name = WORKSPACE_NAME
    return ws


def _target_exists(kind: str, name: str) -> bool:
    """True when the DocType / Page / Report is actually installed on this site."""
    parent = _TARGET_DOCTYPE.get(kind)
    if not parent:
        return False
    try:
        return bool(frappe.db.exists(parent, name))
    except Exception:
        return False


def _resolve_cards():
    """Drop links whose target is missing, then drop cards left with no links."""
    resolved = []
    for card in CARDS:
        links = [l for l in card["links"] if _target_exists(l["link_type"], l["link_to"])]
        if links:
            resolved.append({"label": card["label"], "links": links})
    return resolved


def _content_blocks(shortcuts, cards):
    blocks = [{
        "id": "jarz_hdr",
        "type": "header",
        "data": {"text": '<span class="h4"><b>Jarz POS</b></span>', "col": 12},
    }]
    for i, sc in enumerate(shortcuts):
        blocks.append({
            "id": f"jarz_sc_{i:02d}",
            "type": "shortcut",
            "data": {"shortcut_name": sc["label"], "col": 3},
        })
    if cards:
        blocks.append({
            "id": "jarz_hdr_cards",
            "type": "header",
            "data": {"text": '<span class="h4"><b>Reports &amp; Masters</b></span>', "col": 12},
        })
        for i, card in enumerate(cards):
            blocks.append({
                "id": f"jarz_card_{i:02d}",
                "type": "card",
                "data": {"card_name": card["label"], "col": 4},
            })
    return blocks

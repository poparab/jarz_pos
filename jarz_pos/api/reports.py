"""
Reports API for the Jarz POS mobile app.

Provides stock-level reports grouped by item group, with warehouse
breakdowns and totals.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES


def _ensure_jarz_manager():
    """Raise if the current user is not a JARZ Manager."""
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access reports"), frappe.PermissionError)


@frappe.whitelist()
def get_final_products_report() -> Dict[str, Any]:
    """
    Return stock balances for items in the "Medium" and "Large" item groups,
    pivoted by warehouse.

    Response shape:
    {
        "warehouses": ["WH-A", "WH-B", ...],
        "items": [
            {
                "item_code": "ITEM-001",
                "item_name": "...",
                "item_group": "Medium",
                "stock_uom": "Nos",
                "warehouse_qty": {"WH-A": 10.0, "WH-B": 5.0},
                "total_qty": 15.0
            },
            ...
        ]
    }
    """
    _ensure_jarz_manager()

    target_groups = ["Medium", "Large"]

    # Get all non-disabled items in the target groups
    items = frappe.get_all(
        "Item",
        filters={"item_group": ["in", target_groups], "disabled": 0, "has_variants": 0},
        fields=["name as item_code", "item_name", "item_group", "stock_uom"],
        order_by="item_group asc, item_name asc",
    )

    if not items:
        return {"warehouses": [], "items": []}

    item_codes = [it["item_code"] for it in items]

    # Get actual stock from Bin (only non-zero balances)
    bins = frappe.get_all(
        "Bin",
        filters={"item_code": ["in", item_codes], "actual_qty": [">", 0]},
        fields=["item_code", "warehouse", "actual_qty"],
    )

    # Build warehouse set and per-item warehouse map
    warehouse_set = set()
    item_wh_map: Dict[str, Dict[str, float]] = {}
    for b in bins:
        warehouse_set.add(b["warehouse"])
        item_wh_map.setdefault(b["item_code"], {})[b["warehouse"]] = float(b["actual_qty"])

    warehouses = sorted(warehouse_set)

    result_items = []
    for it in items:
        wh_qty = item_wh_map.get(it["item_code"], {})
        if not wh_qty:
            continue  # skip items with zero stock in all warehouses
        total = sum(wh_qty.values())
        result_items.append({
            "item_code": it["item_code"],
            "item_name": it["item_name"],
            "item_group": it["item_group"],
            "stock_uom": it["stock_uom"],
            "warehouse_qty": wh_qty,
            "total_qty": total,
        })

    return {"warehouses": warehouses, "items": result_items}


@frappe.whitelist()
def get_materials_report() -> Dict[str, Any]:
    """
    Return stock balances for Raw Material, Sub Assembly, and Consumable
    item groups.

    Response shape:
    {
        "raw_materials": [
            {
                "item_code": "RM-001",
                "item_name": "...",
                "item_group": "Raw Material",
                "stock_uom": "Kg",
                "warehouse_qty": {"WH-A": 100.0},
                "total_qty": 100.0,
                "warehouse_count": 1
            },
            ...
        ],
        "sub_assemblies": [ ... ],
        "consumables": [ ... ]
    }
    """
    _ensure_jarz_manager()

    target_groups = ["Raw Material", "Sub Assembly", "Consumable"]

    items = frappe.get_all(
        "Item",
        filters={"item_group": ["in", target_groups], "disabled": 0, "has_variants": 0},
        fields=["name as item_code", "item_name", "item_group", "stock_uom"],
        order_by="item_group asc, item_name asc",
    )

    if not items:
        return {"raw_materials": [], "sub_assemblies": [], "consumables": []}

    item_codes = [it["item_code"] for it in items]

    bins = frappe.get_all(
        "Bin",
        filters={"item_code": ["in", item_codes], "actual_qty": [">", 0]},
        fields=["item_code", "warehouse", "actual_qty"],
    )

    item_wh_map: Dict[str, Dict[str, float]] = {}
    for b in bins:
        item_wh_map.setdefault(b["item_code"], {})[b["warehouse"]] = float(b["actual_qty"])

    raw_materials = []
    sub_assemblies = []
    consumables = []

    for it in items:
        wh_qty = item_wh_map.get(it["item_code"], {})
        if not wh_qty:
            continue
        total = sum(wh_qty.values())
        entry = {
            "item_code": it["item_code"],
            "item_name": it["item_name"],
            "item_group": it["item_group"],
            "stock_uom": it["stock_uom"],
            "warehouse_qty": wh_qty,
            "total_qty": total,
            "warehouse_count": len(wh_qty),
        }
        if it["item_group"] == "Raw Material":
            raw_materials.append(entry)
        elif it["item_group"] == "Sub Assembly":
            sub_assemblies.append(entry)
        else:
            consumables.append(entry)

    # Sort consumables: items in more warehouses first
    consumables.sort(key=lambda x: (-x["warehouse_count"], x["item_name"]))

    return {
        "raw_materials": raw_materials,
        "sub_assemblies": sub_assemblies,
        "consumables": consumables,
    }

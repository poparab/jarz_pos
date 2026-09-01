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


def _ensure_materials_report_access():
    """Materials & Consumables — the line-manager tier (line manager and above)."""
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(ROLES.LINE_MANAGER_TIER):
        frappe.throw(_("You are not permitted to access this report"), frappe.PermissionError)


#: Item groups each bucket of the Materials & Consumables report covers.
#:
#: Every name is expanded to its descendants before filtering, so moving these
#: under a parent group — or splitting packaging and labels out of Raw Material —
#: does not silently empty a bucket. A name that does not exist on a given site
#: is skipped rather than raising, which keeps the roots list safe to extend
#: ahead of the data.
_MATERIAL_ROOTS = ("Raw Material", "Packaging", "Labels")
_SUB_ASSEMBLY_ROOTS = ("Sub Assemblies",)
_CONSUMABLE_ROOTS = ("Consumable",)


def _expand_item_groups(roots: tuple) -> List[str]:
    """Each named group plus every group beneath it in the Item Group tree."""
    names: List[str] = []
    for root in roots:
        bounds = frappe.db.get_value("Item Group", root, ["lft", "rgt"], as_dict=True)
        if not bounds:
            continue
        names.extend(
            row["name"]
            for row in frappe.get_all(
                "Item Group",
                filters={"lft": [">=", bounds["lft"]], "rgt": ["<=", bounds["rgt"]]},
                fields=["name"],
            )
        )
    return names


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

    group_aliases = {
        "Medium": {"Medium", "Meduim"},
        "Large": {"Large"},
    }
    target_groups = sorted({group for aliases in group_aliases.values() for group in aliases})

    # Get all non-disabled items in the target groups
    items = frappe.get_all(
        "Item",
        filters={"item_group": ["in", target_groups], "disabled": 0, "has_variants": 0},
        fields=["name as item_code", "item_name", "item_group", "stock_uom"],
        order_by="item_group asc, item_name asc",
    )

    if not items:
        return {"groups": []}

    item_codes = [it["item_code"] for it in items]

    # Get actual stock from Bin (only non-zero balances)
    bins = frappe.get_all(
        "Bin",
        filters={"item_code": ["in", item_codes], "actual_qty": [">", 0]},
        fields=["item_code", "warehouse", "actual_qty"],
    )

    # Build per-item warehouse map
    item_wh_map: Dict[str, Dict[str, float]] = {}
    for b in bins:
        item_wh_map.setdefault(b["item_code"], {})[b["warehouse"]] = float(b["actual_qty"])

    # Build separate tables per group, Medium first.
    groups_order = ["Medium", "Large"]
    result_groups = []

    for group_name in groups_order:
        group_items = [
            it for it in items if it["item_group"] in group_aliases[group_name]
        ]
        if not group_items:
            continue

        warehouse_set: set = set()
        group_result_items = []
        for it in group_items:
            wh_qty = item_wh_map.get(it["item_code"], {})
            if not wh_qty:
                continue
            warehouse_set.update(wh_qty.keys())
            total = sum(wh_qty.values())
            group_result_items.append({
                "item_code": it["item_code"],
                "item_name": it["item_name"],
                "item_group": group_name,
                "stock_uom": it["stock_uom"],
                "warehouse_qty": wh_qty,
                "total_qty": total,
            })

        if group_result_items:
            result_groups.append({
                "group_name": group_name,
                "warehouses": sorted(warehouse_set),
                "items": group_result_items,
            })

    return {"groups": result_groups}


@frappe.whitelist()
def get_materials_report() -> Dict[str, Any]:
    """
    Return stock balances for the material, sub-assembly and consumable item
    groups — each expanded to its descendants, so the report follows the Item
    Group tree rather than a fixed list of leaf names.

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
    _ensure_materials_report_access()

    # Previously a literal ["Raw Material", "Sub Assembly", "Consumable"]. The
    # middle name never matched anything — the group is "Sub Assemblies", plural
    # — so the sub-assemblies bucket was empty on every site since this shipped.
    material_groups = _expand_item_groups(_MATERIAL_ROOTS)
    sub_assembly_groups = _expand_item_groups(_SUB_ASSEMBLY_ROOTS)
    consumable_groups = _expand_item_groups(_CONSUMABLE_ROOTS)
    target_groups = material_groups + sub_assembly_groups + consumable_groups
    if not target_groups:
        return {"raw_materials": [], "sub_assemblies": [], "consumables": []}

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
    sub_assembly_set = set(sub_assembly_groups)
    consumable_set = set(consumable_groups)

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
        if it["item_group"] in sub_assembly_set:
            sub_assemblies.append(entry)
        elif it["item_group"] in consumable_set:
            consumables.append(entry)
        else:
            raw_materials.append(entry)

    # Sort consumables: items in more warehouses first
    consumables.sort(key=lambda x: (-x["warehouse_count"], x["item_name"]))

    return {
        "raw_materials": raw_materials,
        "sub_assemblies": sub_assemblies,
        "consumables": consumables,
    }

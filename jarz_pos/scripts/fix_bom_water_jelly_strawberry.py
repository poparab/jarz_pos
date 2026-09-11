"""Three BOM data fixes found by the 2026-09-11 production audit.

Idempotent.  ``run(apply=False)`` reports the plan and changes nothing;
``run(apply=True)`` applies it.  Every phase runs inside its own savepoint and
commits only after its own invariant check passes, so a failure in one phase
leaves the others intact and leaves nothing half-written.

1. **Water is a stocked item at zero, and it blocks two cake bases.**
   ``Water`` carries a 1.5 Kg line in ``Fudge Cake`` and ``Red Velvet Cake``.  It
   held a fictional 1,999,957.75 Kg until ``MAT-RECO-2026-00074`` zeroed it on
   2026-09-01, and it has been the sole blocker on both bases ever since — tap
   water is not something the floor receives, counts or pays for.

   The fix is a **non-stock** twin, not a flag on the existing item:

   - ``Item.cant_change`` lists ``is_stock_item`` in ``restricted_fields`` and
     refuses the change outright while a submitted Stock Entry Detail / Stock
     Reconciliation Item / BOM exists.  ``Water`` has 48 SLEs and 2 active BOMs.
   - ``include_item_in_manufacturing = 0`` is honoured by the app precheck and by
     ``stock_entry.get_pending_raw_materials`` — but **not** by
     ``get_bom_raw_materials``, and production runs
     ``Manufacturing Settings.backflush_raw_materials_based_on = "BOM"``.  A batch
     would clear the precheck, transfer fine, then fail at Finish under
     ``allow_negative_stock = 0``.  That is a worse failure than today's.
   - ``is_stock_item`` is the one filter every path honours:
     ``get_bom_items_as_dict(include_non_stock_items=False)`` backs the app
     precheck, Work Order ``set_required_items``, the transfer entry and the
     backflush alike.

   The recipe still documents 1.5 Kg of water; only the stock requirement goes.

2. **``BOM-Redvelvet Large-002`` jelly is 1000x short.**  ``conversion_factor =
   0.001`` on a Kg->Kg line, so the form reads ``0.02 Kg`` while everything
   downstream reads ``stock_qty = 2e-05`` — 0.02 grams.  The Medium sibling is
   correct at 0.015 Kg.  See ``bom-uom-conversion-factor-trap``.

3. **``BOM-strawberry mix-002`` declares 1 Kg out of 2 Kg in.**  1 Kg aldia
   Strawberry + 1 Kg jelly.  ``BOM-Blueberry mix-002`` is the identical shape and
   declares 2 Kg, which is the yield this corrects to — so the per-Kg cost stops
   being double the truth (455.67 -> ~227.84) and a batch stops booking away 1 Kg
   of real stock.

A submitted BOM cannot be edited, so each fix is a new **version**: ``copy_doc``,
mutate, insert, submit, then ``deactivate_superseded`` retires whatever the new
default displaced.  That is the same shape ``migrate_mix_to_subassembly.py`` uses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

TAP_WATER = "Water (tap)"
STOCK_WATER = "Water"
WATER_BASES = ["Fudge Cake", "Red Velvet Cake"]

JELLY_BOM_ITEM = "Redvelvet Large"
JELLY_COMPONENT = "jelly"
JELLY_QTY_KG = 0.02

STRAWBERRY_MIX_ITEM = "strawberry mix"
STRAWBERRY_MIX_YIELD = 2.0

# Derived fields on a copied BOM Item row.  `set_bom_material_details` fills a
# field only when it is empty (`if not item.get(r)`), so a row whose item_code
# changed keeps the OLD item's name, description and rate unless they are
# cleared here first.
DERIVED_ROW_FIELDS = ("item_name", "description", "rate", "base_rate", "amount", "base_amount")


def _default_bom(item_code: str) -> Optional[str]:
    return frappe.db.get_value(
        "BOM", {"item": item_code, "is_default": 1, "is_active": 1, "docstatus": 1}, "name"
    )


def _row(doc, item_code: str):
    for row in doc.items:
        if row.item_code == item_code:
            return row
    return None


# ── Step 1: the non-stock twin ──────────────────────────────────────────


def ensure_tap_water_item(apply: bool) -> Dict[str, Any]:
    if frappe.db.exists("Item", TAP_WATER):
        is_stock = frappe.db.get_value("Item", TAP_WATER, "is_stock_item")
        if is_stock:
            # Never silently "repair" this: an item that has become stocked has
            # a ledger behind it, and flipping the flag back is the very change
            # ERPNext refuses.  Stop and let a human look.
            frappe.throw(f"{TAP_WATER} exists but is_stock_item=1 — resolve by hand")
        return {"status": "already exists", "item": TAP_WATER}

    plan = {"status": "create", "item": TAP_WATER, "is_stock_item": 0, "stock_uom": "Kg"}
    if not apply:
        return plan

    doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": TAP_WATER,
            "item_name": TAP_WATER,
            "item_group": "Raw Material",
            "stock_uom": "Kg",
            "is_stock_item": 0,
            "is_purchase_item": 0,
            "is_sales_item": 0,
            "include_item_in_manufacturing": 1,
            "description": (
                "Mains/tap water used in the cake bases. Non-stock on purpose: it is "
                "not received, counted or paid for, so it must never gate a batch. "
                "The recipe quantity is kept for the formulation."
            ),
        }
    )
    doc.insert()
    return plan


def swap_water_in_base(item_code: str, apply: bool) -> Dict[str, Any]:
    bom = _default_bom(item_code)
    if not bom:
        return {"item": item_code, "status": "no active default BOM"}

    doc = frappe.get_doc("BOM", bom)
    if _row(doc, TAP_WATER) and not _row(doc, STOCK_WATER):
        return {"item": item_code, "bom": bom, "status": "already swapped"}

    old = _row(doc, STOCK_WATER)
    if not old:
        return {"item": item_code, "bom": bom, "status": "no Water line"}

    plan = {
        "item": item_code,
        "from_bom": bom,
        "status": "swap",
        "line": f"{STOCK_WATER} -> {TAP_WATER} @ {flt(old.stock_qty, 4)} {old.stock_uom}",
    }
    if not apply:
        return plan

    new = frappe.copy_doc(doc)
    new.is_active = 1
    new.is_default = 1
    for row in new.items:
        if row.item_code != STOCK_WATER:
            continue
        row.item_code = TAP_WATER
        # The row is what `update_exploded_items` reads to decide whether the
        # component belongs in `tabBOM Explosion Item`; copy_doc carried the old
        # item's 1 across, which would keep tap water in the exploded bill.
        row.is_stock_item = 0
        for field in DERIVED_ROW_FIELDS:
            row.set(field, None)
    new.insert()
    new.submit()
    plan["new_bom"] = new.name
    return plan


# ── Step 2: the jelly conversion factor ─────────────────────────────────


def fix_jelly_conversion(apply: bool) -> Dict[str, Any]:
    bom = _default_bom(JELLY_BOM_ITEM)
    if not bom:
        return {"item": JELLY_BOM_ITEM, "status": "no active default BOM"}

    doc = frappe.get_doc("BOM", bom)
    row = _row(doc, JELLY_COMPONENT)
    if not row:
        return {"item": JELLY_BOM_ITEM, "bom": bom, "status": f"no {JELLY_COMPONENT} line"}
    if flt(row.conversion_factor) == 1.0 and flt(row.stock_qty, 6) == JELLY_QTY_KG:
        return {"item": JELLY_BOM_ITEM, "bom": bom, "status": "already correct"}

    plan = {
        "item": JELLY_BOM_ITEM,
        "from_bom": bom,
        "status": "fix conversion factor",
        "line": (
            f"{JELLY_COMPONENT}: cf {flt(row.conversion_factor, 6)} -> 1.0, "
            f"stock_qty {flt(row.stock_qty, 6)} -> {JELLY_QTY_KG} Kg"
        ),
    }
    if not apply:
        return plan

    new = frappe.copy_doc(doc)
    new.is_active = 1
    new.is_default = 1
    for r in new.items:
        if r.item_code != JELLY_COMPONENT:
            continue
        r.qty = JELLY_QTY_KG
        r.uom = "Kg"
        # `update_stock_qty` only re-derives conversion_factor when it is falsy,
        # and `set_bom_material_details` only fills empty fields — so an explicit
        # 1.0 survives validate() and stock_qty lands at qty * 1.0.
        r.conversion_factor = 1.0
        r.stock_qty = JELLY_QTY_KG
        for field in ("rate", "base_rate", "amount", "base_amount"):
            r.set(field, None)
    new.insert()
    new.submit()
    plan["new_bom"] = new.name
    return plan


# ── Step 3: the strawberry mix yield ────────────────────────────────────


def fix_strawberry_mix_yield(apply: bool) -> Dict[str, Any]:
    bom = _default_bom(STRAWBERRY_MIX_ITEM)
    if not bom:
        return {"item": STRAWBERRY_MIX_ITEM, "status": "no active default BOM"}

    doc = frappe.get_doc("BOM", bom)
    if flt(doc.quantity) == STRAWBERRY_MIX_YIELD:
        return {"item": STRAWBERRY_MIX_ITEM, "bom": bom, "status": "already correct"}

    inputs = sum(flt(r.stock_qty) for r in doc.items)
    plan = {
        "item": STRAWBERRY_MIX_ITEM,
        "from_bom": bom,
        "status": "fix yield",
        "yield": f"{flt(doc.quantity, 3)} -> {STRAWBERRY_MIX_YIELD} Kg",
        "input_sum_kg": flt(inputs, 3),
        "cost_per_kg": f"{flt(doc.total_cost / doc.quantity, 2)} -> "
        f"{flt(doc.total_cost / STRAWBERRY_MIX_YIELD, 2)}",
    }
    if not apply:
        return plan

    new = frappe.copy_doc(doc)
    new.quantity = STRAWBERRY_MIX_YIELD
    new.is_active = 1
    new.is_default = 1
    new.insert()
    new.submit()
    plan["new_bom"] = new.name
    return plan


# ── Step 4: retire what the new defaults displaced ──────────────────────


def deactivate_superseded(apply: bool) -> Dict[str, Any]:
    rows = frappe.db.sql(
        """
        SELECT name, item FROM `tabBOM`
        WHERE docstatus = 1 AND is_default = 0 AND is_active = 1
        ORDER BY item, name
        """,
        as_dict=True,
    )
    if apply:
        for row in rows:
            frappe.db.set_value("BOM", row["name"], "is_active", 0, update_modified=False)
    return {"count": len(rows), "boms": [r["name"] for r in rows]}


# ── Verification ────────────────────────────────────────────────────────


def verify() -> Dict[str, Any]:
    """Re-read the end state from the database. Never trusts the plan dicts."""
    out: Dict[str, Any] = {"ok": True, "failures": []}

    def check(label: str, got: Any, want: Any) -> None:
        if got != want:
            out["ok"] = False
            out["failures"].append(f"{label}: got {got!r}, want {want!r}")

    check("tap water is non-stock", frappe.db.get_value("Item", TAP_WATER, "is_stock_item"), 0)

    for item in WATER_BASES:
        bom = _default_bom(item)
        out[item] = bom
        codes = frappe.db.get_all("BOM Item", {"parent": bom}, pluck="item_code")
        check(f"{item}: tap water present", TAP_WATER in codes, True)
        check(f"{item}: stocked water gone", STOCK_WATER in codes, False)

    jelly_bom = _default_bom(JELLY_BOM_ITEM)
    out[JELLY_BOM_ITEM] = jelly_bom
    jelly = frappe.db.get_value(
        "BOM Item",
        {"parent": jelly_bom, "item_code": JELLY_COMPONENT},
        ["qty", "uom", "stock_qty", "conversion_factor"],
        as_dict=True,
    )
    out["jelly_line"] = jelly
    check("jelly conversion factor", flt(jelly["conversion_factor"]), 1.0)
    check("jelly stock qty", flt(jelly["stock_qty"], 6), JELLY_QTY_KG)

    mix_bom = _default_bom(STRAWBERRY_MIX_ITEM)
    out[STRAWBERRY_MIX_ITEM] = mix_bom
    check(
        "strawberry mix yield",
        flt(frappe.db.get_value("BOM", mix_bom, "quantity")),
        STRAWBERRY_MIX_YIELD,
    )

    # Exactly one active BOM per item, still — a second active default is the
    # failure mode a new version introduces if deactivation is skipped.
    dupes = frappe.db.sql(
        """
        SELECT item, COUNT(*) n FROM `tabBOM`
        WHERE docstatus = 1 AND is_active = 1 GROUP BY item HAVING COUNT(*) > 1
        """,
        as_dict=True,
    )
    check("no item with two active BOMs", dupes, [])

    # The whole point of step 1: the bases must no longer be blocked on water.
    from jarz_pos.api import manufacturing as mfg

    blocked: List[str] = []
    for item in WATER_BASES:
        for r in mfg._get_required_material_rows(_default_bom(item), "JARZ", 1.0, fetch_exploded=0):
            if r["item_code"] in (STOCK_WATER, TAP_WATER):
                blocked.append(f"{item}: {r['item_code']} still required")
    check("water no longer a required material", blocked, [])

    out["active_bom_count"] = frappe.db.count("BOM", {"docstatus": 1, "is_active": 1})
    return out


# ── Runner ──────────────────────────────────────────────────────────────


def run(apply: Any = False) -> Dict[str, Any]:
    apply = str(apply).lower() in ("1", "true", "yes", "apply")
    result: Dict[str, Any] = {"apply": apply, "steps": {}}

    phases = [
        ("tap_water_item", lambda: ensure_tap_water_item(apply)),
        ("water_swap", lambda: [swap_water_in_base(i, apply) for i in WATER_BASES]),
        ("jelly", lambda: fix_jelly_conversion(apply)),
        ("strawberry_mix", lambda: fix_strawberry_mix_yield(apply)),
        ("deactivate_superseded", lambda: deactivate_superseded(apply)),
    ]

    for name, fn in phases:
        if not apply:
            result["steps"][name] = fn()
            continue
        # A bare frappe.db.rollback() would discard every phase committed in this
        # same transaction, not just the failing one — see the inventory reorg.
        frappe.db.savepoint(name)
        try:
            result["steps"][name] = fn()
            frappe.db.commit()
        except Exception as exc:
            frappe.db.rollback(save_point=name)
            result["steps"][name] = {"status": "FAILED", "error": repr(exc)}
            result["aborted_at"] = name
            break

    result["verify"] = verify() if apply else "dry run — not verified"
    return result

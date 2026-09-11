"""Move the three fruit mixes out of six jar BOMs and into the sub-assemblies.

The floor mixes a batch of fruit + jelly, uses part of it and keeps the rest for
next time.  Only mango was modelled that way: ``BOM-Mango Medium-002`` carries a
``Mango mix`` line, so a leftover is simply the Bin quantity in
``Raw Material - J`` and the inventory count picks it up.

Blueberry, strawberry and raspberry were modelled the opposite way -- the jar
BOM carried ``<fruit>`` and ``jelly`` as two separate lines, so a mix batch
consumed the fruit once and the jar consumed it again, and the leftover tub had
nowhere to live.  ``Blueberry mix`` and ``strawberry mix`` existed with live
recipes that no active BOM ever drew down; raspberry had no item at all.

What this does, in order:

1. Creates ``raspberry mix`` (Sub Assemblies, Kg, home warehouse
   ``Raw Material - J``) and its 2 Kg BOM, matching its two siblings.
2. Restates the two existing mix BOMs on ``Valuation Rate``.  They were authored
   on ``Last Purchase Rate`` while every jar BOM and ``BOM-Mango mix-001`` use
   valuation, so the same mix was priced on two different bases depending on
   which side of the line you read.
3. Rebuilds each jar BOM with a single mix line in place of the fruit and jelly
   lines, at ``do_not_explode = 1`` so the planner nets against the leftover
   instead of exploding back to fruit -- the ``Mango mix`` shape exactly.
4. Deactivates every superseded BOM.

The mix quantity is **derived** from the jar BOM being replaced (fruit + jelly,
which the 1:1 recipe turns into that much mix) and then checked against
``JAR_MIX_QTY``.  Deriving it means a jar whose recipe was edited since this was
written cannot be silently re-scaled to a stale constant; the cross-check means
a transcription slip in the constant cannot pass either.

Nothing is cancelled and nothing is edited in place: BOMs are submitted
documents, so each change is a new **version** that becomes the default, which
leaves every historical Work Order pointing at the BOM it actually ran.  Same
shape as ``migrate_mix_to_subassembly.py`` and ``fix_bom_water_jelly_strawberry.py``.

Two supplier tins are deliberately NOT merged.  ``aldia blueberry`` and
``Puratos Blueberry KG`` are the same product from different providers at
genuinely different rates (390.82 vs 467.40 EGP/Kg on production 2026-09-12);
they are already linked as a two-way ``Item Alternative`` with
``allow_alternative_item`` on both, so the Bases tab offers either tin at batch
time, each costed at its own rate.  The recipe names Puratos, which is the only
jar-cost change this migration makes.  Strawberry and raspberry have one
supplier each (owner, 2026-09-12).

Run::

    bench --site <site> execute jarz_pos.scripts.migrate_fruit_mixes_to_subassembly.run
    bench --site <site> execute jarz_pos.scripts.migrate_fruit_mixes_to_subassembly.run \
        --kwargs "{'apply':True}"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe.utils import flt

JELLY = "jelly"
MIX_YIELD_KG = 2.0
HOME_WAREHOUSE = "Raw Material - J"
SUB_ASSEMBLY_GROUP = "Sub Assemblies"
COST_BASIS = "Valuation Rate"

# Every jar uses fruit and jelly 1:1, the same ratio the mix recipe declares, so
# a jar's mix content is just the sum of the two lines it replaces.
JAR_MIX_QTY: Dict[str, float] = {
    "Blueberry Medium": 0.030,
    "Blueberry Large": 0.040,
    "Strawberry Medium": 0.030,
    "Strawberry Large": 0.040,
    "Redvelvet Medium": 0.030,
    "Redvelvet Large": 0.040,
}

# The derived figure is compared against the constant above in absolute Kg.
# Tighter than a gram: the source lines are whole grams, so a real edit moves
# this by 5000x the tolerance and a typo cannot hide inside it.
MIX_DERIVATION_TOLERANCE = 0.0002

MIXES: Dict[str, Dict[str, Any]] = {
    "Blueberry mix": {
        # Either tin may appear on a jar BOM; both are the same product.
        "jar_fruits": ("aldia blueberry", "Puratos Blueberry KG"),
        "recipe_fruit": "Puratos Blueberry KG",
        "jars": ("Blueberry Medium", "Blueberry Large"),
    },
    "strawberry mix": {
        "jar_fruits": ("aldia Strawberry",),
        "recipe_fruit": "aldia Strawberry",
        "jars": ("Strawberry Medium", "Strawberry Large"),
    },
    "raspberry mix": {
        "jar_fruits": ("puratos raspberry",),
        "recipe_fruit": "puratos raspberry",
        "jars": ("Redvelvet Medium", "Redvelvet Large"),
    },
}


def _company() -> str:
    return frappe.db.get_single_value("Global Defaults", "default_company")


def _default_bom(item_code: str) -> Optional[str]:
    return frappe.db.get_value(
        "BOM", {"item": item_code, "is_default": 1, "docstatus": 1}, "name"
    )


# -- Step 1: the raspberry mix item and its BOM --------------------------


def ensure_mix_item(mix: str, apply: bool) -> Dict[str, Any]:
    if frappe.db.exists("Item", mix):
        return {"created": False, "item": mix}
    if not apply:
        return {"created": "would", "item": mix}

    doc = frappe.new_doc("Item")
    doc.item_code = mix
    doc.item_name = mix
    doc.item_group = SUB_ASSEMBLY_GROUP
    doc.stock_uom = "Kg"
    doc.is_stock_item = 1
    doc.include_item_in_manufacturing = 1
    doc.is_purchase_item = 0
    doc.is_sales_item = 0
    # The one knob that decides both where a batch of this lands and where a
    # jar consumes it from -- see bases-had-no-home-warehouse.
    doc.append("item_defaults", {"company": _company(), "default_warehouse": HOME_WAREHOUSE})
    doc.insert()
    return {"created": True, "item": mix}


def ensure_mix_valuation(mix: str, bom_name: Optional[str], apply: bool) -> Dict[str, Any]:
    """Seed ``Item.valuation_rate`` for a mix that has never held stock.

    A ``do_not_explode`` line is priced from the sub-assembly's own stock value,
    never from its recipe: ``set_bom_material_details`` clears ``bom_no`` on such
    a row (bom.py), so ``get_rm_rate`` cannot reach the child BOM.  That is the
    right behaviour for something you hold in stock -- and it is what
    ``Mango mix`` already does, priced at its Bin's 95.00 rather than its
    recipe's.

    But ``get_valuation_rate`` walks Bin -> last SLE -> ``Item.valuation_rate``,
    and a brand-new mix has neither Bin nor SLE.  The field is 0.00 on a fresh
    Item, so every jar carrying it silently loses the whole mix line -- Redvelvet
    came out 27% cheap on the first staging pass.  Seeding the field from the
    recipe gives the first batch a sane cost; real stock takes over the moment
    any exists, because the field is the *last* resort in that chain.
    """
    if not bom_name:
        return {"seeded": False, "reason": "no bom"}
    if frappe.get_all("Bin", filters={"item_code": mix, "actual_qty": ["!=", 0]}, limit=1):
        return {"seeded": False, "reason": "has stock"}
    if frappe.get_all(
        "Stock Ledger Entry",
        filters={"item_code": mix, "valuation_rate": [">", 0], "is_cancelled": 0},
        limit=1,
    ):
        return {"seeded": False, "reason": "has ledger history"}
    if flt(frappe.db.get_value("Item", mix, "valuation_rate")) > 0:
        return {"seeded": False, "reason": "already set"}

    bom = frappe.db.get_value("BOM", bom_name, ["total_cost", "quantity"], as_dict=True)
    unit = flt(bom.total_cost) / flt(bom.quantity) if bom and flt(bom.quantity) else 0.0
    if unit <= 0:
        return {"seeded": False, "reason": "recipe costs nothing"}
    if not apply:
        return {"seeded": "would", "rate": round(unit, 4)}
    frappe.db.set_value("Item", mix, "valuation_rate", unit)
    return {"seeded": True, "rate": round(unit, 4)}


def ensure_mix_bom(mix: str, apply: bool) -> Dict[str, Any]:
    """Create the mix BOM, or restate an existing one on the shared cost basis."""
    spec = MIXES[mix]
    fruit = spec["recipe_fruit"]
    current = _default_bom(mix)

    if current:
        doc = frappe.get_doc("BOM", current)
        if doc.rm_cost_as_per == COST_BASIS:
            return {"bom": current, "changed": False}
        if not apply:
            return {"bom": current, "changed": "would", "from_basis": doc.rm_cost_as_per}
        new = frappe.copy_doc(doc)
        new.rm_cost_as_per = COST_BASIS
        new.is_active = 1
        new.is_default = 1
        new.insert()
        new.submit()
        return {"bom": new.name, "changed": True, "from": current,
                "unit_cost": flt(new.total_cost) / flt(new.quantity)}

    if not apply:
        return {"bom": None, "changed": "would create"}

    doc = frappe.new_doc("BOM")
    doc.item = mix
    doc.company = _company()
    doc.quantity = MIX_YIELD_KG
    doc.currency = frappe.db.get_value("Company", _company(), "default_currency") or "EGP"
    doc.rm_cost_as_per = COST_BASIS
    doc.set_rate_of_sub_assembly_item_based_on_bom = 1
    doc.is_active = 1
    doc.is_default = 1
    doc.with_operations = 0
    for code in (fruit, JELLY):
        doc.append("items", {"item_code": code, "qty": 1.0, "uom": "Kg", "stock_qty": 1.0})
    doc.insert()
    doc.submit()
    return {"bom": doc.name, "changed": "created",
            "unit_cost": flt(doc.total_cost) / flt(doc.quantity)}


# -- Step 2: rebuild the jar BOMs ----------------------------------------


def _fruit_row(doc, mix: str):
    codes = MIXES[mix]["jar_fruits"]
    rows = [r for r in doc.items if r.item_code in codes]
    return rows[0] if len(rows) == 1 else None


def _mix_jelly_row(doc, fruit_qty: float):
    """The jelly line that pairs 1:1 with the fruit, or ``None``.

    Matched on quantity rather than "the jelly line", so a jar that ever carries
    jelly for something else keeps it -- the trap that destroyed Tiramisu's
    espresso sugar the first time the cheesecake mix was migrated.
    """
    rows = [
        r
        for r in doc.items
        if r.item_code == JELLY and abs(flt(r.stock_qty) - fruit_qty) <= 1e-9
    ]
    return rows[0] if len(rows) == 1 else None


def plan_jar(jar: str, mix: str) -> Dict[str, Any]:
    bom = _default_bom(jar)
    if not bom:
        return {"jar": jar, "ok": False, "reason": "no submitted default BOM"}
    doc = frappe.get_doc("BOM", bom)

    expected = JAR_MIX_QTY.get(jar)
    if expected is None:
        return {"jar": jar, "ok": False, "reason": "no expected mix qty declared"}

    fruit = _fruit_row(doc, mix)
    mix_rows = [r for r in doc.items if r.item_code == mix]
    drops: List[str] = []

    if fruit is not None:
        jelly = _mix_jelly_row(doc, flt(fruit.stock_qty))
        if jelly is None:
            return {"jar": jar, "ok": False, "from_bom": bom,
                    "reason": f"no single jelly line matching {flt(fruit.stock_qty)} Kg of fruit"}
        derived = flt(fruit.stock_qty) + flt(jelly.stock_qty)
        drops = [f"{fruit.item_code} {flt(fruit.stock_qty)}", f"{JELLY} {flt(jelly.stock_qty)}"]
    elif len(mix_rows) == 1:
        # Already carries the mix; the quantity it carries is the statement of
        # record, re-checked against the constant below like any other.
        derived = flt(mix_rows[0].stock_qty)
    else:
        return {"jar": jar, "ok": False, "from_bom": bom,
                "reason": f"neither a {mix} fruit line nor a single {mix} line"}

    if abs(derived - expected) > MIX_DERIVATION_TOLERANCE:
        return {"jar": jar, "ok": False, "from_bom": bom,
                "reason": f"derived {derived:.6f} Kg but expected {expected:.6f} Kg"}

    # A migrated jar still needs rebuilding when its mix line is priced at zero
    # (the sub-assembly had no valuation when the line was written) or when it
    # would explode back to fruit instead of netting against the leftover.
    row = mix_rows[0] if len(mix_rows) == 1 else None
    repairs: List[str] = []
    if fruit is None and row is not None:
        if flt(row.rate) <= 0:
            repairs.append("mix line priced at zero")
        if not int(row.do_not_explode or 0):
            repairs.append("mix line explodes")
    if fruit is None and not repairs:
        return {"jar": jar, "ok": True, "done": True, "from_bom": bom}

    return {
        "jar": jar, "ok": True, "done": False, "from_bom": bom, "mix": mix,
        "mix_qty": expected, "drops": drops, "repairs": repairs,
        "old_cost": flt(doc.raw_material_cost, 2),
    }


def rebuild_jar(plan: Dict[str, Any]) -> Dict[str, Any]:
    mix = plan["mix"]
    doc = frappe.get_doc("BOM", plan["from_bom"])
    new = frappe.copy_doc(doc)
    new.rm_cost_as_per = COST_BASIS
    new.set_rate_of_sub_assembly_item_based_on_bom = 1
    new.is_active = 1
    new.is_default = 1

    # Re-matched on the copy so row identity lines up with what is iterated;
    # copy_doc leaves every child row's `name` as None, so comparing names
    # would match None == None across unrelated rows.
    fruit = _fruit_row(new, mix)
    jelly = _mix_jelly_row(new, flt(fruit.stock_qty)) if fruit is not None else None
    kept = [
        r
        for r in new.items
        if r is not fruit and r is not jelly and r.item_code != mix
    ]

    new.set("items", [])
    for row in kept:
        new.append("items", row.as_dict())
    new.append(
        "items",
        {
            "item_code": mix,
            "item_name": mix,
            "qty": plan["mix_qty"],
            "uom": "Kg",
            "stock_qty": plan["mix_qty"],
            # Stored between batches -- that is the whole point of the mix -- so
            # the planner must net against the leftover rather than explode back
            # to fruit and jelly.  `Mango mix` carries exactly this shape.
            "do_not_explode": 1,
        },
    )
    new.insert()
    new.submit()

    fresh = frappe.get_doc("BOM", new.name)
    codes = [r.item_code for r in fresh.items]
    mix_rows = [r for r in fresh.items if r.item_code == mix]
    problems = []
    if len(mix_rows) != 1:
        problems.append(f"{len(mix_rows)} mix lines")
    elif abs(flt(mix_rows[0].stock_qty) - plan["mix_qty"]) > 1e-9:
        problems.append(f"mix qty {flt(mix_rows[0].stock_qty)}")
    elif not int(mix_rows[0].do_not_explode or 0):
        problems.append("mix line still explodes")
    # A zero rate is the failure this migration actually hit: the line is priced
    # from the sub-assembly's stock value, so a mix that has never been made
    # prices at nothing and the jar quietly loses the whole line.
    elif flt(mix_rows[0].rate) <= 0:
        problems.append("mix line priced at zero")
    if any(c in MIXES[mix]["jar_fruits"] for c in codes):
        problems.append("fruit line survived")
    if len(codes) != len(kept) + 1:
        problems.append(f"line count {len(codes)} != {len(kept) + 1}")
    if problems:
        frappe.throw(f"{new.name} failed its invariant check: {'; '.join(problems)}")

    return {
        "new_bom": new.name,
        "new_cost": flt(fresh.raw_material_cost, 2),
        "mix_rate": flt(mix_rows[0].rate, 4),
    }


# -- Step 3: retire what the new defaults displaced -----------------------


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
        frappe.db.commit()
    return {"count": len(rows), "boms": [r["name"] for r in rows]}


# -- Runner ---------------------------------------------------------------


def run(apply: Any = False) -> Dict[str, Any]:
    apply = bool(apply)
    mode = "APPLY" if apply else "PLAN"
    print(f"MARK mode {mode}")

    mixes: Dict[str, Any] = {}
    for mix in MIXES:
        item = ensure_mix_item(mix, apply)
        bom = ensure_mix_bom(mix, apply)
        seeded = ensure_mix_valuation(mix, bom.get("bom"), apply)
        mixes[mix] = {"item": item, "bom": bom, "valuation": seeded}
        print(f"MARK mix {mix:<16} item={item} bom={bom} valuation={seeded}")

    jars: List[Dict[str, Any]] = []
    for mix, spec in MIXES.items():
        for jar in spec["jars"]:
            plan = plan_jar(jar, mix)
            if not plan.get("ok"):
                print(f"MARK jar {jar:<20} SKIP {plan.get('reason')}")
                jars.append(plan)
                continue
            if plan.get("done"):
                print(f"MARK jar {jar:<20} already migrated")
                jars.append(plan)
                continue
            what = f"drop={plan['drops']}" if plan["drops"] else f"repair={plan['repairs']}"
            print(
                f"MARK jar {jar:<20} {plan['from_bom']:<26} {what} "
                f"-> {mix} {plan['mix_qty']} Kg  cost={plan['old_cost']}"
            )
            if apply:
                result = rebuild_jar(plan)
                plan.update(result)
                print(
                    f"MARK     new {result['new_bom']:<26} cost {plan['old_cost']} -> "
                    f"{result['new_cost']}  mix_rate={result['mix_rate']}"
                )
            jars.append(plan)

    if apply:
        frappe.db.commit()
    superseded = deactivate_superseded(apply)
    print(f"MARK superseded {superseded['count']}")

    failed = [j for j in jars if not j.get("ok")]
    print(f"MARK done applied={apply} jars={len(jars)} failed={len(failed)}")
    return {"apply": apply, "mixes": mixes, "jars": jars, "superseded": superseded}

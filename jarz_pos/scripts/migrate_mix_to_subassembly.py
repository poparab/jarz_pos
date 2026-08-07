"""Move the cheesecake mix out of 17 jar BOMs and into the sub-assembly.

What this does, in order:

1. Corrects the mix BOM itself — vanilla 18 g -> 20 g, yield 9.518 -> 9.520 Kg
   (owner, 2026-08-08).  9.520 is what the jar BOMs already assume: a medium
   carries exactly 9.520/120 = 79.3333 g of mix, to the gram.
2. Rebuilds each jar BOM with a single ``Cheesecake Mix`` line in place of the
   four-to-five hand-scaled raw lines, fixes the known recipe errors, marks the
   freezer sub-assemblies non-exploding, and standardises the costing basis.
3. Deactivates every superseded BOM so the floor can only pick the current one.

Nothing is cancelled and nothing is edited in place: BOMs are submitted
documents, so each change is a **new version** that becomes the default, which
leaves every historical Work Order still pointing at the BOM it actually ran.

``do_not_explode`` is set deliberately per line, and the two cases are
opposites.  The cakes and biscuits live in the freezer, so planning must net
against that stock instead of exploding to flour and eggs -> ``1``.  The mix is
never stored, so it must keep exploding to raw cheese or every jar would show
as unmakeable against a permanent zero -> ``0``.

Run::

    bench --site <site> execute jarz_pos.scripts.migrate_mix_to_subassembly.run
    bench --site <site> execute jarz_pos.scripts.migrate_mix_to_subassembly.run \
        --kwargs "{'apply':True}"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe.utils import flt

MIX_ITEM = "Cheesecake Mix"

# Corrected mix formula.  Inputs sum to the yield exactly — no bake, no loss.
MIX_VANILLA_KG = 0.020
MIX_YIELD_KG = 9.520

# Raw materials that exist in a jar BOM *only* as part of the mix, so they can
# be dropped outright once the sub-assembly carries them.
MIX_ONLY_COMPONENTS = ("milkana cheese", "Remas cheese", "dr baker cream", "kamina vanilla")

# Powder sugar is the exception: Tiramisu carries it on two separate lines, one
# inside the mix and one to sweeten the espresso. Only the mix line may go.
#
# Identified by picking the line closest to the computed mix share rather than
# by subtracting that share from every powder-sugar line — the subtracting
# version destroyed Tiramisu's 4 g syrup line and left a 0.03 g stub behind on
# the mix line, because it treated each line as if it were the only one.
MIX_SHARED_COMPONENT = "powder sugar"
MIX_SUGAR_KG_PER_BATCH = 1.5

# How far a powder-sugar line may sit from the computed mix share and still be
# taken as the mix line. The real ones land within a rounding hair; the syrup
# lines are 2-3x away, so there is a wide gap to sit in.
SUGAR_MATCH_TOLERANCE = 0.15

# Fraction of a batch that is milkana — used to re-derive each jar's mix content
# from its existing BOM and check it against the figure below.
MILKANA_KG_PER_BATCH = 2.5

# Mix per finished jar, in Kg.
#   standard medium = 9.520 / 120     standard large = 9.520 / 77
#   mango and tiramisu carry less because the fruit and the espresso stretch the
#   same batch further; their figures are the ones already in the BOMs.
JAR_MIX_QTY: Dict[str, float] = {
    "Blueberry Medium": 0.079333,
    "Chocolate Hazelnut Medium": 0.079333,
    "Lotus Medium": 0.079333,
    "Pistachio Medium": 0.079333,
    "Redvelvet Medium": 0.079333,
    "Strawberry Medium": 0.079333,
    "Blueberry Large": 0.123636,
    "Chocolate Hazelnut Large": 0.123636,
    "Lotus Large": 0.123636,
    "Pistachio Large": 0.123636,
    "Redvelvet Large": 0.123636,
    "Strawberry Large": 0.123636,
    "Mango Medium": 0.050106,
    "Mango Large": 0.065135,
    "Tiramisu Medium": 0.067143,
    "Tiramisu Large": 0.091000,
}

# Held in the freezer, so the planner must see the sub-assembly, not its inputs.
FREEZER_SUB_ASSEMBLIES = (
    "Butter Biscuit",
    "Fudge Cake",
    "Red Velvet Cake",
    "Sponge Cake",
    "Savoiardi",
    "Chocolate ganache",
    "Mango mix",
)

# Medium jars must not consume 330 packaging.
PACKAGING_SWAPS: Dict[str, Dict[str, str]] = {
    "Mango Medium": {
        "Glass Jar 330": "Glass Jar",
        "Jar Lid 330": "Jar Lid",
        "Mango  Jar Label 330": "Mango Jar Label 212",
    },
}

# (qty, uom) corrections that survive the mix rebuild.
QTY_CORRECTIONS: Dict[str, Dict[str, Tuple[float, str]]] = {
    # Entered as 0.020 Gram where every sibling uses 0.020 Kg.
    "Redvelvet Large": {"jelly": (0.020, "Kg")},
}

COST_BASIS = "Valuation Rate"

# A jar's re-derived mix content should match JAR_MIX_QTY closely. Wider than
# float noise because the source lines were rounded to 3 decimals of a gram,
# tight enough that a transcription slip cannot pass.
MIX_DERIVATION_TOLERANCE = 0.03

# No exemptions. Tiramisu Large looks like it needs one — it carries the mix
# twice today — but the derivation reads the milkana *BOM Item line*, which
# holds only the flattened half; the other half is inside the sub-assembly line
# and contributes no milkana row. So it derives at 1.0x like everything else,
# and an exemption here would make the guard fire on a BOM that is fine.
DOUBLE_COUNTED_JARS: Dict[str, float] = {}
DOUBLE_COUNT_TOLERANCE = 0.05


def _mix_sugar_line(doc, target_mix: float):
    """The powder-sugar row that represents the mix share, or ``None``.

    Returns the single closest match within tolerance so a jar carrying sugar
    for something else — Tiramisu's espresso syrup — keeps it.
    """
    share = target_mix * (MIX_SUGAR_KG_PER_BATCH / MIX_YIELD_KG)
    if share <= 0:
        return None

    best = None
    best_drift = None
    for row in doc.items:
        if row.item_code != MIX_SHARED_COMPONENT:
            continue
        drift = abs(flt(row.stock_qty) - share) / share
        if drift > SUGAR_MATCH_TOLERANCE:
            continue
        if best_drift is None or drift < best_drift:
            best, best_drift = row, drift
    return best


def _default_bom(item_code: str) -> Optional[str]:
    return frappe.db.get_value(
        "BOM", {"item": item_code, "is_default": 1, "docstatus": 1}, "name"
    )


def _derive_mix_from_milkana(bom_name: str) -> Optional[float]:
    """How much mix a jar BOM currently carries, read off its milkana line."""
    qty = frappe.db.get_value(
        "BOM Item", {"parent": bom_name, "item_code": "milkana cheese"}, "stock_qty"
    )
    if not qty:
        return None
    return flt(qty) / (MILKANA_KG_PER_BATCH / MIX_YIELD_KG)


# ── Step 1: correct the mix BOM ─────────────────────────────────────────


def rebuild_mix_bom(apply: bool) -> Dict[str, Any]:
    current = _default_bom(MIX_ITEM)
    if not current:
        return {"ok": False, "reason": f"{MIX_ITEM} has no submitted default BOM"}

    doc = frappe.get_doc("BOM", current)
    vanilla = next((r for r in doc.items if r.item_code == "kamina vanilla"), None)
    needs = (
        flt(doc.quantity, 3) != MIX_YIELD_KG
        or (vanilla is not None and flt(vanilla.stock_qty, 4) != MIX_VANILLA_KG)
    )
    if not needs:
        return {"ok": True, "changed": False, "bom": current}

    plan = {
        "ok": True,
        "changed": True,
        "from_bom": current,
        "quantity": f"{flt(doc.quantity, 3)} -> {MIX_YIELD_KG}",
        "vanilla": f"{flt(vanilla.stock_qty, 4) if vanilla else 'missing'} -> {MIX_VANILLA_KG}",
    }
    if not apply:
        return plan

    new = frappe.copy_doc(doc)
    new.quantity = MIX_YIELD_KG
    new.rm_cost_as_per = COST_BASIS
    new.is_active = 1
    new.is_default = 1
    for row in new.items:
        if row.item_code == "kamina vanilla":
            row.qty = MIX_VANILLA_KG
            row.uom = "Kg"
            row.stock_qty = MIX_VANILLA_KG
    new.insert()
    new.submit()
    plan["new_bom"] = new.name
    return plan


# ── Step 2: rebuild the jar BOMs ────────────────────────────────────────


def _plan_jar(item_code: str, mix_bom: str) -> Dict[str, Any]:
    bom_name = _default_bom(item_code)
    if not bom_name:
        return {"item_code": item_code, "skipped": "no default BOM"}

    doc = frappe.get_doc("BOM", bom_name)
    target_mix = JAR_MIX_QTY[item_code]

    derived = _derive_mix_from_milkana(bom_name)
    warning = None
    note = None
    if derived is not None:
        expected_factor = DOUBLE_COUNTED_JARS.get(item_code, 1.0)
        drift = abs(derived - target_mix * expected_factor) / (target_mix * expected_factor)
        if expected_factor != 1.0:
            tolerance = DOUBLE_COUNT_TOLERANCE
            note = (
                f"carries the mix {expected_factor:g}x today (double-counted); "
                f"derived {derived:.6f} collapses to {target_mix:.6f}"
            )
        else:
            tolerance = MIX_DERIVATION_TOLERANCE
        if drift > tolerance:
            warning = (
                f"BOM implies {derived:.6f} Kg of mix but the table expects "
                f"{target_mix * expected_factor:.6f} ({drift * 100:.1f}% apart) — "
                f"check before applying"
            )

    removed: List[str] = []
    adjusted: List[str] = []
    swapped: List[str] = []
    corrected: List[str] = []

    sugar_row = _mix_sugar_line(doc, target_mix)
    for row in doc.items:
        if row.item_code in MIX_ONLY_COMPONENTS:
            removed.append(f"{row.item_code} {flt(row.stock_qty * 1000, 3)}g")
        elif row.item_code == MIX_SHARED_COMPONENT:
            if sugar_row is not None and row.name == sugar_row.name:
                removed.append(f"{row.item_code} {flt(row.stock_qty * 1000, 3)}g (mix share)")
            else:
                adjusted.append(
                    f"{row.item_code} {flt(row.stock_qty * 1000, 3)}g kept (not the mix share)"
                )

    for old, new in PACKAGING_SWAPS.get(item_code, {}).items():
        if any(r.item_code == old for r in doc.items):
            swapped.append(f"{old} -> {new}")

    for code, (qty, uom) in QTY_CORRECTIONS.get(item_code, {}).items():
        row = next((r for r in doc.items if r.item_code == code), None)
        if row and (flt(row.qty, 6) != qty or (row.uom or "") != uom):
            corrected.append(f"{code} {flt(row.qty, 6)} {row.uom} -> {qty} {uom}")

    explode_fixes = [
        r.item_code
        for r in doc.items
        if r.item_code in FREEZER_SUB_ASSEMBLIES and not r.do_not_explode
    ]

    has_mix_line = any(r.item_code == MIX_ITEM for r in doc.items)

    return {
        "item_code": item_code,
        "from_bom": bom_name,
        "old_cost": flt(doc.raw_material_cost, 2),
        "mix_qty": target_mix,
        "mix_line": "update" if has_mix_line else "add",
        "removed": removed,
        "adjusted": adjusted,
        "swapped": swapped,
        "corrected": corrected,
        "do_not_explode": explode_fixes,
        "warning": warning,
        "note": note,
    }


def _rebuild_jar(plan: Dict[str, Any], mix_bom: str) -> Dict[str, Any]:
    doc = frappe.get_doc("BOM", plan["from_bom"])
    new = frappe.copy_doc(doc)
    new.rm_cost_as_per = COST_BASIS
    new.is_active = 1
    new.is_default = 1

    target_mix = plan["mix_qty"]
    swaps = PACKAGING_SWAPS.get(plan["item_code"], {})
    corrections = QTY_CORRECTIONS.get(plan["item_code"], {})
    # Matched on the copy, so the row identity lines up with what is iterated.
    sugar_row = _mix_sugar_line(new, target_mix)

    kept = []
    for row in new.items:
        if row.item_code in MIX_ONLY_COMPONENTS:
            continue
        if row.item_code == MIX_ITEM:
            continue  # re-added below at the canonical quantity
        if row.item_code == MIX_SHARED_COMPONENT:
            if sugar_row is not None and row.name == sugar_row.name:
                continue  # the mix's share; the sub-assembly carries it now
            # Any other powder-sugar line belongs to something else and stays.
        if row.item_code in swaps:
            row.item_code = swaps[row.item_code]
            row.item_name = frappe.db.get_value("Item", row.item_code, "item_name")
        if row.item_code in corrections:
            qty, uom = corrections[row.item_code]
            row.qty = qty
            row.uom = uom
            row.stock_qty = qty
        if row.item_code in FREEZER_SUB_ASSEMBLIES:
            # Netted against freezer stock instead of exploded to flour.
            row.do_not_explode = 1
        kept.append(row)

    new.set("items", [])
    for row in kept:
        new.append("items", row.as_dict())

    if target_mix > 0:
        new.append(
            "items",
            {
                "item_code": MIX_ITEM,
                "item_name": MIX_ITEM,
                "qty": target_mix,
                "uom": "Kg",
                "stock_qty": target_mix,
                "bom_no": mix_bom,
                # Never stored, so it must keep exploding to raw cheese.
                "do_not_explode": 0,
            },
        )

    new.insert()
    new.submit()
    return {"new_bom": new.name, "new_cost": flt(new.raw_material_cost, 2)}


# ── Step 3: deactivate everything superseded ────────────────────────────


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


# ── Runner ──────────────────────────────────────────────────────────────


def apply_now() -> Dict[str, Any]:
    """Run the migration for real.

    A no-argument entry point on purpose.  ``--kwargs "{'apply':True}"`` has to
    survive PowerShell, ssh, bash and click before it reaches Python, and the
    quoting is stripped somewhere in that chain often enough that the call
    silently degrades to a dry run — or worse, dies with a NameError after the
    operator believes it ran.  Naming the intent removes the question.
    """
    return run(apply=True)


def run(apply: Any = False) -> Dict[str, Any]:
    apply = bool(apply)
    print("=" * 96)
    print("JARZ — cheesecake mix to sub-assembly" + ("" if apply else "   (DRY RUN)"))
    print("=" * 96)

    mix = rebuild_mix_bom(apply)
    print(f"\n[1] Mix BOM: {mix}")
    if not mix.get("ok"):
        return {"ok": False, "mix": mix}

    mix_bom = mix.get("new_bom") or _default_bom(MIX_ITEM)

    plans = []
    for item_code in JAR_MIX_QTY:
        plan = _plan_jar(item_code, mix_bom)
        plans.append(plan)

    blocking = [p for p in plans if p.get("warning")]
    print(f"\n[2] Jar BOMs — {len(plans)} planned, {len(blocking)} with warnings\n")
    for plan in plans:
        if plan.get("skipped"):
            print(f"  SKIP {plan['item_code']}: {plan['skipped']}")
            continue
        print(f"  {plan['item_code']}  [{plan['from_bom']}]  cost {plan['old_cost']}")
        print(f"      mix line: {plan['mix_line']} {plan['mix_qty']:.6f} Kg")
        for label in ("removed", "adjusted", "swapped", "corrected"):
            for entry in plan[label]:
                print(f"      {label}: {entry}")
        if plan["do_not_explode"]:
            print(f"      do_not_explode -> 1: {', '.join(plan['do_not_explode'])}")
        if plan.get("note"):
            print(f"      note: {plan['note']}")
        if plan.get("warning"):
            print(f"      !! {plan['warning']}")

    if blocking and apply:
        print("\nRefusing to apply: resolve the warnings above first.")
        return {"ok": False, "blocked_by": [p["item_code"] for p in blocking]}

    results = []
    if apply:
        for plan in plans:
            if plan.get("skipped"):
                continue
            outcome = _rebuild_jar(plan, mix_bom)
            results.append({**plan, **outcome})
            print(
                f"  built {plan['item_code']}: {outcome['new_bom']}  "
                f"cost {plan['old_cost']} -> {outcome['new_cost']}"
            )
        frappe.db.commit()

    superseded = deactivate_superseded(apply)
    print(f"\n[3] Superseded BOMs deactivated: {superseded['count']}"
          + ("" if apply else " (would be)"))

    return {
        "ok": True,
        "apply": apply,
        "mix": mix,
        "jars": results or plans,
        "deactivated": superseded["count"],
    }

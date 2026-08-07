"""BOM health audit for the jar catalogue.

Read-only by default.  Every finding is derived from a rule rather than a
hand-written list, so the report stays true as recipes change instead of
rotting into a snapshot of one afternoon's spreadsheet.

Run the audit::

    bench --site <site> execute jarz_pos.scripts.audit_boms.run

Apply only the reversible change (deactivating superseded BOMs)::

    bench --site <site> execute jarz_pos.scripts.audit_boms.run \
        --kwargs "{'apply_deactivate': True}"

Nothing here ever cancels a BOM.  Historical Work Orders reference the old
versions, and ``is_active`` is an ``allow_on_submit`` field — clearing it makes
a BOM unpickable without touching the history that points at it.  Every other
finding needs a new BOM version and is reported for a human to action, because
recipe quantities are a business decision and not something a script should
infer.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import frappe
from frappe.utils import flt

# Groups that hold a sellable jar, as opposed to a component.
FINISHED_GROUPS = ("Medium", "Large")

# Packaging that belongs to the large jar.  A Medium BOM listing one of these
# is filling a 212 product from 330 stock.
LARGE_PACKAGING_MARKER = "330"

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# Rates for the same item are expected to differ slightly between BOMs saved on
# different days.  Past this, the two BOMs are not costing the same thing.
RATE_SPREAD_WARN_PCT = 2.0
RATE_SPREAD_ERROR_PCT = 10.0


def _finding(
    severity: str,
    rule: str,
    subject: str,
    detail: str,
    *,
    bom: Optional[str] = None,
    fixable: bool = False,
) -> Dict[str, Any]:
    return {
        "severity": severity,
        "rule": rule,
        "subject": subject,
        "bom": bom,
        "detail": detail,
        "auto_fixable": fixable,
    }


# ── Rules ───────────────────────────────────────────────────────────────


def rule_superseded_boms_still_active() -> List[Dict[str, Any]]:
    """Submitted, non-default, still selectable.

    This is the rule that gates every other fix: while a superseded BOM is
    active an operator can start a Work Order on it, so correcting the default
    changes nothing about what actually gets made.
    """
    rows = frappe.db.sql(
        """
        SELECT b.name, b.item, b.quantity, b.is_active, b.modified
        FROM `tabBOM` b
        WHERE b.docstatus = 1 AND b.is_default = 0 AND b.is_active = 1
        ORDER BY b.item, b.name
        """,
        as_dict=True,
    )
    return [
        _finding(
            "warning",
            "superseded_bom_active",
            row["item"],
            f"Superseded BOM is still active and selectable on a Work Order "
            f"(yields {flt(row['quantity'], 3)}).",
            bom=row["name"],
            fixable=True,
        )
        for row in rows
    ]


def rule_implausible_bom_quantity() -> List[Dict[str, Any]]:
    """A BOM whose yield and material cost disagree by orders of magnitude.

    Catches both the 9518-instead-of-9.518 typo and the "quantity bumped to 120
    but the ingredients were never scaled" family — running one of those books
    120 jars against one jar of material.
    """
    rows = frappe.db.sql(
        """
        SELECT b.name, b.item, b.quantity, b.raw_material_cost, i.item_group,
               (SELECT AVG(b2.raw_material_cost / NULLIF(b2.quantity, 0))
                FROM `tabBOM` b2
                WHERE b2.item = b.item AND b2.docstatus = 1 AND b2.quantity > 0
               ) AS peer_unit_cost
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        WHERE b.docstatus = 1 AND b.quantity > 0
        """,
        as_dict=True,
    )

    out: List[Dict[str, Any]] = []
    for row in rows:
        peer = flt(row.get("peer_unit_cost"))
        if peer <= 0:
            continue
        unit_cost = flt(row["raw_material_cost"]) / flt(row["quantity"])
        if unit_cost <= 0:
            continue
        ratio = unit_cost / peer
        # An order of magnitude either way is never a recipe change.
        if 0.1 < ratio < 10:
            continue
        out.append(
            _finding(
                "error",
                "implausible_bom_quantity",
                row["item"],
                f"Yields {flt(row['quantity'], 3)} at a material cost of "
                f"{flt(row['raw_material_cost'], 2)} — that is "
                f"{flt(unit_cost, 2)}/unit against {flt(peer, 2)}/unit for other "
                f"versions of this item. Either the quantity or the ingredients "
                f"are wrong by a factor of ~{flt(max(ratio, 1 / ratio), 0)}.",
                bom=row["name"],
            )
        )
    return out


def rule_mix_double_counted(mix_item: str, mix_components: Sequence[str]) -> List[Dict[str, Any]]:
    """A BOM carrying the mix as a sub-assembly *and* as loose ingredients."""
    if not mix_components:
        return []

    rows = frappe.db.sql(
        """
        SELECT b.name, b.item
        FROM `tabBOM` b
        WHERE b.docstatus = 1 AND b.is_default = 1
          AND EXISTS (SELECT 1 FROM `tabBOM Item` s
                      WHERE s.parent = b.name AND s.item_code = %(mix)s)
          AND EXISTS (SELECT 1 FROM `tabBOM Item` f
                      WHERE f.parent = b.name AND f.item_code IN %(components)s)
        """,
        {"mix": mix_item, "components": tuple(mix_components)},
        as_dict=True,
    )

    out: List[Dict[str, Any]] = []
    for row in rows:
        duplicated = frappe.db.sql(
            """
            SELECT bi.item_code, bi.stock_qty
            FROM `tabBOM Item` bi
            WHERE bi.parent = %(bom)s AND bi.item_code IN %(components)s
            """,
            {"bom": row["name"], "components": tuple(mix_components)},
            as_dict=True,
        )
        listed = ", ".join(
            f"{d['item_code']} {flt(d['stock_qty'] * 1000, 3)}g" for d in duplicated
        )
        out.append(
            _finding(
                "error",
                "mix_double_counted",
                row["item"],
                f"Uses the {mix_item} sub-assembly and *also* lists its raw "
                f"materials directly ({listed}). Every unit is charged the mix "
                f"twice, in both cost and material demand.",
                bom=row["name"],
            )
        )
    return out


def rule_mix_not_migrated(mix_item: str, mix_components: Sequence[str]) -> List[Dict[str, Any]]:
    """A jar BOM still hand-scaling the mix into loose ingredients."""
    if not mix_components:
        return []

    rows = frappe.db.sql(
        """
        SELECT b.name, b.item
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        WHERE b.docstatus = 1 AND b.is_default = 1
          AND i.item_group IN %(groups)s
          AND NOT EXISTS (SELECT 1 FROM `tabBOM Item` s
                          WHERE s.parent = b.name AND s.item_code = %(mix)s)
          AND EXISTS (SELECT 1 FROM `tabBOM Item` f
                      WHERE f.parent = b.name AND f.item_code IN %(components)s)
        ORDER BY b.item
        """,
        {"mix": mix_item, "components": tuple(mix_components), "groups": FINISHED_GROUPS},
        as_dict=True,
    )
    return [
        _finding(
            "warning",
            "mix_not_migrated",
            row["item"],
            f"Lists the {mix_item} ingredients directly instead of the "
            f"sub-assembly. The ratio is maintained by hand here and the Daily "
            f"Production Plan cannot see it.",
            bom=row["name"],
        )
        for row in rows
    ]


def rule_wrong_size_packaging() -> List[Dict[str, Any]]:
    """A Medium BOM consuming large-jar packaging.

    Detected by comparing against what the other Medium BOMs use, so it does not
    depend on any naming convention beyond the shared marker.
    """
    rows = frappe.db.sql(
        """
        SELECT b.name, b.item, bi.item_code
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        INNER JOIN `tabBOM Item` bi ON bi.parent = b.name
        WHERE b.docstatus = 1 AND b.is_default = 1
          AND i.item_group = 'Medium'
          AND bi.item_code LIKE %(marker)s
        ORDER BY b.item, bi.idx
        """,
        {"marker": f"%{LARGE_PACKAGING_MARKER}%"},
        as_dict=True,
    )

    grouped: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for row in rows:
        grouped[(row["item"], row["name"])].append(row["item_code"])

    return [
        _finding(
            "error",
            "wrong_size_packaging",
            item,
            f"Medium jar BOM consumes large-jar packaging: {', '.join(codes)}. "
            f"It will draw down 330 stock and never reserve the 212 equivalents.",
            bom=bom,
        )
        for (item, bom), codes in sorted(grouped.items())
    ]


def rule_suspicious_uom() -> List[Dict[str, Any]]:
    """A component entered in a unit 1000x off what its siblings use.

    Compares each line against the same item's usage across the other default
    BOMs in the same item group, so a genuinely tiny dose (vanilla) does not
    trip it while a Kg entered as a Gram does.
    """
    rows = frappe.db.sql(
        """
        SELECT b.name AS bom, b.item AS parent_item, i.item_group,
               bi.item_code, bi.qty, bi.uom, bi.stock_qty
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        INNER JOIN `tabBOM Item` bi ON bi.parent = b.name
        WHERE b.docstatus = 1 AND b.is_default = 1
          AND i.item_group IN %(groups)s
        """,
        {"groups": FINISHED_GROUPS},
        as_dict=True,
    )

    peers: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for row in rows:
        peers[(row["item_group"], row["item_code"])].append(flt(row["stock_qty"]))

    out: List[Dict[str, Any]] = []
    for row in rows:
        values = [v for v in peers[(row["item_group"], row["item_code"])] if v > 0]
        if len(values) < 3:
            # Too few peers to call anything an outlier.
            continue
        median = sorted(values)[len(values) // 2]
        qty = flt(row["stock_qty"])
        if qty <= 0 or median <= 0:
            continue
        if qty * 100 > median:
            continue
        out.append(
            _finding(
                "error",
                "suspicious_uom",
                row["parent_item"],
                f"{row['item_code']} is {flt(row['qty'], 6)} {row['uom']} "
                f"(= {flt(qty, 8)} in stock UOM), but the other {row['item_group']} "
                f"BOMs use about {flt(median, 6)}. Off by roughly "
                f"{flt(median / qty, 0)}x — almost always Gram entered where Kg "
                f"was meant.",
                bom=row["bom"],
            )
        )
    return out


def rule_component_qty_outlier() -> List[Dict[str, Any]]:
    """A component quantity that breaks step with its siblings by a hair.

    Deliberately separate from the UOM rule and set tight: this is the class of
    error that a 1000x check sails past — a 32.000 typed where every sibling
    says 32.470.
    """
    rows = frappe.db.sql(
        """
        SELECT b.name AS bom, b.item AS parent_item, i.item_group,
               bi.item_code, bi.stock_qty
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        INNER JOIN `tabBOM Item` bi ON bi.parent = b.name
        WHERE b.docstatus = 1 AND b.is_default = 1
          AND i.item_group IN %(groups)s
        """,
        {"groups": FINISHED_GROUPS},
        as_dict=True,
    )

    peers: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for row in rows:
        peers[(row["item_group"], row["item_code"])].append(flt(row["stock_qty"]))

    out: List[Dict[str, Any]] = []
    for row in rows:
        values = [v for v in peers[(row["item_group"], row["item_code"])] if v > 0]
        if len(values) < 4:
            continue
        counts: Dict[float, int] = defaultdict(int)
        for value in values:
            counts[round(value, 9)] += 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        modal, modal_count = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0

        # A shared ratio has to be genuinely shared and unambiguous — at least
        # two BOMs on it, and strictly more than any competing value. Requiring
        # near-unanimity instead would be wrong here: Mango and Tiramisu carry
        # deliberately different doses of the same cheese, and their presence
        # must not stop the rule seeing a typo in the flavours that do agree.
        if modal <= 0 or modal_count < 2 or modal_count <= runner_up:
            continue
        qty = round(flt(row["stock_qty"]), 9)
        if qty == modal or qty <= 0:
            continue
        drift = abs(qty - modal) / modal
        if drift > 0.1 or drift < 1e-6:
            # A large gap is a real recipe difference, not a slip of the finger.
            continue
        out.append(
            _finding(
                "warning",
                "component_qty_outlier",
                row["parent_item"],
                f"{row['item_code']} is {flt(qty, 6)} where every other "
                f"{row['item_group']} BOM uses {flt(modal, 6)} "
                f"({flt(drift * 100, 2)}% off). Breaks the shared ratio.",
                bom=row["bom"],
            )
        )
    return out


def rule_inconsistent_rate_basis() -> List[Dict[str, Any]]:
    """Default BOMs valuing the same raw material at different rates."""
    rows = frappe.db.sql(
        """
        SELECT bi.item_code, bi.rate, bi.uom, b.name AS bom, b.rm_cost_as_per
        FROM `tabBOM` b
        INNER JOIN `tabBOM Item` bi ON bi.parent = b.name
        WHERE b.docstatus = 1 AND b.is_default = 1 AND bi.rate > 0
        """,
        as_dict=True,
    )

    normalised: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
    for row in rows:
        rate = flt(row["rate"])
        if (row["uom"] or "").lower() in ("gram", "g"):
            rate *= 1000.0
        normalised[row["item_code"]].append((rate, row["bom"]))

    out: List[Dict[str, Any]] = []
    for item_code, entries in sorted(normalised.items()):
        rates = [r for r, _ in entries]
        low, high = min(rates), max(rates)
        if low <= 0:
            continue
        spread = (high - low) / low * 100.0
        if spread < RATE_SPREAD_WARN_PCT:
            continue
        severity = "error" if spread >= RATE_SPREAD_ERROR_PCT else "warning"
        out.append(
            _finding(
                severity,
                "inconsistent_rate_basis",
                item_code,
                f"Priced between {flt(low, 4)} and {flt(high, 4)} per stock UOM "
                f"across default BOMs — a {flt(spread, 1)}% spread on one item, so "
                f"per-jar costs are not comparable between flavours.",
            )
        )

    bases = frappe.db.sql(
        """
        SELECT rm_cost_as_per, COUNT(*) AS n
        FROM `tabBOM` WHERE docstatus = 1 AND is_default = 1
        GROUP BY rm_cost_as_per ORDER BY n DESC
        """,
        as_dict=True,
    )
    if len(bases) > 1:
        summary = ", ".join(f"{b['rm_cost_as_per']} ({b['n']})" for b in bases)
        out.insert(
            0,
            _finding(
                "error",
                "mixed_rm_cost_as_per",
                "(all default BOMs)",
                f"Default BOMs are split across costing bases: {summary}. This is "
                f"the root cause of the per-item rate spreads below.",
            ),
        )
    return out


def rule_stocked_subassembly_explodes() -> List[Dict[str, Any]]:
    """A sub-assembly held in stock that the parent BOM explodes through.

    Exploding means the planner checks flour and eggs instead of the biscuit
    already sitting in the freezer, so a day that is entirely coverable reads
    as short.
    """
    rows = frappe.db.sql(
        """
        SELECT b.name AS bom, b.item AS parent_item, bi.item_code,
               bi.do_not_explode,
               COALESCE((SELECT SUM(bn.actual_qty) FROM `tabBin` bn
                         WHERE bn.item_code = bi.item_code), 0) AS on_hand
        FROM `tabBOM` b
        INNER JOIN `tabBOM Item` bi ON bi.parent = b.name
        WHERE b.docstatus = 1 AND b.is_default = 1
          AND IFNULL(bi.bom_no, '') <> ''
          AND IFNULL(bi.do_not_explode, 0) = 0
        ORDER BY b.item, bi.idx
        """,
        as_dict=True,
    )
    return [
        _finding(
            "warning",
            "stocked_subassembly_explodes",
            row["parent_item"],
            f"{row['item_code']} is held in stock ({flt(row['on_hand'], 3)} on hand) "
            f"but this BOM explodes through it to raw materials. Set "
            f"do_not_explode on the line so planning nets against the freezer.",
            bom=row["bom"],
        )
        for row in rows
        if flt(row["on_hand"]) > 0
    ]


def rule_orphan_subassembly_boms() -> List[Dict[str, Any]]:
    """A maintained sub-assembly BOM that no current recipe consumes."""
    rows = frappe.db.sql(
        """
        SELECT i.name AS item_code, i.default_bom,
               (SELECT COUNT(*) FROM `tabBOM Item` bi
                INNER JOIN `tabBOM` b ON b.name = bi.parent
                WHERE bi.item_code = i.name AND b.docstatus = 1 AND b.is_default = 1
               ) AS used_in_defaults
        FROM `tabItem` i
        WHERE i.item_group = 'Sub Assemblies' AND i.disabled = 0
        HAVING used_in_defaults = 0
        ORDER BY i.name
        """,
        as_dict=True,
    )
    return [
        _finding(
            "info",
            "orphan_subassembly",
            row["item_code"],
            "Has a maintained default BOM but no current recipe uses it. It will "
            "still be forecast and suggested on the production board.",
            bom=row.get("default_bom"),
        )
        for row in rows
    ]


def rule_yield_mass_balance() -> List[Dict[str, Any]]:
    """Sub-assembly BOMs that disagree about whether baking loses weight.

    Reports the implied loss per BOM rather than asserting a correct figure —
    the number is a kitchen measurement, not something to derive. What matters
    is that comparable products model it the same way.
    """
    boms = frappe.db.sql(
        """
        SELECT b.name, b.item, b.quantity, b.uom, b.process_loss_percentage
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        WHERE b.docstatus = 1 AND b.is_default = 1 AND i.item_group = 'Sub Assemblies'
        ORDER BY b.item
        """,
        as_dict=True,
    )

    out: List[Dict[str, Any]] = []
    for bom in boms:
        lines = frappe.db.sql(
            """
            SELECT bi.item_code, bi.stock_qty, it.stock_uom
            FROM `tabBOM Item` bi
            INNER JOIN `tabItem` it ON it.name = bi.item_code
            WHERE bi.parent = %(bom)s
            """,
            {"bom": bom["name"]},
            as_dict=True,
        )
        # Only mass-comparable lines. A piece count (eggs) has no weight here,
        # so a BOM containing one can only be reported, never judged.
        weighable = [flt(r["stock_qty"]) for r in lines if (r["stock_uom"] or "") == "Kg"]
        non_weighable = [r["item_code"] for r in lines if (r["stock_uom"] or "") != "Kg"]
        if not weighable:
            continue

        input_kg = sum(weighable)
        yield_kg = flt(bom["quantity"])
        if (bom["uom"] or "") != "Kg" or yield_kg <= 0:
            continue

        implied_loss_pct = (input_kg - yield_kg) / input_kg * 100.0 if input_kg else 0.0
        out.append(
            _finding(
                "info",
                "yield_mass_balance",
                bom["item"],
                f"Weighable inputs {flt(input_kg, 3)} Kg -> declared yield "
                f"{flt(yield_kg, 3)} Kg (implied loss {flt(implied_loss_pct, 1)}%, "
                f"process_loss_percentage set to {flt(bom['process_loss_percentage'], 1)}%)"
                + (f"; excludes non-weight lines: {', '.join(non_weighable)}." if non_weighable else "."),
                bom=bom["name"],
            )
        )
    return out


# ── Runner ──────────────────────────────────────────────────────────────


def _resolve_mix_context() -> Tuple[str, List[str]]:
    from jarz_pos.services import daily_production_plan as planning

    mix_item = planning._resolve_mix_item()
    return mix_item, planning._resolve_flattened_mix_components(mix_item)


def collect_findings() -> List[Dict[str, Any]]:
    mix_item, mix_components = _resolve_mix_context()

    findings: List[Dict[str, Any]] = []
    findings += rule_inconsistent_rate_basis()
    findings += rule_implausible_bom_quantity()
    findings += rule_mix_double_counted(mix_item, mix_components)
    findings += rule_wrong_size_packaging()
    findings += rule_suspicious_uom()
    findings += rule_component_qty_outlier()
    findings += rule_mix_not_migrated(mix_item, mix_components)
    findings += rule_stocked_subassembly_explodes()
    findings += rule_superseded_boms_still_active()
    findings += rule_orphan_subassembly_boms()
    findings += rule_yield_mass_balance()

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["rule"], f["subject"]))
    return findings


def deactivate_superseded_boms(dry_run: bool = True) -> Dict[str, Any]:
    """Clear ``is_active`` on every submitted non-default BOM.

    Reversible and non-destructive: ``is_active`` is ``allow_on_submit``, so
    this needs no cancellation and the BOMs stay attached to the Work Orders
    that used them.
    """
    targets = frappe.db.sql(
        """
        SELECT b.name, b.item FROM `tabBOM` b
        WHERE b.docstatus = 1 AND b.is_default = 0 AND b.is_active = 1
        ORDER BY b.item, b.name
        """,
        as_dict=True,
    )

    if not dry_run:
        for row in targets:
            frappe.db.set_value("BOM", row["name"], "is_active", 0, update_modified=False)
        frappe.db.commit()

    return {
        "dry_run": dry_run,
        "count": len(targets),
        "boms": [r["name"] for r in targets],
    }


def run(apply_deactivate: Any = False, verbose: Any = True) -> Dict[str, Any]:
    """Audit the BOM catalogue.  Writes nothing unless ``apply_deactivate``."""
    findings = collect_findings()

    by_severity: Dict[str, int] = defaultdict(int)
    by_rule: Dict[str, int] = defaultdict(int)
    for item in findings:
        by_severity[item["severity"]] += 1
        by_rule[item["rule"]] += 1

    result: Dict[str, Any] = {
        "total": len(findings),
        "by_severity": dict(by_severity),
        "by_rule": dict(by_rule),
        "findings": findings,
    }

    if apply_deactivate:
        result["deactivated"] = deactivate_superseded_boms(dry_run=False)

    if verbose:
        _print_report(result)
    return result


def _print_report(result: Dict[str, Any]) -> None:
    print("=" * 100)
    print("JARZ BOM AUDIT")
    print("=" * 100)
    counts = result["by_severity"]
    print(
        f"{result['total']} findings — "
        f"{counts.get('error', 0)} error, "
        f"{counts.get('warning', 0)} warning, "
        f"{counts.get('info', 0)} info"
    )
    print()

    current_rule = None
    for item in result["findings"]:
        if item["rule"] != current_rule:
            current_rule = item["rule"]
            print(f"\n── {current_rule}  ({result['by_rule'][current_rule]}) " + "─" * 40)
        bom = f"  [{item['bom']}]" if item.get("bom") else ""
        print(f"  {item['severity'].upper():<7} {item['subject']}{bom}")
        print(f"          {item['detail']}")

    if "deactivated" in result:
        print()
        print(f"DEACTIVATED {result['deactivated']['count']} superseded BOMs.")

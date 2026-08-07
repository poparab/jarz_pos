"""Daily production plan maths — jars in, mixer runs out.

The Production Board answers *what should we make over the next N days*.  This
module answers a different, shorter question that the floor actually asks every
morning: **"we want these jars today — how many mixes is that, and how do we
split them across the mixer?"**

The shape of the answer is dictated by the mixer, not by arithmetic.  A mix
run is 1, 1.5 or 2 batches — never 0.4, never 2.3 — so the honest output is a
list of runs plus the overproduction that rounding forces.  Hiding that
rounding behind a single "3.2 batches" figure would be the one number nobody
can act on.

Layering rule: same as ``production_planning`` — pure arithmetic above the
resolver block, every frappe/ERPNext touch behind a ``_resolve_x()`` accessor so
the maths tests need no patching at all.
"""

from __future__ import annotations

import math
from itertools import combinations_with_replacement
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe

# The sub-assembly whose BOM defines one batch.  Held as a setting rather than a
# constant because the item can be renamed, but defaulted so a site that never
# touches Jarz POS Settings still works.
DEFAULT_MIX_ITEM = "Cheesecake Mix"

# What the mixer will accept in one run, in batches.  The floor works in halves
# and will not run less than a full batch.
DEFAULT_RUN_SIZES: Tuple[float, ...] = (1.0, 1.5, 2.0)

# Guard on the search below.  ~4600 combinations at 30 runs, still instant, and
# 30 runs is already 60 batches — far past anything one day's mixer can do.
MAX_RUNS = 30

# Quantities are compared in Kg, where a gram is 0.001.  Anything below a
# milligram is float noise, not a real difference.
QTY_EPSILON = 1e-9


# ── Pure arithmetic ─────────────────────────────────────────────────────
# No frappe below this line until the resolver block.


def batches_needed(*, total_mix_qty: float, batch_qty: float) -> float:
    """Fractional batches implied by a mix quantity.  Not yet rounded to runs."""
    batch_qty = float(batch_qty or 0)
    if batch_qty <= 0:
        return 0.0
    return max(0.0, float(total_mix_qty or 0)) / batch_qty


def plan_mixer_runs(
    batches: float,
    *,
    run_sizes: Sequence[float] = DEFAULT_RUN_SIZES,
) -> Dict[str, Any]:
    """Split a fractional batch requirement into concrete mixer runs.

    Optimises for **fewest runs first, least overproduction second** — the order
    the floor actually cares about, since every run costs a setup and a wash
    regardless of how full it is.  5.0 batches becomes ``2 + 2 + 1``, not
    ``2 + 1.5 + 1.5``: same three runs, same total, but the first fills the
    mixer twice and only under-fills once.

    ``batches`` is never rounded down.  Asking for 0.1 batches returns one full
    run, because there is no such thing as a fifth of a mix — and the
    ``overproduction_batches`` field says so out loud instead of quietly
    pretending the plan was exact.

    Returns ``runs: []`` for a zero requirement, which is different from a
    requirement that cannot be met.
    """
    sizes = sorted({float(s) for s in (run_sizes or ()) if float(s) > 0})
    batches = float(batches or 0)

    if batches <= QTY_EPSILON:
        return {
            "runs": [],
            "run_count": 0,
            "planned_batches": 0.0,
            "required_batches": 0.0,
            "overproduction_batches": 0.0,
            "capped": False,
        }

    if not sizes:
        # No mixer configured: report the raw requirement rather than inventing
        # a run size, so the caller can surface a setup problem.
        return {
            "runs": [],
            "run_count": 0,
            "planned_batches": 0.0,
            "required_batches": batches,
            "overproduction_batches": 0.0,
            "capped": True,
        }

    largest = sizes[-1]
    # The epsilon stops 4.0/2.0 landing on 2.0000000000000004 and asking for a
    # third run nobody needs.
    min_runs = max(1, math.ceil((batches / largest) - QTY_EPSILON))

    best: Optional[Tuple[float, ...]] = None
    for n in range(min_runs, MAX_RUNS + 1):
        best = _cheapest_combination(n, batches, sizes)
        if best is not None:
            break

    if best is None:
        # Requirement exceeds MAX_RUNS runs of the largest size.  Return the
        # biggest plan we will schedule and flag it rather than looping forever.
        capped_runs = tuple([largest] * MAX_RUNS)
        planned = float(sum(capped_runs))
        return {
            "runs": list(capped_runs),
            "run_count": len(capped_runs),
            "planned_batches": planned,
            "required_batches": batches,
            "overproduction_batches": 0.0,
            "capped": True,
        }

    planned = float(sum(best))
    return {
        # Biggest runs first — the mixer gets filled while the team is fresh,
        # and a short final run is the one easiest to drop if the day changes.
        "runs": sorted(best, reverse=True),
        "run_count": len(best),
        "planned_batches": planned,
        "required_batches": batches,
        "overproduction_batches": max(0.0, planned - batches),
        "capped": False,
    }


def _cheapest_combination(
    n: int,
    batches: float,
    sizes: Sequence[float],
) -> Optional[Tuple[float, ...]]:
    """Least-total combination of exactly ``n`` runs covering ``batches``.

    Enumerated rather than solved: with three run sizes the candidate count is
    ``C(n+2, 2)`` — 496 at n=30 — so the exhaustive answer is both instant and
    obviously correct, which a greedy fill would not be for an unevenly spaced
    run-size list.
    """
    best: Optional[Tuple[float, ...]] = None
    best_total = float("inf")

    for combo in combinations_with_replacement(sizes, n):
        total = float(sum(combo))
        if total + QTY_EPSILON < batches:
            continue
        if total < best_total:
            best_total = total
            best = combo

    return best


def aggregate_mix_demand(lines: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Total mix quantity a set of planned jar quantities implies.

    Each line is ``{"item_code", "planned_qty", "mix_qty_per_unit"}``.  Lines
    with no mix content (Molten) contribute zero and are kept in the breakdown
    anyway, because "why is Molten not listed" is a question somebody will ask.
    """
    breakdown: List[Dict[str, Any]] = []
    total = 0.0

    for line in lines or []:
        planned = max(0.0, float(line.get("planned_qty") or 0))
        per_unit = max(0.0, float(line.get("mix_qty_per_unit") or 0))
        qty = planned * per_unit
        total += qty
        breakdown.append(
            {
                "item_code": line.get("item_code"),
                "item_name": line.get("item_name") or line.get("item_code"),
                "planned_qty": planned,
                "mix_qty_per_unit": per_unit,
                "mix_qty": qty,
            }
        )

    return {"total_mix_qty": total, "breakdown": breakdown}


def jars_per_batch(*, batch_qty: float, mix_qty_per_unit: float) -> Optional[float]:
    """How many of one jar a single batch yields.  ``None`` when it uses no mix.

    This is the number the floor already knows by heart (120 medium, 77 large);
    surfacing it from the BOM is how they can tell at a glance whether the BOM
    still matches reality.
    """
    per_unit = float(mix_qty_per_unit or 0)
    if per_unit <= QTY_EPSILON:
        return None
    return float(batch_qty or 0) / per_unit


def summarise_actuals(lines: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Planned-vs-actual roll-up for the end-of-day close.

    ``actual_qty`` of ``None`` means "not counted yet" and is excluded from the
    totals; ``0`` means "counted, made none".  Collapsing the two would let an
    uncounted flavour read as a total failure.
    """
    planned_total = 0.0
    actual_total = 0.0
    counted = 0
    uncounted = 0
    rows: List[Dict[str, Any]] = []

    for line in lines or []:
        planned = max(0.0, float(line.get("planned_qty") or 0))
        raw_actual = line.get("actual_qty")
        planned_total += planned

        if raw_actual is None or raw_actual == "":
            uncounted += 1
            rows.append(
                {
                    "item_code": line.get("item_code"),
                    "planned_qty": planned,
                    "actual_qty": None,
                    "variance_qty": None,
                    "variance_pct": None,
                }
            )
            continue

        actual = max(0.0, float(raw_actual))
        actual_total += actual
        counted += 1
        rows.append(
            {
                "item_code": line.get("item_code"),
                "planned_qty": planned,
                "actual_qty": actual,
                "variance_qty": actual - planned,
                "variance_pct": ((actual - planned) / planned * 100.0) if planned > 0 else None,
            }
        )

    return {
        "planned_total": planned_total,
        "actual_total": actual_total,
        "variance_qty": actual_total - planned_total,
        "variance_pct": ((actual_total - planned_total) / planned_total * 100.0)
        if planned_total > 0
        else None,
        "lines_counted": counted,
        "lines_uncounted": uncounted,
        "lines": rows,
    }


def realised_yield(
    *,
    actual_mix_batches: float,
    actual_units: float,
) -> Optional[float]:
    """Units actually obtained per batch of mix.

    The whole point of the evening count: the BOM claims 120 medium per batch,
    and this says what today really gave.  Drift between the two is the signal
    that a BOM quantity needs revisiting — it is not an error to correct on the
    spot, because a single day's figure is noise.
    """
    batches = float(actual_mix_batches or 0)
    if batches <= QTY_EPSILON:
        return None
    return float(actual_units or 0) / batches


# ── frappe / ERPNext resolvers ──────────────────────────────────────────


def _resolve_default_company() -> str:
    try:
        return frappe.db.get_single_value("Global Defaults", "default_company") or ""
    except Exception:
        return ""


def _resolve_mix_item() -> str:
    """The sub-assembly that defines a batch.  Settings first, constant second."""
    try:
        configured = frappe.db.get_single_value("Jarz POS Settings", "production_mix_item")
    except Exception:
        configured = None
    return (configured or DEFAULT_MIX_ITEM).strip() or DEFAULT_MIX_ITEM


def _resolve_run_sizes() -> Tuple[float, ...]:
    """Mixer run sizes, from Settings as a comma list, falling back to 1/1.5/2."""
    try:
        raw = frappe.db.get_single_value("Jarz POS Settings", "production_mixer_run_sizes")
    except Exception:
        raw = None
    if not raw:
        return DEFAULT_RUN_SIZES

    sizes: List[float] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            continue
        if value > 0:
            sizes.append(value)

    return tuple(sorted(set(sizes))) or DEFAULT_RUN_SIZES


def _resolve_mix_batch_qty(mix_item: str) -> Tuple[float, str, Optional[str]]:
    """Yield of one run of the mix BOM, its UOM, and the BOM name.

    Returns ``(0, uom, None)`` when the mix item has no submitted default BOM —
    a setup problem the caller must surface rather than divide by.
    """
    bom = frappe.db.get_value(
        "BOM",
        {"item": mix_item, "is_default": 1, "docstatus": 1},
        ["name", "quantity", "uom"],
        as_dict=True,
    )
    if not bom:
        stock_uom = frappe.db.get_value("Item", mix_item, "stock_uom") or "Kg"
        return 0.0, stock_uom, None
    return float(bom.get("quantity") or 0), bom.get("uom") or "Kg", bom.get("name")


def _resolve_planable_rows(company: str, mix_item: str) -> List[Dict[str, Any]]:
    """Every finished good with a default BOM, plus how much mix that BOM uses.

    The mix quantity comes from the BOM Item row, i.e. the **one-level** BOM,
    not the explosion — which is the whole reason the mix has to live as a
    sub-assembly line.  A jar whose BOM still carries the mix flattened into
    cheese and cream reads as 0 here and is reported by
    ``find_unmigrated_bom_rows`` rather than silently guessed at.

    ``stock_qty`` rather than ``qty`` so a line entered in Gram still totals in
    the mix item's stock UOM.
    """
    conditions = ["b.is_default = 1", "b.docstatus = 1", "i.disabled = 0"]
    values: Dict[str, Any] = {"mix_item": mix_item}

    if company:
        conditions.append("b.company = %(company)s")
        values["company"] = company

    where = " AND ".join(conditions)
    rows = frappe.db.sql(
        f"""
        SELECT
            i.name                        AS item_code,
            COALESCE(i.item_name, i.name) AS item_name,
            i.item_group                  AS item_group,
            i.stock_uom                   AS stock_uom,
            b.name                        AS default_bom,
            b.quantity                    AS bom_qty,
            b.company                     AS company,
            COALESCE((
                SELECT SUM(bi.stock_qty)
                FROM `tabBOM Item` bi
                WHERE bi.parent = b.name AND bi.item_code = %(mix_item)s
            ), 0)                         AS mix_qty_per_bom
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        WHERE {where}
          AND i.name <> %(mix_item)s
        ORDER BY i.item_group ASC, i.item_name ASC
        """,
        values,
        as_dict=True,
    )

    out: List[Dict[str, Any]] = []
    for row in rows:
        bom_qty = float(row.get("bom_qty") or 1) or 1.0
        # Per finished unit, not per BOM run — the jar BOMs are all quantity 1
        # today but nothing enforces that, and a quantity-120 BOM would
        # otherwise overstate every line by 120x.
        row["mix_qty_per_unit"] = float(row.get("mix_qty_per_bom") or 0) / bom_qty
        out.append(dict(row))
    return out


def _resolve_flattened_mix_components(mix_item: str) -> List[str]:
    """Raw materials that the mix BOM consumes.

    Used only to spot jar BOMs that still carry those raw materials directly —
    the signature of a BOM that predates the sub-assembly migration.
    """
    bom = frappe.db.get_value("BOM", {"item": mix_item, "is_default": 1, "docstatus": 1}, "name")
    if not bom:
        return []
    rows = frappe.db.get_all("BOM Item", filters={"parent": bom}, pluck="item_code")
    return [r for r in rows if r]

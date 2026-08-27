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

from jarz_pos.utils.settings_utils import raw_single_value

# The sub-assembly whose BOM defines one batch.  Held as a setting rather than a
# constant because the item can be renamed, but defaulted so a site that never
# touches Jarz POS Settings still works.
DEFAULT_MIX_ITEM = "Cheesecake Mix"

# What the mixer will accept in one run, in batches, and how well each one
# actually mixes.  Straight from the floor: 1.5 is the right quantity, 2
# stretches the machine, and 1 does not mix well — too little in the bowl.
#
# The penalties encode that ordering rather than a measurement.  Their only job
# is to rank candidate plans, so what matters is 1 being clearly worse than 2
# and 1.5 being free; the absolute values carry no meaning on their own.
QUALITY_PREFERRED = "preferred"
QUALITY_ACCEPTABLE = "acceptable"
QUALITY_POOR = "poor"

QUALITY_PENALTY = {
    QUALITY_PREFERRED: 0.0,
    QUALITY_ACCEPTABLE: 1.0,
    QUALITY_POOR: 3.0,
}

DEFAULT_RUN_QUALITY: Dict[float, str] = {
    1.0: QUALITY_POOR,
    1.5: QUALITY_PREFERRED,
    2.0: QUALITY_ACCEPTABLE,
}

DEFAULT_RUN_SIZES: Tuple[float, ...] = tuple(sorted(DEFAULT_RUN_QUALITY))

# What one wasted batch of mix costs, in the same units as the quality
# penalties.  The mix is never stored, so spare mix is either thrown away or
# forces jars nobody ordered — at 4.0 a full wasted batch outweighs any run
# quality, while half a batch will not by itself justify a poor mix.
DEFAULT_WASTE_WEIGHT = 4.0

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
    run_quality: Optional[Dict[float, str]] = None,
    waste_weight: float = DEFAULT_WASTE_WEIGHT,
) -> Dict[str, Any]:
    """Split a fractional batch requirement into concrete mixer runs.

    Not every run size mixes equally well, so this is a quality decision before
    it is an arithmetic one.  A 1.5 run is what the recipe is built around; a 2
    stretches the machine; a 1 leaves too little in the bowl to come together.
    Scheduling "fewest runs" would happily fill a day with the two sizes the
    floor least wants.

    Scored as ``sum(quality penalty) + waste_weight * spare batches``, with run
    count only as a tie-break.  That makes 5.0 batches come out as
    ``2 + 1.5 + 1.5`` — exact coverage, one stretched run — rather than the
    ``2 + 2 + 1`` a run-count-first rule would pick, or the ``1.5 x 4`` that
    pure quality would pick at the cost of a whole wasted batch.

    ``batches`` is never rounded down.  Asking for 0.1 batches returns one run,
    because there is no such thing as a fifth of a mix — and
    ``overproduction_batches`` says so out loud instead of quietly pretending
    the plan was exact.

    Returns ``runs: []`` for a zero requirement, which is different from a
    requirement that cannot be met.
    """
    # `is None`, not `or`: an explicitly empty mapping means "no mixer sizes are
    # configured", which must fall through to the capped branch below and
    # surface a setup problem. `or` swallowed that into the defaults and
    # invented run sizes for a site that had declared none.
    quality = dict(DEFAULT_RUN_QUALITY if run_quality is None else run_quality)
    sizes = sorted({float(s) for s in quality if float(s) > 0})
    batches = float(batches or 0)
    waste_weight = float(waste_weight if waste_weight is not None else DEFAULT_WASTE_WEIGHT)

    if batches <= QTY_EPSILON:
        return _empty_plan(0.0, capped=False)

    if not sizes:
        # No mixer configured: report the raw requirement rather than inventing
        # a run size, so the caller can surface a setup problem.
        return _empty_plan(batches, capped=True)

    largest = sizes[-1]
    # The epsilon stops 4.0/2.0 landing on 2.0000000000000004 and asking for a
    # third run nobody needs.
    min_runs = max(1, math.ceil((batches / largest) - QTY_EPSILON))

    best: Optional[Tuple[float, ...]] = None
    best_score = float("inf")

    # Searching a few run counts past the minimum is what lets the optimiser
    # trade an extra preferred run against a stretched one; stopping at the
    # minimum would hard-code the run-count-first rule this is replacing.
    for n in range(min_runs, min(min_runs + 3, MAX_RUNS) + 1):
        combo, score = _best_combination(n, batches, sizes, quality, waste_weight)
        if combo is None:
            continue
        if score < best_score - QTY_EPSILON:
            best_score, best = score, combo

    if best is None:
        # Requirement exceeds MAX_RUNS runs of the largest size.  Return the
        # biggest plan we will schedule and flag it rather than looping forever.
        capped_runs = tuple([largest] * MAX_RUNS)
        return {
            **_plan_payload(capped_runs, batches, quality),
            "capped": True,
        }

    return {**_plan_payload(best, batches, quality), "capped": False}


def _empty_plan(required: float, *, capped: bool) -> Dict[str, Any]:
    return {
        "runs": [],
        "run_detail": [],
        "run_count": 0,
        "planned_batches": 0.0,
        "required_batches": required,
        "overproduction_batches": 0.0,
        "quality_penalty": 0.0,
        "capped": capped,
    }


def _plan_payload(
    combo: Sequence[float],
    batches: float,
    quality: Dict[float, str],
) -> Dict[str, Any]:
    # Biggest runs first: the mixer gets filled while the team is fresh, and a
    # short final run is the one easiest to drop if the day changes.
    runs = sorted(combo, reverse=True)
    planned = float(sum(runs))
    return {
        "runs": list(runs),
        "run_detail": [
            {"size": run, "quality": quality.get(run, QUALITY_ACCEPTABLE)} for run in runs
        ],
        "run_count": len(runs),
        "planned_batches": planned,
        "required_batches": batches,
        "overproduction_batches": max(0.0, planned - batches),
        "quality_penalty": sum(
            QUALITY_PENALTY.get(quality.get(run, QUALITY_ACCEPTABLE), 1.0) for run in runs
        ),
    }


def _best_combination(
    n: int,
    batches: float,
    sizes: Sequence[float],
    quality: Dict[float, str],
    waste_weight: float,
) -> Tuple[Optional[Tuple[float, ...]], float]:
    """Best-scoring combination of exactly ``n`` runs covering ``batches``.

    Enumerated rather than solved: with three run sizes the candidate count is
    ``C(n+2, 2)`` — 496 at n=30 — so the exhaustive answer is both instant and
    obviously correct, which no greedy fill would be once run quality and waste
    are traded against each other.
    """
    best: Optional[Tuple[float, ...]] = None
    best_score = float("inf")

    for combo in combinations_with_replacement(sizes, n):
        total = float(sum(combo))
        if total + QTY_EPSILON < batches:
            continue
        penalty = sum(
            QUALITY_PENALTY.get(quality.get(size, QUALITY_ACCEPTABLE), 1.0) for size in combo
        )
        score = penalty + waste_weight * (total - batches)
        if score < best_score - QTY_EPSILON:
            best_score, best = score, combo

    return best, best_score


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


def _resolve_run_quality() -> Dict[float, str]:
    """Mixer run sizes and how well each mixes, from Settings.

    Accepts ``1:poor, 1.5:preferred, 2:acceptable``.  A bare number is treated
    as ``acceptable`` so the older size-only setting keeps working — but a
    site that never states its preferences gets the floor's own ordering from
    ``DEFAULT_RUN_QUALITY`` rather than a flat one, because "all run sizes are
    equally good" is the one thing we know is false.
    """
    try:
        raw = frappe.db.get_single_value("Jarz POS Settings", "production_mixer_run_sizes")
    except Exception:
        raw = None
    if not raw:
        return dict(DEFAULT_RUN_QUALITY)

    parsed: Dict[float, str] = {}
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        size_text, _, quality_text = part.partition(":")
        try:
            size = float(size_text.strip())
        except ValueError:
            continue
        if size <= 0:
            continue

        quality = (quality_text or "").strip().lower()
        if quality not in QUALITY_PENALTY:
            # Fall back to the known preference for that size before assuming
            # "acceptable" — a site listing plain sizes still gets 1 ranked
            # below 1.5 rather than level with it.
            quality = DEFAULT_RUN_QUALITY.get(size, QUALITY_ACCEPTABLE)
        parsed[size] = quality

    return parsed or dict(DEFAULT_RUN_QUALITY)


def _resolve_waste_weight() -> float:
    """What a wasted batch of mix costs the plan, from Settings.

    The run sizes and their qualities have been operator-tunable since this
    module shipped; the weight they are traded against was not, so the one
    number that decides *whether* a poor mix beats throwing mix away could only
    be changed by a deploy. It is the same kind of floor judgement as the run
    qualities and belongs next to them.

    Read through ``raw_single_value`` rather than ``get_single_value``: this is
    a Float, and an unwritten Single field casts to 0.0, which would be read as
    the deliberate and very different "waste is free" — silently flipping the
    plan to prefer perfect runs at any waste. ``None`` means never written and
    takes the default; an explicit 0 is honoured.
    """
    try:
        raw = raw_single_value("Jarz POS Settings", "production_waste_weight")
    except Exception:
        raw = None
    if raw in (None, ""):
        return DEFAULT_WASTE_WEIGHT
    try:
        weight = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_WASTE_WEIGHT
    # A negative weight would pay the plan to waste mix. Nothing on the floor
    # means that, so it degrades to the default rather than optimising for it.
    return weight if weight >= 0 else DEFAULT_WASTE_WEIGHT


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

"""Sub-assembly ("Bases") planning maths.

The Production Board answers *what should we make* for things that sell.  Sub-
assemblies — Fudge Cake, Sponge Cake, Savoiardi, Butter Biscuit, Cheesecake Mix
— never sell, so their ``jarz_velocity_60d`` is 0, their suggested batch count
is 0, and the board has nothing to say about them.  The freezer still runs dry.

This module holds the arithmetic that gives the floor a different signal for
those items: **what the jars we intend to fill will eat**, converted into whole
mixer/oven batches of the base itself.

Layering rule, same as ``production_planning`` and ``daily_production_plan``:
everything here is a pure function over plain numbers and plain dicts.  No
``frappe`` import, no database read, no write.  ``api/subassembly.py`` does the
resolving and calls in here for every number it reports.

Two conventions are load-bearing throughout and are the source of previously
shipped bugs elsewhere in this codebase:

* **Negative stock never inflates a requirement.**  ERPNext permits negative
  ``Bin`` quantities and they are almost always a counting lag, not units owed.
  Every quantity that feeds a subtraction is floored at zero first; the raw
  negative is reported separately so somebody counts the item.
* **Blank is not zero.**  ``None`` means "not known" (no demand signal, no
  capacity computed, no run sizes configured); ``0`` means "known to be zero".
  Collapsing the two is how a screen ends up saying "make nothing" when it
  meant "I have no idea".
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

# Batch counts are reported to 3 decimals.  Enough to see "2.5 batches in the
# freezer" without rendering 0.30000000000000004 at somebody standing at a
# bench.
BATCH_PRECISION = 3

# Quantities (Kg of a base) carry more decimals than batch counts do: a base
# consumed at 0.0004 Kg/day is a real, if slow, consumer and must not round
# away to "nothing needed".  Six is enough to absorb float noise — 35.0 - 12.3
# lands on 22.700000000000003 — without erasing a milligram-scale rate.
QTY_PRECISION = 6

# Days of cover are read as "about a fortnight", never to the microsecond.
COVER_PRECISION = 3

# Below a milligram is float noise, not a real quantity.  Same value the daily
# plan uses, so the two modules agree at the boundary.
QTY_EPSILON = 1e-9

# Run sizes are operator-typed decimals ("1.5"), so the comparison tolerance is
# far looser than the quantity epsilon — 1.5 arriving as 1.4999999999999998 off
# a client round-trip must still match the configured 1.5.
RUN_SIZE_TOLERANCE = 1e-6


# ── Scalars ─────────────────────────────────────────────────────────────


def to_float(value: Any, default: float = 0.0) -> float:
    """``float()`` that degrades instead of raising on ``None``/junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def countable(value: Any) -> float:
    """The part of a quantity that may be counted against a requirement.

    A negative ``Bin`` quantity contributes nothing — it does not, and must not,
    add to what has to be produced.
    """
    return max(0.0, to_float(value, 0.0))


def _round_batches(value: float) -> float:
    return round(value, BATCH_PRECISION)


# ── Batch conversions ───────────────────────────────────────────────────


def batches_from_qty(*, qty: Any, batch_yield: Any) -> float:
    """Fractional batches a quantity represents, given what one batch yields.

    Returns ``0.0`` rather than raising when the BOM yield is missing or zero.
    A zero-yield BOM is a setup problem the caller has to surface: it cannot be
    divided by, and inventing a yield of 1 would quietly report a jar-sized
    batch count for a 40 Kg mix.
    """
    produced = to_float(batch_yield, 0.0)
    if produced <= 0:
        return 0.0
    return _round_batches(to_float(qty, 0.0) / produced)


def batches_on_hand(*, on_hand: Any, batch_yield: Any) -> float:
    """How many whole-and-part batches of this base are sitting in the freezer.

    The negative is floored away here: "-1.4 batches on hand" is not a thing
    anybody can act on, and the raw quantity is reported beside this figure so
    the hole stays visible.
    """
    return batches_from_qty(qty=countable(on_hand), batch_yield=batch_yield)


def shortfall_batches(*, qty_required: Any, on_hand: Any, batch_yield: Any) -> float:
    """Batches that still have to be made to cover a requirement.

    ``max(0, qty_required - max(0, on_hand)) / batch_yield``.  Both floors
    matter: the outer one stops a well-stocked base reporting a negative
    shortfall, the inner one stops a counting hole inflating the answer.
    """
    deficit = countable(qty_required) - countable(on_hand)
    if deficit <= 0:
        return 0.0
    return batches_from_qty(qty=deficit, batch_yield=batch_yield)


# ── Run sizes ───────────────────────────────────────────────────────────


def parse_run_sizes(raw: Any) -> Optional[List[float]]:
    """Sizes out of the ``1:poor, 1.5:preferred, 2`` settings grammar.

    Returns a sorted, de-duplicated list, or ``None`` when nothing usable is
    configured — ``None`` and ``[]`` would mean the same thing to a caller and
    only one of them survives JSON honestly, so this never returns an empty
    list.

    The quality half of each entry is dropped on purpose: this feature only
    asks *which sizes are legal*, and the quality ranking stays the daily plan's
    business.
    """
    if raw is None:
        return None

    sizes: List[float] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        size_text, _, _quality = part.partition(":")
        try:
            size = float(size_text.strip())
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        if not any(abs(size - existing) <= RUN_SIZE_TOLERANCE for existing in sizes):
            sizes.append(size)

    return sorted(sizes) or None


def run_sizes_for_item(
    item_code: Any,
    *,
    mix_item: Any,
    mix_run_sizes: Optional[Sequence[float]],
    overrides: Optional[Mapping[str, Any]] = None,
) -> Optional[List[float]]:
    """Legal run sizes for one base item, or ``None`` when it has none.

    Today exactly one item has configured sizes: the mixer's mix item, via
    ``Jarz POS Settings.production_mixer_run_sizes``.  Everything else is made
    in whatever quantity somebody asks for.

    The ``overrides`` map is the seam for a future per-item setting: drop
    ``{item_code: "1, 2, 4"}`` (or an already-parsed list) in and the response
    shape does not move.  An override wins over the mix default, so an
    explicitly configured mix item still behaves the way it is configured.
    """
    code = str(item_code or "").strip()
    if not code:
        return None

    if overrides:
        raw_override = overrides.get(code)
        if raw_override is not None:
            if isinstance(raw_override, str):
                return parse_run_sizes(raw_override)
            parsed = [s for s in (to_float(v, 0.0) for v in raw_override) if s > 0]
            return sorted(set(parsed)) or None

    if mix_item and code == str(mix_item).strip():
        return list(mix_run_sizes) if mix_run_sizes else None

    return None


def matches_run_size(batches: Any, run_sizes: Optional[Sequence[float]]) -> bool:
    """Whether a requested batch count is one the item is actually run in.

    ``True`` when the item has no configured sizes — an unconfigured item is
    unconstrained, not blocked.  That is the difference between "no rule" and
    "breaks the rule", and defaulting it the other way would make every base
    but the mix un-startable.
    """
    if not run_sizes:
        return True
    value = to_float(batches, 0.0)
    return any(abs(value - to_float(size, 0.0)) <= RUN_SIZE_TOLERANCE for size in run_sizes)


# ── Demand derivation ───────────────────────────────────────────────────


def derive_base_demand(
    jar_targets: Iterable[Mapping[str, Any]],
    bom_rows: Iterable[Mapping[str, Any]],
    base_item_codes: Iterable[str],
) -> Dict[str, float]:
    """Base-item quantities implied by a set of finished-jar targets.

    ``jar_targets`` are ``{"item_code", "qty", "bom_name"}`` — what somebody
    intends to fill today, from the day's plan or from the board's own
    suggestions.

    ``bom_rows`` are the **one-level** ``BOM Item`` rows for those BOMs:
    ``{"bom_name", "item_code", "qty", "bom_quantity"}``.  One level, never the
    explosion — that is the whole reason the bases live as sub-assembly lines,
    and exploding would report flour and cream instead of "Sponge Cake".

    Per row: ``qty_required += (row.qty / bom.quantity) * jar_target_qty``.

    Two BOMs are deliberately skipped rather than guessed at:

    * a jar with no ``bom_name`` — nothing to read;
    * a BOM whose ``quantity`` is missing or zero — dividing by it is
      impossible and substituting 1 would silently multiply the day's demand by
      the real batch size.

    Both contribute nothing.  A base that no target consumes is simply absent
    from the result, which the caller renders as "no demand signal" rather than
    as a demand of zero.
    """
    wanted = {str(code) for code in (base_item_codes or []) if code}
    if not wanted:
        return {}

    rows_by_bom: Dict[str, List[Mapping[str, Any]]] = {}
    for row in bom_rows or []:
        bom_name = str(row.get("bom_name") or "").strip()
        if not bom_name:
            continue
        rows_by_bom.setdefault(bom_name, []).append(row)

    demand: Dict[str, float] = {}

    for target in jar_targets or []:
        bom_name = str(target.get("bom_name") or "").strip()
        if not bom_name:
            continue

        # A negative target is meaningless and must never subtract demand that
        # another jar genuinely created.
        target_qty = countable(target.get("qty"))
        if target_qty <= QTY_EPSILON:
            continue

        for row in rows_by_bom.get(bom_name, []):
            component = str(row.get("item_code") or "").strip()
            if component not in wanted:
                continue

            bom_quantity = to_float(row.get("bom_quantity"), 0.0)
            if bom_quantity <= 0:
                # Unusable yield: skip the row instead of inventing a divisor.
                continue

            per_unit = countable(row.get("qty")) / bom_quantity
            if per_unit <= 0:
                continue

            demand[component] = demand.get(component, 0.0) + (per_unit * target_qty)

    return demand


def build_demand_block(
    *,
    qty_required: Any,
    on_hand: Any,
    batch_yield: Any,
    driver: str,
) -> Dict[str, Any]:
    """The ``demand`` sub-object for one base item.

    Kept here rather than in the API layer so the three batch figures on it can
    never drift apart from the conversions above.
    """
    required = countable(qty_required)
    return {
        "qty_required": required,
        "batches_required": batches_from_qty(qty=required, batch_yield=batch_yield),
        "shortfall_batches": shortfall_batches(
            qty_required=required, on_hand=on_hand, batch_yield=batch_yield
        ),
        "driver": driver,
    }


# ── Derived consumption and cover ───────────────────────────────────────
#
# The ``demand`` block above answers "what does the plan I am looking at
# need today".  Everything below answers a different question — "how long does
# the freezer last" — and the two must both be reported, because a base can be
# fully covered for today's plan and still be four days from empty.
#
# A base has no sales velocity of its own; it is never sold.  Its consumption is
# derived one BOM level down from the jars that eat it, which is why the input
# here is a *rate* (Kg/day) rather than a quantity.


def derive_base_consumption_per_day(
    jar_velocity_targets: Iterable[Mapping[str, Any]],
    bom_rows: Iterable[Mapping[str, Any]],
    base_item_codes: Iterable[str],
) -> Dict[str, float]:
    """Per-day consumption of each base, implied by the jars that consume it.

    Deliberately the *same* arithmetic and the *same* code path as
    ``derive_base_demand``: ``qty`` is dimensionless to that walk, so feeding
    each jar's **effective daily velocity** in as ``qty`` returns a per-day
    quantity per base instead of a per-plan one.  ``sum over jars (jar velocity
    x base qty per jar)`` — exactly the brief — and one BOM walk rather than
    two that could drift apart about what a jar contains.

    A base that no jar's one-level BOM lists is **absent** from the result, not
    zero.  The caller renders absent as ``None``/``no_velocity``, the same way
    the jar board reports an item that never sells; a zero there would read as
    "we know this base needs nothing", which is a claim nobody has evidence for.
    """
    return derive_base_demand(jar_velocity_targets, bom_rows, base_item_codes)


def cover_days(*, on_hand: Any, consumption_per_day: Any) -> Optional[float]:
    """Days the freezer lasts at the derived rate, or ``None`` if unknown.

    Mirrors ``production_planning.days_of_cover`` deliberately, because the two
    figures sit side by side on one screen and an operator reading "4 days" on a
    jar and "4 days" on its base must be reading the same thing:

    * ``None`` — not a sentinel — when the rate is zero or unknown.  The stored
      ``jarz_days_of_stock`` field writes ``999`` for that case, which makes
      "never consumed" and "huge pile" indistinguishable downstream.
    * stock at or below zero covers ``0.0`` days, never a negative number.  A
      negative ``Bin`` is a counting error; "-17 days of cover" means nothing to
      somebody deciding what to make, and the raw ``on_hand`` is reported beside
      this so the hole stays visible.
    """
    rate = to_float(consumption_per_day, 0.0)
    if rate <= 0:
        return None

    stock = to_float(on_hand, 0.0)
    if stock <= 0:
        return 0.0
    return round(stock / rate, COVER_PRECISION)


def cover_suggested_qty(*, consumption_per_day: Any, target_days: Any, on_hand: Any) -> float:
    """Quantity to make so the freezer holds ``target_days`` of consumption.

    ``max(0, rate x target_days - max(0, on_hand))``, in the item's stock UOM.

    **The inner floor is the load-bearing one.**  ERPNext permits negative
    ``Bin`` quantities and production carries 26 of them right now; subtracting
    a negative would add that phantom hole on top of the real requirement.  The
    jar board learned this the hard way — 81 of 183 suggested batches on staging
    were the counting error, not demand — and over-producing a frozen
    perishable is the more expensive error of the two.

    Returns ``0.0`` when the rate is unknown/zero or the target is zero: with no
    consumption signal there is no defensible quantity, and inventing one from a
    target alone would tell the floor to fill a freezer nobody empties.
    """
    rate = to_float(consumption_per_day, 0.0)
    days = to_float(target_days, 0.0)
    if rate <= 0 or days <= 0:
        return 0.0

    deficit = (rate * days) - countable(on_hand)
    if deficit <= QTY_EPSILON:
        return 0.0
    return round(deficit, QTY_PRECISION)


def cover_suggested_batches(*, suggested_qty: Any, batch_yield: Any) -> int:
    """Whole batches covering ``suggested_qty``.

    Rounded **up**, because half a mixer bowl is not a thing anyone can make,
    and reported as a whole ``int`` — this is the number that goes on a
    Work Order, unlike the fractional ``batches_on_hand`` figure above.

    ``0`` when the BOM has no yield, for the same reason ``batches_from_qty``
    returns ``0.0``: a zero-yield BOM cannot be divided by, and inventing a
    yield of 1 would ask for 35 batches of a 13.674 Kg mix.

    The epsilon matches ``production_planning.suggested_batches``: ``27.348 /
    13.674`` can land on ``2.0000000000000004`` in float, and a naive ``ceil``
    would then ask for a third batch nobody needs.
    """
    produced = to_float(batch_yield, 0.0)
    qty = to_float(suggested_qty, 0.0)
    if produced <= 0 or qty <= 0:
        return 0

    return max(0, math.ceil((qty / produced) - 1e-9))


def build_cover_block(
    *,
    consumption_per_day: Any,
    on_hand: Any,
    target_days: Any,
    batch_yield: Any,
) -> Dict[str, Any]:
    """The cover figures for one base, composed once so they cannot disagree.

    ``consumption_per_day=None`` means *no signal* — no jar BOM lists this base
    one level down, or nothing sells at all — and propagates as ``None`` to
    ``days_of_cover`` too.  The two quantity fields still report ``0``/``0.0``
    in that case rather than ``None``, exactly as the jar board reports
    ``suggested_batches: 0`` for an item that never sells: the caller pairs them
    with a ``no_velocity`` status, which is what carries "we do not know".

    ``status`` is deliberately **not** produced here.  It is
    ``production_planning.status_for_days_of_cover`` applied to the thresholds
    the jar board read for this same request — one vocabulary, one set of
    thresholds, resolved in the API layer that already holds them.
    """
    known = consumption_per_day is not None
    rate = round(countable(consumption_per_day), QTY_PRECISION) if known else None

    qty = cover_suggested_qty(
        consumption_per_day=rate, target_days=target_days, on_hand=on_hand
    )

    try:
        target = int(target_days or 0)
    except (TypeError, ValueError):
        target = 0

    return {
        "consumption_per_day": rate,
        "days_of_cover": cover_days(on_hand=on_hand, consumption_per_day=rate),
        "target_days": target,
        "suggested_qty": qty,
        "suggested_batches": cover_suggested_batches(
            suggested_qty=qty, batch_yield=batch_yield
        ),
    }


# ── Summary ─────────────────────────────────────────────────────────────


def summarise_bases(items: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Header counts for the Bases screen.

    ``blocked_by_materials`` counts only a *known* zero.  ``can_make_now_batches``
    of ``None`` means capacity was never computed for that item, which is not
    the same as "the store cannot cover it" and must not be reported as such.
    """
    short = 0
    blocked = 0

    for item in items or []:
        demand = item.get("demand")
        if demand and to_float(demand.get("shortfall_batches"), 0.0) > 0:
            short += 1

        capacity = item.get("can_make_now_batches")
        if capacity is not None and to_float(capacity, 0.0) <= 0:
            blocked += 1

    return {
        "total": len(items or []),
        "short_of_demand": short,
        "blocked_by_materials": blocked,
    }

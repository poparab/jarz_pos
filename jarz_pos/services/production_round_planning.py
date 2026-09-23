"""Production round maths — how many batches of every jar to make now.

The factory restocks every selling branch once per ``cycle_days`` (14) and each
branch also holds ``backup_days`` (7) of extra stock.  This module answers the
question that sits *before* "Send to Branches": given what is on every shelf and
in the factory store today, **how many batches of each jar does the kitchen
make this round**, which bases and mixes have to be made first, and which raw
materials and packaging are short.

Layering rule, same as ``replenishment_planning``: everything here is a pure
function over plain numbers and plain mappings.  No ``frappe`` import, no
database read, no write.  ``api/replenishment.get_production_round`` resolves
the data and calls in here for every number it reports.

The load-bearing conventions:

* **Demand is weekly and per branch.**  A branch's par is set from its own
  weekly sales series, not a company-wide rate: a volatile jar needs more than
  its average, and ``1.28 * sd * sqrt(cycle_weeks)`` is the headroom that makes
  ~90% of cycles not run out.  Anything that sells holds at least
  :data:`MIN_PAR` jars.
* **Negative stock never inflates a requirement** — the rule from
  ``replenishment_planning``.  A negative ``Bin`` is a counting error: it is
  reported raw and floored to zero in every subtraction.
* **Batches, not jars.**  The kitchen makes quarter batches.  A need within a
  small tolerance of a quarter step is not worth another quarter (see
  :func:`round_to_batches`).
* **Stored sub-assemblies are netted, phantoms are not.**  A BOM line with
  ``do_not_explode = 1`` is a base the factory keeps on a shelf (Butter
  Biscuit, Fudge Cake): what is already made is used first.  A line with
  ``do_not_explode = 0`` whose item has a BOM is a phantom made fresh into the
  jar (Cheesecake Mix): it is listed so somebody makes it, and its components
  are exploded straight away.
"""

from __future__ import annotations

import datetime
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from jarz_pos.services.replenishment_planning import (
    QTY_EPSILON,
    coerce_days,
    countable,
    to_float,
)

#: ~90% one-sided service level: the chance a cycle does not run out.
SERVICE_LEVEL_Z = 1.28
#: Anything that sells keeps at least this many jars on the shelf.
MIN_PAR = 3
#: A material short by no more than this share of its need is still listed as
#: missing, but does not mark the jars that use it as blocked: one egg short
#: of 57 is a shopping note, not a reason to flag five flavours.
BLOCKING_SHARE = 0.02

DEFAULT_CYCLE_DAYS, MIN_CYCLE_DAYS, MAX_CYCLE_DAYS = 14, 1, 60
DEFAULT_BACKUP_DAYS, MIN_BACKUP_DAYS, MAX_BACKUP_DAYS = 7, 0, 30
DEFAULT_SALES_WEEKS, MIN_SALES_WEEKS, MAX_SALES_WEEKS = 8, 2, 26
DEFAULT_BATCH_MEDIUM = 120
DEFAULT_BATCH_LARGE = 77
MIN_BATCH_SIZE, MAX_BATCH_SIZE = 1, 10000

#: Item groups of the finished jars, in the order the screen lists them.
SIZE_ORDER = ("Medium", "Large")

QTY_PRECISION = 3
SALES_PRECISION = 1
BATCH_PRECISION = 2


# ── Parameters ──────────────────────────────────────────────────────────


def coerce_count(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Like ``coerce_days`` but a literal 0 is kept (then clamped).

    ``backup_days = 0`` is a real answer ("no backup week"), unlike a cycle of
    zero days.  Blank and junk still mean "not specified".
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def coerce_parameters(
    *,
    cycle_days: Any = None,
    backup_days: Any = None,
    sales_weeks: Any = None,
    batch_medium: Any = None,
    batch_large: Any = None,
) -> Dict[str, int]:
    """Every query-string knob, defaulted and clamped per the contract."""
    return {
        "cycle_days": coerce_days(
            cycle_days, default=DEFAULT_CYCLE_DAYS, minimum=MIN_CYCLE_DAYS, maximum=MAX_CYCLE_DAYS
        ),
        "backup_days": coerce_count(
            backup_days,
            default=DEFAULT_BACKUP_DAYS,
            minimum=MIN_BACKUP_DAYS,
            maximum=MAX_BACKUP_DAYS,
        ),
        "sales_weeks": coerce_days(
            sales_weeks, default=DEFAULT_SALES_WEEKS, minimum=MIN_SALES_WEEKS, maximum=MAX_SALES_WEEKS
        ),
        "batch_medium": coerce_days(
            batch_medium, default=DEFAULT_BATCH_MEDIUM, minimum=MIN_BATCH_SIZE, maximum=MAX_BATCH_SIZE
        ),
        "batch_large": coerce_days(
            batch_large, default=DEFAULT_BATCH_LARGE, minimum=MIN_BATCH_SIZE, maximum=MAX_BATCH_SIZE
        ),
    }


# ── Sales series ────────────────────────────────────────────────────────


def sales_window(today: datetime.date, weeks: int) -> Tuple[datetime.date, datetime.date]:
    """``(from, to)``: ``weeks * 7`` whole days ending **yesterday**.

    Today's part-day is excluded for the same reason as the branch plan: a
    half-finished day would drag every rate down as the morning wears on.
    """
    to_date = today - datetime.timedelta(days=1)
    from_date = today - datetime.timedelta(days=int(weeks) * 7)
    return from_date, to_date


def week_index(to_date: datetime.date, posting_date: datetime.date) -> int:
    """0 = the 7 days ending ``to_date``, 1 = the 7 before that, ..."""
    return (to_date - posting_date).days // 7


def weekly_series(buckets: Optional[Mapping[Any, Any]], weeks: int) -> List[float]:
    """``{week_index: qty}`` -> a dense list of ``weeks`` values.

    Missing weeks are zero.  A net-negative week (more returned than sold) is
    floored at zero: "sold minus four" is not demand anybody can plan for.
    Indexes outside ``0..weeks-1`` are dropped.
    """
    series = [0.0] * max(0, int(weeks))
    for key, qty in (buckets or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(series):
            series[idx] += to_float(qty, 0.0)
    return [max(0.0, value) for value in series]


def mean_and_sd(series: Sequence[float]) -> Tuple[float, float]:
    """Mean and **population** standard deviation; ``(0, 0)`` for no data."""
    values = [to_float(v, 0.0) for v in series or []]
    if not values:
        return 0.0, 0.0
    avg = sum(values) / len(values)
    variance = sum((v - avg) ** 2 for v in values) / len(values)
    return avg, math.sqrt(max(0.0, variance))


# ── Per branch, per jar ─────────────────────────────────────────────────


def par_level(*, avg: Any, sd: Any, cycle_days: Any, backup_days: Any) -> int:
    """Jars a branch should hold after a delivery.

    ``ceil(max(cover_weeks * avg, cycle_weeks * avg + 1.28 * sd * sqrt(cycle_weeks), 3))``.
    The ``max`` means a volatile or slow jar gets *more* than the flat backup
    week, never less.  A jar that does not sell has a par of zero.
    """
    avg = to_float(avg, 0.0)
    if avg <= QTY_EPSILON:
        return 0
    sd = max(0.0, to_float(sd, 0.0))
    cycle_w = to_float(cycle_days, 0.0) / 7.0
    cover_w = (to_float(cycle_days, 0.0) + to_float(backup_days, 0.0)) / 7.0
    target = max(
        cover_w * avg,
        cycle_w * avg + SERVICE_LEVEL_Z * sd * math.sqrt(max(0.0, cycle_w)),
        float(MIN_PAR),
    )
    return int(math.ceil(target - QTY_EPSILON))


def fill_qty(*, par: Any, on_hand: Any) -> int:
    """Whole jars to bring the shelf up to ``par``; negative stock floored."""
    deficit = to_float(par, 0.0) - countable(on_hand)
    if deficit <= QTY_EPSILON:
        return 0
    return int(math.ceil(deficit - QTY_EPSILON))


def days_of_cover(*, on_hand: Any, avg: Any) -> Optional[float]:
    """Days the shelf lasts at this branch's weekly rate; ``None`` if it never sells."""
    avg = to_float(avg, 0.0)
    if avg <= QTY_EPSILON:
        return None
    return round(countable(on_hand) / (avg / 7.0), SALES_PRECISION)


def is_below_backup(*, on_hand: Any, avg: Any, backup_days: Any) -> bool:
    """Sells, and the shelf holds less than the backup days' worth."""
    avg = to_float(avg, 0.0)
    if avg <= QTY_EPSILON:
        return False
    return countable(on_hand) < to_float(backup_days, 0.0) * (avg / 7.0)


# ── Batches ─────────────────────────────────────────────────────────────


def batch_tolerance(batch_size: Any) -> int:
    """Jars short that are not worth another quarter batch: Medium 8, Large 5."""
    return max(5, int(math.ceil(to_float(batch_size, 0.0) / 16.0 - QTY_EPSILON)))


def round_to_batches(net_need: Any, batch_size: Any) -> Tuple[float, int]:
    """``(batches, jars)`` for a net need, in quarter batches.

    Within the tolerance nothing is made.  Otherwise the smallest ``k >= 1``
    quarters with ``k * q >= need - tolerance``; jars are whole jars out of
    those quarters.
    """
    size = to_float(batch_size, 0.0)
    need = to_float(net_need, 0.0)
    if size <= 0:
        return 0.0, 0
    tol = batch_tolerance(size)
    if need <= tol:
        return 0.0, 0
    quarter = size / 4.0
    k = max(1, int(math.ceil((need - tol) / quarter - QTY_EPSILON)))
    return k / 4.0, int(math.floor(k * quarter + QTY_EPSILON))


def jar_status(*, weekly_sales: Any, batches: Any, any_below_backup: bool) -> str:
    if to_float(weekly_sales, 0.0) <= QTY_EPSILON:
        return "no_sales"
    if to_float(batches, 0.0) > 0:
        return "now" if any_below_backup else "round"
    return "covered"


def flavour_of(item_name: Any) -> str:
    """``"Blueberry Large"`` -> ``"Blueberry"``."""
    name = str(item_name or "").strip()
    lowered = name.lower()
    for size in SIZE_ORDER:
        suffix = " " + size.lower()
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)].strip()
    return name


# ── BOM helpers ─────────────────────────────────────────────────────────


def pick_default_boms(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``{item: {"name", "quantity"}}`` from active submitted BOM rows.

    Rows are ``{name, item, quantity, is_default, default_bom}`` where
    ``default_bom`` is the Item's own field.  The Item's ``default_bom`` wins;
    otherwise the BOM flagged ``is_default``.  An item with neither has no BOM.
    """
    by_item: Dict[str, Dict[str, Any]] = {}
    rank: Dict[str, int] = {}
    for row in sorted(rows or [], key=lambda r: str(r.get("name") or "")):
        item = str(row.get("item") or "").strip()
        name = str(row.get("name") or "").strip()
        if not item or not name:
            continue
        if name == str(row.get("default_bom") or "").strip():
            score = 2
        elif int(to_float(row.get("is_default"), 0.0)):
            score = 1
        else:
            continue
        if score > rank.get(item, 0):
            rank[item] = score
            by_item[item] = {"name": name, "quantity": to_float(row.get("quantity"), 0.0)}
    return by_item


def alternatives_map(rows: Iterable[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """``tabItem Alternative`` rows -> ``{item: [alternatives]}``.

    ``two_way = 1`` works in both directions.
    """
    out: Dict[str, Set[str]] = {}
    for row in rows or []:
        item = str(row.get("item_code") or "").strip()
        alt = str(row.get("alternative_item_code") or "").strip()
        if not item or not alt or item == alt:
            continue
        out.setdefault(item, set()).add(alt)
        if int(to_float(row.get("two_way"), 0.0)):
            out.setdefault(alt, set()).add(item)
    return {code: sorted(alts) for code, alts in out.items()}


def _bom_yield(bom: Mapping[str, Any]) -> float:
    quantity = to_float(bom.get("quantity"), 0.0)
    return quantity if quantity > QTY_EPSILON else 1.0


def _line_code(line: Mapping[str, Any]) -> str:
    return str(line.get("item_code") or "").strip()


def bom_order(boms: Mapping[str, Mapping[str, Any]]) -> Tuple[List[str], Set[Tuple[str, str]]]:
    """Every BOM item, parents before children, plus the edges that close a cycle.

    A sub-assembly's demand only ever comes from its ancestors, so walking this
    order guarantees a stored base is netted after everything that uses it has
    added its share.  A cyclic edge is reported so the explosion skips it
    instead of recursing forever.
    """
    white, grey, black = 0, 1, 2
    colour: Dict[str, int] = {code: white for code in boms}
    postorder: List[str] = []
    cyclic: Set[Tuple[str, str]] = set()

    def children(code: str) -> List[str]:
        seen: List[str] = []
        for line in boms.get(code, {}).get("lines") or []:
            child = _line_code(line)
            if child in boms and child not in seen:
                seen.append(child)
        return sorted(seen)

    for root in sorted(boms):
        if colour[root] != white:
            continue
        # Iterative DFS: (node, iterator over its children).
        colour[root] = grey
        stack: List[Tuple[str, Iterable[str]]] = [(root, iter(children(root)))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for child in it:
                if colour[child] == grey:
                    cyclic.add((node, child))
                elif colour[child] == white:
                    colour[child] = grey
                    stack.append((child, iter(children(child))))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                colour[node] = black
                postorder.append(node)

    return list(reversed(postorder)), cyclic


def explode_round(
    roots: Mapping[str, Any],
    boms: Mapping[str, Mapping[str, Any]],
    stock: Mapping[str, Any],
) -> Dict[str, Any]:
    """Explode the jars to make into sub-assemblies and leaf materials.

    ``roots`` is ``{jar item_code: jars to make}``.  ``boms`` is
    ``{item_code: {"name", "quantity", "lines": [{item_code, stock_qty,
    stock_uom, do_not_explode}]}}``.  ``stock`` is raw on-hand per item.

    Returns ``{"stored", "fresh", "to_make", "materials", "used_by",
    "cycles"}``: stored/fresh sub-assembly requirements, the netted quantity
    of each stored one, leaf requirements, and for every component the set of
    jar item_codes that ultimately need it.
    """
    order, cyclic = bom_order(boms)
    stored: Dict[str, float] = {}
    fresh: Dict[str, float] = {}
    materials: Dict[str, float] = {}
    used_by: Dict[str, Set[str]] = {}

    def explode(code: str, qty: float, jars: Set[str], path: Set[str]) -> None:
        bom = boms.get(code)
        if not bom or qty <= QTY_EPSILON:
            return
        per_unit = qty / _bom_yield(bom)
        for line in bom.get("lines") or []:
            child = _line_code(line)
            if not child or (code, child) in cyclic or child in path:
                continue
            amount = to_float(line.get("stock_qty"), 0.0) * per_unit
            if amount <= QTY_EPSILON:
                continue
            used_by.setdefault(child, set()).update(jars)
            if child in boms:
                if int(to_float(line.get("do_not_explode"), 0.0)):
                    stored[child] = stored.get(child, 0.0) + amount
                else:
                    fresh[child] = fresh.get(child, 0.0) + amount
                    explode(child, amount, jars, path | {child})
            else:
                materials[child] = materials.get(child, 0.0) + amount

    for jar in sorted(roots):
        qty = to_float(roots[jar], 0.0)
        if qty > QTY_EPSILON:
            explode(jar, qty, {jar}, {jar})

    to_make: Dict[str, float] = {}
    for code in order:
        if code not in stored:
            continue
        make = max(0.0, stored[code] - countable(stock.get(code)))
        to_make[code] = make
        if make > QTY_EPSILON:
            explode(code, make, set(used_by.get(code, set())), {code})

    return {
        "order": order,
        "stored": stored,
        "fresh": fresh,
        "to_make": to_make,
        "materials": materials,
        "used_by": used_by,
        "cycles": sorted(cyclic),
    }


# ── Rows ────────────────────────────────────────────────────────────────


def _meta(item_meta: Mapping[str, Mapping[str, Any]], code: str) -> Mapping[str, Any]:
    return item_meta.get(code) or {}


def build_prep_rows(
    exploded: Mapping[str, Any],
    boms: Mapping[str, Mapping[str, Any]],
    stock: Mapping[str, Any],
    item_meta: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Sub-assemblies in the order to make them: deepest first."""
    rows: List[Dict[str, Any]] = []
    for code in reversed(exploded.get("order") or []):
        bom = boms.get(code) or {}
        batch_yield = _bom_yield(bom)
        meta = _meta(item_meta, code)
        on_hand = to_float(stock.get(code), 0.0)
        base = {
            "item_code": code,
            "item_name": str(meta.get("item_name") or code),
            "uom": str(meta.get("stock_uom") or ""),
            "on_hand": round(on_hand, QTY_PRECISION),
            "batch_yield": round(batch_yield, QTY_PRECISION),
        }
        if code in exploded["stored"]:
            required = exploded["stored"][code]
            make = exploded["to_make"].get(code, 0.0)
            rows.append(
                {
                    **base,
                    "required": round(required, QTY_PRECISION),
                    "to_make": round(make, QTY_PRECISION),
                    "batches": round(make / batch_yield, BATCH_PRECISION),
                    "made_fresh": False,
                }
            )
        if code in exploded["fresh"]:
            required = exploded["fresh"][code]
            rows.append(
                {
                    **base,
                    "required": round(required, QTY_PRECISION),
                    "to_make": round(required, QTY_PRECISION),
                    "batches": round(required / batch_yield, BATCH_PRECISION),
                    "made_fresh": True,
                }
            )
    return rows


def build_material_rows(
    exploded: Mapping[str, Any],
    stock: Mapping[str, Any],
    alternatives: Mapping[str, Sequence[str]],
    item_meta: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Leaf materials, missing first (largest share missing), then by name."""
    rows: List[Dict[str, Any]] = []
    for code, required in (exploded.get("materials") or {}).items():
        meta = _meta(item_meta, code)
        on_hand = to_float(stock.get(code), 0.0)
        alt_on_hand = sum(
            countable(stock.get(alt)) for alt in alternatives.get(code) or [] if alt != code
        )
        missing = max(0.0, required - countable(on_hand) - alt_on_hand)
        if meta.get("whole_number"):
            # Eggs, lids and labels come whole: "short 0.878 eggs" is one egg.
            required = float(math.ceil(required - QTY_EPSILON))
            missing = float(math.ceil(missing - QTY_EPSILON)) if missing > QTY_EPSILON else 0.0
        rows.append(
            {
                "item_code": code,
                "item_name": str(meta.get("item_name") or code),
                "item_group": str(meta.get("item_group") or ""),
                "uom": str(meta.get("stock_uom") or ""),
                "required": round(required, QTY_PRECISION),
                "on_hand": round(on_hand, QTY_PRECISION),
                "alternative_on_hand": round(alt_on_hand, QTY_PRECISION),
                "missing": round(missing, QTY_PRECISION),
                "used_by": sorted((exploded.get("used_by") or {}).get(code, set())),
            }
        )
    rows.sort(
        key=lambda row: (
            0 if row["missing"] > 0 else 1,
            -(row["missing"] / row["required"]) if row["required"] > 0 else 0.0,
            row["item_name"].lower(),
            row["item_code"],
        )
    )
    return rows


def _size_rank(size: str) -> int:
    return SIZE_ORDER.index(size) if size in SIZE_ORDER else len(SIZE_ORDER)


def build_item_row(
    item: Mapping[str, Any],
    *,
    branches: Sequence[Mapping[str, Any]],
    weekly_sales: Mapping[Tuple[str, str], Mapping[Any, Any]],
    branch_stock: Mapping[Tuple[str, str], Any],
    factory_on_hand: Any,
    batch_sizes: Mapping[str, int],
    cycle_days: int,
    backup_days: int,
    sales_weeks: int,
) -> Dict[str, Any]:
    """One jar across every branch: par, fill, batches, status."""
    code = str(item.get("item_code") or "")
    name = str(item.get("item_name") or code)
    size = str(item.get("item_group") or "")
    batch_size = int(batch_sizes.get(size) or batch_sizes.get(SIZE_ORDER[0]) or DEFAULT_BATCH_MEDIUM)

    branch_rows: List[Dict[str, Any]] = []
    total_avg = 0.0
    total_fill = 0
    any_below = False
    for branch in branches:
        warehouse = str(branch.get("warehouse") or "")
        series = weekly_series(weekly_sales.get((warehouse, code)), sales_weeks)
        avg, sd = mean_and_sd(series)
        on_hand = to_float(branch_stock.get((warehouse, code)), 0.0)
        par = par_level(avg=avg, sd=sd, cycle_days=cycle_days, backup_days=backup_days)
        fill = fill_qty(par=par, on_hand=on_hand)
        below = is_below_backup(on_hand=on_hand, avg=avg, backup_days=backup_days)
        total_avg += avg
        total_fill += fill
        any_below = any_below or below
        branch_rows.append(
            {
                "warehouse": warehouse,
                "label": str(branch.get("label") or branch.get("branch") or warehouse),
                "weekly_sales": round(avg, SALES_PRECISION),
                "par": par,
                "on_hand": on_hand,
                "fill": fill,
                "days_of_cover": days_of_cover(on_hand=on_hand, avg=avg),
                "below_backup": below,
                "stock_is_negative": on_hand < 0,
            }
        )

    factory = to_float(factory_on_hand, 0.0)
    net_need = fill_qty(par=total_fill, on_hand=factory)
    batches, jars = round_to_batches(net_need, batch_size)
    return {
        "item_code": code,
        "item_name": name,
        "flavour": flavour_of(name),
        "size": size,
        "batch_size": batch_size,
        "weekly_sales": round(total_avg, SALES_PRECISION),
        "factory_on_hand": factory,
        "total_fill": total_fill,
        "net_need": net_need,
        "batches": batches,
        "jars": jars,
        "status": jar_status(weekly_sales=total_avg, batches=batches, any_below_backup=any_below),
        "blocked_by": [],
        "branches": branch_rows,
    }


def summarise_branches(
    branches: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for branch in branches:
        warehouse = str(branch.get("warehouse") or "")
        rows = [
            row
            for item in items
            for row in item.get("branches") or []
            if row.get("warehouse") == warehouse
        ]
        out.append(
            {
                "warehouse": warehouse,
                "label": str(branch.get("label") or branch.get("branch") or warehouse),
                "weekly_sales": round(
                    sum(to_float(r.get("weekly_sales"), 0.0) for r in rows), SALES_PRECISION
                ),
                "par_total": sum(int(r.get("par") or 0) for r in rows),
                "on_hand_total": round(sum(countable(r.get("on_hand")) for r in rows), QTY_PRECISION),
                "fill_total": sum(int(r.get("fill") or 0) for r in rows),
                "below_backup_count": sum(1 for r in rows if r.get("below_backup")),
            }
        )
    return out


# ── Payload ─────────────────────────────────────────────────────────────


def build_production_round(
    *,
    generated_on: str,
    company: Optional[str],
    source_warehouse: Optional[str],
    cycle_days: int,
    backup_days: int,
    sales_weeks: int,
    sales_from: Any,
    sales_to: Any,
    batch_sizes: Mapping[str, int],
    items: Sequence[Mapping[str, Any]],
    branches: Sequence[Mapping[str, Any]],
    weekly_sales: Mapping[Tuple[str, str], Mapping[Any, Any]],
    branch_stock: Mapping[Tuple[str, str], Any],
    factory_stock: Mapping[str, Any],
    boms: Mapping[str, Mapping[str, Any]],
    material_stock: Mapping[str, Any],
    alternatives: Mapping[str, Sequence[str]],
    item_meta: Mapping[str, Mapping[str, Any]],
    notices: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """The whole ``get_production_round`` payload, from plain data.

    ``weekly_sales`` is ``{(warehouse, item_code): {week_index: qty}}`` and
    ``branch_stock`` is ``{(warehouse, item_code): raw qty}``.  ``branches``
    are ``{warehouse, branch|label}`` as ``select_selling_warehouses`` returns.
    """
    notes: List[str] = [str(n) for n in notices or [] if n]
    batch_sizes = {str(k): int(v) for k, v in (batch_sizes or {}).items()}
    branches = [b for b in branches or [] if str(b.get("warehouse") or "").strip()]

    item_rows = [
        build_item_row(
            item,
            branches=branches,
            weekly_sales=weekly_sales or {},
            branch_stock=branch_stock or {},
            factory_on_hand=(factory_stock or {}).get(str(item.get("item_code") or "")),
            batch_sizes=batch_sizes,
            cycle_days=cycle_days,
            backup_days=backup_days,
            sales_weeks=sales_weeks,
        )
        for item in items or []
        if str(item.get("item_code") or "").strip()
    ]
    item_rows.sort(
        key=lambda row: (_size_rank(row["size"]), row["flavour"].lower(), row["item_code"])
    )

    boms = boms or {}
    roots: Dict[str, float] = {}
    for row in item_rows:
        if row["jars"] <= 0:
            continue
        if row["item_code"] in boms:
            roots[row["item_code"]] = float(row["jars"])
        else:
            notes.append(
                f"{row['item_name']} has no active default BOM; its materials are not included."
            )

    exploded = explode_round(roots, boms, material_stock or {})
    for parent, child in exploded["cycles"]:
        notes.append(f"BOM cycle: {parent} uses {child}, which leads back to it; that line was skipped.")

    prep = build_prep_rows(exploded, boms, material_stock or {}, item_meta or {})
    materials = build_material_rows(exploded, material_stock or {}, alternatives or {}, item_meta or {})

    blocking_codes = [
        row["item_code"]
        for row in materials
        if row["missing"] > 0 and row["missing"] > BLOCKING_SHARE * row["required"]
    ]
    for row in item_rows:
        if row["jars"] > 0:
            row["blocked_by"] = [
                code for code in blocking_codes
                if row["item_code"] in (exploded["used_by"].get(code) or set())
            ]

    branch_rows = summarise_branches(branches, item_rows)
    for branch in branches:
        warehouse = str(branch.get("warehouse") or "")
        negative = sum(
            1
            for item in item_rows
            for row in item["branches"]
            if row["warehouse"] == warehouse and row["stock_is_negative"]
        )
        if negative:
            label = str(branch.get("label") or branch.get("branch") or warehouse)
            notes.append(
                f"{label} has {negative} jar(s) with negative stock - count the shelf before loading."
            )

    sizes = list(batch_sizes) or list(SIZE_ORDER)
    to_make = [row for row in item_rows if row["jars"] > 0]
    summary = {
        "batches": {
            size: round(
                sum((r["batches"] for r in to_make if r["size"] == size), 0.0), BATCH_PRECISION
            )
            for size in sizes
        },
        "jars": {size: sum(r["jars"] for r in to_make if r["size"] == size) for size in sizes},
        "jars_total": sum(r["jars"] for r in to_make),
        "items_to_make": len(to_make),
        "needed_now_count": sum(1 for r in item_rows if r["status"] == "now"),
        "missing_count": sum(1 for row in materials if row["missing"] > 0),
        "blocked_count": sum(1 for r in to_make if r["blocked_by"]),
    }

    return {
        "generated_on": generated_on,
        "company": company,
        "source_warehouse": source_warehouse,
        "cycle_days": int(cycle_days),
        "backup_days": int(backup_days),
        "cover_days": int(cycle_days) + int(backup_days),
        "sales_weeks": int(sales_weeks),
        "sales_from": str(sales_from) if sales_from else None,
        "sales_to": str(sales_to) if sales_to else None,
        "batch_sizes": batch_sizes,
        "summary": summary,
        "branches": branch_rows,
        "items": item_rows,
        "prep": prep,
        "materials": materials,
        "notices": notes,
    }

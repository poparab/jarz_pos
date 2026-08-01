"""Production planning maths for the Production Board.

This module answers the two questions the Manufacturing screen never could:
*how much should we make* and *how much can we actually make right now*.

Layering rule: this is the shared layer.  ``api/production.py`` and
``api/manufacturing.py`` both import **downwards** into it; it never imports
either of them at module scope.  The one place it needs
``manufacturing._get_required_material_rows`` the import is deferred into the
resolver body, which both breaks the cycle and keeps the call patchable.

Testability contract: every arithmetic helper below the resolver block is a
pure function over plain numbers, so its tests need no ``frappe`` patching at
all.  Everything that touches frappe/ERPNext sits behind a ``_resolve_x()``
accessor for the same reason.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe

from jarz_pos.constants import DEFAULT_UOM

# Used when neither the Item override nor the Jarz Forecast Settings default
# is set.  The Settings field ships via DocType JSON rather than a fixture, so
# the existing Single row reads empty until somebody saves it — every read has
# to survive that.
DEFAULT_TARGET_DAYS = 7

# Warehouse types whose stock must not count towards a finished good's
# on-hand.  A batch sitting in WIP would otherwise inflate its own cover and
# suppress its own suggestion.
NON_SELLABLE_WAREHOUSE_TYPES = ("Work In Progress", "Rejected")

STATUS_CRITICAL = "critical"
STATUS_LOW = "low"
STATUS_OK = "ok"
STATUS_OVERSTOCKED = "overstocked"
STATUS_NO_VELOCITY = "no_velocity"


# ── Pure arithmetic ─────────────────────────────────────────────────────
# No frappe below this line until the resolver block.


def suggested_batches(
    *,
    target_days: float,
    velocity: float,
    season_multiplier: float,
    on_hand: float,
    bom_yield: float,
) -> int:
    """How many whole BOM batches to run to reach ``target_days`` of cover.

    Rounded up to a whole batch, because half a BOM is not a thing anyone can
    make.  The epsilon is load-bearing: ``12.0 / 4.0`` can land on
    ``3.0000000000000004`` in float, and a naive ``ceil`` would then ask for a
    fourth batch nobody needs.
    """
    bom_yield = float(bom_yield or 0)
    if bom_yield <= 0:
        return 0

    effective = float(velocity or 0) * float(season_multiplier or 1.0)
    deficit = (float(target_days or 0) * effective) - float(on_hand or 0)
    if deficit <= 0:
        return 0

    return max(0, math.ceil((deficit / bom_yield) - 1e-9))


def days_of_cover(
    *,
    on_hand: float,
    velocity: float,
    season_multiplier: float,
) -> Optional[float]:
    """Days of demand the current stock covers, or ``None`` if it never sells.

    ``None`` rather than a sentinel: the stored ``jarz_days_of_stock`` field
    writes ``999`` for zero-velocity items, which makes "never sold" and "huge
    pile" indistinguishable downstream.
    """
    effective = float(velocity or 0) * float(season_multiplier or 1.0)
    if effective <= 0:
        return None
    return float(on_hand or 0) / effective


def status_for_days_of_cover(
    days: Optional[float],
    *,
    critical_days: float,
    watch_days: float,
    overstock_days: float,
) -> str:
    """Bucket a cover figure against the Jarz Forecast Settings thresholds."""
    if days is None:
        return STATUS_NO_VELOCITY
    if days <= float(critical_days):
        return STATUS_CRITICAL
    if days <= float(watch_days):
        return STATUS_LOW
    if days > float(overstock_days):
        return STATUS_OVERSTOCKED
    return STATUS_OK


def can_make_now(components: Sequence[Dict[str, Any]]) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Whole batches makeable from stock on hand, plus the component that caps it.

    ``components`` must be the required-material rows for **exactly one** BOM
    batch — i.e. ``_get_required_material_rows(bom, company, bom_qty)``.

    Returns ``(None, None)`` for a BOM with no consuming components, which is
    unbounded rather than zero.
    """
    best_count: Optional[int] = None
    best_row: Optional[Dict[str, Any]] = None

    for row in components or []:
        required = float(row.get("required_qty") or 0)
        if required <= 0:
            # Zero-quantity BOM lines place no constraint on the batch count.
            continue

        if not row.get("source_warehouse"):
            # Nothing can be transferred from a warehouse that was never set,
            # so this blocks the batch outright regardless of stock levels.
            return 0, dict(row, reason="missing_source_warehouse")

        count = int(math.floor(float(row.get("available_qty") or 0) / required))
        if best_count is None or count < best_count:
            best_count = count
            best_row = dict(row, reason="insufficient_stock")

    if best_count is None:
        return None, None
    return best_count, best_row


def aggregate_basket_materials(
    line_component_sets: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Roll raw-material requirements up across every line in the basket.

    This is the fix for the per-line shortage hole: two lines that both consume
    flour were each measured against the same available quantity, so both could
    pass while the pair busted the warehouse.

    Aggregation key is ``(item_code, source_warehouse)`` — the same material in
    the same warehouse must sum, the same material in two different warehouses
    must not.

    Each entry of ``line_component_sets`` is
    ``{"line_index": int, "item_code": str, "components": [rows]}`` where the
    rows are already scaled to that line's full quantity.
    """
    aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for entry in line_component_sets or []:
        line_index = entry.get("line_index")
        line_item = entry.get("item_code")
        for row in entry.get("components") or []:
            item_code = str(row.get("item_code") or "")
            warehouse = str(row.get("source_warehouse") or "")
            key = (item_code, warehouse)

            bucket = aggregated.get(key)
            if bucket is None:
                bucket = {
                    "item_code": item_code,
                    "item_name": row.get("item_name") or item_code,
                    "uom": row.get("uom") or DEFAULT_UOM,
                    "source_warehouse": warehouse or None,
                    # available_qty is a property of the (item, warehouse)
                    # pair, so it is set once and never accumulated.
                    "available_qty": float(row.get("available_qty") or 0),
                    "required_qty": 0.0,
                    "contributing_lines": [],
                }
                aggregated[key] = bucket

            bucket["required_qty"] += float(row.get("required_qty") or 0)
            bucket["contributing_lines"].append(
                {
                    "line_index": line_index,
                    "item_code": line_item,
                    "required_qty": float(row.get("required_qty") or 0),
                }
            )

    components = sorted(
        aggregated.values(),
        key=lambda r: (r["item_code"], r["source_warehouse"] or ""),
    )

    shortages: List[Dict[str, Any]] = []
    for row in components:
        if not row["source_warehouse"]:
            row["missing_qty"] = row["required_qty"]
            row["reason"] = "missing_source_warehouse"
            shortages.append(row)
            continue

        missing = row["required_qty"] - row["available_qty"]
        # Same tolerance the per-line precheck uses, so the two agree at the
        # boundary instead of disagreeing by a float hair.
        if missing > 1e-9:
            row["missing_qty"] = missing
            row["reason"] = "insufficient_stock"
            shortages.append(row)

    return {
        "ok": not shortages,
        "components": components,
        "shortages": shortages,
        "max_feasible_scale": _max_feasible_scale(components),
    }


def _max_feasible_scale(components: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Largest uniform fraction of the basket the warehouse can actually cover.

    ``1.0`` means the basket fits as-is; ``0.6`` means every line would have to
    shrink to 60% to fit.  ``None`` when nothing constrains it.
    """
    scale: Optional[float] = None
    for row in components:
        required = float(row.get("required_qty") or 0)
        if required <= 0:
            continue
        if not row.get("source_warehouse"):
            return 0.0
        ratio = float(row.get("available_qty") or 0) / required
        if scale is None or ratio < scale:
            scale = ratio
    if scale is None:
        return None
    return min(1.0, scale)


def resolve_target_days(
    item_override: Any,
    default_target_days: Any,
) -> Tuple[int, str]:
    """Target days of cover for one item, and where the number came from.

    Precedence: the Item override, then the Jarz Forecast Settings default,
    then ``DEFAULT_TARGET_DAYS``.  Blank and zero both mean "not set" — a zero
    target would suppress every suggestion, which is never what an empty field
    is trying to say.
    """
    try:
        override = int(item_override or 0)
    except (TypeError, ValueError):
        override = 0
    if override > 0:
        return override, "item"

    try:
        default = int(default_target_days or 0)
    except (TypeError, ValueError):
        default = 0
    if default > 0:
        return default, "default"

    return DEFAULT_TARGET_DAYS, "fallback"


def scale_components(
    components: Sequence[Dict[str, Any]],
    *,
    factor: float,
) -> List[Dict[str, Any]]:
    """Multiply per-batch requirement rows by a batch count."""
    factor = float(factor or 0)
    return [dict(row, required_qty=float(row.get("required_qty") or 0) * factor) for row in components or []]


# ── frappe / ERPNext resolvers ──────────────────────────────────────────
# Everything that touches the database sits behind one of these so tests can
# patch a single symbol instead of the world.


def _resolve_get_settings():
    from jarz_pos.services.demand_forecasting import get_settings

    return get_settings


def _resolve_get_active_seasonal_multiplier():
    from jarz_pos.services.demand_forecasting import get_active_seasonal_multiplier

    return get_active_seasonal_multiplier


def _resolve_required_material_rows():
    """Deferred so ``manufacturing`` can import this module without a cycle."""
    from jarz_pos.api.manufacturing import _get_required_material_rows

    return _get_required_material_rows


def _resolve_default_company() -> str:
    try:
        return frappe.db.get_single_value("Global Defaults", "default_company") or ""
    except Exception:
        return ""


def _resolve_bom_company(bom_name: str) -> str:
    try:
        return frappe.db.get_value("BOM", bom_name, "company") or ""
    except Exception:
        return ""


def _resolve_producible_rows(company: str, search: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every item with a submitted default BOM, joined to its velocity fields.

    Deliberately not ``list_default_bom_items``: that one orders by
    ``Item.modified DESC`` and hard-caps at 100, so with no search term it
    returns an arbitrary slice of the catalogue rather than the whole board.
    """
    conditions = ["b.is_default = 1", "b.docstatus = 1", "i.disabled = 0"]
    values: Dict[str, Any] = {}

    if company:
        conditions.append("b.company = %(company)s")
        values["company"] = company

    term = (search or "").strip()
    if term:
        conditions.append("(i.name LIKE %(q)s OR i.item_name LIKE %(q)s)")
        values["q"] = f"%{term}%"

    where = " AND ".join(conditions)
    rows = frappe.db.sql(
        f"""
        SELECT
            i.name                          AS item_code,
            COALESCE(i.item_name, i.name)   AS item_name,
            i.stock_uom                     AS stock_uom,
            i.item_group                    AS item_group,
            b.name                          AS default_bom,
            b.quantity                      AS bom_qty,
            b.company                       AS company,
            i.jarz_velocity_30d             AS velocity_30d,
            i.jarz_velocity_60d             AS velocity_60d,
            i.jarz_velocity_trend           AS velocity_trend,
            i.jarz_velocity_updated_on      AS velocity_updated_on,
            i.jarz_target_days_of_cover     AS target_days_override
        FROM `tabBOM` b
        INNER JOIN `tabItem` i ON i.name = b.item
        WHERE {where}
        ORDER BY i.item_name ASC
        """,
        values,
        as_dict=True,
    )
    return [dict(r) for r in rows]


def _resolve_on_hand_map(item_codes: Sequence[str]) -> Dict[str, float]:
    """Sellable stock per item, in one query, excluding WIP and Rejected.

    ``demand_forecasting.get_current_stock`` sums every warehouse and runs one
    query per item — wrong on both counts for a board that renders the whole
    catalogue at once.
    """
    codes = [c for c in (item_codes or []) if c]
    if not codes:
        return {}

    rows = frappe.db.sql(
        """
        SELECT b.item_code AS item_code, SUM(b.actual_qty) AS qty
        FROM `tabBin` b
        INNER JOIN `tabWarehouse` w ON w.name = b.warehouse
        WHERE b.item_code IN %(codes)s
          AND COALESCE(w.warehouse_type, '') NOT IN %(excluded)s
        GROUP BY b.item_code
        """,
        {"codes": codes, "excluded": NON_SELLABLE_WAREHOUSE_TYPES},
        as_dict=True,
    )
    return {r["item_code"]: float(r["qty"] or 0) for r in rows}


def _resolve_bin_stock_map(pairs: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], float]:
    """Stock for a batch of (item_code, warehouse) pairs in a single query.

    Feeding ``can_make_now`` from this dict is what keeps the board off an N+1:
    one BOM explosion per item is unavoidable, one ``Bin`` read per *component*
    is not.
    """
    wanted = {(str(i), str(w)) for i, w in (pairs or []) if i and w}
    if not wanted:
        return {}

    rows = frappe.db.sql(
        """
        SELECT item_code, warehouse, SUM(actual_qty) AS qty
        FROM `tabBin`
        WHERE item_code IN %(codes)s AND warehouse IN %(warehouses)s
        GROUP BY item_code, warehouse
        """,
        {
            "codes": sorted({i for i, _ in wanted}),
            "warehouses": sorted({w for _, w in wanted}),
        },
        as_dict=True,
    )
    return {(r["item_code"], r["warehouse"]): float(r["qty"] or 0) for r in rows}


def _resolve_cache():
    return frappe.cache()


# ── Composition helpers used by api/production.py ───────────────────────


def get_planning_context(company: str) -> Dict[str, Any]:
    """Thresholds, season, and the default cover target, read once per request."""
    settings = _resolve_get_settings()()
    multiplier, season_name = _resolve_get_active_seasonal_multiplier()(settings)

    return {
        "company": company,
        "season": {"name": season_name, "multiplier": float(multiplier or 1.0)},
        "default_target_days": int(getattr(settings, "default_target_days_of_cover", 0) or 0)
        or DEFAULT_TARGET_DAYS,
        "thresholds": {
            "critical_days": int(getattr(settings, "critical_days_threshold", 0) or 5),
            "watch_days": int(getattr(settings, "watch_days_threshold", 0) or 14),
            "overstock_days": int(getattr(settings, "overstock_days_threshold", 0) or 90),
        },
    }


def build_capacity_map(rows: Sequence[Dict[str, Any]], company: str) -> Dict[str, Dict[str, Any]]:
    """Per-item ``can_make_now`` for a whole page of producible items.

    One BOM explosion per item, then a **single** batched ``Bin`` read for
    every component of every BOM — the available quantities that come back on
    the explosion rows are discarded in favour of that batched read.
    """
    exploded: Dict[str, List[Dict[str, Any]]] = {}
    required_rows = _resolve_required_material_rows()

    for row in rows:
        item_code = row["item_code"]
        try:
            exploded[item_code] = required_rows(
                row["default_bom"],
                row.get("company") or company,
                float(row.get("bom_qty") or 1),
            )
        except Exception:
            # A single unexplodable BOM must not blank the whole board.
            frappe.log_error(
                title="JARZ Production – BOM explosion failed",
                message=f"item={item_code} bom={row.get('default_bom')}",
            )
            exploded[item_code] = []

    pairs = [
        (c["item_code"], c["source_warehouse"])
        for comps in exploded.values()
        for c in comps
        if c.get("source_warehouse")
    ]
    stock = _resolve_bin_stock_map(pairs)

    capacity: Dict[str, Dict[str, Any]] = {}
    for item_code, comps in exploded.items():
        priced = [
            dict(c, available_qty=stock.get((c["item_code"], c.get("source_warehouse")), 0.0))
            for c in comps
        ]
        count, limiting = can_make_now(priced)
        capacity[item_code] = {"can_make_now_batches": count, "limiting_component": limiting}

    return capacity


def build_basket_rollup(lines: Sequence[Dict[str, Any]], company: str) -> Dict[str, Any]:
    """Aggregate the material demand of a whole basket of production lines."""
    required_rows = _resolve_required_material_rows()
    sets: List[Dict[str, Any]] = []

    for index, line in enumerate(lines or []):
        bom_name = line.get("bom_name")
        line_company = _resolve_bom_company(bom_name) or company
        components = required_rows(bom_name, line_company, float(line.get("item_qty") or 0))
        sets.append(
            {
                "line_index": index,
                "item_code": line.get("item_code"),
                "components": components,
            }
        )

    # Re-read availability in one batch so every line is measured against the
    # same snapshot rather than N snapshots taken at N different moments.
    pairs = [
        (c["item_code"], c["source_warehouse"])
        for entry in sets
        for c in entry["components"]
        if c.get("source_warehouse")
    ]
    stock = _resolve_bin_stock_map(pairs)
    for entry in sets:
        entry["components"] = [
            dict(c, available_qty=stock.get((c["item_code"], c.get("source_warehouse")), 0.0))
            for c in entry["components"]
        ]

    return aggregate_basket_materials(sets)

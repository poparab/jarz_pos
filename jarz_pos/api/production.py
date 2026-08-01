"""Production Board API — what to make, how much, and what's actually possible.

Read-only planning endpoints.  Nothing here writes stock or GL; the only write
is ``set_item_target_days``, which sets a planning preference on an Item and is
manager-gated.

Kept separate from ``api/manufacturing.py`` (already ~800 lines) and layered
above ``services/production_planning.py``, which holds the maths.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _

from jarz_pos.constants import ROLES
from jarz_pos.services import production_planning as planning

# Suggestions are recomputed from Bin and BOM data that moves constantly, but a
# board being refreshed by several people at once should not re-explode every
# BOM per pull.  Two minutes is short enough that a stock movement shows up
# while someone is still standing at the bench.
SUGGESTIONS_CACHE_TTL_SEC = 120


def _ensure_production_view_access() -> None:
    """Gate the read-only board.

    Wider than ``ROLES.MANUFACTURING`` on purpose — see the comment on
    ``ROLES.PRODUCTION_VIEW``.  Submitting Work Orders keeps the narrower gate.
    """
    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.PRODUCTION_VIEW):
        frappe.throw(_("Not permitted: production access required"), frappe.PermissionError)


def _ensure_manager_access() -> None:
    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.MANUFACTURING):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_lines(lines: Any) -> List[Dict[str, Any]]:
    if isinstance(lines, str):
        try:
            lines = json.loads(lines)
        except Exception:
            frappe.throw(_("Invalid JSON payload for lines"))
    if not isinstance(lines, list):
        frappe.throw(_("lines must be a list"))

    out: List[Dict[str, Any]] = []
    for i, item in enumerate(lines):
        if not isinstance(item, dict):
            frappe.throw(_("lines[{0}] must be an object").format(i))
        for required in ("item_code", "bom_name", "item_qty"):
            if not item.get(required):
                frappe.throw(_("Missing required field {0} in lines[{1}]").format(required, i))
        out.append(item)
    return out


def _build_suggestion(
    row: Dict[str, Any],
    *,
    context: Dict[str, Any],
    on_hand: float,
    capacity: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    season_multiplier = context["season"]["multiplier"]
    thresholds = context["thresholds"]

    # Deliberately recomputed from jarz_velocity_60d rather than read from
    # jarz_days_of_stock: that stored field bakes in whatever season was active
    # when the weekly job last ran, and writes 999 for anything that never sold.
    velocity_60d = float(row.get("velocity_60d") or 0)
    cover = planning.days_of_cover(
        on_hand=on_hand,
        velocity=velocity_60d,
        season_multiplier=season_multiplier,
    )
    status = planning.status_for_days_of_cover(cover, **thresholds)

    target_days, target_source = planning.resolve_target_days(
        row.get("target_days_override"),
        context["default_target_days"],
    )

    bom_qty = float(row.get("bom_qty") or 1)
    batches = planning.suggested_batches(
        target_days=target_days,
        velocity=velocity_60d,
        season_multiplier=season_multiplier,
        on_hand=on_hand,
        bom_yield=bom_qty,
    )

    return {
        "item_code": row["item_code"],
        "item_name": row.get("item_name") or row["item_code"],
        "item_group": row.get("item_group"),
        "stock_uom": row.get("stock_uom"),
        "default_bom": row.get("default_bom"),
        "bom_qty": bom_qty,
        "company": row.get("company"),
        "on_hand": on_hand,
        "velocity_30d": float(row.get("velocity_30d") or 0),
        "velocity_60d": velocity_60d,
        "velocity_trend": row.get("velocity_trend"),
        "velocity_updated_on": row.get("velocity_updated_on"),
        "season_multiplier": season_multiplier,
        "effective_velocity": velocity_60d * season_multiplier,
        "target_days": target_days,
        "target_days_source": target_source,
        "days_of_cover": cover,
        "status": status,
        "suggested_batches": batches,
        "suggested_units": batches * bom_qty,
        "can_make_now_batches": (capacity or {}).get("can_make_now_batches"),
        "limiting_component": (capacity or {}).get("limiting_component"),
    }


def _summarize(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {
        planning.STATUS_CRITICAL: 0,
        planning.STATUS_LOW: 0,
        planning.STATUS_OK: 0,
        planning.STATUS_OVERSTOCKED: 0,
        planning.STATUS_NO_VELOCITY: 0,
    }
    total_batches = 0
    capped = 0

    for item in items:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
        total_batches += item["suggested_batches"]
        capacity = item.get("can_make_now_batches")
        if capacity is not None and item["suggested_batches"] > capacity:
            capped += 1

    summary["total_suggested_batches"] = total_batches
    summary["capped_by_materials"] = capped
    return summary


@frappe.whitelist()
def get_production_suggestions(
    company: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = None,
    include_capacity: Any = 1,
    force_refresh: Any = 0,
) -> Dict[str, Any]:
    """Rank producible items by urgency and propose a batch count for each.

    ``include_capacity=0`` skips the BOM explosion entirely and returns
    ``can_make_now_batches: None`` — the escape hatch if the board ever gets
    slow on a large catalogue.
    """
    _ensure_production_view_access()

    company = (company or "").strip() or planning._resolve_default_company()
    if not company:
        frappe.throw(_("Company is not configured and no Default Company is set"))

    search = (search or "").strip()
    want_capacity = bool(_coerce_int(include_capacity, 1))
    cache_key = f"jarz_prod_suggestions:{company}:{search}:{int(want_capacity)}"

    if not _coerce_int(force_refresh, 0):
        try:
            cached = planning._resolve_cache().get_value(cache_key)
        except Exception:
            cached = None
        if cached:
            return _apply_status_filter(cached, status)

    context = planning.get_planning_context(company)
    rows = planning._resolve_producible_rows(company, search)
    on_hand_map = planning._resolve_on_hand_map([r["item_code"] for r in rows])
    capacity_map = planning.build_capacity_map(rows, company) if want_capacity else {}

    items = [
        _build_suggestion(
            row,
            context=context,
            on_hand=on_hand_map.get(row["item_code"], 0.0),
            capacity=capacity_map.get(row["item_code"]),
        )
        for row in rows
    ]

    # Most urgent first.  Items that never sell sort last regardless of stock —
    # they are the one bucket nobody should be acting on.
    def _sort_key(item: Dict[str, Any]):
        cover = item["days_of_cover"]
        return (1 if cover is None else 0, cover if cover is not None else 0.0, item["item_name"])

    items.sort(key=_sort_key)

    payload = {
        "generated_on": str(frappe.utils.now_datetime()),
        "company": company,
        "season": context["season"],
        "default_target_days": context["default_target_days"],
        "thresholds": context["thresholds"],
        "velocity_updated_on": _latest_velocity_timestamp(rows),
        "capacity_included": want_capacity,
        "items": items,
        "summary": _summarize(items),
    }

    try:
        planning._resolve_cache().set_value(cache_key, payload, expires_in_sec=SUGGESTIONS_CACHE_TTL_SEC)
    except Exception:
        # A dead cache must degrade to "slower", never to "broken".
        frappe.log_error(title="JARZ Production – suggestion cache write failed", message=cache_key)

    return _apply_status_filter(payload, status)


def _apply_status_filter(payload: Dict[str, Any], status: Optional[str]) -> Dict[str, Any]:
    """Filter by status on the way out so the cache stays status-agnostic."""
    status = (status or "").strip()
    if not status or status == "all":
        return payload

    wanted = {s.strip() for s in status.split(",") if s.strip()}
    filtered = dict(payload)
    filtered["items"] = [i for i in payload["items"] if i["status"] in wanted]
    return filtered


def _latest_velocity_timestamp(rows: List[Dict[str, Any]]) -> Optional[str]:
    """When the velocity job last touched anything on this board.

    Surfaced in the header because a board full of zeroes almost always means
    the weekly job has never run, not that nothing sells.
    """
    stamps = [r.get("velocity_updated_on") for r in rows if r.get("velocity_updated_on")]
    if not stamps:
        return None
    return str(max(stamps))


@frappe.whitelist()
def get_basket_material_rollup(lines: Any, company: Optional[str] = None) -> Dict[str, Any]:
    """Consolidated raw-material demand for a whole basket of production lines.

    Takes the same line shape as ``submit_work_orders``.  This is what the per-
    line check could never answer: two lines drawing on one pile of flour.
    """
    _ensure_production_view_access()

    parsed = _coerce_lines(lines)
    company = (company or "").strip() or planning._resolve_default_company()
    if not company:
        frappe.throw(_("Company is not configured and no Default Company is set"))

    rollup = planning.build_basket_rollup(parsed, company)
    rollup["company"] = company
    rollup["line_count"] = len(parsed)
    return rollup


@frappe.whitelist()
def set_item_target_days(item_code: str, target_days: Any = None) -> Dict[str, Any]:
    """Override the cover target for one item.  Blank or 0 restores the default."""
    _ensure_manager_access()

    item_code = (item_code or "").strip()
    if not item_code:
        frappe.throw(_("item_code is required"))
    if not frappe.db.exists("Item", item_code):
        frappe.throw(_("Item {0} not found").format(item_code))

    value = _coerce_int(target_days, 0)
    if value < 0:
        frappe.throw(_("Target days of cover cannot be negative"))

    frappe.db.set_value("Item", item_code, "jarz_target_days_of_cover", value or None)

    return {
        "ok": True,
        "item_code": item_code,
        "target_days": value or None,
        "uses_default": not value,
    }

"""Production Board API — what to make, how much, and what's actually possible.

Read-only planning endpoints.  Nothing here writes stock or GL; the only write
is ``set_item_target_days``, which sets a planning preference on an Item and is
manager-gated.

Kept separate from ``api/manufacturing.py`` (already ~800 lines) and layered
above ``services/production_planning.py``, which holds the maths.
"""

from __future__ import annotations

import json
import math
from typing import Any, Optional

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


def _coerce_lines(lines: Any) -> list[dict[str, Any]]:
	if isinstance(lines, str):
		try:
			lines = json.loads(lines)
		except Exception:
			frappe.throw(_("Invalid JSON payload for lines"))
	if not isinstance(lines, list):
		frappe.throw(_("lines must be a list"))

	out: list[dict[str, Any]] = []
	for i, item in enumerate(lines):
		if not isinstance(item, dict):
			frappe.throw(_("lines[{0}] must be an object").format(i))
		for required in ("item_code", "bom_name", "item_qty"):
			if not item.get(required):
				frappe.throw(_("Missing required field {0} in lines[{1}]").format(required, i))
		out.append(item)
	return out


def _build_suggestion(
	row: dict[str, Any],
	*,
	context: dict[str, Any],
	on_hand: float,
	capacity: dict[str, Any] | None,
) -> dict[str, Any]:
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
		# Surfaced so the row can tell somebody to count the item. The
		# suggestion deliberately ignores the negative rather than producing to
		# fill what is nearly always a counting error.
		"stock_is_negative": on_hand < 0,
		"suggested_batches": batches,
		"suggested_units": batches * bom_qty,
		"can_make_now_batches": (capacity or {}).get("can_make_now_batches"),
		"limiting_component": (capacity or {}).get("limiting_component"),
	}


def _summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
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
	company: str | None = None,
	search: str | None = None,
	status: str | None = None,
	include_capacity: Any = 1,
	force_refresh: Any = 0,
) -> dict[str, Any]:
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
	# Prefix owned by ``production_planning`` so a write that invalidates the
	# board cannot drift away from the key this reader writes.
	cache_key = f"{planning.SUGGESTIONS_CACHE_PREFIX}{company}:{search}:{int(want_capacity)}"

	if not _coerce_int(force_refresh, 0):
		try:
			cached = planning._resolve_cache().get_value(cache_key)
		except Exception:
			cached = None
		if cached:
			return _apply_status_filter(cached, status)

	context = planning.get_planning_context(company)
	# Phantom BOMs are excluded for the same reason the Bases tab excludes
	# them: ERPNext expands a phantom sub-assembly into its own components at
	# Work Order time, so a jar batch already relieves the raw materials.
	# Ranking one here put ``Cheesecake Mix`` on the board with an Add button,
	# and producing it would have consumed the cheese and cream twice over.
	rows = planning.exclude_phantom_rows(planning._resolve_producible_rows(company, search))
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
	def _sort_key(item: dict[str, Any]):
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


def _apply_status_filter(payload: dict[str, Any], status: str | None) -> dict[str, Any]:
	"""Filter by status on the way out so the cache stays status-agnostic."""
	status = (status or "").strip()
	if not status or status == "all":
		return payload

	wanted = {s.strip() for s in status.split(",") if s.strip()}
	filtered = dict(payload)
	filtered["items"] = [i for i in payload["items"] if i["status"] in wanted]
	return filtered


def _latest_velocity_timestamp(rows: list[dict[str, Any]]) -> str | None:
	"""When the velocity job last touched anything on this board.

	Surfaced in the header because a board full of zeroes almost always means
	the weekly job has never run, not that nothing sells.
	"""
	stamps = [r.get("velocity_updated_on") for r in rows if r.get("velocity_updated_on")]
	if not stamps:
		return None
	return str(max(stamps))


@frappe.whitelist()
def get_basket_material_rollup(lines: Any, company: str | None = None) -> dict[str, Any]:
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
def set_item_target_days(item_code: str, target_days: Any = None) -> dict[str, Any]:
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


# ── Shortage → miscount correction ─────────────────────────────────────────
# The Plan tab's "Insufficient inventory" card, answered in one tap.  Production
# is recorded AFTER the jars are already made, so a shortage on this board is a
# count that was wrong, never stock that is genuinely absent: the jars exist.
# The fix the owner asked for is a Stock Reconciliation that adds exactly the
# missing quantity a few minutes BEFORE the production moment, so the batch then
# posts against it.  It is NOT the fix for a base that was never produced or a
# purchase invoice that was never entered — the confirmation says so, and the
# remarks on the document record that the user confirmed it.

SHORTAGE_COUNT_LEAD_MINUTES = 5


def _ensure_shortage_count_access() -> None:
	"""Same tier that may backdate production: a count moves stock value and
	can land in the past, so floor operators are excluded."""
	roles = set(frappe.get_roles())
	if not roles.intersection(ROLES.PRODUCTION_BACKDATE):
		frappe.throw(_("Not permitted: production managers only"), frappe.PermissionError)


def _shortage_count_plan(lines: Any, scheduled_at: Any = None, company: str | None = None) -> dict[str, Any]:
	"""What the correction would post: one row per short (item, warehouse).

	Always recomputed from the basket on the server — the client's figures are
	never trusted, so the confirmation and the posted document cannot differ.

	``counted_qty`` = the balance at the count moment + the basket's missing
	quantity.  Adding the shortfall as a DELTA (rather than setting the balance
	to the required quantity) is what keeps every later movement intact: the
	rollup's shortage for a backdated basket is already capped by the lowest
	later balance, so this is exactly the quantity the batch is missing.
	"""
	from datetime import timedelta

	from frappe.utils import get_datetime

	from jarz_pos.api import inventory_count
	from jarz_pos.api.manufacturing import _resolve_now_datetime, _resolve_scheduled_datetime

	parsed = _coerce_lines(lines)
	if not parsed:
		frappe.throw(_("Nothing is queued for production"))
	if scheduled_at:
		get_datetime(scheduled_at)  # validate early with a clear error
		parsed = [dict(line, scheduled_at=scheduled_at) for line in parsed]

	company = (company or "").strip() or planning._resolve_default_company()
	if not company:
		frappe.throw(_("Company is not configured and no Default Company is set"))

	production_at = min(_resolve_scheduled_datetime(line.get("scheduled_at")) for line in parsed)
	count_at = production_at - timedelta(minutes=SHORTAGE_COUNT_LEAD_MINUTES)
	# A batch timed later today still gets its count now: ERPNext refuses a
	# stock posting in the future, and "now" is already before the batch.
	now = get_datetime(_resolve_now_datetime())
	if count_at > now:
		count_at = now
	count_at = count_at.replace(microsecond=0)

	rollup = planning.build_basket_rollup(parsed, company)
	rows: list[dict[str, Any]] = []
	skipped: list[dict[str, Any]] = []
	by_warehouse: dict[str, list[dict[str, Any]]] = {}
	for short in rollup.get("shortages") or []:
		warehouse = short.get("source_warehouse")
		missing = float(short.get("missing_qty") or 0)
		if not warehouse or short.get("reason") == "missing_source_warehouse":
			skipped.append({"item_code": short.get("item_code"), "reason": "missing_source_warehouse"})
			continue
		if missing <= 1e-9:
			continue
		# Round UP to the gram/unit the count sheet works in: rounding down
		# would leave the batch short by a float hair and refuse it again.
		missing = math.ceil(missing * 1000 - 1e-6) / 1000
		row = {
			"item_code": short.get("item_code"),
			"item_name": short.get("item_name") or short.get("item_code"),
			"uom": short.get("uom") or "",
			"warehouse": warehouse,
			"required_qty": float(short.get("required_qty") or 0),
			"available_qty": float(short.get("available_qty") or 0),
			"missing_qty": missing,
		}
		rows.append(row)
		by_warehouse.setdefault(warehouse, []).append(row)

	date_text = count_at.strftime("%Y-%m-%d")
	time_text = count_at.strftime("%H:%M:%S")
	for warehouse, wh_rows in by_warehouse.items():
		balances = inventory_count._get_qty_as_of_map(
			warehouse, [r["item_code"] for r in wh_rows], date_text, time_text
		)
		for r in wh_rows:
			current = float(balances.get(r["item_code"], 0.0))
			r["current_qty"] = current
			r["counted_qty"] = max(0.0, round(current + r["missing_qty"], 6))

	return {
		"company": company,
		"production_at": production_at.strftime("%Y-%m-%d %H:%M:%S"),
		"count_at": count_at.strftime("%Y-%m-%d %H:%M:%S"),
		"lead_minutes": SHORTAGE_COUNT_LEAD_MINUTES,
		"items": [r for r in rows if r["counted_qty"] > r["current_qty"] + 1e-9],
		"skipped": skipped,
	}


@frappe.whitelist()
def preview_shortage_count_fix(
	lines: Any, scheduled_at: str | None = None, company: str | None = None
) -> dict[str, Any]:
	"""The confirmation's content: items, counts and the count moment.  Posts nothing."""
	_ensure_shortage_count_access()
	return _shortage_count_plan(lines, scheduled_at, company)


@frappe.whitelist(methods=["POST"])
def fix_shortage_by_count(
	lines: Any,
	scheduled_at: str | None = None,
	company: str | None = None,
	confirm_miscount: Any = 0,
) -> dict[str, Any]:
	"""Post the correction: one Stock Reconciliation per warehouse, all or none.

	``confirm_miscount`` must be set — the user's statement that the stock was
	counted wrong, not a base left unproduced or a purchase left unentered.
	"""
	from jarz_pos.api import inventory_count

	_ensure_shortage_count_access()
	if not _coerce_int(confirm_miscount, 0):
		frappe.throw(_("Confirm that the stock was counted wrong before adding it"))

	plan = _shortage_count_plan(lines, scheduled_at, company)
	if not plan["items"]:
		return {**plan, "ok": True, "stock_reconciliations": []}

	user = frappe.session.user
	full_name = frappe.db.get_value("User", user, "full_name") or user
	remarks = (
		"Production shortage correction from the Production Board: counted wrong, "
		f"not a missing base or purchase invoice (confirmed by {full_name}). "
		f"Production at {plan['production_at']}."
	)

	by_warehouse: dict[str, list[dict[str, Any]]] = {}
	for row in plan["items"]:
		by_warehouse.setdefault(row["warehouse"], []).append(row)

	names: list[str] = []
	for warehouse, rows in by_warehouse.items():
		result = inventory_count._reconcile_counts(
			warehouse,
			plan["count_at"],
			[{"item_code": r["item_code"], "counted_qty": r["counted_qty"]} for r in rows],
			qty_as_of_posting=True,
			remarks=remarks,
		)
		if result.get("stock_reconciliation"):
			names.append(result["stock_reconciliation"])

	# One commit for every warehouse: a failure in the second raises and the
	# request rolls back the first, so the board never gets half a correction.
	frappe.db.commit()
	return {**plan, "ok": True, "stock_reconciliations": names}

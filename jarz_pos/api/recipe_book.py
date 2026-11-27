"""Recipe Book API: every recipe, with all its versions, for the app's book page.

Two read-only endpoints (contract: ``.scratch/recipe-book/CONTRACT.md``):

* ``list_recipes()``  - one row per recipe (an Item, or a titled trial that is
  not an Item yet) with its versions, newest first.
* ``get_recipe(sop)`` - one version at ONE batch (factor 1).  The app scales
  client-side, so quantities inside the steps come back as segments carrying
  the figure and how it scales, not as finished text.

Same gate as the Production Board (``_ensure_production_view_access``).  The
grouping, status and ingredient logic is pure (no frappe) so it is unit-tested
directly; the only database reads are ``_fetch_rows`` (one ``get_all``) and the
reads ``get_recipe`` needs for the one version it opens.

Version ``status``: ``trial``/``retired`` follow the stage; an Approved version
is ``current`` when ``is_active`` is set (for a recipe with no Item, the highest
Approved version), else ``previous``.  Recipe ``status`` is ``approved`` when it
has a current version, else ``trial`` when it has a trial, else ``retired``.
"""

from __future__ import annotations

import html
import re
from typing import Any

import frappe

from jarz_pos.api.sop import (
	SOP_DOCTYPE,
	_build_component_maps_with_direct,
	_coerce_int,
	_ensure_production_view_access,
	_field,
	_resolve_default_bom,
	_resolve_sop_doc,
	_step_rows,
)
from jarz_pos.services import sop_rendering as rendering

STAGE_APPROVED = "Approved"
STAGE_TRIAL = "Trial"
STAGE_RETIRED = "Retired"

TRACK_ORDER = ("Standard line", "Cookie jar line", "Winter warmer", "Light line")

_LIST_FIELDS = [
	"name",
	"item_code",
	"item_name",
	"recipe_title",
	"track",
	"stage",
	"version",
	"is_active",
	"approved_on",
	"change_summary",
	"modified",
]

_BLOCK_END_RE = re.compile(r"</\s*(?:p|div|li|h[1-6]|tr|ul|ol)\s*>", re.IGNORECASE)
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


# ── Pure helpers ────────────────────────────────────────────────────────


def html_to_text(value: Any) -> str:
	"""Editor HTML to plain text, keeping line breaks.  Runs BEFORE tokenising."""
	text = "" if value is None else str(value)
	text = _BREAK_RE.sub("\n", text)
	text = _BLOCK_END_RE.sub("\n", text)
	text = _TAG_RE.sub("", text)
	text = html.unescape(text).replace("\xa0", " ")
	lines = [line.rstrip() for line in text.split("\n")]
	return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def _clean(value: Any) -> str:
	return str(value or "").strip()


def _stamp(value: Any) -> str | None:
	"""Timestamp / date as text, without microseconds."""
	if value in (None, ""):
		return None
	return str(value)[:19]


def _date_text(value: Any) -> str | None:
	return str(value)[:10] if value not in (None, "") else None


def recipe_key(row: Any) -> str:
	return _clean(_field(row, "item_code")) or _clean(_field(row, "recipe_title"))


def _stage(row: Any) -> str:
	return _clean(_field(row, "stage")) or STAGE_APPROVED


def _sort_key(row: Any) -> tuple[int, str]:
	return _coerce_int(_field(row, "version"), 0), str(_field(row, "modified") or "")


def version_statuses(rows: list[Any]) -> dict[str, str]:
	"""``{sop name: current|previous|trial|retired}`` for the versions of ONE recipe."""
	has_item = any(_clean(_field(r, "item_code")) for r in rows)
	approved = [r for r in rows if _stage(r) == STAGE_APPROVED]
	top_approved = max(approved, key=_sort_key) if approved else None

	statuses: dict[str, str] = {}
	for row in rows:
		stage = _stage(row)
		name = _field(row, "name")
		if stage == STAGE_TRIAL:
			statuses[name] = "trial"
		elif stage == STAGE_RETIRED:
			statuses[name] = "retired"
		elif has_item:
			statuses[name] = "current" if _coerce_int(_field(row, "is_active"), 0) else "previous"
		else:
			statuses[name] = "current" if row is top_approved else "previous"
	return statuses


def _version_dict(row: Any, status: str) -> dict[str, Any]:
	in_production = (
		_stage(row) == STAGE_APPROVED
		and bool(_coerce_int(_field(row, "is_active"), 0))
		and bool(_clean(_field(row, "item_code")))
	)
	return {
		"name": _field(row, "name"),
		"version": _coerce_int(_field(row, "version"), 1),
		"stage": _stage(row),
		"status": status,
		"approved_on": _date_text(_field(row, "approved_on")),
		"modified": _stamp(_field(row, "modified")),
		"change_summary": _clean(_field(row, "change_summary")) or None,
		"in_production": in_production,
	}


def build_recipe(rows: list[Any]) -> dict[str, Any]:
	"""One recipe card from every version row of one key (newest first inside)."""
	ordered = sorted(rows, key=_sort_key, reverse=True)
	statuses = version_statuses(ordered)
	versions = [_version_dict(r, statuses[_field(r, "name")]) for r in ordered]
	by_name = {_field(r, "name"): r for r in ordered}

	current = [v for v in versions if v["status"] == "current"]
	trials = [v for v in versions if v["status"] == "trial"]
	if current:
		headline, status = current[0], "approved"
	elif trials:
		headline, status = trials[0], "trial"
	else:
		headline, status = versions[0], "retired"

	head_row = by_name[headline["name"]]
	item_code = next((_clean(_field(r, "item_code")) for r in ordered if _clean(_field(r, "item_code"))), "")
	key = item_code or recipe_key(head_row)
	title = (
		_clean(_field(head_row, "recipe_title"))
		or _clean(_field(head_row, "item_name"))
		or next((_clean(_field(r, "recipe_title")) for r in ordered if _clean(_field(r, "recipe_title"))), "")
		or key
	)
	track = _clean(_field(head_row, "track")) or next(
		(_clean(_field(r, "track")) for r in ordered if _clean(_field(r, "track"))), ""
	)
	return {
		"key": key,
		"title": title,
		"item_code": item_code or None,
		"track": track or None,
		"status": status,
		"headline": headline["name"],
		"has_trial": bool(current and trials),
		"updated": max((v["modified"] or "" for v in versions), default="") or None,
		"versions": versions,
	}


def _track_rank(track: str | None) -> int:
	return TRACK_ORDER.index(track) if track in TRACK_ORDER else len(TRACK_ORDER)


def build_recipe_list(rows: list[Any]) -> dict[str, Any]:
	groups: dict[str, list[Any]] = {}
	for row in rows:
		key = recipe_key(row)
		if key:
			groups.setdefault(key, []).append(row)

	recipes = [build_recipe(group) for group in groups.values()]
	recipes.sort(key=lambda r: (_track_rank(r["track"]), r["title"].lower()))
	tracks = [t for t in TRACK_ORDER if any(r["track"] == t for r in recipes)]
	tracks += sorted({r["track"] for r in recipes if r["track"] and r["track"] not in TRACK_ORDER})
	return {"tracks": tracks, "recipes": recipes}


def _ingredient_entry(key: str, name: str, qty: float, uom: str, section: Any, note: Any) -> dict[str, Any]:
	"""Grams (``unit: "g"``) whenever the UOM is a mass, else the raw qty and UOM."""
	grams = rendering.to_grams(qty, uom)
	if grams is not None:
		amount, unit = grams, "g"
	else:
		amount, unit = qty, _clean(uom)
	return {
		"key": key,
		"name": name,
		"amount": round(amount, 6),
		"unit": unit,
		"section": _clean(section) or None,
		"note": _clean(note) or None,
	}


def ingredients_from_bom(
	qty_map: dict[str, float],
	uom_map: dict[str, str],
	name_map: dict[str, str],
	order: list[str],
) -> list[dict[str, Any]]:
	return [
		_ingredient_entry(
			code.strip().lower(),
			_clean(name_map.get(code)) or code,
			rendering.to_float(qty_map.get(code), 0.0),
			uom_map.get(code) or "",
			None,
			None,
		)
		for code in order
		if code in qty_map
	]


def ingredients_from_table(rows: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
	"""``(ingredients, qty_map, uom_map, name_map, alias_to_key)`` for a no-BOM recipe.

	A token resolves against an ingredient's Item Code first, then its name
	(case-insensitive).  The maps carry both spellings so the shared token
	resolver finds either; ``alias_to_key`` folds a name hit back onto the
	ingredient's own key.  Rows sharing a key keep their own list entry (the key
	gets ``#2``...); in the maps they add up when they share a unit, or when
	both are masses (then in grams), and are otherwise left out of the maps.
	"""
	ingredients: list[dict[str, Any]] = []
	qty_map: dict[str, float] = {}
	uom_map: dict[str, str] = {}
	name_map: dict[str, str] = {}
	seen: dict[str, int] = {}
	primaries: dict[str, str] = {}  # lowered primary -> map code

	for row in rows or []:
		name = _clean(_field(row, "ingredient"))
		code = _clean(_field(row, "item_code")) or name
		if not code:
			continue
		qty = rendering.to_float(_field(row, "qty"), 0.0)
		uom = _clean(_field(row, "uom"))
		base = code.lower()
		seen[base] = seen.get(base, 0) + 1
		key = base if seen[base] == 1 else f"{base}#{seen[base]}"
		ingredients.append(
			_ingredient_entry(key, name or code, qty, uom, _field(row, "section"), _field(row, "note"))
		)

		map_code = primaries.setdefault(base, code)
		if map_code not in qty_map:
			qty_map[map_code], uom_map[map_code], name_map[map_code] = qty, uom, name or code
			continue
		if uom.lower() == uom_map[map_code].lower():
			qty_map[map_code] += qty
		else:
			have = rendering.to_grams(qty_map[map_code], uom_map[map_code])
			add = rendering.to_grams(qty, uom)
			if have is not None and add is not None:
				qty_map[map_code], uom_map[map_code] = have + add, "g"

	# Name aliases, added once every primary key is known so a name never
	# shadows another ingredient's code.
	alias_to_key: dict[str, str] = {}
	lowered_codes = {c.lower() for c in qty_map}
	for row in rows or []:
		name = _clean(_field(row, "ingredient"))
		code = _clean(_field(row, "item_code")) or name
		if not name or not code or name.lower() == code.lower() or name.lower() in lowered_codes:
			continue
		primary = primaries.get(code.lower())
		if primary is None or primary not in qty_map:
			continue
		qty_map[name], uom_map[name], name_map[name] = qty_map[primary], uom_map[primary], name_map[primary]
		lowered_codes.add(name.lower())
		alias_to_key[name.lower()] = primary.lower()
	return ingredients, qty_map, uom_map, name_map, alias_to_key


def build_steps(
	step_rows: list[dict[str, Any]],
	*,
	qty_map: dict[str, Any],
	uom_map: dict[str, Any],
	name_map: dict[str, Any],
	units_per_batch: Any,
	alias_to_key: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
	"""Steps for the book: plain-text instruction tokenised into segments."""
	alias_to_key = alias_to_key or {}
	steps: list[dict[str, Any]] = []
	unresolved: list[str] = []

	for position, raw in enumerate(step_rows or [], start=1):
		segments, missing = rendering.tokenize_instruction(
			html_to_text(raw.get("instruction")),
			component_qty_map=qty_map,
			uom_map=uom_map,
			name_map=name_map,
			units_per_batch=units_per_batch,
		)
		for seg in segments:
			if seg.get("key") in alias_to_key:
				seg["key"] = alias_to_key[seg["key"]]
		title, title_missing = rendering.render_instruction(
			html_to_text(raw.get("title")),
			component_qty_map=qty_map,
			uom_map=uom_map,
			name_map=name_map,
			batches=1,
			units_per_batch=units_per_batch,
		)
		for token in missing + title_missing:
			if token not in unresolved:
				unresolved.append(token)

		numeric = _clean(raw.get("capture_type")) in rendering.NUMERIC_CAPTURE_TYPES
		step_no = _coerce_int(raw.get("step_no"), position) or position
		steps.append(
			{
				"step_no": step_no,
				"title": title,
				"duration_mins": rendering.to_float(raw.get("duration_mins"), 0.0),
				"scaling_mode": _clean(raw.get("scaling_mode")) or rendering.SCALING_FIXED,
				"capture_label": _clean(raw.get("capture_label")) or None,
				"capture_min": rendering.to_float(raw.get("capture_min"), 0.0) if numeric else None,
				"capture_max": rendering.to_float(raw.get("capture_max"), 0.0) if numeric else None,
				"segments": segments,
			}
		)
	return steps, unresolved


# ── frappe reads ────────────────────────────────────────────────────────


def _fetch_rows(filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
	return [
		dict(r)
		for r in frappe.get_all(
			SOP_DOCTYPE,
			filters=filters or {},
			fields=_LIST_FIELDS,
			order_by="modified desc",
			limit_page_length=0,
		)
	]


def _resolve_bom(item_code: str, fallback: str | None) -> dict[str, Any] | None:
	bom = _resolve_default_bom(item_code) if item_code else None
	bom = bom or (fallback or None)
	if not bom:
		return None
	try:
		row = frappe.db.get_value("BOM", bom, ["name", "quantity", "company", "uom"], as_dict=True)
	except Exception:
		frappe.log_error(title="JARZ Recipe Book - BOM read failed", message=f"bom={bom}")
		return None
	return dict(row) if row else None


@frappe.whitelist()
def list_recipes() -> dict[str, Any]:
	"""Every recipe with its versions.  One query; no per-recipe reads."""
	_ensure_production_view_access()
	return build_recipe_list(_fetch_rows())


@frappe.whitelist()
def get_recipe(sop: str) -> dict[str, Any]:
	"""One version of a recipe, at one batch."""
	_ensure_production_view_access()
	doc = _resolve_sop_doc(_clean(sop))

	item_code = _clean(_field(doc, "item_code"))
	title = _clean(_field(doc, "recipe_title")) or _clean(_field(doc, "item_name")) or item_code
	if item_code:
		sibling_filters: dict[str, Any] = {"item_code": item_code}
	else:
		sibling_filters = {"recipe_title": title, "item_code": ["is", "not set"]}
	siblings = _fetch_rows(sibling_filters)
	card = build_recipe(siblings) if siblings else None
	versions = card["versions"] if card else []
	this = next((v for v in versions if v["name"] == doc.name), None) or _version_dict(
		doc, "current" if _coerce_int(_field(doc, "is_active"), 0) else "previous"
	)

	table_rows = list(_field(doc, "ingredients") or [])
	stage = _stage(doc)
	bom = _resolve_bom(item_code, _field(doc, "bom"))
	# An approved recipe with a BOM is that BOM (the kitchen's truth); a trial
	# or a recipe with no BOM reads its own ingredient table.
	use_bom = bom is not None and (stage == STAGE_APPROVED or not table_rows)

	alias_to_key: dict[str, str] = {}
	if use_bom:
		bom_qty = rendering.to_float(bom.get("quantity"), 1.0) or 1.0
		qty_map, uom_map, name_map, order = _build_component_maps_with_direct(
			bom["name"], bom.get("company"), bom_qty
		)
		ingredients = ingredients_from_bom(qty_map, uom_map, name_map, order)
		units_per_batch: Any = bom_qty
		yield_info = {
			"label": "One batch",
			"amount": round(bom_qty, 6),
			"unit": _clean(bom.get("uom")) or None,
		}
	else:
		ingredients, qty_map, uom_map, name_map, alias_to_key = ingredients_from_table(table_rows)
		yield_qty = rendering.to_float(_field(doc, "yield_qty"), 0.0)
		units_per_batch = yield_qty if yield_qty > 0 else None
		yield_info = {
			"label": "One batch",
			"amount": round(yield_qty, 6) if yield_qty > 0 else None,
			"unit": _clean(_field(doc, "yield_uom")) or None,
		}

	steps, unresolved = build_steps(
		_step_rows(doc),
		qty_map=qty_map,
		uom_map=uom_map,
		name_map=name_map,
		units_per_batch=units_per_batch,
		alias_to_key=alias_to_key,
	)

	return {
		"name": doc.name,
		"key": item_code or title,
		"title": title,
		"item_code": item_code or None,
		"track": _clean(_field(doc, "track")) or None,
		"version": this["version"],
		"stage": this["stage"],
		"status": this["status"],
		"approved_on": this["approved_on"],
		"change_summary": this["change_summary"],
		"in_production": this["in_production"],
		"equipment": _clean(_field(doc, "equipment")) or None,
		"prep_time_mins": _coerce_int(_field(doc, "prep_time_mins"), 0),
		"notes": html_to_text(_field(doc, "notes")),
		"yield": yield_info,
		"ingredients": ingredients,
		"steps": steps,
		"versions": versions,
		"unresolved_tokens": unresolved,
	}

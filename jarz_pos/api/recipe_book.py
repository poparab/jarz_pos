"""Recipe Book API: every recipe, with all its versions, for the app's book page.

Read-only endpoints (contract: ``.scratch/recipe-book/CONTRACT.md``):

* ``list_recipes(catalog=0)`` - one row per recipe (an Item, or a titled trial
  that is not an Item yet) with its versions, newest first.  ``catalog=1`` also
  lists BOM-only recipes and adds category / family / size per recipe.
* ``get_recipe(sop=None, item_code=None)`` - one version at ONE batch (factor 1);
  by item code, its current SOP, else its default BOM.  The app scales
  client-side, so quantities inside the steps come back as segments carrying
  the figure and how it scales, not as finished text.
* ``get_jar_family(family)`` - one jar flavour, every size's layers side by side.

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


# ── Catalog (v2): categories, jar families ─────────────────────────────

SIZES = ("Small", "Medium", "Large")
CATEGORIES = [
	{"key": "bases", "label": "Cakes & biscuits"},
	{"key": "fillings", "label": "Mixes & fillings"},
	{"key": "jars", "label": "Jars"},
]
PACKAGING_GROUPS = ("packaging", "labels", "consumable")

# Recipe title / item code (case-insensitive) -> category.  Wins over the
# heuristic below; empty until the heuristic gets one wrong.
CATEGORY_OVERRIDES: dict[str, str] = {}

_SIZE_WORDS = {s.lower(): s for s in SIZES}
_TRAILING_SIZE_RE = re.compile(r"[\s(]*\b(small|medium|large)\b[\s)]*$", re.IGNORECASE)
_TRAILING_JAR_RE = re.compile(r"[\s(]*\bjar\b[\s)]*$", re.IGNORECASE)
_JAR_WORD_RE = re.compile(r"\bjar\b", re.IGNORECASE)
_FILLING_RE = re.compile(
	r"\b(?:mix|ganache|syrup|soak|caramel|filling|cream|sauce|glaze|frosting|curd|compote|jam)(?:s|es)?\b",
	re.IGNORECASE,
)
_BASE_RE = re.compile(
	r"\b(?:cake|biscuit|savoiardi|sponge|crumb|dough|bake|base|cookie)(?:s|es|d)?\b", re.IGNORECASE
)


def _jar_family(title: str) -> str | None:
	text = _TRAILING_SIZE_RE.sub("", title.strip())
	text = _TRAILING_JAR_RE.sub("", text).strip(" ()-")
	if not text:
		return None
	return text[0].upper() + text[1:] if text[0].islower() else text


def classify_recipe(title: Any, item_group: Any = None) -> tuple[str, str | None, str | None]:
	"""``(category, family, size)`` for one recipe; pure.

	Jars are told by size (item group Small/Medium/Large, else a trailing size
	in the title) or the word "jar".  Everything else is a filling when the
	title says mix/syrup/ganache..., a base when it says cake/biscuit/sponge...,
	and a filling when it says neither.  A jar with no size has no family.
	"""
	text = _clean(title)
	group = _clean(item_group)
	size = _SIZE_WORDS.get(group.lower())
	if size is None:
		match = _TRAILING_SIZE_RE.search(text)
		size = _SIZE_WORDS[match.group(1).lower()] if match else None

	is_jar = size is not None or bool(_JAR_WORD_RE.search(text))
	family = _jar_family(text) if size is not None else None

	override = next((v for k, v in CATEGORY_OVERRIDES.items() if k.strip().lower() == text.lower()), None)
	if override:
		return override, (family if override == "jars" else None), (size if override == "jars" else None)
	if is_jar:
		return "jars", family, size
	if _FILLING_RE.search(text):
		return "fillings", None, None
	if _BASE_RE.search(text):
		return "bases", None, None
	return "fillings", None, None


def _bom_only_recipe(item_code: str, item: Any, modified: Any) -> dict[str, Any]:
	return {
		"key": item_code,
		"title": _clean(_field(item, "item_name")) or item_code,
		"item_code": item_code,
		"track": None,
		"status": "approved",
		"headline": None,
		"has_trial": False,
		"updated": _stamp(modified),
		"versions": [],
	}


def _family_entries(recipes: list[dict[str, Any]]) -> list[dict[str, Any]]:
	groups: dict[str, list[dict[str, Any]]] = {}
	names: dict[str, str] = {}
	for rec in recipes:
		family = rec.get("family")
		if not family or rec.get("category") != "jars" or not rec.get("size"):
			continue
		groups.setdefault(family.lower(), []).append(rec)
		names.setdefault(family.lower(), family)

	out = []
	for low, members in groups.items():
		statuses = {m["status"] for m in members}
		out.append(
			{
				"family": names[low],
				"track": next((m["track"] for m in members if m["track"]), None),
				"sizes": [s for s in SIZES if any(m["size"] == s for m in members)],
				"has_trial": any(m["has_trial"] or m["status"] == "trial" for m in members),
				"status": "approved" if "approved" in statuses else ("trial" if "trial" in statuses else "retired"),
				"updated": max((m["updated"] or "" for m in members), default="") or None,
			}
		)
	out.sort(key=lambda f: f["family"].lower())
	return out


def build_catalog(
	rows: list[Any],
	bom_rows: list[Any],
	item_info: dict[str, Any],
) -> dict[str, Any]:
	"""The v1 list plus BOM-only recipes, categories and families.

	``bom_rows``: ``{"item", "modified"}`` of every active default BOM.
	``item_info``: ``{item_code: {"item_name", "item_group", "disabled"}}``.
	"""
	payload = build_recipe_list(rows)
	recipes: list[dict[str, Any]] = payload["recipes"]
	sop_items = {r["item_code"] for r in recipes if r["item_code"]}

	seen: set[str] = set()
	for bom in bom_rows:
		code = _clean(_field(bom, "item"))
		info = item_info.get(code)
		if not code or code in sop_items or code in seen or info is None or _coerce_int(_field(info, "disabled"), 0):
			continue
		seen.add(code)
		recipes.append(_bom_only_recipe(code, info, _field(bom, "modified")))

	for rec in recipes:
		group = _field(item_info.get(rec["item_code"]) or {}, "item_group") if rec["item_code"] else None
		category, family, size = classify_recipe(rec["title"], group)
		rec.update(
			category=category,
			family=family,
			size=size,
			has_method=bool(rec["versions"]),
			source="sop" if rec["versions"] else "bom",
		)

	recipes.sort(key=lambda r: (_track_rank(r["track"]), r["title"].lower()))
	payload["categories"] = [dict(c) for c in CATEGORIES]
	payload["families"] = _family_entries(recipes)
	return payload


def is_packaging_group(item_group: Any, parents: dict[str, str]) -> bool:
	"""Packaging / Labels / Consumable, or anywhere below one of them."""
	seen: set[str] = set()
	group = _clean(item_group)
	while group and group not in seen:
		if group.lower() in PACKAGING_GROUPS:
			return True
		seen.add(group)
		group = _clean(parents.get(group))
	return False


def _round_amount(entry: dict[str, Any]) -> float:
	return round(entry["amount"], 1) if entry["unit"] == "g" else round(entry["amount"], 3)


def per_jar_entries(
	member: dict[str, Any],
	boms: dict[str, dict[str, Any]],
	sops: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
	"""One jar's ingredient entries for one size: the default BOM, else the SOP.

	BOM: every direct line over the BOM quantity.  SOP (no BOM): only when it
	yields jars (``yield_uom`` mentions "jar", ``yield_qty`` > 0), each
	ingredient over the yield.  Otherwise nothing.
	"""
	bom = boms.get(member.get("bom") or "")
	if bom:
		bom_qty = rendering.to_float(bom.get("quantity"), 1.0) or 1.0
		qty_map: dict[str, float] = {}
		uom_map: dict[str, str] = {}
		name_map: dict[str, str] = {}
		order: list[str] = []
		for line in bom.get("lines") or []:
			code = _clean(_field(line, "item_code"))
			if not code:
				continue
			if code not in qty_map:
				order.append(code)
			qty_map[code] = qty_map.get(code, 0.0) + rendering.to_float(_field(line, "stock_qty"), 0.0) / bom_qty
			uom_map[code] = _clean(_field(line, "stock_uom"))
			name_map[code] = _clean(_field(line, "item_name")) or code
		return ingredients_from_bom(qty_map, uom_map, name_map, order)

	sop = sops.get(member.get("sop") or "")
	if sop:
		yield_qty = rendering.to_float(sop.get("yield_qty"), 0.0)
		if yield_qty > 0 and "jar" in _clean(sop.get("yield_uom")).lower():
			out = []
			for row in sop.get("ingredients") or []:
				name = _clean(_field(row, "ingredient"))
				code = _clean(_field(row, "item_code")) or name
				if not code:
					continue
				qty = rendering.to_float(_field(row, "qty"), 0.0) / yield_qty
				out.append(_ingredient_entry(code.lower(), name or code, qty, _clean(_field(row, "uom")), None, None))
			return out
	return []


_TRAILING_NUMBER_RE = re.compile(r"(?:\s+\d+(?:\.\d+)?)+\s*$")


def packaging_role(name: str) -> str:
	"""``"Tiramisu  Jar Label 147"`` -> ``"tiramisu jar label"``: the part, without its size number."""
	text = " ".join(_clean(name).lower().split())
	return _TRAILING_NUMBER_RE.sub("", text).strip() or text


def build_jar_family(
	family: str,
	members: list[dict[str, Any]],
	boms: dict[str, dict[str, Any]],
	sops: dict[str, dict[str, Any]],
	item_groups: dict[str, str],
	group_parents: dict[str, str],
) -> dict[str, Any]:
	"""Side-by-side layer weights of one flavour across its sizes; pure.

	``members``: ``{size, item_code, title, track, status, sop, sop_version, bom}``.
	``item_groups``: ``{item_code lowered: item_group}``.
	"""
	ordered = sorted((m for m in members if m.get("size") in SIZES), key=lambda m: SIZES.index(m["size"]))
	sizes: list[dict[str, Any]] = []
	layers: dict[tuple[str, str], dict[str, Any]] = {}
	packaging: dict[tuple[str, str], dict[str, Any]] = {}
	done: set[str] = set()

	for member in ordered:
		size = member["size"]
		if size in done:
			continue
		done.add(size)
		grams = 0.0
		has_grams = False
		for entry in per_jar_entries(member, boms, sops):
			is_pack = is_packaging_group(item_groups.get(entry["key"]), group_parents)
			if is_pack:
				# One row per role: "glass jar 147" / "glass jar" / "glass jar 330" are the
				# same part in three sizes, so they share a row; `items` keeps the real names.
				role = packaging_role(entry["name"])
				slot = packaging.setdefault(
					(role, entry["unit"]),
					{"key": role, "name": role[:1].upper() + role[1:], "unit": entry["unit"], "per_jar": {}, "items": {}},
				)
				slot["items"][size] = entry["name"]
			else:
				slot = layers.setdefault(
					(entry["key"], entry["unit"]),
					{"key": entry["key"], "name": entry["name"], "unit": entry["unit"], "per_jar": {}},
				)
			slot["per_jar"][size] = _round_amount(entry)
			if not is_pack and entry["unit"] == "g":
				grams += entry["amount"]
				has_grams = True
		sizes.append(
			{
				"size": size,
				"item_code": member.get("item_code"),
				"title": member["title"],
				"sop": member.get("sop"),
				"sop_version": member.get("sop_version"),
				"status": member["status"],
				"jar_grams": round(grams, 1) if has_grams else None,
			}
		)

	return {
		"family": family,
		"track": next((m["track"] for m in ordered if m.get("track")), None),
		"sizes": sizes,
		"layers": list(layers.values()),
		"packaging": list(packaging.values()),
	}


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


def _truthy(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().lower() not in ("", "0", "false", "no", "none", "null")
	return bool(value)


def _fetch_catalog_inputs(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
	"""Active default BOMs, and the Item facts for them and for every SOP item: two reads."""
	bom_rows = [
		dict(r)
		for r in frappe.get_all(
			"BOM",
			filters={"is_default": 1, "is_active": 1, "docstatus": 1},
			fields=["item", "modified"],
			limit_page_length=0,
		)
	]
	codes = {r["item"] for r in bom_rows if r.get("item")} | {_clean(r.get("item_code")) for r in rows}
	codes.discard("")
	info: dict[str, dict[str, Any]] = {}
	if codes:
		for item in frappe.get_all(
			"Item",
			filters={"name": ["in", sorted(codes)]},
			fields=["name", "item_name", "item_group", "disabled"],
			limit_page_length=0,
		):
			info[item["name"]] = dict(item)
	return bom_rows, info


def _catalog() -> dict[str, Any]:
	rows = _fetch_rows()
	bom_rows, info = _fetch_catalog_inputs(rows)
	return build_catalog(rows, bom_rows, info)


@frappe.whitelist()
def list_recipes(catalog: Any = 0) -> dict[str, Any]:
	"""Every recipe with its versions.  ``catalog=1`` adds BOM-only recipes, categories, families."""
	_ensure_production_view_access()
	if _truthy(catalog):
		return _catalog()
	return build_recipe_list(_fetch_rows())


def _item_group(item_code: str) -> str | None:
	if not item_code:
		return None
	try:
		return frappe.db.get_value("Item", item_code, "item_group")
	except Exception:
		return None


def _classification(title: str, item_code: str) -> dict[str, Any]:
	category, family, size = classify_recipe(title, _item_group(item_code))
	return {"category": category, "family": family, "size": size}


def _bom_recipe_payload(item_code: str) -> dict[str, Any]:
	bom = _resolve_bom(item_code, None)
	if bom is None:
		raise frappe.DoesNotExistError(f"No recipe for {item_code}")
	bom_qty = rendering.to_float(bom.get("quantity"), 1.0) or 1.0
	qty_map, uom_map, name_map, order = _build_component_maps_with_direct(bom["name"], bom.get("company"), bom_qty)
	title = _clean(frappe.db.get_value("Item", item_code, "item_name")) or item_code
	return {
		"name": None,
		"key": item_code,
		"title": title,
		"item_code": item_code,
		"track": None,
		"version": None,
		"stage": None,
		"status": "approved",
		"approved_on": None,
		"change_summary": None,
		"in_production": False,
		"equipment": "",
		"prep_time_mins": None,
		"notes": "",
		"yield": {"label": "One batch", "amount": round(bom_qty, 6), "unit": _clean(bom.get("uom")) or None},
		"ingredients": ingredients_from_bom(qty_map, uom_map, name_map, order),
		"steps": [],
		"versions": [],
		"unresolved_tokens": [],
		**_classification(title, item_code),
	}


@frappe.whitelist()
def get_recipe(sop: str | None = None, item_code: str | None = None) -> dict[str, Any]:
	"""One version at one batch; or, by item code, its current SOP, else its default BOM."""
	_ensure_production_view_access()
	if _clean(sop):
		return _sop_payload(sop)
	code = _clean(item_code)
	if not code:
		raise frappe.DoesNotExistError("Pass sop or item_code")
	current = frappe.get_all(
		SOP_DOCTYPE,
		filters={"item_code": code, "stage": STAGE_APPROVED, "is_active": 1},
		fields=["name"],
		order_by="version desc, modified desc",
		limit_page_length=1,
	)
	if current:
		return _sop_payload(current[0]["name"])
	return _bom_recipe_payload(code)


def _sop_payload(sop: Any) -> dict[str, Any]:
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
		**_classification(title, item_code),
	}


@frappe.whitelist()
def get_jar_family(family: str) -> dict[str, Any]:
	"""One jar flavour: every size's layer weights per jar, side by side."""
	_ensure_production_view_access()
	wanted = _clean(family).lower()
	recipes = [
		r
		for r in _catalog()["recipes"]
		if r["category"] == "jars" and r["size"] and r["family"] and r["family"].lower() == wanted
	]
	if not recipes:
		raise frappe.DoesNotExistError(f"No jar family {family}")

	members = []
	for rec in recipes:
		head = next((v for v in rec["versions"] if v["name"] == rec["headline"]), None)
		members.append(
			{
				"size": rec["size"],
				"item_code": rec["item_code"],
				"title": rec["title"],
				"track": rec["track"],
				"status": rec["status"],
				"sop": rec["headline"],
				"sop_version": head["version"] if head else None,
				"bom": None,
			}
		)

	codes = [m["item_code"] for m in members if m["item_code"]]
	boms: dict[str, dict[str, Any]] = {}
	if codes:
		by_item = {
			b["item"]: b
			for b in frappe.get_all(
				"BOM",
				filters={"item": ["in", codes], "is_default": 1, "is_active": 1, "docstatus": 1},
				fields=["name", "item", "quantity"],
				limit_page_length=0,
			)
		}
		for m in members:
			head = by_item.get(m["item_code"])
			if head:
				m["bom"] = head["name"]
				boms[head["name"]] = {"quantity": head["quantity"], "lines": []}
		if boms:
			for line in frappe.get_all(
				"BOM Item",
				filters={"parent": ["in", list(boms)], "parenttype": "BOM"},
				fields=["parent", "item_code", "item_name", "stock_qty", "stock_uom"],
				order_by="parent asc, idx asc",
				limit_page_length=0,
			):
				boms[line["parent"]]["lines"].append(dict(line))

	sops: dict[str, dict[str, Any]] = {}
	sop_names = [m["sop"] for m in members if m["sop"] and not m["bom"]]
	if sop_names:
		for doc in frappe.get_all(
			SOP_DOCTYPE,
			filters={"name": ["in", sop_names]},
			fields=["name", "yield_qty", "yield_uom"],
			limit_page_length=0,
		):
			sops[doc["name"]] = {**dict(doc), "ingredients": []}
		for row in frappe.get_all(
			"Jarz SOP Ingredient",
			filters={"parent": ["in", sop_names], "parenttype": SOP_DOCTYPE},
			fields=["parent", "ingredient", "item_code", "qty", "uom"],
			order_by="parent asc, idx asc",
			limit_page_length=0,
		):
			if row["parent"] in sops:
				sops[row["parent"]]["ingredients"].append(dict(row))

	line_codes = {ln["item_code"] for b in boms.values() for ln in b["lines"]}
	line_codes |= {_clean(r.get("item_code")) for s in sops.values() for r in s["ingredients"]}
	line_codes.discard("")
	item_groups: dict[str, str] = {}
	if line_codes:
		for item in frappe.get_all(
			"Item",
			filters={"name": ["in", sorted(line_codes)]},
			fields=["name", "item_group"],
			limit_page_length=0,
		):
			item_groups[item["name"].lower()] = item["item_group"]
	parents = {
		g["name"]: g["parent_item_group"]
		for g in frappe.get_all("Item Group", fields=["name", "parent_item_group"], limit_page_length=0)
	}
	return build_jar_family(recipes[0]["family"], members, boms, sops, item_groups, parents)

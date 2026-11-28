"""Seed the per-layer jar weights of the Tiramisu and Mango jars (Recipe Book v3).

A jar's BOM holds raw ingredient totals; the layers (savoiardi, coffee soak,
cream, cocoa ...) exist only as the text of each size's "Fill each jar" step.
This reads that text, takes every ``|each`` token in text order as a layer (or
as ``N`` layers where the text says "(two layers)" / "(three layers)" /
"(two layers of {{...}})"), evaluates it against the size's own BOM at one jar,
and writes the result as ``Jarz SOP Layer`` rows on the CURRENT version of
each size.

The write is a direct child-row insert, in place: the method did not change,
and a version used in production (which the controller freezes) must still get
its layer weights.  Run:

    bench --site <site> execute jarz_pos.scripts.seed_jar_layers.seed_jar_layers --kwargs "{'dry_run': 0}"

``dry_run`` (default 1) only prints the derived table (``MARK`` rows) and never
reads or writes the ``Jarz SOP Layer`` table.
"""

from __future__ import annotations

import re
from typing import Any

import frappe

from jarz_pos.api import recipe_book as rb
from jarz_pos.api.sop import _build_component_maps_with_direct
from jarz_pos.services import sop_rendering as rendering

FAMILIES = ("Tiramisu", "Mango")
SIZES = ("Small", "Medium", "Large")
FILL_STEP_PREFIX = "fill each jar"
LAYER_DOCTYPE = "Jarz SOP Layer"

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_LAYERS_OF_RE = re.compile(r"\(\s*(\w+)\s+layers?\s+of\s*$", re.IGNORECASE)
_LAYERS_RE = re.compile(r"\(\s*(\w+)\s+layers?\s*\)", re.IGNORECASE)
_NAME_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?)\s*(?:\(|,|\.|;|\+|\bthen\b|\bon\b|$)", re.IGNORECASE)
_LAYER_NAMES = {
	"savoiardi": "Savoiardi",
	"coffee": "Coffee soak",
	"cream": "Cream",
	"cocoa": "Cocoa",
	"mango mix": "Mango mix",
	"mango cream": "Mango cream",
}


def _count(word: str) -> int | None:
	word = word.strip().lower()
	if word.isdigit():
		return int(word)
	return _NUMBER_WORDS.get(word)


def layer_name(text_after_token: str) -> str:
	"""The layer's name from the words right after its token: ``" coffee, then "`` -> ``"Coffee soak"``."""
	found = _NAME_RE.match(text_after_token or "")
	words = " ".join((found.group(1) if found else "").lower().split())
	if not words:
		return "Layer"
	return _LAYER_NAMES.get(words) or (words[:1].upper() + words[1:])


def english_part(text: Any) -> str:
	"""The English line of a bilingual instruction (the Arabic follows on the next line)."""
	plain = rb.html_to_text(text)
	return plain.split("\n", 1)[0] if plain else ""


def derive_layers(text: Any, evaluate) -> tuple[list[dict[str, Any]], list[str]]:
	"""``(rows, notes)`` from one "Fill each jar" instruction.

	``evaluate(payload)`` returns the grams of one ``|each`` token payload at one
	jar, or ``None`` when it does not resolve.  Rows are ``{"layer", "grams"}`` in
	text order; ``notes`` lists everything a human should look at.
	"""
	english = english_part(text)
	matches = list(rendering._TOKEN_RE.finditer(english))
	notes: list[str] = []
	each: list[tuple[re.Match[str], str]] = []
	for position, match in enumerate(matches):
		_code, _variant, _mult, is_each, valid = rendering._split_payload(match.group(1))
		if valid and is_each:
			following = matches[position + 1].start() if position + 1 < len(matches) else len(english)
			each.append((match, english[match.end() : following]))

	rows: list[dict[str, Any]] = []
	grouped = False
	index = 0
	while index < len(each):
		match, after = each[index]
		name = layer_name(after)
		grams = evaluate(match.group(1))
		if grams is None:
			notes.append(f"{name}: token {{{{{match.group(1)}}}}} does not resolve against the BOM")
			index += 1
			continue

		per_layer, count = grams, 1
		of = _LAYERS_OF_RE.search(after)
		after_count = _LAYERS_RE.search(after)
		if of and index + 1 < len(each):
			count = _count(of.group(1)) or 1
			per_layer = evaluate(each[index + 1][0].group(1))
			if per_layer is None:
				notes.append(f"{name}: the per-layer token does not resolve against the BOM")
				index += 2
				continue
			index += 2  # the per-layer token is consumed with its total
		else:
			if after_count:
				count = _count(after_count.group(1)) or 1
				per_layer = grams / count
			index += 1

		grouped = grouped or count > 1
		for number in range(1, count + 1):
			rows.append({"layer": f"{name} {number}" if count > 1 else name, "grams": round(per_layer, 1)})
	if grouped:
		notes.append(
			"The text says how many layers of each, not how they alternate: rows follow the order the text lists them."
		)
	return rows, notes


def _evaluator(bom: dict[str, Any]):
	bom_qty = rendering.to_float(bom.get("quantity"), 1.0) or 1.0
	qty_map, uom_map, name_map, _order = _build_component_maps_with_direct(
		bom["name"], bom.get("company"), bom_qty
	)
	lowered = rendering._lowered_index(qty_map, name_map, uom_map)

	def evaluate(payload: str) -> float | None:
		result, _label = rendering._evaluate_token(payload, qty_map, uom_map, name_map, lowered, 1.0, bom_qty)
		if result is None or result.get("amount") is None or result.get("unit") != "g":
			return None
		return float(result["amount"])

	return evaluate


def _current_sop(item_code: str) -> dict[str, Any] | None:
	rows = frappe.get_all(
		"Jarz SOP",
		filters={"item_code": item_code, "stage": "Approved"},
		fields=["name", "version", "is_active"],
		order_by="is_active desc, version desc, modified desc",
		limit_page_length=1,
	)
	return dict(rows[0]) if rows else None


def _fill_step(sop_name: str) -> dict[str, Any] | None:
	for step in frappe.get_all(
		"Jarz SOP Step",
		filters={"parent": sop_name, "parenttype": "Jarz SOP"},
		fields=["title", "instruction"],
		order_by="idx asc",
		limit_page_length=0,
	):
		if (step.get("title") or "").strip().lower().startswith(FILL_STEP_PREFIX):
			return dict(step)
	return None


# The Mango jar's real layer weights, from the owner's figures of 2026-11-09 (see the
# Mango block in seed_recipe_sops.py): the method text says only "two layers" /
# "three layers", and the three cream layers are NOT equal (Medium 8 + 30 + 25 g).
# Small is 2/3 of Medium; the Large cream's last layer carries the 0.7 g the one-bowl
# ratio adds.  The text does not give the fill order, so the rows are grouped
# (savoiardi, mango, cream) and the owner drags them into order in the app.
MANGO_LAYERS: dict[str, list[tuple[str, float]]] = {
	"Small": [
		("Savoiardi", 7.3),
		("Savoiardi", 7.3),
		("Mango", 20.0),
		("Mango", 20.0),
		("Mango cream", 5.3),
		("Mango cream", 20.0),
		("Mango cream", 16.7),
	],
	"Medium": [
		("Savoiardi", 11.0),
		("Savoiardi", 11.0),
		("Mango", 30.0),
		("Mango", 30.0),
		("Mango cream", 8.0),
		("Mango cream", 30.0),
		("Mango cream", 25.0),
	],
	"Large": [
		("Savoiardi", 16.0),
		("Savoiardi", 16.0),
		("Mango", 44.6),
		("Mango", 44.6),
		("Mango cream", 12.0),
		("Mango cream", 35.0),
		("Mango cream", 35.7),
	],
}


def _owner_mango_rows(size: str, derived: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
	rows = [{"layer": name, "grams": grams} for name, grams in MANGO_LAYERS[size]]
	derived_total = round(sum(r["grams"] for r in derived), 1)
	owner_total = round(sum(g for _, g in MANGO_LAYERS[size]), 1)
	return rows, (
		f"owner's per-layer figures used (total {owner_total} g vs method {derived_total} g); "
		"fill order not given in the method: grouped, reorder in the app"
	)


def derive_all() -> list[dict[str, Any]]:
	"""One entry per family size: ``{item, sop, version, rows, notes, bom_grams, error}``."""
	out: list[dict[str, Any]] = []
	for family in FAMILIES:
		for size in SIZES:
			item_code = f"{family} {size}"
			entry: dict[str, Any] = {
				"item": item_code,
				"sop": None,
				"version": None,
				"rows": [],
				"notes": [],
				"error": None,
			}
			out.append(entry)
			sop = _current_sop(item_code)
			if sop is None:
				entry["error"] = "no approved SOP"
				continue
			entry["sop"], entry["version"] = sop["name"], sop["version"]
			bom = rb._resolve_bom(item_code, None)
			if bom is None:
				entry["error"] = "no default BOM"
				continue
			step = _fill_step(sop["name"])
			if step is None:
				entry["error"] = 'no "Fill each jar" step'
				continue
			evaluate = _evaluator(bom)
			entry["rows"], entry["notes"] = derive_layers(step["instruction"], evaluate)
			if family == "Mango" and entry["rows"]:
				entry["rows"], note = _owner_mango_rows(size, entry["rows"])
				entry["notes"] = [note]
			if not entry["rows"]:
				entry["error"] = "no |each token resolved"
	return out


def _write(entry: dict[str, Any]) -> None:
	parent = entry["sop"]
	frappe.db.delete(LAYER_DOCTYPE, {"parent": parent, "parenttype": "Jarz SOP", "parentfield": "layers"})
	for idx, row in enumerate(entry["rows"], start=1):
		frappe.get_doc(
			{
				"doctype": LAYER_DOCTYPE,
				"name": frappe.generate_hash(length=10),
				"parent": parent,
				"parenttype": "Jarz SOP",
				"parentfield": "layers",
				"idx": idx,
				"layer": row["layer"],
				"grams": row["grams"],
				"note": None,
			}
		).db_insert()


def seed_jar_layers(dry_run: Any = 1) -> dict[str, Any]:
	"""Derive (and with ``dry_run=0`` write) the Tiramisu and Mango layers; prints ``MARK`` rows."""
	dry = rb._truthy(dry_run)
	entries = derive_all()
	written: list[str] = []
	for entry in entries:
		head = f"MARK {entry['item']} {entry['sop']} v{entry['version']}"
		if entry["error"]:
			print(f"{head} | SKIPPED: {entry['error']}")
			continue
		total = round(sum(r["grams"] for r in entry["rows"]), 1)
		print(f"{head} | layers {len(entry['rows'])} total {total} g")
		for idx, row in enumerate(entry["rows"], start=1):
			print(f"MARK   {entry['item']} #{idx} {row['layer']} = {row['grams']} g")
		for note in entry["notes"]:
			print(f"MARK   note: {note}")
		if not dry:
			_write(entry)
			written.append(entry["sop"])
	if not dry:
		frappe.db.commit()
	print(f"MARK dry_run={dry} written={written}")
	return {"dry_run": dry, "entries": entries, "written": written}

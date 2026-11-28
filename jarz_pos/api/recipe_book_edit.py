"""Recipe Book write side: edit, new version, approve / retire, new recipe, jar layers.

Contract: ``.scratch/recipe-book/CONTRACT_v3.md``.  Reads live in
``jarz_pos.api.recipe_book``; this module only adds the editor form and the
four write endpoints, all behind ``_ensure_recipe_edit_access`` (``ROLES.MANUFACTURING``),
all POST (except the editor read) and all-or-nothing: the work runs inside a
savepoint that is rolled back on any error, so a half-saved multi-size jar
update can never persist.

Instructions are edited as plain text with readable placeholders: every
``{{item:...}}`` token becomes ``[Coffee beans x5]``, and the page sends the
text back with the same placeholders plus the ``token_map`` it was given.  The
codec is pure (no frappe) and round-trips: unchanged text saves back to
byte-identical raw tokens.

A version that a Work Order was stamped with is frozen for good: its steps,
layers and ingredients can only change in a NEW version.
"""

from __future__ import annotations

import json
import math
import re
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import frappe

from jarz_pos.api import recipe_book as rb
from jarz_pos.api.sop import (
	SOP_DOCTYPE,
	_coerce_int,
	_field,
	_resolve_default_bom,
	_resolve_sop_doc,
	_step_rows,
)
from jarz_pos.services import sop_rendering as rendering

STAGE_APPROVED = rb.STAGE_APPROVED
STAGE_TRIAL = rb.STAGE_TRIAL
STAGE_RETIRED = rb.STAGE_RETIRED

APPROVE_OWN_AMOUNTS = (
	"This trial has its own amounts; production uses the BOM. Update the BOM in Desk, "
	"then approve with the BOM amounts."
)

TIMES = "×"  # the multiplication sign used in placeholder labels
MAX_LAYERS = 40
DATA_LEN = 140

_TOKEN_RE = rendering._TOKEN_RE
_BRACKET_RE = re.compile(r"\[([^\[\]\n]+)\]")
_MULT_RE = re.compile(r"(?:\s+[xX]|\s*[" + TIMES + r"*])\s*(\d+(?:\.\d+)?|\.\d+)\s*$")
_EACH_RE = re.compile(r"\s+each\s*$", re.IGNORECASE)
_ENTITY_AMP_RE = re.compile(r"&(?=#?\w+;)")

_STEP_COPY_FIELDS = (
	"title",
	"instruction",
	"image",
	"duration_mins",
	"scaling_mode",
	"requires_confirmation",
	"capture_type",
	"capture_label",
	"capture_min",
	"capture_max",
)
_INGREDIENT_COPY_FIELDS = ("ingredient", "item_code", "qty", "uom", "section", "note")
_HEAD_COPY_FIELDS = (
	"recipe_title",
	"item_code",
	"bom",
	"track",
	"category",
	"yield_qty",
	"yield_uom",
	"yield_percent",
	"prep_time_mins",
	"equipment",
	"notes",
)


def _clean(value: Any) -> str:
	return str(value or "").strip()


def _fail(message: str) -> None:
	frappe.throw(message, frappe.ValidationError)


# ── Number / payload validation (pure) ──────────────────────────────────


def to_number(value: Any, label: str, *, positive: bool = False, minimum: float | None = None) -> float:
	"""A finite number from a number or numeric text; refuses booleans, NaN, junk."""
	if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
		_fail(f"{label} must be a number.")
	try:
		number = float(value)
	except (TypeError, ValueError):
		_fail(f"{label} must be a number.")
	if math.isnan(number) or math.isinf(number):
		_fail(f"{label} must be a number.")
	if positive and number <= 0:
		_fail(f"{label} must be greater than 0.")
	if minimum is not None and number < minimum:
		_fail(f"{label} cannot be below {minimum:g}.")
	return number


def _data(value: Any, label: str, *, required: bool = False) -> str:
	text = _clean(value)
	if required and not text:
		_fail(f"{label} is required.")
	if len(text) > DATA_LEN:
		_fail(f"{label} is too long (the limit is {DATA_LEN} characters).")
	return text


def parse_payload(payload: Any) -> dict[str, Any]:
	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except ValueError:
			_fail("The payload is not valid JSON.")
	if not isinstance(payload, dict):
		_fail("The payload must be an object.")
	return payload


def normalise_layers(rows: Any) -> list[dict[str, Any]]:
	"""``[{layer, grams, note}]`` checked, in fill order."""
	if rows is None:
		return []
	if not isinstance(rows, list):
		_fail("layers must be a list.")
	if len(rows) > MAX_LAYERS:
		_fail(f"A jar can have at most {MAX_LAYERS} layers.")
	out = []
	for position, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			_fail(f"Layer {position} is not valid.")
		out.append(
			{
				"layer": _data(row.get("layer"), f"Layer {position} name", required=True),
				"grams": to_number(row.get("grams"), f"Layer {position} grams", positive=True),
				"note": _data(row.get("note"), f"Layer {position} note"),
			}
		)
	return out


def normalise_ingredients(rows: Any) -> list[dict[str, Any]]:
	"""``[{name, amount, unit, section, note, item_code}]`` checked."""
	if rows is None:
		return []
	if not isinstance(rows, list):
		_fail("ingredients must be a list.")
	out = []
	for position, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			_fail(f"Ingredient {position} is not valid.")
		out.append(
			{
				"name": _data(row.get("name"), f"Ingredient {position} name", required=True),
				"amount": to_number(row.get("amount"), f"Ingredient {position} amount", positive=True),
				"unit": _data(row.get("unit"), f"Ingredient {position} unit", required=True),
				"section": _data(row.get("section"), f"Ingredient {position} section"),
				"note": _clean(row.get("note")),
				"item_code": _data(row.get("item_code"), f"Ingredient {position} item code"),
			}
		)
	return out


def _optional_number(value: Any, label: str) -> float | None:
	if value is None or (isinstance(value, str) and not value.strip()):
		return None
	return to_number(value, label)


# ── Text <-> storage (pure) ─────────────────────────────────────────────


def _escape_text(segment: str) -> str:
	"""``<`` and ``>`` always (a stray one would eat text as a tag), ``&`` only when it would read as an entity."""
	return _ENTITY_AMP_RE.sub("&amp;", segment).replace("<", "&lt;").replace(">", "&gt;")


def to_storage(text: Any) -> str:
	"""Plain editor text to what a Text Editor field stores: ``<br>`` per line break.

	Tokens are copied untouched; only the text between them is escaped.  The
	``<br>`` keeps a following newline so the Production Board, which just
	strips tags, still shows the break (and ``html_to_text`` reads the pair as one).
	"""
	source = ("" if text is None else str(text)).replace("\r\n", "\n").replace("\r", "\n").strip()
	pieces: list[str] = []
	cursor = 0
	for match in _TOKEN_RE.finditer(source):
		pieces.append(_escape_text(source[cursor : match.start()]))
		pieces.append(match.group(0))
		cursor = match.end()
	pieces.append(_escape_text(source[cursor:]))
	return "".join(pieces).replace("\n", "<br>\n")


# ── Placeholder codec (pure) ────────────────────────────────────────────


def _fmt_number(value: float) -> str:
	return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


class Maps:
	"""The component maps one recipe's tokens resolve against."""

	def __init__(self, qty_map: dict, uom_map: dict, name_map: dict):
		self.qty = qty_map or {}
		self.uom = uom_map or {}
		self.name = name_map or {}
		self.lowered = rendering._lowered_index(self.qty, self.name, self.uom)


def token_label(payload: str, maps: Maps) -> str | None:
	"""``"Coffee beans|grams|x5"`` -> ``"Coffee beans x5"`` (without brackets); ``None`` if it does not resolve."""
	code, variant, multiplier, each, valid = rendering._split_payload(payload)
	if not valid or not code:
		return None
	parts = rendering._resolve_parts(code, maps.qty, maps.name, maps.uom, maps.lowered)
	if parts is None:
		return None
	label = " + ".join(_clean(maps.name.get(part)) or part for part, _weight in parts)
	if multiplier != 1.0:
		label += f" {TIMES}{_fmt_number(multiplier)}"
	if each:
		label += " each"
	suffix = {"qty": "(qty)", "count": "(count)", "name": "(name)", "uom": "(uom)", "": "(full)"}.get(variant)
	if suffix:
		label += f" {suffix}"
	return label.replace("[", "(").replace("]", ")")


class TokenCodec:
	"""Raw ``{{item:...}}`` tokens to ``[label]`` placeholders, one label per distinct raw token."""

	def __init__(self, maps: Maps, texts: list[str] | None = None):
		self.maps = maps
		self.token_map: dict[str, str] = {}
		self._by_raw: dict[str, str] = {}
		# A literal "[x]" the author already wrote must not be mistaken for a label.
		self._taken: set[str] = set()
		for text in texts or []:
			for found in _BRACKET_RE.findall(_TOKEN_RE.sub("", text or "")):
				self._taken.add(f"[{found}]")

	def encode(self, text: Any) -> str:
		def _replace(match: re.Match[str]) -> str:
			raw = match.group(0)
			if raw in self._by_raw:
				return self._by_raw[raw]
			label = token_label(match.group(1), self.maps)
			if label is None:
				return raw  # unresolved: stays a raw token, round-trips as is
			candidate, count = f"[{label}]", 1
			while candidate in self.token_map or candidate in self._taken:
				count += 1
				candidate = f"[{label} #{count}]"
			self.token_map[candidate] = raw
			self._by_raw[raw] = candidate
			return candidate

		return _TOKEN_RE.sub(_replace, "" if text is None else str(text))


def parse_placeholder(inner: str) -> tuple[str, float | None, bool]:
	"""``"Coffee beans x5 each"`` -> ``("Coffee beans", 5.0, True)`` (x, * or the times sign)."""
	name, multiplier, each = inner.strip(), None, False
	for _round in range(2):
		found = _EACH_RE.search(name)
		if found and not each:
			each, name = True, name[: found.start()]
			continue
		found = _MULT_RE.search(name)
		if found and multiplier is None:
			multiplier, name = float(found.group(1)), name[: found.start()]
			continue
		break
	return name.strip(), multiplier, each


class PlaceholderDecoder:
	"""``[label]`` placeholders back to raw tokens; unknown ones stay text and are warned about."""

	def __init__(self, token_map: dict[str, str] | None, ingredients: list[dict[str, Any]]):
		self.token_map: dict[str, str] = {}
		self.warnings: list[str] = []
		for label, raw in (token_map or {}).items():
			label_text, raw_text = _clean(label), _clean(raw)
			if _TOKEN_RE.fullmatch(raw_text) and label_text.startswith("[") and label_text.endswith("]"):
				self.token_map[label_text] = raw_text
			else:
				self._warn(f"Ignored the invalid placeholder entry {label_text or '(blank)'}")
		self._index: dict[str, dict[str, Any]] = {}
		for ingredient in ingredients:
			for name in (_clean(ingredient.get("item_code")), _clean(ingredient.get("name"))):
				if name:
					self._index.setdefault(name.lower(), ingredient)

	def _warn(self, message: str) -> None:
		if message not in self.warnings:
			self.warnings.append(message)

	def _from_ingredient(self, inner: str) -> str | None:
		name, multiplier, each = parse_placeholder(inner)
		ingredient = self._index.get(name.lower())
		if ingredient is None:
			return None
		code = _clean(ingredient.get("item_code")) or _clean(ingredient.get("name"))
		if not code or any(ch in code for ch in "|{}"):
			return None
		if multiplier is not None and multiplier <= 0:
			return None
		variant = "grams" if rendering.to_grams(1.0, ingredient.get("unit")) is not None else "qty"
		pieces = [f"item:{code}", variant]
		if multiplier is not None:
			pieces.append(f"x{_fmt_number(multiplier)}")
		if each:
			pieces.append("each")
		return "{{" + "|".join(pieces) + "}}"

	def decode(self, text: Any) -> str:
		source = "" if text is None else str(text)

		def _plain(segment: str) -> str:
			def _replace(match: re.Match[str]) -> str:
				label = match.group(0)
				raw = self.token_map.get(label) or self._from_ingredient(match.group(1))
				if raw:
					return raw
				self._warn(f"Unknown placeholder {label} was left as text")
				return label

			return _BRACKET_RE.sub(_replace, segment)

		pieces: list[str] = []
		cursor = 0
		for match in _TOKEN_RE.finditer(source):  # raw tokens the author typed stay as they are
			pieces.append(_plain(source[cursor : match.start()]))
			pieces.append(match.group(0))
			cursor = match.end()
		pieces.append(_plain(source[cursor:]))
		return "".join(pieces)


# ── Step normalisation (pure) ───────────────────────────────────────────


def normalise_steps(rows: Any, decoder: PlaceholderDecoder) -> list[dict[str, Any]]:
	"""Payload steps to row dicts for the child table, placeholders turned back into tokens."""
	if rows is None:
		return []
	if not isinstance(rows, list):
		_fail("steps must be a list.")
	out = []
	for position, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			_fail(f"Step {position} is not valid.")
		title = _data(decoder.decode(_clean(row.get("title"))), f"Step {position} title", required=True)
		mode = _clean(row.get("scaling_mode")) or rendering.SCALING_FIXED
		if mode not in rendering.SCALING_MODES:
			_fail(f"Step {position}: scaling mode must be one of {', '.join(rendering.SCALING_MODES)}.")
		capture = _clean(row.get("capture_type")) or rendering.CAPTURE_NONE
		if capture not in rendering.CAPTURE_TYPES:
			_fail(f"Step {position}: capture type must be one of {', '.join(rendering.CAPTURE_TYPES)}.")
		capture_min = _optional_number(row.get("capture_min"), f"Step {position} capture minimum")
		capture_max = _optional_number(row.get("capture_max"), f"Step {position} capture maximum")
		if capture_min is not None and capture_max is not None and capture_min > capture_max:
			_fail(f"Step {position}: the capture minimum is above the maximum.")
		confirm = row.get("requires_confirmation", 1)
		out.append(
			{
				"title": title,
				"instruction": to_storage(decoder.decode(row.get("text"))),
				"image": _clean(row.get("image")) or None,
				"duration_mins": to_number(
					row.get("duration_mins") or 0, f"Step {position} duration", minimum=0
				),
				"scaling_mode": mode,
				"requires_confirmation": 1 if (confirm in (None, "") or rb._truthy(confirm)) else 0,
				"capture_type": capture,
				"capture_label": _data(row.get("capture_label"), f"Step {position} capture label"),
				"capture_min": capture_min,
				"capture_max": capture_max,
			}
		)
	return out


# ── The editor form ─────────────────────────────────────────────────────


def _bom_codes(data: dict[str, Any]) -> dict[str, str]:
	return {code.strip().lower(): code for code in data.get("order") or []}


def editor_ingredients(data: dict[str, Any]) -> list[dict[str, Any]]:
	"""Ingredient rows for the form: the BOM (read-only) or the version's own table, as stored."""
	if data["use_bom"]:
		codes = _bom_codes(data)
		return [
			{
				"name": entry["name"],
				"amount": entry["amount"],
				"unit": entry["unit"],
				"section": entry["section"],
				"note": entry["note"],
				"item_code": codes.get(entry["key"]) or entry["name"],
			}
			for entry in data["ingredients"]
		]
	out = []
	for row in data["table_rows"]:
		out.append(
			{
				"name": _clean(_field(row, "ingredient")),
				"amount": rendering.to_float(_field(row, "qty"), 0.0),
				"unit": _clean(_field(row, "uom")),
				"section": _clean(_field(row, "section")) or None,
				"note": _clean(_field(row, "note")) or None,
				"item_code": _clean(_field(row, "item_code")) or None,
			}
		)
	return out


def build_editor_steps(
	step_rows: list[dict[str, Any]], data: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, str]]:
	"""``(steps, token_map)`` for the form: plain text with ``[labels]``, recipe-wide label map."""
	texts: list[str] = []
	for raw in step_rows:
		texts.append(rb.html_to_text(raw.get("title")))
		texts.append(rb.html_to_text(raw.get("instruction")))
	codec = TokenCodec(Maps(data["qty_map"], data["uom_map"], data["name_map"]), texts)

	steps = []
	for raw in step_rows:
		capture = _clean(raw.get("capture_type"))
		numeric = capture in rendering.NUMERIC_CAPTURE_TYPES
		steps.append(
			{
				"title": codec.encode(rb.html_to_text(raw.get("title"))),
				"text": codec.encode(rb.html_to_text(raw.get("instruction"))),
				"duration_mins": rendering.to_float(raw.get("duration_mins"), 0.0),
				"scaling_mode": _clean(raw.get("scaling_mode")) or rendering.SCALING_FIXED,
				"requires_confirmation": 1 if rb._truthy(raw.get("requires_confirmation")) else 0,
				"capture_type": None if capture in ("", rendering.CAPTURE_NONE) else capture,
				"capture_label": _clean(raw.get("capture_label")) or None,
				"capture_min": rendering.to_float(raw.get("capture_min"), 0.0) if numeric else None,
				"capture_max": rendering.to_float(raw.get("capture_max"), 0.0) if numeric else None,
				"image": _clean(raw.get("image")) or None,
			}
		)
	return steps, dict(codec.token_map)


def _next_version(rows: list[dict[str, Any]]) -> int:
	return max((_coerce_int(_field(r, "version"), 0) for r in rows), default=0) + 1


def build_edit_form(doc: Any) -> dict[str, Any]:
	"""The editable form of one stored version."""
	item_code = _clean(_field(doc, "item_code"))
	title = _clean(_field(doc, "recipe_title")) or _clean(_field(doc, "item_name")) or item_code
	data = rb.recipe_data(doc)
	steps, token_map = build_editor_steps(_step_rows(doc), data)
	siblings = rb.sibling_rows(doc)
	used = rb.is_used_in_production(doc.name)
	category, family, size = rb.classify_with_stored(
		title, rb._item_group(item_code), _field(doc, "category")
	)
	yield_qty = rendering.to_float(_field(doc, "yield_qty"), 0.0)
	return {
		"base": doc.name,
		"item_code": item_code or None,
		"title": title,
		"track": _clean(_field(doc, "track")) or None,
		"category": category,
		"size": size,
		"family": family,
		"stage": rb._stage(doc),
		"version": _coerce_int(_field(doc, "version"), 1),
		"next_version": _next_version(siblings),
		"editable_in_place": not used,
		"used_in_production": used,
		"ingredients_source": "bom" if data["use_bom"] else "table",
		"ingredients": editor_ingredients(data),
		"yield_qty": yield_qty if yield_qty > 0 else None,
		"yield_uom": _clean(_field(doc, "yield_uom")) or None,
		"prep_time_mins": _coerce_int(_field(doc, "prep_time_mins"), 0),
		"equipment": _clean(_field(doc, "equipment")),
		"notes": rb.html_to_text(_field(doc, "notes")),
		"change_summary": "",
		"layers": rb.doc_layers(doc),
		"steps": steps,
		"token_map": token_map,
	}


def _bom_blank_form(item_code: str) -> dict[str, Any]:
	"""A blank first-version form for an item that has a default BOM and no SOP."""
	if not frappe.db.exists("Item", item_code):
		raise frappe.DoesNotExistError(f"No item {item_code}")
	stub = SimpleNamespace(item_code=item_code, ingredients=[], stage=STAGE_APPROVED, bom=None)
	data = rb.recipe_data(stub)
	if not data["use_bom"]:
		raise frappe.DoesNotExistError(f"No recipe or BOM for {item_code}")
	name = _clean(frappe.db.get_value("Item", item_code, "item_name")) or item_code
	category, family, size = rb.classify_with_stored(name, rb._item_group(item_code), None)
	return {
		"base": None,
		"item_code": item_code,
		"title": name,
		"track": None,
		"category": category,
		"size": size,
		"family": family,
		"stage": STAGE_APPROVED,
		"version": None,
		"next_version": 1,
		"editable_in_place": False,
		"used_in_production": False,
		"ingredients_source": "bom",
		"ingredients": editor_ingredients(data),
		"yield_qty": None,
		"yield_uom": None,
		"prep_time_mins": 0,
		"equipment": "",
		"notes": "",
		"change_summary": "",
		"layers": [],
		"steps": [],
		"token_map": {},
	}


@frappe.whitelist()
def get_recipe_for_edit(sop: str | None = None, item_code: str | None = None) -> dict[str, Any]:
	"""The editable form of a version; for a BOM-only item, a blank form from Item + BOM."""
	rb._ensure_recipe_edit_access()
	if _clean(sop):
		return build_edit_form(_resolve_sop_doc(_clean(sop)))
	code = _clean(item_code)
	if not code:
		raise frappe.DoesNotExistError("Pass sop or item_code")
	rows = rb._fetch_rows({"item_code": code})
	if rows:
		best = max(rows, key=lambda r: (1 if _coerce_int(r.get("is_active"), 0) else 0, rb._sort_key(r)))
		return build_edit_form(_resolve_sop_doc(best["name"]))
	return _bom_blank_form(code)


# ── Write helpers ───────────────────────────────────────────────────────


@contextmanager
def _atomic(name: str):
	"""Everything inside lands or nothing does."""
	frappe.db.savepoint(name)
	try:
		yield
	except Exception:
		frappe.db.rollback(save_point=name)
		raise


def _today() -> str:
	return str(frappe.utils.today())


def _will_be_active(stage: str, item_code: str, step_count: int) -> int:
	return 1 if (stage == STAGE_APPROVED and item_code and step_count > 0) else 0


def _replaced_by(item_code: str, name: str | None) -> str | None:
	"""The version that is active for this item right now and would be deactivated."""
	if not item_code:
		return None
	filters: dict[str, Any] = {"item_code": item_code, "is_active": 1}
	if name:
		filters["name"] = ["!=", name]
	found = frappe.get_all(SOP_DOCTYPE, filters=filters, pluck="name", limit_page_length=1)
	return found[0] if found else None


def _check_items_exist(rows: list[dict[str, Any]]) -> None:
	for row in rows:
		code = row.get("item_code")
		if code and not frappe.db.exists("Item", code):
			_fail(f"Item {code} does not exist.")


def _child_rows(doc: Any, table: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
	rows = sorted(list(_field(doc, table) or []), key=lambda r: _coerce_int(_field(r, "idx"), 0))
	return [{name: _field(row, name) for name in fields} for row in rows]


def _set_children(doc: Any, table: str, rows: list[dict[str, Any]]) -> None:
	doc.set(table, [])
	for row in rows:
		doc.append(table, dict(row))


def _ingredient_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return [
		{
			"ingredient": r["name"],
			"item_code": r["item_code"] or None,
			"qty": r["amount"],
			"uom": r["unit"],
			"section": r["section"],
			"note": r["note"],
		}
		for r in rows
	]


def _layer_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	return [{"layer": r["layer"], "grams": r["grams"], "note": r["note"] or None} for r in rows]


def _title_for(item_code: str) -> str:
	return _clean(frappe.db.get_value("Item", item_code, "item_name")) or item_code


def _stored_category(
	payload_key: Any, title: str, item_code: str, previous: str, always: bool = False
) -> str:
	"""The Select label to store; blank when the heuristic already says the same (unless ``always``)."""
	key = _clean(payload_key).lower()
	if not key:
		return ""
	if key not in rb.CATEGORY_KEYS:
		_fail(f"Category must be one of: {', '.join(rb.CATEGORY_KEYS)}.")
	default = rb.classify_recipe(title, rb._item_group(item_code))[0]
	if key == default and not previous and not always:
		return ""
	return rb.CATEGORY_LABELS[key]


def _stage_value(value: Any, default: str) -> str:
	stage = _clean(value) or default
	if stage not in (STAGE_APPROVED, STAGE_TRIAL):
		_fail("Stage must be Trial or Approved (use Retire to withdraw a version).")
	return stage


def _insert_version(values: dict[str, Any], steps: list, ingredients: list, layers: list) -> Any:
	doc = frappe.new_doc(SOP_DOCTYPE)
	for key, value in values.items():
		setattr(doc, key, value)
	_set_children(doc, "steps", steps)
	_set_children(doc, "ingredients", ingredients)
	_set_children(doc, "layers", layers)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc


def _check_title_free(title: str, exclude_name: str | None = None) -> None:
	"""A no-Item recipe is keyed by its title: never merge into another recipe by accident."""
	clash = frappe.db.exists(SOP_DOCTYPE, {"recipe_title": title}) or frappe.db.exists(
		SOP_DOCTYPE, {"item_code": title}
	)
	if clash and clash != exclude_name:
		_fail(f"A recipe called {title} already exists. Open it and save a new version instead.")


# ── save_recipe ─────────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def save_recipe(payload: Any) -> dict[str, Any]:
	"""Save an edit, a new version, a first version of a BOM recipe, or a brand-new recipe."""
	rb._ensure_recipe_edit_access()
	data = parse_payload(payload)
	with _atomic("recipe_save"):
		return _save_recipe(data)


def _save_recipe(data: dict[str, Any]) -> dict[str, Any]:
	base_name = _clean(data.get("base"))
	base = None
	if base_name:
		mode = _clean(data.get("mode")).lower()
		if mode not in ("in_place", "new_version"):
			_fail("mode must be in_place or new_version.")
		base = frappe.get_doc(SOP_DOCTYPE, base_name)
		item_code = _clean(_field(base, "item_code"))
		if mode == "in_place" and rb.is_used_in_production(base.name):
			_fail(
				"This version has been used in production, so it cannot be edited. Save it as a new version."
			)
	else:
		mode = "create"
		item_code = _clean(data.get("item_code"))
		if item_code:
			if not frappe.db.exists("Item", item_code):
				_fail(f"Item {item_code} does not exist.")
			if rb._fetch_rows({"item_code": item_code}):
				_fail(f"{item_code} already has recipe versions. Open one and save a new version.")

	first_bom = mode == "create" and bool(item_code)
	if mode == "create":
		stage_default = STAGE_APPROVED if first_bom else STAGE_TRIAL
	elif mode == "new_version":
		stage_default = STAGE_TRIAL
	else:
		stage_default = rb._stage(base)
	stage = _stage_value(data.get("stage"), stage_default)

	# ── head fields
	def _from_base(key: str, default: Any = "") -> Any:
		return _field(base, key) if base is not None and _field(base, key) is not None else default

	if "title" in data and _clean(data.get("title")):
		title = _data(data.get("title"), "Title")
	else:
		title = _clean(_from_base("recipe_title")) or (_title_for(item_code) if item_code else "")
	if not title:
		_fail("Title is required for a recipe with no item.")

	track = _clean(data["track"]) if "track" in data else _clean(_from_base("track"))
	if track and track not in rb.TRACK_ORDER:
		_fail(f"Track must be one of: {', '.join(rb.TRACK_ORDER)}.")
	if "category" in data:
		category = _stored_category(
			data.get("category"), title, item_code, _clean(_from_base("category")), always=mode == "create"
		)
	else:
		category = _clean(_from_base("category"))

	yield_qty = (
		_optional_number(data.get("yield_qty"), "Yield quantity")
		if "yield_qty" in data
		else rendering.to_float(_from_base("yield_qty", None), 0.0) or None
	)
	if yield_qty is not None and yield_qty <= 0:
		yield_qty = None
	yield_uom = (
		_data(data["yield_uom"], "Yield unit") if "yield_uom" in data else _clean(_from_base("yield_uom"))
	)
	prep = (
		int(to_number(data.get("prep_time_mins") or 0, "Prep time", minimum=0))
		if "prep_time_mins" in data
		else _coerce_int(_from_base("prep_time_mins", 0), 0)
	)
	equipment = _clean(data["equipment"]) if "equipment" in data else _clean(_from_base("equipment"))
	notes = to_storage(data["notes"]) if "notes" in data else _from_base("notes", "")
	change_summary = _clean(data.get("change_summary"))
	if mode == "new_version" and not change_summary:
		_fail("Say what changed (change summary) before saving a new version.")

	# ── ingredients: the BOM wins whenever the version uses one
	bom = rb._resolve_bom(item_code, _field(base, "bom") if base is not None else None) if item_code else None
	payload_ingredients = normalise_ingredients(data.get("ingredients")) if "ingredients" in data else None
	base_data = rb.recipe_data(base) if base is not None else None
	base_uses_bom = bool(base_data and base_data["use_bom"])

	def _bom_form() -> list[dict[str, Any]]:
		stub = SimpleNamespace(item_code=item_code, ingredients=[], stage=STAGE_APPROVED, bom=None)
		return editor_ingredients(rb.recipe_data(stub))

	if bom is not None and (base_uses_bom or first_bom):
		# Amounts in the payload are ignored: production follows the BOM.
		ingredient_source, ingredient_rows, resolvable = "bom", [], _bom_form()
	elif bom is None:
		if payload_ingredients is None:
			payload_ingredients = normalise_ingredients(editor_ingredients(base_data) if base_data else [])
		_check_items_exist(payload_ingredients)
		ingredient_source, ingredient_rows, resolvable = "table", payload_ingredients, payload_ingredients
	else:
		# A trial with its own amounts on an item that has a BOM.
		if payload_ingredients is None:
			payload_ingredients = normalise_ingredients(editor_ingredients(base_data))
		_check_items_exist(payload_ingredients)
		if payload_ingredients and stage == STAGE_APPROVED:
			_fail(APPROVE_OWN_AMOUNTS)
		if payload_ingredients:
			ingredient_source, ingredient_rows, resolvable = "table", payload_ingredients, payload_ingredients
		else:
			ingredient_source, ingredient_rows, resolvable = "bom", [], _bom_form()

	# ── layers and steps
	if "layers" in data:
		layers = normalise_layers(data.get("layers"))
	else:
		layers = rb.doc_layers(base) if base is not None else []
	warnings: list[str] = []
	if base is not None and "steps" not in data:
		step_rows = _child_rows(base, "steps", _STEP_COPY_FIELDS)
	else:
		fallback_map = build_editor_steps(_step_rows(base), base_data)[1] if base is not None else {}
		supplied = data.get("token_map") if isinstance(data.get("token_map"), dict) else {}
		decoder = PlaceholderDecoder({**fallback_map, **supplied}, resolvable)
		step_rows = normalise_steps(data.get("steps"), decoder)
		warnings = decoder.warnings

	# ── persist
	if mode == "create" and not item_code:
		_check_title_free(title)
	if base is not None and not item_code and title != _clean(_field(base, "recipe_title")):
		_check_title_free(title, base.name)

	approved_on = _today() if stage == STAGE_APPROVED else None
	child_ingredients = _ingredient_rows(ingredient_rows) if ingredient_source == "table" else []
	active = _will_be_active(stage, item_code, len(step_rows))

	if mode == "in_place":
		previous_stage = rb._stage(base)
		old_title = _clean(_field(base, "recipe_title"))
		base.recipe_title = title
		base.track = track
		base.category = category
		base.stage = stage
		if stage == STAGE_APPROVED and not _field(base, "approved_on"):
			base.approved_on = approved_on
		if stage != STAGE_APPROVED:
			base.approved_on = None
		if stage == STAGE_APPROVED and previous_stage == STAGE_APPROVED:
			# Editing a "previous" version in place must not make it the live one.
			active = 1 if (_coerce_int(_field(base, "is_active"), 0) and active) else 0
		base.yield_qty = yield_qty
		base.yield_uom = yield_uom
		base.prep_time_mins = prep
		base.equipment = equipment
		base.notes = notes
		if change_summary:
			base.change_summary = change_summary
		base.is_active = active
		_set_children(base, "steps", step_rows)
		_set_children(base, "ingredients", child_ingredients)
		_set_children(base, "layers", _layer_rows(layers))
		base.flags.ignore_permissions = True
		base.save()
		if not item_code and old_title and old_title != title:
			frappe.db.set_value(
				SOP_DOCTYPE,
				{"recipe_title": old_title, "item_code": ["is", "not set"]},
				"recipe_title",
				title,
				update_modified=False,
			)
		name = base.name
	else:
		siblings = (
			rb._fetch_rows({"item_code": item_code})
			if item_code
			else rb._fetch_rows({"recipe_title": title, "item_code": ["is", "not set"]})
		)
		values = {
			"recipe_title": title,
			"item_code": item_code or None,
			"bom": (bom["name"] if bom is not None else None) if item_code else None,
			"stage": stage,
			"track": track or None,
			"category": category or None,
			"version": _next_version(siblings) if mode == "new_version" else 1,
			"is_active": active,
			"approved_on": approved_on,
			"change_summary": change_summary,
			"yield_qty": yield_qty,
			"yield_uom": yield_uom,
			"yield_percent": _from_base("yield_percent", 100),
			"prep_time_mins": prep,
			"equipment": equipment,
			"notes": notes,
		}
		doc = _insert_version(values, step_rows, child_ingredients, _layer_rows(layers))
		name = doc.name

	return {"recipe": rb._sop_payload(name), "warnings": warnings}


# ── set_recipe_stage ────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def set_recipe_stage(sop: str, stage: str, use_bom: Any = 0) -> dict[str, Any]:
	"""Approve, retire, or send a version back to Trial."""
	rb._ensure_recipe_edit_access()
	with _atomic("recipe_stage"):
		return _set_recipe_stage(_clean(sop), _clean(stage), rb._truthy(use_bom))


def _set_recipe_stage(sop: str, stage: str, use_bom: bool) -> dict[str, Any]:
	if stage not in (STAGE_APPROVED, STAGE_TRIAL, STAGE_RETIRED):
		_fail("Stage must be Approved, Trial or Retired.")
	doc = frappe.get_doc(SOP_DOCTYPE, sop)
	item_code = _clean(_field(doc, "item_code"))
	used = rb.is_used_in_production(doc.name)
	replaced = None

	if stage == STAGE_APPROVED:
		own_rows = list(_field(doc, "ingredients") or [])
		bom = rb._resolve_bom(item_code, _field(doc, "bom")) if item_code else None
		if own_rows and bom is not None:
			if not use_bom:
				_fail(APPROVE_OWN_AMOUNTS)
			if used:
				_fail("This version has been used in production; its ingredients cannot change.")
			doc.set("ingredients", [])
		steps = list(_field(doc, "steps") or [])
		active = _will_be_active(stage, item_code, len(steps))
		if active:
			replaced = _replaced_by(item_code, doc.name)
		if rb._stage(doc) != STAGE_APPROVED or not _field(doc, "approved_on"):
			doc.approved_on = _today()
		doc.stage = STAGE_APPROVED
		doc.is_active = active
	elif stage == STAGE_RETIRED:
		if item_code and rb._resolve_bom(item_code, _field(doc, "bom")) is not None:
			status = _status_of(doc)
			if status == "current":
				_fail("Approve another version first: this is the current version of an item that has a BOM.")
		doc.stage = STAGE_RETIRED
		doc.is_active = 0
	else:
		if used:
			_fail("This version has been used in production, so it cannot go back to Trial.")
		doc.stage = STAGE_TRIAL
		doc.is_active = 0
		doc.approved_on = None

	doc.flags.ignore_permissions = True
	doc.save()
	payload = rb._sop_payload(doc.name)
	payload["replaced"] = replaced
	return payload


def _status_of(doc: Any) -> str | None:
	rows = rb.sibling_rows(doc)
	if not rows:
		return None
	return rb.version_statuses(rows).get(doc.name)


# ── save_jar_layers ─────────────────────────────────────────────────────


def normalise_family_layers(layers: Any) -> dict[str, list[dict[str, Any]]]:
	"""``{size: [{layer, grams, note}]}`` from ``[{layer, note, grams: {size: g}}]``.

	A layer with no value for a size is not written for that size.  Sizes come
	back in Small, Medium, Large order, only those with at least one value.
	"""
	if isinstance(layers, str):
		try:
			layers = json.loads(layers)
		except ValueError:
			_fail("layers is not valid JSON.")
	if not isinstance(layers, list) or not layers:
		_fail("Give at least one layer.")
	if len(layers) > MAX_LAYERS:
		_fail(f"A jar can have at most {MAX_LAYERS} layers.")
	by_size: dict[str, list[dict[str, Any]]] = {size: [] for size in rb.SIZES}
	lookup = {size.lower(): size for size in rb.SIZES}
	for position, row in enumerate(layers, start=1):
		if not isinstance(row, dict) or not isinstance(row.get("grams"), dict):
			_fail(f"Layer {position} needs a name and a grams value per size.")
		name = _data(row.get("layer"), f"Layer {position} name", required=True)
		note = _data(row.get("note"), f"Layer {position} note")
		for raw_size, value in row["grams"].items():
			size = lookup.get(_clean(raw_size).lower())
			if size is None:
				_fail(f"Layer {position}: {raw_size} is not a size (Small, Medium or Large).")
			if value is None or (isinstance(value, str) and not value.strip()):
				continue
			by_size[size].append(
				{
					"layer": name,
					"grams": to_number(value, f"Layer {position} {size} grams", positive=True),
					"note": note,
					"_pos": position,
				}
			)
	out = {}
	for size in rb.SIZES:
		rows = sorted(by_size[size], key=lambda r: r["_pos"])
		if rows:
			out[size] = [{k: v for k, v in r.items() if k != "_pos"} for r in rows]
	if not out:
		_fail("Give a weight for at least one size.")
	return out


@frappe.whitelist(methods=["POST"])
def save_jar_layers(family: str, layers: Any, change_summary: str | None = None) -> dict[str, Any]:
	"""Set the per-layer weights of a jar flavour, every size at once, all or nothing."""
	rb._ensure_recipe_edit_access()
	with _atomic("jar_layers"):
		return _save_jar_layers(_clean(family), layers, _clean(change_summary))


def _save_jar_layers(family: str, layers: Any, change_summary: str) -> dict[str, Any]:
	wanted = family.lower()
	members = {
		r["size"]: r
		for r in rb._catalog()["recipes"]
		if r["category"] == "jars" and r["size"] and r["family"] and r["family"].lower() == wanted
	}
	if not members:
		raise frappe.DoesNotExistError(f"No jar family {family}")
	by_size = normalise_family_layers(layers)

	plan: list[tuple[str, dict[str, Any], Any, str]] = []  # size, recipe, doc, action
	for size in by_size:
		rec = members.get(size)
		if rec is None or not rec.get("item_code"):
			_fail(f"{family} has no {size} jar.")
		headline = rec.get("headline")
		if not headline:
			plan.append((size, rec, None, "first"))
			continue
		doc = frappe.get_doc(SOP_DOCTYPE, headline)
		if rb._stage(doc) == STAGE_RETIRED:
			_fail(f"{rec['title']} is retired; create a new recipe version in the editor first.")
		action = "new_version" if rb.is_used_in_production(doc.name) else "in_place"
		if action == "new_version" and not change_summary:
			_fail(
				f"Say what changed (change summary): {rec['title']} was used in production, so it gets a new version."
			)
		plan.append((size, rec, doc, action))

	created: list[str] = []
	updated: list[str] = []
	for size, rec, doc, action in plan:
		rows = _layer_rows(by_size[size])
		if action == "in_place":
			_set_children(doc, "layers", rows)
			doc.flags.ignore_permissions = True
			doc.save()
			updated.append({"size": size, "sop": doc.name})
			continue
		item_code = rec["item_code"]
		if action == "first":
			bom = _resolve_default_bom(item_code)
			values = {
				"recipe_title": rec["title"],
				"item_code": item_code,
				"bom": bom,
				"stage": STAGE_APPROVED,
				"version": 1,
				"is_active": 0,
				"approved_on": _today(),
				"change_summary": change_summary,
				"yield_percent": 100,
			}
			new = _insert_version(values, [], [], rows)
		else:
			stage = rb._stage(doc)
			steps = _child_rows(doc, "steps", _STEP_COPY_FIELDS)
			values = {key: _field(doc, key) for key in _HEAD_COPY_FIELDS}
			values.update(
				stage=stage,
				version=_next_version(rb.sibling_rows(doc)),
				is_active=_will_be_active(stage, item_code, len(steps)),
				approved_on=_today() if stage == STAGE_APPROVED else None,
				change_summary=change_summary,
			)
			new = _insert_version(
				values, steps, _child_rows(doc, "ingredients", _INGREDIENT_COPY_FIELDS), rows
			)
		created.append({"size": size, "sop": new.name})

	result = rb.get_jar_family(next(iter(members.values()))["family"])
	result["created"] = created
	result["updated"] = updated
	return result

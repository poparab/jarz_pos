"""Recipe Book v3: placeholder codec, edit / new version / approve, jar layers, layer seed.

No database: frappe is replaced by a mock whose ``throw`` raises, documents are
small fakes, and the version / BOM reads are patched.  The codec tests run the
REAL Tiramisu and Mango "Fill each jar" texts from the seeder through the
editor and back, and expect the raw tokens to come out identical.
"""

import re
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.api import recipe_book as rb
from jarz_pos.api import recipe_book_edit as edit
from jarz_pos.scripts import seed_jar_layers as seed_layers
from jarz_pos.scripts import seed_recipe_sops as seed

TOKEN = re.compile(r"\{\{[^{}]+\}\}")


class Invalid(Exception):
	pass


class Forbidden(Exception):
	pass


class Missing(Exception):
	pass


def fake_frappe():
	m = MagicMock()
	m.ValidationError, m.PermissionError, m.DoesNotExistError = Invalid, Forbidden, Missing

	def _throw(message, exc=None, *args, **kwargs):
		raise (exc or Invalid)(message)

	m.throw.side_effect = _throw
	m.db.exists.return_value = True
	m.get_all.return_value = []
	return m


class FakeDoc:
	"""A Jarz SOP stand-in: attributes, child lists, and a record of what was done to it."""

	def __init__(self, **fields):
		self.name = fields.pop("name", None)
		self.steps, self.ingredients, self.layers = [], [], []
		self.flags = SimpleNamespace()
		self.saved = self.inserted = False
		for key, value in fields.items():
			setattr(self, key, value)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def append(self, table, row):
		getattr(self, table).append(dict(row))

	def save(self):
		self.saved = True

	def insert(self):
		self.inserted = True
		self.name = self.name or "SOP-NEW"


def data_for(use_bom=False, ingredients=None, rows=None, order=None):
	return {
		"use_bom": use_bom,
		"bom": {"name": "BOM-1", "quantity": 1} if use_bom else None,
		"table_rows": rows or [],
		"ingredients": ingredients or [],
		"qty_map": {},
		"uom_map": {},
		"name_map": {},
		"order": order or [],
		"alias_to_key": {},
		"units_per_batch": None,
		"yield": {"label": "One batch", "amount": None, "unit": None},
	}


def step(title="Mix", text="Mix well", **extra):
	return {"title": title, "text": text, "duration_mins": 5, **extra}


def raw_step(title, instruction, **extra):
	return {
		"title": title,
		"instruction": instruction,
		"duration_mins": 1,
		"scaling_mode": "Fixed",
		"requires_confirmation": 1,
		"capture_type": "None",
		**extra,
	}


# ── Placeholder codec on the real recipes ───────────────────────────────

KG = "Kg"
TIRAMISU_SMALL = {
	"Savoiardi": 0.0187,
	"Coffee beans": 0.004,
	"powder sugar": 0.0024,
	"coco powder": 0.0013,
	"Cheesecake Mix": 0.0431,
}
MANGO_SMALL = {"Mango mix": 0.0733, "Cheesecake Mix": 0.0425, "Savoiardi": 0.0147}


def tiramisu_data():
	return {
		"qty_map": dict(TIRAMISU_SMALL),
		"uom_map": dict.fromkeys(TIRAMISU_SMALL, KG),
		"name_map": {k: k.capitalize() if k.islower() else k for k in TIRAMISU_SMALL},
	}


def mango_data():
	return {
		"qty_map": dict(MANGO_SMALL),
		"uom_map": dict.fromkeys(MANGO_SMALL, KG),
		"name_map": {k: k for k in MANGO_SMALL},
	}


def tokens_of(text):
	return TOKEN.findall(rb.html_to_text(text))


class TestPlaceholderRoundTrip(unittest.TestCase):
	def roundtrip(self, steps, data):
		rows = [raw_step(s["title"], s["instruction"]) for s in steps]
		form, token_map = edit.build_editor_steps(rows, data)
		decoder = edit.PlaceholderDecoder(token_map, [])
		saved = edit.normalise_steps(form, decoder)
		self.assertEqual([], decoder.warnings)
		for original, new in zip(rows, saved, strict=True):
			self.assertEqual(tokens_of(original["instruction"]), tokens_of(new["instruction"]))
			self.assertEqual(rb.html_to_text(original["instruction"]), rb.html_to_text(new["instruction"]))
			self.assertEqual(original["title"], new["title"])
		return form, token_map, saved

	def test_tiramisu_small_round_trips_with_readable_labels(self):
		form, token_map, _saved = self.roundtrip(seed.TIRAMISU_STEPS, tiramisu_data())
		brew = form[0]["text"]
		self.assertIn("[Coffee beans]", brew)
		self.assertIn(f"[Coffee beans {edit.TIMES}5]", brew)
		self.assertNotIn("{{", brew)
		fill = next(s for s in form if s["title"] == "Fill each jar")["text"]
		self.assertIn("[Savoiardi each]", fill)
		self.assertIn(f"[Coffee beans {edit.TIMES}2.7 each]", fill)
		self.assertIn("[Cheesecake Mix + Coffee beans each]", fill)
		self.assertEqual("{{item:Coffee beans|grams|x5}}", token_map[f"[Coffee beans {edit.TIMES}5]"])
		self.assertEqual(
			"{{item:Cheesecake Mix+Coffee beans*0.9|grams|each}}",
			token_map["[Cheesecake Mix + Coffee beans each]"],
		)

	def test_mango_weighted_sums_round_trip_and_labels_never_collide(self):
		form, token_map, _saved = self.roundtrip(seed.MANGO_STEPS, mango_data())
		fill = next(s for s in form if s["title"] == "Fill each jar")["text"]
		# the whole-jar mango and the per-layer half are different tokens with one label
		self.assertIn("[Mango mix + Cheesecake Mix each]", fill)
		self.assertIn("[Mango mix + Cheesecake Mix each #2]", fill)
		self.assertEqual(len(set(token_map.values())), len(token_map))
		self.assertEqual(
			"{{item:Mango mix*0.5+Cheesecake Mix*-0.136364|grams|each}}",
			token_map["[Mango mix + Cheesecake Mix each #2]"],
		)

	def test_an_unchanged_save_keeps_the_arabic_text(self):
		_form, _map, saved = self.roundtrip(seed.TIRAMISU_STEPS, tiramisu_data())
		fill = next(s for s in saved if s["title"] == "Fill each jar")["instruction"]
		self.assertIn("سافوياردي", fill)
		self.assertIn("كاكاو", fill)
		self.assertIn("<br>\n", fill)

	def test_a_token_that_does_not_resolve_stays_raw(self):
		data = tiramisu_data()
		form, token_map = edit.build_editor_steps(
			[raw_step("Odd", "Add {{item:Unobtainium|grams}} now")], data
		)
		self.assertEqual("Add {{item:Unobtainium|grams}} now", form[0]["text"])
		self.assertEqual({}, token_map)

	def test_the_same_token_twice_shares_one_label(self):
		form, token_map = edit.build_editor_steps(
			[raw_step("A", "{{item:Savoiardi|grams}} and {{item:Savoiardi|grams}}")], tiramisu_data()
		)
		self.assertEqual("[Savoiardi] and [Savoiardi]", form[0]["text"])
		self.assertEqual(1, len(token_map))

	def test_a_literal_bracket_in_the_text_is_not_taken_for_a_label(self):
		form, token_map = edit.build_editor_steps(
			[raw_step("A", "[Savoiardi] is a word. {{item:Savoiardi|grams}}")], tiramisu_data()
		)
		self.assertIn("[Savoiardi #2]", form[0]["text"])
		decoder = edit.PlaceholderDecoder(token_map, [])
		self.assertEqual(
			"[Savoiardi] is a word. {{item:Savoiardi|grams}}",
			decoder.decode(form[0]["text"].replace("[Savoiardi #2]", "[Savoiardi #2]")),
		)

	def test_format_variants_get_a_suffix(self):
		data = tiramisu_data()
		form, _map = edit.build_editor_steps(
			[
				raw_step(
					"A",
					"{{item:Savoiardi|name}} {{item:Savoiardi|uom}} {{item:Savoiardi|qty}} {{item:Savoiardi}}",
				)
			],
			data,
		)
		self.assertEqual(
			"[Savoiardi (name)] [Savoiardi (uom)] [Savoiardi (qty)] [Savoiardi (full)]", form[0]["text"]
		)


class TestUnknownPlaceholders(unittest.TestCase):
	INGREDIENTS = [
		{"name": "Coffee beans", "item_code": "Coffee beans", "unit": "g"},
		{"name": "Eggs", "item_code": "EGG-1", "unit": "piece"},
	]

	def decode(self, text, token_map=None):
		decoder = edit.PlaceholderDecoder(token_map, self.INGREDIENTS)
		return decoder.decode(text), decoder.warnings

	def test_a_known_ingredient_becomes_a_grams_token(self):
		self.assertEqual(("Use {{item:Coffee beans|grams}}", []), self.decode("Use [coffee beans]"))

	def test_each_and_multiplier_in_any_spelling(self):
		for text in (
			"[Coffee beans each]",
			"[Coffee beans x3 each]",
			f"[Coffee beans {edit.TIMES}3 each]",
			"[Coffee beans *3]",
		):
			out, warnings = self.decode(text)
			self.assertEqual([], warnings, text)
			self.assertTrue(out.startswith("{{item:Coffee beans|grams"), out)
		self.assertEqual(("{{item:Coffee beans|grams|x3|each}}", []), self.decode("[Coffee beans x3 each]"))
		self.assertEqual(
			("{{item:Coffee beans|grams|x2.5}}", []), self.decode(f"[Coffee beans {edit.TIMES}2.5]")
		)

	def test_a_non_mass_ingredient_uses_qty_and_its_item_code(self):
		self.assertEqual(("{{item:EGG-1|qty|x2}}", []), self.decode("[Eggs x2]"))

	def test_anything_else_stays_literal_and_is_warned_about(self):
		out, warnings = self.decode("Add [Mystery] and [Eggs] then [optional]")
		self.assertEqual("Add [Mystery] and {{item:EGG-1|qty}} then [optional]", out)
		self.assertEqual(
			[
				"Unknown placeholder [Mystery] was left as text",
				"Unknown placeholder [optional] was left as text",
			],
			warnings,
		)

	def test_the_token_map_wins_and_must_hold_real_tokens(self):
		out, warnings = self.decode("[Brew]", {"[Brew]": "{{item:Coffee beans|grams|x5}}"})
		self.assertEqual(("{{item:Coffee beans|grams|x5}}", []), (out, warnings))
		out, warnings = self.decode("[Brew]", {"[Brew]": "<script>alert(1)</script>"})
		self.assertEqual("[Brew]", out)
		self.assertEqual(2, len(warnings))

	def test_raw_tokens_the_author_typed_are_left_alone(self):
		out, warnings = self.decode("Use {{item:Coffee beans|grams|x5}} and [Eggs]")
		self.assertEqual("Use {{item:Coffee beans|grams|x5}} and {{item:EGG-1|qty}}", out)
		self.assertEqual([], warnings)

	def test_a_zero_multiplier_is_not_a_placeholder(self):
		out, warnings = self.decode("[Coffee beans x0]")
		self.assertEqual("[Coffee beans x0]", out)
		self.assertEqual(1, len(warnings))


class TestStorageText(unittest.TestCase):
	def test_line_breaks_become_br_and_read_back_as_one_break(self):
		stored = edit.to_storage("Line one\nLine two")
		self.assertEqual("Line one<br>\nLine two", stored)
		self.assertEqual("Line one\nLine two", rb.html_to_text(stored))

	def test_angle_brackets_and_entities_survive(self):
		text = "Keep < 5 C and > 2 C. Salt &amp; pepper & more"
		self.assertEqual(text, rb.html_to_text(edit.to_storage(text)))

	def test_tokens_are_copied_untouched(self):
		text = "Mix {{item:Salt & Pepper|grams}} in\n{{item:A+B*-0.5|qty|x2|each}}"
		stored = edit.to_storage(text)
		self.assertIn("{{item:Salt & Pepper|grams}}", stored)
		self.assertEqual(text, rb.html_to_text(stored))


class TestValidation(unittest.TestCase):
	def setUp(self):
		patcher = patch.object(edit, "frappe", fake_frappe())
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_layers_need_a_name_and_a_positive_finite_number(self):
		self.assertEqual(
			[{"layer": "Cream", "grams": 10.5, "note": ""}],
			edit.normalise_layers([{"layer": " Cream ", "grams": "10.5"}]),
		)
		for bad in (
			[{"layer": "", "grams": 5}],
			[{"layer": "A", "grams": 0}],
			[{"layer": "A", "grams": -1}],
			[{"layer": "A", "grams": "x"}],
			[{"layer": "A", "grams": float("nan")}],
			[{"layer": "A", "grams": True}],
			[{"layer": "A", "grams": None}],
			"nope",
		):
			with self.assertRaises(Invalid, msg=str(bad)):
				edit.normalise_layers(bad)

	def test_ingredients_need_a_name_amount_and_unit(self):
		good = edit.normalise_ingredients(
			[{"name": "Flour", "amount": 1.5, "unit": "Kg", "item_code": "FLOUR"}]
		)
		self.assertEqual(
			("Flour", 1.5, "Kg", "FLOUR"),
			(good[0]["name"], good[0]["amount"], good[0]["unit"], good[0]["item_code"]),
		)
		for bad in (
			{"name": "", "amount": 1, "unit": "g"},
			{"name": "A", "amount": 0, "unit": "g"},
			{"name": "A", "amount": 1, "unit": ""},
		):
			with self.assertRaises(Invalid):
				edit.normalise_ingredients([bad])

	def test_step_fields_are_checked(self):
		decoder = edit.PlaceholderDecoder({}, [])
		with self.assertRaises(Invalid):
			edit.normalise_steps([step(title="")], decoder)
		with self.assertRaises(Invalid):
			edit.normalise_steps([step(scaling_mode="Sometimes")], decoder)
		with self.assertRaises(Invalid):
			edit.normalise_steps([step(capture_type="Smell")], decoder)
		with self.assertRaises(Invalid):
			edit.normalise_steps([step(duration_mins=-1)], decoder)
		with self.assertRaises(Invalid):
			edit.normalise_steps([step(capture_type="Number", capture_min=9, capture_max=3)], decoder)
		(ok,) = edit.normalise_steps(
			[step(capture_type="Temperature", capture_min="40", capture_max=55)], decoder
		)
		self.assertEqual(
			("Temperature", 40.0, 55.0, 1),
			(ok["capture_type"], ok["capture_min"], ok["capture_max"], ok["requires_confirmation"]),
		)

	def test_the_payload_may_be_json_text(self):
		self.assertEqual({"a": 1}, edit.parse_payload('{"a": 1}'))
		for bad in ("{oops", "[1]", 5):
			with self.assertRaises(Invalid):
				edit.parse_payload(bad)


# ── Access ──────────────────────────────────────────────────────────────


class TestAccess(unittest.TestCase):
	def test_the_gate_is_manufacturing_roles(self):
		ff = fake_frappe()
		with patch.object(rb, "frappe", ff):
			ff.get_roles.return_value = ["Production Operator"]
			with self.assertRaises(Forbidden):
				rb._ensure_recipe_edit_access()
			self.assertFalse(rb.can_edit())
			ff.get_roles.return_value = ["JARZ Manager"]
			rb._ensure_recipe_edit_access()
			self.assertTrue(rb.can_edit())

	def test_every_write_endpoint_checks_permission_before_touching_anything(self):
		ff = fake_frappe()
		ff.get_roles.return_value = ["Production Operator"]
		with patch.object(rb, "frappe", ff), patch.object(edit, "frappe", ff):
			for call in (
				lambda: edit.get_recipe_for_edit(sop="SOP-1"),
				lambda: edit.save_recipe({"title": "x"}),
				lambda: edit.set_recipe_stage("SOP-1", "Approved"),
				lambda: edit.save_jar_layers("Tiramisu", []),
			):
				with self.assertRaises(Forbidden):
					call()
		ff.db.savepoint.assert_not_called()
		ff.get_doc.assert_not_called()

	def test_a_failure_rolls_back_to_the_savepoint(self):
		ff = fake_frappe()
		with patch.object(rb, "_ensure_recipe_edit_access"), patch.object(edit, "frappe", ff):
			with self.assertRaises(Invalid):
				edit.save_recipe({"base": "SOP-1", "mode": "sideways"})
		ff.db.savepoint.assert_called_once_with("recipe_save")
		ff.db.rollback.assert_called_once_with(save_point="recipe_save")


# ── save_recipe ─────────────────────────────────────────────────────────


class SaveHarness(unittest.TestCase):
	"""Patches the reads ``_save_recipe`` makes and captures the documents it writes."""

	def setUp(self):
		self.ff = fake_frappe()
		self.new_docs = []
		self.docs = {}

		def _new_doc(_doctype):
			doc = FakeDoc()
			self.new_docs.append(doc)
			return doc

		self.ff.new_doc.side_effect = _new_doc
		self.ff.get_doc.side_effect = lambda _dt, name: self.docs[name]
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(patch.object(edit, "frappe", self.ff))
		self.stack.enter_context(patch.object(rb, "frappe", self.ff))
		self.stack.enter_context(patch.object(rb, "_ensure_recipe_edit_access"))
		self.stack.enter_context(patch.object(rb, "_item_group", return_value=None))
		self.stack.enter_context(patch.object(edit, "_today", return_value="2026-11-28"))
		self.stack.enter_context(patch.object(rb, "_sop_payload", side_effect=lambda name: {"name": name}))
		self.used = set()
		self.stack.enter_context(
			patch.object(rb, "is_used_in_production", side_effect=lambda n: n in self.used)
		)
		self.bom = None
		self.stack.enter_context(patch.object(rb, "_resolve_bom", side_effect=lambda *_a: self.bom))
		self.rows = []
		self.stack.enter_context(
			patch.object(rb, "_fetch_rows", side_effect=lambda *_a, **_k: list(self.rows))
		)
		self.data = data_for()
		self.stack.enter_context(patch.object(rb, "recipe_data", side_effect=lambda _doc: self.data))

	def sop(self, name="SOP-1", **fields):
		base = {
			"item_code": "Cake",
			"recipe_title": "Cake",
			"stage": "Approved",
			"version": 1,
			"is_active": 1,
			"track": "Standard line",
			"bom": None,
			"notes": "",
			"equipment": "",
			"prep_time_mins": 0,
		}
		base.update(fields)
		doc = FakeDoc(name=name, **base)
		self.docs[name] = doc
		return doc


class TestSaveRecipeModes(SaveHarness):
	def test_in_place_is_refused_on_a_version_used_in_production(self):
		self.sop()
		self.used.add("SOP-1")
		with self.assertRaisesRegex(Invalid, "used in production"):
			edit._save_recipe({"base": "SOP-1", "mode": "in_place", "steps": [step()]})
		self.assertFalse(self.docs["SOP-1"].saved)

	def test_in_place_updates_an_unused_version(self):
		doc = self.sop(stage="Trial", is_active=0)
		out = edit._save_recipe(
			{
				"base": "SOP-1",
				"mode": "in_place",
				"title": "Cake v2",
				"change_summary": "Less sugar",
				"steps": [step(text="Mix\nbake")],
				"layers": [{"layer": "Sponge", "grams": 40}],
				"ingredients": [{"name": "Flour", "amount": 1, "unit": "Kg"}],
			}
		)
		self.assertTrue(doc.saved)
		self.assertEqual(
			("Cake v2", "Trial", 0, "Less sugar"),
			(doc.recipe_title, doc.stage, doc.is_active, doc.change_summary),
		)
		self.assertEqual("Mix<br>\nbake", doc.steps[0]["instruction"])
		self.assertEqual([{"layer": "Sponge", "grams": 40.0, "note": None}], doc.layers)
		self.assertEqual({"recipe": {"name": "SOP-1"}, "warnings": []}, out)

	def test_in_place_keeps_a_previous_version_off_the_board(self):
		doc = self.sop(is_active=0)
		edit._save_recipe({"base": "SOP-1", "mode": "in_place", "steps": [step()]})
		self.assertEqual(0, doc.is_active)

	def test_in_place_keeps_the_current_version_active(self):
		doc = self.sop()
		edit._save_recipe({"base": "SOP-1", "mode": "in_place", "steps": [step()]})
		self.assertEqual(1, doc.is_active)

	def test_in_place_without_steps_is_never_active(self):
		doc = self.sop()
		edit._save_recipe({"base": "SOP-1", "mode": "in_place", "steps": []})
		self.assertEqual(0, doc.is_active)

	def test_new_version_needs_a_change_summary(self):
		self.sop()
		with self.assertRaisesRegex(Invalid, "what changed"):
			edit._save_recipe({"base": "SOP-1", "mode": "new_version", "steps": [step()]})
		self.assertEqual([], self.new_docs)

	def test_new_version_number_is_one_above_the_highest_of_the_recipe(self):
		self.sop()
		self.rows = [{"version": 1}, {"version": 5}, {"version": 3}]
		edit._save_recipe({"base": "SOP-1", "mode": "new_version", "change_summary": "x", "steps": [step()]})
		(new,) = self.new_docs
		self.assertEqual(6, new.version)
		self.assertTrue(new.inserted)

	def test_new_version_stage_defaults_to_trial_and_is_not_active(self):
		self.sop()
		edit._save_recipe({"base": "SOP-1", "mode": "new_version", "change_summary": "x", "steps": [step()]})
		(new,) = self.new_docs
		self.assertEqual(("Trial", 0, None), (new.stage, new.is_active, new.approved_on))

	def test_approved_new_version_with_steps_becomes_the_active_one(self):
		self.sop()
		edit._save_recipe(
			{
				"base": "SOP-1",
				"mode": "new_version",
				"stage": "Approved",
				"change_summary": "x",
				"steps": [step()],
			}
		)
		(new,) = self.new_docs
		self.assertEqual(
			("Approved", 1, "2026-11-28", "Cake"), (new.stage, new.is_active, new.approved_on, new.item_code)
		)

	def test_new_version_copies_what_the_payload_leaves_out(self):
		base = self.sop(track="Cookie jar line", equipment="Bowl")
		base.steps = [raw_step("Bake", "Bake it")]
		base.layers = [{"layer": "Cream", "grams": 9.0, "note": None, "idx": 1}]
		edit._save_recipe({"base": "SOP-1", "mode": "new_version", "change_summary": "x"})
		(new,) = self.new_docs
		self.assertEqual(("Cookie jar line", "Bowl"), (new.track, new.equipment))
		self.assertEqual("Bake", new.steps[0]["title"])
		self.assertEqual("Cream", new.layers[0]["layer"])

	def test_an_unknown_mode_or_stage_is_refused(self):
		self.sop()
		with self.assertRaises(Invalid):
			edit._save_recipe({"base": "SOP-1", "mode": "overwrite"})
		with self.assertRaises(Invalid):
			edit._save_recipe({"base": "SOP-1", "mode": "in_place", "stage": "Retired"})

	def test_unknown_placeholders_come_back_as_warnings(self):
		self.sop(stage="Trial", is_active=0)
		out = edit._save_recipe(
			{"base": "SOP-1", "mode": "in_place", "steps": [step(text="Add [Mystery]")], "ingredients": []}
		)
		self.assertEqual(["Unknown placeholder [Mystery] was left as text"], out["warnings"])
		self.assertEqual("Add [Mystery]", self.docs["SOP-1"].steps[0]["instruction"])

	def test_approving_own_amounts_on_a_bom_item_is_refused(self):
		self.sop(stage="Trial", is_active=0)
		self.bom = {"name": "BOM-1", "quantity": 1}
		self.data = data_for(rows=[{"ingredient": "Flour", "qty": 1, "uom": "Kg"}])
		with self.assertRaisesRegex(Invalid, "production uses the BOM"):
			edit._save_recipe(
				{
					"base": "SOP-1",
					"mode": "in_place",
					"stage": "Approved",
					"steps": [step()],
					"ingredients": [{"name": "Flour", "amount": 1, "unit": "Kg"}],
				}
			)

	def test_a_bom_recipe_ignores_the_payload_ingredients(self):
		self.sop()
		self.bom = {"name": "BOM-1", "quantity": 1}
		self.data = data_for(
			use_bom=True,
			ingredients=[
				{"key": "flour", "name": "Flour", "amount": 1500, "unit": "g", "section": None, "note": None}
			],
			order=["FLOUR"],
		)
		edit._save_recipe(
			{
				"base": "SOP-1",
				"mode": "new_version",
				"change_summary": "x",
				"steps": [step(text="Add [Flour]")],
				"ingredients": [{"name": "Sugar", "amount": 9, "unit": "g"}],
			}
		)
		(new,) = self.new_docs
		self.assertEqual([], new.ingredients)
		self.assertEqual("Add {{item:FLOUR|grams}}", new.steps[0]["instruction"])


class TestSaveRecipeCreate(SaveHarness):
	def test_a_new_recipe_needs_a_title(self):
		with self.assertRaisesRegex(Invalid, "Title is required"):
			edit._save_recipe({"steps": [step()]})

	def test_a_new_recipe_is_version_one_trial_with_its_own_ingredients(self):
		self.ff.db.exists.side_effect = lambda *a, **k: False
		edit._save_recipe(
			{
				"title": "Carrot Cake",
				"yield_qty": 2,
				"yield_uom": "trays",
				"ingredients": [
					{"name": "Carrots", "amount": 1.8, "unit": "Kg", "section": "Core", "note": "grated"}
				],
				"steps": [step(text="Grate [Carrots]")],
			}
		)
		(new,) = self.new_docs
		self.assertEqual(
			("Carrot Cake", 1, "Trial", 0, None),
			(new.recipe_title, new.version, new.stage, new.is_active, new.item_code),
		)
		self.assertEqual("Grate {{item:Carrots|grams}}", new.steps[0]["instruction"])
		self.assertEqual(
			[
				{
					"ingredient": "Carrots",
					"item_code": None,
					"qty": 1.8,
					"uom": "Kg",
					"section": "Core",
					"note": "grated",
				}
			],
			new.ingredients,
		)
		self.assertEqual(2.0, new.yield_qty)

	def test_a_title_already_in_use_is_refused_rather_than_merged(self):
		with self.assertRaisesRegex(Invalid, "already exists"):
			edit._save_recipe({"title": "Carrot Cake", "steps": [step()]})

	def test_the_first_version_of_a_bom_item_follows_the_bom(self):
		self.ff.db.exists.side_effect = lambda doctype, *a, **k: doctype == "Item"
		self.ff.db.get_value.return_value = "Fudge Cake"
		self.bom = {"name": "BOM-9", "quantity": 1}
		self.data = data_for(use_bom=True, ingredients=[], order=[])
		edit._save_recipe(
			{
				"item_code": "Fudge Cake",
				"steps": [step()],
				"ingredients": [{"name": "Ignored", "amount": 1, "unit": "g"}],
			}
		)
		(new,) = self.new_docs
		self.assertEqual(
			("Fudge Cake", 1, "Approved", "BOM-9", 1),
			(new.item_code, new.version, new.stage, new.bom, new.is_active),
		)
		self.assertEqual([], new.ingredients)

	def test_an_item_that_already_has_versions_must_use_a_base(self):
		self.rows = [{"version": 1}]
		with self.assertRaisesRegex(Invalid, "already has recipe versions"):
			edit._save_recipe({"item_code": "Cake", "steps": [step()]})

	def test_base_null_means_create_whatever_the_mode_and_needs_no_summary(self):
		self.ff.db.exists.side_effect = lambda *a, **k: False
		for mode in ("new_version", "in_place", None):
			edit._save_recipe({"base": None, "mode": mode, "title": f"Mango jar ({mode})", "steps": [step()]})
			self.assertEqual(1, self.new_docs[-1].version)

	def test_a_missing_item_is_refused(self):
		self.ff.db.exists.side_effect = lambda *a, **k: False
		with self.assertRaisesRegex(Invalid, "does not exist"):
			edit._save_recipe({"item_code": "Nope"})

	def test_a_new_recipe_stores_the_category_label_it_was_sent(self):
		self.ff.db.exists.side_effect = lambda *a, **k: False
		edit._save_recipe({"title": "Carrot Cake", "category": "bases", "steps": [step()]})
		self.assertEqual("Cakes & biscuits", self.new_docs[-1].category)
		edit._save_recipe({"title": "Carrot Cake 2", "category": "jars", "steps": [step()]})
		self.assertEqual("Jars", self.new_docs[-1].category)
		with self.assertRaises(Invalid):
			edit._save_recipe({"title": "Carrot Cake 3", "category": "desserts"})


# ── set_recipe_stage ────────────────────────────────────────────────────


class TestSetRecipeStage(SaveHarness):
	def setUp(self):
		super().setUp()
		self.stack.enter_context(patch.object(rb, "sibling_rows", side_effect=lambda _d: list(self.rows)))

	def test_approving_activates_and_names_the_version_it_replaces(self):
		doc = self.sop(stage="Trial", is_active=0)
		doc.steps = [raw_step("Mix", "Mix")]
		self.ff.get_all.return_value = ["SOP-OLD"]
		out = edit._set_recipe_stage("SOP-1", "Approved", False)
		self.assertEqual(("Approved", 1, "2026-11-28"), (doc.stage, doc.is_active, doc.approved_on))
		self.assertEqual("SOP-OLD", out["replaced"])
		self.assertTrue(doc.saved)

	def test_approving_with_no_steps_does_not_activate(self):
		doc = self.sop(stage="Trial", is_active=0)
		out = edit._set_recipe_stage("SOP-1", "Approved", False)
		self.assertEqual(0, doc.is_active)
		self.assertIsNone(out["replaced"])

	def test_approving_without_an_item_never_replaces_anything(self):
		doc = self.sop(item_code=None, recipe_title="Carrot", stage="Trial", is_active=0)
		doc.steps = [raw_step("Mix", "Mix")]
		out = edit._set_recipe_stage("SOP-1", "Approved", False)
		self.assertEqual((0, None), (doc.is_active, out["replaced"]))

	def test_own_amounts_on_a_bom_item_need_use_bom(self):
		doc = self.sop(stage="Trial", is_active=0)
		doc.ingredients = [{"ingredient": "Flour", "qty": 1, "uom": "Kg"}]
		doc.steps = [raw_step("Mix", "Mix")]
		self.bom = {"name": "BOM-1", "quantity": 1}
		with self.assertRaisesRegex(Invalid, "production uses the BOM"):
			edit._set_recipe_stage("SOP-1", "Approved", False)
		self.assertFalse(doc.saved)
		edit._set_recipe_stage("SOP-1", "Approved", True)
		self.assertEqual([], doc.ingredients)
		self.assertEqual("Approved", doc.stage)

	def test_the_current_version_of_a_bom_item_cannot_be_retired(self):
		self.sop()
		self.bom = {"name": "BOM-1", "quantity": 1}
		self.rows = [
			{"name": "SOP-1", "version": 1, "stage": "Approved", "is_active": 1, "item_code": "Cake"}
		]
		with self.assertRaisesRegex(Invalid, "Approve another version first"):
			edit._set_recipe_stage("SOP-1", "Retired", False)

	def test_a_previous_version_or_a_recipe_without_a_bom_can_be_retired(self):
		doc = self.sop(is_active=0)
		self.bom = {"name": "BOM-1", "quantity": 1}
		self.rows = [
			{"name": "SOP-1", "version": 1, "stage": "Approved", "is_active": 0, "item_code": "Cake"},
			{"name": "SOP-2", "version": 2, "stage": "Approved", "is_active": 1, "item_code": "Cake"},
		]
		edit._set_recipe_stage("SOP-1", "Retired", False)
		self.assertEqual(("Retired", 0), (doc.stage, doc.is_active))
		self.bom = None
		other = self.sop("SOP-3")
		edit._set_recipe_stage("SOP-3", "Retired", False)
		self.assertEqual("Retired", other.stage)

	def test_back_to_trial_only_when_not_used_in_production(self):
		self.sop()
		self.used.add("SOP-1")
		with self.assertRaisesRegex(Invalid, "used in production"):
			edit._set_recipe_stage("SOP-1", "Trial", False)
		self.used.clear()
		doc = self.docs["SOP-1"]
		edit._set_recipe_stage("SOP-1", "Trial", False)
		self.assertEqual(("Trial", 0), (doc.stage, doc.is_active))

	def test_a_used_version_can_still_be_retired(self):
		doc = self.sop(is_active=0)
		self.used.add("SOP-1")
		edit._set_recipe_stage("SOP-1", "Retired", False)
		self.assertEqual("Retired", doc.stage)

	def test_an_unknown_stage_is_refused(self):
		self.sop()
		with self.assertRaises(Invalid):
			edit._set_recipe_stage("SOP-1", "Draft", False)


# ── save_jar_layers ─────────────────────────────────────────────────────


def jar(size, headline, family="Tiramisu"):
	return {
		"key": f"{family} {size}",
		"title": f"{family} {size}",
		"item_code": f"{family} {size}",
		"category": "jars",
		"family": family,
		"size": size,
		"headline": headline,
		"versions": [],
	}


LAYERS = [
	{"layer": "Savoiardi", "grams": {"Small": 18.7, "Medium": 28, "Large": 40}},
	{"layer": "Cream", "note": "from the bowl", "grams": {"Small": 46.7, "Medium": 70, "Large": None}},
]


class TestSaveJarLayers(SaveHarness):
	def setUp(self):
		super().setUp()
		self.stack.enter_context(patch.object(rb, "sibling_rows", side_effect=lambda _d: list(self.rows)))
		self.stack.enter_context(patch.object(edit, "_resolve_default_bom", return_value="BOM-X"))
		self.family = self.stack.enter_context(
			patch.object(rb, "get_jar_family", side_effect=lambda f: {"family": f, "sizes": []})
		)
		self.recipes = [jar("Small", "SOP-S"), jar("Medium", "SOP-M"), jar("Large", None)]
		self.stack.enter_context(patch.object(rb, "_catalog", side_effect=lambda: {"recipes": self.recipes}))

	def jar_sops(self):
		for name, version in (("SOP-S", 3), ("SOP-M", 4)):
			doc = self.sop(
				name, item_code=f"Tiramisu {'Small' if name == 'SOP-S' else 'Medium'}", version=version
			)
			doc.steps = [raw_step("Fill each jar", "Fill")]
			doc.layers = [{"layer": "Old", "grams": 1.0, "note": None, "idx": 1}]

	def test_in_place_for_an_unused_size_new_version_for_a_used_one_first_version_for_none(self):
		self.jar_sops()
		self.used.add("SOP-M")
		self.rows = [{"version": 4}, {"version": 2}]
		out = edit._save_jar_layers("Tiramisu", LAYERS, "Weights measured on the scale")

		small = self.docs["SOP-S"]
		self.assertTrue(small.saved)
		self.assertEqual(
			[("Savoiardi", 18.7), ("Cream", 46.7)], [(x["layer"], x["grams"]) for x in small.layers]
		)
		self.assertEqual("from the bowl", small.layers[1]["note"])

		medium_copy, large_first = self.new_docs
		self.assertFalse(self.docs["SOP-M"].saved)
		self.assertEqual(
			(5, "Approved", 1, "Weights measured on the scale"),
			(medium_copy.version, medium_copy.stage, medium_copy.is_active, medium_copy.change_summary),
		)
		self.assertEqual("Fill each jar", medium_copy.steps[0]["title"])
		self.assertEqual(
			[("Savoiardi", 28.0), ("Cream", 70.0)], [(x["layer"], x["grams"]) for x in medium_copy.layers]
		)

		self.assertEqual(
			("Tiramisu Large", 1, "Approved", 0, "BOM-X"),
			(
				large_first.item_code,
				large_first.version,
				large_first.stage,
				large_first.is_active,
				large_first.bom,
			),
		)
		self.assertEqual([("Savoiardi", 40.0)], [(x["layer"], x["grams"]) for x in large_first.layers])
		self.assertEqual([], large_first.steps)

		self.assertEqual([{"size": "Small", "sop": "SOP-S"}], out["updated"])
		self.assertEqual(
			[("Medium", "SOP-NEW"), ("Large", "SOP-NEW")], [(c["size"], c["sop"]) for c in out["created"]]
		)

	def test_a_used_size_needs_a_change_summary_and_nothing_is_written(self):
		self.jar_sops()
		self.used.add("SOP-M")
		with self.assertRaisesRegex(Invalid, "change summary"):
			edit._save_jar_layers("Tiramisu", LAYERS, "")
		self.assertFalse(self.docs["SOP-S"].saved)
		self.assertEqual([], self.new_docs)

	def test_a_layer_without_a_value_for_a_size_is_not_written_for_it(self):
		self.jar_sops()
		edit._save_jar_layers("Tiramisu", [LAYERS[0], {"layer": "Cream", "grams": {"Small": 46.7}}], "")
		self.assertEqual(2, len(self.docs["SOP-S"].layers))
		self.assertEqual(1, len(self.docs["SOP-M"].layers))

	def test_a_size_with_no_weights_is_left_alone(self):
		self.jar_sops()
		edit._save_jar_layers("Tiramisu", [{"layer": "Cream", "grams": {"Small": 46.7}}], "")
		self.assertFalse(self.docs["SOP-M"].saved)
		self.assertEqual([], self.new_docs)

	def test_unknown_family_size_or_values_are_refused(self):
		self.jar_sops()
		with self.assertRaises(Missing):
			edit._save_jar_layers("Nope", LAYERS, "")
		with self.assertRaisesRegex(Invalid, "not a size"):
			edit._save_jar_layers("Tiramisu", [{"layer": "A", "grams": {"Huge": 5}}], "")
		with self.assertRaises(Invalid):
			edit._save_jar_layers("Tiramisu", [{"layer": "A", "grams": {"Small": -5}}], "")
		with self.assertRaisesRegex(Invalid, "at least one size"):
			edit._save_jar_layers("Tiramisu", [{"layer": "A", "grams": {"Small": None}}], "")
		self.recipes = [jar("Small", "SOP-S")]
		with self.assertRaisesRegex(Invalid, "no Medium jar"):
			edit._save_jar_layers("Tiramisu", LAYERS, "")

	def test_a_failure_halfway_rolls_everything_back(self):
		self.jar_sops()
		self.used.add("SOP-M")

		def _boom(_doctype):
			raise RuntimeError("database went away")

		self.ff.new_doc.side_effect = _boom
		with patch.object(rb, "_ensure_recipe_edit_access"), self.assertRaises(RuntimeError):
			edit.save_jar_layers("Tiramisu", LAYERS, "x")
		self.ff.db.savepoint.assert_called_once_with("jar_layers")
		self.ff.db.rollback.assert_called_once_with(save_point="jar_layers")

	def test_json_text_is_accepted(self):
		self.jar_sops()
		edit._save_jar_layers("Tiramisu", '[{"layer": "Cream", "grams": {"Small": 5}}]', "")
		self.assertEqual("Cream", self.docs["SOP-S"].layers[0]["layer"])


# ── Read side: assembly, edit form, status rule ─────────────────────────


class TestAssembly(unittest.TestCase):
	def layers(self, *pairs):
		return [{"layer": n, "grams": g, "note": None} for n, g in pairs]

	def test_aligned_sizes_fill_rows_by_position(self):
		out = rb.build_assembly(
			{
				"Small": self.layers(("Savoiardi", 18.7), ("Cream", 46.7)),
				"Medium": self.layers(("Savoiardi", 28), ("Cream", 70)),
			}
		)
		self.assertTrue(out["aligned"])
		self.assertEqual({"Small": 18.7, "Medium": 28}, out["rows"][0]["grams"])
		self.assertEqual({"Small": 65.4, "Medium": 98.0}, out["totals"])
		self.assertEqual(["Small", "Medium"], list(out["by_size"]))

	def test_different_names_or_order_are_not_aligned_and_have_no_rows(self):
		out = rb.build_assembly(
			{
				"Small": self.layers(("Savoiardi", 1), ("Cream", 2)),
				"Large": self.layers(("Cream", 2), ("Savoiardi", 1)),
			}
		)
		self.assertEqual((False, []), (out["aligned"], out["rows"]))
		self.assertEqual({"Small": 3.0, "Large": 3.0}, out["totals"])
		out = rb.build_assembly({"Small": self.layers(("A", 1)), "Medium": self.layers(("A", 1), ("B", 2))})
		self.assertFalse(out["aligned"])

	def test_sizes_without_layers_are_absent_and_no_layers_is_empty(self):
		out = rb.build_assembly({"Small": [], "Medium": self.layers(("A", 1))})
		self.assertEqual(["Medium"], list(out["by_size"]))
		self.assertEqual({"aligned": True, "rows": [], "by_size": {}, "totals": {}}, rb.build_assembly({}))

	def test_case_does_not_break_alignment(self):
		out = rb.build_assembly({"Small": self.layers(("Cream", 1)), "Medium": self.layers(("cream", 2))})
		self.assertTrue(out["aligned"])


class TestEditForm(SaveHarness):
	def test_the_form_has_placeholders_a_token_map_and_the_flags(self):
		doc = self.sop(item_code="Tiramisu Small", recipe_title="Tiramisu Small", version=3)
		doc.steps = [
			raw_step(
				"Brew the coffee",
				"Brew {{item:Coffee beans|grams}} with {{item:Coffee beans|grams|x5}}",
				image=None,
			)
		]
		doc.layers = [{"layer": "Savoiardi", "grams": 18.7, "note": None, "idx": 1}]
		self.data = {
			**data_for(use_bom=True),
			**tiramisu_data(),
			"order": ["Coffee beans"],
			"ingredients": [
				{
					"key": "coffee beans",
					"name": "Coffee beans",
					"amount": 4.0,
					"unit": "g",
					"section": None,
					"note": None,
				}
			],
		}
		self.used.add("SOP-1")
		with patch.object(rb, "sibling_rows", return_value=[{"version": 3}, {"version": 2}]):
			form = edit.build_edit_form(doc)
		self.assertEqual("Brew [Coffee beans] with [Coffee beans ×5]", form["steps"][0]["text"])
		self.assertEqual(
			{
				"[Coffee beans]": "{{item:Coffee beans|grams}}",
				"[Coffee beans ×5]": "{{item:Coffee beans|grams|x5}}",
			},
			form["token_map"],
		)
		self.assertEqual(
			(3, 4, False, True, "bom"),
			(
				form["version"],
				form["next_version"],
				form["editable_in_place"],
				form["used_in_production"],
				form["ingredients_source"],
			),
		)
		self.assertEqual([{"layer": "Savoiardi", "grams": 18.7, "note": None}], form["layers"])
		self.assertEqual("Coffee beans", form["ingredients"][0]["item_code"])
		self.assertEqual("", form["change_summary"])

	def test_table_ingredients_come_back_as_stored(self):
		doc = self.sop(item_code=None, recipe_title="Carrot Cake", stage="Trial", is_active=0)
		rows = [
			{
				"ingredient": "Carrots",
				"qty": 1.8,
				"uom": "Kg",
				"section": "Core",
				"note": None,
				"item_code": None,
			}
		]
		self.data = data_for(rows=rows)
		with patch.object(rb, "sibling_rows", return_value=[]):
			form = edit.build_edit_form(doc)
		self.assertEqual("table", form["ingredients_source"])
		self.assertEqual(
			[
				{
					"name": "Carrots",
					"amount": 1.8,
					"unit": "Kg",
					"section": "Core",
					"note": None,
					"item_code": None,
				}
			],
			form["ingredients"],
		)


class TestVersionStatusRule(unittest.TestCase):
	def row(self, name, version, active=0, stage="Approved", item="Cake"):
		return {
			"name": name,
			"version": version,
			"stage": stage,
			"is_active": active,
			"item_code": item,
			"modified": f"2026-11-0{version} 10:00:00",
		}

	def test_the_active_version_is_current(self):
		self.assertEqual(
			{"A": "previous", "B": "current", "C": "previous"},
			rb.version_statuses([self.row("A", 1), self.row("B", 2, 1), self.row("C", 3)]),
		)

	def test_with_none_active_the_highest_approved_is_current_even_with_an_item(self):
		self.assertEqual(
			{"A": "previous", "B": "current", "T": "trial"},
			rb.version_statuses([self.row("A", 1), self.row("B", 2), self.row("T", 3, stage="Trial")]),
		)


class TestCategoryOverride(unittest.TestCase):
	def test_a_stored_category_wins_over_the_name(self):
		self.assertEqual(
			("fillings", None, None), rb.classify_with_stored("Tiramisu Small", None, "Mixes & fillings")
		)
		self.assertEqual(("jars", "Cookie", "Large"), rb.classify_with_stored("Cookie Large", None, "Jars"))
		self.assertEqual(("bases", None, None), rb.classify_with_stored("Cheesecake Mix", None, "bases"))
		self.assertEqual(rb.classify_recipe("Fudge Cake"), rb.classify_with_stored("Fudge Cake", None, ""))

	def test_stored_categories_use_the_newest_version_that_sets_one(self):
		rows = [
			{"name": "A", "item_code": "X", "version": 1, "category": "Jars", "modified": "2026-11-01"},
			{
				"name": "B",
				"item_code": "X",
				"version": 2,
				"category": "Mixes & fillings",
				"modified": "2026-11-02",
			},
			{"name": "C", "item_code": "X", "version": 3, "category": "", "modified": "2026-11-03"},
		]
		self.assertEqual({"X": "fillings"}, rb.stored_categories(rows))


# ── Controller rules (v3) ───────────────────────────────────────────────


class TestControllerV3(unittest.TestCase):
	def sop(self, **fields):
		from jarz_pos.tests.test_doctype_jarz_sop import load, make_sop

		module = load()
		return module, make_sop(module, **fields)

	def validate_rules(self, sop):
		with (
			patch("jarz_pos.doctype.jarz_sop.jarz_sop.frappe") as mock_frappe,
			patch("jarz_pos.doctype.jarz_sop.jarz_sop._", new=lambda m: m),
		):
			mock_frappe.db.exists.return_value = False
			sop._apply_recipe_book_rules()

	def test_an_approved_item_recipe_with_no_steps_is_forced_inactive(self):
		_module, sop = self.sop(stage="Approved", is_active=1, steps=[])
		self.validate_rules(sop)
		self.assertEqual(0, sop.is_active)

	def test_an_approved_item_recipe_with_steps_keeps_its_flag(self):
		_module, sop = self.sop(stage="Approved", is_active=1, steps=[{"title": "Mix"}])
		self.validate_rules(sop)
		self.assertEqual(1, sop.is_active)

	def used_sop(self, previous_rows, current_rows, table="layers"):
		module, sop = self.sop(has_value_changed=lambda field: True)
		setattr(sop, table, current_rows)
		sop.get_doc_before_save = lambda: SimpleNamespace(
			get=lambda name: previous_rows if name == table else []
		)
		return module, sop

	def guard(self, sop, used=True):
		with (
			patch("jarz_pos.doctype.jarz_sop.jarz_sop.frappe") as mock_frappe,
			patch("jarz_pos.doctype.jarz_sop.jarz_sop._", new=lambda m: m),
		):
			mock_frappe.db.exists.return_value = used
			mock_frappe.throw.side_effect = ValueError("frozen")
			sop._guard_steps_are_immutable_once_used()

	def test_a_layer_change_on_a_used_version_is_refused(self):
		before = [{"idx": 1, "layer": "Cream", "grams": 46.7, "note": None}]
		after = [{"idx": 1, "layer": "Cream", "grams": 47.0, "note": None}]
		_module, sop = self.used_sop(before, after)
		with self.assertRaises(ValueError):
			self.guard(sop)

	def test_an_ingredient_change_on_a_used_version_is_refused(self):
		before = [{"idx": 1, "ingredient": "Flour", "qty": 1, "uom": "Kg"}]
		after = [{"idx": 1, "ingredient": "Flour", "qty": 2, "uom": "Kg"}]
		_module, sop = self.used_sop(before, after, table="ingredients")
		with self.assertRaises(ValueError):
			self.guard(sop)

	def test_a_step_text_change_on_a_used_version_is_refused(self):
		before = [{"idx": 1, "title": "Mix", "instruction": "Sift {{item:Flour|grams}}.", "duration_mins": 5}]
		after = [{"idx": 1, "title": "Mix", "instruction": "Sift {{item:Flour|grams|x2}}.", "duration_mins": 5}]
		_module, sop = self.used_sop(before, after, table="steps")
		with self.assertRaises(ValueError):
			self.guard(sop)

	def test_a_step_change_on_an_unused_version_is_allowed(self):
		before = [{"idx": 1, "title": "Mix", "instruction": "a"}]
		after = [{"idx": 1, "title": "Mix well", "instruction": "a"}]
		_module, sop = self.used_sop(before, after, table="steps")
		self.guard(sop, used=False)

	def test_identical_rows_on_a_used_version_are_not_a_change(self):
		rows = [{"idx": 1, "layer": "Cream", "grams": 46.7, "note": None}]
		_module, sop = self.used_sop(rows, [dict(rows[0], note="")])
		self.guard(sop)  # saving a used version for a stage change must still work

	def test_an_unused_version_may_change_its_layers(self):
		before = [{"idx": 1, "layer": "Cream", "grams": 46.7}]
		_module, sop = self.used_sop(before, [{"idx": 1, "layer": "Cream", "grams": 50}])
		self.guard(sop, used=False)


# ── Layer seed ──────────────────────────────────────────────────────────


def evaluator(values):
	def _evaluate(payload):
		return values.get(payload)

	return _evaluate


class TestSeedJarLayers(unittest.TestCase):
	def text_of(self, steps, title="Fill each jar"):
		return next(s["instruction"] for s in steps if s["title"] == title)

	def test_tiramisu_layers_in_text_order_with_their_names(self):
		rows, notes = seed_layers.derive_layers(
			self.text_of(seed.TIRAMISU_STEPS),
			evaluator(
				{
					"Savoiardi|grams|each": 18.7,
					"Coffee beans|grams|x2.7|each": 10.8,
					"Cheesecake Mix+Coffee beans*0.9|grams|each": 46.74,
					"coco powder|grams|each": 1.3,
				}
			),
		)
		self.assertEqual(
			[("Savoiardi", 18.7), ("Coffee soak", 10.8), ("Cream", 46.7), ("Cocoa", 1.3)],
			[(r["layer"], r["grams"]) for r in rows],
		)
		self.assertEqual([], notes)

	def test_mango_splits_the_counted_layers_and_says_the_order_is_unspecified(self):
		rows, notes = seed_layers.derive_layers(
			self.text_of(seed.MANGO_STEPS),
			evaluator(
				{
					"Savoiardi|grams|each": 22.0,
					"Mango mix+Cheesecake Mix*-0.272727|grams|each": 60.0,
					"Mango mix*0.5+Cheesecake Mix*-0.136364|grams|each": 30.0,
					"Cheesecake Mix|grams|x1.272727|each": 63.0,
				}
			),
		)
		self.assertEqual(
			[
				("Savoiardi 1", 11.0),
				("Savoiardi 2", 11.0),
				("Mango mix 1", 30.0),
				("Mango mix 2", 30.0),
				("Mango cream 1", 21.0),
				("Mango cream 2", 21.0),
				("Mango cream 3", 21.0),
			],
			[(r["layer"], r["grams"]) for r in rows],
		)
		self.assertEqual(1, len(notes))
		self.assertIn("order", notes[0])

	def test_a_token_that_does_not_resolve_is_reported_not_guessed(self):
		rows, notes = seed_layers.derive_layers("{{item:Ghost|grams|each}} ghost", evaluator({}))
		self.assertEqual([], rows)
		self.assertIn("does not resolve", notes[0])

	def test_only_the_english_line_is_read(self):
		rows, _notes = seed_layers.derive_layers(
			"{{item:A|grams|each}} cream.\n{{item:A|grams|each}} كريمة.", evaluator({"A|grams|each": 5.0})
		)
		self.assertEqual(1, len(rows))

	def test_names_come_from_the_words_after_the_token(self):
		self.assertEqual("Coffee soak", seed_layers.layer_name(" coffee, then "))
		self.assertEqual("Cocoa", seed_layers.layer_name(" cocoa on top. "))
		self.assertEqual("Mango cream", seed_layers.layer_name(" mango cream (three layers)."))
		self.assertEqual("Sprinkles", seed_layers.layer_name(" sprinkles."))
		self.assertEqual("Layer", seed_layers.layer_name(""))


if __name__ == "__main__":
	unittest.main()

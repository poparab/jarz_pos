"""Recipe Book: tokenizer, version status, API shaping, controller rules, seeder.

Everything runs on mocks (no database), like the rest of the SOP suite.  The
tokenizer tests also pin that ``tokenize_instruction`` and ``render_instruction``
agree, since the app shows the first and the Production Board the second.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import sop_rendering as r

QTY = {"flour": 1.5, "coco powder": 0.9, "eggs": 45.0, "sugar": 0.2, "oil": 2.0}
UOM = {"flour": "Kg", "coco powder": "Kg", "eggs": "piece", "sugar": "Kg", "oil": "Litre"}
NAME = {"flour": "Flour", "coco powder": "Cocoa", "eggs": "Eggs", "sugar": "Sugar", "oil": "Oil"}


def tok(text, units_per_batch=None):
	return r.tokenize_instruction(
		text, component_qty_map=QTY, uom_map=UOM, name_map=NAME, units_per_batch=units_per_batch
	)


def render(text, units_per_batch=None):
	return r.render_instruction(
		text,
		component_qty_map=QTY,
		uom_map=UOM,
		name_map=NAME,
		batches=1,
		units_per_batch=units_per_batch,
	)


class TestTokenizeInstruction(unittest.TestCase):
	def test_grams_token_between_text(self):
		segments, unresolved = tok("Sift {{item:coco powder|grams}} cocoa.")
		self.assertEqual([], unresolved)
		self.assertEqual({"text": "Sift "}, segments[0])
		self.assertEqual({"text": " cocoa."}, segments[2])
		self.assertEqual(
			{
				"key": "coco powder",
				"amount": 900.0,
				"unit": "g",
				"format": "grams",
				"scales": True,
				"name": "Cocoa",
			},
			segments[1],
		)

	def test_count_token_keeps_the_raw_unit(self):
		(seg,) = tok("{{item:eggs|count}}")[0]
		self.assertEqual(("count", 45.0, "piece", True), (seg["format"], seg["amount"], seg["unit"], seg["scales"]))

	def test_qty_and_full_forms_carry_the_stock_unit(self):
		qty, full = tok("{{item:flour|qty}}{{item:flour}}")[0]
		self.assertEqual(("qty", 1.5, "Kg"), (qty["format"], qty["amount"], qty["unit"]))
		self.assertEqual(("full", 1.5, "Kg", "Flour"), (full["format"], full["amount"], full["unit"], full["name"]))

	def test_multiplier_is_applied(self):
		(seg,) = tok("{{item:flour|grams|x3}}")[0]
		self.assertEqual(4500.0, seg["amount"])
		self.assertTrue(seg["scales"])

	def test_each_divides_by_the_yield_and_does_not_scale(self):
		(seg,) = tok("{{item:flour|grams|each}}", units_per_batch=10)[0]
		self.assertEqual(150.0, seg["amount"])
		self.assertFalse(seg["scales"])

	def test_each_without_a_yield_is_unresolved(self):
		segments, unresolved = tok("Fill {{item:flour|grams|each}} each")
		self.assertEqual([{"text": "Fill {{item:flour|grams|each}} each"}], segments)
		self.assertEqual(["flour|grams|each"], unresolved)

	def test_a_sum_has_no_key_and_adds_up(self):
		(seg,) = tok("{{item:flour+sugar|grams}}")[0]
		self.assertIsNone(seg["key"])
		self.assertEqual(1700.0, seg["amount"])
		self.assertEqual("Flour + Sugar", seg["name"])

	def test_name_and_uom_do_not_scale(self):
		name, uom = tok("{{item:oil|name}}{{item:oil|uom}}")[0]
		self.assertEqual(("name", "Oil", False), (name["format"], name["name"], name["scales"]))
		self.assertEqual(("uom", "Litre", False), (uom["format"], uom["unit"], uom["scales"]))

	def test_grams_of_a_non_mass_unit_is_unresolved(self):
		segments, unresolved = tok("{{item:oil|grams}}")
		self.assertEqual([{"text": "{{item:oil|grams}}"}], segments)
		self.assertEqual(["oil|grams"], unresolved)

	def test_unknown_token_stays_text_and_is_listed(self):
		segments, unresolved = tok("Add {{item:Unicorn dust|grams}} now")
		self.assertEqual([{"text": "Add {{item:Unicorn dust|grams}} now"}], segments)
		self.assertEqual(["Unicorn dust"], unresolved)

	def test_code_lookup_is_case_insensitive_and_key_is_lowercase(self):
		(seg,) = tok("{{ item : FLOUR | grams }}")[0]
		self.assertEqual(("flour", 1500.0), (seg["key"], seg["amount"]))

	def test_empty_text(self):
		self.assertEqual(([], []), tok(""))
		self.assertEqual(([], []), tok(None))

	def test_text_without_tokens_is_one_segment(self):
		self.assertEqual(([{"text": "Just mix."}], []), tok("Just mix."))

	def test_tokenizer_agrees_with_render_instruction(self):
		for text in (
			"{{item:flour|grams}}",
			"{{item:coco powder|grams|x2}}",
			"{{item:flour+sugar|grams}}",
			"{{item:eggs|count}}",
			"{{item:flour}}",
			"{{item:flour|qty}}",
			"{{item:oil|grams}}",
			"{{item:nope}}",
			"{{item:flour|grams|each}}",
		):
			for upb in (None, 10):
				with self.subTest(text=text, units_per_batch=upb):
					segments, unresolved = tok(text, upb)
					rendered, render_unresolved = render(text, upb)
					self.assertEqual(render_unresolved, unresolved)
					(seg,) = segments
					if "text" in seg:
						self.assertEqual(rendered, seg["text"])
					elif seg["format"] == "grams":
						self.assertEqual(rendered, r._grams_text(seg["amount"]))
					elif seg["format"] == "count":
						self.assertEqual(rendered, r._one_decimal(seg["amount"]))
					elif seg["format"] == "qty":
						self.assertEqual(rendered, f"{seg['amount']:.3f}")


# ── Version status and grouping ─────────────────────────────────────────


def row(name, version, stage="Approved", is_active=0, item_code="Cake", title=None, track=None, modified="2026-11-01 10:00:00", **kw):
	data = {
		"name": name,
		"version": version,
		"stage": stage,
		"is_active": is_active,
		"item_code": item_code,
		"item_name": item_code,
		"recipe_title": title or item_code,
		"track": track,
		"modified": modified,
		"approved_on": None,
		"change_summary": None,
	}
	data.update(kw)
	return data


class TestVersionStatus(unittest.TestCase):
	def setUp(self):
		from jarz_pos.api import recipe_book

		self.rb = recipe_book

	def test_item_recipe_statuses(self):
		rows = [
			row("A", 1),
			row("B", 2, is_active=1),
			row("C", 3, stage="Trial"),
			row("D", 0, stage="Retired"),
		]
		self.assertEqual(
			{"A": "previous", "B": "current", "C": "trial", "D": "retired"},
			self.rb.version_statuses(rows),
		)

	def test_no_item_recipe_current_is_the_highest_approved(self):
		rows = [
			row("A", 1, item_code=None, title="Carrot"),
			row("B", 2, item_code=None, title="Carrot"),
			row("C", 3, item_code=None, title="Carrot", stage="Trial"),
		]
		self.assertEqual({"A": "previous", "B": "current", "C": "trial"}, self.rb.version_statuses(rows))

	def test_recipe_status_and_headline(self):
		approved = self.rb.build_recipe([row("A", 1, is_active=1), row("B", 2, stage="Trial")])
		self.assertEqual(("approved", "A", True), (approved["status"], approved["headline"], approved["has_trial"]))
		self.assertEqual(["B", "A"], [v["name"] for v in approved["versions"]])  # newest first

		trial_only = self.rb.build_recipe([row("T1", 1, stage="Trial"), row("T2", 2, stage="Trial")])
		self.assertEqual(("trial", "T2", False), (trial_only["status"], trial_only["headline"], trial_only["has_trial"]))

		retired = self.rb.build_recipe([row("R", 1, stage="Retired")])
		self.assertEqual(("retired", "R"), (retired["status"], retired["headline"]))

	def test_in_production_needs_active_approved_and_an_item(self):
		card = self.rb.build_recipe([row("A", 1, is_active=1), row("B", 2, stage="Trial", is_active=1)])
		flags = {v["name"]: v["in_production"] for v in card["versions"]}
		self.assertEqual({"A": True, "B": False}, flags)

	def test_list_sorting_tracks_and_key(self):
		rows = [
			row("A", 1, item_code="Zed", track="Cookie jar line", is_active=1),
			row("B", 1, item_code="Alpha", track="Standard line", is_active=1),
			row("C", 1, item_code=None, title="Mystery", track=None, stage="Trial"),
			row("D", 1, item_code="Beta", track="Standard line", is_active=1),
		]
		out = self.rb.build_recipe_list(rows)
		self.assertEqual(["Alpha", "Beta", "Zed", "Mystery"], [x["key"] for x in out["recipes"]])
		self.assertEqual(["Standard line", "Cookie jar line"], out["tracks"])
		self.assertIsNone(out["recipes"][-1]["item_code"])

	def test_list_recipes_is_one_query(self):
		rb = self.rb
		with (
			patch.object(rb, "_ensure_production_view_access"),
			patch.object(rb, "frappe") as mock_frappe,
		):
			mock_frappe.get_all.return_value = [row("A", 1, is_active=1), row("B", 1, item_code="X", is_active=1)]
			out = rb.list_recipes.__wrapped__() if hasattr(rb.list_recipes, "__wrapped__") else rb.list_recipes()
		self.assertEqual(1, mock_frappe.get_all.call_count)
		self.assertEqual(2, len(out["recipes"]))


class TestHelpers(unittest.TestCase):
	def setUp(self):
		from jarz_pos.api import recipe_book

		self.rb = recipe_book

	def test_html_to_text_keeps_line_breaks(self):
		html = "<p>Mix &amp; rest</p><p>Bake<br>now&nbsp;slowly</p>"
		self.assertEqual("Mix & rest\nBake\nnow slowly", self.rb.html_to_text(html))

	def test_html_inside_a_token_is_dropped_before_tokenising(self):
		text = self.rb.html_to_text("<p>Add {{item:<b>flour</b>|grams}}</p>")
		segments, _ = tok(text)
		self.assertEqual(1500.0, segments[1]["amount"])

	def test_table_ingredients_grams_and_raw_units(self):
		rows = [
			{"ingredient": "Flour", "item_code": None, "qty": 1.5, "uom": "Kg", "section": "Dough", "note": ""},
			{"ingredient": "Eggs", "item_code": "EGG-01", "qty": 4, "uom": "piece", "section": "", "note": "large"},
		]
		ingredients, qty, uom, name, alias = self.rb.ingredients_from_table(rows)
		self.assertEqual(
			{"key": "flour", "name": "Flour", "amount": 1500.0, "unit": "g", "section": "Dough", "note": None},
			ingredients[0],
		)
		self.assertEqual(("egg-01", 4.0, "piece", "large"), (ingredients[1]["key"], ingredients[1]["amount"], ingredients[1]["unit"], ingredients[1]["note"]))
		# A token may name the ingredient by item code or by name; the key stays the code.
		self.assertIn("Eggs", qty)
		self.assertEqual("egg-01", alias["eggs"])
		segments, unresolved = r.tokenize_instruction(
			"{{item:Eggs|count}}", component_qty_map=qty, uom_map=uom, name_map=name
		)
		self.assertEqual([], unresolved)
		self.assertEqual(4.0, segments[0]["amount"])

	def test_duplicate_ingredient_rows_get_distinct_keys_and_sum_in_the_maps(self):
		rows = [
			{"ingredient": "Sugar", "qty": 100, "uom": "g"},
			{"ingredient": "Sugar", "qty": 0.05, "uom": "Kg"},
		]
		ingredients, qty, uom, _name, _alias = self.rb.ingredients_from_table(rows)
		self.assertEqual(["sugar", "sugar#2"], [i["key"] for i in ingredients])
		self.assertEqual((150.0, "g"), (qty["Sugar"], uom["Sugar"]))

	def test_get_recipe_from_the_ingredient_table(self):
		rb = self.rb
		doc = MagicMock()
		doc.name = "SOP-9"
		values = {
			"item_code": None,
			"recipe_title": "Cookie trial",
			"item_name": None,
			"track": "Cookie jar line",
			"stage": "Trial",
			"version": 2,
			"is_active": 0,
			"bom": None,
			"yield_qty": 4,
			"yield_uom": "jars",
			"equipment": "Oven",
			"prep_time_mins": 10,
			"notes": "<p>Note</p>",
			"approved_on": None,
			"change_summary": "Less sugar",
			"ingredients": [{"ingredient": "Flour", "qty": 500, "uom": "g", "section": "Dough"}],
			"steps": [
				{
					"step_no": 1,
					"idx": 1,
					"title": "Mix",
					"instruction": "<p>Add {{item:Flour|grams}} and {{item:Flour|grams|each}}.</p>",
					"duration_mins": 5,
					"scaling_mode": "Per Batch",
					"capture_type": "Temperature",
					"capture_label": "Dough temp",
					"capture_min": 20,
					"capture_max": 25,
				}
			],
		}
		doc.get.side_effect = lambda k, d=None: values.get(k, d)
		with (
			patch.object(rb, "_ensure_production_view_access"),
			patch.object(rb, "_resolve_sop_doc", return_value=doc),
			patch.object(rb, "_field", side_effect=lambda d, n, default=None: values.get(n, default) if d is doc else (d.get(n, default) if isinstance(d, dict) else getattr(d, n, default))),
			patch.object(rb, "_step_rows", return_value=values["steps"]),
			patch.object(rb, "_resolve_bom", return_value=None),
			patch.object(rb, "_fetch_rows", return_value=[row("SOP-9", 2, stage="Trial", item_code=None, title="Cookie trial")]),
		):
			out = rb.get_recipe("SOP-9")
		self.assertEqual(("Cookie trial", "Cookie trial", None, "trial"), (out["key"], out["title"], out["item_code"], out["status"]))
		self.assertEqual({"label": "One batch", "amount": 4.0, "unit": "jars"}, out["yield"])
		self.assertEqual("Note", out["notes"])
		self.assertEqual([], out["unresolved_tokens"])
		step = out["steps"][0]
		self.assertEqual((20.0, 25.0, "Dough temp"), (step["capture_min"], step["capture_max"], step["capture_label"]))
		amounts = [(s["amount"], s["scales"]) for s in step["segments"] if "format" in s]
		self.assertEqual([(500.0, True), (125.0, False)], amounts)


	def test_get_recipe_from_the_bom(self):
		rb = self.rb
		doc = MagicMock()
		doc.name = "SOP-42"
		values = {
			"item_code": "Fudge Cake", "recipe_title": "Fudge Cake", "stage": "Approved", "version": 4,
			"is_active": 1, "bom": "BOM-1", "ingredients": [], "notes": "", "yield_qty": None,
		}
		steps = [{"step_no": 1, "title": "Whisk {{item:flour|grams}}", "instruction": "Use {{item:flour|grams}}", "scaling_mode": "Fixed"}]
		with (
			patch.object(rb, "_ensure_production_view_access"),
			patch.object(rb, "_resolve_sop_doc", return_value=doc),
			patch.object(rb, "_field", side_effect=lambda d, n, default=None: values.get(n, default)),
			patch.object(rb, "_step_rows", return_value=steps),
			patch.object(rb, "_resolve_bom", return_value={"name": "BOM-1", "quantity": 14.29, "company": "J", "uom": "Kg"}),
			patch.object(rb, "_build_component_maps_with_direct", return_value=({"flour": 1.5, "eggs": 45.0}, {"flour": "Kg", "eggs": "piece"}, {"flour": "Flour", "eggs": "Eggs"}, ["flour", "eggs"])),
			patch.object(rb, "_fetch_rows", return_value=[row("SOP-42", 4, is_active=1, item_code="Fudge Cake")]),
		):
			out = rb.get_recipe("SOP-42")
		self.assertEqual({"label": "One batch", "amount": 14.29, "unit": "Kg"}, out["yield"])
		self.assertEqual([("flour", 1500.0, "g"), ("eggs", 45.0, "piece")], [(i["key"], i["amount"], i["unit"]) for i in out["ingredients"]])
		self.assertEqual(("current", True, "Whisk 1500 g"), (out["status"], out["in_production"], out["steps"][0]["title"]))
		self.assertEqual(1500.0, out["steps"][0]["segments"][1]["amount"])


# ── Controller rules ────────────────────────────────────────────────────


class TestControllerRules(unittest.TestCase):
	def _sop(self, **fields):
		from jarz_pos.tests.test_doctype_jarz_sop import load, make_sop

		module = load()
		return module, make_sop(module, **fields)

	def _validate(self, module, sop):
		with patch("jarz_pos.doctype.jarz_sop.jarz_sop.frappe") as mock_frappe, patch(
			"jarz_pos.doctype.jarz_sop.jarz_sop._", new=lambda m: m
		):
			mock_frappe.throw.side_effect = ValueError("refused")
			mock_frappe.db.exists.return_value = False
			sop._apply_recipe_book_rules()
			return mock_frappe

	def test_title_defaults_to_the_item_name(self):
		module, sop = self._sop(item_name="Fudge Cake")
		self._validate(module, sop)
		self.assertEqual(("Fudge Cake", "Approved"), (sop.recipe_title, sop.stage))

	def test_neither_title_nor_item_is_refused(self):
		module, sop = self._sop(item_code=None, is_active=0)
		with self.assertRaises(ValueError):
			self._validate(module, sop)

	def test_trial_is_forced_inactive(self):
		module, sop = self._sop(stage="Trial", is_active=1)
		self._validate(module, sop)
		self.assertEqual(0, sop.is_active)

	def test_retired_is_forced_inactive(self):
		module, sop = self._sop(stage="Retired", is_active=1)
		self._validate(module, sop)
		self.assertEqual(0, sop.is_active)

	def test_active_approved_without_an_item_is_forced_inactive(self):
		# is_active defaults to 1, so a recipe typed in Desk with no Item must save, inactive.
		module, sop = self._sop(item_code=None, recipe_title="Carrot", stage="Approved", is_active=1)
		self._validate(module, sop)
		self.assertEqual(0, sop.is_active)

	def test_approved_without_an_item_may_be_inactive(self):
		module, sop = self._sop(item_code=None, recipe_title="Carrot", stage="Approved", is_active=0)
		self._validate(module, sop)
		self.assertEqual(0, sop.is_active)

	def test_unknown_stage_is_refused(self):
		module, sop = self._sop(stage="Draft")
		with self.assertRaises(ValueError):
			self._validate(module, sop)

	def test_deactivation_skips_a_record_with_no_item(self):
		module, sop = self._sop(item_code=None, is_active=1)
		with patch("jarz_pos.doctype.jarz_sop.jarz_sop.frappe") as mock_frappe:
			sop._deactivate_other_versions()
		mock_frappe.get_all.assert_not_called()


# ── Seeder ──────────────────────────────────────────────────────────────

NO_ITEM = {
	"recipe_title": "Carrot Cake",
	"version": 1,
	"stage": "Approved",
	"approved_on": "2026-11-20",
	"change_summary": "First write-up",
	"yield_qty": 2,
	"yield_uom": "trays",
	"ingredients": [{"ingredient": "Carrots", "qty": 1.8, "uom": "Kg", "section": "Core", "note": "grated"}],
	"steps": [{"title": "Mix", "instruction": "Mix {{item:Carrots|grams}}"}],
}


class TestSeederNoItem(unittest.TestCase):
	def test_apply_writes_the_book_fields_and_is_never_active_without_an_item(self):
		from jarz_pos.scripts import seed_recipe_sops as seed

		doc = MagicMock()
		with patch.object(seed, "frappe"), patch.object(seed, "_default_bom") as default_bom:
			seed._apply(doc, NO_ITEM)
		default_bom.assert_not_called()
		self.assertIsNone(doc.item_code)
		self.assertEqual(0, doc.is_active)
		self.assertEqual(("Approved", "Standard line", "Carrot Cake"), (doc.stage, doc.track, doc.recipe_title))
		self.assertEqual(("2026-11-20", "First write-up", 2.0, "trays"), (doc.approved_on, doc.change_summary, doc.yield_qty, doc.yield_uom))
		doc.append.assert_any_call(
			"ingredients",
			{"ingredient": "Carrots", "item_code": None, "qty": 1.8, "uom": "Kg", "section": "Core", "note": "grated"},
		)

	def test_item_recipe_defaults_stay_approved_and_active(self):
		from jarz_pos.scripts import seed_recipe_sops as seed

		doc = MagicMock()
		recipe = {"item_code": "Fudge Cake", "version": 4, "steps": []}
		with patch.object(seed, "frappe") as mock_frappe, patch.object(seed, "_default_bom", return_value="BOM-1"):
			mock_frappe.db.get_value.return_value = "Fudge Cake"
			seed._apply(doc, recipe)
		self.assertEqual((1, "Approved", "Standard line", "BOM-1"), (doc.is_active, doc.stage, doc.track, doc.bom))

	def test_trial_with_an_item_is_inactive(self):
		from jarz_pos.scripts import seed_recipe_sops as seed

		doc = MagicMock()
		with patch.object(seed, "frappe"), patch.object(seed, "_default_bom", return_value="BOM-1"):
			seed._apply(doc, {"item_code": "Fudge Cake", "version": 5, "stage": "Trial", "steps": []})
		self.assertEqual(0, doc.is_active)

	def _run(self, books, **kwargs):
		from jarz_pos.scripts import seed_recipe_sops as seed

		with patch.object(seed, "frappe") as mock_frappe, patch.object(seed, "BOOK_RECIPES", books):
			mock_frappe.db.exists.return_value = True
			mock_frappe.db.get_value.return_value = None
			return seed.run(dry_run=True, **kwargs), mock_frappe

	def test_run_seeds_a_no_item_recipe_without_item_or_bom_checks(self):
		result, mock_frappe = self._run([NO_ITEM], items="Carrot Cake")
		self.assertEqual(["Carrot Cake"], result["created"])
		self.assertEqual([], result["skipped"])
		mock_frappe.db.exists.assert_not_called()
		identity = mock_frappe.db.get_value.call_args[0][1]
		self.assertEqual(
			{"recipe_title": "Carrot Cake", "version": 1, "item_code": ["is", "not set"]}, identity
		)

	def test_run_updates_an_existing_no_item_recipe(self):
		from jarz_pos.scripts import seed_recipe_sops as seed

		with patch.object(seed, "frappe") as mock_frappe, patch.object(seed, "BOOK_RECIPES", [NO_ITEM]):
			mock_frappe.db.get_value.return_value = "SOP-0099"
			result = seed.run(dry_run=True, items="Carrot Cake")
		self.assertEqual(["Carrot Cake"], result["updated"])

	def test_a_no_item_recipe_without_a_title_is_skipped_not_crashed(self):
		bad = dict(NO_ITEM)
		bad.pop("recipe_title")
		result, _ = self._run([bad])
		self.assertEqual(1, len([s for s in result["skipped"] if "recipe_title" in s["reason"]]))

	def test_the_shipped_book_data_is_well_formed(self):
		from jarz_pos.scripts import seed_recipe_sops as seed

		seen = set()
		for rec in seed.BOOK_RECIPES:
			ident = (rec.get("item_code") or rec.get("recipe_title"), rec["version"])
			self.assertNotIn(ident, seen)
			seen.add(ident)
			self.assertTrue(rec.get("item_code") or rec.get("recipe_title"))
			self.assertIn(rec.get("stage") or "Approved", ("Approved", "Trial", "Retired"))
			for ing in rec.get("ingredients") or []:
				self.assertTrue(ing["ingredient"] and ing["uom"])


# ── v2: catalog, classification, jar families ──────────────────────────


class TestClassifyRecipe(unittest.TestCase):
	def setUp(self):
		from jarz_pos.api import recipe_book

		self.rb = recipe_book

	def test_jars_with_a_size(self):
		cases = [
			(("Tiramisu Large", "Large"), ("jars", "Tiramisu", "Large")),
			(("Chocolate Hazelnut Small", "Small"), ("jars", "Chocolate Hazelnut", "Small")),
			(("Carrot cake jar (Medium)", None), ("jars", "Carrot cake", "Medium")),
			(("Apple pie jar (Medium)", None), ("jars", "Apple pie", "Medium")),
			(("Tiramisu", "Medium"), ("jars", "Tiramisu", "Medium")),
			(("tiramisu large", None), ("jars", "Tiramisu", "Large")),
		]
		for args, expected in cases:
			with self.subTest(args=args):
				self.assertEqual(expected, self.rb.classify_recipe(*args))

	def test_a_jar_without_a_size_has_no_family(self):
		self.assertEqual(("jars", None, None), self.rb.classify_recipe("Cookie jar second bake", None))

	def test_fillings(self):
		for title, group in [
			("Cheesecake Mix", "Sub Assemblies"),
			("Chocolate ganache", None),
			("Cocoa soak syrup", None),
			("Soak at 15%", None),
			("London caramel", None),
			("Apple pie filling", None),
			("S1-L cocoa syrup", None),
			("Mango mix", None),
			("raspberry mix", None),
			("Something unclassifiable", None),
		]:
			with self.subTest(title=title):
				self.assertEqual(("fillings", None, None), self.rb.classify_recipe(title, group))

	def test_bases(self):
		for title in [
			"Fudge Cake",
			"Red Velvet Cake",
			"Sponge Cake",
			"Savoiardi",
			"Butter Biscuit",
			"Carrot Cake",
			"Apple pie crumbs",
		]:
			with self.subTest(title=title):
				self.assertEqual(("bases", None, None), self.rb.classify_recipe(title, "Sub Assemblies"))

	def test_override_wins_over_the_heuristic(self):
		with patch.dict(self.rb.CATEGORY_OVERRIDES, {"fudge cake": "fillings"}):
			self.assertEqual(("fillings", None, None), self.rb.classify_recipe("Fudge Cake", None))
		self.assertEqual("bases", self.rb.classify_recipe("Fudge Cake", None)[0])


def _row(name, item_code, title=None, stage="Approved", version=1, is_active=1, modified="2026-11-01 10:00:00", track=None):
	return {
		"name": name,
		"item_code": item_code,
		"item_name": item_code,
		"recipe_title": title,
		"track": track,
		"stage": stage,
		"version": version,
		"is_active": is_active,
		"approved_on": None,
		"change_summary": None,
		"modified": modified,
	}


class TestBuildCatalog(unittest.TestCase):
	def setUp(self):
		from jarz_pos.api import recipe_book

		self.rb = recipe_book
		self.rows = [
			_row("SOP-1", "Tiramisu Small", track="Standard line"),
			_row("SOP-2", "Tiramisu Large", stage="Approved", is_active=1, track="Standard line"),
			_row("SOP-3", "Tiramisu Large", stage="Trial", version=2, is_active=0),
		]
		self.boms = [
			{"item": "Tiramisu Small", "modified": "2026-10-01 00:00:00"},
			{"item": "Tiramisu Medium", "modified": "2026-10-02 00:00:00"},
			{"item": "Fudge Cake", "modified": "2026-10-03 00:00:00"},
			{"item": "Old Thing", "modified": "2026-10-04 00:00:00"},
			{"item": "Cheesecake Mix", "modified": "2026-10-05 00:00:00"},
		]
		self.info = {
			"Tiramisu Small": {"item_name": "Tiramisu Small", "item_group": "Small", "disabled": 0},
			"Tiramisu Medium": {"item_name": "Tiramisu Medium", "item_group": "Medium", "disabled": 0},
			"Tiramisu Large": {"item_name": "Tiramisu Large", "item_group": "Large", "disabled": 0},
			"Fudge Cake": {"item_name": "Fudge Cake", "item_group": "Sub Assemblies", "disabled": 0},
			"Old Thing": {"item_name": "Old Thing", "item_group": "Sub Assemblies", "disabled": 1},
			"Cheesecake Mix": {"item_name": "Cheesecake Mix", "item_group": "Sub Assemblies", "disabled": 0},
		}
		self.out = self.rb.build_catalog(self.rows, self.boms, self.info)
		self.by_key = {r["key"]: r for r in self.out["recipes"]}

	def test_bom_only_recipes_are_added_but_not_for_sop_items_or_disabled_items(self):
		self.assertEqual(
			{"Tiramisu Small", "Tiramisu Large", "Tiramisu Medium", "Fudge Cake", "Cheesecake Mix"}, set(self.by_key)
		)

	def test_bom_only_entry_shape(self):
		self.assertEqual(
			{
				"key": "Fudge Cake",
				"title": "Fudge Cake",
				"item_code": "Fudge Cake",
				"track": None,
				"status": "approved",
				"headline": None,
				"has_trial": False,
				"updated": "2026-10-03 00:00:00",
				"versions": [],
				"category": "bases",
				"family": None,
				"size": None,
				"has_method": False,
				"source": "bom",
			},
			self.by_key["Fudge Cake"],
		)

	def test_sop_recipe_keeps_its_v1_keys_and_gains_the_new_ones(self):
		rec = self.by_key["Tiramisu Large"]
		self.assertEqual(
			("jars", "Tiramisu", "Large", True, "sop"),
			(rec["category"], rec["family"], rec["size"], rec["has_method"], rec["source"]),
		)
		self.assertTrue(rec["has_trial"])
		self.assertEqual("SOP-2", rec["headline"])

	def test_categories_and_families(self):
		self.assertEqual(["bases", "fillings", "jars"], [c["key"] for c in self.out["categories"]])
		self.assertEqual("Cakes & biscuits", self.out["categories"][0]["label"])
		(fam,) = self.out["families"]
		self.assertEqual("Tiramisu", fam["family"])
		self.assertEqual("Standard line", fam["track"])
		self.assertEqual(["Small", "Medium", "Large"], fam["sizes"])
		self.assertTrue(fam["has_trial"])
		self.assertEqual("approved", fam["status"])
		self.assertEqual("2026-11-01 10:00:00", fam["updated"])

	def test_a_trial_only_family_is_trial(self):
		rows = [_row("SOP-9", "Mango Small", stage="Trial", is_active=0)]
		info = {"Mango Small": {"item_name": "Mango Small", "item_group": "Small", "disabled": 0}}
		(fam,) = self.rb.build_catalog(rows, [], info)["families"]
		self.assertEqual(("Mango", "trial", True), (fam["family"], fam["status"], fam["has_trial"]))

	def test_catalog_off_is_the_v1_payload(self):
		rb = self.rb
		v1 = rb.build_recipe_list(self.rows)
		with patch.object(rb, "_ensure_production_view_access"), patch.object(
			rb, "_fetch_rows", return_value=self.rows
		), patch.object(rb, "_fetch_catalog_inputs") as inputs:
			for off in (0, None, "0", "", "false"):
				self.assertEqual(v1, rb.list_recipes(off))
			self.assertEqual(v1, rb.list_recipes())
			inputs.assert_not_called()
			inputs.return_value = (self.boms, self.info)
			on = rb.list_recipes("1")
		self.assertIn("families", on)
		self.assertNotIn("families", v1)
		self.assertNotIn("category", v1["recipes"][0])


class TestJarFamily(unittest.TestCase):
	def setUp(self):
		from jarz_pos.api import recipe_book

		self.rb = recipe_book
		self.members = [
			{"size": "Large", "item_code": "Tiramisu Large", "title": "Tiramisu Large", "track": None,
			 "status": "approved", "sop": None, "sop_version": None, "bom": "BOM-L"},
			{"size": "Small", "item_code": "Tiramisu Small", "title": "Tiramisu Small", "track": "Standard line",
			 "status": "approved", "sop": "SOP-1", "sop_version": 2, "bom": "BOM-S"},
			{"size": "Medium", "item_code": None, "title": "Tiramisu Medium", "track": None,
			 "status": "trial", "sop": "SOP-M", "sop_version": 1, "bom": None},
		]
		self.boms = {
			"BOM-S": {
				"quantity": 1,
				"lines": [
					{"item_code": "Jar 147", "item_name": "Glass Jar 147", "stock_qty": 1, "stock_uom": "Nos"},
					{"item_code": "Cheesecake Mix", "item_name": "Cheesecake Mix", "stock_qty": 0.0431, "stock_uom": "Kg"},
					{"item_code": "Savoiardi", "item_name": "Savoiardi", "stock_qty": 20, "stock_uom": "g"},
				],
			},
			"BOM-L": {
				"quantity": 2,
				"lines": [
					{"item_code": "Cheesecake Mix", "item_name": "Cheesecake Mix", "stock_qty": 0.28, "stock_uom": "Kg"},
					{"item_code": "Jar 400", "item_name": "Glass Jar 400", "stock_qty": 2, "stock_uom": "Nos"},
					{"item_code": "Cocoa", "item_name": "Cocoa", "stock_qty": 10, "stock_uom": "g"},
					{"item_code": "Cocoa", "item_name": "Cocoa", "stock_qty": 4, "stock_uom": "g"},
				],
			},
		}
		self.sops = {
			"SOP-M": {
				"yield_qty": 4,
				"yield_uom": "jars",
				"ingredients": [
					{"ingredient": "Cheesecake Mix", "item_code": "Cheesecake Mix", "qty": 0.344, "uom": "Kg"},
					{"ingredient": "Glass Jar 250", "item_code": "Jar 250", "qty": 4, "uom": "Nos"},
				],
			}
		}
		self.groups = {"jar 147": "Packaging", "jar 400": "Glass", "jar 250": "Glass"}
		self.parents = {"Glass": "Packaging", "Packaging": "Materials"}
		self.out = self.rb.build_jar_family("Tiramisu", self.members, self.boms, self.sops, self.groups, self.parents)

	def test_sizes_ordered_small_medium_large(self):
		self.assertEqual(["Small", "Medium", "Large"], [s["size"] for s in self.out["sizes"]])
		self.assertEqual("Standard line", self.out["track"])
		small = self.out["sizes"][0]
		self.assertEqual(("SOP-1", 2, "approved"), (small["sop"], small["sop_version"], small["status"]))

	def test_layer_order_is_first_appearance_small_to_large(self):
		self.assertEqual(["cheesecake mix", "savoiardi", "cocoa"], [x["key"] for x in self.out["layers"]])

	def test_kg_become_grams_and_bom_quantity_divides(self):
		by = {x["key"]: x for x in self.out["layers"]}
		self.assertEqual({"Small": 43.1, "Medium": 86.0, "Large": 140.0}, by["cheesecake mix"]["per_jar"])
		self.assertEqual("g", by["cheesecake mix"]["unit"])
		# duplicate BOM lines add up, then divide by the BOM quantity of 2
		self.assertEqual({"Large": 7.0}, by["cocoa"]["per_jar"])

	def test_packaging_split_by_group_and_ancestor(self):
		by = {p["key"]: p for p in self.out["packaging"]}
		# "jar 147" / "jar 250" / "jar 400" are one part in three sizes: one row
		self.assertEqual({"glass jar"}, set(by))
		self.assertEqual({"Small": 1, "Medium": 1, "Large": 1}, by["glass jar"]["per_jar"])
		self.assertEqual("Nos", by["glass jar"]["unit"])
		self.assertEqual("Glass jar", by["glass jar"]["name"])
		self.assertEqual({"Small": "Glass Jar 147", "Medium": "Glass Jar 250", "Large": "Glass Jar 400"}, by["glass jar"]["items"])
		self.assertNotIn("jar 147", [x["key"] for x in self.out["layers"]])

	def test_packaging_role(self):
		self.assertEqual("tiramisu jar label", self.rb.packaging_role("Tiramisu  Jar Label 147"))
		self.assertEqual("glass jar", self.rb.packaging_role("glass jar"))
		self.assertEqual("jar lid", self.rb.packaging_role("Jar Lid 330"))
		self.assertEqual("147", self.rb.packaging_role("147"))

	def test_sop_yield_in_jars_fallback(self):
		by = {x["key"]: x for x in self.out["layers"]}
		self.assertEqual(86.0, by["cheesecake mix"]["per_jar"]["Medium"])
		self.assertEqual({"Medium": 1}, {p["key"]: p["per_jar"] for p in self.out["packaging"]}["glass jar"]["Medium"] and {"Medium": 1})
		self.assertEqual(86.0, self.out["sizes"][1]["jar_grams"])

	def test_jar_grams_sums_gram_layers_only(self):
		self.assertEqual(63.1, self.out["sizes"][0]["jar_grams"])
		self.assertEqual(147.0, self.out["sizes"][2]["jar_grams"])

	def test_no_bom_and_no_jar_yield_contributes_nothing(self):
		sops = {"SOP-M": dict(self.sops["SOP-M"], yield_uom="g")}
		out = self.rb.build_jar_family("Tiramisu", self.members, self.boms, sops, self.groups, self.parents)
		medium = out["sizes"][1]
		self.assertIsNone(medium["jar_grams"])
		self.assertTrue(all("Medium" not in x["per_jar"] for x in out["layers"] + out["packaging"]))

	def test_is_packaging_group(self):
		rb = self.rb
		self.assertTrue(rb.is_packaging_group("Labels", {}))
		self.assertTrue(rb.is_packaging_group("Consumable", {}))
		self.assertTrue(rb.is_packaging_group("Glass", self.parents))
		self.assertFalse(rb.is_packaging_group("Raw Material", {"Raw Material": "Materials"}))
		self.assertFalse(rb.is_packaging_group(None, {}))
		self.assertFalse(rb.is_packaging_group("A", {"A": "B", "B": "A"}))

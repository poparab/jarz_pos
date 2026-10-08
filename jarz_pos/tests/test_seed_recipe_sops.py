"""Tests for the Tiramisu SOPs in ``scripts/seed_recipe_sops.py``.

The recipes are data, but the data carries the kitchen's method, so the tests
render each SOP through the same pure renderer the Production Board uses,
against component maps that match the per-jar BOM figures.  If a token is
mistyped, an item code drifts from the BOM, or a unit stops converting, the
step comes back with a ``{{item:...}}`` still in it and these fail.

``seed_recipe_sops`` imports ``frappe`` at module level, so this module runs on
the bench like the rest of the suite; nothing here touches a database.
"""

import unittest
from unittest.mock import MagicMock, patch

JARS = 10

# Per-jar BOM lines in Kg (the stock UOM).  Medium is 16/3 g of grinds etc.;
# Small is exactly 2/3 of Medium.
MEDIUM = {
    "Coffee beans": 0.016 / 3,
    "powder sugar": 0.0048,
    "Cheesecake Mix": 0.0652,
    "Savoiardi": 0.028,
    "coco powder": 0.002,
}
BOM_PER_JAR = {
    "Tiramisu Large": {
        "Coffee beans": 0.008,
        "powder sugar": 0.0072,
        "Cheesecake Mix": 0.0898,
        "Savoiardi": 0.040,
        "coco powder": 0.003,
    },
    "Tiramisu Medium": MEDIUM,
    "Tiramisu Small": {code: qty * 2 / 3 for code, qty in MEDIUM.items()},
}


def render_recipe(item_code, jars=JARS):
    from jarz_pos.scripts import seed_recipe_sops as seed
    from jarz_pos.services.sop_rendering import render_sop

    recipe = next(r for r in seed.RECIPES if r["item_code"] == item_code)
    qty = BOM_PER_JAR[item_code]
    rendered = render_sop(
        recipe,
        batches=jars,
        units=jars,
        component_qty_map=qty,
        uom_map={code: "Kg" for code in qty},
        name_map={code: code for code in qty},
    )
    return recipe, rendered


def step_text(rendered, number):
    step = rendered["steps"][number - 1]
    return f"{step['title']}\n{step['instruction_html']}"


class TestTiramisuRecipes(unittest.TestCase):
    def test_the_three_jar_sizes_are_seeded_and_the_old_single_recipe_is_gone(self):
        from jarz_pos.scripts import seed_recipe_sops as seed

        codes = [r["item_code"] for r in seed.RECIPES if r["item_code"].startswith("Tiramisu")]
        self.assertEqual(["Tiramisu Large", "Tiramisu Medium", "Tiramisu Small"], sorted(codes))
        self.assertFalse(hasattr(seed, "TIRAMISU_ASSEMBLY"))

    def test_medium_is_version_two_so_a_v1_is_superseded_not_overwritten(self):
        from jarz_pos.scripts import seed_recipe_sops as seed

        versions = {r["item_code"]: r["version"] for r in seed.RECIPES if r["item_code"].startswith("Tiramisu")}
        self.assertEqual(
            {"Tiramisu Large": 1, "Tiramisu Medium": 2, "Tiramisu Small": 1}, versions
        )

    def test_no_recipe_leaves_a_token_unresolved(self):
        for item_code in BOM_PER_JAR:
            _, rendered = render_recipe(item_code)
            self.assertEqual([], rendered["unresolved_tokens"], item_code)
            for step in rendered["steps"]:
                self.assertNotIn("{{", step["title"] + step["instruction_html"], item_code)

    def test_large_run_of_ten_jars(self):
        _, rendered = render_recipe("Tiramisu Large")

        self.assertIn("80 g", step_text(rendered, 1))  # grinds
        self.assertIn("240 g", step_text(rendered, 1))  # liquid = 3 x grinds
        self.assertIn("72 g", step_text(rendered, 2))  # sugar
        self.assertIn("898 g", step_text(rendered, 3))  # cheesecake mix
        self.assertIn("72 g", step_text(rendered, 3))  # syrup into the cream = sugar weight
        self.assertIn("400 g", step_text(rendered, 4))  # savoiardi
        self.assertIn("240 g", step_text(rendered, 4))  # all the coffee, in total
        self.assertIn("30 g", step_text(rendered, 6))  # cocoa

    def test_medium_run_of_ten_jars(self):
        _, rendered = render_recipe("Tiramisu Medium")

        self.assertIn("53.3 g", step_text(rendered, 1))
        self.assertIn("160 g", step_text(rendered, 1))
        self.assertIn("48 g", step_text(rendered, 2))
        self.assertIn("652 g", step_text(rendered, 3))
        self.assertIn("280 g", step_text(rendered, 4))
        self.assertIn("20 g", step_text(rendered, 6))

    def test_small_run_of_ten_jars(self):
        _, rendered = render_recipe("Tiramisu Small")

        self.assertIn("35.6 g", step_text(rendered, 1))
        self.assertIn("106.7 g", step_text(rendered, 1))
        self.assertIn("32 g", step_text(rendered, 2))
        self.assertIn("434.7 g", step_text(rendered, 3))
        self.assertIn("186.7 g", step_text(rendered, 4))
        self.assertIn("13.3 g", step_text(rendered, 6))

    def test_run_totals_scale_with_the_jar_count(self):
        _, one = render_recipe("Tiramisu Large", jars=1)
        _, ten = render_recipe("Tiramisu Large", jars=10)

        self.assertIn("8 g", step_text(one, 1))
        self.assertIn("24 g", step_text(one, 1))
        self.assertIn("80 g", step_text(ten, 1))
        self.assertIn("240 g", step_text(ten, 1))

    def test_per_jar_portions_are_static_spec_text(self):
        expectations = {
            "Tiramisu Large": ("97 g", "24 g", "40 g", "3 g"),
            "Tiramisu Medium": ("70 g", "16 g", "28 g", "2 g"),
            "Tiramisu Small": ("46.7 g", "10.7 g", "18.7 g", "1.3 g"),
        }
        for item_code, (cream, syrup, savoiardi, cocoa) in expectations.items():
            _, rendered = render_recipe(item_code)
            self.assertIn(f"Each jar takes {cream} of cream", step_text(rendered, 3), item_code)
            self.assertIn(f"({savoiardi} per jar)", step_text(rendered, 4), item_code)
            self.assertIn(f"({syrup} per jar)", step_text(rendered, 4), item_code)
            self.assertIn(f"Fill each jar with {cream} of cream", step_text(rendered, 5), item_code)
            self.assertIn(f"({cocoa} per jar)", step_text(rendered, 6), item_code)

    def test_the_liquid_coffee_weigh_in_has_no_fixed_bounds(self):
        # The figure scales with the run, so a min/max would reject real runs.
        recipe, rendered = render_recipe("Tiramisu Large")
        raw = recipe["steps"][0]
        self.assertEqual("Number", raw["capture_type"])
        self.assertEqual("Liquid coffee weighed (g)", raw["capture_label"])
        self.assertNotIn("capture_min", raw)
        self.assertNotIn("capture_max", raw)

    def test_every_step_is_bilingual_and_needs_confirmation(self):
        from jarz_pos.scripts import seed_recipe_sops as seed

        for recipe in seed.TIRAMISU_SOPS:
            self.assertEqual(7, len(recipe["steps"]), recipe["item_code"])
            for step in recipe["steps"]:
                english, _, arabic = step["instruction"].partition("\n")
                self.assertTrue(english.strip() and arabic.strip(), step["title"])
                self.assertEqual(1, step["requires_confirmation"])

    def test_the_run_cocoa_step_scales_per_unit(self):
        from jarz_pos.scripts import seed_recipe_sops as seed

        recipe = next(r for r in seed.RECIPES if r["item_code"] == "Tiramisu Large")
        self.assertEqual("Per Unit", recipe["steps"][5]["scaling_mode"])

    def test_notes_record_the_owner_method_date(self):
        from jarz_pos.scripts import seed_recipe_sops as seed

        for recipe in seed.TIRAMISU_SOPS:
            self.assertIn("2026-10-08", recipe["notes"])
            self.assertIn("2026-08-08", recipe["notes"])


class TestItemsFilter(unittest.TestCase):
    def test_parse_items(self):
        from jarz_pos.scripts.seed_recipe_sops import _parse_items

        self.assertIsNone(_parse_items(None))
        self.assertIsNone(_parse_items(""))
        self.assertIsNone(_parse_items([]))
        self.assertEqual(["A", "B"], _parse_items("A, B ,,A"))
        self.assertEqual(["A", "B"], _parse_items(["A", " B", "A"]))

    def _run(self, **kwargs):
        from jarz_pos.scripts import seed_recipe_sops as seed

        with patch.object(seed, "frappe") as mock_frappe:
            mock_frappe.db.exists.return_value = True
            mock_frappe.db.get_value.return_value = None
            mock_frappe.new_doc.return_value = MagicMock(name="doc")
            result = seed.run(dry_run=True, **kwargs)
        return result, mock_frappe

    def test_default_seeds_every_recipe(self):
        from jarz_pos.scripts import seed_recipe_sops as seed

        result, _ = self._run()
        self.assertEqual(len(seed.RECIPES), len(result["created"]))

    def test_comma_separated_string_restricts_the_run(self):
        result, _ = self._run(items="Tiramisu Large,Tiramisu Small")
        self.assertEqual(["Tiramisu Large", "Tiramisu Small"], sorted(result["created"]))

    def test_a_list_restricts_the_run(self):
        result, _ = self._run(items=["Savoiardi"])
        self.assertEqual(["Savoiardi"], result["created"])

    def test_an_unknown_code_is_reported_not_silently_ignored(self):
        result, _ = self._run(items="Tiramisu Large,Nope")
        self.assertEqual(["Tiramisu Large"], result["created"])
        self.assertEqual(["Nope"], result["unmatched"])


if __name__ == "__main__":
    unittest.main()

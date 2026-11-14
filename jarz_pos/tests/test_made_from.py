"""``attach_subassembly_split``: the pick list's phantom sub-assemblies, kept apart.

ERPNext v16 explodes a phantom BOM line (Cheesecake Mix) and merges it by
item code, so one powder sugar row holds the Tiramisu coffee's sugar AND the
mix's.  The owner, 2026-11-14: show the mix as its own group with its own
ingredients, and never merge its sugar with the coffee's.  The merged total
stays the shortage gate's figure.

Shapes below are the ones the real stack returned on staging (10 Tiramisu
Large + 10 Mango Medium): powder sugar 0.2705 Kg = 0.052 direct + 0.2185 mix.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import production_planning as planning

# Per kg of Cheesecake Mix.
MIX_PER_KG = {"CHEESE": 0.5, "SUGAR": 0.1}


def _mix_rows(bom_no, company, qty, fetch_exploded=0):
	assert bom_no == "BOM-MIX"
	assert fetch_exploded == 0
	return [
		{"item_code": code, "item_name": code.title(), "uom": "Kg", "required_qty": per * qty}
		for code, per in MIX_PER_KG.items()
	]


LINES = [
	{"item_code": "Tiramisu Large", "bom_name": "BOM-TIRA", "item_qty": 10},
	{"item_code": "Mango Medium", "bom_name": "BOM-MANGO", "item_qty": 10},
]
# Mix per jar: Tiramisu 0.1 Kg, Mango 0.05 Kg -> 1.0 + 0.5 = 1.5 Kg of mix.
PHANTOMS = {
	"BOM-TIRA": [{"item_code": "MIX", "item_name": "Cheesecake Mix", "uom": "Kg", "bom_no": "BOM-MIX", "qty_per_unit": 0.1}],
	"BOM-MANGO": [{"item_code": "MIX", "item_name": "Cheesecake Mix", "uom": "Kg", "bom_no": "BOM-MIX", "qty_per_unit": 0.05}],
}


def _rollup():
	# What ERPNext hands back: the mix already exploded and merged by code.
	return {
		"components": [
			{
				"item_code": "SUGAR",
				"required_qty": 0.05 + 0.15,  # coffee 0.05 (Tiramisu only) + mix 0.15
				"contributing_lines": [
					{"line_index": 0, "item_code": "Tiramisu Large", "required_qty": 0.05 + 0.1},
					{"line_index": 1, "item_code": "Mango Medium", "required_qty": 0.05},
				],
			},
			{
				"item_code": "CHEESE",
				"required_qty": 0.75,
				"contributing_lines": [
					{"line_index": 0, "item_code": "Tiramisu Large", "required_qty": 0.5},
					{"line_index": 1, "item_code": "Mango Medium", "required_qty": 0.25},
				],
			},
			{
				"item_code": "SAV",
				"required_qty": 0.6,
				"contributing_lines": [{"line_index": 0, "item_code": "Tiramisu Large", "required_qty": 0.6}],
			},
		]
	}


class TestSubassemblySplit(unittest.TestCase):
	def _split(self, phantoms=PHANTOMS):
		rollup = _rollup()
		rows = MagicMock(side_effect=_mix_rows)
		with (
			patch.object(planning, "_resolve_phantom_bom_lines", return_value=phantoms),
			patch.object(planning, "_resolve_bom_company", return_value="Jarz Co"),
		):
			planning.attach_subassembly_split(rollup, LINES, "Jarz Co", rows)
		return rollup, {c["item_code"]: c for c in rollup["components"]}, rows

	def test_the_mix_is_one_group_for_the_whole_basket(self):
		rollup, _, _ = self._split()

		(mix,) = rollup["subassemblies"]
		self.assertEqual("MIX", mix["item_code"])
		self.assertAlmostEqual(1.5, mix["required_qty"])
		made_from = {r["item_code"]: r["required_qty"] for r in mix["made_from"]}
		self.assertAlmostEqual(0.75, made_from["CHEESE"])
		self.assertAlmostEqual(0.15, made_from["SUGAR"])
		self.assertEqual(
			[("Tiramisu Large", 1.0), ("Mango Medium", 0.5)],
			[(l["item_code"], round(l["required_qty"], 6)) for l in mix["contributing_lines"]],
		)

	def test_the_coffee_sugar_is_never_merged_with_the_mix_sugar(self):
		_, by_code, _ = self._split()

		sugar = by_code["SUGAR"]
		self.assertAlmostEqual(0.05, sugar["direct_qty"])
		# The gate's total is untouched.
		self.assertAlmostEqual(0.2, sugar["required_qty"])
		per_line = [round(l["direct_qty"], 6) for l in sugar["contributing_lines"]]
		self.assertEqual([0.05, 0.0], per_line)

	def test_a_material_only_in_the_mix_has_no_direct_share(self):
		_, by_code, _ = self._split()
		self.assertEqual(0.0, by_code["CHEESE"]["direct_qty"])

	def test_a_direct_line_keeps_all_of_it(self):
		_, by_code, _ = self._split()
		self.assertAlmostEqual(0.6, by_code["SAV"]["direct_qty"])

	def test_no_phantom_line_changes_nothing(self):
		rollup, by_code, rows = self._split(phantoms={})
		rows.assert_not_called()
		self.assertNotIn("subassemblies", rollup)
		self.assertNotIn("direct_qty", by_code["SUGAR"])

	def test_the_lookup_with_no_boms_returns_nothing(self):
		self.assertEqual({}, planning._resolve_phantom_bom_lines([]))


if __name__ == "__main__":
	unittest.main()

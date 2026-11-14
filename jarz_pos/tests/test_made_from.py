"""``attach_made_from``: what goes into a sub-assembly made fresh for the run.

The owner, 2026-11-14: the pick list should show the Cheesecake Mix's own
ingredients, summed over the basket, but never merged with the same material a
jar takes directly (the powder sugar for the Tiramisu coffee).
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import production_planning as planning

MIX_ROWS_PER_KG = [
	{"item_code": "CHEESE", "item_name": "Cream cheese", "uom": "Kg", "per_kg": 0.5},
	{"item_code": "SUGAR", "item_name": "Powder sugar", "uom": "Kg", "per_kg": 0.1},
]


def _mix_rows(bom_no, company, qty, fetch_exploded=0):
	assert bom_no == "BOM-MIX"
	assert fetch_exploded == 0
	return [
		{"item_code": r["item_code"], "item_name": r["item_name"], "uom": r["uom"], "required_qty": r["per_kg"] * qty}
		for r in MIX_ROWS_PER_KG
	]


def _components():
	return [
		{"item_code": "MIX", "item_name": "Cheesecake Mix", "required_qty": 2.0},
		{"item_code": "SUGAR", "item_name": "Powder sugar", "required_qty": 0.3},
		{"item_code": "SAV", "item_name": "Savoiardi", "required_qty": 1.0},
	]


class TestAttachMadeFrom(unittest.TestCase):
	def _attach(self, fresh):
		components = _components()
		rows = MagicMock(side_effect=_mix_rows)
		with (
			patch.object(planning, "_resolve_fresh_subassembly_boms", return_value=fresh) as mock_fresh,
			patch.object(planning, "_resolve_bom_company", return_value="Jarz Co"),
		):
			planning.attach_made_from(components, ["BOM-TIRA-L", "BOM-TIRA-M"], "Jarz Co", rows)
		return {c["item_code"]: c for c in components}, rows, mock_fresh

	def test_the_fresh_mix_carries_its_ingredients_for_the_whole_run(self):
		by_code, rows, mock_fresh = self._attach({"MIX": "BOM-MIX"})

		made_from = {r["item_code"]: r["required_qty"] for r in by_code["MIX"]["made_from"]}
		self.assertAlmostEqual(1.0, made_from["CHEESE"])
		self.assertAlmostEqual(0.2, made_from["SUGAR"])
		rows.assert_called_once()
		mock_fresh.assert_called_once_with(["BOM-TIRA-L", "BOM-TIRA-M"])

	def test_the_jar_sugar_is_never_merged_with_the_mix_sugar(self):
		by_code, _, _ = self._attach({"MIX": "BOM-MIX"})

		# The jar's own powder sugar keeps its figure; the mix's sits under the mix.
		self.assertAlmostEqual(0.3, by_code["SUGAR"]["required_qty"])
		self.assertNotIn("made_from", by_code["SUGAR"])

	def test_a_stored_subassembly_stays_one_line(self):
		by_code, _, _ = self._attach({"MIX": "BOM-MIX"})
		self.assertNotIn("made_from", by_code["SAV"])

	def test_nothing_fresh_reads_no_bom(self):
		by_code, rows, _ = self._attach({})
		rows.assert_not_called()
		self.assertTrue(all("made_from" not in c for c in by_code.values()))

	def test_the_lookup_with_no_boms_returns_nothing(self):
		self.assertEqual({}, planning._resolve_fresh_subassembly_boms([]))


if __name__ == "__main__":
	unittest.main()

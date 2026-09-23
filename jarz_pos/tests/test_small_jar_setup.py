"""Tests for the Small (147 ml) jar: the BOM derivation and the retail guard.

Two rules carry the feature:

  * the Small BOM is the flavour's Medium BOM with every filling line at 2/3 and
    the glass, lid and label swapped for their 147 items, one each, so a 9.52 kg
    mix batch fills exactly 180 Small jars;
  * a Small jar is sold only at a price a price list backs. On a Standard order it
    has no retail price, so its rate would come from the client (the POS shows 0);
    that line is refused instead of booking a free jar.

Mock-level: nothing here touches a site.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.services import invoice_creation as ic
from jarz_pos.setup import small_jar_setup as sj


def _line(item_code, qty, uom="Kg", bom_no="", do_not_explode=0):
	return SimpleNamespace(
		item_code=item_code,
		qty=qty,
		uom=uom,
		conversion_factor=1.0,
		bom_no=bom_no,
		do_not_explode=do_not_explode,
		source_warehouse=None,
	)


_GROUPS = {
	"Glass Jar": "Packaging",
	"Jar Lid": "Packaging",
	"Blueberry Jar label 212": "Labels",
	"Tiramisu Jar Label 212": "Labels",
}


def _group_of(code):
	return _GROUPS.get(code, "Raw Material")


class TestNaming(unittest.TestCase):
	def test_small_item_code(self):
		self.assertEqual(sj.small_item_code("Molten Medium"), "Molten Small")
		self.assertEqual(
			sj.small_item_code("Chocolate Hazelnut Medium"), "Chocolate Hazelnut Small"
		)
		self.assertIsNone(sj.small_item_code("Molten Large"))
		self.assertIsNone(sj.small_item_code("Medium"))

	def test_small_label_code_normalises_the_212_spellings(self):
		self.assertEqual(
			sj.small_label_code("Blueberry Jar label 212"), "Blueberry Jar Label 147"
		)
		self.assertEqual(
			sj.small_label_code("RedVelvet Jar Label 212"), "RedVelvet Jar Label 147"
		)
		self.assertEqual(sj.small_label_code("Mango  Jar Label 212"), "Mango Jar Label 147")
		self.assertIsNone(sj.small_label_code("Molten Jar Label 330"))


class TestBuildSmallBom(unittest.TestCase):
	def test_standard_mix_line_gives_180_jars_per_batch(self):
		rows, labels = sj.build_small_bom_items(
			[
				_line("Butter Biscuit", 0.05, do_not_explode=1),
				_line("Blueberry Jar label 212", 1.0, uom="Nos"),
				_line("Glass Jar", 1.0, uom="Nos"),
				_line("Jar Lid", 1.0, uom="Nos"),
				_line("Blueberry mix", 0.03, do_not_explode=1),
				_line("Cheesecake Mix", 0.079333, bom_no="BOM-Cheesecake Mix-007"),
			],
			_group_of,
		)
		by_code = {r["item_code"]: r for r in rows}
		self.assertEqual(
			set(by_code),
			{
				"Butter Biscuit",
				"Blueberry Jar Label 147",
				"Glass Jar 147",
				"Jar Lid 147",
				"Blueberry mix",
				"Cheesecake Mix",
			},
		)
		self.assertEqual(labels, {"Blueberry Jar Label 147": "Blueberry Jar label 212"})

		mix = by_code["Cheesecake Mix"]
		self.assertAlmostEqual(mix["qty"], 0.052889, places=6)
		self.assertEqual(round(9.52 / mix["qty"]), 180)
		# The sub-assembly keeps its BOM and its explode flag.
		self.assertEqual(mix["bom_no"], "BOM-Cheesecake Mix-007")
		self.assertEqual(by_code["Butter Biscuit"]["do_not_explode"], 1)
		self.assertAlmostEqual(by_code["Butter Biscuit"]["qty"], 0.033333, places=6)
		self.assertAlmostEqual(by_code["Blueberry mix"]["qty"], 0.02, places=6)

		# Packaging is one each, never scaled.
		for code in ("Glass Jar 147", "Jar Lid 147", "Blueberry Jar Label 147"):
			self.assertEqual(by_code[code]["qty"], 1.0)
			self.assertEqual(by_code[code]["uom"], "Nos")

	def test_tiramisu_keeps_its_lighter_mix_line(self):
		rows, _ = sj.build_small_bom_items(
			[
				_line("Savoiardi", 0.028, do_not_explode=1),
				_line("Tiramisu Jar Label 212", 1.0, uom="Nos"),
				_line("Glass Jar", 1.0, uom="Nos"),
				_line("Jar Lid", 1.0, uom="Nos"),
				_line("Cheesecake Mix", 0.067143, bom_no="BOM-Cheesecake Mix-007"),
			],
			_group_of,
		)
		mix = next(r for r in rows if r["item_code"] == "Cheesecake Mix")
		self.assertAlmostEqual(mix["qty"], 0.044762, places=6)

	def test_no_212_packaging_survives(self):
		rows, _ = sj.build_small_bom_items(
			[
				_line("Tiramisu Jar Label 212", 1.0, uom="Nos"),
				_line("Glass Jar", 1.0, uom="Nos"),
				_line("Jar Lid", 1.0, uom="Nos"),
			],
			_group_of,
		)
		codes = {r["item_code"] for r in rows}
		self.assertFalse(codes & {"Glass Jar", "Jar Lid", "Tiramisu Jar Label 212"})

	def test_a_medium_bom_missing_packaging_is_refused(self):
		with self.assertRaises(ValueError) as caught:
			sj.build_small_bom_items(
				[_line("Glass Jar", 1.0, uom="Nos"), _line("Cheesecake Mix", 0.079333)],
				_group_of,
			)
		self.assertIn("lid", str(caught.exception))
		self.assertIn("label", str(caught.exception))


class _Thrown(Exception):
	pass


def _throwing(message, *args, **kwargs):
	raise _Thrown(message)


class TestPriceListOnlyGuard(unittest.TestCase):
	"""A Small line whose rate would come from the client is refused."""

	def _run(self, item_group, item_data=None, **kwargs):
		item_data = item_data or {"item_code": "Molten Small", "qty": 2, "rate": 0}
		with patch("jarz_pos.services.invoice_creation.frappe") as mf:
			mf.db.exists.return_value = True
			# No Item Price, no category rate: provenance "client".
			mf.db.get_value.return_value = None
			mf.get_doc.return_value = MagicMock(
				item_name=item_data["item_code"], stock_uom="Nos", item_group=item_group
			)
			mf.throw.side_effect = _throwing
			return ic._process_regular_item(
				item_data, MagicMock(), price_list="Standard Selling", **kwargs
			)

	def test_small_on_a_standard_order_is_refused(self):
		with self.assertRaises(_Thrown) as caught:
			self._run("Small")
		self.assertIn("Molten Small", str(caught.exception))
		self.assertIn("B2B", str(caught.exception))

	def test_medium_on_a_standard_order_is_unaffected(self):
		result = self._run(
			"Medium", {"item_code": "Molten Medium", "qty": 1, "rate": 120.0}
		)
		self.assertEqual(result["rate"], 120.0)

	def test_small_on_a_free_sample_order_is_allowed(self):
		result = self._run("Small", free_of_charge_order=True)
		self.assertEqual(result["qty"], 2.0)

	def test_small_with_an_explicit_override_is_allowed(self):
		result = self._run(
			"Small",
			{"item_code": "Molten Small", "qty": 1, "rate": 0, "custom_rate_override": 30},
		)
		self.assertEqual(result["price_list_rate"], 30.0)

	def test_small_priced_by_the_list_is_allowed(self):
		with patch("jarz_pos.services.invoice_creation.frappe") as mf:
			mf.db.exists.return_value = True

			def get_value(doctype, filters, field=None, *a, **k):
				if doctype == "Item Price":
					return 30.0
				return None

			mf.db.get_value.side_effect = get_value
			mf.get_doc.return_value = MagicMock(
				item_name="Molten Small", stock_uom="Nos", item_group="Small"
			)
			mf.throw.side_effect = _throwing
			result = ic._process_regular_item(
				{"item_code": "Molten Small", "qty": 1, "rate": 0},
				MagicMock(),
				price_list="B2B Selling",
				enforce_price_list_pricing=True,
			)
		self.assertEqual(result["price_list_rate"], 30.0)


if __name__ == "__main__":
	unittest.main()

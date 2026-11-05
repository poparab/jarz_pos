"""A cashier's line discount on a bundle must reach the bundle's target price.

On 2026-11-04 a 10% discount typed on a Jarz Signature Trio line showed in the
POS cart but ACC-SINV-2026-19321 was saved at the full 480: the bundle path read
only custom_rate_override / price_list_rate and dropped discount_percentage and
discount_amount without a word.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.services import invoice_creation as ic


class TestApplyBundleLineDiscount(unittest.TestCase):
	def test_percentage_reduces_bundle_price(self):
		price, applied = ic._apply_bundle_line_discount("TRIO", 480.0, {"discount_percentage": 10})
		self.assertTrue(applied)
		self.assertAlmostEqual(price, 432.0)

	def test_amount_reduces_bundle_price(self):
		price, applied = ic._apply_bundle_line_discount("TRIO", 480.0, {"discount_amount": 30})
		self.assertTrue(applied)
		self.assertAlmostEqual(price, 450.0)

	def test_amount_wins_over_percentage_like_the_client(self):
		price, _ = ic._apply_bundle_line_discount(
			"TRIO", 480.0, {"discount_amount": 30, "discount_percentage": 50}
		)
		self.assertAlmostEqual(price, 450.0)

	def test_no_discount_leaves_price(self):
		for data in ({}, {"discount_percentage": 0}, {"discount_amount": ""}, None):
			price, applied = ic._apply_bundle_line_discount("TRIO", 480.0, data)
			self.assertFalse(applied)
			self.assertEqual(price, 480.0)

	def test_out_of_range_values_are_refused(self):
		# 100% / the full amount would hand BundleProcessor a 0 target, which it
		# refuses as "price is not set" -- refused here with a clear message instead.
		for data in (
			{"discount_percentage": 100},
			{"discount_percentage": 120},
			{"discount_percentage": -5},
			{"discount_amount": -1},
			{"discount_amount": 480},
			{"discount_amount": 500},
			{"discount_percentage": "abc"},
		):
			with self.assertRaises(frappe.ValidationError, msg=str(data)):
				ic._apply_bundle_line_discount("TRIO", 480.0, data)


class TestProcessBundleItemAppliesDiscount(unittest.TestCase):
	def _run(self, item_data):
		rows = [{"item_code": "TRIO", "is_bundle_parent": 1}, {"item_code": "RV-L", "is_bundle_child": 1}]
		with (
			patch.object(ic, "validate_bundle_configuration_by_item", return_value=(True, "", "B-1")),
			patch.object(ic, "process_bundle_for_invoice", return_value=rows) as pbi,
		):
			out = ic._process_bundle_item(
				"TRIO", 1, item_data.get("rate", 0), MagicMock(), MagicMock(), item_data=item_data
			)
		return pbi.call_args.kwargs["target_bundle_price"], out

	def test_percentage_on_bundle_line_reaches_target_price(self):
		# The exact shape the POS client sends: full price + percentage, discounted rate.
		target, out = self._run(
			{"item_code": "TRIO", "is_bundle": True, "rate": 432.0, "price_list_rate": 480.0, "discount_percentage": 10}
		)
		self.assertAlmostEqual(target, 432.0)
		self.assertTrue(all(r.get("_bundle_line_discount") for r in out))

	def test_discount_applies_on_top_of_custom_price(self):
		target, out = self._run(
			{
				"item_code": "TRIO",
				"is_bundle": True,
				"rate": 400.0,
				"price_list_rate": 450.0,
				"custom_rate_override": 450.0,
				"discount_amount": 50,
			}
		)
		self.assertAlmostEqual(target, 400.0)
		# Both flags set: [CUSTOM LINE PRICING] + [BUNDLE PRICING] are stamped from them.
		self.assertTrue(all(r.get("_bundle_custom_price") and r.get("_bundle_line_discount") for r in out))

	def test_undiscounted_bundle_is_unchanged(self):
		target, out = self._run({"item_code": "TRIO", "is_bundle": True, "rate": 480.0, "price_list_rate": 480.0})
		self.assertEqual(target, 480.0)
		self.assertFalse(any(r.get("_bundle_line_discount") for r in out))


if __name__ == "__main__":
	unittest.main()

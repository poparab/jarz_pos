"""Order prefill from a chat -- pure, no site.

Pins that only catalog codes reach the POS cart, that a bundle's flavours land
in its groups (and an unfinished bundle says so), and that the address, phone
and area come from the customer's own messages and the Territory, never from
the model's wording.

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not
installed.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace

try:  # pragma: no cover - depends on the environment
	import frappe
except ImportError:  # pragma: no cover
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	frappe._ = lambda m: m
	frappe.local = SimpleNamespace()
	frappe.session = SimpleNamespace(user="Guest")
	frappe.db = SimpleNamespace()
	frappe.log_error = lambda *a, **k: None
	frappe.get_traceback = lambda *a, **k: ""
	sys.modules["frappe"] = frappe
	_utils = types.ModuleType("frappe.utils")
	_utils.get_datetime = lambda d=None: d
	_utils.now_datetime = lambda: datetime.datetime(2026, 10, 29, 12, 0)
	_utils.nowdate = lambda: "2026-10-29"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_order as o

PRODUCTS = [
	{"id": "Chocolate Hazelnut Large", "name": "Chocolate Hazelnut Large"},
	{"id": "Lotus Medium", "name": "Lotus Medium"},
]
PAUSED = [{"id": "Mango Large", "name": "Mango Large"}]
TRIO = {
	"id": "BNDL-TRIO",
	"name": "Jarz Signature Trio",
	"erpnext_item": "Jarz Signature Trio",
	"item_groups": [
		{
			"group_key": "g1",
			"group_name": "Large Jars",
			"quantity": 3,
			"items": [
				{"id": "Chocolate Hazelnut Large", "name": "Chocolate Hazelnut Large"},
				{"id": "Mango Large", "name": "Mango Large"},
			],
		}
	],
}
CATALOG = o.build_catalog(PRODUCTS, [TRIO], PAUSED)


class ValidateOrderTest(unittest.TestCase):
	def test_items_are_catalog_codes_with_merged_quantities(self):
		lines, warnings = o.validate_order(
			{
				"items": [
					{"item_code": "chocolate hazelnut large", "qty": 1},
					{"item_code": "Chocolate Hazelnut Large", "qty": 1},
					{"item_code": "Kunafa Large", "qty": 2},
					{"item_code": "Lotus Medium", "qty": 0},
				],
				"bundles": [],
			},
			CATALOG,
		)
		self.assertEqual(
			lines,
			[
				{
					"kind": "item",
					"item_code": "Chocolate Hazelnut Large",
					"item_name": "Chocolate Hazelnut Large",
					"qty": 2,
				}
			],
		)
		self.assertIn("Not on the menu: Kunafa Large", warnings)

	def test_paused_item_stays_with_a_warning(self):
		lines, warnings = o.validate_order({"items": [{"item_code": "Mango Large", "qty": 1}]}, CATALOG)
		self.assertEqual(lines[0]["item_code"], "Mango Large")
		self.assertIn("Mango Large is paused (no stock at any branch)", warnings)

	def test_complete_bundle(self):
		lines, warnings = o.validate_order(
			{
				"bundles": [
					{
						"bundle_code": "Jarz Signature Trio",
						"selections": [{"item_code": "Chocolate Hazelnut Large", "qty": 3}],
					}
				]
			},
			CATALOG,
		)
		self.assertEqual(lines[0]["kind"], "bundle")
		self.assertEqual(lines[0]["bundle_id"], "BNDL-TRIO")
		self.assertTrue(lines[0]["complete"])
		self.assertEqual(
			lines[0]["selections"], {"g1": [{"item_code": "Chocolate Hazelnut Large", "qty": 3}]}
		)
		self.assertEqual(warnings, [])

	def test_short_or_overfull_bundle_is_left_for_staff(self):
		lines, warnings = o.validate_order(
			{"bundles": [{"bundle_code": "Jarz Signature Trio", "selections": []}]}, CATALOG
		)
		self.assertFalse(lines[0]["complete"])
		self.assertIn("Jarz Signature Trio: choose the flavours by hand", warnings)
		lines, warnings = o.validate_order(
			{
				"bundles": [
					{
						"bundle_code": "Jarz Signature Trio",
						"selections": [{"item_code": "Chocolate Hazelnut Large", "qty": 4}],
					}
				]
			},
			CATALOG,
		)
		self.assertTrue(lines[0]["complete"])
		self.assertIn("does not fit", warnings[0])

	def test_unknown_bundle_and_unclear(self):
		lines, warnings = o.validate_order(
			{"bundles": [{"bundle_code": "Party Box"}], "unclear": "size of the lotus jar"}, CATALOG
		)
		self.assertEqual(lines, [])
		self.assertIn("Not a bundle on the menu: Party Box", warnings)
		self.assertIn("Unclear: size of the lotus jar", warnings)

	def test_catalog_text_has_codes_not_prices(self):
		text = o.render_catalog(CATALOG)
		self.assertIn("Jarz Signature Trio | Jarz Signature Trio", text)
		self.assertIn("choose 3 from: Chocolate Hazelnut Large, Mango Large", text)
		self.assertIn("## Paused", text)
		self.assertNotIn("EGP", text)


class ContactChangesTest(unittest.TestCase):
	ROWS = {
		1: {"direction": "Inbound", "body": "عمارة 12 الدور 3 شقة 5 الحي السابع"},
		2: {"direction": "Inbound", "body": "رقمي الجديد 01001234567"},
		3: {"direction": "Outbound", "body": "العنوان عمارة 1"},
		4: {"direction": "Inbound", "body": "ده رقمي 01121480676"},
	}

	def test_address_is_the_customers_message_verbatim(self):
		out = o.contact_changes({"address_message": 1, "phone_message": 2}, self.ROWS, [])
		self.assertEqual(out["address"], {"text": "عمارة 12 الدور 3 شقة 5 الحي السابع"})
		self.assertEqual(out["phone"], "01001234567")

	def test_our_messages_and_known_phones_do_not_count(self):
		out = o.contact_changes({"address_message": 3, "phone_message": 4}, self.ROWS, ["+201121480676"])
		self.assertEqual(out, {"address": None, "phone": None})

	def test_missing_numbers(self):
		self.assertEqual(o.contact_changes({}, self.ROWS, []), {"address": None, "phone": None})


class ResolveAreaTest(unittest.TestCase):
	TERRITORIES = [
		{"name": "EG6OCT", "custom_territory_name_ar": "السادس من اكتوبر", "delivery_income": 60},
		{"name": "EGMASRJD", "custom_territory_name_ar": "مصر الجديدة", "delivery_income": 50},
	]

	def test_by_code_or_arabic_name(self):
		self.assertEqual(
			o.resolve_area("مصر الجديدة", self.TERRITORIES),
			{"territory": "EGMASRJD", "label": "مصر الجديدة", "delivery_income": 50.0},
		)
		self.assertEqual(o.resolve_area("eg6oct", self.TERRITORIES)["delivery_income"], 60.0)

	def test_unknown_area(self):
		self.assertIsNone(o.resolve_area("الإسكندرية", self.TERRITORIES))
		self.assertIsNone(o.resolve_area(None, self.TERRITORIES))


if __name__ == "__main__":
	unittest.main()

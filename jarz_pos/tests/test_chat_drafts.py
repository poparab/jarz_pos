"""Chat inbox AI draft rules (services/chat_drafts.py) -- pure functions, no site.

Pins the guarantees the moderator relies on:

* personal data is masked before anything reaches the model;
* prices come only from placeholders the server fills, an unknown one shows
  ``[؟]`` with a warning, and a number the model typed itself is flagged;
* an upsell is only ever an item from the configured list, never on a complaint;
* the planted wrong price is a real price raised 20-40% and is "caught" only
  when it is gone from what was sent;
* a complaint draft cannot be sent unedited; As-Is vs Edited and the cost.

Needs no chat table: it runs before ``bench migrate`` in CI. Plain
``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not
installed.
"""

import datetime
import random
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
	_utils.now_datetime = lambda: datetime.datetime(2026, 10, 16, 12, 0)
	_utils.nowdate = lambda: "2026-10-16"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_drafts as d

CTX = {
	"prices": {"JAR-NUT": 120.0, "JAR-LOT": 135.0, "BUNDLE-6": 650.0},
	"item_names": {"nutella jar": "JAR-NUT", "lotus jar": "JAR-LOT"},
	"delivery_fees": {"nasr city": 40.0, "مدينة نصر": 40.0, "maadi": 55.0},
	"free_delivery_threshold": 500.0,
	"customer_first_name": "Sara",
	"arabic": True,
}


def ctx(**overrides):
	out = dict(CTX)
	out.update(overrides)
	return out


class DigitsAndScriptTest(unittest.TestCase):
	def test_arabic_indic_digits(self):
		self.assertEqual(d.to_western_digits("٠١٢٣٤٥٦٧٨٩"), "0123456789")
		self.assertEqual(d.to_western_digits("۱۲۰"), "120")
		self.assertEqual(d.to_western_digits(None), "")

	def test_is_arabic(self):
		self.assertTrue(d.is_arabic("عايز أطلب"))
		self.assertFalse(d.is_arabic("hello 123"))


class MaskTest(unittest.TestCase):
	def test_phone_numbers(self):
		self.assertEqual(d.mask_text("call me 01111034268"), "call me [PHONE]")
		self.assertEqual(d.mask_text("+20 111 103 4268 please"), "[PHONE] please")
		self.assertEqual(d.mask_text("رقمي ٠١١١١٠٣٤٢٦٨"), "رقمي [PHONE]")

	def test_short_numbers_stay(self):
		self.assertEqual(d.mask_text("2 jars for 3 people"), "2 jars for 3 people")

	def test_email_and_link(self):
		self.assertEqual(d.mask_text("mail sara.ali@example.com"), "mail [EMAIL]")
		self.assertEqual(d.mask_text("see https://maps.app.goo.gl/abc123 here"), "see [LINK] here")

	def test_customer_names(self):
		out = d.mask_text("Hi I am Sara Ali, Sara here", names=["Sara Ali"])
		self.assertNotIn("Sara", out)
		self.assertIn("[CUSTOMER]", out)

	def test_short_name_parts_are_not_masked_alone(self):
		# "Al" (2 letters) is too short to mask on its own: it would eat words.
		self.assertEqual(d.mask_text("Also fine", names=["Al Bo"]), "Also fine")

	def test_empty(self):
		self.assertEqual(d.mask_text(None), "")


class FormatTest(unittest.TestCase):
	def test_format_egp(self):
		self.assertEqual(d.format_egp(120), "120 جنيه")
		self.assertEqual(d.format_egp(120.0, arabic=False), "120 EGP")
		self.assertEqual(d.format_egp(120.5, arabic=False), "120.50 EGP")

	def test_resolve_item_code(self):
		self.assertEqual(d.resolve_item_code("JAR-NUT", CTX), "JAR-NUT")
		self.assertEqual(d.resolve_item_code("jar-nut", CTX), "JAR-NUT")
		self.assertEqual(d.resolve_item_code("Nutella Jar", CTX), "JAR-NUT")
		self.assertIsNone(d.resolve_item_code("unknown", CTX))
		self.assertIsNone(d.resolve_item_code("", CTX))


class FillPlaceholdersTest(unittest.TestCase):
	def test_fills_price_fee_threshold_and_name(self):
		text, warnings, filled = d.fill_placeholders(
			"أهلا {{customer_first_name}}، الجار ب{{price:JAR-NUT}} والتوصيل لمدينة نصر {{delivery_fee:مدينة نصر}}"
			" ومجاني فوق {{free_delivery_threshold}}",
			CTX,
		)
		self.assertEqual(warnings, [])
		self.assertIn("Sara", text)
		self.assertIn("120 جنيه", text)
		self.assertIn("40 جنيه", text)
		self.assertIn("500 جنيه", text)
		self.assertNotIn("{{", text)
		self.assertEqual(filled, [("JAR-NUT", 120.0)])

	def test_english_reply_uses_egp(self):
		text, _w, _f = d.fill_placeholders("It is {{price:Lotus Jar}}", ctx(arabic=False))
		self.assertEqual(text, "It is 135 EGP")

	def test_case_and_spacing_tolerant(self):
		text, warnings, _f = d.fill_placeholders("{{ price : jar-nut }} / {{delivery_fee:Nasr City}}", CTX)
		self.assertEqual(warnings, [])
		self.assertEqual(text, "120 جنيه / 40 جنيه")

	def test_unknown_item_shows_marker_and_warns(self):
		text, warnings, filled = d.fill_placeholders("السعر {{price:CHEESECAKE}}", CTX)
		self.assertIn(d.UNKNOWN, text)
		self.assertEqual(len(warnings), 1)
		self.assertIn("CHEESECAKE", warnings[0])
		self.assertEqual(filled, [])

	def test_unknown_area_and_placeholder(self):
		text, warnings, _f = d.fill_placeholders("{{delivery_fee:Alexandria}} {{discount}}", CTX)
		self.assertEqual(text.count(d.UNKNOWN), 2)
		self.assertEqual(len(warnings), 2)

	def test_no_free_delivery_offer(self):
		text, warnings, _f = d.fill_placeholders(
			"{{free_delivery_threshold}}", ctx(free_delivery_threshold=None)
		)
		self.assertEqual(text, d.UNKNOWN)
		self.assertEqual(len(warnings), 1)

	def test_empty_first_name_leaves_no_gap(self):
		text, _w, _f = d.fill_placeholders(
			"Hi {{customer_first_name}}, welcome", ctx(customer_first_name="", arabic=False)
		)
		self.assertEqual(text, "Hi, welcome")

	def test_override_changes_only_that_price(self):
		text, _w, filled = d.fill_placeholders(
			"{{price:JAR-NUT}} {{price:JAR-LOT}}", CTX, overrides={"JAR-NUT": 150.0}
		)
		self.assertEqual(text, "150 جنيه 135 جنيه")
		self.assertEqual(filled, [("JAR-NUT", 150.0), ("JAR-LOT", 135.0)])


class TypedNumberTest(unittest.TestCase):
	def test_money_typed_by_the_model_is_flagged(self):
		self.assertEqual(d.typed_number_warnings("السعر 120 جنيه"), [d.TYPED_NUMBER_WARNING])
		self.assertEqual(d.typed_number_warnings("only EGP 95 today"), [d.TYPED_NUMBER_WARNING])
		self.assertEqual(d.typed_number_warnings("السعر ١٢٠ ج"), [d.TYPED_NUMBER_WARNING])
		self.assertEqual(d.typed_number_warnings("total 350 for both"), [d.TYPED_NUMBER_WARNING])

	def test_placeholders_and_harmless_numbers_pass(self):
		self.assertEqual(d.typed_number_warnings("السعر {{price:JAR-NUT}}"), [])
		self.assertEqual(d.typed_number_warnings("2 jars and 3 spoons"), [])
		self.assertEqual(d.typed_number_warnings("الجار 250 جرام"), [])
		self.assertEqual(d.typed_number_warnings("ready in 45 minutes"), [])
		self.assertEqual(d.typed_number_warnings(""), [])


class SanitizeToolInputTest(unittest.TestCase):
	CANDIDATES = ["JAR-LOT", "BUNDLE-6"]

	def test_valid_upsell_kept_with_canonical_code(self):
		clean, warnings = d.sanitize_tool_input(
			{
				"reply": " hi ",
				"intent": "order",
				"is_complaint": False,
				"upsell_item_code": "jar-lot",
				"upsell_line": "Try Lotus too",
			},
			self.CANDIDATES,
		)
		self.assertEqual(warnings, [])
		self.assertEqual(clean["reply"], "hi")
		self.assertEqual(clean["intent"], "Order")
		self.assertEqual(clean["upsell_item_code"], "JAR-LOT")
		self.assertEqual(clean["upsell_line"], "Try Lotus too")

	def test_upsell_off_the_list_is_dropped(self):
		clean, warnings = d.sanitize_tool_input(
			{"reply": "x", "intent": "Inquiry", "upsell_item_code": "JAR-NUT", "upsell_line": "buy more"},
			self.CANDIDATES,
		)
		self.assertIsNone(clean["upsell_item_code"])
		self.assertIsNone(clean["upsell_line"])
		self.assertEqual(len(warnings), 1)

	def test_line_without_code_is_dropped(self):
		clean, _w = d.sanitize_tool_input(
			{"reply": "x", "intent": "Inquiry", "upsell_line": "buy more"}, self.CANDIDATES
		)
		self.assertIsNone(clean["upsell_line"])

	def test_complaint_never_carries_an_upsell(self):
		clean, _w = d.sanitize_tool_input(
			{
				"reply": "sorry",
				"intent": "Complaint",
				"is_complaint": False,
				"upsell_item_code": "JAR-LOT",
				"upsell_line": "Try Lotus",
			},
			self.CANDIDATES,
		)
		self.assertTrue(clean["is_complaint"])
		self.assertIsNone(clean["upsell_item_code"])
		self.assertIsNone(clean["upsell_line"])

	def test_complaint_flag_alone_also_drops_upsell(self):
		clean, _w = d.sanitize_tool_input(
			{"reply": "sorry", "intent": "Other", "is_complaint": True, "upsell_item_code": "JAR-LOT"},
			self.CANDIDATES,
		)
		self.assertIsNone(clean["upsell_item_code"])

	def test_garbage_input(self):
		clean, _w = d.sanitize_tool_input(None, self.CANDIDATES)
		self.assertEqual(clean["reply"], "")
		self.assertEqual(clean["intent"], "Other")
		self.assertFalse(clean["is_complaint"])
		clean, _w = d.sanitize_tool_input({"intent": "shopping"}, [])
		self.assertEqual(clean["intent"], "Other")


class PlantedTest(unittest.TestCase):
	TEMPLATE = "الجار ب{{price:JAR-NUT}}"

	def test_mutation_raises_a_real_price_20_to_40_percent(self):
		for seed in range(25):
			result = d.plant_mutation(self.TEMPLATE, CTX, rng=random.Random(seed))
			self.assertIsNotNone(result)
			text, error, wrong = result
			value = int(wrong)
			self.assertGreater(value, 120)
			self.assertGreaterEqual(value, 140)  # 120 * 1.2 = 144, rounded to 5
			self.assertLessEqual(value, 170)  # 120 * 1.4 = 168, rounded to 5
			self.assertEqual(value % 5, 0)
			self.assertIn(f"{value} جنيه", text)
			self.assertNotIn("120", text)
			self.assertEqual(error, f"price JAR-NUT: 120 → {value}")
			self.assertEqual(d.planted_wrong_value(error), wrong)

	def test_no_price_no_mutation(self):
		self.assertIsNone(d.plant_mutation("أهلا {{customer_first_name}}", CTX, rng=random.Random(1)))
		self.assertIsNone(d.plant_mutation("{{price:UNKNOWN}}", CTX, rng=random.Random(1)))

	def test_planted_wrong_value_parsing(self):
		self.assertEqual(d.planted_wrong_value("price X: 120 → 150"), "150")
		self.assertIsNone(d.planted_wrong_value("no arrow"))
		self.assertIsNone(d.planted_wrong_value(None))

	def test_caught_only_when_the_wrong_number_is_gone(self):
		self.assertFalse(d.planted_caught("الجار ب150 جنيه", "150"))
		self.assertFalse(d.planted_caught("الجار ب١٥٠ جنيه", "150"))
		self.assertTrue(d.planted_caught("الجار ب120 جنيه", "150"))
		self.assertTrue(d.planted_caught("total 1500", "150"))
		self.assertTrue(d.planted_caught("anything", None))

	def test_round_to_5(self):
		self.assertEqual(d.round_to_5(147), 145)
		self.assertEqual(d.round_to_5(148), 150)


class ReviewRulesTest(unittest.TestCase):
	def test_levenshtein(self):
		self.assertEqual(d.levenshtein("kitten", "sitting"), 3)
		self.assertEqual(d.levenshtein("same", "same"), 0)
		self.assertEqual(d.levenshtein("", "abc"), 3)
		self.assertEqual(d.levenshtein("abc", None), 3)
		self.assertEqual(d.levenshtein("السعر 150", "السعر 120"), 1)

	def test_levenshtein_is_capped(self):
		self.assertLessEqual(d.levenshtein("a" * 5000, "b" * 5000, cap=100), 100)

	def test_decide_sent_status(self):
		self.assertEqual(d.decide_sent_status("hello there", "  hello there \n"), ("Sent As-Is", 0))
		self.assertEqual(d.decide_sent_status("hello there", "hello here"), ("Sent Edited", 1))

	def test_complaint_requires_edit(self):
		self.assertTrue(d.complaint_requires_edit(1, "sorry about that", " sorry about that "))
		self.assertTrue(d.complaint_requires_edit(True, "sorry", "sorry"))
		self.assertFalse(d.complaint_requires_edit(1, "sorry", "sorry, Sara, we will call you"))
		self.assertFalse(d.complaint_requires_edit(0, "hi", "hi"))
		self.assertFalse(d.complaint_requires_edit(None, "hi", "hi"))


class CostTest(unittest.TestCase):
	def test_haiku(self):
		self.assertAlmostEqual(d.cost_usd("claude-haiku-5-5", 1000, 500), 0.0035)

	def test_cache_tokens(self):
		self.assertAlmostEqual(d.cost_usd("claude-haiku-5-5", 1000, 500, cache_read=10000), 0.0045)
		self.assertAlmostEqual(d.cost_usd("claude-haiku-5-5", 0, 0, cache_write=1000), 0.00125)

	def test_sonnet_and_unknown_model(self):
		self.assertAlmostEqual(d.cost_usd("claude-sonnet-5-5", 1000, 1000), 0.018)
		self.assertAlmostEqual(d.cost_usd("some-new-model", 1000, 500), 0.0035)
		self.assertEqual(d.cost_usd("claude-haiku-5-5", None, None), 0.0)


PRODUCTS = [
	{"id": "Lotus Medium", "name": "Lotus Medium", "item_group": "Medium", "price": 120},
	{"id": "Redvelvet Medium", "name": "Redvelvet Medium", "item_group": "Medium", "price": 120},
	{"id": "Lotus Large", "name": "Lotus Large", "item_group": "Large", "price": 160},
	{"id": "Tiramisu Large", "name": "Tiramisu Large", "item_group": "Large", "price": 175},
	{"id": "Spoon", "name": "Spoon", "item_group": "Consumables", "price": 0},
]
BUNDLES = [
	{
		"name": "Jarz Signature Trio",
		"price": 480.0,
		"free_shipping": 1,
		"item_groups": [{"group_name": "Large", "quantity": 3}],
	},
	{
		"name": "Jarz Indulgence Five",
		"price": 640.0,
		"free_shipping": 0,
		"item_groups": [{"group_name": "Large", "quantity": 4}, {"group_name": "Medium", "quantity": 1}],
	},
	{"name": "Dead bundle", "price": 0, "item_groups": []},
]
TERRITORIES = [
	{
		"name": "EGNASRCITY",
		"delivery_income": 50,
		"parent_territory": "Egypt",
		"custom_territory_name_ar": "مدينة نصر",
	},
	{
		"name": "Zayed Hay 1",
		"delivery_income": 60,
		"parent_territory": "EGZAYED",
		"custom_territory_name_ar": "الحي الأول",
	},
	{
		"name": "Zayed Hay 2",
		"delivery_income": 60,
		"parent_territory": "EGZAYED",
		"custom_territory_name_ar": "الحي التاني",
	},
	{"name": "_B2BVALID_Territory", "delivery_income": 10, "parent_territory": "EGZAYED"},
	{
		"name": "EGISM",
		"delivery_income": 0,
		"parent_territory": "Egypt",
		"custom_territory_name_ar": "الإسماعيلية",
	},
	{"name": "EGMAADI", "delivery_income": 70, "parent_territory": "Egypt"},
]
GROUP_LABELS = {"EGZAYED": {"en": "EGZAYED", "ar": "الشيخ زايد"}, "Egypt": {"en": "Egypt", "ar": ""}}


class RenderBlocksTest(unittest.TestCase):
	def test_menu_groups_shared_price_and_skips_free_items(self):
		ar = d.render_menu(PRODUCTS, True)
		# Flavours stay in English script; the catalog's "Redvelvet" reads "Red Velvet".
		self.assertIn("• وسط 120 جنيه: Lotus، Red Velvet", ar)
		# Large has two prices, so one line per item, size suffix taken off the name.
		self.assertIn("• Lotus كبير: 160 جنيه", ar)
		self.assertIn("• Tiramisu كبير: 175 جنيه", ar)
		self.assertNotIn("Spoon", ar)
		en = d.render_menu(PRODUCTS, False)
		self.assertIn("Medium 120 EGP: Lotus, Red Velvet", en)

	def test_offers(self):
		ar = d.render_offers(BUNDLES, 500, True)
		self.assertIn("• Jarz Signature Trio (3 كبير) = 480 جنيه + توصيل مجاني 🚚", ar)
		self.assertIn("• Jarz Indulgence Five (4 كبير + 1 وسط) = 640 جنيه", ar)
		self.assertNotIn("Dead bundle", ar)
		self.assertIn("توصيل مجاني لأي طلب فوق 500 جنيه", ar)
		en = d.render_offers(BUNDLES, None, False)
		self.assertIn("+ free delivery", en)
		self.assertNotIn("above", en)

	def test_delivery_areas_fold_groups_and_drop_junk(self):
		ar = d.render_delivery_areas(TERRITORIES, GROUP_LABELS, True)
		self.assertEqual(ar, "مدينة نصر، الشيخ زايد، EGMAADI")
		en = d.render_delivery_areas(TERRITORIES, GROUP_LABELS, False)
		self.assertEqual(en, "مدينة نصر, الشيخ زايد, EGMAADI")

	def test_block_placeholders_fill_from_context(self):
		blocks = {"ar": {"menu": "• وسط 120 جنيه: لوتس", "offers": "", "delivery_areas": "مدينة نصر"}}
		text, warnings, _ = d.fill_placeholders(
			"المنيو:\n{{menu}}\nبنوصل: {{delivery_areas}}", ctx(blocks=blocks)
		)
		self.assertIn("• وسط 120 جنيه: لوتس", text)
		self.assertIn("بنوصل: مدينة نصر", text)
		self.assertEqual(warnings, [])
		text, warnings, _ = d.fill_placeholders("{{offers}}", ctx(blocks=blocks))
		self.assertEqual(text, d.UNKNOWN)
		self.assertEqual(warnings, ["No live data for {{offers}}"])
		_, warnings, _ = d.fill_placeholders("{{menu}}", ctx())
		self.assertEqual(warnings, ["No live data for {{menu}}"])

	def test_block_templates_are_not_typed_numbers(self):
		self.assertEqual(d.typed_number_warnings("الأسعار:\n{{menu}}\n{{offers}}"), [])


class ReadyAnswersAndPromptTest(unittest.TestCase):
	def test_defaults_type_no_prices(self):
		for row in d.DEFAULT_READY_ANSWERS:
			self.assertTrue(row["topic"] and row["answer"])
			self.assertEqual(d.typed_number_warnings(row["answer"]), [], row["topic"])
			for match in d._PLACEHOLDER.finditer(row["answer"]):
				self.assertIn(match.group(1), d.BLOCK_PLACEHOLDERS + ("price", "customer_first_name"))

	def test_defaults_cover_menu_offers_areas(self):
		joined = "\n".join(r["answer"] for r in d.DEFAULT_READY_ANSWERS)
		for key in d.BLOCK_PLACEHOLDERS:
			self.assertIn("{{" + key + "}}", joined)
		self.assertIn("من 30 دقيقة لحد ساعتين بالكتير", joined)

	def test_prompt_asks_for_egyptian_dialect_and_blocks(self):
		self.assertIn("عامية مصرية", d.SYSTEM_PROMPT)
		self.assertIn("Never Modern", d.SYSTEM_PROMPT)
		for key in d.BLOCK_PLACEHOLDERS:
			self.assertIn("{{" + key + "}}", d.SYSTEM_PROMPT)
		self.assertIn("من 30 دقيقة لحد ساعتين بالكتير", d.SYSTEM_PROMPT)


class ReadyAnswersSettingsTest(unittest.TestCase):
	def test_build_settings_reads_active_rows(self):
		from jarz_pos.services import chat_settings

		doc = {
			"ready_answers": [
				{"topic": " Prices ", "when_to_use": "بكام", "answer": "{{menu}}", "active": 1},
				{"topic": "Off", "answer": "x", "active": 0},
				{"topic": "", "answer": "no topic", "active": 1},
				SimpleNamespace(topic="Areas", when_to_use=None, answer="{{delivery_areas}}", active=None),
			]
		}
		out = chat_settings.build_settings(doc)
		self.assertEqual(
			out["ready_answers"],
			[
				{"topic": "Prices", "when_to_use": "بكام", "answer": "{{menu}}"},
				{"topic": "Areas", "when_to_use": "", "answer": "{{delivery_areas}}"},
			],
		)


class FirstNameTest(unittest.TestCase):
	def test_skips_phone_like_names(self):
		self.assertEqual(d.first_name("+201111034268", "Sara Ali"), "Sara")
		self.assertEqual(d.first_name("201111034268", None, "  منى  أحمد "), "منى")
		self.assertEqual(d.first_name(None, ""), "")


if __name__ == "__main__":
	unittest.main()

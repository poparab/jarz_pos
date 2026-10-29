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
import re
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
		self.assertEqual(d.typed_number_warnings("Mango: الوسط 310 سعرة، الكبير 405 سعرة"), [])
		self.assertEqual(d.typed_number_warnings("about 510 kcal"), [])
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
		# $0.10 in / $0.50 out per million tokens
		self.assertAlmostEqual(d.cost_usd("claude-haiku-5-5", 1000, 500), 0.00035)

	def test_cache_tokens(self):
		# Haiku cache read $0.01 / MTok, 5-minute write 1.25x input = $0.125 / MTok
		self.assertAlmostEqual(d.cost_usd("claude-haiku-5-5", 1000, 500, cache_read=10000), 0.00045)
		self.assertAlmostEqual(d.cost_usd("claude-haiku-5-5", 0, 0, cache_write=1000), 0.000125)

	def test_sonnet_and_unknown_model(self):
		# $2 in / $10 out; cache read is 0.05x input ($0.10), not 0.1x
		self.assertAlmostEqual(d.cost_usd("claude-sonnet-5-5", 1000, 1000), 0.012)
		self.assertAlmostEqual(d.cost_usd("claude-sonnet-5-5", 0, 0, cache_read=10000), 0.001)
		self.assertAlmostEqual(d.cost_usd("claude-sonnet-5-5", 0, 0, cache_write=1000), 0.0025)
		self.assertAlmostEqual(d.cost_usd("some-new-model", 1000, 500), 0.00035)
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

	def test_size_prices_cheapest_first(self):
		self.assertEqual(d.render_size_prices(PRODUCTS, True), "وسط 120 جنيه، كبير من 160 جنيه")
		self.assertEqual(d.render_size_prices(PRODUCTS, False), "Medium 120 EGP, Large from 160 EGP")
		self.assertEqual(d.render_size_prices([], True), "")
		blocks = {"ar": {"size_prices": d.render_size_prices(PRODUCTS[:3], True)}}
		text, warnings, _ = d.fill_placeholders("كل النكهات بنفس السعر: {{size_prices}}", ctx(blocks=blocks))
		self.assertEqual(text, "كل النكهات بنفس السعر: وسط 120 جنيه، كبير 160 جنيه")
		self.assertEqual(warnings, [])

	def test_block_templates_are_not_typed_numbers(self):
		self.assertEqual(d.typed_number_warnings("الأسعار:\n{{menu}}\n{{offers}}"), [])
		self.assertEqual(d.typed_number_warnings("Medium 212 ML و Large 330 ML"), [])


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

	def test_defaults_are_short_and_never_a_bare_block(self):
		by_topic = {r["topic"]: r["answer"] for r in d.DEFAULT_READY_ANSWERS}
		self.assertNotIn("{{", by_topic["Greeting"])
		# The price answer gives the numbers, not the whole menu.
		self.assertIn("{{size_prices}}", by_topic["Prices"])
		self.assertNotIn("{{menu}}", by_topic["Prices"])
		for topic, answer in by_topic.items():
			for key in ("menu", "offers", "delivery_areas"):
				tag = "{{" + key + "}}"
				if tag in answer:
					self.assertTrue(answer.replace(tag, "").strip(), topic)
					self.assertEqual(answer.count(tag), 1, topic)

	def test_defaults_follow_the_facts_registry(self):
		joined = "\n".join(r["answer"] for r in d.DEFAULT_READY_ANSWERS)
		for banned in ("نوتيلا", "Nutella", "كنافة", "Kunafa", "Carrot", "جرة", "100%"):
			self.assertNotIn(banned, joined)
		self.assertIsNone(re.search(r"\d+\s*(?:جم|جرام|gm?\b)", joined), "gram weight")
		self.assertIn("بتقعد 7 أيام في التلاجة من يوم ما توصلك", joined)
		self.assertIn("بتقعد 3 شهور في الفريزر", joined)
		self.assertIn("اسم الكافيه بتاعك. والـ 7 طبقات علينا.", joined)

	def test_prompt_asks_for_egyptian_dialect_and_blocks(self):
		self.assertIn("عامية مصرية", d.SYSTEM_PROMPT)
		self.assertIn("Never Modern", d.SYSTEM_PROMPT)
		for key in d.BLOCK_PLACEHOLDERS:
			self.assertIn("{{" + key + "}}", d.SYSTEM_PROMPT)
		# The delivery time is a Brand Fact now, not a sentence of the prompt.
		self.assertNotIn("من 30 دقيقة لحد ساعتين بالكتير", d.SYSTEM_PROMPT)
		self.assertIn("Delivery time", {f["topic"] for f in d.DEFAULT_FACTS})

	def test_prompt_carries_what_sells(self):
		for phrase in ("تحب وسط ولا كبير؟", "أنهي منطقة", "ONE message", "مش متواجدين", "Nutella"):
			self.assertIn(phrase, d.SYSTEM_PROMPT)


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


class ReadyAnswersCapTest(unittest.TestCase):
	"""C2: ready answers are pasted whole into the model prompt, so the table is bounded."""

	def setUp(self):
		from unittest.mock import patch

		from jarz_pos.services import chat_settings

		self.cs = chat_settings
		self.cs._ready_cap_logged.clear()
		self.addCleanup(self.cs._ready_cap_logged.clear)
		patcher = patch.object(self.cs.frappe, "log_error", create=True)
		self.log_error = patcher.start()
		self.addCleanup(patcher.stop)

	@staticmethod
	def _row(n, answer=None, **extra):
		return {
			"topic": f"Topic {n}",
			"when_to_use": "",
			"answer": answer if answer is not None else f"Answer {n}",
			**extra,
		}

	@staticmethod
	def _doc_row(n, modified="2026-10-01 10:00:00", idx=None, active=1, answer=None):
		return {
			"topic": f"Topic {n}",
			"when_to_use": "",
			"answer": answer if answer is not None else f"Answer {n}",
			"active": active,
			"modified": modified,
			"idx": idx if idx is not None else n,
		}

	def test_limits_are_the_agreed_ones(self):
		self.assertEqual(self.cs.MAX_READY_ANSWERS, 50)
		self.assertEqual(self.cs.MAX_READY_ANSWER_CHARS, 12000)

	def test_under_the_caps_nothing_changes_and_nothing_is_logged(self):
		rows = [self._row(n) for n in range(10)]
		self.assertEqual(self.cs.cap_ready_answers(rows), rows)
		self.log_error.assert_not_called()

	def test_empty_and_none(self):
		self.assertEqual(self.cs.cap_ready_answers([]), [])
		self.assertEqual(self.cs.cap_ready_answers(None), [])

	def test_at_most_fifty_answers_and_the_first_fifty_win(self):
		rows = [self._row(n) for n in range(80)]
		out = self.cs.cap_ready_answers(rows)
		self.assertEqual(len(out), 50)
		self.assertEqual(out, rows[:50])

	def test_character_cap_stops_before_the_answer_that_would_overflow(self):
		rows = [self._row(n, "x" * 1000) for n in range(30)]
		out = self.cs.cap_ready_answers(rows)
		rendered = "\n\n".join(self.cs.render_ready_answer(r) for r in out)
		self.assertLessEqual(len(rendered), 12000)
		self.assertLess(len(out), 30)
		self.assertEqual(out, rows[: len(out)])
		# The next whole answer genuinely did not fit.
		self.assertGreater(len(rendered) + 2 + len(self.cs.render_ready_answer(rows[len(out)])), 12000)

	def test_a_short_low_priority_answer_never_jumps_a_long_one(self):
		rows = [self._row(0, "x" * 6000), self._row(1, "y" * 7000), self._row(2, "short")]
		out = self.cs.cap_ready_answers(rows)
		self.assertEqual([r["topic"] for r in out], ["Topic 0"])

	def test_a_single_oversized_answer_is_cut_cleanly_not_dropped(self):
		text = "\n".join(f"line number {i} of a very long reply" for i in range(2000))
		first = self._row(0, text)
		out = self.cs.cap_ready_answers([first, self._row(1)])
		self.assertEqual(len(out), 1)
		self.assertLessEqual(len(self.cs.render_ready_answer(out[0])), 12000)
		self.assertTrue(out[0]["answer"].endswith("…"))
		# Cut at a line boundary: the kept text is whole lines plus the mark.
		kept = out[0]["answer"][:-1].split("\n")
		self.assertTrue(all(line.startswith("line number") and line.endswith("reply") for line in kept))
		# The caller's row is not mutated.
		self.assertEqual(first["answer"], text)

	def test_truncate_cleanly_prefers_a_word_boundary(self):
		out = self.cs._truncate_cleanly("alpha beta gamma delta epsilon", 20)
		self.assertLessEqual(len(out), 20)
		self.assertTrue(out.endswith("…"))
		self.assertIn(out[:-1].split(" ")[-1], {"alpha", "beta", "gamma", "delta"})
		self.assertEqual(self.cs._truncate_cleanly("short", 20), "short")
		self.assertEqual(self.cs._truncate_cleanly("abcdef", 1), "a")

	def test_dropping_answers_is_logged_once_per_outcome(self):
		rows = [self._row(n) for n in range(60)]
		self.cs.cap_ready_answers(rows)
		self.cs.cap_ready_answers(rows)
		self.cs.cap_ready_answers(rows)
		self.assertEqual(self.log_error.call_count, 1)
		message = self.log_error.call_args.args[0]
		self.assertIn("kept 50 of 60", message)
		# A different outcome is a new fact and is logged again.
		self.cs.cap_ready_answers([self._row(n) for n in range(70)])
		self.assertEqual(self.log_error.call_count, 2)

	def test_a_failing_log_does_not_break_the_cap(self):
		self.log_error.side_effect = RuntimeError("no database")
		out = self.cs.cap_ready_answers([self._row(n) for n in range(60)])
		self.assertEqual(len(out), 50)

	def test_build_settings_orders_most_recently_modified_first(self):
		doc = {
			"ready_answers": [
				self._doc_row(1, modified="2026-09-01 08:00:00"),
				self._doc_row(2, modified="2026-10-05 08:00:00"),
				self._doc_row(3, modified="2026-09-20 08:00:00"),
			]
		}
		out = self.cs.build_settings(doc)
		self.assertEqual([r["topic"] for r in out["ready_answers"]], ["Topic 2", "Topic 3", "Topic 1"])

	def test_equal_modified_falls_back_to_grid_order(self):
		doc = {"ready_answers": [self._doc_row(3, idx=3), self._doc_row(1, idx=1), self._doc_row(2, idx=2)]}
		out = self.cs.build_settings(doc)
		self.assertEqual([r["topic"] for r in out["ready_answers"]], ["Topic 1", "Topic 2", "Topic 3"])

	def test_order_is_deterministic_across_input_permutations(self):
		rows = [self._doc_row(n, modified=f"2026-10-0{1 + n % 3} 09:00:00", idx=n + 1) for n in range(9)]
		expected = [r["topic"] for r in self.cs.build_settings({"ready_answers": rows})["ready_answers"]]
		for shift in (1, 4, 7):
			rotated = rows[shift:] + rows[:shift]
			got = [r["topic"] for r in self.cs.build_settings({"ready_answers": rotated})["ready_answers"]]
			self.assertEqual(got, expected)

	def test_build_settings_skips_disabled_rows_before_counting(self):
		rows = [self._doc_row(n, active=0) for n in range(100)] + [self._doc_row(1000 + n) for n in range(5)]
		out = self.cs.build_settings({"ready_answers": rows})["ready_answers"]
		self.assertEqual(len(out), 5)
		self.assertTrue(all(r["topic"].startswith("Topic 10") for r in out))
		self.log_error.assert_not_called()

	def test_build_settings_caps_a_large_table_and_keeps_the_newest(self):
		rows = [self._doc_row(n, modified=f"2026-10-01 {n // 60:02d}:{n % 60:02d}:00") for n in range(120)]
		out = self.cs.build_settings({"ready_answers": rows})["ready_answers"]
		self.assertEqual(len(out), 50)
		self.assertEqual(out[0]["topic"], "Topic 119")
		self.assertEqual(out[-1]["topic"], "Topic 70")
		self.assertEqual(self.log_error.call_count, 1)

	def test_output_shape_is_unchanged(self):
		out = self.cs.build_settings({"ready_answers": [self._doc_row(1)]})["ready_answers"]
		self.assertEqual(set(out[0]), {"topic", "when_to_use", "answer"})

	def test_prompt_lines_are_capped_even_for_a_settings_dict_built_elsewhere(self):
		"""``build_prompt_context`` re-applies the cap; the same call it makes is checked here."""
		rows = [self._row(n, "z" * 500) for n in range(200)]
		lines = [d.render_ready_answer(r) for r in d.cap_ready_answers(rows)]
		self.assertLessEqual(len(lines), 50)
		self.assertLessEqual(len("\n\n".join(lines)), 12000)

	def test_prompt_module_uses_the_shared_helpers(self):
		self.assertIs(d.cap_ready_answers, self.cs.cap_ready_answers)
		self.assertIs(d.render_ready_answer, self.cs.render_ready_answer)

	def test_default_answers_are_untouched_by_the_cap(self):
		out = d.cap_ready_answers(d.DEFAULT_READY_ANSWERS)
		self.assertEqual(out, list(d.DEFAULT_READY_ANSWERS))


class DraftFromResponseTest(unittest.TestCase):
	def test_tool_call_wins_over_text(self):
		content = [
			SimpleNamespace(type="text", text="Here is the draft."),
			SimpleNamespace(type="tool_use", name="submit_draft", input={"reply": "hi", "intent": "Order"}),
		]
		self.assertEqual(d.draft_input_from_content(content), ({"reply": "hi", "intent": "Order"}, False))

	def test_text_only_answer_becomes_the_reply(self):
		content = [SimpleNamespace(type="text", text="  أهلاً بحضرتك 💛  ")]
		data, from_text = d.draft_input_from_content(content)
		self.assertTrue(from_text)
		self.assertEqual(data, {"reply": "أهلاً بحضرتك 💛", "intent": "Other", "is_complaint": False})

	def test_empty_response(self):
		self.assertEqual(d.draft_input_from_content([]), (None, False))
		self.assertEqual(d.draft_input_from_content([SimpleNamespace(type="text", text=" ")]), (None, False))

	def test_structured_output_json_is_the_draft(self):
		content = [
			SimpleNamespace(
				type="text", text='{"reply": "أهلاً 💛", "intent": "Order", "is_complaint": false}'
			)
		]
		self.assertEqual(
			d.draft_input_from_content(content),
			({"reply": "أهلاً 💛", "intent": "Order", "is_complaint": False}, False),
		)

	def test_answer_shape_is_enforced_and_tool_choice_never_forced(self):
		# Sonnet 5.5 / Opus 5.5 answer a forced tool_choice with a 400 (broke every draft on 2026-10-23);
		# structured output enforces the shape instead, with every key required.
		import inspect

		src = inspect.getsource(d._call_claude)
		self.assertIn('"format": {"type": "json_schema", "schema": DRAFT_SCHEMA}', src)
		self.assertNotIn("tool_choice", src)
		self.assertFalse(d.DRAFT_SCHEMA["additionalProperties"])
		self.assertEqual(sorted(d.DRAFT_SCHEMA["required"]), sorted(d.DRAFT_SCHEMA["properties"]))

	def test_thinking_has_room_for_the_tool_call(self):
		# 5.5 models think by default and thinking counts against max_tokens: 600 cut Sonnet off
		# before submit_draft. Low effort plus headroom keeps the tool call inside the budget.
		import inspect

		self.assertGreaterEqual(d.MAX_TOKENS, 2000)
		self.assertEqual(d.DRAFT_EFFORT, "low")
		self.assertIn('"effort": DRAFT_EFFORT', inspect.getsource(d._call_claude))


class FirstNameTest(unittest.TestCase):
	def test_skips_phone_like_names(self):
		self.assertEqual(d.first_name("+201111034268", "Sara Ali"), "Sara")
		self.assertEqual(d.first_name("201111034268", None, "  منى  أحمد "), "منى")
		self.assertEqual(d.first_name(None, ""), "")


NOW = datetime.datetime(2026, 10, 29, 19, 47)

VIP_GIFT = {
	"name": "BACKXQ33VV",
	"discount_type": "Percentage",
	"discount_value": 100,
	"min_order_amount": 0,
	"applies_to": "Specific Items",
	"allow_pickup": 1,
	"active_to": datetime.datetime(2026, 11, 12, 13, 21),
}


class TranscriptTest(unittest.TestCase):
	def test_lines_carry_their_time(self):
		lines = d.transcript_lines(
			[
				{
					"direction": "Inbound",
					"body": "هديه من غير اوردر ولا مع اوردر",
					"timestamp": datetime.datetime(2026, 10, 29, 19, 2),
				},
				{
					"direction": "Outbound",
					"body": "قصدك هدية مع الأوردر؟",
					"timestamp": datetime.datetime(2026, 10, 29, 19, 44),
				},
			],
			mask=True,
			names=[],
		)
		self.assertEqual(lines[0], "[Thu 29 Oct 19:02] Customer: هديه من غير اوردر ولا مع اوردر")
		self.assertTrue(lines[1].startswith("[Thu 29 Oct 19:44] Jarz: "))

	def test_no_timestamp_no_prefix(self):
		self.assertEqual(
			d.transcript_lines([{"direction": "Inbound", "body": "hi"}], False, []), ["Customer: hi"]
		)

	def test_business_suite_placeholder_is_a_marker(self):
		body = "Reply sent from Meta Business Suite (Meta does not share its text)\nتم الرد من Meta Business Suite"
		lines = d.transcript_lines([{"direction": "Outbound", "body": body}], True, [])
		self.assertEqual(lines, ["Jarz (sent from Business Suite, text not available): ..."])


class CustomerProfileTest(unittest.TestCase):
	def test_unlinked(self):
		self.assertIn("not linked", d.render_customer_profile(None, NOW))

	def test_describe_vip_gift(self):
		facts = d.describe_promo(VIP_GIFT, ["Large Jar"])
		self.assertEqual(
			facts,
			"free (100% off) on Large Jar; no minimum order; delivery fee charged as usual; "
			"pickup allowed; valid until 12 Nov 2026",
		)

	def test_describe_capped_percentage_with_minimum(self):
		promo = {
			"discount_type": "Percentage",
			"discount_value": 15,
			"max_discount_amount": 100,
			"min_order_amount": 400,
		}
		self.assertEqual(
			d.describe_promo(promo),
			"15% off up to 100 EGP on the order; minimum order 400 EGP; delivery fee charged as usual",
		)

	def test_free_delivery_has_no_delivery_fee_note(self):
		self.assertNotIn("delivery fee", d.describe_promo({"discount_type": "Free Delivery"}))

	def test_full_profile(self):
		text = d.render_customer_profile(
			{
				"area": "EGMOHANDSEEN",
				"area_ar": "المهندسين",
				"area_deliverable": True,
				"address_on_file": True,
				"total_orders": 17,
				"last_order_date": datetime.datetime(2026, 7, 22),
				"last_order_items": ["Jarz Signature Trio"],
				"usual_items": ["Lotus Large", "Mango Large"],
				"offers": [{"code": "BACKXQ33VV", "facts": "free (100% off) on Large Jar", "wording": "x"}],
				"followups": [
					{
						"step": "day50",
						"at": datetime.datetime(2026, 10, 29, 13, 21),
						"offer_code": "BACKXQ33VV",
					}
				],
			},
			NOW,
		)
		self.assertIn("{{customer_delivery_fee}}", text)
		self.assertIn("EGMOHANDSEEN / المهندسين", text)
		self.assertIn("address is on file", text)
		self.assertIn("Orders so far: 17; last on 22 Jul 2026 (99 days ago): Jarz Signature Trio.", text)
		self.assertIn("Usually orders: Lotus Large, Mango Large.", text)
		self.assertIn("Personal offer code BACKXQ33VV", text)
		self.assertIn("{{customer_offer:BACKXQ33VV}}", text)
		self.assertIn("Thu 29 Oct 13:21: Jarz sent them a 'we have not seen you in a while'", text)
		self.assertNotIn("EGP", text)

	def test_area_outside_delivery_is_not_quoted(self):
		text = d.render_customer_profile({"area": "Alexandria", "area_deliverable": False}, NOW)
		self.assertNotIn("{{delivery_fee", text)
		self.assertIn("not one of our delivery areas", text)

	def test_new_customer(self):
		self.assertIn("No orders yet", d.render_customer_profile({"total_orders": 0}, NOW))


class PausedFlavoursTest(unittest.TestCase):
	PRODUCTS = [
		{"id": "Lotus Large", "name": "Lotus Large", "item_group": "Large", "price": 160},
		{"id": "Mango Large", "name": "Mango Large", "item_group": "Large", "price": 160},
		{"id": "Mango Medium", "name": "Mango Medium", "item_group": "Medium", "price": 120},
		{"id": "Carrot cake Large", "name": "Carrot cake Large", "item_group": "Large", "price": 160},
		{"id": "Gift Card", "name": "Gift Card", "item_group": "Large", "price": 100},
		{"id": "Mango Small", "name": "Mango Small", "item_group": "Small", "price": 0},
	]
	# Branch totals on production, 2026-10-29. Gift Card is not a stock item.
	STOCK = {
		"Lotus Large": 61.0,
		"Mango Large": 0.0,
		"Mango Medium": 1.0,
		"Carrot cake Large": 0.0,
		"Mango Small": 0.0,
	}

	def test_out_of_stock_and_stray_jars_are_paused(self):
		available, paused = d.split_by_stock(self.PRODUCTS, self.STOCK)
		self.assertEqual([p["id"] for p in available], ["Lotus Large", "Gift Card"])
		self.assertEqual(d.paused_names(paused), ["Mango (Large)", "Mango (Medium)", "Carrot Cake (Large)"])

	def test_paused_flavours_leave_the_menu(self):
		available, _paused = d.split_by_stock(self.PRODUCTS, self.STOCK)
		menu = d.render_menu(available, arabic=False)
		self.assertNotIn("Mango", menu)
		self.assertNotIn("Carrot", menu)
		self.assertIn("Lotus", menu)

	def test_no_stock_data_pauses_nothing(self):
		for stock in ({}, {"Lotus Large": 0.0, "Mango Large": 0.0}):
			available, paused = d.split_by_stock(self.PRODUCTS, stock)
			self.assertEqual(len(available), len(self.PRODUCTS))
			self.assertEqual(paused, [])


class CustomerDeliveryFeeTest(unittest.TestCase):
	"""CHAT-02215: a 6th of October customer (60) was quoted Masr El Gedida's fee (50)."""

	AREA = {"customer_area": {"name": "EG6OCT", "fee": 60.0}}

	def test_customer_fee_comes_from_their_area(self):
		out, warnings, _f = d.fill_placeholders("التوصيل {{customer_delivery_fee}}", ctx(**self.AREA))
		self.assertEqual(out, "التوصيل 60 جنيه")
		self.assertEqual(warnings, [])

	def test_no_area_on_file_is_never_guessed(self):
		out, warnings, _f = d.fill_placeholders("التوصيل {{customer_delivery_fee}}", ctx())
		self.assertIn(d.UNKNOWN, out)
		self.assertIn("no delivery area on file", warnings[0])

	def test_other_area_fee_is_flagged(self):
		fees = {"egmasrjd": 50.0, "eg6oct": 60.0}
		warnings = d.delivery_fee_warnings("{{delivery_fee:EGMASRJD}}", ctx(delivery_fees=fees, **self.AREA))
		self.assertEqual(len(warnings), 1)
		self.assertIn("EGMASRJD (50 EGP)", warnings[0])
		self.assertIn("EG6OCT (60 EGP)", warnings[0])

	def test_same_fee_or_no_area_is_not_flagged(self):
		fees = {"eg6oct": 60.0, "zayed": 60.0}
		self.assertEqual(
			d.delivery_fee_warnings("{{delivery_fee:zayed}}", ctx(delivery_fees=fees, **self.AREA)), []
		)
		self.assertEqual(d.delivery_fee_warnings("{{delivery_fee:EGMASRJD}}", ctx()), [])

	def test_billable_territories(self):
		self.assertTrue(d.is_billable_territory({"is_group": 1, "delivery_income": 60}))
		self.assertTrue(d.is_billable_territory({"is_group": 0, "delivery_income": 0}))
		self.assertFalse(d.is_billable_territory({"is_group": 1, "delivery_income": 0}))
		self.assertFalse(d.is_billable_territory({"is_group": 0, "delivery_income": None}))
		self.assertFalse(d.is_billable_territory(None))

	def test_prompt_prefers_the_customer_fee(self):
		self.assertIn("{{customer_delivery_fee}}", d.SYSTEM_PROMPT)
		self.assertIn("never guess one", d.SYSTEM_PROMPT)


class OrderTotalTest(unittest.TestCase):
	"""CHAT-02215: «كدا الاجمالي 370 صح؟» could only be answered by a person."""

	CTX = {
		"prices": {"Chocolate Hazelnut Large": 160.0, "Lotus Medium": 120.0, "Jarz Signature Trio": 480.0},
		"item_names": {},
		"delivery_fees": {"eg6oct": 60.0, "egmasrjd": 50.0},
		"free_delivery_threshold": 1000.0,
		"free_shipping_codes": ["Jarz Signature Trio"],
		"customer_area": {"name": "EG6OCT", "fee": 60.0},
	}

	def fill(self, template, **extra):
		return d.fill_placeholders(template, {**self.CTX, "arabic": True, **extra})

	def test_items_plus_the_customers_delivery(self):
		out, warnings, filled = self.fill("الإجمالي {{order_total:Chocolate Hazelnut Large x2}}")
		self.assertEqual(out, "الإجمالي 320 جنيه + 60 جنيه توصيل = 380 جنيه")
		self.assertEqual(warnings, [])
		self.assertEqual(filled, [("Chocolate Hazelnut Large", 160.0)])

	def test_several_lines_and_a_named_area(self):
		out, _w, _f = self.fill("{{order_total:Chocolate Hazelnut Large x1; Lotus Medium x2 @EGMASRJD}}")
		self.assertEqual(out, "400 جنيه + 50 جنيه توصيل = 450 جنيه")

	def test_free_delivery_bundle_and_threshold(self):
		out, _w, _f = self.fill("{{order_total:Jarz Signature Trio}}")
		self.assertEqual(out, "480 جنيه + توصيل مجاني = 480 جنيه")
		out, _w, _f = self.fill("{{order_total:Chocolate Hazelnut Large x7}}")
		self.assertEqual(out, "1120 جنيه + توصيل مجاني = 1120 جنيه")

	def test_unknown_item_or_no_area(self):
		out, warnings, _f = self.fill("{{order_total:Kunafa x2}}")
		self.assertEqual(out, d.UNKNOWN)
		self.assertIn("Unknown item in order total: Kunafa", warnings)
		out, warnings, _f = self.fill("{{order_total:Lotus Medium}}", customer_area={})
		self.assertEqual(out, "120 جنيه")
		self.assertIn("delivery is not included", warnings[0])

	def test_planted_price_reaches_the_total(self):
		out, _w, _f = d.fill_placeholders(
			"{{order_total:Chocolate Hazelnut Large x2}}",
			{**self.CTX, "arabic": True},
			overrides={"Chocolate Hazelnut Large": 170.0},
		)
		self.assertEqual(out, "340 جنيه + 60 جنيه توصيل = 400 جنيه")


class CustomerOfferPlaceholderTest(unittest.TestCase):
	OFFER = "عاملينلك جار لارج هدية بالكود BACKXQ33VV لحد 12 نوفمبر."

	def test_fills_by_code_ignoring_case_and_dashes(self):
		out, warnings, _f = d.fill_placeholders(
			"عندك هدية 💛 {{customer_offer:back-xq33vv}}", ctx(customer_offers={"BACKXQ33VV": self.OFFER})
		)
		self.assertIn(self.OFFER, out)
		self.assertEqual(warnings, [])

	def test_single_offer_without_code(self):
		out, _w, _f = d.fill_placeholders(
			"{{customer_offer}}", ctx(customer_offers={"BACKXQ33VV": self.OFFER})
		)
		self.assertEqual(out, self.OFFER)

	def test_unknown_offer_is_flagged(self):
		out, warnings, _f = d.fill_placeholders("{{customer_offer:NOPE}}", ctx())
		self.assertIn(d.UNKNOWN, out)
		self.assertEqual(warnings, ["No personal offer NOPE for this customer"])


if __name__ == "__main__":
	unittest.main()

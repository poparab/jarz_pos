"""AI draft style: Arabic flavour names, short Egyptian tone, cart-size upsell, settle logic.

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not installed.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
	import frappe
except ImportError:  # pragma: no cover
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	frappe._ = lambda m: m
	frappe.local = SimpleNamespace()
	frappe.session = SimpleNamespace(user="Administrator")
	frappe.db = SimpleNamespace()
	frappe.log_error = lambda *a, **k: None
	frappe.get_traceback = lambda *a, **k: ""
	sys.modules["frappe"] = frappe
	_utils = types.ModuleType("frappe.utils")
	_utils.get_datetime = lambda d=None: d
	_utils.now_datetime = lambda: datetime.datetime(2026, 11, 16, 18, 0)
	_utils.nowdate = lambda: "2026-11-16"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_drafts as d

PRODUCTS = [
	{"id": "BB-M", "name": "Blueberry Medium", "item_group": "Medium", "price": 120},
	{"id": "RV-M", "name": "Redvelvet Medium", "item_group": "Medium", "price": 120},
	{"id": "CH-M", "name": "Chocolate Hazelnut Medium", "item_group": "Medium", "price": 120},
	{"id": "MG-L", "name": "Mango Large", "item_group": "Large", "price": 160, "name_ar": "مانجو كبير"},
]
BUNDLES = [
	{
		"erpnext_item": "Jarz Gathering Box",
		"name": "Jarz Gathering Box",
		"price": 480,
		"free_shipping": 1,
		"item_groups": [{"group_name": "Medium", "quantity": 4}],
	}
]


class FlavourNamesTest(unittest.TestCase):
	def test_map_covers_the_owner_list(self):
		expected = {
			"blueberry": "بلوبيري",
			"strawberry": "فراولة",
			"lotus": "لوتس",
			"mango": "مانجا",
			"tiramisu": "تيراميسو",
			"pistachio": "فستق",
			"molten": "مولتن",
			"red velvet": "ريد فيلفت",
			"chocolate hazelnut": "شوكلاته بندق",
			"cheesecake": "تشيز كيك",
			"kunafa": "كنافة",
			"carrot cake": "كاروت كيك",
		}
		for en, ar in expected.items():
			self.assertEqual(d.FLAVOUR_AR[en], ar)
		self.assertNotIn("نوتيلا", " ".join(d.FLAVOUR_AR.values()))

	def test_item_name_ar_wins_and_loses_its_size(self):
		self.assertEqual(d.flavour_ar("Mango", "مانجو كبير", "Large"), "مانجو")
		self.assertEqual(d.flavour_ar("Mango", None, "Large"), "مانجا")
		self.assertEqual(d.flavour_ar("Unknown Thing"), "")

	def test_arabic_menu_uses_arabic_names_english_menu_keeps_english(self):
		ar = d.render_menu(PRODUCTS, True)
		self.assertEqual(
			ar,
			"• وسط 120 جنيه\n• كبير 160 جنيه\n"
			"• النكهات: بلوبيري (وسط بس)، ريد فيلفت (وسط بس)، شوكلاته بندق (وسط بس)، مانجو (كبير بس)",
		)
		self.assertNotIn("Redvelvet", ar)
		en = d.render_menu(PRODUCTS, False)
		self.assertIn("Blueberry (Medium only), Red Velvet (Medium only), Chocolate Hazelnut", en)

	def test_flavours_sold_in_every_size_are_named_once(self):
		both = [
			{**p, "id": f"{p['id']}-{g}", "name": p["name"].replace("Medium", g), "item_group": g, "price": price}
			for g, price in (("Large", 160), ("Medium", 120))
			for p in PRODUCTS[:3]
		]
		ar = d.render_menu(both, True)
		self.assertEqual(ar, "• وسط 120 جنيه\n• كبير 160 جنيه\n• النكهات: بلوبيري، ريد فيلفت، شوكلاته بندق")
		self.assertEqual(ar.count("بلوبيري"), 1)

	def test_context_menu_lines_carry_both_names_and_red_velvet_spacing(self):
		self.assertEqual(d.display_name("Redvelvet Medium", "Medium"), "Red Velvet Medium")
		self.assertEqual(d.display_name("Blueberry Medium", "Medium"), "Blueberry Medium")
		ctx = self._context()
		self.assertIn("BB-M | Blueberry Medium / بلوبيري | Medium", ctx["text"])
		self.assertIn("RV-M | Red Velvet Medium / ريد فيلفت | Medium", ctx["text"])
		self.assertNotIn("Redvelvet", ctx["text"])
		self.assertEqual(ctx["item_names"]["بلوبيري وسط"], "BB-M")
		self.assertIn("نوتيلا = Chocolate Hazelnut", ctx["text"])
		# bundle contents without prices, and prices still resolve for placeholders
		self.assertIn("Jarz Gathering Box | Jarz Gathering Box | 4 Medium | free delivery", ctx["text"])
		self.assertEqual(ctx["prices"]["BB-M"], 120)
		text, _w, _f = d.order_total("BB-M x 2", {**ctx, "customer_area": {"fee": 45}}, True)
		self.assertIn("285", text)

	def _context(self):
		import jarz_pos.api

		pos_api = types.ModuleType("jarz_pos.api.pos")
		pos_api.get_profile_products = lambda profile: [dict(p) for p in PRODUCTS]
		pos_api.get_profile_bundles = lambda profile: BUNDLES
		fake_cache = SimpleNamespace(get_value=lambda k: None, set_value=lambda *a, **k: None)
		with (
			mock.patch.object(frappe, "cache", return_value=fake_cache, create=True),
			mock.patch.dict(sys.modules, {"jarz_pos.api.pos": pos_api}),
			mock.patch.object(sys.modules["jarz_pos.api"], "pos", pos_api, create=True),
			mock.patch.object(d, "_branch_stock", return_value={}),
			mock.patch.object(d, "_item_arabic_names", return_value={}),
			mock.patch.object(d, "_territories", return_value=[]),
			mock.patch.object(d, "_territory_group_labels", return_value={}),
			mock.patch.object(d, "_free_delivery_threshold", return_value=None),
			mock.patch.object(d, "load_brand_facts", return_value=[]),
			mock.patch.object(d, "_public_promo_lines", return_value=[]),
		):
			return d.build_context({"default_pos_profile": "P"})


class PromptStyleTest(unittest.TestCase):
	def test_arabic_flavour_rule_replaced_the_english_one(self):
		p = d.SYSTEM_PROMPT
		self.assertNotIn("Keep English brand, bundle and flavour names in English", p)
		self.assertIn("transliterated, never translated", p)
		self.assertIn("بلوبيري", p)
		self.assertIn("«صغير» means Medium", p)

	def test_short_replies_and_no_default_emoji(self):
		p = d.SYSTEM_PROMPT
		self.assertIn("at most about 15 words", p)
		self.assertIn("No closing question by default", p)
		self.assertIn("at most ONE per reply", p)
		self.assertIn("No 💛 by default, no 🚚", p)
		self.assertIn("اكيد يافندم 🥰 تحب تطلب ايه؟", p)
		self.assertNotIn("Emoji: 1-2, from 💛", p)
		self.assertGreaterEqual(p.count("Don't "), 6)

	def test_cart_size_upsell_and_totals_and_claims(self):
		p = d.SYSTEM_PROMPT
		self.assertIn("CART-SIZE rule", p)
		self.assertIn("never a generic", p)
		self.assertIn("Never write a total", p)
		self.assertNotIn("no shock-freezer", p)  # owner approved it, 2026-11-16
		self.assertIn("LATEST messages", p)
		self.assertIn("Never say a payment", p)

	def test_ready_answers_lost_the_generic_boxes_line_and_the_truck(self):
		joined = "\n".join(r["answer"] for r in d.DEFAULT_READY_ANSWERS)
		self.assertNotIn("البوكسات أوفر", joined)
		self.assertNotIn("🚚", joined)

	def test_we_are_the_team_no_team_will_check(self):
		"""Owner, 2026-11-17: «the team will check» reads as dishonest; the customer is talking to us."""
		p = d.SYSTEM_PROMPT
		self.assertIn("22. You ARE the Jarz team", p)
		self.assertIn("«هتأكد من الفرع وأرجعلك حالاً»", p)
		for gone in (
			"say the team will check the",
			"say the team confirms the total",
			"say the team will confirm it",
			"say a team member will check it",
			"promise that the team will look into it",
			"هنوصل رسالتك للفريق",
		):
			self.assertNotIn(gone, p)
		joined = "\n".join(r["answer"] for r in d.DEFAULT_READY_ANSWERS)
		self.assertNotIn("فريق", joined)

	def test_receipt_is_thanked_never_sent_to_review(self):
		p = d.SYSTEM_PROMPT
		self.assertIn("never say anyone will check or review it", p)
		self.assertIn("«تمام يافندم 🥰 شكراً، الأوردر بيتجهز»", p)
		self.assertIn('"verify payment receipt"', p)
		self.assertIn("Never say a payment", p)  # still never claims the money arrived


class TypedNumberTest(unittest.TestCase):
	def test_order_numbers_are_not_amounts(self):
		for text in ("طلب رقم 21916 اتأكد", "الأوردر #21916 جاهز", "order 21916 is ready", "رقم 21811"):
			self.assertEqual(d.typed_number_warnings(text), [], text)

	def test_real_amounts_still_warn(self):
		self.assertEqual(d.typed_number_warnings("السعر 120 جنيه"), [d.TYPED_NUMBER_WARNING])
		self.assertEqual(d.typed_number_warnings("total 350 for both"), [d.TYPED_NUMBER_WARNING])


class SettleTest(unittest.TestCase):
	def test_decision(self):
		sd = d.settle_decision
		self.assertEqual(sd("M2", "M1", 5, False)[0], "skip")  # newer inbound: its job drafts
		self.assertEqual(sd("M1", "M1", 100, True)[0], "skip")  # staff replied after
		self.assertEqual(sd("M1", "M1", 10, False), ("wait", 20))
		self.assertEqual(sd("M1", "M1", 45, False)[0], "go")
		self.assertEqual(sd("M1", None, None, False)[0], "go")

	def test_job_waits_then_drafts_only_the_latest(self):
		states = iter([("M1", 5.0, False), ("M1", 12.0, False), ("M1", 31.0, False)])
		slept = []
		with mock.patch.object(d, "_inbound_quiet_state", side_effect=lambda *_a: next(states)):
			self.assertTrue(d.settle_before_draft("C", "M1", sleep=slept.append))
		self.assertEqual(len(slept), 2)

	def test_job_stops_when_overtaken_or_answered(self):
		with mock.patch.object(d, "_inbound_quiet_state", return_value=("M2", 40.0, False)):
			self.assertFalse(d.settle_before_draft("C", "M1", sleep=lambda s: None))
		with mock.patch.object(d, "_inbound_quiet_state", return_value=("M1", 40.0, True)):
			self.assertFalse(d.settle_before_draft("C", "M1", sleep=lambda s: None))

	def test_stale_after_model_means_discard(self):
		with mock.patch.object(d, "_inbound_quiet_state", return_value=("M2", 1.0, False)):
			self.assertTrue(d.is_stale("C", "M1"))
		with mock.patch.object(d, "_inbound_quiet_state", return_value=("M1", 1.0, True)):
			self.assertTrue(d.is_stale("C", "M1"))
		with mock.patch.object(d, "_inbound_quiet_state", return_value=("M1", 1.0, False)):
			self.assertFalse(d.is_stale("C", "M1"))
		self.assertFalse(d.is_stale("C", None))

	def test_no_draft_for_reactions_and_bare_thanks(self):
		self.assertTrue(d.needs_no_draft("reaction", ""))
		for body in ("تمام", "شكرا", "Ok", "تمام يافندم", "thanks"):
			self.assertTrue(d.needs_no_draft("text", body), body)
		for body in ("تمام عايز اطلب", "ممكن اطلب اوردر؟", ""):
			self.assertFalse(d.needs_no_draft("text", body), body)

	def test_job_skips_when_settle_says_stop(self):
		with (
			mock.patch.object(d, "settle_before_draft", return_value=False),
			mock.patch.object(d, "generate_draft") as gen,
			mock.patch.object(frappe.db, "get_value", return_value=None, create=True),
		):
			self.assertIsNone(d.generate_draft_job("C", "M1"))
			gen.assert_not_called()



class LiveJobNeverSleepsTest(unittest.TestCase):
	"""The draft job shares the single short/default worker with webhook ingest: it must not wait."""

	def test_job_setting_is_zero(self):
		self.assertEqual(d.JOB_SETTLE_SECONDS, 0)

	def test_zero_settle_checks_once_without_sleeping(self):
		slept = []
		with mock.patch.object(d, "_inbound_quiet_state", return_value=("M1", 2.0, False)):
			self.assertTrue(d.settle_before_draft("C", "M1", sleep=slept.append, settle=0))
		self.assertEqual(slept, [])
		with mock.patch.object(d, "_inbound_quiet_state", return_value=("M2", 2.0, False)):
			self.assertFalse(d.settle_before_draft("C", "M1", sleep=slept.append, settle=0))


class SlotsFamiliesAreasTest(unittest.TestCase):
	"""2026-11-16 owner asks: drafts read the real delivery slots, match flavours by family,
	and know Masaken Sheraton is Heliopolis (EGMASRJD); shock-freezer is approved."""

	def test_running_slot(self):
		text = d.render_delivery_slots(
			[
				{"label": "Today, 10:00 PM - 11:30 PM", "is_current": True},
				{"label": "Today, 12:00 AM - 01:00 AM", "is_current": False},
			]
		)
		self.assertIn("A slot is running now: Today, 10:00 PM - 11:30 PM", text)
		self.assertIn("Next slots: Today, 12:00 AM - 01:00 AM", text)

	def test_closed_points_to_the_next_slot(self):
		text = d.render_delivery_slots([{"label": "Tomorrow, 01:00 PM - 02:30 PM", "is_current": False}])
		self.assertIn("No slot is running now", text)
		self.assertIn("next slot: Tomorrow, 01:00 PM - 02:30 PM", text)

	def test_no_slots_never_guesses(self):
		self.assertIn("Do not name a time", d.render_delivery_slots([]))
		self.assertIn("Do not name a time", d.render_delivery_slots(None))

	def test_rule_8_reads_the_slots_and_rule_21_matches_flavours(self):
		p = d.SYSTEM_PROMPT
		self.assertIn('"Delivery slots" block', p)
		self.assertIn("21. Flavour matching", p)
		self.assertIn("SAME family", p)

	def test_families_have_arabic_names(self):
		text = d.render_flavour_families()
		self.assertIn("Fruity: Blueberry / بلوبيري, Strawberry / فراولة, Mango / مانجا", text)
		self.assertIn("Molten Cake / مولتن", text)
		self.assertNotIn("Kunafa", text)

	def test_sheraton_priced_as_heliopolis(self):
		fees = {"egmasrjd": 50.0, "مصر الجديدة": 50.0}
		lines = d.apply_area_aliases(fees)
		self.assertEqual(fees["مساكن شيراتون"], 50.0)
		self.assertIn("مساكن شيراتون = EGMASRJD", lines)

	def test_alias_skipped_when_the_area_is_missing(self):
		fees = {}
		self.assertEqual(d.apply_area_aliases(fees), [])
		self.assertEqual(fees, {})

	def test_unreadable_message_wants_no_draft_and_hides_the_note(self):
		self.assertTrue(d.needs_no_draft("unsupported", "note to staff"))
		lines = d.transcript_lines(
			[{"direction": "Inbound", "message_type": "unsupported", "body": "WhatsApp did not pass this on"}],
			False,
			[],
		)
		self.assertNotIn("did not pass", " ".join(lines))


if __name__ == "__main__":
	unittest.main()

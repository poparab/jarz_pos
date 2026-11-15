"""Chat inbox enhancements (feature chat-link-and-categories) -- mocked, no site.

Pins:

* names: Arabic folding (أإآ→ا, ة→ه, ى→ي, tashkeel, tatweel), handle cleaning, which
  chat names may hint at a customer (never under 3 letters, never above 0.5);
* candidates: one entry per customer, reasons merged, phone beats name, equal scores
  keep the lookup order, the one-tap suggestion only from a phone reason;
* the draft call carries the contact category (schema, cleaning, no selling to a
  Supplier / Content Creator) and a hand-set category is never overwritten;
* ``set_contact_category``, the ``category`` inbox filter and its counts,
  ``create_customer_from_chat`` (existing mobile creates nothing), ``unlink_customer``,
  ``get_fact_overrides`` (shape, planted twins left out);
* the standalone classifier never raises for one chat and skips Manual ones;
* the ingest auto-link prefers the customer with the newest invoice.

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
	frappe.session = SimpleNamespace(user="Guest")
	frappe.db = SimpleNamespace()
	frappe.log_error = lambda *a, **k: None
	frappe.get_traceback = lambda *a, **k: ""
	sys.modules["frappe"] = frappe
	_utils = types.ModuleType("frappe.utils")
	_utils.get_datetime = lambda d=None: d
	_utils.now_datetime = lambda: datetime.datetime(2026, 11, 16, 12, 0)
	_utils.nowdate = lambda: "2026-11-16"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_classify as cl
from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_customer_match as m
from jarz_pos.services import chat_drafts as drafts
from jarz_pos.services import chat_ingest as ing


def _dict(**kw):
	"""A row that answers both ``row.x`` and ``row.get("x")`` like ``frappe._dict``."""

	class Row(dict):
		__getattr__ = dict.get

	return Row(**kw)


def _getdate(value=None):
	"""Like ``frappe.utils.getdate``: a date for a datetime or an ISO string."""
	if isinstance(value, datetime.datetime):
		return value.date()
	if isinstance(value, datetime.date):
		return value
	return datetime.date.fromisoformat(str(value)[:10])


def _throw(message, *a, **k):
	raise ValueError(str(message))


class NameTest(unittest.TestCase):
	def test_arabic_folding(self):
		self.assertEqual(m.normalize_name("أَحْمَد"), m.normalize_name("احمد"))
		self.assertEqual(m.normalize_name("إبراهيم"), m.normalize_name("ابراهيم"))
		self.assertEqual(m.normalize_name("آية"), m.normalize_name("ايه"))
		self.assertEqual(m.normalize_name("فاطمة"), m.normalize_name("فاطمه"))
		self.assertEqual(m.normalize_name("منى"), m.normalize_name("مني"))
		self.assertEqual(m.normalize_name("محمــــد"), "محمد")  # tatweel
		self.assertEqual(m.normalize_name("  Ahmed   ALI "), "ahmed ali")

	def test_digits_punctuation_and_empty(self):
		self.assertEqual(m.normalize_name("ahmed_ali.92"), "ahmed ali")
		self.assertEqual(m.normalize_name(None), "")
		self.assertEqual(m.normalize_name("😀"), "")

	def test_clean_handle(self):
		self.assertEqual(m.clean_handle("@ahmed.ali_92"), "Ahmed Ali")
		self.assertEqual(m.clean_handle("sara-mostafa"), "Sara Mostafa")
		self.assertEqual(m.clean_handle("AhmedAli"), "Ahmed Ali")
		self.assertEqual(m.clean_handle("منى.سمير"), "منى سمير")
		self.assertEqual(m.clean_handle("@12345"), "12345")  # nothing to clean: keep it, never empty
		self.assertEqual(m.clean_handle(""), "")

	def test_display_name_prefers_a_real_name(self):
		self.assertEqual(m.display_name("ahmed.ali"), "Ahmed Ali")
		self.assertEqual(m.display_name("Mona Samir"), "Mona Samir")
		self.assertEqual(m.display_name("+20 101 111 2222", "dina_k"), "Dina K")
		self.assertEqual(m.display_name("😀", "", None), "😀")  # nothing usable: first non-empty
		self.assertEqual(m.display_name(None, ""), "")

	def test_name_match_score(self):
		self.assertEqual(m.name_match_score("أحمد علي", "احمد علي"), 0.5)
		self.assertEqual(m.name_match_score("Ali Ahmed", "Ahmed Ali"), 0.5)  # any order
		self.assertEqual(m.name_match_score("Ahmed Ali", "Ahmed Mohamed Ali"), 0.4)  # first + last
		self.assertEqual(m.name_match_score("Ahmed", "Ahmed"), 0.3)  # one word: weak
		self.assertEqual(m.name_match_score("Ahmed", "Ahmed Ali"), 0.0)
		self.assertEqual(m.name_match_score("Al", "Al"), 0.0)  # under 3 letters
		self.assertEqual(m.name_match_score("ab1", "ab1"), 0.0)
		self.assertEqual(m.name_match_score("", "Ahmed Ali"), 0.0)
		for chat, cust in (("ahmed ali", "Ahmed Ali"), ("Sara", "Sara Mostafa"), ("منى سمير", "منى سمير")):
			self.assertLessEqual(m.name_match_score(chat, cust), 0.5)


class RankingTest(unittest.TestCase):
	def test_dedupe_merges_reasons_in_order_and_scores(self):
		ranked = m.rank_candidates(
			[
				{"name": "C1", "reasons": ["name"], "name_score": 0.5},
				{"name": "C1", "reasons": ["typed_phone"]},
				{"name": "C1", "reasons": ["phone"]},
				{"name": "C2", "reasons": ["name"], "name_score": 0.4},
			]
		)
		self.assertEqual([c["name"] for c in ranked], ["C1", "C2"])
		self.assertEqual(ranked[0]["reasons"], ["phone", "typed_phone", "name"])
		self.assertEqual(ranked[0]["score"], 0.99)  # 0.95 + 0.02 per other reason
		self.assertEqual(ranked[1]["score"], 0.4)
		self.assertNotIn("name_score", ranked[0])

	def test_phone_outranks_name_and_ties_keep_lookup_order(self):
		ranked = m.rank_candidates(
			[
				{"name": "N", "reasons": ["name"], "name_score": 0.5},
				{"name": "A", "reasons": ["phone"]},
				{"name": "B", "reasons": ["phone"]},
			]
		)
		self.assertEqual([c["name"] for c in ranked], ["A", "B", "N"])

	def test_limit_blank_names_and_zero_scores(self):
		raw = [{"name": f"C{i}", "reasons": ["phone"]} for i in range(10)]
		raw += [{"name": "", "reasons": ["phone"]}, {"name": "X", "reasons": ["name"], "name_score": 0}]
		ranked = m.rank_candidates(raw)
		self.assertEqual(len(ranked), m.MAX_CANDIDATES)
		self.assertNotIn("X", [c["name"] for c in ranked])

	def test_name_only_never_above_half(self):
		ranked = m.rank_candidates([{"name": "N", "reasons": ["name"], "name_score": 0.5}])
		self.assertLessEqual(ranked[0]["score"], 0.5)

	def test_one_tap_suggestion_needs_a_phone_reason(self):
		by_name = [{"name": "N", "reasons": ["name"], "score": 0.5}]
		same = [{"name": "S", "reasons": ["same_contact"], "score": 0.85}]
		by_phone = [{"name": "P", "reasons": ["phone", "name"], "score": 0.97}]
		by_typed = [{"name": "T", "reasons": ["typed_phone"], "score": 0.9}]
		self.assertIsNone(m.strong_suggestion(by_name))
		self.assertIsNone(m.strong_suggestion(same))
		self.assertIsNone(m.strong_suggestion([]))
		self.assertEqual(m.strong_suggestion(by_phone)["name"], "P")
		self.assertEqual(m.strong_suggestion(by_typed)["name"], "T")

	def test_local_phones(self):
		self.assertEqual(
			m.local_phones("+201011112222", ["01011112222", "201122223333"], None, ""),
			["01011112222", "01122223333"],
		)


class PhoneLookupTest(unittest.TestCase):
	def run_lookup(self, phones, customers=(), contact_rows=(), invoices=()):
		def get_all(doctype, filters=None, **kw):
			if doctype == "Customer" and "name" in (filters or {}):
				return list(filters["name"][1])
			if doctype == "Customer":
				return [c for c in customers if "mobile_no" in filters]
			return []

		def sql(query, values=None, as_dict=False):
			if "Contact Phone" in query:
				return [(r,) for r in contact_rows]
			return [_dict(customer=c, last_date=d) for c, d in invoices]

		fake_db = SimpleNamespace(has_column=lambda *a: False, sql=sql)
		with (
			mock.patch.object(frappe, "get_all", side_effect=get_all, create=True),
			mock.patch.object(frappe, "db", fake_db, create=True),
		):
			return m.customers_by_phone(phones)

	def test_contact_phone_hit_and_dedupe(self):
		got = self.run_lookup(
			["01011112222"],
			customers=["C1"],
			contact_rows=["C1", "C2"],
			invoices=[("C1", "2026-01-01"), ("C2", "2026-05-01")],
		)
		self.assertEqual(got, ["C2", "C1"])  # newest invoice first, C1 once

	def test_no_phone_no_query(self):
		self.assertEqual(self.run_lookup([]), [])
		self.assertEqual(self.run_lookup(["", None]), [])

	def test_ingest_match_prefers_the_newest_invoice(self):
		with mock.patch.object(m, "customers_by_phone", return_value=["NEW", "OLD"]):
			self.assertEqual(ing._match_customer("+201011112222"), "NEW")

	def test_ingest_match_never_raises_and_needs_a_phone(self):
		self.assertIsNone(ing._match_customer(None))
		with mock.patch.object(m, "customers_by_phone", side_effect=RuntimeError("db down")):
			self.assertIsNone(ing._match_customer("01011112222"))

	def test_find_candidates_never_raises(self):
		with mock.patch.object(m, "_conversation_row", side_effect=RuntimeError("boom")):
			self.assertEqual(m.find_candidates("CHAT-1"), [])
			self.assertIsNone(m.suggestion_for("CHAT-1"))

	def test_suggestion_is_phone_only(self):
		cand = {"name": "C1", "reasons": ["phone"], "score": 0.95}
		with mock.patch.object(m, "find_candidates", return_value=[cand]) as find:
			self.assertEqual(m.suggestion_for("CHAT-1"), cand)
		self.assertEqual(find.call_args.kwargs.get("with_name"), False)
		with mock.patch.object(m, "find_candidates", return_value=[{"name": "N", "reasons": ["name"]}]):
			self.assertIsNone(m.suggestion_for("CHAT-1"))


class PrefillTest(unittest.TestCase):
	def test_apply_answer_copies_from_the_customers_own_messages(self):
		base = {
			"customer_name": "Dina K",
			"mobile_no": "01011112222",
			"phones": ["01011112222"],
			"address": None,
			"territory": None,
			"territory_name": None,
			"location": None,
			"ai": False,
		}
		by_number = {
			1: {"direction": "Inbound", "body": "ايدي 01155556666 لو سمحت", "message_type": "text"},
			2: {"direction": "Inbound", "body": "15 شارع النصر المعادي الدور 3", "message_type": "text"},
			3: {"direction": "Outbound", "body": "العنوان 99 شارع تاني", "message_type": "text"},
			4: {"direction": "Inbound", "body": "30.0444,31.2357", "message_type": "location"},
		}
		territories = [{"name": "Maadi", "custom_territory_name_ar": "المعادي", "delivery_income": 40}]
		raw = {
			"customer_name": "Dina Khaled",
			"phone_message": 1,
			"address_message": 2,
			"location_message": 4,
			"area": "المعادي",
		}
		out = m.apply_prefill_answer(base, raw, by_number, territories)
		self.assertEqual(out["customer_name"], "Dina Khaled")
		self.assertEqual(out["phones"], ["01011112222", "01155556666"])
		self.assertEqual(out["mobile_no"], "01011112222")
		self.assertEqual(out["address"], "15 شارع النصر المعادي الدور 3")
		self.assertEqual(out["territory"], "Maadi")
		self.assertEqual(out["territory_name"], "المعادي")
		self.assertTrue(out["location"])

	def test_apply_answer_ignores_staff_messages_and_unknown_areas(self):
		base = {"customer_name": "X Y", "mobile_no": None, "phones": [], "address": None, "ai": False}
		by_number = {1: {"direction": "Outbound", "body": "العنوان 99 شارع 01011112222"}}
		raw = {
			"customer_name": None,
			"phone_message": 1,
			"address_message": 1,
			"location_message": None,
			"area": "Atlantis",
		}
		out = m.apply_prefill_answer(base, raw, by_number, [{"name": "Maadi"}])
		self.assertEqual(out["customer_name"], "X Y")
		self.assertEqual(out["phones"], [])
		self.assertIsNone(out["address"])
		self.assertIsNone(out.get("territory"))

	def test_ai_failure_returns_the_basic_form_with_an_error(self):
		basic = {"customer_name": "Dina", "mobile_no": None, "phones": [], "ai": False}
		with (
			mock.patch.object(m, "basic_prefill", return_value=dict(basic)),
			mock.patch("jarz_pos.services.chat_settings.get_chat_settings", side_effect=RuntimeError("x")),
			mock.patch.object(cc, "safe_log_error"),
		):
			out = m.ai_prefill("CHAT-1")
		self.assertFalse(out["ai"])
		self.assertTrue(out["ai_error"])
		self.assertEqual(out["customer_name"], "Dina")

	def test_prefill_schema_is_strict(self):
		self.assertEqual(set(m.PREFILL_SCHEMA["required"]), set(m.PREFILL_SCHEMA["properties"]))
		self.assertFalse(m.PREFILL_SCHEMA["additionalProperties"])


class DraftCategoryTest(unittest.TestCase):
	def test_schema_requires_category_and_reason(self):
		props, required = drafts.DRAFT_SCHEMA["properties"], drafts.DRAFT_SCHEMA["required"]
		self.assertEqual(
			props["contact_category"]["enum"], ["Customer", "Supplier", "Content Creator", "Other"]
		)
		self.assertIn("contact_category", required)
		self.assertIn("contact_category_reason", required)
		self.assertEqual(set(required), set(props))  # strict: every key required
		self.assertTrue(any(t.get("type") == "null" for t in props["contact_category_reason"]["anyOf"]))

	def test_prompt_has_the_category_rule(self):
		for needle in ("18. Who is writing", "Supplier", "Content Creator", "contact_category_reason"):
			self.assertIn(needle, drafts.SYSTEM_PROMPT)

	def test_clean_category(self):
		self.assertEqual(drafts.clean_contact_category("supplier"), "Supplier")
		self.assertEqual(drafts.clean_contact_category(" Content_Creator "), "Content Creator")
		self.assertIsNone(drafts.clean_contact_category("Vendor"))
		self.assertIsNone(drafts.clean_contact_category(None))

	def test_sanitize_keeps_a_customer_upsell(self):
		clean, _w = drafts.sanitize_tool_input(
			{
				"reply": "hi",
				"intent": "Inquiry",
				"upsell_item_code": "BOX",
				"upsell_line": "try the box",
				"contact_category": "Customer",
				"contact_category_reason": None,
			},
			["BOX"],
		)
		self.assertEqual(clean["contact_category"], "Customer")
		self.assertEqual(clean["upsell_item_code"], "BOX")
		self.assertIsNone(clean["contact_category_reason"])

	def test_supplier_and_creator_never_get_an_upsell_and_need_a_human(self):
		for category in ("Supplier", "Content Creator"):
			clean, _w = drafts.sanitize_tool_input(
				{
					"reply": "thanks, we will pass this to the team",
					"intent": "Other",
					"upsell_item_code": "BOX",
					"upsell_line": "try the box",
					"needs_human_reason": None,
					"contact_category": category,
					"contact_category_reason": "  Offers\npackaging boxes  ",
				},
				["BOX"],
			)
			self.assertIsNone(clean["upsell_item_code"])
			self.assertIsNone(clean["upsell_line"])
			self.assertIn(category, clean["needs_human_reason"])
			self.assertEqual(clean["contact_category_reason"], "Offers packaging boxes")

	def test_supplier_keeps_the_models_own_human_reason(self):
		clean, _w = drafts.sanitize_tool_input(
			{
				"reply": "x",
				"intent": "Other",
				"needs_human_reason": "price list",
				"contact_category": "Supplier",
			},
			[],
		)
		self.assertEqual(clean["needs_human_reason"], "price list")

	def test_missing_or_bad_category_is_none_and_reason_dropped(self):
		for data in (
			{"reply": "x"},
			{"reply": "x", "contact_category": "Banana", "contact_category_reason": "r"},
		):
			clean, _w = drafts.sanitize_tool_input(data, [])
			self.assertIsNone(clean["contact_category"])
			self.assertIsNone(clean["contact_category_reason"])

	def test_reason_is_capped(self):
		clean, _w = drafts.sanitize_tool_input(
			{"reply": "x", "contact_category": "Other", "contact_category_reason": "w" * 400}, []
		)
		self.assertEqual(len(clean["contact_category_reason"]), drafts.CATEGORY_REASON_CHARS)

	def test_ai_never_overwrites_manual(self):
		self.assertEqual(drafts.category_updates("Manual", "Supplier", "why"), {})
		self.assertEqual(
			drafts.category_updates("AI", "Supplier", "why"),
			{
				"contact_category": "Supplier",
				"contact_category_source": "AI",
				"contact_category_reason": "why",
			},
		)
		self.assertEqual(drafts.category_updates(None, "Other", None)["contact_category_reason"], None)
		self.assertEqual(drafts.category_updates("", None), {})
		self.assertEqual(drafts.category_updates("", "nonsense"), {})


class SummaryShapeTest(unittest.TestCase):
	def test_summary_carries_the_category(self):
		for field in ("contact_category", "contact_category_source", "contact_category_reason"):
			self.assertIn(field, cc.SUMMARY_FIELDS)
		row = _dict(
			name="CHAT-1",
			channel="WhatsApp",
			contact_category="Supplier",
			contact_category_source="AI",
			contact_category_reason="Offers boxes",
		)
		with (
			mock.patch.object(cc, "_customer_name", return_value=None),
			mock.patch.object(cc, "full_name", return_value=None),
		):
			out = cc.serialize_summary(row, False, datetime.datetime(2026, 11, 16))
		self.assertEqual(out["contact_category"], "Supplier")
		self.assertEqual(out["contact_category_source"], "AI")
		self.assertEqual(out["contact_category_reason"], "Offers boxes")

	def test_unclassified_is_null(self):
		with (
			mock.patch.object(cc, "_customer_name", return_value=None),
			mock.patch.object(cc, "full_name", return_value=None),
		):
			out = cc.serialize_summary(_dict(name="CHAT-1"), False, datetime.datetime(2026, 11, 16))
		self.assertIsNone(out["contact_category"])
		self.assertIsNone(out["contact_category_source"])
		self.assertIsNone(out["contact_category_reason"])


class ApiTest(unittest.TestCase):
	def setUp(self):
		try:
			from jarz_pos.api import chat
		except Exception as exc:  # pragma: no cover - stub frappe lacks the api deps
			self.skipTest(f"jarz_pos.api.chat not importable here: {exc}")
		self.chat = chat
		self.stack = mock.patch.object(chat, "_gate")
		self.stack.start()
		self.addCleanup(self.stack.stop)
		throw = mock.patch.object(frappe, "throw", side_effect=_throw, create=True)
		throw.start()
		self.addCleanup(throw.stop)

	def conv(self, **kw):
		return _dict(**{"name": "CHAT-1", "customer": None, **kw})

	# ── set_contact_category ──

	def test_set_category_marks_manual(self):
		set_value = mock.Mock()
		with (
			mock.patch.object(self.chat, "_conversation_or_throw", return_value=self.conv()),
			mock.patch.object(frappe, "db", SimpleNamespace(set_value=set_value), create=True),
			mock.patch.object(cc, "load_summary", return_value={"name": "CHAT-1"}),
			mock.patch.object(cc, "publish_chat_update") as publish,
		):
			out = self.chat.set_contact_category("CHAT-1", "Supplier")
		values = set_value.call_args.args[2]
		self.assertEqual(values["contact_category"], "Supplier")
		self.assertEqual(values["contact_category_source"], "Manual")
		self.assertIsNone(values["contact_category_reason"])
		self.assertEqual(out, {"conversation": {"name": "CHAT-1"}})
		publish.assert_called_once()

	def test_set_category_empty_clears_back_to_ai(self):
		set_value = mock.Mock()
		with (
			mock.patch.object(self.chat, "_conversation_or_throw", return_value=self.conv()),
			mock.patch.object(frappe, "db", SimpleNamespace(set_value=set_value), create=True),
			mock.patch.object(cc, "load_summary", return_value={}),
			mock.patch.object(cc, "publish_chat_update"),
		):
			self.chat.set_contact_category("CHAT-1", "")
		self.assertEqual(
			set_value.call_args.args[2],
			{"contact_category": None, "contact_category_source": None, "contact_category_reason": None},
		)

	def test_set_category_rejects_unknown_values(self):
		set_value = mock.Mock()
		with (
			mock.patch.object(self.chat, "_conversation_or_throw", return_value=self.conv()),
			mock.patch.object(frappe, "db", SimpleNamespace(set_value=set_value), create=True),
		):
			for bad in ("Vendor", "supplier", "customer "):
				if bad.strip() in cc.CONTACT_CATEGORIES:
					continue
				with self.assertRaises(ValueError):
					self.chat.set_contact_category("CHAT-1", bad)
		set_value.assert_not_called()

	# ── get_conversations ──

	def list_filters(self, **kwargs):
		with (
			mock.patch.object(frappe, "get_all", return_value=[], create=True) as get_all,
			mock.patch.object(
				frappe, "db", SimpleNamespace(count=lambda *a, **k: 0, sql=lambda *a, **k: []), create=True
			),
			mock.patch.object(cc, "pending_draft_conversations", return_value=set()),
			# cc.now() reads System Settings through the real frappe.db on a site.
			mock.patch.object(cc, "now", return_value=datetime.datetime(2026, 11, 16, 12, 0)),
		):
			out = self.chat.get_conversations(**kwargs)
		calls = [c for c in get_all.call_args_list if c.args[:1] == (cc.CONVERSATION,)]
		return calls[0].kwargs["filters"], out

	def test_category_filter_exact_for_supplier(self):
		filters, _out = self.list_filters(status="all", category="Supplier")
		self.assertIn([cc.CONVERSATION, "contact_category", "=", "Supplier"], filters)

	def test_customer_filter_includes_unclassified(self):
		filters, _out = self.list_filters(status="all", category="Customer")
		self.assertIn(
			[cc.CONVERSATION, "contact_category", "not in", ["Supplier", "Content Creator", "Other"]], filters
		)

	def test_no_category_means_no_filter_and_unknown_is_refused(self):
		filters, out = self.list_filters(status="all")
		self.assertEqual(filters, [])
		self.assertEqual(out["category_counts"], {"Supplier": 0, "Content Creator": 0})
		with self.assertRaises(ValueError):
			self.list_filters(status="all", category="Vendor")

	def test_category_counts_come_from_open_chats(self):
		def sql(query, *a, **k):
			self.assertIn("status != 'Closed'", query)
			return [("Supplier", 3), ("Content Creator", 1)]

		with mock.patch.object(frappe, "db", SimpleNamespace(sql=sql), create=True):
			self.assertEqual(self.chat._category_counts(), {"Supplier": 3, "Content Creator": 1})
		with mock.patch.object(
			frappe, "db", SimpleNamespace(), create=True
		):  # column/db missing: zero, no raise
			self.assertEqual(self.chat._category_counts(), {"Supplier": 0, "Content Creator": 0})

	# ── suggestion on the thread ──

	def test_thread_suggestion_only_when_unlinked(self):
		cand = {"name": "C1", "reasons": ["phone"]}
		with mock.patch.object(m, "suggestion_for", return_value=cand):
			self.assertEqual(self.chat._customer_suggestion(self.conv()), cand)
			self.assertIsNone(self.chat._customer_suggestion(_dict(name="CHAT-1", customer="C9")))
		with mock.patch.object(m, "suggestion_for", side_effect=RuntimeError("x")):
			self.assertIsNone(self.chat._customer_suggestion(self.conv()))

	# ── create_customer_from_chat / unlink ──

	def test_existing_mobile_creates_nothing(self):
		fake_module = types.ModuleType("jarz_pos.api.customer")
		fake_module.create_customer = mock.Mock()
		cand = {"name": "CUST-1", "customer_name": "Ahmed Ali", "reasons": ["phone"], "score": 0.95}
		with (
			mock.patch.object(self.chat, "_conversation_or_throw", return_value=self.conv()),
			mock.patch.dict(sys.modules, {"jarz_pos.api.customer": fake_module}),
			mock.patch.object(m, "customers_by_phone", return_value=["CUST-1"]) as lookup,
			mock.patch.object(m, "candidates_for", return_value=[cand]),
			mock.patch.object(cc, "load_summary", return_value={"name": "CHAT-1"}),
			mock.patch.object(self.chat, "_link") as link,
		):
			out = self.chat.create_customer_from_chat(
				"CHAT-1", "Ahmed Ali", "+201011112222", "15 Nasr St", "Maadi"
			)
		fake_module.create_customer.assert_not_called()
		link.assert_not_called()
		self.assertEqual(out, {"existing": cand, "conversation": {"name": "CHAT-1"}})
		self.assertEqual(lookup.call_args.args[0], ["01011112222"])  # looked up in the local form

	def test_new_mobile_creates_then_links(self):
		fake_module = types.ModuleType("jarz_pos.api.customer")
		fake_module.create_customer = mock.Mock(return_value={"name": "CUST-9"})
		with (
			mock.patch.object(self.chat, "_conversation_or_throw", return_value=self.conv()),
			mock.patch.dict(sys.modules, {"jarz_pos.api.customer": fake_module}),
			mock.patch.object(m, "customers_by_phone", return_value=[]),
			mock.patch.object(self.chat, "_link", return_value={"name": "CHAT-1"}) as link,
			mock.patch.object(cc, "customer_card", return_value={"name": "CUST-9"}),
		):
			out = self.chat.create_customer_from_chat(
				"CHAT-1", "  Dina   K ", "01155556666", "5 Tahrir St", "Maadi", location_link="", latitude=""
			)
		kwargs = fake_module.create_customer.call_args.kwargs
		self.assertEqual(kwargs["customer_name"], "Dina K")
		self.assertEqual(kwargs["mobile_no"], "01155556666")
		self.assertEqual(kwargs["territory_id"], "Maadi")
		self.assertIsNone(kwargs["location_link"])
		self.assertIsNone(kwargs["latitude"])
		link.assert_called_once_with("CHAT-1", "CUST-9")
		self.assertEqual(
			out, {"conversation": {"name": "CHAT-1"}, "customer": {"name": "CUST-9"}, "created": True}
		)

	def test_missing_fields_are_refused_before_any_lookup(self):
		with (
			mock.patch.object(self.chat, "_conversation_or_throw", return_value=self.conv()),
			mock.patch.object(m, "customers_by_phone") as lookup,
		):
			for args in (("", "01011112222", "a", "t"), ("n", "", "a", "t"), ("n", "01011112222", " ", "t")):
				with self.assertRaises(ValueError):
					self.chat.create_customer_from_chat("CHAT-1", *args)
		lookup.assert_not_called()

	def test_unlink_clears_customer_and_last_order(self):
		set_value = mock.Mock()
		with (
			mock.patch.object(self.chat, "_conversation_or_throw", return_value=self.conv(customer="C1")),
			mock.patch.object(frappe, "db", SimpleNamespace(set_value=set_value), create=True),
			mock.patch.object(cc, "load_summary", return_value={"name": "CHAT-1", "customer": None}),
			mock.patch.object(cc, "publish_chat_update") as publish,
		):
			out = self.chat.unlink_customer("CHAT-1")
		self.assertEqual(set_value.call_args.args[2], {"customer": None, "last_order": None})
		self.assertEqual(out, {"conversation": {"name": "CHAT-1", "customer": None}})
		publish.assert_called_once()

	# ── get_fact_overrides ──

	def test_fact_overrides_shape_and_filters(self):
		creation = datetime.datetime(2026, 11, 14, 16, 20, 5)
		drafts_rows = [
			_dict(
				name="DRAFT-1",
				conversation="CHAT-1",
				creation=creation,
				owner="mona@x.com",
				moderator_hint="tell her delivery is 1 hour and 150",
				fact_conflicts="1 hour delivery → we say 2-3 hours\n150 → Trio is 165",
				status="Sent",
				final_text="",
				draft_text="Delivery in 1 hour",
			)
		]

		def get_all(doctype, **kw):
			if doctype == cc.DRAFT:
				self.draft_kwargs = kw
				return drafts_rows
			return [_dict(name="CHAT-1", contact_name="Sara", channel="WhatsApp")]

		fake_utils = SimpleNamespace(
			getdate=_getdate,
			add_days=lambda d, n: d + datetime.timedelta(days=n),
		)
		with (
			mock.patch.object(frappe, "get_all", side_effect=get_all, create=True),
			mock.patch.object(frappe, "utils", fake_utils, create=True),
			mock.patch.object(frappe, "db", SimpleNamespace(count=lambda *a, **k: 12), create=True),
			mock.patch.object(cc, "now", return_value=datetime.datetime(2026, 11, 16, 9, 0)),
			mock.patch.object(cc, "full_name", return_value="Mona"),
		):
			out = self.chat.get_fact_overrides()
		row = out["rows"][0]
		self.assertEqual(
			row,
			{
				"draft": "DRAFT-1",
				"conversation": "CHAT-1",
				"contact_name": "Sara",
				"channel": "WhatsApp",
				"created_at": "2026-11-14 16:20:05",
				"requested_by": "mona@x.com",
				"requested_by_name": "Mona",
				"moderator_hint": "tell her delivery is 1 hour and 150",
				"conflicts": ["1 hour delivery → we say 2-3 hours", "150 → Trio is 165"],
				"status": "Sent",
				"final_text": None,
				"draft_text": "Delivery in 1 hour",
			},
		)
		self.assertEqual((out["has_more"], out["total"]), (False, 12))
		filters = self.draft_kwargs["filters"]
		self.assertIn([cc.DRAFT, "fact_conflicts", "is", "set"], filters)
		self.assertIn([cc.DRAFT, "is_planted", "=", 0], filters)  # the planted twin is left out
		self.assertIn([cc.DRAFT, "creation", ">=", "2026-10-17 00:00:00"], filters)  # default: 30 days
		self.assertIn([cc.DRAFT, "creation", "<", "2026-11-17 00:00:00"], filters)  # today included
		self.assertEqual(self.draft_kwargs["order_by"], "creation desc")

	def test_fact_overrides_pages_and_has_more(self):
		many = [
			_dict(
				name=f"D{i}",
				conversation="CHAT-1",
				creation=datetime.datetime(2026, 11, 1),
				owner="a@x",
				moderator_hint=None,
				fact_conflicts="a → b",
				status="Pending",
				final_text=None,
				draft_text="t",
			)
			for i in range(3)
		]

		def get_all(doctype, **kw):
			return many if doctype == cc.DRAFT else []

		fake_utils = SimpleNamespace(
			getdate=_getdate,
			add_days=lambda d, n: d + datetime.timedelta(days=n),
		)
		with (
			mock.patch.object(frappe, "get_all", side_effect=get_all, create=True),
			mock.patch.object(frappe, "utils", fake_utils, create=True),
			mock.patch.object(frappe, "db", SimpleNamespace(count=lambda *a, **k: 3), create=True),
			mock.patch.object(cc, "now", return_value=datetime.datetime(2026, 11, 16, 9, 0)),
			mock.patch.object(cc, "full_name", return_value=None),
		):
			out = self.chat.get_fact_overrides(from_date="2026-11-01", to_date="2026-11-02", limit=2)
		self.assertEqual(len(out["rows"]), 2)
		self.assertTrue(out["has_more"])
		self.assertIsNone(out["rows"][0]["contact_name"])
		self.assertIsNone(out["rows"][0]["moderator_hint"])


class GenerateDraftWritesTest(unittest.TestCase):
	"""``generate_draft`` with everything around the model call mocked."""

	def run_draft(self, source, hint, answer):
		inserted, set_calls = [], []
		conv = _dict(
			name="CHAT-1",
			contact_name="Box Co",
			customer=None,
			channel="WhatsApp",
			customer_summary=None,
			contact_category_source=source,
		)
		fake_db = SimpleNamespace(
			get_value=lambda *a, **k: conv,
			set_value=lambda *a, **k: set_calls.append(a),
		)
		call_result = {
			"input": answer,
			"from_text": False,
			"stop_reason": "end_turn",
			"latency_ms": 5,
			"input_tokens": 1,
			"output_tokens": 1,
			"cache_read_tokens": 0,
			"cache_write_tokens": 0,
		}
		context = {"text": "ctx", "candidates": ["BOX"], "approved_topics": []}
		with (
			mock.patch.object(frappe, "db", fake_db, create=True),
			mock.patch.object(frappe, "get_all", return_value=[], create=True),
			mock.patch(
				"jarz_pos.services.chat_settings.get_chat_settings", return_value={"anthropic_api_key": "k"}
			),
			mock.patch.object(drafts, "build_context", return_value=context),
			mock.patch.object(drafts, "customer_profile", return_value=None),
			mock.patch.object(drafts, "render_customer_profile", return_value=""),
			mock.patch.object(drafts, "_other_chat_parts", return_value=[]),
			mock.patch.object(drafts, "_latest_inbound", return_value="MSG-1"),
			mock.patch.object(drafts, "_call_claude", return_value=call_result),
			mock.patch.object(drafts, "_insert_draft", side_effect=lambda v: inserted.append(v) or "DRAFT-1"),
			mock.patch.object(drafts.chat_media, "image_blocks", return_value=[]),
			mock.patch.object(drafts.chat_media, "user_content", side_effect=lambda text, imgs: text),
			mock.patch.object(cc, "now", return_value=datetime.datetime(2026, 11, 16, 12, 0)),
			mock.patch.object(cc, "supersede_live_drafts"),
			mock.patch.object(cc, "publish_chat_update"),
			mock.patch.object(cc, "load_draft", return_value={}),
		):
			drafts.generate_draft("CHAT-1", hint=hint)
		return inserted, set_calls

	ANSWER = {
		"reply": "شكراً لتواصلك، هنوصل رسالتك للفريق",
		"intent": "Other",
		"is_complaint": False,
		"upsell_item_code": "BOX",
		"upsell_line": "x",
		"needs_human_reason": None,
		"fact_conflicts": [],
		"contact_category": "Supplier",
		"contact_category_reason": "Offers packaging boxes",
	}

	def test_category_written_and_no_upsell_for_a_supplier(self):
		inserted, set_calls = self.run_draft(None, None, dict(self.ANSWER))
		values = set_calls[0][2]
		self.assertEqual(values["contact_category"], "Supplier")
		self.assertEqual(values["contact_category_source"], "AI")
		self.assertEqual(values["contact_category_reason"], "Offers packaging boxes")
		self.assertIsNone(inserted[-1]["upsell_item"])
		self.assertTrue(inserted[-1]["needs_human_reason"])
		self.assertIsNone(inserted[-1]["moderator_hint"])

	def test_manual_category_is_left_alone(self):
		_inserted, set_calls = self.run_draft("Manual", None, dict(self.ANSWER))
		values = set_calls[0][2]
		self.assertNotIn("contact_category", values)
		self.assertEqual(values["intent"], "Other")

	def test_moderator_hint_is_stored_trimmed(self):
		answer = {**self.ANSWER, "fact_conflicts": [{"moderator_said": "1 hour", "our_fact": "2-3 hours"}]}
		inserted, _set = self.run_draft(
			None, "tell her" + chr(10) + " delivery is 1 hour " + "x" * 600, answer
		)
		self.assertTrue(inserted[-1]["moderator_hint"].startswith("tell her delivery is 1 hour"))
		self.assertEqual(len(inserted[-1]["moderator_hint"]), drafts.HINT_CHARS)
		self.assertEqual(inserted[-1]["fact_conflicts"], "1 hour → 2-3 hours")

	def test_a_hint_with_no_conflict_is_still_stored(self):
		inserted, _set = self.run_draft(None, "mention the cake", dict(self.ANSWER))
		self.assertEqual(inserted[-1]["moderator_hint"], "mention the cake")
		self.assertEqual(inserted[-1]["fact_conflicts"], "")


class ClassifierTest(unittest.TestCase):
	def test_schema_matches_the_draft_enum(self):
		schema = cl._schema()
		self.assertEqual(schema["properties"]["contact_category"]["enum"], list(cc.CONTACT_CATEGORIES))
		self.assertEqual(set(schema["required"]), set(schema["properties"]))
		self.assertFalse(schema["additionalProperties"])

	def test_clean_result(self):
		self.assertEqual(
			cl.clean_result({"contact_category": "Supplier", "contact_category_reason": " Sells  boxes "}),
			{"category": "Supplier", "reason": "Sells boxes"},
		)
		self.assertEqual(
			cl.clean_result({"contact_category": "Customer", "contact_category_reason": None}),
			{"category": "Customer", "reason": None},
		)
		self.assertIsNone(cl.clean_result({"contact_category": "Vendor"}))
		self.assertIsNone(cl.clean_result(None))
		self.assertIsNone(cl.clean_result("Supplier"))
		long = cl.clean_result({"contact_category": "Other", "contact_category_reason": "x" * 500})
		self.assertEqual(len(long["reason"]), 140)

	def run_one(self, conv_row, answer=None, call_error=None):
		set_value = mock.Mock()
		fake_db = SimpleNamespace(
			get_value=lambda *a, **k: conv_row if a[0] == cc.CONVERSATION else None,
			set_value=set_value,
			count=lambda *a, **k: 0,
		)
		messages = [{"direction": "Inbound", "message_type": "text", "body": "hello we sell boxes"}]
		call = mock.Mock(side_effect=call_error) if call_error else mock.Mock(return_value=answer)
		with (
			mock.patch.object(frappe, "db", fake_db, create=True),
			mock.patch.object(frappe, "get_all", return_value=list(messages), create=True),
			mock.patch(
				"jarz_pos.services.chat_settings.get_chat_settings",
				return_value={"anthropic_api_key": "k", "mask_personal_data": True},
			),
			mock.patch.object(drafts, "transcript_lines", return_value=["Customer: hello we sell boxes"]),
			mock.patch.object(cl, "_call", call),
			mock.patch.object(cc, "safe_log_error"),
		):
			out = cl.classify_conversation("CHAT-1")
		return out, set_value, call

	def test_writes_the_category_from_the_ai(self):
		conv = _dict(
			name="CHAT-1",
			contact_name="Box Co",
			customer=None,
			channel="WhatsApp",
			contact_category_source=None,
		)
		out, set_value, _call = self.run_one(
			conv, {"contact_category": "Supplier", "contact_category_reason": "Sells packaging"}
		)
		self.assertTrue(out["written"])
		self.assertEqual(out["category"], "Supplier")
		self.assertEqual(
			set_value.call_args.args[2],
			{
				"contact_category": "Supplier",
				"contact_category_source": "AI",
				"contact_category_reason": "Sells packaging",
			},
		)

	def test_manual_is_not_read_or_overwritten(self):
		conv = _dict(
			name="CHAT-1",
			contact_name="x",
			customer=None,
			channel="WhatsApp",
			contact_category_source="Manual",
		)
		out, set_value, call = self.run_one(conv, {"contact_category": "Supplier"})
		self.assertFalse(out["written"])
		call.assert_not_called()
		set_value.assert_not_called()

	def test_a_failing_call_never_raises(self):
		conv = _dict(
			name="CHAT-1", contact_name="x", customer=None, channel="WhatsApp", contact_category_source=None
		)
		out, set_value, _call = self.run_one(conv, call_error=RuntimeError("API down"))
		self.assertFalse(out["written"])
		self.assertIn("API down", out["error"])
		set_value.assert_not_called()

	def test_an_unusable_answer_writes_nothing(self):
		conv = _dict(
			name="CHAT-1", contact_name="x", customer=None, channel="WhatsApp", contact_category_source=None
		)
		out, set_value, _call = self.run_one(conv, {"contact_category": "Vendor"})
		self.assertFalse(out["written"])
		set_value.assert_not_called()

	def test_dry_run_only_counts(self):
		with (
			mock.patch.object(cl, "backlog_names", return_value=[f"C{i}" for i in range(30)]),
			mock.patch.object(cl, "classify_conversation") as one,
		):
			out = cl.classify_backlog(limit=300, days=60, dry_run=True)
		one.assert_not_called()
		self.assertEqual(out["found"], 30)
		self.assertTrue(out["dry_run"])
		self.assertEqual(len(out["sample"]), cl.DRY_RUN_SAMPLE)

	def test_default_is_a_dry_run(self):
		with (
			mock.patch.object(cl, "backlog_names", return_value=["C1"]),
			mock.patch.object(cl, "classify_conversation") as one,
		):
			self.assertTrue(cl.classify_backlog()["dry_run"])
		one.assert_not_called()

	def test_real_run_continues_past_failures_and_counts(self):
		outcomes = {
			"C1": {"written": True, "category": "Supplier"},
			"C2": {"written": False, "error": "boom"},
			"C3": {"written": False, "error": "Set by hand"},
			"C4": {"written": True, "category": "Supplier"},
			"C5": {"written": True, "category": "Content Creator"},
		}
		commit = mock.Mock()
		with (
			mock.patch.object(cl, "backlog_names", return_value=list(outcomes)),
			mock.patch.object(cl, "classify_conversation", side_effect=lambda n: outcomes[n]),
			mock.patch.object(frappe, "db", SimpleNamespace(commit=commit), create=True),
		):
			out = cl.classify_backlog(limit=10, days=30, dry_run=False)
		self.assertEqual(out["classified"], 3)
		self.assertEqual(out["errors"], 1)  # "Set by hand" is not an error
		self.assertEqual(out["by_category"], {"Supplier": 2, "Content Creator": 1})
		commit.assert_called()

	def test_backlog_selects_unclassified_recent_chats(self):
		captured = {}

		def get_all(doctype, **kw):
			captured.update(kw, doctype=doctype)
			return ["C1"]

		fake_utils = SimpleNamespace(add_days=lambda d, n: d - datetime.timedelta(days=-n))
		with (
			mock.patch.object(frappe, "get_all", side_effect=get_all, create=True),
			mock.patch.object(frappe, "utils", fake_utils, create=True),
			mock.patch.object(cc, "now", return_value=datetime.datetime(2026, 11, 16)),
		):
			self.assertEqual(cl.backlog_names(limit=50, days=60), ["C1"])
		self.assertEqual(captured["doctype"], cc.CONVERSATION)
		self.assertEqual(captured["filters"]["contact_category"], ["is", "not set"])
		self.assertEqual(captured["filters"]["last_inbound_at"][0], ">=")
		self.assertEqual(captured["limit_page_length"], 50)

	def test_enqueue_uses_the_long_queue_and_never_raises(self):
		with mock.patch.object(frappe, "enqueue", create=True) as enqueue:
			self.assertTrue(cl.enqueue_backlog(limit=100, days=30))
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertFalse(enqueue.call_args.kwargs["dry_run"])
		with (
			mock.patch.object(frappe, "enqueue", side_effect=RuntimeError("redis"), create=True),
			mock.patch.object(cc, "safe_log_error"),
		):
			self.assertFalse(cl.enqueue_backlog())


if __name__ == "__main__":
	unittest.main()

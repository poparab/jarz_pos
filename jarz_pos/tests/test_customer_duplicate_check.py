"""Warn-before-create duplicate check and the no-letters name guard.

Production, 2026-11-22: Woo order 22309 was booked on a NEW customer named "..."
(phone 01007023214) although the same person already existed as "مصطفي حسين"
(phone 01018689687, 5 orders) at essentially the same address. The primary-phone
hard block in ``create_customer`` could not see it (different number), so the
POS now asks ``check_customer_duplicates`` first, and ``create_customer`` refuses
a name with no letters.

The pure part (``jarz_pos.utils.customer_identity``) needs no site. The endpoint
tests mock the database layer, in the style of test_b2b_customer_search_filter.
"""

import re
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from jarz_pos.utils import customer_identity as ci

EXISTING_22309 = "حدايق الزيتون شارع العزيز بالله عمارة شركة الباسم الدور الرابع شقة الباب الحديد"
NEW_22309 = "محافظه القاهره حدايق الزيتون شارع العزيز بالله عمارة شركة الباسم الدور الرابع شقة الباب الحديد"


def _like(pattern: str, value: str) -> bool:
	"""Python model of SQL LIKE ('%' any run, '_' one character)."""
	regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
	return re.fullmatch(regex, value, flags=re.DOTALL) is not None


# ── pure: normalisation ─────────────────────────────────────────────────


class TestNormalizeText(unittest.TestCase):
	def test_folds_arabic_letter_variants(self):
		self.assertEqual(ci.normalize_text("أإآ ى ة ؤ ئ"), "ااا ي ه و ي")

	def test_arabic_indic_digits_become_ascii(self):
		self.assertEqual(ci.normalize_text("عمارة ١٢ شقة ۳"), "عماره 12 شقه 3")

	def test_strips_tashkeel_tatweel_and_punctuation(self):
		self.assertEqual(ci.normalize_text("حَدائِق،  الـــزيتون - (شارع)"), "حدايق الزيتون شارع")

	def test_casefolds_latin(self):
		self.assertEqual(ci.normalize_text("Nasr  CITY"), "nasr city")

	def test_none_is_empty(self):
		self.assertEqual(ci.normalize_text(None), "")


# ── pure: address match ─────────────────────────────────────────────────


class TestAddressMatch(unittest.TestCase):
	def test_order_22309_pair_matches(self):
		self.assertTrue(ci.address_match(NEW_22309, EXISTING_22309))
		self.assertTrue(ci.address_match(EXISTING_22309, NEW_22309))

	def test_unrelated_address_in_same_district_does_not_match(self):
		other = "حدايق الزيتون شارع طومان باي عمارة 12 الدور الثاني شقة 5"
		self.assertFalse(ci.address_match(other, EXISTING_22309))
		self.assertFalse(ci.address_match(NEW_22309, other))

	def test_vague_same_street_address_does_not_match(self):
		# Real production false alarms from the first 22309 run: other customers
		# who only gave the street and district.
		for vague in ("٣ شارع العزيز بالله حدائق الزيتون", "الزيتون شارع العزيز بالله"):
			self.assertFalse(ci.address_match(NEW_22309, vague), vague)
			self.assertFalse(ci.address_match(vague, EXISTING_22309), vague)

	def test_arabic_digit_and_letter_variants_match(self):
		stored = "حدايق الزيتون شارع العزيز بالله عماره شركه الباسم الدور 4 شقه 8"
		typed = "حدائق الزيتون، ش العزيز بالله، عمارة شركة الباسم، الدور ٤ شقة ٨"
		self.assertTrue(ci.address_match(typed, stored))

	def test_tashkeel_and_tatweel_do_not_break_a_match(self):
		typed = "حَدائِق الزيـــتون شارع العزيز بالله عمارة شركة الباسم الدور الرابع"
		self.assertTrue(ci.address_match(typed, EXISTING_22309))

	def test_same_generic_words_different_street_do_not_match(self):
		a = "شارع النزهة عمارة 5 الدور 2 شقة 3"
		b = "شارع الحجاز عمارة 5 الدور 2 شقة 3"
		self.assertFalse(ci.address_match(a, b))

	def test_short_street_name_inside_a_long_address_is_not_enough(self):
		self.assertFalse(ci.address_match("شارع مصطفى النحاس", "مدينة نصر شارع مصطفى النحاس عمارة 40 الدور 3"))

	def test_too_short_addresses_are_ignored(self):
		self.assertFalse(ci.address_match("Talabat", "Talabat"))
		self.assertFalse(ci.address_match("", EXISTING_22309))

	def test_address_text_joins_both_lines(self):
		self.assertEqual(ci.address_text(" a ", "b"), "a b")
		self.assertEqual(ci.address_text("a", None), "a")


class TestAddressLikePatterns(unittest.TestCase):
	def test_patterns_find_the_existing_22309_address(self):
		patterns = ci.address_like_patterns(NEW_22309)
		self.assertTrue(patterns)
		self.assertLessEqual(len(patterns), ci.LIKE_TOKENS)
		for pattern in patterns:
			with self.subTest(pattern=pattern):
				self.assertTrue(_like(pattern, EXISTING_22309))

	def test_governorate_words_are_not_used_for_preselection(self):
		# The existing record has no "القاهره"; preselecting on it would miss it.
		for pattern in ci.address_like_patterns(NEW_22309):
			self.assertNotIn("لق", pattern.replace("_", ""))

	def test_ambiguous_letters_are_wildcarded(self):
		for pattern in ci.address_like_patterns("حدائق الزيتون شارع العزيز بالله"):
			with self.subTest(pattern=pattern):
				self.assertFalse(set(pattern) & set("اأإآيىهةوؤئ"))
		# "حدائق" typed with hamza still preselects a stored "حدايق".
		self.assertTrue(
			any(_like(p, "حدايق الزيتون") for p in ci.address_like_patterns("حدائق الزيتون العزيز"))
		)

	def test_short_input_gives_no_patterns(self):
		self.assertEqual(ci.address_like_patterns("مدينة"), [])
		self.assertEqual(ci.address_like_patterns(None), [])


# ── pure: names and ranking ─────────────────────────────────────────────


class TestCustomerName(unittest.TestCase):
	def test_names_without_letters_are_invalid(self):
		for name in ("...", "-", "123", "؟؟", "ـــ", "٣٤", " . - "):
			with self.subTest(name=name):
				self.assertTrue(ci.is_invalid_customer_name(name))

	def test_real_names_are_valid(self):
		for name in ("مصطفي حسين", "مصطفى", "Ahmed", "أحمد 2", "Cafe 21", "Zoë"):
			with self.subTest(name=name):
				self.assertFalse(ci.is_invalid_customer_name(name))

	def test_blank_is_not_flagged(self):
		for name in (None, "", "   "):
			with self.subTest(name=name):
				self.assertFalse(ci.is_invalid_customer_name(name))

	def test_names_match_is_normalised(self):
		self.assertTrue(ci.names_match("مصطفى حسين", "مصطفي  حسين"))
		self.assertFalse(ci.names_match("", ""))
		self.assertFalse(ci.names_match("مصطفي", "مصطفي حسين"))


class TestReasons(unittest.TestCase):
	def test_name_alone_is_never_reportable(self):
		self.assertFalse(ci.is_reportable(["name"]))
		self.assertTrue(ci.is_reportable(["name", "address"]))

	def test_reasons_are_ordered_strongest_first(self):
		self.assertEqual(
			ci.order_reasons(["name", "address", "secondary_phone", "phone", "address"]),
			["phone", "secondary_phone", "address", "name"],
		)


# ── endpoint (mocked database) ──────────────────────────────────────────


def _customer_row(name, customer_name):
	return {
		"name": name,
		"customer_name": customer_name,
		"mobile_no": "01018689687",
		"customer_primary_address": f"{name}-Shipping",
		"customer_primary_contact": f"{name}-Contact",
		"territory": "EGZAYTOUN",
		"customer_group": "Individual",
	}


class TestCheckCustomerDuplicates(unittest.TestCase):
	def _run(self, *, by_phone=None, by_secondary=None, by_address=None, rows=None, stats=None, **kwargs):
		"""Call the endpoint (``kwargs``) with each lookup stubbed.

		``by_phone`` / ``by_secondary``: customers the primary / secondary number finds.
		``by_address``: ``{customer: address_line1}`` the address lookup finds.
		"""
		from jarz_pos.api import customer as api

		rows = rows or []

		def _by_phone(number, key_prefix):
			return list((by_phone if key_prefix == "dup_phone" else by_secondary) or [])

		def _sql(query, params=None, as_dict=False):
			wanted = set((params or {}).get("names") or ())
			return [r for r in rows if r["name"] in wanted]

		with ExitStack() as stack:
			stack.enter_context(patch.object(api, "_duplicate_customers_by_phone", side_effect=_by_phone))
			stack.enter_context(
				patch.object(api, "_duplicate_customers_by_address", return_value=dict(by_address or {}))
			)
			stack.enter_context(patch.object(api, "_customer_order_stats", return_value=dict(stats or {})))
			stack.enter_context(
				patch.object(
					api, "_customer_search_fields", return_value=(["`name`", "`customer_name`"], False)
				)
			)
			stack.enter_context(patch.object(api, "_augment_customer_with_territory"))
			sql = stack.enter_context(patch.object(api.frappe.db, "sql", side_effect=_sql))
			result = api.check_customer_duplicates(**kwargs)
		return result, sql

	def test_empty_input_returns_empty_without_throwing(self):
		result, sql = self._run()
		self.assertEqual(result, {"matches": [], "name_invalid": False})
		sql.assert_not_called()

	def test_dots_name_is_flagged_but_does_not_throw(self):
		result, _ = self._run(customer_name="...")
		self.assertEqual(result, {"matches": [], "name_invalid": True})

	def test_order_22309_address_match_returns_the_existing_customer(self):
		result, _ = self._run(
			by_address={"CUST-0001": EXISTING_22309},
			rows=[_customer_row("CUST-0001", "مصطفي حسين")],
			stats={"CUST-0001": (5, "2026-11-01")},
			customer_name="...",
			mobile_no="01007023214",
			address=NEW_22309,
		)
		self.assertTrue(result["name_invalid"])
		self.assertEqual(len(result["matches"]), 1)
		match = result["matches"][0]
		self.assertEqual(match["name"], "CUST-0001")
		self.assertEqual(match["match_reasons"], ["address"])
		self.assertEqual(match["matched_address"], EXISTING_22309)
		self.assertEqual(match["order_count"], 5)
		self.assertEqual(match["last_order_date"], "2026-11-01")
		# Same keys a search_customers row carries, so the POS can select it.
		for key in (
			"customer_name",
			"mobile_no",
			"customer_primary_address",
			"customer_primary_contact",
			"territory",
			"customer_group",
			"credit_allowed",
		):
			self.assertIn(key, match)

	def test_secondary_phone_match(self):
		result, _ = self._run(
			by_secondary=["CUST-0002"],
			rows=[_customer_row("CUST-0002", "Someone")],
			customer_name="Other Name",
			mobile_no="01007023214",
			secondary_mobile="01018689687",
		)
		self.assertEqual([m["name"] for m in result["matches"]], ["CUST-0002"])
		self.assertEqual(result["matches"][0]["match_reasons"], ["secondary_phone"])
		self.assertIsNone(result["matches"][0]["matched_address"])
		self.assertEqual(result["matches"][0]["order_count"], 0)
		self.assertIsNone(result["matches"][0]["last_order_date"])

	def test_name_is_only_a_supporting_reason(self):
		result, _ = self._run(
			by_phone=["CUST-0003"],
			rows=[_customer_row("CUST-0003", "مصطفى حسين")],
			customer_name="مصطفي حسين",
			mobile_no="01018689687",
		)
		self.assertEqual(result["matches"][0]["match_reasons"], ["phone", "name"])

	def test_name_only_never_matches(self):
		# No phone and no address hit: a same-named customer is never looked up.
		result, sql = self._run(
			rows=[_customer_row("CUST-0004", "مصطفي حسين")],
			customer_name="مصطفي حسين",
			mobile_no="0100",
			address="x",
		)
		self.assertEqual(result["matches"], [])
		sql.assert_not_called()

	def test_strongest_first_and_capped_at_five(self):
		rows = [_customer_row(f"CUST-A{i}", f"A{i}") for i in range(6)] + [_customer_row("CUST-P", "P")]
		result, _ = self._run(
			by_phone=["CUST-P"],
			by_address={f"CUST-A{i}": EXISTING_22309 for i in range(6)},
			rows=rows,
			stats={"CUST-A3": (9, "2026-10-01")},
			mobile_no="01018689687",
		)
		names = [m["name"] for m in result["matches"]]
		self.assertEqual(len(names), 5)
		self.assertEqual(names[0], "CUST-P")  # phone beats address
		self.assertEqual(names[1], "CUST-A3")  # then the customer with more orders

	def test_same_secondary_as_primary_is_not_searched_twice(self):
		from jarz_pos.api import customer as api

		calls = []
		with (
			patch.object(
				api, "_duplicate_customers_by_phone", side_effect=lambda n, k: calls.append(k) or []
			),
			patch.object(api, "_duplicate_customers_by_address", return_value={}),
		):
			api.check_customer_duplicates(mobile_no="01018689687", secondary_mobile="+201018689687")
		self.assertEqual(calls, ["dup_phone"])


class TestDuplicatePhoneLookup(unittest.TestCase):
	def test_partial_numbers_give_no_terms(self):
		from jarz_pos.api import customer as api

		for number in (None, "", "0100", "0101868"):
			with self.subTest(number=number):
				self.assertEqual(api._duplicate_phone_terms(number), [])

	def test_full_number_uses_the_national_tail(self):
		from jarz_pos.api import customer as api

		self.assertIn("1018689687", api._duplicate_phone_terms("+201018689687"))

	def test_condition_covers_customer_contact_and_address(self):
		from jarz_pos.api import customer as api

		params = {}
		with patch.object(api.frappe.db, "has_column", side_effect=lambda dt, col: col in ("phone", "mobile_no")):
			condition = api._customer_phone_condition(
				["1018689687"], params, key_prefix="dup_phone", include_addresses=True
			)
		self.assertEqual(params, {"dup_phone_0": "%1018689687%"})
		self.assertIn("`mobile_no` LIKE %(dup_phone_0)s", condition)
		self.assertIn("`tabContact Phone`", condition)
		self.assertIn("`tabAddress`", condition)
		self.assertIn("ad.`phone` LIKE %(dup_phone_0)s", condition)

	def test_search_customers_condition_is_unchanged(self):
		from jarz_pos.api import customer as api

		params = {}
		with patch.object(api.frappe.db, "has_column", return_value=True):
			condition = api._customer_phone_condition(["1018689687"], params)
		self.assertEqual(params, {"phone_term_0": "%1018689687%"})
		self.assertNotIn("`tabAddress`", condition)


# ── create_customer name guard ──────────────────────────────────────────


class TestCreateCustomerNameGuard(unittest.TestCase):
	def _create(self, customer_name):
		from jarz_pos.api import customer as api

		def _throw(message, *args, **kwargs):
			raise RuntimeError(message)

		with (
			patch.object(api.frappe, "throw", side_effect=_throw) as throw,
			patch.object(api.frappe, "log_error"),
			patch.object(api.frappe, "logger"),
			patch.object(api.frappe, "get_all", side_effect=RuntimeError("reached phone guard")) as get_all,
		):
			with self.assertRaises(RuntimeError):
				api.create_customer(
					customer_name=customer_name,
					mobile_no="01007023214",
					customer_primary_address=NEW_22309,
					territory_id="EGZAYTOUN",
				)
		return throw.call_args_list[0][0][0], get_all

	def test_dots_name_is_rejected_before_any_lookup(self):
		message, get_all = self._create("...")
		self.assertIn("has no letters", message)
		get_all.assert_not_called()

	def test_arabic_name_passes_the_name_guard(self):
		message, get_all = self._create("مصطفي حسين")
		self.assertNotIn("has no letters", message)
		get_all.assert_called()


if __name__ == "__main__":
	unittest.main()

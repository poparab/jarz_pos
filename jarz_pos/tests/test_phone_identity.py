"""POS phone identity: one subscriber, one Customer, whichever way it is typed.

``create_customer`` blocks duplicate phone numbers with an exact comparison and
``search_customers`` matches with a ``LIKE``. Production stores the same
subscriber as ``01111034268`` and ``+201111034268``, so both were blind across
the pair: staff could not find the record they already had, and the guard did not
stop them creating a second one.
"""

import unittest
from unittest.mock import patch

from jarz_pos.utils.phone import normalize_phone, phone_search_terms, phone_variants


class TestNormalizePhone(unittest.TestCase):

	def test_egyptian_spellings_fold_to_the_local_form(self):
		cases = {
			"01111034268": "01111034268",
			"+201111034268": "01111034268",
			"201111034268": "01111034268",
			"00201111034268": "01111034268",
			"+20 111 103 4268": "01111034268",
			"0111-103-4268": "01111034268",
		}
		for raw, expected in cases.items():
			with self.subTest(raw=raw):
				self.assertEqual(normalize_phone(raw), expected)

	def test_blank_returns_empty_string(self):
		for raw in (None, "", "   "):
			with self.subTest(raw=raw):
				self.assertEqual(normalize_phone(raw), "")

	def test_foreign_numbers_are_left_alone(self):
		self.assertEqual(normalize_phone("+15551234567"), "+15551234567")

	def test_is_idempotent(self):
		once = normalize_phone("+201111034268")
		self.assertEqual(normalize_phone(once), once)


class TestPhoneVariants(unittest.TestCase):

	def test_covers_every_stored_spelling(self):
		variants = phone_variants("01111034268")
		for expected in ("01111034268", "+201111034268", "201111034268"):
			with self.subTest(expected=expected):
				self.assertIn(expected, variants)

	def test_canonical_form_is_first(self):
		self.assertEqual(phone_variants("+201111034268")[0], "01111034268")

	def test_all_spellings_agree(self):
		base = set(phone_variants("01111034268"))
		for raw in ("+201111034268", "201111034268"):
			with self.subTest(raw=raw):
				self.assertTrue(base.issubset(set(phone_variants(raw))))

	def test_blank_returns_empty(self):
		self.assertEqual(phone_variants(None), [])


class TestPhoneSearchTerms(unittest.TestCase):

	def test_includes_the_national_number_shared_by_every_spelling(self):
		self.assertIn("1111034268", phone_search_terms("01111034268"))
		self.assertIn("1111034268", phone_search_terms("+201111034268"))

	def test_a_local_search_would_match_an_international_row(self):
		"""LIKE %term% against '+201111034268' has to hit for at least one term."""
		stored = "+201111034268"
		self.assertTrue(any(term in stored for term in phone_search_terms("01111034268")))

	def test_an_international_search_would_match_a_local_row(self):
		stored = "01111034268"
		self.assertTrue(any(term in stored for term in phone_search_terms("+201111034268")))

	def test_partial_input_is_passed_through(self):
		self.assertIn("11110", phone_search_terms("11110"))

	def test_blank_returns_empty(self):
		self.assertEqual(phone_search_terms(""), [])


class TestCreateCustomerDuplicateGuard(unittest.TestCase):
	"""The guard must reject a number already stored in any spelling."""

	def _exists_calls(self, mobile_no, existing_values):
		from jarz_pos.api import customer as customer_api

		seen = []

		def _exists(doctype, filters=None):
			seen.append((doctype, filters))
			if doctype not in ("Customer", "Contact"):
				return False
			values = filters.get("mobile_no")
			if not isinstance(values, list):
				return False
			_operator, candidates = values
			return any(candidate in existing_values for candidate in candidates)

		with patch.object(customer_api.frappe.db, "exists", side_effect=_exists), \
			 patch.object(customer_api.frappe, "throw", side_effect=RuntimeError("blocked")):
			with self.assertRaises(RuntimeError):
				customer_api.create_customer(
					customer_name="Test",
					mobile_no=mobile_no,
					customer_primary_address="somewhere",
					territory_id="EGMARG",
				)
		return seen

	def test_blocks_when_stored_in_the_other_spelling(self):
		seen = self._exists_calls("01111034268", {"+201111034268"})
		self.assertTrue(seen, "duplicate guard never queried Customer")

	def test_blocks_when_stored_in_the_same_spelling(self):
		seen = self._exists_calls("01111034268", {"01111034268"})
		self.assertTrue(seen)

	def test_guard_queries_with_an_in_filter(self):
		seen = self._exists_calls("01111034268", {"01111034268"})
		doctype, filters = seen[0]
		self.assertEqual(doctype, "Customer")
		operator, candidates = filters["mobile_no"]
		self.assertEqual(operator, "in")
		self.assertIn("+201111034268", candidates)


if __name__ == "__main__":
	unittest.main()

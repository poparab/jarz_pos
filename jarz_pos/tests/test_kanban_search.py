"""Kanban board search: what the OR-filter query is allowed to be built from.

Pure mock/unittest — no site, no DB. This module exists separately from
``test_kanban`` because that module's ``setUpClass`` touches the database, so it
cannot run in the site-less CI logic gate and every search assertion it holds
has therefore never executed there.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api.kanban import (
	_SEARCH_LINK_LIMIT,
	_SEARCH_MATCH_LIMIT,
	_build_invoice_search_or_filters,
	_find_customer_search_matches,
	get_kanban_invoices,
)


class TestCustomerSearchLookup(unittest.TestCase):
	"""The Customer/Contact/Address fan-out behind a typed search term."""

	@patch("jarz_pos.api.kanban.frappe.db", new_callable=MagicMock)
	@patch("jarz_pos.api.kanban.frappe.get_all")
	def test_lookup_is_wide_and_deterministically_ordered(self, mock_get_all, mock_db):
		"""A common name must not be resolved through an arbitrary 50 customers.

		These caps were 50 and the queries were unordered, so searching "Ahmed"
		or a phone prefix filtered the board by whichever 50 rows the database
		happened to return first: orders that plainly matched what was typed
		were simply not on the board, which is the loudest way this search reads
		as broken.
		"""
		mock_db.has_column.return_value = True
		calls = {}

		def _fake_get_all(doctype, **kwargs):
			calls[doctype] = kwargs
			# Non-empty so the Dynamic Link follow-up queries are reached.
			return ["ROW-1"]

		mock_get_all.side_effect = _fake_get_all

		_find_customer_search_matches("Ahmed 010")

		for doctype in ("Customer", "Contact", "Address"):
			self.assertIn(doctype, calls, f"{doctype} should be searched")
			self.assertEqual(
				calls[doctype]["limit"],
				_SEARCH_MATCH_LIMIT,
				f"{doctype} lookup should use the wide search cap",
			)
			self.assertEqual(
				calls[doctype]["order_by"],
				"modified desc",
				f"{doctype} lookup must be deterministically ordered so that a "
				f"truncated result keeps recently-active customers",
			)

		self.assertGreaterEqual(_SEARCH_MATCH_LIMIT, 500)
		self.assertEqual(calls["Dynamic Link"]["limit"], _SEARCH_LINK_LIMIT)

	@patch("jarz_pos.api.kanban.frappe.db", new_callable=MagicMock)
	@patch("jarz_pos.api.kanban.frappe.get_all")
	def test_non_numeric_term_skips_the_phone_fan_out(self, mock_get_all, mock_db):
		"""Only a term containing a digit can match a phone number."""
		mock_db.has_column.return_value = True
		seen = []

		def _fake_get_all(doctype, **kwargs):
			seen.append(doctype)
			return []

		mock_get_all.side_effect = _fake_get_all

		_find_customer_search_matches("Ahmed")

		self.assertEqual(seen, ["Customer"])

	def test_wildcards_in_the_term_stay_literal(self):
		"""A typed % must match a literal %, not every order on the board."""
		result = _build_invoice_search_or_filters("50%_off")
		self.assertIn({"name": ["like", r"%50\%\_off%"]}, result)


class TestSearchTruncationReporting(unittest.TestCase):
	"""What the board reports when a *search* overflows the row cap."""

	class _MetaStub:
		def get_field(self, fieldname):
			return fieldname == "custom_kanban_profile"

	class _TinyLimits:
		# 0 makes `len(rows) >= cap` true on an empty result set, so the
		# truncation branch is reached without dragging real Sales Invoice
		# documents (and a real meta) through card formatting.
		KANBAN_INVOICES = 0

	def _run(self, filters):
		"""Drive get_kanban_invoices past the truncation branch; return (result, db)."""
		patches = [
			patch("jarz_pos.api.kanban._sort_kanban_columns", side_effect=lambda data: data),
			patch("jarz_pos.api.kanban._get_active_payment_receipt_map", return_value={}),
			patch("jarz_pos.api.kanban._find_customer_search_matches", return_value=[]),
			patch("jarz_pos.api.kanban._get_state_field_options", return_value=["Received"]),
			patch("jarz_pos.api.kanban._get_current_user_pos_profiles", return_value=["Main"]),
			patch("jarz_pos.api.kanban.frappe.get_meta", return_value=self._MetaStub()),
			patch("jarz_pos.api.kanban.frappe.get_all", return_value=[]),
			patch("jarz_pos.api.kanban.QUERY_LIMITS", self._TinyLimits),
		]
		db = MagicMock()
		db.count.return_value = 40000
		patches.append(patch("jarz_pos.api.kanban.frappe.db", db))
		for entry in patches:
			entry.start()
		try:
			result = get_kanban_invoices(filters)
		finally:
			for entry in reversed(patches):
				entry.stop()
		return result, db

	def test_truncated_search_does_not_report_a_search_blind_count(self):
		"""``frappe.db.count()`` takes ``filters`` only — it cannot express the search.

		Running it while a search is active counted every invoice the *other*
		filters allowed, so a search for one order reported a total in the tens
		of thousands.
		"""
		result, db = self._run({"searchTerm": "Ali", "branches": ["Main"]})

		self.assertTrue(result.get("success"))
		self.assertTrue(result.get("truncated"))
		db.count.assert_not_called()
		self.assertNotEqual(
			result.get("total_matching"),
			40000,
			"A truncated search must not report a total that ignores what was typed",
		)

	def test_truncated_unsearched_board_still_reports_the_real_total(self):
		"""The guard is about the search clause only — without one the count is exact."""
		result, db = self._run({"branches": ["Main"]})

		self.assertTrue(result.get("truncated"))
		db.count.assert_called_once()
		self.assertEqual(result.get("total_matching"), 40000)

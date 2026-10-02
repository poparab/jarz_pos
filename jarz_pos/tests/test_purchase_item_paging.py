"""Paging the purchasable item list has to be deterministic.

``search_items`` is consumed page by page with LIMIT/OFFSET. MariaDB's sort is
not stable across calls, so ordering by a non-unique column alone means two
items sharing an ``item_name`` that straddle a page boundary can arrive on both
pages or on neither -- a duplicated row, or one the buyer can never reach by
scrolling. The tiebreaker is what makes the boundary fixed.
"""

from __future__ import annotations

import re
import unicodedata
import unittest
from unittest.mock import patch


class TestSearchItemsPaging(unittest.TestCase):
    def _capture_query(self, **kwargs):
        from jarz_pos.api import purchase

        captured = {}

        def fake_get_all(doctype, **query):
            if doctype == "Item":
                captured.update(query)
                return []
            return []

        with patch("jarz_pos.api.purchase._ensure_manager_access"), patch(
            "jarz_pos.api.purchase.frappe.get_all", side_effect=fake_get_all
        ):
            purchase.search_items(**kwargs)

        return captured

    def test_order_by_carries_a_unique_tiebreaker(self):
        query = self._capture_query(search="choc")

        order_by = query.get("order_by", "")
        self.assertIn("item_name asc", order_by)
        self.assertIn(
            "name asc",
            order_by,
            "item_name is not unique; without `name` the page boundary moves "
            "between calls and items duplicate or vanish",
        )

    def test_offset_follows_the_page(self):
        self.assertEqual(0, self._capture_query(search="a", page=0).get("limit_start"))
        self.assertEqual(20, self._capture_query(search="a", page=1).get("limit_start"))
        self.assertEqual(40, self._capture_query(search="a", page=2).get("limit_start"))

    def test_offset_follows_a_custom_limit(self):
        query = self._capture_query(search="a", page=3, limit=10)
        self.assertEqual(10, query.get("limit_page_length"))
        self.assertEqual(30, query.get("limit_start"))

    def test_limit_is_clamped(self):
        """A caller cannot page the whole catalogue into one response."""
        self.assertEqual(
            100, self._capture_query(search="a", limit=5000).get("limit_page_length")
        )
        # 0 is falsy, so `limit or 20` restores the default rather than
        # clamping to 1. Pinned deliberately: a reader would otherwise expect
        # the max(1, ...) floor to apply here.
        self.assertEqual(
            20, self._capture_query(search="a", limit=0).get("limit_page_length")
        )
        # A negative limit IS truthy, so the floor is what catches it.
        self.assertEqual(
            1, self._capture_query(search="a", limit=-5).get("limit_page_length")
        )

    def test_a_negative_page_does_not_produce_a_negative_offset(self):
        self.assertEqual(0, self._capture_query(search="a", page=-3).get("limit_start"))


class _SearchHarness:
    """Runs search_items against an in-memory catalogue behind a LIKE-faithful
    fake of frappe.get_all, recording every query it was asked to make."""

    CATALOGUE = [
        {"name": "sugar", "item_name": "sugar", "item_group": "Raw Material"},
        {"name": "powder sugar", "item_name": "powder sugar", "item_group": "Raw Material"},
        {"name": "milkana cheese", "item_name": "milkana cheese", "item_group": "Raw Material"},
        {
            "name": "Belcolade Milk Chocolate",
            "item_name": "Belcolade Milk Chocolate",
            "item_group": "Raw Material",
        },
    ]

    @staticmethod
    def _like(pattern, value):
        """SQL LIKE under utf8mb4_unicode_ci: case- and accent-blind, % and _."""

        def plain(text):
            decomposed = unicodedata.normalize("NFKD", str(text or ""))
            return "".join(c for c in decomposed if not unicodedata.combining(c))

        regex = "".join(
            ".*" if c == "%" else "." if c == "_" else re.escape(c) for c in plain(pattern)
        )
        return re.fullmatch(regex, plain(value), re.IGNORECASE | re.DOTALL) is not None

    def _run(self, search, arabic_field=False):
        from jarz_pos.api import purchase

        calls = []

        def fake_get_all(doctype, **query):
            calls.append(query)
            if doctype != "Item" or not query.get("or_filters"):
                return []
            rows = [
                r for r in self.CATALOGUE
                if any(self._like(pat, r.get(field)) for field, _op, pat in query["or_filters"])
            ]
            pinned = query.get("filters", {}).get("name")
            if pinned:
                rows = [r for r in rows if r["name"] in pinned[1]]
            # The paged query reads only these four columns; returning nothing
            # keeps the bulk enrichment helpers out of the test.
            return rows if "limit_start" not in query else []

        with patch("jarz_pos.api.purchase._ensure_manager_access"), patch(
            "jarz_pos.api.purchase._has_field", return_value=arabic_field
        ), patch("jarz_pos.api.purchase.frappe.get_all", side_effect=fake_get_all):
            purchase.search_items(search=search)
        return calls


class TestSearchItemsWordMatching(_SearchHarness, unittest.TestCase):
    """The buyer's phone keyboard must not decide whether an item is found.

    Production, 2026-10-02: the iPhone keyboard leaves a trailing space after
    a predicted word, so "sugar " matched nothing while "sugar" matched two
    items, and "cream cheese" / "chocolate milk" missed items whose words run
    in another order.
    """

    def test_trailing_space_is_ignored(self):
        calls = self._run("sugar ")
        self.assertEqual(1, len(calls), "one word must stay a single query")
        self.assertTrue(all(f[2] == "%sugar%" for f in calls[0]["or_filters"]))

    def test_blank_search_does_not_filter(self):
        calls = self._run("   ")
        self.assertEqual([], calls[0]["or_filters"])

    def test_words_match_in_any_order(self):
        calls = self._run("chocolate  milk")
        paged = calls[-1]
        self.assertEqual(["in", ["Belcolade Milk Chocolate"]], paged["filters"]["name"])

    def test_every_word_must_match(self):
        calls = self._run("cheese milkana")
        self.assertEqual(["in", ["milkana cheese"]], calls[-1]["filters"]["name"])

    def test_other_words_ignore_marks_like_the_collation(self):
        """SQL matches the anchor ignoring accents and hamza; the rest must too."""
        from jarz_pos.api.purchase import _fold

        self.assertEqual(_fold("Cr\u00e8me"), _fold("creme"))
        self.assertEqual(_fold("\u0625\u0633\u0631\u0627\u0621"), _fold("\u0627\u0633\u0631\u0627\u0621"))
        self.CATALOGUE = self.CATALOGUE + [
            {"name": "CB", "item_name": "Cr\u00e8me Br\u00fbl\u00e9e mix", "item_group": "Raw Material"},
        ]
        calls = self._run("creme  brulee")
        self.assertEqual(["in", ["CB"]], calls[-1]["filters"]["name"])

    def test_no_match_skips_the_paged_query(self):
        calls = self._run("cream cheese")
        self.assertEqual(1, len(calls), "nothing matched, so no second query")
        self.assertNotIn("limit_start", calls[0])


class TestSearchItemsArabicName(_SearchHarness, unittest.TestCase):
    """English-named items must be reachable by typing Arabic.

    182 of the 199 purchasable items were named in English only, so the buyer,
    typing Arabic, could not find sugar by typing sugar.
    """

    SUGAR_AR = "\u0633\u0643\u0631"  # sugar
    POWDER_AR = "\u0628\u0648\u062f\u0631\u0629"  # powder, taa marbuta
    POWDER_AR_TYPED = "\u0628\u0648\u062f\u0631\u0647"  # same word ending in haa

    def setUp(self):
        self.CATALOGUE = [dict(r) for r in _SearchHarness.CATALOGUE]
        for row in self.CATALOGUE:
            if row["name"] == "sugar":
                row["jarz_item_name_ar"] = self.SUGAR_AR
            if row["name"] == "powder sugar":
                row["jarz_item_name_ar"] = self.SUGAR_AR + " " + self.POWDER_AR
        # Shares four letters with "powder" but is a different word: the SQL
        # wildcard reaches it, the Python check must drop it.
        self.CATALOGUE.append(
            {"name": "decoy", "item_name": "decoy", "item_group": "Raw Material",
             "jarz_item_name_ar": "\u0628\u0648\u062f\u0631\u0627"}
        )

    def test_arabic_field_is_searched_when_migrated(self):
        calls = self._run(self.SUGAR_AR, arabic_field=True)
        fields = [f[0] for f in calls[0]["or_filters"]]
        self.assertIn("jarz_item_name_ar", fields)
        self.assertEqual(1, len(calls), "no loosely-spelled letter, one query")

    def test_arabic_field_is_left_out_before_migrate(self):
        calls = self._run(self.SUGAR_AR, arabic_field=False)
        self.assertNotIn("jarz_item_name_ar", [f[0] for f in calls[0]["or_filters"]])

    def test_final_haa_finds_taa_marbuta(self):
        calls = self._run(self.POWDER_AR_TYPED, arabic_field=True)
        self.assertEqual(["in", ["powder sugar"]], calls[-1]["filters"]["name"])

    def test_arabic_words_in_any_order(self):
        calls = self._run(self.POWDER_AR + " " + self.SUGAR_AR, arabic_field=True)
        self.assertEqual(["in", ["powder sugar"]], calls[-1]["filters"]["name"])

    def test_spelling_fold(self):
        from jarz_pos.api.purchase import _fold

        self.assertEqual(_fold(self.POWDER_AR), _fold(self.POWDER_AR_TYPED))
        # alef maqsura vs yaa at the end of a word
        self.assertEqual(
            _fold("\u0645\u0643\u0631\u0648\u0646\u0649"),
            _fold("\u0645\u0643\u0631\u0648\u0646\u064a"),
        )


if __name__ == "__main__":
    unittest.main()

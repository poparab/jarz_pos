"""Paging the purchasable item list has to be deterministic.

``search_items`` is consumed page by page with LIMIT/OFFSET. MariaDB's sort is
not stable across calls, so ordering by a non-unique column alone means two
items sharing an ``item_name`` that straddle a page boundary can arrive on both
pages or on neither -- a duplicated row, or one the buyer can never reach by
scrolling. The tiebreaker is what makes the boundary fixed.
"""

from __future__ import annotations

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


class TestSearchItemsWordMatching(unittest.TestCase):
    """The buyer's phone keyboard must not decide whether an item is found.

    Production, 2026-10-02: the iPhone keyboard leaves a trailing space after
    a predicted word, so "sugar " matched nothing while "sugar" matched two
    items, and "cream cheese" / "chocolate milk" missed items whose words run
    in another order.
    """

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

    def _run(self, search):
        from jarz_pos.api import purchase

        calls = []

        def fake_get_all(doctype, **query):
            calls.append(query)
            if doctype != "Item" or not query.get("or_filters"):
                return []
            # SQL compares under utf8mb4_unicode_ci: case- and accent-blind.
            anchor = purchase._fold(query["or_filters"][0][2].strip("%"))
            rows = [
                r for r in self.CATALOGUE
                if any(anchor in purchase._fold(r[f]) for f in ("name", "item_name", "item_group"))
            ]
            pinned = query.get("filters", {}).get("name")
            if pinned:
                rows = [r for r in rows if r["name"] in pinned[1]]
            # The paged query reads only these four columns; returning nothing
            # keeps the bulk enrichment helpers out of the test.
            return rows if "limit_start" not in query else []

        with patch("jarz_pos.api.purchase._ensure_manager_access"), patch(
            "jarz_pos.api.purchase.frappe.get_all", side_effect=fake_get_all
        ):
            purchase.search_items(search=search)
        return calls

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


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

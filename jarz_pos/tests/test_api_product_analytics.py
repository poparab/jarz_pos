"""Product analytics: Small (147 ml) jars are counted, not skipped.

Every read is patched, so no site is needed.
"""

import unittest
from unittest.mock import MagicMock, patch


def _row(item_code, item_group, qty, amount, invoice="SINV-1"):
    return {
        "item_code": item_code,
        "item_name": item_code,
        "item_group": item_group,
        "qty": qty,
        "amount": amount,
        "invoice_name": invoice,
        "territory": "EGNASRCITY",
        "posting_date": "2026-09-20",
    }


class TestProductAnalyticsSizes(unittest.TestCase):
    def _run(self, rows, bom_costs=None):
        from jarz_pos.api import product_analytics as pa

        fake = MagicMock()
        fake.db.sql.return_value = rows
        with patch.object(pa, "frappe", fake), patch.object(
            pa, "_ensure_jarz_manager"
        ), patch.object(pa, "_get_bundle_item_codes", return_value=set()), patch.object(
            pa, "_get_bom_costs", return_value=bom_costs or {}
        ), patch.object(pa, "get_area_label", side_effect=lambda t: t):
            return pa.get_product_analytics("2026-09-01", "2026-09-30")

    def test_small_lines_are_counted_under_their_own_type(self):
        result = self._run(
            [
                _row("Molten Small", "Small", 3, 90.0),
                _row("Molten Medium", "Medium", 2, 240.0, invoice="SINV-2"),
                _row("Molten Large", "Large", 1, 160.0, invoice="SINV-3"),
            ],
            bom_costs={"Molten Small": 10.0},
        )

        by_type = {t["type"]: t for t in result["by_product_type"]}
        self.assertEqual(["Bundle", "Small", "Medium", "Large"], [t["type"] for t in result["by_product_type"]])
        self.assertEqual(3, by_type["Small"]["units"])
        self.assertEqual(90.0, by_type["Small"]["revenue"])
        self.assertEqual(30.0, by_type["Small"]["cost"])
        self.assertEqual(2, by_type["Medium"]["units"])
        self.assertEqual(1, by_type["Large"]["units"])

        products = {p["item_code"]: p for p in result["top_products"]}
        self.assertEqual("Small", products["Molten Small"]["type"])
        self.assertEqual(3, result["summary"]["total_orders"])
        self.assertEqual(490.0, result["summary"]["total_revenue"])

    def test_small_only_period_is_not_empty(self):
        result = self._run([_row("Molten Small", "Small", 5, 150.0)])
        self.assertEqual(1, result["summary"]["total_orders"])
        self.assertEqual(150.0, result["summary"]["total_revenue"])


if __name__ == "__main__":
    unittest.main()

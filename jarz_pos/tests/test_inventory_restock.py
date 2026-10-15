"""Inventory analytics restock list: status, cover and suggested quantity.

Pure classification over velocity rows; no site needed (``today`` is pinned).
"""

import unittest
from unittest.mock import patch

TH = {"critical": 5, "watch": 14, "overstock": 90, "slow": 30, "multiplier": 1.0}


def _row(code, stock, qty_30d=0.0, qty_60d=0.0, last_sale=None, stock_value=0.0, group="Medium"):
    return {
        "item_code": code,
        "item_name": code,
        "item_group": group,
        "uom": "Nos",
        "replenishment_type": "Manufacture",
        "stock": stock,
        "stock_value": stock_value,
        "qty_30d": qty_30d,
        "qty_60d": qty_60d,
        "last_sale": last_sale,
    }


class TestBuildRestock(unittest.TestCase):
    def _build(self, rows, th=TH):
        from jarz_pos.api import inventory_analytics as ia

        with patch.object(ia, "today", return_value="2026-10-15"):
            return ia.build_restock(rows, th)

    def test_statuses_and_order(self):
        out = self._build(
            [
                _row("HEALTHY", 60, qty_30d=60),           # 2/day, 30 days cover: no status
                _row("WATCH", 20, qty_30d=60),             # 10 days
                _row("CRIT", 6, qty_30d=60),               # 3 days
                _row("OVER", 500, qty_30d=30, stock_value=1000.0),  # 500 days
                _row("SLOW", 12, last_sale="2026-08-01"),  # no sale in 30 days
                _row("GONE", 0),                           # nothing to say
            ]
        )
        self.assertEqual(["CRIT", "WATCH", "OVER", "SLOW"], [r["item_code"] for r in out])
        by = {r["item_code"]: r for r in out}
        self.assertEqual("critical", by["CRIT"]["status"])
        self.assertEqual(3.0, by["CRIT"]["days_cover"])
        # Target cover = 14 x 2 = 28 days at 2/day = 56, minus 6 on hand.
        self.assertEqual(50, by["CRIT"]["suggested_qty"])
        self.assertEqual(36, by["WATCH"]["suggested_qty"])
        self.assertEqual(0, by["OVER"]["suggested_qty"])
        self.assertIsNone(by["SLOW"]["days_cover"])
        self.assertEqual(2.0, by["OVER"]["valuation_rate"])
        for key in ("item_code", "item_name", "item_group", "stock", "uom", "velocity_30d",
                    "days_cover", "status", "suggested_qty"):
            self.assertIn(key, by["CRIT"])

    def test_selling_with_no_stock_is_critical(self):
        out = self._build([_row("OUT", -4, qty_30d=30)])
        self.assertEqual("critical", out[0]["status"])
        self.assertEqual(0.0, out[0]["days_cover"])
        # Negative stock counts as zero: 1/day x 28 days.
        self.assertEqual(28, out[0]["suggested_qty"])

    def test_seasonal_multiplier_shortens_cover(self):
        th = dict(TH, multiplier=2.0)
        out = self._build([_row("PEAK", 40, qty_30d=60)], th)  # 2/day x2 = 4/day: 10 days
        self.assertEqual("watch", out[0]["status"])
        self.assertEqual(10.0, out[0]["days_cover"])

    def test_recent_seller_is_not_slow(self):
        th = dict(TH, slow=45)
        out = self._build([_row("PAUSED", 10, last_sale="2026-09-10")], th)
        self.assertEqual([], out)


if __name__ == "__main__":
    unittest.main()

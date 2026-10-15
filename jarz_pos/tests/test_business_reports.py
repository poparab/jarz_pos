"""Business reports (unit economics, promos, labour): the pure builders.

Every builder takes plain rows and returns the payload, so no site is needed.
"""

import math
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import business_reports as br


def pnl(**over):
    base = {
        "summary": {
            "sales": 300000.0, "orders": 1000, "shipping_income": 20000.0,
            "shipping_expense": 30000.0, "shipping_net": -10000.0, "net_profit": 5000.0,
        },
        "cost_of_sales": {"total": 130000.0, "cost_of_goods": 120000.0, "rows": []},
        "recurring": {
            "total": 60000.0, "due": 60000.0,
            "rows": [
                {"account": "Salary - J", "posted": 50000.0},
                {"account": "Commission on Sales - J", "posted": 4000.0},
            ],
        },
        "other_expenses": {
            "total": 9000.0,
            "rows": [
                {"account": "Kashier Fees - J", "label": "Kashier Fees", "amount": 1000.0},
                {"account": "Utilities - J", "label": "Utilities", "amount": 8000.0},
            ],
        },
        "data_quality": {"cogs_coverage_pct": 98.0, "warnings": ["recurring_not_fully_posted"]},
    }
    base.update(over)
    return base


class TestUnitEconomics(unittest.TestCase):
    def build(self, **kw):
        args = dict(
            pnl=pnl(), kashier_receipts=100000.0, talabat_sales=20000.0,
            commission_accounts=["Commission on Sales - J"],
            kashier_fee_accounts=["Kashier Fees - J"],
            ad_spend=100000.0, kashier_pct=3, talabat_pct=30, extra_fixed=0,
        )
        args.update(kw)
        return br.build_unit_economics(**args)

    def test_variable_costs_and_contribution(self):
        out = self.build()
        v = out["variable_costs"]
        self.assertEqual(v["cogs"], 120000.0)
        self.assertEqual(v["shipping_net"], 10000.0)
        self.assertEqual(v["kashier_fees"], 3000.0)
        self.assertEqual(v["talabat_commission"], 6000.0)
        self.assertEqual(v["total"], 139000.0)
        self.assertEqual(out["contribution"], 161000.0)
        self.assertEqual(out["avg_order_value"], 300.0)

    def test_booked_estimates_are_not_double_counted(self):
        out = self.build()
        f = out["fixed_costs"]
        # 60000 - 4000 booked commission; 9000 - 1000 booked Kashier fee + 10000 stock adj.
        self.assertEqual(f["recurring"], 56000.0)
        self.assertEqual(f["other"], 18000.0)
        self.assertEqual(f["total"], 174000.0)
        self.assertEqual(out["net_profit"], 161000.0 - 174000.0)
        self.assertIn("booked_partner_commission_replaced_by_estimate", out["notes"])
        self.assertIn("booked_kashier_fees_replaced_by_estimate", out["notes"])
        self.assertIn("stock_adjustment_in_other", out["notes"])
        self.assertIn("recurring_not_fully_posted", out["notes"])

    def test_break_even(self):
        out = self.build()
        ratio = 161000.0 / 300000.0
        self.assertAlmostEqual(out["break_even_revenue"], round(174000.0 / ratio, 2), places=2)
        self.assertEqual(out["break_even_orders"], 1081)
        self.assertAlmostEqual(out["gap_to_break_even"], out["break_even_revenue"] - 300000.0, places=2)

    def test_no_break_even_without_positive_contribution(self):
        out = self.build(talabat_sales=0, kashier_receipts=0, pnl=pnl(
            cost_of_sales={"total": 400000.0, "cost_of_goods": 400000.0, "rows": []}))
        self.assertIsNone(out["break_even_revenue"])
        self.assertIsNone(out["break_even_orders"])
        self.assertIsNone(out["gap_to_break_even"])
        self.assertIn("contribution_not_positive", out["notes"])

    def test_no_orders(self):
        empty = pnl(summary={"sales": 0.0, "orders": 0, "shipping_net": 0.0})
        out = self.build(pnl=empty, kashier_receipts=0, talabat_sales=0, ad_spend=0)
        self.assertEqual(out["per_order"]["revenue"], 0.0)
        self.assertIsNone(out["break_even_revenue"])
        self.assertIn("ad_spend_not_entered", out["notes"])


class TestPromo(unittest.TestCase):
    def test_aggregate(self):
        invoices = [
            {"invoice": "A", "customer": "c1", "revenue": 500.0, "discount": 50.0, "codes": ["X"], "is_new": 1, "repeated": 1},
            {"invoice": "B", "customer": "c2", "revenue": 300.0, "discount": 30.0, "codes": ["X"], "is_new": 0, "repeated": 0},
            {"invoice": "C", "customer": "c3", "revenue": 200.0, "discount": 20.0, "codes": ["Y", "Z"], "is_new": 1, "repeated": 0},
        ]
        redemptions = [
            {"invoice": "A", "code": "X", "discount": 50.0},
            {"invoice": "B", "code": "X", "discount": 30.0},
        ]
        out = br.aggregate_promo(invoices=invoices, redemptions=redemptions,
                                 baseline_orders=10, baseline_revenue=4000.0)
        t = out["totals"]
        self.assertEqual(t["orders_with_promo"], 3)
        self.assertEqual(t["discount_total"], 100.0)
        self.assertEqual(t["promo_revenue"], 1000.0)
        self.assertAlmostEqual(t["promo_aov"], 333.33, places=2)
        self.assertAlmostEqual(t["non_promo_aov"], 3000.0 / 7, places=2)
        self.assertEqual(t["new_customers_via_promo"], 2)
        codes = {c["code"]: c for c in out["codes"]}
        self.assertEqual(codes["X"]["orders"], 2)
        self.assertEqual(codes["X"]["discount"], 80.0)
        self.assertEqual(codes["X"]["new_customers"], 1)
        self.assertEqual(codes["X"]["repeat_after"], 1)
        # C has no redemption rows: falls back to its own codes, discount split evenly.
        self.assertEqual(codes["Y"]["discount"], 10.0)
        self.assertEqual(codes["Z"]["revenue"], 200.0)


class TestLabour(unittest.TestCase):
    def test_prorated_month(self):
        m0, m1 = date(2026, 9, 1), date(2026, 9, 30)
        self.assertAlmostEqual(br.prorated_month_cost(9000, m0, m1, m0, m1), 9000.0)
        self.assertAlmostEqual(br.prorated_month_cost(9000, m0, m1, date(2026, 9, 1), date(2026, 9, 10)), 3000.0)
        # Joined on the 21st: 10 days of a 30-day month.
        self.assertAlmostEqual(br.prorated_month_cost(9000, m0, m1, m0, m1, joined=date(2026, 9, 21)), 3000.0)
        # Leaver relieved on the 10th: _load_payroll already scaled to 10/30.
        self.assertAlmostEqual(br.prorated_month_cost(3000, m0, m1, m0, m1, relieved=date(2026, 9, 10)), 3000.0)
        self.assertAlmostEqual(br.prorated_month_cost(3000, m0, m1, date(2026, 9, 6), m1, relieved=date(2026, 9, 10)), 1500.0)
        self.assertEqual(br.prorated_month_cost(9000, m0, m1, m0, m1, relieved=date(2026, 8, 31)), 0.0)

    def test_allocation_shares(self):
        self.assertEqual(br.allocation_shares({"A": 30, "B": 10}, {}, {}, None), {"A": 0.75, "B": 0.25})
        self.assertEqual(br.allocation_shares({}, {"A": 2}, {"B": 5}, None), {"A": 1.0})
        self.assertEqual(br.allocation_shares({}, {}, {}, "Factory"), {"Factory": 1.0})

    def test_build_report(self):
        records = [
            {"employee": "E1", "employee_name": "One", "branch": "Dokki", "shift_location": "Dokki Loc", "hours": 100, "days": 10, "cost": 6000},
            {"employee": "E1", "employee_name": "One", "branch": "Nasr", "shift_location": "Nasr Loc", "hours": 50, "days": 5, "cost": 3000},
            {"employee": "E2", "employee_name": "Two", "branch": None, "shift_location": "Factory", "hours": 80, "days": 8, "cost": 5000},
        ]
        sales = {"Dokki": 60000.0, "Nasr": 30000.0}
        out = br.build_labour_report(records=records, sales_by_branch=sales, branch=None, cost_basis="recurring_expense")
        t = out["totals"]
        self.assertEqual(t["labour_cost"], 14000.0)
        self.assertEqual(t["sales"], 90000.0)
        self.assertEqual(t["headcount"], 2)
        self.assertEqual(t["hours"], 230.0)
        self.assertAlmostEqual(t["sales_per_hour"], round(90000 / 230, 2))
        rows = {(r["branch"], r["shift_location"]): r for r in out["branches"]}
        self.assertEqual(rows[("Dokki", None)]["labour_pct"], 10.0)
        self.assertIsNone(rows[(None, "Factory")]["labour_pct"])
        emps = {e["employee"]: e for e in out["employees"]}
        self.assertEqual(emps["E1"]["cost"], 9000.0)
        self.assertEqual(emps["E1"]["branch"], "Dokki")
        self.assertIsNone(emps["E2"]["branch"])
        self.assertEqual(emps["E2"]["shift_location"], "Factory")

        one = br.build_labour_report(records=records, sales_by_branch=sales, branch="Nasr", cost_basis="recurring_expense")
        self.assertEqual(one["totals"]["labour_cost"], 3000.0)
        self.assertEqual(one["totals"]["sales"], 30000.0)
        self.assertEqual(one["totals"]["labour_pct"], 10.0)
        self.assertEqual([e["employee"] for e in one["employees"]], ["E1"])
        self.assertEqual(one["employees"][0]["cost"], 3000.0)


def _fake_frappe():
    """A frappe stand-in whose ``throw`` raises the real exception class."""
    fake = MagicMock()
    fake.ValidationError = frappe.ValidationError

    def _throw(msg, exc=frappe.ValidationError, *a, **kw):
        raise exc(msg)

    fake.throw.side_effect = _throw
    return fake


class TestRangeCap(unittest.TestCase):
    """Every report here refuses a range longer than MAX_RANGE_DAYS (366)."""

    def _patched(self):
        return patch.multiple(br, frappe=_fake_frappe(), _=lambda s, *a, **k: s)

    def test_ensure_max_range_boundaries(self):
        with self._patched():
            br.ensure_max_range(date(2025, 1, 1), date(2025, 12, 31))  # 365 days
            br.ensure_max_range(date(2024, 1, 1), date(2024, 12, 31))  # 366 days (leap year)
            with self.assertRaises(frappe.ValidationError):
                br.ensure_max_range(date(2024, 1, 1), date(2025, 1, 1))  # 367 days

    def test_every_endpoint_refuses_an_over_long_range(self):
        too_long = (date(2025, 1, 1), date(2026, 6, 30))
        endpoints = (br.get_labour_cost, br.get_unit_economics, br.get_promo_performance, br.get_stock_adjustments)
        with self._patched(), patch.object(br, "_ensure_jarz_manager"), patch.object(
            br.fr, "_parse_dates", return_value=too_long
        ), patch.object(br.fr, "_default_company") as company:
            for fn in endpoints:
                with self.subTest(endpoint=fn.__name__):
                    with self.assertRaises(frappe.ValidationError):
                        fn(date_from="2025-01-01", date_to="2026-06-30")
        # Refused before any data is read.
        company.assert_not_called()


class TestOwnerInputsAreFinite(unittest.TestCase):
    def test_nan_and_infinity_are_clamped(self):
        nan, inf = float("nan"), float("inf")
        out = br.owner_inputs(inf, nan, -inf, nan)
        self.assertEqual(out, {"ad_spend": 0.0, "kashier_pct": 3.0, "talabat_pct": 0.0, "extra_fixed": 0.0})
        out = br.owner_inputs(-inf, inf, inf, inf)
        self.assertEqual(out, {"ad_spend": 0.0, "kashier_pct": 100.0, "talabat_pct": 100.0, "extra_fixed": 0.0})
        for v in out.values():
            self.assertTrue(math.isfinite(v))

    def test_string_inputs_from_the_query_string(self):
        out = br.owner_inputs("NaN", "inf", "-5", "1e999")
        self.assertEqual(out, {"ad_spend": 0.0, "kashier_pct": 100.0, "talabat_pct": 0.0, "extra_fixed": 0.0})

    def test_ordinary_values_pass_through(self):
        out = br.owner_inputs("15000", 2.5, 35, "40000")
        self.assertEqual(out, {"ad_spend": 15000.0, "kashier_pct": 2.5, "talabat_pct": 35.0, "extra_fixed": 40000.0})

    def test_builder_survives_an_infinite_fixed_cost(self):
        # Belt and braces: even if an infinity reaches the builder directly,
        # break-even is reported as unavailable instead of raising OverflowError.
        out = TestUnitEconomics().build(ad_spend=0.0, extra_fixed=float("inf"))
        self.assertIsNone(out["break_even_orders"])


if __name__ == "__main__":
    unittest.main()

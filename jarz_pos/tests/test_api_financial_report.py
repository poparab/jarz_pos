"""Profit & Loss report: every number must reconcile to the ledger.

``build_report`` is pure (rows in, payload out), so no site is needed. The
fixtures are shaped on production's September 2026 ledger, where the report
was first checked against real data.
"""

import os
import re
import unittest
from datetime import date

from jarz_pos.api import financial_report as fr

SHIP = "Shipping Income - J"
FREIGHT = "Freight and Forwarding Charges - J"


def gl(account, day, root, dr=0.0, cr=0.0, account_type=""):
    return {
        "account": account, "posting_date": date(2026, 9, day), "root_type": root,
        "account_type": account_type, "debit": dr, "credit": cr,
    }


def inv(purpose, day, net, orders=1, returns=0, returns_value=0.0):
    return {
        "purpose": purpose, "posting_date": date(2026, 9, day), "net": net,
        "orders": orders, "returns": returns, "returns_value": returns_value,
    }


def ship(purpose, day, amount):
    return {"purpose": purpose, "posting_date": date(2026, 9, day), "amount": amount}


RECURRING = {
    "Rent - Dokki - J": {"category": "Rent", "items": []},
    "Salary - J": {"category": "Payroll", "items": [], "payroll": True},
}

ORDER_BASIS = {"expense": 250.0, "orders": 10, "delivery_orders": 9, "pickup_orders": 1,
               "stock_posted_orders": 10}


def build(gl_rows, invoice_rows, shipping_rows, *, due=None, order_basis=None,
          fd=date(2026, 9, 1), td=date(2026, 9, 3), shipping_account=SHIP):
    return fr.build_report(
        fd=fd, td=td, gl_rows=gl_rows, invoice_rows=invoice_rows, shipping_rows=shipping_rows,
        shipping_account=shipping_account, freight_account=FREIGHT, recurring=RECURRING,
        due=due or {}, order_basis=order_basis or ORDER_BASIS,
    )


def september():
    """B2C 1,000 less a 100 return, B2B 500, staff 50; shipping billed 60."""
    gl_rows = [
        gl("Sales - J", 1, "Income", dr=100, cr=1000),
        gl("Sales - J", 2, "Income", cr=550),
        gl(SHIP, 1, "Income", cr=60),
        gl("Cost of Goods Sold - J", 1, "Expense", dr=400, account_type="Cost of Goods Sold"),
        gl("Stock Adjustment - J", 2, "Expense", dr=50, account_type="Stock Adjustment"),
        gl(FREIGHT, 1, "Expense", dr=250),
        gl("Rent - Dokki - J", 1, "Expense", dr=200),
        gl("Salary - J", 2, "Expense", dr=100),
        gl("Cash Over Short - J", 2, "Expense", dr=90, cr=10, account_type="Expense Account"),
    ]
    invoice_rows = [
        inv("Standard", 1, 1000, orders=6),
        inv("Standard", 1, -100, orders=0, returns=1, returns_value=-100),
        inv("B2B Supply", 2, 500, orders=2),
        inv("Employee", 2, 50, orders=1),
        inv("Sample - Courier", 2, 0, orders=1),
    ]
    return gl_rows, invoice_rows, [ship("Standard", 1, 60)]


class TestLedgerReconciliation(unittest.TestCase):
    def test_net_profit_equals_the_ledger(self):
        r = build(*september())
        s = r["summary"]
        # Income 1,450 sales + 60 shipping; expenses 400+50+250+200+100+80.
        self.assertEqual(s["total_revenue"], 1510.0)
        self.assertEqual(s["total_expenses"], 1080.0)
        self.assertEqual(s["net_profit"], 430.0)
        self.assertEqual(r["reconciliation"]["ledger_net_profit"], 430.0)
        self.assertTrue(r["reconciliation"]["matches"])
        self.assertEqual(r["reconciliation"]["difference"], 0.0)

    def test_sections_add_up_to_total_expenses(self):
        s = build(*september())["summary"]
        self.assertEqual(
            s["cost_of_sales"] + s["shipping_expense"] + s["recurring_expenses"] + s["other_expenses"],
            s["total_expenses"],
        )
        self.assertEqual(s["gross_profit"], 1450.0 - 450.0)

    def test_trend_buckets_sum_to_the_totals(self):
        r = build(*september())
        for key, total in (("revenue", "total_revenue"), ("expenses", "total_expenses"),
                           ("net_profit", "net_profit")):
            self.assertAlmostEqual(sum(t[key] for t in r["trend"]), r["summary"][total], places=2)

    def test_quiet_days_still_plot_as_zero(self):
        r = build(*september())
        self.assertEqual([t["date"] for t in r["trend"]], ["2026-09-01", "2026-09-02", "2026-09-03"])
        self.assertEqual(r["trend"][2]["revenue"], 0.0)


class TestChannels(unittest.TestCase):
    def test_b2b_is_the_order_purpose_not_the_customer_group(self):
        self.assertEqual(fr.channel_for_purpose("B2B Supply"), fr.CHANNEL_B2B)
        self.assertEqual(fr.channel_for_purpose("Standard"), fr.CHANNEL_B2C)
        self.assertEqual(fr.channel_for_purpose(None), fr.CHANNEL_B2C)
        self.assertEqual(fr.channel_for_purpose("Free Shipping Waiver"), fr.CHANNEL_B2C)
        self.assertEqual(fr.channel_for_purpose("Employee"), fr.CHANNEL_STAFF)
        self.assertEqual(fr.channel_for_purpose("Sample - No Courier"), fr.CHANNEL_SAMPLES)

    def test_returns_are_netted_off_their_channel(self):
        r = build(*september())
        b2c = next(c for c in r["channels"] if c["channel"] == "b2c")
        self.assertEqual(b2c["sales"], 900.0)
        self.assertEqual(b2c["returns"], 1)
        self.assertEqual(b2c["revenue"], 960.0)  # sales + its shipping income
        self.assertEqual(r["summary"]["b2b_revenue"], 500.0)
        self.assertEqual(r["summary"]["b2c_revenue"], 960.0)

    def test_channels_add_up_to_total_revenue(self):
        r = build(*september())
        self.assertFalse([c for c in r["channels"] if c["channel"] == "adjustments"])
        self.assertEqual(sum(c["revenue"] for c in r["channels"]), r["summary"]["total_revenue"])

    def test_ledger_sales_the_invoices_cannot_explain_are_shown_not_hidden(self):
        gl_rows, invoice_rows, shipping_rows = september()
        gl_rows.append(gl("Sales - J", 3, "Income", cr=75))  # e.g. a manual JE to Sales
        r = build(gl_rows, invoice_rows, shipping_rows)
        adj = [c for c in r["channels"] if c["channel"] == "adjustments"]
        self.assertEqual(adj[0]["revenue"], 75.0)
        self.assertEqual(sum(c["revenue"] for c in r["channels"]), r["summary"]["total_revenue"])
        self.assertIn("sales_adjustments", r["reconciliation"]["notes"])
        self.assertTrue(r["reconciliation"]["matches"])


class TestShipping(unittest.TestCase):
    def test_income_is_what_was_billed_and_expense_is_the_courier_cost(self):
        sh = build(*september())["shipping"]
        self.assertEqual(sh["income"], 60.0)
        self.assertEqual(sh["expense"], 250.0)
        self.assertEqual(sh["net"], -190.0)
        self.assertEqual(sh["income_in_expense_account"], 0.0)

    def test_pre_split_income_credited_to_freight_is_grossed_back_up(self):
        """Before 2026-09 the tax row credited Freight, netting income into cost."""
        gl_rows, invoice_rows, shipping_rows = september()
        gl_rows = [g for g in gl_rows if g["account"] != SHIP]
        gl_rows.append(gl(FREIGHT, 1, "Expense", cr=60))  # the tax row, posted to Freight
        r = build(gl_rows, invoice_rows, shipping_rows)
        sh = r["shipping"]
        self.assertEqual(sh["income"], 60.0)
        self.assertEqual(sh["expense"], 250.0)
        self.assertEqual(sh["expense_ledger"], 190.0)
        self.assertEqual(sh["income_in_expense_account"], 60.0)
        # Both sides moved by 60, so net profit is still the ledger's.
        self.assertTrue(r["reconciliation"]["matches"])
        self.assertEqual(r["summary"]["net_profit"], r["reconciliation"]["ledger_net_profit"])

    def test_site_without_a_shipping_income_account(self):
        gl_rows, invoice_rows, shipping_rows = september()
        gl_rows = [g for g in gl_rows if g["account"] != SHIP]
        gl_rows.append(gl(FREIGHT, 1, "Expense", cr=60))
        r = build(gl_rows, invoice_rows, shipping_rows, shipping_account=None)
        self.assertEqual(r["shipping"]["income"], 60.0)
        self.assertTrue(r["reconciliation"]["matches"])


class TestExpenseSections(unittest.TestCase):
    def test_classification(self):
        rec = {"Rent - Dokki - J"}
        c = fr.classify_expense_account
        self.assertEqual(c(FREIGHT, "Chargeable", FREIGHT, rec), "shipping")
        self.assertEqual(c("Cost of Goods Sold - J", "Cost of Goods Sold", FREIGHT, rec), "cost_of_sales")
        self.assertEqual(c("Stock Adjustment - J", "Stock Adjustment", FREIGHT, rec), "cost_of_sales")
        self.assertEqual(c("Rent - Dokki - J", "", FREIGHT, rec), "recurring")
        self.assertEqual(c("Cash Over Short - J", "Expense Account", FREIGHT, rec), "other")

    def test_recurring_shows_posted_against_due(self):
        r = build(*september(), due={"Rent - Dokki - J": 200.0, "Salary - J": 1000.0,
                                      "Pest Control - J": 50.0})
        rows = {x["account"]: x for x in r["recurring"]["rows"]}
        self.assertEqual(rows["Salary - J"]["posted"], 100.0)
        self.assertEqual(rows["Salary - J"]["remaining"], 900.0)
        self.assertEqual(rows["Rent - Dokki - J"]["remaining"], 0.0)
        # Due but never posted still appears, with nothing posted.
        self.assertEqual(rows["Pest Control - J"]["posted"], 0.0)
        self.assertEqual(r["recurring"]["due"], 1250.0)
        self.assertIn("recurring_not_fully_posted", r["data_quality"]["warnings"])

    def test_other_expenses_keep_their_net(self):
        other = build(*september())["other_expenses"]
        self.assertEqual(other["rows"], [{"account": "Cash Over Short - J",
                                          "label": "Cash Over Short", "amount": 80.0}])

    def test_account_label_drops_the_company_abbreviation(self):
        self.assertEqual(fr.account_label("Rent - 6th of October - J"), "Rent - 6th of October")
        self.assertEqual(fr.account_label("Sales"), "Sales")


class TestDataQuality(unittest.TestCase):
    def test_unposted_stock_is_flagged(self):
        """July 2026 on production: 472 orders, no Delivery Notes, so no COGS."""
        basis = dict(ORDER_BASIS, stock_posted_orders=0)
        dq = build(*september(), order_basis=basis)["data_quality"]
        self.assertEqual(dq["cogs_coverage_pct"], 0.0)
        self.assertIn("cogs_incomplete", dq["warnings"])

    def test_courier_cost_missing_from_the_ledger_is_flagged(self):
        basis = dict(ORDER_BASIS, expense=1000.0)  # orders recorded 1,000; ledger 250
        dq = build(*september(), order_basis=basis)["data_quality"]
        self.assertIn("shipping_expense_incomplete", dq["warnings"])

    def test_complete_books_raise_nothing(self):
        dq = build(*september())["data_quality"]
        self.assertEqual(dq["warnings"], [])


class TestBuckets(unittest.TestCase):
    def test_granularity(self):
        self.assertEqual(fr.granularity_for(date(2026, 9, 1), date(2026, 9, 30)), "day")
        self.assertEqual(fr.granularity_for(date(2026, 6, 1), date(2026, 9, 30)), "week")
        self.assertEqual(fr.granularity_for(date(2026, 1, 1), date(2026, 9, 30)), "month")

    def test_weeks_start_on_monday(self):
        self.assertEqual(fr.bucket_key(date(2026, 9, 27), "week"), "2026-09-21")
        self.assertEqual(fr.bucket_key(date(2026, 9, 27), "month"), "2026-09-01")

    def test_months_in_range(self):
        self.assertEqual(
            fr.months_in_range(date(2026, 8, 15), date(2026, 9, 3)),
            [(date(2026, 8, 1), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 30))],
        )


class TestShippingAnalyticsSource(unittest.TestCase):
    """The shipping dashboard must not invent delivery income or count returns."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "api", "shipping_analytics.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.queries = re.findall(r'sql\(f?"""(.*?)"""', src, flags=re.S)
        self.assertTrue(self.queries)

    def test_no_territory_or_override_fallback_for_income(self):
        for q in self.queries:
            self.assertNotIn("delivery_income", q, msg=q[:120])

    def test_every_sales_invoice_query_excludes_returns(self):
        for q in self.queries:
            if "FROM `tabSales Invoice`" in q:
                self.assertIn("is_return = 0", q, msg=q[:120])


if __name__ == "__main__":
    unittest.main()

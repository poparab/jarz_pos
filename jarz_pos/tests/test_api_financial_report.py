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
        # 1-3 September is a partial month: a shortfall there is expected.
        self.assertNotIn("recurring_not_fully_posted", r["data_quality"]["warnings"])

    def test_shortfall_is_a_warning_only_over_whole_months(self):
        gl_rows, invoice_rows, shipping_rows = september()
        r = build(gl_rows, invoice_rows, shipping_rows, td=date(2026, 9, 30),
                  due={"Salary - J": 1000.0})
        self.assertIn("recurring_not_fully_posted", r["data_quality"]["warnings"])

    def test_covers_whole_months(self):
        self.assertTrue(fr.covers_whole_months(date(2026, 8, 1), date(2026, 9, 30)))
        self.assertTrue(fr.covers_whole_months(date(2026, 2, 1), date(2026, 2, 28)))
        self.assertFalse(fr.covers_whole_months(date(2026, 9, 1), date(2026, 9, 26)))
        self.assertFalse(fr.covers_whole_months(date(2026, 8, 28), date(2026, 9, 30)))

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


class TestBranchSplit(unittest.TestCase):
    def test_without_a_branch_the_payload_gains_only_additive_keys(self):
        r = build(*september())
        self.assertEqual(r["branches"], [])
        self.assertEqual(r["filters"]["branch"], None)
        self.assertEqual(r["data_quality"]["gl_scope"], "company")
        self.assertEqual(r["data_quality"]["notes"], [])

    def test_branch_filters_channels_but_leaves_the_ledger_alone(self):
        gl_rows, invoice_rows, shipping_rows = september()
        company = build(gl_rows, invoice_rows, shipping_rows)
        r = fr.build_report(
            fd=date(2026, 9, 1), td=date(2026, 9, 3), gl_rows=gl_rows, invoice_rows=invoice_rows,
            shipping_rows=shipping_rows, shipping_account=SHIP, freight_account=FREIGHT,
            recurring=RECURRING, due={}, order_basis=ORDER_BASIS,
            branch="Dokki",
            branch_rows=[{"branch": "Dokki", "orders": 2, "revenue": 500.0, "cogs": 200.0,
                          "gross_profit": 300.0, "shipping_income": 0.0}],
            branch_invoice_rows=[inv("B2B Supply", 2, 500, orders=2)],
            branch_shipping_rows=[],
        )
        # The whole summary is the company-wide one: one scope per block. Before
        # 2026-10-15 orders / b2b_revenue / b2c_revenue followed the branch while
        # sales and profit did not.
        self.assertEqual(r["summary"], company["summary"])
        self.assertEqual(r["summary"]["orders"], 10)
        self.assertEqual(r["summary"]["b2b_revenue"], 500.0)
        self.assertEqual(r["summary"]["b2c_revenue"], 960.0)
        self.assertTrue(r["reconciliation"]["matches"])
        # Channels are the branch's invoices, with no ledger "adjustments" row.
        self.assertFalse([c for c in r["channels"] if c["channel"] == "adjustments"])
        b2b = next(c for c in r["channels"] if c["channel"] == "b2b")
        self.assertEqual(b2b["revenue"], 500.0)
        self.assertEqual(b2b["share_pct"], 100.0)
        self.assertEqual(sum(c["orders"] for c in r["channels"]), 2)
        self.assertIn("gl_company_wide", r["data_quality"]["notes"])
        self.assertEqual(r["branches"][0]["branch"], "Dokki")
        self.assertEqual(r["filters"]["branch"], "Dokki")

    def test_assemble_branch_rows(self):
        rows = fr.assemble_branch_rows(
            [{"branch": "Dokki", "orders": 3, "revenue": 900}, {"branch": "Nasr", "orders": 1, "revenue": 100}],
            [{"branch": "Dokki", "cogs": 300, "stock_sales": 900, "costed_sales": 450}],
            [{"branch": "Dokki", "amount": 60}],
        )
        self.assertEqual([r["branch"] for r in rows], ["Dokki", "Nasr"])
        d = rows[0]
        self.assertEqual((d["orders"], d["revenue"], d["cogs"], d["gross_profit"]), (3, 900.0, 300.0, 600.0))
        self.assertEqual(d["shipping_income"], 60.0)
        self.assertEqual(d["cogs_coverage_pct"], 50.0)
        self.assertEqual(rows[1]["cogs"], 0.0)
        # Rows without a purpose are legacy blanks: B2C.
        self.assertEqual((d["b2b_revenue"], d["b2c_revenue"]), (0.0, 960.0))

    def test_branch_rows_split_b2b_and_b2c_on_the_summary_basis(self):
        """Net sales + billed shipping per purpose; staff and samples in neither."""
        rows = fr.assemble_branch_rows(
            [
                {"branch": "Dokki", "purpose": "Standard", "orders": 3, "revenue": 900},
                {"branch": "Dokki", "purpose": "Standard", "orders": 0, "revenue": -100},
                {"branch": "Dokki", "purpose": "B2B Supply", "orders": 1, "revenue": 500},
                {"branch": "Dokki", "purpose": "Employee", "orders": 1, "revenue": 50},
                {"branch": "Dokki", "purpose": "Sample - Courier", "orders": 1, "revenue": 0},
            ],
            [{"branch": "Dokki", "cogs": 400, "stock_sales": 1350, "costed_sales": 1350}],
            [{"branch": "Dokki", "purpose": "Standard", "amount": 60},
             {"branch": "Dokki", "purpose": "B2B Supply", "amount": 20}],
        )
        d = rows[0]
        self.assertEqual(d["orders"], 6)
        self.assertEqual(d["revenue"], 1350.0)
        self.assertEqual(d["shipping_income"], 80.0)
        self.assertEqual(d["b2b_revenue"], 520.0)
        self.assertEqual(d["b2c_revenue"], 860.0)

    def test_branch_cogs_is_never_negative(self):
        rows = fr.assemble_branch_rows(
            [{"branch": "Dokki", "orders": 1, "revenue": 0}],
            [{"branch": "Dokki", "cogs": -120, "stock_sales": 0, "costed_sales": 0}],
            [],
        )
        self.assertEqual(rows[0]["cogs"], 0.0)
        self.assertEqual(rows[0]["gross_profit"], 0.0)

    def test_actual_cost_reads_incoming_rate_and_delivery_notes(self):
        join_sql, cost = fr.actual_line_cost_sql()
        self.assertIn("incoming_rate * sii.stock_qty", cost)
        self.assertIn("si.update_stock = 1", cost)
        self.assertIn("dni.si_detail", join_sql)
        self.assertIn("%(fd)s", join_sql)

    def test_default_cost_stays_gross_for_product_analytics(self):
        """Product analytics reports gross (is_return = 0) lines: no netting there."""
        join_sql, cost = fr.actual_line_cost_sql()
        self.assertIn("IFNULL(dn.is_return, 0) = 0", join_sql)
        self.assertNotIn("returned_cost", join_sql + cost)
        self.assertNotIn("GREATEST", cost)

    def test_net_returns_nets_return_dns_on_the_original_line(self):
        join_sql, cost = fr.actual_line_cost_sql(net_returns=True)
        flat = " ".join(join_sql.split())
        # Return DN rows are kept (not filtered out) and summed separately ...
        self.assertNotIn("AND IFNULL(dn.is_return, 0) = 0 AND", flat)
        self.assertIn("THEN ABS(dni.incoming_rate * dni.stock_qty) ELSE 0 END) AS returned_cost", flat)
        # ... keyed on the original invoice line they carry in si_detail ...
        self.assertIn("GROUP BY dni.si_detail", flat)
        self.assertIn("dnc.si_detail = sii.name", flat)
        # ... and only when the return itself was posted inside the range, so a
        # later-month return never rewrites (or negates) this month's cost.
        self.assertIn("OR dn.posting_date BETWEEN %(fd)s AND %(td)s", flat)
        self.assertIn("dsi.posting_date BETWEEN %(fd)s AND %(td)s", flat)
        # A fully returned line costs 0, never negative, and is still costed.
        self.assertIn("GREATEST(dnc.cost - IFNULL(dnc.returned_cost, 0), 0)", cost)
        self.assertIn("- IFNULL(dnc.returned_cost, 0), 0)", cost)
        self.assertTrue(cost.strip().endswith("ELSE NULL END)"))

    def test_branch_rows_net_returns_and_keep_credit_notes_out_of_coverage(self):
        from unittest.mock import MagicMock, patch

        fake = MagicMock()
        seen = []
        fake.db.sql.side_effect = lambda q, p=None, as_dict=False, **kw: seen.append(q) or []
        with patch.object(fr, "frappe", fake):
            self.assertEqual(fr._branch_rows(date(2026, 9, 1), date(2026, 9, 30), "J", SHIP), [])
        sales_q, costs_q, ship_q = (" ".join(q.split()) for q in seen)
        self.assertIn("returned_cost", costs_q)
        self.assertIn(
            "SUM(CASE WHEN si.is_return = 0 AND IFNULL(it.is_stock_item, 0) = 1 "
            "THEN sii.base_net_amount ELSE 0 END) AS stock_sales", costs_q)
        self.assertIn("WHEN si.is_return = 0 AND IFNULL(it.is_stock_item, 0) = 1 AND", costs_q)
        self.assertIn("GROUP BY branch, si.custom_order_purpose", sales_q)
        self.assertIn("GROUP BY branch, si.custom_order_purpose", ship_q)


def _fake_frappe(exists=True):
    """A MagicMock ``frappe`` whose ``throw`` raises the real exception class."""
    from unittest.mock import MagicMock

    import frappe as real

    fake = MagicMock()
    fake.ValidationError = real.ValidationError
    fake.DoesNotExistError = real.DoesNotExistError
    fake.PermissionError = real.PermissionError

    def _throw(msg, exc=real.ValidationError, *a, **kw):
        raise exc(msg)

    fake.throw.side_effect = _throw
    fake.db.exists.return_value = exists
    return fake


class TestBranchValidation(unittest.TestCase):
    def _patch(self, exists):
        from unittest.mock import patch

        return patch.multiple(fr, frappe=_fake_frappe(exists), _=lambda s, *a, **k: s)

    def test_blank_is_all_branches(self):
        with self._patch(False):
            self.assertIsNone(fr.validate_branch(None))
            self.assertIsNone(fr.validate_branch("  "))

    def test_existing_pos_profile_passes(self):
        with self._patch(True):
            self.assertEqual(fr.validate_branch(" Dokki "), "Dokki")

    def test_unknown_branch_is_refused(self):
        import frappe as real

        with self._patch(False), self.assertRaises(real.ValidationError):
            fr.validate_branch("Nowhere")

    def test_no_profile_bucket_label_is_not_a_branch(self):
        import frappe as real

        with self._patch(True), self.assertRaises(real.ValidationError):
            fr.validate_branch(fr.NO_BRANCH_LABEL)

    def test_profit_and_loss_refuses_an_unknown_branch(self):
        from unittest.mock import patch

        import frappe as real

        with self._patch(False), patch.object(fr, "_ensure_jarz_manager"), patch.object(
            fr, "_gl_rows"
        ) as gl_rows, self.assertRaises(real.ValidationError):
            fr.get_profit_and_loss(date_from="2026-09-01", date_to="2026-09-30", branch="Nowhere")
        gl_rows.assert_not_called()


class TestLedgerQuery(unittest.TestCase):
    def test_period_closing_vouchers_are_excluded(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "api", "financial_report.py"), encoding="utf-8") as fh:
            src = fh.read()
        gl_query = src[src.index("def _gl_rows"):src.index("def _invoice_rows")]
        self.assertIn("voucher_type != 'Period Closing Voucher'", gl_query)


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


class TestUnsettledCourierBalancesAsOf(unittest.TestCase):
    """Unsettled courier balances can be read as they stood at ``to_date``."""

    def _run(self, **kwargs):
        from unittest.mock import MagicMock, patch

        from jarz_pos.api import shipping_analytics as sa

        fake = MagicMock()
        seen = []

        def _sql(query, params=None, as_dict=False, **kw):
            seen.append((query, params))
            return [{"party_type": "Employee", "party": "EMP-1", "order_count": 2,
                     "total_owed": 120, "oldest_date": "2026-09-20 10:00:00"}]

        fake.db.sql.side_effect = _sql
        with patch.object(sa, "frappe", fake), patch.object(sa, "_ensure_jarz_manager"), patch.object(
            sa, "_party_label", side_effect=lambda t, p: p
        ), patch.object(sa, "nowdate", return_value="2026-10-15"):
            rows = sa.get_unsettled_courier_balances(**kwargs)
        return rows, seen

    def test_no_as_of_is_todays_unsettled_balance(self):
        rows, seen = self._run()
        query, _params = seen[0]
        self.assertIn("ct.status = 'Unsettled'", query)
        self.assertNotIn("as_of_end", query)
        self.assertEqual(25, rows[0]["days_aged"])

    def test_as_of_counts_rows_settled_after_that_day(self):
        rows, seen = self._run(as_of="2026-09-30", branch="Dokki")
        query, params = seen[0]
        self.assertIn("ct.date <= %(as_of_end)s", query)
        self.assertIn("bsi.pos_profile = %(branch)s", query)
        self.assertEqual("2026-09-30 23:59:59", params["as_of_end"])
        # Aged to the as-of date, not today.
        self.assertEqual(10, rows[0]["days_aged"])

    def test_settled_row_without_settled_at_is_never_a_phantom_balance(self):
        """``modified`` moves on any later save; the row's own date cannot."""
        _rows, seen = self._run(as_of="2026-09-30")
        query, _params = seen[0]
        self.assertIn("COALESCE(ct.settled_at, ct.date) > %(as_of_end)s", query)
        self.assertNotIn("ct.modified", query)


class TestShippingKpiAsOf(unittest.TestCase):
    """The dashboard's unsettled KPI is read on the same as-of date as its table."""

    def _kpis(self, **kwargs):
        from unittest.mock import MagicMock, patch

        import frappe as real

        from jarz_pos.api import shipping_analytics as sa

        fake = MagicMock()
        seen = []

        def _sql(query, params=None, as_dict=False, **kw):
            seen.append((query, dict(params or {})))
            return [real._dict(total_orders=0, delivery_orders=0, pickup_orders=0, total_expense=0,
                               total_income=0, avg_cost_per_order=0, total=120, n=0)]

        fake.db.sql.side_effect = _sql
        fake.db.count.return_value = 0
        with patch.object(sa, "frappe", fake), patch.object(sa, "_ensure_jarz_manager"), patch.object(
            sa, "nowdate", return_value="2026-10-15"
        ):
            out = sa.get_summary_kpis(from_date="2026-09-01", to_date="2026-09-30", **kwargs)
        ct_query, ct_params = next((q, p) for q, p in seen if "tabCourier Transaction" in q)
        return out, ct_query, ct_params

    def test_without_as_of_the_kpi_is_todays_balance(self):
        out, query, params = self._kpis()
        self.assertIn("ct.status = 'Unsettled'", query)
        self.assertNotIn("as_of_end", query)
        self.assertNotIn("as_of_end", params)
        self.assertIsNone(out["unsettled_as_of"])
        self.assertEqual(out["unsettled_courier_total"], 120.0)

    def test_as_of_uses_the_balances_table_scope(self):
        out, query, params = self._kpis(as_of="2026-09-30", branch="Dokki")
        self.assertIn("ct.date <= %(as_of_end)s", query)
        self.assertIn("COALESCE(ct.settled_at, ct.date) > %(as_of_end)s", query)
        self.assertIn("bsi.pos_profile = %(branch)s", query)
        self.assertEqual(params["as_of_end"], "2026-09-30 23:59:59")
        self.assertEqual(out["unsettled_as_of"], "2026-09-30")

    def test_dashboard_reads_kpi_and_table_on_to_date(self):
        from unittest.mock import MagicMock, patch

        from jarz_pos.api import shipping_analytics as sa

        loaders = [
            "get_summary_kpis", "get_alerts_data", "get_cost_by_territory", "get_cost_by_sub_territory",
            "get_cost_by_pos_profile", "get_cost_by_courier", "get_custom_shipping_breakdown",
            "get_double_shipping_impact", "get_daily_trend", "get_pickup_vs_delivery_split",
            "get_unsettled_courier_balances", "get_pickup_delivery_trend",
        ]
        mocks = {name: MagicMock(return_value={}) for name in loaders}
        with patch.multiple(sa, _ensure_jarz_manager=MagicMock(), **mocks), patch.object(
            sa, "nowdate", return_value="2026-10-15"
        ):
            out = sa.get_shipping_analytics(from_date="2026-09-01", to_date="2026-09-30", branch="Dokki")
        self.assertEqual(mocks["get_summary_kpis"].call_args.kwargs["as_of"], "2026-09-30")
        self.assertEqual(mocks["get_summary_kpis"].call_args.kwargs["branch"], "Dokki")
        self.assertEqual(mocks["get_unsettled_courier_balances"].call_args.kwargs["as_of"], "2026-09-30")
        self.assertEqual(out["unsettled_as_of"], "2026-09-30")


if __name__ == "__main__":
    unittest.main()

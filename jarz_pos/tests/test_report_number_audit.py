"""Pins the fixes from the 2026-09-27 audit of every Reports-hub number.

Each dashboard was recomputed on production (1-26 Sep 2026) with independent
SQL; three figures disagreed with the ledger. These source checks keep the
fixed query shapes from drifting back. Pure, no site needed.
"""

import os
import re
import unittest

API = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api")


def _src(module):
    with open(os.path.join(API, module + ".py"), encoding="utf-8") as fh:
        return fh.read()


def _function(src, name):
    start = src.index(f"def {name}")
    nxt = re.search(r"\n(def |@frappe)", src[start + 1:])
    return src[start: start + 1 + nxt.start()] if nxt else src[start:]


class TestProductRevenueIsNetOfInvoiceDiscounts(unittest.TestCase):
    def test_line_amount_is_base_net_amount(self):
        src = _src("product_analytics")
        self.assertIn("sii.base_net_amount AS amount", src)
        self.assertNotRegex(src, r"sii\.amount\s*,")


class TestStockValueComesFromTheLedger(unittest.TestCase):
    def test_uses_bin_stock_value_not_item_valuation_rate(self):
        src = _src("inventory_analytics")
        self.assertIn("SUM(b.stock_value)", src)
        self.assertNotIn("i.valuation_rate", src)


class TestB2bClientsAreWhoOrdersB2b(unittest.TestCase):
    def setUp(self):
        self.src = _src("b2b_analytics")

    def test_client_condition_covers_group_or_b2b_orders(self):
        self.assertIn("_B2B_CLIENT_COND", self.src)
        cond = self.src[self.src.index("_B2B_CLIENT_COND ="):]
        cond = cond[: cond.index(')"""') + 4]
        self.assertIn("customer_group IN %(groups)s", cond)
        self.assertIn("custom_order_purpose = %(purpose)s", cond)

    def test_clients_and_at_risk_use_it(self):
        for fn in ("_clients", "_at_risk_clients"):
            body = _function(self.src, fn)
            self.assertIn("{_B2B_CLIENT_COND}", body, fn)
            # Only an f-string interpolates it; a plain string would send the
            # literal braces to MariaDB.
            self.assertRegex(body, r'sql\(\s*f"""[^"]*\{_B2B_CLIENT_COND\}', fn)

    def test_revenue_by_group_is_not_limited_to_b2b_groups(self):
        body = _function(self.src, "_clients")
        rev = body[body.index("Revenue per group"):]
        rev = rev[: rev.index("as_dict=True")]
        self.assertNotIn("customer_group IN %(groups)s", rev)


if __name__ == "__main__":
    unittest.main()

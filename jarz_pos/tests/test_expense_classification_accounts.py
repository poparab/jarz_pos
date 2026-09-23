"""Delivery income, purchase delivery and vehicle costs each have their own ledger.

Until 2026-09 the customer's delivery charge was credited to Freight and
Forwarding Charges — the courier *expense* — so the P&L showed a negative
expense and the revenue never reached income. These tests pin where each
kind of money now goes, and that a site the patch has not reached yet still
invoices (falling back to the old account) instead of failing.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

COMPANY = "JARZ"
ABBR = "J"


def _accounts_db(existing):
    """A frappe.db stand-in whose Account table is ``existing`` {name: row}."""

    def get_value(doctype, filters, fieldname=None, as_dict=False):
        if doctype == "Company":
            return ABBR
        if doctype == "Account" and isinstance(filters, str):
            row = existing.get(filters)
            if row is None:
                return None
            if as_dict:
                return SimpleNamespace(**row)
            return row.get(fieldname) if isinstance(fieldname, str) else row
        return None

    db = MagicMock()
    db.get_value.side_effect = get_value
    db.exists.side_effect = lambda doctype, name=None: doctype == "Account" and name in existing
    return db


LEAF = {"company": COMPANY, "is_group": 0}


class TestShippingIncomeAccount(unittest.TestCase):
    def test_customer_delivery_charge_goes_to_shipping_income(self):
        from jarz_pos.utils import account_utils

        db = _accounts_db({"Shipping Income - J": LEAF, "Freight and Forwarding Charges - J": LEAF})
        with patch.object(account_utils.frappe, "db", db):
            self.assertEqual(account_utils.get_shipping_income_account(COMPANY), "Shipping Income - J")

    def test_never_resolves_to_the_freight_expense_when_income_exists(self):
        """The whole bug: income credited to an expense account."""
        from jarz_pos.utils import account_utils

        db = _accounts_db({"Shipping Income - J": LEAF, "Freight and Forwarding Charges - J": LEAF})
        with patch.object(account_utils.frappe, "db", db):
            self.assertNotIn("Freight", account_utils.get_shipping_income_account(COMPANY))

    def test_unpatched_site_falls_back_to_freight_instead_of_failing(self):
        from jarz_pos.utils import account_utils

        db = _accounts_db({"Freight and Forwarding Charges - J": LEAF})
        with patch.object(account_utils.frappe, "db", db), patch.object(
            account_utils, "get_freight_expense_account", return_value="Freight and Forwarding Charges - J"
        ):
            self.assertEqual(
                account_utils.get_shipping_income_account(COMPANY), "Freight and Forwarding Charges - J"
            )

    def test_a_group_or_foreign_account_is_not_used(self):
        from jarz_pos.utils import account_utils

        for row in ({"company": COMPANY, "is_group": 1}, {"company": "Other", "is_group": 0}):
            db = _accounts_db({"Shipping Income - J": row})
            with patch.object(account_utils.frappe, "db", db), patch.object(
                account_utils, "get_freight_expense_account", return_value="FALLBACK"
            ):
                self.assertEqual(account_utils.get_shipping_income_account(COMPANY), "FALLBACK")

    def test_delivery_utils_writer_uses_shipping_income(self):
        from jarz_pos.utils import delivery_utils

        with patch("jarz_pos.utils.account_utils.get_shipping_income_account", return_value="Shipping Income - J"):
            self.assertEqual(delivery_utils.get_delivery_account(COMPANY), "Shipping Income - J")


class TestPurchaseDeliveryAccount(unittest.TestCase):
    def test_purchase_delivery_has_its_own_ledger(self):
        from jarz_pos.utils import account_utils

        db = _accounts_db({"Purchase Delivery Charges - J": LEAF, "Freight and Forwarding Charges - J": LEAF})
        with patch.object(account_utils.frappe, "db", db):
            self.assertEqual(
                account_utils.get_purchase_delivery_account(COMPANY), "Purchase Delivery Charges - J"
            )

    def test_unpatched_site_falls_back_to_freight(self):
        from jarz_pos.utils import account_utils

        with patch.object(account_utils.frappe, "db", _accounts_db({})), patch.object(
            account_utils, "get_freight_expense_account", return_value="Freight and Forwarding Charges - J"
        ):
            self.assertEqual(
                account_utils.get_purchase_delivery_account(COMPANY), "Freight and Forwarding Charges - J"
            )


class TestCreateAccountsPatch(unittest.TestCase):
    """The patch creates the tree once, under the right parents, and is idempotent."""

    def _run(self, existing):
        from jarz_pos.Patches.v1_9 import create_expense_classification_accounts as mod

        created = []

        def get_doc(values):
            doc = MagicMock()
            doc.name = f"{values['account_name']} - {ABBR}"
            doc.insert.side_effect = lambda: (created.append(values), existing.add(doc.name))
            return doc

        def get_value(doctype, filters, fieldname=None, **_):
            if doctype == "Company":
                return ABBR
            if doctype == "Account" and isinstance(filters, dict) and "name" in filters:
                return filters["name"] if filters["name"] in groups else None
            if doctype == "Account" and fieldname == "account_type":
                return "Chargeable"
            return None

        groups = {"Direct Income - J", "Indirect Expenses - J"}
        fake = MagicMock()
        fake.get_all.return_value = [COMPANY]
        fake.get_doc.side_effect = get_doc
        fake.db.get_value.side_effect = get_value
        fake.db.exists.side_effect = lambda doctype, name=None: name in existing
        fake.db.has_column.return_value = False
        with patch.object(mod, "frappe", fake):
            mod.execute()
        return created

    def test_creates_the_tree_under_the_right_parents(self):
        existing = {"Freight and Forwarding Charges - J"}
        created = {c["account_name"]: c for c in self._run(existing)}

        self.assertEqual(created["Shipping Income"]["parent_account"], "Direct Income - J")
        self.assertEqual(created["Shipping Income"]["account_type"], "Income Account")
        self.assertEqual(created["Purchase Delivery Charges"]["parent_account"], "Indirect Expenses - J")
        self.assertEqual(created["Vehicle Expenses"]["is_group"], 1)
        for leaf in ("Vehicle Fuel", "Vehicle Maintenance and Repairs", "Vehicle Other Expenses"):
            self.assertEqual(created[leaf]["parent_account"], "Vehicle Expenses - J")
            self.assertEqual(created[leaf]["is_group"], 0)

    def test_second_run_creates_nothing(self):
        existing = {"Freight and Forwarding Charges - J"}
        self._run(existing)
        self.assertEqual(self._run(existing), [])


if __name__ == "__main__":
    unittest.main()

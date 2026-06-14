"""Tests for the B2B commercial-policy / order-purpose layer.

Covers, per purpose, the resolver's translation of policy config into the existing
invoice primitives:
  - shipping income suppression
  - no-courier flag (expense zeroing + courier-assignment block)
  - price-list selection / resolution chain
  - permission gating
  - Standard remains completely inert (the regression guard)

These tests are mock / light-DB so they run reliably against a populated site
(``development.localhost``) without the heavy fixture setup the GL-verification
cases require.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import frappe

from jarz_pos.services import commercial_policy as cp
from jarz_pos.services import delivery_handling as dh
from jarz_pos.services import invoice_creation as ic


_SELLING_PRICE_LIST = "Standard Selling"


def _ns(**kwargs):
    """Lightweight stand-in for a Sales Invoice document (attribute access only)."""
    return types.SimpleNamespace(**kwargs)


# NOTE: We deliberately use plain ``unittest.TestCase`` (not FrappeTestCase) for the
# DB-backed cases. On ERPNext v16, FrappeTestCase pulls in ``erpnext.tests.utils``
# whose module-level BootStrapTestData() collides with existing master data on a
# populated site. We instead insert policy rows (uncommitted; visible on the same
# connection) and ``frappe.db.rollback()`` them in tearDown.
class TestCommercialPolicyResolver(unittest.TestCase):
    """Resolver maps each order purpose to the correct primitive flags."""

    PURPOSES = {
        "B2B Supply": dict(income="Zero", expense="Normal", courier="Courier"),
        "Employee": dict(income="Zero", expense="Zero", courier="No Courier"),
        "Sample - Courier": dict(income="Zero", expense="Normal", courier="Courier", discount=100),
        "Sample - No Courier": dict(income="Zero", expense="Zero", courier="No Courier", discount=100),
        "Free Shipping Waiver": dict(income="Zero", expense="Normal", courier="Courier"),
    }

    def tearDown(self):
        frappe.db.rollback()

    def setUp(self):
        self._policies = {}
        for purpose, cfg in self.PURPOSES.items():
            doc = frappe.get_doc(
                {
                    "doctype": "Jarz Commercial Policy",
                    "policy_name": f"_TEST {purpose}",
                    "enabled": 1,
                    "order_purpose": purpose,
                    "price_list": _SELLING_PRICE_LIST,
                    "discount_percentage": cfg.get("discount", 0),
                    "shipping_income_behavior": cfg["income"],
                    "shipping_expense_behavior": cfg["expense"],
                    "courier_behavior": cfg["courier"],
                }
            ).insert(ignore_permissions=True)
            self._policies[purpose] = doc.name

    def _resolve(self, purpose):
        return cp.resolve_commercial_policy(
            order_purpose=purpose,
            commercial_policy=self._policies[purpose],
        )

    # --- Standard is inert (regression guard) -----------------------------
    def test_standard_explicit_inert(self):
        d = cp.resolve_commercial_policy(order_purpose="Standard")
        self.assertFalse(d.matched)
        self.assertEqual(d.order_purpose, "Standard")
        self.assertFalse(d.suppress_shipping_income)
        self.assertFalse(d.suppress_legacy_delivery_charges)
        self.assertFalse(d.no_courier)
        self.assertIsNone(d.price_list)

    def test_absent_inert(self):
        d = cp.resolve_commercial_policy()
        self.assertFalse(d.matched)
        self.assertEqual(d.order_purpose, "Standard")
        self.assertFalse(d.no_courier)

    # --- Per-purpose mapping ----------------------------------------------
    def test_b2b_supply(self):
        d = self._resolve("B2B Supply")
        self.assertTrue(d.matched)
        self.assertTrue(d.suppress_shipping_income)      # income waived
        self.assertTrue(d.suppress_legacy_delivery_charges)
        self.assertFalse(d.no_courier)                   # courier expense retained
        self.assertEqual(d.price_list, _SELLING_PRICE_LIST)

    def test_employee(self):
        d = self._resolve("Employee")
        self.assertTrue(d.suppress_shipping_income)
        self.assertTrue(d.no_courier)                    # expense zero + no courier

    def test_sample_courier(self):
        d = self._resolve("Sample - Courier")
        self.assertTrue(d.suppress_shipping_income)
        self.assertFalse(d.no_courier)
        self.assertEqual(d.discount_percentage, 100)

    def test_sample_no_courier(self):
        d = self._resolve("Sample - No Courier")
        self.assertTrue(d.suppress_shipping_income)
        self.assertTrue(d.no_courier)
        self.assertEqual(d.discount_percentage, 100)

    def test_free_shipping_waiver(self):
        d = self._resolve("Free Shipping Waiver")
        self.assertTrue(d.suppress_shipping_income)
        self.assertFalse(d.no_courier)                   # courier expense retained

    # --- Resolve by purpose without an explicit policy name ----------------
    def test_resolve_by_purpose_only(self):
        d = cp.resolve_commercial_policy(order_purpose="Employee")
        self.assertTrue(d.matched)
        self.assertTrue(d.no_courier)

    def test_unknown_policy_name_throws(self):
        with self.assertRaises(frappe.ValidationError):
            cp.resolve_commercial_policy(commercial_policy="_TEST does-not-exist")


class TestCommercialPolicyValidation(unittest.TestCase):
    """DocType controller invariants."""

    def tearDown(self):
        frappe.db.rollback()

    def test_no_courier_requires_zero_expense(self):
        doc = frappe.get_doc(
            {
                "doctype": "Jarz Commercial Policy",
                "policy_name": "_TEST invalid no-courier",
                "order_purpose": "Employee",
                "courier_behavior": "No Courier",
                "shipping_expense_behavior": "Normal",  # inconsistent
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_discount_out_of_range_throws(self):
        doc = frappe.get_doc(
            {
                "doctype": "Jarz Commercial Policy",
                "policy_name": "_TEST bad discount",
                "order_purpose": "Sample - Courier",
                "discount_percentage": 150,
                "shipping_income_behavior": "Zero",
                "shipping_expense_behavior": "Normal",
                "courier_behavior": "Courier",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)


class TestCommercialPolicyPermission(unittest.TestCase):
    """Gating: require_role overrides the default manager-pricing access."""

    def test_require_role_denied(self):
        policy = _ns(order_purpose="B2B Supply", require_role="Special B2B Role")
        with patch.object(cp.frappe, "get_roles", return_value=["Sales User"]):
            with self.assertRaises(Exception):
                cp._ensure_policy_permission(policy)

    def test_require_role_granted(self):
        policy = _ns(order_purpose="B2B Supply", require_role="Special B2B Role")
        with patch.object(cp.frappe, "get_roles", return_value=["Special B2B Role"]):
            cp._ensure_policy_permission(policy)  # should not raise

    def test_default_manager_path_denied(self):
        policy = _ns(order_purpose="B2B Supply", require_role=None)
        with patch(
            "jarz_pos.services.invoice_creation._has_manager_pricing_access",
            return_value=False,
        ):
            with self.assertRaises(Exception):
                cp._ensure_policy_permission(policy)


class TestNoCourierSuppression(unittest.TestCase):
    """custom_no_courier zeroes courier expense and blocks courier assignment."""

    def test_expense_zeroed_for_no_courier(self):
        inv = _ns(name="_TEST-NC-1", custom_is_pickup=0, custom_no_courier=1, items=[])
        self.assertEqual(dh._get_delivery_expense_amount(inv), 0.0)

    def test_mark_courier_outstanding_blocks_no_courier(self):
        inv = _ns(name="_TEST-NC-2", custom_is_pickup=0, custom_no_courier=1)
        with patch.object(dh.frappe, "get_doc", return_value=inv):
            with self.assertRaises(Exception):
                dh.mark_courier_outstanding("_TEST-NC-2", party_type="Employee", party="X")


class TestPriceListChainHelpers(unittest.TestCase):
    """Resolution-chain helpers degrade gracefully and honor priority."""

    def test_sales_partner_none(self):
        self.assertIsNone(ic._resolve_sales_partner_price_list(None))

    def test_customer_default_price_list(self):
        cust = _ns(default_price_list="B2B Selling", customer_group=None)
        self.assertEqual(ic._resolve_customer_price_list(cust), "B2B Selling")

    def test_customer_none(self):
        self.assertIsNone(ic._resolve_customer_price_list(None))


class TestEffectivePriceListGating(unittest.TestCase):
    """The price-list resolution chain must keep Standard orders byte-identical."""

    def setUp(self):
        self.pos = _ns(selling_price_list=_SELLING_PRICE_LIST, name="_TEST POS", company=None)
        self.logger = frappe.logger("jarz_pos.test")

    def test_standard_ignores_customer_price_list(self):
        # A Standard order for a customer with a default_price_list must STILL resolve
        # to the POS Profile default — the new tiers only apply to matched policies.
        cust = _ns(default_price_list="Some Other List", customer_group=None)
        eff = ic._resolve_effective_price_list(
            self.pos, [],
            requested_price_list=None,
            suppress_shipping_income=None,
            suppress_legacy_delivery_charges=None,
            logger=self.logger,
            policy_matched=False,
            policy_price_list=None,
            customer_doc=cust,
            sales_partner=None,
        )
        self.assertEqual(eff, _SELLING_PRICE_LIST)

    def test_matched_uses_policy_price_list(self):
        eff = ic._resolve_effective_price_list(
            self.pos, [],
            requested_price_list=None,
            suppress_shipping_income=None,
            suppress_legacy_delivery_charges=None,
            logger=self.logger,
            policy_matched=True,
            policy_price_list=_SELLING_PRICE_LIST,
            customer_doc=None,
            sales_partner=None,
        )
        self.assertEqual(eff, _SELLING_PRICE_LIST)


class TestCustomerTierPricing(unittest.TestCase):
    """Model B: B2B tier resolves from Customer/Customer Group; per-customer override."""

    def tearDown(self):
        frappe.db.rollback()

    def test_resolve_customer_price_list_from_group(self):
        from jarz_pos.api.pos import resolve_customer_price_list

        group = "_TEST B2B Tier"
        if not frappe.db.exists("Customer Group", group):
            frappe.get_doc({
                "doctype": "Customer Group", "customer_group_name": group,
                "parent_customer_group": "All Customer Groups", "is_group": 0,
            }).insert(ignore_permissions=True)
        frappe.db.set_value("Customer Group", group, "default_price_list", _SELLING_PRICE_LIST)
        cust = "_TEST Tier Cust"
        if not frappe.db.exists("Customer", cust):
            frappe.get_doc({
                "doctype": "Customer", "customer_name": cust, "customer_type": "Company",
                "customer_group": group, "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
            }).insert(ignore_permissions=True)
        else:
            frappe.db.set_value("Customer", cust, "customer_group", group)
        self.assertEqual(
            resolve_customer_price_list(cust)["price_list"], _SELLING_PRICE_LIST
        )

    def test_resolve_customer_price_list_unknown(self):
        from jarz_pos.api.pos import resolve_customer_price_list

        self.assertIsNone(resolve_customer_price_list("_TEST nope")["price_list"])

    def test_per_customer_item_price_override(self):
        # A customer-scoped Item Price wins over the generic list rate, and must NOT
        # leak into a different customer's order. Use a dedicated price list so no
        # pre-existing Item Price rows interfere.
        code = frappe.db.get_value("Item", {"is_sales_item": 1, "disabled": 0}, "name")
        plist = "_TEST Tier List"
        if not frappe.db.exists("Price List", plist):
            frappe.get_doc({
                "doctype": "Price List", "price_list_name": plist,
                "selling": 1, "currency": "EGP", "enabled": 1,
            }).insert(ignore_permissions=True)
        cust = "_TEST Override Cust"
        if not frappe.db.exists("Customer", cust):
            frappe.get_doc({
                "doctype": "Customer", "customer_name": cust, "customer_type": "Company",
                "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
                "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
            }).insert(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Item Price", "item_code": code, "price_list": plist,
            "selling": 1, "price_list_rate": 123,
        }).insert(ignore_permissions=True)
        frappe.get_doc({
            "doctype": "Item Price", "item_code": code, "price_list": plist,
            "customer": cust, "selling": 1, "price_list_rate": 99,
        }).insert(ignore_permissions=True)
        # Generic (no customer / other customer) -> 123; the scoped customer -> 99.
        self.assertEqual(ic._resolve_item_rate(code, plist), 123.0)
        self.assertEqual(ic._resolve_item_rate(code, plist, customer="_TEST nope"), 123.0)
        self.assertEqual(ic._resolve_item_rate(code, plist, customer=cust), 99.0)


if __name__ == "__main__":
    unittest.main()

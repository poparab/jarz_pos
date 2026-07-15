"""Manager-pricing gate tests for the price-list override path (pure mock).

Regression guard for JARZ-FRAPPE-BACKEND-2V ("Not permitted: manager pricing access
required" raised from ``_resolve_effective_price_list`` during ``create_pos_invoice``):

The POS client resolves a B2B/tier customer's price list server-side via
``jarz_pos.api.pos.resolve_customer_price_list``, applies it to the cart so displayed
prices are correct, then echoes that SAME name back as ``price_list`` at checkout. The
gate used to treat any ``requested != POS Profile default`` as a manual manager override,
so a cashier / B2B Sales Rep could not create a B2B invoice AT ALL.

The gate now trips only when the requested list is NOT auto-derivable for this exact
order context. That set mirrors ``_resolve_effective_price_list``'s own chain: the POS
Profile default always, plus the policy / sales-partner / customer lists ONLY on a
matched-policy order. A Standard (retail) order therefore still requires manager access
for anything other than the profile default — including the customer's own B2B tier list,
which would otherwise be unapproved B2B pricing on a retail sale.

Everything else that required manager access before still does: ``suppress_shipping_income``,
``suppress_legacy_delivery_charges`` and any line-level ``custom_rate_override`` /
``discount_amount`` / ``discount_percentage``.

Fully mocked (roles, Price List existence, customer/partner lookups) so the permission
matrix is asserted deterministically with no DB / fixture dependency.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import frappe

from jarz_pos.services import invoice_creation as ic


_DEFAULT_PL = "Standard Selling"   # the POS Profile's selling_price_list
_TIER_PL = "B2B Selling"           # the customer's own tier list (server-derivable)
_PARTNER_PL = "Partner Selling"    # a sales partner's list (server-derivable)
_POLICY_PL = "Policy Selling"      # a matched commercial policy's list (server-derivable)
_ARBITRARY_PL = "Wholesale Deep Discount"  # unrelated -> a genuine manual override


def _ns(**kwargs):
    """Lightweight stand-in for a document (attribute access only)."""
    return types.SimpleNamespace(**kwargs)


class _GateTestCase(unittest.TestCase):
    """Shared harness: a POS profile, a tier customer, and role/DB stubs."""

    def setUp(self):
        self.pos = _ns(selling_price_list=_DEFAULT_PL, name="_TEST POS", company=None)
        self.logger = frappe.logger("jarz_pos.test")
        # Customer whose own default_price_list is the B2B tier list — exactly what
        # resolve_customer_price_list would hand the client.
        self.customer = _ns(
            name="_TEST Tier Cust",
            default_price_list=_TIER_PL,
            customer_group=None,
        )

    def _resolve(self, *, is_manager, requested=None, cart=None, customer=None,
                 sales_partner=None, policy_matched=False, policy_price_list=None,
                 suppress_shipping_income=None, suppress_legacy_delivery_charges=None):
        """Run _resolve_effective_price_list with roles + Price List existence stubbed."""
        with patch.object(
            ic, "_has_manager_pricing_access", return_value=is_manager
        ), patch.object(
            ic.frappe.db, "exists", return_value=True
        ), patch.object(
            ic.frappe.db, "get_value", side_effect=self._get_value
        ):
            return ic._resolve_effective_price_list(
                self.pos,
                cart if cart is not None else [],
                requested_price_list=requested,
                suppress_shipping_income=suppress_shipping_income,
                suppress_legacy_delivery_charges=suppress_legacy_delivery_charges,
                logger=self.logger,
                policy_matched=policy_matched,
                policy_price_list=policy_price_list,
                customer_doc=self.customer if customer is None else customer,
                sales_partner=sales_partner,
            )

    @staticmethod
    def _get_value(doctype, *args, **kwargs):
        # Sales Partner -> its configured price list; Customer Group -> none (the test
        # customer carries default_price_list directly).
        if doctype == "Sales Partner":
            return _PARTNER_PL
        return None


class TestPriceListManagerGate(_GateTestCase):
    """(a)-(f) from the fix spec: who may request which price list."""

    # (f) requested == default -> allowed, and the role check must never run.
    def test_requested_equals_default_never_checks_role(self):
        def _boom():
            raise AssertionError("role check must not run when requested == default")

        with patch.object(ic, "_has_manager_pricing_access", side_effect=_boom), patch.object(
            ic.frappe.db, "exists", return_value=True
        ):
            eff = ic._resolve_effective_price_list(
                self.pos, [],
                requested_price_list=_DEFAULT_PL,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
                logger=self.logger,
                policy_matched=False,
                policy_price_list=None,
                customer_doc=None,
                sales_partner=None,
            )
        self.assertEqual(eff, _DEFAULT_PL)

    def test_no_requested_price_list_allowed_for_cashier(self):
        eff = self._resolve(is_manager=False, requested=None)
        self.assertEqual(eff, _DEFAULT_PL)

    # (a) cashier + customer's own tier price list != profile default -> ALLOWED.
    # This is the exact JARZ-FRAPPE-BACKEND-2V regression.
    def test_cashier_may_use_customer_tier_price_list(self):
        eff = self._resolve(
            is_manager=False,
            requested=_TIER_PL,
            policy_matched=True,
            policy_price_list=None,
        )
        self.assertEqual(eff, _TIER_PL)

    def test_cashier_may_use_customer_tier_price_list_from_group(self):
        # Same echo, but the tier comes from Customer Group.default_price_list.
        cust = _ns(name="_TEST Grp Cust", default_price_list=None, customer_group="_TEST Grp")

        def _gv(doctype, *args, **kwargs):
            if doctype == "Customer Group":
                return _TIER_PL
            return None

        with patch.object(ic, "_has_manager_pricing_access", return_value=False), patch.object(
            ic.frappe.db, "exists", return_value=True
        ), patch.object(ic.frappe.db, "get_value", side_effect=_gv):
            eff = ic._resolve_effective_price_list(
                self.pos, [],
                requested_price_list=_TIER_PL,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
                logger=self.logger,
                policy_matched=True,
                policy_price_list=None,
                customer_doc=cust,
                sales_partner=None,
            )
        self.assertEqual(eff, _TIER_PL)

    def test_cashier_may_use_policy_price_list(self):
        eff = self._resolve(
            is_manager=False,
            requested=_POLICY_PL,
            policy_matched=True,
            policy_price_list=_POLICY_PL,
        )
        self.assertEqual(eff, _POLICY_PL)

    def test_cashier_may_use_sales_partner_price_list(self):
        eff = self._resolve(
            is_manager=False,
            requested=_PARTNER_PL,
            sales_partner="_TEST Partner",
            policy_matched=True,
        )
        self.assertEqual(eff, _PARTNER_PL)

    # (b) cashier + arbitrary / unrelated price list -> STILL BLOCKED.
    def test_cashier_blocked_on_arbitrary_price_list(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, requested=_ARBITRARY_PL, policy_matched=True)

    def test_cashier_blocked_on_arbitrary_price_list_standard_order(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, requested=_ARBITRARY_PL, policy_matched=False)

    def test_cashier_blocked_on_other_customers_tier_list(self):
        # The echo bypass is scoped to THIS order's customer: a cashier cannot borrow a
        # tier list that this customer has no claim to.
        cust = _ns(name="_TEST Plain", default_price_list=None, customer_group=None)
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, requested=_TIER_PL, customer=cust,
                          policy_matched=True)

    def test_cashier_blocked_on_customer_tier_list_for_standard_order(self):
        # CONTRACT: the bypass mirrors the server's OWN chain. A Standard (retail) order
        # resolves strictly `requested or default_price_list` — the customer/partner/policy
        # candidates are unreachable — so the tier list is NOT server-derivable here and
        # stays manager-gated. Letting it through would hand a cashier cheaper B2B tier
        # pricing on a retail sale with no manager approval, which is the exact abuse this
        # gate exists to stop. Same customer + same list as
        # test_cashier_may_use_customer_tier_price_list, which passes ONLY because that
        # order is policy_matched. These two tests are a pair: keep them in sync.
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, requested=_TIER_PL, policy_matched=False)

    def test_cashier_blocked_on_sales_partner_list_for_standard_order(self):
        # Same rule for the sales-partner candidate: unreachable on a Standard order.
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, requested=_PARTNER_PL,
                          sales_partner="_TEST Partner", policy_matched=False)

    # (e) manager + any -> ALLOWED.
    def test_manager_may_use_arbitrary_price_list(self):
        eff = self._resolve(is_manager=True, requested=_ARBITRARY_PL, policy_matched=True)
        self.assertEqual(eff, _ARBITRARY_PL)

    def test_manager_may_use_customer_tier_list_for_standard_order(self):
        # The Standard-order restriction is a MANAGER gate, not a prohibition: a manager
        # may still deliberately price a retail order off the customer's tier list.
        eff = self._resolve(is_manager=True, requested=_TIER_PL, policy_matched=False)
        self.assertEqual(eff, _TIER_PL)


class TestOtherManagerTriggersUnchanged(_GateTestCase):
    """The non-price-list manager triggers must be COMPLETELY unaffected by the fix."""

    # (c) cashier + item discount_percentage -> STILL BLOCKED.
    def test_cashier_blocked_on_item_discount_percentage(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, cart=[{"item_code": "X", "discount_percentage": 10}])

    def test_cashier_blocked_on_item_discount_amount(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, cart=[{"item_code": "X", "discount_amount": 5}])

    def test_cashier_blocked_on_custom_rate_override(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, cart=[{"item_code": "X", "custom_rate_override": 1}])

    # (d) cashier + suppress_shipping_income=True -> STILL BLOCKED.
    def test_cashier_blocked_on_suppress_shipping_income(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, suppress_shipping_income=True)

    def test_cashier_blocked_on_suppress_legacy_delivery_charges(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(is_manager=False, suppress_legacy_delivery_charges=True)

    def test_auto_derivable_price_list_does_not_rescue_a_line_discount(self):
        # A legitimate tier echo must NOT launder a manager-only line discount through
        # the gate: the item trigger still fires.
        with self.assertRaises(frappe.ValidationError):
            self._resolve(
                is_manager=False,
                requested=_TIER_PL,
                policy_matched=True,
                cart=[{"item_code": "X", "discount_percentage": 10}],
            )

    def test_manager_allowed_with_discounts_and_suppression(self):
        eff = self._resolve(
            is_manager=True,
            requested=_TIER_PL,
            policy_matched=True,
            suppress_shipping_income=True,
            cart=[{"item_code": "X", "discount_percentage": 10}],
        )
        self.assertEqual(eff, _TIER_PL)


class TestPricingActionRequiresManagerUnit(unittest.TestCase):
    """Direct unit coverage of the predicate, independent of the resolver."""

    def test_arbitrary_list_requires_manager(self):
        self.assertTrue(
            ic._pricing_action_requires_manager(
                [],
                requested_price_list=_ARBITRARY_PL,
                default_price_list=_DEFAULT_PL,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
                auto_derivable_price_lists={_TIER_PL},
            )
        )

    def test_auto_derivable_list_does_not_require_manager(self):
        self.assertFalse(
            ic._pricing_action_requires_manager(
                [],
                requested_price_list=_TIER_PL,
                default_price_list=_DEFAULT_PL,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
                auto_derivable_price_lists={_TIER_PL},
            )
        )

    def test_omitted_auto_derivable_set_preserves_legacy_behavior(self):
        # Back-compat: with no set supplied, any non-default list is an override.
        self.assertTrue(
            ic._pricing_action_requires_manager(
                [],
                requested_price_list=_TIER_PL,
                default_price_list=_DEFAULT_PL,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
            )
        )

    def test_whitespace_only_requested_is_not_an_override(self):
        self.assertFalse(
            ic._pricing_action_requires_manager(
                [],
                requested_price_list="   ",
                default_price_list=_DEFAULT_PL,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
            )
        )

    def test_non_dict_cart_rows_are_ignored(self):
        self.assertFalse(
            ic._pricing_action_requires_manager(
                ["not-a-dict", None],
                requested_price_list=None,
                default_price_list=_DEFAULT_PL,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
            )
        )


class TestAutoDerivablePriceLists(unittest.TestCase):
    """The auto-derivable set mirrors _resolve_effective_price_list's own chain."""

    def test_matched_collects_every_source(self):
        cust = _ns(default_price_list=_TIER_PL, customer_group=None)
        with patch.object(ic.frappe.db, "get_value", return_value=_PARTNER_PL):
            got = ic._auto_derivable_price_lists(
                default_price_list=_DEFAULT_PL,
                policy_matched=True,
                policy_price_list=_POLICY_PL,
                customer_doc=cust,
                sales_partner="_TEST Partner",
            )
        self.assertEqual(got, {_DEFAULT_PL, _POLICY_PL, _PARTNER_PL, _TIER_PL})

    def test_standard_yields_only_the_profile_default(self):
        # The Standard branch of the chain is `requested or default_price_list`, so no
        # customer / partner / policy list is derivable — mirrored exactly here.
        cust = _ns(default_price_list=_TIER_PL, customer_group=None)
        got = ic._auto_derivable_price_lists(
            default_price_list=_DEFAULT_PL,
            policy_matched=False,
            policy_price_list=_POLICY_PL,
            customer_doc=cust,
            sales_partner="_TEST Partner",
        )
        self.assertEqual(got, {_DEFAULT_PL})

    def test_standard_runs_no_queries(self):
        # Performance guard: the Standard path must not pay for partner/customer lookups.
        def _boom(*a, **k):
            raise AssertionError("no DB lookup may run for a Standard order")

        cust = _ns(default_price_list=None, customer_group="_TEST Grp")
        with patch.object(ic.frappe.db, "get_value", side_effect=_boom):
            got = ic._auto_derivable_price_lists(
                default_price_list=_DEFAULT_PL,
                policy_matched=False,
                policy_price_list=None,
                customer_doc=cust,
                sales_partner="_TEST Partner",
            )
        self.assertEqual(got, {_DEFAULT_PL})

    def test_default_policy_matched_is_false(self):
        # Fail closed: an omitted policy_matched must not open the bypass.
        cust = _ns(default_price_list=_TIER_PL, customer_group=None)
        got = ic._auto_derivable_price_lists(
            default_price_list=_DEFAULT_PL, customer_doc=cust
        )
        self.assertEqual(got, {_DEFAULT_PL})

    def test_excludes_empty_and_none(self):
        got = ic._auto_derivable_price_lists(
            default_price_list=_DEFAULT_PL,
            policy_matched=True,
            policy_price_list="  ",
            customer_doc=None,
            sales_partner=None,
        )
        self.assertEqual(got, {_DEFAULT_PL})

    def test_never_contains_an_unrelated_list(self):
        cust = _ns(default_price_list=_TIER_PL, customer_group=None)
        got = ic._auto_derivable_price_lists(
            default_price_list=_DEFAULT_PL,
            policy_matched=True,
            policy_price_list=None,
            customer_doc=cust,
            sales_partner=None,
        )
        self.assertNotIn(_ARBITRARY_PL, got)

    def test_customer_lookup_failure_degrades_gracefully(self):
        # A raising DB layer must not break invoice creation — the set just shrinks.
        cust = _ns(default_price_list=None, customer_group="_TEST Grp")
        with patch.object(ic.frappe.db, "get_value", side_effect=Exception("db down")):
            got = ic._auto_derivable_price_lists(
                default_price_list=_DEFAULT_PL,
                policy_matched=True,
                policy_price_list=None,
                customer_doc=cust,
                sales_partner="_TEST Partner",
            )
        self.assertEqual(got, {_DEFAULT_PL})


if __name__ == "__main__":
    unittest.main()

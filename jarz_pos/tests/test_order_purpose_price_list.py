"""Order Purpose drives the Price List (pure mock).

Production carried Sample - Courier invoices and a B2B Supply invoice booked at
Standard Selling: the cart let a manager pick the purpose and the list independently,
and ``_resolve_effective_price_list`` resolved ``requested or policy_pl``, so a
client-sent list silently beat the policy's own. The server now refuses a contradiction:

  1. matched policy WITH a price list (Employee, Sample): the request must be that list;
  2. matched B2B Supply policy WITHOUT one: the request must be ANY list the server can
     derive for that context (partner list, customer/group tier, B2B baseline when there
     is no selling tier; POS/company default only when none of those exist), because the
     POS cart resolves the tier without the partner;
  3. Standard / any other list-less purpose (Free Shipping Waiver): the request must be
     the POS Profile's default list. The cart's Price List dropdown is being removed, and
     "Selling Bundle of 3/4" (on no production invoice, three item prices each) used to
     slip through because rule 3 only refused RESERVED lists. A profile with no default
     keeps that reserved-only check.

Plus the amendment exemption (a replacement may keep its cancelled source's own list),
``commercial_policy.reserved_price_lists`` and the additive ``reserved_for_purposes``
field on ``api/pos.get_pos_price_lists``.

Everything is mocked (roles, Price List / Sales Invoice lookups, the policy query) so the
matrix is asserted deterministically with no DB or fixture dependency.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import pos
from jarz_pos.services import commercial_policy as cp
from jarz_pos.services import invoice_creation as ic


_PROFILE = "Dokki"
_DEFAULT = "Standard Selling"
_BUNDLE3 = "Selling Bundle of 3"
_B2B = "B2B Selling"
_EMPLOYEE = "Employee"
_SAMPLE = "Sample"
_TIER = "Cafes"
_PARTNER_PL = "Partner Selling"

_SAMPLE_COURIER = "Sample - Courier"
_SAMPLE_NO_COURIER = "Sample - No Courier"

#: What reserved_price_lists returns for Dokki on a production-shaped site.
_RESERVED = {
    _EMPLOYEE: {"Employee"},
    _SAMPLE: {_SAMPLE_COURIER, _SAMPLE_NO_COURIER},
    _B2B: {"B2B Supply"},
}


def _ns(**kwargs):
    """Lightweight stand-in for a document (attribute access only)."""
    return types.SimpleNamespace(**kwargs)


def _untiered():
    return _ns(name="_TEST Untiered", default_price_list=None, customer_group=None)


def _tiered():
    return _ns(name="_TEST Cafe", default_price_list=_TIER, customer_group=None)


class _PurposeCase(unittest.TestCase):
    """Shared harness around ``ic._resolve_effective_price_list``."""

    def setUp(self):
        self.pos = _ns(name=_PROFILE, selling_price_list=_DEFAULT, company=None)
        self.logger = MagicMock()
        self.reserved_mock = None

    def _resolve(
        self,
        requested,
        *,
        matched=False,
        purpose=None,
        policy_pl=None,
        customer=None,
        sales_partner=None,
        partner_pl=None,
        amended_from=None,
        source_invoice=None,
        live_replacement=False,
        is_manager=True,
        reserved=None,
        reserved_side_effect=None,
        gate_side_effect=None,
    ):
        def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
            if doctype == "Price List":
                return {"enabled": 1, "selling": 1}
            if doctype == "Sales Partner":
                return partner_pl
            if doctype == "Sales Invoice":
                return source_invoice
            return None

        def _exists(doctype, filters=None, *args, **kwargs):
            if doctype == "Sales Invoice":
                return live_replacement
            return True

        reserved_kwargs = (
            {"side_effect": reserved_side_effect}
            if reserved_side_effect is not None
            else {"return_value": _RESERVED if reserved is None else reserved}
        )
        gate_patch = (
            patch.object(ic, "_ensure_manager_pricing_access", side_effect=gate_side_effect)
            if gate_side_effect is not None
            else patch.object(ic, "_has_manager_pricing_access", return_value=is_manager)
        )
        with gate_patch, patch.object(
            ic.frappe.db, "get_value", side_effect=_get_value
        ), patch.object(ic.frappe.db, "exists", side_effect=_exists), patch.object(
            ic._commercial_policy, "reserved_price_lists", **reserved_kwargs
        ) as reserved_mock:
            self.reserved_mock = reserved_mock
            return ic._resolve_effective_price_list(
                self.pos,
                [],
                requested_price_list=requested,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
                logger=self.logger,
                policy_matched=matched,
                policy_price_list=policy_pl,
                policy_order_purpose=purpose,
                customer_doc=customer,
                sales_partner=sales_partner,
                amended_from=amended_from,
            )

    def assertMismatch(self, fragments, **kwargs):
        with self.assertRaises(frappe.ValidationError) as cm:
            self._resolve(**kwargs)
        message = str(cm.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return message


# ---------------------------------------------------------------------------
# Rule 1: a policy that owns a list locks it
# ---------------------------------------------------------------------------

class TestRule1PolicyOwnsItsList(_PurposeCase):
    def _employee(self, requested, **kwargs):
        return self._resolve(
            requested, matched=True, purpose="Employee", policy_pl=_EMPLOYEE, **kwargs
        )

    def test_employee_purpose_accepts_employee_list(self):
        # A cashier too: the policy's own list is server-derivable, so no manager gate.
        self.assertEqual(self._employee(_EMPLOYEE, is_manager=False), _EMPLOYEE)

    def test_employee_purpose_rejects_standard_selling(self):
        # Even though Standard Selling IS the profile default — a default request is no
        # excuse on a purpose that owns a list.
        self.assertMismatch(
            ["Order purpose Employee must use price list Employee", _DEFAULT],
            requested=_DEFAULT, matched=True, purpose="Employee", policy_pl=_EMPLOYEE,
        )

    def test_sample_purpose_rejects_other_retail_list(self):
        self.assertMismatch(
            [_SAMPLE_COURIER, "must use price list Sample", _BUNDLE3],
            requested=_BUNDLE3, matched=True, purpose=_SAMPLE_COURIER, policy_pl=_SAMPLE,
        )

    def test_sample_purpose_accepts_sample_list(self):
        self.assertEqual(
            self._resolve(_SAMPLE, matched=True, purpose=_SAMPLE_NO_COURIER, policy_pl=_SAMPLE),
            _SAMPLE,
        )

    def test_check_runs_before_the_manager_gate(self):
        # A manager used to pass the gate and book the wrong list silently. The gate is
        # booby-trapped: reaching it would raise AssertionError, not ValidationError. The
        # request is a non-default, non-derivable list, so the gate WOULD be consulted.
        self.assertMismatch(
            ["must use price list Sample", _BUNDLE3],
            requested=_BUNDLE3, matched=True, purpose=_SAMPLE_COURIER, policy_pl=_SAMPLE,
            gate_side_effect=AssertionError("manager gate reached before the purpose check"),
        )

    def test_empty_request_still_resolves_to_policy_list(self):
        for requested in (None, "", "   "):
            with self.subTest(requested=requested):
                self.assertEqual(self._employee(requested, is_manager=False), _EMPLOYEE)

    def test_rule1_never_queries_reserved_lists(self):
        self._resolve(
            _EMPLOYEE, matched=True, purpose="Employee", policy_pl=_EMPLOYEE,
            reserved_side_effect=AssertionError("reserved map is rule 3 only"),
        )

    def test_case_variant_of_policy_list_is_accepted(self):
        # MariaDB treats "employee" and "Employee" as the same list.
        self.assertEqual(self._employee("employee"), "employee")


# ---------------------------------------------------------------------------
# Rule 2: B2B Supply must use what the chain derives
# ---------------------------------------------------------------------------

class TestRule2B2BSupplyUsesDerivedList(_PurposeCase):
    def _b2b(self, requested, **kwargs):
        return self._resolve(requested, matched=True, purpose="B2B Supply", **kwargs)

    def test_accepts_customer_tier_echo(self):
        # The rep's cart echoes resolve_customer_price_list's answer: valid, no gate.
        self.assertEqual(self._b2b(_TIER, customer=_tiered(), is_manager=False), _TIER)

    def test_rejects_retail_list_for_tiered_customer(self):
        self.assertMismatch(
            ["Order purpose B2B Supply must use price list Cafes", _DEFAULT],
            requested=_DEFAULT, matched=True, purpose="B2B Supply", customer=_tiered(),
        )

    def test_rejects_baseline_when_a_tier_is_configured(self):
        self.assertMismatch(
            ["must use price list Cafes", _B2B],
            requested=_B2B, matched=True, purpose="B2B Supply", customer=_tiered(),
        )

    def test_untiered_customer_accepts_baseline(self):
        self.assertEqual(self._b2b(_B2B, customer=_untiered(), is_manager=False), _B2B)

    def test_untiered_customer_rejects_standard_selling(self):
        # The exact production mismatch: a B2B Supply invoice at Standard Selling.
        self.assertMismatch(
            ["must use price list B2B Selling", _DEFAULT],
            requested=_DEFAULT, matched=True, purpose="B2B Supply", customer=_untiered(),
        )

    def test_empty_request_derives_baseline(self):
        self.assertEqual(self._b2b(None, customer=_untiered(), is_manager=False), _B2B)

    def test_sales_partner_list_and_customer_tier_are_both_accepted(self):
        # The chain picks the partner's list with no request, but the POS cart prices
        # through resolve_customer_price_list, which ignores the partner and echoes the
        # customer tier. Refusing that echo refused every B2B order with such a partner.
        partner = dict(customer=_tiered(), sales_partner="_TEST Partner", partner_pl=_PARTNER_PL)
        self.assertEqual(self._b2b(_PARTNER_PL, **partner), _PARTNER_PL)
        self.assertEqual(self._b2b(_TIER, is_manager=False, **partner), _TIER)
        # Retail is still refused, and the message names every acceptable list.
        self.assertMismatch(
            ["must use price list Partner Selling or Cafes", _DEFAULT],
            requested=_DEFAULT, matched=True, purpose="B2B Supply", **partner,
        )

    def test_sales_partner_does_not_unlock_the_baseline_over_a_tier(self):
        self.assertMismatch(
            ["must use price list Partner Selling or Cafes", _B2B],
            requested=_B2B, matched=True, purpose="B2B Supply", customer=_tiered(),
            sales_partner="_TEST Partner", partner_pl=_PARTNER_PL,
        )

    def test_untiered_customer_with_a_partner_accepts_partner_list_or_baseline(self):
        partner = dict(customer=_untiered(), sales_partner="_TEST Partner", partner_pl=_PARTNER_PL)
        self.assertEqual(self._b2b(_PARTNER_PL, **partner), _PARTNER_PL)
        self.assertEqual(self._b2b(_B2B, is_manager=False, **partner), _B2B)
        self.assertMismatch(
            ["must use price list Partner Selling or B2B Selling", _DEFAULT],
            requested=_DEFAULT, matched=True, purpose="B2B Supply", **partner,
        )

    def test_effective_list_without_a_request_is_still_the_partner_list(self):
        self.assertEqual(
            self._b2b(None, customer=_tiered(), sales_partner="_TEST Partner",
                      partner_pl=_PARTNER_PL),
            _PARTNER_PL,
        )

    def test_rule2_never_queries_reserved_lists(self):
        self._b2b(
            _B2B, customer=_untiered(),
            reserved_side_effect=AssertionError("reserved map is rule 3 only"),
        )


class TestB2BSupplyAcceptablePriceLists(unittest.TestCase):
    """``_b2b_supply_acceptable_price_lists`` with its resolvers stubbed."""

    def _candidates(self, *, partner=None, tier=None, tier_selling=True, baseline=_B2B,
                    default=_DEFAULT, company_default="Company Selling"):
        with patch.object(ic, "_resolve_sales_partner_price_list", return_value=partner), \
             patch.object(ic, "_resolve_customer_price_list", return_value=tier), \
             patch.object(ic, "_resolve_b2b_baseline_price_list", return_value=baseline), \
             patch.object(ic, "_is_selling_price_list", return_value=tier_selling), \
             patch.object(ic, "_resolve_company_default_price_list", return_value=company_default):
            return ic._b2b_supply_acceptable_price_lists(
                default_price_list=default,
                policy_order_purpose="B2B Supply",
                customer_doc=object(),
                sales_partner="_TEST Partner",
            )

    def test_a_non_selling_tier_unlocks_the_baseline_like_the_client(self):
        # api/pos.resolve_customer_price_list skips a non-selling tier to the baseline.
        self.assertEqual(self._candidates(tier=_TIER, tier_selling=False), [_TIER, _B2B])
        self.assertEqual(self._candidates(tier=_TIER, tier_selling=True), [_TIER])

    def test_defaults_only_when_nothing_b2b_is_derivable(self):
        self.assertEqual(self._candidates(baseline=None), [_DEFAULT])
        self.assertEqual(self._candidates(baseline=None, default=None), ["Company Selling"])
        self.assertEqual(self._candidates(baseline=None, default=None, company_default=None), [])
        self.assertNotIn(_DEFAULT, self._candidates(partner=_PARTNER_PL, baseline=None))

    def test_duplicates_collapse_case_insensitively(self):
        self.assertEqual(self._candidates(partner=_TIER, tier="cafes"), [_TIER])


# ---------------------------------------------------------------------------
# Rule 3: retail purposes use the branch (POS Profile) default list only
# ---------------------------------------------------------------------------

class TestRule3RetailCannotUseReservedList(_PurposeCase):
    _STANDARD_BUNDLE_MESSAGE = (
        "Order purpose Standard uses the branch price list Standard Selling, "
        "not Selling Bundle of 3."
    )

    def test_standard_manager_cannot_use_free_non_default_list(self):
        # The dropdown is gone: a free (unreserved) list is no longer a manager option.
        message = self.assertMismatch([], requested=_BUNDLE3, is_manager=True)
        self.assertEqual(message, self._STANDARD_BUNDLE_MESSAGE)

    def test_standard_free_list_is_refused_before_the_manager_gate(self):
        # A cashier gets the purpose message, not "manager pricing access required".
        message = self.assertMismatch(
            [], requested=_BUNDLE3, is_manager=False,
            gate_side_effect=AssertionError("manager gate reached before the purpose check"),
        )
        self.assertEqual(message, self._STANDARD_BUNDLE_MESSAGE)

    def test_standard_default_and_empty_request_pass_for_a_cashier(self):
        self.assertEqual(self._resolve(_DEFAULT, is_manager=False), _DEFAULT)
        for requested in (None, "", "   "):
            with self.subTest(requested=requested):
                self.assertEqual(self._resolve(requested, is_manager=False), _DEFAULT)

    def test_standard_case_variant_of_default_is_accepted(self):
        # MariaDB treats "standard selling" and "Standard Selling" as the same list.
        self.assertEqual(self._resolve("standard selling"), "standard selling")

    def test_standard_rejects_employee_list_even_for_manager(self):
        message = self.assertMismatch(
            ["Price list Employee is reserved for order purpose Employee", "Standard"],
            requested=_EMPLOYEE,
        )
        self.assertEqual(
            message,
            "Price list Employee is reserved for order purpose Employee; order purpose "
            "Standard uses the branch price list Standard Selling.",
        )

    def test_standard_rejects_sample_list_naming_every_owner(self):
        self.assertMismatch(
            [f"{_SAMPLE_COURIER}, {_SAMPLE_NO_COURIER}"], requested=_SAMPLE
        )

    def test_standard_rejects_b2b_baseline(self):
        self.assertMismatch(["reserved for order purpose B2B Supply"], requested=_B2B)

    def test_standard_reserved_match_is_case_insensitive(self):
        self.assertMismatch(["reserved for order purpose Employee"], requested="employee")

    def test_standard_default_request_skips_the_policy_query(self):
        self.assertEqual(
            self._resolve(
                _DEFAULT, is_manager=False,
                reserved_side_effect=AssertionError("default is never reserved; no query"),
            ),
            _DEFAULT,
        )

    def test_reserved_lookup_is_scoped_to_this_profile(self):
        # Still consulted on the refusal path: it picks the more informative wording.
        with self.assertRaises(frappe.ValidationError):
            self._resolve(_BUNDLE3)
        self.reserved_mock.assert_called_once_with(_PROFILE, default_price_list=_DEFAULT)

    def test_free_shipping_waiver_rejects_sample_list(self):
        self.assertMismatch(
            [
                "reserved for order purpose",
                "order purpose Free Shipping Waiver uses the branch price list Standard Selling",
            ],
            requested=_SAMPLE, matched=True, purpose="Free Shipping Waiver",
        )

    def test_free_shipping_waiver_rejects_free_non_default_list_even_for_manager(self):
        message = self.assertMismatch(
            [], requested=_BUNDLE3, matched=True, purpose="Free Shipping Waiver",
            is_manager=True,
        )
        self.assertEqual(
            message,
            "Order purpose Free Shipping Waiver uses the branch price list Standard Selling, "
            "not Selling Bundle of 3.",
        )

    def test_free_shipping_waiver_default_and_empty_request_pass(self):
        for requested in (_DEFAULT, None, ""):
            with self.subTest(requested=requested):
                self.assertEqual(
                    self._resolve(
                        requested, matched=True, purpose="Free Shipping Waiver",
                        customer=_untiered(), is_manager=False,
                    ),
                    _DEFAULT,
                )

    def test_free_shipping_waiver_without_a_request_never_resolves_the_customer_tier(self):
        # A B2B-group customer (prod: "ilo specialty coffee" -> B2B Selling) placing a
        # Free Shipping Waiver order with no list sent must price at the branch default,
        # not fall through the policy chain to its group tier.
        b2b_group_customer = _ns(name="_TEST B2B", default_price_list=_B2B, customer_group=None)
        for requested in (None, ""):
            with self.subTest(requested=requested):
                self.assertEqual(
                    self._resolve(
                        requested, matched=True, purpose="Free Shipping Waiver",
                        customer=b2b_group_customer, is_manager=False,
                    ),
                    _DEFAULT,
                )

    def test_free_shipping_waiver_cannot_borrow_b2b_baseline(self):
        self.assertMismatch(
            ["reserved for order purpose B2B Supply"],
            requested=_B2B, matched=True, purpose="Free Shipping Waiver",
        )


class TestRule3ProfileWithoutDefaultKeepsReservedOnlyCheck(_PurposeCase):
    """No branch default -> nothing to hold the request to; only reserved lists refused."""

    def setUp(self):
        super().setUp()
        self.pos = _ns(name=_PROFILE, selling_price_list=None, company=None)

    def test_manager_may_still_use_free_list(self):
        self.assertEqual(self._resolve(_BUNDLE3, reserved={_EMPLOYEE: {"Employee"}}), _BUNDLE3)
        self.assertEqual(
            self._resolve(
                _BUNDLE3, matched=True, purpose="Free Shipping Waiver",
                reserved={_EMPLOYEE: {"Employee"}},
            ),
            _BUNDLE3,
        )

    def test_free_list_is_still_manager_gated(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(_BUNDLE3, is_manager=False, reserved={_EMPLOYEE: {"Employee"}})

    def test_reserved_list_keeps_the_retail_wording(self):
        message = self.assertMismatch(
            [], requested=_EMPLOYEE, reserved={_EMPLOYEE: {"Employee"}},
        )
        self.assertEqual(
            message,
            "Price list Employee is reserved for order purpose Employee; order purpose "
            "Standard must use a retail price list.",
        )
        self.reserved_mock.assert_called_once_with(_PROFILE, default_price_list=None)


# ---------------------------------------------------------------------------
# No request: the B2B catalog context and ordinary orders are untouched
# ---------------------------------------------------------------------------

class TestAbsentRequestIsUnchanged(_PurposeCase):
    def test_no_request_runs_no_new_lookups(self):
        # _resolve_b2b_pricing_context always passes requested_price_list=None.
        for kwargs in (
            {},
            {"matched": True, "purpose": _SAMPLE_COURIER, "policy_pl": _SAMPLE},
            {"matched": True, "purpose": "Free Shipping Waiver"},
        ):
            with self.subTest(**kwargs):
                self._resolve(
                    None,
                    is_manager=False,
                    amended_from="ACC-SINV-NOPE",
                    source_invoice=None,
                    reserved_side_effect=AssertionError("no reserved query without a request"),
                    **kwargs,
                )


# ---------------------------------------------------------------------------
# Amendment exemption: a replacement may keep its cancelled source's own list
# ---------------------------------------------------------------------------

class TestAmendmentKeepsSourcePriceList(_PurposeCase):
    _SOURCE = "ACC-SINV-2026-00001"

    @staticmethod
    def _source_row(**overrides):
        """A cancelled source for the same customer on the same profile as the new order."""
        row = {
            "selling_price_list": _DEFAULT,
            "docstatus": 2,
            "custom_order_purpose": _SAMPLE_COURIER,
            "customer": _untiered().name,
            "pos_profile": _PROFILE,
            "custom_kanban_profile": _PROFILE,
        }
        row.update(overrides)
        return row

    def _sample_amendment(self, requested=_DEFAULT, *, source=None, live_replacement=False,
                          amended_from=_SOURCE, customer=None):
        if source is None:
            source = self._source_row()
        return self._resolve(
            requested,
            matched=True,
            purpose=_SAMPLE_COURIER,
            policy_pl=_SAMPLE,
            customer=customer if customer is not None else _untiered(),
            amended_from=amended_from,
            source_invoice=source,
            live_replacement=live_replacement,
        )

    def test_historical_mismatch_stays_amendable(self):
        # Sample - Courier @ Standard Selling, booked before the check existed.
        self.assertEqual(self._sample_amendment(), _DEFAULT)
        self.logger.warning.assert_called()

    def test_without_amended_from_the_same_request_is_refused(self):
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(amended_from=None)

    def test_source_must_be_cancelled(self):
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(source=self._source_row(docstatus=1))

    def test_a_new_list_on_amendment_is_still_checked(self):
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(requested=_BUNDLE3)

    def test_source_purpose_must_match(self):
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(source=self._source_row(custom_order_purpose="Standard"))

    def test_source_customer_must_match(self):
        # amended_from is client-supplied: another customer's cancelled order must not
        # lend its list to this one.
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(source=self._source_row(customer="_TEST Someone Else"))

    def test_source_customer_is_required(self):
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(source=self._source_row(customer=None))

    def test_source_pos_profile_must_match(self):
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(
                source=self._source_row(pos_profile="Nasr City", custom_kanban_profile="Nasr City")
            )

    def test_source_kanban_profile_counts_as_its_branch(self):
        # The amendment job recreates on custom_kanban_profile first, pos_profile second.
        self.assertEqual(
            self._sample_amendment(
                source=self._source_row(pos_profile="Nasr City", custom_kanban_profile=_PROFILE)
            ),
            _DEFAULT,
        )
        self.assertEqual(
            self._sample_amendment(
                source=self._source_row(pos_profile=_PROFILE, custom_kanban_profile=None)
            ),
            _DEFAULT,
        )

    def test_only_one_replacement_is_grandfathered(self):
        with self.assertRaises(frappe.ValidationError):
            self._sample_amendment(live_replacement=True)

    def test_missing_source_is_not_exempt(self):
        with self.assertRaises(frappe.ValidationError):
            self._resolve(
                _DEFAULT, matched=True, purpose=_SAMPLE_COURIER, policy_pl=_SAMPLE,
                amended_from=self._SOURCE, source_invoice=None,
            )

    def test_b2b_order_whose_tier_changed_since_stays_amendable(self):
        source = self._source_row(
            selling_price_list=_B2B, custom_order_purpose="B2B Supply", customer=_tiered().name
        )
        self.assertEqual(
            self._resolve(
                _B2B, matched=True, purpose="B2B Supply", customer=_tiered(),
                amended_from=self._SOURCE, source_invoice=source,
            ),
            _B2B,
        )

    def test_standard_amendment_keeping_a_free_non_default_list_stays_amendable(self):
        # A retail invoice booked at "Selling Bundle of 3" before rule 3 tightened must
        # still be amendable once cancelled — and it stays manager-only.
        source = self._source_row(selling_price_list=_BUNDLE3, custom_order_purpose="")
        self.assertEqual(
            self._resolve(
                _BUNDLE3, customer=_untiered(), amended_from=self._SOURCE, source_invoice=source
            ),
            _BUNDLE3,
        )
        warning = self.logger.warning.call_args.args[0]
        self.assertIn("expected Standard Selling", warning)
        with self.assertRaises(frappe.ValidationError):
            self._resolve(
                _BUNDLE3, customer=_untiered(), amended_from=self._SOURCE,
                source_invoice=source, is_manager=False,
            )

    def test_standard_amendment_moving_to_a_new_free_list_is_refused(self):
        source = self._source_row(selling_price_list=_DEFAULT, custom_order_purpose="")
        message = self.assertMismatch(
            [], requested=_BUNDLE3, customer=_untiered(), amended_from=self._SOURCE,
            source_invoice=source,
        )
        self.assertEqual(
            message,
            "Order purpose Standard uses the branch price list Standard Selling, "
            "not Selling Bundle of 3.",
        )

    def test_free_shipping_waiver_amendment_keeps_its_source_list(self):
        source = self._source_row(
            selling_price_list=_BUNDLE3, custom_order_purpose="Free Shipping Waiver"
        )
        self.assertEqual(
            self._resolve(
                _BUNDLE3, matched=True, purpose="Free Shipping Waiver", customer=_untiered(),
                amended_from=self._SOURCE, source_invoice=source,
            ),
            _BUNDLE3,
        )

    def test_standard_amendment_keeping_a_reserved_list_is_still_manager_gated(self):
        source = self._source_row(selling_price_list=_EMPLOYEE, custom_order_purpose="")
        self.assertEqual(
            self._resolve(
                _EMPLOYEE, customer=_untiered(), amended_from=self._SOURCE, source_invoice=source
            ),
            _EMPLOYEE,
        )
        with self.assertRaises(frappe.ValidationError):
            self._resolve(
                _EMPLOYEE, customer=_untiered(), amended_from=self._SOURCE,
                source_invoice=source, is_manager=False,
            )


# ---------------------------------------------------------------------------
# commercial_policy.reserved_price_lists
# ---------------------------------------------------------------------------

_POLICY_ROWS = [
    {"price_list": _EMPLOYEE, "order_purpose": "Employee", "pos_profile": None},
    {"price_list": _SAMPLE, "order_purpose": _SAMPLE_COURIER, "pos_profile": ""},
    {"price_list": _SAMPLE, "order_purpose": _SAMPLE_NO_COURIER, "pos_profile": None},
    {"price_list": None, "order_purpose": "B2B Supply", "pos_profile": None},
    {"price_list": "", "order_purpose": "Free Shipping Waiver", "pos_profile": None},
    {"price_list": "Nasr Staff", "order_purpose": "Employee", "pos_profile": "Nasr City"},
    {"price_list": "Retail Promo", "order_purpose": "Standard", "pos_profile": None},
    {"price_list": "standard selling", "order_purpose": "Employee", "pos_profile": None},
]


class TestReservedPriceListsHelper(unittest.TestCase):
    def _run(self, profile=_PROFILE, *, rows=None, doctype_exists=True, **kwargs):
        with patch("jarz_pos.services.commercial_policy.frappe") as mf:
            mf.db.exists.return_value = doctype_exists
            mf.get_all.return_value = _POLICY_ROWS if rows is None else rows
            mf.db.get_value.return_value = _DEFAULT
            result = cp.reserved_price_lists(profile, **kwargs)
        return result, mf

    def test_policy_lists_map_to_their_purposes(self):
        result, _ = self._run(default_price_list=_DEFAULT)
        self.assertEqual(result, _RESERVED)

    def test_policy_scoped_to_another_profile_is_ignored(self):
        result, _ = self._run(default_price_list=_DEFAULT)
        self.assertNotIn("Nasr Staff", result)

    def test_policy_scoped_to_this_profile_counts(self):
        result, _ = self._run("Nasr City", default_price_list=_DEFAULT)
        self.assertEqual(result["Nasr Staff"], {"Employee"})

    def test_profile_default_is_never_reserved(self):
        # A policy row points Employee at "standard selling" (case variant of the default).
        result, _ = self._run(default_price_list=_DEFAULT)
        self.assertFalse(any(name.casefold() == _DEFAULT.casefold() for name in result))

    def test_default_equal_to_b2b_baseline_is_not_reserved(self):
        result, _ = self._run(default_price_list=_B2B)
        self.assertNotIn(_B2B, result)

    def test_standard_and_listless_policies_reserve_nothing(self):
        result, _ = self._run(default_price_list=_DEFAULT)
        self.assertNotIn("Retail Promo", result)
        self.assertNotIn("", result)

    def test_one_query_for_enabled_policies_only(self):
        _, mf = self._run(default_price_list=_DEFAULT)
        mf.get_all.assert_called_once()
        self.assertEqual(mf.get_all.call_args.args[0], "Jarz Commercial Policy")
        self.assertEqual(mf.get_all.call_args.kwargs["filters"], {"enabled": 1})

    def test_missing_doctype_leaves_only_the_b2b_baseline(self):
        result, mf = self._run(default_price_list=_DEFAULT, doctype_exists=False)
        self.assertEqual(result, {_B2B: {"B2B Supply"}})
        mf.get_all.assert_not_called()

    def test_default_is_looked_up_when_not_supplied(self):
        result, mf = self._run()
        mf.db.get_value.assert_called_once_with("POS Profile", _PROFILE, "selling_price_list")
        self.assertEqual(result, _RESERVED)

    def test_supplied_default_skips_the_profile_lookup(self):
        _, mf = self._run(default_price_list=None)
        mf.db.get_value.assert_not_called()


# ---------------------------------------------------------------------------
# api/pos.get_pos_price_lists: additive reserved_for_purposes
# ---------------------------------------------------------------------------

class TestGetPosPriceListsReservedFor(unittest.TestCase):
    _EXPECTED_KEYS = {
        "name", "currency", "is_default", "zero_shipping_default", "display_label",
        "reserved_for_purposes",
    }

    def _run(self, price_list_rows, reserved):
        def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
            if doctype == "POS Profile" and fieldname == "selling_price_list":
                return _DEFAULT
            if doctype == "POS Profile" and fieldname == "currency":
                return "EGP"
            return None

        with patch("jarz_pos.api.pos.frappe") as mf, patch(
            "jarz_pos.utils.validation_utils.assert_pos_profile_enabled"
        ), patch.object(pos, "_ensure_manager_pricing_access"), patch(
            "jarz_pos.services.commercial_policy.reserved_price_lists", return_value=reserved
        ) as reserved_mock:
            mf.db.get_value.side_effect = _get_value
            mf.db.has_column.return_value = False
            mf.get_all.return_value = price_list_rows
            result = pos.get_pos_price_lists(_PROFILE)
        return {row["name"]: row for row in result}, result, reserved_mock

    @staticmethod
    def _rows(*names):
        return [{"name": name, "currency": "EGP"} for name in names]

    def test_reserved_and_free_lists_are_marked(self):
        by_name, _, reserved_mock = self._run(
            self._rows(_B2B, _EMPLOYEE, _SAMPLE, _BUNDLE3, _DEFAULT), _RESERVED
        )
        self.assertEqual(by_name[_EMPLOYEE]["reserved_for_purposes"], ["Employee"])
        self.assertEqual(
            by_name[_SAMPLE]["reserved_for_purposes"], [_SAMPLE_COURIER, _SAMPLE_NO_COURIER]
        )
        self.assertEqual(by_name[_B2B]["reserved_for_purposes"], ["B2B Supply"])
        self.assertEqual(by_name[_BUNDLE3]["reserved_for_purposes"], [])
        self.assertEqual(by_name[_DEFAULT]["reserved_for_purposes"], [])
        # Computed once, for this profile, not per row.
        reserved_mock.assert_called_once_with(_PROFILE, default_price_list=_DEFAULT)

    def test_existing_keys_unchanged(self):
        by_name, _, _ = self._run(self._rows(_EMPLOYEE, _DEFAULT), _RESERVED)
        for row in by_name.values():
            self.assertEqual(set(row), self._EXPECTED_KEYS)
        self.assertTrue(by_name[_DEFAULT]["is_default"])
        self.assertFalse(by_name[_EMPLOYEE]["is_default"])

    def test_default_is_never_reserved_even_if_the_map_says_so(self):
        by_name, _, _ = self._run(
            self._rows(_DEFAULT, _EMPLOYEE), {**_RESERVED, _DEFAULT: {"Employee"}}
        )
        self.assertEqual(by_name[_DEFAULT]["reserved_for_purposes"], [])

    def test_inserted_default_row_carries_empty_reserved(self):
        _, result, _ = self._run(self._rows(_EMPLOYEE), _RESERVED)
        self.assertEqual(result[0]["name"], _DEFAULT)
        self.assertEqual(result[0]["reserved_for_purposes"], [])
        self.assertEqual(set(result[0]), self._EXPECTED_KEYS)

    def test_map_keys_match_case_insensitively(self):
        by_name, _, _ = self._run(self._rows(_SAMPLE), {"sample": {_SAMPLE_COURIER}})
        self.assertEqual(by_name[_SAMPLE]["reserved_for_purposes"], [_SAMPLE_COURIER])


if __name__ == "__main__":
    unittest.main()

"""Resolver tests for the category price fallback (pure mock).

Exercises ``services/invoice_creation.py::_resolve_item_rate`` precedence after the
contract v2 extension:

    (1) customer-scoped item_code Item Price
    (2) generic item_code Item Price
    (3) ``Jarz Price List Category Rate`` for (price_list, item's item_group)   <- NEW
    (4) get_item_price / fallback

Everything is mocked at ``ic.frappe.db.get_value`` + ``ic.get_item_price`` so the
precedence is asserted deterministically without any DB / doctype-migration dependency.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import invoice_creation as ic


_PL = "Companies"
_CODE = "COFFEE-M-01"
_CUST = "CUST-A"
_GROUP = "Medium"


def _make_get_value(scoped=None, generic_item=None, group_rate=None):
    """Build a frappe.db.get_value side-effect emulating the pricing tables.

    Signature matches ``frappe.db.get_value(doctype, filters, fieldname)``. Category
    rates now come from the ``Jarz Price List Category Rate`` DocType (v2), not Item Price.
    """

    def _gv(doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "Item":
            # _resolve_item_rate looks up the item's item_group.
            return _GROUP
        if doctype == "Jarz Price List Category Rate":
            # Step 3: category rate for (price_list, item_group).
            return group_rate
        if doctype == "Item Price":
            f = filters or {}
            cust = f.get("customer")
            # Customer-scoped row: customer pinned to a concrete value (not the
            # generic ["in", [None, ""]] sentinel).
            if cust not in (None, "", ["in", [None, ""]]):
                return scoped
            # Generic per-item row.
            return generic_item
        return None

    return _gv


class TestItemGroupFallback(unittest.TestCase):
    def test_group_rate_used_when_no_per_item_row(self):
        # No per-item generic row -> fall through to the item_group category rate.
        gv = _make_get_value(scoped=None, generic_item=None, group_rate=75)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 75.0)

    def test_per_item_override_beats_category(self):
        # A generic per-item row (60) must win over the item_group category row (75).
        gv = _make_get_value(scoped=None, generic_item=60, group_rate=75)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 60.0)

    def test_customer_scoped_still_wins(self):
        # Customer-scoped row (50) outranks both the per-item (60) and category (75) rows.
        gv = _make_get_value(scoped=50, generic_item=60, group_rate=75)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL, customer=_CUST), 50.0)

    def test_fallback_when_no_group_row(self):
        # No per-item and no category row -> final get_item_price fallback path.
        gv = _make_get_value(scoped=None, generic_item=None, group_rate=None)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=10
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 10.0)

    def test_category_lookup_not_consulted_when_per_item_exists(self):
        # Precedence guard: when a per-item generic rate exists, neither the
        # Item.item_group lookup nor the Category Rate query must be reached.
        def _gv(doctype, filters=None, fieldname=None, *a, **k):
            if doctype == "Item Price":
                return 60  # generic per-item row present
            if doctype == "Jarz Price List Category Rate":
                raise AssertionError("category rate query should not run")
            if doctype == "Item":
                raise AssertionError("Item.item_group lookup should not run")
            return None

        with patch.object(ic.frappe.db, "get_value", side_effect=_gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 60.0)

    def test_standard_no_price_list_is_byte_identical(self):
        # With no price_list (Standard's inert path) none of the matched lookups run;
        # the rate comes straight from get_item_price/fallback as before.
        def _gv(*a, **k):
            raise AssertionError("no Item Price lookup should occur without a price_list")

        with patch.object(ic.frappe.db, "get_value", side_effect=_gv), patch.object(
            ic, "get_item_price", return_value=None
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, None, fallback_rate=42), 42.0)


_B2B_LIST = "B2B Selling"
_B2B_PURPOSE = "B2B Supply"
_POS_DEFAULT = "Standard Selling"


def _price_list_row(enabled=1, selling=1):
    """frappe.db.get_value side effect for the Price List flag lookup."""

    def _gv(doctype, name=None, fieldname=None, *args, **kwargs):
        if doctype == "Price List":
            return {"enabled": enabled, "selling": selling}
        return None

    return _gv


class TestB2bBaselinePriceList(unittest.TestCase):
    """The fallback that stops an un-tiered B2B customer pricing at retail."""

    def test_b2b_supply_resolves_to_the_base_list(self):
        with patch.object(ic.frappe.db, "get_value", side_effect=_price_list_row()):
            self.assertEqual(
                ic._resolve_b2b_baseline_price_list(_B2B_PURPOSE), _B2B_LIST
            )

    def test_other_purposes_never_get_b2b_pricing(self):
        # "Free Shipping Waiver" also carries no price list, but it is a RETAIL order
        # with the shipping income waived — handing it the B2B list would silently
        # discount every waiver order.
        with patch.object(ic.frappe.db, "get_value", side_effect=_price_list_row()):
            for purpose in ("Free Shipping Waiver", "Standard", "Employee", "", None):
                self.assertIsNone(ic._resolve_b2b_baseline_price_list(purpose), purpose)

    def test_disabled_or_buying_only_list_falls_through(self):
        # Returning a list the invoice would reject anyway just moves the failure to
        # checkout; falling back to the POS default is the safer answer.
        for flags in ({"enabled": 0, "selling": 1}, {"enabled": 1, "selling": 0}):
            with patch.object(
                ic.frappe.db, "get_value", side_effect=_price_list_row(**flags)
            ):
                self.assertIsNone(ic._resolve_b2b_baseline_price_list(_B2B_PURPOSE), flags)

    def test_missing_list_falls_through(self):
        with patch.object(ic.frappe.db, "get_value", return_value=None):
            self.assertIsNone(ic._resolve_b2b_baseline_price_list(_B2B_PURPOSE))

    def test_baseline_is_server_derivable(self):
        # If the baseline were missing from this set, a B2B rep whose cart echoes the
        # resolved list back at checkout would trip the manager gate and be locked out
        # of B2B checkout entirely — the exact lockout that set exists to prevent.
        with patch.object(ic.frappe.db, "get_value", side_effect=_price_list_row()):
            derivable = ic._auto_derivable_price_lists(
                default_price_list=_POS_DEFAULT,
                policy_matched=True,
                policy_price_list=None,
                policy_order_purpose=_B2B_PURPOSE,
                customer_doc=None,
                sales_partner=None,
            )
        self.assertIn(_B2B_LIST, derivable)

    def test_standard_order_derives_nothing_new(self):
        # A Standard (retail) order must not be able to reach the B2B list without
        # manager approval — a cashier applying B2B pricing to a walk-in sale.
        derivable = ic._auto_derivable_price_lists(
            default_price_list=_POS_DEFAULT,
            policy_matched=False,
            policy_order_purpose=_B2B_PURPOSE,
        )
        self.assertEqual(derivable, {_POS_DEFAULT})


class _Profile:
    def __init__(self, price_list=_POS_DEFAULT):
        self.selling_price_list = price_list
        self.name = "Dokki"


class _Customer:
    def __init__(self, default_price_list=None, customer_group=None):
        self.default_price_list = default_price_list
        self.customer_group = customer_group


class TestEffectivePriceListChain(unittest.TestCase):
    """Where the baseline sits in the chain is the whole design decision."""

    def _resolve(self, *, customer_doc=None, purpose=_B2B_PURPOSE, matched=True,
                 group_list=None):
        def _gv(doctype, name=None, fieldname=None, *args, **kwargs):
            if doctype == "Price List":
                return {"enabled": 1, "selling": 1}
            if doctype == "Customer Group":
                return group_list
            return None

        with patch.object(ic.frappe.db, "get_value", side_effect=_gv), patch.object(
            ic.frappe.db, "exists", return_value=True
        ):
            return ic._resolve_effective_price_list(
                _Profile(),
                [{"item_code": "JAR-L1", "qty": 1}],
                requested_price_list=None,
                suppress_shipping_income=None,
                suppress_legacy_delivery_charges=None,
                logger=MagicMock(),
                policy_matched=matched,
                policy_price_list=None,
                policy_order_purpose=purpose,
                customer_doc=customer_doc,
                sales_partner=None,
            )

    def test_untiered_b2b_customer_gets_the_base_list_not_retail(self):
        self.assertEqual(self._resolve(customer_doc=_Customer()), _B2B_LIST)

    def test_customer_tier_still_wins(self):
        self.assertEqual(
            self._resolve(customer_doc=_Customer(default_price_list="Cafes")), "Cafes"
        )

    def test_customer_group_tier_still_wins(self):
        self.assertEqual(
            self._resolve(
                customer_doc=_Customer(customer_group="Cafes"), group_list="Cafes"
            ),
            "Cafes",
        )

    def test_free_shipping_waiver_keeps_retail_pricing(self):
        self.assertEqual(
            self._resolve(customer_doc=_Customer(), purpose="Free Shipping Waiver"),
            _POS_DEFAULT,
        )

    def test_standard_order_is_unchanged(self):
        self.assertEqual(
            self._resolve(customer_doc=_Customer(), purpose="Standard", matched=False),
            _POS_DEFAULT,
        )


if __name__ == "__main__":
    unittest.main()

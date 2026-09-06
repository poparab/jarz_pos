"""Regression tests for three verified money-path pricing bugs in
``services/invoice_creation.py``.

FIX 1  ``custom_rate_override: 0`` used to be treated as "unset" by the manager
       gate and the ``[CUSTOM LINE PRICING]`` audit marker (both checked
       ``value not in (None, "", 0, 0.0)``) while the pricing engine already
       honoured it as a deliberate zero price. A cashier without manager access
       could submit a free line with no gate and no breadcrumb. All four sites
       (gate, audit marker, engine, bundle override) now share one predicate,
       ``_pricing_field_provided``, for which ``0`` is PROVIDED.

FIX 2  ``qty <= 0`` / negative ``price_list_rate`` on a REGULAR (non-bundle)
       cart line bypassed the clamps in ``_process_cart_items`` entirely,
       because those clamps only mutate local variables that are never handed
       to ``_process_regular_item`` (which re-reads the raw cart dict). A
       negative qty could subtract from ``net_total`` on a submitted invoice.
       ``_process_regular_item`` now throws instead of silently clamping.

FIX 3  The B2B price-list coverage check ignored the customer entirely, so an
       Item Price scoped to a DIFFERENT customer made an item look "covered".
       ``_resolve_item_rate`` (correctly) ignores other customers' rows, so the
       rate resolution then silently fell back to trusting the client's own
       number. Coverage is now customer-scoped, ``_resolve_item_rate`` reports
       WHERE a rate came from (``_resolve_item_rate_with_provenance``), and a
       "client" rate on a matched, sub-100%-discount policy now throws instead
       of being trusted; on a non-policy order it is allowed but leaves a
       ``[CLIENT PRICED]`` audit breadcrumb.

These follow the mocking conventions already used in
``test_invoice_creation_accounting.py`` (full ``frappe`` module mock for
``_process_regular_item`` unit tests) and ``test_price_list_resolution.py`` /
``test_commercial_policy.py`` (``frappe.db`` filter-aware fakes for the
coverage / rate-resolution tests) rather than inventing new patterns.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import invoice_creation as ic


class _Thrown(Exception):
    """Stand-in for frappe.throw so the raised message can be inspected."""


def _throwing(message, *args, **kwargs):  # noqa: ARG001
    raise _Thrown(str(message))


# ===========================================================================
# FIX 1 -- a shared "was this pricing field provided" predicate
# ===========================================================================


class TestPricingFieldProvidedPredicate(unittest.TestCase):
    def test_zero_is_provided(self):
        self.assertTrue(ic._pricing_field_provided(0))
        self.assertTrue(ic._pricing_field_provided(0.0))

    def test_none_and_blank_are_not_provided(self):
        self.assertFalse(ic._pricing_field_provided(None))
        self.assertFalse(ic._pricing_field_provided(""))

    def test_nonzero_is_provided(self):
        self.assertTrue(ic._pricing_field_provided(12.5))
        self.assertTrue(ic._pricing_field_provided(-1))  # sign is validated elsewhere


class TestManagerGateTripsOnZeroOverride(unittest.TestCase):
    """The bug: the gate used to special-case 0/0.0 as "no override supplied"."""

    def test_zero_custom_rate_override_requires_manager(self):
        tripped = ic._pricing_action_requires_manager(
            [{"item_code": "ITEM-1", "custom_rate_override": 0}],
            requested_price_list=None,
            default_price_list="Standard Selling",
            suppress_shipping_income=None,
            suppress_legacy_delivery_charges=None,
        )
        self.assertTrue(tripped)

    def test_zero_discount_amount_requires_manager(self):
        tripped = ic._pricing_action_requires_manager(
            [{"item_code": "ITEM-1", "discount_amount": 0.0}],
            requested_price_list=None,
            default_price_list="Standard Selling",
            suppress_shipping_income=None,
            suppress_legacy_delivery_charges=None,
        )
        self.assertTrue(tripped)

    def test_absent_field_does_not_require_manager(self):
        tripped = ic._pricing_action_requires_manager(
            [{"item_code": "ITEM-1"}],
            requested_price_list=None,
            default_price_list="Standard Selling",
            suppress_shipping_income=None,
            suppress_legacy_delivery_charges=None,
        )
        self.assertFalse(tripped)


class TestZeroOverrideEndToEnd(unittest.TestCase):
    """create_pos_invoice: a `custom_rate_override: 0` line is gated AND audited."""

    def _pos_profile(self):
        p = MagicMock()
        p.name = "Test POS"
        p.company = "Test Company"
        p.selling_price_list = "Standard Selling"
        p.currency = "EGP"
        return p

    def _customer(self):
        c = MagicMock()
        c.name = "CUST-001"
        c.customer_name = "Test Customer"
        c.territory = "Cairo"
        return c

    def test_non_manager_is_rejected_for_zero_override(self):
        customer = self._customer()
        pos_profile = self._pos_profile()

        with patch(
            "jarz_pos.services.invoice_creation.validate_cart_data",
            return_value=[{"item_code": "ITEM-1", "custom_rate_override": 0}],
        ), patch(
            "jarz_pos.services.invoice_creation._parse_delivery_charges", return_value=[]
        ), patch(
            "jarz_pos.services.invoice_creation.validate_delivery_datetime", return_value=None
        ), patch(
            "jarz_pos.services.invoice_creation.validate_customer", return_value=customer
        ), patch(
            "jarz_pos.services.invoice_creation.validate_pos_profile", return_value=pos_profile
        ), patch(
            "jarz_pos.services.invoice_creation.frappe"
        ) as mf:
            mf.local.site = "test-site"
            mf.logger.return_value = MagicMock()
            mf.utils.now.return_value = "2026-09-06 12:00:00"
            mf.db.exists.return_value = True
            mf.get_roles.return_value = ["POS User"]
            mf.session.user = "cashier@example.com"
            mf.throw.side_effect = PermissionError("manager pricing access required")

            with self.assertRaises(PermissionError):
                ic.create_pos_invoice(
                    cart_json="[]",
                    customer_name=customer.name,
                    pos_profile_name=pos_profile.name,
                )

    def test_manager_zero_override_is_audit_marked(self):
        from jarz_pos.tests.test_invoice_creation_accounting import (
            _InvoiceDocCapture,
            _mock_customer,
            _mock_pos_profile,
        )
        from jarz_pos.services.delivery_promotions import DeliveryPromotionDecision

        inv = _InvoiceDocCapture()
        customer = _mock_customer()
        pos_profile = _mock_pos_profile(selling_price_list="Retail Default")
        processed_items = [
            {
                "item_code": "ITEM-1",
                "qty": 1,
                "rate": 0.0,
                "price_list_rate": 0.0,
                "custom_rate_override": 0.0,
                "original_price_list_rate": 90.0,
            }
        ]

        with patch(
            "jarz_pos.services.invoice_creation.validate_cart_data",
            return_value=[{"item_code": "ITEM-1", "custom_rate_override": 0}],
        ), patch(
            "jarz_pos.services.invoice_creation._parse_delivery_charges", return_value=[]
        ), patch(
            "jarz_pos.services.invoice_creation.validate_delivery_datetime", return_value=None
        ), patch(
            "jarz_pos.services.invoice_creation.validate_customer", return_value=customer
        ), patch(
            "jarz_pos.services.invoice_creation.validate_pos_profile", return_value=pos_profile
        ), patch(
            "jarz_pos.services.invoice_creation._process_cart_items", return_value=processed_items
        ), patch(
            "jarz_pos.services.invoice_creation._create_invoice_document", return_value=inv
        ), patch(
            "jarz_pos.services.invoice_creation.set_invoice_fields"
        ), patch(
            "jarz_pos.services.invoice_creation.add_items_to_invoice"
        ), patch(
            "jarz_pos.services.invoice_creation._set_initial_state_for_sales_partner"
        ), patch(
            "jarz_pos.services.invoice_creation._validate_and_calculate_document"
        ), patch(
            "jarz_pos.services.invoice_creation._save_document"
        ), patch(
            "jarz_pos.services.invoice_creation._submit_document"
        ), patch(
            "jarz_pos.services.invoice_creation._maybe_register_online_payment_to_partner"
        ), patch(
            "jarz_pos.services.invoice_creation._delivery_promotions.resolve_delivery_promotion"
        ) as resolve_promo, patch(
            "jarz_pos.services.invoice_creation._delivery_promotions.apply_delivery_promotion_audit"
        ), patch(
            "jarz_pos.services.invoice_creation.frappe"
        ) as mf:
            resolve_promo.return_value = DeliveryPromotionDecision(
                matched=False,
                rule_name=None,
                rule_type=None,
                merchandise_subtotal=0.0,
                item_qty=0.0,
                suppress_shipping_income=False,
                suppress_legacy_delivery_charges=False,
            )
            mf.local.site = "test-site"
            mf.logger.return_value = MagicMock()
            mf.utils.now.return_value = "2026-09-06 12:00:00"
            mf.db.exists.return_value = True
            mf.get_roles.return_value = ["JARZ Manager"]
            mf.session.user = "manager@example.com"
            mf.get_all.return_value = []

            ic.create_pos_invoice(
                cart_json="[]",
                customer_name=customer.name,
                pos_profile_name=pos_profile.name,
            )

        self.assertIn("[CUSTOM LINE PRICING]", inv.remarks)


# ===========================================================================
# FIX 2 -- negative qty / price_list_rate on a REGULAR item must throw
# ===========================================================================


class TestRegularItemQtyRateGuards(unittest.TestCase):
    def _run(self, item_data, **kwargs):
        with patch("jarz_pos.services.invoice_creation.frappe") as mf:
            mf.db.exists.return_value = True
            mf.db.get_value.return_value = None
            mf.get_doc.return_value = MagicMock(item_name="Item", stock_uom="Nos")
            mf.throw.side_effect = _throwing
            with self.assertRaises(_Thrown) as caught:
                ic._process_regular_item(item_data, MagicMock(), **kwargs)
        return str(caught.exception)

    def test_negative_qty_is_rejected(self):
        message = self._run({"item_code": "ITEM-1", "qty": -1, "rate": 50.0})
        self.assertIn("Quantity", message)
        self.assertIn("ITEM-1", message)

    def test_zero_qty_is_rejected(self):
        self._run({"item_code": "ITEM-1", "qty": 0, "rate": 50.0})

    def test_negative_price_list_rate_is_rejected(self):
        message = self._run(
            {"item_code": "ITEM-1", "qty": 1, "price_list_rate": -10.0}
        )
        self.assertIn("Price list rate", message)

    def test_negative_rate_without_price_list_rate_is_rejected(self):
        message = self._run({"item_code": "ITEM-1", "qty": 1, "rate": -5.0})
        self.assertIn("Rate", message)

    def test_positive_qty_and_rate_are_unaffected(self):
        with patch("jarz_pos.services.invoice_creation.frappe") as mf:
            mf.db.exists.return_value = True
            mf.db.get_value.return_value = None
            mf.get_doc.return_value = MagicMock(item_name="Item", stock_uom="Nos")

            result = ic._process_regular_item(
                {"item_code": "ITEM-1", "qty": 3, "rate": 25.0}, MagicMock()
            )
        self.assertEqual(result["qty"], 3.0)
        self.assertEqual(result["rate"], 25.0)


# ===========================================================================
# FIX 3a -- coverage must be scoped to THIS customer
# ===========================================================================


def _item_price_exists_factory(rows):
    """A ``frappe.db.exists`` fake that honours ``{"in": [...]}``-shaped filters
    the way MariaDB actually would, so the customer-scoping fix can be exercised
    without a live site."""

    def _matches(row, filters):
        for key, expected in filters.items():
            actual = row.get(key)
            if isinstance(expected, list) and expected and expected[0] == "in":
                if actual not in expected[1]:
                    return False
            elif actual != expected:
                return False
        return True

    def _exists(doctype, filters=None, *args, **kwargs):
        if doctype != "Item Price":
            return False
        return any(_matches(row, filters or {}) for row in rows)

    return _exists


class TestCoverageIsCustomerScoped(unittest.TestCase):
    def setUp(self):
        self.logger = MagicMock()
        self.decision = MagicMock(matched=True, discount_percentage=0, order_purpose="B2B Supply")
        self.cart = [{"item_code": "ITEM-X"}]
        # Only ILO has a price for ITEM-X in this list -- no generic row, no row
        # for ACME.
        self.rows = [
            {"item_code": "ITEM-X", "price_list": "B2B Selling", "selling": 1, "customer": "ILO"}
        ]

    def test_other_customers_price_does_not_count_as_coverage(self):
        exists_fn = _item_price_exists_factory(self.rows)
        with patch.object(ic.frappe.db, "exists", side_effect=exists_fn), patch.object(
            ic.frappe.db, "get_value", return_value=None
        ):
            with self.assertRaises(Exception):
                ic._validate_policy_price_list_coverage(
                    self.decision, "B2B Selling", self.cart, self.logger, customer="ACME"
                )

    def test_default_customer_none_also_excludes_other_customers_rows(self):
        # Even the OLD call shape (no customer kwarg) must not treat a row scoped
        # to a specific OTHER customer as generic coverage.
        exists_fn = _item_price_exists_factory(self.rows)
        with patch.object(ic.frappe.db, "exists", side_effect=exists_fn), patch.object(
            ic.frappe.db, "get_value", return_value=None
        ):
            with self.assertRaises(Exception):
                ic._validate_policy_price_list_coverage(
                    self.decision, "B2B Selling", self.cart, self.logger
                )

    def test_the_scoped_customer_is_covered(self):
        exists_fn = _item_price_exists_factory(self.rows)
        with patch.object(ic.frappe.db, "exists", side_effect=exists_fn), patch.object(
            ic.frappe.db, "get_value", return_value=None
        ):
            # No exception expected: ILO's own price covers ILO's own order.
            ic._validate_policy_price_list_coverage(
                self.decision, "B2B Selling", self.cart, self.logger, customer="ILO"
            )


# ===========================================================================
# FIX 3b/3c -- rate provenance, and the client-priced throw / audit marker
# ===========================================================================


class TestRateProvenance(unittest.TestCase):
    def test_no_price_list_is_client_provenance(self):
        rate, provenance = ic._resolve_item_rate_with_provenance(
            "ITEM-1", None, fallback_rate=42.0
        )
        self.assertEqual(rate, 42.0)
        self.assertEqual(provenance, "client")

    def test_resolve_item_rate_wrapper_still_returns_a_float(self):
        # Backward compatibility for api/pos.py and api/manager.py callers.
        rate = ic._resolve_item_rate("ITEM-1", None, fallback_rate=42.0)
        self.assertEqual(rate, 42.0)
        self.assertIsInstance(rate, float)


class TestClientPricedThrowOrAudit(unittest.TestCase):
    def _process(self, *, enforce, price_list="B2B Selling", customer="CUST-1"):
        with patch("jarz_pos.services.invoice_creation.frappe") as mf:
            mf.db.exists.return_value = True  # item exists
            mf.db.get_value.return_value = None  # no Item Price / category anywhere
            mf.get_doc.return_value = MagicMock(item_name="B2B Item", stock_uom="Nos")
            mf.throw.side_effect = _throwing
            return ic._process_regular_item(
                {"item_code": "ITEM-B2B", "qty": 1, "rate": 90.0},
                MagicMock(),
                price_list=price_list,
                customer=customer,
                enforce_price_list_pricing=enforce,
            ), mf

    def test_matched_policy_refuses_a_client_only_rate(self):
        with self.assertRaises(_Thrown):
            self._process(enforce=True)

    def test_non_policy_order_allows_it_and_flags_it(self):
        result, mf = self._process(enforce=False)
        self.assertTrue(result.get("_client_priced"))
        self.assertEqual(result["rate"], 90.0)
        mf.throw.assert_not_called()

    def test_explicit_manager_override_bypasses_the_client_priced_check(self):
        # A manager-supplied custom_rate_override is a separately gated, audited
        # path ([CUSTOM LINE PRICING]) -- it must not ALSO trip the client-priced
        # throw even when enforcement is on.
        with patch("jarz_pos.services.invoice_creation.frappe") as mf:
            mf.db.exists.return_value = True
            mf.db.get_value.return_value = None
            mf.get_doc.return_value = MagicMock(item_name="B2B Item", stock_uom="Nos")
            mf.throw.side_effect = _throwing

            result = ic._process_regular_item(
                {"item_code": "ITEM-B2B", "qty": 1, "rate": 90.0, "custom_rate_override": 77.0},
                MagicMock(),
                price_list="B2B Selling",
                customer="CUST-1",
                enforce_price_list_pricing=True,
            )
        self.assertEqual(result["rate"], 77.0)
        self.assertNotIn("_client_priced", result)


class TestClientPricedAuditMarkerEndToEnd(unittest.TestCase):
    """create_pos_invoice: a client-priced line on a NON-policy order is allowed
    but leaves the `[CLIENT PRICED]` breadcrumb."""

    def test_client_priced_flag_is_stamped_on_the_invoice(self):
        from jarz_pos.tests.test_invoice_creation_accounting import (
            _InvoiceDocCapture,
            _mock_customer,
            _mock_pos_profile,
        )
        from jarz_pos.services.delivery_promotions import DeliveryPromotionDecision

        inv = _InvoiceDocCapture()
        customer = _mock_customer()
        pos_profile = _mock_pos_profile(selling_price_list="Retail Default")
        processed_items = [
            {
                "item_code": "ITEM-1",
                "qty": 1,
                "rate": 45.0,
                "price_list_rate": 45.0,
                "_client_priced": True,
            }
        ]

        with patch(
            "jarz_pos.services.invoice_creation.validate_cart_data",
            return_value=[{"item_code": "ITEM-1"}],
        ), patch(
            "jarz_pos.services.invoice_creation._parse_delivery_charges", return_value=[]
        ), patch(
            "jarz_pos.services.invoice_creation.validate_delivery_datetime", return_value=None
        ), patch(
            "jarz_pos.services.invoice_creation.validate_customer", return_value=customer
        ), patch(
            "jarz_pos.services.invoice_creation.validate_pos_profile", return_value=pos_profile
        ), patch(
            "jarz_pos.services.invoice_creation._process_cart_items", return_value=processed_items
        ), patch(
            "jarz_pos.services.invoice_creation._create_invoice_document", return_value=inv
        ), patch(
            "jarz_pos.services.invoice_creation.set_invoice_fields"
        ), patch(
            "jarz_pos.services.invoice_creation.add_items_to_invoice"
        ), patch(
            "jarz_pos.services.invoice_creation._set_initial_state_for_sales_partner"
        ), patch(
            "jarz_pos.services.invoice_creation._validate_and_calculate_document"
        ), patch(
            "jarz_pos.services.invoice_creation._save_document"
        ), patch(
            "jarz_pos.services.invoice_creation._submit_document"
        ), patch(
            "jarz_pos.services.invoice_creation._maybe_register_online_payment_to_partner"
        ), patch(
            "jarz_pos.services.invoice_creation._delivery_promotions.resolve_delivery_promotion"
        ) as resolve_promo, patch(
            "jarz_pos.services.invoice_creation._delivery_promotions.apply_delivery_promotion_audit"
        ), patch(
            "jarz_pos.services.invoice_creation.frappe"
        ) as mf:
            resolve_promo.return_value = DeliveryPromotionDecision(
                matched=False,
                rule_name=None,
                rule_type=None,
                merchandise_subtotal=0.0,
                item_qty=0.0,
                suppress_shipping_income=False,
                suppress_legacy_delivery_charges=False,
            )
            mf.local.site = "test-site"
            mf.logger.return_value = MagicMock()
            mf.utils.now.return_value = "2026-09-06 12:00:00"
            mf.db.exists.return_value = True
            mf.get_roles.return_value = ["JARZ Manager"]
            mf.session.user = "manager@example.com"
            mf.get_all.return_value = []

            ic.create_pos_invoice(
                cart_json="[]",
                customer_name=customer.name,
                pos_profile_name=pos_profile.name,
            )

        self.assertIn("[CLIENT PRICED]", getattr(inv, "custom_pos_audit_markers", ""))


if __name__ == "__main__":
    unittest.main()

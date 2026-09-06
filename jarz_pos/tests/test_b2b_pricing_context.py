from types import SimpleNamespace
import unittest
from unittest.mock import patch

import frappe

from jarz_pos.api import pos


PROFILE = "_TEST B2B POS"
CUSTOMER = "_TEST B2B Customer"
COMPANY = "_TEST Company"
PRICE_LIST = "_TEST B2B Selling"


def _decision(purpose, *, configured_price_list=None):
    return SimpleNamespace(
        matched=True,
        policy_name=f"_TEST {purpose} Policy",
        order_purpose=purpose,
        price_list=configured_price_list,
        discount_percentage=100 if purpose == "Sample - Courier" else 0,
        suppress_shipping_income=True,
        no_courier=False,
    )


class TestB2BPricingContext(unittest.TestCase):
    def _run_context(
        self,
        purpose="B2B Supply",
        *,
        roles=None,
        assigned=True,
        customer_permission=True,
        customer_disabled=False,
        customer_price_list=None,
        customer_group=None,
        configured_price_list=None,
        resolved_price_list=PRICE_LIST,
        requested_price_list=None,
        price_list_enabled=True,
        price_list_selling=True,
    ):
        profile_doc = SimpleNamespace(
            name=PROFILE,
            company=COMPANY,
            currency="EGP",
            selling_price_list="Standard Selling",
        )
        customer_doc = SimpleNamespace(
            name=CUSTOMER,
            disabled=1 if customer_disabled else 0,
            default_price_list=customer_price_list,
            customer_group=customer_group,
        )
        policy = _decision(purpose, configured_price_list=configured_price_list)

        def exists(doctype, value):
            if doctype == "Company":
                return True
            if doctype == "POS Profile User":
                self.assertEqual(value, {"parent": PROFILE, "user": "rep@example.com"})
                return assigned
            if doctype == "Customer":
                return value == CUSTOMER
            raise AssertionError(f"Unexpected exists lookup: {doctype} {value}")

        def get_doc(doctype, name):
            if doctype == "POS Profile":
                return profile_doc
            if doctype == "Customer":
                return customer_doc
            if doctype == "Jarz Commercial Policy":
                self.assertEqual(name, policy.policy_name)
                return SimpleNamespace(policy_name=f"{purpose} label")
            raise AssertionError(f"Unexpected get_doc: {doctype} {name}")

        def get_value(doctype, name, fields, **kwargs):
            if doctype == "Price List" and kwargs.get("as_dict"):
                return {
                    "name": resolved_price_list,
                    "enabled": 1 if price_list_enabled else 0,
                    "selling": 1 if price_list_selling else 0,
                    "currency": "EGP",
                }
            if doctype == "Company" and fields == "default_currency":
                return "EGP"
            raise AssertionError(f"Unexpected get_value: {doctype} {name} {fields}")

        with patch.object(
            pos.frappe, "session", SimpleNamespace(user="rep@example.com")
        ), patch.object(pos.frappe, "get_roles", return_value=roles or ["B2B Sales Rep"]), patch.object(pos.frappe.db, "exists", side_effect=exists), patch.object(
            pos.frappe.db, "get_value", side_effect=get_value
        ), patch.object(pos.frappe.db, "has_column", return_value=False), patch.object(
            pos.frappe, "get_doc", side_effect=get_doc
        ), patch.object(
            pos.frappe, "has_permission", return_value=customer_permission
        ), patch(
            "jarz_pos.utils.validation_utils.assert_pos_profile_enabled"
        ) as assert_profile, patch(
            "jarz_pos.services.commercial_policy.resolve_commercial_policy",
            return_value=policy,
        ) as resolve_policy, patch(
            "jarz_pos.services.invoice_creation._resolve_effective_price_list",
            return_value=resolved_price_list,
        ) as resolve_price:
            result = pos._resolve_b2b_pricing_context(
                PROFILE,
                CUSTOMER,
                purpose,
                requested_price_list=requested_price_list,
            )

        assert_profile.assert_called_once_with(PROFILE)
        resolve_policy.assert_called_once()
        resolve_price.assert_called_once()
        return result, resolve_price

    def test_supply_contract_uses_server_derived_customer_tier(self):
        result, resolve_price = self._run_context(customer_price_list=PRICE_LIST)

        self.assertEqual(
            set(result),
            {"profile", "customer", "order_purpose", "commercial_policy", "price_list"},
        )
        self.assertEqual(result["profile"], PROFILE)
        self.assertEqual(result["customer"], CUSTOMER)
        self.assertEqual(result["order_purpose"], "B2B Supply")
        self.assertEqual(
            result["commercial_policy"],
            {
                "name": "_TEST B2B Supply Policy",
                "policy_name": "B2B Supply label",
                "order_purpose": "B2B Supply",
                "price_list": None,
                "discount_percentage": 0.0,
                "waives_shipping_income": True,
                "no_courier": False,
            },
        )
        self.assertEqual(
            result["price_list"],
            {
                "name": PRICE_LIST,
                "display_label": PRICE_LIST,
                "currency": "EGP",
                "is_default": True,
                "zero_shipping_default": False,
            },
        )
        self.assertIsNone(resolve_price.call_args.kwargs["requested_price_list"])
        self.assertEqual(
            resolve_price.call_args.kwargs["customer_doc"].default_price_list,
            PRICE_LIST,
        )

    def test_sample_contract_preserves_configured_policy_list(self):
        result, _ = self._run_context(
            "Sample - Courier",
            configured_price_list=PRICE_LIST,
        )
        self.assertEqual(result["commercial_policy"]["price_list"], PRICE_LIST)
        self.assertEqual(result["commercial_policy"]["discount_percentage"], 100.0)

    def test_null_customer_list_allows_group_or_baseline_resolution(self):
        result, resolve_price = self._run_context(
            customer_price_list=None,
            customer_group="_TEST B2B Group",
        )
        passed_customer = resolve_price.call_args.kwargs["customer_doc"]
        self.assertIsNone(passed_customer.default_price_list)
        self.assertEqual(passed_customer.customer_group, "_TEST B2B Group")
        self.assertEqual(result["price_list"]["name"], PRICE_LIST)

    def test_manager_may_resolve_without_profile_membership(self):
        result, _ = self._run_context(roles=["JARZ Manager"], assigned=False)
        self.assertEqual(result["price_list"]["name"], PRICE_LIST)

    def test_nonmember_rep_is_denied(self):
        with self.assertRaises(frappe.PermissionError):
            self._run_context(assigned=False)

    def test_actor_without_b2b_or_manager_role_is_denied(self):
        with self.assertRaises(frappe.PermissionError):
            self._run_context(roles=["Sales User"])

    def test_inaccessible_customer_is_denied(self):
        with self.assertRaises(frappe.PermissionError):
            self._run_context(customer_permission=False)

    def test_disabled_customer_is_denied(self):
        with self.assertRaises(frappe.ValidationError):
            self._run_context(customer_disabled=True)

    def test_disabled_or_buying_only_price_list_is_denied(self):
        for enabled, selling in ((False, True), (True, False)):
            with self.subTest(enabled=enabled, selling=selling), self.assertRaises(
                frappe.ValidationError
            ):
                self._run_context(
                    price_list_enabled=enabled,
                    price_list_selling=selling,
                )

    def test_standard_and_incomplete_context_are_denied(self):
        with self.assertRaises(frappe.ValidationError):
            pos._resolve_b2b_pricing_context(PROFILE, CUSTOMER, "Standard")
        for profile, customer, purpose in (
            ("", CUSTOMER, "B2B Supply"),
            (PROFILE, "", "B2B Supply"),
            (PROFILE, CUSTOMER, ""),
        ):
            with self.subTest(profile=profile, customer=customer, purpose=purpose), self.assertRaises(
                frappe.ValidationError
            ):
                pos._resolve_b2b_pricing_context(profile, customer, purpose)

    def test_arbitrary_echoed_price_list_is_denied(self):
        with self.assertRaises(frappe.ValidationError):
            self._run_context(requested_price_list="_TEST Unrelated Selling")


class TestB2BCatalogContext(unittest.TestCase):
    def test_products_use_revalidated_context_price_list(self):
        context = {"customer": CUSTOMER, "price_list": {"name": PRICE_LIST}}

        def get_all(doctype, **kwargs):
            if doctype == "POS Profile Item Group":
                return ["Products"]
            if doctype == "Item":
                return [
                    {
                        "id": "ITEM-1",
                        "name": "Product",
                        "price": 10,
                        "item_group": "Products",
                        "allow_negative_stock": 0,
                    }
                ]
            raise AssertionError(f"Unexpected get_all: {doctype}")

        def get_value(doctype, filters, fieldname):
            if doctype == "POS Profile" and fieldname == "warehouse":
                return None
            raise AssertionError(f"Unexpected get_value: {doctype}")

        with patch.object(
            pos, "_resolve_b2b_pricing_context", return_value=context
        ) as resolve_context, patch.object(
            pos.frappe, "get_all", side_effect=get_all
        ), patch.object(pos.frappe.db, "get_value", side_effect=get_value), patch.object(
            pos, "_get_b2b_catalog_item_rate", return_value=42
        ) as resolve_rate:
            rows = pos.get_profile_products(
                PROFILE,
                price_list=PRICE_LIST,
                customer=CUSTOMER,
                order_purpose="B2B Supply",
            )

        resolve_context.assert_called_once_with(
            PROFILE,
            CUSTOMER,
            "B2B Supply",
            requested_price_list=PRICE_LIST,
        )
        self.assertEqual(rows[0]["price"], 42.0)
        self.assertEqual(rows[0]["price_list"], PRICE_LIST)
        resolve_rate.assert_called_once_with("ITEM-1", PRICE_LIST, 10, CUSTOMER)

    def test_b2b_catalog_rate_delegates_customer_and_fallback_to_invoice_engine(self):
        with patch(
            "jarz_pos.services.invoice_creation._resolve_item_rate",
            return_value=73,
        ) as resolve_rate:
            result = pos._get_b2b_catalog_item_rate(
                "ITEM-1",
                PRICE_LIST,
                12,
                CUSTOMER,
            )
        resolve_rate.assert_called_once_with(
            "ITEM-1",
            PRICE_LIST,
            fallback_rate=12,
            customer=CUSTOMER,
        )
        self.assertEqual(result, 73)

    def test_partial_catalog_context_is_rejected_by_context_resolver(self):
        with patch(
            "jarz_pos.utils.validation_utils.assert_pos_profile_enabled"
        ), patch.object(
            pos,
            "_resolve_b2b_pricing_context",
            side_effect=frappe.ValidationError("incomplete"),
        ) as resolve_context:
            with self.assertRaises(frappe.ValidationError):
                pos.get_profile_bundles(PROFILE, customer=CUSTOMER)
        resolve_context.assert_called_once_with(
            PROFILE,
            CUSTOMER,
            "",
            requested_price_list=None,
        )

    def test_explicit_empty_catalog_context_does_not_fall_back_to_legacy(self):
        with patch(
            "jarz_pos.utils.validation_utils.assert_pos_profile_enabled"
        ), patch.object(
            pos,
            "_resolve_b2b_pricing_context",
            side_effect=frappe.ValidationError("incomplete"),
        ) as resolve_context:
            with self.assertRaises(frappe.ValidationError):
                pos.get_profile_bundles(PROFILE, customer="", order_purpose="")
        resolve_context.assert_called_once_with(
            PROFILE,
            "",
            "",
            requested_price_list=None,
        )

    def test_legacy_catalog_without_context_uses_existing_resolution(self):
        with patch.object(
            pos, "_resolve_effective_price_list", return_value=(None, None)
        ) as legacy_resolve, patch.object(pos.frappe, "get_all", return_value=[]):
            rows = pos.get_profile_products(PROFILE)
        legacy_resolve.assert_called_once_with(PROFILE, requested_price_list=None)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()

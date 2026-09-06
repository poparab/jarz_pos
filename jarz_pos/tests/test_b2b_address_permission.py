"""Focused permission contract for B2B-linked Addresses."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jarz_pos.permissions import address


class TestB2BAddressPermission(unittest.TestCase):
    def _check(self, *, linked_customers, readable_customers, roles=None):
        roles = roles or ["B2B Sales Rep"]

        def can_read(doctype, **kwargs):
            self.assertEqual(doctype, "Customer")
            self.assertEqual(kwargs["ptype"], "read")
            self.assertEqual(kwargs["user"], "rep@example.invalid")
            return kwargs["doc"] in readable_customers

        with patch.object(address.frappe, "get_roles", return_value=roles), patch.object(
            address, "_linked_customers", return_value=linked_customers
        ), patch.object(address.frappe, "has_permission", side_effect=can_read):
            return address.has_permission(
                SimpleNamespace(name="ADDR-1"),
                ptype="read",
                user="rep@example.invalid",
            )

    def test_linked_readable_customer_allows_address_read(self):
        self.assertTrue(
            self._check(
                linked_customers=["CUST-OWNED"],
                readable_customers={"CUST-OWNED"},
            )
        )

    def test_address_linked_only_to_unreadable_customer_is_denied(self):
        self.assertFalse(
            self._check(
                linked_customers=["CUST-FOREIGN"],
                readable_customers=set(),
            )
        )

    def test_unlinked_address_is_denied(self):
        self.assertFalse(self._check(linked_customers=[], readable_customers=set()))

    def test_non_b2b_user_keeps_existing_permission_result(self):
        with patch.object(address.frappe, "get_roles", return_value=["Sales User"]), patch.object(
            address, "_linked_customers"
        ) as linked:
            self.assertTrue(
                address.has_permission(
                    SimpleNamespace(name="ADDR-1"),
                    ptype="read",
                    user="sales@example.invalid",
                )
            )
        linked.assert_not_called()

    def test_manager_with_b2b_role_keeps_existing_permission_result(self):
        with patch.object(
            address.frappe,
            "get_roles",
            return_value=["B2B Sales Rep", "System Manager"],
        ), patch.object(address, "_linked_customers") as linked:
            self.assertTrue(
                address.has_permission(
                    SimpleNamespace(name="ADDR-1"),
                    ptype="read",
                    user="manager@example.invalid",
                )
            )
        linked.assert_not_called()

    def test_b2b_list_condition_requires_direct_customer_link(self):
        with patch.object(
            address.frappe, "get_roles", return_value=["B2B Sales Rep"]
        ), patch(
            "frappe.model.db_query.DatabaseQuery.build_match_conditions",
            return_value="`tabCustomer`.`territory` = 'ALLOWED'",
        ):
            condition = address.get_permission_query_conditions(
                "rep@example.invalid"
            )

        self.assertIn("`jarz_b2b_address_link`.`parent` = `tabAddress`.`name`", condition)
        self.assertIn("`jarz_b2b_address_link`.`link_doctype` = 'Customer'", condition)
        self.assertIn("`tabCustomer`.`territory` = 'ALLOWED'", condition)

    def test_non_b2b_list_condition_is_unchanged(self):
        with patch.object(address.frappe, "get_roles", return_value=["Sales User"]):
            self.assertEqual(
                address.get_permission_query_conditions("sales@example.invalid"),
                "",
            )


if __name__ == "__main__":
    unittest.main()

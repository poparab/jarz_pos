"""Regression: ``save_customer_shipping_address`` survives a bumped ``Customer.modified``.

Editing an address from the kanban dialog failed with HTTP 417 on staging (5 hits on
2026-07-31, e.g. Error Log ``8druqu61ub``):

    TimestampMismatchError: Error: <customer> (Customer) has been modified after you
    have opened it (…824282, …921359). Please refresh to get the latest document.

The endpoint loaded the Customer up front, then wrote the Address and the Contact — each
of which moves ``Customer.modified`` — before finally saving the stale in-memory doc.
Frappe's optimistic-lock check then aborted the whole request.

Two behaviours are pinned here:
  1. the Customer is read fresh at write time, so an intervening bump is not fatal;
  2. the phone lands in the Contact's ``phone_nos`` table, so ``Contact.validate()``
     does not blank ``mobile_no`` back to "" on save.

Rollback-based / light-DB, mirroring test_customer_address_link.py (plain unittest, not
FrappeTestCase, for ERPNext v16 CI-safety).
"""

from __future__ import annotations

import unittest
import uuid

import frappe
from frappe.model.document import bulk_insert

from jarz_pos.api.customer import (
    save_customer_shipping_address,
    update_customer_shipping_address,
)
from jarz_pos.utils.customer_address_utils import get_linked_customer_address_names

def _non_group_territory():
    """Return a leaf Territory name the site seeds (never insert one — nested set)."""
    return frappe.db.get_value("Territory", {"is_group": 0}, "name")


class TestSaveCustomerShippingAddress(unittest.TestCase):
    def setUp(self):
        self.original_user = frappe.session.user
        self.extra_customers = []
        self.test_users = []
        self.territory = _non_group_territory()
        self.assertTrue(self.territory, "site must seed at least one non-group Territory")
        suffix = uuid.uuid4().hex[:10]
        self.phone = "0109" + str(int(suffix, 16))[-7:].zfill(7)
        payload = {
            "doctype": "Customer",
            "customer_name": f"_TEST Addr Save Customer {suffix}",
            "customer_type": "Individual",
            "territory": self.territory,
        }
        if frappe.db.exists("Price List", "B2B Selling"):
            payload["default_price_list"] = "B2B Selling"
        payment_terms = frappe.db.get_value("Payment Terms Template", {}, "name")
        if payment_terms:
            payload["payment_terms"] = payment_terms
        self.customer = frappe.get_doc(payload).insert(ignore_permissions=True)

    def tearDown(self):
        frappe.set_user(self.original_user)
        frappe.db.rollback()
        customer_names = [self.customer.name, *self.extra_customers]
        for customer_name in customer_names:
            if not frappe.db.exists("Customer", customer_name):
                continue
            addresses = frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Address",
                    "link_doctype": "Customer",
                    "link_name": customer_name,
                },
                pluck="parent",
                limit_page_length=0,
            )
            contacts = frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Contact",
                    "link_doctype": "Customer",
                    "link_name": customer_name,
                },
                pluck="parent",
                limit_page_length=0,
            )
            for address_name in set(addresses):
                if frappe.db.exists("Address", address_name):
                    frappe.delete_doc(
                        "Address", address_name, force=True, ignore_permissions=True
                    )
            for contact_name in set(contacts):
                if frappe.db.exists("Contact", contact_name):
                    frappe.delete_doc(
                        "Contact", contact_name, force=True, ignore_permissions=True
                    )
            frappe.delete_doc(
                "Customer", customer_name, force=True, ignore_permissions=True
            )
        for user_name in self.test_users:
            if frappe.db.exists("User", user_name):
                frappe.db.delete(
                    "Has Role", {"parent": user_name, "parenttype": "User"}
                )
                frappe.db.delete("User", {"name": user_name})
                frappe.clear_cache(user=user_name)
        frappe.db.commit()

    def _make_b2b_user(self):
        email = f"_test-b2b-address-{uuid.uuid4().hex[:10]}@example.invalid"
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": "B2B Address Test",
                "enabled": 1,
                "user_type": "System User",
                "send_welcome_email": 0,
                "roles": [{"role": "B2B Sales Rep"}],
            }
        )
        user.name = email
        for index, role in enumerate(user.roles, start=1):
            role.name = frappe.generate_hash(length=10)
            role.parent = email
            role.parenttype = "User"
            role.parentfield = "roles"
            role.idx = index
        # This is only an authorization fixture. Bypass unrelated User hooks so
        # site-specific welcome/onboarding automation cannot affect this module.
        bulk_insert("User", [user])
        frappe.clear_cache(user=email)
        self.test_users.append(email)
        return email

    def test_save_succeeds_when_customer_modified_moved(self):
        """A concurrent bump of Customer.modified must not fail the address save."""
        result = save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address="17 Timestamp Street",
            territory=self.territory,
        )
        self.assertTrue(result["success"])

        # Simulate the Address/Contact writes moving the row out from under a
        # doc the caller loaded earlier — this is what raised TimestampMismatchError.
        stale = frappe.get_doc("Customer", self.customer.name)
        frappe.db.set_value("Customer", self.customer.name, "customer_details", "bumped")
        self.assertNotEqual(
            str(stale.modified),
            str(frappe.db.get_value("Customer", self.customer.name, "modified")),
            "precondition: the row must actually be newer than the loaded doc",
        )

        second = save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address="18 Timestamp Street",
            territory=self.territory,
        )
        self.assertTrue(second["success"])
        self.assertEqual(
            frappe.db.get_value("Address", second["selected_address_name"], "address_line1"),
            "18 Timestamp Street",
        )

    def test_selecting_an_existing_address_relinks_without_conflict(self):
        """The kanban path — an address_name, not free text — is the one that broke."""
        first = save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address="21 Reselect Road",
            territory=self.territory,
        )
        address_name = first["selected_address_name"]
        self.assertIn(address_name, get_linked_customer_address_names(self.customer.name))

        again = save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address_name=address_name,
            territory=self.territory,
        )
        self.assertTrue(again["success"])
        self.assertEqual(again["selected_address_name"], address_name)
        self.assertEqual(
            frappe.db.get_value("Customer", self.customer.name, "customer_primary_address"),
            address_name,
        )

    def test_phone_survives_contact_validate(self):
        """Contact.set_primary() rebuilds mobile_no from phone_nos — it must find the row."""
        save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address="33 Phone Lane",
            territory=self.territory,
        )

        contact_name = frappe.db.get_value(
            "Customer", self.customer.name, "customer_primary_contact"
        )
        self.assertTrue(contact_name, "a primary contact should have been created")

        contact = frappe.get_doc("Contact", contact_name)
        self.assertEqual(contact.mobile_no, self.phone)
        self.assertIn(self.phone, [str(row.phone or "").strip() for row in contact.phone_nos])
        self.assertEqual(
            sum(1 for row in contact.phone_nos if row.is_primary_mobile_no),
            1,
            "exactly one row may be the primary mobile, or Contact.validate() throws",
        )

    def test_named_b2b_branch_does_not_change_primary_or_commercial_fields(self):
        """A second delivery branch belongs to the same shared Customer account."""
        before = frappe.db.get_value(
            "Customer",
            self.customer.name,
            [
                "customer_primary_address",
                "customer_type",
                "customer_group",
                "territory",
                "default_price_list",
                "payment_terms",
            ],
            as_dict=True,
        )

        result = save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address="Madinaty All Seasons Park",
            territory=self.territory,
            branch_name="ILO - All Seasons",
            set_as_primary=0,
        )

        address_name = result["selected_address_name"]
        self.assertEqual(
            frappe.db.get_value("Address", address_name, "address_title"),
            "ILO - All Seasons",
        )
        self.assertEqual(result["selected_address"]["branch_name"], "ILO - All Seasons")
        after = frappe.db.get_value(
            "Customer",
            self.customer.name,
            list(before.keys()),
            as_dict=True,
        )
        self.assertEqual(after, before)

    def test_b2b_only_rep_can_select_add_and_edit_nonprimary_branches(self):
        """Branch work must not rewrite the shared Customer or primary Contact."""
        primary = save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address="10 Shared Primary Street",
            territory=self.territory,
        )
        primary_contact = frappe.db.get_value(
            "Customer", self.customer.name, "customer_primary_contact"
        )
        self.assertTrue(primary_contact)

        existing_branch = frappe.get_doc(
            {
                "doctype": "Address",
                "address_title": "Existing B2B Branch",
                "address_type": "Billing",
                "address_line1": "20 Existing Branch Street",
                "city": self.territory,
                "country": "Egypt",
                "is_primary_address": 1,
                "is_shipping_address": 0,
                "links": [
                    {"link_doctype": "Customer", "link_name": self.customer.name}
                ],
            }
        ).insert(ignore_permissions=True)

        customer_fields = [
            "mobile_no",
            "customer_primary_address",
            "customer_primary_contact",
            "customer_type",
            "customer_group",
            "territory",
            "default_price_list",
            "payment_terms",
        ]
        if frappe.db.has_column("Customer", "phone"):
            customer_fields.append("phone")
        customer_before = frappe.db.get_value(
            "Customer", self.customer.name, customer_fields, as_dict=True
        )
        contact_before = frappe.db.get_value(
            "Contact",
            primary_contact,
            ["mobile_no", "phone", "is_primary_contact", "modified"],
            as_dict=True,
        )
        linked_contacts_before = set(
            frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Contact",
                    "link_doctype": "Customer",
                    "link_name": self.customer.name,
                },
                pluck="parent",
                limit_page_length=0,
            )
        )
        branch_phone = "01000009998"
        b2b_user = self._make_b2b_user()

        try:
            frappe.set_user(b2b_user)
            selected = save_customer_shipping_address(
                customer=self.customer.name,
                phone=branch_phone,
                address_name=existing_branch.name,
                set_as_primary=0,
            )
            created = save_customer_shipping_address(
                customer=self.customer.name,
                phone=branch_phone,
                address="30 New B2B Branch Street",
                territory=self.territory,
                branch_name="New B2B Branch",
                set_as_primary=0,
            )
            edited = update_customer_shipping_address(
                customer=self.customer.name,
                address_name=created["selected_address_name"],
                branch_name="Edited B2B Branch",
                phone=branch_phone,
            )
        finally:
            frappe.set_user(self.original_user)

        self.assertTrue(selected["success"])
        self.assertEqual(selected["selected_address_name"], existing_branch.name)
        self.assertEqual(selected["selected_address"]["phone"], branch_phone)
        self.assertEqual(
            frappe.db.get_value(
                "Address",
                existing_branch.name,
                ["address_type", "is_primary_address", "is_shipping_address"],
            ),
            ("Billing", 1, 0),
        )
        self.assertEqual(created["selected_address"]["phone"], branch_phone)
        edited_option = next(
            row
            for row in edited["branch_options"]
            if row["address_name"] == created["selected_address_name"]
        )
        self.assertEqual(edited_option["branch_name"], "Edited B2B Branch")
        self.assertEqual(
            frappe.db.get_value(
                "Customer", self.customer.name, customer_fields, as_dict=True
            ),
            customer_before,
        )
        self.assertEqual(
            frappe.db.get_value(
                "Contact",
                primary_contact,
                ["mobile_no", "phone", "is_primary_contact", "modified"],
                as_dict=True,
            ),
            contact_before,
        )
        linked_contacts_after = set(
            frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Contact",
                    "link_doctype": "Customer",
                    "link_name": self.customer.name,
                },
                pluck="parent",
                limit_page_length=0,
            )
        )
        self.assertEqual(linked_contacts_after, linked_contacts_before)
        self.assertEqual(
            customer_before["customer_primary_address"],
            primary["selected_address_name"],
        )

    def test_b2b_only_rep_cannot_select_a_foreign_address(self):
        suffix = uuid.uuid4().hex[:10]
        foreign_customer = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": f"_TEST Foreign Address Customer {suffix}",
                "customer_type": "Company",
                "territory": self.territory,
            }
        ).insert(ignore_permissions=True)
        self.extra_customers.append(foreign_customer.name)
        foreign_address = frappe.get_doc(
            {
                "doctype": "Address",
                "address_title": f"Foreign Branch {suffix}",
                "address_type": "Shipping",
                "address_line1": "90 Foreign Branch Street",
                "city": self.territory,
                "country": "Egypt",
                "is_shipping_address": 1,
                "links": [
                    {"link_doctype": "Customer", "link_name": foreign_customer.name}
                ],
            }
        ).insert(ignore_permissions=True)
        b2b_user = self._make_b2b_user()

        try:
            frappe.set_user(b2b_user)
            with self.assertRaises(frappe.ValidationError):
                save_customer_shipping_address(
                    customer=self.customer.name,
                    phone=self.phone,
                    address_name=foreign_address.name,
                    set_as_primary=0,
                )
        finally:
            frappe.set_user(self.original_user)

    def test_branch_name_can_be_edited_without_changing_customer_terms(self):
        created = save_customer_shipping_address(
            customer=self.customer.name,
            phone=self.phone,
            address="Heliopolis Branch",
            territory=self.territory,
            branch_name="ILO - Heliopolis",
            set_as_primary=0,
        )
        before = frappe.db.get_value(
            "Customer",
            self.customer.name,
            ["customer_primary_address", "default_price_list", "payment_terms", "territory"],
            as_dict=True,
        )

        result = update_customer_shipping_address(
            customer=self.customer.name,
            address_name=created["selected_address_name"],
            branch_name="ILO - Heliopolis Main",
        )

        self.assertEqual(
            frappe.db.get_value(
                "Address", created["selected_address_name"], "address_title"
            ),
            "ILO - Heliopolis Main",
        )
        option = next(
            row
            for row in result["branch_options"]
            if row["address_name"] == created["selected_address_name"]
        )
        self.assertEqual(option["branch_name"], "ILO - Heliopolis Main")
        after = frappe.db.get_value(
            "Customer", self.customer.name, list(before.keys()), as_dict=True
        )
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

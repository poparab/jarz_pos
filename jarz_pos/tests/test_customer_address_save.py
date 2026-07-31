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

import frappe

from jarz_pos.api.customer import save_customer_shipping_address
from jarz_pos.utils.customer_address_utils import get_linked_customer_address_names

PHONE = "0100999731"


def _non_group_territory():
    """Return a leaf Territory name the site seeds (never insert one — nested set)."""
    return frappe.db.get_value("Territory", {"is_group": 0}, "name")


class TestSaveCustomerShippingAddress(unittest.TestCase):
    def setUp(self):
        self.territory = _non_group_territory()
        self.assertTrue(self.territory, "site must seed at least one non-group Territory")
        self.customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": "_TEST Addr Save Customer",
            "customer_type": "Individual",
            "territory": self.territory,
        }).insert(ignore_permissions=True)

    def tearDown(self):
        frappe.db.rollback()

    def test_save_succeeds_when_customer_modified_moved(self):
        """A concurrent bump of Customer.modified must not fail the address save."""
        result = save_customer_shipping_address(
            customer=self.customer.name,
            phone=PHONE,
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
            phone=PHONE,
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
            phone=PHONE,
            address="21 Reselect Road",
            territory=self.territory,
        )
        address_name = first["selected_address_name"]
        self.assertIn(address_name, get_linked_customer_address_names(self.customer.name))

        again = save_customer_shipping_address(
            customer=self.customer.name,
            phone=PHONE,
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
            phone=PHONE,
            address="33 Phone Lane",
            territory=self.territory,
        )

        contact_name = frappe.db.get_value(
            "Customer", self.customer.name, "customer_primary_contact"
        )
        self.assertTrue(contact_name, "a primary contact should have been created")

        contact = frappe.get_doc("Contact", contact_name)
        self.assertEqual(contact.mobile_no, PHONE)
        self.assertIn(PHONE, [str(row.phone or "").strip() for row in contact.phone_nos])
        self.assertEqual(
            sum(1 for row in contact.phone_nos if row.is_primary_mobile_no),
            1,
            "exactly one row may be the primary mobile, or Contact.validate() throws",
        )


if __name__ == "__main__":
    unittest.main()

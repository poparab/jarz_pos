"""Fix C regression: ``create_customer`` LINKS an existing Address docname instead of
duplicating it as free text.

The B2B ``request_sample`` flow converts a Lead to a Customer and passes the Lead's
existing primary Address *docname* as ``customer_primary_address``. Previously that value
was always treated as a free-text ``address_line1``, creating a NEW Address. Now, when the
value matches an existing Address, it is linked to the new Customer; any other value keeps
the original free-text create-new behavior.

Rollback-based / light-DB, mirroring test_leads_api.py (plain unittest, not
FrappeTestCase, for ERPNext v16 CI-safety: insert on the live connection, uncommitted,
and ``frappe.db.rollback()`` in tearDown).
"""

from __future__ import annotations

import unittest

import frappe

from jarz_pos.api.customer import create_customer
from jarz_pos.utils.customer_address_utils import get_linked_customer_address_names


def _non_group_territory():
    """Return a leaf Territory name the site seeds (never insert one — nested set)."""
    return frappe.db.get_value("Territory", {"is_group": 0}, "name")


def _any_country():
    return frappe.db.get_value("Country", {}, "name")


class TestCreateCustomerLinksExistingAddress(unittest.TestCase):
    def tearDown(self):
        frappe.db.rollback()

    def _make_standalone_address(self, title):
        payload = {
            "doctype": "Address",
            "address_title": title,
            "address_type": "Shipping",
            "address_line1": "12 Existing Rd",
            "city": "Cairo",
            "is_primary_address": 1,
            "is_shipping_address": 1,
        }
        country = _any_country()
        if country:
            payload["country"] = country
        return frappe.get_doc(payload).insert(ignore_permissions=True)

    def test_existing_address_docname_is_linked_not_duplicated(self):
        territory = _non_group_territory()
        self.assertTrue(territory, "site must seed at least one non-group Territory")

        existing = self._make_standalone_address("_TEST Existing Addr")
        addr_count_before = frappe.db.count("Address")

        result = create_customer(
            customer_name="_TEST Link Customer",
            mobile_no="0100999001",
            customer_primary_address=existing.name,  # an existing Address docname
            territory_id=territory,
        )

        # The customer's primary address IS the existing address (no duplicate created).
        self.assertEqual(result["customer_primary_address"], existing.name)
        # No new Address row was inserted for the address.
        self.assertEqual(frappe.db.count("Address"), addr_count_before)
        # The existing Address is now dynamically linked to the new Customer.
        linked = get_linked_customer_address_names(result["name"])
        self.assertIn(existing.name, linked)

    def test_free_text_address_still_creates_new(self):
        territory = _non_group_territory()
        addr_count_before = frappe.db.count("Address")

        result = create_customer(
            customer_name="_TEST FreeText Customer",
            mobile_no="0100999002",
            customer_primary_address="45 Brand New Street",  # free text, not a docname
            territory_id=territory,
        )

        # A brand-new Address was created and set as the primary address.
        self.assertEqual(frappe.db.count("Address"), addr_count_before + 1)
        primary = result["customer_primary_address"]
        self.assertTrue(primary)
        self.assertEqual(
            frappe.db.get_value("Address", primary, "address_line1"),
            "45 Brand New Street",
        )


if __name__ == "__main__":
    unittest.main()

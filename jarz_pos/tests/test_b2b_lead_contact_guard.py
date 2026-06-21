"""customer._contacts_block_lead_conversion — lead-aware Contact guard (pure mock).

When the B2B flow converts a Lead -> Customer, Frappe has already auto-created a
Contact for that Lead carrying its mobile. The strict Contact-mobile guard in
``create_customer`` would wrongly block that conversion. The helper under test
decides whether existing Contacts with the same mobile should still block:

  - block only when a *conflicting* third-party / Customer Contact shares the mobile;
  - the Lead's own auto-created Contact (linked to source_lead, not to any Customer)
    is ignored;
  - on ANY error, fall back to blocking (never create duplicate customers).

``frappe.get_all`` is fully mocked; no DB runs.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_pos.api import customer as cust_api


def _make_get_all(contacts, lead_links, customer_links, raise_exc=False):
    """Build a fake frappe.get_all dispatching on (doctype, filters).

    contacts: list returned for the Contact-by-mobile query.
    lead_links: dict {contact_name: truthy?} for the Lead Dynamic Link query.
    customer_links: dict {contact_name: truthy?} for the Customer Dynamic Link query.
    """

    def _fake(doctype, filters=None, pluck=None, limit=None, **kwargs):
        if raise_exc:
            raise RuntimeError("boom")
        filters = filters or {}
        if doctype == "Contact":
            return list(contacts)
        if doctype == "Dynamic Link":
            parent = filters.get("parent")
            link_doctype = filters.get("link_doctype")
            if link_doctype == "Lead":
                return [1] if lead_links.get(parent) else []
            if link_doctype == "Customer":
                return [1] if customer_links.get(parent) else []
        return []

    return _fake


class TestContactsBlockLeadConversion(unittest.TestCase):
    def test_no_conflicting_contact_not_blocked(self):
        fake = _make_get_all(contacts=[], lead_links={}, customer_links={})
        with patch.object(cust_api.frappe, "get_all", side_effect=fake):
            self.assertFalse(
                cust_api._contacts_block_lead_conversion("0100", "LEAD-1")
            )

    def test_own_lead_contact_only_not_blocked(self):
        # Single contact linked to the source lead and to no customer -> ignore.
        fake = _make_get_all(
            contacts=["CONTACT-A"],
            lead_links={"CONTACT-A": True},
            customer_links={"CONTACT-A": False},
        )
        with patch.object(cust_api.frappe, "get_all", side_effect=fake):
            self.assertFalse(
                cust_api._contacts_block_lead_conversion("0100", "LEAD-1")
            )

    def test_contact_linked_to_different_lead_blocked(self):
        # Contact not linked to source_lead -> real conflict -> blocked.
        fake = _make_get_all(
            contacts=["CONTACT-B"],
            lead_links={"CONTACT-B": False},
            customer_links={"CONTACT-B": False},
        )
        with patch.object(cust_api.frappe, "get_all", side_effect=fake):
            self.assertTrue(
                cust_api._contacts_block_lead_conversion("0100", "LEAD-1")
            )

    def test_contact_linked_to_customer_blocked(self):
        # Contact linked to source_lead but ALSO to a Customer -> blocked.
        fake = _make_get_all(
            contacts=["CONTACT-C"],
            lead_links={"CONTACT-C": True},
            customer_links={"CONTACT-C": True},
        )
        with patch.object(cust_api.frappe, "get_all", side_effect=fake):
            self.assertTrue(
                cust_api._contacts_block_lead_conversion("0100", "LEAD-1")
            )

    def test_mixed_contacts_any_conflict_blocks(self):
        # One ignorable own-lead contact + one third-party contact -> blocked.
        fake = _make_get_all(
            contacts=["CONTACT-OK", "CONTACT-BAD"],
            lead_links={"CONTACT-OK": True, "CONTACT-BAD": False},
            customer_links={"CONTACT-OK": False, "CONTACT-BAD": False},
        )
        with patch.object(cust_api.frappe, "get_all", side_effect=fake):
            self.assertTrue(
                cust_api._contacts_block_lead_conversion("0100", "LEAD-1")
            )

    def test_exception_falls_back_to_blocked(self):
        fake = _make_get_all(
            contacts=[], lead_links={}, customer_links={}, raise_exc=True
        )
        with patch.object(cust_api.frappe, "get_all", side_effect=fake):
            self.assertTrue(
                cust_api._contacts_block_lead_conversion("0100", "LEAD-1")
            )


if __name__ == "__main__":
    unittest.main()

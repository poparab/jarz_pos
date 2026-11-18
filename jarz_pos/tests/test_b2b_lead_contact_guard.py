"""customer._contacts_block_lead_conversion — lead-aware Contact guard (pure mock).

When the B2B flow converts a Lead -> Customer, Frappe has already auto-created a
Contact for that Lead carrying its mobile. The strict Contact-mobile guard in
``create_customer`` would wrongly block that conversion. The helper under test
decides whether existing Contacts with the same mobile should still block:

  - block only when a Contact linked to an existing Customer shares the mobile;
  - the Lead's own auto-created Contact, another Lead's Contact and a Supplier's
    Contact are ignored (2026-11-18: 1,565 Lead and 30 Supplier contacts on
    production held numbers that could never become a customer);
  - on ANY error, fall back to blocking (never create duplicate customers).

``_contacts_block_customer_create`` is the same rule for the plain B2C create.

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
			self.assertFalse(cust_api._contacts_block_lead_conversion("0100", "LEAD-1"))

	def test_own_lead_contact_only_not_blocked(self):
		# Single contact linked to the source lead and to no customer -> ignore.
		fake = _make_get_all(
			contacts=["CONTACT-A"],
			lead_links={"CONTACT-A": True},
			customer_links={"CONTACT-A": False},
		)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertFalse(cust_api._contacts_block_lead_conversion("0100", "LEAD-1"))

	def test_contact_linked_to_different_lead_not_blocked(self):
		# Another Lead's (or a Supplier's) contact is not a customer -> not blocked.
		fake = _make_get_all(
			contacts=["CONTACT-B"],
			lead_links={"CONTACT-B": False},
			customer_links={"CONTACT-B": False},
		)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertFalse(cust_api._contacts_block_lead_conversion("0100", "LEAD-1"))

	def test_contact_linked_to_customer_blocked(self):
		# Contact linked to source_lead but ALSO to a Customer -> blocked.
		fake = _make_get_all(
			contacts=["CONTACT-C"],
			lead_links={"CONTACT-C": True},
			customer_links={"CONTACT-C": True},
		)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertTrue(cust_api._contacts_block_lead_conversion("0100", "LEAD-1"))

	def test_mixed_contacts_customer_contact_blocks(self):
		# Own-lead contact + another party's contact + a Customer's contact -> blocked.
		fake = _make_get_all(
			contacts=["CONTACT-OK", "CONTACT-OTHER", "CONTACT-CUST"],
			lead_links={"CONTACT-OK": True},
			customer_links={"CONTACT-CUST": True},
		)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertTrue(cust_api._contacts_block_lead_conversion("0100", "LEAD-1"))

	def test_mixed_contacts_without_customer_not_blocked(self):
		fake = _make_get_all(
			contacts=["CONTACT-OK", "CONTACT-OTHER"],
			lead_links={"CONTACT-OK": True},
			customer_links={},
		)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertFalse(cust_api._contacts_block_lead_conversion("0100", "LEAD-1"))

	def test_exception_falls_back_to_blocked(self):
		fake = _make_get_all(contacts=[], lead_links={}, customer_links={}, raise_exc=True)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertTrue(cust_api._contacts_block_lead_conversion("0100", "LEAD-1"))


class TestContactsBlockCustomerCreate(unittest.TestCase):
	"""The plain (B2C) create: only a Customer's Contact blocks."""

	def test_no_contact_not_blocked(self):
		fake = _make_get_all(contacts=[], lead_links={}, customer_links={})
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertFalse(cust_api._contacts_block_customer_create("01117220850"))

	def test_lead_or_supplier_contact_not_blocked(self):
		fake = _make_get_all(
			contacts=["LEAD-CONTACT", "SUPPLIER-CONTACT"],
			lead_links={"LEAD-CONTACT": True},
			customer_links={},
		)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertFalse(cust_api._contacts_block_customer_create("01147998816"))

	def test_customer_contact_blocked(self):
		fake = _make_get_all(
			contacts=["SUPPLIER-CONTACT", "CUSTOMER-CONTACT"],
			lead_links={},
			customer_links={"CUSTOMER-CONTACT": True},
		)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertTrue(cust_api._contacts_block_customer_create("01117220850"))

	def test_contact_query_uses_every_spelling(self):
		seen = {}

		def _fake(doctype, filters=None, pluck=None, limit=None, **kwargs):
			if doctype == "Contact":
				seen["forms"] = filters["mobile_no"][1]
			return []

		with patch.object(cust_api.frappe, "get_all", side_effect=_fake):
			cust_api._contacts_block_customer_create("01117220850")
		self.assertIn("01117220850", seen["forms"])
		self.assertIn("+201117220850", seen["forms"])

	def test_exception_falls_back_to_blocked(self):
		fake = _make_get_all(contacts=[], lead_links={}, customer_links={}, raise_exc=True)
		with patch.object(cust_api.frappe, "get_all", side_effect=fake):
			self.assertTrue(cust_api._contacts_block_customer_create("0100"))


if __name__ == "__main__":
	unittest.main()

"""Tests for the B2B base-price seeder (``setup/b2b_pricing``).

The rule is one sentence — the "B2B Selling" list prices every large jar at 92 and
every medium at 77 — but the important half of this module is what it must NOT do.
It is create-only, unlike ``setup/employee_pricing``, because a B2B price is
negotiated and the Pricing page exists to change it. So the cases that matter are:

  * a rate somebody set in the UI survives a migrate (no realign);
  * ``realign=True`` DOES correct it, and says so where it can be found later;
  * a customer-scoped Item Price — a negotiated per-account rate — is never read
    as "the" rate and never rewritten, in either mode;
  * a category row is never invented for an Item Group the site does not have,
    because a Link to a missing record poisons every later save that touches it.

Mock-level, like ``test_employee_pricing``: the module's ``frappe`` is replaced, so
the suite never writes an Item Price to the site it runs against.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.setup import b2b_pricing as bp


_CATEGORY_DOCTYPE = "Jarz Price List Category Rate"


class _Site:
	"""A tiny in-memory stand-in for the pricing tables this module touches."""

	def __init__(
		self,
		*,
		item_groups=("Large", "Medium"),
		items=None,
		category_rates=None,
		item_prices=None,
		price_list_exists=True,
	):
		self.item_groups = set(item_groups)
		#: item_group -> [item_code]
		self.items = dict(items or {})
		self.category_rates = [dict(r) for r in (category_rates or [])]
		self.item_prices = [dict(r) for r in (item_prices or [])]
		self.price_list_exists = price_list_exists
		#: (doctype, name, field, value) for every db.set_value
		self.writes = []
		#: the dicts handed to frappe.get_doc(...).insert()
		self.inserts = []
		#: (message, title) for every frappe.log_error
		self.errors = []

	# --- frappe.db -------------------------------------------------------
	def exists(self, doctype, name=None):
		if doctype == "Price List":
			return self.price_list_exists and name == bp.PRICE_LIST
		if doctype == "Item Group":
			return name in self.item_groups
		return False

	def get_value(self, doctype, name=None, fieldname=None, **kwargs):
		if doctype == "Price List" and fieldname == "currency":
			return "EGP"
		if doctype == "Company" and fieldname == "default_currency":
			return "EGP"
		return None

	def set_value(self, doctype, name, field, value, update_modified=True):
		self.writes.append((doctype, name, field, value))
		rows = self.category_rates if doctype == _CATEGORY_DOCTYPE else self.item_prices
		for row in rows:
			if row["name"] == name:
				row[field] = value

	# --- frappe.get_all ---------------------------------------------------
	def get_all(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		filters = filters or {}
		if doctype == "Item":
			groups = filters.get("item_group", ["in", []])[1]
			out = []
			for group in groups:
				out.extend(self.items.get(group, []))
			return out
		if doctype == _CATEGORY_DOCTYPE:
			return [
				dict(r)
				for r in self.category_rates
				if r["price_list"] == filters.get("price_list")
				and r["item_group"] == filters.get("item_group")
			]
		if doctype == "Item Price":
			# The module only ever asks for GENERIC rows (customer in [None, ""]).
			assert filters.get("customer") == ["in", [None, ""]], (
				"the seeder must never read a customer-scoped Item Price"
			)
			return [
				dict(r)
				for r in self.item_prices
				if r["item_code"] == filters.get("item_code")
				and r["price_list"] == filters.get("price_list")
				and not r.get("customer")
			]
		return []

	# --- frappe.get_doc(...).insert() ------------------------------------
	def get_doc(self, payload):
		self.inserts.append(dict(payload))
		doc = MagicMock()
		doc.insert = MagicMock()
		return doc

	def log_error(self, message, title=None):
		self.errors.append((message, title))


def _run(site, **kwargs):
	"""Run the seeder against ``site`` with the module's frappe fully mocked."""
	fake = MagicMock()
	fake.db.exists.side_effect = site.exists
	fake.db.get_value.side_effect = site.get_value
	fake.db.set_value.side_effect = site.set_value
	fake.get_all.side_effect = site.get_all
	fake.get_doc.side_effect = site.get_doc
	fake.log_error.side_effect = site.log_error
	fake.defaults.get_global_default.return_value = "Jarz"
	with patch.object(bp, "frappe", fake):
		return bp.ensure_b2b_base_prices(**kwargs)


def _inserted_item_prices(site):
	return {
		r["item_code"]: r["price_list_rate"]
		for r in site.inserts
		if r["doctype"] == "Item Price"
	}


def _inserted_category_rates(site):
	return {
		r["item_group"]: r["rate"]
		for r in site.inserts
		if r["doctype"] == _CATEGORY_DOCTYPE
	}


class TestSeedsBothLayers(unittest.TestCase):
	def test_empty_list_gets_category_rates_and_item_prices(self):
		site = _Site(items={"Large": ["JAR-L1", "JAR-L2"], "Medium": ["JAR-M1"]})
		log = _run(site)

		self.assertEqual(
			_inserted_category_rates(site), {"Large": 92.0, "Medium": 77.0}
		)
		self.assertEqual(
			_inserted_item_prices(site),
			{"JAR-L1": 92.0, "JAR-L2": 92.0, "JAR-M1": 77.0},
		)
		self.assertEqual(log["summary"]["category_rates_created"], 2)
		self.assertEqual(log["summary"]["item_prices_created"], 3)
		self.assertEqual(site.writes, [], "seeding must not rewrite anything")

	def test_missing_item_group_gets_no_category_row(self):
		# "Meduim" does not exist here. Inventing a category row for it would create a
		# Link to a missing Item Group and break every later save that touches it.
		site = _Site(item_groups=("Large",), items={"Large": ["JAR-L1"]})
		log = _run(site)

		self.assertEqual(_inserted_category_rates(site), {"Large": 92.0})
		self.assertTrue(
			any("Medium" in s for s in log["summary"]["skipped"]),
			log["summary"]["skipped"],
		)

	def test_typo_group_is_priced_when_it_exists(self):
		# "Meduim" holds real items on some sites; missing it prices those jars wrong.
		site = _Site(
			item_groups=("Large", "Medium", "Meduim"),
			items={"Meduim": ["JAR-TYPO"], "Medium": [], "Large": []},
		)
		_run(site)

		self.assertEqual(_inserted_category_rates(site)["Meduim"], 77.0)
		self.assertEqual(_inserted_item_prices(site), {"JAR-TYPO": 77.0})

	def test_missing_price_list_is_a_clean_no_op(self):
		site = _Site(items={"Large": ["JAR-L1"]}, price_list_exists=False)
		log = _run(site)

		self.assertEqual(site.inserts, [])
		self.assertEqual(site.writes, [])
		self.assertTrue(log["summary"]["skipped"])


class TestCreateOnly(unittest.TestCase):
	"""The whole point of this module: a configured price is somebody's decision."""

	def test_existing_rate_is_left_alone_without_realign(self):
		site = _Site(
			items={"Large": ["JAR-L1"], "Medium": []},
			item_prices=[
				{
					"name": "IP-1",
					"item_code": "JAR-L1",
					"price_list": bp.PRICE_LIST,
					"price_list_rate": 100.0,
				}
			],
		)
		log = _run(site)

		self.assertEqual(site.writes, [])
		self.assertEqual(site.item_prices[0]["price_list_rate"], 100.0)
		self.assertEqual(log["summary"]["kept"], 1)
		self.assertTrue(any("JAR-L1" in s for s in log["kept"]))

	def test_existing_category_rate_is_left_alone_without_realign(self):
		site = _Site(
			items={"Large": [], "Medium": []},
			category_rates=[
				{
					"name": "CR-1",
					"price_list": bp.PRICE_LIST,
					"item_group": "Large",
					"rate": 105.0,
				}
			],
		)
		_run(site)

		self.assertEqual(site.writes, [])
		self.assertEqual(site.category_rates[0]["rate"], 105.0)

	def test_correct_rate_is_never_rewritten(self):
		# A no-op migrate must perform no writes at all, or every one of them churns
		# `modified` on rows nobody touched.
		site = _Site(
			items={"Large": ["JAR-L1"], "Medium": []},
			category_rates=[
				{
					"name": "CR-1",
					"price_list": bp.PRICE_LIST,
					"item_group": "Large",
					"rate": 92.0,
				},
				{
					"name": "CR-2",
					"price_list": bp.PRICE_LIST,
					"item_group": "Medium",
					"rate": 77.0,
				},
			],
			item_prices=[
				{
					"name": "IP-1",
					"item_code": "JAR-L1",
					"price_list": bp.PRICE_LIST,
					"price_list_rate": 92.0,
				}
			],
		)
		log = _run(site)

		self.assertEqual(site.writes, [])
		self.assertEqual(site.inserts, [])
		self.assertEqual(log["summary"]["kept"], 0)
		self.assertEqual(log["summary"]["unchanged"], 3)


class TestRealign(unittest.TestCase):
	"""The opt-in corrective pass — run by hand, never by the migrate hook."""

	def test_realign_corrects_a_stale_rate(self):
		site = _Site(
			items={"Large": ["JAR-L1"], "Medium": []},
			item_prices=[
				{
					"name": "IP-1",
					"item_code": "JAR-L1",
					"price_list": bp.PRICE_LIST,
					"price_list_rate": 100.0,
				}
			],
		)
		log = _run(site, realign=True)

		self.assertIn(("Item Price", "IP-1", "price_list_rate", 92.0), site.writes)
		self.assertEqual(site.item_prices[0]["price_list_rate"], 92.0)
		self.assertEqual(log["summary"]["item_prices_updated"], 1)

	def test_realign_reports_every_correction_to_the_error_log(self):
		# frappe.logger().info is effectively silent on the servers, so a reverted UI
		# edit with no Error Log entry is a change nobody can explain afterwards.
		site = _Site(
			items={"Large": ["JAR-L1"], "Medium": []},
			item_prices=[
				{
					"name": "IP-1",
					"item_code": "JAR-L1",
					"price_list": bp.PRICE_LIST,
					"price_list_rate": 100.0,
				}
			],
		)
		_run(site, realign=True)

		self.assertEqual(len(site.errors), 1)
		message, title = site.errors[0]
		self.assertIn("JAR-L1", message)
		self.assertIn("100.0 -> 92.0", message)
		self.assertIn("realigned", title)

	def test_seeding_alone_never_writes_an_error_log_entry(self):
		# Nothing was corrected, so there is nothing to explain: an Error Log row here
		# would be noise in the place operators go to find real problems.
		site = _Site(items={"Large": ["JAR-L1"], "Medium": []})
		_run(site)

		self.assertEqual(site.errors, [])


class TestConstants(unittest.TestCase):
	def test_rates_are_the_agreed_numbers(self):
		self.assertEqual(bp.LARGE_RATE, 92.0)
		self.assertEqual(bp.MEDIUM_RATE, 77.0)

	def test_price_list_and_purpose_are_the_names_the_resolver_imports(self):
		# services/invoice_creation and api/pos both import these; a rename here that
		# is not mirrored there silently sends B2B orders back to retail pricing.
		self.assertEqual(bp.PRICE_LIST, "B2B Selling")
		self.assertEqual(bp.B2B_SUPPLY_PURPOSE, "B2B Supply")


if __name__ == "__main__":
	unittest.main()

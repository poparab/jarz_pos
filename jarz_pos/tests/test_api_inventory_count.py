"""Tests for inventory count API endpoints."""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


if "frappe" not in sys.modules:
	fake_frappe = types.ModuleType("frappe")
	fake_utils = types.ModuleType("frappe.utils")

	class FakePermissionError(Exception):
		pass

	def fake_whitelist(*args, **kwargs):
		def decorator(func):
			return func

		if args and callable(args[0]) and len(args) == 1 and not kwargs:
			return args[0]
		return decorator

	def fake_throw(message, exc=Exception):
		raise exc(message)

	fake_frappe._ = lambda message: message
	fake_frappe.PermissionError = FakePermissionError
	fake_frappe.whitelist = fake_whitelist
	fake_frappe.throw = fake_throw
	fake_frappe.defaults = SimpleNamespace(get_user_default=lambda *args, **kwargs: None)
	fake_frappe.db = SimpleNamespace(
		exists=lambda *args, **kwargs: None,
		get_single_value=lambda *args, **kwargs: None,
		get_value=lambda *args, **kwargs: None,
		sql=lambda *args, **kwargs: [],
	)
	fake_frappe.get_all = lambda *args, **kwargs: []
	fake_frappe.get_cached_doc = lambda *args, **kwargs: SimpleNamespace()
	fake_frappe.get_roles = lambda *args, **kwargs: []
	fake_utils.cint = lambda value: int(value or 0)
	fake_utils.getdate = lambda value: value
	fake_utils.strip_html = lambda value: str(value)

	sys.modules["frappe"] = fake_frappe
	sys.modules["frappe.utils"] = fake_utils

from jarz_pos.api import inventory_count


class TestInventoryCountAPI(unittest.TestCase):
	"""Focused tests for inventory count configuration and item resolution."""

	def test_normalize_count_lines_adds_multiple_uoms_for_one_item(self):
		"""Four 2.7 Kg boxes plus 1.5 Kg is one 12.3 Kg count row."""
		def fake_get_all(doctype, **kwargs):
			self.assertEqual("UOM Conversion Detail", doctype)
			self.assertEqual("Box", kwargs["filters"]["uom"])
			return [{"conversion_factor": 2.7}]

		lines = [
			{
				"item_code": "BLUEBERRY",
				"counted_qty": 4,
				"uom": "Box",
				"valuation_rate": 80,
			},
			{"item_code": "BLUEBERRY", "counted_qty": 1.5, "uom": "Kg"},
		]
		with patch.object(
			inventory_count.frappe.db, "get_value", return_value="Kg"
		), patch.object(inventory_count.frappe, "get_all", side_effect=fake_get_all):
			counted, valuation_rates, batches, serials = inventory_count._normalize_count_lines(lines)

		self.assertEqual({"BLUEBERRY": 12.3}, counted)
		self.assertEqual({"BLUEBERRY": 80.0}, valuation_rates)
		self.assertEqual({"BLUEBERRY": None}, batches)
		self.assertEqual({"BLUEBERRY": None}, serials)

	def test_submit_reconciliation_builds_one_row_for_multiple_uom_components(self):
		class FakeReconciliation:
			def __init__(self):
				self.company = None
				self.flags = SimpleNamespace(ignore_permissions=False)
				self.name = "MAT-RECO-1"
				self.items = []

			def append(self, field, row):
				self.assert_field(field)
				self.items.append(row)

			def assert_field(self, field):
				if field != "items":
					raise AssertionError(field)

			def insert(self):
				return None

			def submit(self):
				return None

		doc = FakeReconciliation()

		def fake_get_value(doctype, *args, **kwargs):
			if doctype == "Item" and len(args) >= 2 and args[1] == "stock_uom":
				return "Kg"
			if doctype == "Warehouse":
				return None
			if doctype == "Item":
				return 0
			return None

		def fake_get_all(doctype, **kwargs):
			if doctype == "UOM Conversion Detail":
				return [{"conversion_factor": 2.7}]
			if doctype == "Company":
				return []
			raise AssertionError((doctype, kwargs))

		lines = [
			{"item_code": "BLUEBERRY", "counted_qty": 4, "uom": "Box", "valuation_rate": 80},
			{"item_code": "BLUEBERRY", "counted_qty": 1.5, "uom": "Kg", "valuation_rate": 80},
		]
		with patch.object(inventory_count, "_ensure_manager_access"), patch.object(
			inventory_count, "_get_bin_qty_map", return_value={"BLUEBERRY": 0}
		), patch.object(
			inventory_count.frappe.db, "get_value", side_effect=fake_get_value
		), patch.object(
			inventory_count.frappe.db, "get_single_value", return_value=0
		), patch.object(
			inventory_count.frappe.db, "commit", create=True
		), patch.object(
			inventory_count.frappe, "get_all", side_effect=fake_get_all
		), patch.object(
			inventory_count.frappe, "new_doc", return_value=doc, create=True
		):
			result = inventory_count.submit_reconciliation(
				warehouse="Raw Material - J",
				posting_date="2026-09-06",
				lines=lines,
				enforce_all=0,
			)

		self.assertTrue(result["ok"])
		self.assertEqual(1, result["differences"])
		self.assertEqual(1, len(doc.items))
		self.assertAlmostEqual(12.3, doc.items[0]["qty"])

	def _submit_one_increase(self, item_code, *, item_fields, get_all):
		"""Run submit_reconciliation for a single counted item and return the row.

		The caller supplies what the site knows about the item, so a test can
		state precisely which valuation source is meant to be the only one
		available.
		"""

		class FakeReconciliation:
			def __init__(self):
				self.company = None
				self.flags = SimpleNamespace(ignore_permissions=False)
				self.name = "MAT-RECO-1"
				self.items = []

			def append(self, field, row):
				if field != "items":
					raise AssertionError(field)
				self.items.append(row)

			def insert(self):
				return None

			def submit(self):
				return None

		doc = FakeReconciliation()

		def fake_get_value(doctype, *args, **kwargs):
			if doctype == "Item" and len(args) >= 2:
				return item_fields.get(args[1])
			return None

		with patch.object(inventory_count, "_ensure_manager_access"), patch.object(
			inventory_count, "_get_bin_qty_map", return_value={item_code: 0}
		), patch.object(
			inventory_count.frappe.db, "get_value", side_effect=fake_get_value
		), patch.object(
			inventory_count.frappe.db, "get_single_value", return_value=0
		), patch.object(
			inventory_count.frappe.db, "commit", create=True
		), patch.object(
			inventory_count.frappe, "get_all", side_effect=get_all
		), patch.object(
			inventory_count.frappe, "new_doc", return_value=doc, create=True
		):
			result = inventory_count.submit_reconciliation(
				warehouse="Raw Material - J",
				posting_date="2026-09-12",
				lines=[{"item_code": item_code, "counted_qty": 0.465}],
				enforce_all=0,
			)

		self.assertTrue(result["ok"])
		self.assertEqual(1, len(doc.items))
		return doc.items[0]

	def test_first_count_of_a_subassembly_prices_from_the_item_valuation_rate(self):
		"""A never-produced sub-assembly is countable on its seeded rate.

		`raspberry mix` was created by the fruit-mix migration with no ledger,
		no bin and no Item Price -- it is manufactured, so it is never bought
		and never sold. Its recipe cost was seeded onto Item.valuation_rate,
		which the resolver used to ignore, and the first floor count of it was
		refused outright.
		"""

		def fake_get_all(doctype, **kwargs):
			if doctype == "UOM Conversion Detail":
				return []
			if doctype in ("Stock Ledger Entry", "Item Price", "Company", "BOM"):
				return []
			raise AssertionError((doctype, kwargs))

		row = self._submit_one_increase(
			"raspberry mix",
			item_fields={
				"stock_uom": "Kg",
				"last_purchase_rate": 0.0,
				"valuation_rate": 265.0,
				"has_batch_no": 0,
				"has_serial_no": 0,
			},
			get_all=fake_get_all,
		)

		self.assertAlmostEqual(265.0, row["valuation_rate"])

	def test_zero_last_purchase_rate_does_not_shadow_the_item_price(self):
		"""last_purchase_rate is 0.0, not NULL, on anything never bought.

		Returning it merely because it "is not None" made every later source
		unreachable, so an item priced only by an Item Price was refused too.
		"""

		def fake_get_all(doctype, **kwargs):
			if doctype == "UOM Conversion Detail":
				return []
			if doctype == "Item Price" and kwargs.get("filters", {}).get("buying"):
				return [{"price_list_rate": 42.5}]
			if doctype in ("Stock Ledger Entry", "Item Price", "Company", "BOM"):
				return []
			raise AssertionError((doctype, kwargs))

		row = self._submit_one_increase(
			"NEVER-BOUGHT",
			item_fields={
				"stock_uom": "Kg",
				"last_purchase_rate": 0.0,
				"valuation_rate": 0.0,
				"has_batch_no": 0,
				"has_serial_no": 0,
			},
			get_all=fake_get_all,
		)

		self.assertAlmostEqual(42.5, row["valuation_rate"])

	def test_a_manufactured_item_falls_back_to_its_recipe_cost(self):
		"""Last resort before refusing: the active BOM's unit cost."""

		def fake_get_all(doctype, **kwargs):
			if doctype == "UOM Conversion Detail":
				return []
			if doctype == "BOM":
				return [{"total_cost": 530.0, "quantity": 2.0}]
			if doctype in ("Stock Ledger Entry", "Item Price", "Company"):
				return []
			raise AssertionError((doctype, kwargs))

		row = self._submit_one_increase(
			"MIX-NO-SEEDED-RATE",
			item_fields={
				"stock_uom": "Kg",
				"last_purchase_rate": 0.0,
				"valuation_rate": 0.0,
				"has_batch_no": 0,
				"has_serial_no": 0,
			},
			get_all=fake_get_all,
		)

		self.assertAlmostEqual(265.0, row["valuation_rate"])

	def test_to_stock_qty_rejects_an_unconfigured_uom(self):
		with patch.object(
			inventory_count.frappe.db, "get_value", return_value="Kg"
		), patch.object(inventory_count.frappe, "get_all", return_value=[]):
			with self.assertRaisesRegex(Exception, "UOM Carton is not configured for Item BLUEBERRY"):
				inventory_count._to_stock_qty("BLUEBERRY", 2, "Carton")

	def test_to_stock_qty_rejects_non_positive_or_non_finite_conversion(self):
		for factor in (0, -2, float("nan"), float("inf")):
			with self.subTest(factor=factor), patch.object(
				inventory_count.frappe.db, "get_value", return_value="Kg"
			), patch.object(
				inventory_count.frappe,
				"get_all",
				return_value=[{"conversion_factor": factor}],
			):
				with self.assertRaisesRegex(Exception, "invalid conversion factor"):
					inventory_count._to_stock_qty("BLUEBERRY", 2, "Box")

	def test_to_stock_qty_rejects_negative_or_non_finite_count(self):
		for quantity in (-1, float("nan"), float("inf"), "not-a-number"):
			with self.subTest(quantity=quantity), patch.object(
				inventory_count.frappe.db, "get_value", return_value="Kg"
			):
				with self.assertRaisesRegex(Exception, "must be"):
					inventory_count._to_stock_qty("BLUEBERRY", quantity, "Kg")

	def test_normalize_count_lines_rejects_conflicting_single_row_metadata(self):
		base = {"item_code": "BLUEBERRY", "counted_qty": 1, "uom": "Kg"}
		conflicts = (
			("valuation_rate", 80, 90, "conflicting valuation rates"),
			("batch_no", "BATCH-1", "BATCH-2", "conflicting batch numbers"),
			("serial_no", "SERIAL-1", "SERIAL-2", "conflicting serial numbers"),
		)
		for field, first, second, error in conflicts:
			with self.subTest(field=field), patch.object(
				inventory_count.frappe.db, "get_value", return_value="Kg"
			):
				lines = [dict(base, **{field: first}), dict(base, **{field: second})]
				with self.assertRaisesRegex(Exception, error):
					inventory_count._normalize_count_lines(lines)

	def test_list_items_for_count_requires_warehouse(self):
		with patch.object(inventory_count, "_ensure_manager_access"):
			with self.assertRaises(Exception):
				inventory_count.list_items_for_count(warehouse="")

	def test_alternative_count_groups_dedupe_overlapping_two_way_links(self):
		rows = [
			{"item_code": "ALDIA", "item_name": "Aldia", "stock_uom": "Kg", "current_qty": 3.547},
			{"item_code": "PURATOS", "item_name": "Puratos", "stock_uom": "Kg", "current_qty": 25},
			{"item_code": "FUTURE", "item_name": "Future", "stock_uom": "Kg", "current_qty": 2},
		]
		links = [
			{"item_code": "ALDIA", "alternative_item_code": "PURATOS"},
			{"item_code": "PURATOS", "alternative_item_code": "FUTURE"},
		]
		with patch.object(
			inventory_count.frappe, "get_all", side_effect=[links, links]
		):
			inventory_count._attach_alternative_count_groups(rows)

		self.assertEqual({"ALDIA|FUTURE|PURATOS"}, {row["alternative_group_key"] for row in rows})
		self.assertEqual([30.547] * 3, [row["combined_net_current_qty"] for row in rows])
		self.assertTrue(all(len(row["linked_items"]) == 3 for row in rows))

	def test_list_items_for_count_applies_resolved_profile_items(self):
		expected_uoms = [{"uom": "Nos", "conversion_factor": 1.0}]

		def fake_get_all(doctype, **kwargs):
			self.assertEqual(doctype, "Item")
			self.assertEqual(
				kwargs["filters"]["name"],
				["in", ["ITEM-1", "ITEM-2"]],
			)
			return [
				{
					"item_code": "ITEM-1",
					"item_name": "Item 1",
					"item_group": "Finished Goods",
					"stock_uom": "Nos",
					"has_batch_no": 0,
					"has_serial_no": 0,
				},
			]

		with patch.object(inventory_count, "_ensure_manager_access"), patch.object(
			inventory_count,
			"_resolve_count_item_codes",
			return_value=["ITEM-1", "ITEM-2"],
		), patch.object(inventory_count.frappe, "get_all", side_effect=fake_get_all), patch.object(
			inventory_count,
			"_get_bin_qty_map",
			return_value={"ITEM-1": 7.0},
		), patch.object(
			inventory_count,
			"_get_uom_conversions",
			return_value=expected_uoms,
		), patch.object(
			inventory_count,
			"_resolve_item_valuation",
			return_value=12.5,
		):
			result = inventory_count.list_items_for_count(warehouse="Main Warehouse")

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["item_code"], "ITEM-1")
		self.assertEqual(result[0]["current_qty"], 7.0)
		self.assertEqual(result[0]["uoms"], expected_uoms)
		self.assertEqual(result[0]["valuation_rate"], 12.5)

	def test_resolve_count_item_codes_combines_groups_and_exceptions(self):
		profile = SimpleNamespace(
			include_child_groups=1,
			item_groups=[SimpleNamespace(item_group="Finished Goods", enabled=1)],
			item_exceptions=[
				SimpleNamespace(item_code="SPECIAL-ITEM", action="Include", enabled=1),
				SimpleNamespace(item_code="OLD-ITEM", action="Exclude", enabled=1),
			],
		)

		def fake_get_all(doctype, **kwargs):
			self.assertEqual(doctype, "Item")
			filters = kwargs["filters"]
			if filters.get("item_group") == ["in", ["Finished Goods", "Seasonal"]]:
				return ["ITEM-1", "OLD-ITEM"]
			if filters.get("name") == ["in", ["SPECIAL-ITEM"]]:
				return ["SPECIAL-ITEM"]
			self.fail(f"Unexpected get_all call: {doctype} {kwargs}")

		with patch.object(
			inventory_count,
			"_get_active_warehouse_count_profile_name",
			return_value="Main Warehouse",
		), patch.object(
			inventory_count.frappe,
			"get_cached_doc",
			return_value=profile,
		), patch.object(
			inventory_count,
			"_expand_item_groups",
			return_value=["Finished Goods", "Seasonal"],
		), patch.object(inventory_count.frappe, "get_all", side_effect=fake_get_all):
			result = inventory_count._resolve_count_item_codes("Main Warehouse")

		self.assertEqual(result, ["ITEM-1", "SPECIAL-ITEM"])

	def test_resolve_count_item_codes_returns_none_without_profile(self):
		with patch.object(
			inventory_count,
			"_get_active_warehouse_count_profile_name",
			return_value=None,
		):
			self.assertIsNone(inventory_count._resolve_count_item_codes("Main Warehouse"))

	def test_resolve_count_item_codes_returns_empty_list_for_empty_profile(self):
		profile = SimpleNamespace(include_child_groups=0, item_groups=[], item_exceptions=[])
		with patch.object(
			inventory_count,
			"_get_active_warehouse_count_profile_name",
			return_value="Main Warehouse",
		), patch.object(
			inventory_count.frappe,
			"get_cached_doc",
			return_value=profile,
		):
			self.assertEqual(inventory_count._resolve_count_item_codes("Main Warehouse"), [])

	def test_submit_reconciliation_validation(self):
		with patch.object(inventory_count, "_ensure_manager_access"):
			with self.assertRaises(Exception):
				inventory_count.submit_reconciliation(
					warehouse="",
					posting_date=None,
					lines=[],
				)

	def test_list_items_for_count_filters_to_stock_items(self):
		"""The missing filter. Without it a warehouse with no count profile
		listed every enabled item on the site -- 190 rows on production, 83 of
		them non-stock rows a Stock Reconciliation rejects outright."""
		captured = {}

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				captured["filters"] = kwargs.get("filters")
				captured["order_by"] = kwargs.get("order_by")
			return []

		with patch.object(inventory_count, "_ensure_manager_access"), patch.object(
			inventory_count, "_resolve_count_item_codes", return_value=None
		), patch.object(inventory_count.frappe, "get_all", fake_get_all):
			inventory_count.list_items_for_count(warehouse="Raw Material - J")

		self.assertEqual(1, captured["filters"]["is_stock_item"])
		self.assertEqual(0, captured["filters"]["disabled"])
		# Alphabetical, because the limit means the sort decides which rows survive.
		self.assertEqual("item_name asc", captured["order_by"])

	def test_list_items_for_count_expands_a_group_to_its_descendants(self):
		"""Picking "Materials" must count Raw Material, Packaging, Labels and
		Sub Assemblies -- a group node holds no items of its own, so an exact
		match on it returned nothing."""
		captured = {}

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				captured["filters"] = kwargs.get("filters")
			return []

		with patch.object(inventory_count, "_ensure_manager_access"), patch.object(
			inventory_count, "_resolve_count_item_codes", return_value=None
		), patch.object(
			inventory_count,
			"_expand_item_groups",
			return_value=["Materials", "Raw Material", "Packaging", "Labels"],
		), patch.object(inventory_count.frappe, "get_all", fake_get_all):
			inventory_count.list_items_for_count(
				warehouse="Raw Material - J", item_group="Materials"
			)

		self.assertEqual(
			["in", ["Materials", "Raw Material", "Packaging", "Labels"]],
			captured["filters"]["item_group"],
		)

	def test_list_item_groups_drops_groups_with_nothing_countable(self):
		"""Bundles, Assets, Services and Label Printing hold only non-stock rows.
		Offering them as count categories only ever produced an empty sheet."""
		tree = [
			{"name": "Materials", "item_group_name": "Materials", "is_group": 1, "lft": 1, "rgt": 6},
			{"name": "Raw Material", "item_group_name": "Raw Material", "is_group": 0, "lft": 2, "rgt": 3},
			{"name": "Labels", "item_group_name": "Labels", "is_group": 0, "lft": 4, "rgt": 5},
			{"name": "Bundles", "item_group_name": "Bundles", "is_group": 0, "lft": 7, "rgt": 8},
		]

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				# Only Raw Material and Labels hold a countable item.
				return ["Raw Material", "Labels"]
			if doctype == "Item Group":
				return tree
			return []

		with patch.object(inventory_count, "_ensure_manager_access"), patch.object(
			inventory_count.frappe, "get_all", fake_get_all
		), patch.object(
			inventory_count.frappe,
			"db",
			SimpleNamespace(count=lambda *a, **k: 3),
		):
			result = inventory_count.list_item_groups()

		names = [row["name"] for row in result]
		# The parent survives because its subtree is populated; Bundles does not.
		self.assertEqual(["Materials", "Raw Material", "Labels"], names)
		self.assertEqual(1, result[0]["is_group"])

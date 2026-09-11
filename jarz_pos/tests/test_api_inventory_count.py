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
	fake_frappe.get_traceback = lambda *args, **kwargs: ''
	fake_frappe.log_error = lambda *args, **kwargs: None

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

	def _resolve(self, *, item_fields, get_all, warehouse="Raw Material - J"):
		"""Run the shared valuation chain with a stated view of the site."""

		def fake_get_value(doctype, *args, **kwargs):
			if doctype == "Item" and len(args) >= 2:
				return item_fields.get(args[1])
			return None

		with patch.object(
			inventory_count.frappe, "get_all", side_effect=get_all
		), patch.object(
			inventory_count.frappe.db, "get_value", side_effect=fake_get_value
		):
			return inventory_count._resolve_item_valuation("ITEM", warehouse)

	@staticmethod
	def _nothing_anywhere(doctype, **kwargs):
		if doctype in ("Stock Ledger Entry", "Item Price", "BOM"):
			return []
		raise AssertionError((doctype, kwargs))

	def _submit_one_increase(self, item_code, *, item_fields, get_all):
		"""Run submit_reconciliation for a single counted item and return the row."""

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

	def _sheet_only(self, doctype, **kwargs):
		if doctype == "UOM Conversion Detail":
			return []
		return self._nothing_anywhere(doctype, **kwargs)

	def test_first_count_of_a_subassembly_prices_from_the_item_valuation_rate(self):
		"""A never-produced sub-assembly is countable on its seeded rate.

		`raspberry mix` was created by the fruit-mix migration with no ledger,
		no bin and no Item Price -- it is manufactured, so it is never bought
		and never sold. Its recipe cost was seeded onto Item.valuation_rate,
		which the resolver used to ignore, and the first floor count of it was
		refused outright.
		"""
		row = self._submit_one_increase(
			"raspberry mix",
			item_fields={
				"stock_uom": "Kg",
				"last_purchase_rate": 0.0,
				"valuation_rate": 265.0,
				"has_batch_no": 0,
				"has_serial_no": 0,
			},
			get_all=self._sheet_only,
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
			return self._nothing_anywhere(doctype, **kwargs)

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

	def test_bom_fallback_uses_company_currency_and_ignores_draft_recipes(self):
		"""The recipe is the last cost source, and only a submitted one counts.

		`is_active` defaults to 1 on a BOM, so a draft recipe -- which already
		carries a `total_cost` computed during validate -- would otherwise
		price a stock increase. `base_total_cost` is the company-currency
		figure; `total_cost` is in the BOM's own currency.
		"""
		seen = {}

		def fake_get_all(doctype, **kwargs):
			if doctype == "BOM":
				seen.update(kwargs)
				return [{"base_total_cost": 530.0, "total_cost": 999.0, "quantity": 2.0}]
			return self._nothing_anywhere(doctype, **kwargs)

		rate = self._resolve(
			item_fields={"last_purchase_rate": 0.0, "valuation_rate": 0.0},
			get_all=fake_get_all,
		)

		self.assertAlmostEqual(265.0, rate)
		self.assertEqual(1, seen["filters"]["docstatus"])
		self.assertEqual(1, seen["filters"]["is_active"])
		self.assertIn("is_default desc", seen["order_by"])
		self.assertIn("base_total_cost", seen["fields"])

	def test_cost_sources_outrank_the_selling_price(self):
		"""A Stock Reconciliation reprices the WHOLE balance, not the delta.

		Valuing an increase off a retail price therefore restates every unit at
		margin and books the difference to stock adjustment. Every cost-based
		source must win first; the selling price is only a last resort so that
		a count is never refused outright.
		"""

		def only_selling(doctype, **kwargs):
			if doctype == "Item Price" and kwargs.get("filters", {}).get("selling"):
				return [{"price_list_rate": 900.0}]
			return self._nothing_anywhere(doctype, **kwargs)

		def selling_and_buying(doctype, **kwargs):
			if doctype == "Item Price" and kwargs.get("filters", {}).get("buying"):
				return [{"price_list_rate": 400.0}]
			return only_selling(doctype, **kwargs)

		def selling_and_bom(doctype, **kwargs):
			if doctype == "BOM":
				return [{"base_total_cost": 530.0, "quantity": 2.0}]
			return only_selling(doctype, **kwargs)

		no_cost = {"last_purchase_rate": 0.0, "valuation_rate": 0.0}

		# The item's own valuation rate beats any price list, as in ERPNext.
		self.assertAlmostEqual(
			265.0,
			self._resolve(
				item_fields={"last_purchase_rate": 0.0, "valuation_rate": 265.0},
				get_all=selling_and_buying,
			),
		)
		# Buying beats selling.
		self.assertAlmostEqual(
			400.0, self._resolve(item_fields=no_cost, get_all=selling_and_buying)
		)
		# The recipe beats selling.
		self.assertAlmostEqual(
			265.0, self._resolve(item_fields=no_cost, get_all=selling_and_bom)
		)
		# With nothing else at all, selling still keeps the count possible.
		self.assertAlmostEqual(
			900.0, self._resolve(item_fields=no_cost, get_all=only_selling)
		)

	def test_the_ledger_probe_ignores_cancelled_entries(self):
		"""A reconciliation posted at a bad rate and then cancelled must not
		price the next count. ERPNext's own probe filters `is_cancelled = 0`."""
		seen = []

		def fake_get_all(doctype, **kwargs):
			if doctype == "Stock Ledger Entry":
				seen.append(kwargs["filters"])
				return []
			return self._nothing_anywhere(doctype, **kwargs)

		self._resolve(
			item_fields={"last_purchase_rate": 0.0, "valuation_rate": 265.0},
			get_all=fake_get_all,
		)

		self.assertEqual(2, len(seen))
		for filters in seen:
			self.assertEqual(0, filters["is_cancelled"])
		self.assertEqual("Raw Material - J", seen[0]["warehouse"])
		self.assertNotIn("warehouse", seen[1])

	def test_a_failing_source_does_not_forfeit_the_ones_below_it(self):
		"""An infrastructure failure must not read as "this item has no rate".

		The chain briefly sat under one broad `except`, so a raise in the first
		source discarded every later fallback and the operator was told five
		sources had been checked when none had.
		"""
		logged = []

		def exploding_ledger(doctype, **kwargs):
			if doctype == "Stock Ledger Entry":
				raise RuntimeError("database is down")
			return self._nothing_anywhere(doctype, **kwargs)

		with patch.object(
			inventory_count,
			"_log_valuation_source_failure",
			side_effect=lambda item, label: logged.append(label),
		):
			rate = self._resolve(
				item_fields={"last_purchase_rate": 0.0, "valuation_rate": 265.0},
				get_all=exploding_ledger,
			)

		self.assertAlmostEqual(265.0, rate)
		self.assertEqual(2, len(logged))

	def test_an_item_with_no_cost_anywhere_is_still_refused(self):
		"""The refusal is the behaviour the operator actually sees."""
		with self.assertRaises(Exception) as caught:
			self._submit_one_increase(
				"NOTHING-KNOWN",
				item_fields={
					"stock_uom": "Kg",
					"last_purchase_rate": 0.0,
					"valuation_rate": 0.0,
					"item_name": "Nothing Known",
					"has_batch_no": 0,
					"has_serial_no": 0,
				},
				get_all=self._sheet_only,
			)

		message = str(caught.exception)
		self.assertIn("NOTHING-KNOWN", message)
		self.assertIn("Raw Material - J", message)
		# It must not advise a Stock Settings field that does not exist.
		self.assertNotIn("Allow Zero Valuation Rate", message)

	def test_a_failing_error_log_does_not_replace_the_refusal(self):
		"""Logging must never become the failure it was meant to explain.

		`frappe.log_error` can raise on its own -- `capture_exception` reads
		System Settings outside its own try -- and the handler around the
		submit called it unguarded. On a real site that swapped the operator's
		refusal for "DocType Error Log not found", which is how this was found:
		the mock suite passed and CI did not.
		"""

		def exploding_log_error(*args, **kwargs):
			raise Exception("DocType Error Log not found")

		with patch.object(
			inventory_count.frappe, "log_error", side_effect=exploding_log_error
		):
			with self.assertRaises(Exception) as caught:
				self._submit_one_increase(
					"NOTHING-KNOWN",
					item_fields={
						"stock_uom": "Kg",
						"last_purchase_rate": 0.0,
						"valuation_rate": 0.0,
						"item_name": "Nothing Known",
						"has_batch_no": 0,
						"has_serial_no": 0,
					},
					get_all=self._sheet_only,
				)

		message = str(caught.exception)
		self.assertIn("NOTHING-KNOWN", message)
		self.assertNotIn("Error Log", message)

	def test_count_sheet_prices_an_item_that_only_has_a_seeded_rate(self):
		"""The sheet and the submit must agree on what an item is worth.

		The sheet used its own copy of the valuation chain, carrying the same
		two defects, so it reported 0.0 for `raspberry mix`. The app drops a
		non-positive rate rather than sending it back, which is why the submit
		had to resolve the item from scratch -- and failed too.
		"""
		rate = self._resolve(
			item_fields={"last_purchase_rate": 0.0, "valuation_rate": 265.0},
			get_all=self._nothing_anywhere,
		)

		self.assertAlmostEqual(265.0, rate)

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

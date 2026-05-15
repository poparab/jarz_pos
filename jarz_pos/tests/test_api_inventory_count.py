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
	fake_utils.strip_html = lambda value: str(value)

	sys.modules["frappe"] = fake_frappe
	sys.modules["frappe.utils"] = fake_utils

from jarz_pos.api import inventory_count


class TestInventoryCountAPI(unittest.TestCase):
	"""Focused tests for inventory count configuration and item resolution."""

	def test_list_items_for_count_requires_warehouse(self):
		with patch.object(inventory_count, "_ensure_manager_access"):
			with self.assertRaises(Exception):
				inventory_count.list_items_for_count(warehouse="")

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

"""Tests for purchase warehouse resolution.

The bug these guard: a Purchase Invoice with ``update_stock=1`` and no warehouse
lets ERPNext pick the destination, so goods received at one branch could raise
another branch's stock. Resolution must be deterministic and must never return
blank.
"""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


if "frappe" not in sys.modules:
	fake_frappe = types.ModuleType("frappe")

	class FakePermissionError(Exception):
		pass

	def fake_whitelist(*args, **kwargs):
		def decorator(func):
			return func

		if args and callable(args[0]) and len(args) == 1 and not kwargs:
			return args[0]
		return decorator

	def fake_throw(message, exc=Exception, **kwargs):
		raise exc(message)

	fake_frappe._ = lambda message: message
	fake_frappe.PermissionError = FakePermissionError
	fake_frappe.whitelist = fake_whitelist
	fake_frappe.throw = fake_throw
	fake_frappe.defaults = SimpleNamespace(get_user_default=lambda *a, **k: None)
	fake_frappe.db = SimpleNamespace(
		exists=lambda *a, **k: None,
		get_single_value=lambda *a, **k: None,
		get_value=lambda *a, **k: None,
		sql=lambda *a, **k: [],
	)
	fake_frappe.get_all = lambda *a, **k: []
	fake_frappe.get_roles = lambda *a, **k: []
	fake_frappe.log_error = lambda *a, **k: None
	fake_frappe.get_traceback = lambda *a, **k: ""

	sys.modules["frappe"] = fake_frappe

from jarz_pos.utils import warehouse_utils


COMPANY = "Jarz"
RAW_STORE = "Raw Materials - J"
CONSUMABLE_STORE = "Consumables - J"
ITEM_STORE = "Stores - Dokki"
FALLBACK_STORE = "All Warehouses - J"

#: Every warehouse below is a usable leaf belonging to COMPANY.
USABLE = {
	RAW_STORE: {"company": COMPANY, "is_group": 0, "disabled": 0},
	CONSUMABLE_STORE: {"company": COMPANY, "is_group": 0, "disabled": 0},
	ITEM_STORE: {"company": COMPANY, "is_group": 0, "disabled": 0},
	FALLBACK_STORE: {"company": COMPANY, "is_group": 0, "disabled": 0},
	"Group Node - J": {"company": COMPANY, "is_group": 1, "disabled": 0},
	"Retired - J": {"company": COMPANY, "is_group": 0, "disabled": 1},
	"Other Co Store - X": {"company": "Other Co", "is_group": 0, "disabled": 0},
}

#: item_code -> item_group
ITEM_GROUPS = {
	"RM-TOMATO": "Vegetables",
	"CN-NYLON": "Consumable",
	"MISC-THING": "Services",
}

#: item_group -> parent, forming: Vegetables -> Raw Material -> All Item Groups
GROUP_PARENTS = {
	"Vegetables": "Raw Material",
	"Raw Material": "All Item Groups",
	"Consumable": "All Item Groups",
	"Services": "All Item Groups",
	"All Item Groups": None,
}


def _fake_get_value(doctype, name, fieldname=None, as_dict=False, **kwargs):
	if doctype == "Warehouse":
		row = USABLE.get(name if isinstance(name, str) else "")
		if not row:
			return None
		if as_dict:
			return SimpleNamespace(**row) if not isinstance(row, dict) else dict(row)
		if fieldname == "company":
			return row["company"]
		return None
	if doctype == "Item" and fieldname == "item_group":
		return ITEM_GROUPS.get(name)
	if doctype == "Item Group":
		if fieldname == "parent_item_group":
			return GROUP_PARENTS.get(name)
		if as_dict:
			# No nested-set bounds -> forces the parent-walk fallback path.
			return {"lft": None, "rgt": None}
	if doctype == "Item Default":
		return None
	return None


class _Settings:
	def __init__(self, routes=None, default_warehouse=None):
		self.purchase_warehouse_routes = routes or []
		self.default_purchase_warehouse = default_warehouse


def _route(item_group, warehouse):
	return SimpleNamespace(item_group=item_group, warehouse=warehouse)


class TestResolvePurchaseWarehouse(unittest.TestCase):
	def setUp(self):
		self.get_value = patch.object(
			warehouse_utils.frappe.db, "get_value", side_effect=_fake_get_value
		)
		self.get_value.start()
		self.addCleanup(self.get_value.stop)

	def _resolve(self, item_code, settings=None, explicit=None):
		with patch.object(warehouse_utils, "_settings", return_value=settings):
			return warehouse_utils.resolve_purchase_warehouse(item_code, COMPANY, explicit)

	# -- explicit ---------------------------------------------------------

	def test_explicit_warehouse_wins(self):
		result = self._resolve("RM-TOMATO", _Settings(default_warehouse=RAW_STORE), explicit=ITEM_STORE)
		self.assertEqual(result, ITEM_STORE)

	def test_explicit_group_warehouse_is_rejected(self):
		"""Stock cannot post into a group node, so accepting one would fail later
		with a far less obvious error."""
		with self.assertRaises(Exception):
			self._resolve("RM-TOMATO", _Settings(), explicit="Group Node - J")

	def test_explicit_disabled_warehouse_is_rejected(self):
		with self.assertRaises(Exception):
			self._resolve("RM-TOMATO", _Settings(), explicit="Retired - J")

	def test_explicit_other_company_warehouse_is_rejected(self):
		with self.assertRaises(Exception):
			self._resolve("RM-TOMATO", _Settings(), explicit="Other Co Store - X")

	# -- item default -----------------------------------------------------

	def test_item_default_beats_group_route(self):
		def with_item_default(doctype, name, fieldname=None, as_dict=False, **kwargs):
			if doctype == "Item Default":
				return ITEM_STORE
			return _fake_get_value(doctype, name, fieldname, as_dict, **kwargs)

		settings = _Settings(routes=[_route("Raw Material", RAW_STORE)])
		with patch.object(warehouse_utils.frappe.db, "get_value", side_effect=with_item_default):
			with patch.object(warehouse_utils, "_settings", return_value=settings):
				result = warehouse_utils.resolve_purchase_warehouse("RM-TOMATO", COMPANY)
		self.assertEqual(result, ITEM_STORE)

	# -- item group routing ----------------------------------------------

	def test_route_matches_via_parent_group(self):
		"""A route on 'Raw Material' must also cover 'Vegetables' nested under it —
		otherwise every leaf group needs its own row."""
		settings = _Settings(routes=[_route("Raw Material", RAW_STORE)])
		self.assertEqual(self._resolve("RM-TOMATO", settings), RAW_STORE)

	def test_route_on_child_group_overrides_parent(self):
		settings = _Settings(
			routes=[_route("Raw Material", RAW_STORE), _route("Vegetables", ITEM_STORE)]
		)
		self.assertEqual(self._resolve("RM-TOMATO", settings), ITEM_STORE)

	def test_consumable_routes_separately_from_raw_material(self):
		settings = _Settings(
			routes=[_route("Raw Material", RAW_STORE), _route("Consumable", CONSUMABLE_STORE)],
			default_warehouse=RAW_STORE,
		)
		self.assertEqual(self._resolve("RM-TOMATO", settings), RAW_STORE)
		self.assertEqual(self._resolve("CN-NYLON", settings), CONSUMABLE_STORE)

	def test_unrouted_group_falls_back_to_default(self):
		"""'Services' matches no route, so it lands on the configured default —
		which is the raw-materials store."""
		settings = _Settings(
			routes=[_route("Raw Material", RAW_STORE), _route("Consumable", CONSUMABLE_STORE)],
			default_warehouse=RAW_STORE,
		)
		self.assertEqual(self._resolve("MISC-THING", settings), RAW_STORE)

	def test_route_to_unusable_warehouse_is_skipped(self):
		settings = _Settings(
			routes=[_route("Raw Material", "Retired - J")], default_warehouse=RAW_STORE
		)
		self.assertEqual(self._resolve("RM-TOMATO", settings), RAW_STORE)

	# -- defaults and failure --------------------------------------------

	def test_misconfigured_default_raises_rather_than_guessing(self):
		settings = _Settings(default_warehouse="Other Co Store - X")
		with self.assertRaises(Exception):
			self._resolve("RM-TOMATO", settings)

	def test_no_configuration_falls_back_to_stock_settings(self):
		def with_stock_default(fieldname, *a, **k):
			return FALLBACK_STORE

		with patch.object(
			warehouse_utils.frappe.db, "get_single_value", side_effect=lambda dt, f=None: FALLBACK_STORE
		):
			result = self._resolve("RM-TOMATO", _Settings())
		self.assertEqual(result, FALLBACK_STORE)

	def test_never_returns_blank(self):
		"""The whole point: a blank warehouse must be an error, not a silent
		mis-post."""
		with patch.object(warehouse_utils.frappe, "get_all", return_value=[]):
			with patch.object(
				warehouse_utils.frappe.db, "get_single_value", side_effect=lambda dt, f=None: None
			):
				with self.assertRaises(Exception):
					self._resolve("RM-TOMATO", _Settings())

	def test_missing_company_raises(self):
		with patch.object(warehouse_utils, "_settings", return_value=_Settings()):
			with self.assertRaises(Exception):
				warehouse_utils.resolve_purchase_warehouse("RM-TOMATO", "")


if __name__ == "__main__":
	unittest.main()

"""Tests for the purchasing warehouse-routing seeder.

The seeder runs on every migrate against live sites, so the properties that
matter are: never overwrite an operator's choice, never throw, and never point
at a warehouse stock cannot post into.
"""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


# The stub is installed *additively*. A plain `if "frappe" not in sys.modules`
# guard makes these tests order-dependent: whichever sibling module loads first
# owns the fake, and this one then finds no `get_single` to patch. Filling in
# only what is missing keeps the suite green in any order.
_frappe = sys.modules.get("frappe")
if _frappe is None:
	_frappe = types.ModuleType("frappe")
	sys.modules["frappe"] = _frappe


def _fake_throw(message, exc=Exception, **kwargs):
	raise exc(message)


def _fake_whitelist(*args, **kwargs):
	def decorator(func):
		return func

	if args and callable(args[0]) and len(args) == 1 and not kwargs:
		return args[0]
	return decorator


class _FakePermissionError(Exception):
	pass


def _ensure(name, value):
	if not hasattr(_frappe, name):
		setattr(_frappe, name, value)


_ensure("_", lambda message: message)
_ensure("throw", _fake_throw)
# Needed because a sibling module may import jarz_pos.api.purchase *after* this
# file has installed the stub — module-level @frappe.whitelist() runs at import.
_ensure("whitelist", _fake_whitelist)
_ensure("PermissionError", _FakePermissionError)
_ensure("session", SimpleNamespace(user="test@jarz.test"))
_ensure("defaults", SimpleNamespace(get_user_default=lambda *a, **k: None))
_ensure(
	"db",
	SimpleNamespace(
		exists=lambda *a, **k: None,
		get_value=lambda *a, **k: None,
		get_single_value=lambda *a, **k: None,
	),
)
_ensure("get_single", lambda *a, **k: SimpleNamespace())
_ensure("get_all", lambda *a, **k: [])
_ensure("log_error", lambda *a, **k: None)
_ensure("get_traceback", lambda *a, **k: "")
_ensure(
	"logger",
	lambda *a, **k: SimpleNamespace(
		warning=lambda *args, **kwargs: None,
		info=lambda *args, **kwargs: None,
	),
)

# A sibling stub may have installed `db` without every attribute this module
# patches, so fill the gaps on the existing namespace too.
for _attr in ("exists", "get_value", "get_single_value"):
	if not hasattr(_frappe.db, _attr):
		setattr(_frappe.db, _attr, lambda *a, **k: None)

from jarz_pos.setup import purchase_setup


RAW = "Raw Material - J"
CONSUMABLES = "Consumables - J"

WAREHOUSES = {
	RAW: {"is_group": 0, "disabled": 0},
	CONSUMABLES: {"is_group": 0, "disabled": 0},
	"Group Node - J": {"is_group": 1, "disabled": 0},
	"Retired - J": {"is_group": 0, "disabled": 1},
}

ITEM_GROUPS = {"Raw Material", "Consumable"}


class _Settings:
	"""Stand-in for the Jarz POS Settings Single."""

	def __init__(self, default_warehouse=None, routes=None):
		self.default_purchase_warehouse = default_warehouse
		self.purchase_warehouse_routes = list(routes or [])
		self.flags = SimpleNamespace(ignore_permissions=False)
		self.saved = False

	def append(self, fieldname, row):
		getattr(self, fieldname).append(SimpleNamespace(**row))

	def save(self):
		self.saved = True


def _exists(doctype, name):
	if doctype == "Warehouse":
		return name in WAREHOUSES
	if doctype == "Item Group":
		return name in ITEM_GROUPS
	return False


def _get_value(doctype, name, fieldname=None, as_dict=False, **kwargs):
	if doctype == "Warehouse" and as_dict:
		row = WAREHOUSES.get(name)
		return dict(row) if row else None
	return None


class TestEnsurePurchaseSetup(unittest.TestCase):
	def setUp(self):
		patches = [
			patch.object(purchase_setup.frappe.db, "exists", side_effect=_exists),
			patch.object(purchase_setup.frappe.db, "get_value", side_effect=_get_value),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def _run(self, settings):
		with patch.object(purchase_setup.frappe, "get_single", return_value=settings):
			return purchase_setup.ensure_purchase_setup()

	def test_seeds_both_routes_and_the_default_on_a_fresh_site(self):
		settings = _Settings()
		log = self._run(settings)

		self.assertEqual(settings.default_purchase_warehouse, RAW)
		routes = {(r.item_group, r.warehouse) for r in settings.purchase_warehouse_routes}
		self.assertEqual(
			routes, {("Raw Material", RAW), ("Consumable", CONSUMABLES)}
		)
		self.assertTrue(settings.saved)
		self.assertEqual(log["skipped"], [])

	def test_never_overwrites_an_operator_choice(self):
		"""Someone who repointed the default in the UI keeps their setting."""
		settings = _Settings(default_warehouse=CONSUMABLES)
		self._run(settings)
		self.assertEqual(settings.default_purchase_warehouse, CONSUMABLES)

	def test_is_idempotent_across_repeated_migrates(self):
		settings = _Settings()
		self._run(settings)
		first = len(settings.purchase_warehouse_routes)

		settings.saved = False
		log = self._run(settings)

		self.assertEqual(len(settings.purchase_warehouse_routes), first)
		self.assertFalse(settings.saved, "second run must not write")
		self.assertEqual(log["set"], [])

	def test_existing_route_for_a_group_is_left_alone(self):
		settings = _Settings(
			routes=[SimpleNamespace(item_group="Raw Material", warehouse="Retired - J")]
		)
		self._run(settings)
		raw_routes = [
			r for r in settings.purchase_warehouse_routes if r.item_group == "Raw Material"
		]
		self.assertEqual(len(raw_routes), 1)
		self.assertEqual(raw_routes[0].warehouse, "Retired - J")

	def test_missing_warehouse_is_skipped_not_fatal(self):
		with patch.object(
			purchase_setup.frappe.db,
			"exists",
			side_effect=lambda dt, name: dt == "Item Group" and name in ITEM_GROUPS,
		):
			settings = _Settings()
			log = self._run(settings)

		self.assertIsNone(settings.default_purchase_warehouse)
		self.assertEqual(settings.purchase_warehouse_routes, [])
		self.assertTrue(log["skipped"])

	def test_group_and_disabled_warehouses_are_rejected(self):
		"""Stock cannot post into a group node or a disabled warehouse, so
		routing there would fail later with a far less obvious error."""
		for bad in ("Group Node - J", "Retired - J"):
			self.assertFalse(purchase_setup._warehouse_is_usable(bad))
		for good in (RAW, CONSUMABLES):
			self.assertTrue(purchase_setup._warehouse_is_usable(good))

	def test_unavailable_settings_single_does_not_raise(self):
		with patch.object(
			purchase_setup.frappe, "get_single", side_effect=Exception("no such doctype")
		):
			log = purchase_setup.ensure_purchase_setup()
		self.assertEqual(log["set"], [])

	def test_save_failure_does_not_raise(self):
		settings = _Settings()
		settings.save = lambda: (_ for _ in ()).throw(Exception("db down"))
		log = self._run(settings)
		self.assertTrue(log["set"], "changes were staged before the failed save")


if __name__ == "__main__":
	unittest.main()

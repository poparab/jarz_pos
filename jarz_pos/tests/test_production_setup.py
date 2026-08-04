"""Tests for the production-floor role seeder.

One property matters above all others here and it is not obvious from reading
the seeder: **granting the floor role read access must not revoke anybody
else's**. Frappe's ``get_valid_perms`` discards every standard DocPerm for a
doctype the moment that doctype has one Custom DocPerm row, so a naive insert
does not add a permission — it replaces the whole set. Getting that wrong on
``Item`` locks every Desk user out of every form with an Item link field.

The fake below reproduces that resolution rule rather than mocking it away, so
the assertions are about who can actually read Item, not about which helper got
called.
"""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


_frappe = sys.modules.get("frappe")
if _frappe is None:
	_frappe = types.ModuleType("frappe")
	sys.modules["frappe"] = _frappe


def _ensure(name, value):
	if not hasattr(_frappe, name):
		setattr(_frappe, name, value)


_ensure("_", lambda message: message)
_ensure("db", SimpleNamespace(exists=lambda *a, **k: None))
_ensure("get_all", lambda *a, **k: [])
_ensure("get_doc", lambda *a, **k: SimpleNamespace(insert=lambda **kw: None))
_ensure("clear_cache", lambda *a, **k: None)
_ensure(
	"logger",
	lambda *a, **k: SimpleNamespace(
		warning=lambda *args, **kwargs: None,
		info=lambda *args, **kwargs: None,
		error=lambda *args, **kwargs: None,
	),
)
if not hasattr(_frappe.db, "exists"):
	_frappe.db.exists = lambda *a, **k: None

# The module under test imports these lazily from frappe.permissions; the real
# package is not importable without a bench, so stand one up.
_perms = sys.modules.get("frappe.permissions")
if _perms is None:
	_perms = types.ModuleType("frappe.permissions")
	sys.modules["frappe.permissions"] = _perms
_frappe.permissions = _perms

from jarz_pos.setup import production_setup


ROLE = production_setup.ROLE_NAME

# A trimmed stand-in for what ERPNext ships. Only the role names matter.
STANDARD_PERMS = {
	"Item": ["System Manager", "Item Manager", "Stock User", "Sales User"],
	"BOM": ["System Manager", "Manufacturing Manager", "Stock User"],
	"Work Order": ["System Manager", "Manufacturing User"],
}


class FakePermissions:
	"""In-memory DocPerm / Custom DocPerm tables with Frappe's resolution rule."""

	def __init__(self, custom=None):
		self.standard = {dt: list(roles) for dt, roles in STANDARD_PERMS.items()}
		self.custom = {dt: list(rows) for dt, rows in (custom or {}).items()}

	# ── the rule under test ──────────────────────────────────────────────
	def effective_roles(self, doctype):
		"""Mirror of ``frappe.permissions.get_valid_perms``.

		Custom DocPerm rows do not merge with standard ones — if any exist for
		a doctype, they are the entire permission set for it.
		"""
		if self.custom.get(doctype):
			return {row["role"] for row in self.custom[doctype]}
		return set(self.standard.get(doctype, []))

	# ── frappe surface the seeder touches ────────────────────────────────
	def db_exists(self, doctype, name=None):
		if doctype == "DocType":
			return name in self.standard
		if doctype in ("Role", "Role Profile"):
			# Already seeded; those paths are not what these tests exercise.
			return True
		if doctype == "Custom DocPerm":
			filters = name or {}
			return any(
				row["role"] == filters.get("role")
				and row.get("permlevel", 0) == filters.get("permlevel", 0)
				for row in self.custom.get(filters.get("parent"), [])
			)
		return False

	def get_all(self, doctype, filters=None, pluck=None, **kwargs):
		parent = (filters or {}).get("parent")
		if doctype == "Custom DocPerm":
			rows = self.custom.get(parent, [])
		elif doctype == "DocPerm":
			rows = [{"role": r, "parent": parent} for r in self.standard.get(parent, [])]
		else:
			return []
		return [row[pluck] for row in rows] if pluck else [dict(r) for r in rows]

	def get_doc(self, payload):
		store = self

		class _Doc(SimpleNamespace):
			def insert(self, **kwargs):
				if self.doctype == "Custom DocPerm":
					store.custom.setdefault(self.parent, []).append(
						{"role": self.role, "permlevel": self.permlevel, "read": self.read}
					)

		return _Doc(**payload)

	# ── frappe.permissions surface ───────────────────────────────────────
	def setup_custom_perms(self, parent):
		if not self.custom.get(parent):
			self.custom[parent] = [
				{"role": role, "permlevel": 0, "read": 1} for role in self.standard.get(parent, [])
			]
			return True

	def reset_perms(self, doctype):
		self.custom.pop(doctype, None)


class _SeederCase(unittest.TestCase):
	def run_seeder(self, custom=None):
		world = FakePermissions(custom=custom)
		patches = [
			patch.object(production_setup.frappe.db, "exists", side_effect=world.db_exists),
			patch.object(production_setup.frappe, "get_all", side_effect=world.get_all),
			patch.object(production_setup.frappe, "get_doc", side_effect=world.get_doc),
			patch.object(production_setup.frappe, "clear_cache", lambda *a, **k: None),
			patch.object(
				production_setup.frappe.permissions,
				"setup_custom_perms",
				world.setup_custom_perms,
				create=True,
			),
			patch.object(
				production_setup.frappe.permissions,
				"reset_perms",
				world.reset_perms,
				create=True,
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		log = production_setup.ensure_production_setup()
		return world, log


class TestSeederIsAdditive(_SeederCase):
	def test_clean_site_keeps_every_standard_role(self):
		"""The regression: seeding the floor role must not evict anyone."""
		world, log = self.run_seeder()

		for doctype, roles in STANDARD_PERMS.items():
			effective = world.effective_roles(doctype)
			self.assertIn(ROLE, effective, f"{ROLE} was not granted read on {doctype}")
			for role in roles:
				self.assertIn(role, effective, f"{role} lost access to {doctype}")
		self.assertEqual(log["repaired"], [])

	def test_system_manager_can_still_read_item(self):
		"""Stated as the symptom, so a future reader recognises the bug report."""
		world, _ = self.run_seeder()
		self.assertIn("System Manager", world.effective_roles("Item"))

	def test_running_twice_changes_nothing(self):
		world, _ = self.run_seeder()
		before = {dt: world.effective_roles(dt) for dt in STANDARD_PERMS}

		second, log = self.run_seeder(custom=world.custom)
		self.assertEqual({dt: second.effective_roles(dt) for dt in STANDARD_PERMS}, before)
		self.assertEqual(log["repaired"], [])
		self.assertEqual(log["created"], [])


class TestRepairsAlreadyBrokenSites(_SeederCase):
	#: What the previous version of the seeder left behind: one row, nothing else.
	DAMAGE = {dt: [{"role": ROLE, "permlevel": 0, "read": 1}] for dt in STANDARD_PERMS}

	def test_damage_signature_really_is_broken(self):
		"""Guards the fake: without the fix this state locks System Manager out."""
		world = FakePermissions(custom=self.DAMAGE)
		self.assertEqual(world.effective_roles("Item"), {ROLE})

	def test_seeder_restores_displaced_standard_perms(self):
		world, log = self.run_seeder(custom={dt: list(rows) for dt, rows in self.DAMAGE.items()})

		for doctype, roles in STANDARD_PERMS.items():
			effective = world.effective_roles(doctype)
			self.assertIn(ROLE, effective)
			for role in roles:
				self.assertIn(role, effective, f"{role} was not restored on {doctype}")
		self.assertEqual(sorted(log["repaired"]), sorted(STANDARD_PERMS))


class TestLeavesHumanCustomisationAlone(_SeederCase):
	def test_an_admins_own_rows_are_not_reset(self):
		"""A hand-tightened doctype is somebody's deliberate policy, not damage."""
		admin_rows = {"Item": [{"role": "Stock Manager", "permlevel": 0, "read": 1}]}
		world, log = self.run_seeder(custom={dt: list(r) for dt, r in admin_rows.items()})

		effective = world.effective_roles("Item")
		self.assertEqual(log["repaired"], [])
		self.assertIn("Stock Manager", effective)
		# Added to their set, not merged back into the standard one — matching
		# what Frappe does for any doctype with custom perms.
		self.assertIn(ROLE, effective)
		self.assertNotIn("Sales User", effective)


if __name__ == "__main__":
	unittest.main()

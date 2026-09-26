"""User administration from the app: who may do it, and what it may not do.

Runs against a real site (``bench --site ... run-tests``) because the rules
under test are Frappe's own: a Role Profile rebuilding ``User.roles`` on save,
the password store, and the link check on delete. A mock would pass all of
them by construction. Every test rolls back.

What this pins:

* **The door.** Only JARZ Manager / System Manager / Administrator. A line
  manager or a cashier is refused on every read and write.
* **Privileged accounts.** A JARZ Manager cannot reset a System Manager's
  password or assign a profile that grants System Manager. Without this rule
  the screen is a way to sign in as the owner.
* **Profiles, not grants.** Roles come from the chosen profiles and are
  replaced when the profiles change. An empty list is refused, because Frappe
  would silently keep the old roles.
* **No self-lockout.** A manager cannot disable, delete or re-profile
  themselves.
"""

import unittest

import frappe
from frappe.utils.password import check_password

from jarz_pos.services import user_admin as svc

P_STAFF = "_Test UA Staff"
P_MANAGER = "_Test UA Manager"
P_LINE = "_Test UA Line Manager"
P_PRIV = "_Test UA Privileged"

MANAGER = "ua.manager@jarz.test"
LINE = "ua.line@jarz.test"
CASHIER = "ua.cashier@jarz.test"
SYSADMIN = "ua.sysadmin@jarz.test"
NEW = "ua.new@jarz.test"
PW = "Str0ng-Test-Pass!"


def _profile(name, roles):
    if frappe.db.exists("Role Profile", name):
        frappe.delete_doc("Role Profile", name, force=True, ignore_permissions=True)
    doc = frappe.new_doc("Role Profile")
    doc.role_profile = name
    for role in roles:
        doc.append("roles", {"role": role})
    doc.insert(ignore_permissions=True)


def _user(email, profiles):
    if frappe.db.exists("User", email):
        frappe.delete_doc("User", email, force=True, ignore_permissions=True)
    doc = frappe.new_doc("User")
    doc.email = email
    doc.first_name = email.split("@")[0]
    doc.send_welcome_email = 0
    doc.user_type = "System User"
    doc.set("role_profiles", [{"role_profile": p} for p in profiles])
    doc.new_password = PW
    doc.insert(ignore_permissions=True)
    return doc


class TestUserAdmin(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        _profile(P_STAFF, ["POS User", "Sales User"])
        _profile(P_MANAGER, ["JARZ Manager", "POS User"])
        _profile(P_LINE, ["JARZ line manager", "POS User"])
        _profile(P_PRIV, ["System Manager"])
        _user(MANAGER, [P_MANAGER])
        _user(LINE, [P_LINE])
        _user(CASHIER, [P_STAFF])
        _user(SYSADMIN, [P_PRIV])
        frappe.clear_cache()

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.clear_cache()

    def _as(self, user):
        frappe.set_user(user)

    def _create(self, **kw):
        args = dict(email=NEW, first_name="New", password=PW, role_profiles=[P_STAFF])
        args.update(kw)
        return svc.create_user(**args)

    # -- the door -------------------------------------------------------------

    def test_line_manager_and_cashier_are_refused(self):
        for user in (LINE, CASHIER):
            self._as(user)
            with self.assertRaises(frappe.PermissionError):
                svc.list_users()
            with self.assertRaises(frappe.PermissionError):
                svc.get_context()
            with self.assertRaises(frappe.PermissionError):
                self._create()
            with self.assertRaises(frappe.PermissionError):
                svc.reset_password(CASHIER, PW + "x")

    def test_manager_lists_users_with_tier_and_profiles(self):
        self._as(MANAGER)
        rows = {r["name"]: r for r in svc.list_users()}
        self.assertEqual(rows[CASHIER]["tier"], "staff")
        self.assertEqual(rows[LINE]["tier"], "line_manager")
        self.assertEqual(rows[CASHIER]["role_profiles"], [P_STAFF])
        self.assertTrue(rows[MANAGER]["is_self"])
        self.assertTrue(rows[SYSADMIN]["is_privileged"])
        self.assertFalse(rows[SYSADMIN]["can_edit"])
        self.assertNotIn("Administrator", rows)
        self.assertNotIn("Guest", rows)

    # -- create ---------------------------------------------------------------

    def test_create_user_takes_roles_from_profile_and_sets_password(self):
        self._as(MANAGER)
        out = self._create(role_profiles='["%s"]' % P_LINE, mobile_no="01000000000")
        self.assertEqual(out["role_profiles"], [P_LINE])
        roles = set(frappe.get_roles(NEW))
        self.assertIn("JARZ line manager", roles)
        self.assertIn("POS User", roles)
        self.assertEqual(out["tier"], "line_manager")
        self.assertEqual(frappe.db.get_value("User", NEW, "user_type"), "System User")
        self.assertEqual(check_password(NEW, PW), NEW)

    def test_create_refuses_bad_input(self):
        self._as(MANAGER)
        with self.assertRaises(frappe.ValidationError):
            self._create(role_profiles=[])
        with self.assertRaises(frappe.ValidationError):
            self._create(password="short")
        with self.assertRaises(frappe.DuplicateEntryError):
            self._create(email=CASHIER)
        with self.assertRaises(frappe.ValidationError):
            self._create(role_profiles=["No Such Profile"])

    def test_manager_cannot_assign_a_privileged_profile(self):
        self._as(MANAGER)
        with self.assertRaises(frappe.PermissionError):
            self._create(role_profiles=[P_PRIV])
        ctx = {p["name"]: p for p in svc.get_context()["role_profiles"]}
        self.assertFalse(ctx[P_PRIV]["assignable"])
        self.assertTrue(ctx[P_STAFF]["assignable"])

    # -- privileged targets ---------------------------------------------------

    def test_manager_cannot_touch_a_system_manager(self):
        self._as(MANAGER)
        with self.assertRaises(frappe.PermissionError):
            svc.reset_password(SYSADMIN, PW + "x")
        with self.assertRaises(frappe.PermissionError):
            svc.set_enabled(SYSADMIN, 0)
        with self.assertRaises(frappe.PermissionError):
            svc.delete_user(SYSADMIN)
        with self.assertRaises(frappe.PermissionError):
            svc.update_user(SYSADMIN, first_name="Hijacked")

    def test_system_manager_can_touch_a_system_manager(self):
        self._as(SYSADMIN)
        svc.reset_password(MANAGER, PW + "x", sign_out=0)
        self.assertEqual(check_password(MANAGER, PW + "x"), MANAGER)

    # -- edit -----------------------------------------------------------------

    def test_changing_profiles_replaces_roles(self):
        self._as(MANAGER)
        self._create(role_profiles=[P_LINE])
        svc.update_user(NEW, role_profiles=[P_STAFF])
        roles = set(frappe.get_roles(NEW))
        self.assertNotIn("JARZ line manager", roles)
        self.assertIn("Sales User", roles)
        with self.assertRaises(frappe.ValidationError):
            svc.update_user(NEW, role_profiles=[])

    def test_update_changes_only_what_was_sent(self):
        self._as(MANAGER)
        self._create(mobile_no="0100", last_name="Keep")
        out = svc.update_user(NEW, first_name="Renamed")
        self.assertEqual(out["first_name"], "Renamed")
        self.assertEqual(out["last_name"], "Keep")
        self.assertEqual(out["mobile_no"], "0100")
        self.assertEqual(out["role_profiles"], [P_STAFF])

    def test_reset_password(self):
        self._as(MANAGER)
        svc.reset_password(CASHIER, "An0ther-Pass!")
        self.assertEqual(check_password(CASHIER, "An0ther-Pass!"), CASHIER)
        with self.assertRaises(frappe.ValidationError):
            svc.reset_password(CASHIER, "short")

    # -- disable / delete -----------------------------------------------------

    def test_no_self_lockout(self):
        self._as(MANAGER)
        with self.assertRaises(frappe.PermissionError):
            svc.set_enabled(MANAGER, 0)
        with self.assertRaises(frappe.PermissionError):
            svc.delete_user(MANAGER)
        with self.assertRaises(frappe.PermissionError):
            svc.update_user(MANAGER, role_profiles=[P_STAFF])
        # Renaming yourself is harmless.
        svc.update_user(MANAGER, first_name="Boss")

    def test_disable_and_enable(self):
        self._as(MANAGER)
        self.assertEqual(svc.set_enabled(CASHIER, 0)["enabled"], 0)
        self.assertEqual(svc.set_enabled(CASHIER, 1)["enabled"], 1)

    def test_delete_fresh_user(self):
        self._as(MANAGER)
        self._create()
        svc.delete_user(NEW)
        self.assertFalse(frappe.db.exists("User", NEW))

    def test_delete_linked_user_is_refused_and_kept(self):
        employee = frappe.db.get_value("Employee", {"status": "Active", "user_id": ["is", "not set"]})
        if not employee:
            self.skipTest("no unlinked active Employee on this site")
        self._as(MANAGER)
        self._create(employee=employee)
        self.assertEqual(frappe.db.get_value("Employee", employee, "user_id"), NEW)
        with self.assertRaises(frappe.ValidationError):
            svc.delete_user(NEW)
        self.assertTrue(frappe.db.exists("User", NEW))
        self.assertEqual(frappe.db.get_value("Employee", employee, "user_id"), NEW)

    def test_employee_link_moves_and_clears(self):
        employees = frappe.get_all(
            "Employee", filters={"status": "Active", "user_id": ["is", "not set"]}, pluck="name", limit=2
        )
        if len(employees) < 2:
            self.skipTest("needs two unlinked active Employees")
        a, b = employees
        self._as(MANAGER)
        self._create(employee=a)
        svc.update_user(NEW, employee=b)
        self.assertFalse(frappe.db.get_value("Employee", a, "user_id"))
        self.assertEqual(frappe.db.get_value("Employee", b, "user_id"), NEW)
        svc.update_user(NEW, clear_employee=1)
        self.assertFalse(frappe.db.get_value("Employee", b, "user_id"))


if __name__ == "__main__":
    unittest.main()

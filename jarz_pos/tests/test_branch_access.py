"""Branch access: POS Profile membership, one-day access, and the open-branch rule.

What this pins, and why each part is worth a test:

* **Scope.** A line manager may staff only their own branches and never edit
  their own membership; a cashier may do nothing. A hole here lets somebody
  hand themselves a till.
* **The open-branch rule.** No membership change while a branch has an open
  shift -- add, remove, a same-day grant, cancelling a running grant, and the
  hourly job's start/end all wait. The refusal must name the branch and who
  holds the shift.
* **``row_added``.** A day access removes only a row it added. Getting that
  wrong one way locks a regular cashier out of their own branch the morning
  after an unnecessary grant; the other way leaves a cover with access
  forever. Overlapping grants keep the row until the LAST one ends.
* **The roster tick box is best-effort.** A refused grant must never undo the
  shift the manager just saved.
* **Cover location.** A cover is rostered at the ABSENT person's branch, not
  the coverer's home branch, or the geofence refuses their check-in where
  they were actually sent.

The service's data access is replaced by an in-memory ``World`` so the real
decision logic runs end to end without a site database.
"""

import unittest
from contextlib import ExitStack, contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import roster as roster_api
from jarz_pos.constants import ROLES
from jarz_pos.services import branch_access as svc
from jarz_pos.services import roster as roster_service

NOW = datetime(2026, 9, 23, 10, 0, 0)
TODAY = NOW.date()
TOMORROW = date(2026, 9, 24)

ENABLED = ["6th of october", "Dokki", "Nasr city"]


def _identity(text, *args, **kwargs):
    return text


def _fake_throw(msg, exc=None, title=None, **kwargs):
    raise (exc or frappe.ValidationError)(msg)


class World:
    """In-memory stand-in for the three tables the service reads and writes."""

    def __init__(
        self,
        caller="manager@jarz.test",
        roles=(ROLES.JARZ_MANAGER,),
        caller_profiles=(),
        open_shifts=None,
        now=NOW,
    ):
        self.caller = caller
        self.roles = {caller: set(roles)}
        self.caller_profiles = list(caller_profiles)
        self.open_shifts = dict(open_shifts or {})
        self.now = now
        self.rows = {}  # row name -> (user, pos_profile)
        self.grants = {}  # name -> dict
        self.logs = []
        self._seq = 0
        self.frappe = None
        self.fail_load = set()
        self.disabled_users = set()

    # -- setup helpers -------------------------------------------------------

    def _next(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def add_member(self, user, profile):
        name = self._next("ROW")
        self.rows[name] = (user, profile)
        return name

    def add_grant(self, user, profile, day, status="Scheduled", row_added=0, granted_by=None):
        starts_at, expires_at = svc.window_for(day)
        name = self._next("JPDA")
        self.grants[name] = {
            "name": name,
            "user": user,
            "pos_profile": profile,
            "access_date": day,
            "status": status,
            "row_added": row_added,
            "starts_at": starts_at,
            "expires_at": expires_at,
            "granted_by": granted_by or self.caller,
            "last_deferral": None,
        }
        return name

    def is_member(self, user, profile):
        return (user, profile) in self.rows.values()

    def actions(self):
        return [entry["action"] for entry in self.logs]

    # -- fakes ---------------------------------------------------------------

    def _membership_rows(self, user, profile):
        rows = [n for n, (u, p) in sorted(self.rows.items()) if u == user and p == profile]
        # The caller's own permanent branches, kept out of ``self.rows`` so
        # tests asserting "nothing was written" still see an empty table.
        if not rows and user == self.caller and profile in self.caller_profiles:
            rows = [f"CALLER-{profile}"]
        return rows

    def _insert_membership_row(self, user, profile):
        return self.add_member(user, profile)

    def _delete_membership_rows(self, user, profile):
        names = self._membership_rows(user, profile)
        for name in names:
            self.rows.pop(name, None)
        return len(names)

    def _log(self, user, profile, action, source, *, notes=None, day_access=None, changed_by=None):
        name = self._next("LOG")
        self.logs.append(
            {
                "name": name,
                "user": user,
                "pos_profile": profile,
                "action": action,
                "source": source,
                "notes": notes,
                "day_access": day_access,
                "changed_by": changed_by,
            }
        )
        return name

    def _load_grant(self, name):
        if name in self.fail_load:
            raise RuntimeError("row is broken")
        grant = self.grants.get(name)
        return dict(grant) if grant else None

    def _set_grant(self, name, values):
        self.grants[name].update(values)

    def _create_grant(self, values):
        name = self._next("JPDA")
        self.grants[name] = dict(values, name=name)
        return name

    def _active_grants(self, user, profile, exclude=None):
        return [
            dict(g)
            for n, g in sorted(self.grants.items())
            if g["user"] == user
            and g["pos_profile"] == profile
            and g["status"] == "Active"
            and n != exclude
        ]

    def _open_grants_on(self, user, profile, day):
        return [
            n
            for n, g in sorted(self.grants.items())
            if g["user"] == user
            and g["pos_profile"] == profile
            and g["access_date"] == day
            and g["status"] in ("Scheduled", "Active")
        ]

    def _scan(self, kind, now):
        out = []
        for name, g in sorted(self.grants.items()):
            if kind == "start" and g["status"] == "Scheduled" and g["starts_at"] <= now < g["expires_at"]:
                out.append(name)
            elif kind == "end" and g["status"] == "Active" and g["expires_at"] <= now:
                out.append(name)
            elif kind == "stale" and g["status"] == "Scheduled" and g["expires_at"] <= now:
                out.append(name)
        return out

    @contextmanager
    def active(self, profile_for_location=None):
        with ExitStack() as stack:

            def p(name, **kwargs):
                return stack.enter_context(patch.object(svc, name, **kwargs))

            mock_frappe = p("frappe")
            mock_frappe.throw.side_effect = _fake_throw
            mock_frappe.ValidationError = frappe.ValidationError
            mock_frappe.PermissionError = frappe.PermissionError
            mock_frappe.db.exists.return_value = True
            self.frappe = mock_frappe

            p("_", new=_identity)
            p("_now", side_effect=lambda: self.now)
            p("_session_user", side_effect=lambda: self.caller)
            p("_roles", side_effect=lambda user=None: set(self.roles.get(user or self.caller, set())))
            p("_enabled_profiles", side_effect=lambda: list(ENABLED))
            p(
                "_user_profiles",
                side_effect=lambda user: list(self.caller_profiles) if user == self.caller else [],
            )
            p("_open_shift", side_effect=lambda profile: self.open_shifts.get(profile))
            p(
                "_user_row",
                side_effect=lambda user: {
                    "name": user,
                    "full_name": user.split("@")[0].title(),
                    "enabled": 0 if user in self.disabled_users else 1,
                },
            )
            p("_full_name", side_effect=lambda user: user.split("@")[0].title() if user else None)
            p("_employee_for_user", return_value=(None, None))
            p("_invalidate")
            for name in (
                "_membership_rows",
                "_insert_membership_row",
                "_delete_membership_rows",
                "_log",
                "_load_grant",
                "_set_grant",
                "_create_grant",
                "_active_grants",
                "_open_grants_on",
                "_scan",
            ):
                p(name, side_effect=getattr(self, name))
            p("profile_for_shift_location", side_effect=lambda loc: (profile_for_location or {}).get(loc))
            yield self


def _open_shift(user="seif@jarz.test", since=datetime(2026, 9, 23, 12, 47)):
    return {"name": "POS-OPE-0001", "user": user, "pos_profile": "Dokki", "period_start_date": since}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestScope(unittest.TestCase):
    def test_manager_tier_manages_every_enabled_branch(self):
        for roles in ([ROLES.JARZ_MANAGER], [ROLES.SYSTEM_MANAGER]):
            world = World(roles=roles)
            with world.active():
                self.assertEqual(svc.manageable_profiles(), ENABLED, roles)
                self.assertTrue(svc.can_manage_all())

    def test_administrator_manages_everything_without_any_role(self):
        world = World(caller="Administrator", roles=())
        with world.active():
            self.assertTrue(svc.can_manage_all())
            self.assertEqual(svc.manageable_profiles(), ENABLED)

    def test_line_manager_manages_only_their_own_branches(self):
        """Both spellings of the role are real Role records; both must work."""
        for role in (ROLES.JARZ_LINE_MANAGER, ROLES.JARZ_LINE_MANAGER_ALT):
            world = World(caller="lm@jarz.test", roles=[role], caller_profiles=["Dokki"])
            with world.active():
                self.assertFalse(svc.can_manage_all(), role)
                self.assertEqual(svc.manageable_profiles(), ["Dokki"], role)

    def test_line_manager_can_add_someone_to_their_own_branch(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Dokki"])
        with world.active():
            result = svc.set_branch_access("ali@jarz.test", "Dokki", True)
        self.assertTrue(result["changed"])
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))

    def test_line_manager_is_refused_on_another_branch(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Dokki"])
        with world.active():
            with self.assertRaises(frappe.PermissionError):
                svc.set_branch_access("ali@jarz.test", "Nasr city", True)
            with self.assertRaises(frappe.PermissionError):
                svc.grant_day_access("ali@jarz.test", "Nasr city", TODAY)
        self.assertFalse(world.rows)
        self.assertFalse(world.grants)

    def test_line_manager_cannot_change_their_own_membership(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER_ALT], caller_profiles=["Dokki"])
        world.add_member("lm@jarz.test", "Dokki")
        with world.active():
            with self.assertRaises(frappe.PermissionError):
                svc.set_branch_access("lm@jarz.test", "Dokki", False)
            with self.assertRaises(frappe.PermissionError):
                svc.grant_day_access("lm@jarz.test", "Dokki", TOMORROW)
        self.assertTrue(world.is_member("lm@jarz.test", "Dokki"))

    def test_line_manager_cannot_change_a_managers_access(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Dokki"])
        world.roles["boss@jarz.test"] = {ROLES.JARZ_MANAGER}
        world.add_member("boss@jarz.test", "Dokki")
        with world.active():
            with self.assertRaises(frappe.PermissionError):
                svc.set_branch_access("boss@jarz.test", "Dokki", False)
        self.assertTrue(world.is_member("boss@jarz.test", "Dokki"))

    def test_line_manager_cannot_change_another_line_managers_access(self):
        """Two line managers adding each other would reach every branch either runs."""
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Dokki"])
        world.roles["lm2@jarz.test"] = {ROLES.JARZ_LINE_MANAGER_ALT}
        with world.active():
            with self.assertRaises(frappe.PermissionError):
                svc.set_branch_access("lm2@jarz.test", "Dokki", True)
            with self.assertRaises(frappe.PermissionError):
                svc.grant_day_access("lm2@jarz.test", "Dokki", TOMORROW)
            self.assertFalse(svc.can_edit_user("lm2@jarz.test"))
            self.assertTrue(svc.can_edit_user("ali@jarz.test"))
        self.assertFalse(world.rows)

    def test_a_day_access_does_not_widen_a_line_managers_reach(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Dokki"])
        world.add_member("lm@jarz.test", "Nasr city")
        world.add_grant("lm@jarz.test", "Nasr city", TODAY, status="Active", row_added=1)
        with world.active():
            self.assertEqual(svc.manageable_profiles(), ["Dokki"])

    def test_a_cashier_cannot_probe_users_through_the_error_message(self):
        world = World(caller="cashier@jarz.test", roles=["POS User"])
        with world.active():
            with self.assertRaises(frappe.PermissionError):
                svc.set_branch_access("nobody@jarz.test", "Dokki", True)
            with self.assertRaises(frappe.PermissionError):
                svc.grant_day_access("nobody@jarz.test", "Dokki", TOMORROW)
        world.frappe.db.get_value.assert_not_called()

    def test_cashier_is_refused_everything(self):
        world = World(caller="cashier@jarz.test", roles=["POS User"])
        with world.active():
            self.assertFalse(svc.has_access())
            self.assertEqual(svc.manageable_profiles(), [])
            with self.assertRaises(frappe.PermissionError):
                svc.set_branch_access("ali@jarz.test", "Dokki", True)
            with self.assertRaises(frappe.PermissionError):
                svc.get_branch_access()
            with self.assertRaises(frappe.PermissionError):
                svc.get_access_log()
        self.assertFalse(world.rows)

    def test_line_manager_cannot_read_another_branchs_history(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Dokki"])
        with world.active():
            with self.assertRaises(frappe.PermissionError):
                svc.get_access_log(pos_profile="Nasr city")

    def test_line_manager_history_is_scoped_to_their_branches(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Dokki"])
        with world.active():
            world.frappe.get_all.return_value = []
            svc.get_access_log()
            filters = world.frappe.get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["pos_profile"], ("in", ["Dokki"]))

    def test_disabled_branch_is_refused(self):
        world = World()
        with world.active():
            with self.assertRaises(frappe.ValidationError):
                svc.set_branch_access("ali@jarz.test", "Ismalia", True)


# ---------------------------------------------------------------------------
# No change while the branch is open
# ---------------------------------------------------------------------------


class TestOpenBranchRule(unittest.TestCase):
    def test_add_is_refused_while_open_and_names_the_holder(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        with world.active():
            with self.assertRaises(svc.BranchOpenError) as ctx:
                svc.set_branch_access("ali@jarz.test", "Dokki", True)
        message = str(ctx.exception)
        self.assertIn("Dokki", message)
        self.assertIn("Seif", message)
        self.assertIn("12:47", message)
        self.assertFalse(world.rows)
        self.assertEqual(world.logs, [])

    def test_a_shift_left_open_since_yesterday_shows_the_date(self):
        world = World(open_shifts={"Dokki": _open_shift(since=datetime(2026, 9, 22, 12, 40))})
        with world.active():
            with self.assertRaises(svc.BranchOpenError) as ctx:
                svc.set_branch_access("ali@jarz.test", "Dokki", True)
        self.assertIn("2026-09-22 12:40", str(ctx.exception))

    def test_remove_is_refused_while_open(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        world.add_member("ali@jarz.test", "Dokki")
        with world.active():
            with self.assertRaises(svc.BranchOpenError):
                svc.set_branch_access("ali@jarz.test", "Dokki", False)
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))

    def test_grant_for_today_is_refused_while_open_and_leaves_nothing_behind(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        with world.active():
            with self.assertRaises(svc.BranchOpenError):
                svc.grant_day_access("ali@jarz.test", "Dokki", TODAY)
        self.assertEqual(world.grants, {})
        self.assertFalse(world.rows)

    def test_grant_for_a_later_day_is_accepted_while_open(self):
        """Only today's grant changes membership now; a later one waits for the job."""
        world = World(open_shifts={"Dokki": _open_shift()})
        with world.active():
            result = svc.grant_day_access("ali@jarz.test", "Dokki", TOMORROW)
        self.assertEqual(result["day_access"]["status"], "Scheduled")
        self.assertFalse(world.rows)

    def test_cancelling_a_running_grant_is_refused_while_open(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        world.add_member("ali@jarz.test", "Dokki")
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        with world.active():
            with self.assertRaises(svc.BranchOpenError):
                svc.cancel_day_access(name)
        self.assertEqual(world.grants[name]["status"], "Active")
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))

    def test_cancelling_a_scheduled_grant_is_fine_while_open(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        name = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        with world.active():
            svc.cancel_day_access(name)
        self.assertEqual(world.grants[name]["status"], "Cancelled")


# ---------------------------------------------------------------------------
# Manual membership
# ---------------------------------------------------------------------------


class TestManualMembership(unittest.TestCase):
    def test_add_inserts_a_row_and_logs_it(self):
        world = World()
        with world.active():
            result = svc.set_branch_access("ali@jarz.test", "Dokki", True, notes="new hire")
        self.assertEqual(
            {k: result[k] for k in ("changed", "user", "pos_profile", "allowed")},
            {"changed": True, "user": "ali@jarz.test", "pos_profile": "Dokki", "allowed": True},
        )
        self.assertEqual(world.actions(), ["Added"])
        self.assertEqual(result["log"], world.logs[0]["name"])
        self.assertEqual(world.logs[0]["changed_by"], world.caller)
        self.assertEqual(world.logs[0]["source"], "Branch Access Screen")

    def test_no_change_writes_no_log(self):
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        with world.active():
            added = svc.set_branch_access("ali@jarz.test", "Dokki", True)
            removed = svc.set_branch_access("bob@jarz.test", "Dokki", False)
        self.assertFalse(added["changed"])
        self.assertIsNone(added["log"])
        self.assertFalse(removed["changed"])
        self.assertEqual(world.logs, [])

    def test_manual_add_turns_a_running_day_access_permanent(self):
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        grant = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        with world.active():
            result = svc.set_branch_access("ali@jarz.test", "Dokki", True)
        self.assertTrue(result["changed"])
        self.assertEqual(world.grants[grant]["row_added"], 0)
        self.assertEqual(world.actions(), ["Added"])
        # And the 03:00 expiry now removes nothing.
        world.now = datetime(2026, 9, 24, 4, 0)
        with world.active():
            svc.run_day_access_cycle()
        self.assertEqual(world.grants[grant]["status"], "Ended")
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))

    def test_manual_remove_cancels_the_running_day_access(self):
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        grant = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        later = world.add_grant("ali@jarz.test", "Dokki", date(2026, 9, 26))
        with world.active():
            result = svc.set_branch_access("ali@jarz.test", "Dokki", False)
        self.assertTrue(result["changed"])
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))
        self.assertEqual(world.grants[grant]["status"], "Cancelled")
        # A future day is a separate decision and is left alone.
        self.assertEqual(world.grants[later]["status"], "Scheduled")
        self.assertEqual(world.actions(), ["Removed", "Day Access Cancelled"])

    def test_remove_deletes_duplicate_rows_too(self):
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        world.add_member("ali@jarz.test", "Dokki")
        with world.active():
            svc.set_branch_access("ali@jarz.test", "Dokki", False)
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))


# ---------------------------------------------------------------------------
# Day access
# ---------------------------------------------------------------------------


class TestGrantDayAccess(unittest.TestCase):
    def test_today_starts_at_once_and_adds_the_row(self):
        world = World()
        with world.active():
            result = svc.grant_day_access("ali@jarz.test", "Dokki", TODAY, notes="cover")
        grant = result["day_access"]
        self.assertEqual(grant["status"], "Active")
        self.assertTrue(grant["row_added"])
        self.assertEqual(grant["access_date"], "2026-09-23")
        self.assertEqual(grant["starts_at"], "2026-09-23 00:00:00")
        self.assertEqual(grant["expires_at"], "2026-09-24 03:00:00")
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))
        self.assertEqual(world.grants[grant["name"]]["status"], "Active")
        self.assertEqual(world.actions(), ["Day Access Started"])
        self.assertTrue(result["message"])

    def test_response_carries_the_contract_keys(self):
        world = World()
        with world.active():
            result = svc.grant_day_access("ali@jarz.test", "Dokki", TOMORROW)
        for key in ("name", "status", "access_date", "starts_at", "expires_at", "row_added"):
            self.assertIn(key, result["day_access"])
        self.assertEqual(world.actions(), ["Day Access Scheduled"])

    def test_a_permanent_member_is_refused(self):
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        with world.active():
            with self.assertRaises(svc.AlreadyHasAccessError) as ctx:
                svc.grant_day_access("ali@jarz.test", "Dokki", TOMORROW)
        self.assertIn("already has access to Dokki", str(ctx.exception))

    def test_a_second_grant_for_the_same_day_is_refused(self):
        world = World()
        world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        with world.active():
            with self.assertRaises(svc.AlreadyHasAccessError):
                svc.grant_day_access("ali@jarz.test", "Dokki", TOMORROW)

    def test_a_past_date_is_refused(self):
        world = World()
        with world.active():
            with self.assertRaises(frappe.ValidationError):
                svc.grant_day_access("ali@jarz.test", "Dokki", date(2026, 9, 22))
        self.assertEqual(world.grants, {})

    def test_more_than_fourteen_days_ahead_is_refused(self):
        world = World()
        with world.active():
            with self.assertRaises(frappe.ValidationError):
                svc.grant_day_access("ali@jarz.test", "Dokki", date(2026, 10, 8))
            # Day fourteen itself is allowed.
            result = svc.grant_day_access("ali@jarz.test", "Dokki", date(2026, 10, 7))
        self.assertEqual(result["day_access"]["status"], "Scheduled")

    def test_a_missing_date_is_refused_rather_than_read_as_today(self):
        world = World()
        with world.active():
            with self.assertRaises(frappe.ValidationError):
                svc.grant_day_access("ali@jarz.test", "Dokki", None)
        self.assertEqual(world.grants, {})

    def test_cancel_running_grant_removes_the_row_it_added(self):
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        with world.active():
            result = svc.cancel_day_access(name)
        self.assertEqual(result["day_access"]["status"], "Cancelled")
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))
        self.assertEqual(world.actions(), ["Day Access Cancelled"])

    def test_cancel_an_already_finished_grant_is_refused(self):
        world = World()
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Ended")
        with world.active():
            with self.assertRaises(frappe.ValidationError):
                svc.cancel_day_access(name)


# ---------------------------------------------------------------------------
# The hourly job
# ---------------------------------------------------------------------------


class TestDayAccessCycle(unittest.TestCase):
    def test_start_and_end_on_schedule(self):
        world = World()
        name = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        world.now = datetime(2026, 9, 24, 1, 0)
        with world.active():
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["started"], 1)
        self.assertEqual(world.grants[name]["status"], "Active")
        self.assertEqual(world.grants[name]["row_added"], 1)
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))

        world.now = datetime(2026, 9, 25, 3, 0)
        with world.active():
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["ended"], 1)
        self.assertEqual(world.grants[name]["status"], "Ended")
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))
        self.assertEqual(world.logs[-1]["source"], "Scheduler")

    def test_a_permanent_member_gets_row_added_0_and_keeps_access_after_the_end(self):
        """The lock-out case: the grant must never remove a row it did not add."""
        world = World()
        name = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        world.add_member("ali@jarz.test", "Dokki")  # made permanent after booking
        world.now = datetime(2026, 9, 24, 1, 0)
        with world.active():
            svc.run_day_access_cycle()
        self.assertEqual(world.grants[name]["row_added"], 0)

        world.now = datetime(2026, 9, 25, 4, 0)
        with world.active():
            svc.run_day_access_cycle()
        self.assertEqual(world.grants[name]["status"], "Ended")
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))

    def test_overlapping_grants_keep_the_row_until_the_last_one_ends(self):
        """The access-forever case, from the other side.

        Today's grant added the row. Tomorrow's starts while today's is still
        running, so it must claim the row too (row_added=1); otherwise, once
        today's ends and leaves the row for tomorrow's, nobody ever removes it.
        """
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        first = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        second = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)

        world.now = datetime(2026, 9, 24, 1, 0)
        with world.active():
            svc.run_day_access_cycle()
        self.assertEqual(world.grants[second]["status"], "Active")
        self.assertEqual(world.grants[second]["row_added"], 1)

        world.now = datetime(2026, 9, 24, 4, 0)
        with world.active():
            svc.run_day_access_cycle()
        self.assertEqual(world.grants[first]["status"], "Ended")
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"), "second grant still needs it")

        world.now = datetime(2026, 9, 25, 4, 0)
        with world.active():
            svc.run_day_access_cycle()
        self.assertEqual(world.grants[second]["status"], "Ended")
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))

    def test_expiry_waits_while_the_branch_is_open(self):
        world = World(open_shifts={"Dokki": _open_shift(since=datetime(2026, 9, 23, 12, 30))})
        world.add_member("ali@jarz.test", "Dokki")
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        world.now = datetime(2026, 9, 24, 4, 0)
        with world.active():
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(world.grants[name]["status"], "Active")
        self.assertIn("Dokki has an open shift", world.grants[name]["last_deferral"])
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))

        # Next hour the branch has closed: now it ends.
        world.open_shifts = {}
        world.now = datetime(2026, 9, 24, 5, 0)
        with world.active():
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["ended"], 1)
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))

    def test_start_waits_while_the_branch_is_open(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        name = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        world.now = datetime(2026, 9, 24, 0, 10)
        with world.active():
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(world.grants[name]["status"], "Scheduled")
        self.assertTrue(world.grants[name]["last_deferral"])
        self.assertFalse(world.rows)

    def test_a_grant_that_never_started_is_cancelled_with_the_reason(self):
        world = World()
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY)
        world.grants[name]["last_deferral"] = "Could not start: Dokki has an open shift"
        world.now = datetime(2026, 9, 24, 4, 0)
        with world.active():
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["cancelled"], 1)
        self.assertEqual(world.grants[name]["status"], "Cancelled")
        self.assertIn("Dokki has an open shift", world.grants[name]["last_deferral"])
        self.assertEqual(world.actions(), ["Day Access Cancelled"])
        self.assertFalse(world.rows)

    def test_a_booked_grant_for_a_since_disabled_user_is_cancelled_not_started(self):
        world = World()
        name = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        world.disabled_users.add("ali@jarz.test")
        world.now = datetime(2026, 9, 24, 1, 0)
        with world.active():
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["cancelled"], 1)
        self.assertEqual(world.grants[name]["status"], "Cancelled")
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))

    def test_a_grant_booked_by_a_line_manager_since_moved_off_the_branch_does_not_start(self):
        world = World()
        world.roles["lm@jarz.test"] = {ROLES.JARZ_LINE_MANAGER}
        name = world.add_grant("ali@jarz.test", "Dokki", TOMORROW, granted_by="lm@jarz.test")
        world.now = datetime(2026, 9, 24, 1, 0)
        with world.active():
            svc.run_day_access_cycle()
        self.assertEqual(world.grants[name]["status"], "Cancelled")
        self.assertIn("no longer manages", world.grants[name]["last_deferral"])

    def test_one_broken_grant_does_not_stop_the_others(self):
        world = World()
        broken = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        fine = world.add_grant("bob@jarz.test", "Dokki", TOMORROW)
        world.fail_load.add(broken)
        world.now = datetime(2026, 9, 24, 1, 0)
        with world.active():
            summary = svc.run_day_access_cycle()
            world.frappe.db.rollback.assert_any_call(save_point="jarz_day_access_cycle")
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["started"], 1)
        self.assertEqual(world.grants[fine]["status"], "Active")

    def test_never_raises_even_when_log_error_does(self):
        world = World()
        with world.active():
            world.frappe.db.exists.side_effect = RuntimeError("db down")
            world.frappe.log_error.side_effect = RuntimeError("log down too")
            summary = svc.run_day_access_cycle()
        self.assertEqual(summary["errors"], 1)


# ---------------------------------------------------------------------------
# Roster integration
# ---------------------------------------------------------------------------


class TestUndoneCover(unittest.TestCase):
    """The day-off row is linked from the grant; deleting it must stay possible."""

    def _release(self, world, grants):
        with world.active():
            world.frappe.get_all.return_value = list(grants)
            svc.release_for_day_off("JRDO-0001")

    def test_scheduled_grant_is_cancelled_and_unlinked(self):
        world = World()
        name = world.add_grant("ali@jarz.test", "Dokki", TOMORROW)
        world.grants[name]["roster_day_off"] = "JRDO-0001"
        self._release(world, [name])
        self.assertEqual(world.grants[name]["status"], "Cancelled")
        self.assertIsNone(world.grants[name]["roster_day_off"])

    def test_active_grant_on_a_closed_branch_is_ended_and_its_row_removed(self):
        world = World()
        world.add_member("ali@jarz.test", "Dokki")
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        self._release(world, [name])
        self.assertEqual(world.grants[name]["status"], "Cancelled")
        self.assertFalse(world.is_member("ali@jarz.test", "Dokki"))
        self.assertIsNone(world.grants[name]["roster_day_off"])

    def test_active_grant_on_an_open_branch_keeps_running_but_is_unlinked(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        world.add_member("ali@jarz.test", "Dokki")
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        self._release(world, [name])
        self.assertEqual(world.grants[name]["status"], "Active")
        self.assertTrue(world.is_member("ali@jarz.test", "Dokki"))
        self.assertIsNone(world.grants[name]["roster_day_off"])


class TestRemoveWithoutARow(unittest.TestCase):
    def test_a_running_grant_whose_row_vanished_is_cancelled(self):
        world = World()
        name = world.add_grant("ali@jarz.test", "Dokki", TODAY, status="Active", row_added=1)
        with world.active():
            result = svc.set_branch_access("ali@jarz.test", "Dokki", False)
        self.assertTrue(result["changed"])
        self.assertEqual(world.grants[name]["status"], "Cancelled")


class TestRosterGrant(unittest.TestCase):
    LOCATIONS = {"Dokki": "Dokki", "Nasr City": "Nasr city"}

    def _grant(self, world, location="Dokki", user="cover@jarz.test", on=TODAY):
        with world.active(profile_for_location=self.LOCATIONS):
            world.frappe.db.get_value.return_value = user
            outcome = svc.grant_for_roster("HR-EMP-0001", on, location, source=svc.SOURCE_COVER)
            rollback_calls = list(world.frappe.db.rollback.call_args_list)
        return outcome, rollback_calls

    def test_granted_today(self):
        world = World()
        outcome, _ = self._grant(world)
        self.assertTrue(outcome["granted"])
        self.assertEqual(outcome["status"], "Active")
        self.assertEqual(outcome["pos_profile"], "Dokki")
        self.assertTrue(outcome["day_access"])

    def test_branch_open_is_reported_and_rolled_back_to_the_savepoint(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        outcome, rollbacks = self._grant(world)
        self.assertFalse(outcome["granted"])
        self.assertIn("open shift", outcome["reason"])
        self.assertIn(unittest.mock.call(save_point="jarz_roster_pos_access"), rollbacks)
        self.assertFalse(world.rows)

    def test_already_a_member(self):
        world = World()
        world.add_member("cover@jarz.test", "Dokki")
        outcome, _ = self._grant(world)
        self.assertFalse(outcome["granted"])
        self.assertTrue(outcome["already_member"])

    def test_location_without_a_pos_branch(self):
        world = World()
        outcome, _ = self._grant(world, location="Factory")
        self.assertFalse(outcome["granted"])
        self.assertIsNone(outcome["pos_profile"])
        self.assertIn("Factory", outcome["reason"])

    def test_employee_without_a_user(self):
        world = World()
        outcome, _ = self._grant(world, user=None)
        self.assertFalse(outcome["granted"])
        self.assertTrue(outcome["reason"])

    def test_not_your_branch(self):
        world = World(caller="lm@jarz.test", roles=[ROLES.JARZ_LINE_MANAGER], caller_profiles=["Nasr city"])
        outcome, _ = self._grant(world)
        self.assertFalse(outcome["granted"])
        self.assertFalse(outcome["already_member"])
        self.assertTrue(outcome["reason"])


class TestRosterApiGrantIsBestEffort(unittest.TestCase):
    """The shift the manager saved must survive a refused grant."""

    @contextmanager
    def _api(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(roster_api, "_", new=_identity))
            stack.enter_context(patch.object(roster_api, "_ensure_access"))
            stack.enter_context(patch.object(roster_api, "_ensure_employee_active"))
            stack.enter_context(patch.object(roster_api, "_ensure_employee_in_scope"))
            yield stack

    def test_assign_shift_survives_a_refused_grant(self):
        world = World(open_shifts={"Dokki": _open_shift()})
        with self._api(), world.active(profile_for_location={"Dokki": "Dokki"}), patch.object(
            roster_service,
            "set_shift_for_day",
            return_value={"changed": True, "shift_type": "Branch Closing", "shift_location": "Dokki"},
        ) as write:
            world.frappe.db.get_value.return_value = "cover@jarz.test"
            result = roster_api.assign_shift(
                "HR-EMP-0001", "2026-09-23", "Branch Closing", grant_pos_access="1"
            )
            rollbacks = list(world.frappe.db.rollback.call_args_list)

        write.assert_called_once()
        self.assertTrue(result["success"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["pos_access"]["requested"])
        self.assertFalse(result["pos_access"]["granted"])
        self.assertIn("open shift", result["pos_access"]["reason"])
        # Only the grant's own savepoint is rolled back -- never the request.
        self.assertEqual(rollbacks, [unittest.mock.call(save_point="jarz_roster_pos_access")])

    def test_assign_shift_without_the_tick_box_does_not_grant(self):
        with self._api(), patch.object(
            roster_service, "set_shift_for_day", return_value={"changed": True, "shift_location": "Dokki"}
        ), patch.object(svc, "grant_for_roster") as grant:
            result = roster_api.assign_shift("HR-EMP-0001", "2026-09-23", "Branch Closing", grant_pos_access="0")
        grant.assert_not_called()
        self.assertIsNone(result["pos_access"])

    def test_day_off_cover_grants_the_coverer_at_the_cover_location(self):
        with self._api(), patch.object(
            roster_service,
            "set_day_off",
            return_value={"day_off": "JRDO-1", "cover_shift_location": "Nasr City"},
        ), patch.object(svc, "grant_for_roster", return_value={"granted": True}) as grant, patch.object(
            roster_api, "frappe"
        ) as mock_frappe:
            mock_frappe.db.get_value.return_value = "Absent Person"
            roster_api.set_day_off(
                "HR-EMP-0001",
                "2026-09-23",
                covered_by="HR-EMP-0002",
                cover_shift_type="Branch Cover Full Day",
                grant_pos_access=1,
            )
        args, kwargs = grant.call_args
        self.assertEqual(args[0], "HR-EMP-0002")
        self.assertEqual(args[2], "Nasr City")
        self.assertEqual(kwargs["source"], "Day Off Cover")
        self.assertEqual(kwargs["roster_day_off"], "JRDO-1")

    def test_day_off_without_a_cover_reports_why_nothing_was_granted(self):
        with self._api(), patch.object(
            roster_service, "set_day_off", return_value={"day_off": "JRDO-1", "cover_shift_location": None}
        ), patch.object(svc, "grant_for_roster") as grant:
            result = roster_api.set_day_off("HR-EMP-0001", "2026-09-23", grant_pos_access="true")
        grant.assert_not_called()
        self.assertFalse(result["pos_access"]["granted"])
        self.assertTrue(result["pos_access"]["reason"])


# ---------------------------------------------------------------------------
# Cover location fix
# ---------------------------------------------------------------------------


class TestCoverLocation(unittest.TestCase):
    """A cover goes where the absent person was, not where the coverer lives."""

    def _cover(self, original, cover_previous):
        def fake_assignment_on(employee, _day):
            return original if employee == "HR-EMP-ABSENT" else cover_previous

        with patch.object(roster_service, "frappe") as mock_frappe, patch.object(
            roster_service, "_", new=_identity
        ), patch.object(
            roster_service, "_assignment_on", side_effect=fake_assignment_on
        ), patch.object(roster_service, "_break_assignment"), patch.object(
            roster_service, "_schedule_location", return_value=None
        ), patch.object(
            roster_service,
            "set_shift_for_day",
            side_effect=lambda emp, day, st, loc: {"shift_location": loc},
        ) as cover_write:
            mock_frappe.new_doc.return_value = MagicMock()
            result = roster_service.set_day_off(
                "HR-EMP-ABSENT",
                "2026-09-23",
                covered_by="HR-EMP-COVER",
                cover_shift_type="Branch Cover Full Day",
            )
        return cover_write.call_args[0][3], result

    def test_absent_persons_branch_wins_over_the_coverers_home(self):
        location, result = self._cover(
            SimpleNamespace(name="SA-1", shift_type="Branch Opening", shift_location="Nasr City"),
            SimpleNamespace(name="SA-2", shift_type="Branch Closing", shift_location="Dokki"),
        )
        self.assertEqual(location, "Nasr City")
        self.assertEqual(result["cover_shift_location"], "Nasr City")

    def test_falls_back_to_the_coverers_own_location(self):
        location, _ = self._cover(
            None,
            SimpleNamespace(name="SA-2", shift_type="Branch Closing", shift_location="Dokki"),
        )
        self.assertEqual(location, "Dokki")

    def test_neither_known_leaves_it_to_the_schedule_fallback(self):
        location, _ = self._cover(None, None)
        self.assertIsNone(location)


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------


class TestAsFlag(unittest.TestCase):
    """Dio posts form data: "false" is a non-empty string, and must still be False."""

    def test_values(self):
        world = World()
        with world.active():
            for value in (True, 1, "1", "true", "True", "yes", "on"):
                self.assertTrue(svc.as_flag(value), value)
            for value in (False, 0, None, "", "0", "false", "False", "no", "off"):
                self.assertFalse(svc.as_flag(value), value)
            with self.assertRaises(frappe.ValidationError):
                svc.as_flag("maybe")


class TestCacheInvalidation(unittest.TestCase):
    def test_only_the_changed_users_branch_cache_is_dropped(self):
        cache = {"pos_profiles::ali@jarz.test": ["Dokki"], "pos_profiles::bob@jarz.test": ["Dokki"]}
        with patch.object(svc, "frappe"), patch.object(
            svc.access_control, "_request_cache", return_value=cache
        ):
            svc._invalidate("ali@jarz.test", "Dokki")
        self.assertEqual(cache, {"pos_profiles::bob@jarz.test": ["Dokki"]})


class TestWindow(unittest.TestCase):
    def test_access_runs_midnight_to_three_the_next_morning(self):
        starts_at, expires_at = svc.window_for(date(2026, 9, 30))
        self.assertEqual(starts_at, datetime(2026, 9, 30, 0, 0))
        self.assertEqual(expires_at, datetime(2026, 10, 1, 3, 0))


if __name__ == "__main__":
    unittest.main()

"""Tests for branch (POS Profile) scoping and shift enforcement.

These cover the guarantees the Kanban board and the manager feed both depend on:

* one branch resolver, so the board and the manager feed cannot disagree about
  which branches a user owns;
* branch scoping applied to *writes*, not just reads;
* an open shift required before money or stock moves.
"""

import unittest
from unittest.mock import patch

import frappe

from jarz_pos.utils import access_control
from jarz_pos.utils.access_control import (
    BranchAccessError,
    ShiftRequiredError,
    ensure_open_shift,
    ensure_profile_scoped_invoice_access,
    get_invoice_branch,
    get_user_pos_profiles,
    is_unrestricted_user,
)


def _clear_request_cache():
    """Drop the per-request memo so each test resolves fresh."""
    try:
        frappe.local.jarz_access_cache = {}
    except Exception:
        pass


class TestBranchResolution(unittest.TestCase):
    def setUp(self):
        _clear_request_cache()

    def tearDown(self):
        _clear_request_cache()

    def test_administrator_is_unrestricted(self):
        self.assertTrue(is_unrestricted_user("Administrator"))

    def test_manager_roles_are_not_unrestricted(self):
        """A manager is scoped like anyone else — being a manager is not a branch."""
        for user in ("manager@example.com", "sysmanager@example.com"):
            self.assertFalse(is_unrestricted_user(user))

    def test_guest_has_no_branches(self):
        self.assertEqual(get_user_pos_profiles("Guest"), [])

    def test_profiles_come_from_pos_profile_user_links(self):
        with patch.object(access_control, "is_unrestricted_user", return_value=False), patch.object(
            frappe, "get_all"
        ) as mock_get_all:
            mock_get_all.side_effect = [["Branch A", "Branch B"], ["Branch A", "Branch B"]]
            profiles = get_user_pos_profiles("staff@example.com")

        self.assertEqual(profiles, ["Branch A", "Branch B"])

    def test_unlinked_user_gets_no_branches(self):
        with patch.object(access_control, "is_unrestricted_user", return_value=False), patch.object(
            frappe, "get_all", return_value=[]
        ):
            self.assertEqual(get_user_pos_profiles("nobody@example.com"), [])

    def test_lookup_failure_does_not_masquerade_as_no_branches_silently(self):
        """A DB error still returns [] but must be logged, never swallowed."""
        with patch.object(access_control, "is_unrestricted_user", return_value=False), patch.object(
            frappe, "get_all", side_effect=Exception("db down")
        ), patch.object(frappe, "log_error") as mock_log:
            self.assertEqual(get_user_pos_profiles("staff@example.com"), [])
            self.assertTrue(mock_log.called)


class TestBranchRecipients(unittest.TestCase):
    def test_disabled_branches_resolve_no_recipients(self):
        """Mirrors get_user_pos_profiles: a closed branch reaches nobody.

        Otherwise a disabled branch keeps pushing live events to users who can
        no longer load those orders.
        """
        from jarz_pos.utils.access_control import get_users_for_pos_profiles

        with patch.object(frappe, "get_all", return_value=[]) as mock_get_all:
            self.assertEqual(get_users_for_pos_profiles(["Closed Branch"]), [])
            # Only the POS Profile filter query runs; the child-table read is skipped.
            self.assertEqual(mock_get_all.call_count, 1)

    def test_enabled_branch_returns_its_users(self):
        from jarz_pos.utils.access_control import get_users_for_pos_profiles

        with patch.object(frappe, "get_all") as mock_get_all:
            mock_get_all.side_effect = [
                ["Branch A"],
                [{"user": "a@x.com"}, {"user": "b@x.com"}, {"user": "a@x.com"}],
            ]
            self.assertEqual(
                get_users_for_pos_profiles(["Branch A"]), ["a@x.com", "b@x.com"]
            )

    def test_no_profiles_is_a_no_op(self):
        from jarz_pos.utils.access_control import get_users_for_pos_profiles

        self.assertEqual(get_users_for_pos_profiles([]), [])


class TestInvoiceBranch(unittest.TestCase):
    def test_kanban_profile_wins_over_pos_profile(self):
        """pos_profile freezes at submit; a transfer only moves custom_kanban_profile."""
        inv = frappe._dict(
            {"custom_kanban_profile": "Branch B", "pos_profile": "Branch A"}
        )
        self.assertEqual(get_invoice_branch(inv), "Branch B")

    def test_falls_back_to_pos_profile(self):
        inv = frappe._dict({"custom_kanban_profile": None, "pos_profile": "Branch A"})
        self.assertEqual(get_invoice_branch(inv), "Branch A")

    def test_missing_invoice_is_blank(self):
        self.assertEqual(get_invoice_branch(None), "")


class TestProfileScopedAccess(unittest.TestCase):
    def setUp(self):
        _clear_request_cache()

    def tearDown(self):
        _clear_request_cache()

    def test_allows_own_branch(self):
        inv = frappe._dict({"custom_kanban_profile": "Branch A"})
        with patch.object(access_control, "is_unrestricted_user", return_value=False), patch.object(
            access_control, "get_user_pos_profiles", return_value=["Branch A", "Branch B"]
        ):
            ensure_profile_scoped_invoice_access(inv, action_label="testing")

    def test_blocks_other_branch(self):
        inv = frappe._dict({"custom_kanban_profile": "Branch C"})
        with patch.object(access_control, "is_unrestricted_user", return_value=False), patch.object(
            access_control, "get_user_pos_profiles", return_value=["Branch A"]
        ):
            with self.assertRaises(BranchAccessError):
                ensure_profile_scoped_invoice_access(inv, action_label="testing")

    def test_blocks_when_user_has_no_branch(self):
        inv = frappe._dict({"custom_kanban_profile": "Branch A"})
        with patch.object(access_control, "is_unrestricted_user", return_value=False), patch.object(
            access_control, "get_user_pos_profiles", return_value=[]
        ):
            with self.assertRaises(BranchAccessError):
                ensure_profile_scoped_invoice_access(inv, action_label="testing")

    def test_extra_profiles_are_checked_too(self):
        """An amendment naming a second branch must clear both."""
        inv = frappe._dict({"custom_kanban_profile": "Branch A"})
        with patch.object(access_control, "is_unrestricted_user", return_value=False), patch.object(
            access_control, "get_user_pos_profiles", return_value=["Branch A"]
        ):
            with self.assertRaises(BranchAccessError):
                ensure_profile_scoped_invoice_access(
                    inv, action_label="testing", extra_profiles=["Branch Z"]
                )

    def test_administrator_bypasses(self):
        inv = frappe._dict({"custom_kanban_profile": "Branch C"})
        with patch.object(access_control, "is_unrestricted_user", return_value=True):
            ensure_profile_scoped_invoice_access(inv, action_label="testing")


class TestShiftEnforcement(unittest.TestCase):
    def setUp(self):
        _clear_request_cache()

    def tearDown(self):
        _clear_request_cache()

    def test_user_without_the_flag_is_not_gated(self):
        with patch.object(access_control, "user_requires_pos_shift", return_value=False), patch.object(
            access_control, "get_open_shift_for_profile"
        ) as mock_lookup:
            ensure_open_shift("Branch A", action_label="testing")
            self.assertFalse(mock_lookup.called)

    def test_open_shift_allows_the_action(self):
        with patch.object(access_control, "user_requires_pos_shift", return_value=True), patch.object(
            access_control,
            "get_open_shift_for_profile",
            return_value={"name": "POS-OPE-001", "user": "someone@example.com"},
        ):
            ensure_open_shift("Branch A", action_label="testing")

    def test_missing_shift_blocks_the_action(self):
        with patch.object(access_control, "user_requires_pos_shift", return_value=True), patch.object(
            access_control, "get_open_shift_for_profile", return_value=None
        ):
            with self.assertRaises(ShiftRequiredError):
                ensure_open_shift("Branch A", action_label="testing")

    def test_a_colleagues_shift_counts(self):
        """The question is whether the branch is open, not who opened it.

        A branch holds one shift at a time, so requiring the caller to be the
        opener would lock out every other staff member working that branch.
        """
        with patch.object(access_control, "user_requires_pos_shift", return_value=True), patch.object(
            access_control,
            "get_open_shift_for_profile",
            return_value={"name": "POS-OPE-002", "user": "colleague@example.com"},
        ):
            # The shift is owned by somebody else and the call still goes through.
            ensure_open_shift("Branch A", action_label="testing")

    def test_blank_profile_is_a_no_op(self):
        with patch.object(access_control, "user_requires_pos_shift", return_value=True):
            ensure_open_shift("", action_label="testing")


if __name__ == "__main__":
    unittest.main()

"""Tests for branch-scoped realtime publishing.

Frappe turns ``user`` into the room ``user:<value>``. There is no wildcard, so
``user="*"`` addressed a room nobody joins and every one of those events was
thrown away; passing a *list* produced an equally dead ``user:['a@b.com']``.
Meanwhile a bare ``publish_realtime(event, payload)`` fell through to the
site-wide ``all`` room and reached every System User on every branch.

These tests pin the replacement: one emit per assigned user of the owning branch.
"""

import unittest
from unittest.mock import patch

import frappe

from jarz_pos.utils import realtime
from jarz_pos.utils.realtime import (
    publish_invoice_event,
    publish_to_branches,
    resolve_branch_recipients,
)


class TestRecipientResolution(unittest.TestCase):
    def test_recipients_come_from_the_branch(self):
        with patch.object(
            realtime, "get_users_for_pos_profiles", return_value=["a@x.com", "b@x.com"]
        ):
            self.assertEqual(
                resolve_branch_recipients(["Branch A"]), ["a@x.com", "b@x.com"]
            )

    def test_extra_users_are_merged_and_deduped(self):
        with patch.object(
            realtime, "get_users_for_pos_profiles", return_value=["a@x.com"]
        ):
            self.assertEqual(
                resolve_branch_recipients(["Branch A"], extra_users=["a@x.com", "c@x.com"]),
                ["a@x.com", "c@x.com"],
            )

    def test_guest_is_never_a_recipient(self):
        with patch.object(
            realtime, "get_users_for_pos_profiles", return_value=["Guest", "a@x.com"]
        ):
            self.assertEqual(resolve_branch_recipients(["Branch A"]), ["a@x.com"])


class TestPublishToBranches(unittest.TestCase):
    def test_emits_once_per_user_never_to_a_wildcard(self):
        with patch.object(
            realtime, "get_users_for_pos_profiles", return_value=["a@x.com", "b@x.com"]
        ), patch.object(frappe, "publish_realtime") as mock_publish:
            recipients = publish_to_branches("evt", {"x": 1}, ["Branch A"])

        self.assertEqual(recipients, ["a@x.com", "b@x.com"])
        self.assertEqual(mock_publish.call_count, 2)
        sent_to = {call.kwargs["user"] for call in mock_publish.call_args_list}
        self.assertEqual(sent_to, {"a@x.com", "b@x.com"})
        self.assertNotIn("*", sent_to)

    def test_drops_and_logs_when_branch_has_no_users(self):
        """Falling back to a site-wide broadcast would leak the order instead."""
        with patch.object(
            realtime, "get_users_for_pos_profiles", return_value=[]
        ), patch.object(frappe, "publish_realtime") as mock_publish:
            recipients = publish_to_branches("evt", {"x": 1}, ["Branch A"])

        self.assertEqual(recipients, [])
        self.assertFalse(mock_publish.called)

    def test_one_bad_recipient_does_not_stop_the_rest(self):
        def flaky(event, payload, user=None, after_commit=False):
            if user == "a@x.com":
                raise Exception("socket down")

        with patch.object(
            realtime, "get_users_for_pos_profiles", return_value=["a@x.com", "b@x.com"]
        ), patch.object(frappe, "publish_realtime", side_effect=flaky) as mock_publish, patch.object(
            frappe, "log_error"
        ):
            publish_to_branches("evt", {"x": 1}, ["Branch A"])

        self.assertEqual(mock_publish.call_count, 2)


class TestPublishInvoiceEvent(unittest.TestCase):
    def test_uses_the_operational_branch(self):
        inv = frappe._dict({"custom_kanban_profile": "Branch B", "pos_profile": "Branch A"})
        with patch.object(realtime, "publish_to_branches", return_value=[]) as mock_publish:
            publish_invoice_event("evt", {}, inv)

        self.assertEqual(mock_publish.call_args.args[2], ["Branch B"])

    def test_transfer_reaches_both_sides(self):
        """The losing branch has to drop the card; the gaining branch has to show it."""
        inv = frappe._dict({"custom_kanban_profile": "Branch B"})
        with patch.object(realtime, "publish_to_branches", return_value=[]) as mock_publish:
            publish_invoice_event("evt", {}, inv, extra_profiles=["Branch A"])

        self.assertEqual(mock_publish.call_args.args[2], ["Branch B", "Branch A"])


if __name__ == "__main__":
    unittest.main()

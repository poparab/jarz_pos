"""Tests for CRM follow-up reminders + the completion loop (pure mock / unittest).

Two surfaces, previously with ZERO coverage:

  1. ``crm/follow_ups.py::run_followup_reminders`` and its three passes — lead
     follow-ups (due & not done), stalled-opportunity 7-day cutoff, and re-engagement —
     asserted via mocked ``frappe.get_all`` / ``_ensure_todo``.
  2. ``api/crm.py`` date capture: ``advance_stage(follow_up_date=...)`` stamping and the
     new ``complete_followup`` endpoint that closes the loop (stops daily regeneration).

Everything is mocked at the module boundary — no FrappeTestCase / DB writes.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from jarz_pos.crm import follow_ups as fu
from jarz_pos.api import crm


_TODAY = "2026-07-11"


# ---------------------------------------------------------------------------
# follow_ups.py — scheduled passes
# ---------------------------------------------------------------------------
class TestLeadFollowupPass(unittest.TestCase):
    """Pass 1: leads due today AND not marked done are reminded."""

    def test_filters_due_and_not_done(self):
        captured = {}

        def _get_all(doctype, filters=None, fields=None, **kw):
            captured["filters"] = filters
            return [{"name": "L-1", "owner": "rep@x.com", "lead_name": "Acme"}]

        summary = {"lead_followups": 0, "stalled_opps": 0, "reengagement": 0}
        with patch.object(fu, "_doctype_exists", return_value=True), patch.object(
            fu, "_has_field", return_value=True
        ), patch.object(fu, "_today", return_value=_TODAY), patch.object(
            fu.frappe, "get_all", side_effect=_get_all
        ), patch.object(
            fu, "_ensure_todo", return_value="TODO-1"
        ), patch.object(fu, "_notify"):
            fu._pass_lead_followups(summary)

        # The lead pass must filter on due date (<= today) AND not-done.
        self.assertEqual(captured["filters"]["custom_next_followup_date"], ["<=", _TODAY])
        self.assertEqual(captured["filters"]["custom_followup_done"], 0)
        self.assertEqual(summary["lead_followups"], 1)

    def test_no_todo_created_does_not_increment(self):
        summary = {"lead_followups": 0, "stalled_opps": 0, "reengagement": 0}
        with patch.object(fu, "_doctype_exists", return_value=True), patch.object(
            fu, "_has_field", return_value=True
        ), patch.object(fu, "_today", return_value=_TODAY), patch.object(
            fu.frappe, "get_all", return_value=[{"name": "L-1", "owner": "r@x"}]
        ), patch.object(
            fu, "_ensure_todo", return_value=None  # dedup: an open ToDo already exists
        ), patch.object(fu, "_notify"):
            fu._pass_lead_followups(summary)
        self.assertEqual(summary["lead_followups"], 0)


class TestStalledOpportunityPass(unittest.TestCase):
    """Pass 2: open Opportunities untouched for > 7 days."""

    def test_seven_day_cutoff_filter(self):
        captured = {}

        def _get_all(doctype, filters=None, fields=None, **kw):
            captured["filters"] = filters
            return [{"name": "O-1", "owner": "rep@x.com", "party_name": "Acme"}]

        summary = {"lead_followups": 0, "stalled_opps": 0, "reengagement": 0}
        with patch.object(fu, "_doctype_exists", return_value=True), patch.object(
            fu, "_has_field", return_value=True
        ), patch.object(fu, "_today", return_value=_TODAY), patch.object(
            fu, "_add_days", return_value="2026-07-04"
        ), patch.object(fu.frappe, "get_all", side_effect=_get_all), patch.object(
            fu, "_ensure_todo", return_value="TODO-1"
        ), patch.object(fu, "_notify"):
            fu._pass_stalled_opportunities(summary)

        self.assertEqual(captured["filters"]["status"], "Open")
        self.assertEqual(captured["filters"]["modified"], ["<", "2026-07-04"])
        self.assertEqual(summary["stalled_opps"], 1)


class TestReengagementPass(unittest.TestCase):
    """Pass 3: lost Leads/Opportunities with a re-engage date due today."""

    def test_lost_lead_and_opp_counted(self):
        def _get_all(doctype, filters=None, fields=None, **kw):
            if doctype == "Lead":
                return [{"name": "L-1", "owner": "r@x"}]
            if doctype == "Opportunity":
                return [{"name": "O-1", "owner": "r@x"}]
            return []

        summary = {"lead_followups": 0, "stalled_opps": 0, "reengagement": 0}
        with patch.object(fu, "_doctype_exists", return_value=True), patch.object(
            fu, "_has_field", return_value=True
        ), patch.object(fu, "_today", return_value=_TODAY), patch.object(
            fu.frappe, "get_all", side_effect=_get_all
        ), patch.object(fu, "_ensure_todo", return_value="TODO-1"):
            fu._pass_reengagement(summary)
        self.assertEqual(summary["reengagement"], 2)


class TestRunFollowupReminders(unittest.TestCase):
    """The orchestrator never raises and returns a numeric summary."""

    def test_empty_site_returns_zero_summary(self):
        with patch.object(fu.frappe, "get_all", return_value=[]), patch.object(
            fu, "_doctype_exists", return_value=True
        ), patch.object(fu, "_has_field", return_value=True), patch.object(
            fu, "_today", return_value=_TODAY
        ), patch.object(fu, "_add_days", return_value="2026-07-04"), patch.object(
            fu.frappe.db, "commit"
        ):
            out = fu.run_followup_reminders()
        self.assertEqual(out, {"lead_followups": 0, "stalled_opps": 0, "reengagement": 0})

    def test_never_raises_when_a_pass_explodes(self):
        with patch.object(
            fu, "_pass_lead_followups", side_effect=RuntimeError("boom")
        ), patch.object(fu, "_pass_stalled_opportunities"), patch.object(
            fu, "_pass_reengagement"
        ), patch.object(fu.frappe.db, "commit"):
            # Must swallow the error and still return the summary dict.
            out = fu.run_followup_reminders()
        self.assertIn("lead_followups", out)


# ---------------------------------------------------------------------------
# api/crm.py — date capture on advance_stage + complete_followup
# ---------------------------------------------------------------------------
@contextmanager
def _crm_env(roles):
    with patch.object(crm.frappe, "get_roles", return_value=list(roles)), patch.object(
        crm, "_doctype_exists", return_value=True
    ), patch.object(crm, "_has_field", return_value=True), patch.object(
        crm.frappe.db, "exists", return_value=True
    ), patch.object(crm, "_stage_options", return_value=list(crm.B2B_STAGES)):
        yield


class TestAdvanceStageFollowUpDate(unittest.TestCase):
    """advance_stage stamps the follow-up date and reopens the loop when given one."""

    def test_explicit_date_stamps_and_resets_done(self):
        calls = []
        with _crm_env(["B2B Sales Rep"]):
            with patch.object(
                crm.frappe.db, "set_value", side_effect=lambda *a, **k: calls.append(a)
            ), patch.object(crm, "_schedule_reengage") as mock_reengage:
                out = crm.advance_stage("Lead", "L-1", "Qualify", follow_up_date="2026-08-01")
        fields = {c[2]: c[3] for c in calls if len(c) >= 4}
        self.assertEqual(fields["custom_b2b_stage"], "Qualify")
        self.assertEqual(fields["custom_next_followup_date"], "2026-08-01")
        self.assertEqual(fields["custom_followup_done"], 0)
        mock_reengage.assert_not_called()  # explicit date path, not the +14 default
        self.assertEqual(out["stage"], "Qualify")

    def test_lost_without_date_uses_reengage_default(self):
        with _crm_env(["B2B Sales Rep"]):
            with patch.object(crm.frappe.db, "set_value"), patch.object(
                crm, "_schedule_reengage"
            ) as mock_reengage:
                crm.advance_stage("Lead", "L-1", crm._LOST_STAGE)
        mock_reengage.assert_called_once()  # legacy +14 re-engage path preserved

    def test_lost_with_explicit_date_overrides_reengage(self):
        with _crm_env(["B2B Sales Rep"]):
            with patch.object(crm.frappe.db, "set_value"), patch.object(
                crm, "_schedule_reengage"
            ) as mock_reengage, patch.object(crm, "_stamp_followup_date") as mock_stamp:
                crm.advance_stage("Lead", "L-1", crm._LOST_STAGE, follow_up_date="2026-08-01")
        mock_stamp.assert_called_once()
        mock_reengage.assert_not_called()


class TestCompleteFollowup(unittest.TestCase):
    """complete_followup marks done, clears the date and closes open ToDos."""

    def test_manager_completes(self):
        calls = []
        with patch.object(crm.frappe, "get_roles", return_value=["JARZ Manager"]), patch.object(
            crm, "_doctype_exists", return_value=True
        ), patch.object(crm, "_has_field", return_value=True), patch.object(
            crm.frappe.db, "exists", return_value=True
        ), patch.object(
            crm.frappe.db, "set_value", side_effect=lambda *a, **k: calls.append(a)
        ), patch.object(crm, "_close_open_todos") as mock_close:
            out = crm.complete_followup("Lead", "L-1")
        fields = {c[2]: c[3] for c in calls if len(c) >= 4}
        self.assertEqual(fields["custom_followup_done"], 1)
        self.assertIsNone(fields["custom_next_followup_date"])
        mock_close.assert_called_once_with("Lead", "L-1")
        self.assertEqual(out, {"ok": True})

    def test_owner_rep_can_complete(self):
        with patch.object(crm.frappe, "get_roles", return_value=["B2B Sales Rep"]), patch.object(
            crm.frappe.session, "user", "rep@x.com", create=True
        ), patch.object(crm.frappe.db, "get_value", return_value="rep@x.com"):
            self.assertTrue(crm._can_complete_followup("Lead", "L-1"))

    def test_non_owner_rep_denied(self):
        with patch.object(crm.frappe, "get_roles", return_value=["B2B Sales Rep"]), patch.object(
            crm.frappe.session, "user", "other@x.com", create=True
        ), patch.object(crm.frappe.db, "get_value", return_value="rep@x.com"), patch.object(
            crm.frappe.db, "exists", return_value=False
        ):
            self.assertFalse(crm._can_complete_followup("Lead", "L-1"))

    def test_plain_user_rejected_by_gate(self):
        with patch.object(crm.frappe, "get_roles", return_value=["Sales User"]):
            with self.assertRaises(Exception):
                crm.complete_followup("Lead", "L-1")


if __name__ == "__main__":
    unittest.main()

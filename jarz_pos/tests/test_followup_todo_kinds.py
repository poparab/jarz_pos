"""Reminder-ToDo identity tests (light-DB / unittest).

Guards the fix for the bug that made every CRM follow-up reminder a no-op on a
real site: ``_ensure_todo`` used to skip creation when ANY open ToDo referenced
the record, and this site's "Jarz Lead Round Robin" Assignment Rule opens one on
every Lead the instant it is created. So the reminder always found an "existing"
ToDo and quietly created nothing.

THE POINT OF THIS MODULE is the unrelated-ToDo case. A fresh CI site has no
Assignment Rule, so the old behaviour looked correct there and the bug only
showed on staging/production. Every case below therefore plants its own
unrelated open ToDo rather than trusting the site's configuration — which also
means these tests are meaningful on any site, CI included.

Why plain ``unittest.TestCase`` (not FrappeTestCase): same reason as
``test_leads_api`` -- on ERPNext v16 FrappeTestCase imports ``erpnext.tests.utils``
whose module-level BootStrapTestData() collides with the populated CI ``frontend``
clone. Docs are inserted on the live connection (uncommitted) and rolled back in
tearDown, so the module is non-destructive.
"""

from __future__ import annotations

import unittest

import frappe

from jarz_pos.api import crm as crm_api
from jarz_pos.crm import follow_ups as fu

_ASSIGNMENT_DESCRIPTION = "Automatic Assignment"


def _make_lead(lead_name="_TEST Reminder Lead"):
    """A bare Lead, inserted directly (no catalog fields needed here)."""
    doc = frappe.get_doc({"doctype": "Lead", "lead_name": lead_name})
    doc.insert(ignore_permissions=True)
    return doc.name


def _plant_assignment_todo(lead):
    """The ToDo an Assignment Rule opens on every Lead. The saboteur."""
    doc = frappe.get_doc(
        {
            "doctype": "ToDo",
            "description": _ASSIGNMENT_DESCRIPTION,
            "reference_type": "Lead",
            "reference_name": lead,
            "status": "Open",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _todos(lead, status="Open", fields=None):
    return frappe.get_all(
        "ToDo",
        filters={
            "reference_type": "Lead",
            "reference_name": lead,
            "status": status,
        },
        fields=fields or ["name", "description", "date", "status"],
    )


def _todos_of_kind(lead, kind, status="Open"):
    marker = fu.todo_marker(kind)
    return [t for t in _todos(lead, status=status) if marker in (t["description"] or "")]


def _open_jarz(lead):
    """Open ToDos this app owns (tagged ``[jarz:``)."""
    return [t for t in _todos(lead) if "[jarz:" in (t["description"] or "")]


def _open_non_jarz(lead):
    """Open ToDos this app does NOT own -- ours to leave strictly alone.

    On a site with the Assignment Rule enabled this holds the rule's own ToDo
    as well as anything a test planted, which is exactly why no assertion here
    may use a raw count of the record's ToDos.
    """
    return [t for t in _todos(lead) if "[jarz:" not in (t["description"] or "")]


def _days(offset):
    from frappe.utils import add_days, today

    return add_days(today(), offset)


class ReminderTestCase(unittest.TestCase):
    def tearDown(self):
        frappe.db.rollback()


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------
class TestReminderSurvivesUnrelatedTodo(ReminderTestCase):
    def test_an_assignment_todo_no_longer_suppresses_the_reminder(self):
        lead = _make_lead()
        _plant_assignment_todo(lead)

        created = fu._ensure_todo(
            "Lead",
            lead,
            frappe.session.user,
            "Follow up with lead Acme",
            date=_days(0),
            kind=fu.KIND_LEAD_FOLLOWUP,
        )

        self.assertIsNotNone(
            created,
            "the reminder must be created even though an unrelated open ToDo "
            "already references the lead",
        )
        mine = _todos_of_kind(lead, fu.KIND_LEAD_FOLLOWUP)
        self.assertEqual(len(mine), 1)
        self.assertIn("Follow up with lead Acme", mine[0]["description"])

    def test_the_unrelated_todo_is_left_alone(self):
        lead = _make_lead()
        assignment = _plant_assignment_todo(lead)

        fu._ensure_todo(
            "Lead",
            lead,
            frappe.session.user,
            "Follow up",
            date=_days(0),
            kind=fu.KIND_LEAD_FOLLOWUP,
        )

        self.assertEqual(frappe.db.get_value("ToDo", assignment, "status"), "Open")
        self.assertEqual(
            frappe.db.get_value("ToDo", assignment, "description"),
            _ASSIGNMENT_DESCRIPTION,
        )


# ---------------------------------------------------------------------------
# Identity, dedup and re-dating
# ---------------------------------------------------------------------------
class TestReminderIdentity(ReminderTestCase):
    def test_the_same_kind_twice_re_dates_instead_of_duplicating(self):
        lead = _make_lead()
        first = fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Follow up",
            date=_days(0), kind=fu.KIND_LEAD_FOLLOWUP,
        )
        second = fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Follow up again",
            date=_days(5), kind=fu.KIND_LEAD_FOLLOWUP,
        )

        self.assertEqual(first, second, "the same reminder should be reused")
        mine = _todos_of_kind(lead, fu.KIND_LEAD_FOLLOWUP)
        self.assertEqual(len(mine), 1)
        # The quieter half of the old bug: a moved follow-up date used to leave
        # the reminder sitting on its original date forever.
        self.assertEqual(str(mine[0]["date"]), _days(5))
        self.assertIn("Follow up again", mine[0]["description"])

    def test_different_kinds_coexist_on_one_record(self):
        lead = _make_lead()
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Follow up",
            date=_days(0), kind=fu.KIND_LEAD_FOLLOWUP,
        )
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Re-engage",
            date=_days(14), kind=fu.KIND_REENGAGE,
        )

        self.assertEqual(len(_todos_of_kind(lead, fu.KIND_LEAD_FOLLOWUP)), 1)
        self.assertEqual(len(_todos_of_kind(lead, fu.KIND_REENGAGE)), 1)

    def test_the_description_carries_the_kind_tag(self):
        lead = _make_lead()
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Follow up",
            date=_days(0), kind=fu.KIND_LEAD_FOLLOWUP,
        )
        # _open_jarz, not _todos[0]: on a site with the Assignment Rule the
        # record's first ToDo may well be the rule's, not ours.
        ours = _open_jarz(lead)
        self.assertEqual(len(ours), 1)
        description = ours[0]["description"]
        self.assertIn(fu.todo_marker(fu.KIND_LEAD_FOLLOWUP), description)
        self.assertTrue(description.startswith("[jarz:"))

    def test_kind_is_required(self):
        """The guard against reintroducing the bug: no kind, no call."""
        lead = _make_lead()
        with self.assertRaises(TypeError):
            fu._ensure_todo("Lead", lead, frappe.session.user, "Follow up")

    def test_a_missing_reference_creates_nothing(self):
        self.assertIsNone(
            fu._ensure_todo(
                "Lead", None, frappe.session.user, "Follow up",
                kind=fu.KIND_LEAD_FOLLOWUP,
            )
        )


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------
class TestReminderClosing(ReminderTestCase):
    def test_close_todos_of_kind_closes_only_that_kind(self):
        lead = _make_lead()
        _plant_assignment_todo(lead)
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Follow up",
            date=_days(0), kind=fu.KIND_LEAD_FOLLOWUP,
        )
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Re-engage",
            date=_days(14), kind=fu.KIND_REENGAGE,
        )
        # Never assert on a RAW count of the record's ToDos: on a site with the
        # Assignment Rule enabled the Lead insert opens one of its own, so a
        # total asserts on site configuration rather than on this code. Snapshot
        # what we do not own and require it to be untouched.
        untouched = sorted(t["name"] for t in _open_non_jarz(lead))

        closed = fu.close_todos_of_kind("Lead", lead, fu.KIND_LEAD_FOLLOWUP)

        self.assertEqual(closed, 1)
        self.assertEqual(len(_todos_of_kind(lead, fu.KIND_LEAD_FOLLOWUP)), 0)
        self.assertEqual(len(_todos_of_kind(lead, fu.KIND_REENGAGE)), 1)
        self.assertEqual(sorted(t["name"] for t in _open_non_jarz(lead)), untouched)

    def test_close_all_jarz_todos_spares_the_assignment(self):
        lead = _make_lead()
        assignment = _plant_assignment_todo(lead)
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Follow up",
            date=_days(0), kind=fu.KIND_LEAD_FOLLOWUP,
        )
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Re-engage",
            date=_days(14), kind=fu.KIND_REENGAGE,
        )

        untouched = sorted(t["name"] for t in _open_non_jarz(lead))

        closed = fu.close_all_jarz_todos("Lead", lead)

        self.assertEqual(closed, 2)
        self.assertEqual(frappe.db.get_value("ToDo", assignment, "status"), "Open")
        # Every jarz reminder gone...
        self.assertEqual(len(_open_jarz(lead)), 0)
        # ...and every ToDo this app does not own still open, whether that is
        # just our planted one or also the Assignment Rule's.
        self.assertEqual(sorted(t["name"] for t in _open_non_jarz(lead)), untouched)
        self.assertIn(
            _ASSIGNMENT_DESCRIPTION,
            [t["description"] for t in _open_non_jarz(lead)],
        )


class TestCompleteFollowupScope(ReminderTestCase):
    """The mirror of the same confusion, on the interactive path.

    ``complete_followup`` used to close EVERY open ToDo on the record, so a rep
    marking their follow-up done also closed the Assignment Rule's ToDo and
    silently un-assigned themselves from the lead.
    """

    def test_completing_a_followup_does_not_unassign_the_rep(self):
        lead = _make_lead()
        assignment = _plant_assignment_todo(lead)
        fu._ensure_todo(
            "Lead", lead, frappe.session.user, "Follow up",
            date=_days(0), kind=fu.KIND_LEAD_FOLLOWUP,
        )

        crm_api.complete_followup("Lead", lead)

        self.assertEqual(
            frappe.db.get_value("ToDo", assignment, "status"),
            "Open",
            "the assignment ToDo is not this app's to close",
        )
        self.assertEqual(len(_todos_of_kind(lead, fu.KIND_LEAD_FOLLOWUP)), 0)

    def test_completing_a_followup_still_clears_the_followup_fields(self):
        lead = _make_lead()
        frappe.db.set_value(
            "Lead", lead, "custom_next_followup_date", _days(3), update_modified=False
        )
        frappe.db.set_value(
            "Lead", lead, "custom_followup_done", 0, update_modified=False
        )

        crm_api.complete_followup("Lead", lead)

        row = frappe.db.get_value(
            "Lead",
            lead,
            ["custom_next_followup_date", "custom_followup_done"],
            as_dict=True,
        )
        self.assertEqual(int(row.custom_followup_done or 0), 1)
        self.assertIsNone(row.custom_next_followup_date)

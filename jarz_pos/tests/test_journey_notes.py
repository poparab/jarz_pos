"""Journey notes tests (light-DB / unittest).

Exercises ``jarz_pos.api.journey`` -- the rep's dated field diary -- plus the
enrichment it adds to the three surfaces that render it: the B2B pipeline board
(``crm.get_b2b_pipeline``), the account detail (``crm.get_account``) and the
lead catalog/detail (``leads.get_leads`` / ``leads.get_lead``).

Why plain ``unittest.TestCase`` (not FrappeTestCase): identical to
``test_leads_api`` -- on ERPNext v16 FrappeTestCase imports ``erpnext.tests.utils``
whose module-level BootStrapTestData() collides with the populated CI ``frontend``
clone. Docs are inserted on the live connection (uncommitted, visible on the same
connection) and rolled back in tearDown, so the module is non-destructive.

IMPORTANT -- pre-migrate skip. CI's logic gate runs the suite against staging
WITHOUT running ``bench migrate`` first, so on the very first run after this
lands the ``Jarz Journey Note`` DocType does not exist yet and every case here
would fail on a missing table. :func:`_require_journey` skips the module in that
window instead. Once staging has deployed (and migrated) the cases run for real
-- so a green run on the landing commit proves nothing, and the suite must be
re-run post-deploy.
"""

from __future__ import annotations

import unittest

import frappe

from jarz_pos.api import crm as crm_api
from jarz_pos.api import journey as journey_api
from jarz_pos.api import leads as leads_api

_B2B_ROLE = "B2B Sales Rep"
_COFFEE = "Coffee"


def _journey_installed():
    """Whether the site has migrated the journey DocType."""
    try:
        return bool(frappe.db.exists("DocType", journey_api.JOURNEY_DOCTYPE))
    except Exception:
        return False


def _ensure_b2b_role():
    if not frappe.db.exists("Role", _B2B_ROLE):
        frappe.get_doc(
            {"doctype": "Role", "role_name": _B2B_ROLE, "desk_access": 1, "disabled": 0}
        ).insert(ignore_permissions=True)


def _ensure_category(name=_COFFEE):
    if not frappe.db.exists("Jarz Lead Category", name):
        frappe.get_doc(
            {"doctype": "Jarz Lead Category", "category_name": name}
        ).insert(ignore_permissions=True)


def _make_lead(lead_name="_TEST Journey Cafe"):
    """Insert a catalog Lead through the normal endpoint. Returns its name."""
    out = leads_api.save_lead({"lead_name": lead_name, "category": _COFFEE})
    return out["name"]


def _days(offset):
    """ISO date ``offset`` days from today."""
    from frappe.utils import add_days, today

    return add_days(today(), offset)


class JourneyTestCase(unittest.TestCase):
    """Shared setUp/tearDown: fixtures present, every write rolled back."""

    def setUp(self):
        if not _journey_installed():
            self.skipTest(
                f"{journey_api.JOURNEY_DOCTYPE} not installed on this site yet "
                "(pre-migrate run); re-run after the staging deploy."
            )
        _ensure_b2b_role()
        _ensure_category()

    def tearDown(self):
        frappe.db.rollback()


# ---------------------------------------------------------------------------
# add / get / update / delete
# ---------------------------------------------------------------------------
class TestJourneyCrud(JourneyTestCase):
    def test_add_note_maps_every_field(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Dropped 3 sample jars. Barista liked the matcha.",
            entry_date=_days(0),
            entry_type="Visit",
            contact_person="Mostafa",
            contact_role="Branch Manager",
            contact_phone="0100000009",
            next_action="Call to confirm the trial order",
            next_action_date=_days(3),
            outcome="Interested",
        )

        self.assertTrue(note["name"])
        self.assertEqual(note["reference_doctype"], "Lead")
        self.assertEqual(note["reference_name"], lead)
        self.assertEqual(note["entry_date"], _days(0))
        self.assertEqual(note["entry_type"], "Visit")
        self.assertIn("matcha", note["note"])
        self.assertEqual(note["contact_person"], "Mostafa")
        self.assertEqual(note["contact_role"], "Branch Manager")
        self.assertEqual(note["contact_phone"], "0100000009")
        self.assertEqual(note["next_action"], "Call to confirm the trial order")
        self.assertEqual(note["next_action_date"], _days(3))
        self.assertEqual(note["outcome"], "Interested")
        # Authorship is stamped server-side, never trusted from the client.
        self.assertEqual(note["logged_by"], frappe.session.user)
        self.assertTrue(note["logged_by_name"])
        self.assertTrue(note["can_edit"])

    def test_add_note_defaults_date_and_type(self):
        from frappe.utils import today

        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead, note="Quick call"
        )
        self.assertEqual(note["entry_date"], today())
        self.assertEqual(note["entry_type"], "Visit")

    def test_add_note_requires_a_body(self):
        lead = _make_lead()
        with self.assertRaises(Exception):
            journey_api.add_journey_note(
                reference_doctype="Lead", reference_name=lead, note="   "
            )

    def test_add_note_rejects_unknown_reference(self):
        with self.assertRaises(Exception):
            journey_api.add_journey_note(
                reference_doctype="Lead",
                reference_name="_TEST does not exist",
                note="hi",
            )

    def test_add_note_rejects_unsupported_doctype(self):
        lead = _make_lead()
        with self.assertRaises(Exception):
            journey_api.add_journey_note(
                reference_doctype="Sales Invoice", reference_name=lead, note="hi"
            )

    def test_get_notes_returns_newest_touch_first(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="First visit",
            entry_date=_days(-10),
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Follow-up call",
            entry_date=_days(-2),
        )

        out = journey_api.get_journey_notes("Lead", lead)
        self.assertEqual(out["count"], 2)
        self.assertEqual(len(out["notes"]), 2)
        self.assertEqual(out["notes"][0]["note"], "Follow-up call")
        self.assertEqual(out["notes"][1]["note"], "First visit")

    def test_get_notes_is_scoped_to_its_own_record(self):
        lead_a = _make_lead("_TEST Journey A")
        lead_b = _make_lead("_TEST Journey B")
        journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead_a, note="only on A"
        )

        self.assertEqual(journey_api.get_journey_notes("Lead", lead_b)["count"], 0)
        self.assertEqual(journey_api.get_journey_notes("Lead", lead_a)["count"], 1)

    def test_update_patches_only_supplied_keys(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Original",
            contact_person="Mostafa",
            entry_type="Visit",
        )
        updated = journey_api.update_journey_note(
            note["name"], note="Corrected after the call"
        )
        self.assertEqual(updated["note"], "Corrected after the call")
        # Untouched keys survive.
        self.assertEqual(updated["contact_person"], "Mostafa")
        self.assertEqual(updated["entry_type"], "Visit")

    def test_update_clears_a_field_with_an_empty_string(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Original",
            contact_person="Mostafa",
        )
        updated = journey_api.update_journey_note(note["name"], contact_person="")
        self.assertEqual(updated["contact_person"], "")

    def test_update_cannot_blank_the_note_body(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead, note="Original"
        )
        with self.assertRaises(Exception):
            journey_api.update_journey_note(note["name"], note="  ")

    def test_delete_removes_the_note(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead, note="Delete me"
        )
        journey_api.delete_journey_note(note["name"])
        self.assertEqual(journey_api.get_journey_notes("Lead", lead)["count"], 0)

    def test_options_endpoint_serves_the_select_lists(self):
        options = journey_api.get_journey_options()
        self.assertIn("Visit", options["entry_types"])
        self.assertIn("Call", options["entry_types"])
        self.assertIn("Interested", options["outcomes"])
        # The empty "not set" choice is the app's job, never the payload's.
        self.assertNotIn("", options["outcomes"])


# ---------------------------------------------------------------------------
# Follow-up propagation
# ---------------------------------------------------------------------------
class TestJourneyFollowup(JourneyTestCase):
    """A next-action date must drive the EXISTING reminder machinery."""

    def test_next_action_date_stamps_the_lead_followup(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Manager asked me to call back",
            next_action="Call the manager",
            next_action_date=_days(4),
        )
        row = frappe.db.get_value(
            "Lead",
            lead,
            ["custom_next_followup_date", "custom_followup_done"],
            as_dict=True,
        )
        self.assertEqual(str(row.custom_next_followup_date), _days(4))
        self.assertEqual(int(row.custom_followup_done or 0), 0)

    def test_a_note_without_a_next_action_leaves_the_followup_alone(self):
        lead = _make_lead()
        frappe.db.set_value(
            "Lead", lead, "custom_next_followup_date", _days(9), update_modified=False
        )
        journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead, note="Just saying hi"
        )
        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(9),
        )

    def test_a_later_action_never_pushes_an_earlier_reminder_out(self):
        lead = _make_lead()
        frappe.db.set_value(
            "Lead", lead, "custom_next_followup_date", _days(2), update_modified=False
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Check back at the end of the month",
            next_action_date=_days(20),
        )
        # The sooner reminder wins: a distant note must not silently delay it.
        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(2),
        )

    def test_an_earlier_action_pulls_the_reminder_forward(self):
        lead = _make_lead()
        frappe.db.set_value(
            "Lead", lead, "custom_next_followup_date", _days(20), update_modified=False
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="They want a call tomorrow",
            next_action_date=_days(1),
        )
        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(1),
        )

    def test_next_action_reopens_a_completed_followup(self):
        lead = _make_lead()
        frappe.db.set_value(
            "Lead", lead, "custom_followup_done", 1, update_modified=False
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="New thread opened",
            next_action_date=_days(5),
        )
        self.assertEqual(
            int(frappe.db.get_value("Lead", lead, "custom_followup_done") or 0), 0
        )

    def test_next_action_opens_a_todo_for_the_reminder_feed(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Ask for the owner",
            contact_person="Hany",
            next_action="Ring the owner",
            next_action_date=_days(3),
        )
        todos = frappe.get_all(
            "ToDo",
            filters={
                "reference_type": "Lead",
                "reference_name": lead,
                "status": "Open",
            },
            fields=["description", "date"],
        )
        self.assertEqual(len(todos), 1)
        self.assertEqual(str(todos[0]["date"]), _days(3))
        self.assertIn("Hany", todos[0]["description"])


# ---------------------------------------------------------------------------
# Summary folding (what a card shows)
# ---------------------------------------------------------------------------
class TestJourneySummary(JourneyTestCase):
    def test_summary_reports_the_last_touch(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Old visit",
            entry_date=_days(-30),
            entry_type="Visit",
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Recent call with the manager",
            entry_date=_days(-1),
            entry_type="Call",
            contact_person="Mostafa",
        )

        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertEqual(summary["journey_count"], 2)
        self.assertEqual(summary["last_journey_date"], _days(-1))
        self.assertEqual(summary["last_journey_type"], "Call")
        self.assertEqual(summary["last_journey_contact"], "Mostafa")
        self.assertIn("Recent call", summary["last_journey_note"])

    def test_summary_prefers_the_soonest_pending_action(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="A",
            entry_date=_days(-3),
            next_action="Far",
            next_action_date=_days(15),
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="B",
            entry_date=_days(-2),
            next_action="Near",
            next_action_date=_days(2),
        )
        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertEqual(summary["next_action_date"], _days(2))
        self.assertEqual(summary["next_action"], "Near")

    def test_summary_falls_back_to_the_latest_overdue_action(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="A",
            entry_date=_days(-30),
            next_action="Ancient",
            next_action_date=_days(-20),
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="B",
            entry_date=_days(-10),
            next_action="Overdue",
            next_action_date=_days(-5),
        )
        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertEqual(summary["next_action_date"], _days(-5))
        self.assertEqual(summary["next_action"], "Overdue")

    def test_summary_pending_beats_overdue(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="A",
            entry_date=_days(-10),
            next_action="Overdue",
            next_action_date=_days(-5),
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="B",
            entry_date=_days(-1),
            next_action="Upcoming",
            next_action_date=_days(6),
        )
        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertEqual(summary["next_action_date"], _days(6))

    def test_summary_omits_records_with_no_notes(self):
        lead = _make_lead()
        self.assertEqual(journey_api.journey_summaries("Lead", [lead]), {})

    def test_long_notes_are_truncated_in_the_summary(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="x " * 400,
        )
        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertLessEqual(len(summary["last_journey_note"]), 160)


# ---------------------------------------------------------------------------
# Surface enrichment: pipeline board / account detail / lead catalog
# ---------------------------------------------------------------------------
class TestJourneyOnSurfaces(JourneyTestCase):
    def test_pipeline_card_carries_the_journey_summary(self):
        lead = _make_lead("_TEST Journey Board")
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Visited, left samples",
            entry_date=_days(-1),
            entry_type="Sample Drop",
            next_action="Confirm the order",
            next_action_date=_days(3),
        )

        board = crm_api.get_b2b_pipeline()
        card = _find_card(board, lead)
        self.assertIsNotNone(card, "the lead should be on the board")
        self.assertEqual(card["journey_count"], 1)
        self.assertEqual(card["last_journey_date"], _days(-1))
        self.assertEqual(card["last_journey_type"], "Sample Drop")
        self.assertEqual(card["next_action_date"], _days(3))
        self.assertEqual(card["next_action"], "Confirm the order")

    def test_pipeline_card_without_notes_still_carries_the_keys(self):
        lead = _make_lead("_TEST Journey Empty Board")
        board = crm_api.get_b2b_pipeline()
        card = _find_card(board, lead)
        self.assertIsNotNone(card)
        self.assertEqual(card["journey_count"], 0)
        self.assertIsNone(card["last_journey_date"])
        self.assertIsNone(card["next_action_date"])

    def test_account_detail_returns_the_full_diary(self):
        lead = _make_lead("_TEST Journey Account")
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Owner wants a price list",
            entry_date=_days(-2),
            contact_person="Hany",
        )
        account = crm_api.get_account("Lead", lead)
        self.assertEqual(len(account["journey_notes"]), 1)
        self.assertEqual(account["journey_notes"][0]["contact_person"], "Hany")

    def test_lead_detail_returns_notes_and_summary(self):
        lead = _make_lead("_TEST Journey Detail")
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Second visit",
            entry_date=_days(-1),
            next_action_date=_days(7),
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="First visit",
            entry_date=_days(-8),
        )

        detail = leads_api.get_lead(lead)
        self.assertEqual(len(detail["journey_notes"]), 2)
        self.assertEqual(detail["journey_notes"][0]["note"], "Second visit")
        self.assertEqual(detail["journey_count"], 2)
        self.assertEqual(detail["last_journey_date"], _days(-1))
        self.assertEqual(detail["next_action_date"], _days(7))

    def test_lead_detail_without_notes_carries_empty_defaults(self):
        lead = _make_lead("_TEST Journey Detail Empty")
        detail = leads_api.get_lead(lead)
        self.assertEqual(detail["journey_notes"], [])
        self.assertEqual(detail["journey_count"], 0)
        self.assertIsNone(detail["last_journey_date"])

    def test_catalog_rows_carry_the_summary(self):
        lead = _make_lead("_TEST Journey Catalog")
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Catalog touch",
            entry_date=_days(-4),
            next_action_date=_days(5),
        )
        rows = leads_api.get_leads(category=_COFFEE)["leads"]
        row = next((r for r in rows if r["name"] == lead), None)
        self.assertIsNotNone(row, "the lead should be in the catalog")
        self.assertEqual(row["journey_count"], 1)
        self.assertEqual(row["last_journey_date"], _days(-4))
        self.assertEqual(row["next_action_date"], _days(5))


def _find_card(board, name):
    for cards in (board.get("columns") or {}).values():
        for card in cards:
            if card.get("name") == name:
                return card
    return None


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
class TestJourneyAccess(JourneyTestCase):
    """Every endpoint sits behind the same B2B gate as the rest of the CRM."""

    def test_endpoints_reject_a_user_without_b2b_access(self):
        lead = _make_lead("_TEST Journey Gate")
        note = journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead, note="gated"
        )

        original = frappe.session.user
        try:
            frappe.set_user("Guest")
            for call in (
                lambda: journey_api.get_journey_notes("Lead", lead),
                lambda: journey_api.get_journey_options(),
                lambda: journey_api.add_journey_note(
                    reference_doctype="Lead", reference_name=lead, note="nope"
                ),
                lambda: journey_api.update_journey_note(note["name"], note="nope"),
                lambda: journey_api.delete_journey_note(note["name"]),
            ):
                with self.assertRaises(Exception):
                    call()
        finally:
            frappe.set_user(original)

    def test_a_rep_may_not_edit_another_reps_note(self):
        lead = _make_lead("_TEST Journey Ownership")
        note = journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead, note="mine"
        )
        # Re-stamp authorship to somebody else, then re-check as the caller (who
        # is Administrator here, so drop to a plain B2B rep session).
        frappe.db.set_value(
            journey_api.JOURNEY_DOCTYPE,
            note["name"],
            "logged_by",
            "Guest",
            update_modified=False,
        )
        rep = _ensure_rep_user()
        original = frappe.session.user
        try:
            frappe.set_user(rep)
            with self.assertRaises(Exception):
                journey_api.update_journey_note(note["name"], note="hijacked")
        finally:
            frappe.set_user(original)


def _ensure_rep_user():
    """A non-manager User carrying only the B2B Sales Rep role."""
    email = "_test_journey_rep@example.com"
    if not frappe.db.exists("User", email):
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": "Journey Rep",
                "send_welcome_email": 0,
            }
        )
        user.insert(ignore_permissions=True)
        user.add_roles(_B2B_ROLE)
    return email

"""Journey notes tests (light-DB / unittest).

Exercises ``jarz_pos.api.journey`` -- the rep's dated field diary -- plus the
enrichment it adds to the three surfaces that render it: the B2B pipeline board
(``crm.get_b2b_pipeline``), the account detail (``crm.get_account``) and the
lead catalog/detail (``leads.get_leads`` / ``leads.get_lead``); and the two
promise-keeping features on top of it: ``set_journey_action_done`` (per-action
completion) and ``get_action_calendar`` (the merged month view).

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
from jarz_pos.crm.follow_ups import todo_marker


def journey_marker(note_name):
    """The ToDo tag one journey note's reminder carries."""
    return todo_marker(f"journey:{note_name}")

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


def _journey_todos(reference_name, note_name, fields=None):
    """Open ToDos that are the reminder for ONE journey note.

    Deliberately filtered by the note's marker rather than counting every open
    ToDo on the record: staging and production run an Assignment Rule that
    opens its own ToDo on every Lead, so a bare count asserts on the site's
    configuration instead of on this feature.
    """
    return frappe.get_all(
        "ToDo",
        filters={
            "reference_type": "Lead",
            "reference_name": reference_name,
            "status": "Open",
            "description": ["like", f"%{journey_marker(note_name)}%"],
        },
        fields=fields or ["name", "description", "date"],
    )


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
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Ask for the owner",
            contact_person="Hany",
            next_action="Ring the owner",
            next_action_date=_days(3),
        )
        todos = _journey_todos(lead, note["name"])
        self.assertEqual(len(todos), 1)
        self.assertEqual(str(todos[0]["date"]), _days(3))
        self.assertIn("Hany", todos[0]["description"])
        self.assertIn("Ring the owner", todos[0]["description"])

    def test_the_reminder_survives_an_unrelated_open_todo(self):
        """The regression this site actually hits.

        An Assignment Rule opens a ToDo on every Lead the moment it is created,
        so the reference ALWAYS already has an open ToDo. The old
        ``_ensure_todo`` dedup treated that as "a reminder already exists" and
        skipped the journey one entirely. CI never caught it because a fresh
        test site has no Assignment Rule -- so this case fakes one.
        """
        lead = _make_lead()
        frappe.get_doc(
            {
                "doctype": "ToDo",
                "description": "Automatic Assignment",
                "reference_type": "Lead",
                "reference_name": lead,
                "status": "Open",
            }
        ).insert(ignore_permissions=True)

        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Owner asked for a call",
            next_action="Ring the owner",
            next_action_date=_days(3),
        )

        todos = _journey_todos(lead, note["name"])
        self.assertEqual(
            len(todos),
            1,
            "the journey reminder must be created even when the record already "
            "carries an unrelated open ToDo",
        )
        self.assertEqual(str(todos[0]["date"]), _days(3))

    def test_moving_the_date_re_dates_the_reminder_in_place(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="They asked to postpone",
            next_action="Call back",
            next_action_date=_days(3),
        )
        journey_api.update_journey_note(note["name"], next_action_date=_days(9))

        todos = _journey_todos(lead, note["name"])
        self.assertEqual(len(todos), 1, "re-dating must not duplicate the ToDo")
        self.assertEqual(str(todos[0]["date"]), _days(9))

    def test_clearing_the_date_retires_the_reminder(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Never mind, they called us",
            next_action_date=_days(3),
        )
        self.assertEqual(len(_journey_todos(lead, note["name"])), 1)

        journey_api.update_journey_note(note["name"], next_action_date="")
        self.assertEqual(len(_journey_todos(lead, note["name"])), 0)

    def test_deleting_the_note_retires_the_reminder(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Logged by mistake",
            next_action_date=_days(3),
        )
        self.assertEqual(len(_journey_todos(lead, note["name"])), 1)

        journey_api.delete_journey_note(note["name"])
        self.assertEqual(len(_journey_todos(lead, note["name"])), 0)

    def test_two_notes_keep_two_separate_reminders(self):
        lead = _make_lead()
        first = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Call the barista",
            next_action_date=_days(2),
        )
        second = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Also email the owner",
            next_action_date=_days(6),
        )
        self.assertEqual(len(_journey_todos(lead, first["name"])), 1)
        self.assertEqual(len(_journey_todos(lead, second["name"])), 1)

    def test_the_reminder_lands_on_the_rep_who_logged_it(self):
        """Not the record's owner: this catalog was bulk imported, so its owner
        is Administrator, whose follow-up feed nobody reads."""
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="I will call them",
            next_action_date=_days(3),
        )
        todos = _journey_todos(lead, note["name"], fields=["allocated_to"])
        self.assertEqual(todos[0]["allocated_to"], frappe.session.user)


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


# ---------------------------------------------------------------------------
# Contacts (the editor's WHO picker)
# ---------------------------------------------------------------------------
class TestJourneyContacts(JourneyTestCase):
    """``get_journey_contacts`` / ``add_journey_contact``.

    Skips itself until the lead contacts child table is migrated, for the same
    pre-migrate reason the module skips on a missing journey DocType.
    """

    def setUp(self):
        super().setUp()
        if not leads_api._has_contacts_field():
            self.skipTest(
                "Lead.custom_contacts not migrated on this site yet "
                "(pre-migrate run); re-run after the staging deploy."
            )

    def test_picker_lists_the_people_on_the_lead(self):
        lead = _make_lead()
        leads_api.save_lead_contacts(
            lead,
            [
                {"contact_name": "Sara", "role": "Barista", "phone": "01000000002"},
                {
                    "contact_name": "Mostafa",
                    "role": "Owner",
                    "phone": "01000000001",
                    "is_primary": 1,
                },
            ],
        )

        out = journey_api.get_journey_contacts("Lead", lead)

        self.assertEqual(out["lead"], lead)
        self.assertTrue(out["can_add"])
        # Primary first, whatever order the rows were saved in.
        self.assertEqual(
            [c["contact_name"] for c in out["contacts"]], ["Mostafa", "Sara"]
        )

    def test_picker_is_empty_on_a_lead_with_no_people(self):
        out = journey_api.get_journey_contacts("Lead", _make_lead())
        self.assertEqual(out["contacts"], [])
        self.assertTrue(out["can_add"])

    def test_add_contact_appends_to_the_lead_roster(self):
        lead = _make_lead()
        leads_api.save_lead_contacts(
            lead, [{"contact_name": "Mostafa", "role": "Owner", "phone": "01000000001"}]
        )

        out = journey_api.add_journey_contact(
            "Lead", lead, contact_name="Sara", role="Barista", phone="01000000002"
        )

        self.assertEqual(out["added"]["contact_name"], "Sara")
        names = [c["contact_name"] for c in out["contacts"]]
        self.assertEqual(sorted(names), ["Mostafa", "Sara"])
        # It is the SAME roster the lead page reads back, not a second list.
        self.assertEqual(
            sorted(c["contact_name"] for c in leads_api.get_lead(lead)["contacts"]),
            ["Mostafa", "Sara"],
        )

    def test_first_contact_becomes_primary(self):
        lead = _make_lead()
        out = journey_api.add_journey_contact(
            "Lead", lead, contact_name="Mostafa", phone="01000000001"
        )
        self.assertTrue(out["added"]["is_primary"])

    def test_adding_the_same_person_twice_does_not_duplicate(self):
        lead = _make_lead()
        journey_api.add_journey_contact(
            "Lead", lead, contact_name="Mostafa", phone="01000000001"
        )
        out = journey_api.add_journey_contact(
            "Lead", lead, contact_name="Mostafa", phone="01000000001"
        )
        self.assertEqual(len(out["contacts"]), 1)
        self.assertEqual(out["added"]["contact_name"], "Mostafa")

    def test_add_contact_backfills_a_blank_lead_phone(self):
        lead = _make_lead()
        journey_api.add_journey_contact(
            "Lead", lead, contact_name="Mostafa", phone="01000000001"
        )
        self.assertEqual(
            frappe.db.get_value("Lead", lead, "phone"), "01000000001"
        )

    def test_add_contact_requires_a_name_or_a_phone(self):
        lead = _make_lead()
        with self.assertRaises(Exception):
            journey_api.add_journey_contact("Lead", lead, role="Barista")

    def test_add_contact_rejects_an_unknown_record(self):
        with self.assertRaises(Exception):
            journey_api.add_journey_contact(
                "Lead", "_TEST-does-not-exist", contact_name="Mostafa"
            )


# ---------------------------------------------------------------------------
# Completing a next action
# ---------------------------------------------------------------------------
class JourneyDoneTestCase(JourneyTestCase):
    """Base for the completion + calendar cases.

    Skips itself until ``next_action_done`` is migrated, exactly like
    :class:`TestJourneyContacts` skips on ``Lead.custom_contacts``: CI's logic
    gate runs BEFORE ``bench migrate``, so the first CI run of these cases is
    vacuous BY DESIGN and proves nothing. Re-run after the staging deploy.
    """

    def setUp(self):
        super().setUp()
        if not journey_api._has_done_fields():
            self.skipTest(
                "Jarz Journey Note.next_action_done not migrated on this site "
                "yet (pre-migrate run); re-run after the staging deploy."
            )
        # The permission memos are per-CONTEXT, and a whole test module is one
        # context: without this a set_user in an earlier case would answer for
        # a later one.
        journey_api.clear_request_cache()

    def tearDown(self):
        journey_api.clear_request_cache()
        super().tearDown()


class TestJourneyActionDone(JourneyDoneTestCase):
    def test_completing_closes_the_reminder_and_undo_reopens_it(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Promised to ring the owner",
            next_action="Ring the owner",
            next_action_date=_days(3),
        )
        self.assertEqual(len(_journey_todos(lead, note["name"])), 1)

        done = journey_api.set_journey_action_done(note["name"])

        self.assertTrue(done["next_action_done"])
        self.assertTrue(done["next_action_done_on"])
        self.assertEqual(done["next_action_done_by"], frappe.session.user)
        self.assertTrue(done["next_action_done_by_name"])
        # The promise stays readable: settled is not deleted.
        self.assertEqual(done["next_action"], "Ring the owner")
        self.assertEqual(done["next_action_date"], _days(3))
        self.assertEqual(
            len(_journey_todos(lead, note["name"])),
            0,
            "a completed action must stop nagging",
        )

        undone = journey_api.set_journey_action_done(note["name"], done=0)

        self.assertFalse(undone["next_action_done"])
        self.assertIsNone(undone["next_action_done_on"])
        self.assertEqual(undone["next_action_done_by"], "")
        todos = _journey_todos(lead, note["name"])
        self.assertEqual(len(todos), 1, "undo must bring the reminder back")
        self.assertEqual(str(todos[0]["date"]), _days(3))

    def test_the_string_zero_reads_as_undo(self):
        """Frappe delivers flags as strings, and ``bool("0")`` is True."""
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Call back",
            next_action="Call back",
            next_action_date=_days(2),
        )
        journey_api.set_journey_action_done(note["name"], done="1")
        self.assertTrue(
            frappe.db.get_value(
                journey_api.JOURNEY_DOCTYPE, note["name"], "next_action_done"
            )
        )

        out = journey_api.set_journey_action_done(note["name"], done="0")
        self.assertFalse(out["next_action_done"])

    def test_a_done_action_drops_out_of_the_summary_but_still_counts(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Visited",
            entry_date=_days(-1),
            next_action="Confirm the order",
            next_action_date=_days(4),
        )
        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertEqual(summary["next_action_date"], _days(4))

        journey_api.set_journey_action_done(note["name"])

        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertIsNone(
            summary["next_action_date"],
            "a settled promise must stop showing on the card",
        )
        # A done action is still a touch that happened.
        self.assertEqual(summary["journey_count"], 1)
        self.assertEqual(summary["last_journey_date"], _days(-1))

    def test_the_pipeline_card_drops_a_completed_action(self):
        lead = _make_lead("_TEST Journey Done Board")
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Board touch",
            entry_date=_days(-1),
            next_action="Confirm the order",
            next_action_date=_days(4),
        )
        journey_api.set_journey_action_done(note["name"])

        card = _find_card(crm_api.get_b2b_pipeline(), lead)
        self.assertIsNotNone(card)
        self.assertEqual(card["journey_count"], 1)
        self.assertIsNone(card["next_action_date"])

    def test_completing_the_only_action_closes_the_record_loop(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="The one promise",
            next_action="Call",
            next_action_date=_days(3),
        )
        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(3),
        )

        journey_api.set_journey_action_done(note["name"])

        row = frappe.db.get_value(
            "Lead",
            lead,
            ["custom_next_followup_date", "custom_followup_done"],
            as_dict=True,
        )
        self.assertIsNone(row.custom_next_followup_date)
        self.assertEqual(int(row.custom_followup_done or 0), 1)

    def test_completing_one_of_two_repoints_the_date_at_the_other(self):
        """The one moment it is correct to move a follow-up date LATER.

        ``sync_followup`` only ever pulls the date earlier, so nothing else in
        the app could ever move it forward when the near promise was kept.
        """
        lead = _make_lead()
        near = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Near",
            next_action="Ring",
            next_action_date=_days(2),
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Far",
            next_action="Email",
            next_action_date=_days(6),
        )
        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(2),
        )

        journey_api.set_journey_action_done(near["name"])

        row = frappe.db.get_value(
            "Lead",
            lead,
            ["custom_next_followup_date", "custom_followup_done"],
            as_dict=True,
        )
        self.assertEqual(str(row.custom_next_followup_date), _days(6))
        self.assertEqual(int(row.custom_followup_done or 0), 0)
        # And the card follows the record.
        summary = journey_api.journey_summaries("Lead", [lead])[lead]
        self.assertEqual(summary["next_action_date"], _days(6))
        self.assertEqual(summary["next_action"], "Email")

    def test_a_note_without_a_next_action_cannot_be_completed(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead", reference_name=lead, note="Just a chat"
        )
        with self.assertRaises(Exception):
            journey_api.set_journey_action_done(note["name"])

    def test_an_unrelated_rep_may_not_complete_it(self):
        lead = _make_lead("_TEST Journey Done Gate")
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Mine to do",
            next_action="Ring",
            next_action_date=_days(3),
        )
        rep = _ensure_rep_user()
        original = frappe.session.user
        try:
            frappe.set_user(rep)
            journey_api.clear_request_cache()
            with self.assertRaises(Exception):
                journey_api.set_journey_action_done(note["name"])
        finally:
            frappe.set_user(original)
            journey_api.clear_request_cache()

    def test_the_rep_the_reminder_is_assigned_to_may_complete_it(self):
        """Wider than editing on purpose: the person who OWES the action.

        The reminder does not always land on the author (it can be re-owned),
        and the whole point of the feature is that whoever has to do the thing
        can say it is done.
        """
        lead = _make_lead("_TEST Journey Done Assignee")
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Somebody must ring them",
            next_action="Ring",
            next_action_date=_days(3),
        )
        todo = _journey_todos(lead, note["name"])[0]["name"]
        rep = _ensure_rep_user()
        frappe.db.set_value("ToDo", todo, "allocated_to", rep, update_modified=False)

        original = frappe.session.user
        try:
            frappe.set_user(rep)
            journey_api.clear_request_cache()
            out = journey_api.set_journey_action_done(note["name"])
            self.assertTrue(out["next_action_done"])
            self.assertEqual(out["next_action_done_by"], rep)
        finally:
            frappe.set_user(original)
            journey_api.clear_request_cache()

    def test_can_complete_is_reported_on_the_note(self):
        lead = _make_lead()
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Mine",
            next_action="Ring",
            next_action_date=_days(3),
        )
        listed = journey_api.get_journey_notes("Lead", lead)["notes"][0]
        self.assertTrue(listed["can_complete"])
        self.assertFalse(listed["next_action_done"])


# ---------------------------------------------------------------------------
# Action calendar
# ---------------------------------------------------------------------------
def _actions_for(calendar, reference_name, source=None):
    """Every calendar action on one record (optionally of one source)."""
    return [
        a
        for a in calendar["actions"]
        if a["reference_name"] == reference_name
        and (source is None or a["source"] == source)
    ]


class TestCompletionRespectsOtherWriters(JourneyDoneTestCase):
    """Completion may only clear a follow-up date the DIARY put there.

    ``crm.advance_stage`` writes ``custom_next_followup_date`` from the stage
    editor. Before this was guarded, ticking off any journey action wiped that
    date and closed the loop, silently losing a chase a rep had booked
    elsewhere.
    """

    def test_completing_does_not_clear_a_followup_the_diary_never_set(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Dropped samples",
            next_action="Ask what they thought",
            next_action_date=_days(15),
        )
        # A stage follow-up booked SOONER than the note, by another writer.
        frappe.db.set_value(
            "Lead", lead, "custom_next_followup_date", _days(10), update_modified=False
        )

        journey_api.set_journey_action_done(note["name"])

        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(10),
            "a follow-up booked by the stage editor is not the diary's to drop",
        )
        self.assertFalse(
            frappe.db.get_value("Lead", lead, "custom_followup_done"),
            "the loop must stay open while that other follow-up is pending",
        )

    def test_completing_clears_the_date_it_owns(self):
        lead = _make_lead()
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Promised a callback",
            next_action="Call back",
            next_action_date=_days(4),
        )
        # The note stamped the record itself, so the record is pointing at it.
        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(4),
        )

        journey_api.set_journey_action_done(note["name"])

        self.assertFalse(
            frappe.db.get_value("Lead", lead, "custom_next_followup_date")
        )
        self.assertTrue(frappe.db.get_value("Lead", lead, "custom_followup_done"))

    def test_a_sooner_pending_action_still_pulls_the_date_forward(self):
        """The one case completion may overwrite another writer's date.

        A journey action dated BEFORE what is booked is strictly more urgent, so
        re-pointing at it loses nobody a chase -- it only brings one earlier.
        """
        lead = _make_lead()
        soon = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Ring Tuesday",
            next_action="Ring Tuesday",
            next_action_date=_days(2),
        )
        later = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Ring next month",
            next_action="Ring next month",
            next_action_date=_days(30),
        )
        frappe.db.set_value(
            "Lead", lead, "custom_next_followup_date", _days(20), update_modified=False
        )

        journey_api.set_journey_action_done(later["name"])

        self.assertEqual(
            str(frappe.db.get_value("Lead", lead, "custom_next_followup_date")),
            _days(2),
            "the sooner pending action wins",
        )
        self.assertFalse(frappe.db.get_value("Lead", lead, "custom_followup_done"))
        # Untouched by the completion of its sibling.
        self.assertFalse(
            frappe.db.get_value(
                journey_api.JOURNEY_DOCTYPE, soon["name"], "next_action_done"
            )
        )


class TestCalendarSkipsDeadLeads(JourneyDoneTestCase):
    """The calendar must not promise a chase the reminder passes never make.

    ``crm.follow_ups._pass_lead_followups`` skips leads flagged not-suitable and
    leads merged away; a follow-up row for one of those is a date nobody will
    ever action.
    """

    def _followup_rows_for(self, lead):
        out = journey_api.get_action_calendar(
            from_date=_days(-60), to_date=_days(60), scope="all"
        )
        return [
            a
            for a in out["actions"]
            if a["reference_name"] == lead and a["source"] == "followup"
        ]

    def test_a_live_lead_followup_is_listed(self):
        lead = _make_lead()
        frappe.db.set_value(
            "Lead", lead, "custom_next_followup_date", _days(3), update_modified=False
        )
        self.assertEqual(len(self._followup_rows_for(lead)), 1)

    def test_a_not_suitable_lead_is_skipped(self):
        if not leads_api._has_verdict_field():
            self.skipTest("Lead.custom_not_suitable not migrated on this site yet.")
        lead = _make_lead()
        frappe.db.set_value(
            "Lead",
            lead,
            {"custom_next_followup_date": _days(3), "custom_not_suitable": 1},
            update_modified=False,
        )
        self.assertEqual(self._followup_rows_for(lead), [])

    def test_a_merged_away_lead_is_skipped(self):
        if not leads_api._has_merge_field():
            self.skipTest("Lead.custom_merged_into not migrated on this site yet.")
        survivor = _make_lead("_TEST Journey Survivor")
        lead = _make_lead()
        frappe.db.set_value(
            "Lead",
            lead,
            {"custom_next_followup_date": _days(3), "custom_merged_into": survivor},
            update_modified=False,
        )
        self.assertEqual(self._followup_rows_for(lead), [])


class TestActionCalendar(JourneyDoneTestCase):
    """``get_action_calendar`` merges journey actions and record follow-ups.

    Every assertion is existence-based, never a total count: this suite runs
    against a staging clone of production, so the range always contains real
    leads' real follow-ups alongside the fixtures.
    """

    def test_it_serves_journey_and_followup_rows_and_dedups_them(self):
        with_note = _make_lead("_TEST Calendar Journey")
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=with_note,
            note="Visited",
            next_action="Confirm the order",
            next_action_date=_days(2),
        )
        # A record-level follow-up with no journey note behind it.
        followup_only = _make_lead("_TEST Calendar Followup")
        frappe.db.set_value(
            "Lead",
            followup_only,
            {"custom_next_followup_date": _days(3), "custom_followup_done": 0},
            update_modified=False,
        )

        calendar = journey_api.get_action_calendar(
            from_date=_days(-1), to_date=_days(10)
        )

        journey_rows = _actions_for(calendar, with_note)
        self.assertEqual(
            len(journey_rows),
            1,
            "the note stamps the record's follow-up date too -- the echo must "
            "be deduped away, not rendered twice",
        )
        row = journey_rows[0]
        self.assertEqual(row["source"], "journey")
        self.assertEqual(row["note"], note["name"])
        self.assertEqual(row["reference_doctype"], "Lead")
        self.assertEqual(row["date"], _days(2))
        self.assertEqual(row["action"], "Confirm the order")
        # Titles are resolved in one bulk query, not per row -- assert against
        # what the record actually holds rather than what was passed in.
        self.assertEqual(
            row["title"], frappe.db.get_value("Lead", with_note, "lead_name")
        )
        self.assertEqual(row["owner"], frappe.session.user)
        self.assertTrue(row["owner_name"])
        self.assertTrue(row["can_complete"])
        self.assertFalse(row["done"])
        self.assertFalse(row["overdue"])

        followup_rows = _actions_for(calendar, followup_only)
        self.assertEqual(len(followup_rows), 1)
        self.assertEqual(followup_rows[0]["source"], "followup")
        self.assertEqual(followup_rows[0]["note"], "")
        self.assertEqual(followup_rows[0]["date"], _days(3))
        self.assertEqual(followup_rows[0]["action"], "")
        self.assertEqual(
            followup_rows[0]["title"],
            frappe.db.get_value("Lead", followup_only, "lead_name"),
        )

    def test_done_actions_are_omitted_unless_asked_for(self):
        lead = _make_lead("_TEST Calendar Done")
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Done deal",
            next_action="Ring",
            next_action_date=_days(2),
        )
        journey_api.set_journey_action_done(note["name"])

        default = journey_api.get_action_calendar(
            from_date=_days(-1), to_date=_days(10)
        )
        self.assertEqual(_actions_for(default, lead), [])
        # ...but it is still COUNTED, so the screen can offer to show it.
        self.assertGreaterEqual(default["counts"]["done"], 1)

        included = journey_api.get_action_calendar(
            from_date=_days(-1), to_date=_days(10), include_done=1
        )
        rows = _actions_for(included, lead)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["done"])
        self.assertFalse(rows[0]["overdue"], "a done action is never overdue")

    def test_it_flags_overdue_actions(self):
        lead = _make_lead("_TEST Calendar Overdue")
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Should have called last week",
            entry_date=_days(-10),
            next_action="Ring",
            next_action_date=_days(-4),
        )
        calendar = journey_api.get_action_calendar(
            from_date=_days(-20), to_date=_days(20)
        )
        rows = _actions_for(calendar, lead, source="journey")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["overdue"])
        self.assertGreaterEqual(calendar["counts"]["overdue"], 1)
        self.assertGreaterEqual(calendar["counts"]["pending"], 1)

    def test_scope_mine_hides_another_reps_action_and_all_shows_it(self):
        lead = _make_lead("_TEST Calendar Scope")
        note = journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=lead,
            note="Somebody else's promise",
            next_action="Ring",
            next_action_date=_days(2),
        )
        # Re-stamp authorship: the note is now another rep's diary entry.
        frappe.db.set_value(
            journey_api.JOURNEY_DOCTYPE,
            note["name"],
            "logged_by",
            _ensure_rep_user(),
            update_modified=False,
        )

        mine = journey_api.get_action_calendar(
            from_date=_days(-1), to_date=_days(10), scope="mine"
        )
        self.assertEqual(_actions_for(mine, lead, source="journey"), [])

        everyone = journey_api.get_action_calendar(
            from_date=_days(-1), to_date=_days(10), scope="all"
        )
        rows = _actions_for(everyone, lead, source="journey")
        self.assertEqual(len(rows), 1)
        self.assertEqual(everyone["scope"], "all")

    def test_it_defaults_to_the_current_month(self):
        from frappe.utils import get_first_day, get_last_day, today

        calendar = journey_api.get_action_calendar()
        self.assertEqual(calendar["from_date"], str(get_first_day(today())))
        self.assertEqual(calendar["to_date"], str(get_last_day(today())))
        self.assertEqual(calendar["scope"], "mine")

    def test_it_is_sorted_by_date(self):
        lead = _make_lead("_TEST Calendar Sort")
        for offset in (7, 1, 4):
            journey_api.add_journey_note(
                reference_doctype="Lead",
                reference_name=lead,
                note=f"touch {offset}",
                next_action="Ring",
                next_action_date=_days(offset),
            )
        calendar = journey_api.get_action_calendar(
            from_date=_days(0), to_date=_days(10)
        )
        dates = [a["date"] for a in _actions_for(calendar, lead)]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates, [_days(1), _days(4), _days(7)])

    def test_it_is_gated_like_every_other_b2b_endpoint(self):
        original = frappe.session.user
        try:
            frappe.set_user("Guest")
            journey_api.clear_request_cache()
            with self.assertRaises(Exception):
                journey_api.get_action_calendar()
        finally:
            frappe.set_user(original)
            journey_api.clear_request_cache()


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

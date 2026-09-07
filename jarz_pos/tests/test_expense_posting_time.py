"""Guards for the user-chosen posting TIME on ledger documents.

ERPNext's ledger documents are date-granular. ``Journal Entry``, ``Payment
Entry`` and ``GL Entry`` carry no ``posting_time`` field and no such column —
only stock-bearing documents do. Two consequences drive everything asserted
here:

* ``doc.set_posting_time = 1`` on a Journal Entry is a **no-op**, not a fix.
  There is no field for the flag to unlock, so the time the POS user picked was
  simply discarded while the code read as though it had been honoured. That line
  is gone from ``JarzExpenseRequest.on_submit`` and this module fails if it comes
  back.
* The chosen time therefore lives on ``custom_jarz_posting_time``, seeded by
  ``utils.cleanup.ensure_posting_time_fields`` in ``before_migrate`` — not by a
  fixture, because fixtures sync at the very END of ``bench migrate`` while the
  code that writes the field is already serving.

Pure ``unittest``. The DocType JSON is read off disk and ``on_submit`` is driven
against fakes, so nothing here needs a site — but nothing here can pass
vacuously either: the module under test is imported at import time (an
ImportError is an error, never a skip) and every assertion checks a value that
was actually produced, not merely that a mock was reachable.
"""

import inspect
import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jarz_pos
from jarz_pos.utils import cleanup

# Imported at module scope on purpose: if this import breaks, the whole module
# errors out loudly instead of silently contributing zero assertions.
from jarz_pos.doctype.jarz_expense_request.jarz_expense_request import (
    JarzExpenseRequest,
)

DOCTYPE_JSON = os.path.join(
    os.path.dirname(os.path.abspath(jarz_pos.__file__)),
    "doctype",
    "jarz_expense_request",
    "jarz_expense_request.json",
)

POSTING_TIME_FIELD = "custom_jarz_posting_time"


def _load_doctype():
    with open(DOCTYPE_JSON, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _code_of(func):
    """The executable source of *func*, with comment lines removed.

    Both methods below are documented in comments that name the very identifiers
    being asserted against ("set_posting_time ... was a no-op"). Matching raw
    source would therefore fail on the explanation of the fix rather than on the
    fix, so comments are stripped and only real statements are searched.
    """
    return "\n".join(
        line
        for line in inspect.getsource(func).splitlines()
        if not line.strip().startswith("#")
    )


class TestExpenseRequestSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _load_doctype()
        cls.fields = {
            f.get("fieldname"): f for f in cls.payload.get("fields") or []
        }

    def test_expense_time_is_declared_as_a_time_field(self):
        field = self.fields.get("expense_time")
        self.assertIsNotNone(field, "expense_time missing from the DocType JSON")
        self.assertEqual(field["fieldtype"], "Time")
        self.assertEqual(field["label"], "Expense Time")

    def test_expense_time_is_optional(self):
        """Every existing row has none, and every existing caller omits it.

        Making it required would reject every request the mobile app and
        ``api.expenses.create_expense`` already send.
        """
        self.assertFalse(self.fields["expense_time"].get("reqd"))

    def test_expense_time_sits_immediately_after_expense_date(self):
        order = self.payload["field_order"]
        self.assertEqual(
            order[order.index("expense_date") + 1],
            "expense_time",
            "expense_time must be anchored directly on expense_date",
        )

    def test_expense_date_is_still_a_pure_date(self):
        """``expense_month`` is derived from it; a Datetime would change nothing
        in the ledger but would break the ``%Y-%m`` derivation's assumptions."""
        self.assertEqual(self.fields["expense_date"]["fieldtype"], "Date")

    def test_doctype_carries_a_modified_timestamp(self):
        """A DocType/fixture import SKIPS when ``modified`` matches the stored
        doc, so a schema edit that does not bump it lands nowhere while the
        deploy still reports success."""
        stamp = self.payload.get("modified")
        self.assertTrue(stamp, "DocType JSON has no 'modified' timestamp to compare")
        datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")


class _RecordingJournalEntry:
    """Records exactly what ``on_submit`` puts on the Journal Entry.

    Deliberately NOT a ``MagicMock``: a mock accepts every attribute silently,
    including ``set_posting_time`` — the very no-op this change removed — so an
    assertion against a mock could never notice it coming back.
    """

    def __init__(self):
        self.accounts = []
        self.flags = SimpleNamespace(ignore_permissions=False)
        # utils.posting_datetime decides which guard to apply from the document's
        # own doctype, so a fake without this would take the "not a ledger doc"
        # path and pass for the wrong reason.
        self.doctype = "Journal Entry"
        self.name = "ACC-JV-2026-00001"
        self.inserted = False
        self.submitted = False

    def append(self, table, row):
        getattr(self, table).append(row)

    def insert(self):
        self.inserted = True

    def submit(self):
        self.submitted = True


def _fake_request(expense_time=None):
    return SimpleNamespace(
        name="JEXP-00001",
        journal_entry=None,
        company="Jarz",
        expense_date="2026-09-08",
        expense_time=expense_time,
        remarks="Taxi to the branch",
        amount=25,
        reason_account="Fuel - J",
        paying_account="Cash - J",
        reason_label="Fuel",
        payment_source_label="Cash",
        db_set=MagicMock(),
    )


class TestOnSubmitPropagatesTheTime(unittest.TestCase):
    """``on_submit`` must move ``expense_time`` onto the JE custom field."""

    def _run(self, doc, field_in_meta=True):
        journal_entry = _RecordingJournalEntry()

        mock_frappe = MagicMock()
        mock_frappe.new_doc.return_value = journal_entry

        # ``utils.posting_datetime`` does the meta lookup, in its OWN module
        # namespace. Patching only the doctype module's ``frappe`` would leave
        # that lookup hitting a real site — which, site-less, raises, is caught,
        # and reports "field absent". Every case here would then pass for a
        # reason that has nothing to do with what it claims to test.
        posting_frappe = MagicMock()
        posting_frappe.get_meta.return_value.get_field.return_value = (
            {"fieldname": POSTING_TIME_FIELD} if field_in_meta else None
        )

        # The real ``jarz_pos.services.delivery_handling`` pulls in erpnext and
        # a chunk of the api package for one pure string helper. Stubbing it
        # keeps this test about posting time; the identity function preserves
        # the remark so the assertions below still see real data.
        stub = MagicMock()
        stub._strip_je_tag_lookalikes.side_effect = lambda value: value

        module = "jarz_pos.doctype.jarz_expense_request.jarz_expense_request"
        with patch.dict(
            "sys.modules", {"jarz_pos.services.delivery_handling": stub}
        ), patch(f"{module}.frappe", mock_frappe), patch(
            f"{module}._", side_effect=lambda text: text
        ), patch(
            "jarz_pos.utils.posting_datetime.frappe", posting_frappe
        ):
            JarzExpenseRequest.on_submit(doc)

        return journal_entry

    def test_chosen_time_lands_on_the_custom_field(self):
        journal_entry = self._run(_fake_request("14:35:00"))
        self.assertEqual(
            getattr(journal_entry, POSTING_TIME_FIELD, None), "14:35:00"
        )

    def test_a_time_read_back_from_the_database_still_lands(self):
        """Frappe hands a ``Time`` column back as a ``timedelta``, not a string.

        Any submit that did not come straight from the create call — an approval
        flow, a background retry, a Desk submit — sees that shape, and a naive
        assignment would store ``"14:35:00"``'s timedelta repr instead.
        """
        journal_entry = self._run(_fake_request(timedelta(hours=14, minutes=35)))
        self.assertEqual(getattr(journal_entry, POSTING_TIME_FIELD, None), "14:35:00")

    def test_the_entry_is_still_posted_normally(self):
        """The time must never be the reason an expense fails to post."""
        journal_entry = self._run(_fake_request("14:35:00"))
        self.assertEqual(journal_entry.posting_date, "2026-09-08")
        self.assertEqual(len(journal_entry.accounts), 2)
        self.assertTrue(journal_entry.inserted)
        self.assertTrue(journal_entry.submitted)

    def test_no_time_means_no_write(self):
        journal_entry = self._run(_fake_request(None))
        self.assertFalse(hasattr(journal_entry, POSTING_TIME_FIELD))
        self.assertTrue(journal_entry.submitted)

    def test_unmigrated_expense_doctype_does_not_break_the_posting(self):
        """A Document carries attributes only for fields in its meta.

        On a site whose DocType has not synced yet there is no ``expense_time``
        attribute at all, and a plain ``self.expense_time`` would raise
        AttributeError — turning a missing display field into a failed expense.
        """
        doc = _fake_request()
        del doc.expense_time
        journal_entry = self._run(doc)
        self.assertFalse(hasattr(journal_entry, POSTING_TIME_FIELD))
        self.assertTrue(journal_entry.submitted)

    def test_missing_custom_field_does_not_break_the_posting(self):
        """A site that has not migrated since this release still posts the
        expense; it simply carries no time."""
        journal_entry = self._run(_fake_request("14:35:00"), field_in_meta=False)
        self.assertFalse(hasattr(journal_entry, POSTING_TIME_FIELD))
        self.assertTrue(journal_entry.submitted)

    def test_set_posting_time_is_never_set_again(self):
        """It unlocks a field Journal Entry does not have. Setting it looked
        like honouring the user's time while discarding it."""
        journal_entry = self._run(_fake_request("14:35:00"))
        self.assertFalse(hasattr(journal_entry, "set_posting_time"))
        self.assertNotIn("set_posting_time", _code_of(JarzExpenseRequest.on_submit))

    def test_the_write_goes_through_the_shared_ledger_helper(self):
        """One definition of the field, its guard and the timedelta handling.

        Assigning it by hand here would be a second implementation of something
        ``utils.posting_datetime`` already owns for every ledger writer — and
        belt-and-braces that have drifted apart are worse than either alone.
        """
        self.assertIn(
            "apply_ledger_posting_datetime", _code_of(JarzExpenseRequest.on_submit)
        )


class TestExpenseMonthStaysDateOnly(unittest.TestCase):
    def test_month_is_derived_from_the_date_alone(self):
        source = _code_of(JarzExpenseRequest.validate)
        self.assertIn('getdate(self.expense_date).strftime("%Y-%m")', source)
        self.assertNotIn(
            "expense_time",
            source,
            "the filing month must not depend on the time of day",
        )


class TestPostingTimeSeeder(unittest.TestCase):
    """The seeder is the only thing that creates this field on any site."""

    @staticmethod
    def _seeded():
        """``{(dt, fieldname): merged_attrs}`` for one run against a fake frappe.

        ``_ensure_custom_field`` puts the core attributes in the dict handed to
        ``frappe.get_doc`` and applies the rest through ``doc.set(...)``, so both
        are merged to give one view of the field as it would be inserted.
        """
        created = []

        fake = MagicMock()
        fake.db.exists.return_value = False  # nothing exists ⇒ create everything

        def _capture(payload):
            doc = MagicMock()
            created.append((payload, doc))
            return doc

        fake.get_doc.side_effect = _capture

        with patch.object(cleanup, "frappe", fake):
            cleanup.ensure_posting_time_fields()

        merged = {}
        for payload, doc in created:
            attrs = dict(payload)
            for call in doc.set.call_args_list:
                if len(call.args) == 2:
                    attrs[call.args[0]] = call.args[1]
            merged[(payload["dt"], payload["fieldname"])] = attrs
        return merged

    def test_both_ledger_doctypes_are_seeded(self):
        seeded = self._seeded()
        self.assertEqual(
            set(seeded),
            {
                ("Journal Entry", POSTING_TIME_FIELD),
                ("Payment Entry", POSTING_TIME_FIELD),
            },
        )

    def test_field_definition_is_identical_on_both(self):
        for attrs in self._seeded().values():
            with self.subTest(dt=attrs["dt"]):
                self.assertEqual(attrs["fieldtype"], "Time")
                self.assertEqual(attrs["label"], "Jarz Posting Time")
                self.assertEqual(attrs["insert_after"], "posting_date")
                self.assertEqual(attrs.get("no_copy"), 1)
                self.assertEqual(attrs.get("print_hide"), 1)
                self.assertEqual(attrs.get("translatable"), 0)
                self.assertTrue(attrs.get("description"))

    def test_field_is_editable_unlike_the_dedup_tag(self):
        """User-meaningful data a manager may need to correct in Desk. Making it
        read_only would put a developer between an operator and a typo."""
        for attrs in self._seeded().values():
            with self.subTest(dt=attrs["dt"]):
                self.assertFalse(attrs.get("read_only"))

    def test_the_seeded_fieldname_is_the_one_the_writer_uses(self):
        """The seeder creates the field; ``utils.posting_datetime`` writes it.

        They are two files with no compile-time link between them, so a rename
        in either would leave a field nobody writes and a write that lands
        nowhere — and both failures are silent.
        """
        from jarz_pos.utils.posting_datetime import LEDGER_POSTING_TIME_FIELD

        self.assertEqual(LEDGER_POSTING_TIME_FIELD, POSTING_TIME_FIELD)
        self.assertEqual(
            {fieldname for _dt, fieldname in self._seeded()},
            {LEDGER_POSTING_TIME_FIELD},
        )

    def test_seeder_is_create_only(self):
        fake = MagicMock()
        fake.db.exists.return_value = True  # everything already exists
        with patch.object(cleanup, "frappe", fake):
            cleanup.ensure_posting_time_fields()
        self.assertFalse(fake.get_doc.called, "the seeder rewrote an existing field")

    def test_seeder_never_raises(self):
        """A raising before_migrate hook aborts the migrate for the whole bench."""
        fake = MagicMock()
        fake.db.exists.side_effect = Exception("database on fire")
        with patch.object(cleanup, "frappe", fake):
            cleanup.ensure_posting_time_fields()  # must not raise


class TestHooksRegistration(unittest.TestCase):
    def test_seeder_runs_before_migrate_and_after_the_collision_sweep(self):
        """``remove_colliding_custom_fields_for_fixtures`` deletes any Custom
        Field whose name differs from the fixture's, so seeding before it would
        only hand it something to delete."""
        from jarz_pos import hooks

        seeder = "jarz_pos.utils.cleanup.ensure_posting_time_fields"
        sweep = "jarz_pos.utils.cleanup.remove_colliding_custom_fields_for_fixtures"
        self.assertIn(seeder, hooks.before_migrate)
        self.assertGreater(
            hooks.before_migrate.index(seeder), hooks.before_migrate.index(sweep)
        )


if __name__ == "__main__":
    unittest.main()

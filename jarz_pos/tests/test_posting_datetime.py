"""Guards for the shared posting date/time splitter.

``jarz_pos.utils.posting_datetime`` is the single definition of what an
operator-supplied posting moment means, used by every write that lets the till
choose one: stock transfers, inventory counts, purchases and returns, cash
transfers, expenses, employee advances.

Three properties are load-bearing and each has cases below.

* **The legacy shape must not move.** The backend deploys before the mobile
  patch reaches devices, so for weeks every caller sends a bare
  ``"YYYY-MM-DD"``. Those requests must produce byte-for-byte the same document
  they produce today — in particular ``set_posting_time = 1`` WITH no
  ``posting_time``, because ``TransactionBase.validate_posting_time`` overwrites
  posting_date with ``now_datetime()`` for any document where that flag is
  falsy. Dropping the flag would not preserve old behaviour, it would delete
  backdating outright.
* **A time that cannot be honoured is never dropped in silence.** A malformed
  value is refused (``frappe.throw``) and a missing custom field is logged.
  Silently discarding the time half is the exact failure this module exists to
  remove, and it is invisible: the document still posts, on the right day, at
  the wrong time.
* **Ledger documents are not stock documents.** Journal Entry and Payment Entry
  have no ``posting_time`` column, so writing one there — or setting
  ``set_posting_time`` — is a no-op that reads as a fix. The ledger helper must
  never touch either field.

Pure ``unittest`` against fakes, but nothing here can pass vacuously: the
module under test is imported at module scope (an ImportError is an error, not
a skip), the refusal cases go through the REAL ``frappe.throw`` rather than a
mock that would accept anything, and every assertion reads a value the code
actually produced.
"""

import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch

import frappe

# Module scope on purpose: if this import breaks, the whole module errors out
# loudly instead of quietly contributing zero assertions.
from jarz_pos.utils import posting_datetime as pdt

MODULE = "jarz_pos.utils.posting_datetime"


class _FakeDoc:
    """A document that only has the attributes something actually set.

    Deliberately NOT a ``MagicMock``: a mock answers ``hasattr`` for every name,
    so ``assertFalse(hasattr(doc, "posting_time"))`` — the assertion that pins
    "a date-only request writes no time" — could never fail against one.
    """

    def __init__(self, doctype=None):
        if doctype:
            self.doctype = doctype


class TestSplitPostingDatetime(unittest.TestCase):
    def test_date_only_is_the_legacy_shape_and_carries_no_time(self):
        self.assertEqual(("2026-09-08", None), pdt.split_posting_datetime("2026-09-08"))

    def test_date_and_full_time(self):
        self.assertEqual(
            ("2026-09-08", "14:30:00"),
            pdt.split_posting_datetime("2026-09-08 14:30:00"),
        )

    def test_seconds_are_optional(self):
        self.assertEqual(
            ("2026-09-08", "14:30:00"),
            pdt.split_posting_datetime("2026-09-08 14:30"),
        )

    def test_iso_t_separator_is_accepted(self):
        self.assertEqual(
            ("2026-09-08", "14:30:00"),
            pdt.split_posting_datetime("2026-09-08T14:30:00"),
        )

    def test_a_single_digit_hour_is_accepted(self):
        self.assertEqual(
            ("2026-09-08", "09:05:00"),
            pdt.split_posting_datetime("2026-09-08 9:05"),
        )

    def test_fractional_seconds_are_normalised_to_six_digits(self):
        """Frappe's Time default keeps six, and ``api/manufacturing`` orders two
        movements in the same second by comparing these as STRINGS."""
        self.assertEqual(
            ("2026-09-08", "14:30:00.500000"),
            pdt.split_posting_datetime("2026-09-08T14:30:00.5"),
        )

    def test_an_all_zero_fraction_is_dropped(self):
        """Dart's ``toIso8601String()`` always emits ``.000`` for a whole second;
        ``14:30:00.000000`` and ``14:30:00`` are equal times that compare
        unequal as strings."""
        self.assertEqual(
            ("2026-09-08", "14:30:00"),
            pdt.split_posting_datetime("2026-09-08T14:30:00.000"),
        )

    def test_empty_inputs_mean_no_choice_at_all(self):
        for value in (None, "", "   "):
            with self.subTest(value=repr(value)):
                self.assertEqual((None, None), pdt.split_posting_datetime(value))

    def test_a_datetime_object_keeps_its_time(self):
        """``datetime`` is a subclass of ``date``; checking ``date`` first would
        throw the whole time away."""
        self.assertEqual(
            ("2026-09-08", "14:30:00"),
            pdt.split_posting_datetime(datetime(2026, 9, 8, 14, 30, 0)),
        )

    def test_a_datetime_object_keeps_its_microseconds(self):
        self.assertEqual(
            ("2026-09-08", "14:30:00.505453"),
            pdt.split_posting_datetime(datetime(2026, 9, 8, 14, 30, 0, 505453)),
        )

    def test_a_date_object_carries_no_time(self):
        self.assertEqual(
            ("2026-09-08", None), pdt.split_posting_datetime(date(2026, 9, 8))
        )

    def test_malformed_values_are_refused_not_silently_dropped(self):
        """The refusal goes through the real ``frappe.throw``.

        A stub would let ``split_posting_datetime`` fall through and return, so
        this case would pass against a parser that dropped the time instead of
        rejecting it — the very bug it is meant to catch.
        """
        for value in (
            "not-a-date",
            "08/09/2026",
            "2026-9-8",  # unpadded; Frappe's own Date columns are zero-padded
            "2026-02-30",  # matches the shape, is not a day
            "2026-09-08 25:00:00",
            "2026-09-08 14:61",
            "2026-09-08 14",  # an hour with no minutes is not a time
            "2026-09-08T14:30:00Z",  # a UTC instant, not a local wall clock
            "2026-09-08T14:30:00+02:00",
        ):
            with self.subTest(value=value):
                with self.assertRaises(frappe.ValidationError):
                    pdt.split_posting_datetime(value)

    def test_the_refusal_names_the_offending_value(self):
        with self.assertRaises(frappe.ValidationError) as ctx:
            pdt.split_posting_datetime("2026-09-08 25:00:00")
        self.assertIn("2026-09-08 25:00:00", str(ctx.exception))


class TestFormatTimeValue(unittest.TestCase):
    """A Frappe ``Time`` column comes back as a ``timedelta``, not a string."""

    def test_a_timedelta_is_rendered_as_a_wall_clock(self):
        self.assertEqual("14:30:00", pdt.format_time_value(timedelta(seconds=52200)))

    def test_a_timedelta_keeps_its_microseconds(self):
        self.assertEqual(
            "14:30:00.505453",
            pdt.format_time_value(timedelta(seconds=52200, microseconds=505453)),
        )

    def test_midnight_is_a_real_time_not_an_empty_one(self):
        self.assertEqual("00:00:00", pdt.format_time_value(timedelta(0)))

    def test_a_time_object_is_accepted(self):
        self.assertEqual("14:30:00", pdt.format_time_value(time(14, 30)))

    def test_a_string_is_normalised(self):
        self.assertEqual("09:05:00", pdt.format_time_value("9:05"))

    def test_values_outside_one_day_are_not_guessed_at(self):
        for value in (timedelta(days=1), timedelta(seconds=-60)):
            with self.subTest(value=value):
                self.assertIsNone(pdt.format_time_value(value))

    def test_empty_and_unparseable_values_are_none_and_never_raise(self):
        """Its inputs come from the database, not from a caller — there is no
        operator standing there to correct them."""
        for value in (None, "", "half past two", 3.5):
            with self.subTest(value=repr(value)):
                self.assertIsNone(pdt.format_time_value(value))


class TestJoinPostingDatetime(unittest.TestCase):
    """Reassembling a moment stored as a date on one field and a time on another."""

    def test_a_stored_date_and_time_column_are_rejoined(self):
        self.assertEqual(
            "2026-09-08 14:30:00",
            pdt.join_posting_datetime(date(2026, 9, 8), timedelta(seconds=52200)),
        )

    def test_no_stored_time_yields_the_bare_date(self):
        self.assertEqual(
            "2026-09-08", pdt.join_posting_datetime("2026-09-08", None)
        )

    def test_no_date_at_all_means_no_choice_was_recorded(self):
        self.assertIsNone(pdt.join_posting_datetime(None, timedelta(seconds=52200)))

    def test_a_time_already_on_the_date_value_survives(self):
        self.assertEqual(
            "2026-09-08 14:30:00",
            pdt.join_posting_datetime("2026-09-08 14:30:00", None),
        )


class TestApplyStockPostingDatetime(unittest.TestCase):
    """Stock Entry / Stock Reconciliation / Purchase Invoice / Purchase Receipt."""

    def test_a_chosen_time_reaches_posting_time_on_a_document(self):
        doc = _FakeDoc("Stock Entry")
        pdt.apply_stock_posting_datetime(doc, "2026-09-08 14:30:00")
        self.assertEqual("2026-09-08", doc.posting_date)
        self.assertEqual("14:30:00", doc.posting_time)
        self.assertEqual(1, doc.set_posting_time)

    def test_a_chosen_time_reaches_posting_time_on_a_mapping(self):
        """ERPNext's mappers (``make_return_doc``, ``make_stock_entry``) hand back
        a dict, and ``frappe._dict`` is a dict subclass."""
        payload = {"doctype": "Stock Entry"}
        pdt.apply_stock_posting_datetime(payload, "2026-09-08 14:30:00")
        self.assertEqual("2026-09-08", payload["posting_date"])
        self.assertEqual("14:30:00", payload["posting_time"])
        self.assertEqual(1, payload["set_posting_time"])

    def test_a_date_only_value_still_raises_the_flag_but_writes_no_time(self):
        """The regression this pins: ``TransactionBase.validate_posting_time``
        replaces posting_date AND posting_time with ``now_datetime()`` whenever
        ``set_posting_time`` is falsy, so a date-only backdate that dropped the
        flag would post today instead."""
        doc = _FakeDoc("Purchase Invoice")
        pdt.apply_stock_posting_datetime(doc, "2026-09-08")
        self.assertEqual("2026-09-08", doc.posting_date)
        self.assertEqual(1, doc.set_posting_time)
        self.assertFalse(hasattr(doc, "posting_time"))

    def test_an_empty_value_leaves_the_document_completely_alone(self):
        """Callers keep their own ``today()`` fallback behind this."""
        doc = _FakeDoc("Stock Entry")
        for value in (None, ""):
            with self.subTest(value=repr(value)):
                pdt.apply_stock_posting_datetime(doc, value)
        self.assertFalse(hasattr(doc, "posting_date"))
        self.assertFalse(hasattr(doc, "set_posting_time"))

    def test_fractional_precision_survives_onto_the_document(self):
        doc = _FakeDoc("Stock Entry")
        pdt.apply_stock_posting_datetime(doc, "2026-09-08 14:30:00.505453")
        self.assertEqual("14:30:00.505453", doc.posting_time)
        # Ordering against an earlier receipt in the same second is a string
        # comparison in api/manufacturing; truncation would invert it.
        self.assertGreater(doc.posting_time, "14:30:00.290189")


class TestApplyLedgerPostingDatetime(unittest.TestCase):
    """Journal Entry / Payment Entry — no posting_time column anywhere."""

    @staticmethod
    def _frappe(field_exists=True, meta_raises=False):
        fake = MagicMock()
        if meta_raises:
            fake.get_meta.side_effect = Exception("DocType not installed")
        else:
            fake.get_meta.return_value.get_field.return_value = (
                {"fieldname": pdt.LEDGER_POSTING_TIME_FIELD} if field_exists else None
            )
        return fake

    def test_the_chosen_time_lands_on_the_custom_field(self):
        doc = _FakeDoc("Journal Entry")
        with patch(f"{MODULE}.frappe", self._frappe()):
            pdt.apply_ledger_posting_datetime(doc, "2026-09-08 14:30:00")
        self.assertEqual("2026-09-08", doc.posting_date)
        self.assertEqual("14:30:00", getattr(doc, pdt.LEDGER_POSTING_TIME_FIELD))

    def test_posting_time_and_set_posting_time_are_never_written(self):
        """Neither field exists on these doctypes. ``set_posting_time = 1`` on a
        Journal Entry looked like honouring the operator's time for years while
        doing nothing at all."""
        doc = _FakeDoc("Payment Entry")
        with patch(f"{MODULE}.frappe", self._frappe()):
            pdt.apply_ledger_posting_datetime(doc, "2026-09-08 14:30:00")
        self.assertFalse(hasattr(doc, "posting_time"))
        self.assertFalse(hasattr(doc, "set_posting_time"))

    def test_a_date_only_value_writes_the_date_and_nothing_else(self):
        doc = _FakeDoc("Journal Entry")
        with patch(f"{MODULE}.frappe", self._frappe()):
            pdt.apply_ledger_posting_datetime(doc, "2026-09-08")
        self.assertEqual("2026-09-08", doc.posting_date)
        self.assertFalse(hasattr(doc, pdt.LEDGER_POSTING_TIME_FIELD))

    def test_an_unmigrated_site_still_posts_and_says_so(self):
        """This code and the field arrive in one commit, but the field only
        exists after that deploy's migrate has run. The posting must survive —
        and the dropped time must reach the Error Log rather than vanish."""
        doc = _FakeDoc("Journal Entry")
        fake = self._frappe(field_exists=False)
        with patch(f"{MODULE}.frappe", fake):
            pdt.apply_ledger_posting_datetime(doc, "2026-09-08 14:30:00")
        self.assertEqual("2026-09-08", doc.posting_date)
        self.assertFalse(hasattr(doc, pdt.LEDGER_POSTING_TIME_FIELD))
        fake.log_error.assert_called_once()
        self.assertIn("14:30:00", fake.log_error.call_args.args[0])

    def test_a_broken_meta_lookup_never_costs_the_posting(self):
        doc = _FakeDoc("Journal Entry")
        with patch(f"{MODULE}.frappe", self._frappe(meta_raises=True)):
            pdt.apply_ledger_posting_datetime(doc, "2026-09-08 14:30:00")
        self.assertEqual("2026-09-08", doc.posting_date)

    def test_a_failing_error_log_never_costs_the_posting_either(self):
        """``frappe.log_error`` can itself raise — an over-long title, or no DB
        connection in a background job. Losing the note is acceptable; losing
        the posting is not."""
        doc = _FakeDoc("Journal Entry")
        fake = self._frappe(field_exists=False)
        fake.log_error.side_effect = Exception("Error Log write failed")
        with patch(f"{MODULE}.frappe", fake):
            pdt.apply_ledger_posting_datetime(doc, "2026-09-08 14:30:00")
        self.assertEqual("2026-09-08", doc.posting_date)

    def test_an_empty_value_leaves_the_document_alone(self):
        doc = _FakeDoc("Journal Entry")
        with patch(f"{MODULE}.frappe", self._frappe()):
            pdt.apply_ledger_posting_datetime(doc, None)
        self.assertFalse(hasattr(doc, "posting_date"))

    def test_a_mapping_is_handled_too(self):
        payload = {"doctype": "Payment Entry"}
        with patch(f"{MODULE}.frappe", self._frappe()):
            pdt.apply_ledger_posting_datetime(payload, "2026-09-08 14:30:00")
        self.assertEqual("2026-09-08", payload["posting_date"])
        self.assertEqual("14:30:00", payload[pdt.LEDGER_POSTING_TIME_FIELD])
        self.assertNotIn("set_posting_time", payload)

    def test_a_malformed_value_is_still_refused(self):
        """Tolerating a missing custom field is a schema state; tolerating a
        malformed string would be tolerating a caller bug."""
        doc = _FakeDoc("Journal Entry")
        with self.assertRaises(frappe.ValidationError):
            pdt.apply_ledger_posting_datetime(doc, "2026-09-08 99:99")


if __name__ == "__main__":
    unittest.main()


class TestFutureTimeGates(unittest.TestCase):
    """The clock half of "the future is never postable".

    Both guards used to compare DAYS only, which was sufficient while the clock
    component could only come from the device's own ``now``. Once an operator
    can name a time, 23:59 chosen at 09:00 is as future as tomorrow — and a
    forward-stamped stock entry distorts every as-of valuation until it is
    reached, then makes a legitimate finish fail as "before its transfer".
    """

    def test_same_day_future_clock_is_refused(self):
        from jarz_pos.api import manufacturing

        thrown = []

        def fake_throw(msg, exc=None):
            thrown.append(str(msg))
            raise RuntimeError("thrown")

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe,                 patch.object(manufacturing, "_resolve_now_datetime",
                             return_value=datetime(2026, 9, 8, 9, 0, 0)),                 patch.object(manufacturing, "_resolve_user_roles", return_value=set()):
            mock_frappe.throw.side_effect = fake_throw
            with self.assertRaises(RuntimeError):
                manufacturing._assert_posting_date_allowed(
                    datetime(2026, 9, 8, 23, 59, 0)
                )

        self.assertTrue(thrown, "a future clock time on today must be refused")
        self.assertIn("future", thrown[0].lower())

    def test_same_day_past_clock_is_allowed(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe,                 patch.object(manufacturing, "_resolve_now_datetime",
                             return_value=datetime(2026, 9, 8, 9, 0, 0)),                 patch.object(manufacturing, "_resolve_user_roles", return_value=set()):
            mock_frappe.throw.side_effect = AssertionError("must not throw")
            manufacturing._assert_posting_date_allowed(
                datetime(2026, 9, 8, 8, 30, 0)
            )

    def test_date_only_today_still_passes(self):
        """A date-only request carries midnight, which must not read as future.

        This is the legacy client's exact shape during the deploy window.
        """
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe,                 patch.object(manufacturing, "_resolve_now_datetime",
                             return_value=datetime(2026, 9, 8, 9, 0, 0)),                 patch.object(manufacturing, "_resolve_user_roles", return_value=set()):
            mock_frappe.throw.side_effect = AssertionError("must not throw")
            manufacturing._assert_posting_date_allowed(datetime(2026, 9, 8, 0, 0, 0))

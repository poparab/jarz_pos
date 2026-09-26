"""A bill paid after the days it covers is booked to the months it covers.

Meta charges paid ads in arrears: 5 October's charge pays for 25 September to
5 October. ``JarzExpenseRequest.on_submit`` books September's six days at
30 September against Accrued Expenses, and the payment on the 5th clears that
liability and expenses only October's five days.

Pure ``unittest``: the arithmetic is a pure module, and ``on_submit`` /
``on_cancel`` are driven against recording fakes (not ``MagicMock`` journal
entries, which would accept any attribute and assert nothing).
"""

import json
import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import jarz_pos
from jarz_pos.doctype.jarz_expense_request.jarz_expense_request import (
    JarzExpenseRequest,
)
from jarz_pos.services.expense_periods import (
    MAX_PERIOD_DAYS,
    PeriodError,
    earlier_month_shares,
    split_by_month,
    validate_period,
)

MODULE = "jarz_pos.doctype.jarz_expense_request.jarz_expense_request"
ACCRUED = "Accrued Expenses - J"
ADS = "Paid Ads - Media Buying - J"


class TestSplitByMonth(unittest.TestCase):
    def test_meta_cycle_across_a_month_end_splits_by_days(self):
        shares = split_by_month(1100, date(2026, 9, 25), date(2026, 10, 5))
        self.assertEqual([(s.month, s.days, s.amount) for s in shares], [("2026-09", 6, 600.0), ("2026-10", 5, 500.0)])
        self.assertEqual(shares[0].month_end, date(2026, 9, 30))

    def test_rounding_never_loses_a_piastre(self):
        shares = split_by_month(1000, date(2026, 9, 29), date(2026, 10, 1))
        self.assertEqual([s.amount for s in shares], [666.67, 333.33])
        self.assertAlmostEqual(sum(s.amount for s in shares), 1000, places=2)

    def test_three_months(self):
        shares = split_by_month(9200, date(2026, 8, 1), date(2026, 10, 31))
        self.assertEqual([s.month for s in shares], ["2026-08", "2026-09", "2026-10"])
        self.assertEqual([s.days for s in shares], [31, 30, 31])
        self.assertAlmostEqual(sum(s.amount for s in shares), 9200, places=2)

    def test_single_month(self):
        shares = split_by_month(500, date(2026, 9, 5), date(2026, 9, 15))
        self.assertEqual([(s.month, s.amount) for s in shares], [("2026-09", 500)])


class TestEarlierMonthShares(unittest.TestCase):
    def test_period_inside_the_payment_month_posts_nothing_extra(self):
        self.assertEqual(earlier_month_shares(500, date(2026, 9, 5), date(2026, 9, 15), date(2026, 9, 15)), [])

    def test_only_months_before_the_payment_are_returned(self):
        shares = earlier_month_shares(1100, date(2026, 9, 25), date(2026, 10, 5), date(2026, 10, 5))
        self.assertEqual([(s.month, s.amount) for s in shares], [("2026-09", 600.0)])

    def test_period_wholly_in_the_previous_month(self):
        shares = earlier_month_shares(3000, date(2026, 9, 1), date(2026, 9, 30), date(2026, 10, 2))
        self.assertEqual([(s.month, s.amount) for s in shares], [("2026-09", 3000)])


class TestValidatePeriod(unittest.TestCase):
    def test_reversed_period_is_refused(self):
        with self.assertRaises(PeriodError):
            validate_period(date(2026, 9, 10), date(2026, 9, 5), date(2026, 9, 20))

    def test_period_ending_after_the_payment_is_refused(self):
        with self.assertRaises(PeriodError):
            validate_period(date(2026, 9, 10), date(2026, 9, 25), date(2026, 9, 20))

    def test_period_ending_on_the_payment_day_is_fine(self):
        validate_period(date(2026, 9, 5), date(2026, 9, 15), date(2026, 9, 15))

    def test_a_year_typo_is_refused(self):
        with self.assertRaises(PeriodError):
            validate_period(date(2025, 9, 5), date(2026, 9, 15), date(2026, 9, 15))
        self.assertEqual(MAX_PERIOD_DAYS, 366)


class _RecordingJournalEntry:
    def __init__(self, name):
        self.name = name
        self.doctype = "Journal Entry"
        self.accounts = []
        self.flags = SimpleNamespace(ignore_permissions=False)
        self.inserted = self.submitted = self.cancelled = False
        self.docstatus = 1

    def append(self, table, row):
        getattr(self, table).append(row)

    def insert(self):
        self.inserted = True

    def submit(self):
        self.submitted = True

    def cancel(self):
        self.cancelled = True


def _request(period_from=None, period_to=None, amount=1100, expense_date="2026-10-05"):
    return SimpleNamespace(
        name="JEXP-00099",
        journal_entry=None,
        company="JARZ",
        expense_date=expense_date,
        expense_time=None,
        remarks="Meta ads",
        amount=amount,
        reason_account=ADS,
        paying_account="Bank - J",
        reason_label="Paid Ads - Media Buying",
        payment_source_label="Bank",
        period_from=period_from,
        period_to=period_to,
        db_set=MagicMock(),
    )


def _balance(je):
    return round(
        sum(r.get("debit_in_account_currency", 0) for r in je.accounts)
        - sum(r.get("credit_in_account_currency", 0) for r in je.accounts),
        2,
    )


class TestOnSubmitSplitsThePayment(unittest.TestCase):
    def _run(self, doc):
        created = []

        def new_doc(doctype):
            je = _RecordingJournalEntry(f"ACC-JV-{len(created) + 1:05d}")
            created.append(je)
            return je

        mock_frappe = MagicMock()
        mock_frappe.new_doc.side_effect = new_doc
        mock_frappe.db.get_value.side_effect = lambda doctype, *args, **kwargs: "J" if doctype == "Company" else ACCRUED

        stub = MagicMock()
        stub._strip_je_tag_lookalikes.side_effect = lambda value: value
        posting_frappe = MagicMock()
        posting_frappe.get_meta.return_value.get_field.return_value = None

        with patch.dict("sys.modules", {"jarz_pos.services.delivery_handling": stub}), patch(
            f"{MODULE}.frappe", mock_frappe
        ), patch(f"{MODULE}._", side_effect=lambda text: text), patch(
            "jarz_pos.utils.posting_datetime.frappe", posting_frappe
        ):
            JarzExpenseRequest.on_submit(doc)
        # new_doc order: the payment entry first, then the month-end accruals.
        return created[0], created[1:]

    def test_cross_month_payment_books_the_earlier_days_at_month_end(self):
        doc = _request("2026-09-25", "2026-10-05")
        payment, accruals = self._run(doc)

        self.assertEqual(len(accruals), 1)
        accrual = accruals[0]
        self.assertEqual(accrual.posting_date, date(2026, 9, 30))
        self.assertTrue(accrual.submitted)
        self.assertEqual(
            [(r["account"], r["debit_in_account_currency"], r["credit_in_account_currency"]) for r in accrual.accounts],
            [(ADS, 600.0, 0), (ACCRUED, 0, 600.0)],
        )

        self.assertEqual(payment.posting_date, "2026-10-05")
        self.assertEqual(
            [(r["account"], r["debit_in_account_currency"], r["credit_in_account_currency"]) for r in payment.accounts],
            [(ADS, 500.0, 0), (ACCRUED, 600.0, 0), ("Bank - J", 0, 1100.0)],
        )
        self.assertEqual(_balance(payment), 0)
        self.assertEqual(_balance(accrual), 0)
        doc.db_set.assert_any_call("period_journal_entries", accrual.name)

    def test_period_inside_the_month_posts_one_plain_entry(self):
        payment, accruals = self._run(_request("2026-10-01", "2026-10-05"))
        self.assertEqual(accruals, [])
        self.assertEqual([r["account"] for r in payment.accounts], [ADS, "Bank - J"])

    def test_period_wholly_in_the_previous_month_expenses_nothing_now(self):
        payment, accruals = self._run(_request("2026-09-01", "2026-09-30", amount=3000, expense_date="2026-10-02"))
        self.assertEqual(len(accruals), 1)
        self.assertEqual(
            [(r["account"], r["debit_in_account_currency"], r["credit_in_account_currency"]) for r in payment.accounts],
            [(ACCRUED, 3000, 0), ("Bank - J", 0, 3000.0)],
        )

    def test_no_period_is_unchanged(self):
        payment, accruals = self._run(_request())
        self.assertEqual(accruals, [])
        self.assertEqual(
            [(r["account"], r["debit_in_account_currency"]) for r in payment.accounts],
            [(ADS, 1100.0), ("Bank - J", 0)],
        )


class _Thrown(Exception):
    pass


def _validate(kind, period_from=None, period_to=None, flagged=False):
    doc = SimpleNamespace(
        expense_kind=kind, period_from=period_from, period_to=period_to,
        expense_date="2026-10-05", reason_account=ADS, reason_label="Paid Ads",
    )
    mock_frappe = MagicMock()
    mock_frappe.throw.side_effect = _Thrown
    mock_frappe.db.has_column.return_value = True
    mock_frappe.db.get_value.return_value = 1 if flagged else 0
    with patch(f"{MODULE}.frappe", mock_frappe), patch(f"{MODULE}._", side_effect=lambda text: text):
        JarzExpenseRequest._validate_service_period(doc)


class TestPeriodIsForAdHocOnly(unittest.TestCase):
    """Monthly Expenses pays Recurring/Salary rows with no period input."""

    def test_flagged_ledger_does_not_block_a_recurring_payment(self):
        _validate("Recurring", flagged=True)  # no raise

    def test_a_period_on_a_salary_row_is_refused(self):
        with self.assertRaises(_Thrown):
            _validate("Salary", "2026-09-25", "2026-10-05")

    def test_flagged_ad_hoc_without_period_is_refused(self):
        with self.assertRaises(_Thrown):
            _validate("Ad-hoc", flagged=True)

    def test_blank_kind_counts_as_ad_hoc(self):
        with self.assertRaises(_Thrown):
            _validate(None, flagged=True)

    def test_valid_ad_hoc_period_passes(self):
        _validate("Ad-hoc", "2026-09-25", "2026-10-05", flagged=True)


class TestSchemaReviewFixes(unittest.TestCase):
    def setUp(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(jarz_pos.__file__)),
            "doctype", "jarz_expense_request", "jarz_expense_request.json",
        )
        with open(path, encoding="utf-8") as handle:
            self.fields = {f["fieldname"]: f for f in json.load(handle)["fields"]}

    def test_amended_copy_does_not_inherit_the_journal_entry(self):
        # on_submit returns early when journal_entry is set, so an inherited
        # name meant an amended expense posted nothing and read "Approved".
        self.assertEqual(self.fields["journal_entry"].get("no_copy"), 1)

    def test_desk_shows_the_period_section_for_ad_hoc_expenses(self):
        self.assertIn("Ad-hoc", self.fields["section_break_period"]["depends_on"])
        self.assertNotIn("period_from", self.fields["section_break_period"]["depends_on"])


class TestOnCancelReversesEveryEntry(unittest.TestCase):
    def test_payment_and_accruals_are_all_cancelled(self):
        entries = {n: _RecordingJournalEntry(n) for n in ("ACC-JV-1", "ACC-JV-2", "ACC-JV-3")}
        mock_frappe = MagicMock()
        mock_frappe.db.exists.return_value = True
        mock_frappe.get_doc.side_effect = lambda dt, name: entries[name]
        doc = SimpleNamespace(journal_entry="ACC-JV-1", period_journal_entries="ACC-JV-2\nACC-JV-3\n")
        with patch(f"{MODULE}.frappe", mock_frappe):
            JarzExpenseRequest.on_cancel(doc)
        self.assertTrue(all(je.cancelled for je in entries.values()))

    def test_already_cancelled_entries_are_skipped(self):
        je = _RecordingJournalEntry("ACC-JV-1")
        je.docstatus = 2
        mock_frappe = MagicMock()
        mock_frappe.db.exists.return_value = True
        mock_frappe.get_doc.return_value = je
        with patch(f"{MODULE}.frappe", mock_frappe):
            JarzExpenseRequest.on_cancel(SimpleNamespace(journal_entry="ACC-JV-1", period_journal_entries=None))
        self.assertFalse(je.cancelled)


class TestSchemaAndWiring(unittest.TestCase):
    def test_period_fields_are_declared(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(jarz_pos.__file__)),
            "doctype", "jarz_expense_request", "jarz_expense_request.json",
        )
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        fields = {f["fieldname"]: f for f in payload["fields"]}
        self.assertEqual(fields["period_from"]["fieldtype"], "Date")
        self.assertEqual(fields["period_to"]["fieldtype"], "Date")
        self.assertTrue(fields["period_journal_entries"].get("read_only"))
        self.assertTrue(fields["period_journal_entries"].get("no_copy"))
        for name in ("section_break_period", "period_from", "period_to", "period_journal_entries"):
            self.assertIn(name, payload["field_order"])

    def test_account_flag_seeder_runs_before_migrate(self):
        from jarz_pos import hooks

        self.assertIn("jarz_pos.utils.cleanup.ensure_account_requires_period_field", hooks.before_migrate)

    def test_patch_is_registered(self):
        path = os.path.join(os.path.dirname(os.path.abspath(jarz_pos.__file__)), "patches.txt")
        with open(path, encoding="utf-8") as handle:
            self.assertIn("jarz_pos.Patches.v1_9.create_accrued_expenses_account", handle.read().split())


if __name__ == "__main__":
    unittest.main()

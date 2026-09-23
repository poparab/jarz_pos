"""Tests for Delivery Partner recurring fees (daily / weekly / monthly).

Deliverk charges 50 EGP every day whether or not it delivered for us. These pin the
period grid (what "a day / week / month" means, and when it is due), the catch-up
and re-anchoring rules, and the scheduler's never-raise contract. The ledger side
is exercised end to end on staging; here the settlement JE is checked with mocks.
"""

import datetime
import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.services import partner_recurring_fees as prf

D = datetime.date


class TestPeriodBounds(unittest.TestCase):
	def test_daily_is_one_calendar_day(self):
		self.assertEqual(prf.period_bounds("2026-09-23", "Daily", 0), (D(2026, 9, 23), D(2026, 9, 23)))
		self.assertEqual(prf.period_bounds("2026-09-23", "Daily", 8), (D(2026, 10, 1), D(2026, 10, 1)))

	def test_weekly_is_seven_days_from_the_anchor(self):
		self.assertEqual(prf.period_bounds("2026-09-23", "Weekly", 0), (D(2026, 9, 23), D(2026, 9, 29)))
		self.assertEqual(prf.period_bounds("2026-09-23", "Weekly", 1), (D(2026, 9, 30), D(2026, 10, 6)))

	def test_monthly_ends_the_day_before_the_next_anchor(self):
		self.assertEqual(prf.period_bounds("2026-09-23", "Monthly", 0), (D(2026, 9, 23), D(2026, 10, 22)))
		self.assertEqual(prf.period_bounds("2026-12-15", "Monthly", 1), (D(2027, 1, 15), D(2027, 2, 14)))

	def test_monthly_from_the_31st_does_not_drift_to_the_28th(self):
		# Computed from the anchor each time: Feb clamps, March goes back to the 31st.
		self.assertEqual(prf.period_bounds("2027-01-31", "Monthly", 1)[0], D(2027, 2, 28))
		self.assertEqual(prf.period_bounds("2027-01-31", "Monthly", 2)[0], D(2027, 3, 31))
		# Periods tile with no gap and no overlap.
		for i in range(12):
			_, end = prf.period_bounds("2027-01-31", "Monthly", i)
			nxt, _ = prf.period_bounds("2027-01-31", "Monthly", i + 1)
			self.assertEqual(end + datetime.timedelta(days=1), nxt)

	def test_unknown_frequency_throws(self):
		with self.assertRaises(frappe.ValidationError):
			prf.period_bounds("2026-09-23", "Hourly", 0)


class TestDuePeriods(unittest.TestCase):
	def test_first_day_is_due_on_the_day(self):
		self.assertEqual(
			prf.due_periods("2026-09-23", "Daily", "2026-09-23"),
			[(D(2026, 9, 23), D(2026, 9, 23))],
		)

	def test_nothing_due_before_the_start(self):
		self.assertEqual(prf.due_periods("2026-09-23", "Daily", "2026-09-22"), [])

	def test_catch_up_fills_every_missed_day(self):
		due = prf.due_periods("2026-09-20", "Daily", "2026-09-23")
		self.assertEqual([p[0] for p in due], [D(2026, 9, d) for d in (20, 21, 22, 23)])

	def test_already_accrued_days_are_skipped(self):
		existing = [(D(2026, 9, 20), D(2026, 9, 20)), (D(2026, 9, 21), D(2026, 9, 21))]
		due = prf.due_periods("2026-09-20", "Daily", "2026-09-23", existing=existing)
		self.assertEqual([p[0] for p in due], [D(2026, 9, 22), D(2026, 9, 23)])

	def test_weekly_is_due_on_its_first_day_only(self):
		self.assertEqual(len(prf.due_periods("2026-09-01", "Weekly", "2026-09-07")), 1)
		self.assertEqual(len(prf.due_periods("2026-09-01", "Weekly", "2026-09-08")), 2)

	def test_end_date_stops_the_contract(self):
		due = prf.due_periods("2026-09-20", "Daily", "2026-09-30", end_date="2026-09-22")
		self.assertEqual([p[0] for p in due], [D(2026, 9, 20), D(2026, 9, 21), D(2026, 9, 22)])

	def test_reanchoring_never_bills_an_accrued_day_twice(self):
		# A weekly fee accrued 1-7 Sep, then the start date is moved to 3 Sep. The
		# new grid's first week (3-9) overlaps days already paid for, so it is not
		# billed again; the next one (10-16) is.
		existing = [(D(2026, 9, 1), D(2026, 9, 7))]
		due = prf.due_periods("2026-09-03", "Weekly", "2026-09-12", existing=existing)
		self.assertEqual(due, [(D(2026, 9, 10), D(2026, 9, 16))])

	def test_switching_daily_to_weekly_skips_the_overlap(self):
		existing = [(D(2026, 9, d), D(2026, 9, d)) for d in range(1, 6)]
		due = prf.due_periods("2026-09-01", "Weekly", "2026-09-08", existing=existing)
		self.assertEqual(due, [(D(2026, 9, 8), D(2026, 9, 14))])

	def test_runaway_backfill_is_capped_per_run(self):
		due = prf.due_periods("2020-01-01", "Daily", "2026-09-23", limit=10)
		self.assertEqual(len(due), 10)
		self.assertEqual(due[0][0], D(2020, 1, 1))

	def test_missing_config_is_a_no_op(self):
		self.assertEqual(prf.due_periods(None, "Daily", "2026-09-23"), [])
		self.assertEqual(prf.due_periods("2026-09-23", None, "2026-09-23"), [])
		self.assertEqual(prf.due_periods("2026-09-23", "", "2026-09-23"), [])


class TestLabels(unittest.TestCase):
	def test_labels(self):
		self.assertEqual(prf.accrual_label({"frequency": "Daily", "period_start": "2026-09-23", "period_end": "2026-09-23"}), "Daily fee 2026-09-23")
		self.assertEqual(
			prf.accrual_label({"frequency": "Weekly", "period_start": "2026-09-23", "period_end": "2026-09-29"}),
			"Weekly fee 2026-09-23 to 2026-09-29",
		)


class TestDeliveryPartnerValidation(unittest.TestCase):
	def _doc(self, **kw):
		from jarz_pos.doctype.delivery_partner.delivery_partner import DeliveryPartner

		doc = DeliveryPartner.__new__(DeliveryPartner)
		values = {
			"partner_name": "P",
			"settlement_account": "Deliverk - J",
			"recurring_fee_amount": 50,
			"recurring_fee_frequency": "Daily",
			"recurring_fee_start_date": "2026-09-23",
			"recurring_fee_end_date": None,
		}
		values.update(kw)
		doc.__dict__.update(values)
		return doc

	def test_valid_config_passes(self):
		self._doc().validate()

	def test_no_fee_needs_nothing(self):
		self._doc(recurring_fee_amount=0, recurring_fee_frequency=None, recurring_fee_start_date=None).validate()

	def test_fee_requires_frequency_start_and_account(self):
		for missing in ("recurring_fee_frequency", "recurring_fee_start_date", "settlement_account"):
			with self.subTest(missing=missing), self.assertRaises(frappe.ValidationError):
				self._doc(**{missing: None}).validate()

	def test_negative_fee_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._doc(recurring_fee_amount=-5).validate()

	def test_end_before_start_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._doc(recurring_fee_end_date="2026-09-01").validate()


class TestSchedulerNeverRaises(unittest.TestCase):
	@patch.object(prf, "accrue_partner")
	@patch.object(prf, "frappe")
	def test_one_partner_failing_does_not_stop_the_next(self, mock_frappe, mock_accrue):
		mock_frappe.get_all.return_value = ["Bad", "Good"]
		mock_accrue.side_effect = [RuntimeError("closed period"), ["DPFEE-00001"]]
		out = prf.run_partner_recurring_fees(up_to="2026-09-23")
		self.assertEqual(out, {"Bad": "error", "Good": 1})
		mock_frappe.db.rollback.assert_called_once()
		mock_frappe.log_error.assert_called_once()

	@patch.object(prf, "frappe")
	def test_pre_migrate_schema_is_a_no_op(self, mock_frappe):
		mock_frappe.get_all.side_effect = Exception("Unknown column recurring_fee_amount")
		self.assertEqual(prf.run_partner_recurring_fees(), {})


class TestSettlementJeRecurringLine(unittest.TestCase):
	"""The weekly transfer clears recurring fees off the payable on their own line."""

	@patch("jarz_pos.services.delivery_handling.validate_account_exists")
	@patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value="Freight - J")
	@patch("jarz_pos.services.delivery_handling.get_delivery_partner_supplier", return_value="Deliverk")
	@patch("jarz_pos.services.delivery_handling._get_partner_settlement_account", return_value="Deliverk - J")
	@patch("jarz_pos.services.delivery_handling._find_partner_settlement_je", return_value=None)
	@patch("jarz_pos.services.delivery_handling._tag_journal_entry")
	@patch("jarz_pos.services.delivery_handling.frappe")
	def test_lines(self, mock_frappe, *_):
		from jarz_pos.services.delivery_handling import create_partner_settlement_je

		je = MagicMock()
		je.name = "JE-1"
		lines = []
		je.append.side_effect = lambda _t, row: lines.append(row)
		mock_frappe.new_doc.return_value = je
		mock_frappe.db.exists.return_value = True

		create_partner_settlement_je(
			delivery_partner="Deliverk",
			company="JARZ",
			bank_account="Bank - J",
			order_fee_total=110,
			recurring_fee_total=350,
			extra_charges=[{"description": "Waiting", "amount": 20}],
			token="t",
		)
		by_remark = {r.get("user_remark"): r for r in lines}
		self.assertEqual(by_remark["Delivery fees – Deliverk"]["debit_in_account_currency"], 110)
		self.assertEqual(by_remark["Recurring fees – Deliverk"]["debit_in_account_currency"], 350)
		self.assertEqual(by_remark["Recurring fees – Deliverk"]["party"], "Deliverk")
		bank = [r for r in lines if r["account"] == "Bank - J"][0]
		self.assertEqual(bank["credit_in_account_currency"], 480)
		je.submit.assert_called_once()

	@patch("jarz_pos.services.delivery_handling.validate_account_exists")
	@patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value="Freight - J")
	@patch("jarz_pos.services.delivery_handling.get_delivery_partner_supplier", return_value="Deliverk")
	@patch("jarz_pos.services.delivery_handling._get_partner_settlement_account", return_value="Deliverk - J")
	@patch("jarz_pos.services.delivery_handling._find_partner_settlement_je", return_value=None)
	@patch("jarz_pos.services.delivery_handling._tag_journal_entry")
	@patch("jarz_pos.services.delivery_handling.frappe")
	def test_recurring_only_settlement(self, mock_frappe, *_):
		from jarz_pos.services.delivery_handling import create_partner_settlement_je

		je = MagicMock()
		lines = []
		je.append.side_effect = lambda _t, row: lines.append(row)
		mock_frappe.new_doc.return_value = je
		mock_frappe.db.exists.return_value = True

		create_partner_settlement_je(
			delivery_partner="Deliverk", company="JARZ", bank_account="Bank - J",
			order_fee_total=0, recurring_fee_total=50, token="t",
		)
		self.assertEqual(len(lines), 2)
		self.assertEqual(sum(r["debit_in_account_currency"] for r in lines), 50)
		self.assertEqual(sum(r["credit_in_account_currency"] for r in lines), 50)

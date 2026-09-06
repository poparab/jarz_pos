"""Unit tests for the Monthly Expenses money maths.

Everything that decides *how much is still owed* is a pure function in
``jarz_pos.api.monthly_expenses``, so these run without a site or a database and
still exercise the real code — no test here asserts against a value the test
itself computed, and every fixture is fed through the production function.

The two hard parts are covered deliberately:

* **attribution** — GL money is only credited to an expense when exactly one due
  expense could have produced it. The shared-account case must NOT be split.
* **period vs posting month** — August rent paid in September must count as paid
  in August and must NOT make September look paid.
"""

from datetime import date
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


MONTH_START = date(2026, 8, 1)
MONTH_END = date(2026, 8, 31)
MONTH_KEY = "2026-08"
RENT_DOKKI = "Rent - Dokki - J"
RENT_FACTORY = "Rent - Factory - J"


def _item(name, account=RENT_DOKKI, amount=47000.0, **overrides):
	row = {
		"name": name,
		"expense_name": name,
		"category": "Rent",
		"status": "Active",
		"amount": amount,
		"currency": "EGP",
		"frequency": "Monthly",
		"monthly_equivalent": amount,
		"day_of_month": 5,
		"expense_account": account,
		"cost_center": "Main - J",
		"supplier": None,
		"default_paying_account": None,
		"start_date": date(2025, 1, 1),
		"end_date": None,
		"auto_repeat": None,
		"notes": None,
	}
	row.update(overrides)
	return row


def _request(**overrides):
	row = {
		"name": "JER-0001",
		"amount": 47000.0,
		"currency": "EGP",
		"expense_date": date(2026, 8, 5),
		"expense_month": MONTH_KEY,
		"period_month": MONTH_KEY,
		"expense_kind": "Recurring",
		"recurring_expense": "REC-1",
		"employee": None,
		"reason_account": RENT_DOKKI,
		"reason_label": "Rent - Dokki",
		"paying_account": "Cash - J",
		"payment_source_label": "Cash",
		"journal_entry": "ACC-JV-0001",
		"remarks": None,
		"requested_by": "manager@example.com",
		"owner": "manager@example.com",
		"company": "JARZ",
	}
	row.update(overrides)
	return row


def _build(items, month_start=MONTH_START, month_end=MONTH_END, **kwargs):
	from jarz_pos.api.monthly_expenses import _build_registry_rows

	rows, leftovers = _build_registry_rows(items, month_start, month_end, **kwargs)
	return {r["name"]: r for r in rows}, leftovers


# ── status ladder ─────────────────────────────────────────────────────────


class TestPaymentStatus(unittest.TestCase):
	"""The five transitions the screen colours rows by."""

	def _status(self, due, paid, due_this_month=True):
		from jarz_pos.api.monthly_expenses import _payment_status

		return _payment_status(due, paid, due_this_month)

	def test_not_due_when_the_month_has_no_occurrence(self):
		self.assertEqual(self._status(47000, 0, due_this_month=False), "Not Due")

	def test_not_due_when_nothing_is_owed(self):
		self.assertEqual(self._status(0, 0), "Not Due")

	def test_unpaid_when_nothing_has_been_paid(self):
		self.assertEqual(self._status(47000, 0), "Unpaid")

	def test_partial_when_some_has_been_paid(self):
		self.assertEqual(self._status(47000, 20000), "Partial")

	def test_paid_when_the_full_amount_landed(self):
		self.assertEqual(self._status(47000, 47000), "Paid")

	def test_overpaid_beyond_tolerance(self):
		self.assertEqual(self._status(47000, 94000), "Overpaid")

	def test_half_a_pound_short_still_counts_as_paid(self):
		# Tolerance is absolute, not a ratio: a rounding wobble is not a debt.
		self.assertEqual(self._status(47000, 46999.6), "Paid")

	def test_a_pound_short_is_partial(self):
		self.assertEqual(self._status(47000, 46999.0), "Partial")

	def test_half_a_pound_over_is_still_paid_not_overpaid(self):
		self.assertEqual(self._status(47000, 47000.4), "Paid")

	def test_a_pound_over_is_overpaid(self):
		self.assertEqual(self._status(47000, 47001.0), "Overpaid")

	def test_a_stray_piastre_does_not_read_as_partial(self):
		self.assertEqual(self._status(47000, 0.2), "Unpaid")


# ── overpayment guard ─────────────────────────────────────────────────────


class TestOverpayMaths(unittest.TestCase):
	def _excess(self, due, paid, requested):
		from jarz_pos.api.monthly_expenses import _overpay_excess

		return _overpay_excess(due, paid, requested)

	def test_paying_exactly_what_remains_is_not_an_overpayment(self):
		self.assertEqual(self._excess(47000, 20000, 27000), 0.0)

	def test_paying_less_than_what_remains_is_not_an_overpayment(self):
		self.assertEqual(self._excess(47000, 20000, 1000), 0.0)

	def test_paying_past_the_due_amount_reports_the_excess(self):
		self.assertEqual(self._excess(47000, 20000, 30000), 3000.0)

	def test_paying_a_not_due_item_is_entirely_excess(self):
		self.assertEqual(self._excess(0, 0, 5000), 5000.0)

	def test_within_half_a_pound_is_tolerated(self):
		self.assertEqual(self._excess(47000, 0, 47000.4), 0.0)


class TestOverpayGuard(unittest.TestCase):
	def _guard(self, due, paid, requested, allow_overpay):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = RuntimeError("refused")
		with patch.object(monthly_expenses, "frappe", mock_frappe):
			try:
				result = monthly_expenses._guard_overpay(
					"Rent - Dokki", due, paid, requested, allow_overpay, MONTH_KEY
				)
			except RuntimeError:
				return mock_frappe, None
		return mock_frappe, result

	def test_a_legitimate_payment_passes_without_a_throw(self):
		mock_frappe, result = self._guard(47000, 20000, 27000, False)
		mock_frappe.throw.assert_not_called()
		self.assertEqual(result, 0.0)

	def test_an_overpayment_is_refused(self):
		mock_frappe, result = self._guard(47000, 47000, 47000, False)
		self.assertIsNone(result)
		mock_frappe.throw.assert_called_once()

	def test_the_refusal_states_due_already_paid_and_requested(self):
		mock_frappe, _result = self._guard(47000, 30000, 20000, False)
		message = str(mock_frappe.throw.call_args[0][0])
		# A manager can only act on this if all three numbers are in front of
		# them — the usual cause is an Auto Repeat JE posted in Desk.
		self.assertIn("47000", message)
		self.assertIn("30000", message)
		self.assertIn("20000", message)
		self.assertIn("3000", message)  # the excess
		self.assertIn("allow_overpay", message)

	def test_allow_overpay_lets_it_through_and_returns_the_excess(self):
		mock_frappe, result = self._guard(47000, 47000, 47000, True)
		mock_frappe.throw.assert_not_called()
		self.assertEqual(result, 47000.0)


class TestAsBool(unittest.TestCase):
	def test_truthy_wire_values(self):
		from jarz_pos.api.monthly_expenses import _as_bool

		for value in (1, "1", "true", "True", "yes", "on", True):
			self.assertTrue(_as_bool(value), value)

	def test_falsy_wire_values(self):
		from jarz_pos.api.monthly_expenses import _as_bool

		for value in (0, "0", "", "false", None, False, "no"):
			self.assertFalse(_as_bool(value), value)


# ── attribution ───────────────────────────────────────────────────────────


class TestUnlinkedByAccount(unittest.TestCase):
	"""Both sides of the subtraction are POSTING-month scoped.

	``gl_by_account`` is what hit the ledger this month, so the only thing that
	may cancel it is an app payment whose Journal Entry also hit the ledger this
	month — whatever period that payment settles.
	"""

	def _unlinked(self, gl, linked_posted=None):
		from jarz_pos.api.monthly_expenses import _unlinked_by_account

		return _unlinked_by_account(gl, linked_posted)

	def test_gl_with_no_app_payment_is_entirely_unexplained(self):
		self.assertEqual(self._unlinked({RENT_DOKKI: 47000}, {}), {RENT_DOKKI: 47000.0})

	def test_gl_fully_explained_by_an_app_payment_leaves_nothing(self):
		self.assertEqual(
			self._unlinked({RENT_DOKKI: 47000}, {RENT_DOKKI: 47000}), {RENT_DOKKI: 0.0}
		)

	def test_a_desk_entry_on_top_of_an_app_payment_shows_as_unexplained(self):
		self.assertEqual(
			self._unlinked({RENT_DOKKI: 94000}, {RENT_DOKKI: 47000}), {RENT_DOKKI: 47000.0}
		)

	def test_a_month_with_no_gl_of_its_own_never_goes_negative(self):
		# August viewed after its rent was paid in September: nothing posted in
		# August, and a payment posted elsewhere cannot make August negative.
		self.assertEqual(self._unlinked({RENT_DOKKI: 0}, {RENT_DOKKI: 47000}), {RENT_DOKKI: 0.0})

	def test_money_this_app_posted_this_month_explains_this_months_gl(self):
		# September's ledger holds the JE for August's rent. It is subtracted
		# because it POSTED here, not because of the period it settles.
		self.assertEqual(
			self._unlinked({RENT_DOKKI: 47000}, {RENT_DOKKI: 47000}),
			{RENT_DOKKI: 0.0},
		)
		self.assertEqual(self._unlinked({RENT_DOKKI: 47000}, {}), {RENT_DOKKI: 47000.0})

	def test_a_payment_posted_in_another_month_does_not_cancel_this_months_gl(self):
		# THE C1 REGRESSION, at its smallest. August's Desk-posted rent is real
		# money in August's ledger; a September-posted payment is not in this
		# bucket at all, so it cannot make August's 47,000 disappear.
		self.assertEqual(self._unlinked({RENT_DOKKI: 47000}, {}), {RENT_DOKKI: 47000.0})


class TestAttributeUnlinked(unittest.TestCase):
	def _attribute(self, rows, unlinked):
		from jarz_pos.api.monthly_expenses import _attribute_unlinked

		return _attribute_unlinked(rows, unlinked)

	def test_a_lone_due_item_absorbs_the_account_money_and_is_flagged_inferred(self):
		rows = [{"name": "REC-1", "expense_account": RENT_DOKKI, "due_this_month": True}]
		attribution, leftovers = self._attribute(rows, {RENT_DOKKI: 47000})
		self.assertEqual(attribution["REC-1"]["paid_unlinked"], 47000.0)
		self.assertTrue(attribution["REC-1"]["inferred"])
		self.assertFalse(attribution["REC-1"]["shared_account"])
		self.assertEqual(leftovers, {})

	def test_a_lone_due_item_with_no_account_money_is_not_inferred(self):
		rows = [{"name": "REC-1", "expense_account": RENT_DOKKI, "due_this_month": True}]
		attribution, leftovers = self._attribute(rows, {RENT_DOKKI: 0})
		self.assertEqual(attribution["REC-1"]["paid_unlinked"], 0.0)
		self.assertFalse(attribution["REC-1"]["inferred"])
		self.assertEqual(leftovers, {})

	def test_two_due_items_on_one_account_share_it_and_neither_is_credited(self):
		rows = [
			{"name": "REC-1", "expense_account": RENT_DOKKI, "due_this_month": True},
			{"name": "REC-2", "expense_account": RENT_DOKKI, "due_this_month": True},
		]
		attribution, leftovers = self._attribute(rows, {RENT_DOKKI: 47000})
		for name in ("REC-1", "REC-2"):
			self.assertEqual(attribution[name]["paid_unlinked"], 0.0)
			self.assertTrue(attribution[name]["shared_account"])
			self.assertFalse(attribution[name]["inferred"])
		# Reported once, at account level — never split 23500/23500.
		self.assertEqual(leftovers, {RENT_DOKKI: 47000.0})

	def test_a_paused_sibling_does_not_make_the_account_shared(self):
		rows = [
			{"name": "REC-1", "expense_account": RENT_DOKKI, "due_this_month": True},
			{"name": "REC-2", "expense_account": RENT_DOKKI, "due_this_month": False},
		]
		attribution, leftovers = self._attribute(rows, {RENT_DOKKI: 47000})
		self.assertEqual(attribution["REC-1"]["paid_unlinked"], 47000.0)
		self.assertFalse(attribution["REC-1"]["shared_account"])
		self.assertEqual(attribution["REC-2"]["paid_unlinked"], 0.0)
		self.assertEqual(leftovers, {})

	def test_money_on_an_account_with_nothing_due_is_left_over(self):
		rows = [{"name": "REC-1", "expense_account": RENT_DOKKI, "due_this_month": False}]
		_attribution, leftovers = self._attribute(rows, {RENT_DOKKI: 1200})
		self.assertEqual(leftovers, {RENT_DOKKI: 1200.0})


# ── the row builder, end to end ───────────────────────────────────────────


class TestRegistryRowMaths(unittest.TestCase):
	def test_nothing_paid_is_unpaid_and_owes_the_whole_amount(self):
		rows, _leftovers = _build([_item("REC-1")])
		row = rows["REC-1"]
		self.assertTrue(row["due_this_month"])
		self.assertEqual(row["due_amount"], 47000.0)
		self.assertEqual(row["paid_amount"], 0.0)
		self.assertEqual(row["remaining"], 47000.0)
		self.assertEqual(row["payment_status"], "Unpaid")

	def test_partial_payment_leaves_the_rest_outstanding(self):
		rows, _leftovers = _build(
			[_item("REC-1")], paid_linked_by_item={"REC-1": 20000}
		)
		row = rows["REC-1"]
		self.assertEqual(row["paid_linked"], 20000.0)
		self.assertEqual(row["paid_unlinked"], 0.0)
		self.assertEqual(row["remaining"], 27000.0)
		self.assertEqual(row["payment_status"], "Partial")

	def test_paying_the_remainder_settles_the_month(self):
		rows, _leftovers = _build(
			[_item("REC-1")],
			paid_linked_by_item={"REC-1": 47000},
			gl_by_account={RENT_DOKKI: 47000},
			linked_posted_by_account={RENT_DOKKI: 47000},
		)
		row = rows["REC-1"]
		self.assertEqual(row["paid_amount"], 47000.0)
		self.assertEqual(row["remaining"], 0.0)
		self.assertEqual(row["payment_status"], "Paid")
		self.assertFalse(row["inferred"])

	def test_a_desk_posting_is_inferred_onto_the_only_due_item(self):
		rows, leftovers = _build(
			[_item("REC-1")], gl_by_account={RENT_DOKKI: 47000}
		)
		row = rows["REC-1"]
		self.assertEqual(row["paid_linked"], 0.0)
		self.assertEqual(row["paid_unlinked"], 47000.0)
		self.assertEqual(row["remaining"], 0.0)
		self.assertEqual(row["payment_status"], "Paid")
		self.assertTrue(row["inferred"])
		self.assertEqual(leftovers, {})

	def test_an_auto_repeat_on_top_of_an_app_payment_reads_as_overpaid(self):
		rows, _leftovers = _build(
			[_item("REC-1")],
			paid_linked_by_item={"REC-1": 47000},
			linked_posted_by_account={RENT_DOKKI: 47000},
			gl_by_account={RENT_DOKKI: 94000},
		)
		row = rows["REC-1"]
		self.assertEqual(row["paid_amount"], 94000.0)
		self.assertEqual(row["remaining"], 0.0)
		self.assertEqual(row["payment_status"], "Overpaid")

	def test_a_shared_account_is_never_split_across_its_items(self):
		rows, leftovers = _build(
			[_item("REC-1", RENT_DOKKI), _item("REC-2", RENT_DOKKI)],
			gl_by_account={RENT_DOKKI: 94000},
		)
		for name in ("REC-1", "REC-2"):
			row = rows[name]
			self.assertTrue(row["shared_account"])
			self.assertFalse(row["inferred"])
			self.assertEqual(row["paid_amount"], 0.0)
			self.assertEqual(row["remaining"], 47000.0)
			self.assertEqual(row["payment_status"], "Unpaid")
		self.assertEqual(leftovers, {RENT_DOKKI: 94000.0})

	def test_a_shared_account_still_credits_an_exact_linked_payment(self):
		rows, leftovers = _build(
			[_item("REC-1", RENT_DOKKI), _item("REC-2", RENT_DOKKI)],
			paid_linked_by_item={"REC-1": 47000},
			linked_posted_by_account={RENT_DOKKI: 47000},
			gl_by_account={RENT_DOKKI: 94000},
		)
		self.assertEqual(rows["REC-1"]["paid_amount"], 47000.0)
		self.assertEqual(rows["REC-1"]["payment_status"], "Paid")
		self.assertEqual(rows["REC-2"]["paid_amount"], 0.0)
		self.assertEqual(rows["REC-2"]["payment_status"], "Unpaid")
		self.assertEqual(leftovers, {RENT_DOKKI: 47000.0})

	def test_dedicated_accounts_are_attributed_independently(self):
		rows, leftovers = _build(
			[_item("REC-1", RENT_DOKKI), _item("REC-2", RENT_FACTORY)],
			gl_by_account={RENT_DOKKI: 47000},
		)
		self.assertEqual(rows["REC-1"]["payment_status"], "Paid")
		self.assertTrue(rows["REC-1"]["inferred"])
		self.assertEqual(rows["REC-2"]["payment_status"], "Unpaid")
		self.assertEqual(leftovers, {})

	def test_a_paused_item_is_not_due_and_owes_nothing(self):
		rows, _leftovers = _build([_item("REC-1", status="Paused")])
		row = rows["REC-1"]
		self.assertFalse(row["due_this_month"])
		self.assertEqual(row["due_amount"], 0.0)
		self.assertEqual(row["remaining"], 0.0)
		self.assertEqual(row["payment_status"], "Not Due")
		self.assertFalse(row["can_pay"])

	def test_a_quarterly_item_off_cadence_is_not_due(self):
		rows, _leftovers = _build(
			[_item("REC-1", frequency="Quarterly", start_date=date(2026, 3, 1))]
		)
		self.assertEqual(rows["REC-1"]["payment_status"], "Not Due")

	def test_an_account_outside_indirect_expenses_cannot_be_paid_from_here(self):
		rows, _leftovers = _build(
			[_item("REC-1", account="Cost of Goods Sold - J")],
			payable_accounts={RENT_DOKKI},
		)
		row = rows["REC-1"]
		self.assertTrue(row["due_this_month"])
		self.assertFalse(row["account_payable"])
		self.assertFalse(row["can_pay"])

	def test_due_date_uses_the_selected_month(self):
		rows, _leftovers = _build([_item("REC-1", day_of_month=5)])
		self.assertEqual(rows["REC-1"]["due_date"], "2026-08-05")


class TestDueDateClamping(unittest.TestCase):
	def _due_date(self, day, anchor):
		from jarz_pos.api.monthly_expenses import _due_date_for_month

		return _due_date_for_month(day, anchor)

	def test_a_legacy_day_31_still_renders_in_february(self):
		self.assertEqual(self._due_date(31, date(2026, 2, 1)), "2026-02-28")

	def test_a_legacy_day_31_renders_in_a_leap_february(self):
		self.assertEqual(self._due_date(31, date(2024, 2, 1)), "2024-02-29")

	def test_a_normal_day_is_untouched(self):
		self.assertEqual(self._due_date(5, date(2026, 8, 1)), "2026-08-05")

	def test_blank_day_has_no_due_date(self):
		self.assertIsNone(self._due_date(None, date(2026, 8, 1)))
		self.assertIsNone(self._due_date("", date(2026, 8, 1)))
		self.assertIsNone(self._due_date(0, date(2026, 8, 1)))


# ── period_month vs expense_month ─────────────────────────────────────────


class TestPayingAPastMonth(unittest.TestCase):
	"""August rent paid on 3 September.

	``period_month=2026-08`` (what it pays for), ``expense_month=2026-09``
	(when the Journal Entry hit the ledger). Both views have to be right.
	"""

	PAYMENT = _request(
		name="JER-0002",
		period_month="2026-08",
		expense_month="2026-09",
		expense_date=date(2026, 9, 3),
	)

	def _index(self, month_key, requests=None):
		from jarz_pos.api.monthly_expenses import _split_period_requests

		return _split_period_requests(
			[self.PAYMENT] if requests is None else requests, month_key
		)

	def _month(self, month_key, month_start, month_end, gl, requests=None):
		index = self._index(month_key, requests)
		rows, leftovers = _build(
			[_item("REC-1")],
			month_start=month_start,
			month_end=month_end,
			paid_linked_by_item=index["paid_linked_by_item"],
			linked_posted_by_account=index["linked_posted_by_account"],
			gl_by_account={RENT_DOKKI: gl},
		)
		return index, rows["REC-1"], leftovers

	def test_august_counts_it_as_paid_even_though_no_august_gl_exists(self):
		index, row, leftovers = self._month(
			"2026-08", date(2026, 8, 1), date(2026, 8, 31), gl=0.0
		)
		self.assertEqual(index["paid_linked_by_item"], {"REC-1": 47000.0})
		# The JE landed in September, so it explains none of August's ledger.
		self.assertEqual(index["linked_posted_by_account"], {})
		self.assertEqual(row["paid_amount"], 47000.0)
		self.assertEqual(row["remaining"], 0.0)
		self.assertEqual(row["payment_status"], "Paid")
		self.assertEqual(leftovers, {})

	def test_september_is_still_unpaid_despite_the_september_journal_entry(self):
		index, row, leftovers = self._month(
			"2026-09", date(2026, 9, 1), date(2026, 9, 30), gl=47000.0
		)
		self.assertEqual(index["paid_linked_by_item"], {})
		# Posted here, so it explains September's GL and nothing is inferred.
		self.assertEqual(index["linked_posted_by_account"], {RENT_DOKKI: 47000.0})
		self.assertEqual(row["paid_amount"], 0.0)
		self.assertEqual(row["remaining"], 47000.0)
		self.assertEqual(row["payment_status"], "Unpaid")
		self.assertFalse(row["inferred"])
		self.assertEqual(leftovers, {})

	def test_the_september_payment_is_listed_under_august(self):
		index = self._index("2026-08")
		payments = index["payments_by_item"]["REC-1"]
		self.assertEqual(len(payments), 1)
		self.assertEqual(payments[0]["amount"], 47000.0)
		self.assertEqual(payments[0]["period_month"], "2026-08")
		self.assertEqual(payments[0]["expense_month"], "2026-09")
		self.assertEqual(payments[0]["journal_entry"], "ACC-JV-0001")


class TestAugustPaidTwiceIsNotReportedAsSettled(unittest.TestCase):
	"""C1: a real double payment that the old formula reported as ``Paid``.

	Auto Repeat's draft JE for August rent is submitted in Desk during August,
	so GL(Aug) = 47,000. On 3 September a manager, not seeing it, pays August's
	rent again in the app (``period_month=2026-08``, ``expense_month=2026-09``).
	94,000 EGP has left the company for one month's rent.

	The old formula subtracted PERIOD-scoped payments from POSTING-scoped GL, so
	the September payment cancelled August's Desk money: unlinked fell to 0, the
	row read ``Paid`` with ``remaining=0`` and ``inferred=False``, and no
	Overpaid gap was raised. The month looked cleanly settled.
	"""

	SEPTEMBER_PAYMENT_FOR_AUGUST = _request(
		name="JER-0002",
		period_month="2026-08",
		expense_month="2026-09",
		expense_date=date(2026, 9, 3),
	)

	def _view(self, month_key, month_start, month_end, gl):
		from jarz_pos.api.monthly_expenses import _build_gaps, _split_period_requests

		index = _split_period_requests([self.SEPTEMBER_PAYMENT_FOR_AUGUST], month_key)
		rows, leftovers = _build(
			[_item("REC-1")],
			month_start=month_start,
			month_end=month_end,
			paid_linked_by_item=index["paid_linked_by_item"],
			linked_posted_by_account=index["linked_posted_by_account"],
			gl_by_account={RENT_DOKKI: gl},
		)
		gaps = _build_gaps(list(rows.values()), True, leftovers, {})
		return rows["REC-1"], leftovers, gaps

	def test_august_shows_the_double_payment_as_overpaid(self):
		row, leftovers, _gaps = self._view(
			"2026-08", date(2026, 8, 1), date(2026, 8, 31), gl=47000.0
		)
		# The app payment (period-scoped) plus August's own Desk money, which is
		# still unexplained because that payment posted in September.
		self.assertEqual(row["paid_linked"], 47000.0)
		self.assertEqual(row["paid_unlinked"], 47000.0)
		self.assertEqual(row["paid_amount"], 94000.0)
		self.assertEqual(row["due_amount"], 47000.0)
		self.assertEqual(row["payment_status"], "Overpaid")
		self.assertTrue(row["inferred"])
		self.assertEqual(leftovers, {})

	def test_the_overpayment_is_reported_as_a_gap(self):
		_row, _leftovers, gaps = self._view(
			"2026-08", date(2026, 8, 1), date(2026, 8, 31), gl=47000.0
		)
		messages = [g["message"] for g in gaps]
		self.assertTrue(
			any("more posted than due" in m for m in messages),
			messages,
		)

	def test_september_does_not_also_claim_the_payment(self):
		# The same payment must not settle two months. September's own rent is
		# untouched, and its GL is fully explained by the app payment.
		row, leftovers, _gaps = self._view(
			"2026-09", date(2026, 9, 1), date(2026, 9, 30), gl=47000.0
		)
		self.assertEqual(row["paid_amount"], 0.0)
		self.assertEqual(row["paid_unlinked"], 0.0)
		self.assertEqual(row["remaining"], 47000.0)
		self.assertEqual(row["payment_status"], "Unpaid")
		self.assertEqual(leftovers, {})

	def test_an_ordinary_same_month_payment_is_still_plain_paid(self):
		# The case that must NOT regress into a false Overpaid: one payment, one
		# JE, both in August. Its GL is explained, so nothing is inferred.
		from jarz_pos.api.monthly_expenses import _split_period_requests

		index = _split_period_requests([_request()], "2026-08")
		rows, leftovers = _build(
			[_item("REC-1")],
			paid_linked_by_item=index["paid_linked_by_item"],
			linked_posted_by_account=index["linked_posted_by_account"],
			gl_by_account={RENT_DOKKI: 47000.0},
		)
		row = rows["REC-1"]
		self.assertEqual(row["paid_amount"], 47000.0)
		self.assertEqual(row["payment_status"], "Paid")
		self.assertFalse(row["inferred"])
		self.assertEqual(leftovers, {})

	def test_a_desk_only_posting_is_still_inferred_as_paid(self):
		# No app payment at all: Auto Repeat posted it, and the lone due item on
		# that account absorbs it. Inferred, because no document links them.
		from jarz_pos.api.monthly_expenses import _split_period_requests

		index = _split_period_requests([], "2026-08")
		rows, leftovers = _build(
			[_item("REC-1")],
			paid_linked_by_item=index["paid_linked_by_item"],
			linked_posted_by_account=index["linked_posted_by_account"],
			gl_by_account={RENT_DOKKI: 47000.0},
		)
		row = rows["REC-1"]
		self.assertEqual(row["paid_amount"], 47000.0)
		self.assertEqual(row["payment_status"], "Paid")
		self.assertTrue(row["inferred"])
		self.assertEqual(leftovers, {})

	def test_an_adhoc_payment_on_the_account_is_still_left_to_inference(self):
		# Preserved deliberately: an ad-hoc request really did put money on the
		# rent account, so subtracting it would leave the lone due item reading
		# Unpaid while its account is settled.
		from jarz_pos.api.monthly_expenses import _split_period_requests

		index = _split_period_requests(
			[_request(expense_kind="Ad-hoc", recurring_expense=None)], "2026-08"
		)
		self.assertEqual(index["linked_posted_by_account"], {})
		rows, _leftovers = _build(
			[_item("REC-1")],
			paid_linked_by_item=index["paid_linked_by_item"],
			linked_posted_by_account=index["linked_posted_by_account"],
			gl_by_account={RENT_DOKKI: 47000.0},
		)
		self.assertEqual(rows["REC-1"]["payment_status"], "Paid")
		self.assertTrue(rows["REC-1"]["inferred"])


class TestSplitPeriodRequests(unittest.TestCase):
	def _index(self, requests, month_key=MONTH_KEY):
		from jarz_pos.api.monthly_expenses import _split_period_requests

		return _split_period_requests(requests, month_key)

	def test_a_recurring_payment_credits_its_item_and_its_account(self):
		index = self._index([_request()])
		self.assertEqual(index["paid_linked_by_item"], {"REC-1": 47000.0})
		self.assertEqual(index["linked_posted_by_account"], {RENT_DOKKI: 47000.0})

	def test_the_account_bucket_follows_the_posting_month_not_the_period(self):
		# One payment, two views. It settles August and it posted in September,
		# so it explains September's ledger and none of August's. Filling the
		# account bucket from the period branch is defect C1.
		payment = _request(period_month="2026-08", expense_month="2026-09")

		august = self._index([payment], "2026-08")
		self.assertEqual(august["paid_linked_by_item"], {"REC-1": 47000.0})
		self.assertEqual(august["linked_posted_by_account"], {})

		september = self._index([payment], "2026-09")
		self.assertEqual(september["paid_linked_by_item"], {})
		self.assertEqual(september["linked_posted_by_account"], {RENT_DOKKI: 47000.0})

	def test_a_same_month_payment_lands_in_both_scopes(self):
		index = self._index([_request()])
		self.assertEqual(index["paid_linked_by_item"], {"REC-1": 47000.0})
		self.assertEqual(index["linked_posted_by_account"], {RENT_DOKKI: 47000.0})

	def test_a_salary_payment_for_another_period_still_explains_this_months_gl(self):
		index = self._index(
			[
				_request(
					expense_kind="Salary",
					recurring_expense=None,
					employee="HR-EMP-00001",
					reason_account="Salary - J",
					amount=6750,
					period_month="2026-07",
					expense_month=MONTH_KEY,
				)
			]
		)
		# Not August's salary, but it IS money in August's salary account.
		self.assertEqual(index["paid_linked_by_employee"], {})
		self.assertEqual(index["salary_linked_by_account"], {"Salary - J": 6750.0})

	def test_two_payments_for_the_same_item_add_up(self):
		index = self._index(
			[
				_request(name="JER-1", amount=20000),
				_request(name="JER-2", amount=27000),
			]
		)
		self.assertEqual(index["paid_linked_by_item"], {"REC-1": 47000.0})
		self.assertEqual(len(index["payments_by_item"]["REC-1"]), 2)

	def test_a_salary_payment_credits_the_employee_not_an_item(self):
		index = self._index(
			[
				_request(
					expense_kind="Salary",
					recurring_expense=None,
					employee="HR-EMP-00001",
					reason_account="Salary - J",
					amount=6750,
				)
			]
		)
		self.assertEqual(index["paid_linked_by_employee"], {"HR-EMP-00001": 6750.0})
		self.assertEqual(index["salary_linked_by_account"], {"Salary - J": 6750.0})
		self.assertEqual(index["paid_linked_by_item"], {})

	def test_an_adhoc_request_is_left_in_the_unexplained_pot(self):
		# It really did put money on the rent account, so the single due item
		# should still pick it up via inference — crediting the account here
		# would cancel that out and the rent would read as unpaid.
		index = self._index(
			[_request(expense_kind="Ad-hoc", recurring_expense=None)]
		)
		self.assertEqual(index["linked_posted_by_account"], {})
		self.assertEqual(index["paid_linked_by_item"], {})

	def test_a_recurring_request_with_no_link_is_also_left_unexplained(self):
		# The kind says Recurring but nothing says WHICH expense; it cannot be
		# attributed, so it stays in the pot rather than cancelling GL blindly.
		index = self._index([_request(recurring_expense=None)])
		self.assertEqual(index["linked_posted_by_account"], {})
		self.assertEqual(index["paid_linked_by_item"], {})

	def test_an_unrelated_month_is_ignored_entirely(self):
		index = self._index(
			[_request(period_month="2026-05", expense_month="2026-05")]
		)
		self.assertEqual(index["paid_linked_by_item"], {})
		self.assertEqual(index["linked_posted_by_account"], {})


# ── payroll ───────────────────────────────────────────────────────────────


def _employee(name="HR-EMP-00001", base=6000.0, variable=750.0, **overrides):
	row = {
		"employee": name,
		"employee_name": name.replace("HR-EMP-", "Employee "),
		"designation": "Chef",
		"department": "Kitchen - J",
		"salary_structure": "Standard",
		"base": base,
		"variable": variable,
		"monthly": base + variable,
	}
	row.update(overrides)
	return row


class TestPayrollRows(unittest.TestCase):
	def _rows(self, employees, **kwargs):
		from jarz_pos.api.monthly_expenses import _build_payroll_rows

		kwargs.setdefault("salary_account", "Salary - J")
		return {r["employee"]: r for r in _build_payroll_rows(employees, **kwargs)}

	def test_due_falls_back_to_base_plus_variable(self):
		# `monthly` omitted on purpose. With it present the fixture would be
		# asserting against a number the fixture itself added up, and the
		# base+variable branch — the one HRMS leaves us on when a Salary
		# Structure Assignment carries no computed total — would never run.
		rows = self._rows([_employee(base=6000.0, variable=750.0, monthly=None)])
		row = rows["HR-EMP-00001"]
		self.assertEqual(row["base"], 6000.0)
		self.assertEqual(row["variable"], 750.0)
		self.assertEqual(row["due_amount"], 6750.0)
		self.assertEqual(row["remaining"], 6750.0)
		self.assertEqual(row["payment_status"], "Unpaid")

	def test_an_explicit_monthly_total_wins_over_the_fallback(self):
		# A structure whose total is not simply base+variable (a deduction, a
		# pro-rated joiner). The stated total is what is owed.
		rows = self._rows([_employee(base=6000.0, variable=750.0, monthly=6200.0)])
		self.assertEqual(rows["HR-EMP-00001"]["due_amount"], 6200.0)

	def test_partial_salary_payment_leaves_the_rest(self):
		rows = self._rows(
			[_employee()], paid_linked_by_employee={"HR-EMP-00001": 4000}
		)
		row = rows["HR-EMP-00001"]
		self.assertEqual(row["remaining"], 2750.0)
		self.assertEqual(row["payment_status"], "Partial")

	def test_a_full_salary_payment_settles_the_row(self):
		rows = self._rows(
			[_employee()], paid_linked_by_employee={"HR-EMP-00001": 6750}
		)
		self.assertEqual(rows["HR-EMP-00001"]["payment_status"], "Paid")
		self.assertEqual(rows["HR-EMP-00001"]["remaining"], 0.0)

	def test_salary_gl_is_never_inferred_onto_an_employee(self):
		# There is one Salary account for sixteen people; nothing about a GL
		# posting says whose salary it was.
		rows = self._rows([_employee("HR-EMP-1"), _employee("HR-EMP-2")])
		for row in rows.values():
			self.assertEqual(row["paid_amount"], 0.0)
			self.assertEqual(row["payment_status"], "Unpaid")

	def test_an_employee_with_a_submitted_slip_cannot_be_paid_here(self):
		rows = self._rows(
			[_employee()],
			slips_by_employee={"HR-EMP-00001": {"name": "SAL-SLIP-0001"}},
		)
		row = rows["HR-EMP-00001"]
		self.assertTrue(row["has_salary_slip"])
		self.assertEqual(row["salary_slip"], "SAL-SLIP-0001")
		self.assertFalse(row["can_pay"])

	def test_no_salary_account_means_nobody_can_be_paid(self):
		rows = self._rows([_employee()], salary_account=None)
		self.assertFalse(rows["HR-EMP-00001"]["can_pay"])


class TestSalaryAccountResolution(unittest.TestCase):
	def _resolve(self, accounts, payable):
		from jarz_pos.api.monthly_expenses import _resolve_salary_account

		return _resolve_salary_account(accounts, payable)

	def test_a_single_account_is_used_as_is(self):
		self.assertEqual(self._resolve(["Salary - J"], {"Salary - J"}), "Salary - J")

	def test_the_aggregate_salary_ledger_wins_over_components(self):
		accounts = ["Basic Salary - J", "Salary - J", "Transport Allowance - J"]
		payable = set(accounts)
		self.assertEqual(self._resolve(accounts, payable), "Salary - J")

	def test_unpayable_accounts_are_skipped_when_a_payable_one_exists(self):
		self.assertEqual(
			self._resolve(["Direct Wages - J", "Salary - J"], {"Salary - J"}),
			"Salary - J",
		)

	def test_no_accounts_means_no_resolution(self):
		self.assertIsNone(self._resolve([], set()))


# ── the salary double-post guard ──────────────────────────────────────────


class TestSalarySlipDoublePostGuard(unittest.TestCase):
	"""HRMS books the salary expense on the Salary Slip.

	This module collapses that into one JE, which is only correct while no slip
	exists. If one does, paying here would debit the salary account twice.
	"""

	def _pay(self, slips):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = RuntimeError("refused")
		mock_frappe.session.user = "manager@example.com"

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses, "_default_company", return_value="JARZ"
		), patch.object(
			monthly_expenses, "_submitted_salary_slips", return_value=slips
		) as slip_lookup, patch.object(
			monthly_expenses, "_compute_month"
		) as compute:
			try:
				monthly_expenses.pay_salary(
					"HR-EMP-00001",
					month="2026-08",
					amount=6750,
					paying_account="Cash - J",
				)
				raised = False
			except RuntimeError:
				raised = True
		return mock_frappe, slip_lookup, compute, raised

	def test_a_submitted_slip_blocks_the_payment(self):
		mock_frappe, _lookup, compute, raised = self._pay(
			{"HR-EMP-00001": {"name": "SAL-SLIP-0001"}}
		)
		self.assertTrue(raised)
		mock_frappe.throw.assert_called_once()
		message = str(mock_frappe.throw.call_args[0][0])
		self.assertIn("SAL-SLIP-0001", message)
		# Refused before any money maths, and certainly before a document exists.
		compute.assert_not_called()
		mock_frappe.get_doc.assert_not_called()

	def test_the_slip_lookup_is_strict_on_the_payment_path(self):
		# A failed HRMS query must not be read as "no slips" when we are about
		# to post; that is the whole double-post risk.
		_frappe, lookup, _compute, _raised = self._pay(
			{"HR-EMP-00001": {"name": "SAL-SLIP-0001"}}
		)
		self.assertTrue(lookup.call_args.kwargs.get("strict"))

	def test_no_slip_does_not_trip_the_guard(self):
		mock_frappe, _lookup, compute, _raised = self._pay({})
		# It gets past the guard and goes on to compute the month; whatever it
		# fails on afterwards against the mocked context, it must not be this.
		messages = [str(call[0][0]) for call in mock_frappe.throw.call_args_list]
		self.assertFalse(any("Salary Slip" in m for m in messages), messages)
		compute.assert_called()


# ── payment wiring ────────────────────────────────────────────────────────


class _FakeDoc:
	def __init__(self, name="JER-0009"):
		self.name = name
		self.flags = SimpleNamespace(ignore_permissions=False)
		self.insert = MagicMock()
		self.submit = MagicMock()
		self.reload = MagicMock()
		self.approved_by = None
		self.approved_on = None

	def as_dict(self):
		return {"name": self.name}


class TestPayRecurringExpenseWiring(unittest.TestCase):
	"""The guard must see the same numbers the screen showed."""

	def _context(self, due=47000.0, paid=20000.0, **row_overrides):
		row = {
			"name": "REC-1",
			"expense_name": "Rent - Dokki",
			"expense_account": RENT_DOKKI,
			"status": "Active",
			"due_this_month": True,
			"due_amount": due,
			"paid_amount": paid,
			"remaining": max(due - paid, 0.0),
			"account_payable": True,
			"can_pay": True,
		}
		row.update(row_overrides)
		return {
			"month": MONTH_KEY,
			"company": "JARZ",
			"registry": [row],
			"payroll": {"rows": []},
			"summary": {"remaining": max(due - paid, 0.0)},
		}

	def _pay(
		self,
		amount=27000,
		allow_overpay=0,
		due=47000.0,
		paid=20000.0,
		**row_overrides,
	):
		from jarz_pos.api import monthly_expenses

		fake_doc = _FakeDoc()
		captured = {}
		guard_calls = []

		def _get_doc(payload):
			captured.update(payload)
			return fake_doc

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.throw.side_effect = RuntimeError("refused")

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses,
			"_compute_month",
			return_value=self._context(due, paid, **row_overrides),
		), patch.object(
			monthly_expenses, "_resolve_paying_account", return_value="Cash - J"
		), patch.object(
			monthly_expenses,
			"_guard_overpay",
			side_effect=lambda *args, **kwargs: guard_calls.append(args) or 0.0,
		), patch.object(
			monthly_expenses, "_serialize_expense", return_value={"name": fake_doc.name}
		):
			result = monthly_expenses.pay_recurring_expense(
				"REC-1",
				month=MONTH_KEY,
				amount=amount,
				paying_account="Cash - J",
				allow_overpay=allow_overpay,
			)
		return result, captured, fake_doc, guard_calls

	def test_the_guard_receives_due_already_paid_and_the_requested_amount(self):
		_result, _captured, _doc, guard_calls = self._pay(amount=27000)
		self.assertEqual(len(guard_calls), 1)
		label, due, already_paid, requested, allow_overpay, month_key = guard_calls[0]
		self.assertEqual(label, "Rent - Dokki")
		self.assertEqual(due, 47000.0)
		self.assertEqual(already_paid, 20000.0)
		self.assertEqual(requested, 27000.0)
		self.assertFalse(allow_overpay)
		self.assertEqual(month_key, MONTH_KEY)

	def test_allow_overpay_is_forwarded_to_the_guard(self):
		_result, _captured, _doc, guard_calls = self._pay(allow_overpay="1")
		self.assertTrue(guard_calls[0][4])

	def test_the_request_carries_the_kind_link_and_period(self):
		_result, captured, _doc, _guard = self._pay()
		self.assertEqual(captured["doctype"], "Jarz Expense Request")
		self.assertEqual(captured["expense_kind"], "Recurring")
		self.assertEqual(captured["recurring_expense"], "REC-1")
		self.assertEqual(captured["period_month"], MONTH_KEY)
		self.assertEqual(captured["reason_account"], RENT_DOKKI)
		self.assertEqual(captured["paying_account"], "Cash - J")

	def test_the_request_is_created_pre_approved_and_submitted_in_one_call(self):
		_result, captured, doc, _guard = self._pay()
		# requires_approval=0 + an immediate submit is what makes the Journal
		# Entry post now rather than waiting for a second round-trip.
		self.assertEqual(captured["requires_approval"], 0)
		doc.insert.assert_called_once()
		self.assertTrue(doc.insert.call_args.kwargs.get("ignore_permissions"))
		doc.submit.assert_called_once()
		self.assertTrue(doc.flags.ignore_permissions)
		self.assertEqual(doc.approved_by, "manager@example.com")

	def test_the_recomputed_item_comes_back_with_the_payment(self):
		result, _captured, _doc, _guard = self._pay()
		self.assertTrue(result["success"])
		self.assertEqual(result["payment"], {"name": "JER-0009"})
		self.assertEqual(result["item"]["name"], "REC-1")

	def test_a_zero_amount_is_refused_before_anything_is_created(self):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = RuntimeError("refused")
		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses, "_compute_month"
		) as compute:
			with self.assertRaises(RuntimeError):
				monthly_expenses.pay_recurring_expense(
					"REC-1", month=MONTH_KEY, amount=0, paying_account="Cash - J"
				)
		compute.assert_not_called()
		mock_frappe.get_doc.assert_not_called()

	# ── the row's own can_pay must be honoured ────────────────────────────

	def test_an_account_outside_indirect_expenses_is_refused(self):
		# This module computed `account_payable` for the row the screen drew.
		# Ignoring it lets the API accept what the UI greys out, and the failure
		# then surfaces from inside DocType validation with no usable reason.
		with self.assertRaises(RuntimeError):
			self._pay(account_payable=False, can_pay=False)

	def test_the_refusal_names_the_offending_account(self):
		message = self._throw_message(
			account_payable=False, expense_account="Cost of Goods Sold - J"
		)
		self.assertIn("Cost of Goods Sold - J", message)
		self.assertIn("Indirect Expenses", message)

	def _throw_message(self, **row_overrides):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.throw.side_effect = RuntimeError("refused")
		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses,
			"_compute_month",
			return_value=self._context(**row_overrides),
		), patch.object(
			monthly_expenses, "_resolve_paying_account", return_value="Cash - J"
		):
			try:
				monthly_expenses.pay_recurring_expense(
					"REC-1", month=MONTH_KEY, amount=1000, paying_account="Cash - J"
				)
			except RuntimeError:
				pass
		return str(mock_frappe.throw.call_args[0][0])

	def test_a_paused_expense_is_refused_by_name_not_just_by_the_overpay_guard(self):
		message = self._throw_message(
			status="Paused", due_this_month=False, due_amount=0.0, paid_amount=0.0
		)
		self.assertIn("not due", message)
		self.assertIn("Paused", message)
		self.assertIn("allow_overpay", message)

	def test_allow_overpay_still_lets_a_not_due_expense_be_paid_deliberately(self):
		_result, captured, doc, _guard = self._pay(
			allow_overpay=1,
			amount=1000,
			due=0.0,
			paid=0.0,
			status="Paused",
			due_this_month=False,
		)
		doc.submit.assert_called_once()
		self.assertEqual(captured["amount"], 1000.0)

	# ── serialization (H2) ───────────────────────────────────────────────

	def test_the_row_is_locked_before_the_state_the_guard_reads(self):
		# Two taps of Pay both read the pre-payment state and both pass the
		# guard unless the second blocks on the first's row lock. A lock taken
		# after `_compute_month` protects nothing: the numbers are already read.
		from jarz_pos.api import monthly_expenses

		order = []

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_doc.return_value = _FakeDoc()
		mock_frappe.throw.side_effect = RuntimeError("refused")
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: order.append(
			("lock", a, kw)
		)

		def _compute(*_args, **_kwargs):
			order.append(("compute", None, None))
			return self._context()

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses, "_compute_month", side_effect=_compute
		), patch.object(
			monthly_expenses, "_resolve_paying_account", return_value="Cash - J"
		), patch.object(
			monthly_expenses, "_guard_overpay", return_value=0.0
		), patch.object(
			monthly_expenses, "_serialize_expense", return_value={}
		):
			monthly_expenses.pay_recurring_expense(
				"REC-1", month=MONTH_KEY, amount=1000, paying_account="Cash - J"
			)

		steps = [step for step, _a, _kw in order]
		self.assertEqual(steps[0], "lock", order)
		self.assertIn("compute", steps)
		_step, args, kwargs = order[0]
		self.assertEqual(args[0], "Jarz Recurring Expense")
		self.assertEqual(args[1], "REC-1")
		self.assertTrue(kwargs.get("for_update"))


class TestOverpayRefusalReachesTheEndpoint(unittest.TestCase):
	"""The real guard, wired through the real endpoint."""

	def _pay(self, amount, allow_overpay):
		from jarz_pos.api import monthly_expenses

		fake_doc = _FakeDoc()
		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_doc.return_value = fake_doc
		mock_frappe.throw.side_effect = RuntimeError("refused")

		context = {
			"month": MONTH_KEY,
			"company": "JARZ",
			"registry": [
				{
					"name": "REC-1",
					"expense_name": "Rent - Dokki",
					"expense_account": RENT_DOKKI,
					"status": "Active",
					"due_this_month": True,
					"due_amount": 47000.0,
					"paid_amount": 47000.0,
					"remaining": 0.0,
					"account_payable": True,
					"can_pay": True,
				}
			],
			"payroll": {"rows": []},
			"summary": {},
		}

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses, "_compute_month", return_value=context
		), patch.object(
			monthly_expenses, "_resolve_paying_account", return_value="Cash - J"
		), patch.object(
			monthly_expenses, "_serialize_expense", return_value={"name": fake_doc.name}
		):
			try:
				monthly_expenses.pay_recurring_expense(
					"REC-1",
					month=MONTH_KEY,
					amount=amount,
					paying_account="Cash - J",
					allow_overpay=allow_overpay,
				)
				refused = False
			except RuntimeError:
				refused = True
		return refused, mock_frappe, fake_doc

	def test_paying_an_already_settled_month_is_refused(self):
		refused, mock_frappe, fake_doc = self._pay(47000, allow_overpay=0)
		self.assertTrue(refused)
		fake_doc.submit.assert_not_called()
		message = str(mock_frappe.throw.call_args[0][0])
		self.assertIn("47000", message)

	def test_allow_overpay_lets_the_second_payment_through(self):
		refused, _mock_frappe, fake_doc = self._pay(47000, allow_overpay=1)
		self.assertFalse(refused)
		fake_doc.submit.assert_called_once()


# ── paying a salary ───────────────────────────────────────────────────────


class TestPaySalaryWiring(unittest.TestCase):
	"""The salary payment path: what it writes, and what stops it."""

	EMPLOYEE = "HR-EMP-00001"

	def _context(
		self,
		due=6750.0,
		paid=0.0,
		unattributed_gl=0.0,
		payroll_due=None,
		payroll_paid=None,
	):
		# `payroll_due` / `payroll_paid` default to this one employee's figures,
		# which is the single-employee company the other cases assume. Pass them
		# explicitly to model a real payroll, where the company owes far more
		# than any one person and unattributed money can sit well inside it.
		row = {
			"employee": self.EMPLOYEE,
			"employee_name": "Employee 00001",
			"due_amount": due,
			"paid_amount": paid,
			"remaining": max(due - paid, 0.0),
			"has_salary_slip": False,
			"can_pay": True,
		}
		return {
			"month": MONTH_KEY,
			"company": "JARZ",
			"registry": [],
			"payroll": {
				"rows": [row],
				"salary_account": "Salary - J",
				"salary_account_label": "Salary",
				"unattributed_gl": unattributed_gl,
				"due": due if payroll_due is None else payroll_due,
				"paid": paid if payroll_paid is None else payroll_paid,
				"remaining": max(due - paid, 0.0),
			},
			"summary": {},
		}

	def _pay(self, amount=6750, allow_overpay=0, slips=None, **context_kwargs):
		from jarz_pos.api import monthly_expenses

		fake_doc = _FakeDoc()
		captured = {}
		order = []

		def _get_doc(payload):
			captured.update(payload)
			return fake_doc

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.throw.side_effect = RuntimeError("refused")
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: order.append(
			("lock", a, kw)
		)

		def _compute(*_args, **_kwargs):
			order.append(("compute", None, None))
			return self._context(**context_kwargs)

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses, "_default_company", return_value="JARZ"
		), patch.object(
			monthly_expenses, "_submitted_salary_slips", return_value=slips or {}
		), patch.object(
			monthly_expenses, "_compute_month", side_effect=_compute
		), patch.object(
			monthly_expenses, "_resolve_paying_account", return_value="Cash - J"
		), patch.object(
			monthly_expenses, "_serialize_expense", return_value={"name": fake_doc.name}
		):
			try:
				result = monthly_expenses.pay_salary(
					self.EMPLOYEE,
					month=MONTH_KEY,
					amount=amount,
					paying_account="Cash - J",
					allow_overpay=allow_overpay,
				)
				refused = False
			except RuntimeError:
				result, refused = None, True
		return result, captured, fake_doc, mock_frappe, order, refused

	# ── happy path ────────────────────────────────────────────────────────

	def test_the_request_carries_the_salary_kind_the_employee_and_the_period(self):
		_result, captured, _doc, _frappe, _order, refused = self._pay()
		self.assertFalse(refused)
		self.assertEqual(captured["doctype"], "Jarz Expense Request")
		self.assertEqual(captured["expense_kind"], "Salary")
		self.assertEqual(captured["employee"], self.EMPLOYEE)
		self.assertEqual(captured["period_month"], MONTH_KEY)
		self.assertIsNone(captured["recurring_expense"])
		self.assertEqual(captured["reason_account"], "Salary - J")
		self.assertEqual(captured["paying_account"], "Cash - J")
		self.assertEqual(captured["amount"], 6750.0)
		self.assertEqual(captured["requires_approval"], 0)

	def test_the_request_is_submitted_and_the_row_comes_back(self):
		result, _captured, doc, _frappe, _order, _refused = self._pay()
		doc.insert.assert_called_once()
		doc.submit.assert_called_once()
		self.assertTrue(result["success"])
		self.assertEqual(result["row"]["employee"], self.EMPLOYEE)
		self.assertIn("due", result["payroll"])

	# ── H3: the guard the per-employee figure cannot be ───────────────────

	def test_unattributed_salary_gl_blocks_the_payment(self):
		# A lump payroll JE booked in Desk is invisible per employee, so
		# `paid_amount` is 0 for all sixteen and the row guard sees nothing.
		_result, _captured, doc, mock_frappe, _order, refused = self._pay(
			unattributed_gl=108000.0
		)
		self.assertTrue(refused)
		doc.submit.assert_not_called()
		message = str(mock_frappe.throw.call_args[0][0])
		self.assertIn("108000", message)
		self.assertIn("Salary", message)
		self.assertIn(MONTH_KEY, message)
		self.assertIn("allow_overpay", message)

	def test_allow_overpay_overrides_the_unattributed_gl_block(self):
		_result, _captured, doc, _frappe, _order, refused = self._pay(
			unattributed_gl=108000.0, allow_overpay=1
		)
		self.assertFalse(refused)
		doc.submit.assert_called_once()

	def test_salary_gl_within_tolerance_does_not_block(self):
		_result, _captured, doc, _frappe, _order, refused = self._pay(
			unattributed_gl=0.4
		)
		self.assertFalse(refused)
		doc.submit.assert_called_once()

	def test_unattributed_gl_inside_the_payroll_does_not_block(self):
		"""The production case, and the reason this guard is not a blanket one.

		Staff pay salaries ad-hoc, naming the person only in a free-text remark,
		so a real month opens with unattributed salary GL already posted —
		20,930 of a 108,000 payroll on 2026-09. Refusing every payment on that
		basis would make `allow_overpay=1` the ordinary way to use the screen,
		and a guard that is always overridden protects nothing. Paying one
		employee 6,750 here takes the month to 27,680, far inside the payroll,
		so it must go through untouched.
		"""
		_result, _captured, doc, _frappe, _order, refused = self._pay(
			unattributed_gl=20930.0, payroll_due=108000.0, payroll_paid=0.0
		)
		self.assertFalse(refused)
		doc.submit.assert_called_once()

	def test_a_payment_that_would_exceed_the_payroll_still_blocks(self):
		"""The hazard the guard is actually for: more salary out than is owed.

		A lump payroll Journal Entry for the whole 108,000 booked in Desk is
		invisible per employee, so every row still reads Unpaid. Paying anyone
		on top of it takes the month past the payroll, and that is the moment to
		stop — regardless of how little the individual payment is.
		"""
		_result, _captured, doc, _frappe, _order, refused = self._pay(
			amount=6750,
			unattributed_gl=108000.0,
			payroll_due=108000.0,
			payroll_paid=0.0,
		)
		self.assertTrue(refused)
		doc.submit.assert_not_called()

	# ── H2: serialization ─────────────────────────────────────────────────

	def test_the_employee_row_is_locked_before_any_state_is_read(self):
		_result, _captured, _doc, _frappe, order, _refused = self._pay()
		step, args, kwargs = order[0]
		self.assertEqual(step, "lock")
		self.assertEqual(args[0], "Employee")
		self.assertEqual(args[1], self.EMPLOYEE)
		self.assertTrue(kwargs.get("for_update"))
		self.assertIn("compute", [s for s, _a, _kw in order])


# ── the paying account must be one the picker offers (M7) ─────────────────


class _Source:
	def __init__(self, account):
		self.account = account


class TestResolvePayingAccount(unittest.TestCase):
	"""Read and write must agree about what is a valid source of money."""

	def _resolve(self, account, row=None, cashlike=("Cash - J", "Bank - J")):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = RuntimeError("refused")
		mock_frappe.db.get_value.return_value = row

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses,
			"_cashlike_accounts",
			return_value=[_Source(a) for a in cashlike],
		):
			try:
				resolved = monthly_expenses._resolve_paying_account(account, "JARZ")
			except RuntimeError:
				return mock_frappe, None
		return mock_frappe, resolved

	def _row(self, name, is_group=0, company="JARZ"):
		return {
			"name": name,
			"is_group": is_group,
			"company": company,
			"account_name": name.split(" - ")[0],
		}

	def test_a_cash_account_from_the_picker_is_accepted(self):
		_frappe, resolved = self._resolve("Cash - J", self._row("Cash - J"))
		self.assertEqual(resolved, "Cash - J")

	def test_an_income_account_is_refused(self):
		# DEBIT Rent / CREDIT Sales books an expense by inventing revenue, and
		# nothing downstream would ever flag it.
		mock_frappe, resolved = self._resolve("Sales - J", self._row("Sales - J"))
		self.assertIsNone(resolved)
		message = str(mock_frappe.throw.call_args[0][0])
		self.assertIn("Sales - J", message)

	def test_an_expense_account_is_refused(self):
		_frappe, resolved = self._resolve(RENT_DOKKI, self._row(RENT_DOKKI))
		self.assertIsNone(resolved)

	def test_a_group_account_is_still_refused_first(self):
		mock_frappe, resolved = self._resolve(
			"Bank Accounts - J", self._row("Bank Accounts - J", is_group=1)
		)
		self.assertIsNone(resolved)
		self.assertIn("group", str(mock_frappe.throw.call_args[0][0]))

	def test_another_companys_account_is_refused(self):
		mock_frappe, resolved = self._resolve(
			"Cash - X", self._row("Cash - X", company="OTHER")
		)
		self.assertIsNone(resolved)
		self.assertIn("OTHER", str(mock_frappe.throw.call_args[0][0]))

	def test_a_missing_account_is_refused(self):
		mock_frappe, resolved = self._resolve("Nope - J", None)
		self.assertIsNone(resolved)
		self.assertIn("not found", str(mock_frappe.throw.call_args[0][0]))

	def test_an_empty_picker_refuses_everything_rather_than_letting_anything_through(self):
		# Fails closed: with no cash or bank ledger configured there is no
		# correct account to credit, so nothing should post.
		_frappe, resolved = self._resolve("Cash - J", self._row("Cash - J"), cashlike=())
		self.assertIsNone(resolved)


# ── summary roll-up ───────────────────────────────────────────────────────


class TestSummarize(unittest.TestCase):
	def _summary(self, rows, registry_run_rate=0.0, payroll_run_rate=0.0):
		from jarz_pos.api.monthly_expenses import _summarize

		return _summarize(rows, [], registry_run_rate, payroll_run_rate)

	def test_remaining_is_the_sum_of_row_remainders_not_due_minus_paid(self):
		# Overpaying one landlord must not hide an unpaid one.
		rows = [
			{"due_amount": 47000, "paid_amount": 94000, "payment_status": "Overpaid"},
			{"due_amount": 47000, "paid_amount": 0, "payment_status": "Unpaid"},
		]
		summary = self._summary(rows)
		self.assertEqual(summary["due"], 94000.0)
		self.assertEqual(summary["paid"], 94000.0)
		self.assertEqual(summary["remaining"], 47000.0)
		self.assertEqual(summary["overpaid"], 47000.0)

	def test_status_counts(self):
		rows = [
			{"due_amount": 100, "paid_amount": 100, "payment_status": "Paid"},
			{"due_amount": 100, "paid_amount": 40, "payment_status": "Partial"},
			{"due_amount": 100, "paid_amount": 0, "payment_status": "Unpaid"},
			{"due_amount": 100, "paid_amount": 300, "payment_status": "Overpaid"},
			{"due_amount": 0, "paid_amount": 0, "payment_status": "Not Due"},
		]
		summary = self._summary(rows)
		self.assertEqual(summary["items_total"], 4)
		self.assertEqual(summary["items_paid"], 2)
		self.assertEqual(summary["items_partial"], 1)
		self.assertEqual(summary["items_unpaid"], 1)

	def test_run_rate_is_registry_plus_payroll(self):
		summary = self._summary([], registry_run_rate=188000, payroll_run_rate=108000)
		self.assertEqual(summary["run_rate"], 296000.0)


# ── registry maintenance ──────────────────────────────────────────────────


class TestDayOfMonthValidation(unittest.TestCase):
	def _day(self, value):
		from jarz_pos.api.monthly_expenses import _normalized_day_of_month

		return _normalized_day_of_month(value)

	def test_blank_is_allowed(self):
		self.assertIsNone(self._day(None))
		self.assertIsNone(self._day(""))

	def test_a_day_inside_the_safe_window_is_accepted(self):
		self.assertEqual(self._day(1), 1)
		self.assertEqual(self._day("28"), 28)

	def test_day_29_is_refused_because_february_has_no_29th_most_years(self):
		with self.assertRaises(ValueError):
			self._day(29)

	def test_day_31_is_refused_rather_than_silently_clamped(self):
		with self.assertRaises(ValueError) as ctx:
			self._day(31)
		self.assertIn("29-31", str(ctx.exception))

	def test_zero_and_negative_are_refused(self):
		for value in (0, -1):
			with self.assertRaises(ValueError):
				self._day(value)

	def test_nonsense_is_refused(self):
		with self.assertRaises(ValueError):
			self._day("the fifth")


class TestMonthlyEquivalent(unittest.TestCase):
	def _monthly(self, amount, frequency):
		from jarz_pos.api.monthly_expenses import _monthly_equivalent

		return _monthly_equivalent(amount, frequency)

	def test_matches_the_doctype_for_every_frequency(self):
		from jarz_pos.doctype.jarz_recurring_expense.jarz_recurring_expense import (
			FREQUENCY_MONTHS,
		)

		for frequency, months in FREQUENCY_MONTHS.items():
			self.assertEqual(self._monthly(1200, frequency), 1200 / months)

	def test_an_unknown_frequency_falls_back_to_monthly(self):
		self.assertEqual(self._monthly(1200, "Fortnightly"), 1200)
		self.assertEqual(self._monthly(1200, None), 1200)

	def test_the_api_and_doctype_share_one_frequency_map(self):
		from jarz_pos.api.monthly_expenses import FREQUENCY_MONTHS as api_map
		from jarz_pos.doctype.jarz_recurring_expense.jarz_recurring_expense import (
			FREQUENCY_MONTHS as doctype_map,
		)

		self.assertEqual(api_map, doctype_map)


class TestAvailableMonths(unittest.TestCase):
	def _months(self, anchor, back=12):
		from jarz_pos.api.monthly_expenses import _available_months

		return _available_months(anchor, back)

	def test_twelve_back_plus_current_newest_last(self):
		months = self._months("2026-09")
		self.assertEqual(len(months), 13)
		self.assertEqual(months[-1], "2026-09")
		self.assertEqual(months[0], "2025-09")
		self.assertEqual(months, sorted(months))

	def test_january_walks_back_into_the_previous_year(self):
		months = self._months("2026-01", back=2)
		self.assertEqual(months, ["2025-11", "2025-12", "2026-01"])

	def test_december_is_not_rolled_into_the_next_year(self):
		months = self._months("2026-12", back=1)
		self.assertEqual(months, ["2026-11", "2026-12"])


# ── _compute_month: which bucket reaches which builder ────────────────────


class TestComputeMonthWiring(unittest.TestCase):
	"""``_compute_month`` is the only place the index buckets are handed out.

	Every pure function below it is well covered, but nothing checked that the
	right bucket reaches the right builder — swap two of them and the money is
	wrong while every other test still passes. These drive the real function
	with ``frappe`` and the loaders mocked at the boundary, so the wiring
	itself is under test.
	"""

	SALARY = "Salary - J"

	def _compute(
		self,
		requests=(),
		gl=None,
		month="2026-08",
		month_start=date(2026, 8, 1),
		month_end=date(2026, 8, 31),
		registry=None,
		payroll_raises=False,
	):
		from jarz_pos.api import monthly_expenses

		gl = {RENT_DOKKI: 0.0, self.SALARY: 0.0} if gl is None else gl
		registry = [_item("REC-1")] if registry is None else registry

		payroll_raw = {
			"configured": True,
			"employees_total": 1,
			"employees_with_structure": 1,
			"employees_without_structure": 0,
			"monthly_total": 6750.0,
			"rows": [_employee()],
			"missing": [],
		}

		def _payroll(*_args, **_kwargs):
			if payroll_raises:
				raise RuntimeError("HRMS is not installed")
			return payroll_raw

		with patch.object(monthly_expenses, "frappe", MagicMock()), patch.object(
			monthly_expenses,
			"_month_context",
			return_value=(month_start, month_end, month, "JARZ", "EGP"),
		), patch.object(
			monthly_expenses, "_load_registry", return_value=list(registry)
		), patch.object(
			monthly_expenses, "_load_period_requests", return_value=list(requests)
		), patch.object(
			monthly_expenses,
			"_indirect_expense_accounts",
			return_value=[
				{"account": RENT_DOKKI, "label": "Rent Dokki"},
				{"account": self.SALARY, "label": "Salary"},
			],
		), patch.object(
			monthly_expenses, "_payroll_expense_accounts", return_value=[self.SALARY]
		), patch.object(
			monthly_expenses,
			"_gl_posted_by_account",
			return_value={a: {"amount": v} for a, v in gl.items()},
		), patch.object(
			monthly_expenses, "_account_label_map", return_value={}
		), patch.object(
			monthly_expenses, "_load_payroll", side_effect=_payroll
		), patch.object(
			monthly_expenses, "_submitted_salary_slips", return_value={}
		):
			return monthly_expenses._compute_month(month, "JARZ")

	def _registry_row(self, context, name="REC-1"):
		return next(r for r in context["registry"] if r["name"] == name)

	# ── the buckets go where they belong ──────────────────────────────────

	def test_a_linked_payment_credits_its_own_item_and_explains_its_own_gl(self):
		context = self._compute(
			requests=[_request()], gl={RENT_DOKKI: 47000.0, self.SALARY: 0.0}
		)
		row = self._registry_row(context)
		self.assertEqual(row["paid_linked"], 47000.0)
		self.assertEqual(row["paid_unlinked"], 0.0)
		self.assertEqual(row["payment_status"], "Paid")
		self.assertFalse(row["inferred"])
		# Nothing leaked into payroll.
		self.assertEqual(context["payroll"]["paid"], 0.0)
		self.assertEqual(context["payroll"]["unattributed_gl"], 0.0)

	def test_a_salary_payment_credits_the_employee_and_explains_salary_gl(self):
		salary_payment = _request(
			name="JER-SAL",
			expense_kind="Salary",
			recurring_expense=None,
			employee="HR-EMP-00001",
			reason_account=self.SALARY,
			amount=6750.0,
		)
		context = self._compute(
			requests=[salary_payment], gl={RENT_DOKKI: 0.0, self.SALARY: 6750.0}
		)
		payroll = context["payroll"]
		self.assertEqual(payroll["paid"], 6750.0)
		self.assertEqual(payroll["remaining"], 0.0)
		self.assertEqual(payroll["unattributed_gl"], 0.0)
		# And it did NOT credit the rent item.
		self.assertEqual(self._registry_row(context)["paid_amount"], 0.0)

	def test_a_desk_payroll_journal_entry_is_reported_as_unattributed(self):
		context = self._compute(gl={RENT_DOKKI: 0.0, self.SALARY: 108000.0})
		payroll = context["payroll"]
		self.assertEqual(payroll["unattributed_gl"], 108000.0)
		# Never folded into a row: nothing says whose salary it was.
		self.assertEqual(payroll["paid"], 0.0)
		for row in payroll["rows"]:
			self.assertEqual(row["paid_amount"], 0.0)

	def test_registry_gl_is_not_offered_to_the_payroll_reconciliation(self):
		# Rent GL must not show up as unattributed salary.
		context = self._compute(gl={RENT_DOKKI: 47000.0, self.SALARY: 0.0})
		self.assertEqual(context["payroll"]["unattributed_gl"], 0.0)
		self.assertTrue(self._registry_row(context)["inferred"])

	# ── C1, through the real wiring ───────────────────────────────────────

	def test_c1_a_september_payment_does_not_settle_augusts_desk_posting(self):
		september_payment = _request(
			name="JER-0002",
			period_month="2026-08",
			expense_month="2026-09",
			expense_date=date(2026, 9, 3),
		)
		context = self._compute(
			requests=[september_payment],
			gl={RENT_DOKKI: 47000.0, self.SALARY: 0.0},
		)
		row = self._registry_row(context)
		self.assertEqual(row["paid_linked"], 47000.0)
		self.assertEqual(row["paid_unlinked"], 47000.0)
		self.assertEqual(row["paid_amount"], 94000.0)
		self.assertEqual(row["payment_status"], "Overpaid")
		self.assertTrue(row["inferred"])

	def test_c1_september_itself_stays_unpaid(self):
		september_payment = _request(
			name="JER-0002",
			period_month="2026-08",
			expense_month="2026-09",
			expense_date=date(2026, 9, 3),
		)
		context = self._compute(
			requests=[september_payment],
			month="2026-09",
			month_start=date(2026, 9, 1),
			month_end=date(2026, 9, 30),
			gl={RENT_DOKKI: 47000.0, self.SALARY: 0.0},
		)
		row = self._registry_row(context)
		self.assertEqual(row["paid_amount"], 0.0)
		self.assertEqual(row["payment_status"], "Unpaid")

	# ── H4: HRMS absent must not take the screen down ─────────────────────

	def test_an_hrms_failure_degrades_to_an_empty_payroll_block(self):
		context = self._compute(
			gl={RENT_DOKKI: 47000.0, self.SALARY: 0.0}, payroll_raises=True
		)
		payroll = context["payroll"]
		self.assertFalse(payroll["configured"])
		self.assertFalse(payroll["readable"])
		self.assertEqual(payroll["rows"], [])
		self.assertEqual(payroll["due"], 0.0)
		self.assertEqual(payroll["paid"], 0.0)
		self.assertEqual(payroll["remaining"], 0.0)
		self.assertEqual(payroll["employees_total"], 0)
		self.assertEqual(payroll["missing"], [])

	def test_the_registry_half_still_renders_when_hrms_is_absent(self):
		# The half of the screen that needs no HRMS at all.
		context = self._compute(
			gl={RENT_DOKKI: 47000.0, self.SALARY: 0.0}, payroll_raises=True
		)
		row = self._registry_row(context)
		self.assertEqual(row["due_amount"], 47000.0)
		self.assertEqual(row["payment_status"], "Paid")
		self.assertTrue(context["registry_present"])

	def test_the_missing_payroll_announces_itself_as_a_gap(self):
		from jarz_pos.api.monthly_expenses import _build_gaps

		context = self._compute(payroll_raises=True)
		gaps = _build_gaps(
			context["registry"], context["registry_present"], context["leftovers"], context["payroll"]
		)
		messages = [g["message"] for g in gaps]
		self.assertTrue(any("HRMS" in m for m in messages), messages)
		self.assertTrue(
			any(g["severity"] == "critical" for g in gaps if "HRMS" in g["message"]),
			gaps,
		)

	def test_a_healthy_payroll_raises_no_such_gap(self):
		from jarz_pos.api.monthly_expenses import _build_gaps

		context = self._compute()
		self.assertTrue(context["payroll"]["readable"])
		gaps = _build_gaps(
			context["registry"], context["registry_present"], context["leftovers"], context["payroll"]
		)
		self.assertFalse(any("HRMS" in g["message"] for g in gaps), gaps)


# ── access gate ───────────────────────────────────────────────────────────


def _module_ast():
	"""Parse ``monthly_expenses`` as source so the gate can be checked by shape.

	Reading the source rather than the imported objects is deliberate: the
	``frappe.whitelist`` decorator returns the same function object, so at
	runtime there is nothing left to inspect about WHERE the gate is called —
	only that it is somewhere in the text. Position is the whole point here.
	"""
	import ast
	import inspect

	from jarz_pos.api import monthly_expenses

	return ast.parse(inspect.getsource(monthly_expenses))


def _whitelisted_functions():
	"""Every ``@frappe.whitelist()`` function in the module, enumerated.

	Enumerated rather than listed by hand so a new endpoint is covered the day
	it is written, not the day someone remembers to add it to a test.
	"""
	import ast

	found = []
	for node in _module_ast().body:
		if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			continue
		for decorator in node.decorator_list:
			target = decorator.func if isinstance(decorator, ast.Call) else decorator
			if (
				isinstance(target, ast.Attribute)
				and target.attr == "whitelist"
				and isinstance(target.value, ast.Name)
				and target.value.id == "frappe"
			):
				found.append((node.name, node))
				break
	return found


def _executable_body(node):
	"""The function's statements with a leading docstring dropped."""
	import ast

	body = list(node.body)
	if (
		body
		and isinstance(body[0], ast.Expr)
		and isinstance(body[0].value, ast.Constant)
		and isinstance(body[0].value.value, str)
	):
		body = body[1:]
	return body


def _call_name(statement):
	"""``foo()`` as a bare statement → ``"foo"``; anything else → ``None``."""
	import ast

	if not isinstance(statement, ast.Expr):
		return None
	call = statement.value
	if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
		return None
	return call.func.id


class TestAccessGateIsShared(unittest.TestCase):
	def test_the_gate_is_the_same_object_as_the_desk_pages(self):
		# The mobile drawer gate is written against this role set. Forking it
		# is how a visible button starts answering "Not permitted".
		from jarz_pos.api import monthly_expenses, recurring_expenses

		self.assertIs(monthly_expenses._ensure_manager, recurring_expenses._ensure_manager)

	def test_the_module_exposes_the_endpoints_the_contract_names(self):
		# A sanity floor under the enumeration below: if `_whitelisted_functions`
		# ever silently found nothing, every assertion over it would vacuously
		# pass and the gate would be unchecked everywhere.
		found = {name for name, _node in _whitelisted_functions()}
		self.assertLessEqual(
			{
				"get_monthly_expenses",
				"pay_recurring_expense",
				"pay_salary",
				"save_recurring_expense",
				"set_recurring_expense_status",
				"cancel_expense_payment",
			},
			found,
		)

	def test_every_whitelisted_endpoint_gates_before_it_does_anything(self):
		# Enumerated, not hard-coded: a seventh endpoint added later is checked
		# automatically. And asserted POSITIONALLY, not by substring — an
		# endpoint that reads data and only THEN calls the gate has already
		# leaked it, but would satisfy "the source mentions `_ensure_manager()`".
		for name, node in _whitelisted_functions():
			statements = _executable_body(node)
			self.assertTrue(statements, name)
			self.assertEqual(
				_call_name(statements[0]),
				"_ensure_manager",
				"{0} must call _ensure_manager() as its first statement".format(name),
			)

	def test_every_endpoint_that_reads_the_period_columns_checks_the_schema(self):
		# `_compute_month` -> `_load_period_requests` selects `period_month` and
		# friends. Unmigrated, that is a raw `Unknown column` OperationalError
		# instead of a "run bench migrate" message — and for
		# `save_recurring_expense`, one raised AFTER the document was saved, so
		# the user's write is rolled back behind an opaque DB error.
		for name, node in _whitelisted_functions():
			statements = _executable_body(node)
			calls = [_call_name(stmt) for stmt in statements[:2]]
			self.assertIn(
				"_require_period_fields",
				calls,
				"{0} reaches _compute_month and must call _require_period_fields() "
				"next to the gate".format(name),
			)


if __name__ == "__main__":
	unittest.main()

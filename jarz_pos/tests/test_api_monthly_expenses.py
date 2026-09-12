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


# ── deductions: penalties, advances, employee orders ──────────────────────
#
# The money model these cover, once, so the assertions below read as arithmetic:
#
#     gross_due        the Salary Structure Assignment's base + variable
#     penalty_total    ACTIVE penalties for THIS month
#     due_amount       gross_due - penalty_total   <- what the company owes
#     paid_amount      linked cash payments + what a settlement discharged
#     remaining        max(due_amount - paid_amount, 0)
#     advance_total    OPEN advance balance, ALL-TIME
#     order_total      OPEN Employee-purpose invoice balance, ALL-TIME
#     net_payable      max(remaining - advance_total - order_total, 0)
#
# The all-time/month-scoped asymmetry is deliberate and is asserted below
# (`test_an_advance_from_another_month_is_still_deducted`): a windowed balance
# hides exactly the stale debt worth collecting.


def _penalty(name="JPEN-00001", employee="HR-EMP-00001", **overrides):
	row = {
		"name": name,
		"employee": employee,
		"employee_name": "Employee 00001",
		"penalty_date": date(2026, 8, 3),
		"period_month": MONTH_KEY,
		"unit": "Days",
		"quantity": 1.0,
		"amount": 225.0,
		"day_rate": 225.0,
		"equivalent_days": 1.0,
		"currency": "EGP",
		"reason": "Absent without notice",
		"settled": 0,
		"settled_via": None,
	}
	row.update(overrides)
	return row


def _advance(
	name="HR-EAD-2026-00004",
	employee="HR-EMP-00001",
	paid=500.0,
	claimed=0.0,
	returned=0.0,
	settled=0.0,
	account="Employee Advances - J",
	**overrides,
):
	from jarz_pos.api.monthly_expenses import F_SETTLED_AMOUNT

	row = {
		"name": name,
		"employee": employee,
		"employee_name": "Employee 00001",
		"posting_date": date(2026, 7, 4),
		"advance_amount": paid,
		"paid_amount": paid,
		"claimed_amount": claimed,
		"return_amount": returned,
		"status": "Paid",
		"purpose": "Personal",
		"advance_account": account,
		"currency": "EGP",
		"company": "JARZ",
		"docstatus": 1,
		F_SETTLED_AMOUNT: settled,
	}
	row.update(overrides)
	return row


def _order(name="ACC-SINV-2026-18146", customer="CUST-0001", outstanding=184.0, **overrides):
	row = {
		"name": name,
		"customer": customer,
		"customer_name": "Employee 00001",
		"posting_date": date(2026, 8, 2),
		"grand_total": 184.0,
		"outstanding_amount": outstanding,
		"status": "Unpaid",
	}
	row.update(overrides)
	return row


def _payroll(employees=None, **kwargs):
	from jarz_pos.api.monthly_expenses import _build_payroll_rows

	kwargs.setdefault("salary_account", "Salary - J")
	rows = _build_payroll_rows(
		[_employee()] if employees is None else employees, **kwargs
	)
	return {r["employee"]: r for r in rows}


class TestPenaltyConversion(unittest.TestCase):
	"""One offence, three ways of writing it down, one money value.

	``day_rate`` is a THIRTIETH of the monthly salary — a fixed calendar basis,
	so the same offence costs the same in February as in August.
	"""

	def _convert(self, unit, quantity=None, amount=None, day_rate=225.0):
		from jarz_pos.api.monthly_expenses import _penalty_amounts

		return _penalty_amounts(unit, quantity, amount, day_rate)

	def test_a_day_costs_a_thirtieth_of_the_monthly_salary(self):
		from jarz_pos.api.monthly_expenses import _day_rate

		self.assertEqual(_day_rate(6750.0), 225.0)
		self.assertEqual(_day_rate(9000.0), 300.0)

	def test_no_salary_structure_means_no_day_rate(self):
		from jarz_pos.api.monthly_expenses import _day_rate

		self.assertEqual(_day_rate(0.0), 0.0)
		self.assertEqual(_day_rate(None), 0.0)

	def test_days_are_priced_at_the_day_rate(self):
		amount, days = self._convert("Days", quantity=2)
		self.assertEqual(amount, 450.0)
		self.assertEqual(days, 2.0)

	def test_half_days_are_priced_at_half_the_day_rate(self):
		amount, days = self._convert("Half Days", quantity=3)
		self.assertEqual(amount, 337.5)
		self.assertEqual(days, 1.5)

	def test_money_is_taken_as_given_and_reported_back_in_days(self):
		# Both directions are always stored: an employee told "450 EGP" can also
		# be told "that is two days", which is the conversation that actually
		# happens.
		amount, days = self._convert("Money", amount=450)
		self.assertEqual(amount, 450.0)
		self.assertEqual(days, 2.0)

	def test_money_without_a_day_rate_is_still_a_valid_penalty(self):
		# Off-payroll: there is no salary to divide, so the day equivalent is
		# unknowable. It must not be a division by zero.
		amount, days = self._convert("Money", amount=450, day_rate=0)
		self.assertEqual(amount, 450.0)
		self.assertEqual(days, 0.0)

	def test_the_api_and_the_doctype_share_one_conversion(self):
		# The API needs the money value before the document exists (to guard the
		# month's penalty total); the DocType needs it because a penalty entered
		# in Desk must come out identical. Two callers, one formula.
		# Imported hard, never behind a skip: a skipTest here would turn the one
		# assertion that keeps the two in step into a silent no-op on exactly the
		# bench where they had drifted.
		from jarz_pos.api.monthly_expenses import _penalty_amounts
		from jarz_pos.doctype.jarz_employee_penalty.jarz_employee_penalty import (
			convert_penalty as doctype_amounts,
		)

		for unit, quantity, amount in (
			("Days", 2, None),
			("Half Days", 3, None),
			("Money", 0, 450),
		):
			self.assertEqual(
				_penalty_amounts(unit, quantity, amount, 225.0),
				doctype_amounts(unit, quantity, amount, 225.0),
				unit,
			)


class TestPenaltyOnThePayrollRow(unittest.TestCase):
	"""A penalty lowers what the company owes — and nothing else."""

	def test_a_penalty_lowers_due_amount_below_gross(self):
		rows = _payroll(penalties_by_employee={"HR-EMP-00001": [_penalty()]})
		row = rows["HR-EMP-00001"]
		self.assertEqual(row["gross_due"], 6750.0)
		self.assertEqual(row["day_rate"], 225.0)
		self.assertEqual(row["penalty_total"], 225.0)
		self.assertEqual(row["penalty_days"], 1.0)
		self.assertEqual(row["due_amount"], 6525.0)
		self.assertEqual(row["remaining"], 6525.0)
		self.assertEqual(row["payment_status"], "Unpaid")

	def test_base_and_variable_are_not_repurposed(self):
		# The screen still shows what the structure says; only `due_amount`
		# moves. Rewriting `base` would make the salary itself look reduced.
		row = _payroll(penalties_by_employee={"HR-EMP-00001": [_penalty()]})["HR-EMP-00001"]
		self.assertEqual(row["base"], 6000.0)
		self.assertEqual(row["variable"], 750.0)

	def test_several_penalties_add_up(self):
		rows = _payroll(
			penalties_by_employee={
				"HR-EMP-00001": [
					_penalty(),
					_penalty(name="JPEN-00002", unit="Half Days", quantity=1, amount=112.5, equivalent_days=0.5),
				]
			}
		)
		row = rows["HR-EMP-00001"]
		self.assertEqual(row["penalty_total"], 337.5)
		self.assertEqual(row["penalty_days"], 1.5)
		self.assertEqual(row["due_amount"], 6412.5)

	def test_the_penalties_are_listed_on_the_row(self):
		row = _payroll(penalties_by_employee={"HR-EMP-00001": [_penalty()]})["HR-EMP-00001"]
		self.assertEqual(len(row["penalties"]), 1)
		penalty = row["penalties"][0]
		self.assertEqual(penalty["name"], "JPEN-00001")
		self.assertEqual(penalty["unit"], "Days")
		self.assertEqual(penalty["amount"], 225.0)
		self.assertEqual(penalty["equivalent_days"], 1.0)
		self.assertFalse(penalty["settled"])

	def test_a_penalty_bigger_than_the_salary_owes_nothing_rather_than_less_than_nothing(self):
		# Only reachable with allow_overpay=1, but a negative due would flow into
		# the roll-up and silently cancel out somebody else's unpaid salary.
		row = _payroll(
			penalties_by_employee={"HR-EMP-00001": [_penalty(amount=9000.0, equivalent_days=40.0)]}
		)["HR-EMP-00001"]
		self.assertEqual(row["due_amount"], 0.0)
		self.assertEqual(row["net_payable"], 0.0)
		self.assertEqual(row["payment_status"], "Not Due")

	def test_the_penalty_reaches_the_month_summary(self):
		from jarz_pos.api.monthly_expenses import _summarize

		rows = list(_payroll(penalties_by_employee={"HR-EMP-00001": [_penalty()]}).values())
		summary = _summarize([], rows, 0.0, 0.0)
		# 6750 gross, 225 penalty: the month's payroll obligation is 6525.
		self.assertEqual(summary["due"], 6525.0)
		self.assertEqual(summary["remaining"], 6525.0)

	def test_a_penalty_for_another_employee_does_not_touch_this_row(self):
		rows = _payroll(penalties_by_employee={"HR-EMP-99999": [_penalty(employee="HR-EMP-99999")]})
		self.assertEqual(rows["HR-EMP-00001"]["penalty_total"], 0.0)
		self.assertEqual(rows["HR-EMP-00001"]["due_amount"], 6750.0)


class TestAdvancesAndOrdersOnThePayrollRow(unittest.TestCase):
	def test_an_open_advance_is_deducted_from_the_cash_to_hand_over(self):
		rows = _payroll(advances_by_employee={"HR-EMP-00001": [_advance()]})
		row = rows["HR-EMP-00001"]
		self.assertEqual(row["advance_total"], 500.0)
		# The DUE is untouched: the company still owes the salary, it just hands
		# over less cash because part of it is already in the employee's pocket.
		self.assertEqual(row["due_amount"], 6750.0)
		self.assertEqual(row["remaining"], 6750.0)
		self.assertEqual(row["net_payable"], 6250.0)
		self.assertEqual(row["deductions_total"], 500.0)

	def test_an_advance_from_another_month_is_still_deducted(self):
		# ALL-TIME on purpose. The fixture's advance is dated July and the month
		# under test is August; windowing it would make a real debt disappear.
		row = _payroll(
			advances_by_employee={"HR-EMP-00001": [_advance(posting_date=date(2026, 3, 1))]}
		)["HR-EMP-00001"]
		self.assertEqual(row["advance_total"], 500.0)

	def test_a_claimed_advance_carries_no_balance(self):
		row = _payroll(
			advances_by_employee={"HR-EMP-00001": [_advance(paid=500.0, claimed=500.0)]}
		)["HR-EMP-00001"]
		self.assertEqual(row["advance_total"], 0.0)
		self.assertEqual(row["net_payable"], 6750.0)

	def test_an_already_settled_advance_is_not_recovered_twice(self):
		row = _payroll(
			advances_by_employee={"HR-EMP-00001": [_advance(paid=500.0, returned=500.0, settled=500.0)]}
		)["HR-EMP-00001"]
		self.assertEqual(row["advance_total"], 0.0)

	def test_an_unpaid_staff_order_is_deducted(self):
		row = _payroll(orders_by_employee={"HR-EMP-00001": [_order()]})["HR-EMP-00001"]
		self.assertEqual(row["order_total"], 184.0)
		self.assertEqual(row["net_payable"], 6566.0)
		self.assertEqual(row["orders"][0]["invoice"], "ACC-SINV-2026-18146")

	def test_net_payable_is_remaining_less_advances_and_orders(self):
		row = _payroll(
			penalties_by_employee={"HR-EMP-00001": [_penalty()]},
			advances_by_employee={"HR-EMP-00001": [_advance()]},
			orders_by_employee={"HR-EMP-00001": [_order()]},
			paid_linked_by_employee={"HR-EMP-00001": 1000.0},
		)["HR-EMP-00001"]
		# 6750 gross - 225 penalty = 6525 due; 1000 already paid leaves 5525
		# remaining; less 500 advance and 184 jars.
		self.assertEqual(row["due_amount"], 6525.0)
		self.assertEqual(row["remaining"], 5525.0)
		self.assertEqual(row["net_payable"], 4841.0)
		self.assertEqual(row["deductions_total"], 909.0)

	def test_net_payable_is_floored_at_zero(self):
		# Owing the company more than a month's salary does not produce a
		# negative payslip; the remainder stays on the advance.
		row = _payroll(
			advances_by_employee={"HR-EMP-00001": [_advance(paid=5000.0)]},
			orders_by_employee={"HR-EMP-00001": [_order(outstanding=3000.0)]},
		)["HR-EMP-00001"]
		self.assertEqual(row["net_payable"], 0.0)

	def test_a_settlement_counts_as_paid_without_a_cash_payment(self):
		row = _payroll(settled_by_employee={"HR-EMP-00001": 500.0})["HR-EMP-00001"]
		self.assertEqual(row["paid_linked"], 0.0)
		self.assertEqual(row["settled_amount"], 500.0)
		self.assertEqual(row["paid_amount"], 500.0)
		self.assertEqual(row["remaining"], 6250.0)
		self.assertEqual(row["payment_status"], "Partial")


class TestOffPayrollRows(unittest.TestCase):
	"""Kareem Mamdouh and the CEO draw no salary and still owe money.

	"Make sure it appears here" is the whole request: an advance to someone with
	no Salary Structure Assignment must be visible, not silently absent because
	payroll has never heard of them.
	"""

	STUB = {
		"employee": "HR-EMP-00009",
		"employee_name": "Kareem Mamdouh",
		"base": 0.0,
		"variable": 0.0,
		"monthly": 0.0,
	}

	def _rows(self, **kwargs):
		return _payroll(off_payroll_rows=[self.STUB], **kwargs)

	def test_an_off_payroll_employee_with_an_advance_gets_a_row(self):
		rows = self._rows(
			advances_by_employee={"HR-EMP-00009": [_advance(employee="HR-EMP-00009", paid=5000.0)]}
		)
		row = rows["HR-EMP-00009"]
		self.assertTrue(row["off_payroll"])
		self.assertEqual(row["gross_due"], 0.0)
		self.assertEqual(row["day_rate"], 0.0)
		self.assertEqual(row["advance_total"], 5000.0)
		self.assertEqual(row["deductions_total"], 5000.0)

	def test_a_zero_due_row_owes_nothing_and_is_not_payable_money(self):
		rows = self._rows(
			advances_by_employee={"HR-EMP-00009": [_advance(employee="HR-EMP-00009", paid=5000.0)]}
		)
		row = rows["HR-EMP-00009"]
		self.assertEqual(row["due_amount"], 0.0)
		self.assertEqual(row["remaining"], 0.0)
		self.assertEqual(row["net_payable"], 0.0)
		self.assertEqual(row["payment_status"], "Not Due")

	def test_a_zero_due_row_does_not_inflate_the_unpaid_count(self):
		# A 0-due 0-paid row read as "Unpaid" would put a phantom outstanding
		# item on the manager's dashboard every single month.
		from jarz_pos.api.monthly_expenses import _summarize

		rows = list(
			self._rows(
				advances_by_employee={"HR-EMP-00009": [_advance(employee="HR-EMP-00009", paid=5000.0)]}
			).values()
		)
		summary = _summarize([], rows, 0.0, 0.0)
		self.assertEqual(summary["items_total"], 1)  # the salaried employee only
		self.assertEqual(summary["items_unpaid"], 1)
		self.assertEqual(summary["due"], 6750.0)
		self.assertEqual(summary["overpaid"], 0.0)

	def test_off_payroll_rows_sort_below_the_payroll(self):
		from jarz_pos.api.monthly_expenses import _build_payroll_rows

		rows = _build_payroll_rows(
			[_employee()],
			salary_account="Salary - J",
			off_payroll_rows=[self.STUB],
			advances_by_employee={"HR-EMP-00009": [_advance(employee="HR-EMP-00009")]},
		)
		self.assertEqual([r["employee"] for r in rows], ["HR-EMP-00001", "HR-EMP-00009"])

	def test_a_salaried_employee_is_never_marked_off_payroll(self):
		self.assertFalse(self._rows()["HR-EMP-00001"]["off_payroll"])


class TestDeductionsRollUp(unittest.TestCase):
	def _deductions(self, rows=None, **kwargs):
		from jarz_pos.api.monthly_expenses import _build_deductions

		if rows is None:
			rows = list(
				_payroll(
					penalties_by_employee={"HR-EMP-00001": [_penalty()]},
					advances_by_employee={"HR-EMP-00001": [_advance()]},
					orders_by_employee={"HR-EMP-00001": [_order()]},
				).values()
			)
		return _build_deductions(rows, **kwargs)

	def test_the_totals_are_the_sum_of_the_rows_beneath_them(self):
		deductions = self._deductions()
		self.assertEqual(deductions["penalty_total"], 225.0)
		self.assertEqual(deductions["penalty_days"], 1.0)
		self.assertEqual(deductions["advance_total"], 500.0)
		self.assertEqual(deductions["order_total"], 184.0)
		self.assertEqual(deductions["total"], 909.0)
		self.assertEqual(deductions["net_payable"], 5841.0)

	def test_the_client_is_told_the_units_and_the_day_basis(self):
		deductions = self._deductions([])
		self.assertEqual(deductions["penalty_units"], ["Days", "Half Days", "Money"])
		self.assertEqual(deductions["days_per_month"], 30)

	def test_an_advance_on_a_customer_receivable_is_reported(self):
		# Production's three advances all sit on `Debtors - J`, i.e. customer AR,
		# where the balance is mixed in with real customers' debt.
		deductions = self._deductions(
			[], advances_by_employee={"HR-EMP-00001": [_advance(account="Debtors - J")]}
		)
		suspect = deductions["advance_accounts_suspect"]
		self.assertEqual(len(suspect), 1)
		self.assertEqual(suspect[0]["account"], "Debtors - J")
		self.assertEqual(suspect[0]["total"], 500.0)

	def test_a_real_advance_ledger_is_not_reported(self):
		deductions = self._deductions(
			[], advances_by_employee={"HR-EMP-00001": [_advance()]}
		)
		self.assertEqual(deductions["advance_accounts_suspect"], [])


class TestDeductionGaps(unittest.TestCase):
	def _gaps(self, **deductions):
		from jarz_pos.api.monthly_expenses import _build_deductions, _build_gaps

		block = _build_deductions([], **deductions)
		return {
			g.get("code"): g
			for g in _build_gaps([], True, {}, {}, block)
			if g.get("code")
		}

	def test_zero_employee_purpose_invoices_is_reported_as_a_gap(self):
		# Production has none, so jar debt legitimately reads zero — and the
		# screen has to say WHY, or the zero reads as "nobody takes jars".
		gap = self._gaps(employee_orders_present=False).get("employee_orders_unused")
		self.assertIsNotNone(gap)
		self.assertEqual(gap["severity"], "info")
		self.assertIn("Employee Order", gap["message"])

	def test_the_gap_disappears_once_the_flow_is_used(self):
		self.assertNotIn(
			"employee_orders_unused", self._gaps(employee_orders_present=True)
		)

	def test_a_staff_order_linked_to_nobody_is_surfaced_not_dropped(self):
		# Staging carried exactly this: one Employee-purpose invoice, 92 EGP
		# still outstanding, on a Customer whose `custom_employee` was empty.
		# Attributing it would be a guess, so it stays off every row AND out of
		# `order_total` — but it must not vanish, or the board reports zero jar
		# debt while a real unpaid staff order exists.
		block = self._gaps(
			employee_orders_present=True,
			unattributed_orders=[
				{
					"name": "ACC-SINV-2026-18146",
					"customer": "E2E EMPFEAT Staff Tester",
					"customer_name": "E2E EMPFEAT Staff Tester",
					"outstanding_amount": 92.0,
					"grand_total": 92.0,
				}
			],
		)
		gap = block.get("employee_orders_unattributed")
		self.assertIsNotNone(gap)
		self.assertEqual(gap["severity"], "warning")
		self.assertIn("E2E EMPFEAT Staff Tester", gap["message"])

	def test_no_orphan_orders_raises_no_orphan_gap(self):
		self.assertNotIn(
			"employee_orders_unattributed",
			self._gaps(employee_orders_present=True, unattributed_orders=[]),
		)

	def test_an_orphan_order_stays_out_of_the_deducted_total(self):
		# It is money the company is owed, but not money any named person's
		# salary can be reduced by — so it is listed separately and never added
		# into `order_total`, which the rows must continue to sum to.
		from jarz_pos.api.monthly_expenses import _build_deductions

		block = _build_deductions(
			[],
			unattributed_orders=[
				{"name": "ACC-SINV-1", "customer": "X", "outstanding_amount": 92.0}
			],
		)
		self.assertEqual(block["order_total"], 0.0)
		self.assertEqual(block["unattributed_order_total"], 92.0)
		self.assertEqual(len(block["unattributed_orders"]), 1)

	def test_unreadable_advances_are_a_warning_not_a_silent_zero(self):
		gap = self._gaps(advances_readable=False).get("advances_unreadable")
		self.assertIsNotNone(gap)
		self.assertEqual(gap["severity"], "warning")

	def test_readable_advances_raise_no_such_gap(self):
		self.assertNotIn("advances_unreadable", self._gaps(advances_readable=True))

	def test_an_advance_on_customer_ar_is_a_warning(self):
		gap = self._gaps(
			advances_by_employee={"HR-EMP-00001": [_advance(account="Debtors - J")]}
		).get("advance_account_is_debtors")
		self.assertIsNotNone(gap)
		self.assertIn("Debtors - J", gap["message"])

	def test_an_older_four_argument_call_gains_no_new_gaps(self):
		# `_build_gaps` is called with four arguments from several places and
		# from the existing tests; those calls looked at no deductions at all and
		# must not start asserting things about them.
		from jarz_pos.api.monthly_expenses import _build_gaps

		gaps = _build_gaps([], True, {}, {})
		self.assertEqual([g for g in gaps if g.get("code")], [])


class TestAdvanceBalanceReadsDegradeWithoutHrms(unittest.TestCase):
	"""HRMS is not a required app. Half a screen beats none."""

	def _load(self, hrms=True, rows=None, raises=False):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		if raises:
			mock_frappe.get_all.side_effect = RuntimeError("no such table")
		else:
			mock_frappe.get_all.return_value = list(rows or [])

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "hrms_available", return_value=hrms
		), patch.object(
			monthly_expenses, "_advance_has_field", return_value=True
		):
			return monthly_expenses._load_advances("JARZ")

	def test_no_hrms_means_no_advances_and_an_explicit_unreadable_flag(self):
		grouped, readable = self._load(hrms=False)
		self.assertEqual(grouped, {})
		self.assertFalse(readable)

	def test_a_failed_query_is_unreadable_rather_than_zero(self):
		grouped, readable = self._load(raises=True)
		self.assertEqual(grouped, {})
		self.assertFalse(readable)

	def test_open_advances_are_grouped_by_employee(self):
		grouped, readable = self._load(rows=[_advance(), _advance(name="HR-EAD-2", paid=5000.0)])
		self.assertTrue(readable)
		self.assertEqual(len(grouped["HR-EMP-00001"]), 2)

	def test_a_fully_settled_advance_is_history_and_is_dropped(self):
		# A settlement reaches the balance through HRMS's `return_amount`, not
		# through the jarz column — see the next two tests.
		grouped, _readable = self._load(rows=[_advance(paid=500.0, returned=500.0)])
		self.assertEqual(grouped, {})

	def test_a_settlement_is_counted_once_not_twice(self):
		# HRMS derives `return_amount` from the Advance Payment Ledger Entry that
		# our settlement Journal Entry creates (set_total_advance_paid sums every
		# non-Expense-Claim voucher against the advance). So after settling 500
		# of 500, BOTH return_amount and custom_jarz_settled_amount read 500.
		# Subtracting both gives -500; the floor hides that at full settlement.
		grouped, _readable = self._load(
			rows=[_advance(paid=500.0, returned=500.0, settled=500.0)]
		)
		self.assertEqual(grouped, {})

	def test_a_partial_settlement_does_not_write_off_the_rest(self):
		# This is the case the floor could NOT hide, and the reason the jarz
		# column is out of the balance: recovering 200 of a 500 advance leaves
		# 300 genuinely owed. Counting the recovery twice reads 500 - 200 - 200
		# = 100, quietly writing off 200 the employee still owes.
		from jarz_pos.api.monthly_expenses import _advance_open_amount

		grouped, _readable = self._load(
			rows=[_advance(paid=500.0, returned=200.0, settled=200.0)]
		)
		self.assertEqual(_advance_open_amount(grouped["HR-EMP-00001"][0]), 300.0)

	def test_rows_still_render_when_advances_cannot_be_read(self):
		# The payroll table is built from maps, so an empty advance map costs the
		# advance column and nothing else.
		rows = _payroll(advances_by_employee={})
		row = rows["HR-EMP-00001"]
		self.assertEqual(row["due_amount"], 6750.0)
		self.assertEqual(row["advance_total"], 0.0)
		self.assertEqual(row["net_payable"], 6750.0)


# ── settlement: capping, refusing, and posting ────────────────────────────


class TestAdvanceSettlementPlanning(unittest.TestCase):
	"""The cap is the only thing between recovery and double recovery."""

	EMPLOYEE = "HR-EMP-00001"

	def _plan(self, requests, advances=None):
		from jarz_pos.api import monthly_expenses

		advances = {a["name"]: a for a in (advances or [_advance()])}

		def _get_value(doctype, name, fields, **kwargs):
			if kwargs.get("for_update"):
				return name
			return advances.get(name)

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = RuntimeError("refused")
		mock_frappe.db.get_value.side_effect = _get_value

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_advance_has_field", return_value=True
		):
			try:
				plan = monthly_expenses._plan_advance_settlements(
					self.EMPLOYEE, "JARZ", requests
				)
			except RuntimeError:
				return mock_frappe, None
		return mock_frappe, plan

	def test_an_unstated_amount_settles_the_whole_open_balance(self):
		_frappe, plan = self._plan([{"name": "HR-EAD-2026-00004", "amount": None}])
		self.assertEqual(plan[0]["amount"], 500.0)
		self.assertEqual(plan[0]["account"], "Employee Advances - J")

	def test_a_partial_settlement_is_taken_as_asked(self):
		_frappe, plan = self._plan([{"name": "HR-EAD-2026-00004", "amount": 200.0}])
		self.assertEqual(plan[0]["amount"], 200.0)

	def test_settling_more_than_is_open_is_refused(self):
		mock_frappe, plan = self._plan([{"name": "HR-EAD-2026-00004", "amount": 900.0}])
		self.assertIsNone(plan)
		message = str(mock_frappe.throw.call_args[0][0])
		self.assertIn("500", message)
		self.assertIn("twice", message)

	def test_a_rounding_overshoot_is_capped_not_refused(self):
		_frappe, plan = self._plan([{"name": "HR-EAD-2026-00004", "amount": 500.3}])
		self.assertEqual(plan[0]["amount"], 500.0)

	def test_the_same_advance_cannot_be_settled_a_second_time(self):
		# The state the FIRST settlement left behind: `custom_jarz_settled_amount`
		# now covers the whole payout, so there is nothing left to recover.
		mock_frappe, plan = self._plan(
			[{"name": "HR-EAD-2026-00004", "amount": None}],
			advances=[_advance(paid=500.0, returned=500.0, settled=500.0)],
		)
		self.assertIsNone(plan)
		self.assertIn("already been", str(mock_frappe.throw.call_args[0][0]))

	def test_a_partly_settled_advance_only_offers_what_is_left(self):
		_frappe, plan = self._plan(
			[{"name": "HR-EAD-2026-00004", "amount": None}],
			advances=[_advance(paid=500.0, returned=300.0, settled=300.0)],
		)
		self.assertEqual(plan[0]["amount"], 200.0)
		self.assertEqual(plan[0]["settled_before"], 300.0)

	def test_another_employees_advance_is_refused(self):
		mock_frappe, plan = self._plan(
			[{"name": "HR-EAD-2026-00004", "amount": None}],
			advances=[_advance(employee="HR-EMP-99999")],
		)
		self.assertIsNone(plan)
		self.assertIn("HR-EMP-99999", str(mock_frappe.throw.call_args[0][0]))

	def test_the_same_advance_listed_twice_in_one_call_is_refused(self):
		mock_frappe, plan = self._plan(
			[
				{"name": "HR-EAD-2026-00004", "amount": 200.0},
				{"name": "HR-EAD-2026-00004", "amount": 300.0},
			]
		)
		self.assertIsNone(plan)
		self.assertIn("twice", str(mock_frappe.throw.call_args[0][0]))

	def test_the_row_is_locked_before_its_balance_is_read(self):
		# Two settlements racing: the second must block here and then see the
		# balance the first one left, not the balance it started from.
		mock_frappe, _plan = self._plan([{"name": "HR-EAD-2026-00004", "amount": None}])
		first = mock_frappe.db.get_value.call_args_list[0]
		self.assertTrue(first.kwargs.get("for_update"))
		self.assertEqual(first.args[1], "HR-EAD-2026-00004")

	def test_nothing_requested_reads_nothing_at_all(self):
		mock_frappe, plan = self._plan([])
		self.assertEqual(plan, [])
		mock_frappe.db.get_value.assert_not_called()


class TestSettlementListParsing(unittest.TestCase):
	def _parse(self, value, key="name"):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = RuntimeError("refused")
		with patch.object(monthly_expenses, "frappe", mock_frappe):
			try:
				return monthly_expenses._parse_settlement_list(value, key)
			except RuntimeError:
				return None

	def test_a_json_string_is_accepted(self):
		self.assertEqual(
			self._parse('[{"name": "HR-EAD-1", "amount": 500}]'),
			[{"name": "HR-EAD-1", "amount": 500.0}],
		)

	def test_a_bare_list_of_names_means_the_whole_open_balance(self):
		# `None`, not 0: zero is "settle nothing", unstated is "settle it all".
		self.assertEqual(self._parse(["HR-EAD-1"]), [{"name": "HR-EAD-1", "amount": None}])

	def test_nothing_parses_to_nothing(self):
		for value in (None, "", []):
			self.assertEqual(self._parse(value), [])

	def test_the_orders_list_is_keyed_on_invoice(self):
		self.assertEqual(
			self._parse('[{"invoice": "ACC-SINV-1", "amount": 184}]', key="invoice"),
			[{"invoice": "ACC-SINV-1", "amount": 184.0}],
		)

	def test_malformed_json_is_refused(self):
		self.assertIsNone(self._parse("not json"))

	def test_an_entry_with_no_name_is_refused(self):
		self.assertIsNone(self._parse([{"amount": 500}]))


class TestPaySalaryWithSettlements(unittest.TestCase):
	"""Cash and settlement are two documents and one transaction."""

	EMPLOYEE = "HR-EMP-00001"

	def _context(self, due=6750.0, paid=0.0):
		row = {
			"employee": self.EMPLOYEE,
			"employee_name": "Employee 00001",
			"gross_due": due,
			"due_amount": due,
			"paid_amount": paid,
			"remaining": max(due - paid, 0.0),
			"advance_total": 500.0,
			"advances": [{"name": "HR-EAD-2026-00004", "outstanding": 500.0}],
			"order_total": 184.0,
			"orders": [{"invoice": "ACC-SINV-2026-18146", "outstanding": 184.0}],
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
				"unattributed_gl": 0.0,
				"due": due,
				"paid": paid,
				"remaining": max(due - paid, 0.0),
			},
			"deductions": {"advance_total": 500.0},
			"summary": {},
		}

	def _pay(
		self,
		amount=0,
		settle_advances=None,
		settle_orders=None,
		advance_plan=None,
		order_plan=None,
		**context_kwargs,
	):
		from jarz_pos.api import monthly_expenses

		fake_doc = _FakeDoc()
		guard_calls = []

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_doc.return_value = fake_doc
		mock_frappe.throw.side_effect = RuntimeError("refused")

		default_advance_plan = [
			{
				"name": "HR-EAD-2026-00004",
				"amount": 500.0,
				"account": "Employee Advances - J",
				"open_amount": 500.0,
				"settled_before": 0.0,
			}
		]

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses, "_default_company", return_value="JARZ"
		), patch.object(
			monthly_expenses, "_submitted_salary_slips", return_value={}
		), patch.object(
			monthly_expenses, "_compute_month", return_value=self._context(**context_kwargs)
		), patch.object(
			monthly_expenses, "_resolve_paying_account", return_value="Cash - J"
		) as resolve, patch.object(
			monthly_expenses, "_serialize_expense", return_value={"name": fake_doc.name}
		), patch.object(
			monthly_expenses,
			"_plan_advance_settlements",
			return_value=(default_advance_plan if advance_plan is None else advance_plan),
		) as plan_advances, patch.object(
			monthly_expenses, "_plan_order_settlements", return_value=(order_plan or [])
		) as plan_orders, patch.object(
			monthly_expenses,
			"_post_settlement_journal_entry",
			return_value="ACC-JV-2026-00042",
		) as post_je, patch.object(
			monthly_expenses,
			"_guard_overpay",
			side_effect=lambda *args, **kwargs: guard_calls.append(args) or 0.0,
		):
			try:
				result = monthly_expenses.pay_salary(
					self.EMPLOYEE,
					month=MONTH_KEY,
					amount=amount,
					paying_account="Cash - J",
					settle_advances=settle_advances,
					settle_orders=settle_orders,
				)
				refused = False
			except RuntimeError:
				result, refused = None, True
		return {
			"result": result,
			"refused": refused,
			"frappe": mock_frappe,
			"doc": fake_doc,
			"post_je": post_je,
			"plan_advances": plan_advances,
			"plan_orders": plan_orders,
			"resolve": resolve,
			"guard_calls": guard_calls,
		}

	# ── a settlement-only payslip ─────────────────────────────────────────

	def test_zero_cash_with_a_settlement_posts_only_the_settlement(self):
		# The whole salary went on advances: no cash moves, so no Jarz Expense
		# Request and no cash Journal Entry — but the advance must still close.
		out = self._pay(amount=0, settle_advances=["HR-EAD-2026-00004"])
		self.assertFalse(out["refused"])
		out["post_je"].assert_called_once()
		out["frappe"].get_doc.assert_not_called()
		out["doc"].submit.assert_not_called()
		self.assertIsNone(out["result"]["payment"])
		self.assertEqual(out["result"]["settlement"]["journal_entry"], "ACC-JV-2026-00042")
		self.assertEqual(out["result"]["settlement"]["total"], 500.0)

	def test_a_settlement_only_payslip_needs_no_paying_account(self):
		# No cash leaves any drawer, so refusing for want of a cash account would
		# be refusing for the wrong reason.
		out = self._pay(amount=0, settle_advances=["HR-EAD-2026-00004"])
		out["resolve"].assert_not_called()

	def test_zero_cash_and_no_settlement_is_still_refused(self):
		out = self._pay(amount=0, advance_plan=[])
		self.assertTrue(out["refused"])
		out["post_je"].assert_not_called()
		out["frappe"].get_doc.assert_not_called()

	def test_a_negative_amount_is_refused_before_anything_is_planned(self):
		out = self._pay(amount=-100, advance_plan=[])
		self.assertTrue(out["refused"])
		out["plan_advances"].assert_not_called()

	# ── cash and settlement together ──────────────────────────────────────

	def test_both_halves_are_posted_for_a_mixed_payslip(self):
		out = self._pay(amount=6250, settle_advances=["HR-EAD-2026-00004"])
		self.assertFalse(out["refused"])
		out["doc"].submit.assert_called_once()
		out["post_je"].assert_called_once()
		self.assertEqual(out["result"]["payment"], {"name": "JER-0009"})
		self.assertEqual(out["result"]["settlement"]["total"], 500.0)

	def test_the_guard_measures_cash_plus_settlement(self):
		# Paying 6,250 in cash and settling 500 discharges the whole 6,750. The
		# guard has to see 6,750, or a second 500 could be paid on top.
		out = self._pay(amount=6250, settle_advances=["HR-EAD-2026-00004"])
		_label, due, already_paid, requested, _allow, _month = out["guard_calls"][0]
		self.assertEqual(due, 6750.0)
		self.assertEqual(already_paid, 0.0)
		self.assertEqual(requested, 6750.0)

	def test_the_settlement_journal_entry_is_told_the_employee_and_the_month(self):
		out = self._pay(amount=0, settle_advances=["HR-EAD-2026-00004"])
		kwargs = out["post_je"].call_args.kwargs
		self.assertEqual(kwargs["employee"], self.EMPLOYEE)
		self.assertEqual(kwargs["month_key"], MONTH_KEY)
		self.assertEqual(kwargs["salary_account"], "Salary - J")

	def test_only_this_employees_open_orders_may_be_settled(self):
		out = self._pay(amount=0, settle_orders=["ACC-SINV-2026-18146"])
		allowed = out["plan_orders"].call_args.args[3]
		self.assertEqual(list(allowed), ["ACC-SINV-2026-18146"])


class TestSettlementJournalEntryShape(unittest.TestCase):
	"""What ERPNext needs in order to actually reduce the two balances."""

	def _post(self, advance_plan=None, order_plan=None, remarks=None):
		from jarz_pos.api import monthly_expenses

		je = MagicMock()
		je.accounts = []
		je.name = "ACC-JV-2026-00042"

		def _append(table, row):
			je.accounts.append(row)

		je.append.side_effect = _append

		mock_frappe = MagicMock()
		mock_frappe.new_doc.return_value = je

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_advance_has_field", return_value=True
		), patch.object(
			monthly_expenses, "apply_ledger_posting_datetime"
		), patch.object(
			monthly_expenses, "join_posting_datetime", return_value="2026-08-31 12:00:00"
		):
			name = monthly_expenses._post_settlement_journal_entry(
				company="JARZ",
				employee="HR-EMP-00001",
				month_key=MONTH_KEY,
				salary_account="Salary - J",
				advance_plan=advance_plan
				if advance_plan is not None
				else [
					{
						"name": "HR-EAD-2026-00004",
						"amount": 500.0,
						"account": "Employee Advances - J",
						"settled_before": 0.0,
					}
				],
				order_plan=order_plan or [],
				posting_date="2026-08-31",
				posting_time="12:00:00",
				remarks=remarks,
			)
		return name, je, mock_frappe

	def test_the_salary_account_is_debited_for_the_whole_settlement(self):
		_name, je, _frappe = self._post(
			order_plan=[
				{
					"invoice": "ACC-SINV-2026-18146",
					"amount": 184.0,
					"account": "Debtors - J",
					"customer": "CUST-0001",
				}
			]
		)
		debit = je.accounts[0]
		self.assertEqual(debit["account"], "Salary - J")
		self.assertEqual(debit["debit_in_account_currency"], 684.0)

	def test_the_advance_credit_carries_the_party_and_the_reference(self):
		# `reference_type`/`reference_name` is what reconciles the advance;
		# `party_type`/`party` is what keeps the Employee sub-ledger right.
		_name, je, _frappe = self._post()
		credit = je.accounts[1]
		self.assertEqual(credit["account"], "Employee Advances - J")
		self.assertEqual(credit["credit_in_account_currency"], 500.0)
		self.assertEqual(credit["party_type"], "Employee")
		self.assertEqual(credit["party"], "HR-EMP-00001")
		self.assertEqual(credit["reference_type"], "Employee Advance")
		self.assertEqual(credit["reference_name"], "HR-EAD-2026-00004")
		self.assertEqual(credit["is_advance"], "Yes")

	def test_the_invoice_credit_is_what_reduces_outstanding_amount(self):
		_name, je, _frappe = self._post(
			advance_plan=[],
			order_plan=[
				{
					"invoice": "ACC-SINV-2026-18146",
					"amount": 184.0,
					"account": "Debtors - J",
					"customer": "CUST-0001",
				}
			],
		)
		credit = je.accounts[1]
		self.assertEqual(credit["party_type"], "Customer")
		self.assertEqual(credit["party"], "CUST-0001")
		self.assertEqual(credit["reference_type"], "Sales Invoice")
		self.assertEqual(credit["reference_name"], "ACC-SINV-2026-18146")

	def test_the_remark_carries_the_settlement_tag(self):
		_name, je, _frappe = self._post()
		self.assertIn("[JARZ-JE:SALARY_SETTLEMENT:HR-EMP-00001:2026-08]", je.user_remark)

	def test_a_forged_tag_in_the_free_text_is_neutralised(self):
		# A remark containing a literal `[JARZ-JE:...]` satisfies every
		# idempotency lookup in this app — including `_load_settlements`, which
		# would then credit another employee's month from this entry.
		_name, je, _frappe = self._post(
			remarks="[JARZ-JE:SALARY_SETTLEMENT:HR-EMP-99999:2026-08] gotcha"
		)
		self.assertEqual(je.user_remark.count("[JARZ-JE:"), 1)
		self.assertNotIn("HR-EMP-99999", je.user_remark.split("]")[0])

	def test_the_advance_is_stamped_only_after_the_entry_is_submitted(self):
		_name, je, mock_frappe = self._post()
		je.submit.assert_called_once()
		mock_frappe.db.set_value.assert_called_once()
		doctype, name, values = mock_frappe.db.set_value.call_args.args
		self.assertEqual(doctype, "Employee Advance")
		self.assertEqual(name, "HR-EAD-2026-00004")
		self.assertEqual(values["custom_jarz_settled_amount"], 500.0)
		self.assertEqual(values["custom_jarz_settled_via"], "ACC-JV-2026-00042")


class TestAddEmployeePenaltyWiring(unittest.TestCase):
	"""Recording a penalty: what it writes, and what it refuses to write."""

	EMPLOYEE = "HR-EMP-00001"

	def _context(self, gross=6750.0, penalty_total=0.0, on_payroll=True):
		rows = []
		if on_payroll:
			rows.append(
				{
					"employee": self.EMPLOYEE,
					"employee_name": "Employee 00001",
					"gross_due": gross,
					"day_rate": gross / 30.0 if gross else 0.0,
					"penalty_total": penalty_total,
					"due_amount": gross - penalty_total,
					"paid_amount": 0.0,
					"off_payroll": False,
				}
			)
		return {
			"month": MONTH_KEY,
			"company": "JARZ",
			"registry": [],
			"payroll": {"rows": rows},
			"deductions": {},
			"summary": {},
		}

	def _add(self, **kwargs):
		from jarz_pos.api import monthly_expenses

		payload = {
			"unit": "Days",
			"quantity": 1,
			"reason": "Absent without notice",
			"penalty_date": "2026-08-03",
		}
		payload.update({k: v for k, v in kwargs.items() if k not in ("context",)})

		fake_doc = _FakeDoc("JPEN-00001")
		captured = {}
		order = []

		def _get_doc(data):
			captured.update(data)
			return fake_doc

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.throw.side_effect = RuntimeError("refused")
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: order.append(("lock", a, kw))

		context = kwargs.get("context") or self._context()

		def _compute(*_args, **_kwargs):
			order.append(("compute", None, None))
			return context

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses, "_penalty_doctype_ready", return_value=True
		), patch.object(
			monthly_expenses,
			"_month_context",
			return_value=(MONTH_START, MONTH_END, MONTH_KEY, "JARZ", "EGP"),
		), patch.object(
			monthly_expenses, "_compute_month", side_effect=_compute
		):
			try:
				result = monthly_expenses.add_employee_penalty(self.EMPLOYEE, **payload)
				refused = False
			except RuntimeError:
				result, refused = None, True
		return {
			"result": result,
			"refused": refused,
			"captured": captured,
			"doc": fake_doc,
			"frappe": mock_frappe,
			"order": order,
		}

	def test_a_day_penalty_is_stored_as_money_and_as_days(self):
		out = self._add(unit="Days", quantity=2)
		self.assertFalse(out["refused"])
		captured = out["captured"]
		self.assertEqual(captured["doctype"], "Jarz Employee Penalty")
		self.assertEqual(captured["employee"], self.EMPLOYEE)
		self.assertEqual(captured["period_month"], MONTH_KEY)
		self.assertEqual(captured["unit"], "Days")
		self.assertEqual(captured["amount"], 450.0)
		self.assertEqual(captured["equivalent_days"], 2.0)
		self.assertEqual(captured["day_rate"], 225.0)
		out["doc"].submit.assert_called_once()

	def test_a_half_day_penalty_is_half_the_day_rate(self):
		out = self._add(unit="Half Days", quantity=1)
		self.assertEqual(out["captured"]["amount"], 112.5)
		self.assertEqual(out["captured"]["equivalent_days"], 0.5)

	def test_a_money_penalty_is_taken_as_given_and_converted_back_to_days(self):
		out = self._add(unit="Money", quantity=0, amount=450)
		self.assertEqual(out["captured"]["amount"], 450.0)
		self.assertEqual(out["captured"]["equivalent_days"], 2.0)

	def test_the_day_rate_is_snapshotted_onto_the_document(self):
		# A raise six months later must not silently re-price a penalty already
		# agreed with the employee.
		out = self._add(unit="Days", quantity=1)
		self.assertEqual(out["captured"]["day_rate"], 225.0)

	def test_a_penalty_with_no_reason_is_refused(self):
		out = self._add(reason="  ")
		self.assertTrue(out["refused"])
		out["frappe"].get_doc.assert_not_called()
		self.assertIn("reason", str(out["frappe"].throw.call_args[0][0]))

	def test_an_unknown_unit_is_refused(self):
		out = self._add(unit="Weeks")
		self.assertTrue(out["refused"])
		out["frappe"].get_doc.assert_not_called()

	def test_a_day_penalty_without_a_salary_structure_is_refused_with_advice(self):
		out = self._add(unit="Days", quantity=1, context=self._context(on_payroll=False))
		self.assertTrue(out["refused"])
		message = str(out["frappe"].throw.call_args[0][0])
		self.assertIn("money", message)
		out["frappe"].get_doc.assert_not_called()

	def test_a_money_penalty_is_still_allowed_off_payroll(self):
		# The off-payroll escape hatch: no day has a price, but 450 EGP does.
		out = self._add(
			unit="Money", quantity=0, amount=450, context=self._context(on_payroll=False)
		)
		self.assertFalse(out["refused"])
		self.assertEqual(out["captured"]["amount"], 450.0)
		self.assertEqual(out["captured"]["day_rate"], 0.0)

	def test_penalties_beyond_the_whole_salary_are_refused(self):
		out = self._add(
			unit="Days", quantity=1, context=self._context(penalty_total=6700.0)
		)
		self.assertTrue(out["refused"])
		out["frappe"].get_doc.assert_not_called()
		self.assertIn("allow_overpay", str(out["frappe"].throw.call_args[0][0]))

	def test_allow_overpay_forfeits_the_whole_month_deliberately(self):
		out = self._add(
			unit="Days",
			quantity=1,
			allow_overpay=1,
			context=self._context(penalty_total=6700.0),
		)
		self.assertFalse(out["refused"])
		out["doc"].submit.assert_called_once()

	def test_a_zero_quantity_is_refused(self):
		out = self._add(unit="Days", quantity=0)
		self.assertTrue(out["refused"])

	def test_the_employee_row_is_locked_before_the_state_the_guard_reads(self):
		out = self._add()
		step, args, kwargs = out["order"][0]
		self.assertEqual(step, "lock")
		self.assertEqual(args[0], "Employee")
		self.assertEqual(args[1], self.EMPLOYEE)
		self.assertTrue(kwargs.get("for_update"))
		self.assertIn("compute", [s for s, _a, _kw in out["order"]])


class TestCancelEmployeePenalty(unittest.TestCase):
	def _cancel(self, settled=0, docstatus=1, reason="Wrong person"):
		from jarz_pos.api import monthly_expenses

		doc = MagicMock()
		doc.docstatus = docstatus
		doc.settled = settled
		doc.settled_via = "JER-0009" if settled else None
		doc.employee = "HR-EMP-00001"
		doc.period_month = MONTH_KEY
		doc.company = "JARZ"

		mock_frappe = MagicMock()
		mock_frappe.get_doc.return_value = doc
		mock_frappe.throw.side_effect = RuntimeError("refused")

		with patch.object(monthly_expenses, "frappe", mock_frappe), patch.object(
			monthly_expenses, "_ensure_manager"
		), patch.object(monthly_expenses, "_require_period_fields"), patch.object(
			monthly_expenses,
			"_compute_month",
			return_value={"payroll": {"rows": []}, "summary": {}, "deductions": {}},
		):
			try:
				result = monthly_expenses.cancel_employee_penalty("JPEN-00001", reason)
				refused = False
			except RuntimeError:
				result, refused = None, True
		return result, refused, doc, mock_frappe

	def test_an_active_penalty_is_cancelled(self):
		result, refused, doc, _frappe = self._cancel()
		self.assertFalse(refused)
		doc.cancel.assert_called_once()
		self.assertTrue(result["success"])

	def test_a_settled_penalty_cannot_be_cancelled(self):
		# The reduced salary has already been paid; un-deducting it here would
		# silently re-open a month that was settled with the employee.
		_result, refused, doc, mock_frappe = self._cancel(settled=1)
		self.assertTrue(refused)
		doc.cancel.assert_not_called()
		self.assertIn("JER-0009", str(mock_frappe.throw.call_args[0][0]))

	def test_an_already_cancelled_penalty_is_refused(self):
		_result, refused, doc, _frappe = self._cancel(docstatus=2)
		self.assertTrue(refused)
		doc.cancel.assert_not_called()

	def test_a_cancellation_needs_a_reason(self):
		_result, refused, doc, _frappe = self._cancel(reason="   ")
		self.assertTrue(refused)
		doc.cancel.assert_not_called()


class TestSettlementReadBack(unittest.TestCase):
	"""A settlement is a Journal Entry, so the month finds it by its tag."""

	def _load(self, remarks_and_totals):
		from jarz_pos.api import monthly_expenses

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = [
			{"name": "ACC-JV-{0}".format(i), "user_remark": remark, "total_debit": total}
			for i, (remark, total) in enumerate(remarks_and_totals)
		]
		with patch.object(monthly_expenses, "frappe", mock_frappe):
			return monthly_expenses._load_settlements(MONTH_KEY, "JARZ")

	def test_the_employee_and_month_are_read_out_of_the_tag(self):
		settled = self._load(
			[("[JARZ-JE:SALARY_SETTLEMENT:HR-EMP-00001:2026-08] salary", 684.0)]
		)
		self.assertEqual(settled, {"HR-EMP-00001": 684.0})

	def test_another_months_settlement_is_not_credited_to_this_one(self):
		settled = self._load(
			[("[JARZ-JE:SALARY_SETTLEMENT:HR-EMP-00001:2026-07] salary", 684.0)]
		)
		self.assertEqual(settled, {})

	def test_an_untagged_entry_is_ignored(self):
		self.assertEqual(self._load([("Ordinary journal entry", 5000.0)]), {})

	def test_two_settlements_for_one_employee_add_up(self):
		settled = self._load(
			[
				("[JARZ-JE:SALARY_SETTLEMENT:HR-EMP-00001:2026-08] a", 500.0),
				("[JARZ-JE:SALARY_SETTLEMENT:HR-EMP-00001:2026-08] b", 184.0),
			]
		)
		self.assertEqual(settled, {"HR-EMP-00001": 684.0})


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
				"add_employee_penalty",
				"cancel_employee_penalty",
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

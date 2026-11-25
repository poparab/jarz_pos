"""Tests for the post-dispatch return workflow.

The invariants worth locking down are the ones that are expensive to discover in
production:

* the source invoice is never voided — a return only ever adds documents;
* every Journal Entry balances, and none can be posted to a party account
  without a party (ERPNext v16 rejects that outright);
* the freight reversal appears if and only if the operator declined to pay the
  courier for the trip;
* dedup keys are derived from the credit note, so a *second* partial return
  posts its own entries instead of silently matching the first one's.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import invoice_return as ir


def _invoice(**overrides):
	data = {
		"name": "ACC-SINV-2026-00001",
		"docstatus": 1,
		"is_return": 0,
		"custom_sales_invoice_state": "Out for Delivery",
		"custom_was_out_for_delivery": 1,
		"outstanding_amount": 0.0,
		"grand_total": 500.0,
		"custom_return_status": None,
		"custom_is_pickup": 0,
		"sales_partner": None,
	}
	data.update(overrides)
	doc = MagicMock()
	doc.name = data["name"]
	doc.company = "JARZ"
	doc.customer = "CUST-0001"
	doc.get.side_effect = lambda key, default=None: data.get(key, default)
	return doc


class TestLineParsing(unittest.TestCase):
	def test_parses_json_string(self):
		parsed = ir._parse_lines('[{"si_detail": "row-1", "qty": 2}]')
		self.assertEqual(parsed, [{"si_detail": "row-1", "qty": 2.0}])

	def test_parses_list_of_dicts(self):
		parsed = ir._parse_lines([{"si_detail": "row-1", "qty": 3}])
		self.assertEqual(parsed, [{"si_detail": "row-1", "qty": 3.0}])

	def test_drops_zero_and_negative_quantities(self):
		parsed = ir._parse_lines(
			[{"si_detail": "a", "qty": 0}, {"si_detail": "b", "qty": -1}, {"si_detail": "c", "qty": 1}]
		)
		self.assertEqual([row["si_detail"] for row in parsed], ["c"])

	def test_malformed_input_is_empty_not_an_error(self):
		self.assertEqual(ir._parse_lines("not json"), [])
		self.assertEqual(ir._parse_lines(None), [])
		self.assertEqual(ir._parse_lines([1, 2, 3]), [])


class TestMoneyState(unittest.TestCase):
	@patch.object(ir, "_unsettled_courier_transactions", return_value=[])
	def test_unpaid_when_outstanding_equals_total(self, _txns):
		self.assertEqual(
			ir._money_state(_invoice(outstanding_amount=500.0, grand_total=500.0)),
			ir.MONEY_UNPAID,
		)

	@patch.object(ir, "_unsettled_courier_transactions", return_value=[])
	def test_prepaid_when_settled_with_no_courier_transaction(self, _txns):
		self.assertEqual(
			ir._money_state(_invoice(outstanding_amount=0.0, grand_total=500.0)),
			ir.MONEY_PREPAID,
		)

	@patch.object(ir, "_unsettled_courier_transactions")
	def test_courier_outstanding_beats_prepaid(self, txns):
		"""An invoice whose receivable moved to Courier Outstanding reads as
		'paid' by outstanding_amount alone — the courier row must win."""
		txns.return_value = [{"amount": 500.0, "status": "Unsettled"}]
		self.assertEqual(
			ir._money_state(_invoice(outstanding_amount=0.0, grand_total=500.0)),
			ir.MONEY_COURIER_OUTSTANDING,
		)

	@patch.object(ir, "_unsettled_courier_transactions")
	def test_courier_settled_is_distinct(self, txns):
		txns.return_value = [{"amount": 500.0, "status": "Settled"}]
		self.assertEqual(
			ir._money_state(_invoice(outstanding_amount=0.0, grand_total=500.0)),
			ir.MONEY_COURIER_SETTLED,
		)


@patch.object(ir, "get_freight_expense_account", return_value="Freight - J")
@patch.object(ir, "get_creditors_account", return_value="Creditors - J")
@patch.object(ir, "_get_courier_outstanding_account", return_value="Courier Outstanding - J")
@patch.object(ir, "get_company_receivable_account", return_value="Debtors - J")
class TestJournalLegPlanning(unittest.TestCase):
	"""Each planned entry must balance, and only the right ones must appear."""

	@staticmethod
	def _balanced(plan):
		debit = sum(l["amount"] for l in plan["legs"] if l.get("debit"))
		credit = sum(l["amount"] for l in plan["legs"] if not l.get("debit"))
		return abs(debit - credit) < 0.005

	def _plan(self, money_state, *, pay_courier=True, courier=None, courier_release=None):
		return ir.plan_return_journal_legs(
			inv=_invoice(),
			money_state=money_state,
			return_total=500.0,
			credit_note_name="ACC-SINV-2026-00099",
			pay_courier_for_trip=pay_courier,
			courier=courier,
			courier_release=courier_release,
		)

	def test_deposit_is_not_released_from_courier_outstanding(self, *_):
		"""500 order, 200 deposit at the branch: the courier carried 300 only."""
		plans = self._plan(ir.MONEY_COURIER_OUTSTANDING, courier_release=300.0)
		legs = plans[0]["legs"]
		self.assertTrue(self._balanced(plans[0]))
		release = next(l for l in legs if l["account"] == "Courier Outstanding - J")
		self.assertEqual(release["amount"], 300.0)
		credit_note_leg = next(l for l in legs if l.get("reference_name") == "ACC-SINV-2026-00099")
		# The CN keeps -200 open: the customer's deposit, as credit or refund.
		self.assertEqual(credit_note_leg["amount"], 300.0)

	def test_release_never_exceeds_the_return(self, *_):
		plans = self._plan(ir.MONEY_COURIER_OUTSTANDING, courier_release=900.0)
		release = next(l for l in plans[0]["legs"] if l["account"] == "Courier Outstanding - J")
		self.assertEqual(release["amount"], 500.0)

	def test_unpaid_posts_a_balanced_ar_knockoff(self, *_):
		plans = self._plan(ir.MONEY_UNPAID)
		self.assertEqual([p["je_type"] for p in plans], [ir.JE_AR_KNOCKOFF])
		self.assertTrue(self._balanced(plans[0]))

	def test_ar_knockoff_references_both_vouchers(self, *_):
		"""Both outstandings must land on zero, which needs one reference each."""
		legs = self._plan(ir.MONEY_UNPAID)[0]["legs"]
		refs = {l.get("reference_name") for l in legs}
		self.assertEqual(refs, {"ACC-SINV-2026-00001", "ACC-SINV-2026-00099"})
		for leg in legs:
			self.assertEqual(leg["party_type"], "Customer")

	def test_courier_outstanding_releases_the_courier(self, *_):
		plans = self._plan(ir.MONEY_COURIER_OUTSTANDING)
		self.assertEqual([p["je_type"] for p in plans], [ir.JE_COURIER_OUTSTANDING])
		self.assertTrue(self._balanced(plans[0]))
		accounts = {l["account"] for l in plans[0]["legs"]}
		self.assertIn("Courier Outstanding - J", accounts)

	def test_prepaid_posts_no_reversal(self, *_):
		"""The money is already in the branch; the refund handles it."""
		self.assertEqual(self._plan(ir.MONEY_PREPAID), [])

	def test_courier_settled_posts_no_reversal(self, *_):
		self.assertEqual(self._plan(ir.MONEY_COURIER_SETTLED), [])

	def test_freight_reversed_only_when_courier_is_not_paid(self, *_):
		courier = {"party_type": "Supplier", "party": "SUP-1", "shipping_amount": 40.0}

		paid = self._plan(ir.MONEY_PREPAID, pay_courier=True, courier=courier)
		self.assertEqual(paid, [])

		unpaid = self._plan(ir.MONEY_PREPAID, pay_courier=False, courier=courier)
		self.assertEqual([p["je_type"] for p in unpaid], [ir.JE_FREIGHT_REVERSAL])
		self.assertTrue(self._balanced(unpaid[0]))

	def test_freight_reversal_carries_the_courier_party(self, *_):
		"""v16 rejects a Creditors line with no party."""
		courier = {"party_type": "Supplier", "party": "SUP-1", "shipping_amount": 40.0}
		legs = self._plan(ir.MONEY_PREPAID, pay_courier=False, courier=courier)[0]["legs"]
		creditors = next(l for l in legs if l["account"] == "Creditors - J")
		self.assertEqual(creditors["party_type"], "Supplier")
		self.assertEqual(creditors["party"], "SUP-1")

	def test_zero_shipping_reverses_nothing(self, *_):
		courier = {"party_type": "Supplier", "party": "SUP-1", "shipping_amount": 0.0}
		self.assertEqual(self._plan(ir.MONEY_PREPAID, pay_courier=False, courier=courier), [])


class TestCourierCarriedAmounts(unittest.TestCase):
	"""What the courier carries vs what the branch already took as a deposit."""

	ROWS = [
		{"amount": 300.0, "status": "Unsettled"},  # dispatch: 500 order, 200 deposit
	]

	def test_open_amount_nets_earlier_returns(self):
		rows = self.ROWS + [{"amount": -100.0, "status": "Unsettled"}]
		self.assertEqual(ir._courier_open_amount(rows), 200.0)

	def test_settled_rows_are_not_open(self):
		rows = [{"amount": 300.0, "status": "Settled"}]
		self.assertEqual(ir._courier_open_amount(rows), 0.0)

	def test_open_amount_never_negative(self):
		rows = self.ROWS + [{"amount": -500.0, "status": "Unsettled"}]
		self.assertEqual(ir._courier_open_amount(rows), 0.0)

	def test_deposit_is_grand_total_minus_dispatched(self):
		self.assertEqual(ir._deposit_collected(_invoice(grand_total=500.0), self.ROWS), 200.0)

	def test_plain_cod_has_no_deposit(self):
		rows = [{"amount": 500.0, "status": "Unsettled"}]
		self.assertEqual(ir._deposit_collected(_invoice(grand_total=500.0), rows), 0.0)

	def test_no_dispatch_no_deposit(self):
		self.assertEqual(ir._deposit_collected(_invoice(grand_total=500.0), []), 0.0)


class TestCourierFeePosition(unittest.TestCase):
	"""The trip fee is found on its own terms, not on the row that carries cash.

	Production order 21983 (2026-11-17): paid by InstaPay, pulled back 21 minutes
	after dispatch, and the courier was still paid 80 — because the fee was only
	ever looked up on a row with ``amount > 0`` and an online order has none.
	"""

	ONLINE = {
		"name": "CT-1",
		"party_type": "Supplier",
		"party": "SUP-1",
		"amount": 0.0,
		"shipping_amount": 80.0,
		"status": "Unsettled",
	}
	PARTNER = {
		"name": "CT-2",
		"party_type": "Supplier",
		"party": "RIDER-1",
		"amount": 0.0,
		"shipping_amount": 0.0,
		"status": "Settled",
		"is_partner_order": 1,
		"delivery_partner": "Deliverk",
		"partner_fee": 50.0,
		"partner_settled": 0,
	}

	def test_an_online_order_has_no_cash_row(self):
		self.assertIsNone(ir._cash_courier_row([self.ONLINE]))

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_an_online_order_still_has_a_fee_to_decide(self, _find):
		position = ir._courier_fee_position(_invoice(), [self.ONLINE])
		self.assertEqual(position["shipping_amount"], 80.0)
		self.assertEqual((position["party_type"], position["party"]), ("Supplier", "SUP-1"))
		self.assertEqual(position["is_partner_order"], 0)

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_a_fee_already_paid_out_is_left_alone(self, _find):
		"""A paid fee cannot be taken back by a return: no settlement screen can
		collect from a courier, so a lone negative row would be closed without a
		posting and the reversal entry would sit on Creditors for good."""
		row = dict(self.ONLINE, status="Settled")
		self.assertIsNone(ir._courier_fee_position(_invoice(), [row]))
		self.assertEqual(ir._courier_fee_already_paid([row]), 80.0)

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_an_unpaid_fee_is_not_reported_as_paid(self, _find):
		self.assertEqual(ir._courier_fee_already_paid([self.ONLINE]), 0.0)

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_a_settled_pair_leaves_nothing_to_reverse(self, _find):
		"""Fee reversed by an earlier return, then both rows closed: still nothing."""
		rows = [
			dict(self.ONLINE, status="Settled"),
			dict(self.ONLINE, name="CT-R", shipping_amount=-80.0, status="Settled"),
		]
		self.assertIsNone(ir._courier_fee_position(_invoice(), rows))
		self.assertEqual(ir._courier_fee_already_paid(rows), 0.0)

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_a_fee_already_reversed_is_not_reversed_twice(self, _find):
		"""Second partial return: the first one's negative row nets the fee to zero."""
		rows = [self.ONLINE, dict(self.ONLINE, name="CT-R", shipping_amount=-80.0)]
		self.assertIsNone(ir._courier_fee_position(_invoice(), rows))

	@patch.object(ir, "_find_existing_je_by_tag", return_value=None)
	def test_no_accrual_means_nothing_to_reverse(self, _find):
		"""The reversal mirrors the dispatch accrual; with none booked there is no mirror."""
		self.assertIsNone(ir._courier_fee_position(_invoice(), [self.ONLINE]))

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_a_prepaid_partner_order_reports_the_partner_fee(self, _find):
		position = ir._courier_fee_position(_invoice(), [self.PARTNER])
		self.assertEqual(position["partner_fee"], 50.0)
		self.assertEqual(position["shipping_amount"], 0.0)
		self.assertEqual(position["delivery_partner"], "Deliverk")
		self.assertEqual(position["is_partner_order"], 1)

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_a_partner_fee_already_reversed_is_gone(self, _find):
		rows = [self.PARTNER, dict(self.PARTNER, name="CT-R", partner_fee=-50.0)]
		self.assertIsNone(ir._courier_fee_position(_invoice(), rows))

	@patch.object(ir, "_find_existing_je_by_tag", return_value="ACC-JV-1")
	def test_no_rows_no_fee(self, _find):
		self.assertIsNone(ir._courier_fee_position(_invoice(), []))


@patch.object(ir, "get_delivery_partner_supplier", return_value="SUP-DELIVERK")
@patch.object(ir, "_get_partner_settlement_account", return_value="Deliverk - J")
@patch.object(ir, "get_freight_expense_account", return_value="Freight - J")
@patch.object(ir, "get_creditors_account", return_value="Creditors - J")
@patch.object(ir, "get_company_receivable_account", return_value="Debtors - J")
class TestPartnerFeeReversal(unittest.TestCase):
	"""A partner's fee stands through a return unless the trip never happened."""

	COURIER = {
		"party_type": "Supplier",
		"party": "RIDER-1",
		"delivery_partner": "Deliverk",
		"is_partner_order": 1,
		"shipping_amount": 0.0,
		"partner_fee": 50.0,
	}

	def _plan(self, *, pay_courier):
		return ir.plan_return_journal_legs(
			inv=_invoice(custom_is_partner_order=1),
			money_state=ir.MONEY_PREPAID,
			return_total=500.0,
			credit_note_name="ACC-SINV-2026-00099",
			pay_courier_for_trip=pay_courier,
			courier=self.COURIER,
		)

	def test_the_fee_stands_when_the_trip_happened(self, *_):
		self.assertEqual(self._plan(pay_courier=True), [])

	def test_the_fee_is_reversed_on_the_partners_own_payable(self, *_):
		plans = self._plan(pay_courier=False)
		self.assertEqual([p["je_type"] for p in plans], [ir.JE_PARTNER_REVERSAL])
		legs = plans[0]["legs"]
		payable = next(l for l in legs if l["account"] == "Deliverk - J")
		freight = next(l for l in legs if l["account"] == "Freight - J")
		self.assertTrue(payable["debit"])
		self.assertFalse(freight["debit"])
		self.assertEqual((payable["amount"], freight["amount"]), (50.0, 50.0))
		# The accrual credited the partner's Supplier, not the rider and not Creditors.
		self.assertEqual((payable["party_type"], payable["party"]), ("Supplier", "SUP-DELIVERK"))
		self.assertNotIn("Creditors - J", {l["account"] for l in legs})

	def test_a_stale_partner_flag_does_not_swallow_our_couriers_fee(self, *_):
		"""Sent with a partner, then re-dispatched to our own courier: the invoice
		keeps its partner flag for good, but the fee that stands is our courier's.
		The plan must reverse it on Creditors — and report it, so the courier's row
		is cut by exactly what the ledger took back."""
		own = {"party_type": "Supplier", "party": "SUP-1", "is_partner_order": 0, "shipping_amount": 80.0}
		plans = ir.plan_return_journal_legs(
			inv=_invoice(custom_is_partner_order=1),
			money_state=ir.MONEY_PREPAID,
			return_total=500.0,
			credit_note_name="ACC-SINV-2026-00099",
			pay_courier_for_trip=False,
			courier=own,
		)
		self.assertEqual([p["je_type"] for p in plans], [ir.JE_FREIGHT_REVERSAL])
		self.assertEqual(ir._planned_fee_reversals(plans), (80.0, 0.0))

	def test_the_row_mirrors_the_planned_partner_reversal(self, *_):
		self.assertEqual(ir._planned_fee_reversals(self._plan(pay_courier=False)), (0.0, 50.0))
		self.assertEqual(ir._planned_fee_reversals(self._plan(pay_courier=True)), (0.0, 0.0))

	def test_a_partner_row_without_a_fee_reverses_nothing(self, *_):
		courier = dict(self.COURIER, partner_fee=0.0)
		plans = ir.plan_return_journal_legs(
			inv=_invoice(custom_is_partner_order=1),
			money_state=ir.MONEY_PREPAID,
			return_total=500.0,
			credit_note_name="ACC-SINV-2026-00099",
			pay_courier_for_trip=False,
			courier=courier,
		)
		self.assertEqual(plans, [])


class TestCompensatingCourierRow(unittest.TestCase):
	"""The row that makes a fee reversal real — couriers are settled from rows."""

	def _insert(self, courier, **kwargs):
		ct = MagicMock()
		with patch.object(ir, "frappe") as mock_frappe:
			mock_frappe.new_doc.return_value = ct
			name = ir._compensating_courier_transaction(
				_invoice(), courier, credit_note_name="ACC-SINV-2026-00099", **kwargs
			)
		self.assertEqual(name, ct.name)
		ct.insert.assert_called_once()
		return ct

	def test_online_order_fee_only_row_stays_open_for_settlement(self):
		ct = self._insert(
			{"party_type": "Supplier", "party": "SUP-1"}, return_total=0.0, shipping_reversed=80.0
		)
		self.assertEqual(ct.amount, 0)
		self.assertEqual(ct.shipping_amount, -80.0)
		# +80 on the dispatch row, -80 here: the courier's balance nets to nothing.
		self.assertEqual(ct.status, "Unsettled")
		self.assertEqual(ct.is_partner_order, 0)

	def test_partner_fee_reversal_lands_on_the_weekly_bill(self):
		ct = self._insert(
			{"party_type": "Supplier", "party": "RIDER-1", "is_partner_order": 1, "delivery_partner": "Deliverk"},
			return_total=0.0,
			shipping_reversed=0.0,
			partner_fee_reversed=50.0,
		)
		self.assertEqual(ct.partner_fee, -50.0)
		self.assertEqual(ct.partner_settled, 0)
		self.assertEqual(ct.delivery_partner, "Deliverk")
		# No cash and no own-courier fee: nothing for the rider to settle.
		self.assertEqual(ct.status, "Settled")

	def test_partner_cash_release_keeps_the_fee_when_the_trip_happened(self):
		ct = self._insert(
			{"party_type": "Supplier", "party": "RIDER-1", "is_partner_order": 1, "delivery_partner": "Deliverk"},
			return_total=500.0,
			shipping_reversed=0.0,
		)
		self.assertEqual(ct.amount, -500.0)
		self.assertEqual(ct.partner_fee, 0)
		self.assertEqual(ct.partner_settled, 1)
		self.assertEqual(ct.status, "Unsettled")


class TestPostReturnJe(unittest.TestCase):
	"""The single posting door enforcing the v16 rules."""

	@patch.object(ir, "_find_existing_je_by_tag", return_value="JE-EXISTING")
	def test_reuses_an_existing_tagged_entry(self, _find):
		result = ir._post_return_je(
			company="JARZ",
			dedup_key="CN-1",
			je_type=ir.JE_AR_KNOCKOFF,
			human="x",
			legs=[{"account": "A", "amount": 10, "debit": True}],
		)
		self.assertEqual(result, "JE-EXISTING")

	@patch.object(ir, "_find_existing_je_by_tag", return_value=None)
	def test_immaterial_legs_post_nothing(self, _find):
		result = ir._post_return_je(
			company="JARZ",
			dedup_key="CN-1",
			je_type=ir.JE_AR_KNOCKOFF,
			human="x",
			legs=[{"account": "A", "amount": 0.0001, "debit": True}],
		)
		self.assertIsNone(result)

	@patch.object(ir, "validate_account_exists")
	@patch.object(ir, "_find_existing_je_by_tag", return_value=None)
	@patch.object(ir, "frappe")
	def test_party_account_without_a_party_is_refused(self, mock_frappe, _find, _validate):
		mock_frappe.new_doc.return_value = MagicMock()
		mock_frappe.db.get_value.return_value = "Receivable"
		mock_frappe.throw.side_effect = RuntimeError("no party")

		with self.assertRaises(RuntimeError):
			ir._post_return_je(
				company="JARZ",
				dedup_key="CN-1",
				je_type=ir.JE_AR_KNOCKOFF,
				human="x",
				legs=[{"account": "Debtors - J", "amount": 10, "debit": True}],
			)

	@patch.object(ir, "validate_account_exists")
	@patch.object(ir, "_find_existing_je_by_tag", return_value=None)
	@patch.object(ir, "frappe")
	def test_unbalanced_legs_are_refused(self, mock_frappe, _find, _validate):
		mock_frappe.new_doc.return_value = MagicMock()
		mock_frappe.db.get_value.return_value = "Income"
		mock_frappe.throw.side_effect = RuntimeError("unbalanced")

		with self.assertRaises(RuntimeError):
			ir._post_return_je(
				company="JARZ",
				dedup_key="CN-1",
				je_type=ir.JE_AR_KNOCKOFF,
				human="x",
				legs=[
					{"account": "A", "amount": 10, "debit": True},
					{"account": "B", "amount": 7, "debit": False},
				],
			)

	@patch.object(ir, "validate_account_exists")
	@patch.object(ir, "_find_existing_je_by_tag", return_value=None)
	@patch.object(ir, "frappe")
	def test_voucher_type_is_journal_entry_never_bank_entry(self, mock_frappe, _find, _validate):
		"""'Bank Entry' trips validate_cheque_info and fails deterministically."""
		je = MagicMock()
		mock_frappe.new_doc.return_value = je
		mock_frappe.db.get_value.return_value = "Income"

		ir._post_return_je(
			company="JARZ",
			dedup_key="CN-1",
			je_type=ir.JE_AR_KNOCKOFF,
			human="x",
			legs=[
				{"account": "A", "amount": 10, "debit": True},
				{"account": "B", "amount": 10, "debit": False},
			],
		)
		self.assertEqual(je.voucher_type, "Journal Entry")

	@patch.object(ir, "validate_account_exists")
	@patch.object(ir, "_find_existing_je_by_tag", return_value=None)
	@patch.object(ir, "frappe")
	def test_dedup_tag_keys_on_the_credit_note(self, mock_frappe, mock_find, _validate):
		"""A second partial return must not match the first return's entry."""
		je = MagicMock()
		mock_frappe.new_doc.return_value = je
		mock_frappe.db.get_value.return_value = "Income"

		ir._post_return_je(
			company="JARZ",
			dedup_key="CN-SECOND",
			je_type=ir.JE_AR_KNOCKOFF,
			human="x",
			legs=[
				{"account": "A", "amount": 10, "debit": True},
				{"account": "B", "amount": 10, "debit": False},
			],
		)
		mock_find.assert_called_with("JARZ", "CN-SECOND", ir.JE_AR_KNOCKOFF)
		self.assertIn("CN-SECOND", je.user_remark)

	@patch.object(ir, "validate_account_exists")
	@patch.object(ir, "_find_existing_je_by_tag", return_value=None)
	@patch.object(ir, "frappe")
	def test_negative_amount_flips_the_side(self, mock_frappe, _find, _validate):
		je = MagicMock()
		rows = []
		je.append.side_effect = lambda _table, row: rows.append(row)
		mock_frappe.new_doc.return_value = je
		mock_frappe.db.get_value.return_value = "Income"

		ir._post_return_je(
			company="JARZ",
			dedup_key="CN-1",
			je_type=ir.JE_AR_KNOCKOFF,
			human="x",
			legs=[
				{"account": "A", "amount": -10, "debit": True},
				{"account": "B", "amount": 10, "debit": True},
			],
		)
		# The negative debit becomes a credit, so the entry balances.
		self.assertEqual(rows[0]["credit_in_account_currency"], 10)
		self.assertEqual(rows[1]["debit_in_account_currency"], 10)


class TestReturnEligibility(unittest.TestCase):
	@patch.object(ir, "returns_enabled", return_value=False)
	def test_kill_switch_blocks_everything(self, _enabled):
		result = ir.get_invoice_return_eligibility(_invoice())
		self.assertFalse(result["can_return"])
		self.assertEqual(result["return_block_code"], "returns_disabled")

	@patch.object(ir, "_original_delivery_note", return_value="DN-1")
	@patch.object(ir, "returns_enabled", return_value=True)
	def test_dispatched_invoice_is_returnable(self, _enabled, _dn):
		self.assertTrue(ir.get_invoice_return_eligibility(_invoice())["can_return"])

	@patch.object(ir, "_original_delivery_note", return_value="DN-1")
	@patch.object(ir, "returns_enabled", return_value=True)
	def test_undispatched_invoice_is_not_returnable(self, _enabled, _dn):
		result = ir.get_invoice_return_eligibility(
			_invoice(custom_sales_invoice_state="In Progress", custom_was_out_for_delivery=0)
		)
		self.assertEqual(result["return_block_code"], "not_dispatched")

	@patch.object(ir, "_original_delivery_note", return_value="DN-1")
	@patch.object(ir, "returns_enabled", return_value=True)
	def test_a_credit_note_cannot_be_returned(self, _enabled, _dn):
		result = ir.get_invoice_return_eligibility(_invoice(is_return=1))
		self.assertEqual(result["return_block_code"], "return_invoice")

	@patch.object(ir, "_original_delivery_note", return_value="DN-1")
	@patch.object(ir, "returns_enabled", return_value=True)
	def test_fully_returned_invoice_is_blocked(self, _enabled, _dn):
		result = ir.get_invoice_return_eligibility(_invoice(custom_return_status="Fully Returned"))
		self.assertEqual(result["return_block_code"], "already_returned")

	@patch.object(ir, "_original_delivery_note", return_value=None)
	@patch.object(ir, "returns_enabled", return_value=True)
	def test_missing_delivery_note_is_an_explicit_blocker(self, _enabled, _dn):
		"""Legacy orders must fail up front, not halfway through the graph."""
		result = ir.get_invoice_return_eligibility(_invoice())
		self.assertEqual(result["return_block_code"], "original_delivery_note_missing")


class TestGoodsCondition(unittest.TestCase):
	"""Returned goods are sellable unless someone says they are not.

	Every return used to put the goods back as sellable stock; "Damaged" was a
	label and nothing more, so a spoiled jar came back into the freezer count.
	"""

	def test_goods_go_back_to_stock_by_default(self):
		for return_type in ("Customer Return", "Failed Delivery", "Wrong Item"):
			self.assertEqual(ir._resolve_goods_condition(None, return_type), ir.GOODS_RESTOCK)

	def test_a_damaged_return_is_waste(self):
		self.assertEqual(ir._resolve_goods_condition(None, "Damaged"), ir.GOODS_WASTE)
		self.assertEqual(ir._resolve_goods_condition("", "Damaged"), ir.GOODS_WASTE)

	def test_an_explicit_choice_beats_the_return_type(self):
		# Damaged box, jars fine: back on the shelf.
		self.assertEqual(ir._resolve_goods_condition("restock", "Damaged"), ir.GOODS_RESTOCK)
		# Delivery failed and the order melted on the way back.
		self.assertEqual(ir._resolve_goods_condition("waste", "Failed Delivery"), ir.GOODS_WASTE)
		self.assertEqual(ir._resolve_goods_condition("Write-Off", "Customer Return"), ir.GOODS_WASTE)

	def test_an_unknown_condition_is_not_guessed(self):
		self.assertIsNone(ir._resolve_goods_condition("compost", "Customer Return"))

	@patch.object(ir, "returns_enabled", return_value=True)
	def test_an_unknown_condition_is_refused_before_any_work(self, _enabled):
		result = ir.run_invoice_return(
			invoice_id="INV-1",
			lines=[{"si_detail": "r", "qty": 1}],
			reason="x",
			goods_condition="compost",
		)
		self.assertFalse(result["success"])
		self.assertIn("compost", result["error"])

	def test_returned_rows_are_merged_per_item_and_warehouse(self):
		ledger = [
			{"item_code": "JAR-A", "warehouse": "Dokki - J", "actual_qty": 1.0, "stock_uom": "Nos"},
			{"item_code": "JAR-A", "warehouse": "Dokki - J", "actual_qty": 2.0, "stock_uom": "Nos"},
			{"item_code": "JAR-B", "warehouse": "Dokki - J", "actual_qty": 1.0, "stock_uom": "Nos"},
		]
		with patch.object(ir, "frappe") as mock_frappe:
			mock_frappe.get_all.return_value = ledger
			rows = ir._returned_stock_rows("MAT-DN-RET-1")
		self.assertEqual(
			sorted((r["item_code"], r["warehouse"], r["qty"]) for r in rows),
			[("JAR-A", "Dokki - J", 3.0), ("JAR-B", "Dokki - J", 1.0)],
		)
		# Only what the return DN put IN: the filter must not pick up outgoing rows.
		filters = mock_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["voucher_no"], "MAT-DN-RET-1")
		self.assertEqual(filters["actual_qty"], [">", 0])

	def _write_off(self, rows, account="Stock Adjustment - J"):
		se = MagicMock()
		se.items = []
		se.append.side_effect = lambda _table, row: se.items.append(row)
		inv = _invoice(cost_center="Dokki CC - J")
		with (
			patch.object(ir, "_returned_stock_rows", return_value=rows),
			patch.object(ir, "_write_off_account", return_value=account),
			patch.object(ir, "frappe") as mock_frappe,
		):
			mock_frappe.new_doc.return_value = se
			mock_frappe.throw.side_effect = RuntimeError
			name = ir._write_off_returned_goods(
				inv, "MAT-DN-RET-1", credit_note_name="ACC-SINV-2026-00099", reason="melted"
			)
		return name, se

	def test_the_write_off_issues_exactly_what_came_back(self):
		rows = [{"item_code": "JAR-A", "warehouse": "Dokki - J", "qty": 3.0, "stock_uom": "Nos"}]
		name, se = self._write_off(rows)
		self.assertEqual(name, se.name)
		self.assertEqual(se.purpose, "Material Issue")
		self.assertEqual(len(se.items), 1)
		line = se.items[0]
		self.assertEqual((line["item_code"], line["qty"], line["s_warehouse"]), ("JAR-A", 3.0, "Dokki - J"))
		# The loss is booked to the Stock Adjustment Account, on the order's cost centre.
		self.assertEqual(line["expense_account"], "Stock Adjustment - J")
		self.assertEqual(line["cost_center"], "Dokki CC - J")
		# Findable again from either document.
		self.assertIn("ACC-SINV-2026-00001", se.remarks)
		self.assertIn("ACC-SINV-2026-00099", se.remarks)
		self.assertTrue(se.remarks.startswith(ir.WASTE_REMARKS_PREFIX))
		se.submit.assert_called_once()

	def test_a_return_that_moved_no_stock_writes_nothing_off(self):
		name, se = self._write_off([])
		self.assertIsNone(name)
		se.submit.assert_not_called()

	def test_no_adjustment_account_is_an_error_not_a_silent_restock(self):
		rows = [{"item_code": "JAR-A", "warehouse": "Dokki - J", "qty": 1.0, "stock_uom": "Nos"}]
		with self.assertRaises(RuntimeError):
			self._write_off(rows, account=None)


class TestSubmitValidation(unittest.TestCase):
	"""Argument validation happens before anything is written."""

	@patch.object(ir, "returns_enabled", return_value=True)
	def test_unknown_refund_mode_is_refused(self, _enabled):
		result = ir.run_invoice_return(
			invoice_id="INV-1",
			lines=[{"si_detail": "r", "qty": 1}],
			reason="x",
			refund_mode="teleport",
		)
		self.assertFalse(result["success"])

	@patch.object(ir, "returns_enabled", return_value=True)
	def test_unknown_return_type_is_refused(self, _enabled):
		result = ir.run_invoice_return(
			invoice_id="INV-1",
			lines=[{"si_detail": "r", "qty": 1}],
			reason="x",
			return_type="Vibes",
		)
		self.assertFalse(result["success"])

	@patch.object(ir, "returns_enabled", return_value=True)
	def test_reason_is_required(self, _enabled):
		result = ir.run_invoice_return(
			invoice_id="INV-1",
			lines=[{"si_detail": "r", "qty": 1}],
			reason="  ",
		)
		self.assertFalse(result["success"])

	@patch.object(ir, "returns_enabled", return_value=True)
	def test_at_least_one_line_is_required(self, _enabled):
		result = ir.run_invoice_return(invoice_id="INV-1", lines=[], reason="x")
		self.assertFalse(result["success"])

	@patch.object(ir, "returns_enabled", return_value=False)
	def test_kill_switch_refuses_before_any_work(self, _enabled):
		result = ir.run_invoice_return(
			invoice_id="INV-1",
			lines=[{"si_detail": "r", "qty": 1}],
			reason="x",
		)
		self.assertFalse(result["success"])


class TestEmployeeOrderReturns(unittest.TestCase):
	"""A PAID Employee order refunds from the till; it cannot be kept as credit.

	A customer-credit return on a prepaid order leaves the credit note at a
	negative outstanding on the staff Customer. ``get_employee_ledger`` counts
	``!= 0`` (the employee is shown as owed money) while monthly deductions only
	take ``> 0`` (salary never pays it back). Refund Now zeroes the note.
	"""

	def test_guard_truth_table(self):
		employee_paid = _invoice(custom_order_purpose="Employee", outstanding_amount=0.0)
		self.assertTrue(ir._employee_credit_return_blocked(employee_paid, ir.MONEY_PREPAID))

		# An unpaid (credit) Employee order posts the AR knock-off either way.
		employee_unpaid = _invoice(custom_order_purpose="Employee", outstanding_amount=500.0)
		self.assertFalse(ir._employee_credit_return_blocked(employee_unpaid, ir.MONEY_UNPAID))

		# Every other purpose keeps its customer-credit option.
		for purpose in (None, "Standard", "B2B Supply"):
			self.assertFalse(
				ir._employee_credit_return_blocked(_invoice(custom_order_purpose=purpose), ir.MONEY_PREPAID),
				repr(purpose),
			)

		# A zero-value order strands nothing.
		free = _invoice(custom_order_purpose="Employee", grand_total=0.0)
		self.assertFalse(ir._employee_credit_return_blocked(free, ir.MONEY_PREPAID))

	def _run(self, refund_mode):
		inv = _invoice(custom_order_purpose="Employee", outstanding_amount=0.0, grand_total=150.0)
		row = MagicMock()
		row.name = "row-1"
		row.qty = 1
		row.item_code = "JAR-M"
		inv.items = [row]

		with (
			patch.object(ir, "returns_enabled", return_value=True),
			patch.object(ir, "frappe") as mock_frappe,
			patch.object(ir, "ensure_profile_scoped_invoice_access"),
			patch.object(ir, "get_invoice_return_eligibility", return_value={"can_return": True}),
			patch.object(ir, "_returned_qty_by_row", return_value={}),
			patch.object(ir, "_money_state", return_value=ir.MONEY_PREPAID),
			patch.object(ir, "get_invoice_branch", return_value="Nasr City"),
			patch.object(ir, "_unsettled_courier_transactions", return_value=[]),
			patch.object(ir, "ensure_open_shift_for_invoice") as shift,
			patch.object(ir, "_original_delivery_note", return_value="DN-1"),
			patch.object(ir, "_build_return_delivery_note", side_effect=RuntimeError("stop here")),
		):
			mock_frappe.get_all.return_value = []
			mock_frappe.get_doc.return_value = inv
			mock_frappe.db.sql.return_value = [[1]]
			result = ir.run_invoice_return(
				invoice_id=inv.name,
				lines=[{"si_detail": "row-1", "qty": 1}],
				reason="Customer changed mind",
				refund_mode=refund_mode,
			)
		return result, mock_frappe, shift

	def test_customer_credit_is_refused_before_anything_is_posted(self):
		result, mock_frappe, shift = self._run(ir.REFUND_CUSTOMER_CREDIT)

		self.assertFalse(result["success"])
		self.assertEqual(result["return_block_code"], "employee_credit_not_supported")
		self.assertIn("Refund Now", result["error"])
		mock_frappe.db.savepoint.assert_not_called()
		shift.assert_not_called()

	def test_refund_now_passes_the_guard_and_needs_a_shift(self):
		result, mock_frappe, shift = self._run(ir.REFUND_NOW)

		self.assertNotEqual(result.get("return_block_code"), "employee_credit_not_supported")
		shift.assert_called_once()
		# It went on to build the return (stopped by the test's own side effect).
		mock_frappe.db.savepoint.assert_called_once()


class TestRefundSourceAccount(unittest.TestCase):
	"""Refund now pays out of the ledger that received the money."""

	def _resolve(self, method, *, received=(), accounts=None, mop_account=None):
		accounts = accounts or {}

		def get_value(doctype, filters, fieldname=None, as_dict=False):
			if doctype == "Mode of Payment Account":
				return mop_account
			row = accounts.get(filters)
			if fieldname == "account_type":
				return (row or {}).get("account_type")
			return row

		inv = _invoice(custom_payment_method=method)
		with (
			patch.object(ir, "get_pos_cash_account", return_value="Cash - 6th"),
			patch.object(ir, "_get_courier_outstanding_account", return_value="Courier Outstanding - J"),
			patch("jarz_pos.api.kanban._get_actual_payment_method_map", return_value={inv.name: method}),
			patch.object(ir.frappe.db, "sql", return_value=received),
			patch.object(ir.frappe.db, "get_value", side_effect=get_value),
		):
			return ir.resolve_refund_source_account(inv, "6th of october")

	BANK = {"is_group": 0, "account_type": "Bank", "root_type": "Asset", "company": "JARZ"}
	KASHIER = {"is_group": 0, "account_type": "", "root_type": "Asset", "company": "JARZ"}
	COURIER = {"is_group": 0, "account_type": "", "root_type": "Asset", "company": "JARZ"}

	def test_kashier_card_uses_the_untyped_kashier_ledger(self):
		got = self._resolve(
			"Kashier Card", received=(("kashier - J",),), accounts={"kashier - J": self.KASHIER}
		)
		self.assertEqual(got, ("kashier - J", False))

	def test_cod_order_with_instapay_deposit_refunds_from_the_bank(self):
		got = self._resolve(
			"Cash", received=(("Bank Account - J",),), accounts={"Bank Account - J": self.BANK}
		)
		self.assertEqual(got, ("Bank Account - J", False))

	def test_cash_order_paid_into_the_till_stays_on_branch_cash(self):
		till = {"is_group": 0, "account_type": "Cash", "root_type": "Asset", "company": "JARZ"}
		got = self._resolve("Cash", received=(("Cash - 6th",),), accounts={"Cash - 6th": till})
		self.assertEqual(got, ("Cash - 6th", True))

	def test_courier_outstanding_receipt_is_skipped_for_mop_default(self):
		# Production 19874-1: InstaPay order whose Payment Entry landed in Courier
		# Outstanding; the refund must come from the InstaPay bank, not the till.
		got = self._resolve(
			"Instapay",
			received=(("Courier Outstanding - J",),),
			mop_account="Bank Account - J",
			accounts={"Courier Outstanding - J": self.COURIER, "Bank Account - J": self.BANK},
		)
		self.assertEqual(got, ("Bank Account - J", False))

	def test_cash_and_empty_method_use_branch_cash(self):
		self.assertEqual(self._resolve("Cash"), ("Cash - 6th", True))
		self.assertEqual(self._resolve("Cash on Delivery"), ("Cash - 6th", True))
		self.assertEqual(self._resolve(""), ("Cash - 6th", True))

	def test_instapay_uses_the_bank_that_received_the_money(self):
		got = self._resolve(
			"Instapay", received=(("Bank Account - J",),), accounts={"Bank Account - J": self.BANK}
		)
		self.assertEqual(got, ("Bank Account - J", False))

	def test_mode_of_payment_default_when_no_payment_entry(self):
		got = self._resolve(
			"Instapay", mop_account="Bank Account - J", accounts={"Bank Account - J": self.BANK}
		)
		self.assertEqual(got, ("Bank Account - J", False))

	def test_other_branch_cash_account_is_refused(self):
		till = {"is_group": 0, "account_type": "Cash", "root_type": "Asset", "company": "JARZ"}
		got = self._resolve("Instapay", mop_account="Cash - Maadi", accounts={"Cash - Maadi": till})
		self.assertEqual(got, ("Cash - 6th", True))

	def test_group_ledger_falls_back_to_cash(self):
		grp = {"is_group": 1, "account_type": "Bank", "root_type": "Asset", "company": "JARZ"}
		got = self._resolve("Instapay", mop_account="Banks - J", accounts={"Banks - J": grp})
		self.assertEqual(got, ("Cash - 6th", True))

	def test_no_mapping_falls_back_to_cash(self):
		self.assertEqual(self._resolve("Instapay"), ("Cash - 6th", True))


class TestReturnedCancellationLabel(unittest.TestCase):
	"""A full return labels the original as cancelled (owner decision 2026-11-17);
	a partial one does not, and a failed label write never fails the return."""

	def _stamp(self, *, fully):
		inv = _invoice()
		with (
			patch.object(ir, "_completes_the_order", return_value=fully),
			patch.object(ir, "_returned_column_updates", return_value={}),
			patch.object(ir, "update_submitted_sales_invoice_fields"),
			patch.object(ir, "frappe") as mock_frappe,
		):
			status = ir._stamp_return_state(
				inv,
				requested={"row-1": 1.0},
				returned_before={},
				reason="Customer refused",
				notes=None,
				return_total=-500.0,
			)
		return status, mock_frappe

	def test_full_return_is_labelled_returned_cancelled(self):
		status, mock_frappe = self._stamp(fully=True)

		self.assertEqual(status, "Fully Returned")
		mock_frappe.db.set_value.assert_called_once()
		_, name, values = mock_frappe.db.set_value.call_args.args
		self.assertEqual(name, "ACC-SINV-2026-00001")
		self.assertEqual(values["custom_cancellation_type"], "Returned - Cancelled")
		self.assertIn("Customer refused", values["custom_cancellation_reason"])

	def test_partial_return_is_not_labelled(self):
		status, mock_frappe = self._stamp(fully=False)

		self.assertEqual(status, "Partially Returned")
		mock_frappe.db.set_value.assert_not_called()

	def test_a_failed_label_write_does_not_fail_the_return(self):
		inv = _invoice()
		with patch.object(ir, "frappe") as mock_frappe:
			mock_frappe.db.set_value.side_effect = RuntimeError("db down")
			ir._stamp_returned_cancellation_label(inv, "x")  # must not raise
		mock_frappe.log_error.assert_called_once()


if __name__ == "__main__":
	unittest.main()

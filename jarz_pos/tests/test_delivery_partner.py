"""Tests for the Delivery Partner feature.

The rule these all pin down, in one sentence: a delivery partner's rider is a pure
carrier, treated exactly like our own courier on the cash side, except that nothing
is ever deducted for his fee. He hands the branch the FULL amount he collected (cash
orders) or carries nothing at all (prepaid). What his company charges for the trip is
a separate debt, accrued at dispatch and paid by one weekly bank transfer.

Two consequences are load-bearing and are asserted repeatedly below:

  * ``Courier Transaction.shipping_amount`` is ZERO on every partner row — that
    column means "what the rider is owed out of the cash he carries". The fee lives
    on ``partner_fee``. This is what lets partner rows travel through the ordinary
    courier cash machinery with no special case, so every ``amount - shipping``
    downstream naturally yields the full amount the branch collects.
  * The partner's ledger is one-directional. It is only ever CREDITED at dispatch and
    DEBITED when we pay them. There is no "the partner owes us" case any more.

The last class is a regression guard: ordinary courier orders must be untouched by
all of this, which is the whole point of keeping the fee on its own column.
"""

import unittest
from unittest.mock import patch, MagicMock
import frappe


class TestResolveDeliveryPartner(unittest.TestCase):
	"""Test _resolve_delivery_partner helper."""

	def test_returns_none_when_no_party(self):
		from jarz_pos.services.settlement_strategies import _resolve_delivery_partner
		self.assertIsNone(_resolve_delivery_partner(None, None))
		self.assertIsNone(_resolve_delivery_partner("Employee", None))
		self.assertIsNone(_resolve_delivery_partner(None, "EMP-001"))

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_returns_partner_when_linked(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _resolve_delivery_partner
		mock_frappe.db.get_value.return_value = "Partner A"
		result = _resolve_delivery_partner("Employee", "EMP-001")
		self.assertEqual(result, "Partner A")
		mock_frappe.db.get_value.assert_called_once_with("Employee", "EMP-001", "custom_delivery_partner")

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_returns_none_when_no_link(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _resolve_delivery_partner
		mock_frappe.db.get_value.return_value = None
		self.assertIsNone(_resolve_delivery_partner("Supplier", "SUP-001"))

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_returns_none_on_exception(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _resolve_delivery_partner
		mock_frappe.db.get_value.side_effect = Exception("Field not found")
		self.assertIsNone(_resolve_delivery_partner("Employee", "EMP-X"))


class TestPartnerDispatch(unittest.TestCase):
	"""dispatch_settlement routes partner orders to the partner strategies."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_dispatch_routes_to_partner_when_linked(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import dispatch_settlement, PARTNER_STRATEGY

		mock_inv = MagicMock()
		mock_inv.name = "INV-P001"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 100.0
		mock_inv.company = "Test Co"
		mock_inv.get.return_value = "Cash"
		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: {
			("Employee", "EMP-001", "custom_delivery_partner"): "Partner A",
			("Sales Invoice", "INV-P001", "outstanding_amount"): 100.0,
		}.get(a[:3], None)

		mock_handler = MagicMock(return_value={"success": True, "mode": "partner_unpaid_settle_now"})
		original = PARTNER_STRATEGY[("unpaid", "now")]
		PARTNER_STRATEGY[("unpaid", "now")] = mock_handler
		try:
			result = dispatch_settlement(
				"INV-P001", mode="now", pos_profile="POS-001",
				party_type="Employee", party="EMP-001", partner_fee=55.0,
			)
			mock_handler.assert_called_once()
			# The typed fee has to survive the hop into the handler, or the handler
			# refuses the dispatch.
			self.assertEqual(mock_handler.call_args.kwargs["partner_fee"], 55.0)
			self.assertTrue(result["success"])
		finally:
			PARTNER_STRATEGY[("unpaid", "now")] = original

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_dispatch_routes_to_normal_when_no_partner(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		mock_inv = MagicMock()
		mock_inv.name = "INV-N001"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 100.0
		mock_inv.get.return_value = "Cash"
		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: {
			("Employee", "EMP-002", "custom_delivery_partner"): None,
			("Sales Invoice", "INV-N001", "outstanding_amount"): 100.0,
		}.get(a[:3], None)

		with patch("jarz_pos.services.settlement_strategies.handle_unpaid_settle_now") as mock_handler:
			mock_handler.return_value = {"success": True, "mode": "unpaid_settle_now"}
			result = dispatch_settlement(
				"INV-N001", mode="now", pos_profile="POS-001",
				party_type="Employee", party="EMP-002",
			)
			mock_handler.assert_called_once()
			self.assertEqual(result["mode"], "unpaid_settle_now")
			# An ordinary dispatch must not acquire a partner_fee argument.
			self.assertNotIn("partner_fee", mock_handler.call_args.kwargs)

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_unpaid_instapay_partner_order_stays_unpaid(self, mock_frappe):
		"""An unpaid InstaPay order is unpaid whoever carries it.

		This used to exclude partner orders, which sent them down a cash path and
		recorded money nobody had collected. The rider carries nothing either way.
		"""
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		mock_inv = MagicMock()
		mock_inv.name = "INV-IP1"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 200.0
		mock_inv.get.side_effect = lambda k, *a: "InstaPay" if k == "custom_payment_method" else None
		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: {
			("Employee", "EMP-001", "custom_delivery_partner"): "Partner A",
			("Sales Invoice", "INV-IP1", "outstanding_amount"): 200.0,
		}.get(a[:3], None)

		with patch(
			"jarz_pos.services.settlement_strategies.handle_unpaid_online_deliver_unconfirmed"
		) as mock_online:
			mock_online.return_value = {"success": True, "mode": "unpaid_online_deliver_unconfirmed"}
			result = dispatch_settlement(
				"INV-IP1", mode="now", pos_profile="POS-001",
				party_type="Employee", party="EMP-001", partner_fee=40.0,
			)
			mock_online.assert_called_once()
			self.assertEqual(mock_online.call_args.kwargs["partner_fee"], 40.0)
			self.assertEqual(result["mode"], "unpaid_online_deliver_unconfirmed")


class TestPartnerFeeIsMandatory(unittest.TestCase):
	"""The partner's price is entered per order; our area rate is never a fallback.

	A partner subdivides one of our areas into dozens of its own zones, so our rate
	is not merely imprecise for them — it is a different number for a different map.
	Guessing it would book a wrong cost on essentially every partner order, silently.
	"""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_missing_fee_is_refused(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _require_partner_fee
		mock_frappe.throw.side_effect = Exception("thrown")
		inv = MagicMock()
		inv.name = "INV-1"
		with self.assertRaises(Exception):
			_require_partner_fee(inv, "Partner A", None)
		mock_frappe.throw.assert_called_once()
		self.assertIn("Partner A", mock_frappe.throw.call_args.args[0])

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_empty_string_is_refused(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _require_partner_fee
		mock_frappe.throw.side_effect = Exception("thrown")
		inv = MagicMock()
		inv.name = "INV-1"
		with self.assertRaises(Exception):
			_require_partner_fee(inv, "Partner A", "   ")

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_negative_fee_is_refused(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _require_partner_fee
		mock_frappe.throw.side_effect = Exception("thrown")
		inv = MagicMock()
		inv.name = "INV-1"
		with self.assertRaises(Exception):
			_require_partner_fee(inv, "Partner A", -5)

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_accepted_fee_is_persisted_for_reporting(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _require_partner_fee
		inv = MagicMock()
		inv.name = "INV-1"
		self.assertEqual(_require_partner_fee(inv, "Partner A", "55.4"), 55.4)
		# Written to the invoice so the delivery-cost reports show what we actually
		# paid rather than our own area rate.
		mock_frappe.db.set_value.assert_called_once_with(
			"Sales Invoice", "INV-1", "custom_shipping_expense", 55.4, update_modified=False
		)

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_zero_fee_is_allowed(self, mock_frappe):
		"""Zero is a real answer (a free trip); only *absent* is refused."""
		from jarz_pos.services.settlement_strategies import _require_partner_fee
		inv = MagicMock()
		inv.name = "INV-1"
		self.assertEqual(_require_partner_fee(inv, "Partner A", 0), 0.0)
		mock_frappe.throw.assert_not_called()


class TestPartnerCourierTransactionShape(unittest.TestCase):
	"""Every partner row keeps the fee off the cash column."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_shipping_amount_is_always_zero(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _create_partner_courier_transaction
		captured = {}

		def _get_doc(payload):
			captured.update(payload)
			doc = MagicMock()
			doc.name = "CT-1"
			return doc

		mock_frappe.get_doc.side_effect = _get_doc
		inv = MagicMock()
		inv.name = "INV-1"

		_create_partner_courier_transaction(
			inv, party_type="Employee", party="EMP-1", delivery_partner="Partner A",
			order_amount=850.0, partner_fee=55.0, status="Unsettled",
		)

		self.assertEqual(captured["amount"], 850.0)
		# The rider is owed nothing out of the 850 he is carrying.
		self.assertEqual(captured["shipping_amount"], 0.0)
		self.assertEqual(captured["partner_fee"], 55.0)
		self.assertEqual(captured["is_partner_order"], 1)
		self.assertEqual(captured["delivery_partner"], "Partner A")


class TestPartnerCashSettleNow(unittest.TestCase):
	"""Cash order, rider hands the money over at the counter."""

	@patch("jarz_pos.services.settlement_strategies.validate_account_exists")
	@patch("jarz_pos.services.settlement_strategies.get_pos_cash_account")
	@patch("jarz_pos.services.settlement_strategies._get_receivable_account")
	@patch("jarz_pos.services.settlement_strategies._create_payment_entry")
	@patch("jarz_pos.services.settlement_strategies.create_partner_fee_accrual_je")
	@patch("jarz_pos.services.settlement_strategies.ensure_delivery_note_for_invoice")
	@patch("jarz_pos.services.settlement_strategies._create_partner_courier_transaction")
	@patch("jarz_pos.services.settlement_strategies._stamp_partner_fields")
	@patch("jarz_pos.services.settlement_strategies.update_submitted_sales_invoice_state")
	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_full_amount_reaches_the_branch_drawer(
		self, mock_frappe, mock_state, mock_stamp, mock_ct, mock_dn, mock_fee_je,
		mock_pe, mock_recv, mock_cash, mock_validate,
	):
		from jarz_pos.services.settlement_strategies import handle_partner_unpaid_settle_now

		inv = MagicMock()
		inv.name = "INV-PU1"
		inv.company = "Test Co"
		inv.grand_total = 850.0
		mock_frappe.db.get_value.return_value = 850.0
		mock_dn.return_value = {"delivery_note": "DN-001"}
		mock_fee_je.return_value = "JE-FEE-1"
		mock_recv.return_value = "Debtors - T"
		mock_cash.return_value = "Cash - T"
		pe = MagicMock()
		pe.name = "PE-1"
		mock_pe.return_value = pe
		mock_ct.return_value = "CT-1"

		result = handle_partner_unpaid_settle_now(
			inv, pos_profile="POS-001", payment_type="Cash",
			party_type="Employee", party="EMP-001",
			delivery_partner="Partner A", partner_fee=55.0,
		)

		# The customer's money goes into the BRANCH drawer, in full — not onto a
		# bank ledger, and not net of the fee. This is the defect the whole change
		# exists to fix: the old model booked 795 to a bank account and never
		# touched the till the cash was physically in.
		mock_pe.assert_called_once()
		pe_args = mock_pe.call_args.args
		self.assertEqual(pe_args[2], "Cash - T")
		self.assertEqual(pe_args[3], 850.0)

		# The fee is a separate debt to the partner company.
		mock_fee_je.assert_called_once()
		self.assertEqual(mock_fee_je.call_args.kwargs["fee"], 55.0)
		self.assertEqual(mock_fee_je.call_args.kwargs["delivery_partner"], "Partner A")

		ct_kwargs = mock_ct.call_args.kwargs
		self.assertEqual(ct_kwargs["order_amount"], 850.0)
		self.assertEqual(ct_kwargs["partner_fee"], 55.0)
		self.assertEqual(ct_kwargs["status"], "Settled")

		self.assertEqual(result["shipping_amount"], 0.0)
		self.assertEqual(result["partner_fee"], 55.0)
		self.assertEqual(result["amount_collected"], 850.0)
		self.assertEqual(result["payment_entry"], "PE-1")
		# Nothing is paid to the partner at dispatch — that is the weekly transfer.
		self.assertNotIn("settlement_journal_entry", result)
		mock_stamp.assert_called_once_with("INV-PU1", "Partner A")
		mock_state.assert_called_once_with(inv, "Out for Delivery")

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_refuses_without_a_fee(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import handle_partner_unpaid_settle_now
		mock_frappe.throw.side_effect = Exception("thrown")
		inv = MagicMock()
		inv.name = "INV-X"
		with self.assertRaises(Exception):
			handle_partner_unpaid_settle_now(
				inv, pos_profile="POS-001", payment_type="Cash",
				party_type="Employee", party="EMP-001",
				delivery_partner="Partner A", partner_fee=None,
			)


class TestPartnerCashSettleLater(unittest.TestCase):
	"""Cash order, money comes back later — the ordinary courier path, verbatim."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	@patch("jarz_pos.services.settlement_strategies._stamp_partner_fields")
	@patch("jarz_pos.services.settlement_strategies.mark_courier_outstanding")
	def test_delegates_with_the_typed_fee(self, mock_mco, mock_stamp, mock_frappe):
		from jarz_pos.services.settlement_strategies import handle_partner_unpaid_settle_later

		inv = MagicMock()
		inv.name = "INV-PUL1"
		mock_mco.return_value = {"courier_transaction": "CT-2", "amount": 300.0}

		result = handle_partner_unpaid_settle_later(
			inv, pos_profile="POS-001", payment_type="Cash",
			party_type="Employee", party="EMP-001",
			delivery_partner="Partner A", partner_fee=30.0,
		)

		# Same function an ordinary courier uses. The fee rides in as the explicit
		# override, which is the ONLY source the service accepts for a partner.
		mock_mco.assert_called_once_with(
			"INV-PUL1", courier=None, party_type="Employee", party="EMP-001",
			shipping_override=30.0,
		)
		self.assertTrue(result["is_partner_order"])
		self.assertEqual(result["mode"], "partner_unpaid_settle_later")


class TestPartnerPrepaid(unittest.TestCase):
	"""Prepaid order — the rider carries no money and is given none."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	@patch("jarz_pos.services.settlement_strategies.update_submitted_sales_invoice_state")
	@patch("jarz_pos.services.settlement_strategies._stamp_partner_fields")
	@patch("jarz_pos.services.settlement_strategies._create_partner_courier_transaction")
	@patch("jarz_pos.services.settlement_strategies.ensure_delivery_note_for_invoice")
	@patch("jarz_pos.services.settlement_strategies.create_partner_fee_accrual_je")
	def _run(self, handler, mock_fee_je, mock_dn, mock_ct, mock_stamp, mock_state, mock_frappe):
		inv = MagicMock()
		inv.name = "INV-PP1"
		inv.company = "Test Co"
		inv.grand_total = 700.0
		mock_dn.return_value = {"delivery_note": "DN-003"}
		mock_fee_je.return_value = "JE-FEE-2"
		mock_ct.return_value = "CT-3"

		result = handler(
			inv, pos_profile="POS-001", payment_type="Online",
			party_type="Supplier", party="SUP-001",
			delivery_partner="Partner B", partner_fee=70.0,
		)
		return result, mock_ct, mock_fee_je

	def test_settle_now_has_no_cash_position(self):
		from jarz_pos.services.settlement_strategies import handle_partner_paid_settle_now
		result, mock_ct, mock_fee_je = self._run(handle_partner_paid_settle_now)

		ct_kwargs = mock_ct.call_args.kwargs
		# No money in either direction, so the row is born Settled and never shows
		# up in courier balances or at shift close.
		self.assertEqual(ct_kwargs["order_amount"], 0)
		self.assertEqual(ct_kwargs["partner_fee"], 70.0)
		self.assertEqual(ct_kwargs["status"], "Settled")
		self.assertEqual(result["amount_collected"], 0.0)
		self.assertEqual(result["shipping_amount"], 0.0)
		mock_fee_je.assert_called_once()

	def test_settle_later_is_identical(self):
		"""There is no cash whose timing could differ, so now and later agree."""
		from jarz_pos.services.settlement_strategies import handle_partner_paid_settle_later
		result, mock_ct, _ = self._run(handle_partner_paid_settle_later)
		self.assertEqual(mock_ct.call_args.kwargs["order_amount"], 0)
		self.assertEqual(mock_ct.call_args.kwargs["status"], "Settled")
		self.assertEqual(result["mode"], "partner_paid_settle_later")


class TestPartnerStrategyDict(unittest.TestCase):
	def test_all_four_combinations_registered(self):
		from jarz_pos.services.settlement_strategies import PARTNER_STRATEGY
		for key in (("unpaid", "now"), ("unpaid", "later"), ("paid", "now"), ("paid", "later")):
			self.assertIn(key, PARTNER_STRATEGY)
			self.assertTrue(callable(PARTNER_STRATEGY[key]))


class TestDeliveryPartnerBillingAPI(unittest.TestCase):
	"""The weekly run: what we owe, reconciled against the partner's own invoice."""

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_balances_read_the_fee_column(self, mock_frappe):
		from jarz_pos.api.delivery_partners import get_delivery_partner_balances
		mock_frappe.db.sql.return_value = [
			{"delivery_partner": "Partner A", "partner_name": "Partner A",
			 "order_count": 3, "total_fee": 165.0, "oldest_date": "2026-08-25"},
		]
		rows = get_delivery_partner_balances()
		self.assertEqual(rows[0]["total_fee"], 165.0)
		# Older callers read these names; they must keep working.
		self.assertEqual(rows[0]["total_shipping_fee"], 165.0)
		self.assertEqual(rows[0]["unsettled_count"], 3)

		sql = mock_frappe.db.sql.call_args.args[0]
		# Billing is gated on partner_settled, NOT on the rider's cash status — a
		# prepaid trip is born Settled yet is still owed to the partner.
		self.assertIn("partner_settled", sql)
		self.assertIn("partner_fee", sql)

	@patch("jarz_pos.api.delivery_partners.create_partner_settlement_je")
	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_settles_only_the_ticked_trips(self, mock_frappe, mock_je):
		from jarz_pos.api.delivery_partners import settle_delivery_partner

		dp = MagicMock()
		dp.settlement_account = "Partner A - J"
		dp.bank_account = None
		dp.partner_name = "Partner A"
		mock_frappe.get_doc.return_value = dp
		mock_frappe.get_all.return_value = [
			{"name": "CT-1", "partner_fee": 55.0, "reference_invoice": "INV-1"},
			{"name": "CT-2", "partner_fee": 45.0, "reference_invoice": "INV-2"},
		]
		mock_frappe.db.get_value.return_value = "Test Co"
		mock_je.return_value = "JE-SETTLE-1"

		res = settle_delivery_partner(
			"Partner A", bank_account="Bank - J", courier_transactions=["CT-1", "CT-2"],
		)

		self.assertEqual(res["order_count"], 2)
		self.assertEqual(res["total_fee"], 100.0)
		self.assertEqual(res["journal_entry"], "JE-SETTLE-1")
		# Only the partner-billing fields move; the rider's cash status is a
		# different question and must not be touched here.
		for call in mock_frappe.db.set_value.call_args_list:
			payload = call.args[2]
			self.assertEqual(set(payload), {"partner_settled", "partner_settlement_je", "partner_settled_on"})

	@patch("jarz_pos.api.delivery_partners.create_partner_settlement_je")
	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_fixed_charges_are_added_to_the_payment(self, mock_frappe, mock_je):
		from jarz_pos.api.delivery_partners import settle_delivery_partner

		dp = MagicMock()
		dp.settlement_account = "Partner A - J"
		dp.bank_account = None
		dp.partner_name = "Partner A"
		mock_frappe.get_doc.return_value = dp
		mock_frappe.get_all.return_value = [
			{"name": "CT-1", "partner_fee": 55.0, "reference_invoice": "INV-1"},
		]
		mock_frappe.db.get_value.return_value = "Test Co"
		mock_je.return_value = "JE-SETTLE-2"

		res = settle_delivery_partner(
			"Partner A", bank_account="Bank - J",
			extra_charges=[{"description": "Monthly subscription", "amount": 500.0}],
		)

		self.assertEqual(res["total_fee"], 55.0)
		self.assertEqual(res["extra_charges_total"], 500.0)
		self.assertEqual(res["total_paid"], 555.0)
		self.assertEqual(mock_je.call_args.kwargs["order_fee_total"], 55.0)
		self.assertEqual(len(mock_je.call_args.kwargs["extra_charges"]), 1)

	@patch("jarz_pos.api.delivery_partners.create_partner_settlement_je")
	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_fixed_charge_alone_can_be_paid(self, mock_frappe, mock_je):
		"""A week with no trips can still carry a subscription line."""
		from jarz_pos.api.delivery_partners import settle_delivery_partner

		dp = MagicMock()
		dp.settlement_account = "Partner A - J"
		dp.bank_account = None
		dp.partner_name = "Partner A"
		mock_frappe.get_doc.return_value = dp
		mock_frappe.get_all.return_value = []
		mock_frappe.db.get_value.return_value = "Test Co"
		mock_je.return_value = "JE-SETTLE-3"

		res = settle_delivery_partner(
			"Partner A", bank_account="Bank - J",
			extra_charges=[{"description": "Waiting time", "amount": 120.0}],
		)
		self.assertEqual(res["total_paid"], 120.0)
		self.assertEqual(res["order_count"], 0)

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_nothing_to_settle_is_not_an_error(self, mock_frappe):
		from jarz_pos.api.delivery_partners import settle_delivery_partner
		dp = MagicMock()
		dp.settlement_account = "Partner A - J"
		mock_frappe.get_doc.return_value = dp
		mock_frappe.get_all.return_value = []
		res = settle_delivery_partner("Partner A")
		self.assertTrue(res["success"])
		self.assertEqual(res["order_count"], 0)

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_unknown_transaction_is_refused_not_skipped(self, mock_frappe):
		"""Silently billing fewer trips than were ticked defeats the reconciliation."""
		from jarz_pos.api.delivery_partners import settle_delivery_partner
		dp = MagicMock()
		dp.settlement_account = "Partner A - J"
		mock_frappe.get_doc.return_value = dp
		mock_frappe.get_all.return_value = [
			{"name": "CT-1", "partner_fee": 55.0, "reference_invoice": "INV-1"},
		]
		mock_frappe.throw.side_effect = Exception("thrown")
		with self.assertRaises(Exception):
			settle_delivery_partner("Partner A", courier_transactions=["CT-1", "CT-DOES-NOT-EXIST"])


class TestOrdinaryCourierIsUnaffected(unittest.TestCase):
	"""Regression guard: none of the above may change a normal delivery.

	The fee living on its own column is precisely what buys this. If someone ever
	moves the partner fee back onto ``shipping_amount``, these fail.
	"""

	def test_summary_still_deducts_an_ordinary_courier_fee(self):
		from jarz_pos.services.delivery_handling import _summarize_courier_transactions
		rows = [
			{"amount": 850.0, "shipping_amount": 55.0},
			{"amount": 300.0, "shipping_amount": 30.0},
		]
		totals = _summarize_courier_transactions(rows)
		self.assertEqual(totals["order_amount"], 1150.0)
		self.assertEqual(totals["shipping_amount"], 85.0)
		self.assertEqual(totals["net_to_branch"], 1065.0)

	def test_partner_row_contributes_its_full_amount(self):
		from jarz_pos.services.delivery_handling import _summarize_courier_transactions
		rows = [{"amount": 850.0, "shipping_amount": 0.0, "partner_fee": 55.0, "is_partner_order": 1}]
		totals = _summarize_courier_transactions(rows)
		# Nothing is withheld from a partner rider, so the branch collects all 850.
		self.assertEqual(totals["net_to_branch"], 850.0)
		self.assertEqual(totals["shipping_amount"], 0.0)

	def test_mixed_batch_nets_correctly(self):
		"""One rider of each kind settling in the same batch."""
		from jarz_pos.services.delivery_handling import _summarize_courier_transactions
		rows = [
			{"amount": 850.0, "shipping_amount": 0.0, "partner_fee": 55.0, "is_partner_order": 1},
			{"amount": 400.0, "shipping_amount": 40.0},
		]
		totals = _summarize_courier_transactions(rows)
		self.assertEqual(totals["net_to_branch"], 850.0 + 360.0)


if __name__ == "__main__":
	unittest.main()

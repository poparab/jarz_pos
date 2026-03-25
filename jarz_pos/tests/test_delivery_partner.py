"""Tests for the Delivery Partner feature.

Tests cover:
  - Delivery Partner doctype validation
  - Partner resolution helper (_resolve_delivery_partner)
  - Partner settlement strategy dispatch
  - Partner strategy handlers (all 4 combinations)
  - Partner bank settlement API (delivery_partners.py)
  - Partner fields stamping on Sales Invoice
"""

import unittest
from unittest.mock import patch, MagicMock, call
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
		result = _resolve_delivery_partner("Supplier", "SUP-001")
		self.assertIsNone(result)

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_returns_none_on_exception(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import _resolve_delivery_partner
		mock_frappe.db.get_value.side_effect = Exception("Field not found")
		result = _resolve_delivery_partner("Employee", "EMP-X")
		self.assertIsNone(result)


class TestPartnerDispatch(unittest.TestCase):
	"""Test dispatch_settlement routes to partner strategies."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_dispatch_routes_to_partner_when_linked(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		mock_inv = MagicMock()
		mock_inv.name = "INV-P001"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 100.0
		mock_inv.company = "Test Co"
		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: {
			("Employee", "EMP-001", "custom_delivery_partner"): "Partner A",
			("Sales Invoice", "INV-P001", "outstanding_amount"): 100.0,
		}.get(a[:3], None)

		with patch("jarz_pos.services.settlement_strategies.handle_partner_unpaid_settle_now") as mock_handler:
			mock_handler.return_value = {"success": True, "mode": "partner_unpaid_settle_now"}
			result = dispatch_settlement(
				"INV-P001",
				mode="now",
				pos_profile="POS-001",
				party_type="Employee",
				party="EMP-001",
			)
			mock_handler.assert_called_once()
			self.assertTrue(result["success"])
			self.assertEqual(result["mode"], "partner_unpaid_settle_now")

	@patch("jarz_pos.services.settlement_strategies.frappe")
	def test_dispatch_routes_to_normal_when_no_partner(self, mock_frappe):
		from jarz_pos.services.settlement_strategies import dispatch_settlement

		mock_inv = MagicMock()
		mock_inv.name = "INV-N001"
		mock_inv.docstatus = 1
		mock_inv.outstanding_amount = 100.0
		mock_frappe.get_doc.return_value = mock_inv
		mock_frappe.db.get_value.side_effect = lambda *a, **kw: {
			("Employee", "EMP-002", "custom_delivery_partner"): None,
			("Sales Invoice", "INV-N001", "outstanding_amount"): 100.0,
		}.get(a[:3], None)

		with patch("jarz_pos.services.settlement_strategies.handle_unpaid_settle_now") as mock_handler:
			mock_handler.return_value = {"success": True, "mode": "unpaid_settle_now"}
			result = dispatch_settlement(
				"INV-N001",
				mode="now",
				pos_profile="POS-001",
				party_type="Employee",
				party="EMP-002",
			)
			mock_handler.assert_called_once()
			self.assertEqual(result["mode"], "unpaid_settle_now")


class TestPartnerUnpaidSettleNow(unittest.TestCase):
	"""Test handle_partner_unpaid_settle_now strategy."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	@patch("jarz_pos.services.settlement_strategies._create_payment_entry")
	@patch("jarz_pos.services.settlement_strategies.get_pos_cash_account")
	@patch("jarz_pos.services.settlement_strategies._get_receivable_account")
	@patch("jarz_pos.services.settlement_strategies._get_delivery_expense_amount")
	@patch("jarz_pos.services.settlement_strategies.ensure_delivery_note_for_invoice")
	@patch("jarz_pos.services.settlement_strategies._create_partner_courier_transaction")
	@patch("jarz_pos.services.settlement_strategies._stamp_partner_fields")
	def test_collects_full_order_amount(
		self, mock_stamp, mock_ct, mock_dn, mock_exp, mock_recv, mock_cash, mock_pe, mock_frappe
	):
		from jarz_pos.services.settlement_strategies import handle_partner_unpaid_settle_now

		mock_inv = MagicMock()
		mock_inv.name = "INV-PU1"
		mock_inv.company = "Test Co"
		mock_inv.grand_total = 500.0
		mock_frappe.db.get_value.return_value = 500.0
		mock_recv.return_value = "Debtors - T"
		mock_cash.return_value = "Cash - T"
		mock_pe.return_value = MagicMock(name="PE-001")
		mock_exp.return_value = 50.0
		mock_dn.return_value = {"delivery_note": "DN-001"}
		mock_ct.return_value = "CT-PARTNER-001"

		result = handle_partner_unpaid_settle_now(
			mock_inv,
			pos_profile="POS-001",
			payment_type="Cash",
			party_type="Employee",
			party="EMP-001",
			delivery_partner="Partner A",
		)

		self.assertTrue(result["success"])
		self.assertEqual(result["mode"], "partner_unpaid_settle_now")
		self.assertTrue(result["is_partner_order"])
		self.assertEqual(result["delivery_partner"], "Partner A")
		self.assertEqual(result["shipping_amount"], 50.0)

		# Verify CT created with full order amount
		mock_ct.assert_called_once()
		ct_kwargs = mock_ct.call_args
		self.assertEqual(ct_kwargs.kwargs["order_amount"], 500.0)
		self.assertEqual(ct_kwargs.kwargs["shipping_amount"], 50.0)
		self.assertEqual(ct_kwargs.kwargs["status"], "Settled")
		self.assertEqual(ct_kwargs.kwargs["delivery_partner"], "Partner A")

		# Verify invoice stamped
		mock_stamp.assert_called_once_with("INV-PU1", "Partner A")


class TestPartnerUnpaidSettleLater(unittest.TestCase):
	"""Test handle_partner_unpaid_settle_later strategy."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	@patch("jarz_pos.services.settlement_strategies._get_delivery_expense_amount")
	@patch("jarz_pos.services.settlement_strategies.ensure_delivery_note_for_invoice")
	@patch("jarz_pos.services.settlement_strategies._create_partner_courier_transaction")
	@patch("jarz_pos.services.settlement_strategies._stamp_partner_fields")
	def test_creates_unsettled_ct(self, mock_stamp, mock_ct, mock_dn, mock_exp, mock_frappe):
		from jarz_pos.services.settlement_strategies import handle_partner_unpaid_settle_later

		mock_inv = MagicMock()
		mock_inv.name = "INV-PUL1"
		mock_inv.company = "Test Co"
		mock_inv.grand_total = 300.0
		mock_exp.return_value = 30.0
		mock_dn.return_value = {"delivery_note": "DN-002"}
		mock_ct.return_value = "CT-PARTNER-002"

		result = handle_partner_unpaid_settle_later(
			mock_inv,
			pos_profile="POS-001",
			payment_type="Cash",
			party_type="Employee",
			party="EMP-001",
			delivery_partner="Partner B",
		)

		self.assertTrue(result["success"])
		self.assertEqual(result["mode"], "partner_unpaid_settle_later")
		self.assertTrue(result["is_partner_order"])

		# CT should be Unsettled
		ct_kwargs = mock_ct.call_args
		self.assertEqual(ct_kwargs.kwargs["status"], "Unsettled")
		self.assertEqual(ct_kwargs.kwargs["order_amount"], 300.0)


class TestPartnerPaidSettleNow(unittest.TestCase):
	"""Test handle_partner_paid_settle_now — no cash exchange."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	@patch("jarz_pos.services.settlement_strategies._get_delivery_expense_amount")
	@patch("jarz_pos.services.settlement_strategies.ensure_delivery_note_for_invoice")
	@patch("jarz_pos.services.settlement_strategies._create_partner_courier_transaction")
	@patch("jarz_pos.services.settlement_strategies._stamp_partner_fields")
	def test_no_cash_exchange_for_online(self, mock_stamp, mock_ct, mock_dn, mock_exp, mock_frappe):
		from jarz_pos.services.settlement_strategies import handle_partner_paid_settle_now

		mock_inv = MagicMock()
		mock_inv.name = "INV-PP1"
		mock_inv.company = "Test Co"
		mock_inv.grand_total = 700.0
		mock_exp.return_value = 70.0
		mock_dn.return_value = {"delivery_note": "DN-003"}
		mock_ct.return_value = "CT-PARTNER-003"

		result = handle_partner_paid_settle_now(
			mock_inv,
			pos_profile="POS-001",
			payment_type="Online",
			party_type="Supplier",
			party="SUP-001",
			delivery_partner="Partner C",
		)

		self.assertTrue(result["success"])
		self.assertEqual(result["mode"], "partner_paid_settle_now")

		# Order amount should be 0 for paid orders
		ct_kwargs = mock_ct.call_args
		self.assertEqual(ct_kwargs.kwargs["order_amount"], 0)
		self.assertEqual(ct_kwargs.kwargs["shipping_amount"], 70.0)
		self.assertEqual(ct_kwargs.kwargs["status"], "Settled")


class TestPartnerPaidSettleLater(unittest.TestCase):
	"""Test handle_partner_paid_settle_later — no cash, fee tracked."""

	@patch("jarz_pos.services.settlement_strategies.frappe")
	@patch("jarz_pos.services.settlement_strategies._get_delivery_expense_amount")
	@patch("jarz_pos.services.settlement_strategies.ensure_delivery_note_for_invoice")
	@patch("jarz_pos.services.settlement_strategies._create_partner_courier_transaction")
	@patch("jarz_pos.services.settlement_strategies._stamp_partner_fields")
	def test_unsettled_fee_only(self, mock_stamp, mock_ct, mock_dn, mock_exp, mock_frappe):
		from jarz_pos.services.settlement_strategies import handle_partner_paid_settle_later

		mock_inv = MagicMock()
		mock_inv.name = "INV-PPL1"
		mock_inv.company = "Test Co"
		mock_inv.grand_total = 400.0
		mock_exp.return_value = 40.0
		mock_dn.return_value = {"delivery_note": "DN-004"}
		mock_ct.return_value = "CT-PARTNER-004"

		result = handle_partner_paid_settle_later(
			mock_inv,
			pos_profile="POS-001",
			payment_type="Online",
			party_type="Supplier",
			party="SUP-001",
			delivery_partner="Partner D",
		)

		self.assertTrue(result["success"])
		self.assertEqual(result["mode"], "partner_paid_settle_later")
		self.assertTrue(result["is_partner_order"])

		ct_kwargs = mock_ct.call_args
		self.assertEqual(ct_kwargs.kwargs["order_amount"], 0)
		self.assertEqual(ct_kwargs.kwargs["status"], "Unsettled")


class TestPartnerStrategyDict(unittest.TestCase):
	"""Test PARTNER_STRATEGY dict has all 4 keys."""

	def test_all_partner_strategies_present(self):
		from jarz_pos.services.settlement_strategies import PARTNER_STRATEGY

		expected_keys = [
			("unpaid", "now"),
			("unpaid", "later"),
			("paid", "now"),
			("paid", "later"),
		]
		for key in expected_keys:
			self.assertIn(key, PARTNER_STRATEGY, f"Missing partner strategy for {key}")
			self.assertTrue(callable(PARTNER_STRATEGY[key]), f"Strategy {key} not callable")


class TestDeliveryPartnerBalancesAPI(unittest.TestCase):
	"""Test delivery_partners.py API endpoints."""

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_get_balances_all(self, mock_frappe):
		from jarz_pos.api.delivery_partners import get_delivery_partner_balances

		mock_frappe.db.sql.return_value = [
			{"delivery_partner": "Partner A", "partner_name": "Partner A", "order_count": 5, "total_shipping": 250.0, "oldest_date": "2025-01-01"},
		]
		result = get_delivery_partner_balances()
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["delivery_partner"], "Partner A")
		mock_frappe.db.sql.assert_called_once()

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_get_balances_specific_partner(self, mock_frappe):
		from jarz_pos.api.delivery_partners import get_delivery_partner_balances

		mock_frappe.db.sql.return_value = [
			{"delivery_partner": "Partner B", "partner_name": "Partner B", "order_count": 3, "total_shipping": 120.0, "oldest_date": "2025-02-01"},
		]
		result = get_delivery_partner_balances(delivery_partner="Partner B")
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["total_shipping"], 120.0)

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_get_unsettled_details(self, mock_frappe):
		from jarz_pos.api.delivery_partners import get_delivery_partner_unsettled_details

		mock_frappe.get_all.return_value = [
			{"name": "CT-001", "reference_invoice": "INV-001", "amount": 500, "shipping_amount": 50},
			{"name": "CT-002", "reference_invoice": "INV-002", "amount": 300, "shipping_amount": 30},
		]
		result = get_delivery_partner_unsettled_details("Partner A")
		self.assertEqual(len(result), 2)
		mock_frappe.get_all.assert_called_once()

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_get_unsettled_details_requires_partner(self, mock_frappe):
		from jarz_pos.api.delivery_partners import get_delivery_partner_unsettled_details

		mock_frappe.throw.side_effect = Exception("delivery_partner is required")
		with self.assertRaises(Exception):
			get_delivery_partner_unsettled_details("")

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_settle_no_unsettled(self, mock_frappe):
		from jarz_pos.api.delivery_partners import settle_delivery_partner

		mock_dp = MagicMock()
		mock_dp.bank_account = None
		mock_dp.settlement_account = None
		mock_dp.partner_name = "Partner A"
		mock_frappe.get_doc.return_value = mock_dp
		mock_frappe.get_all.return_value = []

		result = settle_delivery_partner("Partner A")
		self.assertTrue(result["success"])
		self.assertEqual(result["order_count"], 0)
		self.assertIn("message", result)

	@patch("jarz_pos.api.delivery_partners.frappe")
	def test_settle_creates_journal_entry(self, mock_frappe):
		from jarz_pos.api.delivery_partners import settle_delivery_partner

		mock_dp = MagicMock()
		mock_dp.bank_account = "BA-001"
		mock_dp.settlement_account = "Expense - T"
		mock_dp.partner_name = "Partner A"
		mock_frappe.get_doc.side_effect = lambda *a, **kw: (
			mock_dp if a == ("Delivery Partner", "Partner A")
			else _make_je_mock()
		)
		mock_frappe.get_all.return_value = [
			{"name": "CT-001", "shipping_amount": 50, "reference_invoice": "INV-001"},
			{"name": "CT-002", "shipping_amount": 30, "reference_invoice": "INV-002"},
		]
		mock_frappe.db.get_value.side_effect = lambda dt, dn, field=None: {
			("Bank Account", "BA-001", "account"): "Bank Account - T",
			("Account", "Bank Account - T", "company"): "Test Co",
		}.get((dt, dn, field) if field else (dt, dn), None)
		mock_frappe.utils.today.return_value = "2025-06-01"

		# Mock the JE get_doc for creation
		mock_je = MagicMock()
		mock_je.name = "JE-001"
		original_side_effect = mock_frappe.get_doc.side_effect

		def get_doc_handler(*a, **kw):
			if isinstance(a[0], dict) and a[0].get("doctype") == "Journal Entry":
				return mock_je
			if len(a) == 2 and a[0] == "Delivery Partner":
				return mock_dp
			return MagicMock()

		mock_frappe.get_doc.side_effect = get_doc_handler

		result = settle_delivery_partner("Partner A")
		self.assertTrue(result["success"])
		self.assertEqual(result["order_count"], 2)
		self.assertEqual(result["total_shipping"], 80.0)
		self.assertEqual(result["journal_entry"], "JE-001")
		mock_je.insert.assert_called_once()
		mock_je.submit.assert_called_once()


def _make_je_mock():
	m = MagicMock()
	m.name = "JE-mock"
	return m


if __name__ == "__main__":
	unittest.main()

import unittest
from unittest.mock import MagicMock, patch


def _raise_frappe(message, exc=None, title=None):
	if exc and isinstance(exc, type) and issubclass(exc, Exception):
		raise exc(message)
	raise Exception(message)


def _make_mock_frappe():
	mock_frappe = MagicMock()
	mock_frappe.throw.side_effect = _raise_frappe
	mock_frappe.session.user = "staff@example.com"
	mock_frappe.message_log = []
	return mock_frappe


class TestShiftAPI(unittest.TestCase):
	def test_normalize_opening_balances_accepts_json_string(self):
		from jarz_pos.api.shift import _normalize_opening_balances_payload

		payload = '[{"opening_amount":1250.5,"mode_of_payment":"Cash"}]'
		result = _normalize_opening_balances_payload(payload)

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["mode_of_payment"], "Cash")
		self.assertEqual(result[0]["opening_amount"], 1250.5)

	def test_required_cash_count_rejects_missing_value(self):
		from jarz_pos.api.shift import _get_required_cash_count

		mock_frappe = _make_mock_frappe()

		with patch("jarz_pos.api.shift.frappe", mock_frappe):
			with self.assertRaises(Exception):
				_get_required_cash_count(
					{"mode_of_payment": "Cash"},
					"opening_amount",
					"Opening cash count",
				)

	def test_required_cash_count_accepts_explicit_zero(self):
		from jarz_pos.api.shift import _get_required_cash_count

		amount = _get_required_cash_count(
			{"mode_of_payment": "Cash", "opening_amount": "0.00"},
			"opening_amount",
			"Opening cash count",
		)

		self.assertEqual(amount, 0)

	def test_normalize_closing_balances_accepts_json_string(self):
		from jarz_pos.api.shift import _normalize_closing_balances_payload

		payload = '[{"closing_amount":25876.66,"mode_of_payment":"Cash"}]'
		result = _normalize_closing_balances_payload(payload)

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["mode_of_payment"], "Cash")
		self.assertEqual(result[0]["closing_amount"], 25876.66)

	def test_normalize_closing_balances_rejects_invalid_string(self):
		from jarz_pos.api.shift import _normalize_closing_balances_payload

		mock_frappe = _make_mock_frappe()

		with patch("jarz_pos.api.shift.frappe", mock_frappe):
			with self.assertRaises(Exception):
				_normalize_closing_balances_payload("not-json")

	def test_get_shift_payment_methods_hides_system_amounts(self):
		from jarz_pos.api.shift import get_shift_payment_methods

		mock_frappe = _make_mock_frappe()
		mock_frappe.get_doc.return_value = MagicMock(company="JARZ", branch="Dokki")

		with patch("jarz_pos.api.shift.frappe", mock_frappe), \
				 patch("jarz_pos.api.shift._assert_user_has_profile_access"), \
				 patch("jarz_pos.api.shift._get_profile_primary_mode_of_payment", return_value="Cash"), \
				 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"), \
				 patch("jarz_pos.api.shift._ensure_mode_of_payment_account"):
			result = get_shift_payment_methods("Dokki")

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["mode_of_payment"], "Cash")
		self.assertEqual(result[0]["amounts_hidden"], 1)
		self.assertNotIn("current_balance", result[0])
		self.assertNotIn("default_amount", result[0])
		self.assertNotIn("suggested_opening_amount", result[0])

	def test_start_shift_requires_explicit_opening_amount(self):
		from jarz_pos.api.shift import start_shift

		mock_frappe = _make_mock_frappe()
		mock_frappe.db.get_value.return_value = "JARZ"
		mock_frappe.new_doc.return_value = MagicMock(
			balance_details=[],
			append=MagicMock(),
			insert=MagicMock(),
			submit=MagicMock(),
			add_comment=MagicMock(),
			name="POS-OPE-2026-00001",
		)

		with patch("jarz_pos.api.shift.frappe", mock_frappe), \
				 patch("jarz_pos.api.shift._assert_user_has_profile_access"), \
				 patch("jarz_pos.api.shift._get_latest_opening_entry", return_value=None), \
				 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"), \
				 patch("jarz_pos.api.shift._ensure_mode_of_payment_account"), \
				 patch("jarz_pos.api.shift._get_account_balance", return_value=1250.0):
			with self.assertRaises(Exception):
				start_shift(
					"Dokki",
					[{"mode_of_payment": "Cash", "account": "Dokki - J"}],
				)

	def test_get_shift_summary_hides_pre_submit_amounts(self):
		from jarz_pos.api.shift import get_shift_summary

		mock_frappe = _make_mock_frappe()
		opening = MagicMock()
		opening.user = "staff@example.com"
		opening.name = "POS-OPE-2026-00001"
		opening.status = "Open"
		opening.company = "JARZ"
		opening.pos_profile = "Dokki"
		opening.period_start_date = "2026-05-27 08:00:00"
		opening.period_end_date = None
		mock_frappe.get_doc.return_value = opening

		closing_draft = MagicMock()
		closing_draft.total_quantity = 4
		closing_draft.payment_reconciliation = [
			MagicMock(mode_of_payment="Cash", opening_amount=500),
		]

		with patch("jarz_pos.api.shift.frappe", mock_frappe), \
				 patch("jarz_pos.api.shift.make_closing_entry_from_opening", return_value=closing_draft), \
				 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"), \
				 patch(
					"jarz_pos.api.shift._get_shift_account_movements",
					return_value=[
						{
							"voucher_type": "Sales Invoice",
							"voucher_no": "ACC-SINV-0001",
							"debit": 500,
							"credit": 0,
						}
					],
				 ):
			result = get_shift_summary("POS-OPE-2026-00001")

		self.assertEqual(result["amounts_hidden"], 1)
		self.assertEqual(result["variance_visible"], 0)
		self.assertEqual(result["invoice_count"], 1)
		self.assertNotIn("account_balance", result)
		self.assertNotIn("total_sales", result)
		self.assertNotIn("total_outflows", result)
		self.assertNotIn("net_movement", result)
		self.assertNotIn("account_movements", result)
		self.assertNotIn("grand_total", result)
		self.assertNotIn("net_total", result)
		self.assertIn("courier_close_block", result)
		self.assertFalse(result["courier_close_block"]["blocked"])
		self.assertEqual(result["payment_reconciliation"], [{"mode_of_payment": "Cash"}])

	def test_end_shift_requires_explicit_closing_amount(self):
		from jarz_pos.api.shift import end_shift

		mock_frappe = _make_mock_frappe()
		opening = MagicMock()
		opening.user = "staff@example.com"
		opening.status = "Open"
		opening.docstatus = 1
		opening.company = "JARZ"
		opening.pos_profile = "Dokki"
		opening.name = "POS-OPE-2026-00001"
		mock_frappe.get_doc.return_value = opening

		closing = MagicMock()
		closing.payment_reconciliation = [MagicMock(mode_of_payment="Cash")]

		with patch("jarz_pos.api.shift.frappe", mock_frappe), \
				 patch("jarz_pos.api.shift.make_closing_entry_from_opening", return_value=closing), \
				 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"), \
				 patch("jarz_pos.api.shift._get_account_balance", return_value=25876.66):
			with self.assertRaises(Exception):
				end_shift(
					"POS-OPE-2026-00001",
					[{"mode_of_payment": "Cash"}],
				)

		closing.insert.assert_not_called()

	def test_end_shift_normalizes_string_payload_before_processing(self):
		from jarz_pos.api.shift import end_shift

		mock_frappe = _make_mock_frappe()
		opening = MagicMock()
		opening.user = "staff@example.com"
		opening.status = "Open"
		opening.docstatus = 1
		opening.company = "JARZ"
		opening.pos_profile = "Dokki"
		opening.name = "POS-OPE-2026-00001"
		mock_frappe.get_doc.return_value = opening

		closing = MagicMock()
		closing.payment_reconciliation = [MagicMock(mode_of_payment="Cash", opening_amount=500)]
		closing.name = "POS-CLO-2026-00001"
		closing.status = "Closed"
		closing.net_total = 0
		closing.total_quantity = 0

		with patch("jarz_pos.api.shift.frappe", mock_frappe), \
				 patch("jarz_pos.api.shift.make_closing_entry_from_opening", return_value=closing), \
				 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"), \
				 patch("jarz_pos.api.shift._get_account_balance", return_value=25876.66), \
				 patch("jarz_pos.api.shift._get_shift_account_movements", return_value=[]), \
				 patch("jarz_pos.api.shift._throw_if_shift_has_unsettled_courier_transactions"):
			result = end_shift(
				"POS-OPE-2026-00001",
				'[{"closing_amount":25876.66,"mode_of_payment":"Cash"}]',
			)

		self.assertEqual(closing.payment_reconciliation[0].closing_amount, 25876.66)
		self.assertEqual(closing.payment_reconciliation[0].expected_amount, 25876.66)
		closing.insert.assert_called_once_with(ignore_permissions=True)
		closing.submit.assert_called_once_with()
		self.assertEqual(result["closing_entry"], "POS-CLO-2026-00001")
		self.assertEqual(result["amounts_hidden"], 0)
		self.assertEqual(result["variance_visible"], 1)
		self.assertEqual(result["payment_reconciliation"][0]["difference"], 0)

	def test_get_shift_courier_close_block_uses_effective_profile_and_counts_rows(self):
		from jarz_pos.api.shift import _get_shift_courier_close_block

		mock_frappe = _make_mock_frappe()
		mock_frappe.db.has_column.return_value = True
		mock_frappe.db.sql.return_value = [
			{
				"courier_transaction": "CT-0001",
				"reference_invoice": "ACC-SINV-0001",
				"amount": 120,
				"shipping_amount": 30,
				"party_type": "Employee",
				"party": "HR-EMP-0001",
			},
			{
				"courier_transaction": "CT-0002",
				"reference_invoice": "ACC-SINV-0001",
				"amount": 80,
				"shipping_amount": 10,
				"party_type": "Employee",
				"party": "HR-EMP-0001",
			},
		]

		def _db_get_value(doctype, name, fieldname):
			if doctype == "Employee":
				return "Ali Courier"
			return None

		mock_frappe.db.get_value.side_effect = _db_get_value

		with patch("jarz_pos.api.shift.frappe", mock_frappe):
			result = _get_shift_courier_close_block("Nasr city")

		query = mock_frappe.db.sql.call_args.args[0]
		self.assertIn("COALESCE(NULLIF(si.custom_kanban_profile, ''), si.pos_profile)", query)
		self.assertTrue(result["blocked"])
		self.assertEqual(result["transaction_count"], 2)
		self.assertEqual(result["invoice_count"], 1)
		self.assertEqual(result["party_count"], 1)
		self.assertEqual(result["net_balance"], 160)
		self.assertEqual(result["parties"][0]["display_name"], "Ali Courier")

	def test_end_shift_blocks_when_unsettled_courier_transactions_exist(self):
		from jarz_pos.api.shift import end_shift

		mock_frappe = _make_mock_frappe()
		opening = MagicMock()
		opening.user = "staff@example.com"
		opening.status = "Open"
		opening.docstatus = 1
		opening.company = "JARZ"
		opening.pos_profile = "Dokki"
		opening.name = "POS-OPE-2026-00001"
		mock_frappe.get_doc.return_value = opening

		closing = MagicMock()
		closing.payment_reconciliation = [MagicMock(mode_of_payment="Cash", opening_amount=500)]

		with patch("jarz_pos.api.shift.frappe", mock_frappe), \
				 patch("jarz_pos.api.shift.make_closing_entry_from_opening", return_value=closing), \
				 patch("jarz_pos.api.shift._get_shift_courier_close_block", return_value={
					 "blocked": True,
					 "transaction_count": 2,
					 "party_count": 1,
					 "invoice_count": 1,
				 }), \
				 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"), \
				 patch("jarz_pos.api.shift._get_account_balance", return_value=25876.66):
			with self.assertRaises(Exception):
				end_shift(
					"POS-OPE-2026-00001",
					[{"closing_amount": 25876.66, "mode_of_payment": "Cash"}],
				)

		closing.insert.assert_not_called()
		closing.submit.assert_not_called()
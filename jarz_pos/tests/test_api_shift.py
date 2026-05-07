import unittest
from unittest.mock import MagicMock, patch


def _raise_frappe(message, exc=None, title=None):
	if exc and isinstance(exc, type) and issubclass(exc, Exception):
		raise exc(message)
	raise Exception(message)


class TestShiftAPI(unittest.TestCase):
	def test_normalize_closing_balances_accepts_json_string(self):
		from jarz_pos.api.shift import _normalize_closing_balances_payload

		payload = '[{"closing_amount":25876.66,"mode_of_payment":"Cash"}]'
		result = _normalize_closing_balances_payload(payload)

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["mode_of_payment"], "Cash")
		self.assertEqual(result[0]["closing_amount"], 25876.66)

	def test_normalize_closing_balances_rejects_invalid_string(self):
		from jarz_pos.api.shift import _normalize_closing_balances_payload

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = _raise_frappe

		with patch("jarz_pos.api.shift.frappe", mock_frappe):
			with self.assertRaises(Exception):
				_normalize_closing_balances_payload("not-json")

	def test_end_shift_normalizes_string_payload_before_processing(self):
		from jarz_pos.api.shift import end_shift

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
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
		closing.name = "POS-CLO-2026-00001"
		closing.status = "Closed"

		with patch("jarz_pos.api.shift.frappe", mock_frappe), \
				 patch("jarz_pos.api.shift.make_closing_entry_from_opening", return_value=closing), \
				 patch("jarz_pos.api.shift._resolve_pos_profile_account", return_value="Dokki - J"), \
				 patch("jarz_pos.api.shift._get_account_balance", return_value=25876.66), \
				 patch("jarz_pos.api.shift._get_shift_account_movements", return_value=[]):
			result = end_shift(
				"POS-OPE-2026-00001",
				'[{"closing_amount":25876.66,"mode_of_payment":"Cash"}]',
			)

		self.assertEqual(closing.payment_reconciliation[0].closing_amount, 25876.66)
		self.assertEqual(closing.payment_reconciliation[0].expected_amount, 25876.66)
		closing.insert.assert_called_once_with(ignore_permissions=True)
		closing.submit.assert_called_once_with()
		self.assertEqual(result["closing_entry"], "POS-CLO-2026-00001")
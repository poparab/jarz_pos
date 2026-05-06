"""Focused tests for expense API staff permission handling."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _FakeExpenseDoc:
	def __init__(self):
		self.flags = SimpleNamespace(ignore_permissions=False)
		self.insert = MagicMock()
		self.submit = MagicMock()
		self.reload = MagicMock()

	def as_dict(self):
		return {"name": "JER-0001"}


class TestExpenseAPI(unittest.TestCase):
	def test_create_expense_accepts_dict_pos_profiles_for_staff(self):
		"""Staff expense creation should normalize dict-shaped POS profile rows."""
		from jarz_pos.api.expenses import create_expense

		fake_doc = _FakeExpenseDoc()
		captured_doc = {}

		def _get_doc(doc_dict):
			captured_doc.update(doc_dict)
			return fake_doc

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.get_doc.side_effect = _get_doc

		with patch("jarz_pos.api.expenses.frappe", mock_frappe), \
				 patch("jarz_pos.api.expenses._is_manager", return_value=False), \
				 patch("jarz_pos.api.expenses._default_company", return_value="Jarz"), \
				 patch("jarz_pos.api.expenses.get_pos_profiles", return_value=[{"name": "Dokki", "allow_delivery_partner": False}]), \
				 patch("jarz_pos.api.expenses._resolve_named_account", return_value="Cash - J"), \
				 patch("jarz_pos.api.expenses._serialize_expense", return_value={"name": "JER-0001"}):
			result = create_expense(
				amount=25,
				reason_account="Indirect Expenses - J",
				pos_profile="Dokki",
				remarks="Taxi",
			)

		self.assertTrue(result.get("success"))
		self.assertEqual(captured_doc["pos_profile"], "Dokki")
		self.assertEqual(captured_doc["payment_source_label"], "Dokki")
		self.assertEqual(captured_doc["paying_account"], "Cash - J")
		fake_doc.insert.assert_called_once()
		fake_doc.submit.assert_not_called()
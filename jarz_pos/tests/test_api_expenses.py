"""Focused tests for expense API bootstrap and staff permission handling."""

from datetime import date
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
	def test_get_expense_bootstrap_omits_duplicate_cash_account_for_pos_profile(self):
		"""Manager bootstrap should not list a POS profile account twice."""
		from jarz_pos.api.expenses import PaymentSource, get_expense_bootstrap

		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_all.side_effect = [
			[
				{"name": "Nasr city - J", "account_name": "Nasr city", "account_type": "Cash"},
				{"name": "Wallet - J", "account_name": "Wallet", "account_type": "Cash"},
			],
			[
				{"name": "Nasr city - J", "account_name": "Nasr city"},
				{"name": "Wallet - J", "account_name": "Wallet"},
			],
		]

		with patch("jarz_pos.api.expenses.frappe", mock_frappe), \
				 patch("jarz_pos.api.expenses._is_manager", return_value=True), \
				 patch("jarz_pos.api.expenses._default_company", return_value="Jarz"), \
				 patch("jarz_pos.api.expenses._manager_pos_profiles", return_value=["Nasr city"]), \
				 patch(
				 	"jarz_pos.api.expenses._pos_profile_accounts",
				 	return_value=[
				 		PaymentSource(
				 			account="Nasr city - J",
				 			label="Nasr city",
				 			category="pos_profile",
				 			balance=25,
				 			pos_profile="Nasr city",
				 		)
				 	],
				 ), \
				 patch("jarz_pos.api.expenses._balance_on", return_value=0), \
				 patch("jarz_pos.api.expenses._account_label_map", return_value={}), \
				 patch("jarz_pos.api.expenses._load_months", return_value=["2024-06"]), \
				 patch("jarz_pos.api.expenses._month_label", side_effect=lambda month: month), \
				 patch("jarz_pos.api.expenses.getdate", return_value=date(2024, 6, 1)), \
				 patch("jarz_pos.api.expenses._collect_expenses", return_value=[]), \
				 patch("jarz_pos.api.expenses._indirect_expense_accounts", return_value=[]):
			result = get_expense_bootstrap()

		payment_sources = result["payment_sources"]
		accounts = [source["account"] for source in payment_sources]
		self.assertEqual(accounts.count("Nasr city - J"), 1)
		self.assertEqual(set(accounts), {"Nasr city - J", "Wallet - J"})

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
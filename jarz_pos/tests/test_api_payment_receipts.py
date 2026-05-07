import unittest
from unittest.mock import MagicMock, patch

from frappe.exceptions import PermissionError as FrappePermissionError


def _raise_frappe(message, exc=None, title=None):
	if exc and isinstance(exc, type) and issubclass(exc, Exception):
		raise exc(message)
	raise Exception(message)


class _FakeReceiptDoc:
	def __init__(self, *, status="Unconfirmed", pos_profile="Dokki"):
		self.name = "PPR-0001"
		self.status = status
		self.pos_profile = pos_profile
		self.confirmed_by = None
		self.confirmed_date = None
		self.save = MagicMock()


class _FakeInvoiceDoc:
	def __init__(self, name="ACC-SINV-0001", customer_name="Jarz Test Customer"):
		self.name = name
		self.customer_name = customer_name


class TestPaymentReceiptsAPI(unittest.TestCase):
	def test_has_payment_receipt_confirm_access_matches_role_policy(self):
		from jarz_pos.api.payment_receipts import _has_payment_receipt_confirm_access

		mock_frappe = MagicMock()
		mock_frappe.session.user = "user@example.com"

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki"]):
			mock_frappe.get_roles.return_value = ["Sales User"]
			self.assertFalse(_has_payment_receipt_confirm_access("Dokki"))

			mock_frappe.get_roles.return_value = ["JARZ line manager"]
			self.assertTrue(_has_payment_receipt_confirm_access("Dokki"))
			self.assertFalse(_has_payment_receipt_confirm_access("Nasr city"))

			mock_frappe.get_roles.return_value = ["JARZ Manager"]
			self.assertTrue(_has_payment_receipt_confirm_access("Nasr city"))

	def test_confirm_receipt_denies_staff(self):
		from jarz_pos.api.payment_receipts import confirm_receipt

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.throw.side_effect = _raise_frappe
		mock_frappe.get_doc.return_value = _FakeReceiptDoc()

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._has_payment_receipt_confirm_access", return_value=False):
			with self.assertRaises(FrappePermissionError):
				confirm_receipt("PPR-0001")

		mock_frappe.get_doc.return_value.save.assert_not_called()
		mock_frappe.db.commit.assert_not_called()

	def test_list_payment_receipts_exposes_confirm_capability(self):
		from jarz_pos.api.payment_receipts import list_payment_receipts

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = [{
			"name": "PPR-0001",
			"sales_invoice": "ACC-SINV-0001",
			"payment_method": "Instapay",
			"amount": 120.0,
			"pos_profile": "Dokki",
			"status": "Unconfirmed",
			"receipt_image": "/files/receipt.png",
			"receipt_image_url": "/files/receipt.png",
			"uploaded_by": "staff@example.com",
			"upload_date": "2026-05-07 12:00:00",
			"confirmed_by": None,
			"confirmed_date": None,
			"creation": "2026-05-07 12:00:00",
			"modified": "2026-05-07 12:00:00",
		}]
		mock_frappe.get_doc.return_value = _FakeInvoiceDoc()

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._has_payment_receipt_confirm_access", return_value=False), \
				 patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki"]):
			result = list_payment_receipts()

		self.assertEqual(len(result), 1)
		self.assertFalse(result[0]["can_confirm"])
		self.assertEqual(result[0]["customer_name"], "Jarz Test Customer")
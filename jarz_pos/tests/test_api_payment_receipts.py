import unittest
import sys
import types
from unittest.mock import MagicMock, patch

try:
	from frappe.exceptions import PermissionError as FrappePermissionError
	from frappe.exceptions import ValidationError as FrappeValidationError
except ModuleNotFoundError:
	class FrappePermissionError(Exception):
		pass

	class FrappeValidationError(Exception):
		pass

	frappe_module = types.ModuleType("frappe")
	exceptions_module = types.ModuleType("frappe.exceptions")
	exceptions_module.PermissionError = FrappePermissionError
	exceptions_module.ValidationError = FrappeValidationError
	frappe_module.exceptions = exceptions_module
	frappe_module._ = lambda message: message
	frappe_module.whitelist = lambda *args, **kwargs: (lambda fn: fn)
	sys.modules.setdefault("frappe", frappe_module)
	sys.modules.setdefault("frappe.exceptions", exceptions_module)


def _raise_frappe(message, exc=None, title=None):
	if exc and isinstance(exc, type) and issubclass(exc, Exception):
		raise exc(message)
	raise Exception(message)


class _FakeReceiptDoc:
	def __init__(self, *, status="Unconfirmed", pos_profile="Dokki", sales_invoice="ACC-SINV-0001", payment_method="InstaPay", amount=120.0, receipt_image_url="/files/receipt.png"):
		self.name = "PPR-0001"
		self.status = status
		self.pos_profile = pos_profile
		self.sales_invoice = sales_invoice
		self.payment_method = payment_method
		self.amount = amount
		self.receipt_image = receipt_image_url
		self.receipt_image_url = receipt_image_url
		self.confirmed_by = None
		self.confirmed_date = None
		self.save = MagicMock()


class _FakeInvoiceDoc:
	def __init__(self, name="ACC-SINV-0001", customer_name="Jarz Test Customer", woo_order_id=None):
		self.name = name
		self.customer_name = customer_name
		self.woo_order_id = woo_order_id

	# list_payment_receipts reads woo_order_id through Document.get().
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)


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
		self.assertEqual(
			mock_frappe.get_all.call_args_list[0].kwargs["filters"]["status"],
			["!=", "Changed"],
		)

	def test_mark_payment_receipts_changed_for_invoice_updates_active_receipts(self):
		from jarz_pos.api.payment_receipts import mark_payment_receipts_changed_for_invoice

		mock_frappe = MagicMock()
		receipt_doc = _FakeReceiptDoc(status="Unconfirmed")
		mock_frappe.get_all.return_value = [{
			"name": "PPR-0001",
			"payment_method": "InstaPay",
		}]
		mock_frappe.get_doc.return_value = receipt_doc

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			result = mark_payment_receipts_changed_for_invoice(
				"ACC-SINV-0001",
				payment_methods=["Instapay"],
			)

		self.assertEqual(result, ["PPR-0001"])
		self.assertEqual(receipt_doc.status, "Changed")
		receipt_doc.save.assert_called_once_with(ignore_permissions=True)

	def test_create_payment_receipt_ignores_changed_receipts(self):
		from jarz_pos.api.payment_receipts import create_payment_receipt

		mock_frappe = MagicMock()
		new_receipt = MagicMock()
		new_receipt.name = "PPR-0002"
		mock_frappe.get_all.return_value = []
		mock_frappe.get_doc.return_value = new_receipt
		mock_frappe.session.user = "manager@example.com"

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			result = create_payment_receipt(
				sales_invoice="ACC-SINV-0001",
				payment_method="Instapay",
				amount=120.0,
				pos_profile="Dokki",
			)

		self.assertTrue(result["success"])
		self.assertEqual(result["receipt_name"], "PPR-0002")
		self.assertEqual(
			mock_frappe.get_all.call_args.kwargs["filters"]["status"],
			["!=", "Changed"],
		)

	def test_ensure_uploaded_payment_receipt_requires_image_and_matching_invoice(self):
		from jarz_pos.api.payment_receipts import ensure_uploaded_payment_receipt

		mock_frappe = MagicMock()
		mock_frappe.db.exists.return_value = True
		mock_frappe.throw.side_effect = _raise_frappe
		mock_frappe.get_doc.return_value = _FakeReceiptDoc(
			receipt_image_url="",
		)

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			with self.assertRaises(Exception) as exc:
				ensure_uploaded_payment_receipt(
					"PPR-0001",
					sales_invoice="ACC-SINV-0001",
					payment_method="Instapay",
					amount=120.0,
				)

		self.assertIn("uploaded image", str(exc.exception))

	def test_upload_receipt_image_replaces_previous_file(self):
		from jarz_pos.api.payment_receipts import upload_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.throw.side_effect = _raise_frappe
		receipt = _FakeReceiptDoc(receipt_image_url="/files/old.png")
		new_file = MagicMock()
		new_file.name = "FILE-NEW"
		new_file.file_url = "/files/new.png"

		def _get_doc(*args, **kwargs):
			if args and args[0] == "POS Payment Receipt":
				return receipt
			return new_file

		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.get_all.return_value = [
			{"name": "FILE-OLD", "file_url": "/files/old.png", "attached_to_field": "receipt_image"},
		]

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			result = upload_receipt_image("PPR-0001", "aGVsbG8=", "shot.png")

		self.assertTrue(result["success"])
		self.assertTrue(result["replaced"])
		self.assertEqual(result["file_url"], "/files/new.png")
		self.assertEqual(receipt.receipt_image, "/files/new.png")
		self.assertEqual(receipt.receipt_image_url, "/files/new.png")
		mock_frappe.delete_doc.assert_called_once_with(
			"File", "FILE-OLD", ignore_permissions=True, force=True
		)

	def test_upload_receipt_image_rejects_confirmed_receipt(self):
		from jarz_pos.api.payment_receipts import upload_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = _raise_frappe
		mock_frappe.get_doc.return_value = _FakeReceiptDoc(status="Confirmed")

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			with self.assertRaises(Exception) as exc:
				upload_receipt_image("PPR-0001", "aGVsbG8=", "shot.png")

		self.assertIn("Confirmed payment receipts cannot be changed", str(exc.exception))
		mock_frappe.delete_doc.assert_not_called()

	def test_remove_receipt_image_clears_unconfirmed_receipt(self):
		from jarz_pos.api.payment_receipts import remove_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.db.exists.return_value = True
		mock_frappe.throw.side_effect = _raise_frappe
		receipt = _FakeReceiptDoc(receipt_image_url="/files/old.png")
		receipt.upload_date = "2026-08-27 10:00:00"
		mock_frappe.get_doc.return_value = receipt
		mock_frappe.get_all.return_value = [
			{"name": "FILE-OLD", "file_url": "/files/old.png", "attached_to_field": "receipt_image"},
		]

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			result = remove_receipt_image("PPR-0001")

		self.assertTrue(result["success"])
		self.assertIsNone(receipt.receipt_image)
		self.assertIsNone(receipt.receipt_image_url)
		self.assertIsNone(receipt.upload_date)
		# The record itself survives so the next upload reuses the same row.
		self.assertEqual(receipt.status, "Unconfirmed")
		receipt.save.assert_called_once()
		mock_frappe.delete_doc.assert_called_once_with(
			"File", "FILE-OLD", ignore_permissions=True, force=True
		)

	def test_remove_receipt_image_rejects_confirmed_receipt(self):
		from jarz_pos.api.payment_receipts import remove_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.db.exists.return_value = True
		mock_frappe.throw.side_effect = _raise_frappe
		receipt = _FakeReceiptDoc(status="Confirmed")
		mock_frappe.get_doc.return_value = receipt

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			with self.assertRaises(Exception) as exc:
				remove_receipt_image("PPR-0001")

		self.assertIn("Confirmed payment receipts cannot be changed", str(exc.exception))
		self.assertEqual(receipt.receipt_image_url, "/files/receipt.png")
		receipt.save.assert_not_called()
		mock_frappe.delete_doc.assert_not_called()

	def test_remove_receipt_image_rejects_changed_receipt(self):
		from jarz_pos.api.payment_receipts import remove_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.db.exists.return_value = True
		mock_frappe.throw.side_effect = _raise_frappe
		mock_frappe.get_doc.return_value = _FakeReceiptDoc(status="Changed")

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			with self.assertRaises(Exception) as exc:
				remove_receipt_image("PPR-0001")

		self.assertIn("Changed payment receipts cannot be edited", str(exc.exception))


class TestConfirmOnlinePaymentGate(unittest.TestCase):
	"""confirm_online_payment: manager permission gate + screenshot validation."""

	def test_confirm_online_payment_denies_staff(self):
		from jarz_pos.tests.test_payment_collection_change import (
			_import_delivery_handling,
			_FakeInvoice,
		)

		invoice = _FakeInvoice(
			name="INV-GATE",
			custom_payment_method="Instapay",
			custom_payment_confirmation_status="Awaiting Payment",
		)
		module, _ = _import_delivery_handling(invoice)

		module._ensure_payment_receipt_confirm_access = MagicMock(
			side_effect=FrappePermissionError("Only branch managers and above can confirm")
		)
		module._create_payment_entry = MagicMock()
		module.confirm_receipt = MagicMock()
		module.ensure_uploaded_payment_receipt = MagicMock()

		with self.assertRaises(FrappePermissionError):
			module.confirm_online_payment(
				invoice_name="INV-GATE",
				pos_profile="Dokki",
				reference_no="REF-1",
				receipt_name="PPR-1",
			)

		# Gate runs first: no accounting or receipt confirmation happens
		module._create_payment_entry.assert_not_called()
		module.confirm_receipt.assert_not_called()
		module.ensure_uploaded_payment_receipt.assert_not_called()
		module._ensure_payment_receipt_confirm_access.assert_called_once_with("Dokki")

	def test_confirm_online_payment_validates_screenshot(self):
		from jarz_pos.tests.test_payment_collection_change import (
			_import_delivery_handling,
			_FakeInvoice,
		)

		invoice = _FakeInvoice(
			name="INV-SHOT",
			custom_payment_method="Instapay",
			custom_payment_confirmation_status="Awaiting Payment",
			outstanding_amount=150.0,
		)
		module, stub_frappe = _import_delivery_handling(invoice)

		module._ensure_payment_receipt_confirm_access = MagicMock(return_value=None)
		module._get_real_customer_payment_entry = MagicMock(return_value=None)
		module._normalize_collection_method = MagicMock(return_value="Instapay")
		module._is_online_collection_method = MagicMock(return_value=True)
		module._create_payment_entry = MagicMock()
		module.confirm_receipt = MagicMock()
		stub_frappe.db.get_value = MagicMock(return_value=150.0)
		module.ensure_uploaded_payment_receipt = MagicMock(
			side_effect=Exception("Payment receipt must have an uploaded image")
		)

		with self.assertRaises(Exception) as exc:
			module.confirm_online_payment(
				invoice_name="INV-SHOT",
				pos_profile="Dokki",
				reference_no="REF-1",
				receipt_name="PPR-1",
			)

		self.assertIn("uploaded image", str(exc.exception))
		# Screenshot validation blocks booking and receipt confirmation
		module._create_payment_entry.assert_not_called()
		module.confirm_receipt.assert_not_called()
		module.ensure_uploaded_payment_receipt.assert_called_once()
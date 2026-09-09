import contextlib
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
		self.rejected_by = "manager@example.com" if status == "Rejected" else None
		self.rejected_date = "2026-09-01 09:00:00" if status == "Rejected" else None
		self.rejection_reason = "Wrong amount" if status == "Rejected" else None
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

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), 				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
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


	# -- Branch scoping on the write endpoints -------------------------------
	#
	# ``list_payment_receipts`` has always scoped what a user can SEE to the POS
	# Profiles they are assigned to, while upload/remove/create scoped nothing --
	# so any ``Sales User`` who knew a receipt name could replace or drop another
	# branch's proof of transfer.

	def test_upload_receipt_image_refuses_another_branch(self):
		from jarz_pos.api.payment_receipts import upload_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = _raise_frappe
		receipt = _FakeReceiptDoc(pos_profile="Nasr city")
		mock_frappe.get_doc.return_value = receipt

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
			with self.assertRaises(FrappePermissionError) as exc:
				upload_receipt_image("PPR-0001", "aGVsbG8=", "shot.png")

		self.assertIn("branch you are not assigned to", str(exc.exception))
		receipt.save.assert_not_called()
		mock_frappe.delete_doc.assert_not_called()

	def test_remove_receipt_image_refuses_another_branch(self):
		from jarz_pos.api.payment_receipts import remove_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.db.exists.return_value = True
		mock_frappe.throw.side_effect = _raise_frappe
		receipt = _FakeReceiptDoc(pos_profile="Nasr city")
		mock_frappe.get_doc.return_value = receipt

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
			with self.assertRaises(FrappePermissionError) as exc:
				remove_receipt_image("PPR-0001")

		self.assertIn("branch you are not assigned to", str(exc.exception))
		self.assertEqual(receipt.receipt_image_url, "/files/receipt.png")
		receipt.save.assert_not_called()
		mock_frappe.delete_doc.assert_not_called()

	def test_create_payment_receipt_refuses_another_branch(self):
		from jarz_pos.api.payment_receipts import create_payment_receipt

		mock_frappe = MagicMock()
		mock_frappe.throw.side_effect = _raise_frappe

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
			with self.assertRaises(FrappePermissionError) as exc:
				create_payment_receipt(
					sales_invoice="ACC-SINV-0001",
					payment_method="Instapay",
					amount=120.0,
					pos_profile="Nasr city",
				)

		self.assertIn("branch you are not assigned to", str(exc.exception))
		mock_frappe.db.commit.assert_not_called()

	def test_branch_access_tolerates_a_receipt_with_no_profile(self):
		from jarz_pos.api.payment_receipts import _has_receipt_branch_access

		# ``pos_profile`` is reqd, so an empty one can only be a row predating the
		# field. Refusing would brick it for everyone, its own branch included.
		with patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
			self.assertTrue(_has_receipt_branch_access(""))
			self.assertTrue(_has_receipt_branch_access(None))
			self.assertFalse(_has_receipt_branch_access("Nasr city"))

	# -- A rejection asks for a better screenshot; it is not a dead end ------

	def test_upload_receipt_image_reopens_a_rejected_receipt(self):
		from jarz_pos.api.payment_receipts import upload_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.throw.side_effect = _raise_frappe
		receipt = _FakeReceiptDoc(status="Rejected", receipt_image_url="/files/bad.png")
		new_file = MagicMock()
		new_file.name = "FILE-NEW"
		new_file.file_url = "/files/good.png"

		def _get_doc(*args, **kwargs):
			if args and args[0] == "POS Payment Receipt":
				return receipt
			return new_file

		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.get_all.return_value = []

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
			result = upload_receipt_image("PPR-0001", "aGVsbG8=", "shot.png")

		self.assertTrue(result["success"])
		self.assertTrue(result["reopened"])
		self.assertEqual(result["status"], "Unconfirmed")
		# Back in the manager's queue, with the stale verdict cleared.
		self.assertEqual(receipt.status, "Unconfirmed")
		self.assertIsNone(receipt.rejected_by)
		self.assertIsNone(receipt.rejected_date)
		self.assertIsNone(receipt.rejection_reason)
		self.assertEqual(receipt.receipt_image_url, "/files/good.png")

	def test_upload_receipt_image_leaves_an_unconfirmed_receipt_alone(self):
		from jarz_pos.api.payment_receipts import upload_receipt_image

		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.throw.side_effect = _raise_frappe
		receipt = _FakeReceiptDoc()
		new_file = MagicMock()
		new_file.name = "FILE-NEW"
		new_file.file_url = "/files/new.png"

		def _get_doc(*args, **kwargs):
			if args and args[0] == "POS Payment Receipt":
				return receipt
			return new_file

		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.get_all.return_value = []

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
			result = upload_receipt_image("PPR-0001", "aGVsbG8=", "shot.png")

		self.assertFalse(result["reopened"])
		self.assertEqual(receipt.status, "Unconfirmed")

	# -- Jarz POS Staff files the proof; it never adjudicates it -------------

	def test_pos_staff_may_write_receipts_but_never_confirm(self):
		import json
		import os

		from jarz_pos.api.payment_receipts import _has_payment_receipt_confirm_access

		doctype_json = os.path.join(
			os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
			"doctype", "pos_payment_receipt", "pos_payment_receipt.json",
		)
		with open(doctype_json, encoding="utf-8") as handle:
			perms = json.load(handle)["permissions"]

		staff = [row for row in perms if row.get("role") == "Jarz POS Staff"]
		self.assertEqual(len(staff), 1, "Jarz POS Staff must hold exactly one DocPerm row")
		# Upload and re-upload need create + write. Deleting the row itself is a
		# manager's call, so no delete.
		self.assertEqual(staff[0].get("create"), 1)
		self.assertEqual(staff[0].get("write"), 1)
		self.assertEqual(staff[0].get("read"), 1)
		self.assertNotEqual(staff[0].get("delete"), 1)

		# Write access must not leak into approval authority: confirm and reject
		# are gated in the API, not by the DocPerm row.
		mock_frappe = MagicMock()
		mock_frappe.session.user = "staff@example.com"
		mock_frappe.get_roles.return_value = ["Jarz POS Staff", "POS User"]
		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			self.assertFalse(_has_payment_receipt_confirm_access("Dokki"))
			self.assertFalse(_has_payment_receipt_confirm_access(None))

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

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), 				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
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

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), 				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
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

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), 				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
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

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), 				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
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

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), 				 patch("jarz_pos.api.payment_receipts._allowed_pos_profiles", return_value=["Dokki"]):
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
		module._confirm_receipt_record = MagicMock()
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
		module._confirm_receipt_record.assert_not_called()
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
		module._confirm_receipt_record = MagicMock()
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
		module._confirm_receipt_record.assert_not_called()
		module.ensure_uploaded_payment_receipt.assert_called_once()


@contextlib.contextmanager
def _stubbed_confirm_online_payment(booked):
	"""Install a stub ``delivery_handling`` for the lazy import in confirm_receipt.

	``confirm_receipt`` imports ``confirm_online_payment`` inside the function
	body -- it has to, the two modules import each other. Patching the real
	module would drag in ``erpnext``, which only exists inside a bench, so the
	whole class would be skipped on a bare interpreter and the money path would
	go untested exactly where it is easiest to regress.
	"""
	previous = sys.modules.get("jarz_pos.services.delivery_handling")
	stub = types.ModuleType("jarz_pos.services.delivery_handling")
	stub.confirm_online_payment = booked
	sys.modules["jarz_pos.services.delivery_handling"] = stub
	try:
		yield stub
	finally:
		if previous is not None:
			sys.modules["jarz_pos.services.delivery_handling"] = previous
		else:
			sys.modules.pop("jarz_pos.services.delivery_handling", None)


class TestPendingReceiptFiling(unittest.TestCase):
	"""The row that makes an awaiting order visible on the receipts list.

	Before this, ``handle_unpaid_online_deliver_unconfirmed`` wrote no receipt at
	all, so the list of orders still owing a transfer showed only the orders that
	had already been dealt with -- 20 production orders worth 10,780 EGP were on
	no receipt screen on 2026-09-09.
	"""

	def test_files_an_unconfirmed_row_with_the_select_label(self):
		from jarz_pos.api.payment_receipts import ensure_pending_payment_receipt

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = []
		created = MagicMock()
		created.name = "POS-RCPT-2026-00044"
		mock_frappe.get_doc.return_value = created

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			name = ensure_pending_payment_receipt(
				"ACC-SINV-0001",
				payment_method="Instapay",
				amount=480.0,
				pos_profile="Dokki",
			)

		self.assertEqual(name, "POS-RCPT-2026-00044")
		payload = mock_frappe.get_doc.call_args.args[0]
		# The invoice spells it "Instapay"; the DocType Select accepts only
		# "InstaPay", so writing the invoice's spelling straight through would
		# fail validation and file no row at all.
		self.assertEqual(payload["payment_method"], "InstaPay")
		self.assertEqual(payload["status"], "Unconfirmed")
		self.assertEqual(payload["amount"], 480.0)
		self.assertEqual(payload["pos_profile"], "Dokki")
		created.insert.assert_called_once_with(ignore_permissions=True)

	def test_maps_mobile_wallet_to_the_wallet_label(self):
		from jarz_pos.api.payment_receipts import ensure_pending_payment_receipt

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = []
		mock_frappe.get_doc.return_value = MagicMock(name="x")

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			ensure_pending_payment_receipt(
				"ACC-SINV-0002",
				payment_method="Mobile Wallet",
				amount=100.0,
				pos_profile="Dokki",
			)

		self.assertEqual(
			mock_frappe.get_doc.call_args.args[0]["payment_method"], "Wallet"
		)

	def test_files_nothing_for_a_method_that_takes_no_screenshot(self):
		from jarz_pos.api.payment_receipts import ensure_pending_payment_receipt

		mock_frappe = MagicMock()
		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			for method in ("Cash", "Kashier Card", "", None):
				self.assertIsNone(
					ensure_pending_payment_receipt(
						"ACC-SINV-0003",
						payment_method=method,
						amount=50.0,
						pos_profile="Dokki",
					)
				)
		mock_frappe.get_doc.assert_not_called()

	def test_is_idempotent_and_refreshes_a_stale_amount(self):
		from jarz_pos.api.payment_receipts import ensure_pending_payment_receipt

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = [{
			"name": "POS-RCPT-2026-00044",
			"payment_method": "InstaPay",
			"status": "Unconfirmed",
			"amount": 480.0,
		}]

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			name = ensure_pending_payment_receipt(
				"ACC-SINV-0001",
				payment_method="Instapay",
				amount=525.0,
				pos_profile="Dokki",
			)

		self.assertEqual(name, "POS-RCPT-2026-00044")
		mock_frappe.get_doc.assert_not_called()
		# A re-rated invoice must not strand: ensure_uploaded_payment_receipt
		# refuses a receipt whose amount has drifted from the order.
		mock_frappe.db.set_value.assert_called_once()
		self.assertEqual(mock_frappe.db.set_value.call_args.args[3], 525.0)

	def test_leaves_a_confirmed_row_alone(self):
		from jarz_pos.api.payment_receipts import ensure_pending_payment_receipt

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = [{
			"name": "POS-RCPT-2026-00044",
			"payment_method": "InstaPay",
			"status": "Confirmed",
			"amount": 480.0,
		}]

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			ensure_pending_payment_receipt(
				"ACC-SINV-0001",
				payment_method="Instapay",
				amount=525.0,
				pos_profile="Dokki",
			)

		# Confirmed is evidence a manager looked at. Never rewritten.
		mock_frappe.db.set_value.assert_not_called()

	def test_never_raises_so_a_dispatch_cannot_be_blocked(self):
		from jarz_pos.api.payment_receipts import ensure_pending_payment_receipt

		mock_frappe = MagicMock()
		mock_frappe.get_all.side_effect = Exception("db is down")

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			self.assertIsNone(
				ensure_pending_payment_receipt(
					"ACC-SINV-0001",
					payment_method="Instapay",
					amount=480.0,
					pos_profile="Dokki",
				)
			)

	def test_retiring_spares_a_confirmed_receipt(self):
		from jarz_pos.api.payment_receipts import retire_pending_payment_receipts

		mock_frappe = MagicMock()
		mock_frappe.get_all.return_value = ["PPR-0001"]
		doc = _FakeReceiptDoc(status="Unconfirmed")
		mock_frappe.get_doc.return_value = doc

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe):
			result = retire_pending_payment_receipts("ACC-SINV-0001")

		self.assertEqual(result, ["PPR-0001"])
		self.assertEqual(doc.status, "Changed")
		# Only pending rows are selected -- a Confirmed one is the audit trail
		# for a payment that really happened.
		self.assertEqual(
			mock_frappe.get_all.call_args.kwargs["filters"]["status"],
			["in", ["Unconfirmed", "Rejected"]],
		)


class TestConfirmReceiptCollectsTheMoney(unittest.TestCase):
	"""Confirming in the receipts list must post the payment, not just stamp.

	It used to be a pure stamp, while the Payment Entry was posted only by
	``confirm_online_payment`` on the other screen. Production carried four
	orders -- 17987, 18039, 18048, 18053, 2,460 EGP -- whose proof of transfer a
	manager had confirmed while the invoice stayed fully unpaid.
	"""

	def _awaiting_frappe(self, receipt, outstanding=480.0, status="Awaiting Payment"):
		mock_frappe = MagicMock()
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.throw.side_effect = _raise_frappe
		mock_frappe.get_doc.return_value = receipt
		mock_frappe.db.get_value.return_value = {
			"name": "ACC-SINV-0001",
			"docstatus": 1,
			"outstanding_amount": outstanding,
			"custom_payment_confirmation_status": status,
			"custom_kanban_profile": "Dokki",
			"pos_profile": "Dokki",
		}
		return mock_frappe

	def test_routes_an_awaiting_unpaid_receipt_through_the_payment_path(self):
		from jarz_pos.api.payment_receipts import confirm_receipt

		receipt = _FakeReceiptDoc(status="Unconfirmed")
		mock_frappe = self._awaiting_frappe(receipt)
		booked = MagicMock(return_value={
			"payment_entry": "ACC-PAY-0001",
			"payment_confirmation_status": "Payment Confirmed",
		})

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._ensure_payment_receipt_confirm_access"), \
				 _stubbed_confirm_online_payment(booked):
			result = confirm_receipt("PPR-0001")

		self.assertEqual(result["payment_entry"], "ACC-PAY-0001")
		booked.assert_called_once_with(
			"ACC-SINV-0001", "Dokki", receipt_name="PPR-0001"
		)
		# The stamp is the payment path's job, not a second write from here.
		receipt.save.assert_not_called()

	def test_refuses_without_a_screenshot_and_leaves_the_row_pending(self):
		from jarz_pos.api.payment_receipts import confirm_receipt

		receipt = _FakeReceiptDoc(status="Unconfirmed", receipt_image_url="")
		mock_frappe = self._awaiting_frappe(receipt)
		booked = MagicMock()

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._ensure_payment_receipt_confirm_access"), \
				 _stubbed_confirm_online_payment(booked):
			with self.assertRaises(Exception) as exc:
				confirm_receipt("PPR-0001")

		self.assertIn("screenshot", str(exc.exception))
		booked.assert_not_called()
		# Unconfirmed and still listed is the honest state: nobody has
		# evidenced this transfer yet.
		self.assertEqual(receipt.status, "Unconfirmed")
		receipt.save.assert_not_called()

	def test_an_already_confirmed_receipt_can_still_release_the_money(self):
		"""The way out of the stuck state, from the same button.

		Returning "already confirmed" here is exactly what made those four
		orders permanent.
		"""
		from jarz_pos.api.payment_receipts import confirm_receipt

		receipt = _FakeReceiptDoc(status="Confirmed")
		mock_frappe = self._awaiting_frappe(receipt)
		booked = MagicMock(return_value={"payment_entry": "ACC-PAY-0002"})

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._ensure_payment_receipt_confirm_access"), \
				 _stubbed_confirm_online_payment(booked):
			result = confirm_receipt("PPR-0001")

		self.assertEqual(result["payment_entry"], "ACC-PAY-0002")
		booked.assert_called_once()

	def test_a_receipt_on_a_paid_order_keeps_the_plain_stamp(self):
		from jarz_pos.api.payment_receipts import confirm_receipt

		receipt = _FakeReceiptDoc(status="Unconfirmed")
		mock_frappe = self._awaiting_frappe(receipt, outstanding=0.0)
		mock_frappe.utils.now.return_value = "2026-09-09 12:00:00"
		booked = MagicMock()

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._ensure_payment_receipt_confirm_access"), \
				 _stubbed_confirm_online_payment(booked):
			result = confirm_receipt("PPR-0001")

		booked.assert_not_called()
		self.assertEqual(receipt.status, "Confirmed")
		self.assertEqual(receipt.confirmed_by, "manager@example.com")
		self.assertNotIn("payment_entry", result)

	def test_a_receipt_on_a_non_awaiting_order_keeps_the_plain_stamp(self):
		from jarz_pos.api.payment_receipts import confirm_receipt

		receipt = _FakeReceiptDoc(status="Unconfirmed")
		mock_frappe = self._awaiting_frappe(receipt, status="Converted to Cash")
		mock_frappe.utils.now.return_value = "2026-09-09 12:00:00"
		booked = MagicMock()

		with patch("jarz_pos.api.payment_receipts.frappe", mock_frappe), \
				 patch("jarz_pos.api.payment_receipts._ensure_payment_receipt_confirm_access"), \
				 _stubbed_confirm_online_payment(booked):
			confirm_receipt("PPR-0001")

		booked.assert_not_called()
		self.assertEqual(receipt.status, "Confirmed")

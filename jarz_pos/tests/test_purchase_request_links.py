"""Tests for the request→purchase link guards and the idempotency short-circuit.

Both exist to stop a purchase quietly doing the wrong thing:

* a mismatched ``material_request_item`` would make ERPNext credit *someone
  else's* request as received, closing a request nobody fulfilled;
* a double-tapped submit used to create two invoices — double stock, double
  cash out.
"""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


if "frappe" not in sys.modules:
	fake_frappe = types.ModuleType("frappe")

	class FakePermissionError(Exception):
		pass

	def fake_whitelist(*args, **kwargs):
		def decorator(func):
			return func

		if args and callable(args[0]) and len(args) == 1 and not kwargs:
			return args[0]
		return decorator

	def fake_throw(message, exc=Exception, **kwargs):
		raise exc(message)

	fake_frappe._ = lambda message: message
	fake_frappe.PermissionError = FakePermissionError
	fake_frappe.whitelist = fake_whitelist
	fake_frappe.throw = fake_throw
	fake_frappe.defaults = SimpleNamespace(get_user_default=lambda *a, **k: None)
	fake_frappe.db = SimpleNamespace(
		exists=lambda *a, **k: None,
		get_single_value=lambda *a, **k: None,
		get_value=lambda *a, **k: None,
		count=lambda *a, **k: 0,
		sql=lambda *a, **k: [],
	)
	fake_frappe.get_all = lambda *a, **k: []
	fake_frappe.get_roles = lambda *a, **k: []
	fake_frappe.log_error = lambda *a, **k: None
	fake_frappe.get_traceback = lambda *a, **k: ""
	fake_frappe.session = SimpleNamespace(user="staff@jarz.test")

	sys.modules["frappe"] = fake_frappe

from jarz_pos.api import purchase


MR = "MAT-MR-0001"
MR_LINE = "abc123"


def _line_value(doctype, name, fieldname=None, as_dict=False, **kwargs):
	if doctype == "Material Request Item" and name == MR_LINE:
		return {"parent": MR, "item_code": "RM-TOMATO"}
	if doctype == "Material Request" and fieldname == "docstatus":
		return 1
	return None


class TestValidateRequestLink(unittest.TestCase):
	def test_accepts_a_matching_link(self):
		with patch.object(purchase.frappe.db, "get_value", side_effect=_line_value):
			purchase._validate_request_link(MR, MR_LINE, "RM-TOMATO")  # must not raise

	def test_rejects_a_line_from_another_request(self):
		with patch.object(purchase.frappe.db, "get_value", side_effect=_line_value):
			with self.assertRaises(Exception):
				purchase._validate_request_link("MAT-MR-9999", MR_LINE, "RM-TOMATO")

	def test_rejects_a_line_for_another_item(self):
		"""Crediting the wrong item's request is the failure this prevents."""
		with patch.object(purchase.frappe.db, "get_value", side_effect=_line_value):
			with self.assertRaises(Exception):
				purchase._validate_request_link(MR, MR_LINE, "CN-NYLON")

	def test_rejects_a_vanished_line(self):
		with patch.object(purchase.frappe.db, "get_value", return_value=None):
			with self.assertRaises(Exception):
				purchase._validate_request_link(MR, MR_LINE, "RM-TOMATO")

	def test_rejects_a_request_that_is_not_submitted(self):
		def draft(doctype, name, fieldname=None, as_dict=False, **kwargs):
			if doctype == "Material Request" and fieldname == "docstatus":
				return 0
			return _line_value(doctype, name, fieldname, as_dict, **kwargs)

		with patch.object(purchase.frappe.db, "get_value", side_effect=draft):
			with self.assertRaises(Exception):
				purchase._validate_request_link(MR, MR_LINE, "RM-TOMATO")


class TestBillNoValidation(unittest.TestCase):
	def test_blank_bill_no_allowed_when_not_required(self):
		with patch.object(purchase.frappe.db, "get_single_value", return_value=0):
			purchase._validate_bill_no("Supplier A", None)  # must not raise

	def test_blank_bill_no_rejected_when_required(self):
		with patch.object(purchase.frappe.db, "get_single_value", return_value=1):
			with self.assertRaises(Exception):
				purchase._validate_bill_no("Supplier A", "   ")

	def test_supplied_bill_no_always_passes(self):
		with patch.object(purchase.frappe.db, "get_single_value", return_value=1):
			purchase._validate_bill_no("Supplier A", "INV-77")  # must not raise


class TestIdempotency(unittest.TestCase):
	def test_repeat_key_returns_the_original_invoice(self):
		"""A retry must return the first invoice rather than buying again."""
		with patch.object(purchase, "_ensure_manager_access"):
			with patch.object(
				purchase.frappe.db,
				"get_value",
				return_value={
					"name": "PINV-0001",
					"status": "Paid",
					"outstanding_amount": 0.0,
				},
			):
				result = purchase.create_purchase_invoice(
					supplier="Supplier A",
					items=[{"item_code": "RM-TOMATO", "qty": 1}],
					idempotency_key="key-1",
				)

		self.assertTrue(result["deduplicated"])
		self.assertEqual(result["purchase_invoice"], "PINV-0001")

	def test_missing_items_still_rejected_before_the_key_is_consulted(self):
		with patch.object(purchase, "_ensure_manager_access"):
			with self.assertRaises(Exception):
				purchase.create_purchase_invoice(
					supplier="Supplier A", items=[], idempotency_key="key-1"
				)


class TestCoerceRows(unittest.TestCase):
	def test_accepts_json_string_payload(self):
		"""Frappe hands form-encoded calls a JSON string, not a list."""
		rows = purchase._coerce_rows('[{"item_code": "A", "qty": 2}]')
		self.assertEqual(rows, [{"item_code": "A", "qty": 2}])

	def test_wraps_a_single_dict(self):
		self.assertEqual(purchase._coerce_rows({"item_code": "A"}), [{"item_code": "A"}])

	def test_none_is_empty(self):
		self.assertEqual(purchase._coerce_rows(None), [])

	def test_malformed_json_raises(self):
		with self.assertRaises(Exception):
			purchase._coerce_rows("{not json")


if __name__ == "__main__":
	unittest.main()

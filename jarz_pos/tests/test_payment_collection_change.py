import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock


def _raise_throw(message, *args, **kwargs):
    raise Exception(message)


class _FakeInvoice:
    def __init__(self, **data):
        self._data = {
            "name": "INV-CHANGE-001",
            "company": "Test Company",
            "docstatus": 1,
            "grand_total": 150.0,
            "custom_sales_invoice_state": "Delivered",
            "sales_invoice_state": "Delivered",
            "custom_shipping_expense": 25.0,
            "sales_partner": None,
            "is_return": 0,
            **data,
        }
        self.flags = SimpleNamespace()
        self._save_calls = []

    def __getattr__(self, key):
        if key in self._data:
            return self._data[key]
        raise AttributeError(key)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def save(self, **kwargs):
        self._save_calls.append(kwargs)
        return self


class _JournalEntryCapture:
    def __init__(self):
        self.accounts = []
        self.voucher_type = None
        self.posting_date = None
        self.company = None
        self.title = None
        self.user_remark = None
        self.name = "JE-CHANGE-001"

    def append(self, child_table, row):
        if child_table == "accounts":
            self.accounts.append(row)

    def save(self, **kwargs):
        return None

    def submit(self):
        return None


def _build_stub_frappe(invoice=None):
    frappe_mod = types.ModuleType("frappe")
    frappe_mod._ = lambda message: message
    frappe_mod.throw = _raise_throw
    frappe_mod.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe_mod.generate_hash = lambda length=16: "AUTO-TOKEN"
    frappe_mod.session = SimpleNamespace(user="manager@example.com")
    frappe_mod.local = SimpleNamespace(site="test.local")
    frappe_mod.utils = SimpleNamespace(
        now=lambda: "2026-05-18 10:00:00",
        nowdate=lambda: "2026-05-18",
        now_datetime=lambda: "2026-05-18 10:00:00",
        flt=lambda value, precision=None: round(float(value or 0), precision or 2),
    )
    frappe_mod.db = SimpleNamespace(
        sql=MagicMock(return_value=None),
        savepoint=MagicMock(return_value=None),
        rollback=MagicMock(return_value=None),
        set_value=MagicMock(),
        get_value=MagicMock(return_value=None),
        exists=MagicMock(return_value=False),
        has_column=MagicMock(return_value=False),
    )
    frappe_mod.get_doc = MagicMock(return_value=invoice or _FakeInvoice())
    frappe_mod.get_all = MagicMock(return_value=[])
    frappe_mod.new_doc = MagicMock(return_value=_JournalEntryCapture())
    frappe_mod.publish_realtime = MagicMock()
    frappe_mod.get_roles = MagicMock(return_value=["System Manager", "JARZ Manager"])
    frappe_mod.log_error = MagicMock()
    frappe_mod.logger = MagicMock(return_value=MagicMock())
    frappe_mod.ValidationError = type("ValidationError", (Exception,), {})
    frappe_mod.PermissionError = type("PermissionError", (Exception,), {})
    frappe_mod.TimestampMismatchError = type("TimestampMismatchError", (Exception,), {})
    frappe_mod.exceptions = SimpleNamespace(ValidationError=frappe_mod.ValidationError)
    return frappe_mod


def _build_stub_account_utils():
    module = types.ModuleType("jarz_pos.utils.account_utils")
    module.get_freight_expense_account = MagicMock(return_value="Freight - TC")
    module.get_courier_outstanding_account = MagicMock(return_value="Courier Outstanding - TC")
    module.get_pos_cash_account = MagicMock(return_value="Cash - TC")
    module.validate_account_exists = MagicMock()
    module.get_creditors_account = MagicMock(return_value="Creditors - TC")
    return module


def _build_stub_payment_receipts():
    module = types.ModuleType("jarz_pos.api.payment_receipts")
    module.mark_payment_receipts_changed_for_invoice = MagicMock(return_value=[])
    module.ensure_uploaded_payment_receipt = MagicMock(return_value={
        "name": "PPR-001",
        "sales_invoice": "INV-CHANGE-001",
        "payment_method": "InstaPay",
        "amount": 150.0,
        "status": "Unconfirmed",
        "receipt_image_url": "/files/receipt.png",
    })
    return module


def _import_delivery_handling(invoice=None):
    stub_frappe = _build_stub_frappe(invoice=invoice)
    stub_account_utils = _build_stub_account_utils()
    stub_payment_receipts = _build_stub_payment_receipts()
    previous_payment_receipts = sys.modules.get("jarz_pos.api.payment_receipts")
    previous_frappe = sys.modules.get("frappe")
    previous_account_utils = sys.modules.get("jarz_pos.utils.account_utils")
    previous_delivery_handling = sys.modules.get("jarz_pos.services.delivery_handling")
    sys.modules.pop("jarz_pos.services.delivery_handling", None)
    sys.modules["frappe"] = stub_frappe
    sys.modules["jarz_pos.utils.account_utils"] = stub_account_utils
    sys.modules["jarz_pos.api.payment_receipts"] = stub_payment_receipts
    try:
        module = importlib.import_module("jarz_pos.services.delivery_handling")
    finally:
        if previous_delivery_handling is not None:
            sys.modules["jarz_pos.services.delivery_handling"] = previous_delivery_handling
        else:
            sys.modules.pop("jarz_pos.services.delivery_handling", None)
        if previous_frappe is not None:
            sys.modules["frappe"] = previous_frappe
        else:
            sys.modules.pop("frappe", None)
        if previous_account_utils is not None:
            sys.modules["jarz_pos.utils.account_utils"] = previous_account_utils
        else:
            sys.modules.pop("jarz_pos.utils.account_utils", None)
        if previous_payment_receipts is not None:
            sys.modules["jarz_pos.api.payment_receipts"] = previous_payment_receipts
        else:
            sys.modules.pop("jarz_pos.api.payment_receipts", None)
    return module, stub_frappe


class TestPaymentCollectionChangeHelpers(unittest.TestCase):
    def test_apply_collection_change_to_cash_updates_ct_without_journal_entry(self):
        invoice = _FakeInvoice(name="INV-CASH-001", custom_payment_method="Instapay")
        module, stub_frappe = _import_delivery_handling(invoice)
        module.mark_payment_receipts_changed_for_invoice = MagicMock(return_value=["PPR-0001"])

        ct = {
            "name": "CT-001",
            "payment_mode": "Deferred",
            "notes": "",
            "idempotency_token": None,
        }

        result = module._apply_collection_change_to_cash(
            inv=invoice,
            ct=ct,
            new_method="Cash",
            order_amount=150.0,
            shipping_amount=20.0,
            notes="cash collected at door",
            idempotency_token="TOKEN-1",
        )

        self.assertEqual(result["mode"], "online_intent_to_cash")
        self.assertIsNone(result["journal_entry"])
        self.assertEqual(result["changed_receipts"], ["PPR-0001"])
        module.mark_payment_receipts_changed_for_invoice.assert_called_once_with("INV-CASH-001")
        stub_frappe.db.set_value.assert_called_once()
        values = stub_frappe.db.set_value.call_args.args[2]
        self.assertEqual(values["payment_mode"], "Cash")
        self.assertEqual(values["shipping_amount"], 20.0)
        self.assertEqual(values["idempotency_token"], "TOKEN-1")
        self.assertIn("Payment collection changed on", values["notes"])
        self.assertIn("changed_receipts=PPR-0001", values["notes"])
        self.assertEqual(invoice.custom_payment_method, "Cash")
        self.assertTrue(invoice.flags.ignore_validate_update_after_submit)
        self.assertEqual(len(invoice._save_calls), 1)

    def test_apply_collection_change_to_online_creates_je_and_shipping_only_ct(self):
        invoice = _FakeInvoice(name="INV-ONLINE-001", custom_payment_method="Cash")
        module, stub_frappe = _import_delivery_handling(invoice)
        module._get_online_collection_account = MagicMock(return_value="Bank Account - TC")
        module._get_courier_outstanding_account = MagicMock(return_value="Courier Outstanding - TC")
        module.validate_account_exists = MagicMock()
        stub_frappe.get_all.return_value = []
        journal_entry = _JournalEntryCapture()
        stub_frappe.new_doc.return_value = journal_entry

        ct = {
            "name": "CT-ONLINE-001",
            "payment_mode": "Deferred",
            "notes": "",
            "idempotency_token": None,
            "journal_entry": None,
        }

        result = module._apply_collection_change_to_online(
            inv=invoice,
            ct=ct,
            new_method="Instapay",
            order_amount=150.0,
            shipping_amount=25.0,
            reference_no="REF-123",
            reference_date="2026-05-18",
            receipt_name="PR-001",
            receipt_data={
                "name": "PR-001",
                "payment_method": "InstaPay",
                "status": "Unconfirmed",
                "receipt_image_url": "/files/receipt.png",
            },
            notes="paid by instapay at door",
            idempotency_token="TOKEN-2",
        )

        self.assertEqual(result["mode"], "cod_to_online")
        self.assertEqual(result["journal_entry"], "JE-CHANGE-001")
        self.assertEqual(len(journal_entry.accounts), 2)
        self.assertEqual(journal_entry.accounts[0]["account"], "Bank Account - TC")
        self.assertEqual(journal_entry.accounts[0]["debit_in_account_currency"], 150.0)
        self.assertEqual(journal_entry.accounts[1]["account"], "Courier Outstanding - TC")
        self.assertEqual(journal_entry.accounts[1]["credit_in_account_currency"], 150.0)
        values = stub_frappe.db.set_value.call_args.args[2]
        self.assertEqual(values["amount"], 0)
        self.assertEqual(values["shipping_amount"], 25.0)
        self.assertEqual(values["payment_mode"], "Instapay")
        self.assertEqual(values["journal_entry"], "JE-CHANGE-001")
        self.assertIn("receipt=PR-001", values["notes"])
        self.assertEqual(result["receipt_image_url"], "/files/receipt.png")
        self.assertEqual(result["receipt_status"], "Unconfirmed")
        self.assertEqual(invoice.custom_payment_method, "Instapay")
        self.assertTrue(invoice.flags.ignore_validate_update_after_submit)
        self.assertEqual(len(invoice._save_calls), 1)


class TestPaymentCollectionChangeService(unittest.TestCase):
    def test_blocks_real_customer_payment_entry(self):
        module, _ = _import_delivery_handling()
        module._get_collection_change_source_ct = MagicMock(return_value={
            "name": "CT-001",
            "party_type": "Employee",
            "party": "EMP-001",
            "amount": 150.0,
            "shipping_amount": 25.0,
            "is_partner_order": 0,
            "delivery_partner": None,
            "partner_invoice_ref": None,
        })
        module._get_real_customer_payment_entry = MagicMock(return_value={"name": "PE-001"})
        module._validate_collection_receipt = MagicMock()

        with self.assertRaises(Exception) as exc:
            module.change_payment_collection_method(
                invoice_name="INV-CHANGE-001",
                new_method="Cash",
                pos_profile="Nasr city",
            )

        self.assertIn("real customer payment", str(exc.exception))

    def test_cash_flow_publishes_realtime_event(self):
        module, stub_frappe = _import_delivery_handling(_FakeInvoice(name="INV-CASH-002"))
        module._get_collection_change_source_ct = MagicMock(return_value={
            "name": "CT-002",
            "party_type": "Employee",
            "party": "EMP-002",
            "amount": 150.0,
            "shipping_amount": 20.0,
            "payment_mode": "Deferred",
            "is_partner_order": 0,
            "delivery_partner": None,
            "partner_invoice_ref": None,
        })
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module._validate_collection_receipt = MagicMock()
        module._apply_collection_change_to_cash = MagicMock(return_value={
            "mode": "online_intent_to_cash",
            "invoice": "INV-CASH-002",
            "courier_transaction": "CT-002",
            "journal_entry": None,
            "order_amount": 150.0,
            "shipping_amount": 20.0,
            "idempotency_token": "AUTO-TOKEN",
        })

        result = module.change_payment_collection_method(
            invoice_name="INV-CASH-002",
            new_method="Cash",
            pos_profile="Nasr city",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "payment_collection_changed")
        self.assertEqual(result["new_method"], "Cash")
        stub_frappe.publish_realtime.assert_called_once()
        module._apply_collection_change_to_cash.assert_called_once()

    def test_online_collection_requires_uploaded_receipt(self):
        module, _ = _import_delivery_handling(_FakeInvoice(name="INV-ONLINE-NEEDS-REF"))
        module._get_collection_change_source_ct = MagicMock(return_value={
            "name": "CT-003",
            "party_type": "Employee",
            "party": "EMP-003",
            "amount": 150.0,
            "shipping_amount": 25.0,
            "payment_mode": "Deferred",
            "is_partner_order": 0,
            "delivery_partner": None,
            "partner_invoice_ref": None,
        })
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module._validate_collection_receipt = MagicMock()

        with self.assertRaises(Exception) as exc:
            module.change_payment_collection_method(
                invoice_name="INV-ONLINE-NEEDS-REF",
                new_method="Instapay",
                pos_profile="Nasr city",
            )

        self.assertIn("requires an uploaded payment receipt", str(exc.exception))

    def test_replayed_token_returns_existing_result(self):
        invoice = _FakeInvoice(name="INV-ONLINE-REPLAY", custom_payment_method="Cash")
        module, stub_frappe = _import_delivery_handling(invoice)
        module._get_collection_change_source_ct = MagicMock(return_value={
            "name": "CT-REPLAY-1",
            "party_type": "Employee",
            "party": "EMP-004",
            "amount": 0.0,
            "shipping_amount": 15.0,
            "payment_mode": "Instapay",
            "journal_entry": "JE-EXISTING",
            "idempotency_token": "TOKEN-REPLAY",
            "is_partner_order": 0,
            "delivery_partner": None,
            "partner_invoice_ref": None,
        })

        result = module.change_payment_collection_method(
            invoice_name="INV-ONLINE-REPLAY",
            new_method="Instapay",
            pos_profile="Nasr city",
            reference_no="REF-999",
            idempotency_token="TOKEN-REPLAY",
        )

        self.assertEqual(result["journal_entry"], "JE-EXISTING")
        self.assertEqual(result["mode"], "cod_to_online")
        stub_frappe.db.savepoint.assert_not_called()
        stub_frappe.publish_realtime.assert_not_called()
        self.assertEqual(invoice.custom_payment_method, "Instapay")
        self.assertTrue(invoice.flags.ignore_validate_update_after_submit)
        self.assertEqual(len(invoice._save_calls), 1)

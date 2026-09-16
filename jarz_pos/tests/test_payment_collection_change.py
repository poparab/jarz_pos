import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


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


class _PendingReceipt:
    """Stand-in for ``api.payment_receipts.PendingReceipt``.

    Built locally rather than imported: the real module cannot be imported
    inside the stub-``frappe`` window (it does ``from frappe.exceptions import
    ...``, and the stub is a plain module, not a package). The contract mirrored
    here is what ``reconcile_payment_confirmation`` reads -- ``name`` for "is
    there a receipt at all" and ``changed`` for "did this call write anything";
    ``test_api_payment_receipts`` asserts the real object against the real
    function.

    ``__bool__`` is mirrored rather than left to default. A ``SimpleNamespace``
    is truthy even with ``name=None`` -- the exact inverse of the real class --
    so a later refactor of the reconciler to ``if not pending:``, which is
    CORRECT against the real object, would silently stop exercising the
    log-note branch below while the test carried on passing.
    """

    def __init__(self, name, *, created=False, amount_synced=False):
        self.name = name
        self.created = created
        self.amount_synced = amount_synced

    @property
    def changed(self):
        return self.created or self.amount_synced

    def __bool__(self):
        return bool(self.name)


def _pending_receipt(name, *, created=False, amount_synced=False):
    return _PendingReceipt(name, created=created, amount_synced=amount_synced)


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
    module.confirm_receipt = MagicMock(return_value={"success": True})
    # ``confirm_online_payment`` stamps the row through the extracted
    # ``_confirm_receipt_record``; going through the whitelisted
    # ``confirm_receipt`` would recurse, because that entry point now routes an
    # awaiting-payment receipt back into ``confirm_online_payment``.
    module._confirm_receipt_record = MagicMock(return_value=None)
    module.ensure_pending_payment_receipt = MagicMock(
        return_value=_pending_receipt("PPR-PENDING", created=True)
    )
    module.receipt_method_label = MagicMock(return_value="InstaPay")
    # Imported by delivery_handling at module scope: the dispatch and
    # confirmation paths both ask whether a transfer screenshot is on file.
    module.get_live_transfer_receipt = MagicMock(return_value=None)
    module.retire_pending_payment_receipts = MagicMock(return_value=[])
    module._ensure_payment_receipt_confirm_access = MagicMock(return_value=None)
    module._has_payment_receipt_confirm_access = MagicMock(return_value=True)
    return module


_REAL_DEPENDENCIES_WARMED = False


def _warm_real_dependencies():
    """Import the real dependency graph once, before any stub is installed.

    ``_import_delivery_handling`` re-imports ``delivery_handling`` under a stub
    ``frappe`` that is a plain module, not a package. Any transitive
    ``import frappe.<submodule>`` executed inside that window therefore blows up
    with "No module named 'frappe.X'; 'frappe' is not a package" -- unless the
    submodule is already cached in ``sys.modules``, in which case the import
    system returns the cached entry without ever touching the stub parent.

    ``delivery_handling`` imports ``erpnext.stock.stock_ledger``, which pulls in
    ``erpnext/__init__`` -> ``frappe.utils.user`` -> ``import frappe.share``, so
    whether this suite passed depended on what had happened to import erpnext
    earlier in the same process. Importing the real module here caches every
    real dependency up front, so the stubbed re-import below only re-executes
    ``delivery_handling`` itself and resolves everything else from the cache.
    """
    global _REAL_DEPENDENCIES_WARMED
    if _REAL_DEPENDENCIES_WARMED:
        return
    # Latch first: on an interpreter without a bench (no frappe/erpnext at all)
    # this cannot succeed, and the stubbed import below fails loudly on its own
    # rather than being retried once per test.
    _REAL_DEPENDENCIES_WARMED = True
    try:
        importlib.import_module("jarz_pos.services.delivery_handling")
    except ImportError:
        pass


def _import_delivery_handling(invoice=None):
    _warm_real_dependencies()
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
        module, _ = _import_delivery_handling(_FakeInvoice(name="INV-CASH-002"))
        # Assert on the module seam, not on ``stub_frappe.publish_realtime``:
        # ``_publish_branch_event`` imports ``jarz_pos.utils.realtime`` lazily, at
        # call time, which is outside the stub window -- so that helper binds the
        # real ``frappe`` and the stub's mock would never see the call.
        module._publish_branch_event = MagicMock(return_value=[])
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
        module._publish_branch_event.assert_called_once()
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
        module._publish_branch_event = MagicMock(return_value=[])
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
        module._publish_branch_event.assert_not_called()
        self.assertEqual(invoice.custom_payment_method, "Instapay")
        self.assertTrue(invoice.flags.ignore_validate_update_after_submit)
        self.assertEqual(len(invoice._save_calls), 1)


class TestConfirmOnlinePayment(unittest.TestCase):
    """confirm_online_payment: booking, idempotency, field stamping."""

    def test_confirm_creates_pe_debiting_bank_crediting_debtors_and_stamps_fields(self):
        invoice = _FakeInvoice(
            name="INV-ONLINE-CONF",
            custom_payment_method="Instapay",
            custom_payment_confirmation_status="Awaiting Payment",
            outstanding_amount=150.0,
        )
        module, stub_frappe = _import_delivery_handling(invoice)

        module._ensure_payment_receipt_confirm_access = MagicMock(return_value=None)
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module._normalize_collection_method = MagicMock(return_value="Instapay")
        module._is_online_collection_method = MagicMock(return_value=True)
        module.ensure_uploaded_payment_receipt = MagicMock(return_value={
            "name": "PPR-1",
            "payment_method": "Instapay",
            "amount": 150.0,
            "status": "Unconfirmed",
            "receipt_image_url": "/files/receipt.png",
        })
        module._confirm_receipt_record = MagicMock(return_value=None)
        module._get_receivable_account = MagicMock(return_value="Debtors - TC")
        module._get_online_collection_account = MagicMock(return_value="Instapay - TC")

        captured = {}

        def _fake_pe(inv, paid_from, paid_to, outstanding, *args, **kwargs):
            captured["paid_from"] = paid_from
            captured["paid_to"] = paid_to
            captured["outstanding"] = outstanding
            return SimpleNamespace(name="PE-ONLINE-1")

        module._create_payment_entry = MagicMock(side_effect=_fake_pe)
        stub_frappe.db.get_value = MagicMock(return_value=150.0)

        result = module.confirm_online_payment(
            invoice_name="INV-ONLINE-CONF",
            pos_profile="Nasr city",
            reference_no="REF-777",
            receipt_name="PPR-1",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["payment_entry"], "PE-ONLINE-1")
        self.assertEqual(result["payment_confirmation_status"], "Payment Confirmed")
        self.assertEqual(result["method"], "Instapay")
        # DR Instapay/Bank ledger (paid_to), CR Debtors (paid_from)
        self.assertEqual(captured["paid_to"], "Instapay - TC")
        self.assertEqual(captured["paid_from"], "Debtors - TC")
        self.assertEqual(captured["outstanding"], 150.0)
        # Fields stamped on the invoice → confirmed
        self.assertEqual(invoice.custom_payment_confirmation_status, "Payment Confirmed")
        self.assertEqual(invoice.custom_payment_confirmation_reference, "REF-777")
        self.assertEqual(invoice.custom_payment_confirmed_by, "manager@example.com")
        module._confirm_receipt_record.assert_called_once()
        module.ensure_uploaded_payment_receipt.assert_called_once()

    def test_double_confirm_is_noop(self):
        invoice = _FakeInvoice(
            name="INV-ALREADY",
            custom_payment_method="Instapay",
            custom_payment_confirmation_status="Payment Confirmed",
        )
        module, stub_frappe = _import_delivery_handling(invoice)

        module._ensure_payment_receipt_confirm_access = MagicMock(return_value=None)
        module._get_real_customer_payment_entry = MagicMock(return_value={"name": "PE-EXISTING"})
        module._create_payment_entry = MagicMock()
        module._confirm_receipt_record = MagicMock()

        result = module.confirm_online_payment(
            invoice_name="INV-ALREADY",
            pos_profile="Nasr city",
            reference_no="REF-1",
            receipt_name="PPR-1",
        )

        self.assertTrue(result["success"])
        self.assertTrue(result.get("already_confirmed"))
        self.assertEqual(result["payment_entry"], "PE-EXISTING")
        self.assertEqual(result["payment_confirmation_status"], "Payment Confirmed")
        module._create_payment_entry.assert_not_called()
        module._confirm_receipt_record.assert_not_called()


def _raise_with_exc(message, exc=None, *args, **kwargs):
    """``frappe.throw`` that keeps the exception class.

    The shared stub's ``_raise_throw`` raises a bare ``Exception`` whatever class
    was passed, so a branch refusal would be indistinguishable from any other
    failure. The scoping tests below must prove it is a PermissionError.
    """
    if isinstance(exc, type) and issubclass(exc, BaseException):
        raise exc(message)
    raise Exception(message)


class TestListUnconfirmedOnlineOrdersBranchScope(unittest.TestCase):
    """list_unconfirmed_online_orders: the receipt-list branch rule.

    The rows carry customer names, amounts and transfer screenshots. An explicit
    ``pos_profile`` used to be trusted as given, and a caller assigned to no
    branch got an unfiltered query -- every branch's awaiting orders.
    """

    _ROW = {
        "name": "INV-AWAIT-001",
        "customer": "CUST-1",
        "customer_name": "Jarz Test Customer",
        "grand_total": 150.0,
        "outstanding_amount": 150.0,
        "custom_payment_method": "Instapay",
        "custom_ofd_unconfirmed_since": "2026-09-13 09:00:00",
        "custom_courier_party_type": "Employee",
        "custom_courier_party": "EMP-1",
        "pos_profile": "Dokki",
        "custom_kanban_profile": "Dokki",
        "woo_order_id": None,
    }

    def _module(self):
        module, stub_frappe = _import_delivery_handling()
        stub_frappe.throw = MagicMock(side_effect=_raise_with_exc)
        module._latest_active_payment_receipt = MagicMock(return_value={
            "name": "PPR-1",
            "status": "Unconfirmed",
            "receipt_image_url": "/files/receipt.png",
        })
        module._resolve_party_display_name = MagicMock(return_value="Courier One")
        module._seconds_since_datetime = MagicMock(return_value=60)
        module._has_payment_receipt_confirm_access = MagicMock(return_value=True)
        module.normalize_woo_order_id = MagicMock(return_value=None)
        return module, stub_frappe

    def test_explicit_profile_of_another_branch_is_refused(self):
        from jarz_pos.utils.access_control import BranchAccessError

        module, stub_frappe = self._module()
        stub_frappe.get_all = MagicMock(return_value=[dict(self._ROW)])

        with patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki"]):
            with self.assertRaises(BranchAccessError) as exc:
                module.list_unconfirmed_online_orders(pos_profile="Nasr city")

        self.assertIn("branch you are not assigned to", str(exc.exception))
        # Refused before the query: nothing of the other branch is read.
        stub_frappe.get_all.assert_not_called()
        module._latest_active_payment_receipt.assert_not_called()

    def test_explicit_profile_is_refused_for_a_branchless_caller(self):
        from jarz_pos.utils.access_control import BranchAccessError

        module, stub_frappe = self._module()

        with patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=[]):
            with self.assertRaises(BranchAccessError):
                module.list_unconfirmed_online_orders(pos_profile="Dokki")

        stub_frappe.get_all.assert_not_called()

    def test_branchless_caller_without_profile_gets_empty_list(self):
        module, stub_frappe = self._module()
        stub_frappe.get_all = MagicMock(return_value=[dict(self._ROW)])

        with patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=[]):
            result = module.list_unconfirmed_online_orders()

        self.assertEqual(result, {"success": True, "orders": []})
        # No unfiltered query is issued at all.
        stub_frappe.get_all.assert_not_called()
        module._latest_active_payment_receipt.assert_not_called()

    def test_assigned_user_without_profile_is_scoped_to_their_branches(self):
        module, stub_frappe = self._module()
        stub_frappe.get_all = MagicMock(return_value=[dict(self._ROW)])

        with patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki", "Nasr city"]):
            result = module.list_unconfirmed_online_orders()

        self.assertTrue(result["success"])
        self.assertEqual(len(result["orders"]), 1)
        order = result["orders"][0]
        self.assertEqual(order["invoice"], "INV-AWAIT-001")
        self.assertEqual(order["receipt_name"], "PPR-1")
        self.assertEqual(order["receipt_image_url"], "/files/receipt.png")
        self.assertTrue(order["can_confirm"])
        _, kwargs = stub_frappe.get_all.call_args
        self.assertEqual(kwargs["filters"]["custom_kanban_profile"], ["in", ["Dokki", "Nasr city"]])
        self.assertEqual(kwargs["filters"]["custom_payment_confirmation_status"], "Awaiting Payment")

    def test_assigned_user_explicit_own_profile_narrows_filter(self):
        module, stub_frappe = self._module()
        stub_frappe.get_all = MagicMock(return_value=[dict(self._ROW)])

        with patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Dokki", "Nasr city"]):
            result = module.list_unconfirmed_online_orders(pos_profile=" Dokki ")

        self.assertEqual(len(result["orders"]), 1)
        _, kwargs = stub_frappe.get_all.call_args
        self.assertEqual(kwargs["filters"]["custom_kanban_profile"], "Dokki")


class TestUnpaidOnlineCollectionChange(unittest.TestCase):
    """The dispatch shape produced by handle_unpaid_online_deliver_unconfirmed.

    Its Courier Transaction carries ``amount == 0`` (the courier collects nothing) while
    the receivable is still on Debtors. Before 2026-09-01 that combination was refused
    with "Courier transaction has no customer amount to change", which stranded every
    unpaid-InstaPay order whose customer then wanted to pay cash.
    """

    FREIGHT_CT = {
        "name": "CT-FREIGHT-1",
        "party_type": "Employee",
        "party": "EMP-010",
        "amount": 0.0,
        "shipping_amount": 35.0,
        "payment_mode": "Deferred",
        "notes": "Courier freight accrual (unpaid online delivery, awaiting payment)",
        "idempotency_token": None,
        "is_partner_order": 0,
        "delivery_partner": None,
        "partner_invoice_ref": None,
    }

    def _module(self, invoice, outstanding):
        module, stub_frappe = _import_delivery_handling(invoice)
        module._publish_branch_event = MagicMock(return_value=[])
        module._get_collection_change_source_ct = MagicMock(return_value=dict(self.FREIGHT_CT))
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module._validate_collection_receipt = MagicMock()
        stub_frappe.db.get_value = MagicMock(return_value=outstanding)
        return module, stub_frappe

    def test_switch_to_cash_moves_receivable_and_arms_the_existing_courier_row(self):
        invoice = _FakeInvoice(name="INV-UO-1", grand_total=480.0,
                               custom_payment_method="Instapay",
                               custom_payment_confirmation_status="Awaiting Payment",
                               outstanding_amount=480.0)
        module, stub_frappe = self._module(invoice, 480.0)
        module.mark_payment_receipts_changed_for_invoice = MagicMock(return_value=[])
        module._get_receivable_account = MagicMock(return_value="Debtors - TC")
        module._get_courier_outstanding_account = MagicMock(return_value="Courier Outstanding - TC")
        module._create_payment_entry = MagicMock(return_value=SimpleNamespace(name="JV-UO-1"))

        result = module.change_payment_collection_method(
            invoice_name="INV-UO-1", new_method="Cash", pos_profile="Dokki")

        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "payment_collection_changed")
        # Debtors -> Courier Outstanding for the full outstanding amount.
        module._create_payment_entry.assert_called_once()
        args = module._create_payment_entry.call_args.args
        self.assertEqual(args[1], "Debtors - TC")
        self.assertEqual(args[2], "Courier Outstanding - TC")
        self.assertEqual(args[3], 480.0)
        # The EXISTING freight row is armed; a second row would double the freight.
        values = stub_frappe.db.set_value.call_args.args[2]
        self.assertEqual(values["amount"], 480.0)
        self.assertEqual(values["payment_mode"], "Cash")
        self.assertNotIn("shipping_amount", values)
        self.assertEqual(invoice.custom_payment_method, "Cash")
        self.assertEqual(invoice.custom_payment_confirmation_status, "Converted to Cash")

    def test_switch_to_another_online_method_moves_no_money_and_needs_no_receipt(self):
        invoice = _FakeInvoice(name="INV-UO-2", grand_total=810.0,
                               custom_payment_method="Instapay",
                               custom_payment_confirmation_status="Awaiting Payment",
                               outstanding_amount=810.0)
        module, stub_frappe = self._module(invoice, 810.0)
        module.mark_payment_receipts_changed_for_invoice = MagicMock(return_value=[])
        module._create_payment_entry = MagicMock()

        result = module.change_payment_collection_method(
            invoice_name="INV-UO-2", new_method="Mobile Wallet", pos_profile="Dokki")

        self.assertTrue(result["success"])
        module._create_payment_entry.assert_not_called()
        module._validate_collection_receipt.assert_not_called()
        values = stub_frappe.db.set_value.call_args.args[2]
        self.assertNotIn("amount", values)          # nothing collected yet
        self.assertEqual(values["payment_mode"], "Mobile Wallet")
        self.assertEqual(invoice.custom_payment_method, "Mobile Wallet")
        # Still waiting on a transfer -- confirm_online_payment books it.
        self.assertEqual(invoice.custom_payment_confirmation_status, "Awaiting Payment")

    def test_paid_order_with_zero_amount_row_reports_that_it_is_paid(self):
        invoice = _FakeInvoice(name="INV-UO-3", grand_total=660.0, outstanding_amount=0.0)
        module, _ = self._module(invoice, 0.0)
        module._get_real_customer_payment_entry = MagicMock(return_value={"name": "PE-9"})

        with self.assertRaises(Exception) as exc:
            module.change_payment_collection_method(
                invoice_name="INV-UO-3", new_method="Cash", pos_profile="Dokki")

        self.assertIn("real customer payment", str(exc.exception))

    def test_nothing_left_to_collect_says_so_plainly(self):
        invoice = _FakeInvoice(name="INV-UO-4", grand_total=660.0, outstanding_amount=0.0)
        module, _ = self._module(invoice, 0.0)

        with self.assertRaises(Exception) as exc:
            module.change_payment_collection_method(
                invoice_name="INV-UO-4", new_method="Cash", pos_profile="Dokki")

        self.assertIn("nothing left to collect", str(exc.exception))



class TestSettledFreightCollectionChange(unittest.TestCase):
    """The same unpaid-online order AFTER a shift close settled the courier's freight.

    That row is a FREIGHT row: its Settled flag records that the courier was paid HIS
    FEE, not that the customer paid anything. Until 2026-09-01 the lookup filtered
    ``status != 'Settled'``, so the action died the moment the fee was settled and the
    endpoint answered "No unsettled courier transaction was found for this invoice"
    while the customer still owed the whole grand total. Three production orders were
    stranded that way -- ACC-SINV-2026-18023 (810), -18025 (370) and -18026-2 (850) --
    all settled between 00:04 and 00:46 the same night.
    """

    SETTLED_FREIGHT_CT = {
        "name": "CT-FREIGHT-SETTLED",
        "party_type": "Employee",
        "party": "EMP-010",
        "amount": 0.0,
        "shipping_amount": 35.0,
        "payment_mode": "Deferred",
        "status": "Settled",
        "notes": "Courier freight accrual (unpaid online delivery, awaiting payment)",
        "idempotency_token": None,
        "is_partner_order": 0,
        "delivery_partner": None,
        "partner_invoice_ref": None,
    }

    def _module(self, invoice, outstanding, source_ct):
        module, stub_frappe = _import_delivery_handling(invoice)
        module._publish_branch_event = MagicMock(return_value=[])
        module._get_collection_change_source_ct = MagicMock(return_value=source_ct)
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module._validate_collection_receipt = MagicMock()
        module.mark_payment_receipts_changed_for_invoice = MagicMock(return_value=[])
        module._get_receivable_account = MagicMock(return_value="Debtors - TC")
        module._get_courier_outstanding_account = MagicMock(return_value="Courier Outstanding - TC")
        module.resolve_assignment_pos_profile = MagicMock(return_value="Dokki")
        module.get_pos_cash_account = MagicMock(return_value="Cash - Dokki - TC")
        module.validate_account_exists = MagicMock()
        stub_frappe.db.get_value = MagicMock(return_value=outstanding)
        return module, stub_frappe

    def _awaiting_invoice(self, name, total):
        return _FakeInvoice(
            name=name,
            grand_total=total,
            custom_payment_method="Instapay",
            custom_payment_confirmation_status="Awaiting Payment",
            outstanding_amount=total,
        )

    def test_switch_to_cash_books_it_in_the_branch_drawer_not_courier_outstanding(self):
        invoice = self._awaiting_invoice("ACC-SINV-2026-18023", 810.0)
        module, stub_frappe = self._module(
            invoice, 810.0, dict(self.SETTLED_FREIGHT_CT)
        )
        module._create_payment_entry = MagicMock(return_value=SimpleNamespace(name="PE-BRANCH-1"))

        result = module.change_payment_collection_method(
            invoice_name="ACC-SINV-2026-18023", new_method="Cash", pos_profile="Dokki")

        self.assertTrue(result["success"])
        self.assertEqual(result["collection_change_mode"], "unpaid_online_cash_at_branch")
        self.assertEqual(result["cash_account"], "Cash - Dokki - TC")

        # DR branch cash / CR Debtors -- the courier closed out, so this money can no
        # longer arrive as Courier Outstanding. No courier party on the posting.
        module._create_payment_entry.assert_called_once()
        args = module._create_payment_entry.call_args.args
        self.assertEqual(args[1], "Debtors - TC")
        self.assertEqual(args[2], "Cash - Dokki - TC")
        self.assertEqual(args[3], 810.0)
        self.assertEqual(len(args), 4)
        module._get_courier_outstanding_account.assert_not_called()

        # The drawer is the ORDER's branch, resolved from the invoice.
        module.resolve_assignment_pos_profile.assert_called_once()
        self.assertEqual(
            module.resolve_assignment_pos_profile.call_args.kwargs["requested_pos_profile"],
            "Dokki",
        )

        # A settled row is history: only its audit trail may still be written. Arming
        # its money columns would reopen a courier balance for cash he never carried.
        values = stub_frappe.db.set_value.call_args.args[2]
        self.assertNotIn("amount", values)
        self.assertNotIn("payment_mode", values)
        self.assertNotIn("status", values)
        self.assertIn("notes", values)
        self.assertEqual(values["idempotency_token"], result["idempotency_token"])

        self.assertEqual(invoice.custom_payment_method, "Cash")
        self.assertEqual(invoice.custom_payment_confirmation_status, "Converted to Cash")

    def test_switch_between_online_methods_moves_nothing_and_needs_no_receipt(self):
        invoice = self._awaiting_invoice("ACC-SINV-2026-18025", 370.0)
        module, stub_frappe = self._module(
            invoice, 370.0, dict(self.SETTLED_FREIGHT_CT)
        )
        module._create_payment_entry = MagicMock()

        result = module.change_payment_collection_method(
            invoice_name="ACC-SINV-2026-18025", new_method="Mobile Wallet", pos_profile="Dokki")

        self.assertTrue(result["success"])
        self.assertEqual(result["collection_change_mode"], "unpaid_online_retarget")
        module._create_payment_entry.assert_not_called()
        module._validate_collection_receipt.assert_not_called()
        values = stub_frappe.db.set_value.call_args.args[2]
        self.assertNotIn("amount", values)
        self.assertNotIn("payment_mode", values)
        self.assertEqual(invoice.custom_payment_method, "Mobile Wallet")
        # Still waiting on a transfer -- confirm_online_payment books it.
        self.assertEqual(invoice.custom_payment_confirmation_status, "Awaiting Payment")

    def test_no_courier_row_at_all_still_books_the_cash_at_the_branch(self):
        # The dispatch assigns no courier when the client sent none, and accrues no
        # freight when the territory charges none, so this order never had a row.
        invoice = self._awaiting_invoice("INV-NO-CT", 500.0)
        module, stub_frappe = self._module(invoice, 500.0, None)
        module._create_payment_entry = MagicMock(return_value=SimpleNamespace(name="PE-BRANCH-2"))
        stub_frappe.db.exists = MagicMock(return_value=False)

        result = module.change_payment_collection_method(
            invoice_name="INV-NO-CT", new_method="Cash", pos_profile="Dokki")

        self.assertTrue(result["success"])
        self.assertEqual(result["collection_change_mode"], "unpaid_online_cash_at_branch")
        self.assertIsNone(result["courier_transaction"])
        args = module._create_payment_entry.call_args.args
        self.assertEqual(args[2], "Cash - Dokki - TC")
        stub_frappe.db.set_value.assert_not_called()

    def test_a_row_the_lookup_refused_blocks_the_branch_posting(self):
        # A partner row is born Settled while its rider is still out with the order, and
        # a row for another courier is somebody else's cash position. Neither may be
        # silently bypassed into "the branch has the money".
        invoice = self._awaiting_invoice("INV-PARTNER", 640.0)
        module, stub_frappe = self._module(invoice, 640.0, None)
        module._create_payment_entry = MagicMock()
        stub_frappe.db.exists = MagicMock(return_value=True)

        with self.assertRaises(Exception) as exc:
            module.change_payment_collection_method(
                invoice_name="INV-PARTNER", new_method="Cash", pos_profile="Dokki")

        self.assertIn("No unsettled courier transaction", str(exc.exception))
        module._create_payment_entry.assert_not_called()

    def test_a_replayed_token_on_a_settled_row_does_not_choke_on_deferred(self):
        # The settled branch never rewrites payment_mode, so a retry replays a row that
        # still says "Deferred". Normalising that as a collection method throws
        # "Invalid collection method" -- the reused mode has to be checked first.
        replayed = dict(self.SETTLED_FREIGHT_CT, idempotency_token="TOKEN-1")
        invoice = self._awaiting_invoice("INV-REPLAY", 810.0)
        module, _ = self._module(invoice, 810.0, replayed)
        module._create_payment_entry = MagicMock()

        result = module.change_payment_collection_method(
            invoice_name="INV-REPLAY", new_method="Cash", pos_profile="Dokki",
            idempotency_token="TOKEN-1")

        self.assertTrue(result["success"])
        self.assertEqual(invoice.custom_payment_method, "Cash")
        module._create_payment_entry.assert_not_called()

    def test_a_paid_order_with_no_row_says_there_is_nothing_to_collect(self):
        invoice = _FakeInvoice(name="INV-PAID-NO-CT", grand_total=200.0, outstanding_amount=0.0)
        module, _ = self._module(invoice, 0.0, None)

        with self.assertRaises(Exception) as exc:
            module.change_payment_collection_method(
                invoice_name="INV-PAID-NO-CT", new_method="Cash", pos_profile="Dokki")

        self.assertIn("nothing left to collect", str(exc.exception))


class TestCollectionChangeSourceLookup(unittest.TestCase):
    """Which Courier Transaction a collection change is allowed to read and stamp."""

    UNSETTLED = {"name": "CT-LIVE", "party_type": "Employee", "party": "EMP-1", "amount": 0.0}
    SETTLED_FREIGHT = {"name": "CT-DONE", "party_type": "Employee", "party": "EMP-1", "amount": 0.0}

    def test_an_unsettled_row_wins_and_the_settled_fallback_is_never_queried(self):
        module, stub_frappe = _import_delivery_handling()
        stub_frappe.get_all = MagicMock(return_value=[dict(self.UNSETTLED)])

        row = module._get_collection_change_source_ct("INV-1", "Employee", "EMP-1")

        self.assertEqual(row["name"], "CT-LIVE")
        self.assertEqual(stub_frappe.get_all.call_count, 1)

    def test_the_fallback_takes_only_a_zero_amount_non_partner_settled_row(self):
        module, stub_frappe = _import_delivery_handling()
        stub_frappe.get_all = MagicMock(side_effect=[[], [dict(self.SETTLED_FREIGHT)]])

        row = module._get_collection_change_source_ct("INV-1", "Employee", "EMP-1")

        self.assertEqual(row["name"], "CT-DONE")
        filters = stub_frappe.get_all.call_args_list[1].kwargs["filters"]
        self.assertEqual(filters["status"], "Settled")
        # Settled money was already counted at a shift close; a partner row is born
        # Settled while its rider is still carrying the order.
        self.assertEqual(filters["amount"], ["<=", 0.0001])
        self.assertEqual(filters["is_partner_order"], 0)

    def test_nothing_usable_returns_none(self):
        module, stub_frappe = _import_delivery_handling()
        stub_frappe.get_all = MagicMock(return_value=[])

        self.assertIsNone(module._get_collection_change_source_ct("INV-1", None, None))

    def test_a_settled_row_cannot_carry_the_customers_cash(self):
        module, _ = _import_delivery_handling()

        self.assertFalse(module._courier_row_can_still_carry_cash({"status": "Settled"}))
        self.assertFalse(module._courier_row_can_still_carry_cash(None))
        self.assertTrue(module._courier_row_can_still_carry_cash({"status": "Unsettled"}))
        # An older row read without the column reads as still open, which is the
        # behaviour every caller had before the settled fallback existed.
        self.assertTrue(module._courier_row_can_still_carry_cash({}))


class TestReconcilePaymentConfirmation(unittest.TestCase):
    """The backstop that re-aligns an awaiting order with its own ledger.

    Its one dangerous mistake would be treating "nothing outstanding" as "the
    customer paid". An unpaid RETURN knocks the receivable off with a journal
    entry and ``mark_courier_outstanding`` moves it to Courier Outstanding while
    the cash is still in the courier's pocket -- both zero the outstanding and
    neither is a collection.
    """

    def _module(self, invoice, outstanding, status="Awaiting Payment"):
        module, stub_frappe = _import_delivery_handling(invoice)
        stub_frappe.db.get_value = MagicMock(return_value={
            "name": invoice.name,
            "company": "Test Company",
            "docstatus": 1,
            "outstanding_amount": outstanding,
            "grand_total": 480.0,
            "custom_payment_confirmation_status": status,
            "custom_payment_method": "Instapay",
            "custom_kanban_profile": "Dokki",
            "pos_profile": "Dokki",
        })
        return module, stub_frappe

    def test_zero_outstanding_without_a_customer_payment_is_not_a_collection(self):
        invoice = _FakeInvoice(name="INV-RETURNED")
        module, _ = self._module(invoice, 0.0)
        # A returned order: the receivable was knocked off by a journal entry,
        # so there is no real customer Payment Entry.
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module.retire_pending_payment_receipts = MagicMock(return_value=[])
        module.ensure_pending_payment_receipt = MagicMock(
            return_value=_pending_receipt(None)
        )
        module.update_submitted_sales_invoice_fields = MagicMock()
        module._log_reconcile_note = MagicMock()

        result = module.reconcile_payment_confirmation("INV-RETURNED")

        self.assertIsNone(result)
        # Never claim money arrived, and never drop the receipt that is the
        # only remaining trace of the order on the receipts list.
        module.update_submitted_sales_invoice_fields.assert_not_called()
        module.retire_pending_payment_receipts.assert_not_called()
        # Left awaiting on purpose, but not silently: it is invisible on the
        # receipts list and would otherwise be re-swept and skipped for ever.
        module._log_reconcile_note.assert_called_once()
        self.assertIn(
            "no customer payment entry",
            module._log_reconcile_note.call_args.args[1],
        )

    def test_a_real_customer_payment_confirms_and_retires_the_pending_receipt(self):
        invoice = _FakeInvoice(name="INV-PAID")
        module, _ = self._module(invoice, 0.0)
        module._get_real_customer_payment_entry = MagicMock(
            return_value={"name": "ACC-PAY-0001", "paid_to": "Bank Account - TC"}
        )
        module.retire_pending_payment_receipts = MagicMock(return_value=["PPR-1"])
        module.update_submitted_sales_invoice_fields = MagicMock()

        result = module.reconcile_payment_confirmation("INV-PAID")

        self.assertEqual(result["action"], "confirmed_from_ledger")
        self.assertEqual(result["payment_entry"], "ACC-PAY-0001")
        fields = module.update_submitted_sales_invoice_fields.call_args.args[1]
        self.assertEqual(
            fields["custom_payment_confirmation_status"], "Payment Confirmed"
        )
        module.retire_pending_payment_receipts.assert_called_once_with("INV-PAID")

    def test_an_unpaid_order_gets_the_receipt_that_makes_it_visible(self):
        invoice = _FakeInvoice(name="INV-OWING")
        module, _ = self._module(invoice, 480.0)
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module.ensure_pending_payment_receipt = MagicMock(
            return_value=_pending_receipt("PPR-NEW", created=True)
        )
        module.update_submitted_sales_invoice_fields = MagicMock()

        result = module.reconcile_payment_confirmation("INV-OWING")

        self.assertEqual(result["action"], "receipt_filed")
        self.assertEqual(result["receipt"], "PPR-NEW")
        module.update_submitted_sales_invoice_fields.assert_not_called()

    def test_an_order_whose_receipt_is_already_on_file_is_not_reported(self):
        """A sweep that wrote nothing must not read as a sweep that healed.

        The hourly job counts every truthy result as an order it reconciled and
        commits on the strength of that count. While a reused receipt came back
        indistinguishable from a filed one, production logged "reconciled 23/23"
        every hour and committed an empty transaction -- verified 2026-09-09,
        second pass, zero new rows.
        """
        invoice = _FakeInvoice(name="INV-OWING-STEADY")
        module, _ = self._module(invoice, 480.0)
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module.ensure_pending_payment_receipt = MagicMock(
            return_value=_pending_receipt("PPR-EXISTING")
        )
        module.update_submitted_sales_invoice_fields = MagicMock()
        module._log_reconcile_note = MagicMock()

        self.assertIsNone(module.reconcile_payment_confirmation("INV-OWING-STEADY"))
        module.update_submitted_sales_invoice_fields.assert_not_called()
        # Nothing happened and nothing is wrong, so there is nothing to say
        # either -- an hourly Error Log per order would be the same noise in a
        # different place.
        module._log_reconcile_note.assert_not_called()

    def test_a_re_synced_receipt_amount_is_reported_as_its_own_action(self):
        """A drifted amount really was rewritten, so the sweep did do work.

        Reported separately from a filed receipt because the two need different
        follow-up: one made an order visible, the other repaired an order that
        could not be confirmed at all after a post-submit re-rate.
        """
        invoice = _FakeInvoice(name="INV-RERATED")
        module, _ = self._module(invoice, 525.0)
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module.ensure_pending_payment_receipt = MagicMock(
            return_value=_pending_receipt("PPR-EXISTING", amount_synced=True)
        )
        module.update_submitted_sales_invoice_fields = MagicMock()

        result = module.reconcile_payment_confirmation("INV-RERATED")

        self.assertEqual(result["action"], "receipt_amount_synced")
        self.assertEqual(result["receipt"], "PPR-EXISTING")
        module.update_submitted_sales_invoice_fields.assert_not_called()

    def test_an_owed_order_whose_receipt_cannot_be_filed_says_so(self):
        """The one state that would otherwise be silent AND wrong.

        Money outstanding, a method that does take a receipt, and no row filed
        -- an invoice with no POS profile to file it against, or a failed
        insert. The only other trace is ``frappe.logger().error``, which is not
        retrievable on these servers, and now that a quiet sweep means
        "everything is in step" this had to stop being quiet.
        """
        invoice = _FakeInvoice(name="INV-NO-PROFILE")
        module, _ = self._module(invoice, 480.0)
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module.ensure_pending_payment_receipt = MagicMock(
            return_value=_pending_receipt(None)
        )
        module.receipt_method_label = MagicMock(return_value="InstaPay")
        module.update_submitted_sales_invoice_fields = MagicMock()
        module._log_reconcile_note = MagicMock()

        self.assertIsNone(module.reconcile_payment_confirmation("INV-NO-PROFILE"))

        module.update_submitted_sales_invoice_fields.assert_not_called()
        module._log_reconcile_note.assert_called_once()
        self.assertIn(
            "none could be filed", module._log_reconcile_note.call_args.args[1]
        )

    def test_an_order_that_is_not_awaiting_is_left_alone(self):
        invoice = _FakeInvoice(name="INV-DONE")
        module, _ = self._module(invoice, 0.0, status="Payment Confirmed")
        module._get_real_customer_payment_entry = MagicMock(return_value={"name": "PE-1"})
        module.update_submitted_sales_invoice_fields = MagicMock()
        module.retire_pending_payment_receipts = MagicMock()

        self.assertIsNone(module.reconcile_payment_confirmation("INV-DONE"))
        module.update_submitted_sales_invoice_fields.assert_not_called()
        module.retire_pending_payment_receipts.assert_not_called()


class TestReceiptCollectionClassifier(unittest.TestCase):
    """Where an order's money sits, read off the ledger rather than a flag.

    This is what decides whether confirming a transfer screenshot posts a
    Payment Entry, moves the money off the courier, or refuses. Getting a shape
    wrong here is a money defect in both directions: a second Payment Entry
    against an order whose receivable already moved, or a receipt stamped while
    the courier is still recorded as holding the cash.
    """

    def _classify(self, row, *, cts=None, real_pe=None):
        module, stub_frappe = _import_delivery_handling()
        stub_frappe.db.get_value = MagicMock(return_value=row)
        stub_frappe.get_all = MagicMock(return_value=list(cts or []))
        module._get_real_customer_payment_entry = MagicMock(return_value=real_pe)
        return module.classify_receipt_collection("INV-001")

    def _row(self, **over):
        row = {
            "name": "INV-001",
            "company": "Test Company",
            "docstatus": 1,
            "outstanding_amount": 480.0,
            "grand_total": 480.0,
            "custom_payment_method": "Cash",
            "custom_payment_confirmation_status": "",
            "custom_kanban_profile": "Dokki",
            "pos_profile": "Dokki",
        }
        row.update(over)
        return row

    def test_an_awaiting_order_is_awaiting(self):
        plan = self._classify(self._row(custom_payment_confirmation_status="Awaiting Payment"))
        self.assertEqual(plan["shape"], "awaiting")

    def test_an_unpaid_order_that_was_never_stamped_is_unpaid(self):
        plan = self._classify(self._row())
        self.assertEqual(plan["shape"], "unpaid")
        self.assertEqual(plan["outstanding"], 480.0)

    def test_money_parked_on_an_unsettled_courier_row_is_courier_cash(self):
        """Orders 17450 and 17453: dispatched as cash, rider not settled yet."""
        plan = self._classify(
            self._row(outstanding_amount=0.0),
            cts=[{"name": "CT-1", "status": "Unsettled", "amount": 480.0, "payment_mode": "Deferred"}],
        )
        self.assertEqual(plan["shape"], "courier_cash")
        self.assertEqual(plan["courier_transaction"], "CT-1")
        self.assertEqual(plan["amount"], 480.0)

    def test_money_already_settled_as_cash_is_settled_cash(self):
        """Order 17417: the branch till counted it, so only a correction moves it."""
        plan = self._classify(
            self._row(outstanding_amount=0.0),
            cts=[{"name": "CT-2", "status": "Settled", "amount": 1020.0, "payment_mode": "Deferred"}],
        )
        self.assertEqual(plan["shape"], "settled_cash")

    def test_a_row_already_switched_online_needs_nothing(self):
        """A collection change (or a hand correction stamped the same way) ran."""
        plan = self._classify(
            self._row(outstanding_amount=0.0),
            cts=[{"name": "CT-3", "status": "Unsettled", "amount": 480.0, "payment_mode": "Instapay"}],
        )
        self.assertEqual(plan["shape"], "none")
        self.assertTrue(plan["already_paid"])

    def test_a_real_customer_payment_needs_nothing(self):
        plan = self._classify(self._row(), real_pe={"name": "ACC-PAY-1"})
        self.assertEqual(plan["shape"], "none")

    def test_an_order_converted_to_cash_needs_nothing(self):
        plan = self._classify(self._row(custom_payment_confirmation_status="Converted to Cash"))
        self.assertEqual(plan["shape"], "none")

    def test_a_draft_or_missing_invoice_needs_nothing(self):
        self.assertEqual(self._classify(self._row(docstatus=0))["shape"], "none")
        self.assertEqual(self._classify(None)["shape"], "none")

    def test_an_on_account_order_is_left_to_the_credit_ledger(self):
        """Collecting a credit order here would hide the rest of the debt.

        ``record_credit_payment`` allocates FIFO across everything the customer
        owes, and the credit ledger finds those debts partly BY the Credit
        payment method -- which booking here would overwrite.
        """
        plan = self._classify(self._row(custom_payment_method="Credit"))
        self.assertEqual(plan["shape"], "none")
        self.assertTrue(plan["credit_order"])

    def test_a_relabelled_credit_order_is_still_a_credit_order(self):
        """The payment method is mutable; the terms stamp is not.

        A manager tapping "Change collection method -> Instapay" on a credit
        order rewrites the method without moving a pound, so matching only the
        method would let the next screenshot book the whole balance against
        this one invoice.
        """
        plan = self._classify(
            self._row(custom_payment_method="Instapay", custom_credit_terms_days=30)
        )
        self.assertEqual(plan["shape"], "none")
        self.assertTrue(plan["credit_order"])

    def test_a_freight_only_row_is_not_customer_cash(self):
        """The unpaid-online dispatch writes amount 0: the rider carries nothing."""
        plan = self._classify(
            self._row(outstanding_amount=0.0),
            cts=[{"name": "CT-4", "status": "Unsettled", "amount": 0.0, "payment_mode": "Deferred"}],
        )
        self.assertEqual(plan["shape"], "none")


class TestPaymentMethodAlignment(unittest.TestCase):
    """A screenshot on file is what says the order is being paid by transfer.

    Every reader downstream keys off the INVOICE's method, so dispatching an
    order as awaiting-payment while leaving it declared Cash would create an
    order nobody could ever confirm.
    """

    def _module(self, invoice, receipt=None):
        module, _ = _import_delivery_handling(invoice)
        module.get_live_transfer_receipt = MagicMock(return_value=receipt)
        module.update_submitted_sales_invoice_fields = MagicMock()
        return module

    def test_a_cash_declared_order_adopts_the_receipt_method(self):
        invoice = _FakeInvoice(custom_payment_method="Cash")
        module = self._module(invoice, {"name": "PPR-1", "payment_method": "InstaPay"})

        self.assertEqual(module._align_payment_method_with_transfer_receipt(invoice), "Instapay")
        module.update_submitted_sales_invoice_fields.assert_called_once_with(
            invoice, {"custom_payment_method": "Instapay"}
        )

    def test_a_wallet_receipt_maps_to_the_invoice_spelling(self):
        invoice = _FakeInvoice(custom_payment_method="Cash")
        module = self._module(invoice, {"name": "PPR-2", "payment_method": "Wallet"})

        self.assertEqual(
            module._align_payment_method_with_transfer_receipt(invoice), "Mobile Wallet"
        )

    def test_an_order_already_online_is_left_alone(self):
        invoice = _FakeInvoice(custom_payment_method="Instapay")
        module = self._module(invoice, {"name": "PPR-3", "payment_method": "InstaPay"})

        self.assertEqual(module._align_payment_method_with_transfer_receipt(invoice), "Instapay")
        module.update_submitted_sales_invoice_fields.assert_not_called()

    def test_no_receipt_changes_nothing(self):
        invoice = _FakeInvoice(custom_payment_method="Cash")
        module = self._module(invoice, None)

        self.assertIsNone(module._align_payment_method_with_transfer_receipt(invoice))
        module.update_submitted_sales_invoice_fields.assert_not_called()


class TestConfirmOnlinePaymentFromTheReceipt(unittest.TestCase):
    """Confirming collects for an order that was never stamped Awaiting Payment.

    Woo declares every non-gateway order ``cod``, so an order the customer paid
    by transfer is still declared Cash when a manager confirms the screenshot.
    Refusing there -- which is what this function did -- is what left the money
    recorded as the courier's cash.
    """

    def _module(self, invoice, *, outstanding=480.0, receipt_method="InstaPay"):
        module, stub_frappe = _import_delivery_handling(invoice)

        def _get_value(doctype, name, field=None, **kwargs):
            if doctype == "POS Payment Receipt":
                return receipt_method
            if doctype == "Sales Invoice" and field == "outstanding_amount":
                return outstanding
            return None

        stub_frappe.db.get_value = MagicMock(side_effect=_get_value)
        module._ensure_payment_receipt_confirm_access = MagicMock()
        module._get_real_customer_payment_entry = MagicMock(return_value=None)
        module.ensure_uploaded_payment_receipt = MagicMock(
            return_value={"name": "PPR-1", "status": "Unconfirmed"}
        )
        module._confirm_receipt_record = MagicMock()
        module._get_receivable_account = MagicMock(return_value="Debtors - TC")
        module._get_online_collection_account = MagicMock(return_value="Bank Account - TC")
        module._create_payment_entry = MagicMock(
            return_value=SimpleNamespace(name="ACC-PAY-NEW")
        )
        module.update_submitted_sales_invoice_fields = MagicMock()
        module._publish_branch_event = MagicMock()
        return module, stub_frappe

    def test_books_a_cash_declared_order_into_the_receipt_ledger(self):
        invoice = _FakeInvoice(
            custom_payment_method="Cash",
            custom_payment_confirmation_status="",
            outstanding_amount=480.0,
            grand_total=480.0,
        )
        module, _ = self._module(invoice)

        result = module.confirm_online_payment("INV-CHANGE-001", "Dokki", receipt_name="PPR-1")

        self.assertEqual(result["payment_entry"], "ACC-PAY-NEW")
        self.assertEqual(result["method"], "Instapay")
        # The ledger comes from the RECEIPT, not from the stale declaration.
        self.assertEqual(module._get_online_collection_account.call_args[0][0], "Instapay")
        self.assertEqual(
            module.ensure_uploaded_payment_receipt.call_args.kwargs["payment_method"], "InstaPay"
        )
        # ... and the invoice is corrected, or every screen keeps saying Cash.
        fields = module.update_submitted_sales_invoice_fields.call_args[0][1]
        self.assertEqual(fields["custom_payment_method"], "Instapay")
        self.assertEqual(fields["custom_payment_confirmation_status"], "Payment Confirmed")

    def test_an_awaiting_order_still_uses_its_own_declared_method(self):
        invoice = _FakeInvoice(
            custom_payment_method="Mobile Wallet",
            custom_payment_confirmation_status="Awaiting Payment",
            outstanding_amount=480.0,
            grand_total=480.0,
        )
        module, _ = self._module(invoice, receipt_method="Wallet")

        result = module.confirm_online_payment("INV-CHANGE-001", "Dokki", receipt_name="PPR-1")

        self.assertEqual(result["method"], "Mobile Wallet")
        fields = module.update_submitted_sales_invoice_fields.call_args[0][1]
        self.assertNotIn("custom_payment_method", fields)

    def test_refuses_a_non_awaiting_order_with_no_receipt(self):
        invoice = _FakeInvoice(
            custom_payment_method="Cash", custom_payment_confirmation_status=""
        )
        module, _ = self._module(invoice)

        with self.assertRaises(Exception) as exc:
            module.confirm_online_payment("INV-CHANGE-001", "Dokki")

        self.assertIn("needs the customer's receipt", str(exc.exception))
        module._create_payment_entry.assert_not_called()

    def test_refuses_to_double_book_money_sitting_with_the_courier(self):
        """Zero outstanding here means the receivable MOVED, not that it was paid."""
        invoice = _FakeInvoice(
            custom_payment_method="Cash",
            custom_payment_confirmation_status="",
            outstanding_amount=0.0,
        )
        module, _ = self._module(invoice, outstanding=0.0)
        module.classify_receipt_collection = MagicMock(
            return_value={"shape": "courier_cash", "invoice": "INV-CHANGE-001"}
        )

        with self.assertRaises(Exception) as exc:
            module.confirm_online_payment("INV-CHANGE-001", "Dokki", receipt_name="PPR-1")

        self.assertIn("cash with the courier", str(exc.exception))
        module._create_payment_entry.assert_not_called()

    def test_the_duplicate_payment_check_is_a_locking_read(self):
        """The invoice row lock does not refresh the reader's snapshot.

        MariaDB runs at REPEATABLE READ and Frappe sets no isolation level, so
        the loser of two concurrent confirmations blocks on the invoice lock,
        wakes after the winner commits, and a PLAIN read still answers "no
        payment yet" from its own older snapshot -- posting a second Payment
        Entry for the same transfer. Both balance, so nothing downstream ever
        notices. Only a locking read sees the winner's row.
        """
        invoice = _FakeInvoice(
            custom_payment_method="Cash",
            custom_payment_confirmation_status="",
            outstanding_amount=480.0,
            grand_total=480.0,
        )
        module, _ = self._module(invoice)

        module.confirm_online_payment("INV-CHANGE-001", "Dokki", receipt_name="PPR-1")

        self.assertTrue(
            module._get_real_customer_payment_entry.call_args.kwargs.get("for_update"),
            "confirm_online_payment must ask for the LOCKING duplicate check",
        )

    def test_refuses_an_on_account_order(self):
        invoice = _FakeInvoice(
            custom_payment_method="Credit", custom_payment_confirmation_status=""
        )
        module, _ = self._module(invoice)

        with self.assertRaises(Exception) as exc:
            module.confirm_online_payment("INV-CHANGE-001", "Dokki", receipt_name="PPR-1")

        self.assertIn("on-account order", str(exc.exception))
        module._create_payment_entry.assert_not_called()

    def test_refuses_an_order_converted_to_cash(self):
        invoice = _FakeInvoice(
            custom_payment_method="Cash",
            custom_payment_confirmation_status="Converted to Cash",
        )
        module, _ = self._module(invoice)

        with self.assertRaises(Exception) as exc:
            module.confirm_online_payment("INV-CHANGE-001", "Dokki", receipt_name="PPR-1")

        self.assertIn("converted to cash", str(exc.exception))
        module._create_payment_entry.assert_not_called()


class TestDispatchKeepsTransferOrdersOffTheCourier(unittest.TestCase):
    """The legacy OFD entry point is the one the kanban card actually calls.

    ``dispatch_settlement`` has routed online orders away from the cash path
    since 2026-07-20, but this function never learned the rule: it only looked
    correct because the CLIENT sends a different endpoint for orders it believes
    are InstaPay. An order paid by transfer but declared Cash (every Woo order
    is) therefore arrived here and had its receivable moved onto the rider.
    """

    def _module(self, invoice, receipt=None):
        module, stub_frappe = _import_delivery_handling(invoice)
        module.get_live_transfer_receipt = MagicMock(return_value=receipt)
        module.handle_unpaid_online_deliver_unconfirmed = MagicMock(
            return_value={"success": True, "mode": "unpaid_online_deliver_unconfirmed"}
        )
        module.mark_courier_outstanding = MagicMock(return_value={"success": True, "mode": "cash"})
        module.handle_credit_deliver_on_account = MagicMock(return_value={"success": True})
        module.resolve_assignment_pos_profile = MagicMock(return_value="Dokki")
        module.resolve_courier_delivery_partner = MagicMock(return_value=None)
        module.assert_courier_matches_pos_profile = MagicMock(return_value={"delivery_partner": None})
        module._guard_courier_money_action_by_name = MagicMock()
        stub_frappe.db.get_value = MagicMock(return_value=480.0)
        return module, stub_frappe

    def _dispatch(self, module):
        return module.handle_out_for_delivery_transition(
            "INV-CHANGE-001", "courier", "later", "Dokki",
            party_type="Employee", party="HR-EMP-1",
        )

    def test_a_cash_declared_order_with_a_receipt_is_rerouted(self):
        invoice = _FakeInvoice(custom_payment_method="Cash", outstanding_amount=480.0)
        module, _ = self._module(invoice, {"name": "PPR-1", "payment_method": "InstaPay"})

        result = self._dispatch(module)

        module.handle_unpaid_online_deliver_unconfirmed.assert_called_once()
        module.mark_courier_outstanding.assert_not_called()
        self.assertEqual(result["mode"], "unpaid_online_deliver_unconfirmed")

    def test_a_declared_instapay_order_is_rerouted_too(self):
        """The two entry points must agree, whichever endpoint the client used."""
        invoice = _FakeInvoice(custom_payment_method="Instapay", outstanding_amount=480.0)
        module, _ = self._module(invoice, None)

        self._dispatch(module)

        module.handle_unpaid_online_deliver_unconfirmed.assert_called_once()
        module.mark_courier_outstanding.assert_not_called()

    def test_a_plain_cash_order_still_goes_to_the_courier(self):
        invoice = _FakeInvoice(custom_payment_method="Cash", outstanding_amount=480.0)
        module, _ = self._module(invoice, None)

        self._dispatch(module)

        module.mark_courier_outstanding.assert_called_once()
        module.handle_unpaid_online_deliver_unconfirmed.assert_not_called()

    def test_a_gateway_order_is_not_stamped_awaiting_a_transfer(self):
        """Card/gateway orders are prepaid: stamping one arms the hourly alarm."""
        invoice = _FakeInvoice(custom_payment_method="Kashier Card", outstanding_amount=480.0)
        module, _ = self._module(invoice, None)

        self._dispatch(module)

        module.mark_courier_outstanding.assert_called_once()
        module.handle_unpaid_online_deliver_unconfirmed.assert_not_called()


class TestMarkCourierOutstandingBackstop(unittest.TestCase):
    """The one function where a customer's debt becomes the rider's cash."""

    def _module(self, receipt):
        invoice = _FakeInvoice(custom_payment_method="Cash", custom_is_pickup=0, custom_no_courier=0)
        module, stub_frappe = _import_delivery_handling(invoice)
        module.get_live_transfer_receipt = MagicMock(return_value=receipt)
        return module, stub_frappe

    def test_refuses_an_order_that_carries_a_transfer_receipt(self):
        module, _ = self._module({"name": "PPR-1", "payment_method": "InstaPay"})

        with self.assertRaises(Exception) as exc:
            module._mark_courier_outstanding_locked(
                "INV-CHANGE-001", None, "Employee", "HR-EMP-1"
            )

        self.assertIn("transfer receipt", str(exc.exception))

    def test_the_deliberate_cash_conversion_is_allowed_through(self):
        """``convert_online_order_to_cod`` means it: the rider collects cash."""
        module, _ = self._module({"name": "PPR-1", "payment_method": "InstaPay"})

        with self.assertRaises(Exception) as exc:
            module._mark_courier_outstanding_locked(
                "INV-CHANGE-001", None, "Employee", "HR-EMP-1",
                allow_transfer_receipt=True,
            )

        # It gets past the backstop and fails later on the stubbed ledger --
        # what matters is that the refusal above is not the reason.
        self.assertNotIn("transfer receipt", str(exc.exception))


class TestRealCustomerPaymentLookup(unittest.TestCase):
    """The one question every money path asks: has this order really been paid?"""

    def _module(self):
        module, stub_frappe = _import_delivery_handling()
        module._get_courier_outstanding_account = MagicMock(
            return_value="Courier Outstanding - TC"
        )
        return module, stub_frappe

    def test_it_locks_only_when_asked(self):
        module, stub_frappe = self._module()
        stub_frappe.db.sql = MagicMock(return_value=[])

        module._get_real_customer_payment_entry("INV-1", "Test Company")
        plain = stub_frappe.db.sql.call_args[0][0]
        self.assertNotIn("FOR UPDATE", plain.upper())

        module._get_real_customer_payment_entry("INV-1", "Test Company", for_update=True)
        locking = stub_frappe.db.sql.call_args[0][0]
        self.assertIn("FOR UPDATE", locking.upper())

    def test_the_shared_reader_locks_only_when_asked(self):
        """One reader backs every duplicate-payment guard in the module.

        The dispatch guard and the sales-partner cash guard reach it through
        the same keyword; keeping the locking behaviour in one place is what
        stops it from holding in one guard and quietly not in the next.
        """
        module, stub_frappe = self._module()
        stub_frappe.db.sql = MagicMock(return_value=[])

        module._submitted_payment_entries_for_invoice("INV-1")
        plain = stub_frappe.db.sql.call_args[0][0]
        self.assertIn("tabPayment Entry", plain)
        self.assertNotIn("FOR UPDATE", plain.upper())

        module._submitted_payment_entries_for_invoice("INV-1", for_update=True)
        locking = stub_frappe.db.sql.call_args[0][0]
        self.assertIn("FOR UPDATE", locking.upper())

    def test_the_reader_answers_with_an_empty_list_not_none(self):
        """Every call site iterates the result directly."""
        module, stub_frappe = self._module()
        stub_frappe.db.sql = MagicMock(return_value=None)

        self.assertEqual(module._submitted_payment_entries_for_invoice("INV-1"), [])

    def test_courier_outstanding_is_not_a_customer_payment(self):
        """Moving the receivable onto the rider collects nothing from anybody."""
        module, stub_frappe = self._module()
        stub_frappe.db.sql = MagicMock(
            return_value=[{"name": "ACC-PAY-1", "paid_to": "Courier Outstanding - TC",
                           "payment_type": "Receive", "mode_of_payment": None}]
        )

        self.assertIsNone(
            module._get_real_customer_payment_entry("INV-1", "Test Company")
        )

    def test_a_refund_is_not_a_customer_payment(self):
        """Only money coming IN counts. A Pay entry against the invoice is a
        refund going the other way."""
        module, stub_frappe = self._module()
        stub_frappe.db.sql = MagicMock(
            return_value=[{"name": "ACC-PAY-3", "paid_to": "Bank Account - TC",
                           "payment_type": "Pay", "mode_of_payment": "Instapay"}]
        )

        self.assertIsNone(
            module._get_real_customer_payment_entry("INV-1", "Test Company")
        )

    def test_a_bank_payment_is_a_customer_payment(self):
        module, stub_frappe = self._module()
        stub_frappe.db.sql = MagicMock(
            return_value=[{"name": "ACC-PAY-2", "paid_to": "Bank Account - TC",
                           "payment_type": "Receive", "mode_of_payment": "Instapay"}]
        )

        found = module._get_real_customer_payment_entry("INV-1", "Test Company")
        self.assertEqual((found or {}).get("name"), "ACC-PAY-2")

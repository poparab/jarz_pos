"""Tests: Invalid-token send failures do NOT spam the Error Log.

Covers PROD-POS-002 Phase 3 — 100 consecutive send attempts to the same stale
token should produce zero Error Log rows (expected failures route to
frappe.logger("fcm").info) and trigger _disable_token on the first failure.
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _load_module():
    fake_frappe = types.ModuleType("frappe")
    fake_frappe._ = lambda x: x
    fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
    fake_frappe.throw = MagicMock(side_effect=Exception)
    fake_frappe.log_error = MagicMock()
    fake_frappe.get_traceback = MagicMock(return_value="traceback")
    # Use a stable logger object so .info call_count is tracked
    fake_fcm_logger = SimpleNamespace(info=MagicMock())
    fake_frappe.logger = MagicMock(return_value=fake_fcm_logger)
    fake_frappe.get_all = MagicMock(return_value=[])
    fake_frappe.get_doc = MagicMock()
    fake_frappe.db = SimpleNamespace(set_value=MagicMock(), count=MagicMock(return_value=0))
    fake_frappe.session = SimpleNamespace(user="operator@example.com")
    fake_frappe.utils = SimpleNamespace(
        now_datetime=MagicMock(return_value="2026-05-12T00:00:00"),
        now=MagicMock(return_value="2026-05-12 00:00:00"),
        today=MagicMock(return_value="2026-05-12"),
        nowtime=MagicMock(return_value="00:00:00"),
        add_to_date=MagicMock(return_value="2026-05-12 00:00:00"),
        get_datetime=MagicMock(return_value="2026-05-12 00:00:00"),
    )
    fake_frappe.conf = {}
    fake_frappe.local = SimpleNamespace(conf={}, site="frontend")
    fake_frappe.get_site_path = MagicMock(return_value="/site/private/files")
    fake_frappe.publish_realtime = MagicMock()
    fake_frappe.msgprint = MagicMock()
    fake_frappe.delete_doc = MagicMock()

    # Firebase — SDK initialized successfully but every send raises an invalid-token error.
    fake_firebase = types.ModuleType("firebase_admin")
    fake_firebase.get_app = MagicMock()  # Does NOT raise — signals already-initialized
    fake_firebase.initialize_app = MagicMock()

    fake_creds = types.ModuleType("firebase_admin.credentials")
    fake_creds.Certificate = MagicMock(return_value=MagicMock())

    class _UnregisteredError(Exception):
        pass

    class _SenderIdMismatchError(Exception):
        pass

    fake_messaging = types.ModuleType("firebase_admin.messaging")
    fake_messaging.UnregisteredError = _UnregisteredError
    fake_messaging.SenderIdMismatchError = _SenderIdMismatchError
    fake_messaging.Notification = MagicMock()
    fake_messaging.AndroidNotification = MagicMock()
    fake_messaging.AndroidConfig = MagicMock()
    # Every send raises "Requested entity was not found." — the most common prod variant.
    fake_messaging.send = MagicMock(
        side_effect=Exception("Requested entity was not found.")
    )

    # Message mock must expose .token attribute
    fake_message_instance = SimpleNamespace(token="stale-token-that-never-works")
    fake_messaging.Message = MagicMock(return_value=fake_message_instance)

    fake_firebase.credentials = fake_creds
    fake_firebase.messaging = fake_messaging

    patches = {
        "frappe": fake_frappe,
        "firebase_admin": fake_firebase,
        "firebase_admin.credentials": fake_creds,
        "firebase_admin.messaging": fake_messaging,
    }
    with patch.dict(sys.modules, patches):
        sys.modules.pop("jarz_pos.api.notifications", None)
        mod = importlib.import_module("jarz_pos.api.notifications")
        importlib.reload(mod)

    # Simulate device rows for _disable_token
    def _get_all_side_effect(doctype, filters=None, fields=None, **kwargs):
        if doctype == "Jarz Mobile Device":
            return [{"name": "DEV-STALE", "enabled": 1}]
        return []

    mod.frappe.get_all.side_effect = _get_all_side_effect

    dev = SimpleNamespace(name="DEV-STALE", enabled=1)
    mod.frappe.get_doc.return_value = dev

    return mod, fake_frappe, fake_fcm_logger


class TestFcmLoggingNoErrorSpam(unittest.TestCase):
    def setUp(self):
        self.mod, self.fake_frappe, self.fake_fcm_logger = _load_module()

    def _make_data(self):
        return {
            "type": "new_invoice",
            "invoice_id": "INV-0001",
            "title": "New Order",
            "body": "1 item",
            "customer_name": "Test",
            "pos_profile": "Nasr",
            "grand_total": "100.00",
            "sales_invoice_state": "Received",
            "timestamp": "2026-05-12T00:00:00",
            "requires_acceptance": "1",
            "item_summary": "Widget x 1",
            "branch_display": "Nasr",
            "total_display": "100.00",
            "item_count": "1",
            "items": "[]",
            "notification_id": "INV-0001",
        }

    def test_100_invalid_sends_produce_zero_fcm_send_error_log_rows(self):
        """100 pushes to a stale token must not produce any 'FCM Send Error' Error Log entries."""
        data = self._make_data()

        for _ in range(100):
            self.mod._send_fcm_notifications(["stale-token-that-never-works"], data)

        error_log_calls = [
            c for c in self.fake_frappe.log_error.call_args_list
            if len(c.args) >= 2 and c.args[1] == "FCM Send Error"
        ]
        self.assertEqual(len(error_log_calls), 0, msg=(
            f"Expected 0 'FCM Send Error' Error Log rows for known-invalid token, "
            f"got {len(error_log_calls)}: {error_log_calls}"
        ))

    def test_100_invalid_sends_route_to_fcm_logger_info(self):
        """Expected invalid-token failures are logged via frappe.logger('fcm').info."""
        data = self._make_data()

        for _ in range(100):
            self.mod._send_fcm_notifications(["stale-token-that-never-works"], data)

        self.assertGreater(
            self.fake_fcm_logger.info.call_count, 0,
            msg="Expected at least one frappe.logger('fcm').info call for invalid token"
        )

    def test_token_disabled_after_first_failure(self):
        """_disable_token is called; the device row gets set to enabled=0."""
        data = self._make_data()

        self.mod._send_fcm_notifications(["stale-token-that-never-works"], data)

        set_value_calls = [
            c for c in self.fake_frappe.db.set_value.call_args_list
            if c.args[0] == "Jarz Mobile Device" and c.args[1] == "DEV-STALE"
        ]
        self.assertGreater(len(set_value_calls), 0, msg="Expected db.set_value to disable DEV-STALE")

    def test_unexpected_error_logs_to_error_log_once(self):
        """An unexpected error (e.g. quota) logs to Error Log, but only once per token."""
        quota_exc = Exception("Quota exceeded for quota metric 'cloudfunctions.googleapis.com'")
        self.mod._FCM_LOGGED_ERROR_TOKENS.discard("some-token")
        result = self.mod._new_fcm_send_result(["some-token"])

        for _ in range(10):
            self.mod._record_fcm_send_error("some-token", quota_exc, result)

        error_log_calls = [
            c for c in self.fake_frappe.log_error.call_args_list
            if len(c.args) >= 2 and c.args[1] == "FCM Send Error"
        ]
        self.assertEqual(len(error_log_calls), 1, msg=(
            f"Expected exactly 1 Error Log row for unexpected quota error, got {len(error_log_calls)}"
        ))

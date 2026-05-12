"""Tests: _is_invalid_token_error correctly classifies FCM errors.

Covers PROD-POS-002 Phase 1 — the helper must:
- Return True for UnregisteredError instances (SDK class-based).
- Return True for 'Requested entity was not found.' (production gRPC variant).
- Return True for 'NotRegistered' and 'registration-token-not-registered'.
- Return False for unrelated errors (quota, network, auth) that should NOT
  cause a token to be disabled.
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock


def _load_module():
    fake_frappe = types.ModuleType("frappe")
    fake_frappe._ = lambda x: x
    fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
    fake_frappe.throw = MagicMock(side_effect=Exception)
    fake_frappe.log_error = MagicMock()
    fake_frappe.get_traceback = MagicMock(return_value="traceback")
    fake_logger = SimpleNamespace(info=MagicMock())
    fake_frappe.logger = MagicMock(return_value=fake_logger)
    fake_frappe.get_all = MagicMock(return_value=[])
    fake_frappe.get_doc = MagicMock()
    fake_frappe.db = SimpleNamespace(set_value=MagicMock(), count=MagicMock(return_value=0))
    fake_frappe.session = SimpleNamespace(user="admin@example.com")
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

    # Build a fake firebase_admin with custom exception classes for SDK-based detection.
    fake_firebase = types.ModuleType("firebase_admin")
    fake_firebase.get_app = MagicMock(side_effect=ValueError("not initialized"))
    fake_firebase.initialize_app = MagicMock()

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
    fake_messaging.Message = MagicMock()
    fake_messaging.send = MagicMock()

    fake_creds = types.ModuleType("firebase_admin.credentials")
    fake_creds.Certificate = MagicMock(return_value=MagicMock())

    fake_firebase.credentials = fake_creds
    fake_firebase.messaging = fake_messaging

    patches = {
        "frappe": fake_frappe,
        "firebase_admin": fake_firebase,
        "firebase_admin.credentials": fake_creds,
        "firebase_admin.messaging": fake_messaging,
    }
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(sys.modules, patches):
        sys.modules.pop("jarz_pos.api.notifications", None)
        mod = importlib.import_module("jarz_pos.api.notifications")
        importlib.reload(mod)

    return mod, fake_messaging


class TestFcmInvalidTokenClassification(unittest.TestCase):
    def setUp(self):
        self.mod, self.fake_messaging = _load_module()

    # --- Must return True (token should be disabled) ---

    def test_sdk_unregistered_error_class(self):
        exc = self.fake_messaging.UnregisteredError("token gone")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_sdk_sender_id_mismatch_error_class(self):
        exc = self.fake_messaging.SenderIdMismatchError("mismatch")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_requested_entity_not_found_string(self):
        """The gRPC variant seen 558 times in the 96h prod window."""
        exc = Exception("Requested entity was not found.")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_requested_entity_not_found_mixed_case(self):
        exc = Exception("REQUESTED ENTITY WAS NOT FOUND.")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_not_registered_string(self):
        exc = Exception("NotRegistered")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_registration_token_not_registered_string(self):
        exc = Exception("registration-token-not-registered")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_invalid_registration_string(self):
        exc = Exception("invalid registration")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_error_code_not_found(self):
        exc = Exception("some message")
        exc.error_code = "NOT_FOUND"
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_error_code_registration_token_not_registered(self):
        exc = Exception("some message")
        exc.code = "registration-token-not-registered"
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_error_code_invalid_argument(self):
        exc = Exception("some message")
        exc.code = "invalid-argument"
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    # --- Must return False (token should NOT be disabled) ---

    def test_quota_exceeded_not_classified_as_invalid(self):
        exc = Exception("Quota exceeded for quota metric")
        self.assertFalse(self.mod._is_invalid_token_error(exc))

    def test_network_error_not_classified_as_invalid(self):
        exc = ConnectionError("Network unreachable")
        self.assertFalse(self.mod._is_invalid_token_error(exc))

    def test_auth_error_not_classified_as_invalid(self):
        exc = Exception("The caller does not have permission")
        self.assertFalse(self.mod._is_invalid_token_error(exc))

    def test_generic_exception_not_classified_as_invalid(self):
        exc = RuntimeError("unexpected failure")
        self.assertFalse(self.mod._is_invalid_token_error(exc))

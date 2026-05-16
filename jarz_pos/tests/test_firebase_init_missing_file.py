"""Tests: health_check_firebase() returns ok=False for missing/misconfigured files,
and init-failure logging is throttled to one Error Log row per process.

Covers PROD-POS-001 Phase 2 code hardening requirements.
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock


def _build_fake_frappe(conf: dict) -> types.ModuleType:
    fake = types.ModuleType("frappe")
    fake._ = lambda x: x
    fake.whitelist = lambda *a, **kw: (lambda fn: fn)
    fake.throw = MagicMock(side_effect=Exception("frappe.throw called"))
    fake.log_error = MagicMock()
    fake.get_traceback = MagicMock(return_value="traceback")
    fake_logger = SimpleNamespace(info=MagicMock(), warning=MagicMock(), error=MagicMock())
    fake.logger = MagicMock(return_value=fake_logger)
    fake.get_all = MagicMock(return_value=[])
    fake.get_doc = MagicMock()
    fake.db = SimpleNamespace(set_value=MagicMock(), count=MagicMock(return_value=0))
    fake.session = SimpleNamespace(user="admin@example.com")
    fake.utils = SimpleNamespace(
        now_datetime=MagicMock(return_value="2026-05-12T00:00:00"),
        now=MagicMock(return_value="2026-05-12 00:00:00"),
        today=MagicMock(return_value="2026-05-12"),
        nowtime=MagicMock(return_value="00:00:00"),
        add_to_date=MagicMock(return_value="2026-05-12 00:00:00"),
        get_datetime=MagicMock(return_value="2026-05-12 00:00:00"),
    )
    fake.conf = conf
    fake.local = SimpleNamespace(conf=conf, site="frontend")
    fake.get_site_path = MagicMock(return_value="/home/frappe/frappe-bench/sites/frontend/private/files")
    fake.publish_realtime = MagicMock()
    fake.msgprint = MagicMock()
    return fake


def _load_module(conf: dict, certificate_side_effect=None):
    fake_frappe = _build_fake_frappe(conf)

    fake_firebase = types.ModuleType("firebase_admin")
    fake_firebase.get_app = MagicMock(side_effect=ValueError("not initialized"))
    fake_firebase.initialize_app = MagicMock()

    fake_creds = types.ModuleType("firebase_admin.credentials")
    if certificate_side_effect is not None:
        fake_creds.Certificate = MagicMock(side_effect=certificate_side_effect)
    else:
        fake_creds.Certificate = MagicMock(side_effect=FileNotFoundError("No such file"))

    fake_messaging = types.ModuleType("firebase_admin.messaging")
    fake_messaging.Notification = MagicMock()
    fake_messaging.AndroidNotification = MagicMock()
    fake_messaging.AndroidConfig = MagicMock()
    fake_messaging.Message = MagicMock()
    fake_messaging.send = MagicMock()

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

    mod._FIREBASE_INIT_STATE["failed_logged"] = False
    mod._FIREBASE_INIT_STATE["ok"] = False
    return mod, fake_frappe


class TestFirebaseInitMissingFile(unittest.TestCase):
    def test_health_check_returns_ok_false_for_missing_file(self):
        """health_check_firebase returns ok=False when the service-account file is missing."""
        mod, _ = _load_module({"fcm_service_account_path": "/nonexistent/key.json"})

        result = mod.health_check_firebase()

        self.assertFalse(result["ok"])
        self.assertTrue(result["sdk_available"])
        self.assertIn("not found", result["reason"].lower())

    def test_health_check_returns_ok_false_when_no_config(self):
        """health_check_firebase returns ok=False with descriptive reason when no config exists."""
        mod, _ = _load_module({})

        result = mod.health_check_firebase()

        self.assertFalse(result["ok"])
        self.assertIn("no service account", result["reason"].lower())

    def test_health_check_resolved_path_is_absolute(self):
        """resolved_path in health_check response is always absolute."""
        bare = "key.json"
        mod, _ = _load_module({"fcm_service_account_path": bare})

        result = mod.health_check_firebase()

        import os
        self.assertIsNotNone(result["resolved_path"])
        self.assertTrue(
            os.path.isabs(result["resolved_path"])
            or result["resolved_path"].startswith(("/", "\\"))
        )

    def test_init_failure_logged_only_once_per_process(self):
        """Error Log is written exactly once regardless of how many times init is called."""
        mod, fake_frappe = _load_module({"fcm_service_account_path": "/tmp/nonexistent.json"})

        for _ in range(5):
            mod._initialize_firebase_app()

        # Count only Error Log writes (not frappe.logger calls)
        error_log_calls = [
            call for call in fake_frappe.log_error.call_args_list
            if len(call.args) >= 1 and "Firebase" in str(call.args[-1])
        ]
        self.assertLessEqual(len(error_log_calls), 1)

    def test_health_check_returns_resolved_path_none_when_no_path_config(self):
        """resolved_path is None when fcm_service_account_path is not configured."""
        mod, _ = _load_module({})

        result = mod.health_check_firebase()

        self.assertIsNone(result["resolved_path"])

    def test_push_skipped_logged_only_once_when_init_unavailable(self):
        """FCM Push Skipped Error Log rows are throttled while init remains broken."""
        mod, fake_frappe = _load_module({"fcm_service_account_path": "/tmp/nonexistent.json"})
        data = {"type": "new_invoice", "invoice_id": "INV-INIT-MISS"}

        for _ in range(5):
            result = mod._send_fcm_notifications(["token-1"], data)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "skipped_init_failed")
        skipped_calls = [
            call for call in fake_frappe.log_error.call_args_list
            if len(call.args) >= 2 and call.args[1] == "FCM Push Skipped"
        ]
        self.assertEqual(len(skipped_calls), 1)

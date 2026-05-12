"""Tests: _initialize_firebase_app resolves relative service-account paths.

Verifies PROD-POS-001 fix: bare filenames are joined against
frappe.get_site_path("private", "files") before being passed to
credentials.Certificate(); absolute paths pass through unchanged.
"""

import importlib
import os
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
    fake.db = SimpleNamespace(
        set_value=MagicMock(), count=MagicMock(return_value=0), get_value=MagicMock()
    )
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
    # get_site_path joins args under a fake site root
    fake.get_site_path = MagicMock(return_value="/home/frappe/frappe-bench/sites/frontend/private/files")
    fake.publish_realtime = MagicMock()
    fake.msgprint = MagicMock()
    return fake


def _build_fake_firebase() -> tuple:
    fake_firebase = types.ModuleType("firebase_admin")
    # Simulate "not yet initialized" on every get_app call so init always runs.
    fake_firebase.get_app = MagicMock(side_effect=ValueError("not initialized"))
    fake_firebase.initialize_app = MagicMock()

    fake_creds = types.ModuleType("firebase_admin.credentials")
    fake_creds.Certificate = MagicMock(return_value=MagicMock())

    fake_messaging = types.ModuleType("firebase_admin.messaging")
    fake_messaging.Notification = MagicMock()
    fake_messaging.AndroidNotification = MagicMock()
    fake_messaging.AndroidConfig = MagicMock()
    fake_messaging.Message = MagicMock()
    fake_messaging.send = MagicMock()

    fake_firebase.credentials = fake_creds
    fake_firebase.messaging = fake_messaging
    return fake_firebase, fake_creds


def _load_module(conf: dict):
    fake_frappe = _build_fake_frappe(conf)
    fake_firebase, fake_creds = _build_fake_firebase()

    patches = {
        "frappe": fake_frappe,
        "firebase_admin": fake_firebase,
        "firebase_admin.credentials": fake_creds,
        "firebase_admin.messaging": fake_firebase.messaging,
    }
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(sys.modules, patches):
        sys.modules.pop("jarz_pos.api.notifications", None)
        mod = importlib.import_module("jarz_pos.api.notifications")
        importlib.reload(mod)

    # Reset module-level state after load
    mod._FIREBASE_INIT_STATE["failed_logged"] = False
    mod._FIREBASE_INIT_STATE["ok"] = False
    return mod, fake_frappe, fake_creds


class TestFirebaseInitRelativePath(unittest.TestCase):
    def test_relative_filename_resolved_under_site_private_files(self):
        """A bare filename is joined to frappe.get_site_path("private", "files")."""
        bare = "jarz-pos-firebase-adminsdk-fbsvc-5842596d09.json"
        mod, fake_frappe, fake_creds = _load_module({"fcm_service_account_path": bare})

        result = mod._initialize_firebase_app()

        self.assertTrue(result)
        expected = os.path.join(
            "/home/frappe/frappe-bench/sites/frontend/private/files", bare
        )
        fake_creds.Certificate.assert_called_once_with(expected)

    def test_absolute_path_passes_through_unchanged(self):
        """An absolute path is used as-is without any join."""
        abs_path = "/home/frappe/frappe-bench/sites/frontend/private/files/jarz-key.json"
        mod, fake_frappe, fake_creds = _load_module({"fcm_service_account_path": abs_path})

        result = mod._initialize_firebase_app()

        self.assertTrue(result)
        fake_creds.Certificate.assert_called_once_with(abs_path)

    def test_returns_false_when_no_path_configured(self):
        """Returns False and logs an error when neither path nor inline JSON is configured."""
        mod, fake_frappe, fake_creds = _load_module({})

        result = mod._initialize_firebase_app()

        self.assertFalse(result)
        fake_creds.Certificate.assert_not_called()
        # Error Log written at least once
        self.assertTrue(fake_frappe.log_error.called)

    def test_init_state_ok_set_on_success(self):
        """_FIREBASE_INIT_STATE['ok'] is set to True after successful init."""
        abs_path = "/tmp/key.json"
        mod, _, _ = _load_module({"fcm_service_account_path": abs_path})

        mod._initialize_firebase_app()

        self.assertTrue(mod._FIREBASE_INIT_STATE["ok"])

    def test_init_state_failed_logged_cleared_on_success(self):
        """failed_logged is reset to False when init succeeds after a previous failure."""
        abs_path = "/tmp/key.json"
        mod, _, _ = _load_module({"fcm_service_account_path": abs_path})
        mod._FIREBASE_INIT_STATE["failed_logged"] = True  # Simulate prior failure logged

        mod._initialize_firebase_app()

        self.assertFalse(mod._FIREBASE_INIT_STATE["failed_logged"])

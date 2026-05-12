"""Tests: _disable_token disables ALL Jarz Mobile Device rows sharing a token.

Covers PROD-POS-002 Phase 2 — when multiple device rows share the same stale
token (e.g. a user re-installed the app), every enabled row must be disabled.
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


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

    fake_firebase = types.ModuleType("firebase_admin")
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

    return mod, fake_frappe


class TestFcmTokenDisabledPropagates(unittest.TestCase):
    def setUp(self):
        self.mod, self.fake_frappe = _load_module()

    def test_two_rows_same_token_both_disabled(self):
        """Both enabled device rows sharing a token are disabled."""
        self.fake_frappe.get_all.return_value = [
            {"name": "DEV-001", "enabled": 1},
            {"name": "DEV-002", "enabled": 1},
        ]
        dev1 = SimpleNamespace(name="DEV-001", enabled=1)
        dev2 = SimpleNamespace(name="DEV-002", enabled=1)
        self.fake_frappe.get_doc.side_effect = lambda dt, name: {
            "DEV-001": dev1,
            "DEV-002": dev2,
        }[name]

        self.mod._disable_token("stale-token-xyz")

        set_value_calls = self.fake_frappe.db.set_value.call_args_list
        disabled_names = {c.args[1] for c in set_value_calls if c.args[2] == "enabled"}
        self.assertIn("DEV-001", disabled_names)
        self.assertIn("DEV-002", disabled_names)
        # Both set to 0
        for c in set_value_calls:
            if c.args[1] in ("DEV-001", "DEV-002"):
                self.assertEqual(c.args[3], 0)

    def test_already_disabled_row_not_touched(self):
        """A row that is already disabled is skipped (no db.set_value call)."""
        self.fake_frappe.get_all.return_value = [
            {"name": "DEV-003", "enabled": 0},
        ]
        dev3 = SimpleNamespace(name="DEV-003", enabled=0)
        self.fake_frappe.get_doc.return_value = dev3

        self.mod._disable_token("already-dead-token")

        self.fake_frappe.db.set_value.assert_not_called()

    def test_no_rows_found_is_a_noop(self):
        """_disable_token is a no-op when no device row has the token."""
        self.fake_frappe.get_all.return_value = []

        self.mod._disable_token("unknown-token")

        self.fake_frappe.db.set_value.assert_not_called()
        self.fake_frappe.get_doc.assert_not_called()

    def test_mixed_enabled_disabled_only_enabled_row_updated(self):
        """When one row is enabled and another is already disabled, only the enabled row is updated."""
        self.fake_frappe.get_all.return_value = [
            {"name": "DEV-ACTIVE", "enabled": 1},
            {"name": "DEV-DEAD", "enabled": 0},
        ]
        active = SimpleNamespace(name="DEV-ACTIVE", enabled=1)
        dead = SimpleNamespace(name="DEV-DEAD", enabled=0)
        self.fake_frappe.get_doc.side_effect = lambda dt, name: {
            "DEV-ACTIVE": active,
            "DEV-DEAD": dead,
        }[name]

        self.mod._disable_token("mixed-token")

        set_value_calls = self.fake_frappe.db.set_value.call_args_list
        # Only DEV-ACTIVE should be updated
        self.assertEqual(len(set_value_calls), 1)
        self.assertEqual(set_value_calls[0].args[1], "DEV-ACTIVE")

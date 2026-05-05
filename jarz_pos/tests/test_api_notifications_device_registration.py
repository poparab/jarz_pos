import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestNotificationDeviceRegistration(unittest.TestCase):
    def _load_notifications_module(self):
        fake_frappe = types.ModuleType("frappe")
        fake_frappe._ = lambda message: message
        fake_frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
        fake_frappe.throw = MagicMock(side_effect=Exception)
        fake_frappe.log_error = MagicMock()
        fake_frappe.get_traceback = MagicMock(return_value="traceback")
        fake_frappe.logger = MagicMock(return_value=SimpleNamespace(info=MagicMock()))
        fake_frappe.delete_doc = MagicMock()
        fake_frappe.get_all = MagicMock(return_value=[])
        fake_frappe.get_doc = MagicMock()
        fake_frappe.db = SimpleNamespace(set_value=MagicMock())
        fake_frappe.session = SimpleNamespace(user="operator@example.com")
        fake_frappe.utils = SimpleNamespace(now_datetime=MagicMock(return_value="2026-05-05T18:05:52"))
        fake_frappe.conf = {}

        with patch.dict(sys.modules, {"frappe": fake_frappe}):
            sys.modules.pop("jarz_pos.api.notifications", None)
            module = importlib.import_module("jarz_pos.api.notifications")
            return importlib.reload(module)

    def test_upsert_mobile_device_prunes_duplicate_rows_before_update(self):
        notifications = self._load_notifications_module()
        notifications.frappe.get_all.return_value = [
            {"name": "DEV-CURRENT", "user": "operator@example.com", "enabled": 1, "modified": "2026-05-05 18:05:51"},
            {"name": "DEV-OLD", "user": "operator@example.com", "enabled": 0, "modified": "2026-05-05 18:05:50"},
            {"name": "DEV-OTHER", "user": "other@example.com", "enabled": 0, "modified": "2026-05-05 18:05:49"},
        ]
        notifications.frappe.get_doc.return_value = SimpleNamespace(name="DEV-CURRENT")

        result = notifications._upsert_mobile_device(
            {
                "token": "token-1",
                "user": "operator@example.com",
                "platform": "Android",
                "device_name": "Android POS",
                "app_version": None,
                "enabled": 1,
                "pos_profiles": '["Nasr city"]',
            }
        )

        self.assertEqual(result.name, "DEV-CURRENT")
        notifications.frappe.delete_doc.assert_any_call(
            "Jarz Mobile Device",
            "DEV-OLD",
            ignore_permissions=True,
            force=True,
        )
        notifications.frappe.delete_doc.assert_any_call(
            "Jarz Mobile Device",
            "DEV-OTHER",
            ignore_permissions=True,
            force=True,
        )
        notifications.frappe.db.set_value.assert_called_once_with(
            "Jarz Mobile Device",
            "DEV-CURRENT",
            {
                "token": "token-1",
                "user": "operator@example.com",
                "platform": "Android",
                "device_name": "Android POS",
                "app_version": None,
                "enabled": 1,
                "pos_profiles": '["Nasr city"]',
                "last_seen": "2026-05-05T18:05:52",
            },
            update_modified=True,
        )

    def test_upsert_mobile_device_recovers_when_insert_races_with_existing_token(self):
        notifications = self._load_notifications_module()
        insert_doc = MagicMock()
        insert_doc.insert.side_effect = RuntimeError("duplicate token")
        notifications.frappe.get_all.side_effect = [
            [],
            [{"name": "DEV-RACE", "user": "operator@example.com", "enabled": 1, "modified": "2026-05-05 18:05:52"}],
        ]
        notifications.frappe.get_doc.side_effect = [
            insert_doc,
            SimpleNamespace(name="DEV-RACE"),
        ]

        result = notifications._upsert_mobile_device(
            {
                "token": "token-2",
                "user": "operator@example.com",
                "platform": "Android",
                "device_name": "Android POS",
                "app_version": None,
                "enabled": 1,
                "pos_profiles": '["6th of october"]',
            }
        )

        self.assertEqual(result.name, "DEV-RACE")
        notifications.frappe.db.set_value.assert_called_once_with(
            "Jarz Mobile Device",
            "DEV-RACE",
            {
                "token": "token-2",
                "user": "operator@example.com",
                "platform": "Android",
                "device_name": "Android POS",
                "app_version": None,
                "enabled": 1,
                "pos_profiles": '["6th of october"]',
                "last_seen": "2026-05-05T18:05:52",
            },
            update_modified=True,
        )
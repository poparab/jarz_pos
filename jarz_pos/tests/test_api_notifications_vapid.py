import importlib
import json
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch


class TestVapidWebPush(unittest.TestCase):
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
        fake_frappe.db = SimpleNamespace(
            set_value=MagicMock(),
            commit=MagicMock(),
        )
        fake_frappe.session = SimpleNamespace(user="operator@example.com")
        fake_frappe.utils = SimpleNamespace(now_datetime=MagicMock(return_value=datetime(2026, 6, 6, 10, 0, 0)))
        fake_frappe.conf = {}

        with patch.dict(sys.modules, {"frappe": fake_frappe}):
            sys.modules.pop("jarz_pos.api.notifications", None)
            module = importlib.import_module("jarz_pos.api.notifications")
            return importlib.reload(module)

    # ── Key generation ────────────────────────────────────────────────────────

    def test_generate_vapid_keys_returns_base64url_and_pem(self):
        notifications = self._load_notifications_module()
        pub, priv = notifications._generate_vapid_keys()
        self.assertIsInstance(pub, str)
        self.assertIsInstance(priv, str)
        # Public key is base64url (65 bytes → 87 chars without padding)
        self.assertGreater(len(pub), 80)
        self.assertNotIn("=", pub, "Public key must have no padding")
        self.assertNotIn("+", pub)
        self.assertNotIn("/", pub)
        self.assertIn("BEGIN EC PRIVATE KEY", priv)

    def test_get_or_create_vapid_keys_generates_when_missing(self):
        notifications = self._load_notifications_module()
        fake_settings = SimpleNamespace(vapid_public_key="", vapid_private_key="")
        notifications.frappe.get_doc.return_value = fake_settings

        with patch.object(notifications, "_generate_vapid_keys", return_value=("PUB", "PRIV")) as mock_gen:
            pub, priv = notifications._get_or_create_vapid_keys()

        mock_gen.assert_called_once()
        self.assertEqual(pub, "PUB")
        self.assertEqual(priv, "PRIV")
        notifications.frappe.db.set_value.assert_called_once()
        notifications.frappe.db.commit.assert_called_once()

    def test_get_or_create_vapid_keys_returns_existing(self):
        notifications = self._load_notifications_module()
        fake_settings = SimpleNamespace(
            vapid_public_key="EXISTING_PUB",
            vapid_private_key="EXISTING_PRIV",
        )
        notifications.frappe.get_doc.return_value = fake_settings

        with patch.object(notifications, "_generate_vapid_keys") as mock_gen:
            pub, priv = notifications._get_or_create_vapid_keys()

        mock_gen.assert_not_called()
        self.assertEqual(pub, "EXISTING_PUB")
        self.assertEqual(priv, "EXISTING_PRIV")
        notifications.frappe.db.set_value.assert_not_called()

    # ── get_vapid_public_key endpoint ──────────────────────────────────────────

    def test_get_vapid_public_key_returns_dict_with_public_key(self):
        notifications = self._load_notifications_module()
        with patch.object(notifications, "_get_or_create_vapid_keys", return_value=("PUB123", "PRIV")):
            result = notifications.get_vapid_public_key()
        self.assertEqual(result, {"public_key": "PUB123"})

    # ── register_vapid_subscription endpoint ──────────────────────────────────

    def test_register_vapid_subscription_valid_creates_record(self):
        notifications = self._load_notifications_module()
        sub_json = json.dumps({
            "endpoint": "https://web.push.apple.com/test",
            "keys": {"p256dh": "key1", "auth": "auth1"},
        })

        notifications.frappe.get_all.return_value = []  # no existing record
        new_doc = SimpleNamespace(name="WPSUB-001", insert=MagicMock())
        notifications.frappe.get_doc.return_value = new_doc

        result = notifications.register_vapid_subscription(sub_json)

        new_doc.insert.assert_called_once_with(ignore_permissions=True)
        self.assertTrue(result["success"])

    def test_register_vapid_subscription_invalid_json_throws(self):
        notifications = self._load_notifications_module()
        with self.assertRaises(Exception):
            notifications.register_vapid_subscription("not-json")
        notifications.frappe.throw.assert_called_once()

    def test_register_vapid_subscription_deduplicates_by_endpoint(self):
        notifications = self._load_notifications_module()
        sub_json = json.dumps({
            "endpoint": "https://web.push.apple.com/existing",
            "keys": {"p256dh": "key1", "auth": "auth1"},
        })
        notifications.frappe.get_all.return_value = [{"name": "WPSUB-EXISTING"}]

        result = notifications.register_vapid_subscription(sub_json)

        notifications.frappe.db.set_value.assert_called_once()
        call_args = notifications.frappe.db.set_value.call_args
        self.assertEqual(call_args[0][0], "Jarz Web Push Subscription")
        self.assertEqual(call_args[0][1], "WPSUB-EXISTING")
        self.assertTrue(result["success"])

    # ── _get_vapid_subscriptions_for_users ────────────────────────────────────

    def test_get_vapid_subscriptions_for_users_returns_json_strings(self):
        notifications = self._load_notifications_module()
        sub1 = json.dumps({"endpoint": "https://ep1", "keys": {"p256dh": "k1", "auth": "a1"}})
        sub2 = json.dumps({"endpoint": "https://ep2", "keys": {"p256dh": "k2", "auth": "a2"}})
        notifications.frappe.get_all.return_value = [
            {"subscription_json": sub1, "endpoint": "https://ep1"},
            {"subscription_json": sub2, "endpoint": "https://ep2"},
            # Duplicate endpoint — should be deduped
            {"subscription_json": sub1, "endpoint": "https://ep1"},
        ]

        result = notifications._get_vapid_subscriptions_for_users(["operator@example.com"])

        self.assertEqual(len(result), 2)
        self.assertIn(sub1, result)
        self.assertIn(sub2, result)

    def test_get_vapid_subscriptions_for_users_empty_users_returns_empty(self):
        notifications = self._load_notifications_module()
        result = notifications._get_vapid_subscriptions_for_users([])
        self.assertEqual(result, [])
        notifications.frappe.get_all.assert_not_called()

    # ── _send_vapid_notifications ─────────────────────────────────────────────

    def test_send_vapid_notifications_success(self):
        notifications = self._load_notifications_module()
        sub_json = json.dumps({
            "endpoint": "https://web.push.apple.com/test",
            "keys": {"p256dh": "k1", "auth": "a1"},
        })
        fake_settings = SimpleNamespace(
            vapid_public_key="PUB",
            vapid_private_key="PRIV",
            vapid_subject="https://erp.orderjarz.com",
        )
        notifications.frappe.get_doc.return_value = fake_settings

        mock_webpush = MagicMock(return_value=None)
        fake_wp_module = SimpleNamespace(
            webpush=mock_webpush,
            WebPushException=Exception,
        )

        with patch.object(notifications, "_get_or_create_vapid_keys", return_value=("PUB", "PRIV")):
            with patch.dict(sys.modules, {"pywebpush": fake_wp_module}):
                result = notifications._send_vapid_notifications(
                    [sub_json],
                    {"type": "new_invoice", "title": "Order!", "body": "Table 1"},
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 0)
        self.assertEqual(result["status"], "success")

    def test_send_vapid_notifications_410_disables_subscription(self):
        notifications = self._load_notifications_module()
        sub_json = json.dumps({
            "endpoint": "https://web.push.apple.com/expired",
            "keys": {"p256dh": "k1", "auth": "a1"},
        })
        fake_settings = SimpleNamespace(
            vapid_subject="https://erp.orderjarz.com",
        )
        notifications.frappe.get_doc.return_value = fake_settings
        notifications.frappe.get_all.return_value = [{"name": "WPSUB-EXPIRED"}]

        class FakeWebPushException(Exception):
            response = SimpleNamespace(status_code=410)

        def mock_webpush(**_kwargs):
            raise FakeWebPushException("gone")

        fake_wp_module = SimpleNamespace(
            webpush=mock_webpush,
            WebPushException=FakeWebPushException,
        )

        with patch.object(notifications, "_get_or_create_vapid_keys", return_value=("PUB", "PRIV")):
            with patch.dict(sys.modules, {"pywebpush": fake_wp_module}):
                result = notifications._send_vapid_notifications(
                    [sub_json],
                    {"type": "new_invoice", "title": "t", "body": "b"},
                )

        self.assertEqual(result["failure_count"], 1)
        notifications.frappe.db.set_value.assert_called_once_with(
            "Jarz Web Push Subscription",
            "WPSUB-EXPIRED",
            "enabled",
            0,
            update_modified=False,
        )

    # ── _push_new_invoice integration ─────────────────────────────────────────

    def test_push_new_invoice_sends_both_fcm_and_vapid(self):
        notifications = self._load_notifications_module()

        fake_fcm_result = {"ok": True, "status": "success", "success_count": 2, "failure_count": 0}
        fake_vapid_result = {"ok": True, "status": "success", "success_count": 1, "failure_count": 0}

        with patch.object(notifications, "_get_tokens_for_users", return_value=["tok1", "tok2"]):
            with patch.object(notifications, "_get_vapid_subscriptions_for_users", return_value=["sub1"]):
                with patch.object(notifications, "_send_fcm_notifications", return_value=fake_fcm_result) as mock_fcm:
                    with patch.object(notifications, "_send_vapid_notifications", return_value=fake_vapid_result) as mock_vapid:
                        result = notifications._push_new_invoice(
                            {
                                "invoice_id": "SINV-001",
                                "customer_name": "Walk-in",
                                "territory": "Nasr City",
                                "pos_profile": "Branch A",
                            },
                            ["operator@example.com"],
                        )

        mock_fcm.assert_called_once()
        mock_vapid.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["success_count"], 3)
        self.assertIn("fcm", result)
        self.assertIn("vapid", result)

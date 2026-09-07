"""Tests: a push that reaches nobody has to leave evidence.

Branches reported missing order alerts on 2026-09-07. Every diagnostic on the
path went through ``frappe.logger().info()``, which both servers discard, so
the two states that mean "a human was not told" were indistinguishable from a
healthy send:

* a token FCM rejects is disabled -- announced only to the dropped logger, so
  the reachable fleet shrank invisibly (two accounts lost push that day and the
  only trace was the row itself);
* a new-invoice alert that resolves recipients but ZERO enabled tokens returns
  ``status="skipped_no_tokens"`` with ``ok=True``.

These cover the reporting, not the sending: the assertion is that Error Log --
the only sink that survives on the servers -- receives the event, and that the
throttle cannot silently swallow a *different* gap.

Same "mock frappe + firebase_admin, import fresh" harness as
``test_fcm_invalid_token_normalization``, so it runs without a site (the CI
logic gate runs before ``bench migrate``).
"""

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _FakeCache:
    """Stands in for frappe.cache(): the throttle must be cross-worker."""

    def __init__(self):
        self.store = {}

    def get_value(self, key):
        return self.store.get(key)

    def set_value(self, key, value, expires_in_sec=None):
        self.store[key] = value


def _load_module():
    fake_frappe = types.ModuleType("frappe")
    fake_frappe._ = lambda x: x
    fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
    fake_frappe.throw = MagicMock(side_effect=Exception)
    fake_frappe.log_error = MagicMock()
    fake_frappe.get_traceback = MagicMock(return_value="traceback")
    fake_frappe.logger = MagicMock(return_value=SimpleNamespace(info=MagicMock()))
    fake_frappe.get_all = MagicMock(return_value=[])
    fake_frappe.get_doc = MagicMock()
    fake_frappe.db = SimpleNamespace(
        set_value=MagicMock(), count=MagicMock(return_value=0)
    )
    fake_frappe.cache = MagicMock(return_value=_FakeCache())
    fake_frappe.session = SimpleNamespace(user="admin@example.com")
    fake_frappe.utils = SimpleNamespace(
        now_datetime=MagicMock(return_value="2026-09-07T00:00:00"),
        now=MagicMock(return_value="2026-09-07 00:00:00"),
        today=MagicMock(return_value="2026-09-07"),
        nowtime=MagicMock(return_value="00:00:00"),
        add_to_date=MagicMock(return_value="2026-09-07 00:00:00"),
        get_datetime=MagicMock(return_value="2026-09-07 00:00:00"),
    )
    fake_frappe.conf = {}
    fake_frappe.local = SimpleNamespace(conf={}, site="frontend")
    fake_frappe.get_site_path = MagicMock(return_value="/site/private/files")
    fake_frappe.publish_realtime = MagicMock()
    fake_frappe.msgprint = MagicMock()

    fake_firebase = types.ModuleType("firebase_admin")
    fake_firebase.get_app = MagicMock(side_effect=ValueError("not initialized"))
    fake_firebase.initialize_app = MagicMock()

    class _UnregisteredError(Exception):
        pass

    class _InvalidArgumentError(Exception):
        pass

    fake_messaging = types.ModuleType("firebase_admin.messaging")
    fake_messaging.UnregisteredError = _UnregisteredError
    fake_messaging.SenderIdMismatchError = type("_SenderIdMismatch", (Exception,), {})
    for attr in (
        "Notification",
        "AndroidNotification",
        "AndroidConfig",
        "Message",
        "send",
        "WebpushConfig",
        "WebpushNotification",
        "WebpushFCMOptions",
    ):
        setattr(fake_messaging, attr, MagicMock())

    fake_creds = types.ModuleType("firebase_admin.credentials")
    fake_creds.Certificate = MagicMock(return_value=MagicMock())
    fake_exceptions = types.ModuleType("firebase_admin.exceptions")
    fake_exceptions.InvalidArgumentError = _InvalidArgumentError

    fake_firebase.credentials = fake_creds
    fake_firebase.messaging = fake_messaging
    fake_firebase.exceptions = fake_exceptions

    patches = {
        "frappe": fake_frappe,
        "firebase_admin": fake_firebase,
        "firebase_admin.credentials": fake_creds,
        "firebase_admin.messaging": fake_messaging,
        "firebase_admin.exceptions": fake_exceptions,
    }
    with patch.dict(sys.modules, patches):
        sys.modules.pop("jarz_pos.api.notifications", None)
        mod = importlib.import_module("jarz_pos.api.notifications")
        importlib.reload(mod)

    return mod, fake_frappe


class TestNotificationGapLogging(unittest.TestCase):
    def setUp(self):
        self.mod, self.frappe = _load_module()

    def test_gap_reaches_error_log(self):
        """Error Log is the only sink that survives on the servers."""
        self.mod._log_notification_gap("Push token disabled", "body")
        self.assertTrue(self.frappe.log_error.called)

    def test_repeat_of_same_key_is_throttled(self):
        """Order volume must not drown the signal."""
        self.mod._log_notification_gap("t", "b", throttle_key="profile-a")
        self.mod._log_notification_gap("t", "b", throttle_key="profile-a")
        self.assertEqual(self.frappe.log_error.call_count, 1)

    def test_a_different_key_is_not_throttled(self):
        """A second branch going dark is a separate event, not a duplicate."""
        self.mod._log_notification_gap("t", "b", throttle_key="profile-a")
        self.mod._log_notification_gap("t", "b", throttle_key="profile-b")
        self.assertEqual(self.frappe.log_error.call_count, 2)

    def test_throttle_is_shared_not_per_process(self):
        """The per-process dedup on the send-error path is what made the
        2026-09-05 failures look like they had stopped after 36 rows when they
        were still happening on every order. This one goes through the cache."""
        self.mod._log_notification_gap("t", "b", throttle_key="k")
        self.assertTrue(self.frappe.cache.called)

    def test_title_is_truncated_to_the_column_width(self):
        """Error Log.method is Data/varchar(140) and validates with a THROW.

        Reporting a gap must never become a second failure on the alert path.
        """
        self.mod._log_notification_gap("T" * 400, "body")
        title = self.frappe.log_error.call_args.kwargs["title"]
        self.assertLessEqual(len(title), 140)

    def test_reporting_failure_is_swallowed(self):
        """A broken sink must not take the notification down with it."""
        self.frappe.log_error.side_effect = Exception("Error Log is full")
        self.mod._log_notification_gap("t", "b")  # must not raise


class TestDisabledTokenIsAnnounced(unittest.TestCase):
    def setUp(self):
        self.mod, self.frappe = _load_module()

    def _one_row(self, enabled=1, user="branch@orderjarz.com", last_seen=None):
        return [
            {
                "name": "DEV-1",
                "enabled": enabled,
                "user": user,
                "platform": "Android",
                "last_seen": last_seen,
            }
        ]

    def test_disabling_a_token_writes_an_error_log(self):
        self.frappe.get_all.return_value = self._one_row()
        self.frappe.get_doc.return_value = SimpleNamespace(enabled=1)
        self.frappe.db.count.return_value = 0

        self.mod._disable_token("dead-token")

        self.assertTrue(self.frappe.log_error.called)
        message = self.frappe.log_error.call_args.kwargs["message"]
        self.assertIn("branch@orderjarz.com", message)

    def test_message_says_when_the_user_is_now_unreachable(self):
        """0 remaining devices is the difference between degraded and dark."""
        self.frappe.get_all.return_value = self._one_row()
        self.frappe.get_doc.return_value = SimpleNamespace(enabled=1)
        self.frappe.db.count.return_value = 0

        self.mod._disable_token("dead-token")

        message = self.frappe.log_error.call_args.kwargs["message"]
        self.assertIn("no longer be reached", message)

    def test_message_does_not_cry_blackout_when_another_device_remains(self):
        self.frappe.get_all.return_value = self._one_row()
        self.frappe.get_doc.return_value = SimpleNamespace(enabled=1)
        self.frappe.db.count.return_value = 2

        self.mod._disable_token("dead-token")

        message = self.frappe.log_error.call_args.kwargs["message"]
        self.assertIn("Other devices remain reachable", message)

    def test_an_already_disabled_row_is_not_re_announced(self):
        self.frappe.get_all.return_value = self._one_row(enabled=0)
        self.frappe.get_doc.return_value = SimpleNamespace(enabled=0)

        self.mod._disable_token("dead-token")

        self.assertFalse(self.frappe.log_error.called)


class TestAndroidGetsDataOnlyMessages(unittest.TestCase):
    """The closed-tablet half of the same 2026-09-07 report.

    An FCM message carrying a ``notification`` block is rendered by the Android
    SDK itself when the app is backgrounded or killed, and
    ``JarzFirebaseMessagingService.onMessageReceived`` is never called. That
    service is the only path that starts the order alarm on Android, so the
    alarm rang in the foreground and nowhere else. Android therefore has to be
    sent data-only; web and iOS must keep the block or they display nothing.
    """

    def setUp(self):
        self.mod, self.frappe = _load_module()
        self.sent = []

        class _Message:
            def __init__(_self, **kwargs):
                _self.kwargs = kwargs

        messaging = sys.modules["firebase_admin.messaging"]
        messaging.Message = _Message
        messaging.send = lambda message, dry_run=False: self.sent.append(message) or "id"
        messaging.Notification = lambda **kw: ("notification", kw)
        messaging.AndroidNotification = lambda **kw: ("android_notification", kw)
        messaging.AndroidConfig = lambda **kw: ("android_config", kw)
        messaging.WebpushConfig = None
        messaging.WebpushNotification = None
        self.mod._initialize_firebase_app = lambda: True

    def _send(self, tokens, rows):
        self.frappe.get_all.return_value = rows
        self.mod._send_fcm_notifications(
            tokens,
            {"type": "new_invoice", "invoice_id": "INV-1", "title": "New Order", "body": "b"},
        )
        return {m.kwargs["token"]: m.kwargs for m in self.sent}

    def test_android_token_gets_no_notification_block(self):
        by_token = self._send(
            ["tok-android"], [{"token": "tok-android", "platform": "Android"}]
        )
        self.assertNotIn("notification", by_token["tok-android"])

    def test_android_token_still_carries_the_data(self):
        """Data-only is the point: the service reads type/invoice_id from it."""
        by_token = self._send(
            ["tok-android"], [{"token": "tok-android", "platform": "Android"}]
        )
        self.assertEqual(by_token["tok-android"]["data"]["type"], "new_invoice")

    def test_web_and_ios_keep_the_notification_block(self):
        by_token = self._send(
            ["tok-web", "tok-ios"],
            [
                {"token": "tok-web", "platform": "Web"},
                {"token": "tok-ios", "platform": "iOS"},
            ],
        )
        self.assertIn("notification", by_token["tok-web"])
        self.assertIn("notification", by_token["tok-ios"])

    def test_unknown_platform_keeps_the_old_shape(self):
        """A token with no row must not silently go dark: falling back to the
        notification block is the behaviour that at least displays something."""
        by_token = self._send(["tok-orphan"], [])
        self.assertIn("notification", by_token["tok-orphan"])

    def test_a_failed_platform_lookup_does_not_break_the_send(self):
        self.frappe.get_all.side_effect = Exception("db down")
        self.mod._send_fcm_notifications(
            ["tok-a"],
            {"type": "new_invoice", "invoice_id": "I", "title": "t", "body": "b"},
        )
        self.assertEqual(len(self.sent), 1)
        self.assertIn("notification", self.sent[0].kwargs)


if __name__ == "__main__":
    unittest.main()

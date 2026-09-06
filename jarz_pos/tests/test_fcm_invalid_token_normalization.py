"""Tests: _is_invalid_token_error normalises BOTH firebase-admin error-code
conventions.

Companion to ``test_fcm_invalid_token_classification.py``. That suite pins the
original (JS-SDK-flavoured) behaviour; this one is table-driven over the
specific defect found on production: the Python ``firebase-admin`` SDK raises
``code="INVALID_ARGUMENT"`` (uppercase, underscored) with message
``"Invalid argument."``, and the pre-fix comparison only matched the
JavaScript SDK's ``"invalid-argument"`` spelling — so a permanently-rejected
token was never disabled and was retried forever (54 Error Log rows in two
days on production).

Same "mock frappe + firebase_admin, import fresh" harness as the sibling
classification test, so this runs without a site (the CI logic gate runs
before ``bench migrate``).
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

    fake_firebase = types.ModuleType("firebase_admin")
    fake_firebase.get_app = MagicMock(side_effect=ValueError("not initialized"))
    fake_firebase.initialize_app = MagicMock()

    class _UnregisteredError(Exception):
        pass

    class _SenderIdMismatchError(Exception):
        pass

    class _InvalidArgumentError(Exception):
        """Mirrors firebase_admin.exceptions.InvalidArgumentError."""

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
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(sys.modules, patches):
        sys.modules.pop("jarz_pos.api.notifications", None)
        mod = importlib.import_module("jarz_pos.api.notifications")
        importlib.reload(mod)

    return mod, fake_messaging, fake_exceptions


class TestFcmInvalidTokenNormalization(unittest.TestCase):
    """Table-driven: every row must classify as invalid-token = True/False."""

    def setUp(self):
        self.mod, self.fake_messaging, self.fake_exceptions = _load_module()

    def test_python_sdk_uppercase_underscored_code(self):
        """The actual Python firebase-admin spelling: code='INVALID_ARGUMENT'."""
        exc = Exception("Invalid argument.")
        exc.code = "INVALID_ARGUMENT"
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_js_sdk_lowercase_hyphenated_code_still_matches(self):
        """Regression guard: the original JS-SDK spelling must keep matching."""
        exc = Exception("some message")
        exc.code = "invalid-argument"
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_invalid_argument_message_alone(self):
        """No code attribute at all — only the Python SDK's message text."""
        exc = Exception("Invalid argument.")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_unregistered_error_class(self):
        exc = self.fake_messaging.UnregisteredError("token gone")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_not_found_code_uppercase(self):
        exc = Exception("some message")
        exc.code = "NOT_FOUND"
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_not_found_code_mixed_case_is_normalised(self):
        exc = Exception("some message")
        exc.code = "not_found"
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_sdk_invalid_argument_error_class(self):
        """firebase_admin.exceptions.InvalidArgumentError, if importable."""
        exc = self.fake_exceptions.InvalidArgumentError("bad token")
        self.assertTrue(self.mod._is_invalid_token_error(exc))

    def test_permission_denied_is_not_an_invalid_token(self):
        """A real INVALID_ARGUMENT-adjacent-but-unrelated error must stay False."""
        exc = Exception("The caller does not have permission")
        exc.code = "PERMISSION_DENIED"
        self.assertFalse(self.mod._is_invalid_token_error(exc))

    def test_unrelated_exception_with_no_code_is_not_an_invalid_token(self):
        exc = RuntimeError("unexpected failure")
        self.assertFalse(self.mod._is_invalid_token_error(exc))


class TestFcmInvalidTokenDisableCap(unittest.TestCase):
    """Pins the safety cap in _record_fcm_send_error.

    INVALID_ARGUMENT is the firebase-admin SDK's generic argument error: it
    fires for a dead registration token, but also for a malformed *message*
    (bad data/apns/android config) shared across every token in the run. These
    two cases must be told apart WITHOUT narrowing _is_invalid_token_error
    (that would un-fix the original bug, since the real production message is
    just "Invalid argument." with no way to tell which case it is from the
    error alone):

    - a lone bad token among healthy ones is still disabled normally.
    - a majority of attempted tokens failing the same way in one run looks
      like a payload defect, not simultaneous device death, so disabling is
      suppressed and the failures are recorded as unexpected instead.
    """

    def setUp(self):
        self.mod, self.fake_messaging, self.fake_exceptions = _load_module()

    def _invalid_argument_exc(self):
        exc = Exception("Invalid argument.")
        exc.code = "INVALID_ARGUMENT"
        return exc

    def test_majority_invalid_argument_suppresses_disabling(self):
        """Payload-is-bad scenario: 3 attempted, all 3 return INVALID_ARGUMENT.

        Only the first stays under the 50% cap and is disabled; the rest are
        recorded as unexpected failures with disabling suppressed, and exactly
        one loud Error Log is written for the run (not per token).
        """
        result = self.mod._new_fcm_send_result(["tok-1", "tok-2", "tok-3"])
        result["attempted_count"] = 3

        disable_mock = MagicMock()
        with patch.object(self.mod, "_disable_token", disable_mock):
            for token in ("tok-1", "tok-2", "tok-3"):
                self.mod._record_fcm_send_error(token, self._invalid_argument_exc(), result)

        self.assertEqual(result["invalid_token_count"], 1)
        self.assertEqual(result["suppressed_invalid_token_count"], 2)
        self.assertEqual(result["unexpected_failure_count"], 2)
        self.assertEqual(result["failure_count"], 3)
        self.assertEqual(disable_mock.call_count, 1)

        cap_trip_logs = [
            c for c in self.mod.frappe.log_error.call_args_list
            if c.kwargs.get("title") == "FCM invalid-token cap tripped"
        ]
        self.assertEqual(
            len(cap_trip_logs), 1,
            msg=f"Expected exactly one cap-trip Error Log, got {len(cap_trip_logs)}",
        )

    def test_single_bad_token_among_good_ones_is_still_disabled(self):
        """Tokens-are-bad scenario: 1 invalid out of 4 attempted stays under the
        cap and is disabled normally — the cap must not suppress the ordinary
        one-dead-device case.
        """
        result = self.mod._new_fcm_send_result(["tok-1", "tok-2", "tok-3", "tok-4"])
        result["attempted_count"] = 4

        disable_mock = MagicMock()
        with patch.object(self.mod, "_disable_token", disable_mock):
            self.mod._record_fcm_send_error("tok-1", self._invalid_argument_exc(), result)

        self.assertEqual(result["invalid_token_count"], 1)
        self.assertEqual(result["suppressed_invalid_token_count"], 0)
        self.assertEqual(result["unexpected_failure_count"], 0)
        disable_mock.assert_called_once_with("tok-1")

        cap_trip_logs = [
            c for c in self.mod.frappe.log_error.call_args_list
            if c.kwargs.get("title") == "FCM invalid-token cap tripped"
        ]
        self.assertEqual(len(cap_trip_logs), 0)

    def test_cap_never_trips_for_two_or_fewer_attempted_tokens(self):
        """Both tokens in a 2-token run go bad — too small a batch to imply a
        payload regression, so both are disabled exactly as before this fix.
        """
        result = self.mod._new_fcm_send_result(["tok-1", "tok-2"])
        result["attempted_count"] = 2

        disable_mock = MagicMock()
        with patch.object(self.mod, "_disable_token", disable_mock):
            for token in ("tok-1", "tok-2"):
                self.mod._record_fcm_send_error(token, self._invalid_argument_exc(), result)

        self.assertEqual(result["invalid_token_count"], 2)
        self.assertEqual(result["suppressed_invalid_token_count"], 0)
        self.assertEqual(disable_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()

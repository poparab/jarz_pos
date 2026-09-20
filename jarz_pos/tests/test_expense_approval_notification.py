"""A pending expense request must reach the managers who can answer it.

An expense filed by a cashier lands at docstatus 0 with ``requires_approval=1``
and sits there silently. These cover the four things that made that silence a
bug, and the two ways the fix could become one:

* the alert goes to the holders of the role that gates approve/reject, not to a
  POS profile;
* the requester is not told about their own request;
* two pending requests do not collapse into one tray entry on Android;
* a test run pushes nothing, because CI runs against the live staging site.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarz_pos.api import notifications
from jarz_pos.constants import ROLES, WS_EVENTS


class _NotInATestRun:
    """Make the module believe it is serving live traffic.

    Assigns and restores by hand rather than using ``patch.object``:
    ``frappe.flags`` is a ``frappe._dict`` whose ``__dict__`` is None, so
    ``patch.object`` dies in ``get_original``. Same shape as
    ``test_push_suppressed_in_test_run``.
    """

    def __enter__(self):
        self._saved_module_flag = getattr(frappe, "in_test", False)
        self._saved_local_flag = frappe.flags.get("in_test", False)
        frappe.in_test = False
        frappe.flags.in_test = False
        return self

    def __exit__(self, *exc_info):
        frappe.in_test = self._saved_module_flag
        frappe.flags.in_test = self._saved_local_flag
        return False


def _expense_doc(**overrides):
    base = dict(
        name="JEXP-2026-00042",
        amount=250.5,
        currency="EGP",
        reason_label="Cleaning Supplies",
        reason_account="Cleaning Supplies - JZ",
        payment_source_label="Nasr city",
        paying_account="Cash - Nasr city - JZ",
        pos_profile="Nasr city",
        company="Jarz",
        expense_date="2026-09-20",
        expense_time=None,
        remarks="Mop and detergent",
        requested_by="cashier@jarz.test",
        requires_approval=1,
        docstatus=0,
        rejection_reason=None,
        rejected_on=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestExpenseApprovalRecipients(unittest.TestCase):
    def test_recipients_are_the_role_that_gates_approval(self):
        with patch.object(
            notifications, "_get_users_with_roles", return_value=["a@jarz.test"]
        ) as get_users:
            notifications._expense_approval_recipients("cashier@jarz.test")

        get_users.assert_called_once_with([ROLES.JARZ_MANAGER])

    def test_requester_is_not_told_about_their_own_request(self):
        """A manager who files one approves it on the spot; alerting them is noise."""
        with patch.object(
            notifications,
            "_get_users_with_roles",
            return_value=["boss@jarz.test", "cashier@jarz.test"],
        ):
            holders, recipients = notifications._expense_approval_recipients(
                "cashier@jarz.test"
            )

        self.assertEqual(recipients, ["boss@jarz.test"])
        # The holders are reported separately so the caller can tell "nobody has
        # the role" from "the only holder filed it".
        self.assertEqual(holders, ["boss@jarz.test", "cashier@jarz.test"])

    def test_no_requester_keeps_everyone(self):
        with patch.object(
            notifications,
            "_get_users_with_roles",
            return_value=["boss@jarz.test"],
        ):
            holders, recipients = notifications._expense_approval_recipients(None)

        self.assertEqual(recipients, ["boss@jarz.test"])
        self.assertEqual(holders, ["boss@jarz.test"])


class TestExpenseApprovalPayload(unittest.TestCase):
    def test_data_payload_is_all_strings(self):
        """FCM rejects a non-string data value for the whole batch."""
        payload = notifications._build_expense_approval_payload(_expense_doc())
        data = notifications._prepare_expense_approval_data_payload(payload)

        for key, value in data.items():
            self.assertIsInstance(value, str, f"{key} is {type(value)!r}")

    def test_title_and_body_name_the_money_the_person_and_the_reason(self):
        with patch.object(
            notifications.frappe.db, "get_value", return_value="Mona Cashier"
        ):
            payload = notifications._build_expense_approval_payload(_expense_doc())
        data = notifications._prepare_expense_approval_data_payload(payload)

        self.assertEqual(data["type"], "expense_approval_required")
        self.assertIn("250.50", data["title"])
        self.assertIn("EGP", data["title"])
        self.assertIn("Mona Cashier", data["body"])
        self.assertIn("Cleaning Supplies", data["body"])
        self.assertIn("Nasr city", data["body"])

    def test_notification_id_is_the_expense_so_two_requests_do_not_collapse(self):
        """The Android tag collapses: a shared one hides the earlier request."""
        first = notifications._prepare_expense_approval_data_payload(
            notifications._build_expense_approval_payload(_expense_doc())
        )
        second = notifications._prepare_expense_approval_data_payload(
            notifications._build_expense_approval_payload(
                _expense_doc(name="JEXP-2026-00043")
            )
        )

        self.assertEqual(first["notification_id"], "JEXP-2026-00042")
        self.assertEqual(second["notification_id"], "JEXP-2026-00043")
        self.assertNotEqual(first["notification_id"], second["notification_id"])

    def test_unnamed_doc_yields_no_payload(self):
        self.assertEqual(
            notifications._build_expense_approval_payload(_expense_doc(name="")), {}
        )


class TestExpenseApprovalPush(unittest.TestCase):
    def test_a_test_run_pushes_nothing(self):
        """CI runs against the live staging site; tearDown cannot recall an FCM."""
        with patch.object(notifications, "_send_fcm_notifications") as send, patch.object(
            notifications, "_publish_to_recipients"
        ) as publish:
            result = notifications.notify_expense_approval_required(_expense_doc())

        self.assertEqual(result["status"], "suppressed_test_run")
        send.assert_not_called()
        publish.assert_not_called()

    def test_live_traffic_publishes_and_pushes_to_managers(self):
        with _NotInATestRun():
            with patch.object(
                notifications,
                "_get_users_with_roles",
                return_value=["boss@jarz.test"],
            ), patch.object(
                notifications,
                "_get_token_targets_for_users",
                return_value=(["tok-1"], {"tok-1": "android"}),
            ), patch.object(
                notifications, "_get_vapid_subscriptions_for_users", return_value=[]
            ), patch.object(
                notifications,
                "_send_fcm_notifications",
                return_value={"ok": True, "status": "sent", "success_count": 1, "failure_count": 0},
            ) as send, patch.object(
                notifications, "_publish_to_recipients"
            ) as publish, patch.object(
                notifications.frappe.db, "get_value", return_value="Mona Cashier"
            ):
                result = notifications.notify_expense_approval_required(_expense_doc())

        self.assertTrue(result["ok"])
        self.assertEqual(result["recipients"], 1)

        publish.assert_called_once()
        self.assertEqual(
            publish.call_args[0][0], WS_EVENTS.EXPENSE_APPROVAL_REQUESTED
        )

        send.assert_called_once()
        tokens, data = send.call_args[0][0], send.call_args[0][1]
        self.assertEqual(list(tokens), ["tok-1"])
        self.assertEqual(data["type"], "expense_approval_required")
        self.assertEqual(data["expense_id"], "JEXP-2026-00042")

    def test_no_manager_holds_the_role_is_logged_not_swallowed(self):
        with _NotInATestRun():
            with patch.object(
                notifications, "_get_users_with_roles", return_value=[]
            ), patch.object(
                notifications, "_log_notification_gap"
            ) as gap, patch.object(
                notifications, "_send_fcm_notifications"
            ) as send:
                result = notifications.notify_expense_approval_required(_expense_doc())

        self.assertEqual(result["status"], "skipped_no_recipients")
        send.assert_not_called()
        gap.assert_called_once()

    def test_sole_manager_filing_their_own_request_is_not_a_misconfiguration(self):
        """Telling an admin to grant a role that is already granted is a wrong answer."""
        with _NotInATestRun():
            with patch.object(
                notifications,
                "_get_users_with_roles",
                return_value=["cashier@jarz.test"],
            ), patch.object(notifications, "_log_notification_gap") as gap:
                result = notifications.notify_expense_approval_required(_expense_doc())

        self.assertEqual(result["status"], "skipped_requester_is_sole_manager")
        gap.assert_not_called()

    def test_worker_entry_point_refuses_a_blank_name(self):
        self.assertEqual(
            notifications.send_expense_approval_alert("")["status"],
            "skipped_no_expense",
        )

    def test_a_failure_never_escapes_to_the_caller(self):
        """after_insert runs inside the insert's transaction."""
        with _NotInATestRun():
            with patch.object(
                notifications,
                "_get_users_with_roles",
                side_effect=RuntimeError("boom"),
            ), patch.object(notifications.frappe, "log_error"):
                result = notifications.notify_expense_approval_required(_expense_doc())

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed_exception")


class TestExpenseApprovalAndroidChannel(unittest.TestCase):
    def test_approval_type_gets_its_own_channel(self):
        """Not the order channel: that bypasses DND and plays the order alarm."""
        self.assertIn(
            notifications.EXPENSE_APPROVAL_NOTIFICATION_TYPE,
            notifications.APPROVAL_NOTIFICATION_TYPES,
        )
        self.assertNotEqual(
            notifications.ANDROID_APPROVAL_CHANNEL_ID,
            notifications.ANDROID_ORDER_ALERT_CHANNEL_ID,
        )
        self.assertNotEqual(
            notifications.ANDROID_APPROVAL_CHANNEL_ID,
            notifications.ANDROID_SHIFT_CHANNEL_ID,
        )


class TestExpenseRequestFiresTheAlert(unittest.TestCase):
    """The gate is the document's own state, so every creation path is covered."""

    def _run_after_insert(self, doc_fields):
        from jarz_pos.doctype.jarz_expense_request.jarz_expense_request import (
            JarzExpenseRequest,
        )

        doc = _expense_doc(**doc_fields)
        with _NotInATestRun():
            with patch.object(notifications.frappe, "enqueue") as enqueue:
                JarzExpenseRequest.after_insert(doc)
        return enqueue

    def test_pending_request_alerts(self):
        enqueue = self._run_after_insert({"requires_approval": 1, "docstatus": 0})
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        # after_commit is the whole point: the fan-out must not run inside the
        # insert's transaction, or a failed push can roll back the expense.
        self.assertTrue(kwargs["enqueue_after_commit"])
        self.assertEqual(kwargs["expense"], "JEXP-2026-00042")
        self.assertEqual(
            enqueue.call_args.args[0],
            "jarz_pos.api.notifications.send_expense_approval_alert",
        )

    def test_manager_filed_request_does_not_alert(self):
        """requires_approval=0 is submitted on the spot - nobody has to decide."""
        enqueue = self._run_after_insert({"requires_approval": 0, "docstatus": 0})
        enqueue.assert_not_called()

    def test_already_rejected_request_does_not_alert(self):
        enqueue = self._run_after_insert(
            {"requires_approval": 1, "docstatus": 0, "rejection_reason": "No"}
        )
        enqueue.assert_not_called()

    def test_a_test_run_queues_nothing(self):
        """A queued job is not rolled back by the tearDown that undoes the row."""
        from jarz_pos.doctype.jarz_expense_request.jarz_expense_request import (
            JarzExpenseRequest,
        )

        with patch.object(notifications.frappe, "enqueue") as enqueue:
            JarzExpenseRequest.after_insert(_expense_doc())

        enqueue.assert_not_called()

    def test_a_queue_failure_never_fails_the_expense(self):
        from jarz_pos.doctype.jarz_expense_request.jarz_expense_request import (
            JarzExpenseRequest,
        )

        with _NotInATestRun():
            with patch.object(
                notifications.frappe, "enqueue", side_effect=RuntimeError("no redis")
            ), patch.object(notifications.frappe, "log_error") as log_error:
                JarzExpenseRequest.after_insert(_expense_doc())

        log_error.assert_called_once()
        # Deferred, so a damaged transaction cannot make the log write itself
        # the thing that rolls the expense back.
        self.assertTrue(log_error.call_args.kwargs.get("defer_insert"))


class TestExpenseApprovalWebPush(unittest.TestCase):
    """Most managers are on the web PWA: 6 subscriptions vs 3 Android tokens."""

    def test_web_tag_is_per_request_not_per_type(self):
        """A web tag REPLACES: a shared one hides the earlier request."""
        fake_messaging = SimpleNamespace(
            WebpushNotification=lambda **kw: SimpleNamespace(**kw),
            WebpushConfig=lambda **kw: SimpleNamespace(**kw),
            WebpushFCMOptions=lambda **kw: SimpleNamespace(**kw),
        )

        def tag_for(name):
            data = notifications._prepare_expense_approval_data_payload(
                notifications._build_expense_approval_payload(_expense_doc(name=name))
            )
            with patch.object(
                notifications, "messaging", fake_messaging, create=True
            ):
                config = notifications._build_webpush_config(data, "t", "b")
            return config.notification.tag

        first = tag_for("JEXP-2026-00042")
        second = tag_for("JEXP-2026-00043")

        self.assertEqual(first, "JEXP-2026-00042")
        self.assertEqual(second, "JEXP-2026-00043")
        self.assertNotEqual(first, second)

    def test_invoice_web_tag_is_unchanged(self):
        fake_messaging = SimpleNamespace(
            WebpushNotification=lambda **kw: SimpleNamespace(**kw),
            WebpushConfig=lambda **kw: SimpleNamespace(**kw),
            WebpushFCMOptions=lambda **kw: SimpleNamespace(**kw),
        )
        data = {"type": "new_invoice", "invoice_id": "SINV-0003"}
        with patch.object(notifications, "messaging", fake_messaging, create=True):
            config = notifications._build_webpush_config(data, "t", "b")

        self.assertEqual(config.notification.tag, "SINV-0003")


if __name__ == "__main__":
    unittest.main()

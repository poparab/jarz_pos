"""A test run must not ring a real branch's phones.

CI's backend suite runs against the LIVE staging site, so a test that submits a
Sales Invoice fires the real `on_submit` hooks. `frappe.db.rollback()` in
tearDown undoes the invoice; it cannot undo an FCM message already handed to
Google. Before this guard, every push to `main` touching `jarz_pos/**` pushed
"New Order: _TEST B2B Branch <hex>" (Nasr city) to every phone signed into the
staging app, for an invoice that no longer existed -- so the app could neither
accept nor dismiss the alert and it kept re-alarming.

The guard is deliberately at the doc-event boundary, not inside
`handle_invoice_submission`: the notification unit tests call that function
directly and must still see it push.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarz_pos.api import notifications
from jarz_pos.events import sales_invoice as sales_invoice_events


class _NotInATestRun:
    """Context manager that makes the module believe it is serving live traffic.

    Assigns and restores by hand rather than using `patch.object`: `frappe.flags`
    is a `frappe._dict`, whose `__dict__` is None, so `patch.object` dies in
    `get_original` with "'NoneType' object is not subscriptable".
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


class TestPushSuppressedInTestRun(unittest.TestCase):
    def test_suppressed_while_this_very_suite_is_running(self):
        # This assertion is only meaningful because it runs under `bench
        # run-tests`, which sets the flag the guard reads.
        self.assertTrue(notifications.outbound_alerts_suppressed("unit"))

    def test_not_suppressed_when_serving_live_traffic(self):
        with _NotInATestRun():
            self.assertFalse(notifications.outbound_alerts_suppressed("unit"))

    def test_on_submit_hook_pushes_nothing_during_a_test_run(self):
        doc = SimpleNamespace(
            name="ACC-SINV-2026-99999",
            is_return=0,
            is_pos=1,
            custom_kanban_profile="Nasr city",
            pos_profile="Nasr city",
            status="Paid",
        )

        with patch.object(notifications, "handle_invoice_submission") as handle, patch.object(
            sales_invoice_events, "_safe_publish"
        ) as fallback_publish:
            sales_invoice_events.publish_new_invoice(doc)

        handle.assert_not_called()
        # The suppressed path is a clean return, not a swallowed exception --
        # the legacy fallback publish is for real failures only.
        fallback_publish.assert_not_called()

    def test_on_submit_hook_still_pushes_when_serving_live_traffic(self):
        """The guard must not silence the hook it exists for."""
        doc = SimpleNamespace(
            name="ACC-SINV-2026-99998",
            is_return=0,
            is_pos=1,
            custom_kanban_profile="Nasr city",
            pos_profile="Nasr city",
            status="Paid",
        )

        with _NotInATestRun():
            with patch.object(notifications, "handle_invoice_submission") as handle:
                sales_invoice_events.publish_new_invoice(doc)

        handle.assert_called_once_with(doc)

    def test_reassignment_and_cancellation_push_nothing_during_a_test_run(self):
        with patch.object(notifications, "_build_invoice_alert_payload") as build:
            notifications.notify_invoice_reassignment("ACC-SINV-2026-99997", "Nasr city")
            notifications.notify_invoice_cancellation("ACC-SINV-2026-99997", "test")

        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()

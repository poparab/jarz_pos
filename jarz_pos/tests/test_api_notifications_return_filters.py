"""Credit notes must never be mistaken for incoming orders.

A credit note built by ``services.invoice_return`` inherits ``is_pos``,
``custom_kanban_profile`` and ``custom_acceptance_status`` from the invoice it
reverses — all three are ``no_copy: 0``. Every notification path in
``api.notifications`` therefore has to exclude ``is_return`` explicitly, or a
post-dispatch return alerts branch staff about an order that does not exist.

Verified against staging before these guards existed: of 9,558 submitted
invoices carrying a ``custom_kanban_profile``, exactly one was not
``is_pos=1, is_return=0`` — credit note ACC-SINV-2026-17056 — and it went
through the on-submit push path to all 10 users on the "Nasr city" profile.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class TestNotificationReturnFilters(unittest.TestCase):

    # ── Query filters ────────────────────────────────────────────────────────

    def test_pos_order_filters_exclude_returns_and_non_pos(self):
        from jarz_pos.api import notifications

        self.assertEqual(
            notifications.POS_ORDER_FILTERS,
            {"docstatus": 1, "is_pos": 1, "is_return": 0},
        )

    def test_pending_alert_query_filters_out_credit_notes(self):
        from jarz_pos.api import notifications

        captured = []

        def fake_get_all(doctype, filters=None, **kwargs):
            captured.append(filters or {})
            return []

        with patch.object(notifications.frappe, "get_all", side_effect=fake_get_all):
            notifications._get_pending_alert_rows_for_profiles(["Nasr city"], "2026-08-05 00:00:00")

        # Both the effective-profile query and the legacy pos_profile fallback.
        self.assertEqual(len(captured), 2)
        for filters in captured:
            self.assertEqual(filters.get("is_return"), 0)
            self.assertEqual(filters.get("is_pos"), 1)
            self.assertEqual(filters.get("docstatus"), 1)

    def test_recent_invoice_polling_filters_out_credit_notes(self):
        from jarz_pos.api import notifications

        captured = []

        def fake_get_all(doctype, filters=None, **kwargs):
            captured.append(filters or {})
            return []

        with patch.object(notifications.frappe, "get_all", side_effect=fake_get_all):
            result = notifications.get_recent_invoices(minutes=5)

        self.assertTrue(result["success"])
        self.assertEqual(len(captured), 2)
        for filters in captured:
            self.assertEqual(filters.get("is_return"), 0)
            self.assertEqual(filters.get("is_pos"), 1)

    def test_update_check_counts_match_the_recent_invoice_filters(self):
        from jarz_pos.api import notifications

        captured = []

        def fake_count(doctype, filters=None, **kwargs):
            captured.append(filters or {})
            return 0

        with patch.object(notifications.frappe.db, "count", side_effect=fake_count):
            result = notifications.check_for_updates()

        self.assertTrue(result["success"])
        self.assertEqual(len(captured), 2)
        for filters in captured:
            self.assertEqual(filters.get("is_return"), 0)
            self.assertEqual(filters.get("is_pos"), 1)

    # ── on_submit push path ──────────────────────────────────────────────────

    def _submitting_doc(self, **overrides):
        base = {
            "name": "ACC-SINV-2026-17056",
            "is_return": 1,
            "is_pos": 0,
            "custom_kanban_profile": "Nasr city",
            "pos_profile": "Nasr city",
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_credit_note_submit_sends_no_new_order_push(self):
        """The path that actually reached staff — a bare `is_return` short-circuit."""
        from jarz_pos.api import notifications

        with patch.object(notifications, "_build_invoice_alert_payload") as build, patch.object(
            notifications, "_publish_invoice_alert"
        ) as publish, patch.object(notifications, "_push_new_invoice") as push:
            notifications.handle_invoice_submission(self._submitting_doc())

        build.assert_not_called()
        publish.assert_not_called()
        push.assert_not_called()

    def test_ordinary_pos_invoice_still_pushes(self):
        """The guard must not silence the case the hook exists for."""
        from jarz_pos.api import notifications

        payload = {"invoice_id": "ACC-SINV-2026-17055-1", "requires_acceptance": True}

        with patch.object(
            notifications, "_build_invoice_alert_payload", return_value=payload
        ), patch.object(
            notifications, "_resolve_recipients_for_payload", return_value=["staff@jarz.test"]
        ), patch.object(
            notifications, "_publish_invoice_alert"
        ) as publish, patch.object(
            notifications, "_push_new_invoice", return_value={"fcm": {}, "vapid": {}}
        ) as push:
            notifications.handle_invoice_submission(
                self._submitting_doc(name="ACC-SINV-2026-17055-1", is_return=0, is_pos=1)
            )

        publish.assert_called_once()
        push.assert_called_once()

    # ── Acceptance guard ─────────────────────────────────────────────────────

    def test_credit_note_cannot_be_acknowledged(self):
        from jarz_pos.api import notifications

        doc = SimpleNamespace(name="ACC-SINV-2026-17056", docstatus=1, is_return=1)

        with patch.object(notifications.frappe, "get_doc", return_value=doc), patch.object(
            notifications, "_ensure_user_can_accept"
        ) as can_accept:
            with self.assertRaises(Exception):
                notifications.acknowledge_invoice("ACC-SINV-2026-17056")

        can_accept.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Stopping a Work Order has to do what ERPNext's own stop_unstop does.

`_stop_work_order` had a primary path and a fallback, and on ERPNext 16 the
primary path could never run: `stop_unstop` is a module-level function, not a
`WorkOrder` method, so `doc.stop_unstop(...)` raised `AttributeError` on every
call. The fallback -- a bare status write -- was therefore the ONLY path the
function had ever taken, and every cancel from the Running tab leaked its
`reserved_qty_for_production`.

These tests exist because that failure was invisible: the cancel succeeded, the
board updated, and the only symptom was a reservation nobody looks at. A
fallback that is always taken looks like resilience and is actually the bug, so
the primary path is pinned by name here.
"""

import unittest
from unittest.mock import MagicMock, call, patch


class FakeWorkOrder:
    """A Work Order document exposing exactly ERPNext 16's real surface.

    Deliberately does NOT define `stop_unstop`: that is the whole point. If the
    code under test reaches for it again, these tests fail with AttributeError
    rather than silently falling back the way production did.
    """

    def __init__(self):
        self.flags = MagicMock()
        self.calls = []

    def update_status(self, status=None):
        self.calls.append(("update_status", status))
        return status

    def update_planned_qty(self):
        self.calls.append(("update_planned_qty", None))

    def notify_update(self):
        self.calls.append(("notify_update", None))


class TestStopWorkOrder(unittest.TestCase):
    def _run(self, doc, get_doc_raises=False):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            if get_doc_raises:
                mock_frappe.get_doc.side_effect = RuntimeError("boom")
            else:
                mock_frappe.get_doc.return_value = doc
            manufacturing._stop_work_order("MFG-WO-jarz-2026-00029")
            return mock_frappe

    def test_calls_what_erpnexts_stop_unstop_calls(self):
        """`update_status` is the one that matters: it runs
        `update_required_items`, which is what actually releases
        `reserved_qty_for_production`."""
        doc = FakeWorkOrder()
        mock_frappe = self._run(doc)

        self.assertEqual(
            doc.calls,
            [
                ("update_status", "Stopped"),
                ("update_planned_qty", None),
                ("notify_update", None),
            ],
        )
        # The happy path must not touch the status column directly.
        mock_frappe.db.set_value.assert_not_called()
        mock_frappe.log_error.assert_not_called()

    def test_does_not_reach_for_stop_unstop(self):
        """On ERPNext 16 `stop_unstop` is a module function, not a method, and
        it is whitelisted behind a Work Order write permission a Production
        Operator does not hold."""
        doc = FakeWorkOrder()
        self.assertFalse(
            hasattr(doc, "stop_unstop"),
            "the fake models ERPNext 16 - stop_unstop is not a document method",
        )
        self._run(doc)  # would raise AttributeError if the old call came back

    def test_falls_back_to_a_status_write_when_the_document_cannot_be_loaded(self):
        """A cancel whose material has already been returned must not fail on
        the last step -- that leaves the batch on the board with an empty WIP,
        which reads as "nothing was transferred" and invites a second start."""
        mock_frappe = self._run(None, get_doc_raises=True)

        mock_frappe.db.set_value.assert_called_once_with(
            "Work Order",
            "MFG-WO-jarz-2026-00029",
            "status",
            "Stopped",
            update_modified=False,
        )
        # And it must be loud: a silent fallback is what hid this for months.
        mock_frappe.log_error.assert_called_once()

    def test_a_failure_midway_still_falls_back(self):
        """`update_status` succeeding and `update_planned_qty` throwing must not
        leave the Work Order running."""
        doc = FakeWorkOrder()
        doc.update_planned_qty = MagicMock(side_effect=RuntimeError("boom"))

        mock_frappe = self._run(doc)
        self.assertIn(("update_status", "Stopped"), doc.calls)
        mock_frappe.db.set_value.assert_called_once()

    def test_ignores_permissions_on_the_document(self):
        """The endpoint's own gate is ROLES.PRODUCTION_EXECUTE; a Production
        Operator holds no Work Order write permission in Frappe's terms."""
        doc = FakeWorkOrder()
        self._run(doc)
        self.assertEqual(doc.flags.ignore_permissions, True)


if __name__ == "__main__":
    unittest.main()

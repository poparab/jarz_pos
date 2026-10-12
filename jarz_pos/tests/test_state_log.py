"""``jarz_pos.services.state_log`` - the per-transition history of kanban states.

Pure ``unittest`` with mocks: no site, no DB, no ``erpnext.tests.utils``.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import state_log as sl


class _Flags(dict):
    """frappe's flags object supports ``.get`` and attribute access."""

    def __getattr__(self, item):
        return self.get(item)


class _Doc:
    """Minimal stand-in for a Sales Invoice document."""

    def __init__(self, name="SINV-1", state="In Progress", before_state=None,
                 is_return=0, pos_profile="Dokki", flags=None):
        self.name = name
        self._data = {
            "custom_sales_invoice_state": state,
            "is_return": is_return,
            "pos_profile": pos_profile,
        }
        self.flags = _Flags() if flags is None else flags
        self._before = None
        if before_state is not None:
            self._before = _Doc(name=name, state=before_state)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def get_doc_before_save(self):
        return self._before


def _patched_frappe(insert_side_effect=None):
    fake = MagicMock()
    fake.session.user = "someone@example.com"
    fake.utils.now_datetime.return_value = "2026-10-13 10:00:00"
    fake.db.get_value.return_value = "Dokki"
    row = MagicMock()
    row.name = "ISL-2026-0000001"
    if insert_side_effect is not None:
        row.insert.side_effect = insert_side_effect
    fake.get_doc.return_value = row
    return fake, row


class TestRecordStateChange(unittest.TestCase):
    def test_writes_one_row_with_both_states(self):
        fake, row = _patched_frappe()
        with patch.object(sl, "frappe", fake):
            name = sl.record_state_change("SINV-1", "Recieved", "In Progress", "kanban")
        self.assertEqual(name, "ISL-2026-0000001")
        values = fake.get_doc.call_args.args[0]
        self.assertEqual(values["doctype"], "Invoice State Log")
        self.assertEqual(values["sales_invoice"], "SINV-1")
        self.assertEqual(values["from_state"], "Recieved")
        self.assertEqual(values["to_state"], "In Progress")
        self.assertEqual(values["source"], "kanban")
        self.assertEqual(values["changed_by"], "someone@example.com")
        self.assertEqual(values["changed_at"], "2026-10-13 10:00:00")
        row.insert.assert_called_once_with(ignore_permissions=True)

    def test_same_state_is_a_no_op(self):
        fake, row = _patched_frappe()
        with patch.object(sl, "frappe", fake):
            self.assertIsNone(sl.record_state_change("SINV-1", "In Progress", "In Progress", "kanban"))
            self.assertIsNone(sl.record_state_change("SINV-1", " In Progress ", "In Progress", "kanban"))
        row.insert.assert_not_called()

    def test_empty_target_or_invoice_is_a_no_op(self):
        fake, row = _patched_frappe()
        with patch.object(sl, "frappe", fake):
            self.assertIsNone(sl.record_state_change("SINV-1", "Recieved", "", "x"))
            self.assertIsNone(sl.record_state_change("SINV-1", "Recieved", None, "x"))
            self.assertIsNone(sl.record_state_change("", "Recieved", "In Progress", "x"))
        row.insert.assert_not_called()

    def test_first_entry_has_no_from_state(self):
        fake, _row = _patched_frappe()
        with patch.object(sl, "frappe", fake):
            sl.record_state_change("SINV-1", None, "Recieved", "submit")
        self.assertIsNone(fake.get_doc.call_args.args[0]["from_state"])

    def test_explicit_user_time_and_profile_are_used(self):
        fake, _row = _patched_frappe()
        with patch.object(sl, "frappe", fake):
            sl.record_state_change(
                "SINV-1", "Ready", "In Progress", "migration:drop_ready",
                user="Administrator", at="2026-10-13 01:00:00", pos_profile="Nasr city",
            )
        values = fake.get_doc.call_args.args[0]
        self.assertEqual(values["changed_by"], "Administrator")
        self.assertEqual(values["changed_at"], "2026-10-13 01:00:00")
        self.assertEqual(values["pos_profile"], "Nasr city")
        fake.db.get_value.assert_not_called()

    def test_profile_is_looked_up_when_not_given(self):
        fake, _row = _patched_frappe()
        with patch.object(sl, "frappe", fake):
            sl.record_state_change("SINV-1", "Recieved", "In Progress", "cancel")
        fake.db.get_value.assert_called_once_with("Sales Invoice", "SINV-1", "pos_profile")
        self.assertEqual(fake.get_doc.call_args.args[0]["pos_profile"], "Dokki")


class TestNeverRaises(unittest.TestCase):
    def test_insert_failure_is_swallowed_and_reported(self):
        fake, _row = _patched_frappe(insert_side_effect=RuntimeError("db gone"))
        with patch.object(sl, "frappe", fake):
            self.assertIsNone(sl.record_state_change("SINV-1", "Recieved", "In Progress", "kanban"))
        fake.log_error.assert_called_once()

    def test_log_error_raising_too_is_still_swallowed(self):
        fake, _row = _patched_frappe(insert_side_effect=RuntimeError("db gone"))
        fake.log_error.side_effect = RuntimeError("log_error exploded")
        with patch.object(sl, "frappe", fake):
            self.assertIsNone(sl.record_state_change("SINV-1", "Recieved", "In Progress", "kanban"))

    def test_lookup_failure_is_swallowed(self):
        fake, _row = _patched_frappe()
        fake.db.get_value.side_effect = RuntimeError("lookup failed")
        with patch.object(sl, "frappe", fake):
            self.assertIsNone(sl.record_state_change("SINV-1", "Recieved", "In Progress", "kanban"))


class TestSubmitHook(unittest.TestCase):
    def test_logs_the_first_state_from_nothing(self):
        doc = _Doc(state="Recieved")
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_submit(doc)
        rec.assert_called_once_with("SINV-1", None, "Recieved", "submit", pos_profile="Dokki")

    def test_source_flag_is_honoured(self):
        doc = _Doc(state="Recieved", flags=_Flags(jarz_state_source="pos_checkout"))
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_submit(doc)
        self.assertEqual(rec.call_args.args[3], "pos_checkout")

    def test_credit_notes_are_skipped(self):
        doc = _Doc(state="Returned", is_return=1)
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_submit(doc)
        rec.assert_not_called()

    def test_hook_never_raises(self):
        doc = _Doc(state="Recieved")
        with patch.object(sl, "record_state_change", side_effect=RuntimeError("boom")), \
                patch.object(sl, "frappe", MagicMock()):
            sl.log_state_on_submit(doc)


class TestUpdateAfterSubmitHook(unittest.TestCase):
    def test_logs_from_and_to_using_the_doc_before_save(self):
        doc = _Doc(state="In Progress", before_state="Recieved",
                   flags=_Flags(jarz_state_source="kanban"))
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_update_after_submit(doc)
        rec.assert_called_once_with("SINV-1", "Recieved", "In Progress", "kanban", pos_profile="Dokki")

    def test_default_source_is_save(self):
        doc = _Doc(state="Out for Delivery", before_state="In Progress")
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_update_after_submit(doc)
        self.assertEqual(rec.call_args.args[3], "save")

    def test_unchanged_state_writes_nothing(self):
        # record_state_change itself no-ops on from == to; check end to end.
        doc = _Doc(state="In Progress", before_state="In Progress")
        fake, row = _patched_frappe()
        with patch.object(sl, "frappe", fake):
            sl.log_state_on_update_after_submit(doc)
        row.insert.assert_not_called()

    def test_credit_notes_are_skipped(self):
        doc = _Doc(state="Returned", before_state="Recieved", is_return=1)
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_update_after_submit(doc)
        rec.assert_not_called()

    def test_missing_doc_before_save_logs_from_nothing(self):
        doc = _Doc(state="In Progress")
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_update_after_submit(doc)
        self.assertEqual(rec.call_args.args[1], "")

    def test_hook_never_raises(self):
        doc = _Doc(state="In Progress", before_state="Recieved")
        doc.get_doc_before_save = MagicMock(side_effect=RuntimeError("no snapshot"))
        with patch.object(sl, "frappe", MagicMock()):
            sl.log_state_on_update_after_submit(doc)


class TestLostTransactionIsNotSwallowed(unittest.TestCase):
    """A deadlock or lock-wait timeout means InnoDB already rolled the whole
    transaction back; swallowing it would let the caller commit half a move."""

    def test_record_reraises_a_deadlock(self):
        fake, _ = _patched_frappe(insert_side_effect=sl.QueryDeadlockError("deadlock"))
        with patch.object(sl, "frappe", fake):
            with self.assertRaises(sl.QueryDeadlockError):
                sl.record_state_change("SINV-1", "Recieved", "In Progress", "kanban")

    def test_record_reraises_a_lock_wait_timeout(self):
        fake, _ = _patched_frappe(insert_side_effect=sl.QueryTimeoutError("lock wait"))
        with patch.object(sl, "frappe", fake):
            with self.assertRaises(sl.QueryTimeoutError):
                sl.record_state_change("SINV-1", "Recieved", "In Progress", "kanban")

    def test_hooks_reraise_a_deadlock(self):
        boom = sl.QueryDeadlockError("deadlock")
        with patch.object(sl, "record_state_change", side_effect=boom):
            with self.assertRaises(sl.QueryDeadlockError):
                sl.log_state_on_submit(_Doc(state="Recieved"))
            with self.assertRaises(sl.QueryDeadlockError):
                sl.log_state_on_update_after_submit(
                    _Doc(state="In Progress", before_state="Recieved"))


class TestBranchFollowsTransfer(unittest.TestCase):
    def test_hook_logs_the_board_branch_after_a_transfer(self):
        doc = _Doc(state="Out for Delivery", before_state="In Progress", pos_profile="Dokki")
        doc._data["custom_kanban_profile"] = "Nasr city"
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_update_after_submit(doc)
        self.assertEqual(rec.call_args.kwargs["pos_profile"], "Nasr city")

    def test_falls_back_to_pos_profile(self):
        with patch.object(sl, "record_state_change") as rec:
            sl.log_state_on_submit(_Doc(state="Recieved", pos_profile="Dokki"))
        self.assertEqual(rec.call_args.kwargs["pos_profile"], "Dokki")


if __name__ == "__main__":
    unittest.main()

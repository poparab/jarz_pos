"""Every approve/confirm/start action must have a way to say no.

The system grew a collection of one-way doors: ``approve_expense`` with no
reject, ``confirm_receipt`` with no reject, ``start_production_batch`` with no
cancel, ``close_plan`` as the only exit from a planned day. Each of them left
the operator a single button, so "no" was expressed by doing nothing — which is
indistinguishable from nobody having looked yet.

These tests cover the reverse actions and, more importantly, the refusals: the
cases where the reverse action must NOT be allowed to run, because the forward
one already moved money or stock.

Follows ``test_api_manufacturing_start_finish``: pure ``unittest.TestCase``,
modules imported inside the test body, and the module-level ``frappe`` symbol
patched wholesale so nothing reaches a database.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


NOW = datetime(2026, 9, 2, 10, 15, 0)


class Thrown(Exception):
    """What a wired-up ``frappe.throw`` raises in these tests."""


def wire_throw(mock_frappe):
    """Make ``frappe.throw`` actually raise, carrying its message.

    A bare ``MagicMock`` returns quietly, so the code under test walks straight
    past its own guard and every refusal test becomes a false pass. A plain
    ``side_effect = Thrown`` is not enough either — mock raises the class with no
    arguments and the message never reaches the assertion.
    """

    def _throw(message, *_args, **_kwargs):
        raise Thrown(str(message))

    mock_frappe.throw.side_effect = _throw
    mock_frappe.PermissionError = Thrown
    return mock_frappe


def translate(module_path):
    """Neutralise ``from frappe import _`` for one module.

    ``patch("<module>.frappe")`` does not cover that binding, and the real
    translator wants a request context to resolve a language.
    """
    return patch(f"{module_path}._", new=lambda msg: msg)


# ══════════════════════════════════════════════════════════════════════════
# Expenses — approve had no counterpart at all
# ══════════════════════════════════════════════════════════════════════════


class _ExpenseDoc:
    """Enough of ``Jarz Expense Request`` to exercise reject/cancel."""

    def __init__(self, docstatus=0, **overrides):
        self.name = overrides.pop("name", "JEXP-00001")
        self.docstatus = docstatus
        self.rejected_by = overrides.pop("rejected_by", None)
        self.rejected_on = overrides.pop("rejected_on", None)
        self.rejection_reason = overrides.pop("rejection_reason", None)
        self.journal_entry = overrides.pop("journal_entry", None)
        self.flags = SimpleNamespace(ignore_permissions=False)
        self.save = MagicMock()
        self.cancel = MagicMock()
        self.reload = MagicMock()
        self.add_comment = MagicMock()
        for key, value in overrides.items():
            setattr(self, key, value)

    def as_dict(self):
        return {
            "name": self.name,
            "docstatus": self.docstatus,
            "rejected_by": self.rejected_by,
            "rejected_on": self.rejected_on,
            "rejection_reason": self.rejection_reason,
        }


class TestRejectExpense(unittest.TestCase):
    def _run(self, doc, reason="Not a business expense", is_manager=True):
        from jarz_pos.api import expenses

        with patch("jarz_pos.api.expenses._is_manager", return_value=is_manager), patch(
            "jarz_pos.api.expenses._serialize_expense", side_effect=lambda d, **_: d
        ), patch("jarz_pos.api.expenses.now_datetime", return_value=NOW), translate(
            "jarz_pos.api.expenses"
        ), patch(
            "jarz_pos.api.expenses.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            mock_frappe.session.user = "manager@jarz.test"
            mock_frappe.get_doc.return_value = doc
            return expenses.reject_expense(doc.name, reason)

    def test_stamps_who_rejected_and_why_and_keeps_the_row(self):
        doc = _ExpenseDoc(docstatus=0)

        self._run(doc)

        self.assertEqual("manager@jarz.test", doc.rejected_by)
        self.assertEqual(NOW, doc.rejected_on)
        self.assertEqual("Not a business expense", doc.rejection_reason)
        # Kept as a draft, not deleted: the requester has to be able to read
        # the reason, and a row that vanishes teaches people to re-file it.
        self.assertEqual(0, doc.docstatus)
        doc.save.assert_called_once()

    def test_requires_a_reason(self):
        doc = _ExpenseDoc(docstatus=0)

        with self.assertRaises(Thrown) as ctx:
            self._run(doc, reason="   ")

        self.assertIn("reason is required", str(ctx.exception))
        doc.save.assert_not_called()

    def test_non_manager_cannot_reject(self):
        doc = _ExpenseDoc(docstatus=0)

        with self.assertRaises(Thrown):
            self._run(doc, is_manager=False)

        doc.save.assert_not_called()

    def test_refuses_an_already_approved_expense_and_names_the_alternative(self):
        """The important refusal.

        An approved expense has already posted its Journal Entry. Silently
        "rejecting" it would leave the money out of the account with a document
        that reads Rejected, so the endpoint declines and points at cancel,
        which actually reverses the ledger.
        """
        doc = _ExpenseDoc(docstatus=1, journal_entry="ACC-JV-0001")

        with self.assertRaises(Thrown) as ctx:
            self._run(doc)

        self.assertIn("Cancel it instead", str(ctx.exception))
        doc.save.assert_not_called()

    def test_refuses_to_reject_twice(self):
        doc = _ExpenseDoc(docstatus=0, rejection_reason="already said no")

        with self.assertRaises(Thrown) as ctx:
            self._run(doc)

        self.assertIn("already rejected", str(ctx.exception))
        doc.save.assert_not_called()


class TestCancelExpense(unittest.TestCase):
    def _run(self, doc, reason="Paid twice", is_manager=True):
        from jarz_pos.api import expenses

        with patch("jarz_pos.api.expenses._is_manager", return_value=is_manager), patch(
            "jarz_pos.api.expenses._serialize_expense", side_effect=lambda d, **_: d
        ), patch("jarz_pos.api.expenses.now_datetime", return_value=NOW), translate(
            "jarz_pos.api.expenses"
        ), patch(
            "jarz_pos.api.expenses.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            mock_frappe.session.user = "manager@jarz.test"
            mock_frappe.get_doc.return_value = doc
            result = expenses.cancel_expense(doc.name, reason)
            return result, mock_frappe

    def test_cancels_the_document_so_its_journal_entry_is_reversed(self):
        doc = _ExpenseDoc(docstatus=1, journal_entry="ACC-JV-0001")

        _, mock_frappe = self._run(doc)

        # The reversal itself is `JarzExpenseRequest.on_cancel`; what this
        # endpoint owes is calling cancel at all, and recording why.
        doc.cancel.assert_called_once()
        args = mock_frappe.db.set_value.call_args.args
        self.assertEqual("Jarz Expense Request", args[0])
        self.assertEqual("JEXP-00001", args[1])
        self.assertEqual("Paid twice", args[2]["rejection_reason"])
        self.assertEqual("manager@jarz.test", args[2]["rejected_by"])

    def test_refuses_a_draft_and_names_reject(self):
        doc = _ExpenseDoc(docstatus=0)

        with self.assertRaises(Thrown) as ctx:
            self._run(doc)

        self.assertIn("Reject it instead", str(ctx.exception))
        doc.cancel.assert_not_called()

    def test_refuses_an_already_cancelled_expense(self):
        doc = _ExpenseDoc(docstatus=2)

        with self.assertRaises(Thrown) as ctx:
            self._run(doc)

        self.assertIn("already cancelled", str(ctx.exception))
        doc.cancel.assert_not_called()

    def test_requires_a_reason(self):
        doc = _ExpenseDoc(docstatus=1)

        with self.assertRaises(Thrown):
            self._run(doc, reason="")

        doc.cancel.assert_not_called()


class TestExpenseSerialisationOfRejection(unittest.TestCase):
    """A rejected request never leaves docstatus 0, so the docstatus alone
    cannot tell "refused" from "still waiting". The serialiser has to."""

    def _serialise(self, doc):
        from jarz_pos.api import expenses

        with patch("jarz_pos.api.expenses._account_label_map", return_value={}), patch(
            "jarz_pos.api.expenses._bilingual_label_from_account",
            return_value={"label_en": "x", "label_ar": "x"},
        ):
            return expenses._serialize_expense(doc, account_labels={})

    def test_pending_request_reads_as_pending(self):
        payload = self._serialise(
            {"name": "JEXP-1", "docstatus": 0, "requires_approval": 1, "status": None}
        )
        self.assertEqual("Pending Approval", payload["status"])
        self.assertFalse(payload["is_rejected"])

    def test_rejected_draft_reads_as_rejected_not_pending(self):
        payload = self._serialise(
            {
                "name": "JEXP-1",
                "docstatus": 0,
                "requires_approval": 1,
                "status": None,
                "rejection_reason": "Not approved",
                "rejected_by": "manager@jarz.test",
                "rejected_on": NOW,
            }
        )
        self.assertEqual("Rejected", payload["status"])
        self.assertTrue(payload["is_rejected"])
        self.assertEqual("Not approved", payload["rejection_reason"])

    def test_rejected_draft_is_not_also_awaiting_approval_on_the_timeline(self):
        payload = self._serialise(
            {
                "name": "JEXP-1",
                "docstatus": 0,
                "requires_approval": 1,
                "creation": NOW,
                "status": None,
                "rejection_reason": "Not approved",
                "rejected_on": NOW,
            }
        )
        labels = [entry["label"] for entry in payload["timeline"]]
        self.assertIn("Rejected", labels)
        self.assertNotIn("Awaiting Approval", labels)

    def test_a_cancelled_expense_reads_as_cancelled_not_rejected(self):
        payload = self._serialise(
            {
                "name": "JEXP-1",
                "docstatus": 2,
                "status": None,
                "rejection_reason": "Paid twice",
                "rejected_on": NOW,
            }
        )
        self.assertEqual("Cancelled", payload["status"])
        labels = [entry["label"] for entry in payload["timeline"]]
        self.assertIn("Cancelled", labels)
        self.assertNotIn("Rejected", labels)


# ══════════════════════════════════════════════════════════════════════════
# Manufacturing — start/finish with no way to abort
# ══════════════════════════════════════════════════════════════════════════


def work_order(**overrides):
    doc = {
        "name": "WO-0001",
        "docstatus": 1,
        "company": "Jarz Co",
        "production_item": "PIST-CAKE",
        "qty": 50.0,
        "produced_qty": 0.0,
        "material_transferred_for_manufacturing": 50.0,
        "status": "In Process",
        "stock_uom": "Nos",
        "wip_warehouse": "WIP - J",
        "source_warehouse": "Raw Material - J",
    }
    doc.update(overrides)
    return SimpleNamespace(**doc)


LEFTOVER = [{"item_code": "FLOUR", "warehouse": "WIP - J", "qty": 12.0}]


class TestCancelProductionBatch(unittest.TestCase):
    def _run(self, wo, reason="Wrong flavour started", leftover=None):
        from jarz_pos.api import manufacturing

        leftover = LEFTOVER if leftover is None else leftover

        with patch("jarz_pos.api.manufacturing._ensure_production_execute_access"), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc", return_value=wo
        ), patch(
            "jarz_pos.api.manufacturing._get_wip_leftover_rows", return_value=leftover
        ), patch(
            "jarz_pos.api.manufacturing._post_wip_return",
            return_value={"stock_entry": "STE-RETURN", "returned_items": leftover},
        ) as mock_return, patch(
            "jarz_pos.api.manufacturing._stop_work_order"
        ) as mock_stop, patch(
            "jarz_pos.api.manufacturing._stamp_work_order"
        ) as mock_stamp, patch(
            "jarz_pos.api.manufacturing._resolve_current_user", return_value="ops@jarz.test"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime", return_value=NOW
        ), patch(
            "jarz_pos.api.manufacturing.get_datetime", side_effect=lambda v: v
        ), patch(
            "jarz_pos.api.manufacturing._debug_log"
        ), translate(
            "jarz_pos.api.manufacturing"
        ), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            result = manufacturing.cancel_production_batch(wo.name, reason)

        return result, mock_return, mock_stop, mock_stamp

    def test_returns_wip_then_stops_the_work_order(self):
        """Both halves matter.

        Stopping the Work Order without returning the material leaves stock in a
        warehouse nobody counts. Returning the material without stopping leaves
        the batch on the floor board with an empty WIP, which reads as "nothing
        was transferred" and invites a second start.
        """
        result, mock_return, mock_stop, _ = self._run(work_order())

        mock_return.assert_called_once()
        self.assertIn(
            "Wrong flavour started", mock_return.call_args.kwargs["remarks"]
        )
        mock_stop.assert_called_once_with("WO-0001")
        self.assertEqual("STE-RETURN", result["stock_entry"])
        self.assertEqual("Stopped", result["status"])

    def test_stamps_who_cancelled_and_why(self):
        _, _, _, mock_stamp = self._run(work_order())

        mock_stamp.assert_called_once_with(
            "WO-0001",
            {
                "jarz_cancelled_by": "ops@jarz.test",
                "jarz_cancelled_at": NOW,
                "jarz_cancel_reason": "Wrong flavour started",
            },
        )

    def test_refuses_once_anything_has_been_produced(self):
        """The load-bearing refusal.

        A Manufacture entry means finished goods are in stock. Cancelling then
        would either strand them or require reversing a stock posting, so the
        batch must be finished for what it actually made.
        """
        _, _, _, _ = None, None, None, None

        with self.assertRaises(Thrown) as ctx:
            self._run(work_order(produced_qty=12.0))

        self.assertIn("already been produced", str(ctx.exception))
        self.assertIn("Finish the batch", str(ctx.exception))

    def test_cancels_a_batch_whose_wip_was_already_returned_by_hand(self):
        """No leftover is not an error here.

        ``return_wip_to_store`` is right to refuse when there is nothing to
        move; a cancel is not, because the batch still has to leave the board.
        """
        result, mock_return, mock_stop, _ = self._run(work_order(), leftover=[])

        mock_return.assert_not_called()
        mock_stop.assert_called_once_with("WO-0001")
        self.assertIsNone(result["stock_entry"])

    def test_requires_a_reason(self):
        with self.assertRaises(Thrown) as ctx:
            self._run(work_order(), reason="  ")

        self.assertIn("reason is required", str(ctx.exception))

    def test_refuses_an_unsubmitted_work_order(self):
        with self.assertRaises(Thrown) as ctx:
            self._run(work_order(docstatus=0))

        self.assertIn("not submitted", str(ctx.exception))

    def test_refuses_a_batch_that_is_already_stopped(self):
        with self.assertRaises(Thrown) as ctx:
            self._run(work_order(status="Stopped"))

        self.assertIn("already Stopped", str(ctx.exception))

    def test_locks_the_row_before_reading_produced_qty(self):
        """Two tablets racing cancel against finish must not both see zero."""
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_production_execute_access"), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_doc", return_value=work_order()
        ) as mock_resolve, patch(
            "jarz_pos.api.manufacturing._get_wip_leftover_rows", return_value=[]
        ), patch(
            "jarz_pos.api.manufacturing._stop_work_order"
        ), patch(
            "jarz_pos.api.manufacturing._stamp_work_order"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_current_user", return_value="ops@jarz.test"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime", return_value=NOW
        ), patch(
            "jarz_pos.api.manufacturing.get_datetime", side_effect=lambda v: v
        ), patch(
            "jarz_pos.api.manufacturing._debug_log"
        ), translate(
            "jarz_pos.api.manufacturing"
        ), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            manufacturing.cancel_production_batch("WO-0001", "mistake")

        self.assertTrue(mock_resolve.call_args.kwargs.get("for_update"))


class TestStopWorkOrder(unittest.TestCase):
    def test_uses_the_erpnext_method_not_a_raw_status_write(self):
        """``stop_unstop`` also releases reserved quantities and updates the
        production plan; a bare ``db_set`` would skip both."""
        from jarz_pos.api import manufacturing

        doc = MagicMock()
        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = doc
            manufacturing._stop_work_order("WO-0001")

        doc.stop_unstop.assert_called_once_with("Stopped")
        mock_frappe.db.set_value.assert_not_called()

    def test_falls_back_to_a_status_write_when_the_method_is_unavailable(self):
        """The material is already back on the shelf by the time this runs.
        Failing here would leave the batch on the board with an empty WIP."""
        from jarz_pos.api import manufacturing

        doc = MagicMock()
        doc.stop_unstop.side_effect = AttributeError("no such method")
        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = doc
            manufacturing._stop_work_order("WO-0001")

        mock_frappe.db.set_value.assert_called_once_with(
            "Work Order", "WO-0001", "status", "Stopped", update_modified=False
        )


# ══════════════════════════════════════════════════════════════════════════
# Payment receipts — confirm with no counterpart
# ══════════════════════════════════════════════════════════════════════════


class _ReceiptDoc:
    def __init__(self, status="Unconfirmed", pos_profile="Nasr city"):
        self.name = "PREC-0001"
        self.status = status
        self.pos_profile = pos_profile
        self.rejected_by = None
        self.rejected_date = None
        self.rejection_reason = None
        self.confirmed_by = None
        self.confirmed_date = None
        self.save = MagicMock()


class TestRejectReceipt(unittest.TestCase):
    def _run(self, doc, reason="Screenshot shows a different order"):
        from jarz_pos.api import payment_receipts

        with patch(
            "jarz_pos.api.payment_receipts._ensure_payment_receipt_confirm_access"
        ), translate("jarz_pos.api.payment_receipts"), patch(
            "jarz_pos.api.payment_receipts.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            mock_frappe.session.user = "manager@jarz.test"
            mock_frappe.utils.now.return_value = "2026-09-02 10:15:00"
            mock_frappe.get_doc.return_value = doc
            return payment_receipts.reject_receipt(doc.name, reason)

    def test_records_who_rejected_and_why(self):
        doc = _ReceiptDoc()

        result = self._run(doc)

        self.assertEqual("Rejected", doc.status)
        self.assertEqual("manager@jarz.test", doc.rejected_by)
        self.assertEqual("Screenshot shows a different order", doc.rejection_reason)
        self.assertEqual("Rejected", result["status"])
        doc.save.assert_called_once()

    def test_requires_a_reason(self):
        doc = _ReceiptDoc()

        with self.assertRaises(Thrown):
            self._run(doc, reason="")

        doc.save.assert_not_called()

    def test_refuses_a_confirmed_receipt(self):
        """Confirming is what a Payment Entry is later posted against.
        Un-rejecting the evidence after the fact is a ledger reversal."""
        doc = _ReceiptDoc(status="Confirmed")

        with self.assertRaises(Thrown) as ctx:
            self._run(doc)

        self.assertIn("already confirmed", str(ctx.exception))
        doc.save.assert_not_called()

    def test_rejecting_twice_is_idempotent_rather_than_an_error(self):
        doc = _ReceiptDoc(status="Rejected")

        result = self._run(doc)

        self.assertEqual("Rejected", result["status"])
        doc.save.assert_not_called()


class TestConfirmReceiptAfterRejection(unittest.TestCase):
    def test_confirming_clears_the_rejection_stamp(self):
        """The way back from a rejection made in error.

        Refusing would strand the receipt in a state with no exit and push the
        branch to upload a duplicate image instead.
        """
        from jarz_pos.api import payment_receipts

        doc = _ReceiptDoc(status="Rejected")
        doc.rejected_by = "manager@jarz.test"
        doc.rejection_reason = "wrong amount"

        with patch(
            "jarz_pos.api.payment_receipts._ensure_payment_receipt_confirm_access"
        ), patch("jarz_pos.api.payment_receipts.frappe") as mock_frappe:
            wire_throw(mock_frappe)
            mock_frappe.session.user = "manager@jarz.test"
            mock_frappe.utils.now.return_value = "2026-09-02 10:15:00"
            mock_frappe.get_doc.return_value = doc
            payment_receipts.confirm_receipt(doc.name)

        self.assertEqual("Confirmed", doc.status)
        self.assertIsNone(doc.rejected_by)
        self.assertIsNone(doc.rejection_reason)


# ══════════════════════════════════════════════════════════════════════════
# Daily production plan — closing was the only exit
# ══════════════════════════════════════════════════════════════════════════


class _PlanDoc:
    def __init__(self, status="Planned"):
        self.name = "JPP-0001"
        self.status = status
        self.cancellation_reason = None
        self.closed_on = None
        self.closed_by = None
        self.save = MagicMock()


class TestCancelPlan(unittest.TestCase):
    def _run(self, doc, reason="Mixer down all day"):
        from jarz_pos.api import daily_plan

        with patch("jarz_pos.api.daily_plan._ensure_execute_access"), patch(
            "jarz_pos.api.daily_plan._serialise", side_effect=lambda d: {"status": d.status}
        ), translate("jarz_pos.api.daily_plan"), patch(
            "jarz_pos.api.daily_plan.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            mock_frappe.session.user = "ops@jarz.test"
            mock_frappe.db.exists.return_value = True
            mock_frappe.utils.now_datetime.return_value = NOW
            mock_frappe.get_doc.return_value = doc
            return daily_plan.cancel_plan(doc.name, reason)

    def test_marks_the_plan_cancelled_with_a_reason(self):
        doc = _PlanDoc()

        result = self._run(doc)

        self.assertEqual("Cancelled", doc.status)
        self.assertEqual("Mixer down all day", doc.cancellation_reason)
        self.assertEqual("ops@jarz.test", doc.closed_by)
        self.assertEqual("Cancelled", result["status"])
        doc.save.assert_called_once()

    def test_refuses_a_closed_plan(self):
        """A closed day records what was actually made. Cancelling it after the
        fact would erase a real production record."""
        doc = _PlanDoc(status="Closed")

        with self.assertRaises(Thrown) as ctx:
            self._run(doc)

        self.assertIn("already closed", str(ctx.exception))
        doc.save.assert_not_called()

    def test_refuses_to_cancel_twice(self):
        doc = _PlanDoc(status="Cancelled")

        with self.assertRaises(Thrown) as ctx:
            self._run(doc)

        self.assertIn("already cancelled", str(ctx.exception))
        doc.save.assert_not_called()

    def test_requires_a_reason(self):
        doc = _PlanDoc()

        with self.assertRaises(Thrown):
            self._run(doc, reason="")

        doc.save.assert_not_called()


class TestClosePlanRefusesACancelledDay(unittest.TestCase):
    def test_close_plan_will_not_close_a_cancelled_plan(self):
        from jarz_pos.api import daily_plan

        doc = _PlanDoc(status="Cancelled")

        with patch("jarz_pos.api.daily_plan._ensure_execute_access"), translate(
            "jarz_pos.api.daily_plan"
        ), patch("jarz_pos.api.daily_plan.frappe") as mock_frappe:
            wire_throw(mock_frappe)
            mock_frappe.db.exists.return_value = True
            mock_frappe.get_doc.return_value = doc

            with self.assertRaises(Thrown) as ctx:
                daily_plan.close_plan("JPP-0001")

        self.assertIn("cancelled", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()

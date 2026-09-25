"""Settlement terms API + reminders (api/settlement_terms.py,
services/settlement_reminders.py) -- mocked, no DocType needed.

What is pinned, and why:

* **save validation runs before anything is written.** A bad weekday / month
  day / interval is refused through ``frappe.throw`` and no document is built;
  the write gate runs before the validation. A valid save clears the fields of
  other cycles, and re-saving a fortnightly schedule keeps its stored anchor
  (otherwise every edit would silently shift the grid).
* **get_collections_due** orders rows overdue -> due_today -> due_soon ->
  unscheduled -> ok, drops customers with nothing open, and refuses a branch the
  caller is not assigned to.
* **get_settlement_terms** payload shape (the wire contract with Flutter).
* **the approvals queue** is None for a caller without credit-ledger access and
  a count otherwise; the count is cached.
* **reminders**: bookkeeping is written BEFORE the push is queued; the tagged
  ToDo is re-dated / created / closed by marker only; the Invoice-after-Invoice
  on-submit trigger fires only for a credit invoice with older open credit
  invoices; nothing leaves the process in a test run.

Pure ``unittest`` with mocks (see test_task_board for why not FrappeTestCase).
Patches target the module under test (``settlement_terms.frappe`` etc.) and the
helpers it imported by name -- patching ``credit.frappe`` would miss them.
"""

import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe  # noqa: F401  (real frappe: this module runs under bench)

from jarz_pos.api import settlement_terms as st
from jarz_pos.services import settlement_reminders as sr
from jarz_pos.services import settlement_schedule as ss

D = datetime.date
THU = D(2026, 9, 24)
FRI = D(2026, 9, 25)


class _Thrown(Exception):
    pass


def _throwing(message="throw", *args, **kwargs):
    """A ``frappe.throw`` that raises (a MagicMock one would let execution continue)."""
    raise _Thrown(str(message))


def _inv(name, posting, amount, customer="C1", customer_name="Cafe One"):
    return {
        "name": name,
        "posting_date": posting,
        "outstanding_amount": amount,
        "customer": customer,
        "customer_name": customer_name,
    }


# ─────────────────────────────────────────────────────────────────────────────
# save_settlement_terms
# ─────────────────────────────────────────────────────────────────────────────


class TestSaveValidation(unittest.TestCase):
    def _save(self, fr, **kwargs):
        params = {"customer": "C1", "cycle": "Weekly", "weekdays": "Thu"}
        params.update(kwargs)
        return st.save_settlement_terms(**params)

    def _frappe(self):
        fr = MagicMock()
        fr.throw.side_effect = _throwing
        fr.db.exists.return_value = True
        fr.db.get_value.return_value = None
        return fr

    def test_bad_values_are_refused_before_any_write(self):
        cases = [
            {"weekdays": "Thx"},
            {"weekdays": ""},
            {"week_interval": 0},
            {"cycle": "Days of Month", "month_days": "32"},
            {"cycle": "Days of Month", "month_days": ""},
            {"cycle": "Every N Days", "interval_days": 0},
            {"cycle": "Every N Days", "interval_days": None},
            {"cycle": "Monthly"},
            {"remind_days_before": -1},
            {"overdue_repeat_days": 0},
            {"anchor_date": "2026-02-30"},
        ]
        for case in cases:
            with self.subTest(case=case):
                fr = self._frappe()
                with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_payment_access"):
                    with self.assertRaises(_Thrown):
                        self._save(fr, **case)
                fr.new_doc.assert_not_called()
                fr.get_doc.assert_not_called()

    def test_write_gate_runs_first(self):
        fr = self._frappe()
        with patch.object(st, "frappe", fr), patch.object(
            st, "_ensure_credit_payment_access", side_effect=_Thrown("denied")
        ), patch.object(ss, "normalize_terms_input") as normalize:
            with self.assertRaises(_Thrown):
                self._save(fr)
        normalize.assert_not_called()
        fr.new_doc.assert_not_called()

    def test_missing_customer_is_refused(self):
        fr = self._frappe()
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_payment_access"):
            with self.assertRaises(_Thrown):
                self._save(fr, customer="  ")

    def test_missing_doctype_is_refused(self):
        fr = self._frappe()
        fr.db.exists.side_effect = lambda doctype, *a, **k: doctype == "Customer"
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_payment_access"):
            with self.assertRaises(_Thrown):
                self._save(fr)
        fr.new_doc.assert_not_called()

    def test_valid_save_inserts_normalized_values(self):
        fr = self._frappe()
        doc = MagicMock()
        fr.new_doc.return_value = doc
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_payment_access"), \
                patch.object(st, "_terms_payload", return_value={"success": True}) as payload:
            result = self._save(fr, weekdays='["thu", "Mon"]', month_days="15", notes=" pays cash ")
        self.assertEqual(result, {"success": True})
        payload.assert_called_once_with("C1")
        fr.new_doc.assert_called_once_with(st.TERMS_DOCTYPE)
        self.assertEqual(doc.customer, "C1")
        written = {c.args[0]: c.args[1] for c in doc.set.call_args_list}
        self.assertEqual(written["cycle"], "Weekly")
        self.assertEqual(written["weekdays"], "Mon,Thu")
        self.assertIsNone(written["month_days"], "fields of other cycles are cleared")
        self.assertEqual(written["notes"], "pays cash")
        doc.insert.assert_called_once_with(ignore_permissions=True)
        doc.save.assert_not_called()

    def test_resave_keeps_the_stored_anchor(self):
        fr = self._frappe()
        fr.db.get_value.return_value = "C1"
        doc = MagicMock()
        doc.get.side_effect = lambda key, default=None: {"anchor_date": "2026-09-03"}.get(key, default)
        fr.get_doc.return_value = doc
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_payment_access"), \
                patch.object(st, "_terms_payload", return_value={"success": True}):
            self._save(fr, week_interval=2)
        written = {c.args[0]: c.args[1] for c in doc.set.call_args_list}
        self.assertEqual(written["anchor_date"], D(2026, 9, 3))
        self.assertEqual(written["week_interval"], 2)
        doc.save.assert_called_once_with(ignore_permissions=True)
        doc.insert.assert_not_called()

    def test_explicit_anchor_wins(self):
        fr = self._frappe()
        fr.db.get_value.return_value = "C1"
        doc = MagicMock()
        doc.get.side_effect = lambda key, default=None: {"anchor_date": "2026-09-03"}.get(key, default)
        fr.get_doc.return_value = doc
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_payment_access"), \
                patch.object(st, "_terms_payload", return_value={"success": True}):
            self._save(fr, cycle="Every N Days", interval_days="10", anchor_date="2026-09-10")
        written = {c.args[0]: c.args[1] for c in doc.set.call_args_list}
        self.assertEqual(written["anchor_date"], D(2026, 9, 10))
        self.assertEqual(written["interval_days"], 10)
        self.assertIsNone(written["weekdays"])


# ─────────────────────────────────────────────────────────────────────────────
# get_settlement_terms
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSettlementTerms(unittest.TestCase):
    def test_payload_shape(self):
        row = {
            "name": "C1", "customer": "C1", "customer_name": "Cafe One", "enabled": 1,
            "cycle": "Weekly", "weekdays": "Thu", "week_interval": 1, "month_days": None,
            "interval_days": None, "anchor_date": None, "remind_days_before": 1,
            "overdue_repeat_days": 2, "responsible_user": None, "notes": "returns ok",
            "last_reminder_on": None, "last_reminder_kind": None, "modified": "2026-09-25 09:00:00",
        }
        with patch.object(st, "_ensure_credit_ledger_access"), \
                patch.object(st, "_require_customer", return_value="C1"), \
                patch.object(st, "_load_terms_row", return_value=row), \
                patch.object(st, "_open_credit_invoices", return_value=[_inv("S1", D(2026, 9, 10), 100)]), \
                patch.object(st, "_credit_currency", return_value="EGP"), \
                patch.object(st, "_today", return_value=FRI), \
                patch.object(st, "_can_edit", return_value=True):
            out = st.get_settlement_terms("C1")
        for key in ("success", "customer", "customer_name", "terms", "description", "status", "currency", "can_edit"):
            self.assertIn(key, out)
        self.assertTrue(out["can_edit"])
        self.assertEqual(out["description"], "Every Thursday")
        self.assertEqual(out["terms"]["weekdays"], ["Thu"])
        self.assertEqual(out["terms"]["month_days"], [])
        self.assertTrue(out["terms"]["exists"])
        self.assertTrue(out["terms"]["enabled"])
        self.assertEqual(out["status"]["state"], "overdue")
        self.assertEqual(out["status"]["overdue_amount"], 100.0)
        for key in ("state", "next_due_date", "next_due_amount", "due_now_amount", "overdue_amount",
                    "open_balance", "oldest_overdue_date", "upcoming_dates"):
            self.assertIn(key, out["status"])

    def test_no_terms_record(self):
        fr = MagicMock()
        fr.db.get_value.return_value = "Cafe One"
        with patch.object(st, "frappe", fr), patch.object(st, "_ensure_credit_ledger_access"), \
                patch.object(st, "_require_customer", return_value="C1"), \
                patch.object(st, "_load_terms_row", return_value=None), \
                patch.object(st, "_open_credit_invoices", return_value=[_inv("S1", D(2026, 9, 10), 100)]), \
                patch.object(st, "_credit_currency", return_value="EGP"), \
                patch.object(st, "_today", return_value=FRI), \
                patch.object(st, "_can_edit", return_value=False):
            out = st.get_settlement_terms("C1")
        self.assertIsNone(out["terms"])
        self.assertIsNone(out["description"])
        self.assertEqual(out["status"]["state"], "unscheduled")
        self.assertFalse(out["can_edit"])


# ─────────────────────────────────────────────────────────────────────────────
# get_collections_due
# ─────────────────────────────────────────────────────────────────────────────


def _terms_row(customer, **values):
    row = {"name": customer, "customer": customer, "enabled": 1, "remind_days_before": 1, "overdue_repeat_days": 2}
    row.update(values)
    return row


OPEN_ROWS = [
    _inv("z1", D(2026, 9, 1), 500, "ZED", "Zed"),
    _inv("a1", D(2026, 9, 24), 90, "ALPHA", "Alpha"),
    _inv("b1", D(2026, 9, 10), 10, "BETA", "Beta"),
    _inv("d1", D(2026, 9, 18), 30, "DELTA", "Delta"),
    _inv("e1", D(2026, 9, 20), 20, "EPS", "Eps"),
    _inv("g1", D(2026, 9, 1), 999, "GAMMA", "Gamma"),
]
TERMS_ROWS = {
    "ALPHA": _terms_row("ALPHA", cycle="Weekly", weekdays="Thu"),
    "BETA": _terms_row("BETA", cycle="Weekly", weekdays="Thu"),
    "DELTA": _terms_row("DELTA", cycle="Weekly", weekdays="Thu", responsible_user="rep@x"),
    "EPS": _terms_row("EPS", cycle="Weekly", weekdays="Fri"),
    "GAMMA": _terms_row("GAMMA", cycle="Days of Month", month_days="20"),
}


class TestCollectionsDue(unittest.TestCase):
    def _call(self, allowed=("B1",), **kwargs):
        with patch.object(st, "_ensure_credit_ledger_access"), \
                patch.object(st, "_allowed_profiles", return_value=list(allowed)), \
                patch.object(st, "_open_credit_invoices", return_value=list(OPEN_ROWS)) as open_q, \
                patch.object(st, "_load_terms_rows", return_value=dict(TERMS_ROWS)), \
                patch.object(st, "_credit_currency", return_value="EGP"), \
                patch.object(st, "_today", return_value=THU):
            out = st.get_collections_due(**kwargs)
        return out, open_q

    def test_order_counts_and_keys(self):
        # days_ahead=0: "due soon" is then each record's own remind_days_before.
        out, open_q = self._call(days_ahead=0)
        self.assertTrue(out["success"])
        self.assertEqual(out["currency"], "EGP")
        self.assertEqual(
            [r["customer"] for r in out["rows"]],
            ["BETA", "GAMMA", "DELTA", "EPS", "ZED", "ALPHA"],
        )
        self.assertEqual(out["counts"]["overdue"], 2)
        self.assertEqual(out["counts"]["due_today"], 1)
        self.assertEqual(out["counts"]["due_soon"], 1)
        open_q.assert_called_once_with(profiles=["B1"])
        delta = [r for r in out["rows"] if r["customer"] == "DELTA"][0]
        self.assertEqual(delta["responsible_user"], "rep@x")
        self.assertEqual(delta["description"], "Every Thursday")
        zed = [r for r in out["rows"] if r["customer"] == "ZED"][0]
        self.assertEqual(zed["state"], "unscheduled")
        self.assertIsNone(zed["cycle"])

    def test_days_ahead_widens_due_soon(self):
        out, _ = self._call()  # default days_ahead = 7
        self.assertEqual(
            [r["customer"] for r in out["rows"]],
            ["BETA", "GAMMA", "DELTA", "EPS", "ALPHA", "ZED"],
        )
        out, _ = self._call(days_ahead=7)
        self.assertEqual(
            [r["customer"] for r in out["rows"]],
            ["BETA", "GAMMA", "DELTA", "EPS", "ALPHA", "ZED"],
        )
        self.assertEqual(out["filters"]["days_ahead"], 7)

    def test_branch_scoping(self):
        out, open_q = self._call(allowed=("B1", "B2"), branch="B2")
        open_q.assert_called_once_with(profiles=["B2"])
        out, open_q = self._call(allowed=("B1",), branch="B9")
        self.assertEqual(out["notice_code"], "branch_not_permitted")
        self.assertEqual(out["rows"], [])
        open_q.assert_not_called()

    def test_no_branch(self):
        out, open_q = self._call(allowed=())
        self.assertEqual(out["notice_code"], "no_branch_assigned")
        open_q.assert_not_called()


class TestCollectionsCount(unittest.TestCase):
    def test_cached_value_short_circuits(self):
        fr = MagicMock()
        fr.session.user = "m@x"
        fr.cache.return_value.get_value.return_value = 5
        with patch.object(st, "frappe", fr), patch.object(st, "_allowed_profiles") as allowed:
            self.assertEqual(st.count_collections_needing_attention(), 5)
        allowed.assert_not_called()

    def test_counts_overdue_and_due_today_only(self):
        fr = MagicMock()
        fr.session.user = "m@x"
        fr.cache.return_value.get_value.return_value = None
        with patch.object(st, "frappe", fr), \
                patch.object(st, "_allowed_profiles", return_value=["B1"]), \
                patch.object(st, "_open_credit_invoices", return_value=list(OPEN_ROWS)), \
                patch.object(st, "_load_terms_rows", return_value=dict(TERMS_ROWS)), \
                patch.object(st, "_today", return_value=THU):
            self.assertEqual(st.count_collections_needing_attention(), 3)  # BETA, GAMMA, DELTA
        fr.cache.return_value.set_value.assert_called_once()


class TestApprovalsQueue(unittest.TestCase):
    def setUp(self):
        from jarz_pos.api import approvals

        self.approvals = approvals

    def test_registered(self):
        keys = [key for key, _ in self.approvals._QUEUES]
        self.assertIn("credit_collections", keys)

    def test_no_access_means_no_queue(self):
        with patch("jarz_pos.api.manager._has_manager_dashboard_access", return_value=False), \
                patch.object(st, "count_collections_needing_attention") as count:
            self.assertIsNone(self.approvals._credit_collections_queue())
        count.assert_not_called()

    def test_count(self):
        with patch("jarz_pos.api.manager._has_manager_dashboard_access", return_value=True), \
                patch.object(st, "count_collections_needing_attention", return_value=3):
            self.assertEqual(self.approvals._credit_collections_queue(), {"count": 3})


# ─────────────────────────────────────────────────────────────────────────────
# Reminders
# ─────────────────────────────────────────────────────────────────────────────


def _summary():
    return {"checked": 0, "sent": 0, "todos_opened": 0, "todos_closed": 0, "errors": 0}


class TestReminderPass(unittest.TestCase):
    ROW = {
        "name": "C1", "customer": "C1", "customer_name": "Cafe One", "enabled": 1,
        "cycle": "Weekly", "weekdays": "Thu", "week_interval": 1, "remind_days_before": 1,
        "overdue_repeat_days": 2, "responsible_user": None,
        "last_reminder_on": None, "last_reminder_kind": None,
    }
    INVOICES = [{"name": "S1", "posting_date": D(2026, 9, 10), "outstanding_amount": 100}]

    def test_bookkeeping_before_push(self):
        parent = MagicMock()
        summary = _summary()
        with patch.object(sr, "frappe") as fr, \
                patch.object(sr, "sync_settlement_todo") as todo, \
                patch.object(sr, "_recipients", return_value=["m@x"]), \
                patch.object(sr, "enqueue_settlement_push", side_effect=lambda *a, **k: parent.enqueue(*a, **k)):
            fr.db.set_value.side_effect = lambda *a, **k: parent.set_value(*a, **k)
            sr._process_terms_row(dict(self.ROW), list(self.INVOICES), FRI, "EGP", summary)
        # The code tests the push's return value, which MagicMock records as
        # ``enqueue().__bool__``; only the two real calls and their order matter.
        self.assertEqual(
            [c[0] for c in parent.mock_calls if "." not in c[0]], ["set_value", "enqueue"]
        )
        set_args = parent.set_value.call_args
        self.assertEqual(set_args.args[0], sr.TERMS_DOCTYPE)
        self.assertEqual(set_args.args[2], {"last_reminder_on": FRI, "last_reminder_kind": "overdue"})
        push_args = parent.enqueue.call_args.args
        self.assertEqual(push_args[0], "C1")
        self.assertEqual(push_args[2], "overdue")
        self.assertEqual(push_args[3], 100.0)
        self.assertEqual(push_args[5], "2026-09-17")
        self.assertEqual(summary["sent"], 1)
        todo.assert_called_once()
        self.assertEqual(todo.call_args.args[1], "2026-09-17")

    def test_same_day_rerun_sends_nothing(self):
        row = dict(self.ROW, last_reminder_on=FRI, last_reminder_kind="overdue")
        with patch.object(sr, "frappe") as fr, patch.object(sr, "sync_settlement_todo"), \
                patch.object(sr, "_recipients", return_value=["m@x"]), \
                patch.object(sr, "enqueue_settlement_push") as push:
            sr._process_terms_row(row, list(self.INVOICES), FRI, "EGP", _summary())
        fr.db.set_value.assert_not_called()
        push.assert_not_called()

    def test_disabled_closes_todos_and_sends_nothing(self):
        row = dict(self.ROW, enabled=0)
        with patch.object(sr, "frappe") as fr, patch.object(sr, "sync_settlement_todo") as todo, \
                patch.object(sr, "enqueue_settlement_push") as push:
            sr._process_terms_row(row, list(self.INVOICES), FRI, "EGP", _summary())
        todo.assert_called_once()
        self.assertIsNone(todo.call_args.args[1])
        push.assert_not_called()
        fr.db.set_value.assert_not_called()

    def test_pass_never_raises(self):
        with patch.object(sr, "frappe") as fr:
            fr.db.exists.side_effect = RuntimeError("db down")
            out = sr.run_settlement_reminders()
        self.assertEqual(out["errors"], 1)


class TestSettlementTodo(unittest.TestCase):
    def test_redate_create_and_close_by_marker(self):
        summary = _summary()
        with patch.object(sr, "frappe") as fr:
            fr.get_all.return_value = [
                {"name": "T1", "allocated_to": "old@x"},
                {"name": "T2", "allocated_to": "m@x"},
            ]
            sr.sync_settlement_todo("C1", "2026-09-17", ["m@x", "n@x"], "Collect 100", summary)
        filters = fr.get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["reference_type"], "Customer")
        self.assertEqual(filters["description"], ["like", "%[jarz:settlement]%"])
        fr.db.set_value.assert_called_once()
        self.assertEqual(fr.db.set_value.call_args.args[:2], ("ToDo", "T2"))
        self.assertEqual(fr.db.set_value.call_args.args[2]["date"], "2026-09-17")
        get_doc_args = [c.args[0] for c in fr.get_doc.call_args_list]
        self.assertIn("ToDo", get_doc_args)  # T1 closed through the document
        created = [a for a in get_doc_args if isinstance(a, dict)]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["allocated_to"], "n@x")
        self.assertTrue(created[0]["description"].startswith("[jarz:settlement]"))
        self.assertEqual(created[0]["reference_name"], "C1")
        self.assertEqual(summary["todos_closed"], 1)
        self.assertEqual(summary["todos_opened"], 1)

    def test_nothing_due_closes_all_of_ours(self):
        summary = _summary()
        with patch.object(sr, "frappe") as fr:
            fr.get_all.return_value = [{"name": "T1", "allocated_to": "a@x"}, {"name": "T2", "allocated_to": "b@x"}]
            sr.sync_settlement_todo("C1", None, ["a@x"], "", summary)
        self.assertEqual(summary["todos_closed"], 2)
        fr.db.set_value.assert_not_called()


class TestInvoiceAfterInvoiceTrigger(unittest.TestCase):
    TERMS = {"name": "C1", "enabled": 1, "cycle": "Invoice after Invoice", "responsible_user": None, "customer_name": "Cafe One"}

    def _doc(self, method="Credit", stamp=30, **extra):
        values = dict(name="SINV-2", is_return=0, customer="C1", customer_name="Cafe One",
                      custom_payment_method=method, custom_credit_terms_days=stamp)
        values.update(extra)
        return SimpleNamespace(**values)

    def _run(self, doc, terms=None, open_rows=None, suppressed=False):
        with patch.object(sr, "frappe") as fr, \
                patch.object(sr, "_suppressed", return_value=suppressed), \
                patch.object(sr, "_recipients", return_value=["m@x"]), \
                patch.object(sr, "enqueue_settlement_push") as push, \
                patch.object(sr, "nowdate", return_value="2026-09-24"), \
                patch("jarz_pos.api.credit._open_credit_invoices", return_value=open_rows or []), \
                patch("jarz_pos.api.credit._credit_currency", return_value="EGP"):
            fr.db.exists.return_value = True
            fr.db.get_value.return_value = self.TERMS if terms is None else terms
            sr.on_sales_invoice_submit(doc)
        return fr, push

    def test_pushes_the_previous_invoices(self):
        rows = [{"name": "SINV-1", "outstanding_amount": 100}, {"name": "SINV-2", "outstanding_amount": 40}]
        _fr, push = self._run(self._doc(), open_rows=rows)
        push.assert_called_once()
        args, kwargs = push.call_args
        self.assertEqual(args[0], "C1")
        self.assertEqual(args[2], ss.KIND_COLLECT_ON_DELIVERY)
        self.assertEqual(args[3], 100.0)
        self.assertEqual(kwargs["invoice"], "SINV-2")

    def test_stamp_alone_counts_as_credit(self):
        rows = [{"name": "SINV-1", "outstanding_amount": 100}]
        _fr, push = self._run(self._doc(method="Instapay", stamp=30), open_rows=rows)
        push.assert_called_once()

    def test_non_credit_invoice_does_no_db_work(self):
        fr, push = self._run(self._doc(method="Cash", stamp=0))
        fr.db.get_value.assert_not_called()
        fr.db.exists.assert_not_called()
        push.assert_not_called()

    def test_return_and_suppression_and_other_cycles(self):
        fr, push = self._run(self._doc(is_return=1))
        push.assert_not_called()
        fr, push = self._run(self._doc(), suppressed=True)
        fr.db.get_value.assert_not_called()
        push.assert_not_called()
        weekly = dict(self.TERMS, cycle="Weekly")
        _fr, push = self._run(self._doc(), terms=weekly, open_rows=[{"name": "SINV-1", "outstanding_amount": 100}])
        push.assert_not_called()
        disabled = dict(self.TERMS, enabled=0)
        _fr, push = self._run(self._doc(), terms=disabled, open_rows=[{"name": "SINV-1", "outstanding_amount": 100}])
        push.assert_not_called()

    def test_no_older_invoice_no_push(self):
        _fr, push = self._run(self._doc(), open_rows=[{"name": "SINV-2", "outstanding_amount": 40}])
        push.assert_not_called()

    def test_never_raises(self):
        with patch.object(sr, "frappe") as fr, patch.object(sr, "_suppressed", return_value=False):
            fr.db.exists.side_effect = RuntimeError("boom")
            sr.on_sales_invoice_submit(self._doc())  # must not raise


class TestPushPlumbing(unittest.TestCase):
    def test_data_payload(self):
        now = datetime.datetime(2026, 9, 24, 9, 0, 0)
        data = sr.build_push_data("C1", "Cafe One", "overdue", 100.0, "EGP", "2026-09-17", "Every Thursday", now=now)
        self.assertEqual(data["type"], "settlement_reminder")
        self.assertEqual(data["kind"], "overdue")
        self.assertEqual(data["customer"], "C1")
        self.assertEqual(data["customer_name"], "Cafe One")
        self.assertEqual(data["route"], "/credit-accounts/detail")
        self.assertEqual(data["amount"], "100.00")
        self.assertEqual(data["due_date"], "2026-09-17")
        self.assertEqual(data["notification_id"], "settlement-C1-overdue-2026-09-24")
        self.assertNotIn("invoice_id", data)
        for key, value in data.items():
            self.assertIsInstance(value, str, key)

    def test_on_the_approvals_channel(self):
        from jarz_pos.api import notifications

        self.assertEqual(sr.NOTIFICATION_TYPE, notifications.SETTLEMENT_REMINDER_NOTIFICATION_TYPE)
        self.assertIn(sr.NOTIFICATION_TYPE, notifications.APPROVAL_NOTIFICATION_TYPES)

    def test_enqueue_suppressed_in_tests(self):
        with patch.object(sr, "frappe") as fr, patch.object(sr, "_suppressed", return_value=True):
            self.assertFalse(sr.enqueue_settlement_push("C1", "Cafe", "overdue", 1.0, "EGP", None, ["m@x"]))
        fr.enqueue.assert_not_called()

    def test_enqueue_after_commit(self):
        with patch.object(sr, "frappe") as fr, patch.object(sr, "_suppressed", return_value=False):
            self.assertTrue(sr.enqueue_settlement_push("C1", "Cafe", "overdue", 1.0, "EGP", None, ["b@x", "a@x", "a@x", ""]))
        kwargs = fr.enqueue.call_args.kwargs
        self.assertTrue(kwargs["enqueue_after_commit"])
        self.assertEqual(kwargs["recipients"], ["a@x", "b@x"])
        self.assertNotIn("event", kwargs)

    def test_send_suppressed_in_tests(self):
        from jarz_pos.api import notifications

        with patch.object(sr, "_suppressed", return_value=True), \
                patch.object(notifications, "_send_fcm_notifications") as fcm:
            out = sr.send_settlement_reminder("C1", "Cafe", "overdue", 1.0, "EGP", None, ["m@x"])
        self.assertEqual(out["status"], "suppressed_test_run")
        fcm.assert_not_called()

    def test_real_suppression_guard_is_consulted(self):
        with patch("jarz_pos.api.notifications.outbound_alerts_suppressed", return_value=True) as guard:
            self.assertTrue(sr._suppressed("settlement_reminder:overdue"))
        guard.assert_called_once()


class TestHooks(unittest.TestCase):
    def test_registered(self):
        from jarz_pos import hooks

        self.assertIn(
            "jarz_pos.services.settlement_reminders.run_settlement_reminders",
            hooks.scheduler_events["cron"]["0 9 * * *"],
        )
        self.assertIn(
            "jarz_pos.services.settlement_reminders.on_sales_invoice_submit",
            hooks.doc_events["Sales Invoice"]["on_submit"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

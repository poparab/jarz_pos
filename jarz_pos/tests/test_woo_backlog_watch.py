"""Tests for the WooCommerce backlog watch (detection-only).

These pin the rules behind ``jarz_pos.services.woo_backlog_watch``:

* what counts as "stuck" (Woo-linked, submitted, non-terminal, overdue) and,
  just as importantly, what does not (a healthy in-flight order, a terminal
  one, a cancelled one);
* the single most important guarantee in the module: NOTHING here ever writes
  to a Sales Invoice, a Payment Entry, a Delivery Note or a Courier
  Transaction -- only to its own ``Jarz Woo Backlog Exception`` rows;
* idempotency across two runs, and an order leaving the queue once it is no
  longer stuck.

They run without a bench: ``frappe`` is stubbed when it cannot be imported,
and every behavioural test swaps the service module's own ``frappe``
reference for a fake, so nothing here needs a site or a database. Mirrors the
approach in ``test_territory_exceptions.py``.
"""

from __future__ import annotations

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal frappe stub so the service module can be imported outside a bench
# ---------------------------------------------------------------------------

def _make_utils_stub():
    utils = types.ModuleType("frappe.utils")
    utils.now_datetime = lambda: datetime.datetime(2026, 9, 7, 12, 0, 0)

    def add_to_date(dt, hours=0, **kwargs):
        return dt + datetime.timedelta(hours=hours)

    utils.add_to_date = add_to_date
    utils.flt = lambda value, *a, **k: float(value or 0)
    utils.get_datetime = lambda value: value
    return utils


def _make_frappe_stub():
    frappe = types.ModuleType("frappe")

    class ValidationError(Exception):
        pass

    class PermissionError_(Exception):
        pass

    class DuplicateEntryError(Exception):
        pass

    frappe.ValidationError = ValidationError
    frappe.PermissionError = PermissionError_
    frappe.DuplicateEntryError = DuplicateEntryError

    def throw(msg, exc=None, title=None):
        raise (exc or ValidationError)(msg)

    frappe.throw = throw
    frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe.db = MagicMock()
    frappe.get_all = MagicMock(return_value=[])
    frappe.get_doc = MagicMock()
    frappe.get_roles = MagicMock(return_value=["System Manager"])
    frappe.get_traceback = MagicMock(return_value="")
    frappe.log_error = MagicMock()
    frappe.logger = MagicMock()
    frappe.session = SimpleNamespace(user="Administrator")
    frappe._ = lambda text: text
    frappe.utils = _make_utils_stub()
    return frappe


try:  # pragma: no cover - depends on whether the runner has a bench
    import frappe as _real_frappe  # noqa: F401
except Exception:  # pragma: no cover
    _stub = _make_frappe_stub()
    sys.modules["frappe"] = _stub
    sys.modules["frappe.utils"] = _stub.utils
else:  # pragma: no cover
    if not hasattr(_real_frappe, "whitelist"):
        _real_frappe.whitelist = lambda *a, **k: (lambda fn: fn)  # type: ignore


from jarz_pos.services import woo_backlog_watch as mod  # noqa: E402


# ---------------------------------------------------------------------------
# Fake frappe used for the behavioural tests
# ---------------------------------------------------------------------------

class _FakeDoc:
    """Just enough of a Document for ``_insert_exception``/auto-close."""

    def __init__(self, data, registry):
        self.data = dict(data)
        self.flags = SimpleNamespace()
        self.name = None
        self._registry = registry
        for key, value in self.data.items():
            setattr(self, key, value)

    def insert(self, ignore_permissions=False):
        self.name = f"WBLE-{len(self._registry) + 1:04d}"
        self._registry.append(self)
        return self

    def save(self, ignore_permissions=False):
        self._registry.append(self)
        return self

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)
        return None


def _fake_frappe(
    *,
    exists=None,
    get_value=None,
    has_column=True,
    table_exists=True,
    sql_result=None,
    get_all=None,
    docs=None,
):
    """A frappe double with only what this module touches.

    ``fake.db.set_value`` and ``fake.get_doc`` record every doctype they are
    called against in ``fake.written_doctypes`` -- the safety test relies on
    that list containing nothing but the exception DocType.
    """
    inserted: list = []
    saved: list = []
    written_doctypes: list = []
    stored = dict(docs or {})
    fake = SimpleNamespace()

    fake.db = SimpleNamespace()
    fake.db.table_exists = MagicMock(return_value=table_exists)
    fake.db.has_column = MagicMock(return_value=has_column)
    fake.db.exists = MagicMock(side_effect=exists or (lambda *a, **k: None))
    fake.db.get_value = MagicMock(side_effect=get_value or (lambda *a, **k: None))
    fake.db.sql = MagicMock(return_value=list(sql_result or []))
    fake.db.savepoint = MagicMock()
    fake.db.rollback = MagicMock()
    fake.db.commit = MagicMock()

    def _set_value(doctype, *args, **kwargs):
        written_doctypes.append(doctype)
        return None

    fake.db.set_value = MagicMock(side_effect=_set_value)

    fake.get_all = MagicMock(side_effect=get_all or (lambda *a, **k: []))

    def _get_doc(*args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict):
            written_doctypes.append(args[0].get("doctype"))
            return _FakeDoc(args[0], inserted)
        doctype, name = args[0], args[1]
        written_doctypes.append(doctype)
        doc = _FakeDoc(stored.get(name, {}), saved)
        doc.name = name
        doc.doctype = doctype
        return doc

    fake.get_doc = MagicMock(side_effect=_get_doc)
    fake.get_roles = MagicMock(return_value=["System Manager"])
    fake.get_traceback = MagicMock(return_value="traceback")
    fake.log_error = MagicMock()
    fake.logger = MagicMock(return_value=SimpleNamespace(setLevel=lambda *_a, **_k: None, info=lambda *a, **k: None))
    fake.session = SimpleNamespace(user="manager@jarz.test")
    fake.utils = _make_utils_stub()
    fake.PermissionError = RuntimeError

    def _throw(msg, exc=None, title=None):
        raise (exc or RuntimeError)(msg)

    fake.throw = _throw
    fake.inserted = inserted
    fake.saved = saved
    fake.written_doctypes = written_doctypes
    return fake


def _candidate_row(**overrides):
    row = {
        "name": "ACC-SINV-2026-00500",
        "woo_order_id": 9001,
        "customer": "CUST-0100",
        "posting_date": datetime.date(2026, 9, 1),
        "posting_time": datetime.timedelta(hours=10),
        "ops_state": "In Progress",
        "pos_profile": "Nasr city",
        "custom_kanban_profile": None,
        "outstanding_amount": 350.0,
        "grand_total": 350.0,
        "currency": "EGP",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Pure detection
# ---------------------------------------------------------------------------

class TestIsTerminalState(unittest.TestCase):
    def test_terminal_states_are_recognised(self):
        for state in ("Delivered", "Cancelled", "Returned"):
            with self.subTest(state=state):
                self.assertTrue(mod.is_terminal_state(state))

    def test_mid_states_are_not_terminal(self):
        for state in ("Recieved", "Received", "In Progress", "Ready", "Out for Delivery", "", None):
            with self.subTest(state=state):
                self.assertFalse(mod.is_terminal_state(state))


class TestIsStuck(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 7, 12, 0, 0)

    def test_finds_a_stuck_woo_invoice(self):
        """Submitted, non-terminal, posted well over 24h ago -> stuck."""
        stuck = mod.is_stuck(
            docstatus=1,
            ops_state="In Progress",
            posting_date=datetime.date(2026, 9, 1),
            posting_time=datetime.timedelta(hours=10),
            now=self.NOW,
        )
        self.assertTrue(stuck)

    def test_ignores_a_healthy_recent_order(self):
        """Posted an hour ago, still In Progress -- perfectly normal."""
        stuck = mod.is_stuck(
            docstatus=1,
            ops_state="In Progress",
            posting_date=datetime.date(2026, 9, 7),
            posting_time=datetime.timedelta(hours=11),
            now=self.NOW,
        )
        self.assertFalse(stuck)

    def test_ignores_a_delivered_order_however_old(self):
        stuck = mod.is_stuck(
            docstatus=1,
            ops_state="Delivered",
            posting_date=datetime.date(2026, 1, 1),
            posting_time=datetime.timedelta(hours=0),
            now=self.NOW,
        )
        self.assertFalse(stuck)

    def test_ignores_a_cancelled_invoice(self):
        """docstatus=2 (cancelled) is never stuck -- there is nothing to fix."""
        stuck = mod.is_stuck(
            docstatus=2,
            ops_state="In Progress",
            posting_date=datetime.date(2026, 1, 1),
            posting_time=datetime.timedelta(hours=0),
            now=self.NOW,
        )
        self.assertFalse(stuck)

    def test_ignores_a_draft_invoice(self):
        stuck = mod.is_stuck(
            docstatus=0,
            ops_state="Received",
            posting_date=datetime.date(2026, 1, 1),
            posting_time=datetime.timedelta(hours=0),
            now=self.NOW,
        )
        self.assertFalse(stuck)

    def test_exactly_at_the_threshold_is_not_yet_stuck(self):
        finish_by = mod.expected_finish_by(datetime.date(2026, 9, 6), datetime.timedelta(hours=12))
        self.assertEqual(finish_by, self.NOW)
        stuck = mod.is_stuck(
            docstatus=1,
            ops_state="Ready",
            posting_date=datetime.date(2026, 9, 6),
            posting_time=datetime.timedelta(hours=12),
            now=self.NOW,
        )
        self.assertFalse(stuck)


class TestExpectedFinishBy(unittest.TestCase):
    def test_handles_timedelta_posting_time(self):
        finish_by = mod.expected_finish_by(
            datetime.date(2026, 9, 1), datetime.timedelta(hours=8, minutes=30)
        )
        self.assertEqual(finish_by, datetime.datetime(2026, 9, 2, 8, 30, 0))

    def test_handles_string_posting_time(self):
        finish_by = mod.expected_finish_by(datetime.date(2026, 9, 1), "08:30:00")
        self.assertEqual(finish_by, datetime.datetime(2026, 9, 2, 8, 30, 0))

    def test_handles_iso_date_string(self):
        finish_by = mod.expected_finish_by("2026-09-01", "00:00:00")
        self.assertEqual(finish_by, datetime.datetime(2026, 9, 2, 0, 0, 0))

    def test_none_date_returns_none(self):
        self.assertIsNone(mod.expected_finish_by(None, datetime.timedelta(0)))

    def test_custom_threshold(self):
        finish_by = mod.expected_finish_by(
            datetime.date(2026, 9, 1), datetime.timedelta(0), stuck_after_hours=48
        )
        self.assertEqual(finish_by, datetime.datetime(2026, 9, 3, 0, 0, 0))


class TestBuildSnapshot(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 7, 12, 0, 0)

    def test_snapshot_carries_the_money_and_branch(self):
        snapshot = mod.build_snapshot(_candidate_row(), now=self.NOW)

        self.assertEqual(snapshot["sales_invoice"], "ACC-SINV-2026-00500")
        self.assertEqual(snapshot["pos_profile"], "Nasr city")
        self.assertEqual(snapshot["outstanding_amount"], 350.0)
        self.assertEqual(snapshot["ops_state"], "In Progress")
        self.assertGreater(snapshot["age_hours"], 0)

    def test_falls_back_to_kanban_profile_when_pos_profile_is_blank(self):
        snapshot = mod.build_snapshot(
            _candidate_row(pos_profile=None, custom_kanban_profile="Dokki"), now=self.NOW
        )
        self.assertEqual(snapshot["pos_profile"], "Dokki")


class TestBuildDetail(unittest.TestCase):
    def test_detail_names_the_order_state_and_branch(self):
        snapshot = mod.build_snapshot(_candidate_row(), now=datetime.datetime(2026, 9, 7, 12, 0, 0))
        detail = mod.build_detail(snapshot)

        self.assertIn("ACC-SINV-2026-00500", detail)
        self.assertIn("9001", detail)
        self.assertIn("In Progress", detail)
        self.assertIn("Nasr city", detail)
        self.assertIn("backlog_migration", detail)

    def test_detail_never_overflows_the_small_text_budget(self):
        snapshot = mod.build_snapshot(
            _candidate_row(customer="C" * 4000), now=datetime.datetime(2026, 9, 7, 12, 0, 0)
        )
        detail = mod.build_detail(snapshot)
        self.assertLessEqual(len(detail), 1000)


# ---------------------------------------------------------------------------
# run_woo_backlog_sweep
# ---------------------------------------------------------------------------

class TestRunWooBacklogSweep(unittest.TestCase):
    def test_finds_and_records_a_stuck_invoice(self):
        fake = _fake_frappe(sql_result=[_candidate_row()])
        with patch.object(mod, "frappe", fake):
            summary = mod.run_woo_backlog_sweep()

        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["created"], 1)
        self.assertEqual(len(fake.inserted), 1)
        payload = fake.inserted[0].data
        self.assertEqual(payload["sales_invoice"], "ACC-SINV-2026-00500")
        self.assertEqual(payload["status"], mod.STATUS_OPEN)

    def test_ignores_a_healthy_invoice_never_returned_by_the_query(self):
        """The SQL query itself excludes healthy orders; an empty result set
        must simply produce an empty, successful summary."""
        fake = _fake_frappe(sql_result=[])
        with patch.object(mod, "frappe", fake):
            summary = mod.run_woo_backlog_sweep()

        self.assertEqual(summary["scanned"], 0)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(fake.inserted, [])

    def test_is_idempotent_across_two_runs(self):
        """Re-running the sweep over the same candidate must not create a
        second row -- only the first run inserts."""
        fake = _fake_frappe(sql_result=[_candidate_row()])
        with patch.object(mod, "frappe", fake):
            first = mod.run_woo_backlog_sweep()
            # Simulate the row now existing for the second pass.
            fake.db.exists = MagicMock(return_value="WBLE-0001")
            second = mod.run_woo_backlog_sweep()

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["already_open"], 1)
        self.assertEqual(len(fake.inserted), 1, "must not insert a second row for the same invoice")

    def test_never_writes_to_a_sales_invoice_or_any_other_doctype(self):
        """The single most important guarantee: nothing here writes to a Sales
        Invoice, Payment Entry, Delivery Note or Courier Transaction. Fails
        loudly if any write path touches anything but the exception DocType."""
        fake = _fake_frappe(sql_result=[_candidate_row(), _candidate_row(name="ACC-SINV-2026-00501")])
        with patch.object(mod, "frappe", fake):
            mod.run_woo_backlog_sweep()

        forbidden = {"Sales Invoice", "Payment Entry", "Delivery Note", "Courier Transaction"}
        self.assertFalse(
            forbidden.intersection(fake.written_doctypes),
            f"wrote to a forbidden doctype: {fake.written_doctypes}",
        )
        self.assertTrue(
            all(d == mod.EXCEPTION_DOCTYPE for d in fake.written_doctypes),
            f"unexpected write target(s): {fake.written_doctypes}",
        )

    def test_bounded_by_the_limit_argument(self):
        fake = _fake_frappe(sql_result=[_candidate_row()])
        with patch.object(mod, "frappe", fake):
            mod.run_woo_backlog_sweep(limit=7)

        _query, params = fake.db.sql.call_args.args
        self.assertEqual(params["limit"], 7)

    def test_never_raises_when_the_query_explodes(self):
        fake = _fake_frappe()
        fake.db.sql = MagicMock(side_effect=RuntimeError("table is on fire"))
        with patch.object(mod, "frappe", fake):
            summary = mod.run_woo_backlog_sweep()

        self.assertEqual(summary["scanned"], 0)

    def test_never_raises_when_the_doctype_is_not_migrated_yet(self):
        fake = _fake_frappe(table_exists=False)
        with patch.object(mod, "frappe", fake):
            summary = mod.run_woo_backlog_sweep()

        self.assertEqual(summary.get("skipped"), "doctype not migrated yet")

    def test_one_bad_row_does_not_abort_the_sweep(self):
        bad = _candidate_row(name="")  # blank invoice name -> build_snapshot fails the guard
        good = _candidate_row(name="ACC-SINV-2026-00777")
        fake = _fake_frappe(sql_result=[bad, good])
        with patch.object(mod, "frappe", fake):
            summary = mod.run_woo_backlog_sweep()

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["created"], 1)


# ---------------------------------------------------------------------------
# close_resolved_backlog_exceptions
# ---------------------------------------------------------------------------

class TestCloseResolvedBacklogExceptions(unittest.TestCase):
    def _run(self, open_rows, invoice_map):
        def _get_all(doctype, **kwargs):
            return list(open_rows) if doctype == mod.EXCEPTION_DOCTYPE else []

        def _get_value(doctype, name=None, fields=None, as_dict=False, **kwargs):
            if doctype == mod.INVOICE_DOCTYPE:
                return invoice_map.get(name)
            return None

        fake = _fake_frappe(
            get_all=_get_all,
            get_value=_get_value,
            docs={row["name"]: {"detail": "original"} for row in open_rows},
        )
        with patch.object(mod, "frappe", fake):
            summary = mod.close_resolved_backlog_exceptions()
        return summary, fake

    def test_an_unstuck_order_is_closed_and_leaves_the_queue(self):
        rows = [{"name": "WBLE-1", "sales_invoice": "SI-1"}]
        invoices = {"SI-1": {"docstatus": 1, "custom_sales_invoice_state": "Delivered",
                              "sales_invoice_state": "Delivered",
                              "outstanding_amount": 0, "grand_total": 350.0}}
        summary, fake = self._run(rows, invoices)

        self.assertEqual(summary["closed"], 1)
        self.assertEqual(len(fake.saved), 1)
        self.assertEqual(fake.saved[0].status, mod.STATUS_RESOLVED)
        self.assertEqual(fake.saved[0].resolved_by, "manager@jarz.test")

    def test_a_cancelled_invoice_is_closed_too(self):
        rows = [{"name": "WBLE-1", "sales_invoice": "SI-1"}]
        invoices = {"SI-1": {"docstatus": 2, "custom_sales_invoice_state": "In Progress",
                              "sales_invoice_state": "In Progress",
                              "outstanding_amount": 0, "grand_total": 350.0}}
        summary, _fake = self._run(rows, invoices)

        self.assertEqual(summary["closed"], 1)

    def test_a_still_stuck_order_stays_open_and_refreshes_its_snapshot(self):
        rows = [{"name": "WBLE-1", "sales_invoice": "SI-1"}]
        invoices = {"SI-1": {"docstatus": 1, "custom_sales_invoice_state": "Ready",
                              "sales_invoice_state": "Ready",
                              "outstanding_amount": 350.0, "grand_total": 350.0}}
        summary, fake = self._run(rows, invoices)

        self.assertEqual(summary["closed"], 0)
        self.assertEqual(fake.saved, [])
        self.assertIn(mod.EXCEPTION_DOCTYPE, fake.written_doctypes)

    def test_a_deleted_invoice_is_closed(self):
        rows = [{"name": "WBLE-1", "sales_invoice": "SI-GONE"}]
        summary, _fake = self._run(rows, {})
        self.assertEqual(summary["closed"], 1)

    def test_never_writes_to_the_invoice_itself(self):
        rows = [{"name": "WBLE-1", "sales_invoice": "SI-1"}]
        invoices = {"SI-1": {"docstatus": 1, "custom_sales_invoice_state": "Ready",
                              "sales_invoice_state": "Ready",
                              "outstanding_amount": 350.0, "grand_total": 350.0}}
        _summary, fake = self._run(rows, invoices)

        self.assertNotIn(mod.INVOICE_DOCTYPE, fake.written_doctypes)


if __name__ == "__main__":
    unittest.main()

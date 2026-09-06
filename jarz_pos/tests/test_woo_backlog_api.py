"""Tests for the read-only WooCommerce backlog queue endpoint.

Covers the permission gate (manager-dashboard tier, mirroring
``api.manager._ensure_manager_dashboard_access``) and the per-day/per-branch
aggregate maths -- the whole point of the endpoint is showing the backlog
ACCUMULATING, so the rollups have to add up exactly.

Runs without a bench, the same way ``test_woo_backlog_watch.py`` does: a
minimal ``frappe`` stub for import, and a fake ``frappe`` swapped in per test.
"""

from __future__ import annotations

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_utils_stub():
    utils = types.ModuleType("frappe.utils")
    utils.now_datetime = lambda: datetime.datetime(2026, 9, 7, 12, 0, 0)
    utils.flt = lambda value, *a, **k: float(value or 0)
    utils.get_datetime = lambda value: value
    return utils


def _make_frappe_stub():
    frappe = types.ModuleType("frappe")

    class PermissionError_(Exception):
        pass

    frappe.PermissionError = PermissionError_

    def throw(msg, exc=None, title=None):
        raise (exc or PermissionError_)(msg)

    frappe.throw = throw
    frappe.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    frappe.db = MagicMock()
    frappe.get_all = MagicMock(return_value=[])
    frappe.get_roles = MagicMock(return_value=["System Manager"])
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


from jarz_pos.api import woo_backlog as mod  # noqa: E402
from jarz_pos.constants import ROLES  # noqa: E402


def _fake_frappe(*, roles=None, get_all=None):
    fake = SimpleNamespace()
    fake.db = SimpleNamespace()
    fake.get_all = MagicMock(side_effect=get_all or (lambda *a, **k: []))
    fake.get_roles = MagicMock(return_value=list(roles or []))
    fake.utils = _make_utils_stub()
    fake._ = lambda text: text

    class PermissionError_(Exception):
        pass

    fake.PermissionError = PermissionError_

    def _throw(msg, exc=None, title=None):
        raise (exc or PermissionError_)(msg)

    fake.throw = _throw
    return fake


def _row(**overrides):
    row = {
        "name": "WBLE-0001",
        "sales_invoice": "ACC-SINV-2026-00500",
        "woo_order_id": 9001,
        "customer": "CUST-0100",
        "pos_profile": "Nasr city",
        "ops_state": "In Progress",
        "posting_date": "2026-09-01",
        "expected_finish_by": datetime.datetime(2026, 9, 2, 10, 0, 0),
        "outstanding_amount": 350.0,
        "grand_total": 350.0,
        "currency": "EGP",
        "first_detected_on": "2026-09-05 08:00:00",
        "last_seen_on": "2026-09-07 06:00:00",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------

class TestPermissionGate(unittest.TestCase):
    def test_system_manager_is_allowed(self):
        fake = _fake_frappe(roles=["System Manager"])
        with patch.object(mod, "frappe", fake):
            mod._ensure_manager_dashboard_access()  # must not raise

    def test_jarz_manager_is_allowed(self):
        fake = _fake_frappe(roles=[ROLES.JARZ_MANAGER])
        with patch.object(mod, "frappe", fake):
            mod._ensure_manager_dashboard_access()

    def test_line_manager_is_allowed(self):
        fake = _fake_frappe(roles=[ROLES.JARZ_LINE_MANAGER])
        with patch.object(mod, "frappe", fake):
            mod._ensure_manager_dashboard_access()

    def test_alt_spelling_line_manager_is_allowed(self):
        fake = _fake_frappe(roles=[ROLES.JARZ_LINE_MANAGER_ALT])
        with patch.object(mod, "frappe", fake):
            mod._ensure_manager_dashboard_access()

    def test_plain_pos_user_is_refused(self):
        fake = _fake_frappe(roles=["POS User"])
        with patch.object(mod, "frappe", fake):
            with self.assertRaises(fake.PermissionError):
                mod._ensure_manager_dashboard_access()

    def test_no_roles_is_refused(self):
        fake = _fake_frappe(roles=[])
        with patch.object(mod, "frappe", fake):
            with self.assertRaises(fake.PermissionError):
                mod._ensure_manager_dashboard_access()

    def test_endpoint_refuses_before_touching_the_database(self):
        """A refused caller must never trigger a query against the queue."""
        fake = _fake_frappe(roles=["POS User"])
        with patch.object(mod, "frappe", fake):
            with self.assertRaises(fake.PermissionError):
                mod.get_woo_backlog_queue()
        fake.get_all.assert_not_called()


# ---------------------------------------------------------------------------
# Aggregate maths
# ---------------------------------------------------------------------------

class TestAggregateMaths(unittest.TestCase):
    def _call(self, rows, branch=None):
        fake = _fake_frappe(roles=["System Manager"], get_all=lambda *a, **k: list(rows))
        with patch.object(mod, "frappe", fake):
            return mod.get_woo_backlog_queue(branch=branch), fake

    def test_totals_sum_every_row_once(self):
        rows = [
            _row(name="WBLE-1", outstanding_amount=350.0),
            _row(name="WBLE-2", outstanding_amount=650.0, sales_invoice="SI-2"),
        ]
        result, _fake = self._call(rows)

        self.assertTrue(result["success"])
        self.assertEqual(result["totals"], {"count": 2, "amount": 1000.0})
        self.assertEqual(len(result["rows"]), 2)

    def test_by_day_groups_on_posting_date(self):
        rows = [
            _row(name="WBLE-1", posting_date="2026-09-01", outstanding_amount=350.0),
            _row(name="WBLE-2", posting_date="2026-09-01", outstanding_amount=200.0, sales_invoice="SI-2"),
            _row(name="WBLE-3", posting_date="2026-09-02", outstanding_amount=500.0, sales_invoice="SI-3"),
        ]
        result, _fake = self._call(rows)

        by_day = {entry["date"]: entry for entry in result["by_day"]}
        self.assertEqual(by_day["2026-09-01"], {"date": "2026-09-01", "count": 2, "amount": 550.0})
        self.assertEqual(by_day["2026-09-02"], {"date": "2026-09-02", "count": 1, "amount": 500.0})
        self.assertEqual(result["totals"], {"count": 3, "amount": 1050.0})

    def test_by_branch_groups_on_pos_profile_and_sorts_by_amount_desc(self):
        rows = [
            _row(name="WBLE-1", pos_profile="Nasr city", outstanding_amount=100.0),
            _row(name="WBLE-2", pos_profile="Dokki", outstanding_amount=900.0, sales_invoice="SI-2"),
            _row(name="WBLE-3", pos_profile="Dokki", outstanding_amount=50.0, sales_invoice="SI-3"),
        ]
        result, _fake = self._call(rows)

        self.assertEqual(
            result["by_branch"],
            [
                {"pos_profile": "Dokki", "count": 2, "amount": 950.0},
                {"pos_profile": "Nasr city", "count": 1, "amount": 100.0},
            ],
        )

    def test_missing_branch_buckets_under_no_branch(self):
        rows = [_row(pos_profile=None)]
        result, _fake = self._call(rows)

        self.assertEqual(result["by_branch"][0]["pos_profile"], "(no branch)")

    def test_branch_filter_is_passed_through_to_the_query(self):
        fake = _fake_frappe(roles=["System Manager"], get_all=lambda *a, **k: [])
        with patch.object(mod, "frappe", fake):
            mod.get_woo_backlog_queue(branch="Dokki")

        _args, kwargs = fake.get_all.call_args
        self.assertEqual(kwargs["filters"]["pos_profile"], "Dokki")
        self.assertEqual(kwargs["filters"]["status"], mod.STATUS_OPEN)

    def test_empty_queue_returns_zeroed_totals(self):
        result, _fake = self._call([])

        self.assertEqual(result["totals"], {"count": 0, "amount": 0.0})
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["by_day"], [])
        self.assertEqual(result["by_branch"], [])


if __name__ == "__main__":
    unittest.main()

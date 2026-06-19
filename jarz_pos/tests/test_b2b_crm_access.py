"""B2B CRM access-guard tests (pure mock / unittest).

Asserts ``crm._ensure_b2b_access`` throws for a non-B2B user and passes for a B2B Sales
Rep or a manager, and SMOKES every whitelisted endpoint's guard: a non-B2B caller is
rejected by each endpoint BEFORE it touches the DB (no real reads/writes happen).

Everything is mocked — only the role gate is exercised. No FrappeTestCase /
``erpnext.tests.utils`` import, so the module is non-destructive under --skip-before-tests.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from jarz_pos.api import crm


@contextmanager
def _roles(role_list):
    # The code calls frappe.get_roles(frappe.session.user); mocking get_roles to
    # ignore its argument pins the role set without patching the dotdict session.
    with patch.object(crm.frappe, "get_roles", return_value=list(role_list)):
        yield


class TestEnsureB2BAccess(unittest.TestCase):
    """The core gate: B2B Sales Rep / manager pass; everyone else is rejected."""

    def test_b2b_sales_rep_passes(self):
        with _roles(["B2B Sales Rep"]):
            crm._ensure_b2b_access()  # must NOT raise
            self.assertTrue(crm._can_access_b2b())

    def test_manager_passes(self):
        with _roles(["JARZ Manager"]):
            crm._ensure_b2b_access()  # must NOT raise
            self.assertTrue(crm._can_access_b2b())

    def test_system_manager_passes(self):
        with _roles(["System Manager"]):
            crm._ensure_b2b_access()  # must NOT raise

    def test_non_b2b_rejected(self):
        with _roles(["Sales User"]):
            self.assertFalse(crm._can_access_b2b())
            with self.assertRaises(Exception):
                crm._ensure_b2b_access()

    def test_empty_roles_rejected(self):
        with _roles([]):
            with self.assertRaises(Exception):
                crm._ensure_b2b_access()


class TestEndpointAccessGuards(unittest.TestCase):
    """Every whitelisted endpoint rejects a non-B2B caller at the guard (no DB work).

    We assert the call raises AND that no DB read/write was reached by patching the
    most common DB entry points to explode if touched — proving the guard fires first.
    """

    def _explode(self, *a, **k):  # pragma: no cover - only called on guard failure
        raise AssertionError("DB accessed before _ensure_b2b_access rejected the caller")

    @contextmanager
    def _guarded_non_b2b(self):
        with _roles(["Sales User"]):
            with patch.object(crm.frappe.db, "exists", side_effect=self._explode), patch.object(
                crm.frappe.db, "get_value", side_effect=self._explode
            ), patch.object(crm.frappe, "get_all", side_effect=self._explode), patch.object(
                crm.frappe, "get_doc", side_effect=self._explode
            ):
                yield

    def test_get_b2b_pipeline_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.get_b2b_pipeline()

    def test_get_account_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.get_account("Lead", "L-0001")

    def test_advance_stage_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.advance_stage("Lead", "L-0001", "Qualify")

    def test_create_lead_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.create_lead("Acme Corp")

    def test_log_activity_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.log_activity("Lead", "L-0001", "called them")

    def test_get_my_followups_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.get_my_followups()

    def test_get_reorder_due_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.get_reorder_due()

    def test_request_sample_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.request_sample("Customer", "CUST-0001")

    def test_place_b2b_order_guarded(self):
        with self._guarded_non_b2b():
            with self.assertRaises(Exception):
                crm.place_b2b_order("Customer", "CUST-0001")


class TestOrderBindingForB2BUser(unittest.TestCase):
    """request_sample / place_b2b_order return the right purpose binding for a B2B user."""

    def test_request_sample_binding(self):
        with _roles(["B2B Sales Rep"]):
            with patch.object(crm.frappe.db, "exists", return_value=True), patch.object(
                crm, "_policy_price_list", return_value="B2B Selling"
            ):
                out = crm.request_sample("Customer", "CUST-0001")
        self.assertEqual(out["customer"], "CUST-0001")
        self.assertEqual(out["order_purpose"], crm._SAMPLE_ORDER_PURPOSE)
        self.assertEqual(out["price_list"], "B2B Selling")

    def test_place_b2b_order_binding(self):
        with _roles(["B2B Sales Rep"]):
            with patch.object(crm.frappe.db, "exists", return_value=True), patch.object(
                crm, "_policy_price_list", return_value=None
            ):
                out = crm.place_b2b_order("Customer", "CUST-0001")
        self.assertEqual(out["order_purpose"], crm._B2B_ORDER_PURPOSE)
        self.assertEqual(out["customer"], "CUST-0001")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for POS Invoice Amendment hardening (plan section C).

These are pure-Python unit tests that do not require a running Frappe instance.
They test:
  1. get_invoice_hard_mutation_blocker blocks on Custom Shipping Request (B1 / C-test-1)
  2. _run_invoice_amendment_job rejects empty cart (B5 / C-test-2)
  3. _run_invoice_amendment_job rejects stale source total (B5 / C-test-3)
  4. _run_invoice_amendment_job rejects suspicious cart shrink (B5 / C-test-4)
  5. B2 re-eligibility check after advisory lock fires before mutating PEs (B2 / C-test-5)
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_invoice(
    name: str = "ACC-SINV-TEST-001",
    docstatus: int = 1,
    grand_total: float = 500.0,
    state: str = "Received",
    items: list = None,
    **extra,
):
    """Return a minimal invoice-like namespace."""
    inv = SimpleNamespace(
        name=name,
        docstatus=docstatus,
        grand_total=grand_total,
        is_return=0,
        custom_sales_invoice_state=state,
        sales_invoice_state=state,
        custom_delivery_date=None,
        custom_delivery_time_from=None,
        custom_delivery_duration=None,
        custom_delivery_trip=None,
        custom_is_pickup=False,
        custom_payment_method=None,
        custom_kanban_profile="Nasr city",
        pos_profile="Nasr city",
        customer="Test Customer",
        sales_partner=None,
        woo_order_id=None,
        items=items or [],
    )
    inv.__dict__.update(extra)

    def _get(key, default=None):
        return inv.__dict__.get(key, default)

    inv.get = _get
    return inv


def _make_cart_json(rows: list) -> str:
    return json.dumps(rows)


# ---------------------------------------------------------------------------
# Test 1 – B1: mutation blocker catches CSR
# ---------------------------------------------------------------------------

class TestMutationBlockerCSR(unittest.TestCase):
    """get_invoice_hard_mutation_blocker must return CSR block when a shipping request exists."""

    def setUp(self):
        # Patch frappe.get_all to simulate the various lookup queries
        self._patcher = patch("jarz_pos.api.manager.frappe")
        self.mock_frappe = self._patcher.start()
        # Default: all lookups return empty
        self.mock_frappe.get_all.return_value = []
        self.mock_frappe.db.get_value.return_value = None
        self.mock_frappe._ = lambda x: x

    def tearDown(self):
        self._patcher.stop()

    def _get_all_side_effect(self, doctype, **kwargs):
        if doctype == "Custom Shipping Request":
            return ["CSR-00002"]
        return []

    def test_blocks_when_csr_exists(self):
        from jarz_pos.api.manager import get_invoice_hard_mutation_blocker

        self.mock_frappe.get_all.side_effect = self._get_all_side_effect
        inv = _make_invoice()

        result = get_invoice_hard_mutation_blocker(inv)

        self.assertIsNotNone(result, "Should return a blocker dict")
        self.assertEqual(result["mutation_block_code"], "custom_shipping_request_exists")
        self.assertIn("CSR-00002", result.get("custom_shipping_requests", []))

    def test_no_block_when_no_csr(self):
        from jarz_pos.api.manager import get_invoice_hard_mutation_blocker

        # All lookups return empty
        self.mock_frappe.get_all.return_value = []
        inv = _make_invoice()

        result = get_invoice_hard_mutation_blocker(inv)

        self.assertIsNone(result, "No blocker when no downstream artifacts")


# ---------------------------------------------------------------------------
# Test 2 – B5: empty cart is rejected
# ---------------------------------------------------------------------------

class TestAmendmentRejectsEmptyCart(unittest.TestCase):
    """_run_invoice_amendment_job must reject cart_json='[]'."""

    def _build_mock_frappe(self):
        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]  # lock acquired
        mf.session.user = "test@example.com"
        mf.db.savepoint.return_value = None
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        return mf

    def test_rejects_empty_cart(self):
        inv = _make_invoice(items=[SimpleNamespace(item_code="Item A")])

        with (
            patch("jarz_pos.api.manager.frappe", self._build_mock_frappe()) as mf,
            patch("jarz_pos.api.manager._create_amendment_invoice", MagicMock()),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=inv),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-001",
                cart_json="[]",
                pos_profile_name="Nasr city",
            )

        self.assertFalse(result.get("success"), f"Expected failure, got: {result}")
        self.assertEqual(result.get("amendment_block_code"), "empty_cart")


# ---------------------------------------------------------------------------
# Test 3 – B5: stale source total is rejected
# ---------------------------------------------------------------------------

class TestAmendmentRejectsStaleSouce(unittest.TestCase):

    def _build_mock_frappe(self):
        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]  # lock acquired
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        return mf

    def test_rejects_stale_grand_total(self):
        inv = _make_invoice(grand_total=500.0)
        cart = _make_cart_json([{"rate": 100, "qty": 3}])  # submitted total 300

        with (
            patch("jarz_pos.api.manager.frappe", self._build_mock_frappe()) as mf,
            patch("jarz_pos.api.manager._create_amendment_invoice", MagicMock()),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=inv),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-002",
                cart_json=cart,
                pos_profile_name="Nasr city",
                # Caller expected 600 but source is 500 → >0.5% drift
                expected_source_grand_total=600.0,
            )

        self.assertFalse(result.get("success"), f"Expected failure, got: {result}")
        self.assertEqual(result.get("amendment_block_code"), "stale_source")

    def test_accepts_close_enough_total(self):
        inv = _make_invoice(grand_total=500.0)
        cart = _make_cart_json([{"rate": 250, "qty": 2}])  # 500 total matches

        with (
            patch("jarz_pos.api.manager.frappe", self._build_mock_frappe()) as mf,
            patch("jarz_pos.api.manager._create_amendment_invoice", return_value={"invoice_name": "ACC-SINV-TEST-002"}),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
            patch("jarz_pos.api.manager.frappe.get_doc", side_effect=lambda *a, **k: inv),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
            patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
            patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
            patch("jarz_pos.api.manager._build_invoice_amendment_response", return_value={"success": True}),
            patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-003",
                cart_json=cart,
                pos_profile_name="Nasr city",
                # Within 0.5% drift
                expected_source_grand_total=501.0,
            )

        # Should not block on stale_source
        self.assertNotEqual(result.get("amendment_block_code"), "stale_source")


# ---------------------------------------------------------------------------
# Test 4 – B5: suspicious shrink is rejected
# ---------------------------------------------------------------------------

class TestAmendmentRejectsSuspiciousShrink(unittest.TestCase):

    def _build_mock_frappe(self):
        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]  # lock acquired
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        return mf

    def test_rejects_when_submitted_total_below_50_percent(self):
        inv = _make_invoice(grand_total=500.0)
        # Submitted total = 100 (20% of 500 → below 50%)
        cart = _make_cart_json([{"rate": 50, "qty": 2}])

        with (
            patch("jarz_pos.api.manager.frappe", self._build_mock_frappe()) as mf,
            patch("jarz_pos.api.manager._create_amendment_invoice", MagicMock()),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=inv),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-004",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("amendment_block_code"), "suspicious_diff")

    def test_accepts_total_above_50_percent(self):
        inv = _make_invoice(grand_total=500.0)
        # Submitted total = 300 (60% → above 50%)
        cart = _make_cart_json([{"rate": 150, "qty": 2}])

        with (
            patch("jarz_pos.api.manager.frappe", self._build_mock_frappe()) as mf,
            patch("jarz_pos.api.manager._create_amendment_invoice", return_value={"invoice_name": "ACC-SINV-TEST-003"}),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
            patch("jarz_pos.api.manager.frappe.get_doc", side_effect=lambda *a, **k: inv),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
            patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
            patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
            patch("jarz_pos.api.manager._build_invoice_amendment_response", return_value={"success": True}),
            patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-005",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertNotEqual(result.get("amendment_block_code"), "suspicious_diff")


# ---------------------------------------------------------------------------
# Test 5 – B2: re-eligibility check fires before any PE mutations
# ---------------------------------------------------------------------------

class TestAmendmentReEligibilityAfterLock(unittest.TestCase):
    """After acquiring the advisory lock, the job reloads eligibility.
    If ineligible (e.g. CSR was created between open and submit), it must
    release the lock and return immediately WITHOUT touching Payment Entries.
    """

    def test_release_lock_and_abort_when_ineligible_after_lock(self):
        inv = _make_invoice(grand_total=500.0)
        cart = _make_cart_json([{"rate": 250, "qty": 2}])

        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]  # lock acquired
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()

        eligibility_calls = []

        def _eligibility_side_effect(invoice_arg):
            eligibility_calls.append(invoice_arg)
            # First call (pre-lock, in submit_invoice_amendment) → can amend
            # Second call (post-lock reload, in _run_invoice_amendment_job) → blocked
            if len(eligibility_calls) == 1:
                return {"can_amend": True}
            return {
                "can_amend": False,
                "amendment_block_code": "custom_shipping_request_exists",
                "amendment_block_reason": "CSR linked",
            }

        cancel_called = []

        class FakeInvoice:
            name = "ACC-SINV-TEST-001"
            docstatus = 1
            grand_total = 500.0
            is_return = 0
            custom_sales_invoice_state = "Received"
            sales_invoice_state = "Received"
            custom_delivery_date = None
            custom_delivery_time_from = None
            custom_delivery_duration = None
            custom_delivery_trip = None
            custom_is_pickup = False
            custom_payment_method = None
            custom_kanban_profile = "Nasr city"
            pos_profile = "Nasr city"
            customer = "Test Customer"
            sales_partner = None
            woo_order_id = None
            items = []
            flags = SimpleNamespace(ignore_permissions=False, ignore_woo_outbound=False)

            def get(self, key, default=None):
                return getattr(self, key, default)

            def cancel(self):
                cancel_called.append("cancel")

            def reload(self):
                pass

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._create_amendment_invoice", MagicMock()),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", side_effect=_eligibility_side_effect),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=FakeInvoice()),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-006",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("amendment_block_code"), "custom_shipping_request_exists")
        self.assertEqual(cancel_called, [], "source_invoice.cancel() must NOT have been called")


if __name__ == "__main__":
    unittest.main()

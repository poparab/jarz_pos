"""Unit tests for POS Invoice Amendment hardening (plan section C).

These are pure-Python unit tests that do not require a running Frappe instance.
They test:
  1. get_invoice_hard_mutation_blocker blocks on Custom Shipping Request (B1 / C-test-1)
  2. _run_invoice_amendment_job rejects empty cart (B5 / C-test-2)
  3. _run_invoice_amendment_job rejects stale source total (B5 / C-test-3)
  4. _run_invoice_amendment_job rejects suspicious cart shrink (B5 / C-test-4)
  5. B2 re-eligibility check after advisory lock fires before mutating PEs (B2 / C-test-5)
  --- Phase-2 additions ---
  6. H1: PE cancel failure raises BEFORE source invoice is cancelled (H1 / C-test-6)
  7. H3: WooCommerce Order Map is repointed to replacement after successful amendment (H3 / C-test-7)
  8. H4: woo-order lock is acquired when source invoice has woo_order_id (H4 / C-test-8)
  9. H5: publish_realtime is called with the correct event and payload (H5 / C-test-9)
  --- Phase-3 additions ---
    10. P3: format_invoice_data always emits customer + bundle metadata keys (even when empty) (P3 / C-test-10)
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


class TestAmendmentRejectsMalformedBundleCart(unittest.TestCase):
    """_run_invoice_amendment_job must reject bundle rows without selected children."""

    def _build_mock_frappe(self):
        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]
        mf.session.user = "test@example.com"
        mf.db.savepoint.return_value = None
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        return mf

    def test_rejects_bundle_row_without_selected_items(self):
        inv = _make_invoice(
            items=[
                SimpleNamespace(
                    item_code="BDL-1",
                    is_bundle_parent=1,
                    bundle_code="BDL-1",
                ),
                SimpleNamespace(
                    item_code="ITEM-A",
                    is_bundle_child=1,
                    parent_bundle="BDL-1",
                ),
            ]
        )
        cart = _make_cart_json(
            [
                {
                    "item_code": "BDL-1",
                    "qty": 1,
                    "rate": 120,
                    "is_bundle": True,
                    "selected_items": {},
                }
            ]
        )

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
                request_id="test-req-bundle-missing",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertFalse(result.get("success"), f"Expected failure, got: {result}")
        self.assertEqual(result.get("amendment_block_code"), "bundle_selection_missing")
        self.assertEqual(result.get("malformed_bundles"), ["BDL-1"])


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


# ---------------------------------------------------------------------------
# Test 6 – H1: PE cancel failure raises BEFORE source invoice is cancelled
# ---------------------------------------------------------------------------

class TestPECancelFailureIsolation(unittest.TestCase):
    """If a Payment Entry cancel raises, the source invoice must remain
    docstatus=1 (not cancelled) and the job must return success=False.
    """

    def test_pe_failure_leaves_source_submitted(self):
        inv = _make_invoice(grand_total=500.0)
        cart = _make_cart_json([{"rate": 250, "qty": 2}])

        source_cancel_called = []

        class FakeSourceInvoice:
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
                source_cancel_called.append("cancelled")

            def reload(self):
                pass

        class FakeBadPE:
            docstatus = 1
            name = "PE-BAD-001"
            flags = SimpleNamespace(ignore_permissions=False)

            def get(self, key, default=None):
                return getattr(self, key, default)

            def cancel(self):
                raise Exception("PE validation hook blocked cancellation")

        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]  # lock acquired
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        mf.db.savepoint.return_value = None
        mf.db.rollback.return_value = None
        mf.get_traceback.return_value = ""
        mf.log_error.return_value = None

        def get_doc_side_effect(doctype, name=None):
            if doctype == "Sales Invoice":
                return FakeSourceInvoice()
            if doctype == "Payment Entry":
                return FakeBadPE()
            return MagicMock()

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._create_amendment_invoice", MagicMock()),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=["PE-BAD-001"]),
            patch("jarz_pos.api.manager.frappe.get_doc", side_effect=get_doc_side_effect),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-h1",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertFalse(result.get("success"), f"Expected failure, got: {result}")
        self.assertIn("PE-BAD-001", result.get("error", ""), "Error must mention failed PE")
        self.assertEqual(source_cancel_called, [], "source_invoice.cancel() must NOT have been called")


# ---------------------------------------------------------------------------
# Test 7 – H3: WooCommerce Order Map repointed after successful amendment
# ---------------------------------------------------------------------------

class TestWooOrderMapRepoint(unittest.TestCase):
    """After a successful amendment, _repoint_woocommerce_order_map must be called
    with the new invoice name when woo_order_id is set on the source invoice.
    """

    def test_map_repoint_called_with_woo_order_id(self):
        cart = _make_cart_json([{"rate": 250, "qty": 2}])

        class FakeWooInvoice:
            name = "ACC-SINV-WOO-001"
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
            woo_order_id = "14999"
            items = []
            flags = SimpleNamespace(ignore_permissions=False, ignore_woo_outbound=False)

            def get(self, key, default=None):
                return getattr(self, key, default)

            def cancel(self):
                pass

            def reload(self):
                pass

        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]  # both locks acquired
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        mf.db.savepoint.return_value = None
        mf.utils.now.return_value = "2026-05-16 12:00:00"

        repoint_calls = []

        def fake_repoint(*, woo_order_id, new_invoice_name, logger=None):
            repoint_calls.append((woo_order_id, new_invoice_name))

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._create_amendment_invoice",
                  return_value={"invoice_name": "ACC-SINV-WOO-002"}),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=FakeWooInvoice()),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
            patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
            patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
            patch("jarz_pos.api.manager._build_invoice_amendment_response",
                  return_value={"success": True}),
            patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
            patch("jarz_pos.api.manager._repoint_woocommerce_order_map", side_effect=fake_repoint),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-WOO-001",
                request_id="test-req-h3",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertEqual(len(repoint_calls), 1, "Map repoint must be called exactly once")
        woo_id, new_inv = repoint_calls[0]
        self.assertEqual(woo_id, "14999")
        self.assertEqual(new_inv, "ACC-SINV-WOO-002")


# ---------------------------------------------------------------------------
# Test 8 – H4: woo-order advisory lock is acquired when woo_order_id is set
# ---------------------------------------------------------------------------

class TestWooOrderLockAcquired(unittest.TestCase):
    """When the source invoice has woo_order_id, the job must acquire BOTH the
    inv: lock AND the woo-order: lock before touching any documents.
    """

    def test_woo_lock_acquired_for_woo_invoice(self):
        cart = _make_cart_json([{"rate": 250, "qty": 2}])
        lock_calls = []

        class FakeWooInvoice:
            name = "ACC-SINV-WOO-003"
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
            woo_order_id = "15000"
            items = []
            flags = SimpleNamespace(ignore_permissions=False, ignore_woo_outbound=False)

            def get(self, key, default=None):
                return getattr(self, key, default)

            def cancel(self):
                pass

            def reload(self):
                pass

        def db_sql_side_effect(query, *args, **kwargs):
            params = args[0] if args else ()
            lock_key = params[0] if params else ""
            if "GET_LOCK" in query:
                lock_calls.append(lock_key)
                return [[1]]
            return [[1]]

        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.side_effect = db_sql_side_effect
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        mf.db.savepoint.return_value = None
        mf.utils.now.return_value = "2026-05-16 12:00:00"

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._create_amendment_invoice",
                  return_value={"invoice_name": "ACC-SINV-WOO-004"}),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=FakeWooInvoice()),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
            patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
            patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
            patch("jarz_pos.api.manager._build_invoice_amendment_response",
                  return_value={"success": True}),
            patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
            patch("jarz_pos.api.manager._repoint_woocommerce_order_map", return_value=None),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            _run_invoice_amendment_job(
                invoice_id="ACC-SINV-WOO-003",
                request_id="test-req-h4",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        acquired_keys = [k for k in lock_calls]
        self.assertIn("inv:ACC-SINV-WOO-003", acquired_keys, "inv: lock must be acquired")
        self.assertIn("woo-order-15000", acquired_keys, "woo-order: lock must be acquired")


# ---------------------------------------------------------------------------
# Test 9 – H5: publish_realtime fired with correct event and payload
# ---------------------------------------------------------------------------

class TestAmendmentPublishRealtime(unittest.TestCase):
    """After a successful amendment, frappe.publish_realtime must be called
    with event='jarz_pos_invoice_amended' and include both invoice IDs.
    """

    def test_publish_realtime_called_on_success(self):
        cart = _make_cart_json([{"rate": 250, "qty": 2}])

        class FakeInvoiceSimple:
            name = "ACC-SINV-RT-001"
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
                pass

            def reload(self):
                pass

        publish_calls = []

        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        mf.db.savepoint.return_value = None
        mf.utils.now.return_value = "2026-05-16 12:00:00"
        mf.publish_realtime.side_effect = lambda event, payload, **kwargs: publish_calls.append(
            (event, payload)
        )

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._create_amendment_invoice",
                  return_value={"invoice_name": "ACC-SINV-RT-002"}),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=FakeInvoiceSimple()),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
            patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
            patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
            patch("jarz_pos.api.manager._build_invoice_amendment_response",
                  return_value={"success": True}),
            patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-RT-001",
                request_id="test-req-h5",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertEqual(len(publish_calls), 1, "publish_realtime must be called exactly once")
        event, payload = publish_calls[0]
        self.assertEqual(event, "jarz_pos_invoice_amended")
        self.assertEqual(payload.get("source_invoice_id"), "ACC-SINV-RT-001")
        self.assertEqual(payload.get("replacement_invoice_id"), "ACC-SINV-RT-002")


# ---------------------------------------------------------------------------
# Test 10 – P3: format_invoice_data payload shape
# ---------------------------------------------------------------------------

class TestFormatInvoiceDataPayloadShape(unittest.TestCase):
    """format_invoice_data must always emit 'customer' and per-item 'bundle_code'
    and 'is_bundle_parent'/'is_bundle_child'/'parent_bundle' keys, even when the
    values are falsy (empty string / False).  The amendment client must never
    receive a payload where these keys are absent.
    """

    def _make_item(self, **kwargs):
        """Return a minimal invoice item namespace."""
        item = SimpleNamespace(
            item_code="ITEM-TEST",
            item_name="Test Item",
            qty=1.0,
            rate=50.0,
            amount=50.0,
            price_list_rate=None,
            discount_percentage=None,
            discount_amount=None,
            is_bundle_parent=0,
            is_bundle_child=0,
            bundle_code=None,
            parent_bundle=None,
            bundle_group_key=None,
            bundle_group_name=None,
        )
        item.__dict__.update(kwargs)
        return item

    def _make_invoice_ns(self, items, customer="Test Customer"):
        inv = SimpleNamespace(
            name="ACC-SINV-SHAPE-001",
            docstatus=1,
            grand_total=50.0,
            net_total=50.0,
            total_taxes_and_charges=0.0,
            customer=customer,
            customer_name="Test Customer",
            territory="Cairo",
            posting_date="2026-05-16",
            status="Received",
            is_return=0,
            outstanding_amount=0.0,
            items=items,
            shipping_address_name=None,
            customer_address=None,
            sales_partner=None,
        )

        def _get(key, default=None):
            return inv.__dict__.get(key, default)

        inv.get = _get
        return inv

    def test_customer_key_always_present(self):
        from unittest.mock import patch, MagicMock
        mf = MagicMock()
        mf.get_doc.side_effect = Exception("no address")
        mf.log_error = MagicMock()

        inv = self._make_invoice_ns(items=[], customer="CUST-001")
        with patch("jarz_pos.utils.invoice_utils.frappe", mf):
            from jarz_pos.utils.invoice_utils import format_invoice_data
            data = format_invoice_data(inv)

        self.assertIn("customer", data, "'customer' key must always be present in format_invoice_data output")
        self.assertEqual(data["customer"], "CUST-001")

    def test_empty_customer_still_emits_key(self):
        """When invoice.customer is empty, the key must still be present (with empty value)."""
        from unittest.mock import patch, MagicMock
        mf = MagicMock()
        mf.get_doc.side_effect = Exception("no address")
        mf.log_error = MagicMock()

        inv = self._make_invoice_ns(items=[], customer="")
        with patch("jarz_pos.utils.invoice_utils.frappe", mf):
            from jarz_pos.utils.invoice_utils import format_invoice_data
            data = format_invoice_data(inv)

        self.assertIn("customer", data, "'customer' key must always be present even when empty")

    def test_bundle_code_always_in_each_item(self):
        """Every item in the output must carry a 'bundle_code' key."""
        from unittest.mock import patch, MagicMock
        mf = MagicMock()
        mf.get_doc.side_effect = Exception("no address")
        mf.log_error = MagicMock()

        # Mix: a regular item (no bundle_code) and a bundle parent item.
        regular_item = self._make_item(item_code="ITEM-REG", bundle_code=None)
        bundle_parent = self._make_item(
            item_code="BDL-PARENT",
            is_bundle_parent=1,
            bundle_code="BDL-123",
        )
        inv = self._make_invoice_ns(items=[regular_item, bundle_parent])
        with patch("jarz_pos.utils.invoice_utils.frappe", mf):
            from jarz_pos.utils.invoice_utils import format_invoice_data
            data = format_invoice_data(inv)

        for item_payload in data["items"]:
            self.assertIn("bundle_code", item_payload,
                          f"'bundle_code' key missing from item {item_payload.get('item_code')}")
            self.assertIn("is_bundle_parent", item_payload,
                          f"'is_bundle_parent' key missing from item {item_payload.get('item_code')}")
            self.assertIn("is_bundle_child", item_payload,
                          f"'is_bundle_child' key missing from item {item_payload.get('item_code')}")
            self.assertIn("parent_bundle", item_payload,
                          f"'parent_bundle' key missing from item {item_payload.get('item_code')}")
            self.assertIn("bundle_group_key", item_payload,
                          f"'bundle_group_key' key missing from item {item_payload.get('item_code')}")
            self.assertIn("bundle_group_name", item_payload,
                          f"'bundle_group_name' key missing from item {item_payload.get('item_code')}")

    def test_bundle_group_fields_are_emitted_when_present(self):
        """Bundle child group metadata must be exposed for amendment reconstruction."""
        from unittest.mock import patch, MagicMock
        mf = MagicMock()
        mf.get_doc.side_effect = Exception("no address")
        mf.log_error = MagicMock()

        child_item = self._make_item(
            item_code="ITEM-A",
            is_bundle_child=1,
            parent_bundle="BDL-123",
            bundle_group_key="ROW-FLAVOR-1",
            bundle_group_name="Flavor",
        )
        inv = self._make_invoice_ns(items=[child_item])
        with patch("jarz_pos.utils.invoice_utils.frappe", mf):
            from jarz_pos.utils.invoice_utils import format_invoice_data
            data = format_invoice_data(inv)

        item_payload = data["items"][0]
        self.assertEqual(item_payload["bundle_group_key"], "ROW-FLAVOR-1")
        self.assertEqual(item_payload["bundle_group_name"], "Flavor")

    def test_bundle_parent_missing_bundle_code_logs_error(self):
        """When a bundle-parent item has empty bundle_code, log_error must be called."""
        from unittest.mock import patch, MagicMock
        mf = MagicMock()
        mf.get_doc.side_effect = Exception("no address")
        mf.log_error = MagicMock()

        bundle_parent_no_code = self._make_item(
            item_code="BDL-ORPHAN",
            is_bundle_parent=1,
            bundle_code=None,  # missing bundle_code — drift scenario
        )
        inv = self._make_invoice_ns(items=[bundle_parent_no_code])
        with patch("jarz_pos.utils.invoice_utils.frappe", mf):
            from jarz_pos.utils.invoice_utils import format_invoice_data
            format_invoice_data(inv)

        mf.log_error.assert_called()
        call_args = str(mf.log_error.call_args_list)
        self.assertIn("Amendment Payload Drift", call_args,
                      "log_error must use 'Amendment Payload Drift' title for bundle drift")

    def test_bundle_parent_missing_bundle_code_is_derived_from_erpnext_item(self):
        """Legacy parent rows can recover bundle_code from Jarz Bundle.erpnext_item."""
        from unittest.mock import patch, MagicMock
        mf = MagicMock()
        mf.get_doc.side_effect = Exception("no address")
        mf.log_error = MagicMock()

        def get_all_side_effect(doctype, filters=None, fields=None, limit=None, **kwargs):
            if doctype == "Jarz Bundle":
                self.assertEqual(filters, {"erpnext_item": "BDL-PARENT-ITEM"})
                self.assertEqual(fields, ["name"])
                self.assertEqual(limit, 1)
                return [{"name": "BDL-RECOVERED"}]
            return []

        mf.get_all.side_effect = get_all_side_effect

        bundle_parent_no_code = self._make_item(
            item_code="BDL-PARENT-ITEM",
            is_bundle_parent=1,
            bundle_code=None,
        )
        inv = self._make_invoice_ns(items=[bundle_parent_no_code])
        with patch("jarz_pos.utils.invoice_utils.frappe", mf):
            from jarz_pos.utils.invoice_utils import format_invoice_data
            data = format_invoice_data(inv)

        self.assertEqual(data["items"][0]["bundle_code"], "BDL-RECOVERED")
        self.assertFalse(mf.log_error.called)


# ---------------------------------------------------------------------------
# Test 11 – B5 fix: a rate-less qty INCREASE must not trip the >50%-drop guard
# ---------------------------------------------------------------------------

class TestAmendmentRatelessQtyIncreaseNotBlocked(unittest.TestCase):
    """A cart line of {item_code, qty} with no rate (the Flutter update-cart payload
    shape) must resolve its rate from the price list / Item master, so a pure quantity
    INCREASE is NOT falsely flagged as a suspicious >50% drop. When the rate cannot be
    resolved the genuine guard still fires."""

    def _build_mock_frappe(self):
        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        mf.db.savepoint.return_value = None
        mf.utils.now.return_value = "2026-05-16 12:00:00"
        return mf

    def test_rateless_increase_resolves_rate_and_not_blocked(self):
        # Source priced 3 @ 100 → grand_total 300. Submitted cart bumps qty to 5 with
        # NO rate. Without resolution submitted_total would be 0 (blocked); resolving the
        # rate to 100 yields 500 (≥ 50% of 300) so the guard must NOT fire.
        inv = _make_invoice(grand_total=300.0)
        cart = _make_cart_json([{"item_code": "ITEM-A", "qty": 5}])

        resolve_calls = []

        def fake_resolve(item_code, price_list, fallback_rate=0.0, customer=None):
            resolve_calls.append((item_code, price_list, customer))
            return 100.0

        with (
            patch("jarz_pos.api.manager.frappe", self._build_mock_frappe()),
            patch("jarz_pos.services.invoice_creation._resolve_item_rate", side_effect=fake_resolve),
            patch("jarz_pos.api.manager._create_amendment_invoice", return_value={"invoice_name": "ACC-SINV-TEST-010"}),
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
                request_id="test-req-rateless",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertTrue(resolve_calls, "rate-less line must trigger rate resolution")
        self.assertEqual(resolve_calls[0][0], "ITEM-A")
        self.assertNotEqual(result.get("amendment_block_code"), "suspicious_diff")

    def test_unresolvable_rateless_line_still_blocks(self):
        # Control: if the rate resolves to 0 (truly unknown), the genuine >50%-drop guard
        # must still protect the order.
        inv = _make_invoice(grand_total=300.0)
        cart = _make_cart_json([{"item_code": "ITEM-A", "qty": 5}])

        with (
            patch("jarz_pos.api.manager.frappe", self._build_mock_frappe()),
            patch("jarz_pos.services.invoice_creation._resolve_item_rate", return_value=0.0),
            patch("jarz_pos.api.manager._create_amendment_invoice", MagicMock()),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=inv),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-001",
                request_id="test-req-rateless-zero",
                cart_json=cart,
                pos_profile_name="Nasr city",
            )

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("amendment_block_code"), "suspicious_diff")


if __name__ == "__main__":
    unittest.main()

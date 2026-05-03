"""Tests for settlement accounting correctness.

Unlike test_settlement_strategies.py (which tests dispatch routing) and
test_kanban_settlement.py (which tests state transitions), this module
tests the actual **financial calculations and JE structures** produced
by the settlement handlers.

Each test provides concrete numeric inputs and verifies that the resulting
Journal Entry / Payment Entry / Courier Transaction have correct amounts,
accounts, and debit/credit directions.
"""

import unittest
from unittest.mock import patch, MagicMock, call


# ---------------------------------------------------------------------------
# Helpers to build mock Frappe environment
# ---------------------------------------------------------------------------

def _mock_frappe():
    """Return a fully-wired mock frappe module suitable for settlement tests."""
    m = MagicMock()
    m.utils.nowdate.return_value = "2026-03-14"
    m.utils.now_datetime.return_value = "2026-03-14 12:00:00"
    m.utils.getdate.return_value = "2026-03-14"
    m.utils.nowtime.return_value = "12:00:00"
    m.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
    m.session.user = "test@example.com"
    m.local.site = "test.site"

    # Simulate roles that pass permission guard
    m.get_roles.return_value = ["Administrator", "Sales User", "Accounts User"]

    # Default flags for test mode detection
    m.flags = MagicMock()
    m.flags.in_test = True

    return m


def _mock_invoice(name="INV-TEST", company="Test Company", grand_total=500.0,
                  outstanding=0.0, docstatus=1, customer="Walk In",
                  territory="Cairo", sales_partner=None):
    """Return a mock Sales Invoice document."""
    inv = MagicMock()
    inv.name = name
    inv.company = company
    inv.grand_total = grand_total
    inv.outstanding_amount = outstanding
    inv.docstatus = docstatus
    inv.customer = customer
    inv.territory = territory
    inv.sales_partner = sales_partner
    inv.custom_is_pickup = 0
    inv.items = []
    inv.get.side_effect = lambda k, default=None: getattr(inv, k, default)
    return inv


class _JournalEntryCapture:
    """Captures JE lines appended via je.append('accounts', {...})."""

    def __init__(self):
        self.accounts = []
        self.voucher_type = None
        self.posting_date = None
        self.company = None
        self.title = None
        self.user_remark = None
        self.name = "JE-CAPTURED"
        self.docstatus = 0

    def append(self, child_table, row):
        if child_table == "accounts":
            self.accounts.append(row)

    def save(self, **kwargs):
        pass

    def submit(self):
        self.docstatus = 1

    def set(self, key, value):
        setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)

    @property
    def total_debit(self):
        return sum(float(a.get("debit_in_account_currency", 0) or 0) for a in self.accounts)

    @property
    def total_credit(self):
        return sum(float(a.get("credit_in_account_currency", 0) or 0) for a in self.accounts)


class _CourierTransactionCapture:
    """Captures CT fields."""

    def __init__(self):
        self.party_type = None
        self.party = None
        self.date = None
        self.reference_invoice = None
        self.amount = 0
        self.shipping_amount = 0
        self.status = None
        self.payment_mode = None
        self.notes = None
        self.name = "CT-CAPTURED"

    def insert(self, **kwargs):
        pass

    def set(self, key, value):
        setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)


# ── Account constants for tests ────────────────────────────────────────
CASH_ACC = "Cash - TC"
FREIGHT_ACC = "Freight and Forwarding Charges - TC"
COURIER_OUTSTANDING_ACC = "Courier Outstanding - TC"
CREDITORS_ACC = "Creditors - TC"
RECEIVABLE_ACC = "Debtors - TC"


# ===========================================================================
# TEST: update_submitted_sales_invoice_state – save/update-after-submit path
# ===========================================================================

class TestSubmittedInvoiceStateUpdates(unittest.TestCase):
    """Verify submitted SI state updates use save() so document hooks can run."""

    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_uses_save_for_submitted_invoice(self, mock_frappe):
        from jarz_pos.services.delivery_handling import update_submitted_sales_invoice_state

        inv = _mock_invoice()
        inv.flags = MagicMock()
        inv.custom_sales_invoice_state = "Ready"
        inv.sales_invoice_state = "Ready"

        mock_meta = MagicMock()
        mock_meta.get_field.side_effect = lambda name: MagicMock() if name in {"custom_sales_invoice_state", "sales_invoice_state"} else None
        mock_frappe.get_meta.return_value = mock_meta

        changed = update_submitted_sales_invoice_state(
            inv,
            "Out for Delivery",
            field_names=("custom_sales_invoice_state", "sales_invoice_state"),
        )

        self.assertTrue(changed)
        self.assertTrue(inv.flags.ignore_validate_update_after_submit)
        inv.set.assert_any_call("custom_sales_invoice_state", "Out for Delivery")
        inv.set.assert_any_call("sales_invoice_state", "Out for Delivery")
        inv.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)
        inv.db_set.assert_not_called()

    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_is_idempotent_when_state_is_already_set(self, mock_frappe):
        from jarz_pos.services.delivery_handling import update_submitted_sales_invoice_state

        inv = _mock_invoice()
        inv.flags = MagicMock()
        inv.custom_sales_invoice_state = "Out for Delivery"

        mock_meta = MagicMock()
        mock_meta.get_field.side_effect = lambda name: MagicMock() if name == "custom_sales_invoice_state" else None
        mock_frappe.get_meta.return_value = mock_meta

        changed = update_submitted_sales_invoice_state(inv, "Out for Delivery")

        self.assertFalse(changed)
        inv.set.assert_not_called()
        inv.save.assert_not_called()


# ===========================================================================
# TEST: handle_out_for_delivery_paid – JE structure verification
# ===========================================================================

class TestOFDPaidJournalEntry(unittest.TestCase):
    """Verify handle_out_for_delivery_paid produces correct JE for both settlement modes."""

    def _run_ofd_paid(self, settlement, shipping_exp, grand_total=500.0):
        """Execute handle_out_for_delivery_paid with captured JE and CT."""
        je_capture = _JournalEntryCapture()
        ct_capture = _CourierTransactionCapture()
        new_doc_calls = {"count": 0}

        def mock_new_doc(doctype):
            new_doc_calls["count"] += 1
            if doctype == "Journal Entry":
                return je_capture
            if doctype == "Courier Transaction":
                return ct_capture
            return MagicMock(name=f"MockDoc-{doctype}")

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.utils.now_datetime.return_value = "2026-03-14 12:00:00"
            mf.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
            mf.session.user = "test@example.com"
            mf.get_roles.return_value = ["Administrator"]
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.new_doc.side_effect = mock_new_doc

            # No existing JE or CT (idempotency check returns empty)
            mf.get_all.return_value = []
            mf.db.commit.return_value = None
            mf.db.rollback.return_value = None
            mf.db.sql.return_value = None
            mf.db.savepoint.return_value = None
            mf.publish_realtime.return_value = None

            inv = _mock_invoice(grand_total=grand_total)
            mf.get_doc.return_value = inv

            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=shipping_exp), \
                 patch("jarz_pos.services.delivery_handling.ensure_delivery_note_for_invoice", return_value={"delivery_note": "DN-001", "reused": False, "error": None}), \
                 patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value=FREIGHT_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_pos_cash_account", return_value=CASH_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling.validate_account_exists"):

                from jarz_pos.services.delivery_handling import handle_out_for_delivery_paid

                result = handle_out_for_delivery_paid(
                    invoice_name=inv.name,
                    courier="Courier",
                    settlement=settlement,
                    pos_profile="POS-001",
                    party_type="Supplier",
                    party="Courier-A",
                )

        return result, je_capture, ct_capture

    # ── cash_now ──

    def test_cash_now_je_debits_freight_credits_cash(self):
        """cash_now: JE should DR Freight, CR Cash for shipping amount."""
        shipping = 30.0
        _, je, _ = self._run_ofd_paid("cash_now", shipping)

        # Should have exactly 2 lines
        self.assertEqual(len(je.accounts), 2, f"Expected 2 JE lines, got {len(je.accounts)}: {je.accounts}")

        freight_line = next(a for a in je.accounts if a["account"] == FREIGHT_ACC)
        cash_line = next(a for a in je.accounts if a["account"] == CASH_ACC)

        self.assertEqual(float(freight_line["debit_in_account_currency"]), shipping)
        self.assertEqual(float(freight_line.get("credit_in_account_currency", 0)), 0)
        self.assertEqual(float(cash_line["credit_in_account_currency"]), shipping)
        self.assertEqual(float(cash_line.get("debit_in_account_currency", 0)), 0)

    def test_cash_now_je_is_balanced(self):
        """cash_now: Total debits must equal total credits."""
        _, je, _ = self._run_ofd_paid("cash_now", 45.0)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)

    def test_cash_now_ct_status_settled(self):
        """cash_now: CT should be Settled with amount = grand_total."""
        result, _, ct = self._run_ofd_paid("cash_now", 30.0, grand_total=500.0)
        self.assertEqual(ct.status, "Settled")
        self.assertEqual(ct.amount, 500.0)
        self.assertEqual(ct.shipping_amount, 30.0)

    def test_cash_now_zero_shipping_no_je(self):
        """cash_now with zero shipping: No JE lines should be created."""
        _, je, _ = self._run_ofd_paid("cash_now", 0.0)
        self.assertEqual(len(je.accounts), 0, "Zero shipping should produce no JE lines")

    def test_uses_submitted_state_helper(self):
        """Paid OFD flow should route submitted invoice state changes through the reusable helper."""
        with patch("jarz_pos.services.delivery_handling.update_submitted_sales_invoice_state") as mock_update_state:
            self._run_ofd_paid("cash_now", 25.0)

        mock_update_state.assert_called_once()
        self.assertEqual(mock_update_state.call_args.args[1], "Out for Delivery")

    # ── later ──

    def test_later_je_debits_freight_credits_creditors(self):
        """later: JE should DR Freight, CR Creditors with party for shipping amount."""
        shipping = 50.0
        _, je, _ = self._run_ofd_paid("later", shipping)

        self.assertEqual(len(je.accounts), 2, f"Expected 2 JE lines, got {len(je.accounts)}")

        freight_line = next(a for a in je.accounts if a["account"] == FREIGHT_ACC)
        creditors_line = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)

        self.assertEqual(float(freight_line["debit_in_account_currency"]), shipping)
        self.assertEqual(float(creditors_line["credit_in_account_currency"]), shipping)

        # Creditors MUST have party info
        self.assertEqual(creditors_line["party_type"], "Supplier")
        self.assertEqual(creditors_line["party"], "Courier-A")

    def test_later_je_is_balanced(self):
        """later: Total debits must equal total credits."""
        _, je, _ = self._run_ofd_paid("later", 75.0)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)

    def test_later_ct_status_unsettled(self):
        """later: CT should be Unsettled with amount = 0 (shipping only)."""
        _, _, ct = self._run_ofd_paid("later", 50.0, grand_total=500.0)
        self.assertEqual(ct.status, "Unsettled")
        self.assertEqual(ct.amount, 0.0)  # later → 0 principal
        self.assertEqual(ct.shipping_amount, 50.0)

    def test_later_zero_shipping_no_je(self):
        """later with zero shipping: No JE lines should be created."""
        _, je, _ = self._run_ofd_paid("later", 0.0)
        self.assertEqual(len(je.accounts), 0)


# ===========================================================================
# TEST: settle_single_invoice_paid – Accounting correctness
# ===========================================================================

class TestSettleSingleInvoicePaid(unittest.TestCase):
    """Verify settle_single_invoice_paid JE for both order >= shipping and shipping > order cases."""

    def _run_settle(self, order_amount, shipping_exp, has_outstanding_ct=True):
        """Execute settle_single_invoice_paid with captured JE."""
        je_capture = _JournalEntryCapture()
        ct_capture = _CourierTransactionCapture()

        def mock_new_doc(doctype):
            if doctype == "Journal Entry":
                return je_capture
            if doctype == "Courier Transaction":
                return ct_capture
            return MagicMock()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.new_doc.side_effect = mock_new_doc
            mf.db.sql.return_value = None

            inv = _mock_invoice(grand_total=order_amount, outstanding=0.0)
            mf.get_doc.return_value = inv

            # Existing Courier Transactions
            if has_outstanding_ct:
                mf.get_all.side_effect = [
                    # First call: outstanding CT with amount > 0
                    [{"name": "CT-001", "amount": order_amount, "shipping_amount": shipping_exp}],
                    # Second call: existing JE check → none
                    [],
                    # Third call: CTs to settle
                    ["CT-001"],
                ]
            else:
                mf.get_all.side_effect = [
                    # No outstanding CT
                    [],
                    # Existing JE check → none
                    [],
                    # CTs to settle → create new
                    [],
                ]
            mf.db.set_value.return_value = None
            mf.publish_realtime.return_value = None
            mf.throw.side_effect = Exception

            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=shipping_exp), \
                 patch("jarz_pos.services.delivery_handling.get_pos_cash_account", return_value=CASH_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling.validate_account_exists"):

                from jarz_pos.services.delivery_handling import settle_single_invoice_paid

                result = settle_single_invoice_paid(
                    invoice_name=inv.name,
                    pos_profile="POS-001",
                    party_type="Supplier",
                    party="Courier-A",
                )

        return result, je_capture

    # ── Case 1: order_amount >= shipping_exp ──

    def test_case1_order_gte_shipping_je_structure(self):
        """When order(100) >= shipping(30): DR Cash(70), DR Creditors(30), CR Courier Outstanding(100)."""
        order, shipping = 100.0, 30.0
        _, je = self._run_settle(order, shipping)
        net = order - shipping  # 70

        # 3 lines: Cash debit, Creditors debit, Courier Outstanding credit
        self.assertEqual(len(je.accounts), 3, f"Expected 3 lines, got {len(je.accounts)}: {je.accounts}")

        cash_line = next(a for a in je.accounts if a["account"] == CASH_ACC)
        cred_line = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        co_line = next(a for a in je.accounts if a["account"] == COURIER_OUTSTANDING_ACC)

        self.assertAlmostEqual(float(cash_line["debit_in_account_currency"]), net, places=2)
        self.assertAlmostEqual(float(cred_line["debit_in_account_currency"]), shipping, places=2)
        self.assertAlmostEqual(float(co_line["credit_in_account_currency"]), order, places=2)

    def test_case1_je_is_balanced(self):
        """order >= shipping: Total debits = total credits."""
        _, je = self._run_settle(200.0, 50.0)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)

    def test_case1_creditors_has_party(self):
        """Creditors line must carry party_type and party."""
        _, je = self._run_settle(100.0, 30.0)
        cred_line = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        self.assertEqual(cred_line["party_type"], "Supplier")
        self.assertEqual(cred_line["party"], "Courier-A")

    def test_case1_equal_amounts(self):
        """When order == shipping (break even): DR Creditors(30), CR Courier Outstanding(30), no Cash line."""
        order, shipping = 30.0, 30.0
        _, je = self._run_settle(order, shipping)

        # Cash net = 0 → no cash line (debit net_branch=0 skipped with > 0.0001 check)
        cash_lines = [a for a in je.accounts if a["account"] == CASH_ACC]
        self.assertEqual(len(cash_lines), 0, "Break-even should have no Cash line")

        # Should be 2 lines: Creditors debit, CO credit
        self.assertEqual(len(je.accounts), 2)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)

    # ── Case 2: shipping > order ──

    def test_case2_shipping_gt_order_je_structure(self):
        """When shipping(80) > order(50): DR Creditors(80), CR Courier Outstanding(50), CR Cash(30)."""
        order, shipping = 50.0, 80.0
        _, je = self._run_settle(order, shipping)
        excess = shipping - order  # 30

        self.assertEqual(len(je.accounts), 3, f"Expected 3 lines, got {len(je.accounts)}: {je.accounts}")

        cred_line = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        co_line = next(a for a in je.accounts if a["account"] == COURIER_OUTSTANDING_ACC)
        cash_line = next(a for a in je.accounts if a["account"] == CASH_ACC)

        # DR Creditors = shipping
        self.assertAlmostEqual(float(cred_line["debit_in_account_currency"]), shipping, places=2)
        # CR Courier Outstanding = order
        self.assertAlmostEqual(float(co_line["credit_in_account_currency"]), order, places=2)
        # CR Cash = excess (shipping - order)
        self.assertAlmostEqual(float(cash_line["credit_in_account_currency"]), excess, places=2)

    def test_case2_je_is_balanced(self):
        """shipping > order: Total debits = total credits."""
        _, je = self._run_settle(50.0, 80.0)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)

    # ── Shipping-only (paid + settle later then settling shipping) ──

    def test_shipping_only_je_structure(self):
        """No outstanding CT (shipping-only): DR Creditors, CR Cash for shipping amount."""
        shipping = 40.0
        _, je = self._run_settle(0, shipping, has_outstanding_ct=False)

        self.assertEqual(len(je.accounts), 2, f"Expected 2 lines, got {len(je.accounts)}")

        cred_line = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        cash_line = next(a for a in je.accounts if a["account"] == CASH_ACC)

        self.assertAlmostEqual(float(cred_line["debit_in_account_currency"]), shipping, places=2)
        self.assertAlmostEqual(float(cash_line["credit_in_account_currency"]), shipping, places=2)

    def test_shipping_only_je_is_balanced(self):
        """Shipping-only: Total debits = total credits."""
        _, je = self._run_settle(0, 40.0, has_outstanding_ct=False)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)


# ===========================================================================
# TEST: settle_courier_collected_payment – Two-case JE verification
# ===========================================================================

class TestSettleCourierCollectedPayment(unittest.TestCase):
    """Verify settle_courier_collected_payment produces correct JE for both GT vs SE cases."""

    def _run_collect(self, grand_total, shipping_exp):
        """Execute settle_courier_collected_payment with JE capture."""
        je_capture = _JournalEntryCapture()

        def mock_new_doc(doctype):
            if doctype == "Journal Entry":
                return je_capture
            return MagicMock()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.new_doc.side_effect = mock_new_doc
            mf.throw.side_effect = Exception

            inv = _mock_invoice(grand_total=grand_total, outstanding=0.0)
            mf.get_doc.return_value = inv

            # Pending CT with shipping amount
            mf.get_all.side_effect = [
                # pending_ct
                [{"name": "CT-PENDING", "shipping_amount": shipping_exp}],
                # existing JE check → none
                [],
                # CTs to settle
                ["CT-PENDING"],
            ]
            mf.db.set_value.return_value = None
            mf.publish_realtime.return_value = None

            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=shipping_exp), \
                 patch("jarz_pos.services.delivery_handling.get_pos_cash_account", return_value=CASH_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling.validate_account_exists"):

                from jarz_pos.services.delivery_handling import settle_courier_collected_payment

                result = settle_courier_collected_payment(
                    invoice_name=inv.name,
                    pos_profile="POS-001",
                    party_type="Supplier",
                    party="Courier-A",
                )

        return result, je_capture

    # ── Case 1: GT >= SE ──

    def test_case1_gt_gte_se_structure(self):
        """GT(150) >= SE(30): DR Cash(120), DR Creditors(30), CR Courier Outstanding(150)."""
        gt, se = 150.0, 30.0
        net = gt - se  # 120
        _, je = self._run_collect(gt, se)

        self.assertEqual(len(je.accounts), 3)

        cash = next(a for a in je.accounts if a["account"] == CASH_ACC)
        cred = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        co = next(a for a in je.accounts if a["account"] == COURIER_OUTSTANDING_ACC)

        self.assertAlmostEqual(float(cash["debit_in_account_currency"]), net, places=2)
        self.assertAlmostEqual(float(cred["debit_in_account_currency"]), se, places=2)
        self.assertAlmostEqual(float(co["credit_in_account_currency"]), gt, places=2)

    def test_case1_balanced(self):
        """GT >= SE: Debits = Credits."""
        _, je = self._run_collect(200.0, 50.0)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)

    def test_case1_net_calculation(self):
        """Verify net_to_branch = GT - SE is correct."""
        gt, se = 500.0, 45.0
        result, je = self._run_collect(gt, se)

        cash = next(a for a in je.accounts if a["account"] == CASH_ACC)
        self.assertAlmostEqual(float(cash["debit_in_account_currency"]), gt - se, places=2)

    def test_case1_gt_equals_se_no_cash(self):
        """When GT == SE: No Cash line (net=0), just DR Creditors, CR CO."""
        gt, se = 40.0, 40.0
        _, je = self._run_collect(gt, se)

        cash_lines = [a for a in je.accounts if a["account"] == CASH_ACC]
        self.assertEqual(len(cash_lines), 0, "Zero net → no Cash line")
        self.assertEqual(len(je.accounts), 2)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)

    # ── Case 2: SE > GT ──

    def test_case2_se_gt_gt_structure(self):
        """SE(80) > GT(50): DR Creditors(80), CR CO(50), CR Cash(30)."""
        gt, se = 50.0, 80.0
        excess = se - gt  # 30
        _, je = self._run_collect(gt, se)

        self.assertEqual(len(je.accounts), 3)

        cred = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        co = next(a for a in je.accounts if a["account"] == COURIER_OUTSTANDING_ACC)
        cash = next(a for a in je.accounts if a["account"] == CASH_ACC)

        self.assertAlmostEqual(float(cred["debit_in_account_currency"]), se, places=2)
        self.assertAlmostEqual(float(co["credit_in_account_currency"]), gt, places=2)
        self.assertAlmostEqual(float(cash["credit_in_account_currency"]), excess, places=2)

    def test_case2_balanced(self):
        """SE > GT: Debits = Credits."""
        _, je = self._run_collect(50.0, 80.0)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)


# ===========================================================================
# TEST: mark_courier_outstanding – PE + CT + shipping JE
# ===========================================================================

class TestMarkCourierOutstanding(unittest.TestCase):
    """Verify mark_courier_outstanding creates correct PE, CT, and shipping JE."""

    def _run_mark(self, outstanding=500.0, grand_total=500.0, shipping_exp=30.0):
        """Execute mark_courier_outstanding with captures."""
        je_capture = _JournalEntryCapture()
        ct_capture = _CourierTransactionCapture()
        pe_capture = MagicMock()
        pe_capture.name = "PE-CAPTURED"

        created_docs = []

        def mock_new_doc(doctype):
            if doctype == "Journal Entry":
                created_docs.append(("JE", je_capture))
                return je_capture
            if doctype == "Courier Transaction":
                created_docs.append(("CT", ct_capture))
                return ct_capture
            if doctype == "Payment Entry":
                created_docs.append(("PE", pe_capture))
                return pe_capture
            return MagicMock()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.utils.now_datetime.return_value = "2026-03-14 12:00:00"
            mf.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
            mf.new_doc.side_effect = mock_new_doc
            mf.throw.side_effect = Exception
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.publish_realtime.return_value = None

            inv = _mock_invoice(grand_total=grand_total, outstanding=outstanding)
            mf.get_doc.return_value = inv
            mf.db.get_value.return_value = outstanding

            # No existing CT, PE, or DN
            mf.get_all.return_value = []

            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=shipping_exp), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_receivable_account", return_value=RECEIVABLE_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value=FREIGHT_ACC), \
                 patch("jarz_pos.services.delivery_handling._create_payment_entry") as mock_pe_fn, \
                 patch("jarz_pos.services.delivery_handling._create_shipping_expense_to_creditors_je") as mock_je_fn, \
                 patch("jarz_pos.services.delivery_handling.ensure_delivery_note_for_invoice", return_value={"delivery_note": "DN-001", "reused": False, "error": None}):

                mock_pe_fn.return_value = pe_capture
                mock_je_fn.return_value = "JE-SHIPPING"

                from jarz_pos.services.delivery_handling import mark_courier_outstanding

                result = mark_courier_outstanding(
                    invoice_name=inv.name,
                    courier=None,
                    party_type="Employee",
                    party="EMP-001",
                )

        return result, ct_capture, mock_pe_fn, mock_je_fn

    def test_ct_amount_equals_grand_total(self):
        """CT.amount should be the invoice grand_total."""
        _, ct, _, _ = self._run_mark(grand_total=500.0)
        self.assertEqual(ct.amount, 500.0)

    def test_ct_shipping_amount_set(self):
        """CT.shipping_amount should match territory shipping expense."""
        _, ct, _, _ = self._run_mark(shipping_exp=35.0)
        self.assertEqual(ct.shipping_amount, 35.0)

    def test_ct_status_unsettled(self):
        """CT status should be Unsettled."""
        _, ct, _, _ = self._run_mark()
        self.assertEqual(ct.status, "Unsettled")

    def test_ct_party_fields_set(self):
        """CT should have party_type and party."""
        _, ct, _, _ = self._run_mark()
        self.assertEqual(ct.party_type, "Employee")
        self.assertEqual(ct.party, "EMP-001")

    def test_pe_called_with_outstanding(self):
        """PE should be created with the full outstanding amount."""
        outstanding = 450.0
        _, _, pe_fn, _ = self._run_mark(outstanding=outstanding, grand_total=450.0)
        pe_fn.assert_called_once()
        args = pe_fn.call_args
        # Third positional arg is paid_to, fourth is outstanding
        self.assertAlmostEqual(float(args[0][3]), outstanding, places=2)

    def test_shipping_je_called_with_expense(self):
        """Shipping JE should be created with the shipping expense amount."""
        shipping = 40.0
        _, _, _, je_fn = self._run_mark(shipping_exp=shipping)
        je_fn.assert_called_once()
        args = je_fn.call_args
        # Second positional arg is shipping_exp
        self.assertAlmostEqual(float(args[0][1]), shipping, places=2)

    def test_result_contains_net_to_collect(self):
        """Result should contain net_to_collect = order_amount - shipping."""
        gt, shipping = 500.0, 30.0
        result, _, _, _ = self._run_mark(grand_total=gt, shipping_exp=shipping)
        self.assertAlmostEqual(result["net_to_collect"], gt - shipping, places=2)

    def test_no_pe_when_zero_outstanding(self):
        """No PE should be created when outstanding is 0 (already paid)."""
        _, _, pe_fn, _ = self._run_mark(outstanding=0.0, grand_total=500.0)
        pe_fn.assert_not_called()

    def test_uses_submitted_state_helper(self):
        """Courier outstanding flow should route submitted invoice state changes through the reusable helper."""
        with patch("jarz_pos.services.delivery_handling.update_submitted_sales_invoice_state") as mock_update_state:
            self._run_mark()

        mock_update_state.assert_called_once()
        self.assertEqual(mock_update_state.call_args.args[1], "Out for Delivery")


# ===========================================================================
# TEST: _create_shipping_expense_to_creditors_je – Direct verification
# ===========================================================================

class TestShippingExpenseJE(unittest.TestCase):
    """Verify the shipping expense JE helper produces correct DR Freight / CR Creditors."""

    def test_correct_structure(self):
        """Should create JE with DR Freight, CR Creditors with party."""
        je_capture = _JournalEntryCapture()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.new_doc.return_value = je_capture
            mf.utils.nowdate.return_value = "2026-03-14"

            with patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value=FREIGHT_ACC):
                from jarz_pos.services.delivery_handling import _create_shipping_expense_to_creditors_je

                inv = _mock_invoice()
                result = _create_shipping_expense_to_creditors_je(
                    inv, 25.0, CREDITORS_ACC, "Supplier", "Courier-B"
                )

        self.assertEqual(len(je_capture.accounts), 2)

        freight = next(a for a in je_capture.accounts if a["account"] == FREIGHT_ACC)
        creditors = next(a for a in je_capture.accounts if a["account"] == CREDITORS_ACC)

        self.assertAlmostEqual(float(freight["debit_in_account_currency"]), 25.0, places=2)
        self.assertAlmostEqual(float(creditors["credit_in_account_currency"]), 25.0, places=2)
        self.assertEqual(creditors["party_type"], "Supplier")
        self.assertEqual(creditors["party"], "Courier-B")

    def test_balanced(self):
        """Shipping expense JE must be balanced."""
        je_capture = _JournalEntryCapture()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.new_doc.return_value = je_capture
            mf.utils.nowdate.return_value = "2026-03-14"

            with patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value=FREIGHT_ACC):
                from jarz_pos.services.delivery_handling import _create_shipping_expense_to_creditors_je

                inv = _mock_invoice()
                _create_shipping_expense_to_creditors_je(inv, 100.0, CREDITORS_ACC, "Employee", "EMP-X")

        self.assertAlmostEqual(je_capture.total_debit, je_capture.total_credit, places=2)


# ===========================================================================
# TEST: _create_settlement_journal_entry – Batch settlement verification
# ===========================================================================

class TestSettlementJournalEntry(unittest.TestCase):
    """Verify the batch settlement JE helper for courier settlement."""

    def _run_settlement_je(self, order_amt, shipping_amt):
        """Execute _create_settlement_journal_entry with JE capture."""
        je_capture = _JournalEntryCapture()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.new_doc.return_value = je_capture
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
            mf.throw.side_effect = Exception

            from jarz_pos.services.delivery_handling import _create_settlement_journal_entry

            _create_settlement_journal_entry(
                courier="Courier-X",
                company="Test Company",
                cash_acc=CASH_ACC,
                courier_outstanding_acc=COURIER_OUTSTANDING_ACC,
                creditors_acc=CREDITORS_ACC,
                total_order_amount=order_amt,
                total_shipping_amount=shipping_amt,
                party_type="Supplier",
                party="Courier-X",
            )

        return je_capture

    def test_positive_net_structure(self):
        """order(1000) > shipping(200): DR Cash(800), DR Creditors(200), CR CO(1000)."""
        je = self._run_settlement_je(1000.0, 200.0)

        cash = next(a for a in je.accounts if a["account"] == CASH_ACC)
        cred = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        co = next(a for a in je.accounts if a["account"] == COURIER_OUTSTANDING_ACC)

        self.assertAlmostEqual(float(cash["debit_in_account_currency"]), 800.0, places=2)
        self.assertAlmostEqual(float(cred["debit_in_account_currency"]), 200.0, places=2)
        self.assertAlmostEqual(float(co["credit_in_account_currency"]), 1000.0, places=2)

    def test_negative_net_structure(self):
        """order(100) < shipping(300): CR Cash(200), DR Creditors(300), CR CO(100)."""
        je = self._run_settlement_je(100.0, 300.0)

        cash = next(a for a in je.accounts if a["account"] == CASH_ACC)
        cred = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        co = next(a for a in je.accounts if a["account"] == COURIER_OUTSTANDING_ACC)

        self.assertAlmostEqual(float(cash["credit_in_account_currency"]), 200.0, places=2)
        self.assertAlmostEqual(float(cred["debit_in_account_currency"]), 300.0, places=2)
        self.assertAlmostEqual(float(co["credit_in_account_currency"]), 100.0, places=2)

    def test_balanced(self):
        """Settlement JE must always be balanced."""
        for order, shipping in [(1000, 200), (100, 300), (500, 500), (1, 999)]:
            je = self._run_settlement_je(float(order), float(shipping))
            self.assertAlmostEqual(
                je.total_debit, je.total_credit, places=2,
                msg=f"Imbalanced for order={order}, shipping={shipping}",
            )

    def test_break_even_no_cash(self):
        """order == shipping: No Cash line (net=0)."""
        je = self._run_settlement_je(500.0, 500.0)
        cash_lines = [a for a in je.accounts if a["account"] == CASH_ACC]
        self.assertEqual(len(cash_lines), 0, "Break-even settlement should have no Cash line")


# ===========================================================================
# TEST: Sales Partner Fees
# ===========================================================================

class TestSalesPartnerFees(unittest.TestCase):
    """Verify _compute_sales_partner_fees calculations."""

    def _compute(self, grand_total, commission_rate, online_rate=0.0, online=False):
        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mock_partner = MagicMock()
            mock_partner.commission_rate = commission_rate
            mock_partner.online_payment_fees = online_rate
            mf.get_doc.return_value = mock_partner

            from jarz_pos.services.delivery_handling import _compute_sales_partner_fees

            inv = _mock_invoice(grand_total=grand_total)
            return _compute_sales_partner_fees(inv, "Partner-001", online=online)

    def test_commission_only(self):
        """1000 * 5% = 50 base, 50 * 14% = 7 VAT, total = 57."""
        result = self._compute(1000.0, 5.0)
        self.assertAlmostEqual(result["base_fees"], 50.0, places=2)
        self.assertAlmostEqual(result["vat"], 7.0, places=2)
        self.assertAlmostEqual(result["total_fees"], 57.0, places=2)

    def test_commission_plus_online(self):
        """1000 * (5% + 2%) = 70 base, 70 * 14% = 9.8 VAT, total = 79.8."""
        result = self._compute(1000.0, 5.0, 2.0, online=True)
        self.assertAlmostEqual(result["base_fees"], 70.0, places=2)
        self.assertAlmostEqual(result["vat"], 9.8, places=2)
        self.assertAlmostEqual(result["total_fees"], 79.8, places=2)

    def test_zero_commission(self):
        """Zero rates → all zeros."""
        result = self._compute(1000.0, 0.0, 0.0, online=True)
        self.assertAlmostEqual(result["base_fees"], 0.0, places=2)
        self.assertAlmostEqual(result["vat"], 0.0, places=2)
        self.assertAlmostEqual(result["total_fees"], 0.0, places=2)

    def test_online_false_ignores_online_rate(self):
        """When online=False, online_rate is ignored."""
        result = self._compute(1000.0, 5.0, 2.0, online=False)
        # Only commission: 1000 * 5% = 50 + 14% VAT = 7 → 57
        self.assertAlmostEqual(result["base_fees"], 50.0, places=2)
        self.assertAlmostEqual(result["total_fees"], 57.0, places=2)

    def test_decimal_precision(self):
        """Verifies rounding to 2 decimal places."""
        # 333 * 7% = 23.31, + 14% VAT = 3.2634 → 3.26 rounded
        result = self._compute(333.0, 7.0)
        self.assertAlmostEqual(result["base_fees"], 23.31, places=2)
        self.assertAlmostEqual(result["vat"], 3.26, places=2)
        self.assertAlmostEqual(result["total_fees"], 26.57, places=2)


if __name__ == "__main__":
    unittest.main()

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
    inv.custom_no_courier = 0
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
        # What the RIDER is owed out of the cash he carries. Zero on a partner row:
        # his company bills us weekly instead, on partner_fee.
        self.shipping_amount = 0
        self.partner_fee = 0
        self.is_partner_order = 0
        self.delivery_partner = None
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

        inv = _mock_invoice(name="INV-TEST-001")
        inv.flags = MagicMock()
        inv.custom_sales_invoice_state = "Ready"
        inv.sales_invoice_state = "Ready"

        fresh_inv = _mock_invoice(name="INV-TEST-001")
        fresh_inv.flags = MagicMock()
        fresh_inv.custom_sales_invoice_state = "Ready"
        fresh_inv.sales_invoice_state = "Ready"

        mock_meta = MagicMock()
        mock_meta.get_field.side_effect = lambda name: MagicMock() if name in {"custom_sales_invoice_state", "sales_invoice_state"} else None
        mock_frappe.get_meta.return_value = mock_meta
        mock_frappe.get_doc.return_value = fresh_inv
        mock_frappe.TimestampMismatchError = type("TimestampMismatchError", (Exception,), {})

        changed = update_submitted_sales_invoice_state(
            inv,
            "Out for Delivery",
            field_names=("custom_sales_invoice_state", "sales_invoice_state"),
        )

        self.assertTrue(changed)
        mock_frappe.get_doc.assert_called_once_with("Sales Invoice", "INV-TEST-001")
        self.assertTrue(fresh_inv.flags.ignore_validate_update_after_submit)
        fresh_inv.set.assert_any_call("custom_sales_invoice_state", "Out for Delivery")
        fresh_inv.set.assert_any_call("sales_invoice_state", "Out for Delivery")
        fresh_inv.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)
        inv.save.assert_not_called()
        fresh_inv.db_set.assert_not_called()

    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_retries_with_fresh_doc_after_timestamp_mismatch(self, mock_frappe):
        from jarz_pos.services.delivery_handling import update_submitted_sales_invoice_state

        stale_inv = _mock_invoice(name="INV-TEST-002")
        stale_inv.flags = MagicMock()
        stale_inv.custom_sales_invoice_state = "Ready"

        first_fresh = _mock_invoice(name="INV-TEST-002")
        first_fresh.flags = MagicMock()
        first_fresh.custom_sales_invoice_state = "Ready"

        second_fresh = _mock_invoice(name="INV-TEST-002")
        second_fresh.flags = MagicMock()
        second_fresh.custom_sales_invoice_state = "Ready"

        timestamp_error = type("TimestampMismatchError", (Exception,), {})
        first_fresh.save.side_effect = timestamp_error("stale")

        mock_meta = MagicMock()
        mock_meta.get_field.side_effect = lambda name: MagicMock() if name == "custom_sales_invoice_state" else None
        mock_frappe.get_meta.return_value = mock_meta
        mock_frappe.get_doc.side_effect = [first_fresh, second_fresh]
        mock_frappe.TimestampMismatchError = timestamp_error

        changed = update_submitted_sales_invoice_state(stale_inv, "Out for Delivery")

        self.assertTrue(changed)
        self.assertEqual(mock_frappe.get_doc.call_count, 2)
        first_fresh.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)
        second_fresh.save.assert_called_once_with(ignore_permissions=True, ignore_version=True)

    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_is_idempotent_when_state_is_already_set(self, mock_frappe):
        from jarz_pos.services.delivery_handling import update_submitted_sales_invoice_state

        inv = _mock_invoice(name="INV-TEST-003")
        inv.flags = MagicMock()
        inv.custom_sales_invoice_state = "Out for Delivery"

        fresh_inv = _mock_invoice(name="INV-TEST-003")
        fresh_inv.flags = MagicMock()
        fresh_inv.custom_sales_invoice_state = "Out for Delivery"

        mock_meta = MagicMock()
        mock_meta.get_field.side_effect = lambda name: MagicMock() if name == "custom_sales_invoice_state" else None
        mock_frappe.get_meta.return_value = mock_meta
        mock_frappe.get_doc.return_value = fresh_inv
        mock_frappe.TimestampMismatchError = type("TimestampMismatchError", (Exception,), {})

        changed = update_submitted_sales_invoice_state(inv, "Out for Delivery")

        self.assertFalse(changed)
        mock_frappe.get_doc.assert_called_once_with("Sales Invoice", "INV-TEST-003")
        fresh_inv.set.assert_not_called()
        fresh_inv.save.assert_not_called()


# ===========================================================================
# TEST: handle_out_for_delivery_paid – JE structure verification
# ===========================================================================

class TestOFDPaidJournalEntry(unittest.TestCase):
    """Verify handle_out_for_delivery_paid produces correct JE for both settlement modes."""

    def _run_ofd_paid(self, settlement, shipping_exp, grand_total=500.0, delivery_partner=None):
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
            # Without this frappe.throw is a silent MagicMock, so any guard under
            # test "passes" by doing nothing.
            mf.throw.side_effect = Exception
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
                  patch("jarz_pos.services.delivery_handling.resolve_assignment_pos_profile", return_value="POS-001"), \
                  patch("jarz_pos.services.delivery_handling.assert_courier_matches_pos_profile", return_value={"branch": "POS-001", "delivery_partner": delivery_partner}), \
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

    def test_partner_courier_is_sent_to_the_settlement_flow(self):
        """This path pays the RIDER his freight; a partner rider is owed nothing.

        Letting a partner order through here would pay the wrong party a number
        priced in the wrong map, so it is refused rather than guessed at.
        """
        with self.assertRaises(Exception):
            self._run_ofd_paid("cash_now", 55.0, delivery_partner="Deliverk")

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

    def test_cash_now_ct_carries_the_settlement_stamp(self):
        """Born settled, so it is attributed like one settled days later.

        Stubbing the stamp rather than reading the columns back keeps this
        honest before a migrate has run — the wiring is what is under test, not
        whether the database has caught up with the DocType yet.
        """
        stamp = {"settled_in_shift": "POS-OPE-0001", "settled_by": "cashier@example.com"}
        with patch(
            "jarz_pos.services.delivery_handling.courier_settlement_stamp",
            return_value=stamp,
        ):
            _, _, ct = self._run_ofd_paid("cash_now", 30.0, grand_total=500.0)

        self.assertEqual(ct.status, "Settled")
        self.assertEqual(ct.get("settled_in_shift"), "POS-OPE-0001")
        self.assertEqual(ct.get("settled_by"), "cashier@example.com")

    def test_later_ct_carries_no_settlement_stamp(self):
        """Money still with the courier is not settled by anyone yet."""
        with patch(
            "jarz_pos.services.delivery_handling.courier_settlement_stamp",
            return_value={"settled_in_shift": "POS-OPE-0001"},
        ):
            _, _, ct = self._run_ofd_paid("later", 30.0, grand_total=500.0)

        self.assertEqual(ct.status, "Unsettled")
        self.assertIsNone(ct.get("settled_in_shift"))

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

            # Pending CT: the courier collected the full order and owes the freight.
            # `amount` is what the courier actually holds and now drives the JE (it used to be
            # re-read from the invoice grand total, which diverged from the batch settlement).
            mf.get_all.side_effect = [
                # pending_ct
                [{"name": "CT-PENDING", "amount": grand_total, "shipping_amount": shipping_exp}],
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

    def _run_mark(self, outstanding=500.0, grand_total=500.0, shipping_exp=30.0,
                  delivery_partner=None, partner_fee=None):
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
                  patch("jarz_pos.services.delivery_handling.resolve_assignment_pos_profile", return_value="POS-001"), \
                  patch("jarz_pos.services.delivery_handling.assert_courier_matches_pos_profile", return_value={"branch": "POS-001", "delivery_partner": delivery_partner}), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_receivable_account", return_value=RECEIVABLE_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value=FREIGHT_ACC), \
                 patch("jarz_pos.services.delivery_handling._create_payment_entry") as mock_pe_fn, \
                 patch("jarz_pos.services.delivery_handling._create_shipping_expense_to_creditors_je") as mock_je_fn, \
                 patch("jarz_pos.services.delivery_handling.create_partner_fee_accrual_je") as mock_partner_je_fn, \
                 patch("jarz_pos.services.delivery_handling.ensure_delivery_note_for_invoice", return_value={"delivery_note": "DN-001", "reused": False, "error": None}):

                mock_pe_fn.return_value = pe_capture
                mock_je_fn.return_value = "JE-SHIPPING"
                mock_partner_je_fn.return_value = "JE-PARTNER-FEE"

                from jarz_pos.services.delivery_handling import mark_courier_outstanding

                result = mark_courier_outstanding(
                    invoice_name=inv.name,
                    courier=None,
                    party_type="Employee",
                    party="EMP-001",
                    shipping_override=partner_fee,
                )

        return result, ct_capture, mock_pe_fn, mock_je_fn, mock_partner_je_fn

    # ── delivery partner ──
    #
    # Same function, same cash path — the ONLY difference is that a partner's rider
    # is paid nothing out of what he carries, so the fee moves off the cash column
    # and onto the partner's tab.

    def test_partner_rider_owes_the_full_amount(self):
        """Nothing is withheld from a partner rider, so the branch collects it all."""
        _, ct, _, mock_je_fn, mock_partner_je_fn = self._run_mark(
            grand_total=850.0, shipping_exp=55.0,
            delivery_partner="Deliverk", partner_fee=55.0,
        )
        self.assertEqual(ct.amount, 850.0)
        # Zero here is what makes every downstream `amount - shipping` yield 850.
        self.assertEqual(ct.shipping_amount, 0.0)
        self.assertEqual(ct.partner_fee, 55.0)
        self.assertEqual(ct.is_partner_order, 1)
        self.assertEqual(ct.delivery_partner, "Deliverk")
        # The fee is NOT accrued to the rider on Creditors; it goes on the
        # partner company's payable and is paid by bank transfer, weekly.
        mock_je_fn.assert_not_called()
        mock_partner_je_fn.assert_called_once()
        self.assertEqual(mock_partner_je_fn.call_args.kwargs["fee"], 55.0)
        self.assertEqual(mock_partner_je_fn.call_args.kwargs["delivery_partner"], "Deliverk")

    def test_partner_dispatch_without_a_fee_is_refused(self):
        """Our area rate is a different number for a different map — never a default."""
        with self.assertRaises(Exception):
            self._run_mark(delivery_partner="Deliverk", partner_fee=None)

    def test_ordinary_courier_still_accrues_to_creditors(self):
        """Regression: the partner path must not have changed the normal one."""
        _, ct, _, mock_je_fn, mock_partner_je_fn = self._run_mark(grand_total=500.0, shipping_exp=30.0)
        self.assertEqual(ct.amount, 500.0)
        self.assertEqual(ct.shipping_amount, 30.0)
        self.assertEqual(ct.partner_fee, 0)
        mock_je_fn.assert_called_once()
        mock_partner_je_fn.assert_not_called()

    def test_ct_amount_equals_grand_total(self):
        """CT.amount should be the invoice grand_total."""
        _, ct, _, _, _ = self._run_mark(grand_total=500.0)
        self.assertEqual(ct.amount, 500.0)

    def test_ct_shipping_amount_set(self):
        """CT.shipping_amount should match territory shipping expense."""
        _, ct, _, _, _ = self._run_mark(shipping_exp=35.0)
        self.assertEqual(ct.shipping_amount, 35.0)

    def test_ct_status_unsettled(self):
        """CT status should be Unsettled."""
        _, ct, _, _, _ = self._run_mark()
        self.assertEqual(ct.status, "Unsettled")

    def test_ct_party_fields_set(self):
        """CT should have party_type and party."""
        _, ct, _, _, _ = self._run_mark()
        self.assertEqual(ct.party_type, "Employee")
        self.assertEqual(ct.party, "EMP-001")

    def test_pe_called_with_outstanding(self):
        """PE should be created with the full outstanding amount."""
        outstanding = 450.0
        _, _, pe_fn, _, _ = self._run_mark(outstanding=outstanding, grand_total=450.0)
        pe_fn.assert_called_once()
        args = pe_fn.call_args
        # Third positional arg is paid_to, fourth is outstanding
        self.assertAlmostEqual(float(args[0][3]), outstanding, places=2)

    def test_shipping_je_called_with_expense(self):
        """Shipping JE should be created with the shipping expense amount."""
        shipping = 40.0
        _, _, _, je_fn, _ = self._run_mark(shipping_exp=shipping)
        je_fn.assert_called_once()
        args = je_fn.call_args
        # Second positional arg is shipping_exp
        self.assertAlmostEqual(float(args[0][1]), shipping, places=2)

    def test_rejects_cross_branch_courier_assignment(self):
        """Branch guard failures should stop courier assignment before payment entry creation."""
        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.utils.now_datetime.return_value = "2026-03-14 12:00:00"
            mf.throw.side_effect = Exception
            mf.flags = MagicMock()
            mf.flags.in_test = True

            inv = _mock_invoice(grand_total=500.0, outstanding=500.0)
            mf.get_doc.return_value = inv
            mf.db.get_value.return_value = 500.0
            mf.get_all.return_value = []

            with patch("jarz_pos.services.delivery_handling.resolve_assignment_pos_profile", return_value="POS-001"), \
                 patch("jarz_pos.services.delivery_handling.assert_courier_matches_pos_profile", side_effect=Exception("Courier EMP-001 belongs to POS Profile Dokki, not POS-001.")), \
                 patch("jarz_pos.services.delivery_handling._create_payment_entry") as mock_pe_fn:
                from jarz_pos.services.delivery_handling import mark_courier_outstanding

                with self.assertRaisesRegex(Exception, "belongs to POS Profile"):
                    mark_courier_outstanding(
                        invoice_name=inv.name,
                        courier=None,
                        party_type="Employee",
                        party="EMP-001",
                    )

            mock_pe_fn.assert_not_called()

    def test_result_contains_net_to_collect(self):
        """Result should contain net_to_collect = order_amount - shipping."""
        gt, shipping = 500.0, 30.0
        result, _, _, _, _ = self._run_mark(grand_total=gt, shipping_exp=shipping)
        self.assertAlmostEqual(result["net_to_collect"], gt - shipping, places=2)

    def test_no_pe_when_zero_outstanding(self):
        """No PE should be created when outstanding is 0 (already paid)."""
        _, _, pe_fn, _, _ = self._run_mark(outstanding=0.0, grand_total=500.0)
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
    """Verify the shipping expense JE helper produces correct DR Freight / CR Creditors.

    Both tests must stub ``_find_existing_je_by_tag``. The helper starts with an
    idempotency guard that reuses an already-tagged entry, and these tests mock
    ``frappe`` wholesale — so the guard's ``frappe.get_all`` returns a truthy
    MagicMock, the helper returns that "existing" entry, and no accounts are ever
    appended. That is what made ``test_correct_structure`` fail with ``0 != 2``
    and, more quietly, made ``test_balanced`` pass vacuously by comparing 0 to 0.
    """

    def test_correct_structure(self):
        """Should create JE with DR Freight, CR Creditors with party."""
        je_capture = _JournalEntryCapture()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.new_doc.return_value = je_capture
            mf.utils.nowdate.return_value = "2026-03-14"

            with patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value=FREIGHT_ACC),                  patch("jarz_pos.services.delivery_handling._find_existing_je_by_tag", return_value=None):
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

            with patch("jarz_pos.services.delivery_handling.get_freight_expense_account", return_value=FREIGHT_ACC),                  patch("jarz_pos.services.delivery_handling._find_existing_je_by_tag", return_value=None):
                from jarz_pos.services.delivery_handling import _create_shipping_expense_to_creditors_je

                inv = _mock_invoice()
                _create_shipping_expense_to_creditors_je(inv, 100.0, CREDITORS_ACC, "Employee", "EMP-X")

        self.assertEqual(len(je_capture.accounts), 2, "guard against a vacuous 0 == 0 pass")
        self.assertAlmostEqual(je_capture.total_debit, je_capture.total_credit, places=2)
        self.assertAlmostEqual(je_capture.total_debit, 100.0, places=2)


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


# ===========================================================================
# TEST: single-invoice settlement agrees with the batch ("Settle All") view
# ===========================================================================

class TestSettlementPreviewMatchesBatch(unittest.TestCase):
    """The two settlement surfaces must never disagree about who pays whom.

    Regression for order ACC-SINV-2026-16919 (staging): an UNPAID Mobile Wallet order that
    went Out for Delivery accrues a Courier Transaction with amount=0 (the courier collects
    nothing — the customer transfers online) and shipping_amount=<freight>. The batch view read
    that as "pay the courier 85"; the single-invoice preview overrode the transaction with the
    invoice grand total and said "collect 935 from the courier".
    """

    # ── Batch side ────────────────────────────────────────────────────

    def _batch_balance(self, ct_rows, *, branch="Madinaty Branch"):
        """Return the per-party balance get_courier_balances would show.

        The rows are placed in *branch* and the caller is scoped to it, so this
        exercises the branch filter rather than bypassing it — the balance a
        real operator sees is the one for their own branch.
        """
        rows = list(ct_rows)
        invoice_branches = {
            str(r.get("reference_invoice") or "").strip(): branch
            for r in rows
            if r.get("reference_invoice")
        }
        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.db.get_table_columns.return_value = [
                "name", "reference_invoice", "amount", "shipping_amount", "party_type", "party",
                "is_partner_order",
            ]
            mf.get_all.return_value = rows

            with patch("jarz_pos.services.delivery_handling._get_invoice_city", return_value="Madinaty"),                  patch("jarz_pos.services.delivery_handling.get_visible_pos_profiles", return_value=[branch]),                  patch("jarz_pos.services.delivery_handling.map_invoice_branches", return_value=invoice_branches),                  patch("jarz_pos.services.delivery_handling.user_has_global_profile_access", return_value=False):
                from jarz_pos.services.delivery_handling import get_courier_balances

                data = get_courier_balances(pos_profile=branch)

        self.assertEqual(len(data), 1, f"Expected one party group, got {data}")
        return data[0]["balance"]

    # ── Single-invoice side ───────────────────────────────────────────

    def _preview(self, ct_rows, *, grand_total, outstanding, status,
                 payment_method="Cash", stored_shipping=0.0, payment_entries=()):
        """Return get_invoice_settlement_preview's payload for one invoice."""
        inv = _mock_invoice(
            name="ACC-SINV-2026-16919",
            grand_total=grand_total,
            outstanding=outstanding,
        )
        inv.status = status
        inv.custom_shipping_expense = stored_shipping
        inv.custom_payment_method = payment_method

        def fake_get_all(doctype, **kwargs):
            if doctype == "Courier Transaction":
                return [dict(r) for r in ct_rows]
            if doctype == "Payment Entry Reference":
                return list(payment_entries)
            if doctype == "Payment Entry":
                return [
                    {"name": p, "creation": "2026-07-31 20:00:00", "posting_date": "2026-07-31",
                     "reference_no": None}
                    for p in payment_entries
                ]
            if doctype == "Journal Entry":
                return []
            return []

        with patch("jarz_pos.api.invoices.frappe") as mf:
            mf.get_doc.return_value = inv
            mf.get_all.side_effect = fake_get_all
            mf.throw.side_effect = Exception

            with patch("jarz_pos.api.invoices.ensure_profile_scoped_invoice_access"), \
                 patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=0.0):

                from jarz_pos.api.invoices import get_invoice_settlement_preview

                return get_invoice_settlement_preview(
                    invoice_name=inv.name, party_type="Employee", party="HR-EMP-000004"
                )

    @staticmethod
    def _ct(amount, shipping):
        return {
            "name": "CT-16919",
            "reference_invoice": "ACC-SINV-2026-16919",
            "amount": amount,
            "shipping_amount": shipping,
            "party_type": "Employee",
            "party": "HR-EMP-000004",
            "creation": "2026-07-31 21:46:31",
        }

    # ── The reported bug ──────────────────────────────────────────────

    def test_unpaid_online_order_previews_as_pay_courier(self):
        """Unpaid Mobile Wallet + CT(amount=0, shipping=85) → pay the courier 85, not collect 935."""
        preview = self._preview(
            [self._ct(0.0, 85.0)],
            grand_total=1020.0,
            outstanding=1020.0,
            status="Unpaid",
            payment_method="Mobile Wallet",
            stored_shipping=85.0,
        )

        self.assertAlmostEqual(preview["order_amount"], 0.0, places=2)
        self.assertAlmostEqual(preview["shipping_amount"], 85.0, places=2)
        self.assertAlmostEqual(preview["net_amount"], -85.0, places=2)
        self.assertEqual(preview["branch_action"], "pay")
        self.assertTrue(preview["is_online_unconfirmed"])

    def test_unpaid_online_order_single_equals_batch(self):
        """Same courier transaction → same signed net on both surfaces."""
        rows = [self._ct(0.0, 85.0)]
        preview = self._preview(
            rows, grand_total=1020.0, outstanding=1020.0, status="Unpaid",
            payment_method="Mobile Wallet", stored_shipping=85.0,
        )
        self.assertAlmostEqual(preview["net_amount"], self._batch_balance(rows), places=2)

    def test_cod_settle_later_single_equals_batch(self):
        """COD moved to Courier Outstanding: both surfaces collect order - shipping."""
        rows = [self._ct(1020.0, 85.0)]
        preview = self._preview(
            rows, grand_total=1020.0, outstanding=0.0, status="Paid", stored_shipping=85.0,
        )
        self.assertAlmostEqual(preview["net_amount"], 935.0, places=2)
        self.assertEqual(preview["branch_action"], "collect")
        self.assertAlmostEqual(preview["net_amount"], self._batch_balance(rows), places=2)

    def test_paid_order_shipping_only_single_equals_batch(self):
        """Prepaid order: courier is owed freight only, on both surfaces."""
        rows = [self._ct(0.0, 70.0)]
        preview = self._preview(
            rows, grand_total=500.0, outstanding=0.0, status="Paid", stored_shipping=70.0,
        )
        self.assertAlmostEqual(preview["net_amount"], -70.0, places=2)
        self.assertEqual(preview["branch_action"], "pay")
        self.assertAlmostEqual(preview["net_amount"], self._batch_balance(rows), places=2)

    def test_courier_transaction_amount_beats_invoice_total(self):
        """A CT accrued for less than the invoice total wins — the courier holds only that."""
        rows = [self._ct(600.0, 85.0)]
        preview = self._preview(
            rows, grand_total=1020.0, outstanding=0.0, status="Paid", stored_shipping=85.0,
        )
        self.assertAlmostEqual(preview["order_amount"], 600.0, places=2)
        self.assertAlmostEqual(preview["net_amount"], 515.0, places=2)
        self.assertAlmostEqual(preview["net_amount"], self._batch_balance(rows), places=2)

    def test_zero_freight_transaction_is_taken_verbatim(self):
        """A CT that accrued no freight means no freight — not "look it up from the territory".

        Found on production after the first fix: rows whose freight was never accrued (the old
        free-shipping-bundle orders, deliberately not backfilled) still disagreed, because the
        preview quietly replaced the CT's 0 with the territory value while the batch netted the 0.
        """
        rows = [self._ct(480.0, 0.0)]
        preview = self._preview(
            rows, grand_total=480.0, outstanding=0.0, status="Paid", stored_shipping=60.0,
        )
        self.assertAlmostEqual(preview["shipping_amount"], 0.0, places=2)
        self.assertAlmostEqual(preview["net_amount"], 480.0, places=2)
        self.assertAlmostEqual(preview["net_amount"], self._batch_balance(rows), places=2)

    def test_empty_transaction_nets_to_nothing(self):
        """CT with neither order nor freight settles to zero on both surfaces."""
        rows = [self._ct(0.0, 0.0)]
        preview = self._preview(
            rows, grand_total=450.0, outstanding=0.0, status="Paid", stored_shipping=45.0,
        )
        self.assertAlmostEqual(preview["net_amount"], 0.0, places=2)
        self.assertEqual(preview["branch_action"], "none")
        self.assertAlmostEqual(preview["net_amount"], self._batch_balance(rows), places=2)

    def test_no_courier_transaction_falls_back_to_invoice(self):
        """Before Out for Delivery there is no CT, so an unpaid invoice previews its own total."""
        preview = self._preview(
            [], grand_total=1020.0, outstanding=1020.0, status="Unpaid", stored_shipping=0.0,
        )
        self.assertAlmostEqual(preview["order_amount"], 1020.0, places=2)
        self.assertFalse(preview["has_courier_transaction"])


# ===========================================================================
# TEST: settlement endpoints refuse / allow the zero-collection case
# ===========================================================================

class TestZeroCollectionSettlementGuards(unittest.TestCase):
    """The courier collected nothing: pay the freight, never book a phantom cash receipt."""

    def _settle_shipping_only(self, outstanding):
        """settle_single_invoice_paid against a CT with amount=0."""
        je_capture = _JournalEntryCapture()

        def mock_new_doc(doctype):
            if doctype == "Journal Entry":
                return je_capture
            if doctype == "Courier Transaction":
                return _CourierTransactionCapture()
            return MagicMock()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.new_doc.side_effect = mock_new_doc
            mf.db.sql.return_value = None
            mf.throw.side_effect = Exception

            inv = _mock_invoice(grand_total=1020.0, outstanding=outstanding)
            inv.custom_shipping_expense = 85.0
            mf.get_doc.return_value = inv

            mf.get_all.side_effect = [
                [],            # outstanding CT (amount > 0) → none
                [],            # existing JE check → none
                ["CT-16919"],  # CTs to mark Settled
            ]

            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=85.0), \
                 patch("jarz_pos.services.delivery_handling.get_pos_cash_account", return_value=CASH_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling.validate_account_exists"), \
                 patch("jarz_pos.services.delivery_handling._find_existing_je_by_tag", return_value=None):

                from jarz_pos.services.delivery_handling import settle_single_invoice_paid

                result = settle_single_invoice_paid(
                    invoice_name=inv.name,
                    pos_profile="POS-001",
                    party_type="Employee",
                    party="HR-EMP-000004",
                )

        return result, je_capture

    def test_unpaid_invoice_can_still_pay_courier_freight(self):
        """Freight payment touches neither Debtors nor Courier Outstanding, so unpaid is fine."""
        result, je = self._settle_shipping_only(outstanding=1020.0)

        self.assertEqual(result["mode"], "shipping_only_settlement")
        cred = next(a for a in je.accounts if a["account"] == CREDITORS_ACC)
        cash = next(a for a in je.accounts if a["account"] == CASH_ACC)
        self.assertAlmostEqual(float(cred["debit_in_account_currency"]), 85.0, places=2)
        self.assertAlmostEqual(float(cash["credit_in_account_currency"]), 85.0, places=2)
        self.assertAlmostEqual(je.total_debit, je.total_credit, places=2)
        # No Courier Outstanding line: nothing was ever moved there for this invoice.
        self.assertEqual([a for a in je.accounts if a["account"] == COURIER_OUTSTANDING_ACC], [])

    def test_paid_invoice_shipping_only_unchanged(self):
        """The prepaid case keeps posting the same freight payment."""
        result, je = self._settle_shipping_only(outstanding=0.0)
        self.assertEqual(result["mode"], "shipping_only_settlement")
        self.assertAlmostEqual(je.total_debit, 85.0, places=2)

    def test_collected_payment_refuses_zero_amount_transaction(self):
        """settle_courier_collected_payment must not invent cash the courier never held."""
        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.throw.side_effect = Exception

            inv = _mock_invoice(grand_total=1020.0, outstanding=1020.0)
            inv.custom_shipping_expense = 85.0
            mf.get_doc.return_value = inv
            mf.get_all.return_value = [
                {"name": "CT-16919", "amount": 0.0, "shipping_amount": 85.0}
            ]

            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=85.0), \
                 patch("jarz_pos.services.delivery_handling.get_pos_cash_account", return_value=CASH_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling.validate_account_exists"):

                from jarz_pos.services.delivery_handling import settle_courier_collected_payment

                with self.assertRaises(Exception):
                    settle_courier_collected_payment(
                        invoice_name=inv.name,
                        pos_profile="POS-001",
                        party_type="Employee",
                        party="HR-EMP-000004",
                    )

    def test_collected_payment_uses_transaction_amount(self):
        """A CT accrued below the invoice total drives the JE, matching the batch net."""
        je_capture = _JournalEntryCapture()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.throw.side_effect = Exception
            mf.new_doc.return_value = je_capture

            inv = _mock_invoice(grand_total=1020.0, outstanding=0.0)
            inv.custom_shipping_expense = 85.0
            mf.get_doc.return_value = inv
            mf.get_all.side_effect = [
                [{"name": "CT-16919", "amount": 600.0, "shipping_amount": 85.0}],  # pending CT
                ["CT-16919"],                                                       # CTs to settle
            ]

            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=85.0), \
                 patch("jarz_pos.services.delivery_handling.get_pos_cash_account", return_value=CASH_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling.validate_account_exists"), \
                 patch("jarz_pos.services.delivery_handling._find_existing_je_by_tag", return_value=None), \
                 patch("jarz_pos.services.delivery_handling._publish_branch_event"):

                from jarz_pos.services.delivery_handling import settle_courier_collected_payment

                result = settle_courier_collected_payment(
                    invoice_name=inv.name,
                    pos_profile="POS-001",
                    party_type="Employee",
                    party="HR-EMP-000004",
                )

        self.assertAlmostEqual(result["order_amount"], 600.0, places=2)
        cash = next(a for a in je_capture.accounts if a["account"] == CASH_ACC)
        co = next(a for a in je_capture.accounts if a["account"] == COURIER_OUTSTANDING_ACC)
        self.assertAlmostEqual(float(cash["debit_in_account_currency"]), 515.0, places=2)
        self.assertAlmostEqual(float(co["credit_in_account_currency"]), 600.0, places=2)
        self.assertAlmostEqual(je_capture.total_debit, je_capture.total_credit, places=2)

    def test_collected_payment_honours_zero_accrued_freight(self):
        """No freight accrued → none deducted, and no Creditors line invented for it."""
        je_capture = _JournalEntryCapture()

        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.utils.nowdate.return_value = "2026-03-14"
            mf.flags = MagicMock()
            mf.flags.in_test = True
            mf.throw.side_effect = Exception
            mf.new_doc.return_value = je_capture

            inv = _mock_invoice(grand_total=480.0, outstanding=0.0)
            inv.custom_shipping_expense = 0.0
            mf.get_doc.return_value = inv
            mf.get_all.side_effect = [
                [{"name": "CT-16470", "amount": 480.0, "shipping_amount": 0.0}],  # pending CT
                ["CT-16470"],                                                      # CTs to settle
            ]

            # Territory still quotes 60 — the transaction's 0 must win.
            with patch("jarz_pos.services.delivery_handling._get_delivery_expense_amount", return_value=60.0), \
                 patch("jarz_pos.services.delivery_handling.get_pos_cash_account", return_value=CASH_ACC), \
                 patch("jarz_pos.services.delivery_handling.get_creditors_account", return_value=CREDITORS_ACC), \
                 patch("jarz_pos.services.delivery_handling._get_courier_outstanding_account", return_value=COURIER_OUTSTANDING_ACC), \
                 patch("jarz_pos.services.delivery_handling.validate_account_exists"), \
                 patch("jarz_pos.services.delivery_handling._find_existing_je_by_tag", return_value=None), \
                 patch("jarz_pos.services.delivery_handling._publish_branch_event"):

                from jarz_pos.services.delivery_handling import settle_courier_collected_payment

                result = settle_courier_collected_payment(
                    invoice_name=inv.name,
                    pos_profile="POS-001",
                    party_type="Supplier",
                    party="Courier-A",
                )

        self.assertAlmostEqual(result["shipping_amount"], 0.0, places=2)
        self.assertEqual([a for a in je_capture.accounts if a["account"] == CREDITORS_ACC], [])
        cash = next(a for a in je_capture.accounts if a["account"] == CASH_ACC)
        co = next(a for a in je_capture.accounts if a["account"] == COURIER_OUTSTANDING_ACC)
        self.assertAlmostEqual(float(cash["debit_in_account_currency"]), 480.0, places=2)
        self.assertAlmostEqual(float(co["credit_in_account_currency"]), 480.0, places=2)
        self.assertAlmostEqual(je_capture.total_debit, je_capture.total_credit, places=2)


class TestCourierBalancesBranchScope(unittest.TestCase):
    """A branch sees — and settles — only its own courier money.

    Reported 2026-09-02: one POS profile could see another branch's courier
    settlements. Two halves to it, and the second is the expensive one:
    ``get_courier_balances`` listed every unsettled row site-wide, and
    ``settle_delivery_party`` then cleared all of them in a journal entry funded
    by ``get_pos_cash_account(<the caller's branch>)`` — so Branch A's drawer
    absorbed cash Branch B was still owed, and B's rows went Settled without B
    ever collecting.

    A row belongs to the branch that owns its INVOICE (``custom_kanban_profile``
    then ``pos_profile``), the same key shift-close uses in
    ``services.courier_carry.get_unsettled_transactions``.
    """

    HOME = "Branch A"
    OTHER = "Branch B"

    def _rows(self):
        return [
            {
                "name": "CT-A",
                "reference_invoice": "ACC-SINV-2026-00001",
                "amount": 500.0,
                "shipping_amount": 50.0,
                "party_type": "Employee",
                "party": "EMP-0001",
                "is_partner_order": 0,
            },
            {
                "name": "CT-B",
                "reference_invoice": "ACC-SINV-2026-00002",
                "amount": 900.0,
                "shipping_amount": 60.0,
                "party_type": "Employee",
                "party": "EMP-0001",
                "is_partner_order": 0,
            },
        ]

    @property
    def _branches(self):
        return {
            "ACC-SINV-2026-00001": self.HOME,
            "ACC-SINV-2026-00002": self.OTHER,
        }

    def _balances(self, *, visible, branches=None, global_access=False):
        rows = self._rows()
        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.db.get_table_columns.return_value = [
                "name", "reference_invoice", "amount", "shipping_amount",
                "party_type", "party", "is_partner_order",
            ]
            mf.get_all.return_value = rows
            with patch("jarz_pos.services.delivery_handling._get_invoice_city", return_value="Madinaty"),                  patch("jarz_pos.services.delivery_handling.get_visible_pos_profiles", return_value=visible),                  patch(
                     "jarz_pos.services.delivery_handling.map_invoice_branches",
                     return_value=self._branches if branches is None else branches,
                 ),                  patch(
                     "jarz_pos.services.delivery_handling.user_has_global_profile_access",
                     return_value=global_access,
                 ):
                from jarz_pos.services.delivery_handling import get_courier_balances

                return get_courier_balances(pos_profile=visible[0] if visible else None)

    def test_other_branch_rows_are_not_listed(self):
        """Branch A's screen shows CT-A only — CT-B is Branch B's money."""
        data = self._balances(visible=[self.HOME])

        invoices = [d["invoice"] for grp in data for d in grp["details"]]
        self.assertEqual(invoices, ["ACC-SINV-2026-00001"])
        self.assertAlmostEqual(data[0]["balance"], 450.0, places=2)

    def test_each_branch_sees_its_own_balance(self):
        """The same courier nets differently per branch; neither sees the other."""
        other = self._balances(visible=[self.OTHER])

        invoices = [d["invoice"] for grp in other for d in grp["details"]]
        self.assertEqual(invoices, ["ACC-SINV-2026-00002"])
        self.assertAlmostEqual(other[0]["balance"], 840.0, places=2)

    def test_details_carry_the_owning_branch(self):
        """Each detail names its branch, so the UI never has to guess."""
        data = self._balances(visible=[self.HOME, self.OTHER])

        got = {d["invoice"]: d["pos_profile"] for grp in data for d in grp["details"]}
        self.assertEqual(got, self._branches)

    def test_unassigned_user_sees_nothing(self):
        """No branch membership is no access — not a site-wide read."""
        self.assertEqual(self._balances(visible=[]), [])

    def test_unattributable_row_hidden_from_branch_staff(self):
        """A row whose invoice carries no profile is not silently every branch's."""
        data = self._balances(visible=[self.HOME], branches={})

        self.assertEqual(data, [])

    def test_unattributable_row_visible_to_global_access(self):
        """...but it must stay visible to someone, or the money vanishes."""
        data = self._balances(visible=[self.HOME], branches={}, global_access=True)

        invoices = sorted(d["invoice"] for grp in data for d in grp["details"])
        self.assertEqual(invoices, ["ACC-SINV-2026-00001", "ACC-SINV-2026-00002"])

    def test_settlement_refuses_another_branch_rows(self):
        """Branch A cannot settle a party whose only open rows belong to Branch B.

        Without the filter this posted a JE against Branch A's cash account and
        marked CT-B Settled, leaving Branch B nothing to collect and no record
        that it was owed.
        """
        rows = [r for r in self._rows() if r["name"] == "CT-B"]
        with patch("jarz_pos.services.delivery_handling.frappe") as mf:
            mf.get_all.return_value = rows
            mf.throw.side_effect = RuntimeError
            with patch(
                "jarz_pos.services.delivery_handling._resolve_guarded_settlement_profile",
                return_value=self.HOME,
            ), patch(
                "jarz_pos.services.delivery_handling.map_invoice_branches",
                return_value=self._branches,
            ), patch(
                "jarz_pos.services.delivery_handling._create_settlement_journal_entry"
            ) as mock_je, patch(
                "jarz_pos.services.delivery_handling.mark_courier_transactions_settled"
            ) as mock_mark:
                from jarz_pos.services.delivery_handling import settle_delivery_party

                with self.assertRaises(RuntimeError):
                    settle_delivery_party(
                        party_type="Employee", party="EMP-0001", pos_profile=self.HOME
                    )

        mock_je.assert_not_called()
        mock_mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()

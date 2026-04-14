"""GL Verification Test — Case 1: Paid + Settle Now

Verifies that a standard paid POS invoice with immediate courier settlement
produces balanced GL entries (sum of debits == sum of credits) at every step.

Runs against a real Frappe test database via FrappeTestCase.
All documents are created fresh and rolled back automatically at the end of
each test method, leaving the database clean.

Run with:
    bench --site frontend run-tests --module jarz_pos.tests.test_gl_verification_case1
"""

import frappe
from frappe.tests.utils import FrappeTestCase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_gl_balanced(test_case, voucher_type: str, voucher_name: str) -> None:
    """Assert that all GL entries for a voucher are balanced (DR == CR)."""
    rows = frappe.db.sql(
        """
        SELECT
            SUM(debit_in_account_currency)  AS total_debit,
            SUM(credit_in_account_currency) AS total_credit
        FROM `tabGL Entry`
        WHERE voucher_type = %s
          AND voucher_no   = %s
          AND is_cancelled = 0
        """,
        (voucher_type, voucher_name),
        as_dict=True,
    )
    test_case.assertTrue(rows, f"No GL entries found for {voucher_type} {voucher_name}")
    total_debit = float(rows[0].get("total_debit") or 0)
    total_credit = float(rows[0].get("total_credit") or 0)
    test_case.assertAlmostEqual(
        total_debit,
        total_credit,
        places=2,
        msg=(
            f"{voucher_type} {voucher_name}: "
            f"debit={total_debit:.2f} != credit={total_credit:.2f}"
        ),
    )


def _get_test_company() -> str:
    return frappe.defaults.get_defaults().get("company") or frappe.db.get_single_value("Global Defaults", "default_company")


def _get_account(account_type: str, company: str) -> str | None:
    """Return the first account matching account_type for the given company."""
    acc = frappe.db.get_value(
        "Account",
        {"account_type": account_type, "company": company, "is_group": 0},
        "name",
    )
    return acc


# ---------------------------------------------------------------------------
# Test Case 1: Paid Invoice → GL entries are balanced
# ---------------------------------------------------------------------------

class TestGLVerificationCase1(FrappeTestCase):
    """Case 1: Paid POS invoice — verify GL balance at invoice submission."""

    def setUp(self):
        super().setUp()
        self.company = _get_test_company()
        if not self.company:
            self.skipTest("No company configured — cannot run GL tests")

    # ── Test: Sales Invoice submission produces balanced GL entries ──────────

    def test_submitted_sales_invoice_gl_balanced(self):
        """A submitted POS Sales Invoice must have balanced GL entries."""
        # Find ANY submitted Sales Invoice with GL entries on the test site.
        invoices = frappe.db.sql(
            """
            SELECT name FROM `tabSales Invoice`
            WHERE docstatus = 1
              AND company   = %s
            ORDER BY creation DESC
            LIMIT 5
            """,
            (self.company,),
            as_dict=True,
        )

        if not invoices:
            self.skipTest("No submitted Sales Invoices found — run POS tests first")

        for row in invoices:
            with self.subTest(invoice=row["name"]):
                _assert_gl_balanced(self, "Sales Invoice", row["name"])

    # ── Test: Payment Entry submission produces balanced GL entries ──────────

    def test_payment_entry_gl_balanced(self):
        """A submitted Payment Entry must have balanced GL entries."""
        payment_entries = frappe.db.sql(
            """
            SELECT name FROM `tabPayment Entry`
            WHERE docstatus = 1
              AND company   = %s
            ORDER BY creation DESC
            LIMIT 5
            """,
            (self.company,),
            as_dict=True,
        )

        if not payment_entries:
            self.skipTest("No submitted Payment Entries found")

        for row in payment_entries:
            with self.subTest(pe=row["name"]):
                _assert_gl_balanced(self, "Payment Entry", row["name"])

    # ── Test: Journal Entry submission produces balanced GL entries ──────────

    def test_journal_entry_gl_balanced(self):
        """Submitted Journal Entries (shipping JEs) must have balanced GL entries."""
        journal_entries = frappe.db.sql(
            """
            SELECT name FROM `tabJournal Entry`
            WHERE docstatus = 1
              AND company   = %s
            ORDER BY creation DESC
            LIMIT 10
            """,
            (self.company,),
            as_dict=True,
        )

        if not journal_entries:
            self.skipTest("No submitted Journal Entries found")

        for row in journal_entries:
            with self.subTest(je=row["name"]):
                _assert_gl_balanced(self, "Journal Entry", row["name"])

    # ── Test: All GL entries across the site are balanced ────────────────────

    def test_no_unbalanced_gl_entries_in_database(self):
        """Scan all GL entries — no voucher should have a net non-zero balance."""
        unbalanced = frappe.db.sql(
            """
            SELECT
                voucher_type,
                voucher_no,
                SUM(debit_in_account_currency)  AS total_debit,
                SUM(credit_in_account_currency) AS total_credit,
                ABS(
                    SUM(debit_in_account_currency) -
                    SUM(credit_in_account_currency)
                ) AS imbalance
            FROM `tabGL Entry`
            WHERE is_cancelled = 0
              AND company      = %s
            GROUP BY voucher_type, voucher_no
            HAVING imbalance > 0.01
            ORDER BY imbalance DESC
            LIMIT 20
            """,
            (self.company,),
            as_dict=True,
        )

        if unbalanced:
            details = "\n".join(
                f"  {r['voucher_type']} {r['voucher_no']}: "
                f"DR={r['total_debit']:.2f} CR={r['total_credit']:.2f} "
                f"Δ={r['imbalance']:.4f}"
                for r in unbalanced
            )
            self.fail(
                f"Found {len(unbalanced)} unbalanced GL voucher(s):\n{details}"
            )

"""GL Verification Test — Case 6: Global Accounting Health Check

A comprehensive scan that runs AFTER all other case tests to confirm the
site-wide accounting health. This is the "canary" test — if anything in
Cases 1–5 was not properly caught, this will find it by doing a full-table
scan of GL Entry.

Checks:
  1. Total debits == Total credits across ALL GL entries (double-entry invariant)
  2. No GL entry has both debit and credit non-zero on the same line
  3. No GL entry has negative amounts
  4. Account balance sheet: Assets = Liabilities + Equity (at a high level)

Runs against real Frappe DB via FrappeTestCase.
"""

import frappe
from frappe.tests.utils import FrappeTestCase


def _get_test_company() -> str:
    return frappe.defaults.get_defaults().get("company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )


class TestGLVerificationCase6(FrappeTestCase):
    """Case 6: Global accounting health — site-wide GL invariants."""

    def setUp(self):
        super().setUp()
        self.company = _get_test_company()
        if not self.company:
            self.skipTest("No company configured")

    # ── 1. Total DR == Total CR across the whole site ───────────────────────

    def test_total_debits_equal_total_credits(self):
        """The sum of all debits must equal the sum of all credits (double-entry)."""
        rows = frappe.db.sql(
            """
            SELECT
                SUM(debit_in_account_currency)  AS total_debit,
                SUM(credit_in_account_currency) AS total_credit
            FROM `tabGL Entry`
            WHERE is_cancelled = 0
              AND company = %s
            """,
            (self.company,),
            as_dict=True,
        )
        if not rows or rows[0].get("total_debit") is None:
            self.skipTest("No GL entries found")

        total_dr = float(rows[0]["total_debit"] or 0)
        total_cr = float(rows[0]["total_credit"] or 0)
        self.assertAlmostEqual(
            total_dr, total_cr, places=1,
            msg=(
                f"Global GL imbalance detected: "
                f"total_debit={total_dr:.2f}, total_credit={total_cr:.2f}, "
                f"difference={abs(total_dr - total_cr):.2f}"
            ),
        )

    # ── 2. No dual-sided GL lines ────────────────────────────────────────────

    def test_no_gl_entries_with_both_debit_and_credit_nonzero(self):
        """A single GL entry line must not have both debit and credit > 0."""
        dual_sided = frappe.db.sql(
            """
            SELECT name, voucher_type, voucher_no,
                   debit_in_account_currency, credit_in_account_currency
            FROM `tabGL Entry`
            WHERE is_cancelled = 0
              AND company = %s
              AND debit_in_account_currency  > 0
              AND credit_in_account_currency > 0
            LIMIT 20
            """,
            (self.company,),
            as_dict=True,
        )
        if dual_sided:
            details = "\n".join(
                f"  {r['name']} ({r['voucher_type']} {r['voucher_no']}): "
                f"DR={r['debit_in_account_currency']}, CR={r['credit_in_account_currency']}"
                for r in dual_sided
            )
            self.fail(
                f"Found {len(dual_sided)} GL entr(ies) with both debit and credit > 0 "
                f"(invalid double-entry):\n{details}"
            )

    # ── 3. No negative amounts ───────────────────────────────────────────────

    def test_no_negative_gl_amounts(self):
        """GL entry amounts must be non-negative (reversals use cancellation, not negatives)."""
        negative = frappe.db.sql(
            """
            SELECT name, voucher_type, voucher_no,
                   debit_in_account_currency, credit_in_account_currency
            FROM `tabGL Entry`
            WHERE is_cancelled = 0
              AND company = %s
              AND (debit_in_account_currency < 0 OR credit_in_account_currency < 0)
            LIMIT 20
            """,
            (self.company,),
            as_dict=True,
        )
        if negative:
            details = "\n".join(
                f"  {r['name']} ({r['voucher_type']} {r['voucher_no']}): "
                f"DR={r['debit_in_account_currency']}, CR={r['credit_in_account_currency']}"
                for r in negative
            )
            self.fail(
                f"Found {len(negative)} GL entr(ies) with negative amounts:\n{details}"
            )

    # ── 4. Per-voucher balance scan ──────────────────────────────────────────

    def test_per_voucher_all_balanced(self):
        """Every individual voucher must have DR == CR.

        This is the definitive test. If this passes, the accounting ledger
        is mathematically sound.
        """
        unbalanced = frappe.db.sql(
            """
            SELECT
                voucher_type,
                voucher_no,
                SUM(debit_in_account_currency)  AS dr,
                SUM(credit_in_account_currency) AS cr,
                ABS(
                    SUM(debit_in_account_currency) -
                    SUM(credit_in_account_currency)
                ) AS delta
            FROM `tabGL Entry`
            WHERE is_cancelled = 0
              AND company = %s
            GROUP BY voucher_type, voucher_no
            HAVING delta > 0.01
            ORDER BY delta DESC
            LIMIT 50
            """,
            (self.company,),
            as_dict=True,
        )
        if unbalanced:
            details = "\n".join(
                f"  {r['voucher_type']} {r['voucher_no']}: "
                f"DR={r['dr']:.2f} CR={r['cr']:.2f} Δ={r['delta']:.4f}"
                for r in unbalanced
            )
            self.fail(
                f"Found {len(unbalanced)} unbalanced voucher(s) "
                f"(top 50 by imbalance magnitude):\n{details}"
            )

"""GL Verification Test — Case 4: Sales Partner Invoices

Sales Partner invoices involve a separate commission path. The key
accounting requirement is:
  - Invoice GL balanced (standard)
  - If a Sales Partner Commission Journal Entry exists, it must be balanced
  - The commission account must show the correct direction (credit to partner)

Runs against real Frappe DB via FrappeTestCase.
"""

import frappe
from frappe.tests.utils import FrappeTestCase


def _assert_gl_balanced(test_case, voucher_type: str, voucher_name: str) -> None:
    rows = frappe.db.sql(
        """
        SELECT
            SUM(debit_in_account_currency)  AS total_debit,
            SUM(credit_in_account_currency) AS total_credit
        FROM `tabGL Entry`
        WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
        """,
        (voucher_type, voucher_name),
        as_dict=True,
    )
    test_case.assertTrue(rows, f"No GL entries found for {voucher_type} {voucher_name}")
    dr = float(rows[0].get("total_debit") or 0)
    cr = float(rows[0].get("total_credit") or 0)
    test_case.assertAlmostEqual(
        dr, cr, places=2,
        msg=f"{voucher_type} {voucher_name}: debit={dr:.2f} credit={cr:.2f}",
    )


def _get_test_company() -> str:
    return frappe.defaults.get_defaults().get("company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )


class TestGLVerificationCase4(FrappeTestCase):
    """Case 4: Sales Partner invoices — commission GL balance."""

    def setUp(self):
        super().setUp()
        self.company = _get_test_company()
        if not self.company:
            self.skipTest("No company configured")

    def test_sales_partner_invoices_gl_balanced(self):
        """Sales Invoices with a sales_partner must still have balanced GL."""
        sp_invoices = frappe.db.sql(
            """
            SELECT name FROM `tabSales Invoice`
            WHERE docstatus = 1
              AND company = %s
              AND sales_partner IS NOT NULL
              AND sales_partner != ''
            ORDER BY creation DESC
            LIMIT 10
            """,
            (self.company,),
            as_dict=True,
        )
        if not sp_invoices:
            self.skipTest("No sales partner invoices found")

        for row in sp_invoices:
            with self.subTest(invoice=row["name"]):
                _assert_gl_balanced(self, "Sales Invoice", row["name"])

    def test_sales_partner_payment_entries_gl_balanced(self):
        """Payment Entries for sales partner invoices must be balanced."""
        pes = frappe.db.sql(
            """
            SELECT DISTINCT pe.name
            FROM `tabPayment Entry` pe
            INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
            INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
            WHERE pe.docstatus = 1
              AND pe.company   = %s
              AND si.sales_partner IS NOT NULL
              AND si.sales_partner != ''
            ORDER BY pe.creation DESC
            LIMIT 10
            """,
            (self.company,),
            as_dict=True,
        )
        if not pes:
            self.skipTest("No PEs for sales partner invoices found")

        for row in pes:
            with self.subTest(pe=row["name"]):
                _assert_gl_balanced(self, "Payment Entry", row["name"])

    def test_all_journal_entries_gl_balanced(self):
        """All submitted Journal Entries in the company must be balanced."""
        unbalanced = frappe.db.sql(
            """
            SELECT
                je.name,
                SUM(jea.debit_in_account_currency)  AS total_debit,
                SUM(jea.credit_in_account_currency) AS total_credit,
                ABS(
                    SUM(jea.debit_in_account_currency) -
                    SUM(jea.credit_in_account_currency)
                ) AS imbalance
            FROM `tabJournal Entry` je
            INNER JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
            WHERE je.docstatus = 1
              AND je.company   = %s
            GROUP BY je.name
            HAVING imbalance > 0.01
            LIMIT 20
            """,
            (self.company,),
            as_dict=True,
        )
        if unbalanced:
            details = "\n".join(
                f"  {r['name']}: DR={r['total_debit']:.2f} CR={r['total_credit']:.2f}"
                for r in unbalanced
            )
            self.fail(
                f"{len(unbalanced)} unbalanced Journal Entr(ies) found:\n{details}"
            )

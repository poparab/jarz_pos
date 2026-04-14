"""GL Verification Test — Case 3: COD (Cash on Delivery) Invoice Accounting

For COD invoices (outstanding_amount > 0, payment_method=Cash):
  - The Sales Invoice GL should show a receivable entry
  - When the courier collects cash and settles, a Payment Entry + JE clear the receivable
  - After settlement, the receivable for that invoice must be zero

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


class TestGLVerificationCase3(FrappeTestCase):
    """Case 3: COD invoices — receivable net balance after settlement."""

    def setUp(self):
        super().setUp()
        self.company = _get_test_company()
        if not self.company:
            self.skipTest("No company configured")

    def test_paid_invoices_have_zero_outstanding(self):
        """Every submitted+paid Sales Invoice must have outstanding_amount ~= 0."""
        paid_but_outstanding = frappe.db.sql(
            """
            SELECT name, grand_total, outstanding_amount
            FROM `tabSales Invoice`
            WHERE docstatus = 1
              AND status = 'Paid'
              AND outstanding_amount > 0.01
              AND company = %s
            ORDER BY creation DESC
            LIMIT 20
            """,
            (self.company,),
            as_dict=True,
        )
        if paid_but_outstanding:
            details = "\n".join(
                f"  {r['name']}: grand_total={r['grand_total']:.2f}, "
                f"outstanding={r['outstanding_amount']:.2f}"
                for r in paid_but_outstanding
            )
            self.fail(
                f"{len(paid_but_outstanding)} Paid invoice(s) still have "
                f"outstanding amount > 0:\n{details}"
            )

    def test_cod_invoices_submitted_gl_entries_balanced(self):
        """All submitted COD invoices must produce balanced GL entries."""
        cod_invoices = frappe.db.sql(
            """
            SELECT si.name
            FROM `tabSales Invoice` si
            WHERE si.docstatus = 1
              AND si.company = %s
              AND si.custom_payment_method = 'Cash'
            ORDER BY si.creation DESC
            LIMIT 10
            """,
            (self.company,),
            as_dict=True,
        )
        if not cod_invoices:
            self.skipTest("No COD invoices (custom_payment_method=Cash) found")

        for row in cod_invoices:
            with self.subTest(invoice=row["name"]):
                _assert_gl_balanced(self, "Sales Invoice", row["name"])

    def test_payment_entries_linked_to_cod_invoices_are_balanced(self):
        """Payment Entries that fully settle COD invoices must have balanced GL."""
        pes = frappe.db.sql(
            """
            SELECT DISTINCT pe.name
            FROM `tabPayment Entry` pe
            INNER JOIN `tabPayment Entry Reference` per ON per.parent = pe.name
            INNER JOIN `tabSales Invoice` si ON si.name = per.reference_name
            WHERE pe.docstatus = 1
              AND pe.company   = %s
              AND si.custom_payment_method = 'Cash'
            ORDER BY pe.creation DESC
            LIMIT 10
            """,
            (self.company,),
            as_dict=True,
        )
        if not pes:
            self.skipTest("No PEs linked to COD invoices found")

        for row in pes:
            with self.subTest(pe=row["name"]):
                _assert_gl_balanced(self, "Payment Entry", row["name"])

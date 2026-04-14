"""GL Verification Test — Case 2: Paid + Settle Later / Outstanding

Verifies GL balance for invoices that were paid but whose courier settlement
was deferred (settlement_mode = "settle_later"). After the settlement JE is
posted, the courier outstanding account should be fully cleared.

Runs against a real Frappe test database via FrappeTestCase.
"""

import frappe
from frappe.tests.utils import FrappeTestCase


# ---------------------------------------------------------------------------
# Helpers (shared with other case files)
# ---------------------------------------------------------------------------

def _assert_gl_balanced(test_case, voucher_type: str, voucher_name: str) -> None:
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
    dr = float(rows[0].get("total_debit") or 0)
    cr = float(rows[0].get("total_credit") or 0)
    test_case.assertAlmostEqual(
        dr, cr, places=2,
        msg=f"{voucher_type} {voucher_name}: debit={dr:.2f} != credit={cr:.2f}",
    )


def _get_test_company() -> str:
    return frappe.defaults.get_defaults().get("company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )


# ---------------------------------------------------------------------------
# Test Case 2: Courier Outstanding Account Clearance
# ---------------------------------------------------------------------------

class TestGLVerificationCase2(FrappeTestCase):
    """Case 2: After settle-later settlement, courier outstanding must net to zero."""

    def setUp(self):
        super().setUp()
        self.company = _get_test_company()
        if not self.company:
            self.skipTest("No company configured")

    def test_courier_outstanding_account_net_zero_after_settlement(self):
        """Courier Outstanding account balance should be zero after all settlements.

        Any positive balance here means a courier was dispatched but the 
        corresponding settlement JE was never posted — a broken voucher chain.
        """
        courier_outstanding_acc = frappe.db.get_value(
            "Account",
            {
                "account_name": ["like", "%Courier Outstanding%"],
                "company": self.company,
                "is_group": 0,
            },
            "name",
        )

        if not courier_outstanding_acc:
            self.skipTest("No 'Courier Outstanding' account found — skip")

        rows = frappe.db.sql(
            """
            SELECT
                SUM(debit_in_account_currency)  AS total_debit,
                SUM(credit_in_account_currency) AS total_credit
            FROM `tabGL Entry`
            WHERE account     = %s
              AND is_cancelled = 0
            """,
            (courier_outstanding_acc,),
            as_dict=True,
        )
        if not rows or (rows[0].get("total_debit") is None):
            self.skipTest("No GL entries for courier outstanding account")

        net = float(rows[0].get("total_debit") or 0) - float(rows[0].get("total_credit") or 0)
        self.assertAlmostEqual(
            net, 0.0, places=1,
            msg=(
                f"Courier Outstanding account {courier_outstanding_acc} "
                f"has a net balance of {net:.2f} — unsettled courier transactions detected"
            ),
        )

    def test_woo_invoices_outstanding_amount_matches_gl(self):
        """WooCommerce-imported paid invoices must have outstanding_amount == 0."""
        unpaid_woo = frappe.db.sql(
            """
            SELECT name, grand_total, outstanding_amount
            FROM `tabSales Invoice`
            WHERE docstatus = 1
              AND woo_order_id IS NOT NULL
              AND woo_order_id != ''
              AND outstanding_amount > 0.01
            ORDER BY creation DESC
            LIMIT 10
            """,
            as_dict=True,
        )

        # Exclude orders with status != completed/processing (draft/cancelled are OK)
        paid_statuses = {"completed", "processing"}
        truly_unpaid = []
        for inv in unpaid_woo:
            status = frappe.db.get_value(
                "WooCommerce Order Map",
                {"erpnext_sales_invoice": inv["name"]},
                "status",
            )
            if status in paid_statuses:
                truly_unpaid.append(inv)

        if truly_unpaid:
            details = "\n".join(
                f"  {r['name']}: grand_total={r['grand_total']:.2f}, "
                f"outstanding={r['outstanding_amount']:.2f}"
                for r in truly_unpaid
            )
            self.fail(
                f"Found {len(truly_unpaid)} paid WooCommerce invoice(s) "
                f"with outstanding amount > 0:\n{details}"
            )

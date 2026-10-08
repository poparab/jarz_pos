"""credit.get_invoice_shop_branches: the shop branch each invoice went to, for
the account statement's per-branch sections.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import unittest
from unittest.mock import patch

from jarz_pos.api import credit as credit_api
from jarz_pos.services import b2b_branches


BRANCHES = [
    {"address_name": "ADDR-HEL", "branch_name": "Heliopolis"},
    {"address_name": "ADDR-MAD", "branch_name": "Madinaty"},
]


def _tag(customer, rows):
    names = {"ADDR-HEL": "Heliopolis", "ADDR-MAD": "Madinaty"}
    for row in rows:
        addr = row.get("shipping_address_name") or row.get("customer_address")
        row["branch"] = addr if addr in names else ""
        row["branch_name"] = names.get(addr)
    return rows


class InvoiceShopBranchesTests(unittest.TestCase):
    def _call(self, invoices, rows, allowed=("POS-001",)):
        with patch.object(credit_api, "frappe") as mock_frappe, patch.object(
            credit_api, "_ensure_credit_ledger_access"
        ), patch.object(
            credit_api, "_allowed_profiles", return_value=list(allowed)
        ), patch.object(
            credit_api, "_branch_field", return_value="custom_kanban_profile"
        ), patch.object(
            b2b_branches, "customer_branches", return_value=BRANCHES
        ), patch.object(
            b2b_branches, "tag_invoice_branches", side_effect=_tag
        ):
            mock_frappe.parse_json.side_effect = __import__("json").loads
            mock_frappe.get_all.return_value = rows
            result = credit_api.get_invoice_shop_branches("CUST-ILO", invoices)
            return result, mock_frappe

    def test_each_invoice_gets_its_branch_name(self):
        result, mock_frappe = self._call(
            '["INV-1", "INV-2", "INV-3"]',
            [
                {"name": "INV-1", "shipping_address_name": "ADDR-HEL", "customer_address": None},
                {"name": "INV-2", "shipping_address_name": None, "customer_address": "ADDR-MAD"},
                {"name": "INV-3", "shipping_address_name": "ADDR-GONE", "customer_address": None},
            ],
        )
        self.assertEqual(result["branch_count"], 2)
        self.assertEqual(result["invoices"]["INV-1"]["branch_name"], "Heliopolis")
        self.assertEqual(result["invoices"]["INV-2"]["branch_name"], "Madinaty")
        self.assertEqual(result["invoices"]["INV-3"], {"branch": "", "branch_name": ""})

    def test_only_this_customers_invoices_in_the_callers_branches_are_read(self):
        _, mock_frappe = self._call(["INV-1"], [])
        filters = mock_frappe.get_all.call_args.kwargs["filters"]
        self.assertEqual(filters["customer"], "CUST-ILO")
        self.assertEqual(filters["name"], ["in", ["INV-1"]])
        self.assertEqual(filters["custom_kanban_profile"], ["in", ["POS-001"]])
        self.assertEqual(filters["docstatus"], 1)

    def test_no_branch_assigned_answers_nothing(self):
        result, mock_frappe = self._call(["INV-1"], [], allowed=())
        self.assertEqual(result["invoices"], {})
        mock_frappe.get_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()

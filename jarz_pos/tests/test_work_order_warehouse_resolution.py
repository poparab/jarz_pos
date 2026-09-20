"""A Work Order must never be submitted against a guessed warehouse.

``_find_company_warehouse`` used to end in "any leaf warehouse for company" --
whichever row the database returned first. Every caller resolves a Work Order's
WIP or FG warehouse and ``_ensure_work_order`` SUBMITS that Work Order, so on a
site with blank Company defaults and warehouses not named for their purpose,
finished goods were manufactured into an arbitrary warehouse with nothing
saying so. It surfaced weeks later as bin drift, by which point the stock
entries were submitted.

The "both warehouses must be resolvable" guard existed the whole time and could
not fire, because the catch-all made None almost impossible. These cover both
halves: the catch-all is gone, and the guard it was masking now refuses.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestFindCompanyWarehouse(unittest.TestCase):
    def test_warehouse_type_match_still_wins(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = "Finished Goods - J"
            self.assertEqual(
                manufacturing._find_company_warehouse(
                    "JARZ", "Finished Goods", ["FG", "Finished Goods"]
                ),
                "Finished Goods - J",
            )

    def test_name_hint_match_still_wins(self):
        """production_planning documents relying on the WIP name hint here."""
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = None
            mock_frappe.get_all.return_value = [{"name": "Work In Progress - J"}]
            self.assertEqual(
                manufacturing._find_company_warehouse(
                    "JARZ", "WIP", ["WIP", "Work In Progress"]
                ),
                "Work In Progress - J",
            )

    def test_returns_none_rather_than_an_arbitrary_warehouse(self):
        """The bug: no type match and no name match used to yield ANY leaf warehouse."""
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            # Nothing matches by type, nothing matches by name. The old code
            # then ran an unfiltered get_value and returned whatever came back.
            mock_frappe.db.get_value.return_value = None
            mock_frappe.get_all.return_value = []

            result = manufacturing._find_company_warehouse(
                "JARZ", "Finished Goods", ["FG", "Finished Goods"]
            )

        self.assertIsNone(result)

    def test_does_not_query_for_an_unfiltered_leaf_warehouse(self):
        """Stronger than the None check: the catch-all query must not be issued.

        A future refactor could reintroduce the fallback and still return None
        in this mock's shape; asserting the query is never made pins the
        behaviour rather than one of its outcomes.
        """
        from jarz_pos.api import manufacturing

        calls = []

        def record_get_value(doctype, filters=None, fieldname=None, **kwargs):
            calls.append((doctype, filters))
            return None

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = record_get_value
            mock_frappe.get_all.return_value = []
            manufacturing._find_company_warehouse(
                "JARZ", "Finished Goods", ["FG", "Finished Goods"]
            )

        # The only legitimate Warehouse get_value carries a warehouse_type.
        for doctype, filters in calls:
            if doctype != "Warehouse":
                continue
            self.assertIn(
                "warehouse_type",
                filters or {},
                f"unfiltered leaf-warehouse lookup reintroduced: {filters!r}",
            )


class TestEnsureWorkOrderRefuses(unittest.TestCase):
    def test_refuses_and_names_the_missing_warehouse(self):
        from jarz_pos.api import manufacturing

        line = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 5}

        with patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses",
            return_value={"company": "JARZ", "wip_warehouse": "", "fg_warehouse": ""},
        ), patch(
            "jarz_pos.api.manufacturing._find_company_warehouse", return_value=None
        ), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            mock_frappe.throw.side_effect = frappe.ValidationError
            mock_frappe._ = lambda text: text

            with self.assertRaises(frappe.ValidationError):
                manufacturing._ensure_work_order(line, "JARZ", {}, None)

            # It must refuse BEFORE building the document, not after.
            mock_frappe.get_doc.assert_not_called()

        message = str(mock_frappe.throw.call_args.args[0])
        self.assertIn("WIP", message)
        self.assertIn("Finished Goods", message)
        self.assertIn("JARZ", message)

    def test_a_resolvable_pair_does_not_refuse(self):
        from jarz_pos.api import manufacturing

        line = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 5}

        with patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses",
            return_value={
                "company": "JARZ",
                "wip_warehouse": "Work In Progress - J",
                "fg_warehouse": "Finished Goods - J",
            },
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe._ = lambda text: text
            mock_frappe.get_doc.return_value = MagicMock(name="WO")

            try:
                manufacturing._ensure_work_order(line, "JARZ", {}, None)
            except Exception:
                # The rest of the function is out of scope here; what matters is
                # that the guard did not fire.
                pass

        mock_frappe.throw.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch


class TestReportsAPI(unittest.TestCase):
    def test_get_final_products_report_includes_medium_group(self):
        from jarz_pos.api import reports

        items = [
            {
                "item_code": "ITEM-M",
                "item_name": "Blueberry Medium",
                "item_group": "Medium",
                "stock_uom": "Nos",
            },
            {
                "item_code": "ITEM-L",
                "item_name": "Blueberry Large",
                "item_group": "Large",
                "stock_uom": "Nos",
            },
        ]
        bins = [
            {"item_code": "ITEM-M", "warehouse": "WH-A", "actual_qty": 4},
            {"item_code": "ITEM-L", "warehouse": "WH-A", "actual_qty": 7},
        ]

        def fake_get_all(doctype, **kwargs):
            if doctype == "Item":
                self.assertEqual(
                    ["in", ["Large", "Medium", "Meduim"]],
                    kwargs["filters"]["item_group"],
                )
                return items

            if doctype == "Bin":
                self.assertEqual(
                    ["in", ["ITEM-M", "ITEM-L"]],
                    kwargs["filters"]["item_code"],
                )
                return bins

            self.fail(f"Unexpected doctype lookup: {doctype}")

        with patch("jarz_pos.api.reports._ensure_jarz_manager"), patch(
            "jarz_pos.api.reports.frappe.get_all",
            side_effect=fake_get_all,
        ):
            result = reports.get_final_products_report()

        self.assertEqual(["Medium", "Large"], [group["group_name"] for group in result["groups"]])
        self.assertEqual("Medium", result["groups"][0]["items"][0]["item_group"])
        self.assertEqual("Blueberry Medium", result["groups"][0]["items"][0]["item_name"])
        self.assertEqual(4.0, result["groups"][0]["items"][0]["total_qty"])

    def test_get_final_products_report_normalizes_legacy_meduim_group(self):
        from jarz_pos.api import reports

        items = [
            {
                "item_code": "ITEM-M",
                "item_name": "Strawberry Medium",
                "item_group": "Meduim",
                "stock_uom": "Nos",
            }
        ]
        bins = [
            {"item_code": "ITEM-M", "warehouse": "WH-B", "actual_qty": 3},
        ]

        def fake_get_all(doctype, **kwargs):
            if doctype == "Item":
                return items
            if doctype == "Bin":
                return bins
            self.fail(f"Unexpected doctype lookup: {doctype}")

        with patch("jarz_pos.api.reports._ensure_jarz_manager"), patch(
            "jarz_pos.api.reports.frappe.get_all",
            side_effect=fake_get_all,
        ):
            result = reports.get_final_products_report()

        self.assertEqual(1, len(result["groups"]))
        self.assertEqual("Medium", result["groups"][0]["group_name"])
        self.assertEqual("Medium", result["groups"][0]["items"][0]["item_group"])
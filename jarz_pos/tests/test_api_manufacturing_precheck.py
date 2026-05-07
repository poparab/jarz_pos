import unittest
from unittest.mock import MagicMock, patch


class TestManufacturingPrecheck(unittest.TestCase):
    def test_get_material_precheck_issues_reports_source_warehouse_shortage(self):
        from jarz_pos.api import manufacturing

        bom_items = {
            "PIST-SPR": {
                "item_code": "PIST-SPR",
                "item_name": "Pistachio spread",
                "qty": 1.83,
                "uom": "Kg",
                "source_warehouse": "Raw Material - J",
                "default_warehouse": None,
                "include_item_in_manufacturing": 1,
                "idx": 1,
            }
        }
        line = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 61}

        with patch(
            "jarz_pos.api.manufacturing._resolve_get_bom_items_as_dict",
            return_value=MagicMock(return_value=bom_items),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_get_latest_stock_qty",
            return_value=MagicMock(return_value=1.408),
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = 0

            issues = manufacturing._get_material_precheck_issues(line, "Jarz Co")

        self.assertEqual(1, len(issues))
        self.assertEqual("insufficient_stock", issues[0]["type"])
        self.assertEqual("Raw Material - J", issues[0]["source_warehouse"])
        self.assertAlmostEqual(1.83, issues[0]["required_qty"])
        self.assertAlmostEqual(1.408, issues[0]["available_qty"])
        self.assertAlmostEqual(0.422, issues[0]["missing_qty"], places=3)

    def test_get_bom_details_uses_live_source_warehouse_availability(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_required_material_rows",
            return_value=[
                {
                    "item_code": "PIST-SPR",
                    "item_name": "Pistachio spread",
                    "uom": "Kg",
                    "required_qty": 1.83,
                    "available_qty": 1.408,
                    "source_warehouse": "Raw Material - J",
                }
            ],
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = [
                {"name": "BOM-PIST-CAKE", "quantity": 61, "company": "Jarz Co"},
                {"item_name": "Pistachio Cheesecake", "stock_uom": "Nos"},
            ]

            details = manufacturing.get_bom_details("PIST-CAKE")

        self.assertEqual("BOM-PIST-CAKE", details["default_bom"])
        self.assertEqual(61.0, details["bom_qty"])
        self.assertEqual(1.408, details["components"][0]["available_qty"])
        self.assertEqual("Raw Material - J", details["components"][0]["source_warehouse"])

    def test_submit_work_orders_skips_work_order_creation_when_precheck_fails(self):
        from jarz_pos.api import manufacturing

        line = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 61}

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._assert_material_availability",
            side_effect=Exception("blocked by precheck"),
        ), patch("jarz_pos.api.manufacturing._ensure_work_order") as mock_ensure_work_order, patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            result = manufacturing.submit_work_orders([line])

        mock_ensure_work_order.assert_not_called()
        self.assertEqual(1, len(result["results"]))
        self.assertFalse(result["results"][0]["ok"])
        self.assertIn("blocked by precheck", result["results"][0]["error"])
        mock_frappe.log_error.assert_called()
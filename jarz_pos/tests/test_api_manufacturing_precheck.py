import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch


class TestManufacturingPrecheck(unittest.TestCase):
    def test_resolve_work_order_warehouses_prefers_item_default_fg_warehouse(self):
        from jarz_pos.api import manufacturing

        line = {"item_code": "TRANSIT-CAKE", "bom_name": "BOM-TRANSIT-CAKE", "item_qty": 2}
        defaults = {"wip_warehouse": "WIP - J", "fg_warehouse": "Finished Goods - J"}

        def fake_get_value(doctype, filters=None, fieldname=None):
            if doctype == "Item Default":
                return "Goods In Transit - J"
            if doctype == "Warehouse" and filters == "Goods In Transit - J" and fieldname == "company":
                return "Jarz Co"
            return None

        with patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = fake_get_value

            resolved = manufacturing._resolve_work_order_warehouses(line, "Jarz Co", defaults)

        self.assertEqual("WIP - J", resolved["wip_warehouse"])
        self.assertEqual("Goods In Transit - J", resolved["fg_warehouse"])

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

    def test_make_and_submit_se_applies_requested_posting_datetime(self):
        from jarz_pos.api import manufacturing

        scheduled_dt = datetime(2026, 5, 8, 14, 30, 0)
        inserted_doc = MagicMock()
        inserted_doc.name = "STE-0001"

        with patch(
            "jarz_pos.api.manufacturing._resolve_make_stock_entry",
            return_value=MagicMock(return_value={"doctype": "Stock Entry"}),
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = inserted_doc

            name = manufacturing._make_and_submit_se(
                "WO-0001",
                "Manufacture",
                3,
                scheduled_dt,
            )

        payload = mock_frappe.get_doc.call_args.args[0]
        self.assertEqual("STE-0001", name)
        self.assertEqual("2026-05-08", payload["posting_date"])
        self.assertEqual("14:30:00", payload["posting_time"])
        self.assertEqual(1, payload["set_posting_time"])
        self.assertEqual(3, payload["fg_completed_qty"])
        inserted_doc.insert.assert_called_once()
        inserted_doc.submit.assert_called_once()

    def test_make_and_submit_se_raises_original_insert_submit_error(self):
        from jarz_pos.api import manufacturing

        scheduled_dt = datetime(2026, 5, 8, 14, 30, 0)
        inserted_doc = MagicMock()
        inserted_doc.insert.side_effect = Exception("insufficient stock")

        with patch(
            "jarz_pos.api.manufacturing._resolve_make_stock_entry",
            return_value=MagicMock(return_value={"doctype": "Stock Entry"}),
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.get_doc.return_value = inserted_doc

            with self.assertRaisesRegex(Exception, "insufficient stock"):
                manufacturing._make_and_submit_se(
                    "WO-0001",
                    "Material Transfer for Manufacture",
                    3,
                    scheduled_dt,
                )

        mock_frappe.get_attr.assert_not_called()

    def test_submit_work_orders_rolls_back_failed_line_and_preserves_original_error(self):
        from jarz_pos.api import manufacturing

        line = {
            "item_code": "PIST-CAKE",
            "bom_name": "BOM-PIST-CAKE",
            "item_qty": 5,
            "scheduled_at": "2026-05-08 14:30:00",
        }
        scheduled_dt = datetime(2026, 5, 8, 14, 30, 0)

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._assert_material_availability"
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_scheduled_datetime", return_value=scheduled_dt
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-0001"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", side_effect=Exception("insufficient stock")
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.savepoint.return_value = None

            result = manufacturing.submit_work_orders([line])

        mock_frappe.db.savepoint.assert_called_once()
        mock_frappe.db.rollback.assert_called_once()
        mock_frappe.db.commit.assert_not_called()
        self.assertEqual(1, len(result["results"]))
        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual("insufficient stock", result["results"][0]["error"])

    def test_submit_work_orders_propagates_scheduled_datetime_to_follow_up_documents(self):
        from jarz_pos.api import manufacturing

        line = {
            "item_code": "PIST-CAKE",
            "bom_name": "BOM-PIST-CAKE",
            "item_qty": 5,
            "scheduled_at": "2026-05-08 14:30:00",
        }
        scheduled_dt = datetime(2026, 5, 8, 14, 30, 0)
        wo_doc = MagicMock()
        wo_doc.status = "Completed"

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._assert_material_availability"
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_scheduled_datetime", return_value=scheduled_dt
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-0001"
        ) as mock_ensure_work_order, patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", side_effect=["STE-1", "STE-2"]
        ) as mock_make_and_submit_se, patch(
            "jarz_pos.api.manufacturing._set_work_order_actual_dates"
        ) as mock_set_work_order_actual_dates, patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            mock_frappe.get_doc.return_value = wo_doc

            result = manufacturing.submit_work_orders([line])

        mock_ensure_work_order.assert_called_once_with(line, "Jarz Co", {}, scheduled_dt)
        self.assertEqual(
            [
                ("WO-0001", "Material Transfer for Manufacture", 5.0, scheduled_dt),
                ("WO-0001", "Manufacture", 5.0, scheduled_dt),
            ],
            [call.args for call in mock_make_and_submit_se.call_args_list],
        )
        mock_set_work_order_actual_dates.assert_called_once_with("WO-0001", scheduled_dt)
        self.assertTrue(result["results"][0]["ok"])

    def test_submit_work_orders_reports_resolved_work_order_warehouses(self):
        from jarz_pos.api import manufacturing

        line = {
            "item_code": "TRANSIT-CAKE",
            "bom_name": "BOM-TRANSIT-CAKE",
            "item_qty": 5,
            "scheduled_at": "2026-05-08 14:30:00",
        }
        scheduled_dt = datetime(2026, 5, 8, 14, 30, 0)
        wo_doc = MagicMock()
        wo_doc.status = "Completed"
        wo_doc.wip_warehouse = "WIP - J"
        wo_doc.fg_warehouse = "Goods In Transit - J"

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._assert_material_availability"
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults",
            return_value={"wip_warehouse": "WIP - J", "fg_warehouse": "Finished Goods - J"},
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses",
            return_value={"wip_warehouse": "WIP - J", "fg_warehouse": "Goods In Transit - J"},
        ), patch(
            "jarz_pos.api.manufacturing._resolve_scheduled_datetime", return_value=scheduled_dt
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-0001"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", side_effect=["STE-1", "STE-2"]
        ), patch(
            "jarz_pos.api.manufacturing._set_work_order_actual_dates"
        ), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            mock_frappe.get_doc.return_value = wo_doc

            result = manufacturing.submit_work_orders([line])

        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual("WIP - J", result["results"][0]["wip_warehouse"])
        self.assertEqual("Goods In Transit - J", result["results"][0]["fg_warehouse"])


class TestBasketPrecheck(unittest.TestCase):
    """The aggregate check that closes the per-line shortage hole.

    Two lines drawing on one pile of flour could each pass their own precheck
    and still bust the warehouse — and because the submit loop commits per
    line, line 1 would already have consumed stock by the time line 2 failed.
    """

    LINES = [
        {"item_code": "CAKE-A", "bom_name": "BOM-A", "item_qty": 10},
        {"item_code": "CAKE-B", "bom_name": "BOM-B", "item_qty": 10},
    ]

    SHORTAGE = {
        "item_code": "FLOUR",
        "item_name": "Flour",
        "uom": "Kg",
        "source_warehouse": "Raw Material - J",
        "required_qty": 12.0,
        "available_qty": 10.0,
        "missing_qty": 2.0,
        "reason": "insufficient_stock",
    }

    def test_short_basket_fails_every_line_without_creating_any_work_order(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_basket_shortages", return_value=[self.SHORTAGE]
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order"
        ) as mock_ensure_wo, patch(
            "jarz_pos.api.manufacturing._make_and_submit_se"
        ) as mock_se, patch(
            "jarz_pos.api.manufacturing._", new=lambda msg: msg
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            result = manufacturing.submit_work_orders(self.LINES)

        # Nothing was created and nothing was committed.
        mock_ensure_wo.assert_not_called()
        mock_se.assert_not_called()
        mock_frappe.db.savepoint.assert_not_called()
        mock_frappe.db.commit.assert_not_called()

        self.assertEqual(2, len(result["results"]))
        self.assertTrue(all(r["ok"] is False for r in result["results"]))
        self.assertEqual([self.SHORTAGE], result["basket_shortages"])
        self.assertIn("FLOUR", result["results"][0]["error"])

    def test_feasible_basket_proceeds_to_the_per_line_loop(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_basket_shortages", return_value=[]
        ), patch(
            "jarz_pos.api.manufacturing._assert_material_availability"
        ), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", side_effect=["WO-1", "WO-2"]
        ) as mock_ensure_wo, patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", return_value="STE-1"
        ), patch(
            "jarz_pos.api.manufacturing._set_work_order_actual_dates"
        ), patch("jarz_pos.api.manufacturing.frappe"):
            result = manufacturing.submit_work_orders(self.LINES)

        self.assertEqual(2, mock_ensure_wo.call_count)
        self.assertNotIn("basket_shortages", result)

    def test_strict_basket_can_be_switched_off(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_basket_shortages"
        ) as mock_shortages, patch(
            "jarz_pos.api.manufacturing._assert_material_availability"
        ), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-1"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", return_value="STE-1"
        ), patch(
            "jarz_pos.api.manufacturing._set_work_order_actual_dates"
        ), patch("jarz_pos.api.manufacturing.frappe"):
            manufacturing.submit_work_orders(self.LINES, strict_basket=False)
            # HTTP hands whitelisted args over as strings, so "0" must also mean off.
            manufacturing.submit_work_orders(self.LINES, strict_basket="0")

        mock_shortages.assert_not_called()

    def test_single_work_order_path_skips_the_aggregate_pass(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_basket_shortages"
        ) as mock_shortages, patch(
            "jarz_pos.api.manufacturing._assert_material_availability"
        ), patch(
            "jarz_pos.api.manufacturing._get_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.manufacturing._get_mfg_defaults", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses", return_value={}
        ), patch(
            "jarz_pos.api.manufacturing._ensure_work_order", return_value="WO-1"
        ), patch(
            "jarz_pos.api.manufacturing._make_and_submit_se", return_value="STE-1"
        ), patch(
            "jarz_pos.api.manufacturing._set_work_order_actual_dates"
        ), patch("jarz_pos.api.manufacturing.frappe"):
            manufacturing.submit_single_work_order("CAKE-A", "BOM-A", 10)

        # One line cannot conflict with itself; re-exploding its BOM would be
        # pure duplicated work on top of the per-line precheck.
        mock_shortages.assert_not_called()

    def test_a_broken_rollup_degrades_to_allowing_the_submit(self):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_build_basket_rollup",
            side_effect=Exception("BOM explosion blew up"),
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            shortages = manufacturing._get_basket_shortages(self.LINES, "Jarz Co")

        # It is an extra guard, not a new gate: a failure here must never block
        # a submit the per-line prechecks would have allowed.
        self.assertEqual([], shortages)
        mock_frappe.log_error.assert_called_once()


class TestCoerceFlag(unittest.TestCase):
    def test_string_forms_http_actually_sends(self):
        from jarz_pos.api import manufacturing

        for value in (False, 0, "0", "false", "False", "no", ""):
            self.assertFalse(manufacturing._coerce_flag(value, default=True), value)
        for value in (True, 1, "1", "true", "True", "yes"):
            self.assertTrue(manufacturing._coerce_flag(value, default=False), value)

    def test_none_and_nonsense_fall_back_to_the_default(self):
        from jarz_pos.api import manufacturing

        self.assertTrue(manufacturing._coerce_flag(None, default=True))
        self.assertFalse(manufacturing._coerce_flag(None, default=False))
        self.assertTrue(manufacturing._coerce_flag("maybe", default=True))

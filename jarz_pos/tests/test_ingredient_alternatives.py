"""Focused unit coverage for explicit manufacturing Item Alternatives."""

import unittest
from unittest.mock import MagicMock, patch


class Thrown(Exception):
    pass


def _item(code, **overrides):
    row = {
        "name": code,
        "item_name": code,
        "brand": None,
        "stock_uom": "Kg",
        "disabled": 0,
        "is_stock_item": 1,
        "include_item_in_manufacturing": 1,
        "allow_alternative_item": 1,
        "has_batch_no": 0,
        "has_serial_no": 0,
        "end_of_life": None,
    }
    row.update(overrides)
    return row


def _wire_throw(mock_frappe):
    mock_frappe.throw.side_effect = lambda message, *_a, **_k: (_ for _ in ()).throw(
        Thrown(str(message))
    )


class TestIngredientAlternativeValidation(unittest.TestCase):
    def test_accepts_a_direct_two_way_same_uom_alternative(self):
        from jarz_pos.api import manufacturing

        with patch.object(
            manufacturing, "_get_material_item", side_effect=[_item("ALDIA"), _item("PURATOS")]
        ), patch.object(
            manufacturing, "_direct_two_way_alternative_codes", return_value=["PURATOS"]
        ), patch.object(
            manufacturing.frappe.db, "get_value", return_value="Jarz"
        ):
            selected = manufacturing._validate_material_selection(
                "ALDIA", "PURATOS", "Jarz", "Raw Material - J"
            )

        self.assertEqual("PURATOS", selected["name"])

    def test_rejects_a_stale_or_one_way_selection(self):
        from jarz_pos.api import manufacturing

        with patch.object(
            manufacturing, "_get_material_item", side_effect=[_item("ALDIA"), _item("PURATOS")]
        ), patch.object(
            manufacturing, "_direct_two_way_alternative_codes", return_value=[]
        ), patch.object(manufacturing, "_", new=lambda message: message), patch.object(
            manufacturing, "frappe"
        ) as mock_frappe:
            _wire_throw(mock_frappe)
            with self.assertRaisesRegex(Thrown, "not a direct two-way alternative"):
                manufacturing._validate_material_selection(
                    "ALDIA", "PURATOS", "Jarz", "Raw Material - J"
                )

    def test_rejects_tracked_alternatives_until_bundle_selection_is_supported(self):
        from jarz_pos.api import manufacturing

        with patch.object(
            manufacturing,
            "_get_material_item",
            side_effect=[_item("ALDIA", has_batch_no=1), _item("PURATOS", has_batch_no=1)],
        ), patch.object(
            manufacturing, "_direct_two_way_alternative_codes", return_value=["PURATOS"]
        ), patch.object(manufacturing, "_", new=lambda message: message), patch.object(
            manufacturing, "frappe"
        ) as mock_frappe:
            _wire_throw(mock_frappe)
            with self.assertRaisesRegex(Thrown, "Tracked Items cannot be selected"):
                manufacturing._validate_material_selection("ALDIA", "PURATOS", "Jarz")

    def test_rejects_an_item_at_or_after_its_end_of_life(self):
        from jarz_pos.api import manufacturing

        with patch.object(
            manufacturing,
            "_get_material_item",
            side_effect=[_item("ALDIA"), _item("PURATOS", end_of_life="2000-01-01")],
        ), patch.object(
            manufacturing, "_direct_two_way_alternative_codes", return_value=["PURATOS"]
        ), patch.object(manufacturing, "_", new=lambda message: message), patch.object(
            manufacturing, "frappe"
        ) as mock_frappe:
            _wire_throw(mock_frappe)
            with self.assertRaisesRegex(Thrown, "reached its end of life"):
                manufacturing._validate_material_selection("ALDIA", "PURATOS", "Jarz")


class TestIngredientAlternativeApplication(unittest.TestCase):
    def test_selected_row_drives_availability_precheck_and_pricing(self):
        from jarz_pos.api import manufacturing

        rows = [
            {
                "item_code": "ALDIA",
                "item_name": "Aldia blueberry",
                "stock_uom": "Kg",
                "uom": "Kg",
                "required_qty": 4.0,
                "source_warehouse": "Raw Material - J",
                "available_qty": 3.547,
            }
        ]
        with patch.object(
            manufacturing, "_validate_material_selection", return_value=_item("PURATOS")
        ), patch.object(manufacturing, "_get_live_stock_qty", return_value=25.0):
            selected = manufacturing._apply_material_selections(
                rows, {"ALDIA": "PURATOS"}, "Jarz"
            )

        self.assertEqual("ALDIA", selected[0]["original_item_code"])
        self.assertEqual("PURATOS", selected[0]["item_code"])
        self.assertEqual(25.0, selected[0]["available_qty"])

    def test_transfer_row_keeps_original_item_and_resets_original_rate(self):
        from jarz_pos.api import manufacturing

        entry = {
            "items": [
                {
                    "item_code": "ALDIA",
                    "qty": 2,
                    "transfer_qty": 2,
                    "conversion_factor": 1,
                    "s_warehouse": "Raw Material - J",
                    "basic_rate": 111,
                    "valuation_rate": 111,
                    "description": "Aldia description",
                    "expense_account": "Original expense - J",
                    "cost_center": "Original cost center - J",
                }
            ]
        }
        with patch.object(
            manufacturing, "_validate_material_selection", return_value=_item("PURATOS")
        ):
            manufacturing._apply_stock_entry_material_selections(
                entry, {"ALDIA": "PURATOS"}, "Jarz"
            )

        row = entry["items"][0]
        self.assertEqual("ALDIA", row["original_item"])
        self.assertEqual("PURATOS", row["item_code"])
        self.assertEqual(2.0, row["transfer_qty"])
        self.assertIsNone(row["basic_rate"])
        self.assertIsNone(row["valuation_rate"])
        self.assertIsNone(row["description"])
        self.assertIsNone(row["expense_account"])
        self.assertIsNone(row["cost_center"])

    def test_rejects_two_recipe_rows_using_one_selected_item(self):
        """ERPNext groups WIP by actual item but retains one original_item.

        Its finish and transferred-quantity bookkeeping therefore cannot map
        one actual row back to two distinct Work Order Item rows safely.
        """
        from jarz_pos.api import manufacturing

        rows = [
            {"item_code": "ALDIA", "source_warehouse": "Raw - J", "required_qty": 2},
            {"item_code": "BLUEBERRY", "source_warehouse": "Raw - J", "required_qty": 3},
        ]
        selections = {"ALDIA": "PURATOS", "BLUEBERRY": "PURATOS"}
        with patch.object(
            manufacturing, "_validate_material_selection", return_value=_item("PURATOS")
        ), patch.object(
            manufacturing, "_get_live_stock_qty", return_value=25
        ), patch.object(manufacturing, "_", new=lambda message: message), patch.object(
            manufacturing, "frappe"
        ) as mock_frappe:
            _wire_throw(mock_frappe)
            with self.assertRaisesRegex(Thrown, "cannot both use PURATOS"):
                manufacturing._apply_material_selections(rows, selections, "Jarz")

    def test_rejects_an_alternative_for_a_non_stock_uom_recipe_row(self):
        """Core finish would turn 2 Box x 2.7 into 2 Kg for the alternative."""
        from jarz_pos.api import manufacturing

        rows = [
            {
                "item_code": "ALDIA",
                "uom": "Kg",
                "stock_uom": "Kg",
                "bom_uom": "Box",
                "bom_conversion_factor": 2.7,
                "source_warehouse": "Raw - J",
                "required_qty": 5.4,
            }
        ]
        with patch.object(manufacturing, "_", new=lambda message: message), patch.object(
            manufacturing, "frappe"
        ) as mock_frappe:
            _wire_throw(mock_frappe)
            with self.assertRaisesRegex(Thrown, "must use its Stock UOM Kg"):
                manufacturing._apply_material_selections(
                    rows, {"ALDIA": "PURATOS"}, "Jarz"
                )

    def test_cancel_source_map_includes_the_actual_alternative_item(self):
        from jarz_pos.api import manufacturing

        with patch.object(manufacturing, "frappe") as mock_frappe:
            mock_frappe.get_all.return_value = [
                {"item_code": "ALDIA", "source_warehouse": "Raw Material - J"}
            ]
            mock_frappe.db.sql.return_value = [
                {
                    "item_code": "PURATOS",
                    "original_item": "ALDIA",
                    "s_warehouse": "Raw Material - J",
                }
            ]
            result = manufacturing._resolve_work_order_source_warehouses("WO-1")

        self.assertEqual("Raw Material - J", result["ALDIA"])
        self.assertEqual("Raw Material - J", result["PURATOS"])


class TestIngredientAlternativeOptions(unittest.TestCase):
    def test_options_expose_separate_stock_rates_uoms_and_combined_total(self):
        from jarz_pos.api import inventory_count, manufacturing

        row = {
            "item_code": "ALDIA",
            "item_name": "Aldia blueberry",
            "stock_uom": "Kg",
            "uom": "Kg",
            "required_qty": 4.0,
            "source_warehouse": "Raw Material - J",
        }

        def validate(_original, selected, _company, _warehouse):
            return _item(selected, item_name={"ALDIA": "Aldia", "PURATOS": "Puratos"}[selected])

        with patch.object(manufacturing, "_ensure_production_view_access"), patch.object(
            manufacturing, "_get_bom_company", return_value="Jarz"
        ), patch.object(
            manufacturing, "_get_required_material_rows", return_value=[row]
        ), patch.object(
            manufacturing, "_direct_two_way_alternative_codes", return_value=["PURATOS"]
        ), patch.object(
            manufacturing, "_validate_material_selection", side_effect=validate
        ), patch.object(
            manufacturing, "_get_live_stock_qty", side_effect=[3.547, 25.0]
        ), patch.object(
            manufacturing, "_resolve_valuation_rate", side_effect=[80.0, 100.0]
        ), patch.object(
            inventory_count,
            "_get_uom_conversions",
            side_effect=[
                [{"uom": "Kg", "conversion_factor": 1}, {"uom": "Box", "conversion_factor": 2.7}],
                [{"uom": "Kg", "conversion_factor": 1}, {"uom": "Box", "conversion_factor": 5}],
            ],
        ):
            result = manufacturing.get_material_options("BOM-BLUEBERRY", 1)

        component = result["components"][0]
        self.assertEqual(28.547, component["combined_available_qty"])
        self.assertEqual(["ALDIA", "PURATOS"], [o["item_code"] for o in component["options"]])
        self.assertEqual([2.7, 5], [o["uoms"][1]["conversion_factor"] for o in component["options"]])

    def test_combined_available_qty_does_not_let_a_negative_bin_hide_usable_stock(self):
        from jarz_pos.api import inventory_count, manufacturing

        row = {
            "item_code": "ALDIA",
            "item_name": "Aldia blueberry",
            "stock_uom": "Kg",
            "uom": "Kg",
            "required_qty": 4.0,
            "source_warehouse": "Raw Material - J",
        }
        with patch.object(manufacturing, "_ensure_production_view_access"), patch.object(
            manufacturing, "_get_bom_company", return_value="Jarz"
        ), patch.object(
            manufacturing, "_get_required_material_rows", return_value=[row]
        ), patch.object(
            manufacturing, "_direct_two_way_alternative_codes", return_value=["PURATOS"]
        ), patch.object(
            manufacturing,
            "_validate_material_selection",
            side_effect=lambda _original, selected, _company, _warehouse: _item(selected),
        ), patch.object(
            manufacturing, "_get_live_stock_qty", side_effect=[-2.0, 5.0]
        ), patch.object(
            manufacturing, "_resolve_valuation_rate", return_value=1.0
        ), patch.object(inventory_count, "_get_uom_conversions", return_value=[]):
            result = manufacturing.get_material_options("BOM-BLUEBERRY", 1)

        component = result["components"][0]
        self.assertEqual([-2.0, 5.0], [o["available_qty"] for o in component["options"]])
        self.assertEqual(5.0, component["combined_available_qty"])


class TestIngredientAlternativeBasket(unittest.TestCase):
    def test_basket_rollup_passes_each_lines_selection_to_the_bom_reader(self):
        from jarz_pos.services import production_planning

        getter = MagicMock(return_value=[])
        line = {
            "item_code": "CAKE",
            "bom_name": "BOM-CAKE",
            "item_qty": 10,
            "material_selections": {"ALDIA": "PURATOS"},
        }
        with patch.object(
            production_planning, "_resolve_required_material_rows", return_value=getter
        ), patch.object(
            production_planning, "_resolve_bom_company", return_value="Jarz"
        ), patch.object(
            production_planning, "_resolve_bin_stock_map", return_value={}
        ), patch.object(production_planning, "attach_stock_elsewhere"):
            production_planning.build_basket_rollup([line], "Jarz")

        self.assertEqual(
            {"ALDIA": "PURATOS"}, getter.call_args.kwargs["material_selections"]
        )


if __name__ == "__main__":
    unittest.main()

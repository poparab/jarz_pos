import unittest
from types import SimpleNamespace
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
    def test_get_materials_report_buckets_sub_assemblies(self):
        """The bucket was empty on every site: the filter said "Sub Assembly",
        the group is "Sub Assemblies". Nothing covered it, so nothing caught it."""
        from jarz_pos.api import reports

        tree = {
            "Raw Material": (10, 11),
            "Packaging": (12, 13),
            "Labels": (14, 15),
            "Sub Assemblies": (16, 17),
            "Consumable": (20, 21),
        }
        items = [
            {"item_code": "RM-1", "item_name": "flour", "item_group": "Raw Material", "stock_uom": "Kg"},
            {"item_code": "LB-1", "item_name": "Lotus label", "item_group": "Labels", "stock_uom": "Nos"},
            {"item_code": "SA-1", "item_name": "Cheesecake Mix", "item_group": "Sub Assemblies", "stock_uom": "Kg"},
            {"item_code": "CN-1", "item_name": "Tissue", "item_group": "Consumable", "stock_uom": "Nos"},
        ]
        bins = [
            {"item_code": "RM-1", "warehouse": "WH-A", "actual_qty": 5},
            {"item_code": "LB-1", "warehouse": "WH-A", "actual_qty": 200},
            {"item_code": "SA-1", "warehouse": "WH-A", "actual_qty": 12},
            {"item_code": "CN-1", "warehouse": "WH-A", "actual_qty": 8},
        ]

        def fake_get_value(doctype, name, fields, as_dict=False):
            self.assertEqual("Item Group", doctype)
            bounds = tree.get(name)
            return {"lft": bounds[0], "rgt": bounds[1]} if bounds else None

        def fake_get_all(doctype, **kwargs):
            if doctype == "Item Group":
                lft = kwargs["filters"]["lft"][1]
                return [{"name": n} for n, b in tree.items() if b[0] == lft]
            if doctype == "Item":
                return items
            if doctype == "Bin":
                return bins
            self.fail(f"Unexpected doctype lookup: {doctype}")

        # frappe.db is an unbound Local proxy without a site connection, so it
        # has to be replaced wholesale — patching frappe.db.get_value raises
        # "object is not bound" before the test body ever runs.
        with patch("jarz_pos.api.reports._ensure_materials_report_access"), patch(
            "jarz_pos.api.reports.frappe.db", SimpleNamespace(get_value=fake_get_value)
        ), patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all):
            result = reports.get_materials_report()

        self.assertEqual(["Cheesecake Mix"], [r["item_name"] for r in result["sub_assemblies"]])
        self.assertEqual(["Tissue"], [r["item_name"] for r in result["consumables"]])
        # Labels split out of Raw Material still count as materials.
        self.assertEqual({"flour", "Lotus label"}, {r["item_name"] for r in result["raw_materials"]})

    def test_get_materials_report_skips_groups_absent_on_this_site(self):
        """Packaging and Labels do not exist until the reshelving runs; the
        report must degrade to the groups that are actually there."""
        from jarz_pos.api import reports

        with patch(
            "jarz_pos.api.reports.frappe.db",
            SimpleNamespace(get_value=lambda *a, **k: None),
        ):
            self.assertEqual([], reports._expand_item_groups(("Packaging", "Labels")))

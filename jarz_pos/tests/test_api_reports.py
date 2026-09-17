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

            if doctype == "Company":
                return []

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
            if doctype == "Company":
                return []
            self.fail(f"Unexpected doctype lookup: {doctype}")

        with patch("jarz_pos.api.reports._ensure_jarz_manager"), patch(
            "jarz_pos.api.reports.frappe.get_all",
            side_effect=fake_get_all,
        ):
            result = reports.get_final_products_report()

        self.assertEqual(1, len(result["groups"]))
        self.assertEqual("Medium", result["groups"][0]["group_name"])
        self.assertEqual("Medium", result["groups"][0]["items"][0]["item_group"])

    def test_get_final_products_report_columns_do_not_move_with_the_stock(self):
        """Columns used to be per-group and derived from the Bin rows that
        happened to exist, so on production the Large table had no "Finished
        Goods" column (no Large size was in the store that day) while the Medium
        table grew a "Raw Material" one off a single stray Bin row."""
        from jarz_pos.api import reports

        items = [
            {"item_code": "ITEM-M", "item_name": "Lotus Medium", "item_group": "Medium", "stock_uom": "Nos"},
            {"item_code": "ITEM-M2", "item_name": "Carrot cake Medium", "item_group": "Medium", "stock_uom": "Nos"},
            {"item_code": "ITEM-L", "item_name": "Lotus Large", "item_group": "Large", "stock_uom": "Nos"},
        ]
        # Only a Medium sits in the finished-goods store; only a Large sits in
        # the branch. Neither table may lose the other's column.
        bins = [
            {"item_code": "ITEM-M", "warehouse": "Finished Goods - J", "actual_qty": 120},
            {"item_code": "ITEM-L", "warehouse": "Dokki - J", "actual_qty": 18},
        ]

        def fake_get_all(doctype, **kwargs):
            if doctype == "Item":
                return items
            if doctype == "Bin":
                # Negatives must be fetched too — see the oversold-branch test.
                self.assertEqual(["!=", 0], kwargs["filters"]["actual_qty"])
                return bins
            if doctype == "Company":
                return [{"default_fg_warehouse": "Finished Goods - J"}]
            self.fail(f"Unexpected doctype lookup: {doctype}")

        db = SimpleNamespace(get_value=lambda *a, **k: 0)
        with patch("jarz_pos.api.reports._ensure_jarz_manager"), patch(
            "jarz_pos.api.reports.frappe.db", db
        ), patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all):
            result = reports.get_final_products_report()

        medium, large = result["groups"]
        expected = ["Dokki - J", "Finished Goods - J"]
        self.assertEqual(expected, medium["warehouses"])
        self.assertEqual(expected, large["warehouses"], "Large lost the finished-goods column")

        # A size that has run out everywhere is the most useful line on a stock
        # count; it used to be dropped entirely.
        rows = {r["item_name"]: r for r in medium["items"]}
        self.assertIn("Carrot cake Medium", rows)
        self.assertEqual(0, rows["Carrot cake Medium"]["total_qty"])
        self.assertEqual({}, rows["Carrot cake Medium"]["warehouse_qty"])

    def test_get_final_products_report_counts_oversold_branches(self):
        """``actual_qty > 0`` dropped a negative bin, so the report read HIGH:
        Chocolate Hazelnut Medium showed 74 on production while the ledger said
        70, because Dokki's -4 was never fetched."""
        from jarz_pos.api import reports

        items = [
            {"item_code": "ITEM-M", "item_name": "Chocolate Hazelnut Medium", "item_group": "Medium", "stock_uom": "Nos"},
        ]
        bins = [
            {"item_code": "ITEM-M", "warehouse": "Finished Goods - J", "actual_qty": 62},
            {"item_code": "ITEM-M", "warehouse": "6th of october - J", "actual_qty": 10},
            {"item_code": "ITEM-M", "warehouse": "Nasr city - J", "actual_qty": 2},
            {"item_code": "ITEM-M", "warehouse": "Dokki - J", "actual_qty": -4},
        ]

        def fake_get_all(doctype, **kwargs):
            if doctype == "Item":
                return items
            if doctype == "Bin":
                return bins
            self.fail(f"Unexpected doctype lookup: {doctype}")

        with patch("jarz_pos.api.reports._ensure_jarz_manager"), patch(
            "jarz_pos.api.reports._finished_goods_warehouses", return_value=[]
        ), patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all):
            result = reports.get_final_products_report()

        row = result["groups"][0]["items"][0]
        self.assertEqual(70.0, row["total_qty"])
        self.assertEqual(-4.0, row["warehouse_qty"]["Dokki - J"])

    def test_finished_goods_warehouses_reads_the_company_not_the_settings(self):
        """v16 moved ``default_fg_warehouse`` from Manufacturing Settings onto
        the Company. Production still answers the old address off a stale
        ``tabSingles`` row, so asking Manufacturing Settings looks fine there and
        raises ``Field ... does not exist`` on staging."""
        from jarz_pos.api import reports

        def fake_get_all(doctype, **kwargs):
            self.assertEqual("Company", doctype)
            return [{"default_fg_warehouse": "Finished Goods - J"}]

        with patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all), patch(
            "jarz_pos.api.reports.frappe.db", SimpleNamespace(get_value=lambda *a, **k: 0)
        ):
            self.assertEqual(["Finished Goods - J"], reports._finished_goods_warehouses())

    def test_finished_goods_warehouses_degrades_without_a_setting(self):
        """Site-less harness and a fresh site both land here; neither may raise."""
        from jarz_pos.api import reports

        with patch(
            "jarz_pos.api.reports.frappe.get_all",
            side_effect=lambda *a, **k: [{"default_fg_warehouse": None}],
        ), patch(
            "jarz_pos.api.reports.frappe.db",
            SimpleNamespace(get_value=lambda *a, **k: None),
        ):
            self.assertEqual([], reports._finished_goods_warehouses())

        # A group warehouse holds nothing and must not become a column.
        with patch(
            "jarz_pos.api.reports.frappe.get_all",
            side_effect=lambda *a, **k: [{"default_fg_warehouse": "All Warehouses - J"}],
        ), patch(
            "jarz_pos.api.reports.frappe.db",
            SimpleNamespace(get_value=lambda *a, **k: 1),
        ):
            self.assertEqual([], reports._finished_goods_warehouses())

        # The field itself can be gone on an older or newer schema.
        with patch(
            "jarz_pos.api.reports.frappe.get_all",
            side_effect=Exception("Field default_fg_warehouse does not exist"),
        ):
            self.assertEqual([], reports._finished_goods_warehouses())

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

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestReportsAPI(unittest.TestCase):
	def setUp(self):
		# The days-of-cover sales read is SQL; keep these stock tests hermetic.
		# It degrades to {} on its own too, but must not hit the database here.
		patcher = patch("jarz_pos.api.reports._jar_sales_by_warehouse", return_value={})
		patcher.start()
		self.addCleanup(patcher.stop)

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
					["in", ["Large", "Medium", "Meduim", "Small"]],
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

		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch(
				"jarz_pos.api.reports.frappe.get_all",
				side_effect=fake_get_all,
			),
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

		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch(
				"jarz_pos.api.reports.frappe.get_all",
				side_effect=fake_get_all,
			),
		):
			result = reports.get_final_products_report()

		self.assertEqual(1, len(result["groups"]))
		self.assertEqual("Medium", result["groups"][0]["group_name"])
		self.assertEqual("Medium", result["groups"][0]["items"][0]["item_group"])

	def test_get_final_products_report_lists_small_last(self):
		from jarz_pos.api import reports

		items = [
			{"item_code": "ITEM-L", "item_name": "Molten Large", "item_group": "Large", "stock_uom": "Nos"},
			{"item_code": "ITEM-M", "item_name": "Molten Medium", "item_group": "Medium", "stock_uom": "Nos"},
			{"item_code": "ITEM-S", "item_name": "Molten Small", "item_group": "Small", "stock_uom": "Nos"},
		]
		bins = [{"item_code": "ITEM-S", "warehouse": "WH-A", "actual_qty": 9}]

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				self.assertIn("Small", kwargs["filters"]["item_group"][1])
				return items
			if doctype == "Bin":
				return bins
			if doctype == "Company":
				return []
			self.fail(f"Unexpected doctype lookup: {doctype}")

		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch(
				"jarz_pos.api.reports.frappe.get_all",
				side_effect=fake_get_all,
			),
		):
			result = reports.get_final_products_report()

		self.assertEqual(["Medium", "Large", "Small"], [group["group_name"] for group in result["groups"]])
		small = result["groups"][-1]
		self.assertEqual("Molten Small", small["items"][0]["item_name"])
		self.assertEqual(9.0, small["items"][0]["total_qty"])
		# Every size carries the same warehouse columns.
		self.assertEqual({tuple(g["warehouses"]) for g in result["groups"]}, {tuple(small["warehouses"])})

	def test_get_final_products_report_columns_do_not_move_with_the_stock(self):
		"""Columns used to be per-group and derived from the Bin rows that
		happened to exist, so on production the Large table had no "Finished
		Goods" column (no Large size was in the store that day) while the Medium
		table grew a "Raw Material" one off a single stray Bin row."""
		from jarz_pos.api import reports

		items = [
			{"item_code": "ITEM-M", "item_name": "Lotus Medium", "item_group": "Medium", "stock_uom": "Nos"},
			{
				"item_code": "ITEM-M2",
				"item_name": "Carrot cake Medium",
				"item_group": "Medium",
				"stock_uom": "Nos",
			},
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
		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch("jarz_pos.api.reports.frappe.db", db),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
		):
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
			{
				"item_code": "ITEM-M",
				"item_name": "Chocolate Hazelnut Medium",
				"item_group": "Medium",
				"stock_uom": "Nos",
			},
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

		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch("jarz_pos.api.reports._finished_goods_warehouses", return_value=[]),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
		):
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

		with (
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
			patch("jarz_pos.api.reports.frappe.db", SimpleNamespace(get_value=lambda *a, **k: 0)),
		):
			self.assertEqual(["Finished Goods - J"], reports._finished_goods_warehouses())

	def test_finished_goods_warehouses_degrades_without_a_setting(self):
		"""Site-less harness and a fresh site both land here; neither may raise."""
		from jarz_pos.api import reports

		with (
			patch(
				"jarz_pos.api.reports.frappe.get_all",
				side_effect=lambda *a, **k: [{"default_fg_warehouse": None}],
			),
			patch(
				"jarz_pos.api.reports.frappe.db",
				SimpleNamespace(get_value=lambda *a, **k: None),
			),
		):
			self.assertEqual([], reports._finished_goods_warehouses())

		# A group warehouse holds nothing and must not become a column.
		with (
			patch(
				"jarz_pos.api.reports.frappe.get_all",
				side_effect=lambda *a, **k: [{"default_fg_warehouse": "All Warehouses - J"}],
			),
			patch(
				"jarz_pos.api.reports.frappe.db",
				SimpleNamespace(get_value=lambda *a, **k: 1),
			),
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
			{
				"item_code": "SA-1",
				"item_name": "Cheesecake Mix",
				"item_group": "Sub Assemblies",
				"stock_uom": "Kg",
			},
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
		with (
			patch("jarz_pos.api.reports._ensure_materials_report_access"),
			patch("jarz_pos.api.reports.frappe.db", SimpleNamespace(get_value=fake_get_value)),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
		):
			result = reports.get_materials_report()

		self.assertEqual(["Cheesecake Mix"], [r["item_name"] for r in result["sub_assemblies"]])
		self.assertEqual(["Tissue"], [r["item_name"] for r in result["consumables"]])
		# Labels split out of Raw Material still count as materials.
		self.assertEqual({"flour", "Lotus label"}, {r["item_name"] for r in result["raw_materials"]})

	def test_get_materials_report_counts_oversold_branches(self):
		"""The two halves of one screen must not disagree.

		get_final_products_report stopped dropping negative bins; this one did
		not, so the SAME item in the SAME condition read differently depending
		on which tab you opened -- and the materials total read HIGH by exactly
		the oversold amount.
		"""
		from jarz_pos.api import reports

		tree = {"Raw Material": (10, 11)}
		items = [
			{"item_code": "RM-1", "item_name": "flour", "item_group": "Raw Material", "stock_uom": "Kg"},
		]
		bins = [
			{"item_code": "RM-1", "warehouse": "Raw Material - J", "actual_qty": 70},
			{"item_code": "RM-1", "warehouse": "Dokki - J", "actual_qty": -4},
		]
		captured = {}

		def fake_get_value(doctype, name, fields, as_dict=False):
			bounds = tree.get(name)
			return {"lft": bounds[0], "rgt": bounds[1]} if bounds else None

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item Group":
				lft = kwargs["filters"]["lft"][1]
				return [{"name": n} for n, b in tree.items() if b[0] == lft]
			if doctype == "Item":
				return items
			if doctype == "Bin":
				captured["bin_filters"] = kwargs["filters"]
				return bins
			self.fail(f"Unexpected doctype lookup: {doctype}")

		with (
			patch("jarz_pos.api.reports._ensure_materials_report_access"),
			patch("jarz_pos.api.reports.frappe.db", SimpleNamespace(get_value=fake_get_value)),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
		):
			result = reports.get_materials_report()

		# The same filter the finished-goods report uses.
		self.assertEqual(["!=", 0], captured["bin_filters"]["actual_qty"])

		row = result["raw_materials"][0]
		self.assertEqual(66, row["total_qty"])
		self.assertEqual(-4, row["warehouse_qty"]["Dokki - J"])

	def test_get_materials_report_skips_groups_absent_on_this_site(self):
		"""Packaging and Labels do not exist until the reshelving runs; the
		report must degrade to the groups that are actually there."""
		from jarz_pos.api import reports

		with patch(
			"jarz_pos.api.reports.frappe.db",
			SimpleNamespace(get_value=lambda *a, **k: None),
		):
			self.assertEqual([], reports._expand_item_groups(("Packaging", "Labels")))

	def test_final_products_carry_stock_value_and_totals(self):
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
			{"item_code": "ITEM-M", "warehouse": "WH-A", "actual_qty": 4, "stock_value": 200.0},
			{"item_code": "ITEM-M", "warehouse": "WH-B", "actual_qty": 1, "stock_value": 50.0},
			{"item_code": "ITEM-L", "warehouse": "WH-A", "actual_qty": 2, "stock_value": 180.0},
		]

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				return items
			if doctype == "Bin":
				self.assertIn("stock_value", kwargs["fields"])
				return bins
			if doctype == "Company":
				return []
			self.fail(f"Unexpected doctype lookup: {doctype}")

		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
		):
			result = reports.get_final_products_report()

		medium = result["groups"][0]
		self.assertEqual(250.0, medium["items"][0]["stock_value"])
		self.assertEqual({"qty": 5.0, "stock_value": 250.0}, medium["totals"])
		self.assertEqual({"qty": 7.0, "stock_value": 430.0}, result["totals"])
		self.assertEqual({"branch": None, "warehouse": None}, result["filters"])

	def test_final_products_branch_shows_only_that_warehouse(self):
		from jarz_pos.api import reports

		items = [
			{
				"item_code": "ITEM-M",
				"item_name": "Blueberry Medium",
				"item_group": "Medium",
				"stock_uom": "Nos",
			},
		]
		captured = {}

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				return items
			if doctype == "Bin":
				captured["filters"] = kwargs["filters"]
				return [
					{
						"item_code": "ITEM-M",
						"warehouse": "Stores - Dokki",
						"actual_qty": 3,
						"stock_value": 90.0,
					}
				]
			self.fail(f"Unexpected doctype lookup: {doctype}")

		db = SimpleNamespace(get_value=lambda doctype, name, field: "Stores - Dokki")
		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch("jarz_pos.api.reports.frappe.db", db),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
		):
			result = reports.get_final_products_report(branch="Dokki")

		self.assertEqual("Stores - Dokki", captured["filters"]["warehouse"])
		self.assertEqual(["Stores - Dokki"], result["groups"][0]["warehouses"])
		self.assertEqual({"qty": 3.0, "stock_value": 90.0}, result["totals"])
		self.assertEqual("Dokki", result["filters"]["branch"])

	def test_materials_report_carries_bucket_and_grand_totals(self):
		from jarz_pos.api import reports

		tree = {"Raw Material": (10, 11)}
		items = [{"item_code": "RM-1", "item_name": "flour", "item_group": "Raw Material", "stock_uom": "Kg"}]

		def fake_get_value(doctype, name, fields, as_dict=False):
			bounds = tree.get(name)
			return {"lft": bounds[0], "rgt": bounds[1]} if bounds else None

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item Group":
				lft = kwargs["filters"]["lft"][1]
				return [{"name": n} for n, b in tree.items() if b[0] == lft]
			if doctype == "Item":
				return items
			if doctype == "Bin":
				return [
					{
						"item_code": "RM-1",
						"warehouse": "Raw Material - J",
						"actual_qty": 10,
						"stock_value": 400.0,
					}
				]
			self.fail(f"Unexpected doctype lookup: {doctype}")

		with (
			patch("jarz_pos.api.reports._ensure_materials_report_access"),
			patch("jarz_pos.api.reports.frappe.db", SimpleNamespace(get_value=fake_get_value)),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
		):
			result = reports.get_materials_report()

		self.assertEqual(400.0, result["raw_materials"][0]["stock_value"])
		self.assertEqual({"qty": 10.0, "stock_value": 400.0}, result["bucket_totals"]["raw_materials"])
		self.assertEqual({"qty": 0.0, "stock_value": 0.0}, result["bucket_totals"]["consumables"])
		self.assertEqual({"qty": 10.0, "stock_value": 400.0}, result["totals"])


FG = "Finished Goods - J"
DOKKI = "Dokki - J"
NASR = "Nasr city - J"
ZAMALEK = "Zamalek - J"


class TestFinalProductsDaysOfCover(unittest.TestCase):
	"""Days-of-cover on the Final Products report (14-day net sales rate)."""

	def _run(self, items, bins, sales, fg=(FG,), branch=None, db=None):
		from jarz_pos.api import reports

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				return items
			if doctype == "Bin":
				return bins
			self.fail(f"Unexpected doctype lookup: {doctype}")

		sales_mock = MagicMock(return_value=sales)
		patches = [
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch("jarz_pos.api.reports._finished_goods_warehouses", return_value=list(fg)),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
			patch("jarz_pos.api.reports._jar_sales_by_warehouse", sales_mock),
		]
		if db is not None:
			patches.append(patch("jarz_pos.api.reports.frappe.db", db))
		for p in patches:
			p.start()
		try:
			result = reports.get_final_products_report(branch=branch)
		finally:
			for p in reversed(patches):
				p.stop()
		return result, sales_mock

	@staticmethod
	def _item(code="ITEM-M", name="Lotus Medium", group="Medium"):
		return {"item_code": code, "item_name": name, "item_group": group, "stock_uom": "Nos"}

	@staticmethod
	def _bin(warehouse, qty, code="ITEM-M"):
		return {"item_code": code, "warehouse": warehouse, "actual_qty": qty, "stock_value": 0.0}

	def test_branch_column_uses_its_own_rate(self):
		# 28 sold at Dokki over 14 days = 2/day; 10 on the shelf = 5 days.
		result, sales_mock = self._run(
			[self._item()],
			[self._bin(DOKKI, 10)],
			{(DOKKI, "ITEM-M"): 28.0},
		)
		row = result["groups"][0]["items"][0]
		self.assertEqual(2.0, row["sells_per_day"][DOKKI])
		self.assertEqual(5.0, row["days_cover"][DOKKI])
		# Company-wide read: no warehouse filter, 14-day window.
		args = sales_mock.call_args[0]
		self.assertIsNone(args[1])
		self.assertEqual(14, args[2])
		self.assertEqual({"sales_days": 14, "red_below": 3, "green_above": 7}, result["cover"])

	def test_finished_goods_column_uses_the_company_wide_rate(self):
		# Dokki 2/day + Nasr city 1/day (not a column: no bin there) = 3/day.
		result, _ = self._run(
			[self._item()],
			[self._bin(FG, 60), self._bin(DOKKI, 4)],
			{(DOKKI, "ITEM-M"): 28.0, (NASR, "ITEM-M"): 14.0},
		)
		group = result["groups"][0]
		self.assertEqual([DOKKI, FG], group["warehouses"])
		row = group["items"][0]
		self.assertEqual(20.0, row["days_cover"][FG])
		self.assertEqual(2.0, row["days_cover"][DOKKI])
		# One sells_per_day entry per column, 0.0 where that column sold none;
		# demand outside the columns is not listed but is in the total.
		self.assertEqual({DOKKI: 2.0, FG: 0.0}, row["sells_per_day"])
		self.assertEqual(3.0, row["total_sells_per_day"])
		# 64 jars company-wide / 3 per day.
		self.assertEqual(21.3, row["total_days_cover"])

	def test_column_with_no_sales_has_no_cover(self):
		result, _ = self._run(
			[self._item(), self._item("ITEM-M2", "Carrot cake Medium")],
			[self._bin(ZAMALEK, 5), self._bin(DOKKI, 3, code="ITEM-M2")],
			{(DOKKI, "ITEM-M2"): 14.0},
		)
		rows = {r["item_code"]: r for r in result["groups"][0]["items"]}
		# Every column has an entry, even where the item is absent.
		self.assertEqual({DOKKI, FG, ZAMALEK}, set(rows["ITEM-M"]["days_cover"]))
		self.assertIsNone(rows["ITEM-M"]["days_cover"][ZAMALEK])
		self.assertIsNone(rows["ITEM-M"]["days_cover"][DOKKI])
		# The FG store with no demand anywhere has nothing to measure against.
		self.assertIsNone(rows["ITEM-M"]["days_cover"][FG])
		self.assertEqual(0.0, rows["ITEM-M"]["total_sells_per_day"])
		self.assertIsNone(rows["ITEM-M"]["total_days_cover"])
		self.assertIsNone(rows["ITEM-M2"]["days_cover"][ZAMALEK])

	def test_empty_or_oversold_shelf_with_sales_covers_zero_days(self):
		result, _ = self._run(
			[self._item(), self._item("ITEM-M2", "Carrot cake Medium")],
			[self._bin(DOKKI, -4), self._bin(NASR, 6, code="ITEM-M2")],
			{(DOKKI, "ITEM-M"): 14.0, (NASR, "ITEM-M"): 7.0},
		)
		row = {r["item_code"]: r for r in result["groups"][0]["items"]}["ITEM-M"]
		self.assertEqual(0.0, row["days_cover"][DOKKI])  # -4 on hand, never negative
		self.assertEqual(0.0, row["days_cover"][NASR])  # no bin at all, but it sells
		self.assertEqual(0.0, row["days_cover"][FG])  # store empty, company sells 1.5/day
		self.assertEqual(1.5, row["total_sells_per_day"])
		self.assertEqual(0.0, row["total_days_cover"])

	def test_branch_view_reads_that_warehouse_only(self):
		result, sales_mock = self._run(
			[self._item()],
			[self._bin("Stores - Dokki", 21)],
			{("Stores - Dokki", "ITEM-M"): 42.0},
			fg=(),
			branch="Dokki",
			db=SimpleNamespace(get_value=lambda *a, **k: "Stores - Dokki"),
		)
		self.assertEqual("Stores - Dokki", sales_mock.call_args[0][1])
		row = result["groups"][0]["items"][0]
		self.assertEqual({"Stores - Dokki": 3.0}, row["sells_per_day"])
		self.assertEqual({"Stores - Dokki": 7.0}, row["days_cover"])
		self.assertEqual(3.0, row["total_sells_per_day"])
		self.assertEqual(7.0, row["total_days_cover"])

	def test_empty_report_still_carries_the_cover_block(self):
		result, _ = self._run([], [], {})
		self.assertEqual([], result["groups"])
		self.assertEqual({"sales_days": 14, "red_below": 3, "green_above": 7}, result["cover"])

	def test_failed_sales_read_leaves_every_cover_none(self):
		"""The stock report must never break because the sales read did."""
		from jarz_pos.api import reports

		items = [self._item()]
		bins = [self._bin(FG, 60), self._bin(DOKKI, 4)]

		def fake_get_all(doctype, **kwargs):
			if doctype == "Item":
				return items
			if doctype == "Bin":
				return bins
			self.fail(f"Unexpected doctype lookup: {doctype}")

		def broken_sql(*a, **k):
			raise Exception("Lost connection to MySQL server")

		logged = []
		with (
			patch("jarz_pos.api.reports._ensure_jarz_manager"),
			patch("jarz_pos.api.reports._finished_goods_warehouses", return_value=[FG]),
			patch("jarz_pos.api.reports.frappe.get_all", side_effect=fake_get_all),
			patch("jarz_pos.api.reports.frappe.db", SimpleNamespace(sql=broken_sql)),
			patch("jarz_pos.api.reports.nowdate", return_value="2026-10-20"),
			patch(
				"jarz_pos.api.reports._log_degraded_read",
				side_effect=lambda title, message: logged.append(title),
			),
		):
			result = reports.get_final_products_report()

		row = result["groups"][0]["items"][0]
		self.assertEqual(64.0, row["total_qty"])
		self.assertEqual({DOKKI: None, FG: None}, row["days_cover"])
		self.assertEqual({DOKKI: 0.0, FG: 0.0}, row["sells_per_day"])
		self.assertEqual(0.0, row["total_sells_per_day"])
		self.assertIsNone(row["total_days_cover"])
		self.assertEqual(1, len(logged))

	def test_sales_helper_survives_a_failing_logger(self):
		"""``frappe.log_error`` can itself raise; the helper must still return {}."""
		from jarz_pos.api import reports

		def broken(*a, **k):
			raise Exception("boom")

		with (
			patch("jarz_pos.api.reports.frappe.db", SimpleNamespace(sql=broken)),
			patch("jarz_pos.api.reports.nowdate", return_value="2026-10-20"),
			patch("jarz_pos.api.reports.frappe.log_error", side_effect=broken),
			patch("jarz_pos.api.reports.frappe.logger", side_effect=broken),
			patch("jarz_pos.api.reports.frappe.get_traceback", side_effect=broken),
		):
			self.assertEqual({}, reports._jar_sales_by_warehouse(["ITEM-M"], None, 14))

	def test_sales_helper_query_shape(self):
		from jarz_pos.api import reports

		captured = []

		def fake_sql(query, params, as_dict=False):
			captured.append((query, params))
			return [{"warehouse": DOKKI, "item_code": "ITEM-M", "qty": 9}]

		with (
			patch("jarz_pos.api.reports.frappe.db", SimpleNamespace(sql=fake_sql)),
			patch("jarz_pos.api.reports.nowdate", return_value="2026-10-20"),
		):
			company = reports._jar_sales_by_warehouse(["ITEM-M"], None, 14)
			branch = reports._jar_sales_by_warehouse(["ITEM-M"], DOKKI, 14)

		self.assertEqual({(DOKKI, "ITEM-M"): 9.0}, company)
		self.assertEqual({(DOKKI, "ITEM-M"): 9.0}, branch)
		company_query, company_params = captured[0]
		branch_query, branch_params = captured[1]
		self.assertIn("si.docstatus = 1", company_query)
		self.assertNotIn("%(warehouse)s", company_query)
		self.assertNotIn("warehouse", company_params)
		self.assertIn("sii.warehouse = %(warehouse)s", branch_query)
		self.assertEqual(DOKKI, branch_params["warehouse"])
		# Whole completed days ending yesterday.
		self.assertEqual("2026-10-19", str(company_params["to_date"]))
		self.assertEqual("2026-10-06", str(company_params["from_date"]))
		# No items: no query at all.
		self.assertEqual({}, reports._jar_sales_by_warehouse([], None, 14))
		self.assertEqual(2, len(captured))

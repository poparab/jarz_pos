"""Product analytics: Small (147 ml) jars are counted, not skipped; cost of goods
comes from posted cost first and the BOM only as a fallback.

The report aggregates in SQL, so these tests feed the two aggregate queries
(per product, per territory × day). Every read is patched; no site is needed.
"""

import unittest
from unittest.mock import MagicMock, patch


def _product(
	item_code, item_group, qty, amount, *, cost=0.0, actual_cost=None, bom_unit=0.0, is_header=0, in_bundle=0
):
	"""One row of the per-product aggregate.

	``actual_cost`` None means no line had posted cost; then the cost is the BOM
	fallback (``bom_unit × qty``) when there is a BOM.
	"""
	has_actual = actual_cost is not None
	has_bom = not has_actual and bom_unit > 0
	if has_actual:
		cost = actual_cost
	elif has_bom:
		cost = bom_unit * qty
	return {
		"item_code": item_code,
		"item_name": item_code,
		"item_group": item_group,
		"is_header": is_header,
		"in_bundle": in_bundle,
		"qty": qty,
		"amount": amount,
		"cost": cost,
		"actual_cost": actual_cost or 0.0,
		"actual_qty": qty if has_actual else 0.0,
		"actual_amount": amount if has_actual else 0.0,
		"bom_qty": qty if has_bom else 0.0,
		"bom_amount": amount if has_bom else 0.0,
		"bom_unit": bom_unit,
	}


def _territory(orders, revenue, cost, territory="EGNASRCITY", day="2026-09-20"):
	return {"territory": territory, "posting_date": day, "orders": orders, "revenue": revenue, "cost": cost}


class TestProductAnalytics(unittest.TestCase):
	def _run(self, product_rows, territory_rows, branch=None, bundles=None):
		from jarz_pos.api import product_analytics as pa

		fake = MagicMock()
		seen = []

		def _sql(query, params=None, as_dict=False, **kwargs):
			seen.append((query, params))
			if "GROUP BY l.item_code" in query:
				return product_rows
			if "GROUP BY l.territory" in query:
				return territory_rows
			return []

		fake.db.sql.side_effect = _sql
		with (
			patch.object(pa, "frappe", fake),
			patch.object(pa, "_ensure_jarz_manager"),
			patch.object(pa, "_get_bundle_item_codes", return_value=bundles or set()),
			patch.object(pa, "get_area_label", side_effect=lambda t: t),
		):
			result = pa.get_product_analytics("2026-09-01", "2026-09-30", branch=branch)
		return result, seen

	def test_small_lines_are_counted_under_their_own_type(self):
		result, _ = self._run(
			[
				_product("Molten Small", "Small", 3, 90.0, bom_unit=10.0),
				_product("Molten Medium", "Medium", 2, 240.0),
				_product("Molten Large", "Large", 1, 160.0),
			],
			[_territory(3, 490.0, 30.0)],
		)

		by_type = {t["type"]: t for t in result["by_product_type"]}
		self.assertEqual(
			["Bundle", "Small", "Medium", "Large"], [t["type"] for t in result["by_product_type"]]
		)
		self.assertEqual(3, by_type["Small"]["units"])
		self.assertEqual(90.0, by_type["Small"]["revenue"])
		self.assertEqual(30.0, by_type["Small"]["cost"])
		self.assertEqual(2, by_type["Medium"]["units"])
		self.assertEqual(1, by_type["Large"]["units"])

		products = {p["item_code"]: p for p in result["top_products"]}
		self.assertEqual("Small", products["Molten Small"]["type"])
		self.assertEqual("bom", products["Molten Small"]["cost_source"])
		self.assertEqual(10.0, products["Molten Small"]["bom_cost_per_unit"])
		self.assertEqual("none", products["Molten Large"]["cost_source"])
		self.assertEqual(3, result["summary"]["total_orders"])
		self.assertEqual(490.0, result["summary"]["total_revenue"])

	def test_small_only_period_is_not_empty(self):
		result, _ = self._run([_product("Molten Small", "Small", 5, 150.0)], [_territory(1, 150.0, 0.0)])
		self.assertEqual(1, result["summary"]["total_orders"])
		self.assertEqual(150.0, result["summary"]["total_revenue"])

	def test_posted_cost_wins_over_bom_and_sets_coverage(self):
		result, _ = self._run(
			[
				_product("Molten Small", "Small", 4, 120.0, actual_cost=36.0, bom_unit=10.0),
				_product("Molten Large", "Large", 1, 80.0, bom_unit=50.0),
			],
			[_territory(2, 200.0, 86.0)],
		)
		products = {p["item_code"]: p for p in result["top_products"]}
		small = products["Molten Small"]
		self.assertEqual("actual", small["cost_source"])
		self.assertEqual(36.0, small["total_cost"])
		self.assertEqual(9.0, small["actual_cost_per_unit"])
		self.assertEqual(100.0, small["cost_coverage_pct"])
		self.assertEqual("bom", products["Molten Large"]["cost_source"])
		# 120 of 200 revenue was costed from posted cost of goods.
		self.assertEqual(60.0, result["cost_coverage_pct"])
		self.assertEqual({"actual": 120.0, "bom": 80.0, "none": 0.0}, result["cost_sources"])
		self.assertEqual(round(200.0 - 36.0 - 50.0, 2), result["summary"]["total_gross_profit"])

	def test_bundle_header_counts_units_only(self):
		result, _ = self._run(
			[
				_product("BUNDLE-3", "Bundles", 1, 0.0, is_header=1, in_bundle=1),
				_product("Molten Small", "Small", 3, 270.0, in_bundle=1),
			],
			[_territory(1, 270.0, 0.0)],
			bundles={"BUNDLE-3"},
		)
		by_type = {t["type"]: t for t in result["by_product_type"]}
		self.assertEqual(1, by_type["Bundle"]["units"])
		self.assertEqual(270.0, by_type["Bundle"]["revenue"])
		self.assertEqual(0, by_type["Small"]["units"])
		self.assertEqual(["Molten Small"], [p["item_code"] for p in result["top_products"]])
		self.assertEqual(3.0, result["bundle_composition"][0]["times_in_bundle"])

	def test_branch_is_a_sql_filter_and_echoed(self):
		result, seen = self._run(
			[_product("Molten Small", "Small", 1, 30.0)], [_territory(1, 30.0, 0.0)], branch="Dokki"
		)
		query, params = seen[0]
		self.assertIn("si.pos_profile = %(branch)s", query)
		self.assertEqual("Dokki", params["branch"])
		self.assertEqual("Dokki", result["filters"]["branch"])

	def test_no_branch_means_no_branch_condition(self):
		_, seen = self._run([_product("Molten Small", "Small", 1, 30.0)], [_territory(1, 30.0, 0.0)])
		self.assertNotIn("%(branch)s", seen[0][0])

	def test_empty_period(self):
		result, _ = self._run([], [])
		self.assertEqual(0, result["summary"]["total_orders"])
		self.assertEqual(0.0, result["cost_coverage_pct"])
		self.assertEqual([], result["top_products"])


if __name__ == "__main__":
	unittest.main()

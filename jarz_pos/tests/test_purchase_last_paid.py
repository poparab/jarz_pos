"""Last paid price is per stock unit, whatever unit it was billed in.

Production, 2026-10-19: butter last bought as "Carton 10 Kg" at 5500 was
offered as 5500 per Kg, so a 20 Kg request priced at 110,000 and no custody
could pay for it — the purchase from requests could not be submitted.
"""

import unittest
from unittest.mock import patch


class TestLastPaidDetails(unittest.TestCase):
	def _run(self, rows, codes=("butter",)):
		from jarz_pos.api import purchase

		captured = {}

		def fake_sql(query, values=None, as_dict=False):
			captured["query"] = query
			captured["values"] = values
			return rows

		with patch("jarz_pos.api.purchase.frappe.db.sql", side_effect=fake_sql):
			result = purchase.last_paid_details_bulk(list(codes))
		return result, captured

	def test_carton_price_is_divided_down_to_the_stock_unit(self):
		result, _ = self._run(
			[{"item_code": "butter", "uom": "Carton 10 Kg", "rate": 5500.0, "conversion_factor": 10.0, "stock_uom_rate": 550.0}]
		)
		self.assertEqual(550.0, result["butter"]["rate"])
		self.assertEqual("Carton 10 Kg", result["butter"]["uom"])
		self.assertEqual(5500.0, result["butter"]["uom_rate"])
		self.assertEqual(10.0, result["butter"]["conversion_factor"])

	def test_first_row_wins_because_the_query_orders_newest_first(self):
		result, captured = self._run(
			[
				{"item_code": "butter", "uom": "Kg", "rate": 154.0, "conversion_factor": 1.0},
				{"item_code": "butter", "uom": "Kg", "rate": 136.5, "conversion_factor": 1.0},
			]
		)
		self.assertEqual(154.0, result["butter"]["rate"])
		query = " ".join(captured["query"].split())
		self.assertIn("order by pi.posting_date desc, pi.posting_time desc", query)

	def test_returns_and_zero_rates_are_excluded_in_the_query(self):
		_, captured = self._run([])
		query = " ".join(captured["query"].split())
		self.assertIn("ifnull(pi.is_return, 0) = 0", query)
		self.assertIn("pii.rate > 0", query)
		self.assertIn("pi.docstatus = 1", query)
		self.assertEqual(("butter",), captured["values"]["codes"])

	def test_supplier_of_the_latest_line_is_returned(self):
		# Buying from requests defaults the invoice to whoever we last paid.
		result, captured = self._run(
			[
				{"item_code": "butter", "uom": "Kg", "rate": 154.0, "conversion_factor": 1.0, "supplier": "Lactalis"},
				{"item_code": "butter", "uom": "Kg", "rate": 136.5, "conversion_factor": 1.0, "supplier": "Older Co"},
			]
		)
		self.assertEqual("Lactalis", result["butter"]["supplier"])
		self.assertIn("pi.supplier", " ".join(captured["query"].split()))

	def test_missing_conversion_falls_back_to_stock_uom_rate(self):
		result, _ = self._run(
			[{"item_code": "butter", "uom": "Box", "rate": 300.0, "conversion_factor": 0, "stock_uom_rate": 100.0}]
		)
		self.assertEqual(100.0, result["butter"]["rate"])

	def test_no_codes_makes_no_query(self):
		from jarz_pos.api import purchase

		with patch("jarz_pos.api.purchase.frappe.db.sql") as sql:
			self.assertEqual({}, purchase.last_paid_details_bulk([]))
			self.assertEqual({}, purchase.last_paid_details_bulk([None, ""]))
		sql.assert_not_called()


if __name__ == "__main__":
	unittest.main()

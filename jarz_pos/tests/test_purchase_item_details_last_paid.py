"""``get_item_details`` carries the last price actually paid.

Reorder refills the cart from it. Without these fields a refilled line kept the
old invoice's rate, so a price that had moved since was bought at yesterday's
figure unless the buyer noticed. ``search_items`` already sends the same three
fields; this keeps the two in step.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.api import purchase as pu


class TestItemDetailsLastPaid(unittest.TestCase):
	def setUp(self):
		fake = MagicMock()
		fake.get_roles.return_value = ["Purchase Manager"]
		fake.get_doc.return_value = SimpleNamespace(
			name="RM-Butter", item_name="Butter", stock_uom="Kg"
		)
		patches = [
			patch.object(pu, "frappe", fake),
			patch.object(pu, "_get_item_uoms", return_value=[]),
			patch.object(pu, "_get_item_prices", return_value=[]),
			patch.object(pu, "_item_tax_templates_bulk", return_value={}),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_last_paid_fields_come_from_the_latest_invoice(self):
		last = {"RM-Butter": {"rate": 550.0, "uom": "Carton 10 Kg", "uom_rate": 5500.0, "conversion_factor": 10}}
		with patch.object(pu, "last_paid_details_bulk", return_value=last) as bulk:
			result = pu.get_item_details("RM-Butter")
		bulk.assert_called_once_with(["RM-Butter"])
		self.assertEqual(result["last_purchase_rate"], 550.0, "per stock unit")
		self.assertEqual(result["last_purchase_uom"], "Carton 10 Kg")
		self.assertEqual(result["last_purchase_uom_rate"], 5500.0)

	def test_never_bought_reads_as_zero(self):
		with patch.object(pu, "last_paid_details_bulk", return_value={}):
			result = pu.get_item_details("RM-Butter")
		self.assertEqual(result["last_purchase_rate"], 0)
		self.assertIsNone(result["last_purchase_uom"])
		self.assertEqual(result["last_purchase_uom_rate"], 0)


if __name__ == "__main__":
	unittest.main()

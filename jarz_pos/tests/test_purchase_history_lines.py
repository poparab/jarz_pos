"""The line fields the purchase history hands to the app's reorder button.

Reorder refills the cart from these rows. When a line's UOM has since been
removed from the Item, the app falls back to the stock UOM and has to restate
the quantity and rate in it — 5 x "Box of 12" at 120 is 60 units at 10, not 5
units at 120. The Item no longer knows what a Box held; only the invoice line's
own ``conversion_factor`` does, so the history must return it.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api import purchase as pu


INVOICE = {"name": "ACC-PINV-0001", "supplier": "Acme Supplies"}
LINE = {
	"parent": "ACC-PINV-0001",
	"item_code": "RM-Cups",
	"item_name": "Cups",
	"qty": 5,
	"uom": "Box of 12",
	"rate": 120,
	"amount": 600,
	"warehouse": "Stores - JZ",
	"item_tax_template": "",
	"conversion_factor": 12,
}


class TestPurchaseHistoryLines(unittest.TestCase):
	def setUp(self):
		self.item_fields = None

		def get_all(doctype, **kwargs):
			if doctype == "Purchase Invoice":
				return [dict(INVOICE)]
			if doctype == "Purchase Invoice Item":
				self.item_fields = kwargs.get("fields")
				return [dict(LINE)]
			return []

		fake = MagicMock()
		fake.get_roles.return_value = ["Purchase Manager"]
		fake.get_all.side_effect = get_all
		fake.db.count.return_value = 1

		p = patch.object(pu, "frappe", fake)
		p.start()
		self.addCleanup(p.stop)

	def test_line_query_asks_for_the_conversion_factor(self):
		pu.get_purchase_invoices()
		self.assertIn(
			"conversion_factor",
			self.item_fields,
			"without it a reorder cannot restate a removed UOM in the stock UOM",
		)

	def test_conversion_factor_reaches_the_invoice_lines(self):
		result = pu.get_purchase_invoices()
		line = result["invoices"][0]["items"][0]
		self.assertEqual(line["uom"], "Box of 12")
		self.assertEqual(line["conversion_factor"], 12)


if __name__ == "__main__":
	unittest.main()

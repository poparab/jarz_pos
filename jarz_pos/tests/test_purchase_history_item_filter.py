"""The purchase history's item filter.

``item_code`` keeps only the invoices that carry a line of that item. It finds
those invoices first, then lists them with the other filters on top; an item
never bought returns an empty page without listing every invoice.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api import purchase as pu


class TestPurchaseHistoryItemFilter(unittest.TestCase):
	def setUp(self):
		self.calls = []
		self.parents = ["ACC-PINV-0002", "ACC-PINV-0007"]

		def get_all(doctype, **kwargs):
			self.calls.append((doctype, kwargs))
			if doctype == "Purchase Invoice Item" and kwargs.get("pluck") == "parent":
				return list(self.parents)
			if doctype == "Purchase Invoice":
				return [{"name": n} for n in self.parents]
			return []

		self.fake = MagicMock()
		self.fake.get_roles.return_value = ["Purchase Manager"]
		self.fake.get_all.side_effect = get_all
		self.fake.db.count.return_value = 2

		p = patch.object(pu, "frappe", self.fake)
		p.start()
		self.addCleanup(p.stop)

	def _invoice_query(self):
		return next(kw for dt, kw in self.calls if dt == "Purchase Invoice")

	def test_item_code_limits_the_invoices_to_its_parents(self):
		result = pu.get_purchase_invoices(item_code="RM-Cups", status="Paid")
		lookup = self.calls[0]
		self.assertEqual(lookup[0], "Purchase Invoice Item")
		self.assertEqual(lookup[1]["filters"]["item_code"], "RM-Cups")
		filters = self._invoice_query()["filters"]
		self.assertEqual(filters["name"], ["in", self.parents])
		self.assertEqual(filters["status"], "Paid", "the item filter combines with the rest")
		self.assertEqual(result["total"], 2)

	def test_item_never_bought_returns_an_empty_page(self):
		self.parents = []
		result = pu.get_purchase_invoices(item_code="RM-Unbought")
		self.assertEqual(result, {"invoices": [], "total": 0})
		self.assertFalse(
			any(dt == "Purchase Invoice" for dt, _ in self.calls),
			"an empty match must not fall through to an unfiltered listing",
		)

	def test_no_item_code_leaves_the_listing_unfiltered(self):
		pu.get_purchase_invoices()
		self.assertNotIn("name", self._invoice_query()["filters"])


if __name__ == "__main__":
	unittest.main()

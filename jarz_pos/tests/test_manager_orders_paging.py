"""``get_manager_orders`` paging and the B2C / B2B channel filter.

Mock-based: ``frappe.get_all`` is captured, so the test pins the filters the
query sends and the ``has_more`` arithmetic, not what a site happens to hold.
"""

import unittest
from unittest import mock

from jarz_pos.api import manager


def _row(i, purpose="Standard"):
	return {
		"name": f"ACC-SINV-{i:05d}",
		"customer": "C",
		"customer_name": "C",
		"posting_date": "2026-11-08",
		"posting_time": "10:00:00",
		"grand_total": 100,
		"net_total": 100,
		"status": "Paid",
		"pos_profile": "Dokki",
		"custom_sales_invoice_state": "Received",
		"sales_invoice_state": None,
		"woo_order_id": None,
		"custom_order_purpose": purpose,
	}


class TestManagerOrdersPaging(unittest.TestCase):
	def _call(self, rows, **kwargs):
		meta = mock.Mock()
		meta.get_field.side_effect = lambda f: f == "custom_sales_invoice_state"
		with (
			mock.patch.object(manager, "_ensure_manager_dashboard_access"),
			mock.patch.object(manager, "_current_user_allowed_profiles", return_value=["Dokki"]),
			mock.patch.object(manager.frappe, "get_meta", return_value=meta, create=True),
			mock.patch.object(manager.frappe, "get_all", return_value=rows, create=True) as get_all,
		):
			result = manager.get_manager_orders(**kwargs)
		return result, get_all.call_args.kwargs

	def test_one_extra_row_means_there_is_another_page(self):
		result, kw = self._call([_row(i) for i in range(21)], limit=20, start=40)
		self.assertTrue(result["has_more"])
		self.assertEqual(20, len(result["invoices"]))
		self.assertEqual(40, kw["limit_start"])
		self.assertEqual(21, kw["limit_page_length"])

	def test_last_page_has_no_more(self):
		result, _ = self._call([_row(i) for i in range(7)], limit=20)
		self.assertFalse(result["has_more"])
		self.assertEqual(7, len(result["invoices"]))

	def test_b2b_filters_on_b2b_supply(self):
		_, kw = self._call([], limit=20, channel="B2B")
		self.assertIn(["custom_order_purpose", "=", "B2B Supply"], kw["filters"])

	def test_b2c_excludes_b2b_staff_and_samples(self):
		_, kw = self._call([], limit=20, channel="b2c")
		self.assertIn(["custom_order_purpose", "not in", ["B2B Supply", "Employee"]], kw["filters"])
		self.assertIn(["custom_order_purpose", "not like", "Sample%"], kw["filters"])

	def test_all_adds_no_purpose_filter(self):
		_, kw = self._call([], limit=20, channel="all")
		self.assertFalse([f for f in kw["filters"] if f[0] == "custom_order_purpose"])

	def test_payload_carries_the_purpose(self):
		result, _ = self._call([_row(1, "B2B Supply")], limit=20)
		self.assertEqual("B2B Supply", result["invoices"][0]["order_purpose"])


if __name__ == "__main__":
	unittest.main()

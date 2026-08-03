"""Tests for exposing the WooCommerce order number to the POS app.

Staff, couriers and customers all identify an order by its Woo number, never by
the ERPNext Sales Invoice name, so every list/preview payload the Flutter app
renders an identifier from must carry ``woo_order_id``.

The trap these tests pin down: ``woo_order_id`` is an ``Int`` custom field on
Sales Invoice, so an order that never came from WooCommerce reads back as ``0``
rather than ``NULL``. Passed through untouched, the app would label POS-native
orders "#0". Normalization to ``None`` therefore happens once, centrally, and
these tests keep it that way.
"""

import unittest
from unittest.mock import patch


class TestNormalizeWooOrderId(unittest.TestCase):
    """jarz_pos.utils.invoice_utils.normalize_woo_order_id."""

    def test_returns_the_id_for_a_woo_order(self):
        from jarz_pos.utils.invoice_utils import normalize_woo_order_id

        self.assertEqual(normalize_woo_order_id(16834), 16834)

    def test_coerces_a_stringified_id(self):
        from jarz_pos.utils.invoice_utils import normalize_woo_order_id

        self.assertEqual(normalize_woo_order_id("16834"), 16834)

    def test_zero_means_not_a_woo_order(self):
        """The Int column's default — must never surface as the id 0."""
        from jarz_pos.utils.invoice_utils import normalize_woo_order_id

        self.assertIsNone(normalize_woo_order_id(0))
        self.assertIsNone(normalize_woo_order_id("0"))

    def test_none_and_blank_are_not_woo_orders(self):
        from jarz_pos.utils.invoice_utils import normalize_woo_order_id

        self.assertIsNone(normalize_woo_order_id(None))
        self.assertIsNone(normalize_woo_order_id(""))

    def test_unparseable_values_degrade_to_none(self):
        from jarz_pos.utils.invoice_utils import normalize_woo_order_id

        self.assertIsNone(normalize_woo_order_id("not-a-number"))


class TestGetWooOrderIds(unittest.TestCase):
    """jarz_pos.utils.invoice_utils.get_woo_order_ids."""

    def test_maps_only_invoices_that_have_a_woo_id(self):
        from jarz_pos.utils import invoice_utils

        rows = [
            {"name": "ACC-SINV-2026-00001", "woo_order_id": 16834},
            {"name": "ACC-SINV-2026-00002", "woo_order_id": 0},
            {"name": "ACC-SINV-2026-00003", "woo_order_id": None},
        ]

        with patch.object(invoice_utils.frappe, "get_all", return_value=rows):
            result = invoice_utils.get_woo_order_ids(
                ["ACC-SINV-2026-00001", "ACC-SINV-2026-00002", "ACC-SINV-2026-00003"]
            )

        self.assertEqual(result, {"ACC-SINV-2026-00001": 16834})

    def test_empty_input_never_queries(self):
        from jarz_pos.utils import invoice_utils

        with patch.object(invoice_utils.frappe, "get_all") as get_all:
            self.assertEqual(invoice_utils.get_woo_order_ids([]), {})
            self.assertEqual(invoice_utils.get_woo_order_ids(None), {})
            get_all.assert_not_called()

    def test_blank_and_none_entries_are_dropped(self):
        from jarz_pos.utils import invoice_utils

        with patch.object(invoice_utils.frappe, "get_all") as get_all:
            invoice_utils.get_woo_order_ids([None, "", "   ", "ACC-SINV-2026-00001"])

        _, kwargs = get_all.call_args
        self.assertEqual(kwargs["filters"], {"name": ["in", ["ACC-SINV-2026-00001"]]})

    def test_is_one_query_regardless_of_row_count(self):
        """The callers are list endpoints; an N+1 here would be a real cost."""
        from jarz_pos.utils import invoice_utils

        names = [f"ACC-SINV-2026-{i:05d}" for i in range(50)]

        with patch.object(invoice_utils.frappe, "get_all", return_value=[]) as get_all:
            invoice_utils.get_woo_order_ids(names)

        self.assertEqual(get_all.call_count, 1)


if __name__ == "__main__":
    unittest.main()

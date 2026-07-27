"""Regression tests for the two accounting defects the deep audit surfaced.

Both were found by auditing real production data rather than by reasoning about
the code, and both are invisible to a double-entry check because every voucher
involved balances perfectly:

* dispatching the same invoice twice created two Payment Entries to Courier
  Outstanding, overstating what the courier owed and understating Debtors;
* a POS invoice that acquired ``is_pos`` and was then saved through a normal
  validate picked the POS Profile's ``update_stock`` back up and booked its
  stock a second time, on top of the Delivery Note.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.events.sales_invoice import suppress_pos_invoice_stock_update


class _Doc:
    """Minimal stand-in for a Sales Invoice document."""

    def __init__(self, *, is_pos=1, update_stock=1, name="ACC-SINV-TEST"):
        self.is_pos = is_pos
        self.update_stock = update_stock
        self.name = name


class TestStockUpdateSuppression(unittest.TestCase):
    """POS invoices must never move stock themselves."""

    def test_pos_invoice_has_stock_update_suppressed(self):
        doc = _Doc(is_pos=1, update_stock=1)
        suppress_pos_invoice_stock_update(doc)
        self.assertEqual(doc.update_stock, 0)

    def test_already_suppressed_is_left_alone(self):
        doc = _Doc(is_pos=1, update_stock=0)
        suppress_pos_invoice_stock_update(doc)
        self.assertEqual(doc.update_stock, 0)

    def test_non_pos_invoice_is_not_touched(self):
        """A non-POS invoice may legitimately move stock — don't interfere."""
        doc = _Doc(is_pos=0, update_stock=1)
        suppress_pos_invoice_stock_update(doc)
        self.assertEqual(doc.update_stock, 1)

    def test_truthy_string_flags_are_handled(self):
        doc = _Doc(is_pos="1", update_stock="1")
        suppress_pos_invoice_stock_update(doc)
        self.assertEqual(doc.update_stock, 0)

    def test_missing_document_is_a_noop(self):
        suppress_pos_invoice_stock_update(None)  # must not raise

    def test_malformed_document_does_not_raise(self):
        """A junk value must never take down invoice validation."""
        doc = _Doc()
        doc.is_pos = object()
        suppress_pos_invoice_stock_update(doc)


class TestDispatchIsSerialised(unittest.TestCase):
    """`mark_courier_outstanding` must not run twice concurrently per invoice."""

    @patch("jarz_pos.services.delivery_handling._mark_courier_outstanding_locked")
    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_lock_is_taken_and_released(self, mock_frappe, mock_body):
        from jarz_pos.services import delivery_handling

        mock_frappe.db.sql.return_value = [[1]]
        mock_body.return_value = {"ok": True}

        result = delivery_handling.mark_courier_outstanding("INV-1", None, "Employee", "EMP-1")

        self.assertEqual(result, {"ok": True})
        statements = [c.args[0] for c in mock_frappe.db.sql.call_args_list]
        self.assertTrue(any("GET_LOCK" in s for s in statements), statements)
        self.assertTrue(any("RELEASE_LOCK" in s for s in statements), statements)

    @patch("jarz_pos.services.delivery_handling._mark_courier_outstanding_locked")
    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_second_concurrent_dispatch_is_refused(self, mock_frappe, mock_body):
        """When the lock is held, the body must not run at all."""
        from jarz_pos.services import delivery_handling

        mock_frappe.db.sql.return_value = [[0]]  # GET_LOCK timed out
        mock_frappe.throw.side_effect = RuntimeError("dispatch in progress")
        mock_frappe._ = lambda text: text

        with self.assertRaises(RuntimeError):
            delivery_handling.mark_courier_outstanding("INV-1", None, "Employee", "EMP-1")
        mock_body.assert_not_called()

    @patch("jarz_pos.services.delivery_handling._mark_courier_outstanding_locked")
    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_lock_is_released_even_when_the_body_raises(self, mock_frappe, mock_body):
        """A failed dispatch must not strand the lock and wedge the invoice."""
        from jarz_pos.services import delivery_handling

        mock_frappe.db.sql.return_value = [[1]]
        mock_body.side_effect = ValueError("boom")

        with self.assertRaises(ValueError):
            delivery_handling.mark_courier_outstanding("INV-1", None, "Employee", "EMP-1")

        statements = [c.args[0] for c in mock_frappe.db.sql.call_args_list]
        self.assertTrue(any("RELEASE_LOCK" in s for s in statements), statements)

    @patch("jarz_pos.services.delivery_handling._mark_courier_outstanding_locked")
    @patch("jarz_pos.services.delivery_handling.frappe")
    def test_lock_key_is_per_invoice(self, mock_frappe, mock_body):
        """Two different orders must be able to dispatch at the same time."""
        from jarz_pos.services import delivery_handling

        mock_frappe.db.sql.return_value = [[1]]
        mock_body.return_value = {}

        delivery_handling.mark_courier_outstanding("INV-A", None, "Employee", "E1")
        delivery_handling.mark_courier_outstanding("INV-B", None, "Employee", "E1")

        keys = [
            c.args[1][0]
            for c in mock_frappe.db.sql.call_args_list
            if len(c.args) > 1 and "GET_LOCK" in c.args[0]
        ]
        self.assertIn("jarz-ofd:INV-A", keys)
        self.assertIn("jarz-ofd:INV-B", keys)
        self.assertNotEqual(keys[0], keys[1])


if __name__ == "__main__":
    unittest.main()

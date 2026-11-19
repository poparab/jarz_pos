"""Pickup mode across a POS invoice amendment.

Regression cover for order 22088 / ``ACC-SINV-2026-19775`` (2026-11-19): the order
came in as pickup by mistake, was edited twice in the app as delivery, and stayed
pickup both times, with the 50 EGP delivery fee the operator entered suppressed.

``_run_invoice_amendment_job`` OR-ed the source invoice's ``custom_is_pickup`` into
whatever the client sent, so once an order was pickup no edit could undo it. The
app also only sent ``pickup`` when it was on. Now an explicit ``pickup`` (0 or 1)
wins and only a missing value inherits the source's mode.

Mock-based; runs without a live site (CI logic gate).
"""

import unittest

from jarz_pos.tests.test_amendment_price_list import (
	_FakeSourceInvoice,
	_mock_manager_frappe,
	_run_amendment,
)

_PICKUP_ARG = 8  # positional index of ``pickup`` in the create_pos_invoice call


class TestResolveAmendmentPickup(unittest.TestCase):
	def _resolve(self, source_pickup, pickup):
		from jarz_pos.api.manager import _resolve_amendment_pickup

		return _resolve_amendment_pickup(_FakeSourceInvoice(custom_is_pickup=source_pickup), pickup)

	def test_explicit_zero_turns_a_pickup_order_into_delivery(self):
		for value in (0, "0", False, "false"):
			with self.subTest(value=value):
				self.assertFalse(self._resolve(1, value))

	def test_explicit_one_turns_a_delivery_order_into_pickup(self):
		for value in (1, "1", True, "true"):
			with self.subTest(value=value):
				self.assertTrue(self._resolve(0, value))

	def test_missing_value_keeps_the_source_mode(self):
		for value in (None, "", "  "):
			with self.subTest(value=value):
				self.assertTrue(self._resolve(1, value))
				self.assertFalse(self._resolve(0, value))


class TestAmendmentJobForwardsPickup(unittest.TestCase):
	def test_pickup_order_edited_to_delivery_is_created_as_delivery(self):
		"""Order 22088: source is pickup, the operator sends pickup=0."""
		source = _FakeSourceInvoice(custom_is_pickup=1)
		creation, _ = _run_amendment(source, _mock_manager_frappe(), pickup="0")

		self.assertTrue(creation.called, "The replacement invoice was never created")
		self.assertFalse(creation.call_args.args[_PICKUP_ARG])

	def test_old_client_without_pickup_keeps_a_pickup_order_pickup(self):
		source = _FakeSourceInvoice(custom_is_pickup=1)
		creation, _ = _run_amendment(source, _mock_manager_frappe())

		self.assertTrue(creation.call_args.args[_PICKUP_ARG])


if __name__ == "__main__":
	unittest.main()

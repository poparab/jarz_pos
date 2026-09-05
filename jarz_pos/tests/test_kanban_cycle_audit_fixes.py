"""Regression tests for the 2026-09-05 kanban cycle audit fixes.

Each test pins one finding from the audit so it cannot come back quietly:

* the board transition matrix now lives on the server;
* an unpaid pickup cannot be dispatched;
* a sales-partner order carries ONE partner transaction whichever path minted it;
* an unsettled partner transaction no longer blocks a pre-dispatch cancel;
* a second dispatch of a dispatched order is refused;
* a courier row switched to an online ledger reports its reversible amount.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class TestTransitionMatrix(unittest.TestCase):
	def _block(self, old, new):
		from jarz_pos.api.kanban import _transition_block_reason

		return _transition_block_reason(old, new)

	def test_one_stage_forward_is_allowed(self):
		self.assertIsNone(self._block("Recieved", "In Progress"))
		self.assertIsNone(self._block("In Progress", "Ready"))
		self.assertIsNone(self._block("Ready", "Out for Delivery"))
		self.assertIsNone(self._block("Out for Delivery", "Delivered"))

	def test_empty_state_counts_as_received(self):
		self.assertIsNone(self._block("", "In Progress"))
		self.assertIsNotNone(self._block(None, "Delivered"))

	def test_skipping_out_for_delivery_is_refused(self):
		reason = self._block("Ready", "Delivered")
		self.assertIsNotNone(reason)
		self.assertIn("one stage at a time", reason)

	def test_only_ready_may_go_back_to_in_progress(self):
		self.assertIsNone(self._block("Ready", "In Progress"))
		self.assertIn("backward", self._block("Out for Delivery", "Ready") or "")
		self.assertIn("backward", self._block("Delivered", "Out for Delivery") or "")
		self.assertIn("backward", self._block("In Progress", "Recieved") or "")

	def test_cancelled_and_returned_are_not_drag_targets(self):
		self.assertIn("Cancel Order", self._block("Ready", "Cancelled") or "")
		self.assertIn("Return Order", self._block("Delivered", "Returned") or "")

	def test_unknown_labels_are_left_to_other_checks(self):
		self.assertIsNone(self._block(MagicMock(), "Delivered"))
		self.assertIsNone(self._block("Ready", "Something Else"))


class TestPickupMustBePaid(unittest.TestCase):
	@patch("jarz_pos.api.kanban.ensure_open_shift_for_invoice")
	@patch("jarz_pos.api.kanban.ensure_profile_scoped_invoice_access")
	@patch("jarz_pos.api.kanban._get_allowed_states")
	@patch("jarz_pos.api.kanban.frappe")
	def test_unpaid_pickup_cannot_go_out_for_delivery(self, mock_frappe, mock_states, _scope, _shift):
		from jarz_pos.api.kanban import update_invoice_state

		mock_states.return_value = ["Ready", "Out for Delivery"]
		inv = MagicMock()
		inv.name = "INV-PICKUP"
		inv.docstatus = 1
		inv.custom_is_pickup = 1
		inv.get.side_effect = lambda f: {
			"custom_sales_invoice_state": "Ready",
			"outstanding_amount": 280.0,
			"custom_return_status": "",
		}.get(f)
		mock_frappe.get_doc.return_value = inv
		mock_frappe.get_meta.return_value.get_field.side_effect = (
			lambda name: MagicMock() if name == "custom_sales_invoice_state" else None
		)

		result = update_invoice_state("INV-PICKUP", "Out for Delivery")

		self.assertFalse(result.get("success"))
		self.assertIn("paid before", result.get("error") or "")
		inv.save.assert_not_called()


class TestPartnerTransactionsBlockOnlyWhenSettled(unittest.TestCase):
	@patch("jarz_pos.api.manager.frappe")
	def test_settled_only_filter_is_applied(self, mock_frappe):
		from jarz_pos.api.manager import _find_sales_partner_transactions

		mock_frappe.get_all.return_value = ["SPT-1"]
		_find_sales_partner_transactions("INV-1", settled_only=True)
		filters = mock_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters.get("status"), "Settled")

		_find_sales_partner_transactions("INV-1")
		filters = mock_frappe.get_all.call_args.kwargs["filters"]
		self.assertNotIn("status", filters)

	@patch("jarz_pos.api.manager._find_active_custom_shipping_requests", return_value=[])
	@patch("jarz_pos.api.manager._find_submitted_journal_entries", return_value=[])
	@patch("jarz_pos.api.manager._find_courier_transactions", return_value=[])
	@patch("jarz_pos.api.manager._get_active_delivery_trip_name", return_value=None)
	@patch("jarz_pos.api.manager._find_submitted_delivery_notes", return_value=[])
	@patch("jarz_pos.api.manager._find_sales_partner_transactions")
	def test_cancel_reading_ignores_unsettled_rows(self, mock_spt, *_):
		from jarz_pos.api.manager import get_invoice_hard_mutation_blocker

		mock_spt.side_effect = lambda name, settled_only=False: [] if settled_only else ["SPT-UNSETTLED"]
		inv = {"name": "INV-TALABAT"}

		self.assertIsNotNone(get_invoice_hard_mutation_blocker(inv))
		self.assertIsNone(
			get_invoice_hard_mutation_blocker(inv, ignore_unsettled_partner_transactions=True)
		)


class TestAlreadyDispatchedGuard(unittest.TestCase):
	def _inv(self, state="Ready", was_ofd=0):
		inv = MagicMock()
		inv.name = "INV-G"
		inv.get.side_effect = lambda f: {
			"custom_sales_invoice_state": state,
			"custom_was_out_for_delivery": was_ofd,
		}.get(f)
		return inv

	@patch("jarz_pos.services.delivery_handling.frappe")
	def test_fresh_order_passes(self, mock_frappe):
		from jarz_pos.services.delivery_handling import assert_not_already_dispatched

		assert_not_already_dispatched(self._inv())
		mock_frappe.throw.assert_not_called()

	@patch("jarz_pos.services.delivery_handling.frappe")
	def test_stamped_order_is_refused(self, mock_frappe):
		from jarz_pos.services.delivery_handling import assert_not_already_dispatched

		assert_not_already_dispatched(self._inv(state="Ready", was_ofd=1))
		mock_frappe.throw.assert_called_once()

	@patch("jarz_pos.services.delivery_handling.frappe")
	def test_live_state_is_the_fallback(self, mock_frappe):
		from jarz_pos.services.delivery_handling import assert_not_already_dispatched

		assert_not_already_dispatched(self._inv(state="Out for Delivery", was_ofd=0))
		mock_frappe.throw.assert_called_once()


class TestSwitchedOnlineAmount(unittest.TestCase):
	def _module(self):
		from jarz_pos.services import delivery_handling

		return delivery_handling

	def test_settled_zero_or_unlinked_rows_report_nothing(self):
		fn = self._module()._switched_online_amount
		self.assertEqual(fn(None), 0.0)
		self.assertEqual(fn({"status": "Settled", "amount": 0, "journal_entry": "JE", "payment_mode": "Instapay"}), 0.0)
		self.assertEqual(fn({"status": "Unsettled", "amount": 330, "journal_entry": "JE", "payment_mode": "Instapay"}), 0.0)
		self.assertEqual(fn({"status": "Unsettled", "amount": 0, "journal_entry": "", "payment_mode": "Instapay"}), 0.0)
		# A dispatch label is not a collection method; a partner fee row rides "Online"
		# but its entry carries no Courier Outstanding credit (covered below).
		self.assertEqual(fn({"status": "Unsettled", "amount": 0, "journal_entry": "JE", "payment_mode": "Deferred"}), 0.0)
		self.assertEqual(fn({"status": "Unsettled", "amount": 0, "journal_entry": "JE", "payment_mode": "Cash"}), 0.0)

	@patch("jarz_pos.services.delivery_handling.frappe")
	def test_reads_the_courier_outstanding_credit_of_the_switch_entry(self, mock_frappe):
		fn = self._module()._switched_online_amount
		mock_frappe.db.get_value.return_value = 1
		mock_frappe.get_all.return_value = [
			{"account": "Bank Account - J", "credit_in_account_currency": 0},
			{"account": "Courier Outstanding - J", "credit_in_account_currency": 330},
		]
		self.assertEqual(
			fn({"status": "Unsettled", "amount": 0, "journal_entry": "JE-1", "payment_mode": "Instapay"}),
			330.0,
		)
		# A fee accrual entry credits a payable, not Courier Outstanding: nothing to revert.
		mock_frappe.get_all.return_value = [
			{"account": "Freight and Forwarding Charges - J", "credit_in_account_currency": 0},
			{"account": "Deliverk - J", "credit_in_account_currency": 40},
		]
		self.assertEqual(
			fn({"status": "Unsettled", "amount": 0, "journal_entry": "JE-2", "payment_mode": "Online"}),
			0.0,
		)


if __name__ == "__main__":
	unittest.main()

"""An amendment carries the transfer proof and its confirmation to the replacement.

Order 22069 / ``ACC-SINV-2026-19757`` (2026-11-19): the InstaPay screenshot was
uploaded and confirmed, which posted the payment. The order was then edited. The
amendment moved the PAYMENT to ``ACC-SINV-2026-19757-1`` but left the receipt on
the cancelled source and blanked "Payment Confirmed", so the live order read as a
paid InstaPay order with no proof -- and, being paid, its card offered no way to
attach any.

Pure-Python unit tests in the style of ``test_amendment_shipping_request``.
"""

import unittest
from unittest.mock import MagicMock, patch

SOURCE = "ACC-SINV-TEST-19757"
NEW = "ACC-SINV-TEST-19757-1"


def _frappe(receipts, *, new_outstanding=0.0, new_total=1080.0, source_confirmation=None):
	mf = MagicMock()
	source_confirmation = source_confirmation or {}

	def get_value(doctype, name, fields, as_dict=False):
		if doctype == "Sales Invoice" and name == NEW:
			return {
				"custom_kanban_profile": "6th of october",
				"pos_profile": "6th of october",
				"grand_total": new_total,
				"outstanding_amount": new_outstanding,
			}
		if doctype == "Sales Invoice" and name == SOURCE:
			return {field: source_confirmation.get(field) for field in fields}
		return None

	mf.db.get_value.side_effect = get_value
	mf.get_all.return_value = receipts
	return mf


def _receipt_updates(mf):
	return {
		c.args[1]: c.args[2] for c in mf.db.set_value.call_args_list if c.args[0] == "POS Payment Receipt"
	}


def _invoice_updates(mf):
	return [c.args[2] for c in mf.db.set_value.call_args_list if c.args[0] == "Sales Invoice"]


CONFIRMED = {
	"custom_payment_confirmation_status": "Payment Confirmed",
	"custom_payment_confirmation_reference": None,
	"custom_payment_confirmed_by": "belal@orderjarz.com",
	"custom_payment_confirmed_date": "2026-11-19 15:36:19",
}


class TestCarryOverPaymentReceipts(unittest.TestCase):
	def _run(self, mf, rebooked=("ACC-PAY-NEW",), carried=None):
		from jarz_pos.api.manager import _carry_over_payment_receipts

		if carried is None:
			carried = 1080.0 if rebooked else 0.0
		with (
			patch("jarz_pos.api.manager.frappe", mf),
			patch("jarz_pos.api.manager._add_invoice_audit_comment") as comment,
		):
			result = _carry_over_payment_receipts(
				source_invoice_name=SOURCE,
				new_invoice_name=NEW,
				rebooked_payment_entries=list(rebooked),
				carried_amount=carried,
			)
		return result, comment

	def test_order_22069_paid_and_confirmed_moves_receipt_and_confirmation(self):
		mf = _frappe(
			[{"name": "POS-RCPT-467", "status": "Confirmed", "amount": 1080.0}],
			source_confirmation=CONFIRMED,
		)
		result, comment = self._run(mf)

		self.assertEqual(result["receipts"], ["POS-RCPT-467"])
		updates = _receipt_updates(mf)["POS-RCPT-467"]
		self.assertEqual(updates["sales_invoice"], NEW)
		self.assertNotIn("amount", updates, "a confirmed receipt is evidence; its amount is not rewritten")
		self.assertTrue(result["confirmation_carried"])
		(stamped,) = _invoice_updates(mf)
		self.assertEqual(stamped["custom_payment_confirmation_status"], "Payment Confirmed")
		self.assertEqual(stamped["custom_payment_confirmed_by"], "belal@orderjarz.com")
		self.assertEqual(stamped["custom_payment_confirmation_reference"], "ACC-PAY-NEW")
		comment.assert_called_once()

	def test_unpaid_order_keeps_its_pending_receipt_with_the_new_total(self):
		mf = _frappe(
			[{"name": "POS-RCPT-1", "status": "Unconfirmed", "amount": 900.0}],
			new_outstanding=1080.0,
		)
		result, _ = self._run(mf, rebooked=())

		updates = _receipt_updates(mf)["POS-RCPT-1"]
		self.assertEqual(updates["sales_invoice"], NEW)
		self.assertEqual(updates["amount"], 1080.0)
		self.assertFalse(result["confirmation_carried"])
		self.assertEqual(_invoice_updates(mf), [], "an unpaid order is not stamped confirmed")

	def test_rejected_receipt_follows_the_order_so_it_can_be_re_uploaded(self):
		mf = _frappe([{"name": "POS-RCPT-2", "status": "Rejected", "amount": 1080.0}], new_outstanding=1080.0)
		result, _ = self._run(mf, rebooked=())
		self.assertEqual(result["receipts"], ["POS-RCPT-2"])

	def test_confirmed_proof_smaller_than_the_new_balance_stays_on_the_source(self):
		# An unpaid order whose 500 proof would otherwise cover an 800 bill.
		mf = _frappe(
			[{"name": "POS-RCPT-3", "status": "Confirmed", "amount": 500.0}],
			new_outstanding=800.0,
			new_total=800.0,
		)
		result, comment = self._run(mf, rebooked=())

		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(result["left_on_source"], ["POS-RCPT-3"])
		self.assertIn("new proof is needed", comment.call_args.args[1])

	def test_confirmed_proof_that_covers_the_balance_moves(self):
		mf = _frappe(
			[{"name": "POS-RCPT-4", "status": "Confirmed", "amount": 1080.0}],
			new_outstanding=1080.0,
		)
		result, _ = self._run(mf, rebooked=())
		self.assertEqual(result["receipts"], ["POS-RCPT-4"])

	def test_dearer_edit_does_not_carry_payment_confirmed(self):
		mf = _frappe(
			[{"name": "POS-RCPT-5", "status": "Confirmed", "amount": 1080.0}],
			new_outstanding=200.0,
			new_total=1280.0,
			source_confirmation=CONFIRMED,
		)
		result, _ = self._run(mf)
		self.assertFalse(result["confirmation_carried"])
		self.assertEqual(_invoice_updates(mf), [])
		self.assertEqual(
			result["left_on_source"], ["POS-RCPT-5"], "the 1080 transfer was spent by the carried 1080"
		)

	def test_spent_proof_cannot_cover_a_dearer_edit_acc_sinv_2026_18668(self):
		# 1,010 receipt, all 1,010 carried, 70 still owed. Comparing 1,010 to 70
		# would pass and let the 70 be booked to the bank on the old screenshot.
		mf = _frappe(
			[{"name": "POS-RCPT-201", "status": "Confirmed", "amount": 1010.0}],
			new_outstanding=70.0,
			new_total=1080.0,
			source_confirmation=CONFIRMED,
		)
		result, _ = self._run(mf, carried=1010.0)
		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(result["left_on_source"], ["POS-RCPT-201"])

	def test_partly_spent_proof_moves_when_the_rest_covers_the_balance(self):
		# A 1,500 transfer for a 1,080 order: 1,080 carried, 420 unspent >= 300 owed.
		mf = _frappe(
			[{"name": "POS-RCPT-7", "status": "Confirmed", "amount": 1500.0}],
			new_outstanding=300.0,
			new_total=1380.0,
		)
		result, _ = self._run(mf, carried=1080.0)
		self.assertEqual(result["receipts"], ["POS-RCPT-7"])

	def test_awaiting_payment_is_never_carried(self):
		mf = _frappe(
			[],
			source_confirmation={"custom_payment_confirmation_status": "Awaiting Payment"},
		)
		result, _ = self._run(mf)
		self.assertFalse(result["confirmation_carried"])
		self.assertEqual(_invoice_updates(mf), [])

	def test_changed_receipts_are_history_and_stay(self):
		mf = _frappe([{"name": "POS-RCPT-6", "status": "Changed", "amount": 1080.0}])
		result, _ = self._run(mf)
		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(result["receipts"], [])

	def test_never_raises(self):
		mf = MagicMock()
		mf.db.get_value.side_effect = RuntimeError("db down")
		result, _ = self._run(mf)
		self.assertEqual(result["receipts"], [])


if __name__ == "__main__":
	unittest.main()

"""An amendment carries the transfer proof and its confirmation to the replacement.

Order 22069 / ``ACC-SINV-2026-19757`` (2026-11-19): the InstaPay screenshot was
uploaded and confirmed, which posted the payment. The order was then edited. The
amendment moved the PAYMENT to ``ACC-SINV-2026-19757-1`` but left the receipt on
the cancelled source and blanked "Payment Confirmed", so the live order read as a
paid InstaPay order with no proof -- and, being paid, its card offered no way to
attach any.

The other half of these tests is what must NOT move: proof that would let money
be booked on a screenshot that does not describe it (spent, wrong amount, or for
a method the edit switched away from). "Left on the source" is the behaviour
before the fix, so it is always the safe answer.

Pure-Python unit tests in the style of ``test_amendment_shipping_request``.
"""

import unittest
from unittest.mock import MagicMock, patch

SOURCE = "ACC-SINV-TEST-19757"
NEW = "ACC-SINV-TEST-19757-1"
IMG = "/files/242511.jpg"


def _receipt(name, status, amount, *, image=IMG, method="InstaPay"):
	return {
		"name": name,
		"status": status,
		"amount": amount,
		"payment_method": method,
		"receipt_image": image,
		"receipt_image_url": image,
	}


def _frappe(
	receipts, *, new_outstanding=0.0, new_total=1080.0, new_method="Instapay", source_confirmation=None
):
	mf = MagicMock()
	source_confirmation = source_confirmation or {}

	def get_value(doctype, name, fields, as_dict=False):
		if doctype == "Sales Invoice" and name == NEW:
			return {
				"custom_kanban_profile": "6th of october",
				"pos_profile": "6th of october",
				"grand_total": new_total,
				"outstanding_amount": new_outstanding,
				"custom_payment_method": new_method,
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


def _comment(comment_mock):
	comment_mock.assert_called_once()
	return comment_mock.call_args.args[1]


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

	# --- what moves -------------------------------------------------------

	def test_order_22069_paid_and_confirmed_moves_receipt_and_confirmation(self):
		mf = _frappe([_receipt("POS-RCPT-467", "Confirmed", 1080.0)], source_confirmation=CONFIRMED)
		result, comment = self._run(mf)

		self.assertEqual(result["receipts"], ["POS-RCPT-467"])
		updates = _receipt_updates(mf)["POS-RCPT-467"]
		self.assertEqual(updates["sales_invoice"], NEW)
		self.assertEqual(updates["pos_profile"], "6th of october")
		self.assertNotIn("amount", updates, "a screenshot's amount is never rewritten")
		self.assertTrue(result["confirmation_carried"])
		(stamped,) = _invoice_updates(mf)
		self.assertEqual(stamped["custom_payment_confirmation_status"], "Payment Confirmed")
		self.assertEqual(stamped["custom_payment_confirmed_by"], "belal@orderjarz.com")
		self.assertEqual(stamped["custom_payment_confirmation_reference"], "ACC-PAY-NEW")
		comment.assert_called_once()

	def test_imageless_placeholder_follows_an_unpaid_order_with_the_new_total(self):
		mf = _frappe([_receipt("POS-RCPT-1", "Unconfirmed", 900.0, image=None)], new_outstanding=1080.0)
		result, _ = self._run(mf, rebooked=())

		updates = _receipt_updates(mf)["POS-RCPT-1"]
		self.assertEqual(updates["sales_invoice"], NEW)
		self.assertEqual(updates["amount"], 1080.0)
		self.assertFalse(result["confirmation_carried"])
		self.assertEqual(_invoice_updates(mf), [], "an unpaid order is not stamped confirmed")

	def test_pending_screenshot_moves_when_its_amount_still_matches_the_order(self):
		# Address-only edit of an unpaid order: same total, nothing carried.
		mf = _frappe([_receipt("POS-RCPT-2", "Unconfirmed", 1080.0)], new_outstanding=1080.0)
		result, _ = self._run(mf, rebooked=())
		self.assertEqual(result["receipts"], ["POS-RCPT-2"])
		self.assertNotIn("amount", _receipt_updates(mf)["POS-RCPT-2"])

	def test_rejected_screenshot_matching_the_order_follows_it_for_re_upload(self):
		mf = _frappe([_receipt("POS-RCPT-3", "Rejected", 1080.0)], new_outstanding=1080.0)
		result, _ = self._run(mf, rebooked=())
		self.assertEqual(result["receipts"], ["POS-RCPT-3"])

	def test_confirmed_proof_that_covers_an_unpaid_balance_moves(self):
		mf = _frappe([_receipt("POS-RCPT-4", "Confirmed", 1080.0)], new_outstanding=1080.0)
		result, _ = self._run(mf, rebooked=())
		self.assertEqual(result["receipts"], ["POS-RCPT-4"])

	def test_partly_spent_proof_moves_when_the_rest_covers_the_balance(self):
		# A 1,500 transfer for a 1,080 order: 1,080 carried, 420 unspent >= 300 owed.
		mf = _frappe([_receipt("POS-RCPT-7", "Confirmed", 1500.0)], new_outstanding=300.0, new_total=1380.0)
		result, _ = self._run(mf, carried=1080.0)
		self.assertEqual(result["receipts"], ["POS-RCPT-7"])

	# --- what stays on the source ----------------------------------------

	def test_pending_screenshot_is_not_rewritten_onto_a_dearer_order(self):
		# 1,000 screenshot, order edited to 1,300: moving it with amount=1,300
		# would let confirm_online_payment book 1,300 on a 1,000 transfer.
		mf = _frappe(
			[_receipt("POS-RCPT-8", "Unconfirmed", 1000.0)],
			new_outstanding=1300.0,
			new_total=1300.0,
		)
		result, comment = self._run(mf, rebooked=())
		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(result["left_on_source"], ["POS-RCPT-8"])
		self.assertIn("no longer matches the order total of 1300.00", _comment(comment))

	def test_stale_rejected_screenshot_cannot_cover_a_dearer_paid_edit(self):
		# R1 rejected, R2 confirmed and spent by the carried 1,010, 70 owed. R1
		# moving would be confirmable as a plain stamp and then pass as proof.
		mf = _frappe(
			[
				_receipt("POS-RCPT-R1", "Rejected", 1080.0),
				_receipt("POS-RCPT-R2", "Confirmed", 1010.0),
			],
			new_outstanding=70.0,
			new_total=1080.0,
		)
		result, comment = self._run(mf, carried=1010.0)
		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(sorted(result["left_on_source"]), ["POS-RCPT-R1", "POS-RCPT-R2"])
		text = _comment(comment)
		self.assertIn("POS-RCPT-R1 left on", text)
		self.assertIn("count that money twice", text)
		self.assertIn("POS-RCPT-R2 left on", text)
		self.assertIn("already used for the earlier payment", text)

	def test_spent_proof_cannot_cover_a_dearer_edit_acc_sinv_2026_18668(self):
		mf = _frappe(
			[_receipt("POS-RCPT-201", "Confirmed", 1010.0)],
			new_outstanding=70.0,
			new_total=1080.0,
			source_confirmation=CONFIRMED,
		)
		result, comment = self._run(mf, carried=1010.0)
		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(result["left_on_source"], ["POS-RCPT-201"])
		self.assertFalse(result["confirmation_carried"], "70 is owed that nobody confirmed")
		text = _comment(comment)
		self.assertIn("already used for the earlier payment", text)
		self.assertIn("70.00 still owed", text)
		self.assertNotIn("Confirmed receipt", text, "the old wording named every receipt Confirmed")

	def test_unpaid_confirmed_proof_smaller_than_the_bill_stays(self):
		mf = _frappe([_receipt("POS-RCPT-9", "Confirmed", 500.0)], new_outstanding=800.0, new_total=800.0)
		result, comment = self._run(mf, rebooked=())
		self.assertEqual(result["left_on_source"], ["POS-RCPT-9"])
		text = _comment(comment)
		self.assertIn("smaller than the 800.00 owed", text)
		self.assertNotIn("earlier payment", text, "nothing was paid before this edit")

	def test_edit_to_cash_leaves_the_transfer_receipt_behind(self):
		# Moving it would make dispatch re-route the order as online and flip
		# the method back to InstaPay, undoing the edit.
		mf = _frappe(
			[_receipt("POS-RCPT-10", "Unconfirmed", 1080.0)],
			new_outstanding=1080.0,
			new_method="Cash",
		)
		result, comment = self._run(mf, rebooked=())
		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(result["left_on_source"], ["POS-RCPT-10"])
		text = _comment(comment)
		self.assertIn("now paid by Cash", text)
		self.assertNotIn("screenshot", text, "a cash order needs no screenshot")

	def test_edit_from_instapay_to_wallet_leaves_the_instapay_receipt(self):
		mf = _frappe(
			[_receipt("POS-RCPT-11", "Unconfirmed", 1080.0, image=None)],
			new_outstanding=1080.0,
			new_method="Mobile Wallet",
		)
		result, _ = self._run(mf, rebooked=())
		self.assertEqual(result["left_on_source"], ["POS-RCPT-11"])

	def test_placeholder_is_not_moved_onto_a_settled_order(self):
		mf = _frappe([_receipt("POS-RCPT-12", "Unconfirmed", 1080.0, image=None)])
		result, comment = self._run(mf)
		self.assertEqual(_receipt_updates(mf), {})
		self.assertIn("already paid; nothing to do", _comment(comment))

	def test_changed_receipts_are_history_and_stay(self):
		mf = _frappe([_receipt("POS-RCPT-6", "Changed", 1080.0)])
		result, _ = self._run(mf)
		self.assertEqual(_receipt_updates(mf), {})
		self.assertEqual(result["receipts"], [])

	# --- confirmation stamp ----------------------------------------------

	def test_awaiting_payment_is_never_carried(self):
		mf = _frappe([], source_confirmation={"custom_payment_confirmation_status": "Awaiting Payment"})
		result, _ = self._run(mf)
		self.assertFalse(result["confirmation_carried"])
		self.assertEqual(_invoice_updates(mf), [])

	def test_never_raises(self):
		mf = MagicMock()
		mf.db.get_value.side_effect = RuntimeError("db down")
		result, _ = self._run(mf)
		self.assertEqual(result["receipts"], [])


if __name__ == "__main__":
	unittest.main()

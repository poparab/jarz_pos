"""Tests for the pre-dispatch cancellation guards (Door A hardening).

Covers the three guarantees added alongside the return workflow:

* a cancel may not silently rewrite an already-closed POS shift;
* the Journal Entry blocker must not confuse an invoice with its own
  amendments (``…-00001`` vs ``…-00001-1``);
* cancellation eligibility is reported to the client with a specific reason,
  and says whether the return workflow is the way forward instead.

Pure ``unittest`` with mocks — no site, no fixtures — so these run inside the
CI logic-test job.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api.manager import _mentions_invoice


class TestInvoiceMentionBoundary(unittest.TestCase):
	"""`_mentions_invoice` — the G8 over-blocking fix."""

	INVOICE = "ACC-SINV-2026-00001"

	def test_matches_plain_mention(self):
		self.assertTrue(_mentions_invoice(f"Settlement - {self.INVOICE}", self.INVOICE))

	def test_matches_dedup_tag(self):
		self.assertTrue(_mentions_invoice(f"[JARZ-JE:OFD:{self.INVOICE}]", self.INVOICE))

	def test_matches_when_followed_by_punctuation(self):
		self.assertTrue(_mentions_invoice(f"covers {self.INVOICE}, and more", self.INVOICE))

	def test_does_not_match_amendment_sibling(self):
		"""The whole point: an amendment must not block its own original."""
		self.assertFalse(_mentions_invoice(f"Settlement - {self.INVOICE}-1", self.INVOICE))

	def test_does_not_match_longer_numeric_sibling(self):
		self.assertFalse(_mentions_invoice("Settlement - ACC-SINV-2026-000012", self.INVOICE))

	def test_does_not_match_unrelated_invoice(self):
		self.assertFalse(_mentions_invoice("Settlement - ACC-SINV-2026-00002", self.INVOICE))

	def test_empty_inputs_are_safe(self):
		self.assertFalse(_mentions_invoice(None, self.INVOICE))
		self.assertFalse(_mentions_invoice("", self.INVOICE))
		self.assertFalse(_mentions_invoice("anything", ""))


class TestClosedShiftDetection(unittest.TestCase):
	"""`find_closed_shift_covering` / `assert_vouchers_not_in_closed_shift`."""

	def test_returns_none_without_a_timestamp(self):
		from jarz_pos.utils.access_control import find_closed_shift_covering

		self.assertIsNone(find_closed_shift_covering(None))

	@patch("jarz_pos.utils.access_control.frappe")
	def test_finds_the_covering_closed_shift(self, mock_frappe):
		from jarz_pos.utils import access_control

		mock_frappe.get_all.return_value = [{"name": "POS-CLOSE-0001"}]
		found = access_control.find_closed_shift_covering("2026-07-01 10:00:00")

		self.assertEqual(found["name"], "POS-CLOSE-0001")
		filters = mock_frappe.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["docstatus"], 1)
		# The window must bracket the timestamp from both sides, otherwise an
		# open shift (which has no closing row) could match.
		self.assertEqual(filters["period_start_date"][0], "<=")
		self.assertEqual(filters["period_end_date"][0], ">=")

	@patch("jarz_pos.utils.access_control.frappe")
	def test_open_shift_does_not_match(self, mock_frappe):
		from jarz_pos.utils import access_control

		mock_frappe.get_all.return_value = []
		self.assertIsNone(access_control.find_closed_shift_covering("2026-07-01 10:00:00"))

	@patch("jarz_pos.utils.access_control.frappe")
	def test_detection_failure_does_not_raise(self, mock_frappe):
		"""A lookup error must not take down the cancel path."""
		from jarz_pos.utils import access_control

		mock_frappe.get_all.side_effect = Exception("db gone")
		self.assertIsNone(access_control.find_closed_shift_covering("2026-07-01 10:00:00"))
		self.assertTrue(mock_frappe.log_error.called)

	@patch("jarz_pos.utils.access_control.find_closed_shift_for_voucher")
	@patch("jarz_pos.utils.access_control.frappe")
	def test_throws_when_a_voucher_sits_in_a_closed_shift(self, mock_frappe, mock_find):
		from jarz_pos.utils import access_control

		mock_frappe.throw.side_effect = RuntimeError("blocked")
		mock_find.return_value = {"name": "POS-CLOSE-0001"}

		with self.assertRaises(RuntimeError):
			access_control.assert_vouchers_not_in_closed_shift(
				[("Payment Entry", "PE-0001")], action_label="cancelling this order"
			)

	@patch("jarz_pos.utils.access_control.find_closed_shift_for_voucher")
	@patch("jarz_pos.utils.access_control.frappe")
	def test_allows_when_no_closed_shift_covers_the_voucher(self, mock_frappe, mock_find):
		from jarz_pos.utils import access_control

		mock_find.return_value = None

		access_control.assert_vouchers_not_in_closed_shift(
			[("Payment Entry", "PE-0001")], action_label="cancelling this order"
		)
		self.assertFalse(mock_frappe.throw.called)

	@patch("jarz_pos.utils.access_control.frappe")
	def test_empty_voucher_list_is_a_noop(self, mock_frappe):
		from jarz_pos.utils import access_control

		access_control.assert_vouchers_not_in_closed_shift([], action_label="x")
		self.assertFalse(mock_frappe.throw.called)


class TestClosedShiftIsScopedToTheTill(unittest.TestCase):
	"""A closed shift blocks a cancel only if the voucher posts to THAT shift's till.

	Production case 2026-11-19: Woo #19201 (Nasr City, paid by Kashier card into
	``kashier - J``) could not be cancelled because a Dokki shift had closed over
	the same minutes. Neither the branch nor the account had anything to do with it.
	"""

	CREATED = "2026-10-23 14:41:31"
	TILLS = {"Dokki": "Dokki - J", "Nasr city": "Nasr city - J"}

	def _run(self, closing_rows, ledger_accounts):
		from jarz_pos.utils import access_control

		def get_all(doctype, **kwargs):
			if doctype == "GL Entry":
				return [{"account": acc} for acc in ledger_accounts]
			if doctype == "POS Closing Entry":
				return closing_rows
			raise AssertionError(doctype)

		with (
			patch("jarz_pos.utils.access_control.frappe") as mock_frappe,
			patch(
				"jarz_pos.api.shift._resolve_pos_profile_account",
				side_effect=lambda company, profile, *_: self.TILLS.get(profile),
			),
		):
			mock_frappe.db.get_value.return_value = self.CREATED
			mock_frappe.get_all.side_effect = get_all
			return access_control.find_closed_shift_for_voucher("Payment Entry", "PE-0001")

	@staticmethod
	def _closing(name, profile):
		return {"name": name, "pos_profile": profile, "company": "Jarz"}

	def test_gateway_payment_is_never_blocked(self):
		"""Kashier money sits in no shift's reconciliation."""
		found = self._run([self._closing("POS-CLO-232", "Dokki")], ["Debtors - J", "kashier - J"])
		self.assertIsNone(found)

	def test_other_branch_shift_does_not_block(self):
		found = self._run([self._closing("POS-CLO-232", "Dokki")], ["Debtors - J", "Nasr city - J"])
		self.assertIsNone(found)

	def test_own_till_in_a_closed_shift_blocks(self):
		found = self._run([self._closing("POS-CLO-230", "Nasr city")], ["Debtors - J", "Nasr city - J"])
		self.assertEqual(found["name"], "POS-CLO-230")

	def test_every_covering_shift_is_checked_not_just_the_first(self):
		rows = [self._closing("POS-CLO-232", "Dokki"), self._closing("POS-CLO-230", "Nasr city")]
		found = self._run(rows, ["Debtors - J", "Nasr city - J"])
		self.assertEqual(found["name"], "POS-CLO-230")

	def test_cancelled_voucher_with_no_live_gl_is_not_blocked(self):
		self.assertIsNone(self._run([self._closing("POS-CLO-230", "Nasr city")], []))

	@patch("jarz_pos.utils.access_control.find_closed_shift_covering")
	@patch("jarz_pos.utils.access_control._voucher_ledger_accounts", side_effect=Exception("db gone"))
	@patch("jarz_pos.utils.access_control.frappe")
	def test_unreadable_ledger_falls_back_to_the_stricter_window_rule(self, mock_frappe, _ledger, mock_window):
		"""Not knowing the accounts must never widen what may be cancelled."""
		from jarz_pos.utils import access_control

		mock_frappe.db.get_value.return_value = self.CREATED
		mock_window.return_value = {"name": "POS-CLO-232"}
		found = access_control.find_closed_shift_for_voucher("Payment Entry", "PE-0001")
		self.assertEqual(found["name"], "POS-CLO-232")
		mock_window.assert_called_once_with(self.CREATED)

	@patch("jarz_pos.utils.access_control.frappe")
	def test_closing_lookup_failure_fails_open(self, mock_frappe):
		from jarz_pos.utils import access_control

		mock_frappe.get_all.side_effect = Exception("db gone")
		self.assertIsNone(access_control.find_closed_shift_reconciling(self.CREATED, ["Nasr city - J"]))
		self.assertTrue(mock_frappe.log_error.called)


class TestCancellationEligibility(unittest.TestCase):
	"""`get_invoice_cancellation_eligibility` — what the client renders."""

	@staticmethod
	def _invoice(**overrides):
		data = {
			"name": "ACC-SINV-2026-00001",
			"docstatus": 1,
			"is_return": 0,
			"custom_sales_invoice_state": "In Progress",
			"outstanding_amount": 100.0,
			"grand_total": 100.0,
		}
		data.update(overrides)
		doc = MagicMock()
		doc.name = data["name"]
		doc.get.side_effect = lambda key, default=None: data.get(key, default)
		return doc

	@patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None)
	def test_unpaid_prep_state_can_cancel(self, _blocker):
		from jarz_pos.api.manager import get_invoice_cancellation_eligibility

		result = get_invoice_cancellation_eligibility(self._invoice())
		self.assertTrue(result["can_cancel"])
		self.assertIsNone(result["cancellation_block_code"])

	@patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None)
	def test_dispatched_is_blocked_and_suggests_return(self, _blocker):
		from jarz_pos.api.manager import get_invoice_cancellation_eligibility

		result = get_invoice_cancellation_eligibility(
			self._invoice(custom_sales_invoice_state="Out for Delivery")
		)
		self.assertFalse(result["can_cancel"])
		self.assertEqual(result["cancellation_block_code"], "already_dispatched")
		self.assertTrue(result["cancellation_suggests_return"])

	@patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None)
	def test_return_invoice_is_blocked_without_suggesting_return(self, _blocker):
		from jarz_pos.api.manager import get_invoice_cancellation_eligibility

		result = get_invoice_cancellation_eligibility(self._invoice(is_return=1))
		self.assertEqual(result["cancellation_block_code"], "return_invoice")
		self.assertFalse(result["cancellation_suggests_return"])

	@patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None)
	def test_partial_payment_is_blocked(self, _blocker):
		from jarz_pos.api.manager import get_invoice_cancellation_eligibility

		result = get_invoice_cancellation_eligibility(
			self._invoice(outstanding_amount=40.0, grand_total=100.0)
		)
		self.assertEqual(result["cancellation_block_code"], "partial_payment")
		self.assertTrue(result["cancellation_suggests_return"])

	@patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None)
	def test_draft_invoice_is_blocked(self, _blocker):
		from jarz_pos.api.manager import get_invoice_cancellation_eligibility

		result = get_invoice_cancellation_eligibility(self._invoice(docstatus=0))
		self.assertEqual(result["cancellation_block_code"], "invoice_not_submitted")

	@patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker")
	def test_mutation_blocker_is_surfaced_with_its_code(self, mock_blocker):
		from jarz_pos.api.manager import get_invoice_cancellation_eligibility

		mock_blocker.return_value = {
			"mutation_block_code": "delivery_note_exists",
			"mutation_block_reason": "already has a submitted Delivery Note",
			"delivery_notes": ["DN-0001"],
		}
		result = get_invoice_cancellation_eligibility(self._invoice())

		self.assertEqual(result["cancellation_block_code"], "delivery_note_exists")
		self.assertTrue(result["cancellation_suggests_return"])
		self.assertEqual(result["delivery_notes"], ["DN-0001"])


if __name__ == "__main__":
	unittest.main()

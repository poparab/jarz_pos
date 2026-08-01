"""Tests that settle_courier_for_invoice settles ONE invoice, not a whole courier.

Until 2026-08-01 this endpoint was named and documented per-invoice but delegated to
settle_delivery_party(), which settles EVERY unsettled Courier Transaction for the party.
Anyone calling it with an invoice name swept that courier's entire balance into a single
journal entry.

The assertion that matters in every case below is the same one: settle_delivery_party and
settle_courier — the two party-wide sweeps — are never reached. The routing assertions
(which single-invoice function, with which arguments) pin the replacement behaviour.
"""

import unittest
from unittest.mock import patch, MagicMock

import frappe


def _invoice(docstatus=1, kanban_profile="Main POS", pos_profile="Stale POS"):
	"""A Sales Invoice stub exposing just what the function reads."""
	inv = MagicMock()
	inv.docstatus = docstatus
	fields = {"custom_kanban_profile": kanban_profile, "pos_profile": pos_profile}
	inv.get.side_effect = lambda key, *a: fields.get(key)
	return inv


def _ct(party_type="Employee", party="EMP-001", amount=0.0, shipping=0.0, courier=None):
	return {
		"name": "CT-001",
		"courier": courier,
		"amount": amount,
		"shipping_amount": shipping,
		"party_type": party_type,
		"party": party,
	}


class TestSettleCourierForInvoiceScope(unittest.TestCase):
	"""settle_courier_for_invoice must never widen its scope past the named invoice."""

	def setUp(self):
		self.patches = []

		def start(target, **kwargs):
			p = patch(target, **kwargs)
			self.patches.append(p)
			return p.start()

		# Access guards are exercised in test_access_control.py; neutralise them here so
		# these tests stay focused on scope and routing.
		start("jarz_pos.utils.access_control.ensure_profile_scoped_invoice_access")
		start("jarz_pos.utils.access_control.ensure_open_shift_for_invoice")

		self.preview = start("jarz_pos.api.invoices.get_invoice_settlement_preview")
		self.collected = start(
			"jarz_pos.services.delivery_handling.settle_courier_collected_payment"
		)
		self.paid = start(
			"jarz_pos.services.delivery_handling.settle_single_invoice_paid"
		)

		# The two party-wide sweeps. These must never fire.
		self.sweep_party = start(
			"jarz_pos.services.delivery_handling.settle_delivery_party"
		)
		self.sweep_legacy = start("jarz_pos.services.delivery_handling.settle_courier")

		self.get_doc = start("frappe.get_doc")
		self.get_all = start("frappe.get_all")
		self.get_doc.return_value = _invoice()

	def tearDown(self):
		for p in reversed(self.patches):
			p.stop()

	def assertNoPartyWideSweep(self):
		self.sweep_party.assert_not_called()
		self.sweep_legacy.assert_not_called()

	def _call(self, invoice="ACC-SINV-0001", pos_profile=None):
		from jarz_pos.services.delivery_handling import settle_courier_for_invoice

		return settle_courier_for_invoice(invoice, pos_profile)

	def test_positive_net_routes_to_collected_payment(self):
		"""Courier is holding more than its freight -> record what it hands over."""
		self.get_all.return_value = [_ct(amount=500.0, shipping=50.0)]
		self.preview.return_value = {
			"net_amount": 450.0,
			"order_amount": 500.0,
			"shipping_amount": 50.0,
		}
		self.collected.return_value = {"success": True, "journal_entry": "JE-001"}

		result = self._call(pos_profile="Main POS")

		self.collected.assert_called_once_with(
			"ACC-SINV-0001", "Main POS", "Employee", "EMP-001"
		)
		self.paid.assert_not_called()
		self.assertNoPartyWideSweep()
		self.assertEqual(result["journal_entry"], "JE-001")

	def test_negative_net_routes_to_single_invoice_paid(self):
		"""Customer already paid -> branch pays the courier this invoice's freight."""
		self.get_all.return_value = [_ct(amount=0.0, shipping=50.0)]
		self.preview.return_value = {
			"net_amount": -50.0,
			"order_amount": 0.0,
			"shipping_amount": 50.0,
		}
		self.paid.return_value = {"success": True, "journal_entry": "JE-002"}

		result = self._call(pos_profile="Main POS")

		self.paid.assert_called_once_with(
			"ACC-SINV-0001", "Main POS", "Employee", "EMP-001"
		)
		self.collected.assert_not_called()
		self.assertNoPartyWideSweep()
		self.assertEqual(result["journal_entry"], "JE-002")

	def test_zero_net_posts_nothing(self):
		"""Nothing to move: no journal entry, and emphatically no sweep."""
		self.get_all.return_value = [_ct(amount=50.0, shipping=50.0)]
		self.preview.return_value = {
			"net_amount": 0.0,
			"order_amount": 50.0,
			"shipping_amount": 50.0,
		}

		result = self._call(pos_profile="Main POS")

		self.collected.assert_not_called()
		self.paid.assert_not_called()
		self.assertNoPartyWideSweep()
		self.assertFalse(result["settled"])
		self.assertEqual(result["reason"], "nothing_to_settle")
		self.assertIsNone(result["journal_entry"])

	def test_pos_profile_falls_back_to_invoice_operational_branch(self):
		"""Historic callers omitted pos_profile; derive it rather than throwing.

		custom_kanban_profile wins over pos_profile, which goes stale on branch transfer.
		"""
		self.get_all.return_value = [_ct(amount=0.0, shipping=50.0)]
		self.preview.return_value = {"net_amount": -50.0}
		self.paid.return_value = {"success": True}

		self._call(pos_profile=None)

		self.paid.assert_called_once_with(
			"ACC-SINV-0001", "Main POS", "Employee", "EMP-001"
		)
		self.assertNoPartyWideSweep()

	def test_legacy_label_only_rows_refuse_instead_of_sweeping(self):
		"""A row with no party can only be settled label-wide — refuse, don't widen."""
		self.get_all.return_value = [
			_ct(party_type=None, party=None, courier="Legacy Courier")
		]

		with self.assertRaises(frappe.ValidationError):
			self._call(pos_profile="Main POS")

		self.assertNoPartyWideSweep()
		self.collected.assert_not_called()
		self.paid.assert_not_called()

	def test_multiple_parties_refuse_rather_than_pick_the_first(self):
		"""The old code took cts[0] and settled that party's entire balance."""
		self.get_all.return_value = [
			_ct(party_type="Employee", party="EMP-001", shipping=50.0),
			_ct(party_type="Supplier", party="SUP-009", shipping=30.0),
		]

		with self.assertRaises(frappe.ValidationError):
			self._call(pos_profile="Main POS")

		self.assertNoPartyWideSweep()
		self.collected.assert_not_called()
		self.paid.assert_not_called()

	def test_no_unsettled_transactions_throws(self):
		self.get_all.return_value = []

		with self.assertRaises(frappe.ValidationError):
			self._call(pos_profile="Main POS")

		self.assertNoPartyWideSweep()

	def test_unsubmitted_invoice_throws(self):
		self.get_doc.return_value = _invoice(docstatus=0)

		with self.assertRaises(frappe.ValidationError):
			self._call(pos_profile="Main POS")

		self.assertNoPartyWideSweep()

	def test_courier_transactions_are_scoped_to_the_named_invoice(self):
		"""The CT query must filter on reference_invoice — that is the whole fix."""
		self.get_all.return_value = [_ct(amount=0.0, shipping=50.0)]
		self.preview.return_value = {"net_amount": -50.0}
		self.paid.return_value = {"success": True}

		self._call(pos_profile="Main POS")

		filters = self.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["reference_invoice"], "ACC-SINV-0001")
		self.assertEqual(filters["status"], ["!=", "Settled"])

	def test_preview_is_asked_about_this_invoice_and_party(self):
		"""Routing must be driven by the same preview the POS app uses."""
		self.get_all.return_value = [_ct(amount=500.0, shipping=50.0)]
		self.preview.return_value = {"net_amount": 450.0}
		self.collected.return_value = {"success": True}

		self._call(pos_profile="Main POS")

		self.preview.assert_called_once_with(
			"ACC-SINV-0001", party_type="Employee", party="EMP-001"
		)


if __name__ == "__main__":
	unittest.main()

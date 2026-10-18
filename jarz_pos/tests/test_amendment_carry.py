"""An amendment keeps the source order's approved pricing (order 19041).

Production incident, 2026-10-18: order 19041 / ``ACC-SINV-2026-18933`` was six free
cakes -- every line custom-priced to 0 (``[CUSTOM LINE PRICING]``) with a zero
shipping override. Amending it to swap one flavour produced
``ACC-SINV-2026-18933-1`` at EGP 800: the five kept lines went back to the 160 list
price. Three things the source carried on top of the price list never reached the
replacement:

* per-line custom prices -- neither the app's edit cart nor the server rebuild sent
  ``custom_rate_override``;
* header discounts (POS promos, Woo coupons) -- the job never passed ``promo_codes``;
* the ``[ZERO SHIPPING OVERRIDE]`` audit marker.

Everything here is mock-based and runs without a live site (CI logic gate).
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.services import amendment_carry as carry_mod
from jarz_pos.tests.test_amendment_price_list import (
	_PROFILE,
	_FakeSourceInvoice,
	_mock_manager_frappe,
)

_SOURCE = "ACC-SINV-2026-18933"


def _row(code, *, rate=0.0, qty=1, **extra):
	row = {"item_code": code, "qty": qty, "price_list_rate": rate, "rate": rate, "amount": rate * qty}
	row.update(extra)
	return row


def _free_cakes_source(**overrides):
	data = {
		"name": _SOURCE,
		"grand_total": 0.0,
		"selling_price_list": "Standard Selling",
		"custom_pos_audit_markers": "[ZERO SHIPPING OVERRIDE] [CUSTOM LINE PRICING]",
		"items": [_row("TIRAMISU-L"), _row("PISTACHIO-L"), _row("LOTUS-L")],
	}
	data.update(overrides)
	return data


class TestSourceLineRateOverrides(unittest.TestCase):
	def test_custom_priced_source_yields_its_line_rates(self):
		self.assertEqual(
			carry_mod.source_line_rate_overrides(_free_cakes_source()),
			{"TIRAMISU-L": 0.0, "PISTACHIO-L": 0.0, "LOTUS-L": 0.0},
		)

	def test_without_the_marker_the_list_price_is_not_an_override(self):
		src = _free_cakes_source(custom_pos_audit_markers="", items=[_row("LOTUS-L", rate=160)])
		self.assertEqual(carry_mod.source_line_rate_overrides(src), {})

	def test_legacy_remarks_marker_counts(self):
		src = _free_cakes_source(custom_pos_audit_markers="", remarks="x [CUSTOM LINE PRICING]")
		self.assertIn("LOTUS-L", carry_mod.source_line_rate_overrides(src))

	def test_item_at_two_prices_is_ambiguous_and_dropped(self):
		src = _free_cakes_source(items=[_row("LOTUS-L", rate=0), _row("LOTUS-L", rate=120), _row("TIRAMISU-L")])
		self.assertEqual(carry_mod.source_line_rate_overrides(src), {"TIRAMISU-L": 0.0})

	def test_bundle_and_free_rows_are_not_plain_lines(self):
		src = _free_cakes_source(
			items=[
				_row("BOX-PARENT", is_bundle_parent=1),
				_row("BOX-CHILD", parent_bundle="BOX-PARENT"),
				_row("GIFT", is_free_item=1),
				_row("LOTUS-L"),
			]
		)
		self.assertEqual(carry_mod.source_line_rate_overrides(src), {"LOTUS-L": 0.0})

	def test_bundle_code_row_is_not_a_plain_line(self):
		src = _free_cakes_source(items=[_row("BOX", bundle_code="BX-1"), _row("LOTUS-L")])
		self.assertEqual(carry_mod.source_line_rate_overrides(src), {"LOTUS-L": 0.0})

	def test_charged_row_without_a_list_price_is_not_carried_as_free(self):
		charged = _row("LOTUS-L")
		charged["rate"] = 140.0
		src = _free_cakes_source(items=[charged, _row("TIRAMISU-L")])
		self.assertEqual(carry_mod.source_line_rate_overrides(src), {"TIRAMISU-L": 0.0})

	def test_woo_customer_note_cannot_fake_the_marker(self):
		src = _free_cakes_source(custom_pos_audit_markers="", woo_order_id=19041, remarks="[CUSTOM LINE PRICING]")
		self.assertEqual(carry_mod.source_line_rate_overrides(src), {})

	def test_remarks_ignored_once_the_markers_field_is_in_use(self):
		src = _free_cakes_source(custom_pos_audit_markers="[PICKUP]", remarks="[CUSTOM LINE PRICING]")
		self.assertEqual(carry_mod.source_line_rate_overrides(src), {})


class TestApplySourceLinePricing(unittest.TestCase):
	def test_kept_lines_get_the_source_price(self):
		cart = [{"item_code": "LOTUS-L", "qty": 1, "rate": 160}, {"item_code": "STRAWBERRY-L", "qty": 1}]
		changed = carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 0.0}, lambda code: 160.0)
		self.assertEqual(changed, ["LOTUS-L"])
		self.assertEqual(cart[0]["custom_rate_override"], 0.0)
		self.assertNotIn("custom_rate_override", cart[1], "a newly added line is priced from the list")

	def test_operator_override_for_this_edit_wins(self):
		cart = [{"item_code": "LOTUS-L", "qty": 1, "custom_rate_override": 90}]
		self.assertEqual(carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 0.0}, lambda code: 160.0), [])
		self.assertEqual(cart[0]["custom_rate_override"], 90)

	def test_bundle_rows_untouched(self):
		cart = [{"item_code": "LOTUS-L", "is_bundle": True}]
		self.assertEqual(carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 0.0}, lambda code: 160.0), [])

	def test_rate_equal_to_todays_list_needs_no_override(self):
		cart = [{"item_code": "LOTUS-L", "qty": 1}]
		self.assertEqual(carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 160.0}, lambda code: 160.0), [])
		self.assertNotIn("custom_rate_override", cart[0])

	def test_resolver_failure_still_carries(self):
		def boom(code):
			raise RuntimeError("no price")

		cart = [{"item_code": "LOTUS-L", "qty": 1}]
		self.assertEqual(carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 0.0}, boom), ["LOTUS-L"])

	def test_only_the_source_quantity_inherits_the_price(self):
		cart = [{"item_code": "LOTUS-L", "qty": 3}]
		changed = carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 0.0}, lambda code: 160.0, {"LOTUS-L": 1})
		self.assertEqual(changed, ["LOTUS-L"])
		self.assertEqual(len(cart), 2, "the extra units are split onto their own line")
		self.assertEqual((cart[0]["qty"], cart[0]["custom_rate_override"]), (1, 0.0))
		self.assertEqual(cart[1]["qty"], 2)
		self.assertNotIn("custom_rate_override", cart[1], "extra units are priced from the list")

	def test_quantity_budget_spans_rows(self):
		cart = [{"item_code": "LOTUS-L", "qty": 2}, {"item_code": "LOTUS-L", "qty": 1}]
		carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 0.0}, lambda code: 160.0, {"LOTUS-L": 2})
		self.assertEqual(cart[0]["custom_rate_override"], 0.0)
		self.assertNotIn("custom_rate_override", cart[1])

	def test_quantity_key_is_kept(self):
		cart = [{"item_code": "LOTUS-L", "quantity": 2}]
		carry_mod.apply_source_line_pricing(cart, {"LOTUS-L": 0.0}, lambda code: 160.0, {"LOTUS-L": 1})
		self.assertEqual([r["quantity"] for r in cart], [1, 1])
		self.assertNotIn("qty", cart[1])


class TestTrustedCarry(unittest.TestCase):
	def _flags(self, source, carry):
		flags = SimpleNamespace(jarz_amendment_source=source, jarz_amendment_carry=carry)
		flags.get = lambda key, default=None: getattr(flags, key, default)
		return patch.object(carry_mod, "frappe", SimpleNamespace(flags=flags))

	def test_inside_the_job_for_the_named_source(self):
		carry = {"source": _SOURCE}
		with self._flags(_SOURCE, carry):
			self.assertIs(carry_mod.trusted_carry(_SOURCE), carry)

	def test_client_supplied_amended_from_alone_is_not_trusted(self):
		with self._flags(None, None):
			self.assertIsNone(carry_mod.trusted_carry(_SOURCE))

	def test_carry_for_another_source_is_rejected(self):
		with self._flags(_SOURCE, {"source": "ACC-SINV-2026-00001"}):
			self.assertIsNone(carry_mod.trusted_carry(_SOURCE))
		with self._flags("ACC-SINV-2026-00001", {"source": _SOURCE}):
			self.assertIsNone(carry_mod.trusted_carry(_SOURCE))


class TestStripCarriedPricingForGate(unittest.TestCase):
	_carry = {
		"line_rates": {"LOTUS-L": 0.0},
		"line_discounts": {"TIRAMISU-L": {"discount_percentage": 10.0}},
		"source_qty": {"LOTUS-L": 1.0, "TIRAMISU-L": 2.0},
	}

	def test_inherited_price_does_not_need_a_manager(self):
		out = carry_mod.strip_carried_pricing_for_gate(
			[{"item_code": "LOTUS-L", "qty": 1, "custom_rate_override": 0}], self._carry
		)
		self.assertNotIn("custom_rate_override", out[0])

	def test_inherited_line_discount_is_stripped(self):
		out = carry_mod.strip_carried_pricing_for_gate(
			[{"item_code": "TIRAMISU-L", "qty": 2, "discount_percentage": 10}], self._carry
		)
		self.assertNotIn("discount_percentage", out[0])

	def test_more_free_units_than_the_source_stays_gated(self):
		out = carry_mod.strip_carried_pricing_for_gate(
			[{"item_code": "LOTUS-L", "qty": 10, "custom_rate_override": 0}], self._carry
		)
		self.assertEqual(out[0]["custom_rate_override"], 0)

	def test_quantity_budget_is_shared_across_rows(self):
		out = carry_mod.strip_carried_pricing_for_gate(
			[
				{"item_code": "LOTUS-L", "qty": 1, "custom_rate_override": 0},
				{"item_code": "LOTUS-L", "qty": 1, "custom_rate_override": 0},
			],
			self._carry,
		)
		self.assertNotIn("custom_rate_override", out[0])
		self.assertEqual(out[1]["custom_rate_override"], 0)

	def test_a_different_price_is_a_new_decision(self):
		out = carry_mod.strip_carried_pricing_for_gate(
			[{"item_code": "LOTUS-L", "qty": 1, "custom_rate_override": 50}], self._carry
		)
		self.assertEqual(out[0]["custom_rate_override"], 50)

	def test_input_is_not_mutated_and_no_carry_is_a_noop(self):
		cart = [{"item_code": "LOTUS-L", "qty": 1, "custom_rate_override": 0}]
		carry_mod.strip_carried_pricing_for_gate(cart, self._carry)
		self.assertEqual(cart[0]["custom_rate_override"], 0)
		self.assertIs(carry_mod.strip_carried_pricing_for_gate(cart, None), cart)


class TestApplyHeaderDiscountCarry(unittest.TestCase):
	def _doc(self, *, discount=0.0, lines=(500.0,)):
		doc = SimpleNamespace(discount_amount=discount, items=[{"amount": a} for a in lines])
		doc.get = lambda key, default=None: getattr(doc, key, default)
		return doc

	@staticmethod
	def _eval(*accepted):
		return SimpleNamespace(results=[SimpleNamespace(accepted=a) for a in accepted])

	def test_every_code_accepted_lets_the_re_evaluation_stand(self):
		doc = self._doc(discount=40.0)
		carry = {"header_discount": 50.0}
		self.assertEqual(carry_mod.apply_header_discount_carry(doc, carry, self._eval(True), ["SAVE10"]), 0.0)
		self.assertEqual(doc.discount_amount, 40.0)

	def test_rejected_code_keeps_the_source_discount(self):
		doc = self._doc()
		carry = {"header_discount": 50.0, "woo_discount_total": 50.0}
		self.assertEqual(carry_mod.apply_header_discount_carry(doc, carry, self._eval(False), ["WOO50"]), 50.0)
		self.assertEqual(doc.discount_amount, 50.0)
		self.assertEqual(doc.apply_discount_on, "Grand Total")
		self.assertEqual(doc.custom_promo_applied, 1)
		self.assertEqual(doc.custom_promo_discount_total, 50.0)
		self.assertEqual(doc.custom_promo_woo_discount_total, 50.0)

	def test_codeless_woo_discount_is_kept(self):
		doc = self._doc()
		self.assertEqual(carry_mod.apply_header_discount_carry(doc, {"header_discount": 30.0}, None, []), 30.0)
		self.assertEqual(doc.discount_amount, 30.0)
		self.assertFalse(hasattr(doc, "custom_promo_applied"))

	def test_capped_at_the_new_line_total(self):
		doc = self._doc(lines=(20.0,))
		self.assertEqual(carry_mod.apply_header_discount_carry(doc, {"header_discount": 50.0}, None, []), 20.0)

	def test_no_source_discount_or_no_carry_changes_nothing(self):
		doc = self._doc()
		self.assertEqual(carry_mod.apply_header_discount_carry(doc, {"header_discount": 0}, None, []), 0.0)
		self.assertEqual(carry_mod.apply_header_discount_carry(doc, None, None, []), 0.0)
		self.assertEqual(doc.discount_amount, 0.0)


class TestBuildAmendmentCarry(unittest.TestCase):
	def test_zero_shipping_marker_only_when_the_edit_keeps_it_zero(self):
		src = _free_cakes_source()
		self.assertTrue(carry_mod.build_amendment_carry(src, zero_shipping=True)["zero_shipping_marker"])
		self.assertFalse(carry_mod.build_amendment_carry(src, zero_shipping=False)["zero_shipping_marker"])

	def test_promo_codes_and_header_discount_are_read(self):
		src = _free_cakes_source(custom_promo_codes='["SAVE10"]', discount_amount=25)
		carry = carry_mod.build_amendment_carry(src, zero_shipping=False)
		self.assertEqual(carry["promo_codes"], ["SAVE10"])
		self.assertEqual(carry["header_discount"], 25.0)
		self.assertEqual(carry["source"], _SOURCE)


class TestAmendmentJobCarriesSourcePricing(unittest.TestCase):
	"""The job itself: the 19041 cart must reach creation with the free prices kept."""

	def _run(self, source, cart, *, expect_creation=True, price_list_resolved=None, **job_kwargs):
		creation = MagicMock(return_value={"invoice_name": _SOURCE + "-1"})
		mock_frappe = _mock_manager_frappe()
		with (
			patch("jarz_pos.api.manager.frappe", mock_frappe),
			patch("jarz_pos.api.manager._create_amendment_invoice", creation),
			patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
			patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
			patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
			patch("jarz_pos.api.manager.frappe.get_doc", return_value=source),
			patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
			patch("jarz_pos.api.manager.resolve_order_territory", return_value=None),
			patch("jarz_pos.api.manager.read_invoice_shipping_income", return_value=0.0),
			patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
			patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
			patch("jarz_pos.api.manager._carry_over_invoice_notes", return_value=None),
			patch("jarz_pos.api.manager._build_invoice_amendment_response", return_value={"success": True}),
			patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
			patch("jarz_pos.services.invoice_creation._resolve_item_rate", return_value=160.0),
			patch(
				"jarz_pos.api.manager._resolve_amendment_price_list",
				return_value=price_list_resolved or source.get("selling_price_list"),
			),
		):
			from jarz_pos.api.manager import _run_invoice_amendment_job

			result = _run_invoice_amendment_job(
				invoice_id=source.name,
				request_id="test-req-carry",
				cart_json=json.dumps(cart),
				pos_profile_name=_PROFILE,
				**job_kwargs,
			)
		if not expect_creation:
			self.assertFalse(creation.called)
			return result
		self.assertTrue(creation.called, f"the job never reached invoice creation: {result}")
		return creation

	def _source(self, **overrides):
		data = _free_cakes_source(**overrides)
		data.update(custom_order_purpose=None, selling_price_list="Standard Selling")
		return _FakeSourceInvoice(**data)

	def test_kept_free_lines_stay_free(self):
		cart = [{"item_code": "PISTACHIO-L", "qty": 1, "rate": 160}, {"item_code": "STRAWBERRY-L", "qty": 1}]
		creation = self._run(self._source(), cart)
		sent = json.loads(creation.call_args.args[0])
		self.assertEqual(sent[0].get("custom_rate_override"), 0.0)
		self.assertNotIn("custom_rate_override", sent[1])

	def test_newer_app_opting_out_is_respected(self):
		cart = [{"item_code": "PISTACHIO-L", "qty": 1, "rate": 160}]
		creation = self._run(self._source(), cart, keep_source_line_pricing=0)
		self.assertNotIn("custom_rate_override", json.loads(creation.call_args.args[0])[0])

	def test_source_promo_codes_are_passed_to_creation(self):
		src = self._source(custom_pos_audit_markers="", custom_promo_codes='["SAVE10"]', grand_total=0.0)
		creation = self._run(src, [{"item_code": "PISTACHIO-L", "qty": 1, "rate": 160}])
		self.assertEqual(json.loads(creation.call_args.kwargs["promo_codes"]), ["SAVE10"])

	def test_no_codes_passes_none(self):
		creation = self._run(self._source(), [{"item_code": "PISTACHIO-L", "qty": 1, "rate": 160}])
		self.assertIsNone(creation.call_args.kwargs["promo_codes"])

	def test_extra_units_are_not_given_away(self):
		creation = self._run(self._source(), [{"item_code": "PISTACHIO-L", "qty": 3}])
		sent = json.loads(creation.call_args.args[0])
		self.assertEqual([(r["qty"], r.get("custom_rate_override")) for r in sent], [(1, 0.0), (2, None)])

	def test_a_newly_chosen_price_list_is_not_overruled(self):
		creation = self._run(self._source(), [{"item_code": "PISTACHIO-L", "qty": 1}], price_list_resolved="B2B Selling")
		self.assertNotIn("custom_rate_override", json.loads(creation.call_args.args[0])[0])

	def test_free_order_with_paid_shipping_is_not_suspicious(self):
		# Lines free, EGP 50 shipping: the carried lines are worth 0 but nothing was lost.
		self._run(self._source(grand_total=50.0), [{"item_code": "PISTACHIO-L", "qty": 1}])

	def test_manager_can_bring_a_mis_priced_replacement_back_down(self):
		# The 19041 repair: 18933-1 at 800 amended back to every line free.
		src = self._source(name=_SOURCE + "-1", custom_pos_audit_markers="", grand_total=800.0)
		cart = [{"item_code": code, "qty": 1, "rate": 0, "custom_rate_override": 0} for code in "ABCDE"]
		creation = self._run(src, cart)
		self.assertEqual([r["custom_rate_override"] for r in json.loads(creation.call_args.args[0])], [0] * 5)

	def test_a_cart_that_lost_lines_is_still_refused(self):
		src = self._source(custom_pos_audit_markers="", grand_total=800.0)
		result = self._run(src, [{"item_code": "PISTACHIO-L", "qty": 1}], expect_creation=False)
		self.assertEqual(result.get("amendment_block_code"), "suspicious_diff")


if __name__ == "__main__":
	unittest.main()

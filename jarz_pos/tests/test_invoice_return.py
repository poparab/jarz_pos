"""Tests for the post-dispatch return workflow.

The invariants worth locking down are the ones that are expensive to discover in
production:

* the source invoice is never voided — a return only ever adds documents;
* every Journal Entry balances, and none can be posted to a party account
  without a party (ERPNext v16 rejects that outright);
* the freight reversal appears if and only if the operator declined to pay the
  courier for the trip;
* dedup keys are derived from the credit note, so a *second* partial return
  posts its own entries instead of silently matching the first one's.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.services import invoice_return as ir


def _invoice(**overrides):
    data = {
        "name": "ACC-SINV-2026-00001",
        "docstatus": 1,
        "is_return": 0,
        "custom_sales_invoice_state": "Out for Delivery",
        "custom_was_out_for_delivery": 1,
        "outstanding_amount": 0.0,
        "grand_total": 500.0,
        "custom_return_status": None,
        "custom_is_pickup": 0,
        "sales_partner": None,
    }
    data.update(overrides)
    doc = MagicMock()
    doc.name = data["name"]
    doc.company = "JARZ"
    doc.customer = "CUST-0001"
    doc.get.side_effect = lambda key, default=None: data.get(key, default)
    return doc


class TestLineParsing(unittest.TestCase):
    def test_parses_json_string(self):
        parsed = ir._parse_lines('[{"si_detail": "row-1", "qty": 2}]')
        self.assertEqual(parsed, [{"si_detail": "row-1", "qty": 2.0}])

    def test_parses_list_of_dicts(self):
        parsed = ir._parse_lines([{"si_detail": "row-1", "qty": 3}])
        self.assertEqual(parsed, [{"si_detail": "row-1", "qty": 3.0}])

    def test_drops_zero_and_negative_quantities(self):
        parsed = ir._parse_lines(
            [{"si_detail": "a", "qty": 0}, {"si_detail": "b", "qty": -1}, {"si_detail": "c", "qty": 1}]
        )
        self.assertEqual([row["si_detail"] for row in parsed], ["c"])

    def test_malformed_input_is_empty_not_an_error(self):
        self.assertEqual(ir._parse_lines("not json"), [])
        self.assertEqual(ir._parse_lines(None), [])
        self.assertEqual(ir._parse_lines([1, 2, 3]), [])


class TestMoneyState(unittest.TestCase):
    @patch.object(ir, "_unsettled_courier_transactions", return_value=[])
    def test_unpaid_when_outstanding_equals_total(self, _txns):
        self.assertEqual(
            ir._money_state(_invoice(outstanding_amount=500.0, grand_total=500.0)),
            ir.MONEY_UNPAID,
        )

    @patch.object(ir, "_unsettled_courier_transactions", return_value=[])
    def test_prepaid_when_settled_with_no_courier_transaction(self, _txns):
        self.assertEqual(
            ir._money_state(_invoice(outstanding_amount=0.0, grand_total=500.0)),
            ir.MONEY_PREPAID,
        )

    @patch.object(ir, "_unsettled_courier_transactions")
    def test_courier_outstanding_beats_prepaid(self, txns):
        """An invoice whose receivable moved to Courier Outstanding reads as
        'paid' by outstanding_amount alone — the courier row must win."""
        txns.return_value = [{"amount": 500.0, "status": "Unsettled"}]
        self.assertEqual(
            ir._money_state(_invoice(outstanding_amount=0.0, grand_total=500.0)),
            ir.MONEY_COURIER_OUTSTANDING,
        )

    @patch.object(ir, "_unsettled_courier_transactions")
    def test_courier_settled_is_distinct(self, txns):
        txns.return_value = [{"amount": 500.0, "status": "Settled"}]
        self.assertEqual(
            ir._money_state(_invoice(outstanding_amount=0.0, grand_total=500.0)),
            ir.MONEY_COURIER_SETTLED,
        )


@patch.object(ir, "get_freight_expense_account", return_value="Freight - J")
@patch.object(ir, "get_creditors_account", return_value="Creditors - J")
@patch.object(ir, "_get_courier_outstanding_account", return_value="Courier Outstanding - J")
@patch.object(ir, "get_company_receivable_account", return_value="Debtors - J")
class TestJournalLegPlanning(unittest.TestCase):
    """Each planned entry must balance, and only the right ones must appear."""

    @staticmethod
    def _balanced(plan):
        debit = sum(l["amount"] for l in plan["legs"] if l.get("debit"))
        credit = sum(l["amount"] for l in plan["legs"] if not l.get("debit"))
        return abs(debit - credit) < 0.005

    def _plan(self, money_state, *, pay_courier=True, courier=None):
        return ir.plan_return_journal_legs(
            inv=_invoice(),
            money_state=money_state,
            return_total=500.0,
            credit_note_name="ACC-SINV-2026-00099",
            pay_courier_for_trip=pay_courier,
            courier=courier,
        )

    def test_unpaid_posts_a_balanced_ar_knockoff(self, *_):
        plans = self._plan(ir.MONEY_UNPAID)
        self.assertEqual([p["je_type"] for p in plans], [ir.JE_AR_KNOCKOFF])
        self.assertTrue(self._balanced(plans[0]))

    def test_ar_knockoff_references_both_vouchers(self, *_):
        """Both outstandings must land on zero, which needs one reference each."""
        legs = self._plan(ir.MONEY_UNPAID)[0]["legs"]
        refs = {l.get("reference_name") for l in legs}
        self.assertEqual(refs, {"ACC-SINV-2026-00001", "ACC-SINV-2026-00099"})
        for leg in legs:
            self.assertEqual(leg["party_type"], "Customer")

    def test_courier_outstanding_releases_the_courier(self, *_):
        plans = self._plan(ir.MONEY_COURIER_OUTSTANDING)
        self.assertEqual([p["je_type"] for p in plans], [ir.JE_COURIER_OUTSTANDING])
        self.assertTrue(self._balanced(plans[0]))
        accounts = {l["account"] for l in plans[0]["legs"]}
        self.assertIn("Courier Outstanding - J", accounts)

    def test_prepaid_posts_no_reversal(self, *_):
        """The money is already in the branch; the refund handles it."""
        self.assertEqual(self._plan(ir.MONEY_PREPAID), [])

    def test_courier_settled_posts_no_reversal(self, *_):
        self.assertEqual(self._plan(ir.MONEY_COURIER_SETTLED), [])

    def test_freight_reversed_only_when_courier_is_not_paid(self, *_):
        courier = {"party_type": "Supplier", "party": "SUP-1", "shipping_amount": 40.0}

        paid = self._plan(ir.MONEY_PREPAID, pay_courier=True, courier=courier)
        self.assertEqual(paid, [])

        unpaid = self._plan(ir.MONEY_PREPAID, pay_courier=False, courier=courier)
        self.assertEqual([p["je_type"] for p in unpaid], [ir.JE_FREIGHT_REVERSAL])
        self.assertTrue(self._balanced(unpaid[0]))

    def test_freight_reversal_carries_the_courier_party(self, *_):
        """v16 rejects a Creditors line with no party."""
        courier = {"party_type": "Supplier", "party": "SUP-1", "shipping_amount": 40.0}
        legs = self._plan(ir.MONEY_PREPAID, pay_courier=False, courier=courier)[0]["legs"]
        creditors = next(l for l in legs if l["account"] == "Creditors - J")
        self.assertEqual(creditors["party_type"], "Supplier")
        self.assertEqual(creditors["party"], "SUP-1")

    def test_zero_shipping_reverses_nothing(self, *_):
        courier = {"party_type": "Supplier", "party": "SUP-1", "shipping_amount": 0.0}
        self.assertEqual(self._plan(ir.MONEY_PREPAID, pay_courier=False, courier=courier), [])


class TestPostReturnJe(unittest.TestCase):
    """The single posting door enforcing the v16 rules."""

    @patch.object(ir, "_find_existing_je_by_tag", return_value="JE-EXISTING")
    def test_reuses_an_existing_tagged_entry(self, _find):
        result = ir._post_return_je(
            company="JARZ", dedup_key="CN-1", je_type=ir.JE_AR_KNOCKOFF,
            human="x", legs=[{"account": "A", "amount": 10, "debit": True}],
        )
        self.assertEqual(result, "JE-EXISTING")

    @patch.object(ir, "_find_existing_je_by_tag", return_value=None)
    def test_immaterial_legs_post_nothing(self, _find):
        result = ir._post_return_je(
            company="JARZ", dedup_key="CN-1", je_type=ir.JE_AR_KNOCKOFF,
            human="x", legs=[{"account": "A", "amount": 0.0001, "debit": True}],
        )
        self.assertIsNone(result)

    @patch.object(ir, "validate_account_exists")
    @patch.object(ir, "_find_existing_je_by_tag", return_value=None)
    @patch.object(ir, "frappe")
    def test_party_account_without_a_party_is_refused(self, mock_frappe, _find, _validate):
        mock_frappe.new_doc.return_value = MagicMock()
        mock_frappe.db.get_value.return_value = "Receivable"
        mock_frappe.throw.side_effect = RuntimeError("no party")

        with self.assertRaises(RuntimeError):
            ir._post_return_je(
                company="JARZ", dedup_key="CN-1", je_type=ir.JE_AR_KNOCKOFF,
                human="x", legs=[{"account": "Debtors - J", "amount": 10, "debit": True}],
            )

    @patch.object(ir, "validate_account_exists")
    @patch.object(ir, "_find_existing_je_by_tag", return_value=None)
    @patch.object(ir, "frappe")
    def test_unbalanced_legs_are_refused(self, mock_frappe, _find, _validate):
        mock_frappe.new_doc.return_value = MagicMock()
        mock_frappe.db.get_value.return_value = "Income"
        mock_frappe.throw.side_effect = RuntimeError("unbalanced")

        with self.assertRaises(RuntimeError):
            ir._post_return_je(
                company="JARZ", dedup_key="CN-1", je_type=ir.JE_AR_KNOCKOFF, human="x",
                legs=[
                    {"account": "A", "amount": 10, "debit": True},
                    {"account": "B", "amount": 7, "debit": False},
                ],
            )

    @patch.object(ir, "validate_account_exists")
    @patch.object(ir, "_find_existing_je_by_tag", return_value=None)
    @patch.object(ir, "frappe")
    def test_voucher_type_is_journal_entry_never_bank_entry(self, mock_frappe, _find, _validate):
        """'Bank Entry' trips validate_cheque_info and fails deterministically."""
        je = MagicMock()
        mock_frappe.new_doc.return_value = je
        mock_frappe.db.get_value.return_value = "Income"

        ir._post_return_je(
            company="JARZ", dedup_key="CN-1", je_type=ir.JE_AR_KNOCKOFF, human="x",
            legs=[
                {"account": "A", "amount": 10, "debit": True},
                {"account": "B", "amount": 10, "debit": False},
            ],
        )
        self.assertEqual(je.voucher_type, "Journal Entry")

    @patch.object(ir, "validate_account_exists")
    @patch.object(ir, "_find_existing_je_by_tag", return_value=None)
    @patch.object(ir, "frappe")
    def test_dedup_tag_keys_on_the_credit_note(self, mock_frappe, mock_find, _validate):
        """A second partial return must not match the first return's entry."""
        je = MagicMock()
        mock_frappe.new_doc.return_value = je
        mock_frappe.db.get_value.return_value = "Income"

        ir._post_return_je(
            company="JARZ", dedup_key="CN-SECOND", je_type=ir.JE_AR_KNOCKOFF, human="x",
            legs=[
                {"account": "A", "amount": 10, "debit": True},
                {"account": "B", "amount": 10, "debit": False},
            ],
        )
        mock_find.assert_called_with("JARZ", "CN-SECOND", ir.JE_AR_KNOCKOFF)
        self.assertIn("CN-SECOND", je.user_remark)

    @patch.object(ir, "validate_account_exists")
    @patch.object(ir, "_find_existing_je_by_tag", return_value=None)
    @patch.object(ir, "frappe")
    def test_negative_amount_flips_the_side(self, mock_frappe, _find, _validate):
        je = MagicMock()
        rows = []
        je.append.side_effect = lambda _table, row: rows.append(row)
        mock_frappe.new_doc.return_value = je
        mock_frappe.db.get_value.return_value = "Income"

        ir._post_return_je(
            company="JARZ", dedup_key="CN-1", je_type=ir.JE_AR_KNOCKOFF, human="x",
            legs=[
                {"account": "A", "amount": -10, "debit": True},
                {"account": "B", "amount": 10, "debit": True},
            ],
        )
        # The negative debit becomes a credit, so the entry balances.
        self.assertEqual(rows[0]["credit_in_account_currency"], 10)
        self.assertEqual(rows[1]["debit_in_account_currency"], 10)


class TestReturnEligibility(unittest.TestCase):
    @patch.object(ir, "returns_enabled", return_value=False)
    def test_kill_switch_blocks_everything(self, _enabled):
        result = ir.get_invoice_return_eligibility(_invoice())
        self.assertFalse(result["can_return"])
        self.assertEqual(result["return_block_code"], "returns_disabled")

    @patch.object(ir, "_original_delivery_note", return_value="DN-1")
    @patch.object(ir, "returns_enabled", return_value=True)
    def test_dispatched_invoice_is_returnable(self, _enabled, _dn):
        self.assertTrue(ir.get_invoice_return_eligibility(_invoice())["can_return"])

    @patch.object(ir, "_original_delivery_note", return_value="DN-1")
    @patch.object(ir, "returns_enabled", return_value=True)
    def test_undispatched_invoice_is_not_returnable(self, _enabled, _dn):
        result = ir.get_invoice_return_eligibility(
            _invoice(custom_sales_invoice_state="Ready", custom_was_out_for_delivery=0)
        )
        self.assertEqual(result["return_block_code"], "not_dispatched")

    @patch.object(ir, "_original_delivery_note", return_value="DN-1")
    @patch.object(ir, "returns_enabled", return_value=True)
    def test_a_credit_note_cannot_be_returned(self, _enabled, _dn):
        result = ir.get_invoice_return_eligibility(_invoice(is_return=1))
        self.assertEqual(result["return_block_code"], "return_invoice")

    @patch.object(ir, "_original_delivery_note", return_value="DN-1")
    @patch.object(ir, "returns_enabled", return_value=True)
    def test_fully_returned_invoice_is_blocked(self, _enabled, _dn):
        result = ir.get_invoice_return_eligibility(
            _invoice(custom_return_status="Fully Returned")
        )
        self.assertEqual(result["return_block_code"], "already_returned")

    @patch.object(ir, "_original_delivery_note", return_value=None)
    @patch.object(ir, "returns_enabled", return_value=True)
    def test_missing_delivery_note_is_an_explicit_blocker(self, _enabled, _dn):
        """Legacy orders must fail up front, not halfway through the graph."""
        result = ir.get_invoice_return_eligibility(_invoice())
        self.assertEqual(result["return_block_code"], "original_delivery_note_missing")


class TestSubmitValidation(unittest.TestCase):
    """Argument validation happens before anything is written."""

    @patch.object(ir, "returns_enabled", return_value=True)
    def test_unknown_refund_mode_is_refused(self, _enabled):
        result = ir.run_invoice_return(
            invoice_id="INV-1", lines=[{"si_detail": "r", "qty": 1}],
            reason="x", refund_mode="teleport",
        )
        self.assertFalse(result["success"])

    @patch.object(ir, "returns_enabled", return_value=True)
    def test_unknown_return_type_is_refused(self, _enabled):
        result = ir.run_invoice_return(
            invoice_id="INV-1", lines=[{"si_detail": "r", "qty": 1}],
            reason="x", return_type="Vibes",
        )
        self.assertFalse(result["success"])

    @patch.object(ir, "returns_enabled", return_value=True)
    def test_reason_is_required(self, _enabled):
        result = ir.run_invoice_return(
            invoice_id="INV-1", lines=[{"si_detail": "r", "qty": 1}], reason="  ",
        )
        self.assertFalse(result["success"])

    @patch.object(ir, "returns_enabled", return_value=True)
    def test_at_least_one_line_is_required(self, _enabled):
        result = ir.run_invoice_return(invoice_id="INV-1", lines=[], reason="x")
        self.assertFalse(result["success"])

    @patch.object(ir, "returns_enabled", return_value=False)
    def test_kill_switch_refuses_before_any_work(self, _enabled):
        result = ir.run_invoice_return(
            invoice_id="INV-1", lines=[{"si_detail": "r", "qty": 1}], reason="x",
        )
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()

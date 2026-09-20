"""An amendment must carry a prepaid order's payment across, or refuse.

The POS amendment flow cancels the source invoice, which forces it to cancel any
submitted Payment Entry allocated to it first. For a cash-on-delivery order there
is nothing to cancel. For a **prepaid** order (Kashier, a gateway, any settled
Payment Entry) cancelling without re-booking turns money the business already
holds into an open receivable — and nothing downstream repaid it, because
``create_pos_invoice`` only books a Payment Entry for an Employee counter-paid
order and for an online *sales partner* order.

These are pure-Python unit tests in the style of ``test_amendment_hardening``:
no running Frappe instance, every lookup mocked.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_invoice(name="ACC-SINV-TEST-900", grand_total=670.0, outstanding=0.0, **extra):
    inv = SimpleNamespace(
        name=name,
        docstatus=1,
        grand_total=grand_total,
        outstanding_amount=outstanding,
        is_return=0,
        custom_sales_invoice_state="Received",
        customer="Heba",
        items=[],
    )
    inv.__dict__.update(extra)
    inv.get = lambda key, default=None: inv.__dict__.get(key, default)
    return inv


def _pe_row(name="ACC-PAY-1", paid_amount=670.0, **extra):
    row = {
        "name": name,
        "paid_amount": paid_amount,
        "unallocated_amount": 0.0,
        "clearance_date": None,
        "paid_to": "kashier - J",
        "paid_from": "Debtors - J",
        "mode_of_payment": None,
        "party": "Heba",
        "party_type": "Customer",
        "company": "JARZ",
        "reference_no": "WOO-ACC-SINV-TEST-900",
        "reference_date": "2026-09-19",
        "posting_date": "2026-09-19",
    }
    row.update(extra)
    return row


def _alloc_row(parent="ACC-PAY-1", reference_name="ACC-SINV-TEST-900", allocated_amount=670.0):
    return {
        "parent": parent,
        "reference_doctype": "Sales Invoice",
        "reference_name": reference_name,
        "allocated_amount": allocated_amount,
    }


class TestEvaluateAmendmentPaymentMigration(unittest.TestCase):
    """The classifier that decides whether a paid order may be amended at all."""

    def _run(self, inv, pe_names, pe_rows, alloc_rows):
        def _get_all(doctype, **kwargs):
            if doctype == "Payment Entry":
                return pe_rows
            if doctype == "Payment Entry Reference":
                return alloc_rows
            return []

        with (
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=pe_names),
            patch("jarz_pos.api.manager.frappe.get_all", side_effect=_get_all),
        ):
            from jarz_pos.api.manager import evaluate_amendment_payment_migration

            return evaluate_amendment_payment_migration(inv)

    def test_order_with_no_payment_entry_has_nothing_to_migrate(self):
        """A COD order: the amendment cancels no payment, so there is none to carry."""
        out = self._run(_make_invoice(outstanding=670.0), [], [], [])
        self.assertFalse(out["has_payment"])
        self.assertTrue(out["can_migrate"])
        self.assertEqual(out["payment_entry_rows"], [])

    def test_invoice_with_no_outstanding_field_is_not_treated_as_paid(self):
        """The regression that reddened test_amendment_price_list.

        `outstanding_amount` reads 0 when an order was paid AND when nothing ever
        populated the field. Inferring "paid" from it, then REFUSING on that
        inference, made an ordinary B2B amendment permanently impossible. Only a
        real Payment Entry may drive this verdict.
        """
        inv = _make_invoice()
        del inv.__dict__["outstanding_amount"]
        out = self._run(inv, [], [], [])
        self.assertTrue(out["can_migrate"], out)
        self.assertIsNone(out["block_code"])

    def test_simple_gateway_payment_is_migratable(self):
        out = self._run(_make_invoice(), ["ACC-PAY-1"], [_pe_row()], [_alloc_row()])
        self.assertTrue(out["has_payment"])
        self.assertTrue(out["can_migrate"], out)
        self.assertEqual(out["payment_entries"], ["ACC-PAY-1"])
        self.assertAlmostEqual(out["allocated_total"], 670.0)
        self.assertEqual(out["payment_entry_rows"][0]["paid_to"], "kashier - J")

    def test_payment_shared_with_another_invoice_is_refused(self):
        """Re-issuing it would strip the other invoice's payment too."""
        out = self._run(
            _make_invoice(),
            ["ACC-PAY-1"],
            [_pe_row()],
            [_alloc_row(allocated_amount=400.0), _alloc_row(reference_name="ACC-SINV-OTHER", allocated_amount=270.0)],
        )
        self.assertFalse(out["can_migrate"])
        self.assertEqual(out["block_code"], "paid_amendment_non_simple_payment")

    def test_bank_reconciled_payment_is_refused(self):
        out = self._run(
            _make_invoice(), ["ACC-PAY-1"], [_pe_row(clearance_date="2026-09-20")], [_alloc_row()]
        )
        self.assertFalse(out["can_migrate"])
        self.assertEqual(out["block_code"], "paid_amendment_reconciled_payment")

    def test_unallocated_balance_is_refused(self):
        out = self._run(
            _make_invoice(), ["ACC-PAY-1"], [_pe_row(unallocated_amount=50.0)], [_alloc_row()]
        )
        self.assertFalse(out["can_migrate"])
        self.assertEqual(out["block_code"], "paid_amendment_unallocated_payment")

    def test_partial_payment_is_migrated_not_refused(self):
        """The quietest money loss today: refusing here would preserve the bug."""
        out = self._run(
            _make_invoice(outstanding=270.0),
            ["ACC-PAY-1"],
            [_pe_row(paid_amount=400.0)],
            [_alloc_row(allocated_amount=400.0)],
        )
        self.assertTrue(out["has_payment"])
        self.assertTrue(out["can_migrate"], out)
        self.assertAlmostEqual(out["allocated_total"], 400.0)


class TestRebookAmendmentPayment(unittest.TestCase):
    """The replacement's Payment Entry is rebuilt from the cancelled one's shape."""

    def _run(self, outstanding, source_rows):
        replacement = SimpleNamespace(
            name="ACC-SINV-TEST-900-1",
            outstanding_amount=outstanding,
            posting_date="2026-09-20",
            company="JARZ",
            customer="Heba",
        )
        created = []

        def _new_doc(doctype):
            pe = MagicMock()
            pe.references = []
            pe.append.side_effect = lambda field, row: pe.references.append(row)
            pe.name = "ACC-PAY-NEW-%d" % (len(created) + 1)
            created.append(pe)
            return pe

        with (
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=replacement),
            patch("jarz_pos.api.manager.frappe.new_doc", side_effect=_new_doc),
        ):
            from jarz_pos.api.manager import _rebook_amendment_payment

            out = _rebook_amendment_payment(
                replacement_invoice_name="ACC-SINV-TEST-900-1",
                source_payment_rows=source_rows,
                logger=MagicMock(),
            )
        return out, created

    def test_equal_value_swap_reissues_the_same_payment(self):
        """The 17529 case: same total, so the replacement comes back fully paid."""
        out, created = self._run(670.0, [_pe_row()])
        self.assertEqual(out["payment_entries"], ["ACC-PAY-NEW-1"])
        self.assertAlmostEqual(out["allocated"], 670.0)
        self.assertAlmostEqual(out["unallocated"], 0.0)
        pe = created[0]
        # Rebuilt from the source entry, NOT from a payment-method lookup.
        self.assertEqual(pe.paid_to, "kashier - J")
        self.assertEqual(pe.paid_from, "Debtors - J")
        self.assertEqual(pe.payment_type, "Receive")
        self.assertEqual(pe.paid_amount, 670.0)
        self.assertEqual(pe.posting_date, "2026-09-20")
        self.assertEqual(pe.reference_no, "WOO-ACC-SINV-TEST-900-1")
        self.assertEqual(pe.references[0]["allocated_amount"], 670.0)
        pe.submit.assert_called_once()

    def test_more_expensive_replacement_leaves_a_real_balance(self):
        """Customer owes the difference; we must not invent the extra money."""
        out, created = self._run(770.0, [_pe_row()])
        self.assertAlmostEqual(out["allocated"], 670.0)
        self.assertAlmostEqual(out["unallocated"], 0.0)
        self.assertEqual(created[0].paid_amount, 670.0)

    def test_cheaper_replacement_keeps_the_difference_as_a_credit(self):
        """Customer is owed the difference; it stays on the entry, not written off."""
        out, created = self._run(570.0, [_pe_row()])
        self.assertAlmostEqual(out["allocated"], 570.0)
        self.assertAlmostEqual(out["unallocated"], 100.0)
        self.assertEqual(created[0].paid_amount, 670.0)

    def test_already_settled_replacement_books_nothing(self):
        out, created = self._run(0.0, [_pe_row()])
        self.assertEqual(out["payment_entries"], [])
        self.assertEqual(created, [])

    def test_reference_without_woo_prefix_is_preserved(self):
        _, created = self._run(670.0, [_pe_row(reference_no="INSTAPAY-4411")])
        self.assertEqual(created[0].reference_no, "INSTAPAY-4411")


class TestAmendmentEligibilityBlocksUnmigratablePayment(unittest.TestCase):
    """The Kanban card must be able to say WHY a paid order cannot be edited."""

    def _eligibility(self, migration):
        inv = _make_invoice()
        with (
            patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None),
            patch(
                "jarz_pos.api.manager.evaluate_amendment_payment_migration",
                **migration,
            ),
        ):
            from jarz_pos.api.manager import get_invoice_amendment_eligibility

            return get_invoice_amendment_eligibility(inv)

    def test_blocks_when_payment_cannot_be_migrated(self):
        out = self._eligibility(
            {
                "return_value": {
                    "can_migrate": False,
                    "block_code": "paid_amendment_reconciled_payment",
                    "block_reason": "already bank-reconciled",
                    "payment_entries": ["ACC-PAY-1"],
                }
            }
        )
        self.assertFalse(out["can_amend"])
        self.assertEqual(out["amendment_block_code"], "paid_amendment_reconciled_payment")
        self.assertEqual(out["payment_entries"], ["ACC-PAY-1"])

    def test_allows_when_payment_can_be_migrated(self):
        out = self._eligibility({"return_value": {"can_migrate": True, "is_paid": True}})
        self.assertTrue(out["can_amend"])

    def test_lookup_failure_fails_open(self):
        """A broken lookup must not make every card unamendable."""
        with patch("jarz_pos.api.manager.frappe.log_error"):
            out = self._eligibility({"side_effect": RuntimeError("db down")})
        self.assertTrue(out["can_amend"])


if __name__ == "__main__":
    unittest.main()

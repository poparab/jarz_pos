"""Tests for the credit / on-account order path.

What is locked down here, and why each one matters:

1. **``_is_credit_intent`` is a truth table, not a substring search.** It decides
   which dispatch handler an unpaid order gets, and getting it wrong in either
   direction moves real money: too eager and an InstaPay order stops being
   chased, too shy and a credit order is charged to the courier.
2. **Dispatch routes a credit order to the new handler and NOWHERE else.** The
   two named non-destinations — ``mark_courier_outstanding`` and
   ``handle_unpaid_online_deliver_unconfirmed`` — are the two ways this feature
   can silently corrupt the books: the first charges the RIDER with money the
   shop never handed him and marks the invoice Paid; the second stamps
   "Awaiting Payment", which drops the order into the InstaPay verification queue
   and arms an HOURLY escalation on a thirty-day trade credit.
3. **The handler leaves the customer's money alone.** No Payment Entry, no
   confirmation-status stamp, no pending receipt. This is the whole feature.
4. **The creation gate refuses what it must** — a customer not set up for credit,
   and an order that breaks the limit — while an order landing EXACTLY on the
   limit is allowed. Off-by-one on that boundary is a refusal at a counter with
   a customer standing there.
5. **FIFO allocation** clears the oldest invoice first and part-allocates when
   the money runs out, because that is what actually happens: "when I send them
   the second invoice they pay the first invoice."
6. **Regression:** an unpaid INSTAPAY order still routes exactly where it always
   did. This whole feature is additive or it is a bug.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe


def _throwing(message="throw", *args, **kwargs):
    """A ``frappe.throw`` replacement that actually raises.

    A MagicMock ``throw`` returns None and lets execution continue, so a test
    that expects a refusal quietly asserts nothing at all.
    """
    raise RuntimeError(str(message))


def _mock_invoice(method, **kwargs):
    """An invoice double whose ``get`` answers like a Frappe document's."""
    values = {"custom_payment_method": method}
    values.update(kwargs)
    inv = MagicMock()
    inv.name = values.get("name", "ACC-SINV-TEST-0001")
    inv.docstatus = values.get("docstatus", 1)
    inv.outstanding_amount = values.get("outstanding_amount", 100.0)
    inv.company = values.get("company", "Test Company")
    inv.customer = values.get("customer", "CUST-0001")
    inv.grand_total = values.get("grand_total", 100.0)
    inv.get = MagicMock(side_effect=lambda key, default=None: values.get(key, default))
    return inv


class CreditIntentTests(unittest.TestCase):
    """The predicate that decides the dispatch route."""

    def test_truth_table(self):
        from jarz_pos.services.settlement_strategies import _is_credit_intent

        def _inv(method):
            m = MagicMock()
            m.get = MagicMock(return_value=method)
            return m

        # True: the option itself, plus the spellings normalisation folds away.
        self.assertTrue(_is_credit_intent(_inv("Credit")))
        self.assertTrue(_is_credit_intent(_inv("credit")))
        self.assertTrue(_is_credit_intent(_inv("  CREDIT  ")))
        self.assertTrue(_is_credit_intent(_inv("On Account")))
        self.assertTrue(_is_credit_intent(_inv("on_account")))

        # False: every other method, and the empty / unset cases. An unknown
        # method must keep its existing flow rather than fall into credit.
        self.assertFalse(_is_credit_intent(_inv("Cash")))
        self.assertFalse(_is_credit_intent(_inv("Instapay")))
        self.assertFalse(_is_credit_intent(_inv("Mobile Wallet")))
        self.assertFalse(_is_credit_intent(_inv("Kashier Card")))
        self.assertFalse(_is_credit_intent(_inv("Kashier Wallet")))
        self.assertFalse(_is_credit_intent(_inv("")))
        self.assertFalse(_is_credit_intent(_inv(None)))

    def test_credit_and_online_intents_are_disjoint(self):
        """The two predicates must never both answer True for one order.

        dispatch_settlement checks credit FIRST, so an overlap would not throw —
        it would silently take the credit branch for an InstaPay order.
        """
        from jarz_pos.services.settlement_strategies import (
            _is_credit_intent,
            _is_online_intent,
        )

        for method in ["Credit", "Instapay", "Mobile Wallet", "Cash", "Kashier Card", ""]:
            inv = MagicMock()
            inv.get = MagicMock(return_value=method)
            self.assertFalse(
                _is_credit_intent(inv) and _is_online_intent(inv),
                f"{method!r} is claimed by both intent predicates",
            )


class CreditDispatchRoutingTests(unittest.TestCase):
    """Where an unpaid credit order goes at Out-for-Delivery."""

    @patch("jarz_pos.services.settlement_strategies.frappe")
    def test_unpaid_credit_routes_to_credit_handler(self, mock_frappe):
        from jarz_pos.services.settlement_strategies import dispatch_settlement

        inv = _mock_invoice("Credit", name="INV-CREDIT-OFD")
        mock_frappe.get_doc.return_value = inv
        mock_frappe.db.get_value.return_value = 100.0

        with patch(
            "jarz_pos.services.settlement_strategies.handle_credit_deliver_on_account"
        ) as mock_credit, patch(
            "jarz_pos.services.settlement_strategies.mark_courier_outstanding"
        ) as mock_mco, patch(
            "jarz_pos.services.settlement_strategies.handle_unpaid_online_deliver_unconfirmed"
        ) as mock_online, patch(
            "jarz_pos.services.settlement_strategies.handle_unpaid_settle_later"
        ) as mock_later, patch(
            "jarz_pos.services.settlement_strategies._resolve_delivery_partner",
            return_value=None,
        ):
            mock_credit.return_value = {
                "success": True,
                "mode": "credit_deliver_on_account",
                "payment_confirmation_status": None,
            }

            result = dispatch_settlement(
                "INV-CREDIT-OFD",
                mode="later",
                pos_profile="POS-001",
                party_type="Employee",
                party="HR-EMP-00042",
            )

        mock_credit.assert_called_once()
        # The two ways this feature can corrupt the books.
        mock_mco.assert_not_called()
        mock_online.assert_not_called()
        mock_later.assert_not_called()

        self.assertEqual(result.get("mode"), "credit_deliver_on_account")
        self.assertIsNone(result.get("payment_confirmation_status"))

        # Courier attribution must survive the dispatch hop, or the rider who
        # delivers is never owed his freight.
        _, kwargs = mock_credit.call_args
        self.assertEqual(kwargs.get("party_type"), "Employee")
        self.assertEqual(kwargs.get("party"), "HR-EMP-00042")

    @patch("jarz_pos.services.settlement_strategies.frappe")
    def test_credit_beats_partner_strategy(self, mock_frappe):
        """A credit order dispatched with a PARTNER rider still goes on account.

        The partner's fee is owed either way; what must not happen is the
        PARTNER_STRATEGY cash paths running and booking the customer's money.
        """
        from jarz_pos.services.settlement_strategies import dispatch_settlement

        inv = _mock_invoice("Credit", name="INV-CREDIT-PARTNER")
        mock_frappe.get_doc.return_value = inv
        mock_frappe.db.get_value.return_value = 100.0

        with patch(
            "jarz_pos.services.settlement_strategies.handle_credit_deliver_on_account"
        ) as mock_credit, patch(
            "jarz_pos.services.settlement_strategies.handle_partner_unpaid_settle_later"
        ) as mock_partner, patch(
            "jarz_pos.services.settlement_strategies._resolve_delivery_partner",
            return_value="Talabat",
        ):
            mock_credit.return_value = {"success": True, "mode": "credit_deliver_on_account"}

            dispatch_settlement("INV-CREDIT-PARTNER", mode="later", pos_profile="POS-001")

        mock_credit.assert_called_once()
        mock_partner.assert_not_called()

    @patch("jarz_pos.services.settlement_strategies.frappe")
    def test_regression_unpaid_instapay_still_routes_to_online_handler(self, mock_frappe):
        """The credit branch is ADDITIVE. InstaPay must be untouched by it."""
        from jarz_pos.services.settlement_strategies import dispatch_settlement

        inv = _mock_invoice("Instapay", name="INV-ONLINE-OFD")
        mock_frappe.get_doc.return_value = inv
        mock_frappe.db.get_value.return_value = 100.0

        with patch(
            "jarz_pos.services.settlement_strategies.handle_unpaid_online_deliver_unconfirmed"
        ) as mock_online, patch(
            "jarz_pos.services.settlement_strategies.handle_credit_deliver_on_account"
        ) as mock_credit, patch(
            "jarz_pos.services.settlement_strategies.mark_courier_outstanding"
        ) as mock_mco, patch(
            "jarz_pos.services.settlement_strategies._resolve_delivery_partner",
            return_value=None,
        ):
            mock_online.return_value = {
                "success": True,
                "payment_confirmation_status": "Awaiting Payment",
            }

            result = dispatch_settlement("INV-ONLINE-OFD", mode="later", pos_profile="POS-001")

        mock_online.assert_called_once()
        mock_credit.assert_not_called()
        mock_mco.assert_not_called()
        self.assertEqual(result.get("payment_confirmation_status"), "Awaiting Payment")

    @patch("jarz_pos.services.settlement_strategies.frappe")
    def test_a_paid_credit_invoice_does_not_take_the_credit_branch(self, mock_frappe):
        """The branch is gated on unpaid. A settled credit order is an ordinary
        paid dispatch, and must keep going through the paid strategies."""
        from jarz_pos.services.settlement_strategies import dispatch_settlement

        inv = _mock_invoice("Credit", name="INV-CREDIT-PAID", outstanding_amount=0.0)
        inv.get = MagicMock(
            side_effect=lambda key, default=None: {
                "custom_payment_method": "Credit",
                "status": "Paid",
            }.get(key, default)
        )
        mock_frappe.get_doc.return_value = inv
        mock_frappe.db.get_value.return_value = 0.0

        with patch(
            "jarz_pos.services.settlement_strategies.handle_credit_deliver_on_account"
        ) as mock_credit, patch(
            "jarz_pos.services.settlement_strategies.handle_paid_settle_later"
        ) as mock_paid, patch(
            "jarz_pos.services.settlement_strategies._resolve_delivery_partner",
            return_value=None,
        ):
            mock_paid.return_value = {"success": True, "mode": "paid_settle_later"}

            dispatch_settlement("INV-CREDIT-PAID", mode="later", pos_profile="POS-001")

        mock_credit.assert_not_called()
        mock_paid.assert_called_once()


class CreditHandlerTests(unittest.TestCase):
    """What handle_credit_deliver_on_account does — and refuses to do."""

    def _run_handler(self, inv=None, **overrides):
        from jarz_pos.services import delivery_handling

        inv = inv or _mock_invoice("Credit", name="INV-CREDIT-1")
        patches = {
            "resolve_courier_delivery_partner": MagicMock(return_value=None),
            "resolve_assignment_pos_profile": MagicMock(return_value="POS-001"),
            "assert_courier_matches_pos_profile": MagicMock(
                return_value={"delivery_partner": None}
            ),
            "update_submitted_sales_invoice_state": MagicMock(),
            "update_submitted_sales_invoice_fields": MagicMock(),
            "_persist_invoice_courier_assignment": MagicMock(),
            "_accrue_courier_freight_only": MagicMock(return_value=("CT-1", "JE-1", 30.0)),
            "ensure_delivery_note_for_invoice": MagicMock(
                return_value={"delivery_note": "DN-1", "reused": False}
            ),
            "_publish_branch_event": MagicMock(),
            "ensure_pending_payment_receipt": MagicMock(),
            "mark_courier_outstanding": MagicMock(),
            "_create_payment_entry": MagicMock(),
        }
        patches.update(overrides)

        started = []
        for name, replacement in patches.items():
            patcher = patch.object(delivery_handling, name, replacement)
            patcher.start()
            started.append(patcher)
        frappe_patcher = patch.object(delivery_handling, "frappe")
        mock_frappe = frappe_patcher.start()
        started.append(frappe_patcher)
        mock_frappe.db.get_value.return_value = 100.0
        # A mocked frappe.throw that returns is a mocked frappe.throw that hides
        # every refusal this handler makes. Give it teeth.
        mock_frappe.throw.side_effect = _throwing
        try:
            result = delivery_handling.handle_credit_deliver_on_account(
                inv,
                pos_profile="POS-001",
                party_type="Employee",
                party="HR-EMP-00042",
            )
        finally:
            for patcher in reversed(started):
                patcher.stop()
        return result, patches

    def test_moves_out_for_delivery_without_touching_the_receivable(self):
        result, mocks = self._run_handler()

        self.assertTrue(result["success"])
        self.assertEqual(result["new_state"], "Out for Delivery")
        self.assertEqual(result["mode"], "credit_deliver_on_account")

        mocks["update_submitted_sales_invoice_state"].assert_called_once()
        # The outstanding the shop still owes is reported back, untouched.
        self.assertEqual(result["outstanding_amount"], 100.0)

    def test_creates_no_customer_payment_entry_and_no_courier_outstanding(self):
        """The single most important assertion in this module.

        Either of these would take the customer's debt off Debtors — one by
        charging the rider, one by inventing a receipt — and the shop's balance
        would read as settled while nobody had paid anything.
        """
        _, mocks = self._run_handler()

        mocks["mark_courier_outstanding"].assert_not_called()
        mocks["_create_payment_entry"].assert_not_called()

    def test_does_not_stamp_awaiting_payment_or_file_a_receipt(self):
        """No InstaPay queue, no hourly escalation, no screenshot demand."""
        result, mocks = self._run_handler()

        mocks["update_submitted_sales_invoice_fields"].assert_not_called()
        mocks["ensure_pending_payment_receipt"].assert_not_called()
        # Present and explicitly None, so a client cannot read a missing key as
        # "unknown" and show an awaiting-transfer badge.
        self.assertIn("payment_confirmation_status", result)
        self.assertIsNone(result["payment_confirmation_status"])

    def test_still_accrues_the_courier_freight(self):
        """The rider delivered. The shop's terms are not his problem."""
        result, mocks = self._run_handler()

        mocks["_accrue_courier_freight_only"].assert_called_once()
        _, kwargs = mocks["_accrue_courier_freight_only"].call_args
        self.assertEqual(
            kwargs["notes_tag"], "Courier freight accrual (credit / on account)"
        )
        self.assertEqual(result["courier_transaction"], "CT-1")
        self.assertEqual(result["journal_entry"], "JE-1")
        self.assertEqual(result["shipping_amount"], 30.0)

    def test_delivery_note_is_mandatory(self):
        """Same enforcement as every other OFD path — a credit order is not an
        excuse to move stock without a Delivery Note."""
        with self.assertRaises(RuntimeError) as ctx:
            self._run_handler(
                ensure_delivery_note_for_invoice=MagicMock(
                    return_value={"error": "no stock"}
                )
            )
        self.assertIn("Delivery Note", str(ctx.exception))

    def test_a_courier_is_required(self):
        from jarz_pos.services import delivery_handling

        inv = _mock_invoice("Credit", name="INV-CREDIT-NOCOURIER")
        with patch.object(delivery_handling, "resolve_courier_delivery_partner",
                          MagicMock(return_value=None)), patch.object(
            delivery_handling, "frappe"
        ) as mock_frappe:
            mock_frappe.throw.side_effect = _throwing
            with self.assertRaises(RuntimeError):
                delivery_handling.handle_credit_deliver_on_account(
                    inv, pos_profile="POS-001", party_type=None, party=None
                )


class FreightHelperContractTests(unittest.TestCase):
    """The refactor must not have changed the online path's behaviour."""

    def test_online_path_keeps_its_exact_notes_and_lookup(self):
        from jarz_pos.services import delivery_handling as dh

        # These three strings are the compatibility contract with every Courier
        # Transaction already in the database. The LIKE pattern is deliberately a
        # prefix of the note, so rows written by earlier revisions still dedupe.
        self.assertEqual(
            dh._ONLINE_FREIGHT_NOTES,
            "Courier freight accrual (unpaid online delivery, awaiting payment)",
        )
        self.assertEqual(
            dh._ONLINE_FREIGHT_NOTES_LIKE,
            "%Courier freight accrual (unpaid online%",
        )
        self.assertTrue(
            dh._ONLINE_FREIGHT_NOTES.startswith(
                dh._ONLINE_FREIGHT_NOTES_LIKE.strip("%")
            ),
            "the idempotency pattern no longer matches the note it dedupes on",
        )
        self.assertEqual(
            dh._ONLINE_PARTNER_FREIGHT_NOTES,
            "Partner delivery fee accrual (unpaid online delivery, awaiting payment) "
            "- no cash position",
        )

    def test_credit_and_online_freight_rows_never_dedupe_against_each_other(self):
        from jarz_pos.services import delivery_handling as dh

        self.assertNotEqual(dh._CREDIT_FREIGHT_NOTES, dh._ONLINE_FREIGHT_NOTES)
        self.assertFalse(
            dh._CREDIT_FREIGHT_NOTES.startswith(
                dh._ONLINE_FREIGHT_NOTES_LIKE.strip("%")
            )
        )
        self.assertFalse(
            dh._ONLINE_FREIGHT_NOTES.startswith(
                dh._CREDIT_FREIGHT_NOTES_LIKE.strip("%")
            )
        )

    def test_the_two_credit_predicates_agree(self):
        """``delivery_handling`` cannot import ``settlement_strategies`` (that
        would be circular), so the token set is duplicated. Pin them together."""
        from jarz_pos.services import delivery_handling as dh
        from jarz_pos.services import settlement_strategies as ss

        self.assertEqual(dh._CREDIT_INTENT_TOKENS, ss._CREDIT_INTENT_TOKENS)

        for method in ["Credit", "on account", "Cash", "Instapay", "", None]:
            inv = MagicMock()
            inv.get = MagicMock(return_value=method)
            self.assertEqual(
                dh._invoice_is_credit_intent(inv),
                ss._is_credit_intent(inv),
                f"the two credit predicates disagree on {method!r}",
            )

    def test_helper_no_ops_without_a_courier(self):
        from jarz_pos.services import delivery_handling as dh

        result = dh._accrue_courier_freight_only(
            MagicMock(),
            party_type="",
            party="",
            courier_details={},
            partner_fee=None,
            notes_tag="anything",
        )
        self.assertEqual(result, (None, None, 0.0))


class CreditCreationGateTests(unittest.TestCase):
    """services.invoice_creation._apply_credit_terms."""

    def _invoice(self, grand_total=100.0):
        return SimpleNamespace(
            name="ACC-SINV-NEW",
            customer="CUST-1",
            grand_total=grand_total,
            posting_date="2026-09-10",
            due_date=None,
            custom_credit_terms_days=None,
        )

    def _customer(self):
        return SimpleNamespace(name="CUST-1", customer_name="Blue Bottle Coffee")

    def _apply(self, settings, balance, grand_total=100.0):
        """Run the gate with a mocked frappe whose ``throw`` really throws."""
        from jarz_pos.services import invoice_creation

        inv = self._invoice(grand_total)

        def _throw(message, *args, **kwargs):
            raise RuntimeError(message)

        with patch.object(invoice_creation, "frappe") as mock_frappe, patch.object(
            invoice_creation, "_customer_credit_settings", return_value=settings
        ), patch.object(
            invoice_creation, "get_open_credit_balance", return_value=balance
        ):
            mock_frappe.throw.side_effect = _throw
            mock_frappe.utils.getdate.side_effect = lambda v: v
            mock_frappe.utils.add_days.side_effect = (
                lambda date, days: f"{date}+{days}d"
            )
            invoice_creation._apply_credit_terms(
                inv, self._customer(), MagicMock()
            )
        return inv

    def test_refuses_a_customer_not_set_up_for_credit(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._apply({"allowed": False, "days": 0, "limit": 0.0}, 0.0)

        message = str(ctx.exception)
        # The message has to name the shop AND the field, because the person
        # reading it is at a counter with the customer in front of them.
        self.assertIn("Blue Bottle Coffee", message)
        self.assertIn("Allow orders on credit", message)

    def test_credit_limit_blocks_an_over_limit_order(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._apply(
                {"allowed": True, "days": 15, "limit": 1000.0},
                balance=950.0,
                grand_total=100.0,
            )

        message = str(ctx.exception)
        # All three numbers must be in the refusal: the limit, what is already
        # owed, and what this order would add. "Over limit" alone is unactionable.
        self.assertIn("1,000.00", message)
        self.assertIn("950.00", message)
        self.assertIn("100.00", message)

    def test_an_order_landing_exactly_on_the_limit_is_allowed(self):
        inv = self._apply(
            {"allowed": True, "days": 15, "limit": 1000.0},
            balance=900.0,
            grand_total=100.0,
        )
        self.assertEqual(inv.custom_credit_terms_days, 15)

    def test_no_limit_means_no_limit(self):
        inv = self._apply(
            {"allowed": True, "days": 7, "limit": 0.0},
            balance=999999.0,
            grand_total=5000.0,
        )
        self.assertEqual(inv.custom_credit_terms_days, 7)

    def test_zero_credit_days_falls_back_to_the_system_default(self):
        from jarz_pos.services.invoice_creation import DEFAULT_CREDIT_DAYS

        inv = self._apply({"allowed": True, "days": 0, "limit": 0.0}, 0.0)

        self.assertEqual(inv.custom_credit_terms_days, DEFAULT_CREDIT_DAYS)
        # Frozen at creation: due_date is posting_date + the days actually used.
        self.assertEqual(inv.due_date, f"2026-09-10+{DEFAULT_CREDIT_DAYS}d")

    def test_credit_is_an_accepted_payment_method(self):
        """The gate is unreachable if creation still rejects the method."""
        import inspect

        from jarz_pos.services import invoice_creation

        source = inspect.getsource(invoice_creation.create_pos_invoice)
        self.assertIn("CREDIT_PAYMENT_METHOD", source)
        self.assertEqual(invoice_creation.CREDIT_PAYMENT_METHOD, "Credit")


class CreditPaymentFifoTests(unittest.TestCase):
    """record_credit_payment allocates oldest-invoice-first."""

    def _open_invoices(self):
        # Deliberately out of "obvious" order in value, in oldest-first order in
        # date — FIFO is about age, never about size.
        return [
            {
                "name": "INV-A",
                "posting_date": "2026-07-01",
                "due_date": "2026-07-31",
                "grand_total": 300.0,
                "outstanding_amount": 300.0,
                "custom_kanban_profile": "POS-001",
            },
            {
                "name": "INV-B",
                "posting_date": "2026-08-01",
                "due_date": "2026-08-31",
                "grand_total": 200.0,
                "outstanding_amount": 200.0,
                "custom_kanban_profile": "POS-001",
            },
            {
                "name": "INV-C",
                "posting_date": "2026-09-01",
                "due_date": "2026-10-01",
                "grand_total": 500.0,
                "outstanding_amount": 500.0,
                "custom_kanban_profile": "POS-001",
            },
        ]

    def _record(self, amount):
        from jarz_pos.api import credit as credit_api

        pe = MagicMock()
        pe.name = "PE-CREDIT-0001"

        with patch.object(credit_api, "frappe") as mock_frappe, patch.object(
            credit_api, "_ensure_credit_payment_access"
        ), patch.object(
            credit_api, "_allowed_profiles", return_value=["POS-001"]
        ), patch.object(
            credit_api, "_open_credit_invoices", return_value=self._open_invoices()
        ), patch.object(
            credit_api, "_existing_credit_payment", return_value=None
        ), patch.object(
            credit_api, "_branch_field", return_value="custom_kanban_profile"
        ), patch.object(
            credit_api, "_credit_currency", return_value="EGP"
        ), patch(
            "jarz_pos.services.delivery_handling._normalize_collection_method",
            return_value="Cash",
        ), patch(
            "jarz_pos.services.delivery_handling._is_cash_collection_method",
            return_value=True,
        ), patch(
            "jarz_pos.services.delivery_handling._get_online_collection_account",
            return_value="Bank Account - T",
        ), patch(
            "jarz_pos.services.delivery_handling._get_receivable_account",
            return_value="Debtors - T",
        ), patch(
            "jarz_pos.utils.access_control.ensure_open_shift"
        ), patch(
            "jarz_pos.utils.account_utils.get_pos_cash_account",
            return_value="Cash - POS-001 - T",
        ), patch(
            "jarz_pos.utils.account_utils.validate_account_exists"
        ):
            mock_frappe.db.exists.return_value = True
            mock_frappe.db.get_value.return_value = "Test Company"
            mock_frappe.new_doc.return_value = pe
            mock_frappe.get_meta.return_value.get_field.return_value = None

            result = credit_api.record_credit_payment(
                customer="CUST-1",
                amount=amount,
                pos_profile="POS-001",
                payment_method="Cash",
                posting_date="2026-09-10",
            )
        return result, pe

    def test_full_payment_clears_the_two_oldest_and_part_allocates_the_third(self):
        # 300 + 200 clears A and B outright; 100 of the 600 lands on C.
        result, pe = self._record(600.0)

        allocations = result["allocations"]
        self.assertEqual([a["invoice"] for a in allocations], ["INV-A", "INV-B", "INV-C"])
        self.assertEqual([a["allocated_amount"] for a in allocations], [300.0, 200.0, 100.0])
        self.assertEqual([a["fully_settled"] for a in allocations], [True, True, False])

        self.assertEqual(result["allocated_amount"], 600.0)
        self.assertEqual(result["unallocated_amount"], 0.0)
        # 1000 open, 600 paid.
        self.assertEqual(result["remaining_balance"], 400.0)
        self.assertEqual(result["payment_entry"], "PE-CREDIT-0001")

    def test_partial_payment_part_allocates_the_oldest_and_stops(self):
        result, pe = self._record(120.0)

        allocations = result["allocations"]
        self.assertEqual(len(allocations), 1)
        self.assertEqual(allocations[0]["invoice"], "INV-A")
        self.assertEqual(allocations[0]["allocated_amount"], 120.0)
        self.assertFalse(allocations[0]["fully_settled"])
        self.assertEqual(result["unallocated_amount"], 0.0)
        self.assertEqual(result["remaining_balance"], 880.0)

        # Exactly one reference row was appended: the younger invoices must not
        # be touched at all, or their status flickers to Partly Paid.
        reference_calls = [
            call for call in pe.append.call_args_list if call.args[0] == "references"
        ]
        self.assertEqual(len(reference_calls), 1)

    def test_excess_is_kept_as_an_advance_and_announced(self):
        result, _pe = self._record(1200.0)

        self.assertEqual(result["allocated_amount"], 1000.0)
        self.assertEqual(result["unallocated_amount"], 200.0)
        self.assertEqual(result["remaining_balance"], 0.0)
        # Not refused, and not silent — the person at the counter is told before
        # the shop walks away.
        self.assertEqual(result["notice_code"], "recorded_as_advance")
        self.assertTrue(result["notice"])

    def test_cash_lands_in_the_branch_drawer(self):
        """Shift close counts this money, so it must be the profile's own account."""
        result, pe = self._record(100.0)

        self.assertEqual(result["cash_account"], "Cash - POS-001 - T")
        self.assertEqual(pe.paid_to, "Cash - POS-001 - T")
        self.assertEqual(pe.paid_from, "Debtors - T")
        self.assertEqual(pe.payment_type, "Receive")
        self.assertEqual(pe.party_type, "Customer")
        self.assertEqual(pe.party, "CUST-1")
        pe.submit.assert_called_once()

    def test_a_replayed_payment_is_not_taken_twice(self):
        from jarz_pos.api import credit as credit_api

        with patch.object(credit_api, "frappe") as mock_frappe, patch.object(
            credit_api, "_ensure_credit_payment_access"
        ), patch.object(
            credit_api, "_allowed_profiles", return_value=["POS-001"]
        ), patch.object(
            credit_api, "_existing_credit_payment", return_value="PE-ALREADY-0001"
        ), patch.object(
            credit_api, "_open_credit_invoices"
        ) as mock_open, patch(
            "jarz_pos.services.delivery_handling._normalize_collection_method",
            return_value="Cash",
        ), patch(
            "jarz_pos.services.delivery_handling._is_cash_collection_method",
            return_value=True,
        ), patch(
            "jarz_pos.services.delivery_handling._get_online_collection_account",
            return_value="Bank Account - T",
        ), patch(
            "jarz_pos.services.delivery_handling._get_receivable_account",
            return_value="Debtors - T",
        ), patch(
            "jarz_pos.utils.access_control.ensure_open_shift"
        ), patch(
            "jarz_pos.utils.account_utils.get_pos_cash_account",
            return_value="Cash - POS-001 - T",
        ), patch(
            "jarz_pos.utils.account_utils.validate_account_exists"
        ):
            mock_frappe.db.exists.return_value = True
            mock_frappe.db.get_value.return_value = "Test Company"

            result = credit_api.record_credit_payment(
                customer="CUST-1",
                amount=600.0,
                pos_profile="POS-001",
                idempotency_token="tap-once",
            )

        self.assertTrue(result["already_recorded"])
        self.assertEqual(result["payment_entry"], "PE-ALREADY-0001")
        # Not even read: a replay must post nothing at all.
        mock_frappe.new_doc.assert_not_called()
        mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()

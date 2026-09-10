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

Then the four things the first production week proved were wrong, each of which
only became reachable BECAUSE credit exists — every one of them is a way for the
books to be quietly wrong rather than loudly broken:

7. **A debt must survive a relabel** (``CreditDebtSurvivesARelabelTests``).
   ``custom_payment_method`` is mutable after submit; the frozen
   ``custom_credit_terms_days`` stamp is not. Every reader of "what is owed on
   account" matches EITHER, or a collection-method change that moves no money at
   all makes a real debt vanish and frees the shop's limit.
8. **A part-paid order must not over-charge the courier**
   (``PartPaidCollectionChangeTests``). FIFO allocation makes "partly paid, then
   switched to cash" ordinary; the GL leg and the Courier Transaction have to
   move the same number or settlement drives Courier Outstanding negative.
9. **One delivery, one freight accrual** (``FreightIdempotencyTests``). The
   per-path notes tag is attribution, not idempotency: a re-dispatch on the other
   path must not owe the rider his freight a second time.
10. **The credit action is anchored to a dispatch state**
    (``KanbanCreditActionGateTests``) and **the creation gate normalises exactly
    as dispatch does** (``CreditCreationGateNormalisationTests``).

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

    def test_the_two_freight_tags_stay_distinct_for_ATTRIBUTION_only(self):
        """Each row still says which dispatch wrote it — and that is ALL it says.

        This used to be asserted as "the credit and online rows never dedupe
        against each other", which named the tags as the idempotency boundary.
        They are not, and treating them as one was a live double-accrual: see
        :meth:`FreightIdempotencyTests.test_dedupe_crosses_the_tag_boundary`.
        The tags remain distinct so a row is attributable to the path that wrote
        it; ``_existing_freight_ct`` is what decides whether a row already exists.
        """
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


#: The exact ``or_filters`` every credit-debt query must send. Written out here
#: rather than imported from the helper so a change to the helper has to be made
#: deliberately, in two places, instead of silently agreeing with itself.
_EXPECTED_CREDIT_OR_FILTERS = [
    ["Sales Invoice", "custom_payment_method", "=", "Credit"],
    ["Sales Invoice", "custom_credit_terms_days", ">", 0],
]


class CreditDebtSurvivesARelabelTests(unittest.TestCase):
    """DEFECT A — a debt must not vanish when its payment method is rewritten.

    ``custom_payment_method`` is MUTABLE after submit:
    ``change_payment_collection_method`` writes it on every collection-method
    change, and its ``unpaid_online_retarget`` branch posts no voucher at all —
    the outstanding stays exactly where it was. Keyed on that column alone, a
    manager tapping "Change collection method -> Instapay" on a credit card made
    a real debt disappear from the ledger, freed that much of the shop's limit,
    and left ``record_credit_payment`` unable to allocate against it. The frozen
    ``custom_credit_terms_days`` stamp is the permanent half of the answer, and
    all three readers must ask the same question.
    """

    def test_the_predicate_matches_the_method_OR_the_frozen_stamp(self):
        from jarz_pos.utils import credit_utils

        with patch.object(credit_utils, "has_credit_terms_field", return_value=True):
            self.assertEqual(
                credit_utils.credit_invoice_or_filters(),
                _EXPECTED_CREDIT_OR_FILTERS,
            )

    def test_it_degrades_to_the_method_alone_when_the_stamp_column_is_missing(self):
        """A bench that has not migrated must still answer, not raise."""
        from jarz_pos.utils import credit_utils

        filters = {"docstatus": 1}
        with patch.object(credit_utils, "has_credit_terms_field", return_value=False):
            or_filters = credit_utils.apply_credit_invoice_match(filters)

        self.assertIsNone(or_filters)
        self.assertEqual(filters["custom_payment_method"], "Credit")

    def test_the_ledger_query_does_not_key_on_the_mutable_column(self):
        from jarz_pos.api import credit as credit_api
        from jarz_pos.utils import credit_utils

        with patch.object(credit_api, "frappe") as mock_frappe, patch.object(
            credit_utils, "has_credit_terms_field", return_value=True
        ), patch.object(
            credit_api, "_open_credit_invoice_fields", return_value=["name"]
        ):
            mock_frappe.get_all.return_value = []
            credit_api._open_credit_invoices(customers=["CUST-1"])

        _, kwargs = mock_frappe.get_all.call_args
        # The equality filter is what LOST the debt. It must be gone, not merely
        # supplemented — an AND with the OR would be the same bug.
        self.assertNotIn("custom_payment_method", kwargs["filters"])
        self.assertEqual(kwargs["or_filters"], _EXPECTED_CREDIT_OR_FILTERS)
        # Still only open, submitted, non-return rows.
        self.assertEqual(kwargs["filters"]["docstatus"], 1)
        self.assertEqual(kwargs["filters"]["is_return"], 0)
        self.assertEqual(kwargs["filters"]["outstanding_amount"], [">", 0.005])

    def test_the_limit_check_does_not_key_on_the_mutable_column(self):
        """The other half of the same bug: a relabelled debt FREED the limit."""
        from jarz_pos.services import invoice_creation
        from jarz_pos.utils import credit_utils

        with patch.object(invoice_creation, "frappe") as mock_frappe, patch.object(
            credit_utils, "has_credit_terms_field", return_value=True
        ):
            mock_frappe.get_all.return_value = [{"outstanding_amount": 600.0}]
            balance = invoice_creation.get_open_credit_balance("CUST-1")

        _, kwargs = mock_frappe.get_all.call_args
        self.assertNotIn("custom_payment_method", kwargs["filters"])
        self.assertEqual(kwargs["or_filters"], _EXPECTED_CREDIT_OR_FILTERS)
        self.assertEqual(kwargs["filters"]["customer"], "CUST-1")
        self.assertEqual(balance, 600.0)

    def test_the_kanban_card_asks_the_same_question(self):
        """Three readers, one predicate. Any two disagreeing is a lost debt."""
        from jarz_pos.api import kanban
        from jarz_pos.utils import credit_utils

        with patch.object(kanban, "frappe") as mock_frappe, patch.object(
            credit_utils, "has_credit_terms_field", return_value=True
        ):
            mock_frappe.get_all.side_effect = [[], [], [], []]
            kanban._get_unsettled_customer_amount_map(["INV-1"])

        credit_call = mock_frappe.get_all.call_args_list[2]
        self.assertNotIn("custom_payment_method", credit_call.kwargs["filters"])
        self.assertEqual(credit_call.kwargs["or_filters"], _EXPECTED_CREDIT_OR_FILTERS)

    def test_a_return_is_excluded_and_that_is_deliberate(self):
        """``is_return: 0`` does NOT over-state the debt.

        ``services/invoice_return`` posts a ``JE_AR_KNOCKOFF`` against the
        ORIGINAL invoice for an unpaid order, so the surviving row's outstanding
        is already net of the credit note and the note's own outstanding is zero.
        Admitting returns here would hand ``record_credit_payment``'s FIFO
        allocation a negative-outstanding row to point a Receive Payment Entry at.
        """
        from jarz_pos.api import credit as credit_api
        from jarz_pos.services import invoice_creation
        from jarz_pos.utils import credit_utils

        with patch.object(credit_api, "frappe") as mock_frappe, patch.object(
            credit_utils, "has_credit_terms_field", return_value=True
        ), patch.object(
            credit_api, "_open_credit_invoice_fields", return_value=["name"]
        ):
            mock_frappe.get_all.return_value = []
            credit_api._open_credit_invoices(customers=["CUST-1"])
        self.assertEqual(mock_frappe.get_all.call_args.kwargs["filters"]["is_return"], 0)

        with patch.object(invoice_creation, "frappe") as mock_frappe, patch.object(
            credit_utils, "has_credit_terms_field", return_value=True
        ):
            mock_frappe.get_all.return_value = []
            invoice_creation.get_open_credit_balance("CUST-1")
        self.assertEqual(mock_frappe.get_all.call_args.kwargs["filters"]["is_return"], 0)


class PartPaidCollectionChangeTests(unittest.TestCase):
    """DEFECT B — a part-paid credit order must not over-charge the courier.

    Before credit this was unreachable: an unpaid-online order is never
    partially paid. ``record_credit_payment`` allocates FIFO by design, so
    part-paying the oldest invoice and then switching it to Cash is now ordinary.
    The GL posts the OUTSTANDING; the courier row must say the same number, or
    settlement drives Courier Outstanding negative by the part payment.
    """

    def _source_ct(self):
        return {
            "name": "CT-1",
            "party_type": "Employee",
            "party": "HR-EMP-00042",
            "amount": 0,
            "shipping_amount": 30.0,
            "status": "Unsettled",
            "payment_mode": "Instapay",
            "notes": "",
            "idempotency_token": "an-older-token",
        }

    def test_the_caller_hands_down_the_outstanding_not_the_grand_total(self):
        from jarz_pos.services import delivery_handling as dh

        inv = _mock_invoice(
            "Credit",
            name="INV-CREDIT-PART",
            grand_total=1000.0,
            outstanding_amount=600.0,
        )
        inv.is_return = 0
        inv.sales_partner = None
        inv.custom_sales_invoice_state = "Out for Delivery"
        inv.get = MagicMock(
            side_effect=lambda key, default=None: {
                "custom_payment_method": "Credit",
                "custom_sales_invoice_state": "Out for Delivery",
                "outstanding_amount": 600.0,
            }.get(key, default)
        )

        with patch.object(dh, "frappe") as mock_frappe, patch.object(
            dh, "_get_collection_change_source_ct", return_value=self._source_ct()
        ), patch.object(
            dh, "_get_real_customer_payment_entry", return_value=None
        ), patch.object(
            dh, "_courier_row_can_still_carry_cash", return_value=True
        ), patch.object(
            dh, "_switched_online_amount", return_value=0.0
        ), patch.object(
            dh, "_apply_collection_change_for_unpaid_online"
        ) as mock_apply, patch.object(
            dh, "_publish_branch_event"
        ):
            mock_frappe.get_doc.return_value = inv
            # The DB is the source of truth about payment; the loaded doc may be
            # stale. 600 is what the shop still owes after paying 400 of 1,000.
            mock_frappe.db.get_value.return_value = 600.0
            mock_frappe.generate_hash.return_value = "tok"
            mock_apply.return_value = {"mode": "unpaid_online_to_cash"}

            result = dh.change_payment_collection_method(
                invoice_name="INV-CREDIT-PART",
                new_method="Cash",
                pos_profile="POS-001",
                idempotency_token="tok-1",
            )

        _, kwargs = mock_apply.call_args
        self.assertEqual(kwargs["order_amount"], 600.0)
        # The two numbers that MUST be equal: what the GL moves and what the
        # courier row will claim.
        self.assertEqual(kwargs["order_amount"], kwargs["outstanding"])
        self.assertNotEqual(kwargs["order_amount"], 1000.0)
        # And the client is told the same number, not the order's face value.
        self.assertEqual(result["order_amount"], 600.0)

    def test_the_courier_row_and_the_gl_leg_move_the_same_money(self):
        from jarz_pos.services import delivery_handling as dh

        inv = _mock_invoice(
            "Credit",
            name="INV-CREDIT-PART",
            grand_total=1000.0,
            outstanding_amount=600.0,
        )

        with patch.object(dh, "frappe") as mock_frappe, patch.object(
            dh, "_is_cash_collection_method", return_value=True
        ), patch.object(
            dh, "mark_payment_receipts_changed_for_invoice", return_value=[]
        ), patch.object(
            dh, "_create_payment_entry"
        ) as mock_pe, patch.object(
            dh, "_get_receivable_account", return_value="Debtors - T"
        ), patch.object(
            dh, "_get_courier_outstanding_account", return_value="Courier Outstanding - T"
        ), patch.object(
            dh, "_append_collection_change_note", return_value="note"
        ), patch.object(
            dh, "update_submitted_sales_invoice_fields"
        ):
            mock_pe.return_value = SimpleNamespace(name="PE-1")

            result = dh._apply_collection_change_for_unpaid_online(
                inv=inv,
                ct={"name": "CT-1", "payment_mode": "Instapay", "notes": ""},
                new_method="Cash",
                order_amount=600.0,
                shipping_amount=30.0,
                party_type="Employee",
                party="HR-EMP-00042",
                outstanding=600.0,
                courier_can_carry=True,
                pos_profile="POS-001",
                notes=None,
                idempotency_token="tok-1",
            )

        # GL leg: the Payment Entry moves the outstanding.
        self.assertEqual(mock_pe.call_args.args[3], 600.0)
        # Courier Transaction: the SAME number, not the 1,000 grand total. The
        # rider is charged with what he will actually collect.
        ct_updates = mock_frappe.db.set_value.call_args.args[2]
        self.assertEqual(ct_updates["amount"], 600.0)
        self.assertEqual(result["order_amount"], 600.0)
        self.assertEqual(result["mode"], "unpaid_online_to_cash")


class FreightIdempotencyTests(unittest.TestCase):
    """DEFECT C — the rider must not be owed one delivery's freight twice.

    The Courier Transaction used to dedupe on a TAG-specific ``notes`` LIKE while
    the freight JE deduped per invoice. An order dispatched on the credit path,
    relabelled to an online method and re-dispatched (offline-queue replay, or a
    card dragged back and re-sent) therefore got a SECOND Unsettled row carrying
    ``shipping_amount`` against a single accrual — and settlement paid it.
    """

    def test_dedupe_crosses_the_tag_boundary(self):
        from jarz_pos.services import delivery_handling as dh

        with patch.object(dh, "frappe") as mock_frappe:
            mock_frappe.get_all.side_effect = [
                # 1. the online path's own tag: nothing, because the row that
                #    exists was written by the CREDIT dispatch.
                [],
                # 2. cross-tag, on reference_invoice + party.
                [{"name": "CT-CREDIT-1", "shipping_amount": 30.0, "partner_fee": 0.0}],
            ]
            found = dh._existing_freight_ct(
                invoice_name="INV-1",
                party_type="Employee",
                party="HR-EMP-00042",
                idempotency_like=dh._ONLINE_FREIGHT_NOTES_LIKE,
            )

        self.assertEqual(found, "CT-CREDIT-1")

    def test_the_tag_scoped_lookup_still_wins_and_still_runs_first(self):
        """The online path's PREFIX pattern is a compatibility contract with rows
        already in the database. It must keep matching them, first."""
        from jarz_pos.services import delivery_handling as dh

        with patch.object(dh, "frappe") as mock_frappe:
            mock_frappe.get_all.side_effect = [["CT-OWN-1"], []]
            found = dh._existing_freight_ct(
                invoice_name="INV-1",
                party_type="Employee",
                party="HR-EMP-00042",
                idempotency_like=dh._ONLINE_FREIGHT_NOTES_LIKE,
            )

        self.assertEqual(found, "CT-OWN-1")
        # The second query is not even run when the first one answers.
        self.assertEqual(mock_frappe.get_all.call_count, 1)
        first_filters = mock_frappe.get_all.call_args_list[0].kwargs["filters"]
        self.assertEqual(
            first_filters["notes"], ["like", dh._ONLINE_FREIGHT_NOTES_LIKE]
        )

    def test_a_zero_freight_row_does_not_suppress_an_accrual(self):
        """The cross-tag net catches rows that ALREADY OWE this delivery. A row
        carrying no freight in either place owes nothing and must not block it."""
        from jarz_pos.services import delivery_handling as dh

        with patch.object(dh, "frappe") as mock_frappe:
            mock_frappe.get_all.side_effect = [
                [],
                [{"name": "CT-EMPTY", "shipping_amount": 0.0, "partner_fee": 0.0}],
            ]
            found = dh._existing_freight_ct(
                invoice_name="INV-1",
                party_type="Employee",
                party="HR-EMP-00042",
                idempotency_like=dh._CREDIT_FREIGHT_NOTES_LIKE,
            )

        self.assertIsNone(found)

    def test_a_partner_row_counts_even_though_its_shipping_amount_is_zero(self):
        """A delivery partner's rider carries the fee on ``partner_fee``; his row
        is born Settled with ``shipping_amount`` zero. It is still an accrual."""
        from jarz_pos.services import delivery_handling as dh

        with patch.object(dh, "frappe") as mock_frappe:
            mock_frappe.get_all.side_effect = [
                [],
                [{"name": "CT-PARTNER", "shipping_amount": 0.0, "partner_fee": 45.0}],
            ]
            found = dh._existing_freight_ct(
                invoice_name="INV-1",
                party_type="Supplier",
                party="SUP-TALABAT",
                idempotency_like=dh._ONLINE_FREIGHT_NOTES_LIKE,
            )

        self.assertEqual(found, "CT-PARTNER")

    def test_the_accrual_writes_no_second_courier_transaction(self):
        from jarz_pos.services import delivery_handling as dh

        inv = SimpleNamespace(
            name="INV-1", company="Test Company", custom_shipping_expense=30.0
        )

        with patch.object(dh, "frappe") as mock_frappe, patch.object(
            dh, "_partner_link", return_value=None
        ), patch.object(
            dh, "_existing_freight_ct", return_value="CT-CREDIT-1"
        ), patch.object(
            dh, "get_creditors_account", return_value="Creditors - T"
        ), patch.object(
            dh, "_create_shipping_expense_to_creditors_je", return_value="JE-1"
        ):
            result = dh._accrue_courier_freight_only(
                inv,
                party_type="Employee",
                party="HR-EMP-00042",
                courier_details={},
                partner_fee=None,
                notes_tag=dh._ONLINE_FREIGHT_NOTES,
                partner_notes_tag=dh._ONLINE_PARTNER_FREIGHT_NOTES,
                idempotency_like=dh._ONLINE_FREIGHT_NOTES_LIKE,
            )

        self.assertEqual(result, ("CT-CREDIT-1", "JE-1", 30.0))
        # The whole point: no second Unsettled row carrying shipping_amount.
        mock_frappe.new_doc.assert_not_called()


class KanbanCreditActionGateTests(unittest.TestCase):
    """DEFECT D — the credit action must be anchored to a dispatch state.

    The online shape it mirrors is implicitly gated: ``Awaiting Payment`` is only
    stamped at Out-for-Delivery. A credit order carries no such stamp (by design
    — it would arm the hourly escalation on a 30-day term), so an ungated credit
    shape offered "Change collection method" on an order still in Recieved, and
    with no Courier Transaction that lands on DR branch cash / CR Debtors: cash
    into the drawer for goods still in the kitchen.
    """

    def _credit_query_filters(self):
        from jarz_pos.api import kanban
        from jarz_pos.utils import credit_utils

        with patch.object(kanban, "frappe") as mock_frappe, patch.object(
            credit_utils, "has_credit_terms_field", return_value=True
        ):
            mock_frappe.get_all.side_effect = [[], [], [], []]
            kanban._get_unsettled_customer_amount_map(["INV-1"])
        return mock_frappe.get_all.call_args_list[2].kwargs["filters"]

    def test_the_credit_shape_is_restricted_to_dispatched_orders(self):
        filters = self._credit_query_filters()

        clause = filters.get("custom_sales_invoice_state")
        self.assertIsNotNone(clause, "the credit shape has no state gate at all")
        self.assertEqual(clause[0], "in")
        normalised = {str(s).strip().lower().replace("_", " ") for s in clause[1]}
        self.assertEqual(normalised, {"out for delivery", "delivered"})

    def test_the_board_matches_the_server_side_rule(self):
        """The card must not offer what the service will refuse. The refusal is
        the authority; this pins the two to the same set of states."""
        import inspect

        from jarz_pos.services import delivery_handling as dh

        source = inspect.getsource(dh.change_payment_collection_method)
        self.assertIn("out for delivery", source)
        self.assertIn("delivered", source)

        filters = self._credit_query_filters()
        for state in filters["custom_sales_invoice_state"][1]:
            self.assertIn(
                str(state).strip().lower().replace("_", " "),
                {"out for delivery", "delivered"},
                f"{state!r} is offered on the board but refused by the server",
            )


class CreditCreationGateNormalisationTests(unittest.TestCase):
    """DEFECT E — the creation gate must normalise exactly as dispatch does.

    ``_is_credit_intent`` / ``_invoice_is_credit_intent`` fold case, spaces and
    underscores and also accept "on account". The creation gate compared the raw
    string against ``"Credit"``. An order spelled "on account" therefore skipped
    ``_apply_credit_terms`` entirely — no permission check, no limit, no due date
    and no ``custom_credit_terms_days`` stamp — while still routing to the credit
    handler at dispatch. With no stamp it is also invisible to the ledger's OR.
    """

    def test_the_value_level_predicate_truth_table(self):
        from jarz_pos.utils.credit_utils import is_credit_payment_method

        for spelling in ["Credit", "credit", "  CREDIT  ", "On Account",
                         "on_account", "ONACCOUNT", "on account"]:
            self.assertTrue(
                is_credit_payment_method(spelling), f"{spelling!r} should be credit"
            )
        for spelling in ["Cash", "Instapay", "Mobile Wallet", "Kashier Card",
                         "Kashier Wallet", "", None, "   "]:
            self.assertFalse(
                is_credit_payment_method(spelling),
                f"{spelling!r} should NOT be credit",
            )

    def test_the_creation_gate_and_the_dispatch_predicates_cannot_disagree(self):
        """One truth, three readers. A disagreement here is a debt with no limit
        check, no frozen terms and no presence in the ledger."""
        from jarz_pos.services.delivery_handling import _invoice_is_credit_intent
        from jarz_pos.services.settlement_strategies import _is_credit_intent
        from jarz_pos.utils.credit_utils import is_credit_payment_method

        for method in ["Credit", "credit", "  CREDIT  ", "On Account", "on_account",
                       "ONACCOUNT", "Cash", "Instapay", "Mobile Wallet", "", None]:
            inv = MagicMock()
            inv.get = MagicMock(return_value=method)
            expected = is_credit_payment_method(method)
            self.assertEqual(
                expected,
                _is_credit_intent(inv),
                f"creation gate and settlement_strategies disagree on {method!r}",
            )
            self.assertEqual(
                expected,
                _invoice_is_credit_intent(inv),
                f"creation gate and delivery_handling disagree on {method!r}",
            )

    def test_create_pos_invoice_normalises_instead_of_comparing_strings(self):
        import inspect

        from jarz_pos.services import invoice_creation

        source = inspect.getsource(invoice_creation.create_pos_invoice)
        self.assertIn("is_credit_payment_method(payment_method)", source)
        # The exact comparison that let "on account" through.
        self.assertNotIn("payment_method == CREDIT_PAYMENT_METHOD", source)
        # And every credit spelling is folded to the canonical Select option, so
        # custom_payment_method never stores a value the Select does not offer —
        # which is what keeps the MUTABLE half of the ledger's OR meaningful.
        self.assertIn("payment_method = CREDIT_PAYMENT_METHOD", source)


if __name__ == "__main__":
    unittest.main()

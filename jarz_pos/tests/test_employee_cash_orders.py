"""Employee orders: on the employee's account (credit) or paid at the counter (cash).

What is locked down here, and why each one matters:

1. **Credit stays the default and stays byte-identical.** Every client that
   predates ``employee_payment`` sends nothing, and builds shipped
   2026-08-29..2026-09-13 send ``payment_method=Cash`` on CREDIT Employee orders.
   Neither may create a Payment Entry, or a salary debt silently becomes a till
   receipt nobody handed over.
2. **Cash is refused wherever it cannot mean "an employee paid this till"** —
   a non-Employee purpose, a contradicting payment method, an online payment
   type, an unknown value. A silently defaulted typo would leave real cash out of
   the shift count.
3. **The branch must be able to take the money BEFORE the invoice exists.** No
   open shift or no till is a refusal with nothing inserted.
4. **Money in before goods out.** The Payment Entry is posted after submit and
   before ``fulfil_at_branch``; if it fails the request fails, and the goods are
   not handed over.
5. **The Payment Entry is the right shape**: Receive, staff Customer, from the
   invoice's own receivable, into the branch till, allocating the outstanding,
   idempotent, and absent on a zero-total order.

Pure ``unittest`` with mocks — no site, no fixtures.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.services import invoice_creation as ic
from jarz_pos.services.commercial_policy import CommercialPolicyDecision
from jarz_pos.services.delivery_promotions import DeliveryPromotionDecision

_MODULE = "jarz_pos.services.invoice_creation"
_BRANCH = "Nasr City"
_COMPANY = "Jarz"
_TILL = "Nasr City - J"


def _throwing(message="throw", *args, **kwargs):
    """A ``frappe.throw`` that actually raises.

    A MagicMock ``throw`` returns None and lets execution carry on, so every
    refusal asserted below would "pass" by doing nothing at all.
    """
    raise RuntimeError(str(message))


def _employee_decision(**overrides):
    values = dict(
        matched=True,
        order_purpose="Employee",
        policy_name="Employee Order",
        price_list="Employee",
        suppress_shipping_income=True,
        suppress_legacy_delivery_charges=True,
        no_courier=True,
        deliver_at_branch=True,
    )
    values.update(overrides)
    return CommercialPolicyDecision(**values)


def _standard_decision():
    return CommercialPolicyDecision()


# ---------------------------------------------------------------------------
# 1. Normalisation
# ---------------------------------------------------------------------------

class EmployeePaymentNormalisationTests(unittest.TestCase):
    """``_normalize_employee_payment``: the whole request contract in one place."""

    def _normalize(self, value, decision=None, payment_method=None, payment_type=None):
        with patch.object(ic, "frappe") as mock_frappe:
            mock_frappe.throw.side_effect = _throwing
            return ic._normalize_employee_payment(
                value,
                decision if decision is not None else _employee_decision(),
                payment_method,
                payment_type=payment_type,
            )

    def test_missing_or_blank_means_credit(self):
        for value in (None, "", "   "):
            self.assertEqual(self._normalize(value)[0], "credit", repr(value))

    def test_credit_is_case_insensitive(self):
        for value in ("credit", "CREDIT", " Credit "):
            self.assertEqual(self._normalize(value)[0], "credit", repr(value))

    def test_cash_is_case_insensitive(self):
        for value in ("cash", "CASH", " Cash "):
            self.assertEqual(self._normalize(value)[0], "cash", repr(value))

    def test_an_unknown_value_is_refused_not_defaulted(self):
        for value in ("card", "on account", "paid", "1"):
            with self.assertRaises(RuntimeError, msg=repr(value)) as ctx:
                self._normalize(value)
            self.assertIn("employee_payment", str(ctx.exception))

    def test_credit_leaves_payment_method_untouched(self):
        """The shipped dialog's ``payment_method=Cash`` on a credit order stays a label."""
        self.assertEqual(self._normalize(None, payment_method="Cash"), ("credit", "Cash"))
        self.assertEqual(self._normalize("credit", payment_method="Instapay"), ("credit", "Instapay"))
        self.assertEqual(self._normalize(None, payment_method=None), ("credit", None))

    def test_credit_is_inert_on_a_standard_order(self):
        self.assertEqual(
            self._normalize("credit", decision=_standard_decision(), payment_method="Cash"),
            ("credit", "Cash"),
        )

    def test_cash_forces_payment_method_cash(self):
        self.assertEqual(self._normalize("cash", payment_method=None), ("cash", "Cash"))
        self.assertEqual(self._normalize("cash", payment_method=""), ("cash", "Cash"))
        self.assertEqual(self._normalize("cash", payment_method="Cash"), ("cash", "Cash"))

    def test_cash_is_refused_for_a_non_employee_purpose(self):
        for decision in (
            _standard_decision(),
            _employee_decision(order_purpose="B2B Supply"),
            _employee_decision(order_purpose="Sample"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._normalize("cash", decision=decision)
            self.assertIn("Employee", str(ctx.exception))

    def test_cash_is_refused_for_a_mocked_decision(self):
        """A MagicMock purpose must never pass for Employee."""
        with self.assertRaises(RuntimeError):
            self._normalize("cash", decision=MagicMock())

    def test_cash_is_refused_with_another_payment_method(self):
        for method in ("Instapay", "Mobile Wallet", "Credit", "Kashier Card"):
            with self.assertRaises(RuntimeError, msg=method) as ctx:
                self._normalize("cash", payment_method=method)
            self.assertIn(method, str(ctx.exception))

    def test_cash_is_refused_with_payment_type_online(self):
        with self.assertRaises(RuntimeError):
            self._normalize("cash", payment_type="online")
        # "cash" payment_type is the ordinary counter value and stays allowed.
        self.assertEqual(self._normalize("cash", payment_type="cash")[0], "cash")


# ---------------------------------------------------------------------------
# 2. The pre-insert gate
# ---------------------------------------------------------------------------

class EmployeeCashGateTests(unittest.TestCase):
    def test_checks_the_shift_and_resolves_the_branch_till(self):
        with patch.object(ic, "ensure_open_shift") as shift, patch.object(
            ic, "get_pos_cash_account", return_value=_TILL
        ) as till:
            self.assertEqual(ic._ensure_employee_cash_can_be_taken(_BRANCH, _COMPANY), _TILL)
        shift.assert_called_once()
        self.assertEqual(shift.call_args.args[0], _BRANCH)
        till.assert_called_once_with(_BRANCH, _COMPANY)

    def test_no_open_shift_propagates_unchanged(self):
        """ShiftRequiredError must reach the client as itself (it routes to Start Shift)."""
        with patch.object(
            ic, "ensure_open_shift", side_effect=RuntimeError("No open shift")
        ), patch.object(ic, "get_pos_cash_account") as till:
            with self.assertRaises(RuntimeError):
                ic._ensure_employee_cash_can_be_taken(_BRANCH, _COMPANY)
        till.assert_not_called()

    def test_a_branch_without_a_till_is_refused(self):
        with patch.object(ic, "ensure_open_shift"), patch.object(
            ic, "get_pos_cash_account", side_effect=RuntimeError("No Cash In Hand account")
        ):
            with self.assertRaises(RuntimeError):
                ic._ensure_employee_cash_can_be_taken(_BRANCH, _COMPANY)


# ---------------------------------------------------------------------------
# 3. The Payment Entry
# ---------------------------------------------------------------------------

class EmployeeCashPaymentEntryTests(unittest.TestCase):
    def _invoice(self, **overrides):
        values = dict(
            name="ACC-SINV-EMP-0001",
            company=_COMPANY,
            customer="STAFF-Mona",
            debit_to="Debtors - J",
            posting_date="2026-09-13",
            grand_total=150.0,
            due_date="2026-09-13",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    def _register(self, invoice=None, *, outstanding=150.0, refs=None, pe=None):
        invoice = invoice or self._invoice()
        pe = pe or MagicMock()
        pe.name = "PE-EMP-0001"
        pe.flags = SimpleNamespace()
        with patch.object(ic, "frappe") as mock_frappe, patch.object(
            ic, "get_pos_cash_account", return_value=_TILL
        ) as till, patch.object(
            ic, "get_company_receivable_account", return_value="Debtors Fallback - J"
        ):
            mock_frappe.throw.side_effect = _throwing
            mock_frappe.get_all.side_effect = refs if refs is not None else [[]]
            mock_frappe.db.get_value.return_value = outstanding
            mock_frappe.new_doc.return_value = pe
            mock_frappe.get_meta.return_value.get_field.return_value = True
            mock_frappe.utils.today.return_value = "2026-09-13"
            result = ic._register_employee_counter_cash_payment(invoice, _BRANCH, MagicMock())
        return result, pe, mock_frappe, till

    def test_receive_entry_into_the_branch_till(self):
        result, pe, mock_frappe, till = self._register()

        self.assertEqual(result, "PE-EMP-0001")
        mock_frappe.new_doc.assert_called_once_with("Payment Entry")
        till.assert_called_once_with(_BRANCH, _COMPANY)
        self.assertEqual(pe.payment_type, "Receive")
        self.assertEqual(pe.party_type, "Customer")
        self.assertEqual(pe.party, "STAFF-Mona")
        self.assertEqual(pe.company, _COMPANY)
        # From the invoice's OWN receivable, into the branch drawer.
        self.assertEqual(pe.paid_from, "Debtors - J")
        self.assertEqual(pe.party_account, "Debtors - J")
        self.assertEqual(pe.paid_to, _TILL)
        self.assertEqual(pe.paid_amount, 150.0)
        self.assertEqual(pe.received_amount, 150.0)
        self.assertEqual(pe.mode_of_payment, "Cash")
        self.assertEqual(pe.reference_no, "EMP-CASH-ACC-SINV-EMP-0001")
        self.assertEqual(pe.custom_kanban_profile, _BRANCH)
        pe.insert.assert_called_once()
        pe.submit.assert_called_once()

    def test_allocates_the_outstanding_to_the_invoice(self):
        _, pe, _, _ = self._register(outstanding=150.0)

        reference_calls = [c for c in pe.append.call_args_list if c.args[0] == "references"]
        self.assertEqual(len(reference_calls), 1)
        row = reference_calls[0].args[1]
        self.assertEqual(row["reference_doctype"], "Sales Invoice")
        self.assertEqual(row["reference_name"], "ACC-SINV-EMP-0001")
        self.assertEqual(row["allocated_amount"], 150.0)
        self.assertEqual(row["outstanding_amount"], 150.0)
        self.assertEqual(row["total_amount"], 150.0)

    def test_uses_the_invoice_posting_date(self):
        invoice = self._invoice(posting_date="2026-09-12")
        _, pe, _, _ = self._register(invoice)

        self.assertEqual(pe.posting_date, "2026-09-12")
        self.assertEqual(pe.reference_date, "2026-09-12")

    def test_falls_back_to_the_company_receivable_without_debit_to(self):
        _, pe, _, _ = self._register(self._invoice(debit_to=None))

        self.assertEqual(pe.paid_from, "Debtors Fallback - J")
        self.assertEqual(pe.party_account, "Debtors Fallback - J")

    def test_zero_total_order_posts_no_payment_entry(self):
        result, _, mock_frappe, till = self._register(outstanding=0.0)

        self.assertIsNone(result)
        mock_frappe.new_doc.assert_not_called()
        till.assert_not_called()

    def test_an_existing_receive_entry_is_reused(self):
        result, _, mock_frappe, _ = self._register(
            refs=[["PE-OLD-0001"], [{"name": "PE-OLD-0001"}]]
        )

        self.assertEqual(result, "PE-OLD-0001")
        mock_frappe.new_doc.assert_not_called()
        # The existing-entry lookup asks for SUBMITTED RECEIVE entries only.
        filters = mock_frappe.get_all.call_args_list[1].kwargs["filters"]
        self.assertEqual(filters["docstatus"], 1)
        self.assertEqual(filters["payment_type"], "Receive")

    def test_a_failed_payment_entry_raises_and_names_the_till(self):
        pe = MagicMock()
        pe.insert.side_effect = RuntimeError("Account frozen")
        with self.assertRaises(RuntimeError) as ctx:
            self._register(pe=pe)

        self.assertIn(_TILL, str(ctx.exception))
        self.assertIn("Account frozen", str(ctx.exception))
        pe.submit.assert_not_called()


# ---------------------------------------------------------------------------
# 4. The creation flow
# ---------------------------------------------------------------------------

class _EmployeeInvoice:
    """A Sales Invoice double that records what creation writes onto it."""

    def __init__(self, after_reload=None):
        self.name = "ACC-SINV-EMP-0001"
        self.customer = None
        self.customer_name = None
        self.company = None
        self.pos_profile = None
        self.custom_kanban_profile = None
        self.posting_date = None
        self.sales_partner = None
        self.custom_payment_method = None
        self.custom_pos_audit_markers = None
        self.remarks = ""
        self.update_stock = 1
        self.items = []
        self.taxes = []
        self.status = "Unpaid"
        self.docstatus = 1
        self.net_total = 150.0
        self.grand_total = 150.0
        self.outstanding_amount = 150.0
        self.flags = SimpleNamespace()
        self.reload_count = 0
        self._after_reload = after_reload or {}

    def append(self, table, row=None):
        item = SimpleNamespace(**(row or {}))
        getattr(self, table, []).append(item)
        return item

    def set(self, table, value):
        setattr(self, table, value)

    def reload(self):
        self.reload_count += 1
        for key, value in self._after_reload.items():
            setattr(self, key, value)


class EmployeeOrderCreationFlowTests(unittest.TestCase):
    """``create_pos_invoice`` end to end, with every collaborator mocked."""

    def _run(
        self,
        *,
        employee_payment=None,
        payment_method=None,
        payment_type=None,
        decision=None,
        after_reload=None,
        gate_side_effect=None,
        register_side_effect=None,
    ):
        inv = _EmployeeInvoice(after_reload=after_reload)
        order = []
        customer = SimpleNamespace(name="STAFF-Mona", customer_name="Mona", territory=None)
        profile = MagicMock()
        profile.name = _BRANCH
        profile.company = _COMPANY
        profile.selling_price_list = "Standard Selling"

        def _set_fields(invoice_doc, customer_doc, pos_profile, delivery_datetime, logger):
            invoice_doc.customer = customer_doc.name
            invoice_doc.customer_name = customer_doc.customer_name
            invoice_doc.company = pos_profile.company
            invoice_doc.pos_profile = pos_profile.name
            invoice_doc.posting_date = "2026-09-13"

        def _record(label, side_effect=None, value=None):
            def _inner(*args, **kwargs):
                order.append(label)
                if side_effect is not None:
                    raise side_effect
                return value
            return _inner

        mocks = {
            "gate": MagicMock(side_effect=_record("gate", gate_side_effect, _TILL)),
            "credit_terms": MagicMock(side_effect=_record("credit_terms")),
            "insert": MagicMock(side_effect=_record("insert")),
            "submit": MagicMock(side_effect=_record("submit")),
            "register": MagicMock(
                side_effect=_record("pe", register_side_effect, "PE-EMP-0001")
            ),
            "fulfil": MagicMock(
                side_effect=_record(
                    "fulfil",
                    None,
                    {"success": True, "delivery_note": "DN-1", "state": "Delivered", "error": None},
                )
            ),
            "create_doc": MagicMock(return_value=inv),
        }
        logger = MagicMock()

        with ExitStack() as stack:
            stack.enter_context(patch(f"{_MODULE}.validate_cart_data", return_value=[{"item_code": "JAR-M"}]))
            stack.enter_context(patch(f"{_MODULE}._parse_delivery_charges", return_value=[]))
            stack.enter_context(patch(f"{_MODULE}.validate_delivery_datetime", return_value=None))
            stack.enter_context(patch(f"{_MODULE}.validate_customer", return_value=customer))
            stack.enter_context(patch(f"{_MODULE}.validate_pos_profile", return_value=profile))
            stack.enter_context(patch(f"{_MODULE}._normalize_delivery_window", return_value=(None, None)))
            stack.enter_context(patch(f"{_MODULE}._commercial_policy.resolve_commercial_policy", return_value=decision if decision is not None else _employee_decision()))
            stack.enter_context(patch(f"{_MODULE}._ensure_can_place_standard_order"))
            stack.enter_context(patch(f"{_MODULE}._resolve_effective_price_list", return_value="Employee"))
            stack.enter_context(patch(f"{_MODULE}._validate_policy_price_list_coverage"))
            stack.enter_context(patch(f"{_MODULE}._process_cart_items", return_value=[{"item_code": "JAR-M", "qty": 1, "price_list_rate": 150.0}]))
            stack.enter_context(patch(f"{_MODULE}._create_invoice_document", mocks["create_doc"]))
            stack.enter_context(patch(f"{_MODULE}.set_invoice_fields", side_effect=_set_fields))
            stack.enter_context(patch(f"{_MODULE}.stamp_order_channel", return_value="flutter"))
            stack.enter_context(patch(f"{_MODULE}.resolve_customer_shipping_address", return_value=None))
            stack.enter_context(patch(f"{_MODULE}.resolve_order_territory", return_value=None))
            stack.enter_context(patch(f"{_MODULE}.add_items_to_invoice"))
            stack.enter_context(patch(f"{_MODULE}._set_initial_state_for_sales_partner"))
            stack.enter_context(patch(f"{_MODULE}._delivery_promotions.resolve_delivery_promotion", return_value=DeliveryPromotionDecision()))
            stack.enter_context(patch(f"{_MODULE}._validate_and_calculate_document"))
            stack.enter_context(patch(f"{_MODULE}._apply_credit_terms", mocks["credit_terms"]))
            stack.enter_context(patch(f"{_MODULE}._ensure_employee_cash_can_be_taken", mocks["gate"]))
            stack.enter_context(patch(f"{_MODULE}._save_document", mocks["insert"]))
            stack.enter_context(patch(f"{_MODULE}._submit_document", mocks["submit"]))
            stack.enter_context(patch(f"{_MODULE}._persist_selling_price_list"))
            stack.enter_context(patch(f"{_MODULE}._record_territory_exception"))
            stack.enter_context(patch(f"{_MODULE}._maybe_register_online_payment_to_partner"))
            stack.enter_context(patch(f"{_MODULE}._register_employee_counter_cash_payment", mocks["register"]))
            stack.enter_context(patch(f"{_MODULE}._branch_fulfilment.fulfil_at_branch", mocks["fulfil"]))
            stack.enter_context(patch(f"{_MODULE}._handle_invoice_creation_error"))
            mock_frappe = stack.enter_context(patch(f"{_MODULE}.frappe"))
            mock_frappe.local.site = "test-site"
            mock_frappe.logger.return_value = logger
            mock_frappe.utils.now.return_value = "2026-09-13 12:00:00"
            mock_frappe.db.exists.return_value = False
            mock_frappe.get_all.return_value = []
            mock_frappe.throw.side_effect = _throwing

            try:
                result = ic.create_pos_invoice(
                    cart_json="[]",
                    customer_name="STAFF-Mona",
                    pos_profile_name=_BRANCH,
                    payment_method=payment_method,
                    payment_type=payment_type,
                    order_purpose="Employee",
                    employee_payment=employee_payment,
                )
                error = None
            except RuntimeError as exc:
                result = None
                error = exc

        return SimpleNamespace(
            result=result, error=error, inv=inv, order=order, mocks=mocks, logger=logger
        )

    # -- credit (default) ---------------------------------------------------

    def test_credit_default_creates_no_payment_entry(self):
        run = self._run(employee_payment=None, payment_method="Cash")

        self.assertIsNone(run.error)
        run.mocks["register"].assert_not_called()
        run.mocks["gate"].assert_not_called()
        # The shipped dialog's label still lands exactly as before.
        self.assertEqual(run.inv.custom_payment_method, "Cash")
        self.assertNotIn("[EMPLOYEE PAYMENT]", run.inv.custom_pos_audit_markers or "")
        run.mocks["fulfil"].assert_called_once()

    def test_credit_never_enters_the_b2b_credit_terms_gate(self):
        for value in (None, "credit", "CREDIT"):
            run = self._run(employee_payment=value)
            self.assertIsNone(run.error, repr(value))
            run.mocks["credit_terms"].assert_not_called()

    def test_credit_response_reports_the_unpaid_receivable(self):
        run = self._run(
            employee_payment="credit",
            after_reload={"outstanding_amount": 150.0, "status": "Unpaid"},
        )

        self.assertEqual(run.result["employee_payment"], "credit")
        self.assertIsNone(run.result["payment_entry"])
        self.assertEqual(run.result["outstanding_amount"], 150.0)
        self.assertEqual(run.result["status"], "Unpaid")

    # -- cash ---------------------------------------------------------------

    def test_cash_stamps_payment_method_and_marker(self):
        run = self._run(employee_payment="cash")

        self.assertIsNone(run.error)
        self.assertEqual(run.inv.custom_payment_method, "Cash")
        self.assertIn("[EMPLOYEE PAYMENT] Cash", run.inv.custom_pos_audit_markers)
        # No reader of remarks exists for this tag, so it is not mirrored there.
        self.assertNotIn("[EMPLOYEE PAYMENT]", run.inv.remarks or "")
        run.mocks["credit_terms"].assert_not_called()

    def test_cash_money_in_before_goods_out(self):
        run = self._run(employee_payment="cash")

        self.assertEqual(run.order, ["gate", "insert", "submit", "pe", "fulfil"])
        args = run.mocks["register"].call_args.args
        self.assertIs(args[0], run.inv)
        self.assertEqual(args[1], _BRANCH)
        run.mocks["gate"].assert_called_once_with(_BRANCH, _COMPANY)

    def test_cash_response_carries_the_payment_and_fresh_state(self):
        run = self._run(
            employee_payment="cash",
            after_reload={"outstanding_amount": 0.0, "status": "Paid"},
        )

        self.assertEqual(run.inv.reload_count, 1)
        self.assertEqual(run.result["employee_payment"], "cash")
        self.assertEqual(run.result["payment_entry"], "PE-EMP-0001")
        self.assertEqual(run.result["outstanding_amount"], 0.0)
        self.assertEqual(run.result["status"], "Paid")
        self.assertTrue(run.result["success"])

    def test_a_payment_entry_failure_fails_the_request_and_hands_nothing_over(self):
        run = self._run(employee_payment="cash", register_side_effect=RuntimeError("till frozen"))

        self.assertIsNotNone(run.error)
        self.assertIn("till frozen", str(run.error))
        run.mocks["fulfil"].assert_not_called()
        self.assertEqual(run.order, ["gate", "insert", "submit", "pe"])

    def test_no_open_shift_or_no_till_refuses_before_insert(self):
        run = self._run(employee_payment="cash", gate_side_effect=RuntimeError("No open shift"))

        self.assertIsNotNone(run.error)
        run.mocks["insert"].assert_not_called()
        run.mocks["submit"].assert_not_called()
        run.mocks["register"].assert_not_called()
        run.mocks["fulfil"].assert_not_called()

    def test_cash_on_a_standard_order_is_refused_before_anything_is_built(self):
        run = self._run(employee_payment="cash", decision=_standard_decision())

        self.assertIsNotNone(run.error)
        run.mocks["create_doc"].assert_not_called()
        run.mocks["insert"].assert_not_called()

    def test_cash_with_instapay_is_refused_before_anything_is_built(self):
        run = self._run(employee_payment="cash", payment_method="Instapay")

        self.assertIsNotNone(run.error)
        run.mocks["create_doc"].assert_not_called()

    def test_an_invalid_value_is_refused(self):
        run = self._run(employee_payment="card")

        self.assertIsNotNone(run.error)
        run.mocks["create_doc"].assert_not_called()

    # -- Standard orders ----------------------------------------------------

    def test_standard_response_is_unchanged(self):
        run = self._run(employee_payment=None, decision=_standard_decision())

        self.assertIsNone(run.error)
        for key in ("employee_payment", "payment_entry", "outstanding_amount"):
            self.assertNotIn(key, run.result)
        self.assertEqual(run.inv.reload_count, 0)
        run.mocks["register"].assert_not_called()
        run.mocks["gate"].assert_not_called()


if __name__ == "__main__":
    unittest.main()

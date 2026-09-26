"""A paid order keeps its payment method across an amendment.

Order 17612 / ``ACC-SINV-2026-18471`` (Kashier Card, 480 paid into ``kashier - J``)
was amended from the POS. The payment was carried across correctly, but the POS
payment-method dialog — which has no Kashier option — sent ``Cash``, the job let
that overwrite the source's method, and the replacement ``ACC-SINV-2026-18471-1``
went Out for Delivery stamped Cash. The Woo outbound sync then pushed that label to
the website order as ``cod``.

The money and the label have to agree: when the carried Payment Entry settles the
replacement in full, the source's method is kept whatever the client sends. When
the edit leaves a balance, the cashier's chosen method stands — it says how the
extra will be collected. Unpaid, Employee and credit orders are unchanged.

Pure-Python unit tests in the style of ``test_amendment_shipping_request``.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _source(**extra):
    inv = SimpleNamespace(
        name="ACC-SINV-TEST-612",
        docstatus=1,
        grand_total=480.0,
        is_return=0,
        custom_sales_invoice_state="Recieved",
        custom_payment_method="Kashier Card",
        custom_order_purpose=None,
        customer="Ahmed - 60",
        items=[],
    )
    inv.__dict__.update(extra)
    inv.get = lambda key, default=None: inv.__dict__.get(key, default)
    return inv


class TestPrepaidAmendmentPaymentMethod(unittest.TestCase):
    def _resolve(self, has_payment=True, **extra):
        from jarz_pos.api.manager import _prepaid_amendment_payment_method

        return _prepaid_amendment_payment_method(_source(**extra), has_payment=has_payment)

    def test_a_paid_kashier_order_keeps_kashier(self):
        self.assertEqual(self._resolve(), "Kashier Card")

    def test_every_stamped_paid_method_is_kept(self):
        for method in ("Kashier Wallet", "Instapay", "Mobile Wallet", "Cash"):
            with self.subTest(method=method):
                self.assertEqual(self._resolve(custom_payment_method=method), method)

    def test_an_unpaid_order_is_free_to_change_method(self):
        self.assertIsNone(self._resolve(has_payment=False))

    def test_an_employee_order_is_left_to_its_own_resolver(self):
        self.assertIsNone(self._resolve(custom_order_purpose="Employee", custom_payment_method="Cash"))

    def test_a_credit_order_is_not_locked(self):
        for method in ("Credit", "on account"):
            with self.subTest(method=method):
                self.assertIsNone(self._resolve(custom_payment_method=method))

    def test_a_blank_method_has_nothing_to_keep(self):
        self.assertIsNone(self._resolve(custom_payment_method=""))
        self.assertIsNone(self._resolve(custom_payment_method=None))


class TestEligibilityTellsThePosTheMethod(unittest.TestCase):
    def _eligibility(self, migration, **extra):
        with (
            patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None),
            patch("jarz_pos.api.manager.evaluate_amendment_payment_migration", **migration),
        ):
            from jarz_pos.api.manager import get_invoice_amendment_eligibility

            return get_invoice_amendment_eligibility(_source(**extra))

    def test_a_paid_order_reports_its_method_and_paid_amount(self):
        out = self._eligibility(
            {"return_value": {"can_migrate": True, "has_payment": True, "allocated_total": 480.0}}
        )
        self.assertTrue(out["can_amend"])
        self.assertEqual(out["amendment_payment_method"], "Kashier Card")
        self.assertEqual(out["amendment_paid_amount"], 480.0)

    def test_an_unpaid_order_reports_none(self):
        out = self._eligibility({"return_value": {"can_migrate": True, "has_payment": False}})
        self.assertTrue(out["can_amend"])
        self.assertIsNone(out["amendment_payment_method"])
        self.assertIsNone(out["amendment_paid_amount"])

    def test_a_failed_lookup_reports_none_and_stays_amendable(self):
        with patch("jarz_pos.api.manager.frappe.log_error"):
            out = self._eligibility({"side_effect": RuntimeError("db down")})
        self.assertTrue(out["can_amend"])
        self.assertIsNone(out["amendment_payment_method"])


class TestJobKeepsThePaidMethod(unittest.TestCase):
    """End to end through ``_run_invoice_amendment_job`` with every lookup mocked."""

    def _run(self, *, client_method, source_method="Kashier Card", payment_entries=("ACC-PAY-1",),
             payment_type="Receive", order_purpose=None, outstanding_before=480.0, allocated=480.0):
        created = {}

        class FakeInvoice:
            name = "ACC-SINV-TEST-612"
            docstatus = 1
            grand_total = 480.0
            is_return = 0
            custom_sales_invoice_state = "Recieved"
            custom_kanban_profile = "Nasr city"
            pos_profile = "Nasr city"
            customer = "Ahmed - 60"
            custom_payment_method = source_method
            custom_order_purpose = order_purpose
            custom_shipping_override = None
            custom_shipping_override_status = None
            territory = "Nasr City"
            woo_order_id = "17612"
            items = []
            flags = SimpleNamespace(ignore_permissions=False, ignore_woo_outbound=False)

            def get(self, key, default=None):
                return getattr(self, key, default)

            def cancel(self):
                pass

            def reload(self):
                pass

        class FakePaymentEntry:
            name = "ACC-PAY-1"
            docstatus = 1
            paid_amount = 480.0
            paid_to = "kashier - J"
            paid_from = "Debtors - J"
            mode_of_payment = None
            party = "Ahmed - 60"
            party_type = "Customer"
            company = "JARZ"
            reference_no = "WOO-17612"
            flags = SimpleNamespace(ignore_permissions=False)

            def __init__(self):
                self.payment_type = payment_type

            def get(self, key, default=None):
                return getattr(self, key, default)

            def cancel(self):
                pass

        source = FakeInvoice()

        def _get_doc(doctype, name=None, *args, **kwargs):
            return FakePaymentEntry() if doctype == "Payment Entry" else source

        def _create(*args, **kwargs):
            # 10th positional argument of _create_amendment_invoice is payment_method.
            created["payment_method"] = args[9]
            return {"invoice_name": "ACC-SINV-TEST-612-1"}

        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]
        mf.session.user = "belal@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()
        mf.get_doc.side_effect = _get_doc
        # What creation stamped on the replacement: the method it was created with.
        mf.db.get_value.side_effect = lambda doctype, name, field, *a, **k: (
            created.get("payment_method") if field == "custom_payment_method" else None
        )

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._create_amendment_invoice", side_effect=_create),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=list(payment_entries)),
            patch("jarz_pos.api.manager._resolve_amendment_employee_payment", return_value=None),
            patch("jarz_pos.api.manager._resolve_employee_amendment_payment_method", return_value="Cash"),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
            patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
            patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
            patch("jarz_pos.api.manager._carry_over_invoice_notes", return_value=None),
            patch("jarz_pos.api.manager._build_invoice_amendment_response", return_value={"success": True}),
            patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
            patch("jarz_pos.api.manager._resolve_amendment_delivery_income", return_value=None),
            patch("jarz_pos.api.manager._detach_custom_shipping_requests", return_value=[]),
            patch("jarz_pos.api.manager._attach_custom_shipping_requests", return_value={}),
            patch(
                "jarz_pos.api.manager._rebook_amendment_payment",
                return_value={"payment_entries": ["ACC-PAY-2"], "outstanding_before": outstanding_before,
                              "allocated": allocated, "unallocated": 0.0},
            ),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-612",
                request_id="test-req-612",
                cart_json=json.dumps([{"item_code": "X", "rate": 160, "qty": 3}]),
                pos_profile_name="Nasr city",
                payment_method=client_method,
            )
        relabels = [
            c.args[3] for c in mf.db.set_value.call_args_list
            if len(c.args) >= 4 and c.args[2] == "custom_payment_method"
        ]
        created["final_method"] = relabels[-1] if relabels else created.get("payment_method")
        return result, created

    def test_order_17612_keeps_kashier_when_the_pos_sends_cash(self):
        """Equal value: the carried 480 settles it, so the label follows the money."""
        result, created = self._run(client_method="Cash")
        self.assertTrue(result.get("success"), result)
        self.assertEqual(created["final_method"], "Kashier Card")

    def test_a_cheaper_edit_also_keeps_kashier(self):
        result, created = self._run(client_method="Cash", outstanding_before=400.0, allocated=400.0)
        self.assertEqual(created["final_method"], "Kashier Card")

    def test_a_dearer_edit_takes_the_cashiers_method_for_the_balance(self):
        """480 carried against 640: 160 is still owed, and the cashier chose Cash."""
        result, created = self._run(client_method="Cash", outstanding_before=640.0, allocated=480.0)
        self.assertTrue(result.get("success"), result)
        self.assertEqual(created["final_method"], "Cash")

    def test_a_dearer_edit_can_stay_on_the_same_method(self):
        result, created = self._run(client_method="Instapay", source_method="Instapay",
                                    outstanding_before=640.0, allocated=480.0)
        self.assertEqual(created["final_method"], "Instapay")

    def test_a_paid_order_with_no_client_method_keeps_its_own(self):
        result, created = self._run(client_method=None)
        self.assertTrue(result.get("success"), result)
        self.assertEqual(created["final_method"], "Kashier Card")

    def test_an_unpaid_order_still_takes_the_client_method(self):
        result, created = self._run(client_method="Instapay", source_method="Cash", payment_entries=())
        self.assertTrue(result.get("success"), result)
        self.assertEqual(created["final_method"], "Instapay")

    def test_a_refund_entry_is_not_evidence_of_payment(self):
        result, created = self._run(client_method="Instapay", source_method="Cash", payment_type="Pay")
        self.assertEqual(created["final_method"], "Instapay")

    def test_an_employee_order_is_left_to_its_own_resolver(self):
        result, created = self._run(client_method="Instapay", source_method="Cash", order_purpose="Employee")
        self.assertEqual(created["final_method"], "Cash")


if __name__ == "__main__":
    unittest.main()

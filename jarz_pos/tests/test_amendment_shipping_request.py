"""An amendment must carry a Custom Shipping Request across, not refuse the edit.

Order 17575 / ``ACC-SINV-2026-18433``: a Received, unpaid order had its courier
cost raised 65 -> 160 for a 31 kg delivery (``CSR-00166``). From the moment that
request was raised the order could not be edited at all — the amendment treated
any active request as a hard blocker, while ``kanban.cancel_invoice`` simply
releases them.

The amendment now unlinks the requests before cancelling the source (the
``before_cancel`` guard and Frappe's back-link check both refuse otherwise),
points them at the replacement, and re-applies the override the request
lifecycle wrote. Every OTHER caller of the blocker keeps refusing.

Pure-Python unit tests in the style of ``test_amendment_hardening``.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_invoice(name="ACC-SINV-TEST-750", state="Recieved", **extra):
    inv = SimpleNamespace(
        name=name,
        docstatus=1,
        grand_total=1020.0,
        is_return=0,
        custom_sales_invoice_state=state,
        custom_delivery_trip=None,
        customer="Mohamed",
        items=[],
    )
    inv.__dict__.update(extra)
    inv.get = lambda key, default=None: inv.__dict__.get(key, default)
    return inv


def _csr(name="CSR-00166", docstatus=1, status="Approved", requested_amount=160.0):
    return {
        "name": name,
        "docstatus": docstatus,
        "status": status,
        "requested_amount": requested_amount,
        "creation": "2026-09-22 14:28:34",
    }


def _only_csr_get_all(doctype, **kwargs):
    if doctype == "Custom Shipping Request":
        return ["CSR-00166"]
    return []


class TestEligibilityNoLongerRefusesAShippingRequest(unittest.TestCase):
    def _mf(self):
        mf = MagicMock()
        mf._ = lambda x: x
        mf.get_all.side_effect = _only_csr_get_all
        mf.db.get_value.return_value = None
        return mf

    def test_received_order_with_an_approved_request_can_be_amended(self):
        from jarz_pos.api.manager import get_invoice_amendment_eligibility

        with (
            patch("jarz_pos.api.manager.frappe", self._mf()),
            patch(
                "jarz_pos.api.manager.evaluate_amendment_payment_migration",
                return_value={"can_migrate": True},
            ),
        ):
            result = get_invoice_amendment_eligibility(_make_invoice())

        self.assertTrue(result["can_amend"], result)

    def test_other_blockers_still_refuse_the_amendment(self):
        """Only the shipping request is waived — a courier position still blocks."""
        from jarz_pos.api.manager import get_invoice_amendment_eligibility

        mf = self._mf()

        def _get_all(doctype, **kwargs):
            if doctype in ("Custom Shipping Request", "Courier Transaction"):
                return ["X-1"]
            return []

        mf.get_all.side_effect = _get_all
        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch(
                "jarz_pos.api.manager.evaluate_amendment_payment_migration",
                return_value={"can_migrate": True},
            ),
        ):
            result = get_invoice_amendment_eligibility(_make_invoice())

        self.assertFalse(result["can_amend"])
        self.assertEqual(result["amendment_block_code"], "courier_transaction_exists")

    def test_the_blocker_still_refuses_by_default(self):
        """Cancellation and the before_cancel guard call it without the opt-out."""
        from jarz_pos.api.manager import get_invoice_hard_mutation_blocker

        with patch("jarz_pos.api.manager.frappe", self._mf()):
            result = get_invoice_hard_mutation_blocker(
                _make_invoice(), ignore_unsettled_partner_transactions=True
            )

        self.assertEqual(result["mutation_block_code"], "custom_shipping_request_exists")

    def test_before_cancel_guard_still_refuses_a_plain_cancel(self):
        from jarz_pos.events import sales_invoice as events

        class _Refused(Exception):
            pass

        ev_frappe = MagicMock()
        ev_frappe._ = lambda x: x
        ev_frappe.throw.side_effect = _Refused
        with (
            patch("jarz_pos.api.manager.frappe", self._mf()),
            patch.object(events, "frappe", ev_frappe),
        ):
            with self.assertRaises(_Refused):
                events.block_cancel_if_dispatched(_make_invoice())


class TestDetachCustomShippingRequests(unittest.TestCase):
    def test_unlinks_every_active_request_and_returns_its_shape(self):
        from jarz_pos.api.manager import _detach_custom_shipping_requests

        mf = MagicMock()
        rows = [_csr(), _csr(name="CSR-00170", docstatus=0, status="Pending", requested_amount=200)]
        mf.get_all.return_value = rows
        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch(
                "jarz_pos.api.manager._find_active_custom_shipping_requests",
                return_value=["CSR-00166", "CSR-00170"],
            ),
        ):
            result = _detach_custom_shipping_requests("ACC-SINV-TEST-750")

        self.assertEqual(result, rows)
        cleared = [c.args for c in mf.db.set_value.call_args_list]
        self.assertEqual(
            cleared,
            [
                ("Custom Shipping Request", "CSR-00166", "invoice", None),
                ("Custom Shipping Request", "CSR-00170", "invoice", None),
            ],
        )

    def test_no_request_touches_nothing(self):
        from jarz_pos.api.manager import _detach_custom_shipping_requests

        mf = MagicMock()
        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._find_active_custom_shipping_requests", return_value=[]),
        ):
            self.assertEqual(_detach_custom_shipping_requests("ACC-SINV-TEST-750"), [])
        mf.db.set_value.assert_not_called()


class TestResolveCarriedShippingOverride(unittest.TestCase):
    def _resolve(self, source, rows):
        from jarz_pos.api.manager import _resolve_carried_shipping_override

        return _resolve_carried_shipping_override(source, rows)

    def test_source_fields_are_copied_verbatim(self):
        self.assertEqual(
            self._resolve({"override": 160, "status": "Approved"}, [_csr()]),
            {"status": "Approved", "override": 160.0},
        )

    def test_pending_first_request_stays_pending_with_no_override(self):
        self.assertEqual(
            self._resolve({"override": 0, "status": "Pending"}, [_csr(docstatus=0, status="Pending")]),
            {"status": "Pending", "override": 0.0},
        )

    def test_blank_status_is_derived_from_the_requests(self):
        rows = [_csr(), _csr(name="CSR-2", docstatus=0, status="Pending", requested_amount=200)]
        self.assertEqual(
            self._resolve({"override": None, "status": None}, rows),
            {"status": "Pending", "override": 160.0},
        )


class TestAttachCustomShippingRequests(unittest.TestCase):
    def _attach(self, rows, source_override, replacement=None):
        from jarz_pos.api.manager import _attach_custom_shipping_requests

        mf = MagicMock()
        mf._ = lambda x: x
        mf.db.get_value.return_value = (
            replacement
            if replacement is not None
            else {"customer_name": "Mohamed", "territory": "Nasr City", "custom_shipping_expense": 65.0}
        )
        with patch("jarz_pos.api.manager.frappe", mf):
            result = _attach_custom_shipping_requests(
                source_invoice_name="ACC-SINV-TEST-750",
                replacement_invoice_name="ACC-SINV-TEST-750-1",
                shipping_requests=rows,
                source_override=source_override,
                logger=MagicMock(),
            )
        return mf, result

    def _writes(self, mf, doctype):
        return [c.args[2] for c in mf.db.set_value.call_args_list if c.args[0] == doctype]

    def test_approved_override_follows_the_order(self):
        mf, result = self._attach(
            [_csr()], {"override": 160.0, "status": "Approved", "territory": "Nasr City"}
        )
        self.assertEqual(result["shipping_requests"], ["CSR-00166"])
        csr_writes = self._writes(mf, "Custom Shipping Request")
        self.assertEqual(csr_writes[0]["invoice"], "ACC-SINV-TEST-750-1")
        self.assertNotIn("original_amount", csr_writes[0], "same territory: approval record untouched")
        self.assertEqual(
            self._writes(mf, "Sales Invoice"),
            [
                {
                    "custom_shipping_override": 160.0,
                    "custom_shipping_override_status": "Approved",
                    "custom_shipping_expense": 160.0,
                }
            ],
        )

    def test_pending_request_keeps_the_dispatch_gate_and_the_territory_rate(self):
        mf, _ = self._attach(
            [_csr(docstatus=0, status="Pending")],
            {"override": 0, "status": "Pending", "territory": "Nasr City"},
        )
        self.assertEqual(
            self._writes(mf, "Sales Invoice"),
            [{"custom_shipping_override": 0.0, "custom_shipping_override_status": "Pending"}],
        )

    def test_a_second_request_pending_on_an_approval_keeps_the_approved_cost(self):
        mf, _ = self._attach(
            [_csr(), _csr(name="CSR-2", docstatus=0, status="Pending", requested_amount=200)],
            {"override": 160.0, "status": "Pending", "territory": "Nasr City"},
        )
        self.assertEqual(self._writes(mf, "Sales Invoice")[0]["custom_shipping_expense"], 160.0)

    def test_rejected_draft_does_not_override_the_cost(self):
        mf, _ = self._attach(
            [_csr(docstatus=0, status="Rejected")],
            {"override": 0, "status": "Rejected", "territory": "Nasr City"},
        )
        self.assertNotIn("custom_shipping_expense", self._writes(mf, "Sales Invoice")[0])

    def test_territory_change_rebases_what_a_rejection_reverts_to(self):
        mf, _ = self._attach(
            [_csr()],
            {"override": 160.0, "status": "Approved", "territory": "Dokki"},
            replacement={"customer_name": "Mohamed", "territory": "Nasr City", "custom_shipping_expense": 65.0},
        )
        self.assertEqual(self._writes(mf, "Custom Shipping Request")[0]["original_amount"], 65.0)

    def test_unreadable_replacement_raises(self):
        from jarz_pos.api.manager import _attach_custom_shipping_requests

        class _Thrown(Exception):
            pass

        mf = MagicMock()
        mf._ = lambda x: x
        mf.db.get_value.return_value = None
        mf.throw.side_effect = _Thrown
        with patch("jarz_pos.api.manager.frappe", mf):
            with self.assertRaises(_Thrown):
                _attach_custom_shipping_requests(
                    source_invoice_name="A",
                    replacement_invoice_name="A-1",
                    shipping_requests=[_csr()],
                    source_override={"override": 160, "status": "Approved"},
                    logger=MagicMock(),
                )
        mf.db.set_value.assert_not_called()

    def test_a_failed_comment_does_not_undo_the_carry_over(self):
        from jarz_pos.api.manager import _attach_custom_shipping_requests

        mf = MagicMock()
        mf._ = lambda x: x
        mf.db.get_value.return_value = {"customer_name": "M", "territory": "T", "custom_shipping_expense": 65}
        mf.get_doc.side_effect = RuntimeError("comment table locked")
        with patch("jarz_pos.api.manager.frappe", mf):
            result = _attach_custom_shipping_requests(
                source_invoice_name="A",
                replacement_invoice_name="A-1",
                shipping_requests=[_csr()],
                source_override={"override": 160, "status": "Approved", "territory": "T"},
                logger=MagicMock(),
            )
        self.assertEqual(result["shipping_requests"], ["CSR-00166"])


class TestJobCarriesTheRequest(unittest.TestCase):
    """The job order: detach BEFORE the source cancel, attach AFTER the replacement."""

    def _run(self, attach_side_effect=None):
        events = []

        class FakeInvoice:
            name = "ACC-SINV-TEST-750"
            docstatus = 1
            grand_total = 1020.0
            is_return = 0
            custom_sales_invoice_state = "Recieved"
            custom_kanban_profile = "Nasr city"
            pos_profile = "Nasr city"
            customer = "Mohamed"
            custom_shipping_override = 160.0
            custom_shipping_override_status = "Approved"
            territory = "Nasr City"
            woo_order_id = None
            items = []
            flags = SimpleNamespace(ignore_permissions=False, ignore_woo_outbound=False)

            def get(self, key, default=None):
                return getattr(self, key, default)

            def cancel(self):
                events.append("cancel_source")

            def reload(self):
                pass

        def _detach(invoice_name):
            events.append(("detach", invoice_name))
            return [_csr()]

        def _create(*args, **kwargs):
            events.append("create_replacement")
            return {"invoice_name": "ACC-SINV-TEST-750-1"}

        def _attach(**kwargs):
            events.append(("attach", kwargs["replacement_invoice_name"], kwargs["source_override"]))
            if attach_side_effect:
                raise attach_side_effect
            return {"shipping_requests": ["CSR-00166"], "override_status": "Approved", "override": 160.0}

        response_kwargs = {}

        def _response(**kwargs):
            response_kwargs.update(kwargs)
            return {"success": True}

        mf = MagicMock()
        mf._ = lambda x: x
        mf.parse_json.side_effect = json.loads
        mf.db.sql.return_value = [[1]]
        mf.session.user = "test@example.com"
        mf.local.site = "frontend"
        mf.logger.return_value = MagicMock()

        with (
            patch("jarz_pos.api.manager.frappe", mf),
            patch("jarz_pos.api.manager._create_amendment_invoice", side_effect=_create),
            patch("jarz_pos.api.manager._find_existing_amendment_invoice", return_value=None),
            patch("jarz_pos.api.manager.get_invoice_amendment_eligibility", return_value={"can_amend": True}),
            patch("jarz_pos.api.manager._find_submitted_payment_entries", return_value=[]),
            patch("jarz_pos.api.manager.frappe.get_doc", return_value=FakeInvoice()),
            patch("jarz_pos.api.manager.assert_pos_profile_matches_territory", return_value=None),
            patch("jarz_pos.api.manager._mark_source_invoice_as_amended", return_value=None),
            patch("jarz_pos.api.manager._add_invoice_audit_comment", return_value=None),
            patch("jarz_pos.api.manager._carry_over_invoice_notes", return_value=None),
            patch("jarz_pos.api.manager._build_invoice_amendment_response", side_effect=_response),
            patch("jarz_pos.api.manager._temporary_invoice_creation_form_context", MagicMock()),
            patch("jarz_pos.api.manager._resolve_amendment_delivery_income", return_value=None),
            patch("jarz_pos.api.manager._detach_custom_shipping_requests", side_effect=_detach),
            patch("jarz_pos.api.manager._attach_custom_shipping_requests", side_effect=_attach),
        ):
            from jarz_pos.api.manager import _run_invoice_amendment_job

            result = _run_invoice_amendment_job(
                invoice_id="ACC-SINV-TEST-750",
                request_id="test-req-csr",
                cart_json=json.dumps([{"item_code": "X", "rate": 510, "qty": 2}]),
                pos_profile_name="Nasr city",
            )
        return result, events, response_kwargs, mf

    def test_request_moves_across_in_the_right_order(self):
        result, events, response_kwargs, _ = self._run()

        self.assertTrue(result.get("success"), result)
        self.assertEqual(events[0], ("detach", "ACC-SINV-TEST-750"))
        self.assertEqual(events[1], "cancel_source")
        self.assertEqual(events[2], "create_replacement")
        attach = events[3]
        self.assertEqual(attach[1], "ACC-SINV-TEST-750-1")
        self.assertEqual(
            attach[2], {"override": 160.0, "status": "Approved", "territory": "Nasr City"}
        )
        self.assertEqual(response_kwargs["carried_shipping_requests"], ["CSR-00166"])

    def test_a_failed_carry_over_rolls_the_whole_amendment_back(self):
        result, events, _, mf = self._run(attach_side_effect=RuntimeError("boom"))

        self.assertFalse(result.get("success"))
        self.assertTrue(
            any(c.kwargs.get("save_point") for c in mf.db.rollback.call_args_list),
            "the savepoint must be rolled back so the request re-links to the source",
        )


if __name__ == "__main__":
    unittest.main()

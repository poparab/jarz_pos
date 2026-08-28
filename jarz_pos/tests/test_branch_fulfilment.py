"""Tests for Deliver-at-Branch auto-fulfilment.

The feature exists to close one specific hole: an employee collects jars at the
counter, so the order must land in ``Delivered`` at creation time — but stock in
this app only ever leaves inventory via the Delivery Note created on the
``Out for Delivery`` transition. A "Delivered" card with no Delivery Note is
therefore an inventory leak, not a convenience. These tests pin the ordering
that prevents it, plus the idempotency and failure behaviour around it.

Pure ``unittest`` with ``frappe`` mocked out (the ``@patch('module.frappe')``
style of ``test_kanban_settlement``) so the suite runs without a site — the CI
logic gate runs before ``bench migrate``.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from jarz_pos.services import branch_fulfilment as bf
from jarz_pos.services import commercial_policy as cp


class _Doc(types.SimpleNamespace):
    """SimpleNamespace that also supports dict-style ``.get()`` like a Frappe doc."""

    def get(self, key, default=None):
        return getattr(self, key, default)


def _invoice(state="Recieved", docstatus=1, name="_TEST-BF-1"):
    return _Doc(
        name=name,
        docstatus=docstatus,
        custom_sales_invoice_state=state,
        custom_was_out_for_delivery=0,
        custom_kanban_profile="_TEST BRANCH",
        pos_profile="_TEST BRANCH",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. The policy decision carries the flag
# ─────────────────────────────────────────────────────────────────────────────

class TestDecisionCarriesDeliverAtBranch(unittest.TestCase):
    """``fulfilment_behavior`` translates into ``decision.deliver_at_branch``."""

    def _resolve_with_policy(self, **policy_fields):
        policy = types.SimpleNamespace(
            name="_TEST POLICY",
            order_purpose="Employee",
            price_list=None,
            discount_percentage=0,
            shipping_income_behavior="Zero",
            shipping_expense_behavior="Zero",
            courier_behavior="No Courier",
            **policy_fields,
        )
        with patch.object(cp, "frappe") as mock_frappe, patch.object(
            cp, "_load_policy", return_value=policy
        ), patch.object(cp, "_ensure_policy_permission"):
            mock_frappe.db.exists.return_value = True
            return cp.resolve_commercial_policy(order_purpose="Employee")

    def test_deliver_at_branch_policy_sets_the_flag(self):
        decision = self._resolve_with_policy(fulfilment_behavior="Deliver at Branch")
        self.assertTrue(decision.matched)
        self.assertTrue(decision.deliver_at_branch)

    def test_normal_policy_does_not_set_the_flag(self):
        decision = self._resolve_with_policy(fulfilment_behavior="Normal")
        self.assertTrue(decision.matched)
        self.assertFalse(decision.deliver_at_branch)

    def test_unmigrated_site_without_the_field_stays_normal(self):
        """A site that has not migrated the Select yet must not auto-fulfil."""
        decision = self._resolve_with_policy()  # no fulfilment_behavior attribute
        self.assertFalse(decision.deliver_at_branch)

    def test_standard_fast_path_is_inert(self):
        """Regression guard: the Standard fast-path returns before any policy load."""
        decision = cp.resolve_commercial_policy(order_purpose="Standard")
        self.assertFalse(decision.matched)
        self.assertFalse(decision.deliver_at_branch)
        self.assertFalse(decision.no_courier)

    def test_default_decision_is_false(self):
        self.assertFalse(cp.CommercialPolicyDecision().deliver_at_branch)


# ─────────────────────────────────────────────────────────────────────────────
# 2. fulfil_at_branch
# ─────────────────────────────────────────────────────────────────────────────

@patch("jarz_pos.services.branch_fulfilment.publish_invoice_event")
@patch("jarz_pos.services.branch_fulfilment.update_submitted_sales_invoice_state")
@patch("jarz_pos.services.branch_fulfilment.ensure_delivery_note_for_invoice")
@patch("jarz_pos.services.branch_fulfilment.frappe")
class TestFulfilAtBranch(unittest.TestCase):

    def test_already_delivered_is_idempotent(self, mock_frappe, mock_dn, mock_state, mock_pub):
        """A retried call must not post a second DN or a second consumable issue."""
        mock_frappe.get_doc.return_value = _invoice(state="Delivered")
        mock_frappe.get_all.return_value = [{"parent": "DN-EXISTING"}]
        mock_frappe.db.get_value.return_value = 0  # Delivery Note.is_return

        result = bf.fulfil_at_branch("_TEST-BF-1")

        self.assertTrue(result["success"])
        self.assertEqual(result["state"], "Delivered")
        self.assertEqual(result["delivery_note"], "DN-EXISTING")
        mock_dn.assert_not_called()
        mock_state.assert_not_called()

    def test_draft_invoice_returns_failure_not_an_exception(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """A draft has no stock to release; report it, never raise into the POS."""
        mock_frappe.get_doc.return_value = _invoice(docstatus=0)

        result = bf.fulfil_at_branch("_TEST-BF-1")

        self.assertFalse(result["success"])
        self.assertIn("not submitted", result["error"])
        mock_dn.assert_not_called()
        mock_state.assert_not_called()

    def test_delivery_note_failure_leaves_the_state_untouched(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """No DN means no stock movement, so the card must NOT reach Delivered."""
        mock_frappe.get_doc.return_value = _invoice(state="Recieved")
        mock_dn.return_value = {"delivery_note": None, "error": "warehouse mismatch"}

        result = bf.fulfil_at_branch("_TEST-BF-1")

        self.assertFalse(result["success"])
        self.assertIn("warehouse mismatch", result["error"])
        self.assertEqual(result["state"], "Recieved")
        self.assertIsNone(result["delivery_note"])
        mock_state.assert_not_called()
        mock_pub.assert_not_called()

    def test_delivery_note_exception_returns_failure_not_an_exception(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """A raising DN helper must not take the already-submitted invoice with it."""
        mock_frappe.get_doc.return_value = _invoice(state="Recieved")
        mock_frappe.get_traceback.return_value = "traceback"
        mock_dn.side_effect = RuntimeError("boom")

        result = bf.fulfil_at_branch("_TEST-BF-1")

        self.assertFalse(result["success"])
        self.assertIn("boom", result["error"])
        mock_state.assert_not_called()

    def test_state_write_passes_both_field_aliases(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """``api/kanban`` reads whichever alias the site carries — pass both.

        ``update_submitted_sales_invoice_state`` defaults to the FIRST name only,
        so an implicit call would move the card on some sites and silently not on
        others.
        """
        mock_frappe.get_doc.return_value = _invoice(state="Recieved")
        mock_frappe.db.get_value.return_value = 1  # custom_was_out_for_delivery
        mock_dn.return_value = {"delivery_note": "DN-NEW", "error": None}

        result = bf.fulfil_at_branch("_TEST-BF-1")

        self.assertTrue(result["success"])
        self.assertEqual(result["state"], "Delivered")
        self.assertEqual(result["delivery_note"], "DN-NEW")

        self.assertEqual(mock_state.call_count, 2)
        for call in mock_state.call_args_list:
            self.assertEqual(
                call.kwargs["field_names"],
                ("custom_sales_invoice_state", "sales_invoice_state"),
            )
        self.assertEqual(
            [call.args[1] for call in mock_state.call_args_list],
            ["Out for Delivery", "Delivered"],
        )

    def test_delivery_note_is_created_before_the_delivered_write(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """The ordering IS the feature: stock moves first, the column moves last."""
        order: list = []
        mock_frappe.get_doc.return_value = _invoice(state="Recieved")
        mock_frappe.db.get_value.return_value = 1
        mock_dn.side_effect = lambda *a, **k: (
            order.append("delivery_note"), {"delivery_note": "DN-NEW", "error": None}
        )[1]
        mock_state.side_effect = lambda inv, state, **k: (
            order.append(f"state:{state}"), True
        )[1]

        bf.fulfil_at_branch("_TEST-BF-1")

        self.assertEqual(
            order,
            ["delivery_note", "state:Out for Delivery", "state:Delivered"],
        )

    def test_dispatch_state_is_written_so_the_ofd_hooks_fire(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """Consumables + the was-OFD stamp hang off the real OFD state write.

        Skipping straight to Delivered would leave the couvert/bag Material Issue
        unposted and ``custom_was_out_for_delivery`` unset, which in turn makes
        ``services/invoice_return`` refuse the order.
        """
        mock_frappe.get_doc.return_value = _invoice(state="Recieved")
        mock_frappe.db.get_value.return_value = 1
        mock_dn.return_value = {"delivery_note": "DN-NEW", "error": None}

        bf.fulfil_at_branch("_TEST-BF-1")

        written_states = [call.args[1] for call in mock_state.call_args_list]
        self.assertIn("Out for Delivery", written_states)

    def test_was_out_for_delivery_is_stamped_when_the_hook_missed_it(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """The stamp hook swallows its own errors, so re-assert the flag directly."""
        mock_frappe.get_doc.return_value = _invoice(state="Recieved")
        mock_frappe.db.get_value.return_value = 0  # hook did not stamp it
        mock_dn.return_value = {"delivery_note": "DN-NEW", "error": None}

        bf.fulfil_at_branch("_TEST-BF-1")

        mock_frappe.db.set_value.assert_called_once_with(
            "Sales Invoice",
            "_TEST-BF-1",
            "custom_was_out_for_delivery",
            1,
            update_modified=False,
        )

    def test_realtime_update_is_published_on_success(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        """Open boards move the card without a refresh."""
        mock_frappe.get_doc.return_value = _invoice(state="Recieved")
        mock_frappe.db.get_value.return_value = 1
        mock_dn.return_value = {"delivery_note": "DN-NEW", "error": None}

        bf.fulfil_at_branch("_TEST-BF-1")

        self.assertEqual(mock_pub.call_count, 2)
        events = [call.args[0] for call in mock_pub.call_args_list]
        self.assertEqual(
            events,
            [bf.WS_EVENTS.INVOICE_STATE_CHANGE, bf.WS_EVENTS.KANBAN_UPDATE],
        )
        payload = mock_pub.call_args_list[0].args[1]
        self.assertEqual(payload["new_state"], "Delivered")
        self.assertEqual(payload["delivery_note"], "DN-NEW")

    def test_missing_invoice_name_is_rejected(
        self, mock_frappe, mock_dn, mock_state, mock_pub
    ):
        result = bf.fulfil_at_branch("")

        self.assertFalse(result["success"])
        self.assertIn("required", result["error"])
        mock_frappe.get_doc.assert_not_called()


class TestConstantsAgreeWithTheFrozenBoard(unittest.TestCase):
    """The states written here must be columns the board can actually render."""

    #: Verbatim from ``tests/test_state_options_frozen`` — "Recieved" stays misspelled.
    FROZEN_OPTIONS = [
        "Recieved", "In Progress", "Ready", "Out for Delivery",
        "Delivered", "Cancelled", "Returned",
    ]

    def test_delivered_state_is_a_valid_option(self):
        self.assertIn(bf.DELIVERED_STATE, self.FROZEN_OPTIONS)

    def test_dispatch_state_is_a_valid_option(self):
        self.assertIn(bf.DISPATCH_STATE, self.FROZEN_OPTIONS)

    def test_no_new_state_was_invented(self):
        self.assertEqual(bf.DELIVERED_STATE, "Delivered")
        self.assertEqual(bf.DISPATCH_STATE, "Out for Delivery")

    def test_both_aliases_are_declared(self):
        self.assertEqual(
            bf.STATE_FIELD_ALIASES,
            ("custom_sales_invoice_state", "sales_invoice_state"),
        )


if __name__ == "__main__":
    unittest.main()

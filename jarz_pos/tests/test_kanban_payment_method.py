"""The board/receipt payment-method resolver.

Guards the defect reported on 2026-08-28: a Talabat (delivery partner) order paid
ONLINE showed a green "Cash" badge on the Delivered column and printed "Cash" on
the receipt. The resolver classified the Payment Entry's ``paid_to`` ledger by
substring and ended in ``else: "Cash"``, so every ledger it did not recognise was
asserted to be cash -- including the partner AR subaccount ("Talabat - J") that an
online partner order is booked into, and every Payment Gateway ledger.

Two further cases in the same function are pinned here because they produced the
same wrong word on a card:

* A COD order switched to InstaPay after dispatch. That flow posts a Journal Entry
  and rewrites the Courier Transaction, never the Payment Entry -- which still
  points at Courier Outstanding. The Courier Transaction was consulted last, behind
  an ``if invoice not in method_map`` guard the Payment Entry had always already
  satisfied, so the switch was invisible and the card kept saying Cash.
* An unreadable ledger. It must leave the invoice OUT of the map so the card falls
  back to its status badge, rather than inventing a collection method.

These are pure mapping assertions over stubbed queries: no site data is touched.
"""

import unittest
from unittest.mock import patch

from jarz_pos.api import kanban


COMPANY_ABBR = "J"

#: ``Account`` rows the stub serves, keyed by name.
ACCOUNT_ROWS = {
    f"Cash In Hand - {COMPANY_ABBR}": {"account_type": "Cash", "parent_account": f"Current Assets - {COMPANY_ABBR}"},
    # A branch till is named after its POS profile, never "Cash".
    f"Nasr City - {COMPANY_ABBR}": {"account_type": "Cash", "parent_account": f"Cash In Hand - {COMPANY_ABBR}"},
    # A till typed Bank but parented under Cash In Hand: _get_cash_account allows this.
    f"Dokki - {COMPANY_ABBR}": {"account_type": "Bank", "parent_account": f"Cash In Hand - {COMPANY_ABBR}"},
    f"Bank Account - {COMPANY_ABBR}": {"account_type": "Bank", "parent_account": f"Bank Accounts - {COMPANY_ABBR}"},
    f"Mobile Wallet - {COMPANY_ABBR}": {"account_type": "Bank", "parent_account": f"Bank Accounts - {COMPANY_ABBR}"},
    f"Courier Outstanding - {COMPANY_ABBR}": {"account_type": "Receivable", "parent_account": f"Accounts Receivable - {COMPANY_ABBR}"},
    # The ledger at the heart of the bug: resolve_online_partner_paid_to books an
    # online partner order into an AR subaccount named after the partner.
    f"Talabat - {COMPANY_ABBR}": {"account_type": "Receivable", "parent_account": f"Accounts Receivable - {COMPANY_ABBR}"},
    # A payment gateway ledger, which carries no method in its name at all.
    f"Kashier Collections - {COMPANY_ABBR}": {"account_type": "Bank", "parent_account": f"Bank Accounts - {COMPANY_ABBR}"},
}


class _Row(dict):
    """A dict that also answers attribute access, like a frappe._dict result row."""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - surfaces stub gaps loudly
            raise AttributeError(item) from exc


def _make_get_all(*, payment_entries=None, collection_changes=None):
    """Stub ``frappe.get_all`` for the three doctypes the resolver reads.

    ``payment_entries`` maps invoice name -> paid_to account.
    ``collection_changes`` maps invoice name -> the payment_mode recorded on a
    Courier Transaction whose notes carry a collection change.

    The stub asserts that the resolver does NOT filter those rows by settlement
    status: a settled row must still be served, because the note outlives the
    settlement and the Payment Entry it corrects does too.
    """
    payment_entries = payment_entries or {}
    collection_changes = collection_changes or {}
    pe_name_for = {inv: "PE-%s" % inv for inv in payment_entries}

    def _get_all(doctype, filters=None, fields=None, **kwargs):
        filters = filters or {}

        if doctype == "Courier Transaction":
            assert "status" not in filters, (
                "the collection-change lookup must not exclude settled rows"
            )
            wanted = set(filters.get("reference_invoice", [None, []])[1])
            return [
                _Row(reference_invoice=inv, payment_mode=mode, modified="2026-08-28 12:00:00")
                for inv, mode in collection_changes.items()
                if inv in wanted
            ]

        if doctype == "Payment Entry Reference":
            wanted = set(filters.get("reference_name", [None, []])[1])
            return [
                _Row(reference_name=inv, parent=pe_name_for[inv])
                for inv in payment_entries
                if inv in wanted
            ]

        if doctype == "Payment Entry":
            wanted = set(filters.get("name", [None, []])[1])
            return [
                _Row(name=pe_name_for[inv], paid_to=account)
                for inv, account in payment_entries.items()
                if pe_name_for[inv] in wanted
            ]

        if doctype == "Account":
            wanted = filters.get("name", [None, []])[1]
            return [
                _Row(name=name, **ACCOUNT_ROWS[name])
                for name in wanted
                if name in ACCOUNT_ROWS
            ]

        raise AssertionError("unexpected doctype in resolver: %s" % doctype)

    return _get_all


def _paid(name, **extra):
    row = {"name": name, "outstanding_amount": 0.0, "status": "Paid"}
    row.update(extra)
    return row


class TestClassifyCollectionAccount(unittest.TestCase):
    """The ledger -> method mapping, in isolation."""

    def _classify(self, account):
        return kanban._classify_collection_account(account, ACCOUNT_ROWS.get(account))

    def test_mobile_wallet_ledger(self):
        self.assertEqual(self._classify(f"Mobile Wallet - {COMPANY_ABBR}"), "Mobile Wallet")

    def test_bank_account_ledger_is_instapay(self):
        self.assertEqual(self._classify(f"Bank Account - {COMPANY_ABBR}"), "Instapay")

    def test_courier_outstanding_is_cash(self):
        """The courier is holding the customer's banknotes until settlement."""
        self.assertEqual(self._classify(f"Courier Outstanding - {COMPANY_ABBR}"), "Cash")

    def test_branch_till_is_cash_despite_its_name(self):
        self.assertEqual(self._classify(f"Nasr City - {COMPANY_ABBR}"), "Cash")

    def test_bank_typed_till_under_cash_in_hand_is_cash(self):
        self.assertEqual(self._classify(f"Dokki - {COMPANY_ABBR}"), "Cash")

    def test_partner_receivable_is_not_a_collection(self):
        """The regression. This ledger used to fall through to "Cash"."""
        self.assertIsNone(self._classify(f"Talabat - {COMPANY_ABBR}"))

    def test_gateway_ledger_is_not_guessed(self):
        self.assertIsNone(self._classify(f"Kashier Collections - {COMPANY_ABBR}"))

    def test_unknown_ledger_is_not_guessed(self):
        self.assertIsNone(kanban._classify_collection_account("Some Ledger - J", None))

    def test_blank_account(self):
        self.assertIsNone(kanban._classify_collection_account("", None))


class TestActualPaymentMethodMap(unittest.TestCase):
    """End-to-end precedence over stubbed queries."""

    def _resolve(self, rows, *, payment_entries=None, collection_changes=None):
        stub = _make_get_all(
            payment_entries=payment_entries, collection_changes=collection_changes
        )
        with patch.object(kanban.frappe, "get_all", side_effect=stub):
            return kanban._get_actual_payment_method_map(rows)

    def test_online_partner_order_reports_its_declared_method(self):
        """The reported defect: a Talabat order paid online must not say Cash."""
        result = self._resolve(
            [_paid("SI-TALABAT-1", custom_payment_method="Instapay", sales_partner="Talabat")],
            payment_entries={"SI-TALABAT-1": f"Talabat - {COMPANY_ABBR}"},
        )
        self.assertEqual(result.get("SI-TALABAT-1"), "Instapay")

    def test_online_partner_order_without_declared_method_reports_online(self):
        result = self._resolve(
            [_paid("SI-TALABAT-2", custom_payment_method=None, sales_partner="Talabat")],
            payment_entries={"SI-TALABAT-2": f"Talabat - {COMPANY_ABBR}"},
        )
        self.assertEqual(result.get("SI-TALABAT-2"), "Online")

    def test_gateway_collection_reports_the_declared_method(self):
        result = self._resolve(
            [_paid("SI-GATEWAY-1", custom_payment_method="Kashier Card")],
            payment_entries={"SI-GATEWAY-1": f"Kashier Collections - {COMPANY_ABBR}"},
        )
        self.assertEqual(result.get("SI-GATEWAY-1"), "Kashier Card")

    def test_cash_at_the_till_still_reports_cash(self):
        result = self._resolve(
            [_paid("SI-CASH-1", custom_payment_method="Cash")],
            payment_entries={"SI-CASH-1": f"Nasr City - {COMPANY_ABBR}"},
        )
        self.assertEqual(result.get("SI-CASH-1"), "Cash")

    def test_instapay_collection_still_reports_instapay(self):
        result = self._resolve(
            [_paid("SI-IPY-1", custom_payment_method="Cash")],
            payment_entries={"SI-IPY-1": f"Bank Account - {COMPANY_ABBR}"},
        )
        # The ledger outranks the method the order was TAKEN with: a COD order
        # collected by InstaPay must stop printing "Cash".
        self.assertEqual(result.get("SI-IPY-1"), "Instapay")

    def test_mobile_wallet_collection(self):
        result = self._resolve(
            [_paid("SI-WAL-1")],
            payment_entries={"SI-WAL-1": f"Mobile Wallet - {COMPANY_ABBR}"},
        )
        self.assertEqual(result.get("SI-WAL-1"), "Mobile Wallet")

    def test_cod_settled_against_courier_outstanding_is_cash(self):
        result = self._resolve(
            [_paid("SI-COD-1", custom_payment_method="Cash")],
            payment_entries={"SI-COD-1": f"Courier Outstanding - {COMPANY_ABBR}"},
        )
        self.assertEqual(result.get("SI-COD-1"), "Cash")

    def test_post_dispatch_collection_change_outranks_the_payment_entry(self):
        """The switch posts a JE, never a PE, so the Courier Transaction is the truth.

        The Payment Entry still points at Courier Outstanding here. Before the fix
        that answered "Cash" first and the collection change was never consulted.
        """
        result = self._resolve(
            [_paid("SI-SWITCH-1", custom_payment_method="Instapay")],
            payment_entries={"SI-SWITCH-1": f"Courier Outstanding - {COMPANY_ABBR}"},
            collection_changes={"SI-SWITCH-1": "Instapay"},
        )
        self.assertEqual(result.get("SI-SWITCH-1"), "Instapay")

    def test_collection_change_survives_courier_settlement(self):
        """The label must not revert to Cash once the courier transaction settles.

        The stub serves the change row regardless of status and asserts the query
        carries no status filter -- the exact filter that used to make this flip.
        """
        result = self._resolve(
            [_paid("SI-SETTLED-1", custom_payment_method="Instapay")],
            payment_entries={"SI-SETTLED-1": f"Courier Outstanding - {COMPANY_ABBR}"},
            collection_changes={"SI-SETTLED-1": "Instapay"},
        )
        self.assertEqual(result.get("SI-SETTLED-1"), "Instapay")

    def test_unreadable_ledger_with_nothing_declared_is_omitted(self):
        """Never invent a method. An absent key makes the card show its status."""
        result = self._resolve(
            [_paid("SI-MYSTERY-1", custom_payment_method=None, sales_partner=None)],
            payment_entries={"SI-MYSTERY-1": "Suspense - J"},
        )
        self.assertNotIn("SI-MYSTERY-1", result)

    def test_unpaid_invoice_falls_back_to_the_declared_method(self):
        result = self._resolve(
            [
                {
                    "name": "SI-OPEN-1",
                    "outstanding_amount": 280.0,
                    "status": "Unpaid",
                    "custom_payment_method": "Instapay",
                }
            ]
        )
        self.assertEqual(result.get("SI-OPEN-1"), "Instapay")

    def test_empty_input(self):
        self.assertEqual(self._resolve([]), {})


if __name__ == "__main__":
    unittest.main()

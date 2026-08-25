"""An approved custom shipping override must reach the courier position.

Settlement never re-derives freight from the Sales Invoice — both surfaces read
``Courier Transaction.shipping_amount`` verbatim. So writing an approved override
to the invoice alone leaves settlement on the old territory rate whenever the
request was raised after dispatch, which is the normal case. These tests pin the
two things approval has to move: the CT itself, and the freight accrual behind it.
"""

import unittest
from unittest.mock import MagicMock, patch


def _raise(msg, *_a, **_kw):
    raise ValueError(msg)


def _mock_frappe(cts, *, company="JARZ"):
    """Wire a frappe mock around a fixed set of Courier Transaction rows."""
    m = MagicMock()
    m.utils.nowdate.return_value = "2026-08-25"
    m.utils.flt = lambda v, precision=None: round(float(v or 0), precision or 2)
    m.session.user = "manager@example.com"

    unsettled = [c for c in cts if c.get("status") != "Settled"]
    m.get_all.side_effect = lambda doctype, **kw: (
        list(unsettled) if doctype == "Courier Transaction" else []
    )

    def _exists(doctype, filters=None):
        if doctype == "Courier Transaction":
            if filters and filters.get("status") == ["!=", "Settled"]:
                return bool(unsettled)
            return bool(cts)
        return True

    m.db.exists.side_effect = _exists
    m.db.get_value.side_effect = lambda dt, name, field=None, **kw: (
        company if (dt == "Sales Invoice" and field == "company") else None
    )
    m.throw.side_effect = _raise

    je = MagicMock()
    je.name = "ACC-JV-ADJ-0001"
    je.accounts = []
    je.append.side_effect = lambda _table, row: je.accounts.append(row)
    m.new_doc.return_value = je
    return m, je


class ApprovedOverrideReachesSettlement(unittest.TestCase):

    def _run(self, cts, new_amount, *, reverting=False, existing_je=None):
        from jarz_pos.services import delivery_handling as dh

        m, je = _mock_frappe(cts)
        with patch.object(dh, "frappe", m), \
             patch.object(dh, "_", lambda s: s), \
             patch.object(dh, "_find_existing_je_by_tag", return_value=existing_je), \
             patch.object(dh, "get_freight_expense_account", return_value="Freight - J"), \
             patch.object(dh, "get_creditors_account", return_value="Creditors - J"), \
             patch.object(dh, "validate_account_exists", lambda a: None):
            result = dh.apply_shipping_override_to_courier_position(
                "SINV-1", new_amount, request_name="CSR-1", reverting=reverting,
            )
        return result, m, je

    def test_dispatched_order_moves_the_courier_transaction(self):
        """The reported bug: approval left the CT — and therefore settlement — at 85."""
        cts = [{
            "name": "CT-1", "amount": 0.0, "shipping_amount": 85.0,
            "party_type": "Employee", "party": "HR-EMP-000002",
            "is_partner_order": 0, "delivery_partner": None, "status": "Unsettled",
        }]
        result, m, _je = self._run(cts, 160.0)

        self.assertEqual(result["courier_transaction"], "CT-1")
        self.assertEqual(result["previous_amount"], 85.0)
        m.db.set_value.assert_any_call(
            "Courier Transaction", "CT-1", "shipping_amount", 160.0, update_modified=True,
        )

    def test_freight_delta_is_booked_not_the_whole_amount(self):
        """Dispatch already accrued 85; only the 75 difference may be posted."""
        cts = [{
            "name": "CT-1", "amount": 0.0, "shipping_amount": 85.0,
            "party_type": "Employee", "party": "HR-EMP-000002",
            "is_partner_order": 0, "delivery_partner": None, "status": "Unsettled",
        }]
        result, _m, je = self._run(cts, 160.0)

        self.assertEqual(result["journal_entry"], "ACC-JV-ADJ-0001")
        freight = next(a for a in je.accounts if a["account"] == "Freight - J")
        payable = next(a for a in je.accounts if a["account"] == "Creditors - J")
        self.assertEqual(freight["debit_in_account_currency"], 75.0)
        self.assertEqual(payable["credit_in_account_currency"], 75.0)
        self.assertEqual(payable["party"], "HR-EMP-000002")
        je.submit.assert_called_once()

    def test_lowering_the_override_reverses_the_direction(self):
        cts = [{
            "name": "CT-1", "amount": 0.0, "shipping_amount": 100.0,
            "party_type": "Employee", "party": "HR-EMP-000002",
            "is_partner_order": 0, "delivery_partner": None, "status": "Unsettled",
        }]
        _result, _m, je = self._run(cts, 40.0)

        freight = next(a for a in je.accounts if a["account"] == "Freight - J")
        payable = next(a for a in je.accounts if a["account"] == "Creditors - J")
        self.assertEqual(freight["credit_in_account_currency"], 60.0)
        self.assertEqual(payable["debit_in_account_currency"], 60.0)

    def test_partner_order_adjusts_the_partner_payable_not_creditors(self):
        """A partner order was accrued against the partner's own settlement account."""
        from jarz_pos.services import delivery_handling as dh

        cts = [{
            "name": "CT-P", "amount": 0.0, "shipping_amount": 50.0,
            "party_type": "Supplier", "party": "SUP-COURIER",
            "is_partner_order": 1, "delivery_partner": "Talabat", "status": "Unsettled",
        }]
        m, je = _mock_frappe(cts)
        with patch.object(dh, "frappe", m), \
             patch.object(dh, "_", lambda s: s), \
             patch.object(dh, "_find_existing_je_by_tag", return_value=None), \
             patch.object(dh, "get_freight_expense_account", return_value="Freight - J"), \
             patch.object(dh, "get_creditors_account", return_value="Creditors - J"), \
             patch.object(dh, "_get_partner_settlement_account", return_value="Talabat Payable - J"), \
             patch.object(dh, "get_delivery_partner_supplier", return_value="SUP-TALABAT"), \
             patch.object(dh, "validate_account_exists", lambda a: None):
            dh.apply_shipping_override_to_courier_position(
                "SINV-1", 90.0, request_name="CSR-1",
            )

        payable = next(a for a in je.accounts if a["account"] == "Talabat Payable - J")
        self.assertEqual(payable["credit_in_account_currency"], 40.0)
        self.assertEqual(payable["party"], "SUP-TALABAT")
        self.assertTrue(all(a["account"] != "Creditors - J" for a in je.accounts))

    def test_undispatched_order_is_a_no_op(self):
        """No CT yet: the override is picked up when the CT is born at dispatch."""
        result, m, _je = self._run([], 160.0)

        self.assertIsNone(result["courier_transaction"])
        self.assertIsNone(result["journal_entry"])
        m.new_doc.assert_not_called()

    def test_already_settled_order_is_refused(self):
        """The money has moved; silently rewriting a settled CT would break the books."""
        cts = [{
            "name": "CT-1", "amount": 0.0, "shipping_amount": 85.0,
            "party_type": "Employee", "party": "HR-EMP-000002",
            "is_partner_order": 0, "delivery_partner": None, "status": "Settled",
        }]
        with self.assertRaises(ValueError) as ctx:
            self._run(cts, 160.0)
        self.assertIn("already settled", str(ctx.exception))

    def test_unchanged_amount_posts_nothing(self):
        cts = [{
            "name": "CT-1", "amount": 0.0, "shipping_amount": 85.0,
            "party_type": "Employee", "party": "HR-EMP-000002",
            "is_partner_order": 0, "delivery_partner": None, "status": "Unsettled",
        }]
        result, m, _je = self._run(cts, 85.0)

        self.assertIsNone(result["journal_entry"])
        m.new_doc.assert_not_called()

    def test_adjustment_is_idempotent_per_request(self):
        """A retried approval must not double-book the difference."""
        cts = [{
            "name": "CT-1", "amount": 0.0, "shipping_amount": 85.0,
            "party_type": "Employee", "party": "HR-EMP-000002",
            "is_partner_order": 0, "delivery_partner": None, "status": "Unsettled",
        }]
        result, _m, je = self._run(cts, 160.0, existing_je="ACC-JV-EXISTING")

        self.assertEqual(result["journal_entry"], "ACC-JV-EXISTING")
        je.submit.assert_not_called()

    def test_ambiguous_courier_position_is_refused(self):
        cts = [
            {"name": "CT-1", "amount": 0.0, "shipping_amount": 85.0, "party_type": "Employee",
             "party": "E1", "is_partner_order": 0, "delivery_partner": None, "status": "Unsettled"},
            {"name": "CT-2", "amount": 0.0, "shipping_amount": 40.0, "party_type": "Employee",
             "party": "E1", "is_partner_order": 0, "delivery_partner": None, "status": "Unsettled"},
        ]
        with self.assertRaises(ValueError):
            self._run(cts, 160.0)


class RequestGate(unittest.TestCase):

    @patch("jarz_pos.api.custom_shipping.frappe")
    @patch("jarz_pos.api.custom_shipping._get_delivery_expense_amount")
    def test_request_refused_once_the_position_is_settled(self, _mock_exp, mock_frappe):
        from jarz_pos.api.custom_shipping import request_custom_shipping

        inv = MagicMock()
        inv.docstatus = 1
        mock_frappe.get_doc.return_value = inv
        mock_frappe.session.user = "sales@example.com"

        def _exists(doctype, filters=None):
            if doctype == "Sales Invoice":
                return True
            if doctype == "Courier Transaction":
                # Rows exist, but none of them are unsettled.
                return not (filters or {}).get("status")
            return False

        mock_frappe.db.exists.side_effect = _exists
        mock_frappe.throw.side_effect = _raise

        with self.assertRaises(ValueError) as ctx:
            request_custom_shipping("SINV-1", 160, "Far area with a much longer route today")
        self.assertIn("already settled", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

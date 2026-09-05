"""``change_invoice_shipping_address`` must not re-rate delivery on a SUBMITTED invoice.

Regression for ACC-SINV-2026-18096 (Woo 17176): an address change from Giza to
6th of October four minutes after submit rebuilt the Shipping Income row
(45 -> 60) and saved the submitted invoice with
``ignore_validate_update_after_submit``. The document went 685 -> 700 but the
ledger stayed at 685 -- ERPNext reposts on ``on_update_after_submit`` only when a
tax row's *account head* changes, and the rebuilt row keeps the same account.
Every later Payment Entry then died with "Allocated Amount cannot be greater than
outstanding amount", so the order sat Delivered, unpaid and unpayable.

The guard runs before the first write, so a refusal leaves the invoice untouched,
and the refusal message must reach the caller unwrapped (the endpoint used to
swallow every ``ValidationError`` into a generic "Failed to change ..." message).

Mock-based: no site, no DB. Runs under ``frappe.init`` alone (see tests/README.md).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import customer as customer_api


INVOICE = "ACC-SINV-TEST-0001"
ADDRESS = "Engi-Billing"
OLD_TERRITORY = "EGGIZA"
NEW_TERRITORY = "EG6OCT"


class _FakeInvoice:
    """Just enough of a Sales Invoice for the endpoint's reads and writes."""

    def __init__(self, docstatus: int, taxes):
        self.name = INVOICE
        self.docstatus = docstatus
        self.territory = OLD_TERRITORY
        self.taxes = [frappe._dict(t) for t in taxes]
        self.flags = frappe._dict()
        self.saved = False
        self.reloaded = False

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def reload(self):
        self.reloaded = True

    def calculate_taxes_and_totals(self):
        pass

    def save(self, **_kwargs):
        self.saved = True


def _shipping_row(amount: float):
    return {
        "description": f"Shipping Income ({OLD_TERRITORY})",
        "account_head": "Freight and Forwarding Charges - J",
        "tax_amount": amount,
    }


class AddressChangeFeeGuardTests(unittest.TestCase):
    """Table of (docstatus, booked row, new territory rate) -> allowed / refused."""

    def _run(self, invoice: _FakeInvoice, *, old_rate=(45.0, 35.0), new_rate=(60.0, 60.0)):
        """Drive the endpoint against ``invoice`` with the two territories' rates.

        Returns ``(result_or_None, exception_or_None, link_mock, db_mock)``.
        """
        territories = {
            OLD_TERRITORY: frappe._dict(delivery_income=old_rate[0], delivery_expense=old_rate[1]),
            NEW_TERRITORY: frappe._dict(delivery_income=new_rate[0], delivery_expense=new_rate[1]),
        }
        comment = MagicMock()
        comment.insert.return_value = comment

        def _get_doc(doctype, name=None):
            if isinstance(doctype, dict):
                return comment
            if doctype == "Sales Invoice":
                return invoice
            if doctype == "Address":
                return frappe._dict(city=NEW_TERRITORY)
            if doctype == "Territory":
                return territories[name]
            raise AssertionError(f"unexpected get_doc({doctype!r}, {name!r})")

        db = MagicMock()
        db.exists.return_value = True
        db.get_value.return_value = ""

        link = MagicMock()
        with patch.object(frappe, "db", db), \
                patch.object(frappe, "get_doc", side_effect=_get_doc), \
                patch.object(frappe, "log_error"), \
                patch.object(customer_api, "link_shipping_address_to_invoice", link), \
                patch.object(customer_api, "resolve_address_territory", return_value=NEW_TERRITORY), \
                patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None), \
                patch("jarz_pos.utils.delivery_utils.add_delivery_charges_to_taxes"):
            try:
                return customer_api.change_invoice_shipping_address(INVOICE, ADDRESS), None, link, db
            except Exception as exc:  # noqa: BLE001 - the type is asserted by the caller
                return None, exc, link, db

    # ── the 17176 shape ────────────────────────────────────────────────────────

    def test_submitted_invoice_with_a_different_fee_is_refused_before_any_write(self):
        inv = _FakeInvoice(docstatus=1, taxes=[_shipping_row(45.0)])

        result, exc, link, db = self._run(inv)

        self.assertIsNone(result)
        self.assertIsInstance(exc, frappe.ValidationError)
        # The reason reaches the caller, not the generic wrapper message.
        self.assertIn("already submitted", str(exc))
        self.assertIn(INVOICE, str(exc))
        self.assertIn("45.0 -> 60.0", str(exc))
        self.assertNotIn("Failed to change invoice shipping address", str(exc))
        # Nothing was touched: no address re-link, no territory / expense write,
        # no tax-row rebuild, no save on the submitted document.
        link.assert_not_called()
        db.set_value.assert_not_called()
        db.commit.assert_not_called()
        self.assertFalse(inv.saved)
        self.assertFalse(inv.reloaded)
        self.assertEqual(inv.territory, OLD_TERRITORY)
        self.assertEqual(len(inv.taxes), 1)
        self.assertEqual(inv.taxes[0].tax_amount, 45.0)

    def test_refusal_compares_against_the_booked_row_not_the_old_territory_rate(self):
        # A promo left the booked row at 0 even though the territory bills 45.
        # The rebuild would have charged 60 on a posted order -> refuse.
        inv = _FakeInvoice(docstatus=1, taxes=[_shipping_row(0.0)])

        result, exc, link, _db = self._run(inv)

        self.assertIsNone(result)
        self.assertIsInstance(exc, frappe.ValidationError)
        self.assertIn("0.0 -> 60.0", str(exc))
        link.assert_not_called()

    # ── still allowed ──────────────────────────────────────────────────────────

    def test_submitted_invoice_moving_to_a_territory_with_the_same_fee_is_allowed(self):
        # Same delivery income, different courier expense: the customer-facing
        # total does not move, so the change (and the expense update) go through.
        inv = _FakeInvoice(docstatus=1, taxes=[_shipping_row(45.0)])

        result, exc, link, db = self._run(inv, new_rate=(45.0, 60.0))

        self.assertIsNone(exc, f"unexpected refusal: {exc}")
        self.assertTrue(result["success"])
        self.assertTrue(result["territory_changed"])
        self.assertEqual(result["new_territory"], NEW_TERRITORY)
        self.assertEqual(result["new_income"], 45.0)
        self.assertEqual(result["new_expense"], 60.0)
        link.assert_called_once_with(INVOICE, ADDRESS)
        db.set_value.assert_any_call(
            "Sales Invoice", INVOICE, "territory", NEW_TERRITORY, update_modified=False
        )
        db.set_value.assert_any_call(
            "Sales Invoice", INVOICE, "custom_shipping_expense", 60.0, update_modified=False
        )

    def test_draft_invoice_may_still_be_re_rated(self):
        # Nothing is posted yet, so rebuilding the Shipping Income row is fine.
        inv = _FakeInvoice(docstatus=0, taxes=[_shipping_row(45.0)])

        result, exc, link, _db = self._run(inv)

        self.assertIsNone(exc, f"unexpected refusal: {exc}")
        self.assertTrue(result["territory_changed"])
        self.assertEqual(result["new_income"], 60.0)
        link.assert_called_once_with(INVOICE, ADDRESS)
        self.assertTrue(inv.saved, "the draft's Shipping Income row should be rebuilt")

    def test_submitted_invoice_without_a_shipping_income_row_is_not_refused(self):
        # The rebuild block is gated on had_shipping_income_row, so the total
        # cannot change here; refusing would only block a harmless address fix.
        inv = _FakeInvoice(docstatus=1, taxes=[])

        result, exc, link, _db = self._run(inv)

        self.assertIsNone(exc, f"unexpected refusal: {exc}")
        self.assertTrue(result["territory_changed"])
        link.assert_called_once_with(INVOICE, ADDRESS)
        self.assertFalse(inv.saved, "no row to rebuild, so no save on the submitted document")

    def test_unchanged_territory_is_never_refused(self):
        inv = _FakeInvoice(docstatus=1, taxes=[_shipping_row(45.0)])

        with patch.object(customer_api, "resolve_address_territory", return_value=OLD_TERRITORY):
            db = MagicMock()
            db.exists.return_value = True
            db.get_value.return_value = ""
            with patch.object(frappe, "db", db), \
                    patch.object(frappe, "get_doc", side_effect=lambda dt, name=None: inv if dt == "Sales Invoice" else frappe._dict(city=OLD_TERRITORY)), \
                    patch.object(frappe, "log_error"), \
                    patch.object(customer_api, "link_shipping_address_to_invoice") as link, \
                    patch("jarz_pos.api.manager.get_invoice_hard_mutation_blocker", return_value=None):
                result = customer_api.change_invoice_shipping_address(INVOICE, ADDRESS)

        self.assertTrue(result["success"])
        self.assertFalse(result["territory_changed"])
        link.assert_called_once_with(INVOICE, ADDRESS)
        self.assertFalse(inv.saved)


if __name__ == "__main__":
    unittest.main()

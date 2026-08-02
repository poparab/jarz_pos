"""Tests for resolving the shipping income an invoice actually carried.

A zero shipping income injects NO tax row (see invoice_creation's override
path), and ``custom_delivery_income`` is a NOT NULL Currency column defaulting
to 0 — so neither "no row" nor "stored 0" means "not set" on its own. These
tests pin the rules that let the Kanban card, the order details dialog and the
amendment flow agree on one answer.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _tax(description, amount):
    return {"description": description, "tax_amount": amount}


def _invoice(taxes):
    return SimpleNamespace(get=lambda field, default=None: taxes if field == "taxes" else default)


class TestReadInvoiceShippingIncome(unittest.TestCase):
    """Test class for jarz_pos.utils.invoice_utils.read_invoice_shipping_income."""

    def test_returns_the_charged_amount(self):
        from jarz_pos.utils.invoice_utils import read_invoice_shipping_income

        invoice = _invoice([_tax("Shipping Income (EGRSHEROUK)", 60.0)])

        self.assertEqual(read_invoice_shipping_income(invoice), 60.0)

    def test_invoice_without_a_shipping_row_charged_nothing(self):
        """Free delivery injects no row; that is a real zero, not missing data."""
        from jarz_pos.utils.invoice_utils import read_invoice_shipping_income

        invoice = _invoice([_tax("Freight and Forwarding Charges", 50.0)])

        self.assertEqual(read_invoice_shipping_income(invoice), 0.0)

    def test_invoice_with_no_taxes_at_all_is_zero_not_the_territory_rate(self):
        """Pickup / no-courier / free-delivery orders carry no tax rows at all."""
        from jarz_pos.utils.invoice_utils import read_invoice_shipping_income

        self.assertEqual(read_invoice_shipping_income(_invoice([])), 0.0)

    def test_description_match_is_case_and_suffix_insensitive(self):
        from jarz_pos.utils.invoice_utils import read_invoice_shipping_income

        invoice = _invoice([_tax("shipping income (Some Territory)", 45.5)])

        self.assertEqual(read_invoice_shipping_income(invoice), 45.5)


class TestAmendmentPreservesShippingIncome(unittest.TestCase):
    """Test class for jarz_pos.api.manager._resolve_amendment_delivery_income."""

    def _resolve(self, *, source_taxes, territory_income, requested=None, territory="T1"):
        from jarz_pos.api import manager

        source_invoice = SimpleNamespace(
            get=lambda field, default=None: (
                source_taxes if field == "taxes" else (territory if field == "territory" else default)
            )
        )
        with patch.object(manager, "_territory_default_delivery_income", return_value=territory_income):
            return manager._resolve_amendment_delivery_income(source_invoice, requested)

    def test_charged_shipping_is_not_dropped_to_zero(self):
        """The regression: omitting the key used to read a stored 0 and zero the income."""
        resolved = self._resolve(
            source_taxes=[_tax("Shipping Income (EGRSHEROUK)", 60.0)],
            territory_income=0.0,
        )

        self.assertEqual(resolved, 60.0)

    def test_territory_rate_is_left_unpinned(self):
        """An order charging its territory's rate was never overridden — re-derive it."""
        resolved = self._resolve(
            source_taxes=[_tax("Shipping Income (EGRSHEROUK)", 60.0)],
            territory_income=60.0,
        )

        self.assertIsNone(resolved, "Must stay unset so a new territory can re-price it")

    def test_deliberate_free_delivery_survives(self):
        """Zero against a paying territory is a real override and must be preserved."""
        resolved = self._resolve(
            source_taxes=[_tax("Freight and Forwarding Charges", 50.0)],
            territory_income=60.0,
        )

        self.assertEqual(resolved, 0.0)

    def test_custom_amount_survives(self):
        resolved = self._resolve(
            source_taxes=[_tax("Shipping Income (EGRSHEROUK)", 100.0)],
            territory_income=60.0,
        )

        self.assertEqual(resolved, 100.0)

    def test_row_less_invoice_stays_free(self):
        """No shipping row means the order was free; an edit must not re-charge it."""
        resolved = self._resolve(source_taxes=[], territory_income=60.0)

        self.assertEqual(resolved, 0.0)

    def test_explicit_amount_wins_over_the_source(self):
        resolved = self._resolve(
            source_taxes=[_tax("Shipping Income (EGRSHEROUK)", 60.0)],
            territory_income=60.0,
            requested=25,
        )

        self.assertEqual(resolved, 25.0)

    def test_explicit_zero_is_free_delivery_not_a_missing_key(self):
        resolved = self._resolve(
            source_taxes=[_tax("Shipping Income (EGRSHEROUK)", 60.0)],
            territory_income=60.0,
            requested=0,
        )

        self.assertEqual(resolved, 0.0)

    def test_empty_string_clears_the_override(self):
        resolved = self._resolve(
            source_taxes=[_tax("Shipping Income (EGRSHEROUK)", 100.0)],
            territory_income=60.0,
            requested="",
        )

        self.assertIsNone(resolved, "'' means revert to the territory default")

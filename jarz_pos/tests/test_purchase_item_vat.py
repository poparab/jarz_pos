"""Per-item VAT on a Purchase Invoice.

We buy some items with VAT and some without, which an invoice-level
Purchase Taxes and Charges Template cannot express — it taxes every line at one
rate. The line-level answer is ``item_tax_template``, but on its own it does
nothing: ERPNext charges tax by walking ``doc.taxes`` and looking each row's
account up in the line's item tax map, so a template naming an account with no
tax row is never consulted and the invoice submits with the VAT silently
missing.

``_ensure_item_tax_rows`` is what closes that gap, and these tests pin the two
properties that make it correct:

* a row is added for every account the lines' templates name, so the VAT is
  actually charged;
* that row is rate 0 and flagged ``set_by_item_tax_template``, which is what
  keeps ``get_item_tax_map`` from adopting it as the base rate for *every*
  line — otherwise turning VAT on for one item would tax the whole invoice.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api.purchase import _ensure_item_tax_rows

COMPANY = "Jarz"
VAT_ACCOUNT = "VAT 14% - JZ"
VAT_TEMPLATE = "Egypt VAT 14% - JZ"
OTHER_ACCOUNT = "Customs Duty - JZ"
OTHER_TEMPLATE = "Customs 5% - JZ"


class _Line(dict):
    """A Purchase Invoice Item row. ``dict`` already gives us ``.get``."""


class _PurchaseInvoice:
    """Minimal stand-in for the parts of the doc the helper touches."""

    def __init__(self, items, taxes=None):
        self.items = items
        self.taxes = list(taxes or [])

    def get(self, fieldname):
        return getattr(self, fieldname, None)

    def append(self, table, row):
        getattr(self, table).append(MagicMock(**row, **{"_row": row}))


def _template(*accounts):
    doc = MagicMock()
    doc.taxes = [MagicMock(tax_type=account) for account in accounts]
    return doc


def _appended(pi):
    """The plain dicts handed to ``append``, in order."""
    return [row._row for row in pi.taxes if hasattr(row, "_row")]


class _TaxRowCase(unittest.TestCase):
    """Shared plumbing: templates resolve, and every account is in-company."""

    def setUp(self):
        templates = {
            VAT_TEMPLATE: _template(VAT_ACCOUNT),
            OTHER_TEMPLATE: _template(OTHER_ACCOUNT),
        }
        self._cached_doc = patch(
            "frappe.get_cached_doc", side_effect=lambda _dt, name: templates[name]
        )
        self._cached_value = patch("frappe.get_cached_value", return_value=COMPANY)
        self._cached_doc.start()
        self._cached_value.start()
        self.addCleanup(self._cached_doc.stop)
        self.addCleanup(self._cached_value.stop)


class TestEnsureItemTaxRows(_TaxRowCase):
    def test_row_is_added_for_a_templated_line(self):
        """Without this row ERPNext never charges the VAT at all."""
        pi = _PurchaseInvoice([_Line(item_tax_template=VAT_TEMPLATE)])

        _ensure_item_tax_rows(pi, COMPANY)

        rows = _appended(pi)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account_head"], VAT_ACCOUNT)
        self.assertEqual(rows[0]["charge_type"], "On Net Total")

    def test_added_row_is_zero_rated_and_flagged(self):
        """The flag is the whole "only some items" mechanism.

        ``get_item_tax_map`` skips flagged rows when building the base rate each
        line inherits. A non-zero or unflagged row would tax every untemplated
        line on the invoice too.
        """
        pi = _PurchaseInvoice([_Line(item_tax_template=VAT_TEMPLATE)])

        _ensure_item_tax_rows(pi, COMPANY)

        row = _appended(pi)[0]
        self.assertEqual(row["rate"], 0)
        self.assertEqual(row["set_by_item_tax_template"], 1)

    def test_untemplated_lines_add_nothing(self):
        """A purchase with no VAT keeps a clean, empty taxes table."""
        pi = _PurchaseInvoice([_Line(item_code="RM-SUGAR"), _Line(item_tax_template="")])

        _ensure_item_tax_rows(pi, COMPANY)

        self.assertEqual(pi.taxes, [])

    def test_mixed_cart_adds_one_row_for_the_taxed_line_only(self):
        """The case that motivated the feature: some items taxed, some not."""
        pi = _PurchaseInvoice(
            [
                _Line(item_code="RM-SUGAR"),
                _Line(item_code="PKG-JAR", item_tax_template=VAT_TEMPLATE),
            ]
        )

        _ensure_item_tax_rows(pi, COMPANY)

        self.assertEqual([r["account_head"] for r in _appended(pi)], [VAT_ACCOUNT])

    def test_one_row_per_account_across_repeated_lines(self):
        """Three VAT-bearing items must not produce three identical tax rows."""
        pi = _PurchaseInvoice(
            [_Line(item_tax_template=VAT_TEMPLATE) for _ in range(3)]
        )

        _ensure_item_tax_rows(pi, COMPANY)

        self.assertEqual(len(_appended(pi)), 1)

    def test_distinct_templates_each_get_their_account(self):
        pi = _PurchaseInvoice(
            [
                _Line(item_tax_template=VAT_TEMPLATE),
                _Line(item_tax_template=OTHER_TEMPLATE),
            ]
        )

        _ensure_item_tax_rows(pi, COMPANY)

        self.assertEqual(
            sorted(r["account_head"] for r in _appended(pi)),
            sorted([VAT_ACCOUNT, OTHER_ACCOUNT]),
        )

    def test_an_existing_row_for_the_account_is_left_alone(self):
        """An invoice-level template already charging VAT must win.

        Appending our 0% row on top would give ERPNext two rows for one account
        and halve nothing — it would simply double-count the account.
        """
        existing = MagicMock(account_head=VAT_ACCOUNT)
        pi = _PurchaseInvoice([_Line(item_tax_template=VAT_TEMPLATE)], taxes=[existing])

        _ensure_item_tax_rows(pi, COMPANY)

        self.assertEqual(pi.taxes, [existing])

    def test_a_shipping_row_does_not_suppress_the_vat_row(self):
        """Why we do not rely on ERPNext's own append.

        ``append_taxes_from_item_tax_template`` bails out once ``taxes`` is
        non-empty, so a purchase that carried freight would lose its per-item
        VAT entirely.
        """
        freight = MagicMock(account_head="Freight and Forwarding Charges - JZ")
        pi = _PurchaseInvoice([_Line(item_tax_template=VAT_TEMPLATE)], taxes=[freight])

        _ensure_item_tax_rows(pi, COMPANY)

        self.assertIn(VAT_ACCOUNT, [r["account_head"] for r in _appended(pi)])


class TestCrossCompanyAccountsAreSkipped(unittest.TestCase):
    """A foreign account would fail ERPNext validation far less legibly."""

    def test_account_belonging_to_another_company_is_not_added(self):
        pi = _PurchaseInvoice([_Line(item_tax_template=VAT_TEMPLATE)])

        with patch("frappe.get_cached_doc", return_value=_template(VAT_ACCOUNT)), patch(
            "frappe.get_cached_value", return_value="Some Other Company"
        ):
            _ensure_item_tax_rows(pi, COMPANY)

        self.assertEqual(pi.taxes, [])


if __name__ == "__main__":
    unittest.main()

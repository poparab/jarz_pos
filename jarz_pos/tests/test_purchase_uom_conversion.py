"""The conversion factor a purchase line is booked with.

``create_purchase_invoice`` used to fall back to a factor of 1 whenever a line's
unit had no ``UOM Conversion Detail`` row on the Item. That does not fail — it
books the line silently wrong: 5 x "Box of 12" at 120 became 5 stock units at
120 each, the right invoice total but a twelfth of the stock and 12x the
valuation rate. The mobile reorder reaches it by replaying an old invoice whose
big unit has since been removed from the Item.

These tests pin the three cases that must hold together: a unit with no
conversion is refused by name, the stock unit (stated or implied) stays 1, and a
real conversion row is still honoured.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api import purchase as pu

COMPANY = "Jarz"
SUPPLIER = "Acme Supplies"
ITEM = "RM-Cups"
STOCK_UOM = "Nos"
BIG_UOM = "Box of 12"
WAREHOUSE = "Stores - JZ"


class _Refused(Exception):
    """What the stubbed ``frappe.throw`` raises."""


class _PurchaseInvoiceDoc:
    """Just enough of a Purchase Invoice for the line-building path."""

    def __init__(self):
        self.is_paid = 0
        self.mode_of_payment = None
        self.cash_bank_account = None
        self.items = []
        self.taxes = []
        self.name = "ACC-PINV-0001"
        self.status = "Unpaid"
        self.outstanding_amount = 10.0
        self.inserted = False
        self.submitted = False

    def get(self, fieldname, default=None):
        return getattr(self, fieldname, default)

    def append(self, table, row):
        entry = MagicMock(**row)
        entry.get = row.get
        getattr(self, table).append(entry)
        return entry

    def insert(self, *args, **kwargs):
        self.inserted = True

    def submit(self):
        self.submitted = True


class _UomConversionCase(unittest.TestCase):
    """Everything the endpoint touches, stubbed down to the UOM decision."""

    #: ``UOM Conversion Detail`` rows on the Item, by unit. Override per test.
    conversions = {}

    def setUp(self):
        self.doc = _PurchaseInvoiceDoc()
        self.conversion_lookups = []

        def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "Item" and fieldname == "stock_uom":
                return STOCK_UOM
            if doctype == "UOM Conversion Detail":
                self.conversion_lookups.append(filters)
                return self.conversions.get((filters or {}).get("uom"))
            return None

        def throw(message, *args, **kwargs):
            raise _Refused(message)

        # ``frappe.db`` is a bound-per-request Local proxy, so patching through
        # it fails outside a site. Standing in for the whole module sidesteps
        # that, as test_purchase_payment_defaults does.
        fake = MagicMock()
        fake.get_roles.return_value = ["Purchase Manager"]
        fake.new_doc.return_value = self.doc
        fake.defaults.get_user_default.return_value = COMPANY
        fake.db.get_single_value.return_value = 0  # bill_no not required
        fake.db.get_value.side_effect = get_value
        fake.throw.side_effect = throw

        patches = [
            patch.object(pu, "frappe", fake),
            # ``_`` is bound at import from the real frappe; translation lookups
            # need a site, so the message is passed through untouched.
            patch.object(pu, "_", lambda message: message),
            patch.object(pu, "resolve_purchase_warehouse", return_value=WAREHOUSE),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _create(self, **line):
        # An explicit empty item_tax_template keeps the VAT layer out of the
        # way — its precedence is covered by test_purchase_vat_default.
        row = {"item_code": ITEM, "qty": 5, "rate": 120, "item_tax_template": ""}
        row.update(line)
        return pu.create_purchase_invoice(supplier=SUPPLIER, items=[row])

    def _line(self):
        self.assertEqual(len(self.doc.items), 1)
        return self.doc.items[0]


class TestMissingConversionIsRefused(_UomConversionCase):
    conversions = {}

    def test_unit_with_no_conversion_is_refused_by_name(self):
        """The old 1:1 fallback booked a twelfth of the stock at 12x the rate."""
        with self.assertRaises(_Refused) as exc:
            self._create(uom=BIG_UOM)

        message = str(exc.exception)
        self.assertIn(ITEM, message)
        self.assertIn(BIG_UOM, message)
        self.assertIn(STOCK_UOM, message)
        self.assertIn("add the conversion on the Item", message)
        # Refused before anything was built or saved.
        self.assertEqual(self.doc.items, [])
        self.assertFalse(self.doc.inserted)
        self.assertFalse(self.doc.submitted)

    def test_a_zero_conversion_is_refused_too(self):
        """A row saying 0 states no more than a missing row does."""
        self.conversions = {BIG_UOM: 0}

        with self.assertRaises(_Refused):
            self._create(uom=BIG_UOM)

        self.assertFalse(self.doc.inserted)


class TestStockUomStaysOne(_UomConversionCase):
    conversions = {}

    def test_stock_uom_line_keeps_factor_one(self):
        """The stock unit needs no conversion row, and must not be refused."""
        self._create(uom=STOCK_UOM)

        line = self._line()
        self.assertEqual(line.get("conversion_factor"), 1)
        self.assertEqual(line.get("uom"), STOCK_UOM)
        self.assertEqual(self.conversion_lookups, [])
        self.assertTrue(self.doc.submitted)

    def test_blank_uom_is_booked_in_the_stock_uom(self):
        """No unit means the stock unit — the path labels.bill_print_order uses."""
        self._create()

        line = self._line()
        self.assertEqual(line.get("conversion_factor"), 1)
        self.assertEqual(line.get("uom"), STOCK_UOM)
        self.assertEqual(self.conversion_lookups, [])
        self.assertTrue(self.doc.submitted)


class TestExistingConversionIsUsed(_UomConversionCase):
    conversions = {BIG_UOM: 12}

    def test_existing_conversion_is_used(self):
        self._create(uom=BIG_UOM)

        line = self._line()
        self.assertEqual(line.get("conversion_factor"), 12.0)
        self.assertEqual(line.get("uom"), BIG_UOM)
        self.assertEqual(
            self.conversion_lookups, [{"parent": ITEM, "parenttype": "Item", "uom": BIG_UOM}]
        )
        self.assertTrue(self.doc.submitted)


if __name__ == "__main__":
    unittest.main()

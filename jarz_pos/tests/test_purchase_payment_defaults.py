"""The payment fields a Purchase Invoice is created with.

A Property Setter on this site overrides ``is_paid`` to default to **1** (and
``mode_of_payment`` to "cash") — a leftover from someone customising the desk
form. ``frappe.new_doc`` applies Property Setters, so a doc is *not* born with
the defaults shipped in the doctype JSON.

``create_purchase_invoice`` used to set ``is_paid`` only in the paid branch and
leave it untouched otherwise, which meant every unpaid (credit) purchase reached
``validate`` claiming to be paid with no ``cash_bank_account`` resolved. ERPNext
threw "Payment Entry will not be created since 'Cash or Bank Account' was not
specified" — a message that names neither ``is_paid=0``, which the caller did
send, nor the customisation that overrode it.

So these tests pin the property that fixes it: the endpoint states every payment
field outright, and the doc reflects the *caller's* intent regardless of what a
form customisation defaults to. The doc under test is deliberately seeded the
way the real site hands it over — pre-set to paid — because a stand-in starting
at 0 would pass even against the broken code.

Pure ``unittest`` with mocks — no site.
"""

import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.api import purchase as pu

COMPANY = "Jarz"
SUPPLIER = "Acme Supplies"
ITEM = "RM-Sugar"
WAREHOUSE = "Raw Material - JZ"
CASH_ACCOUNT = "Cash - JZ"


class _PurchaseInvoiceDoc:
    """Stand-in for the doc ``frappe.new_doc`` returns *on this site*.

    Seeded with the Property Setter's values, not the doctype JSON's, so a
    regression that stops clearing them fails here.
    """

    def __init__(self):
        self.is_paid = 1
        self.mode_of_payment = "cash"
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


class _PaymentDefaultsCase(unittest.TestCase):
    """Everything the endpoint touches, stubbed down to the payment decision."""

    def setUp(self):
        self.doc = _PurchaseInvoiceDoc()

        # ``frappe.db`` is a bound-per-request Local proxy, so patching through
        # it fails outside a site. Standing in for the whole module sidesteps
        # that and keeps these tests site-free.
        fake = MagicMock()
        fake.get_roles.return_value = ["Purchase Manager"]
        fake.new_doc.return_value = self.doc
        fake.defaults.get_user_default.return_value = COMPANY
        fake.db.get_single_value.return_value = 0  # bill_no not required
        fake.db.get_value.return_value = "Kg"  # Item.stock_uom
        fake.db.exists.return_value = False  # no POS Profile by that name

        patches = [
            patch.object(pu, "frappe", fake),
            patch.object(pu, "resolve_purchase_warehouse", return_value=WAREHOUSE),
            patch.object(pu, "_get_default_cash_account", return_value=CASH_ACCOUNT),
            patch.object(pu, "_get_mop_account_account", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _create(self, **kwargs):
        # An explicit empty item_tax_template keeps the VAT layer out of the
        # way — its precedence is covered by test_purchase_vat_default.
        return pu.create_purchase_invoice(
            supplier=SUPPLIER,
            items=[{"item_code": ITEM, "qty": 1, "rate": 10, "item_tax_template": ""}],
            **kwargs,
        )

    def test_unpaid_invoice_is_not_marked_paid(self):
        """is_paid=0 must survive a Property Setter that defaults it to 1."""
        self._create(is_paid=0)

        self.assertEqual(
            self.doc.is_paid,
            0,
            "an unpaid purchase reached validate() claiming to be paid — ERPNext "
            "then throws about a missing 'Cash or Bank Account'",
        )
        self.assertTrue(self.doc.inserted, "the invoice should still be created")

    def test_unpaid_invoice_carries_no_payment_fields(self):
        """A credit purchase names no mode of payment and no cash account."""
        self._create(is_paid=0)

        self.assertIsNone(self.doc.mode_of_payment)
        self.assertIsNone(self.doc.cash_bank_account)

    def test_unpaid_is_the_default_when_the_caller_says_nothing(self):
        """Omitting is_paid means unpaid, not whatever the form defaults to."""
        self._create()

        self.assertEqual(self.doc.is_paid, 0)
        self.assertIsNone(self.doc.cash_bank_account)

    def test_paid_invoice_still_resolves_its_cash_account(self):
        """The paid branch is unchanged: mode and account are both set."""
        self._create(is_paid=1, payment_option="cash")

        self.assertEqual(self.doc.is_paid, 1)
        self.assertEqual(self.doc.mode_of_payment, "Cash")
        self.assertEqual(self.doc.cash_bank_account, CASH_ACCOUNT)


if __name__ == "__main__":
    unittest.main()

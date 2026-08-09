"""The POS stock-update suppression hook, and the field read that broke it.

Stock leaves the warehouse through the Delivery Note and nothing else — a
return reverses stock with a Sales Return Delivery Note, so an invoice that
also moved stock would be reversed once and deducted twice. ERPNext's
``set_pos_fields()`` re-applies the POS Profile's own ``update_stock`` during
validate for any document carrying ``is_pos``, so this hook has to force it
back to 0 on every path.

It was silently not doing that. ``int(getattr(doc, "is_pos", 0) or 0)`` raised
``TypeError: int() argument must be ... not 'object'`` roughly sixty times a
week: when a Document's instance dict has no entry for a field, attribute
lookup falls through to the class, where Frappe's generated property descriptor
lives, so ``getattr`` returns the DESCRIPTOR rather than a value. The caller
caught and logged it, which turned a broken guard into a log line nobody read.

These tests pin the reads, including that exact descriptor case.

Pure ``unittest`` with stand-ins — no site.
"""

import unittest

from jarz_pos.events.sales_invoice import (
    _check_flag,
    suppress_pos_invoice_stock_update,
)


class _Descriptor:
    """Stands in for Frappe's generated class-level property descriptor.

    The point is only that it is a plain object: ``int()`` on it raises, which
    is precisely what took the real hook down.
    """


class _Doc:
    """A Document stand-in whose ``get`` reads the instance dict, like Frappe's."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def get(self, fieldname, default=None):
        return self.__dict__.get(fieldname, default)


class _DescriptorDoc:
    """A doc where the field is MISSING from the instance but present on the class.

    This reproduces the production failure exactly: ``get`` returns None (no
    instance value) and a naive ``getattr`` would find the class attribute.
    """

    is_pos = _Descriptor()
    update_stock = _Descriptor()

    def get(self, fieldname, default=None):
        return self.__dict__.get(fieldname, default)


class TestCheckFlag(unittest.TestCase):
    def test_reads_plain_values(self):
        doc = _Doc(is_pos=1, update_stock=0)
        self.assertEqual(_check_flag(doc, "is_pos"), 1)
        self.assertEqual(_check_flag(doc, "update_stock"), 0)

    def test_absent_field_is_zero_not_an_exception(self):
        self.assertEqual(_check_flag(_Doc(), "is_pos"), 0)

    def test_none_and_empty_string_are_zero(self):
        self.assertEqual(_check_flag(_Doc(is_pos=None), "is_pos"), 0)
        self.assertEqual(_check_flag(_Doc(is_pos=""), "is_pos"), 0)

    def test_string_digits_are_coerced(self):
        self.assertEqual(_check_flag(_Doc(is_pos="1"), "is_pos"), 1)
        self.assertEqual(_check_flag(_Doc(is_pos="0"), "is_pos"), 0)

    def test_a_class_level_descriptor_does_not_raise(self):
        """The original bug: int() on the descriptor object threw TypeError."""
        doc = _DescriptorDoc()
        self.assertEqual(_check_flag(doc, "is_pos"), 0)

    def test_an_unparseable_truthy_value_errs_toward_set(self):
        """Skipping the suppression double-books stock; a needless 0 is free."""
        self.assertEqual(_check_flag(_Doc(update_stock=object()), "update_stock"), 1)


class TestSuppressHook(unittest.TestCase):
    def test_pos_invoice_with_stock_update_is_forced_to_zero(self):
        doc = _Doc(is_pos=1, update_stock=1, name="ACC-SINV-0001")
        suppress_pos_invoice_stock_update(doc)
        self.assertEqual(doc.update_stock, 0)

    def test_non_pos_invoice_is_untouched(self):
        doc = _Doc(is_pos=0, update_stock=1, name="ACC-SINV-0002")
        suppress_pos_invoice_stock_update(doc)
        self.assertEqual(doc.update_stock, 1)

    def test_pos_invoice_already_at_zero_is_left_alone(self):
        doc = _Doc(is_pos=1, update_stock=0, name="ACC-SINV-0003")
        suppress_pos_invoice_stock_update(doc)
        self.assertEqual(doc.update_stock, 0)

    def test_a_descriptor_doc_no_longer_aborts_the_hook(self):
        """Before the fix this raised inside the try and logged instead of guarding."""
        doc = _DescriptorDoc()
        suppress_pos_invoice_stock_update(doc)  # must not raise
        # No instance is_pos -> not a POS invoice -> nothing set.
        self.assertNotIn("update_stock", doc.__dict__)

    def test_none_doc_is_a_no_op(self):
        suppress_pos_invoice_stock_update(None)


if __name__ == "__main__":
    unittest.main()

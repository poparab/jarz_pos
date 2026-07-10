"""Resolver tests for the category price fallback (pure mock).

Exercises ``services/invoice_creation.py::_resolve_item_rate`` precedence after the
contract v2 extension:

    (1) customer-scoped item_code Item Price
    (2) generic item_code Item Price
    (3) ``Jarz Price List Category Rate`` for (price_list, item's item_group)   <- NEW
    (4) get_item_price / fallback

Everything is mocked at ``ic.frappe.db.get_value`` + ``ic.get_item_price`` so the
precedence is asserted deterministically without any DB / doctype-migration dependency.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from jarz_pos.services import invoice_creation as ic


_PL = "Companies"
_CODE = "COFFEE-M-01"
_CUST = "CUST-A"
_GROUP = "Medium"


def _make_get_value(scoped=None, generic_item=None, group_rate=None):
    """Build a frappe.db.get_value side-effect emulating the pricing tables.

    Signature matches ``frappe.db.get_value(doctype, filters, fieldname)``. Category
    rates now come from the ``Jarz Price List Category Rate`` DocType (v2), not Item Price.
    """

    def _gv(doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "Item":
            # _resolve_item_rate looks up the item's item_group.
            return _GROUP
        if doctype == "Jarz Price List Category Rate":
            # Step 3: category rate for (price_list, item_group).
            return group_rate
        if doctype == "Item Price":
            f = filters or {}
            cust = f.get("customer")
            # Customer-scoped row: customer pinned to a concrete value (not the
            # generic ["in", [None, ""]] sentinel).
            if cust not in (None, "", ["in", [None, ""]]):
                return scoped
            # Generic per-item row.
            return generic_item
        return None

    return _gv


class TestItemGroupFallback(unittest.TestCase):
    def test_group_rate_used_when_no_per_item_row(self):
        # No per-item generic row -> fall through to the item_group category rate.
        gv = _make_get_value(scoped=None, generic_item=None, group_rate=75)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 75.0)

    def test_per_item_override_beats_category(self):
        # A generic per-item row (60) must win over the item_group category row (75).
        gv = _make_get_value(scoped=None, generic_item=60, group_rate=75)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 60.0)

    def test_customer_scoped_still_wins(self):
        # Customer-scoped row (50) outranks both the per-item (60) and category (75) rows.
        gv = _make_get_value(scoped=50, generic_item=60, group_rate=75)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL, customer=_CUST), 50.0)

    def test_fallback_when_no_group_row(self):
        # No per-item and no category row -> final get_item_price fallback path.
        gv = _make_get_value(scoped=None, generic_item=None, group_rate=None)
        with patch.object(ic.frappe.db, "get_value", side_effect=gv), patch.object(
            ic, "get_item_price", return_value=10
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 10.0)

    def test_category_lookup_not_consulted_when_per_item_exists(self):
        # Precedence guard: when a per-item generic rate exists, neither the
        # Item.item_group lookup nor the Category Rate query must be reached.
        def _gv(doctype, filters=None, fieldname=None, *a, **k):
            if doctype == "Item Price":
                return 60  # generic per-item row present
            if doctype == "Jarz Price List Category Rate":
                raise AssertionError("category rate query should not run")
            if doctype == "Item":
                raise AssertionError("Item.item_group lookup should not run")
            return None

        with patch.object(ic.frappe.db, "get_value", side_effect=_gv), patch.object(
            ic, "get_item_price", return_value=999
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, _PL), 60.0)

    def test_standard_no_price_list_is_byte_identical(self):
        # With no price_list (Standard's inert path) none of the matched lookups run;
        # the rate comes straight from get_item_price/fallback as before.
        def _gv(*a, **k):
            raise AssertionError("no Item Price lookup should occur without a price_list")

        with patch.object(ic.frappe.db, "get_value", side_effect=_gv), patch.object(
            ic, "get_item_price", return_value=None
        ):
            self.assertEqual(ic._resolve_item_rate(_CODE, None, fallback_rate=42), 42.0)


if __name__ == "__main__":
    unittest.main()

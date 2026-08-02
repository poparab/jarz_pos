"""Tests for rebuilding a POS cart from an already-submitted Sales Invoice.

Covers the regression that broke amendments for invoice ACC-SINV-2026-17035:
a Jarz Bundle listing the same item group twice ("Medium x8" + "Medium x2")
collapsed into a single group during client-side reconstruction, so the backend
rejected it with "expected 8 selection(s) from 'Medium', received 10".
"""

import unittest
from types import SimpleNamespace


def _row(**kwargs):
    """Build a Sales Invoice Item stand-in with the fields the rebuilder reads."""
    defaults = {
        "item_code": "",
        "qty": 1.0,
        "rate": 0.0,
        "price_list_rate": 0.0,
        "discount_percentage": 0.0,
        "is_bundle_parent": 0,
        "is_bundle_child": 0,
        "bundle_code": None,
        "parent_bundle": None,
        "bundle_group_key": None,
        "bundle_group_name": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _royal_feast_invoice():
    """The staging invoice shape: one bundle, two rows of the same item group."""
    return SimpleNamespace(
        name="ACC-SINV-2026-17035",
        items=[
            _row(
                item_code="Jarz Royal Feast",
                qty=1.0,
                rate=0.0,
                price_list_rate=960.0,
                discount_percentage=100.0,
                is_bundle_parent=1,
                bundle_code="gf9k3rfeg5",
            ),
            _row(
                item_code="Tiramisu Medium", qty=4.0, rate=96.0, price_list_rate=120.0,
                discount_percentage=20.0, is_bundle_child=1, parent_bundle="gf9k3rfeg5",
                bundle_group_key="gf9h4g3bi2", bundle_group_name="Medium",
            ),
            _row(
                item_code="Molten Medium", qty=2.0, rate=96.0, price_list_rate=120.0,
                discount_percentage=20.0, is_bundle_child=1, parent_bundle="gf9k3rfeg5",
                bundle_group_key="gf9h4g3bi2", bundle_group_name="Medium",
            ),
            _row(
                item_code="Strawberry Medium", qty=2.0, rate=96.0, price_list_rate=120.0,
                discount_percentage=20.0, is_bundle_child=1, parent_bundle="gf9k3rfeg5",
                bundle_group_key="gf9h4g3bi2", bundle_group_name="Medium",
            ),
            _row(
                item_code="Mango Medium", qty=2.0, rate=96.0, price_list_rate=120.0,
                discount_percentage=20.0, is_bundle_child=1, parent_bundle="gf9k3rfeg5",
                bundle_group_key="gf9m0embuv", bundle_group_name="Medium",
            ),
        ],
    )


class TestAmendmentCartRebuild(unittest.TestCase):
    """Test class for jarz_pos.services.amendment_cart."""

    def test_duplicate_item_groups_stay_on_their_own_rows(self):
        """Two bundle rows of the same item group must keep separate selections."""
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        cart = build_amendment_cart_from_invoice(_royal_feast_invoice())

        self.assertEqual(len(cart), 1, "The whole invoice is one bundle row")
        bundle = cart[0]
        self.assertEqual(bundle["item_code"], "gf9k3rfeg5")
        self.assertTrue(bundle["is_bundle"])
        self.assertEqual(bundle["qty"], 1)
        self.assertEqual(bundle["rate"], 960.0)

        selections = bundle["selected_items"]
        self.assertEqual(
            sorted(selections.keys()), ["gf9h4g3bi2", "gf9m0embuv"],
            "Selections must be keyed by the bundle group row, not the group name",
        )

        def total(group_key):
            return sum(entry["selected_quantity"] for entry in selections[group_key])

        # The bundle requires 8 from the first row and 2 from the second.
        self.assertEqual(total("gf9h4g3bi2"), 8)
        self.assertEqual(total("gf9m0embuv"), 2)

    def test_child_prices_come_from_the_invoice(self):
        """Child unit prices are carried over so the discount split reproduces the source."""
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        cart = build_amendment_cart_from_invoice(_royal_feast_invoice())
        entries = [
            entry
            for group in cart[0]["selected_items"].values()
            for entry in group
        ]

        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry["price"], 120.0)
            self.assertEqual(entry["id"], entry["item_code"])

    def test_child_quantities_are_divided_by_bundle_quantity(self):
        """Stored child qty is (per-bundle qty x bundle qty) and must be divided back."""
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        invoice = SimpleNamespace(
            name="INV-QTY",
            items=[
                _row(
                    item_code="BUNDLE-ITEM", qty=3.0, price_list_rate=300.0,
                    discount_percentage=100.0, is_bundle_parent=1, bundle_code="BDL-1",
                ),
                _row(
                    item_code="ITEM-A", qty=6.0, rate=40.0, price_list_rate=50.0,
                    is_bundle_child=1, parent_bundle="BDL-1",
                    bundle_group_key="ROW-1", bundle_group_name="Flavor",
                ),
            ],
        )

        cart = build_amendment_cart_from_invoice(invoice)

        self.assertEqual(cart[0]["qty"], 3)
        self.assertEqual(cart[0]["selected_items"]["ROW-1"][0]["selected_quantity"], 2)

    def test_plain_items_keep_their_line_discount(self):
        """Non-bundle rows round-trip with their price and manual discount."""
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        invoice = SimpleNamespace(
            name="INV-PLAIN",
            items=[
                _row(item_code="ITEM-A", qty=2.0, rate=90.0, price_list_rate=100.0,
                     discount_percentage=10.0),
                _row(item_code="ITEM-B", qty=1.0, rate=50.0, price_list_rate=50.0),
            ],
        )

        cart = build_amendment_cart_from_invoice(invoice)

        self.assertEqual(cart[0], {
            "item_code": "ITEM-A", "qty": 2.0, "rate": 100.0, "discount_percentage": 10.0,
        })
        self.assertEqual(cart[1], {"item_code": "ITEM-B", "qty": 1.0, "rate": 50.0})

    def test_indivisible_child_quantity_is_rejected(self):
        """A child qty that is not a multiple of the bundle qty is not recoverable."""
        import frappe

        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        invoice = SimpleNamespace(
            name="INV-BAD",
            items=[
                _row(
                    item_code="BUNDLE-ITEM", qty=2.0, price_list_rate=300.0,
                    discount_percentage=100.0, is_bundle_parent=1, bundle_code="BDL-1",
                ),
                _row(
                    item_code="ITEM-A", qty=3.0, rate=40.0, price_list_rate=50.0,
                    is_bundle_child=1, parent_bundle="BDL-1",
                    bundle_group_key="ROW-1", bundle_group_name="Flavor",
                ),
            ],
        )

        with self.assertRaises(frappe.ValidationError):
            build_amendment_cart_from_invoice(invoice)

    def test_bundle_without_children_is_rejected(self):
        """A bundle parent with no child rows cannot be rebuilt into selections."""
        import frappe

        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        invoice = SimpleNamespace(
            name="INV-ORPHAN",
            items=[
                _row(
                    item_code="BUNDLE-ITEM", qty=1.0, price_list_rate=300.0,
                    discount_percentage=100.0, is_bundle_parent=1, bundle_code="BDL-1",
                ),
            ],
        )

        with self.assertRaises(frappe.ValidationError):
            build_amendment_cart_from_invoice(invoice)

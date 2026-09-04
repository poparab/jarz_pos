"""Tests for rebuilding a POS cart from an already-submitted Sales Invoice.

Covers the regression that broke amendments for invoice ACC-SINV-2026-17035:
a Jarz Bundle listing the same item group twice ("Medium x8" + "Medium x2")
collapsed into a single group during client-side reconstruction, so the backend
rejected it with "expected 8 selection(s) from 'Medium', received 10".

:class:`TestStaleBundleCode` covers the second, far more common shape found on
ACC-SINV-2026-18026: a Woo-created invoice whose rows point at a Jarz Bundle
that no longer exists and which never stored ``bundle_group_key`` at all.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


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


def _indulgence_five_invoice():
    """ACC-SINV-2026-18026's shape: a Woo invoice with a dead bundle code.

    ``cdijpvbrkt`` no longer exists as a Jarz Bundle, and — as on 94% of the
    invoices carrying a bundle child — none of the children recorded which
    Jarz Bundle Item Group row they came from.
    """
    return SimpleNamespace(
        name="ACC-SINV-2026-18026",
        items=[
            _row(
                item_code="JARZ-INDULGENCE-FIVE", qty=1.0, rate=0.0,
                price_list_rate=400.0, discount_percentage=100.0,
                is_bundle_parent=1, bundle_code="cdijpvbrkt",
            ),
            _row(item_code="LARGE-A", qty=1.0, rate=75.05, price_list_rate=100.0,
                 is_bundle_child=1, parent_bundle="cdijpvbrkt"),
            _row(item_code="LARGE-B", qty=1.0, rate=75.05, price_list_rate=100.0,
                 is_bundle_child=1, parent_bundle="cdijpvbrkt"),
            _row(item_code="LARGE-C", qty=1.0, rate=75.05, price_list_rate=100.0,
                 is_bundle_child=1, parent_bundle="cdijpvbrkt"),
            _row(item_code="LARGE-D", qty=1.0, rate=75.05, price_list_rate=100.0,
                 is_bundle_child=1, parent_bundle="cdijpvbrkt"),
            _row(item_code="LARGE-E", qty=1.0, rate=99.80, price_list_rate=133.0,
                 is_bundle_child=1, parent_bundle="cdijpvbrkt"),
        ],
    )


class TestAmendmentCartRebuild(unittest.TestCase):
    """Test class for jarz_pos.services.amendment_cart."""

    def setUp(self):
        """Pin the bundle-existence lookup for the fixtures in this class.

        The rebuilder now checks the stored bundle code against the database
        before trusting it. These fixtures use ids that exist on no test site,
        so the answer is stated here rather than left to the site's data.
        """
        patcher = patch(
            "jarz_pos.services.amendment_cart._bundle_exists", return_value=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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


class TestAmendmentHelpers(unittest.TestCase):
    """Test class for the small amendment helpers in jarz_pos.api.manager."""

    def test_find_existing_amendment_invoice_returns_the_replacement(self):
        """The idempotency guard: a retried amendment must find its earlier result."""
        from jarz_pos.api import manager

        with patch.object(manager.frappe, "get_all", return_value=["ACC-SINV-2026-17036"]):
            found = manager._find_existing_amendment_invoice("ACC-SINV-2026-17035")

        self.assertEqual(found, "ACC-SINV-2026-17036")

    def test_find_existing_amendment_invoice_returns_none_when_unamended(self):
        from jarz_pos.api import manager

        with patch.object(manager.frappe, "get_all", return_value=[]):
            self.assertIsNone(manager._find_existing_amendment_invoice("ACC-SINV-2026-17035"))

    def test_territory_default_delivery_income_reads_the_territory(self):
        from jarz_pos.api import manager

        with patch.object(manager.frappe.db, "get_value", return_value=60.0) as get_value:
            self.assertEqual(manager._territory_default_delivery_income("EGRSHEROUK"), 60.0)

        get_value.assert_called_once_with("Territory", "EGRSHEROUK", "delivery_income")

    def test_territory_default_delivery_income_handles_a_blank_territory(self):
        from jarz_pos.api import manager

        self.assertIsNone(manager._territory_default_delivery_income(""))
        self.assertIsNone(manager._territory_default_delivery_income(None))


class TestStaleBundleCode(unittest.TestCase):
    """A Woo invoice pointing at a Jarz Bundle that no longer exists.

    This is the ACC-SINV-2026-18026 failure. The stored code ``cdijpvbrkt`` is
    dead, so every group lookup came back empty and the rebuild died with
    "has no bundle group recorded" before the amendment could even start.
    """

    def _rebuild(self, invoice, *, derived_code="irk4mnvoe2", group=("irkqulhim1", "Large")):
        """Rebuild with the bundle catalog faked at its two seams.

        ``group`` reproduces what group derivation really answers for a bundle
        that repeats an item group: the LAST matching row, for every item.
        """
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        with patch(
            "jarz_pos.services.amendment_cart._bundle_exists",
            side_effect=lambda code, cache: code != "cdijpvbrkt",
        ), patch(
            "jarz_pos.services.amendment_cart._derive_bundle_code_from_parent_item",
            return_value=derived_code,
        ) as derive_code, patch(
            "jarz_pos.services.amendment_cart._derive_bundle_group_metadata",
            return_value=group,
        ):
            cart = build_amendment_cart_from_invoice(invoice)
        return cart, derive_code

    def test_dead_bundle_code_is_re_derived_from_the_parent_item(self):
        """The rebuild must not throw, and must target the live bundle."""
        cart, derive_code = self._rebuild(_indulgence_five_invoice())

        self.assertEqual(len(cart), 1)
        self.assertEqual(cart[0]["item_code"], "irk4mnvoe2")
        self.assertTrue(cart[0]["is_bundle"])
        self.assertEqual(cart[0]["rate"], 400.0)
        derive_code.assert_called_once()

    def test_children_are_re_attached_to_the_re_derived_bundle(self):
        """Children still name the dead code; losing them would drop paid lines."""
        cart, _ = self._rebuild(_indulgence_five_invoice())

        entries = [
            entry for group in cart[0]["selected_items"].values() for entry in group
        ]
        self.assertEqual(
            sorted(entry["id"] for entry in entries),
            ["LARGE-A", "LARGE-B", "LARGE-C", "LARGE-D", "LARGE-E"],
        )
        self.assertEqual(sum(entry["selected_quantity"] for entry in entries), 5)

    def test_children_with_no_stored_group_key_are_keyed_by_group_name(self):
        """Derivation can only name ONE row, so the group name is used instead.

        Keying by the derived row would post all five selections at the row that
        needs one — the same rejection under a different message. The name lets
        BundleProcessor split them 4 + 1.
        """
        cart, _ = self._rebuild(_indulgence_five_invoice())

        self.assertEqual(list(cart[0]["selected_items"].keys()), ["Large"])
        self.assertEqual(len(cart[0]["selected_items"]["Large"]), 5)

    def test_stored_group_key_still_wins_when_the_invoice_has_one(self):
        """Invoices written by this app record the exact row: keep using it."""
        invoice = _indulgence_five_invoice()
        for child in invoice.items[1:5]:
            child.bundle_group_key = "irkm6iq1qc"
            child.bundle_group_name = "Large"
        invoice.items[5].bundle_group_key = "irkqulhim1"
        invoice.items[5].bundle_group_name = "Large"

        cart, _ = self._rebuild(invoice)

        selections = cart[0]["selected_items"]
        self.assertEqual(sorted(selections.keys()), ["irkm6iq1qc", "irkqulhim1"])
        self.assertEqual(len(selections["irkm6iq1qc"]), 4)
        self.assertEqual(len(selections["irkqulhim1"]), 1)

    def test_child_prices_are_taken_from_the_invoice(self):
        """The rebuilt cart must reprice the bundle exactly as it was sold."""
        cart, _ = self._rebuild(_indulgence_five_invoice())

        prices = {
            entry["id"]: entry["price"]
            for entry in cart[0]["selected_items"]["Large"]
        }
        self.assertEqual(prices["LARGE-A"], 100.0)
        self.assertEqual(prices["LARGE-E"], 133.0)

    def test_a_live_bundle_code_is_used_without_derivation(self):
        """No behaviour change for invoices whose bundle code is still valid."""
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        derive_code = MagicMock(return_value="")
        with patch(
            "jarz_pos.services.amendment_cart._bundle_exists", return_value=True
        ), patch(
            "jarz_pos.services.amendment_cart._derive_bundle_code_from_parent_item",
            derive_code,
        ):
            cart = build_amendment_cart_from_invoice(_royal_feast_invoice())

        self.assertEqual(cart[0]["item_code"], "gf9k3rfeg5")
        derive_code.assert_not_called()

    def test_a_dead_code_with_no_derivable_bundle_still_throws(self):
        """When nothing resolves, fail loudly instead of rebuilding a wrong cart."""
        import frappe

        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        with patch(
            "jarz_pos.services.amendment_cart._bundle_exists", return_value=False
        ), patch(
            "jarz_pos.services.amendment_cart._derive_bundle_code_from_parent_item",
            return_value="",
        ):
            with self.assertRaises(frappe.ValidationError):
                build_amendment_cart_from_invoice(_indulgence_five_invoice())


class TestRebuiltCartExpandsCleanly(unittest.TestCase):
    """The end-to-end proof: the rebuilt cart must survive bundle expansion.

    Rebuilding is only half the amendment. The cart is handed straight back to
    BundleProcessor, which recomputes every child rate and the uniform discount,
    so a cart that rebuilds but cannot expand is still a failed amendment.
    """

    def test_the_18026_cart_expands_to_five_correctly_keyed_children(self):
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice
        from jarz_pos.tests.test_bundle_processing import _children, _expand

        with patch(
            "jarz_pos.services.amendment_cart._bundle_exists", return_value=False
        ), patch(
            "jarz_pos.services.amendment_cart._derive_bundle_code_from_parent_item",
            return_value="irk4mnvoe2",
        ), patch(
            "jarz_pos.services.amendment_cart._derive_bundle_group_metadata",
            return_value=("irkqulhim1", "Large"),
        ):
            cart = build_amendment_cart_from_invoice(_indulgence_five_invoice())

        _processor, rows = _expand(cart[0]["selected_items"])
        children = _children(rows)

        self.assertEqual(
            [(row["item_code"], row["qty"], row["bundle_group_key"]) for row in children],
            [
                ("LARGE-A", 1.0, "irkm6iq1qc"),
                ("LARGE-B", 1.0, "irkm6iq1qc"),
                ("LARGE-C", 1.0, "irkm6iq1qc"),
                ("LARGE-D", 1.0, "irkm6iq1qc"),
                ("LARGE-E", 1.0, "irkqulhim1"),
            ],
            "The replacement invoice records the row keys the source never had",
        )

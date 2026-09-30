"""Tests for rebuilding a POS cart from an already-submitted Sales Invoice.

Covers the regression that broke amendments for invoice ACC-SINV-2026-17035:
a Jarz Bundle listing the same item group twice ("Medium x8" + "Medium x2")
collapsed into a single group during client-side reconstruction, so the backend
rejected it with "expected 8 selection(s) from 'Medium', received 10".

:class:`TestStaleBundleCode` covers the second, far more common shape found on
ACC-SINV-2026-18026: a Woo-created invoice whose rows point at a Jarz Bundle
that no longer exists and which never stored ``bundle_group_key`` at all.

:class:`TestRepeatedBundleInstances` covers Woo order 17748 / ACC-SINV-2026-18615:
the same bundle bought twice as two separate lines, every child naming the same
``parent_bundle``, which handed all eleven children (20 jars) to each parent.

:class:`TestAmendmentDeliverySlot` covers the midnight slot: a Time field comes
back as ``timedelta(0)`` for 00:00, which is falsy, so the amendment dropped it.
"""

import unittest
from datetime import date, timedelta
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


# ACC-SINV-2026-18615 (Woo order 17748): each Royal Feast instance carries its
# own ten Medium jars. The two instances deliberately differ so a child landing
# on the wrong parent changes the assertion.
_FIRST_FEAST = [
    ("Tiramisu Medium", 3.0),
    ("Molten Medium", 2.0),
    ("Strawberry Medium", 2.0),
    ("Mango Medium", 1.0),
    ("Lotus Medium", 1.0),
    ("Pistachio Medium", 1.0),
]
_SECOND_FEAST = [
    ("Tiramisu Medium", 4.0),
    ("Molten Medium", 2.0),
    ("Mango Medium", 2.0),
    ("Lotus Medium", 1.0),
    ("Oreo Medium", 1.0),
]


def _feast_parent(bundle_code):
    return _row(
        item_code="Jarz Royal Feast", qty=1.0, rate=0.0, price_list_rate=960.0,
        discount_percentage=100.0, is_bundle_parent=1, bundle_code=bundle_code,
    )


def _feast_children(bundle_code, lines):
    """Woo-shaped children: a parent_bundle, but no bundle_group_key/name."""
    return [
        _row(
            item_code=item_code, qty=qty, rate=96.0, price_list_rate=120.0,
            discount_percentage=20.0, is_bundle_child=1, parent_bundle=bundle_code,
        )
        for item_code, qty in lines
    ]


def _plain_line():
    return _row(item_code="Chocolate Hazelnut Large", qty=1.0, rate=150.0, price_list_rate=150.0)


def _royal_feast_twice_invoice(bundle_code="cdi1ojg75e"):
    """The production row order of ACC-SINV-2026-18615 (idx 1..14)."""
    return SimpleNamespace(
        name="ACC-SINV-2026-18615",
        items=[
            _plain_line(),
            _feast_parent(bundle_code),
            *_feast_children(bundle_code, _FIRST_FEAST),
            _feast_parent(bundle_code),
            *_feast_children(bundle_code, _SECOND_FEAST),
        ],
    )


def _selections(bundle_row):
    """``[(item_code, per-bundle qty)]`` across every group of one bundle cart row."""
    return [
        (entry["item_code"], entry["selected_quantity"])
        for group in bundle_row["selected_items"].values()
        for entry in group
    ]


def _as_selections(lines):
    return [(item_code, int(qty)) for item_code, qty in lines]


class TestRepeatedBundleInstances(unittest.TestCase):
    """The same bundle bought twice must rebuild as two bundles, not two copies of both.

    Children were grouped by bundle CODE, which both instances share, so each
    parent was handed all eleven children (20 jars against a bundle of 10).
    """

    def setUp(self):
        """Fake the catalog the way production answers for bundle gf9k3rfeg5.

        ``cdi1ojg75e`` is the stale code the invoice stored; the parent item maps
        to the live ``gf9k3rfeg5``. Group derivation answers with the LAST Medium
        row for every item, which is exactly why un-keyed children are keyed by
        the group name instead.
        """
        for target, kwargs in (
            (
                "jarz_pos.services.amendment_cart._bundle_exists",
                {"side_effect": lambda code, cache: code != "cdi1ojg75e"},
            ),
            (
                "jarz_pos.services.amendment_cart._derive_bundle_code_from_parent_item",
                {"return_value": "gf9k3rfeg5"},
            ),
            (
                "jarz_pos.services.amendment_cart._derive_bundle_group_metadata",
                {"return_value": ("gf9m0embuv", "Medium")},
            ),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _rebuild(self, invoice):
        from jarz_pos.services.amendment_cart import build_amendment_cart_from_invoice

        return build_amendment_cart_from_invoice(invoice)

    def test_each_instance_gets_only_its_own_children(self):
        """Two parents, two sets of ten: nothing doubled, nothing moved across."""
        cart = self._rebuild(_royal_feast_twice_invoice())

        self.assertEqual(len(cart), 3, "plain line + two bundle instances")
        self.assertEqual(cart[0]["item_code"], "Chocolate Hazelnut Large")
        first, second = cart[1], cart[2]
        for bundle in (first, second):
            self.assertTrue(bundle["is_bundle"])
            self.assertEqual(bundle["item_code"], "gf9k3rfeg5")
            self.assertEqual(bundle["qty"], 1)
            self.assertEqual(list(bundle["selected_items"].keys()), ["Medium"])
            self.assertEqual(sum(qty for _, qty in _selections(bundle)), 10)

        self.assertEqual(_selections(first), _as_selections(_FIRST_FEAST))
        self.assertEqual(_selections(second), _as_selections(_SECOND_FEAST))

    def test_stale_code_is_translated_for_both_instances(self):
        """Children still name the dead code; both instances must re-attach them."""
        cart = self._rebuild(_royal_feast_twice_invoice("cdi1ojg75e"))

        bundles = [row for row in cart if row.get("is_bundle")]
        self.assertEqual([row["item_code"] for row in bundles], ["gf9k3rfeg5", "gf9k3rfeg5"])
        all_entries = [entry for row in bundles for entry in _selections(row)]
        self.assertEqual(
            len(all_entries), len(_FIRST_FEAST) + len(_SECOND_FEAST),
            "Every child row appears exactly once across the two instances",
        )

    def test_live_code_splits_the_same_way(self):
        """No stale-code translation involved: the split must not depend on it."""
        cart = self._rebuild(_royal_feast_twice_invoice("gf9k3rfeg5"))

        self.assertEqual(_selections(cart[1]), _as_selections(_FIRST_FEAST))
        self.assertEqual(_selections(cart[2]), _as_selections(_SECOND_FEAST))

    def test_a_plain_line_between_the_instances_does_not_disturb_attribution(self):
        invoice = _royal_feast_twice_invoice()
        # Move the plain line between the two instances.
        plain = invoice.items.pop(0)
        invoice.items.insert(1 + len(_FIRST_FEAST), plain)

        cart = self._rebuild(invoice)

        self.assertEqual(
            [row["item_code"] for row in cart],
            ["gf9k3rfeg5", "Chocolate Hazelnut Large", "gf9k3rfeg5"],
        )
        self.assertEqual(_selections(cart[0]), _as_selections(_FIRST_FEAST))
        self.assertEqual(_selections(cart[2]), _as_selections(_SECOND_FEAST))

    def test_single_instance_output_is_unchanged(self):
        """One parent per code: the exact cart the code-keyed rebuild produced."""
        invoice = SimpleNamespace(
            name="ACC-SINV-SINGLE",
            items=[
                _plain_line(),
                _feast_parent("cdi1ojg75e"),
                *_feast_children("cdi1ojg75e", _FIRST_FEAST),
            ],
        )

        cart = self._rebuild(invoice)

        self.assertEqual(
            cart,
            [
                {"item_code": "Chocolate Hazelnut Large", "qty": 1.0, "rate": 150.0},
                {
                    "item_code": "gf9k3rfeg5",
                    "qty": 1,
                    "rate": 960.0,
                    "price_list_rate": 960.0,
                    "is_bundle": True,
                    "selected_items": {
                        "Medium": [
                            {
                                "id": item_code,
                                "item_code": item_code,
                                "name": item_code,
                                "selected_quantity": int(qty),
                                "price": 120.0,
                            }
                            for item_code, qty in _FIRST_FEAST
                        ]
                    },
                },
            ],
        )

    def test_single_instance_child_above_its_parent_is_still_attached(self):
        """With one parent there is nothing to guess: keep the old attachment."""
        invoice = SimpleNamespace(
            name="ACC-SINV-ODD-ORDER",
            items=[
                *_feast_children("cdi1ojg75e", [("Tiramisu Medium", 10.0)]),
                _feast_parent("cdi1ojg75e"),
            ],
        )

        cart = self._rebuild(invoice)

        self.assertEqual(len(cart), 1)
        self.assertEqual(_selections(cart[0]), [("Tiramisu Medium", 10)])

    def test_child_above_every_instance_is_refused(self):
        """Which of two instances owns it would be a guess: fail loudly instead."""
        import frappe

        invoice = _royal_feast_twice_invoice()
        invoice.items.insert(0, _feast_children("cdi1ojg75e", [("Oreo Medium", 1.0)])[0])

        with self.assertRaises(frappe.ValidationError):
            self._rebuild(invoice)

    def test_orphan_child_on_two_instances_is_still_refused(self):
        """The single-bundle orphan recovery must not start guessing between instances."""
        import frappe

        invoice = _royal_feast_twice_invoice()
        invoice.items.append(
            _row(item_code="Mystery Medium", qty=1.0, price_list_rate=120.0, is_bundle_child=1)
        )

        with self.assertRaises(frappe.ValidationError):
            self._rebuild(invoice)


class TestAmendmentDeliverySlot(unittest.TestCase):
    """The amendment's delivery window is derived from the source invoice's slot.

    ``custom_delivery_time_from`` is a Time field and Frappe returns it as a
    ``datetime.timedelta``. The 00:00 - 01:00 slot is ``timedelta(0)``, which is
    falsy, so ``... or ""`` treated it as no slot and the replacement invoice
    lost its delivery date and time.
    """

    @staticmethod
    def _source(time_from, delivery_date="2026-09-30", duration=None):
        import frappe

        return frappe._dict(
            custom_delivery_date=delivery_date,
            custom_delivery_time_from=time_from,
            custom_delivery_duration=duration,
        )

    def test_midnight_timedelta_is_a_real_slot(self):
        from jarz_pos.api import manager

        self.assertEqual(
            manager._derive_required_delivery_datetime(self._source(timedelta(0))),
            "2026-09-30 00:00:00",
        )

    def test_midnight_string_is_zero_padded(self):
        """``str(timedelta(0))`` is ``"0:00:00"``; the output must be ``00:00:00``."""
        from jarz_pos.api import manager

        self.assertEqual(
            manager._derive_required_delivery_datetime(self._source("0:00:00")),
            "2026-09-30 00:00:00",
        )

    def test_afternoon_timedelta_still_works(self):
        from jarz_pos.api import manager

        self.assertEqual(
            manager._derive_required_delivery_datetime(
                self._source(timedelta(hours=14, minutes=30))
            ),
            "2026-09-30 14:30:00",
        )

    def test_other_time_shapes_are_normalised(self):
        from jarz_pos.api import manager

        cases = {
            timedelta(hours=9): "2026-09-30 09:00:00",
            timedelta(hours=23, minutes=30): "2026-09-30 23:30:00",
            "9:00": "2026-09-30 09:00:00",
            "14:30": "2026-09-30 14:30:00",
            "21:00:00": "2026-09-30 21:00:00",
        }
        for time_from, expected in cases.items():
            self.assertEqual(
                manager._derive_required_delivery_datetime(self._source(time_from)),
                expected,
                time_from,
            )

    def test_a_date_object_is_accepted(self):
        from jarz_pos.api import manager

        self.assertEqual(
            manager._derive_required_delivery_datetime(
                self._source(timedelta(0), delivery_date=date(2026, 9, 30))
            ),
            "2026-09-30 00:00:00",
        )

    def test_a_missing_slot_stays_none(self):
        from jarz_pos.api import manager

        self.assertIsNone(manager._derive_required_delivery_datetime(self._source(None)))
        self.assertIsNone(manager._derive_required_delivery_datetime(self._source("")))
        self.assertIsNone(
            manager._derive_required_delivery_datetime(self._source(timedelta(0), delivery_date=None))
        )
        self.assertIsNone(
            manager._derive_required_delivery_datetime(self._source(timedelta(0), delivery_date=""))
        )

    def test_midnight_end_is_derived_from_the_duration(self):
        from jarz_pos.api import manager

        self.assertEqual(
            manager._derive_delivery_end_datetime(self._source(timedelta(0), duration=3600)),
            "2026-09-30 01:00:00",
        )

    def test_end_keeps_its_time_of_day(self):
        """``add_to_date(datetime, seconds=..., as_string=True)`` returned a bare date."""
        from jarz_pos.api import manager

        self.assertEqual(
            manager._derive_delivery_end_datetime(
                self._source(timedelta(hours=14, minutes=30), duration=5400)
            ),
            "2026-09-30 16:00:00",
        )
        self.assertEqual(
            manager._derive_delivery_end_datetime(
                self._source(timedelta(hours=23), duration=7200)
            ),
            "2026-10-01 01:00:00",
        )

    def test_zero_or_missing_duration_means_no_end(self):
        from jarz_pos.api import manager

        self.assertIsNone(manager._derive_delivery_end_datetime(self._source(timedelta(0), duration=0)))
        self.assertIsNone(manager._derive_delivery_end_datetime(self._source(timedelta(0), duration=None)))
        self.assertIsNone(manager._derive_delivery_end_datetime(self._source(None, duration=3600)))

    def test_the_source_end_only_pairs_with_the_source_start(self):
        """A new start with no end must not inherit the old slot's end."""
        from jarz_pos.api import manager

        source = self._source(timedelta(hours=12), duration=3600)
        # Nothing sent: both come from the source.
        self.assertEqual(
            manager._amendment_delivery_end(source, None, None), "2026-09-30 13:00:00"
        )
        # A new start alone: no end, so slot normalisation derives one.
        self.assertIsNone(manager._amendment_delivery_end(source, "2026-09-30 12:30:00", None))
        # An explicit end always wins.
        self.assertEqual(
            manager._amendment_delivery_end(source, "2026-09-30 12:30:00", "2026-09-30 14:00:00"),
            "2026-09-30 14:00:00",
        )

    def test_an_amendment_keeps_a_midnight_window_as_its_own(self):
        """The explicit-slot decision reads the same derivation, so 00:00 must count."""
        from jarz_pos.api import manager

        self.assertTrue(
            manager._is_source_delivery_start("2026-09-30 00:00:00", self._source(timedelta(0)))
        )

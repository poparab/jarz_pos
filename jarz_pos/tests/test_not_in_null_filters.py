"""The ``["not in", [None, ""]]`` filter, and the courier-party lookup it killed.

Frappe compiles ``{"party": ["not in", [None, ""]]}`` to
``IFNULL(party,'') NOT IN (NULL,'')``. SQL's three-valued logic makes that
UNKNOWN for every row, so the query returns nothing on any real database while
a mocked ``frappe.get_all`` records the dict and moves on. Three separate
production defects had this shape (the settlement-reversal list, the kanban
collection-change badge, and eight copies of the "derive the courier the caller
omitted" lookup — 0 of production's 927 Courier Transactions matched it on
2026-09-14).

Two guards:

* a source scan, so the shape cannot come back anywhere in the app;
* the shared lookup ``_existing_courier_party`` run against the real table,
  because only SQL can tell a working filter from a dead one.
"""

import ast
import pathlib
import unittest

import frappe

from jarz_pos.services import delivery_handling as dh

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_INVOICE = "ACC-SINV-NOTINNULL-TEST"


def _dead_not_in_filters(tree: ast.AST):
    """Yield line numbers of ``["not in", [..., None, ...]]`` literals."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) != 2:
            continue
        operator, values = node.elts
        if not (isinstance(operator, ast.Constant) and operator.value == "not in"):
            continue
        if not isinstance(values, (ast.List, ast.Tuple)):
            continue
        if any(isinstance(v, ast.Constant) and v.value is None for v in values.elts):
            yield node.lineno


class TestNoNotInNullFilterInSource(unittest.TestCase):
    def test_no_filter_compares_not_in_against_none(self):
        offenders = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            if "tests" in path.relative_to(APP_ROOT).parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders.extend(
                f"{path.relative_to(APP_ROOT)}:{line}" for line in _dead_not_in_filters(tree)
            )
        self.assertEqual(
            offenders, [],
            "A ['not in', [None, ...]] filter matches NO rows in SQL "
            "(IFNULL(x,'') NOT IN (NULL,'') is never true). Use ['is', 'set'].",
        )

    def test_the_scanner_recognises_the_dead_shape(self):
        """The scan above is only worth something if it can see the pattern."""
        tree = ast.parse('f = {"party": ["not in", [None, ""]], "x": ["not in", ["a"]]}')
        self.assertEqual(len(list(_dead_not_in_filters(tree))), 1)


class TestExistingCourierPartyAgainstRealSql(unittest.TestCase):
    """``_existing_courier_party`` against real Courier Transaction rows."""

    @classmethod
    def setUpClass(cls):
        if getattr(frappe, "db", None) is None:
            raise AssertionError(
                "These tests require a real database connection. They must never be "
                "skipped: a mocked get_all cannot evaluate the filter they exist to check."
            )

    def setUp(self):
        self.addCleanup(self._cleanup)
        self._cleanup()

    def _cleanup(self):
        frappe.db.delete("Courier Transaction", {"reference_invoice": TEST_INVOICE})

    def _insert_ct(self, name, party_type, party, status, creation):
        doc = frappe.get_doc(
            {
                "doctype": "Courier Transaction",
                "name": name,
                "reference_invoice": TEST_INVOICE,
                "party_type": party_type,
                "party": party,
                "status": status,
                "type": "Pick-Up",
                "amount": 0,
                "shipping_amount": 0,
            }
        )
        doc.creation = creation
        doc.modified = creation
        # Raw insert: the point is the SELECT, not the document's link validation.
        doc.db_insert()

    def test_the_old_filter_matches_nothing_and_the_lookup_finds_the_courier(self):
        self._insert_ct("CT-NOTINNULL-1", "Employee", "EMP-NOTINNULL-A", "Unsettled",
                        "2026-09-01 10:00:00")

        old = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": TEST_INVOICE,
                "party_type": ["not in", [None, ""]],
                "party": ["not in", [None, ""]],
            },
            fields=["party_type", "party"],
        )
        self.assertEqual(
            old, [],
            "['not in', [None, '']] is expected to match NOTHING in SQL; if this "
            "ever returns rows, Frappe changed how it compiles the filter.",
        )
        self.assertEqual(
            dh._existing_courier_party(TEST_INVOICE, open_only=True),
            ("Employee", "EMP-NOTINNULL-A"),
        )
        self.assertEqual(
            dh._existing_courier_party(TEST_INVOICE),
            ("Employee", "EMP-NOTINNULL-A"),
        )

    def test_open_only_prefers_the_courier_still_holding_the_order(self):
        """An order re-handed to a second courier: the newer row is already settled."""
        self._insert_ct("CT-NOTINNULL-OPEN", "Employee", "EMP-NOTINNULL-A", "Unsettled",
                        "2026-09-01 10:00:00")
        self._insert_ct("CT-NOTINNULL-DONE", "Supplier", "SUP-NOTINNULL-B", "Settled",
                        "2026-09-02 10:00:00")

        self.assertEqual(
            dh._existing_courier_party(TEST_INVOICE, open_only=True),
            ("Employee", "EMP-NOTINNULL-A"),
        )
        # Without the restriction the newest row wins, deterministically.
        self.assertEqual(
            dh._existing_courier_party(TEST_INVOICE),
            ("Supplier", "SUP-NOTINNULL-B"),
        )

    def test_settled_rows_only_are_invisible_to_open_only(self):
        self._insert_ct("CT-NOTINNULL-DONE", "Employee", "EMP-NOTINNULL-A", "Settled",
                        "2026-09-01 10:00:00")
        self.assertEqual(dh._existing_courier_party(TEST_INVOICE, open_only=True), (None, None))
        self.assertEqual(
            dh._existing_courier_party(TEST_INVOICE), ("Employee", "EMP-NOTINNULL-A")
        )

    def test_settle_single_refuses_to_derive_a_courier_from_a_settled_row(self):
        """Nothing open means nothing left to settle: deriving from history re-pays freight."""
        self._insert_ct("CT-NOTINNULL-DONE", "Employee", "EMP-NOTINNULL-A", "Settled",
                        "2026-09-01 10:00:00")
        with self.assertRaises(frappe.ValidationError) as ctx:
            dh.settle_single_invoice_paid(TEST_INVOICE, "Any POS Profile", "", "")
        self.assertIn("unable to derive", str(ctx.exception))

    def test_a_row_without_a_party_is_not_a_courier(self):
        self._insert_ct("CT-NOTINNULL-BLANK", "", "", "Unsettled", "2026-09-01 10:00:00")
        self.assertEqual(dh._existing_courier_party(TEST_INVOICE), (None, None))

    def test_an_invoice_with_no_rows_yields_nothing(self):
        self.assertEqual(dh._existing_courier_party(TEST_INVOICE), (None, None))


if __name__ == "__main__":
    unittest.main()

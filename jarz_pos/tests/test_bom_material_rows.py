"""The bill-of-materials level every capacity, shortage and cost figure rests on.

THE defect this module guards against, measured on production for Tiramisu
Large x100:

    board checked:  eggs 48, flour 0.768, cornstarch 0.32, milkana 4.784908
    work order took: Cheesecake Mix 9.1, Savoiardi 4.0, milkana 2.3947

Neither list contained the other's items.  The reader defaulted to the EXPLODED
bill (``tabBOM Explosion Item``) while the Work Order — with
``use_multi_level_bom = 0`` — required the ONE-LEVEL bill (``tabBOM Item``).  So
a batch could pass the pre-check and then fail on a sub-assembly bin holding
-14.64, after ``submit_work_orders`` had already committed earlier lines and
consumed real stock; and a full freezer with low flour blocked a day that was
entirely makeable.

Two things have to stay in step for the numbers to mean anything, and each has
a test below:

  1. ``_get_required_material_rows`` defaults to ``fetch_exploded=0``.
  2. ``_ensure_work_order`` states ``use_multi_level_bom = 0`` outright rather
     than inheriting the site's Property Setter.

Everything else here pins the individual call sites to that same one-level
answer, and pins the UOM chain (``stock_uom`` -> ``uom`` -> ``DEFAULT_UOM``)
that stopped a Kg item reporting "short by 2.083 Nos".
"""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch


# ── Fixtures shaped exactly like ERPNext's ``get_bom_items_as_dict`` ─────
#
# Both branches select ``item.stock_uom`` and both default to
# ``fetch_qty_in_stock_uom=True`` (so ``qty`` is already a stock-UOM number).
# Only the one-level branch also selects ``bom_item.uom`` — the exploded branch
# omits it entirely, which is why these two dicts differ in shape as well as in
# content.

ONE_LEVEL_ROWS = {
    "Cheesecake Mix": {
        "item_code": "Cheesecake Mix",
        "item_name": "Cheesecake Mix",
        "qty": 9.1,
        "uom": "Nos",  # the unit somebody typed the BOM line in
        "stock_uom": "Kg",  # the unit `qty` is actually in
        "source_warehouse": "Freezer - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 1,
    },
    "Savoiardi": {
        "item_code": "Savoiardi",
        "item_name": "Savoiardi",
        "qty": 4.0,
        "uom": "Kg",
        "stock_uom": "Kg",
        "source_warehouse": "Freezer - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 2,
    },
    "milkana cheese": {
        "item_code": "milkana cheese",
        "item_name": "Milkana cheese",
        "qty": 2.3947,
        "uom": "Kg",
        "stock_uom": "Kg",
        "source_warehouse": "Raw Material - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 3,
    },
}

EXPLODED_ROWS = {
    "eggs": {
        "item_code": "eggs",
        "item_name": "Eggs",
        "qty": 48.0,
        # No "uom" key at all: the exploded query never selects bom_item.uom.
        "stock_uom": "Nos",
        "source_warehouse": "Raw Material - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 1,
    },
    "flour": {
        "item_code": "flour",
        "item_name": "Flour",
        "qty": 0.768,
        "stock_uom": "Kg",
        "source_warehouse": "Raw Material - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 2,
    },
    "cornstarch": {
        "item_code": "cornstarch",
        "item_name": "Cornstarch",
        "qty": 0.32,
        "stock_uom": "Kg",
        "source_warehouse": "Raw Material - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 3,
    },
    "milkana cheese": {
        "item_code": "milkana cheese",
        "item_name": "Milkana cheese",
        "qty": 4.784908,
        "stock_uom": "Kg",
        "source_warehouse": "Raw Material - J",
        "default_warehouse": None,
        "include_item_in_manufacturing": 1,
        "idx": 4,
    },
}


def _branching_getter():
    """A ``get_bom_items_as_dict`` stand-in that really honours the flag."""

    def getter(bom, company, qty=1, fetch_exploded=1, **kwargs):
        return EXPLODED_ROWS if fetch_exploded else ONE_LEVEL_ROWS

    return MagicMock(side_effect=getter)


class TestRequiredMaterialRowsDefaultsToOneLevel(unittest.TestCase):
    def _read(self, getter, **kwargs):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_get_bom_items_as_dict", return_value=getter
        ), patch(
            "jarz_pos.api.manufacturing._resolve_get_latest_stock_qty",
            return_value=MagicMock(return_value=0.0),
        ), patch("jarz_pos.api.manufacturing.frappe"):
            return manufacturing._get_required_material_rows(
                "BOM-TIRA-L", "Jarz Co", 100.0, **kwargs
            )

    def test_the_default_is_the_one_level_bill(self):
        # Correctness is the default; the explosion is what you opt into.
        getter = _branching_getter()
        self._read(getter)
        self.assertEqual(0, getter.call_args.kwargs["fetch_exploded"])

    def test_the_exploded_bill_is_opt_in_and_still_reachable(self):
        getter = _branching_getter()
        self._read(getter, fetch_exploded=1)
        self.assertEqual(1, getter.call_args.kwargs["fetch_exploded"])

    def test_a_subassembly_line_yields_the_subassembly_not_its_raw_children(self):
        # The whole fix in one assertion: Tiramisu Large consumes Cheesecake Mix
        # and Savoiardi off the freezer shelf.  It does not consume eggs.
        rows = self._read(_branching_getter())
        codes = [r["item_code"] for r in rows]

        self.assertEqual(["Cheesecake Mix", "Savoiardi", "milkana cheese"], codes)
        for raw in ("eggs", "flour", "cornstarch"):
            self.assertNotIn(raw, codes)

    def test_the_two_bills_really_are_different_questions(self):
        # Guards the fixture as much as the code: if these two ever agree, every
        # other assertion in this module has quietly stopped proving anything.
        one_level = {r["item_code"] for r in self._read(_branching_getter())}
        exploded = {r["item_code"] for r in self._read(_branching_getter(), fetch_exploded=1)}

        self.assertEqual({"milkana cheese"}, one_level & exploded)
        # ...and even the shared item is a different quantity on each bill.
        one_level_milkana = next(
            r for r in self._read(_branching_getter()) if r["item_code"] == "milkana cheese"
        )
        self.assertAlmostEqual(2.3947, one_level_milkana["required_qty"])

    def test_rows_excluded_from_manufacturing_are_dropped(self):
        rows = self._read(
            MagicMock(
                return_value={
                    "PACKAGING": dict(
                        ONE_LEVEL_ROWS["Savoiardi"],
                        item_code="PACKAGING",
                        include_item_in_manufacturing=0,
                    ),
                    "Savoiardi": ONE_LEVEL_ROWS["Savoiardi"],
                }
            )
        )
        self.assertEqual(["Savoiardi"], [r["item_code"] for r in rows])


class TestComponentUomChain(unittest.TestCase):
    """``stock_uom`` -> ``uom`` -> ``DEFAULT_UOM``.

    ERPNext converts the quantity into the stock UOM in BOTH branches, so
    ``bom_item.uom`` is only the unit somebody typed the line in.  Labelling a
    converted number with the typed unit is how a Kg item told an operator it
    was "short by 2.083 Nos".
    """

    def _uom(self, row):
        from jarz_pos.services.production_planning import component_uom

        return component_uom(row)

    def test_stock_uom_wins_over_the_typed_uom(self):
        self.assertEqual("Kg", self._uom({"uom": "Nos", "stock_uom": "Kg"}))

    def test_typed_uom_is_the_fallback_when_stock_uom_is_absent(self):
        # The exploded branch never selects bom_item.uom, but a row that reaches
        # here from anywhere else may still carry only that.
        self.assertEqual("Kg", self._uom({"uom": "Kg"}))

    def test_blank_and_whitespace_count_as_not_stated(self):
        self.assertEqual("Kg", self._uom({"stock_uom": "   ", "uom": "Kg"}))
        self.assertEqual("Kg", self._uom({"stock_uom": None, "uom": "Kg"}))

    def test_default_uom_is_the_last_resort_only(self):
        from jarz_pos.constants import DEFAULT_UOM

        self.assertEqual(DEFAULT_UOM, self._uom({}))
        self.assertEqual(DEFAULT_UOM, self._uom({"uom": "", "stock_uom": ""}))

    def test_the_reader_labels_rows_in_the_unit_the_quantity_is_in(self):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_get_bom_items_as_dict",
            return_value=_branching_getter(),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_get_latest_stock_qty",
            return_value=MagicMock(return_value=0.0),
        ), patch("jarz_pos.api.manufacturing.frappe"):
            rows = manufacturing._get_required_material_rows("BOM-TIRA-L", "Jarz Co", 100.0)

        by_code = {r["item_code"]: r for r in rows}
        # THE regression: this row's typed uom is "Nos" and its 9.1 is Kg.
        self.assertEqual("Kg", by_code["Cheesecake Mix"]["uom"])
        # stock_uom stays on the row raw and undefaulted, so a caller can still
        # tell "no unit was stated" from "the unit really is Nos".
        self.assertEqual("Kg", by_code["Cheesecake Mix"]["stock_uom"])

    def test_exploded_rows_are_no_longer_labelled_nos_by_omission(self):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_get_bom_items_as_dict",
            return_value=_branching_getter(),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_get_latest_stock_qty",
            return_value=MagicMock(return_value=0.0),
        ), patch("jarz_pos.api.manufacturing.frappe"):
            rows = manufacturing._get_required_material_rows(
                "BOM-TIRA-L", "Jarz Co", 100.0, fetch_exploded=1
            )

        by_code = {r["item_code"]: r for r in rows}
        self.assertEqual("Kg", by_code["flour"]["uom"])
        self.assertEqual("Nos", by_code["eggs"]["uom"])  # genuinely Nos, not a default

    def test_subassembly_keeps_its_behaviour_through_the_shared_helper(self):
        from jarz_pos.api import subassembly
        from jarz_pos.constants import DEFAULT_UOM

        cases = [
            ({"uom": "Nos", "stock_uom": "Kg"}, "Kg"),
            ({"uom": "Kg"}, "Kg"),
            ({}, DEFAULT_UOM),
        ]
        for row, expected in cases:
            self.assertEqual(expected, subassembly._component_uom(row), row)

    def test_the_basket_rollup_labels_with_the_same_chain(self):
        from jarz_pos.services.production_planning import aggregate_basket_materials

        result = aggregate_basket_materials(
            [
                {
                    "line_index": 0,
                    "item_code": "TIRA-L",
                    "components": [
                        {
                            "item_code": "Cheesecake Mix",
                            "item_name": "Cheesecake Mix",
                            "uom": "Nos",
                            "stock_uom": "Kg",
                            "required_qty": 9.1,
                            "available_qty": 0.0,
                            "source_warehouse": "Freezer - J",
                        }
                    ],
                }
            ]
        )
        self.assertEqual("Kg", result["components"][0]["uom"])


class TestWorkOrderIsPinnedToTheOneLevelBill(unittest.TestCase):
    def test_ensure_work_order_states_use_multi_level_bom_zero(self):
        """The other half of the pairing.

        ERPNext's ``set_required_items`` calls
        ``get_bom_items_as_dict(..., fetch_exploded=self.use_multi_level_bom)``,
        so this flag decides what the Work Order — and the Stock Entry made from
        it — will actually consume.  Inheriting the site's Property Setter meant
        one edit in Desk could invert every material number in the app with no
        code change and no signal.
        """
        from jarz_pos.api import manufacturing

        line = {"item_code": "TIRA-L", "bom_name": "BOM-TIRA-L", "item_qty": 100}

        with patch(
            "jarz_pos.api.manufacturing._resolve_work_order_warehouses",
            return_value={"wip_warehouse": "WIP - J", "fg_warehouse": "Finished Goods - J"},
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            manufacturing._ensure_work_order(
                line, "Jarz Co", {}, datetime(2026, 8, 28, 9, 0, 0)
            )

        payload = mock_frappe.get_doc.call_args.args[0]
        self.assertEqual("Work Order", payload["doctype"])
        self.assertIn("use_multi_level_bom", payload)
        # Explicitly 0, not merely falsy/absent: frappe's `update_if_missing`
        # only fills a field that is None, so stating 0 is what stops the
        # DocType/Property Setter default from applying at insert().
        self.assertEqual(0, payload["use_multi_level_bom"])


class TestEveryCallSiteReadsTheOneLevelBill(unittest.TestCase):
    """One test per caller, because a default is only as good as its callers.

    Each of these answers "what will we physically pick and consume", so each
    wants the one-level bill.  If a future caller genuinely needs the explosion
    it must pass ``fetch_exploded=1`` explicitly — and then it will show up here
    as a failure, which is the point.
    """

    def test_price_batch_components(self):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._get_required_material_rows", return_value=[]
        ) as mock_rows, patch("jarz_pos.api.manufacturing.frappe"):
            manufacturing._price_batch_components(
                {"bom_name": "BOM-TIRA-L", "item_qty": 100}, "Jarz Co"
            )

        self.assertEqual(0, mock_rows.call_args.kwargs["fetch_exploded"])

    def test_get_material_precheck_issues(self):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._get_required_material_rows", return_value=[]
        ) as mock_rows, patch("jarz_pos.api.manufacturing.frappe"):
            manufacturing._get_material_precheck_issues(
                {"bom_name": "BOM-TIRA-L", "item_qty": 100}, "Jarz Co"
            )

        self.assertEqual(0, mock_rows.call_args.kwargs["fetch_exploded"])

    def test_get_bom_details(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._ensure_manager_access"), patch(
            "jarz_pos.api.manufacturing._get_required_material_rows", return_value=[]
        ) as mock_rows, patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.side_effect = [
                {"name": "BOM-TIRA-L", "quantity": 100, "company": "Jarz Co"},
                {"item_name": "Tiramisu Large", "stock_uom": "Nos"},
            ]
            manufacturing.get_bom_details("TIRA-L")

        self.assertEqual(0, mock_rows.call_args.kwargs["fetch_exploded"])

    def test_build_capacity_map(self):
        from jarz_pos.services import production_planning as planning

        getter = MagicMock(return_value=[])
        with patch(
            "jarz_pos.services.production_planning._resolve_required_material_rows",
            return_value=getter,
        ), patch(
            "jarz_pos.services.production_planning._resolve_bin_stock_map", return_value={}
        ), patch("jarz_pos.services.production_planning.frappe"):
            planning.build_capacity_map(
                [
                    {
                        "item_code": "TIRA-L",
                        "default_bom": "BOM-TIRA-L",
                        "company": "Jarz Co",
                        "bom_qty": 100,
                    }
                ],
                "Jarz Co",
            )

        self.assertEqual(0, getter.call_args.kwargs["fetch_exploded"])

    def test_build_basket_rollup(self):
        from jarz_pos.services import production_planning as planning

        getter = MagicMock(return_value=[])
        with patch(
            "jarz_pos.services.production_planning._resolve_required_material_rows",
            return_value=getter,
        ), patch(
            "jarz_pos.services.production_planning._resolve_bom_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.services.production_planning._resolve_bin_stock_map", return_value={}
        ), patch("jarz_pos.services.production_planning.frappe"):
            planning.build_basket_rollup(
                [{"item_code": "TIRA-L", "bom_name": "BOM-TIRA-L", "item_qty": 100}], "Jarz Co"
            )

        self.assertEqual(0, getter.call_args.kwargs["fetch_exploded"])

    def test_sop_build_component_maps(self):
        from jarz_pos.api import sop

        getter = MagicMock(return_value=[])
        with patch(
            "jarz_pos.api.sop._resolve_required_material_rows", return_value=getter
        ), patch("jarz_pos.api.sop.frappe"):
            sop._build_component_maps("BOM-TIRA-L", "Jarz Co", 100.0)

        self.assertEqual(0, getter.call_args.kwargs["fetch_exploded"])

    def test_subassembly_preview(self):
        from jarz_pos.api import subassembly

        with patch(
            "jarz_pos.api.manufacturing._get_required_material_rows", return_value=[]
        ) as mock_rows:
            subassembly._resolve_required_material_rows("BOM-CHEESECAKE-MIX", "Jarz Co", 1.0)

        self.assertEqual(0, mock_rows.call_args.kwargs["fetch_exploded"])

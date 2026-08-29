"""The "it's in another store" middle ground for material shortages.

A shortage is measured in the recipe line's **source warehouse only**.  When the
cheese is sitting in another branch the operator is told they cannot start while
the company actually has plenty — and because the message never said where, the
floor went looking for a purchase order instead of a stock transfer.

These tests pin the one behaviour that matters twice over:

* the message becomes actionable — it names where the stock is;
* the shortage still **blocks**.  There is deliberately no override here, and a
  failed lookup degrades to "no alternatives found", never to a missing block.

The pure half (``shape_stock_elsewhere``) needs no patching at all; everything
that touches the database sits behind ``_resolve_stock_elsewhere_rows``, so one
patch covers every caller.
"""

import unittest
from unittest.mock import MagicMock, patch


def _bin(item_code, warehouse, qty, warehouse_type=None):
    """One row as ``_resolve_stock_elsewhere_rows`` returns it."""
    return {
        "item_code": item_code,
        "warehouse": warehouse,
        "warehouse_type": warehouse_type,
        "qty": qty,
    }


class TestIsNonSellableWarehouse(unittest.TestCase):
    """WIP and Rejected stock is not stock anybody can be sent to fetch."""

    def _call(self, warehouse, warehouse_type=None):
        from jarz_pos.services.production_planning import is_non_sellable_warehouse

        return is_non_sellable_warehouse(warehouse, warehouse_type)

    def test_the_exact_warehouse_types_are_excluded(self):
        self.assertTrue(self._call("Somewhere - J", "Work In Progress"))
        self.assertTrue(self._call("Somewhere - J", "Rejected"))

    def test_the_short_wip_type_this_site_actually_uses_is_excluded(self):
        # ``_find_company_warehouse`` resolves WIP with warehouse_type "WIP",
        # which is not one of ERPNext's own type labels.
        self.assertTrue(self._call("Somewhere - J", "WIP"))

    def test_the_name_is_enough_when_the_type_was_never_set(self):
        self.assertTrue(self._call("WIP - J"))
        self.assertTrue(self._call("Work In Progress - J", ""))
        self.assertTrue(self._call("Rejected - J", None))

    def test_an_ordinary_store_is_sellable(self):
        self.assertFalse(self._call("Nasr City - J", "Transit"))
        self.assertFalse(self._call("Raw Material - J"))
        self.assertFalse(self._call("Stores - J", None))

    def test_blanks_are_not_excluded_by_accident(self):
        self.assertFalse(self._call("", None))
        self.assertFalse(self._call(None, None))


class TestShapeStockElsewhere(unittest.TestCase):
    """Every rule in the contract, decided without a database."""

    def _call(self, item_codes, rows, exclude=None):
        from jarz_pos.services.production_planning import shape_stock_elsewhere

        return shape_stock_elsewhere(item_codes, rows, exclude)

    def test_stock_in_one_other_warehouse(self):
        found = self._call(
            ["CHEESE"],
            [_bin("CHEESE", "Nasr City - J", 40.5)],
            {"CHEESE": "Raw Material - J"},
        )

        self.assertAlmostEqual(40.5, found["CHEESE"]["available_elsewhere"])
        self.assertEqual(
            [{"warehouse": "Nasr City - J", "available_qty": 40.5}],
            found["CHEESE"]["alternatives"],
        )

    def test_several_warehouses_are_biggest_pile_first(self):
        found = self._call(
            ["CHEESE"],
            [
                _bin("CHEESE", "Dokki - J", 5.0),
                _bin("CHEESE", "Nasr City - J", 40.5),
                _bin("CHEESE", "Maadi - J", 12.0),
            ],
            {"CHEESE": "Raw Material - J"},
        )

        self.assertEqual(
            ["Nasr City - J", "Maadi - J", "Dokki - J"],
            [alt["warehouse"] for alt in found["CHEESE"]["alternatives"]],
        )
        self.assertAlmostEqual(57.5, found["CHEESE"]["available_elsewhere"])

    def test_equal_quantities_break_the_tie_on_warehouse_name(self):
        # Stability matters: the message names the top two, and two calls a
        # second apart must not name different warehouses.
        found = self._call(
            ["CHEESE"],
            [
                _bin("CHEESE", "Zamalek - J", 10.0),
                _bin("CHEESE", "Dokki - J", 10.0),
                _bin("CHEESE", "Maadi - J", 10.0),
            ],
        )

        self.assertEqual(
            ["Dokki - J", "Maadi - J", "Zamalek - J"],
            [alt["warehouse"] for alt in found["CHEESE"]["alternatives"]],
        )

    def test_nothing_anywhere_is_zero_and_empty_never_none(self):
        # "We looked and found nothing" is a real answer here and has to be
        # distinguishable from "nobody looked", which is the field being absent.
        found = self._call(["CHEESE"], [])

        self.assertEqual(0.0, found["CHEESE"]["available_elsewhere"])
        self.assertEqual([], found["CHEESE"]["alternatives"])
        self.assertIsNotNone(found["CHEESE"]["alternatives"])

    def test_the_source_warehouse_is_excluded(self):
        # It is the warehouse already reported as short; naming it would answer
        # "it is where you already looked".
        found = self._call(
            ["CHEESE"],
            [
                _bin("CHEESE", "Raw Material - J", 1.4),
                _bin("CHEESE", "Nasr City - J", 40.5),
            ],
            {"CHEESE": "Raw Material - J"},
        )

        self.assertEqual(
            ["Nasr City - J"], [alt["warehouse"] for alt in found["CHEESE"]["alternatives"]]
        )
        self.assertAlmostEqual(40.5, found["CHEESE"]["available_elsewhere"])

    def test_a_negative_bin_elsewhere_is_not_counted_and_never_subtracts(self):
        # A negative Bin is a counting error, not a debt.  Letting it subtract
        # would hide 40.5 Kg of real cheese behind somebody else's miscount.
        found = self._call(
            ["CHEESE"],
            [
                _bin("CHEESE", "Nasr City - J", 40.5),
                _bin("CHEESE", "Dokki - J", -100.0),
            ],
        )

        self.assertAlmostEqual(40.5, found["CHEESE"]["available_elsewhere"])
        self.assertEqual(
            ["Nasr City - J"], [alt["warehouse"] for alt in found["CHEESE"]["alternatives"]]
        )

    def test_a_zero_bin_is_not_an_alternative(self):
        found = self._call(["CHEESE"], [_bin("CHEESE", "Dokki - J", 0.0)])

        self.assertEqual(0.0, found["CHEESE"]["available_elsewhere"])
        self.assertEqual([], found["CHEESE"]["alternatives"])

    def test_wip_and_rejected_warehouses_are_excluded(self):
        found = self._call(
            ["CHEESE"],
            [
                _bin("CHEESE", "WIP - J", 200.0, "Work In Progress"),
                _bin("CHEESE", "Quarantine - J", 80.0, "Rejected"),
                _bin("CHEESE", "Bakery WIP - J", 60.0),
                _bin("CHEESE", "Nasr City - J", 40.5),
            ],
        )

        self.assertAlmostEqual(40.5, found["CHEESE"]["available_elsewhere"])
        self.assertEqual(
            ["Nasr City - J"], [alt["warehouse"] for alt in found["CHEESE"]["alternatives"]]
        )

    def test_several_items_are_kept_apart(self):
        found = self._call(
            ["CHEESE", "CREAM", "FLOUR"],
            [
                _bin("CHEESE", "Nasr City - J", 40.5),
                _bin("CREAM", "Dokki - J", 3.0),
            ],
            {"CHEESE": "Raw Material - J", "CREAM": "Raw Material - J"},
        )

        self.assertAlmostEqual(40.5, found["CHEESE"]["available_elsewhere"])
        self.assertAlmostEqual(3.0, found["CREAM"]["available_elsewhere"])
        self.assertEqual(0.0, found["FLOUR"]["available_elsewhere"])
        self.assertEqual([], found["FLOUR"]["alternatives"])

    def test_rows_for_an_item_nobody_asked_about_are_ignored(self):
        found = self._call(["CHEESE"], [_bin("SUGAR", "Dokki - J", 99.0)])

        self.assertEqual(["CHEESE"], list(found))
        self.assertEqual(0.0, found["CHEESE"]["available_elsewhere"])

    def test_no_items_asked_about_is_an_empty_answer(self):
        self.assertEqual({}, self._call([], [_bin("CHEESE", "Dokki - J", 5.0)]))

    def test_an_unparseable_quantity_is_skipped_not_crashed(self):
        found = self._call(
            ["CHEESE"],
            [_bin("CHEESE", "Dokki - J", "not a number"), _bin("CHEESE", "Maadi - J", 2.0)],
        )

        self.assertAlmostEqual(2.0, found["CHEESE"]["available_elsewhere"])


class TestResolveStockElsewhereRows(unittest.TestCase):
    """The single batched read behind the whole feature."""

    def test_one_query_for_every_item(self):
        from jarz_pos.services import production_planning as planning

        with patch("jarz_pos.services.production_planning.frappe") as mock_frappe:
            mock_frappe.db.sql.return_value = [_bin("CHEESE", "Nasr City - J", 40.5)]

            rows = planning._resolve_stock_elsewhere_rows(
                ["CHEESE", "CREAM", "FLOUR"], "Jarz Co"
            )

        self.assertEqual(1, mock_frappe.db.sql.call_count)
        values = mock_frappe.db.sql.call_args.args[1]
        self.assertEqual(["CHEESE", "CREAM", "FLOUR"], values["codes"])
        self.assertEqual("Jarz Co", values["company"])
        self.assertEqual([_bin("CHEESE", "Nasr City - J", 40.5)], rows)

    def test_the_query_is_scoped_to_the_company_and_leaf_warehouses(self):
        from jarz_pos.services import production_planning as planning

        with patch("jarz_pos.services.production_planning.frappe") as mock_frappe:
            mock_frappe.db.sql.return_value = []
            planning._resolve_stock_elsewhere_rows(["CHEESE"], "Jarz Co")

        sql = mock_frappe.db.sql.call_args.args[0]
        self.assertIn("w.company = %(company)s", sql)
        self.assertIn("w.is_group = 0", sql)

    def test_nothing_to_look_up_never_touches_the_database(self):
        from jarz_pos.services import production_planning as planning

        with patch("jarz_pos.services.production_planning.frappe") as mock_frappe:
            self.assertEqual([], planning._resolve_stock_elsewhere_rows([], "Jarz Co"))
            self.assertEqual([], planning._resolve_stock_elsewhere_rows(["CHEESE"], ""))

        mock_frappe.db.sql.assert_not_called()

    def test_a_read_failure_degrades_to_no_alternatives(self):
        from jarz_pos.services import production_planning as planning

        with patch("jarz_pos.services.production_planning.frappe") as mock_frappe:
            mock_frappe.db.sql.side_effect = Exception("Bin table is on fire")

            rows = planning._resolve_stock_elsewhere_rows(["CHEESE"], "Jarz Co")

        self.assertEqual([], rows)
        mock_frappe.log_error.assert_called_once()


class TestFindStockElsewhere(unittest.TestCase):
    def test_one_lookup_covers_every_item(self):
        from jarz_pos.services import production_planning as planning

        with patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows",
            return_value=[_bin("CHEESE", "Nasr City - J", 40.5)],
        ) as mock_rows:
            found = planning.find_stock_elsewhere(
                ["CHEESE", "CREAM"], "Jarz Co", {"CHEESE": "Raw Material - J"}
            )

        mock_rows.assert_called_once()
        self.assertEqual(["CHEESE", "CREAM"], mock_rows.call_args.args[0])
        self.assertAlmostEqual(40.5, found["CHEESE"]["available_elsewhere"])
        self.assertEqual([], found["CREAM"]["alternatives"])


class TestAttachStockElsewhere(unittest.TestCase):
    def test_it_stamps_both_fields_onto_the_rows(self):
        from jarz_pos.services import production_planning as planning

        rows = [{"item_code": "CHEESE", "source_warehouse": "Raw Material - J"}]
        with patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows",
            return_value=[_bin("CHEESE", "Nasr City - J", 40.5)],
        ):
            planning.attach_stock_elsewhere(rows, "Jarz Co")

        self.assertAlmostEqual(40.5, rows[0]["available_elsewhere"])
        self.assertEqual("Nasr City - J", rows[0]["alternatives"][0]["warehouse"])

    def test_a_lookup_failure_leaves_the_row_intact_and_never_raises(self):
        from jarz_pos.services import production_planning as planning

        rows = [{"item_code": "CHEESE", "source_warehouse": "Raw Material - J"}]
        with patch(
            "jarz_pos.services.production_planning.find_stock_elsewhere",
            side_effect=Exception("lookup exploded"),
        ), patch("jarz_pos.services.production_planning.frappe") as mock_frappe:
            planning.attach_stock_elsewhere(rows, "Jarz Co")

        self.assertEqual(0.0, rows[0]["available_elsewhere"])
        self.assertEqual([], rows[0]["alternatives"])
        mock_frappe.log_error.assert_called_once()

    def test_no_rows_is_not_a_query(self):
        from jarz_pos.services import production_planning as planning

        with patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows"
        ) as mock_rows:
            planning.attach_stock_elsewhere([], "Jarz Co")
            planning.attach_stock_elsewhere([{"item_name": "no code"}], "Jarz Co")

        mock_rows.assert_not_called()


class TestPrecheckIssuesCarryTheAlternatives(unittest.TestCase):
    """Surface 1 — ``_get_material_precheck_issues``."""

    BOM_ITEMS = {
        "PIST-SPR": {
            "item_code": "PIST-SPR",
            "item_name": "Pistachio spread",
            "qty": 1.83,
            "uom": "Kg",
            "stock_uom": "Kg",
            "source_warehouse": "Raw Material - J",
            "default_warehouse": None,
            "include_item_in_manufacturing": 1,
            "idx": 1,
        }
    }
    LINE = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 61}

    def _issues(self, elsewhere_rows):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_get_bom_items_as_dict",
            return_value=MagicMock(return_value=self.BOM_ITEMS),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_get_latest_stock_qty",
            return_value=MagicMock(return_value=1.408),
        ), patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows",
            return_value=elsewhere_rows,
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = 0
            return manufacturing._get_material_precheck_issues(self.LINE, "Jarz Co")

    def test_a_shortage_says_where_the_stock_is(self):
        issues = self._issues([_bin("PIST-SPR", "Nasr City - J", 40.5)])

        self.assertEqual(1, len(issues))
        self.assertEqual("insufficient_stock", issues[0]["type"])
        # The existing fields are untouched — this is additive.
        self.assertAlmostEqual(0.422, issues[0]["missing_qty"], places=3)
        self.assertAlmostEqual(40.5, issues[0]["available_elsewhere"])
        self.assertEqual(
            [{"warehouse": "Nasr City - J", "available_qty": 40.5}], issues[0]["alternatives"]
        )

    def test_none_anywhere_reports_zero_rather_than_nothing(self):
        issues = self._issues([])

        self.assertEqual(0.0, issues[0]["available_elsewhere"])
        self.assertEqual([], issues[0]["alternatives"])

    def test_the_short_warehouse_itself_is_never_offered_as_an_alternative(self):
        issues = self._issues(
            [
                _bin("PIST-SPR", "Raw Material - J", 1.408),
                _bin("PIST-SPR", "Nasr City - J", 40.5),
            ]
        )

        self.assertEqual(
            ["Nasr City - J"], [alt["warehouse"] for alt in issues[0]["alternatives"]]
        )


class TestPrecheckMessageNamesTheOtherStore(unittest.TestCase):
    """Surface 2 — ``_assert_material_availability``.

    The block itself is not up for debate here: every one of these still ends in
    ``frappe.throw``.  What changes is what the operator reads.
    """

    LINE = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE"}

    def _issue(self, **overrides):
        issue = {
            "type": "insufficient_stock",
            "item_code": "PIST-SPR",
            "item_name": "Pistachio spread",
            "uom": "Kg",
            "required_qty": 1.83,
            "available_qty": 1.408,
            "missing_qty": 0.422,
            "source_warehouse": "Raw Material - J",
        }
        issue.update(overrides)
        return issue

    def _message(self, issues):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._get_material_precheck_issues", return_value=issues
        ), patch("jarz_pos.api.manufacturing._", new=lambda msg: msg), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            manufacturing._assert_material_availability(self.LINE, "Jarz Co")

        mock_frappe.throw.assert_called_once()
        return mock_frappe.throw.call_args.args[0]

    def test_the_other_store_is_named_after_the_existing_numbers(self):
        message = self._message(
            [
                self._issue(
                    available_elsewhere=40.5,
                    alternatives=[{"warehouse": "Nasr City - J", "available_qty": 40.5}],
                )
            ]
        )

        self.assertIn("is short by 0.422 Kg (required 1.830, available 1.408)", message)
        self.assertIn("; 40.5 Kg is available in Nasr City - J", message)

    def test_at_most_two_warehouses_are_named_and_the_rest_are_counted(self):
        message = self._message(
            [
                self._issue(
                    available_elsewhere=60.0,
                    alternatives=[
                        {"warehouse": "Nasr City - J", "available_qty": 40.0},
                        {"warehouse": "Maadi - J", "available_qty": 12.0},
                        {"warehouse": "Dokki - J", "available_qty": 5.0},
                        {"warehouse": "Zamalek - J", "available_qty": 3.0},
                    ],
                )
            ]
        )

        self.assertIn("; 60 Kg is available in Nasr City - J, Maadi - J and 2 more", message)
        self.assertNotIn("Dokki", message)

    def test_no_alternatives_leaves_the_old_wording_exactly_as_it_was(self):
        with_none = self._message([self._issue(available_elsewhere=0.0, alternatives=[])])
        never_looked = self._message([self._issue()])

        self.assertEqual(with_none, never_looked)
        self.assertNotIn("is available in", with_none)

    def test_the_other_issue_types_are_untouched(self):
        message = self._message(
            [
                {
                    "type": "missing_source_warehouse",
                    "item_code": "PIST-SPR",
                    "item_name": "Pistachio spread",
                }
            ]
        )

        self.assertIn("has no source warehouse configured", message)
        self.assertNotIn("is available in", message)

    def test_a_failed_lookup_still_blocks_the_batch(self):
        from jarz_pos.api import manufacturing

        bom_items = TestPrecheckIssuesCarryTheAlternatives.BOM_ITEMS
        line = dict(TestPrecheckIssuesCarryTheAlternatives.LINE)

        with patch(
            "jarz_pos.api.manufacturing._resolve_get_bom_items_as_dict",
            return_value=MagicMock(return_value=bom_items),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_get_latest_stock_qty",
            return_value=MagicMock(return_value=1.408),
        ), patch(
            "jarz_pos.services.production_planning.find_stock_elsewhere",
            side_effect=Exception("Bin table is on fire"),
        ), patch("jarz_pos.services.production_planning.frappe"), patch(
            "jarz_pos.api.manufacturing._", new=lambda msg: msg
        ), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            mock_frappe.db.get_value.return_value = 0

            manufacturing._assert_material_availability(line, "Jarz Co")

        # The whole point: losing the hint must never lose the block.
        mock_frappe.throw.assert_called_once()
        message = mock_frappe.throw.call_args.args[0]
        self.assertIn("is short by 0.422 Kg", message)
        self.assertNotIn("is available in", message)


class TestBasketRollupCarriesTheAlternatives(unittest.TestCase):
    """Surface 3 — ``build_basket_rollup`` shortage rows."""

    def _rollup(self, elsewhere_rows):
        from jarz_pos.services import production_planning as planning

        components = [
            {
                "item_code": "CHEESE",
                "item_name": "Cream cheese",
                "stock_uom": "Kg",
                "required_qty": 12.0,
                "source_warehouse": "Raw Material - J",
                "available_qty": 2.0,
            },
            {
                "item_code": "SUGAR",
                "item_name": "Sugar",
                "stock_uom": "Kg",
                "required_qty": 1.0,
                "source_warehouse": "Raw Material - J",
                "available_qty": 50.0,
            },
        ]

        with patch(
            "jarz_pos.services.production_planning._resolve_required_material_rows",
            return_value=MagicMock(return_value=components),
        ), patch(
            "jarz_pos.services.production_planning._resolve_bom_company",
            return_value="Jarz Co",
        ), patch(
            "jarz_pos.services.production_planning._resolve_bin_stock_map",
            return_value={("CHEESE", "Raw Material - J"): 2.0, ("SUGAR", "Raw Material - J"): 50.0},
        ), patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows",
            return_value=elsewhere_rows,
        ) as mock_rows:
            rollup = planning.build_basket_rollup(
                [{"item_code": "CAKE-A", "bom_name": "BOM-A", "item_qty": 10}], "Jarz Co"
            )
        return rollup, mock_rows

    def test_a_shortage_row_says_where_the_stock_is(self):
        rollup, mock_rows = self._rollup([_bin("CHEESE", "Nasr City - J", 40.5)])

        self.assertEqual(1, len(rollup["shortages"]))
        shortage = rollup["shortages"][0]
        self.assertEqual("CHEESE", shortage["item_code"])
        self.assertAlmostEqual(10.0, shortage["missing_qty"])
        self.assertAlmostEqual(40.5, shortage["available_elsewhere"])
        self.assertEqual("Nasr City - J", shortage["alternatives"][0]["warehouse"])
        # One lookup for the whole basket, and only for the short item.
        mock_rows.assert_called_once()
        self.assertEqual(["CHEESE"], mock_rows.call_args.args[0])

    def test_a_component_that_is_not_short_is_not_looked_up(self):
        rollup, _ = self._rollup([])

        covered = [c for c in rollup["components"] if c["item_code"] == "SUGAR"][0]
        self.assertNotIn("alternatives", covered)
        self.assertNotIn("available_elsewhere", covered)

    def test_the_basket_message_points_at_the_other_store_too(self):
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._", new=lambda msg: msg):
            message = manufacturing._format_basket_shortage_message(
                [
                    {
                        "item_code": "CHEESE",
                        "item_name": "Cream cheese",
                        "uom": "Kg",
                        "source_warehouse": "Raw Material - J",
                        "required_qty": 12.0,
                        "available_qty": 2.0,
                        "missing_qty": 10.0,
                        "reason": "insufficient_stock",
                        "available_elsewhere": 40.5,
                        "alternatives": [{"warehouse": "Nasr City - J", "available_qty": 40.5}],
                    }
                ]
            )

        # Two shortage messages that answer "where is it" differently is how an
        # operator learns to distrust both.
        self.assertIn("; 40.5 Kg is available in Nasr City - J", message)


class TestBasesPreviewCarriesTheAlternatives(unittest.TestCase):
    """Surface 4 — ``api/subassembly.preview_base_batch``."""

    def _preview(self, elsewhere_rows):
        from jarz_pos.api import subassembly

        rows = [
            {
                "item_code": "CHEESE",
                "item_name": "Cream cheese",
                "uom": "Kg",
                "stock_uom": "Kg",
                "required_qty": 12.0,
                "available_qty": 2.0,
                "source_warehouse": "Raw Material - J",
            },
            {
                "item_code": "SUGAR",
                "item_name": "Sugar",
                "uom": "Kg",
                "stock_uom": "Kg",
                "required_qty": 1.0,
                "available_qty": 50.0,
                "source_warehouse": "Raw Material - J",
            },
        ]

        with patch("jarz_pos.api.subassembly._ensure_production_view_access"), patch(
            "jarz_pos.api.subassembly._resolve_item_row",
            return_value={"name": "CHEESECAKE-MIX", "stock_uom": "Kg"},
        ), patch(
            "jarz_pos.api.subassembly._resolve_bom_row",
            return_value={
                "name": "BOM-MIX",
                "item": "CHEESECAKE-MIX",
                "quantity": 40.0,
                "company": "Jarz Co",
                "docstatus": 1,
            },
        ), patch(
            "jarz_pos.api.subassembly._resolve_required_material_rows", return_value=rows
        ), patch(
            "jarz_pos.api.subassembly._resolve_valuation_rate", return_value=0.0
        ), patch(
            "jarz_pos.api.subassembly._resolve_mix_item", return_value="CHEESECAKE-MIX"
        ), patch(
            "jarz_pos.api.subassembly._resolve_mix_run_sizes", return_value=None
        ), patch(
            "jarz_pos.api.subassembly._resolve_has_sop", return_value=False
        ), patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows",
            return_value=elsewhere_rows,
        ) as mock_rows:
            payload = subassembly.preview_base_batch(
                "CHEESECAKE-MIX", bom_name="BOM-MIX", batches=1, company="Jarz Co"
            )
        return payload, mock_rows

    def test_a_short_component_says_where_the_stock_is(self):
        payload, mock_rows = self._preview([_bin("CHEESE", "Nasr City - J", 40.5)])

        short = [c for c in payload["components"] if c["item_code"] == "CHEESE"][0]
        self.assertTrue(payload["has_shortage"])
        self.assertAlmostEqual(10.0, short["shortfall"])
        self.assertAlmostEqual(40.5, short["available_elsewhere"])
        self.assertEqual("Nasr City - J", short["alternatives"][0]["warehouse"])
        mock_rows.assert_called_once()
        self.assertEqual(["CHEESE"], mock_rows.call_args.args[0])

    def test_a_covered_component_is_left_alone(self):
        payload, _ = self._preview([])

        covered = [c for c in payload["components"] if c["item_code"] == "SUGAR"][0]
        self.assertEqual(0.0, covered["shortfall"])
        self.assertNotIn("alternatives", covered)


class TestCapacityMapLimitingComponent(unittest.TestCase):
    """The board's ``limiting_component``, at one query for the whole board."""

    ROW = {
        "item_code": "TIRA-L",
        "default_bom": "BOM-TIRA-L",
        "company": "Jarz Co",
        "bom_qty": 100,
    }
    COMPONENT = {
        "item_code": "CHEESE",
        "item_name": "Cream cheese",
        "required_qty": 12.0,
        "source_warehouse": "Raw Material - J",
    }

    def _capacity(self, on_hand, elsewhere_rows):
        from jarz_pos.services import production_planning as planning

        with patch(
            "jarz_pos.services.production_planning._resolve_required_material_rows",
            return_value=MagicMock(return_value=[dict(self.COMPONENT)]),
        ), patch(
            "jarz_pos.services.production_planning._resolve_bin_stock_map",
            return_value={("CHEESE", "Raw Material - J"): on_hand},
        ), patch(
            "jarz_pos.services.production_planning._resolve_stock_elsewhere_rows",
            return_value=elsewhere_rows,
        ) as mock_rows, patch("jarz_pos.services.production_planning.frappe"):
            capacity = planning.build_capacity_map([dict(self.ROW)], "Jarz Co")
        return capacity, mock_rows

    def test_a_blocking_component_says_where_the_stock_is(self):
        capacity, mock_rows = self._capacity(2.0, [_bin("CHEESE", "Nasr City - J", 40.5)])

        limiting = capacity["TIRA-L"]["limiting_component"]
        self.assertEqual(0, capacity["TIRA-L"]["can_make_now_batches"])
        self.assertAlmostEqual(40.5, limiting["available_elsewhere"])
        self.assertEqual("Nasr City - J", limiting["alternatives"][0]["warehouse"])
        # One query for the board, never one per row.
        mock_rows.assert_called_once()

    def test_a_component_that_still_allows_a_batch_is_not_looked_up(self):
        # The board renders every item; a per-row lookup here is exactly what
        # made this screen slow, and "limiting" is not the same as "blocking".
        capacity, mock_rows = self._capacity(50.0, [])

        limiting = capacity["TIRA-L"]["limiting_component"]
        self.assertEqual(4, capacity["TIRA-L"]["can_make_now_batches"])
        self.assertNotIn("alternatives", limiting)
        mock_rows.assert_not_called()


class TestShapeLimitingComponentPassesItThrough(unittest.TestCase):
    """The Bases card reads the same capacity row, so it must not strip them."""

    def test_the_fields_survive_the_card_shaping(self):
        from jarz_pos.api import subassembly

        shaped = subassembly._shape_limiting_component(
            {
                "item_code": "CHEESE",
                "item_name": "Cream cheese",
                "available_qty": 2.0,
                "required_qty": 12.0,
                "reason": "insufficient_stock",
                "available_elsewhere": 40.5,
                "alternatives": [{"warehouse": "Nasr City - J", "available_qty": 40.5}],
            }
        )

        self.assertAlmostEqual(40.5, shaped["available_elsewhere"])
        self.assertEqual("Nasr City - J", shaped["alternatives"][0]["warehouse"])

    def test_a_row_nobody_looked_up_keeps_the_fields_absent(self):
        from jarz_pos.api import subassembly

        shaped = subassembly._shape_limiting_component(
            {"item_code": "CHEESE", "available_qty": 2.0, "required_qty": 12.0}
        )

        # Absent means "nobody looked"; 0.0 would claim there is none anywhere.
        self.assertNotIn("available_elsewhere", shaped)
        self.assertNotIn("alternatives", shaped)

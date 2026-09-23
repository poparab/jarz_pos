"""Unit tests for the production round maths.

Pure — no ``frappe``, no site — which is the point of keeping it in
``services/production_round_planning``.  The endpoint tests live with the other
replenishment endpoint tests in ``test_replenishment.py``.

Run site-less::

    PYTHONPATH="<apps>/jarz_pos" python -m unittest jarz_pos.tests.test_production_round_planning
"""

import datetime
import unittest

from jarz_pos.services import production_round_planning as rp


class TestParameters(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(
            {
                "cycle_days": 14,
                "backup_days": 7,
                "sales_weeks": 8,
                "batch_medium": 120,
                "batch_large": 77,
            },
            rp.coerce_parameters(),
        )

    def test_backup_zero_is_a_real_answer(self):
        self.assertEqual(0, rp.coerce_parameters(backup_days="0")["backup_days"])
        self.assertEqual(7, rp.coerce_parameters(backup_days="")["backup_days"])
        self.assertEqual(30, rp.coerce_parameters(backup_days=99)["backup_days"])

    def test_clamps(self):
        params = rp.coerce_parameters(
            cycle_days=100, sales_weeks=1, batch_medium="junk", batch_large=50000
        )
        self.assertEqual(60, params["cycle_days"])
        self.assertEqual(2, params["sales_weeks"])
        self.assertEqual(120, params["batch_medium"])
        self.assertEqual(10000, params["batch_large"])
        self.assertEqual(14, rp.coerce_parameters(cycle_days=0)["cycle_days"])


class TestSalesSeries(unittest.TestCase):
    def test_window_is_whole_weeks_ending_yesterday(self):
        self.assertEqual(
            (datetime.date(2026, 7, 29), datetime.date(2026, 9, 22)),
            rp.sales_window(datetime.date(2026, 9, 23), 8),
        )

    def test_week_index(self):
        to_date = datetime.date(2026, 9, 22)
        self.assertEqual(0, rp.week_index(to_date, datetime.date(2026, 9, 22)))
        self.assertEqual(0, rp.week_index(to_date, datetime.date(2026, 9, 16)))
        self.assertEqual(1, rp.week_index(to_date, datetime.date(2026, 9, 15)))
        self.assertEqual(7, rp.week_index(to_date, datetime.date(2026, 7, 29)))

    def test_missing_weeks_are_zero_and_negative_weeks_are_floored(self):
        self.assertEqual([5.0, 0.0, 0.0, 0.0], rp.weekly_series({0: 5, 2: -3, 9: 100}, 4))
        self.assertEqual([0.0, 0.0], rp.weekly_series(None, 2))

    def test_mean_and_population_sd(self):
        avg, sd = rp.mean_and_sd([2, 4, 4, 4, 5, 5, 7, 9])
        self.assertAlmostEqual(5.0, avg)
        self.assertAlmostEqual(2.0, sd)
        self.assertEqual((0.0, 0.0), rp.mean_and_sd([]))


class TestPar(unittest.TestCase):
    def _par(self, avg, sd=0.0, cycle=14, backup=7):
        return rp.par_level(avg=avg, sd=sd, cycle_days=cycle, backup_days=backup)

    def test_steady_jar_holds_cycle_plus_backup(self):
        # Blueberry Large at Nasr city: 35.8/week x 3 weeks = 107.4 -> 108.
        self.assertEqual(108, self._par(35.8))

    def test_volatile_jar_gets_more_than_the_backup_week(self):
        # 2 x 10 + 1.28 x 10 x sqrt(2) = 38.1 beats the flat 30.
        self.assertEqual(39, self._par(10, sd=10))

    def test_slow_seller_holds_at_least_three(self):
        self.assertEqual(3, self._par(0.5))

    def test_a_jar_that_does_not_sell_holds_nothing(self):
        self.assertEqual(0, self._par(0, sd=4))

    def test_no_backup_is_just_the_cycle(self):
        self.assertEqual(20, self._par(10, backup=0))


class TestFillCoverBackup(unittest.TestCase):
    def test_fill_tops_up_to_par(self):
        self.assertEqual(107, rp.fill_qty(par=108, on_hand=1))
        self.assertEqual(0, rp.fill_qty(par=108, on_hand=200))

    def test_negative_stock_never_inflates_the_fill(self):
        self.assertEqual(108, rp.fill_qty(par=108, on_hand=-55))

    def test_days_of_cover(self):
        self.assertEqual(0.2, rp.days_of_cover(on_hand=1, avg=35.8))
        self.assertEqual(0.0, rp.days_of_cover(on_hand=-5, avg=35.8))
        self.assertIsNone(rp.days_of_cover(on_hand=40, avg=0))

    def test_below_backup(self):
        self.assertTrue(rp.is_below_backup(on_hand=1, avg=35.8, backup_days=7))
        self.assertTrue(rp.is_below_backup(on_hand=-9, avg=35.8, backup_days=7))
        self.assertFalse(rp.is_below_backup(on_hand=40, avg=35.8, backup_days=7))
        self.assertFalse(rp.is_below_backup(on_hand=0, avg=0, backup_days=7))
        self.assertFalse(rp.is_below_backup(on_hand=0, avg=10, backup_days=0))


class TestBatchRounding(unittest.TestCase):
    def test_tolerance(self):
        self.assertEqual(8, rp.batch_tolerance(120))
        self.assertEqual(5, rp.batch_tolerance(77))
        self.assertEqual(5, rp.batch_tolerance(16))

    def test_medium_120(self):
        self.assertEqual((0.0, 0), rp.round_to_batches(3, 120))
        self.assertEqual((0.25, 30), rp.round_to_batches(9, 120))
        self.assertEqual((0.5, 60), rp.round_to_batches(51, 120))
        # need - tol = 101 -> 3 quarters (90) is short, 4 (120) covers it.
        self.assertEqual((1.0, 120), rp.round_to_batches(109, 120))

    def test_large_77(self):
        self.assertEqual((0.0, 0), rp.round_to_batches(5, 77))
        self.assertEqual((1.0, 77), rp.round_to_batches(63, 77))
        self.assertEqual((1.5, 115), rp.round_to_batches(105, 77))
        self.assertEqual((0.25, 19), rp.round_to_batches(23, 77))

    def test_nothing_needed(self):
        self.assertEqual((0.0, 0), rp.round_to_batches(0, 120))


class TestStatusAndFlavour(unittest.TestCase):
    def test_status(self):
        self.assertEqual("no_sales", rp.jar_status(weekly_sales=0, batches=1, any_below_backup=True))
        self.assertEqual("now", rp.jar_status(weekly_sales=5, batches=0.5, any_below_backup=True))
        self.assertEqual("round", rp.jar_status(weekly_sales=5, batches=0.5, any_below_backup=False))
        self.assertEqual("covered", rp.jar_status(weekly_sales=5, batches=0, any_below_backup=True))

    def test_flavour(self):
        self.assertEqual("Blueberry", rp.flavour_of("Blueberry Large"))
        self.assertEqual("Chocolate Hazelnut", rp.flavour_of("Chocolate Hazelnut Medium"))
        self.assertEqual("Large", rp.flavour_of("Large"))


class TestBomHelpers(unittest.TestCase):
    def test_item_default_bom_wins_then_is_default(self):
        rows = [
            {"name": "BOM-A-1", "item": "A", "quantity": 1, "is_default": 1, "default_bom": "BOM-A-2"},
            {"name": "BOM-A-2", "item": "A", "quantity": 2, "is_default": 0, "default_bom": "BOM-A-2"},
            {"name": "BOM-B-1", "item": "B", "quantity": 5, "is_default": 1, "default_bom": None},
            {"name": "BOM-C-1", "item": "C", "quantity": 5, "is_default": 0, "default_bom": None},
        ]
        picked = rp.pick_default_boms(rows)
        self.assertEqual({"A", "B"}, set(picked))
        self.assertEqual("BOM-A-2", picked["A"]["name"])
        self.assertEqual(2.0, picked["A"]["quantity"])
        self.assertEqual("BOM-B-1", picked["B"]["name"])

    def test_alternatives_one_and_two_way(self):
        alts = rp.alternatives_map(
            [
                {"item_code": "LID", "alternative_item_code": "LID-B", "two_way": 1},
                {"item_code": "FLOUR", "alternative_item_code": "FLOUR-X", "two_way": 0},
            ]
        )
        self.assertEqual(["LID-B"], alts["LID"])
        self.assertEqual(["LID"], alts["LID-B"])
        self.assertEqual(["FLOUR-X"], alts["FLOUR"])
        self.assertNotIn("FLOUR-X", alts)


def _line(code, qty, dne=0):
    return {"item_code": code, "stock_qty": qty, "stock_uom": "Kg", "do_not_explode": dne}


class TestExplosion(unittest.TestCase):
    BOMS = {
        "JAR-A": {"name": "BOM-JAR-A", "quantity": 10, "lines": [
            _line("BISCUIT", 2.0, dne=1),     # stored base
            _line("CHEESE-MIX", 5.0, dne=0),  # phantom made fresh
            _line("LID", 10),                 # leaf
        ]},
        "BISCUIT": {"name": "BOM-BISCUIT", "quantity": 4, "lines": [
            _line("FLOUR", 2.0),
            _line("BUTTER", 1.0),
        ]},
        "CHEESE-MIX": {"name": "BOM-CM", "quantity": 1, "lines": [
            _line("CREAM CHEESE", 0.8),
            _line("SUGAR", 0.2),
        ]},
    }

    def test_stored_base_partly_in_stock_and_a_phantom(self):
        out = rp.explode_round({"JAR-A": 20}, self.BOMS, {"BISCUIT": 1.0, "CHEESE-MIX": 0.5})
        self.assertAlmostEqual(4.0, out["stored"]["BISCUIT"])
        self.assertAlmostEqual(3.0, out["to_make"]["BISCUIT"])
        self.assertAlmostEqual(10.0, out["fresh"]["CHEESE-MIX"])
        m = out["materials"]
        # Only the 3 kg of biscuit still to make is exploded.
        self.assertAlmostEqual(1.5, m["FLOUR"])
        self.assertAlmostEqual(0.75, m["BUTTER"])
        # The phantom is exploded in full, whatever is on the shelf.
        self.assertAlmostEqual(8.0, m["CREAM CHEESE"])
        self.assertAlmostEqual(2.0, m["SUGAR"])
        self.assertAlmostEqual(20.0, m["LID"])
        self.assertNotIn("BISCUIT", m)
        self.assertNotIn("CHEESE-MIX", m)
        # Jar codes propagate through both kinds of sub-assembly.
        self.assertEqual({"JAR-A"}, out["used_by"]["FLOUR"])
        self.assertEqual({"JAR-A"}, out["used_by"]["SUGAR"])

    def test_prep_rows(self):
        stock = {"BISCUIT": 1.0, "CHEESE-MIX": 0.5}
        out = rp.explode_round({"JAR-A": 20}, self.BOMS, stock)
        rows = rp.build_prep_rows(out, self.BOMS, stock, {"BISCUIT": {"item_name": "Butter Biscuit", "stock_uom": "Kg"}})
        self.assertEqual(["BISCUIT", "CHEESE-MIX"], [r["item_code"] for r in rows])
        biscuit, mix = rows
        self.assertEqual(
            {
                "item_code": "BISCUIT", "item_name": "Butter Biscuit", "uom": "Kg",
                "required": 4.0, "on_hand": 1.0, "to_make": 3.0,
                "batch_yield": 4.0, "batches": 0.75, "made_fresh": False,
            },
            biscuit,
        )
        self.assertTrue(mix["made_fresh"])
        self.assertEqual(10.0, mix["to_make"])
        self.assertEqual(0.5, mix["on_hand"])
        self.assertEqual(10.0, mix["batches"])

    def test_negative_stock_never_inflates_a_base(self):
        out = rp.explode_round({"JAR-A": 20}, self.BOMS, {"BISCUIT": -5.0})
        self.assertAlmostEqual(4.0, out["to_make"]["BISCUIT"])
        self.assertAlmostEqual(2.0, out["materials"]["FLOUR"])

    def test_base_fully_in_stock_explodes_nothing(self):
        out = rp.explode_round({"JAR-A": 20}, self.BOMS, {"BISCUIT": 50})
        self.assertEqual(0.0, out["to_make"]["BISCUIT"])
        self.assertNotIn("FLOUR", out["materials"])

    def test_a_base_is_netted_only_after_every_user_has_added_to_it(self):
        # AMIX sorts before BASE alphabetically, but BASE also consumes AMIX:
        # netting AMIX first would miss BASE's share.
        boms = {
            "J1": {"name": "B-J1", "quantity": 1, "lines": [_line("BASE", 1, 1), _line("AMIX", 1, 1)]},
            "BASE": {"name": "B-BASE", "quantity": 1, "lines": [_line("AMIX", 1, 1), _line("FLOUR", 1)]},
            "AMIX": {"name": "B-AMIX", "quantity": 1, "lines": [_line("SUGAR", 1)]},
        }
        out = rp.explode_round({"J1": 10}, boms, {"BASE": 4, "AMIX": 6})
        self.assertAlmostEqual(6.0, out["to_make"]["BASE"])
        self.assertAlmostEqual(16.0, out["stored"]["AMIX"])
        self.assertAlmostEqual(10.0, out["to_make"]["AMIX"])
        self.assertAlmostEqual(10.0, out["materials"]["SUGAR"])
        self.assertAlmostEqual(6.0, out["materials"]["FLOUR"])

    def test_stored_cycle_is_skipped_not_looped(self):
        boms = {
            "J": {"name": "B-J", "quantity": 1, "lines": [_line("X", 1, 1)]},
            "X": {"name": "B-X", "quantity": 1, "lines": [_line("Y", 1, 1)]},
            "Y": {"name": "B-Y", "quantity": 1, "lines": [_line("X", 1, 1), _line("SALT", 1)]},
        }
        out = rp.explode_round({"J": 1}, boms, {})
        self.assertEqual([("Y", "X")], out["cycles"])
        self.assertAlmostEqual(1.0, out["materials"]["SALT"])
        self.assertAlmostEqual(1.0, out["stored"]["X"])

    def test_phantom_cycle_terminates(self):
        boms = {
            "J": {"name": "B-J", "quantity": 1, "lines": [_line("P", 1)]},
            "P": {"name": "B-P", "quantity": 1, "lines": [_line("Q", 1), _line("SALT", 1)]},
            "Q": {"name": "B-Q", "quantity": 1, "lines": [_line("P", 1), _line("OIL", 1)]},
        }
        out = rp.explode_round({"J": 2}, boms, {})
        self.assertTrue(out["cycles"])
        self.assertAlmostEqual(2.0, out["materials"]["SALT"])
        self.assertAlmostEqual(2.0, out["materials"]["OIL"])


class TestMaterialRows(unittest.TestCase):
    def _rows(self, stock, alternatives=None):
        exploded = {
            "materials": {"LID": 20.0, "FLOUR": 10.0, "SUGAR": 4.0},
            "used_by": {"LID": {"JAR-A"}, "FLOUR": {"JAR-A", "JAR-B"}, "SUGAR": {"JAR-B"}},
        }
        meta = {
            "LID": {"item_name": "Jar Lid", "item_group": "Packaging", "stock_uom": "Nos"},
            "FLOUR": {"item_name": "Flour", "item_group": "Raw Material", "stock_uom": "Kg"},
            "SUGAR": {"item_name": "Sugar", "item_group": "Raw Material", "stock_uom": "Kg"},
        }
        return rp.build_material_rows(exploded, stock, alternatives or {}, meta)

    def test_an_alternative_covers_a_shortage(self):
        rows = {r["item_code"]: r for r in self._rows({"LID": 5, "LID-B": 20, "FLOUR": 10, "SUGAR": 4}, {"LID": ["LID-B"]})}
        self.assertEqual(20.0, rows["LID"]["alternative_on_hand"])
        self.assertEqual(0.0, rows["LID"]["missing"])

    def test_negative_stock_is_reported_raw_and_floored(self):
        rows = {r["item_code"]: r for r in self._rows({"FLOUR": -10})}
        self.assertEqual(-10.0, rows["FLOUR"]["on_hand"])
        self.assertEqual(10.0, rows["FLOUR"]["missing"])

    def test_missing_first_by_share_then_name(self):
        # LID 15/20 missing (75%), FLOUR 10/10 (100%), SUGAR covered.
        rows = self._rows({"LID": 5, "SUGAR": 4})
        self.assertEqual(["FLOUR", "LID", "SUGAR"], [r["item_code"] for r in rows])
        self.assertEqual(["JAR-A", "JAR-B"], rows[0]["used_by"])
        self.assertEqual("Packaging", rows[1]["item_group"])
        self.assertEqual("Nos", rows[1]["uom"])

    def test_whole_number_units_round_up(self):
        exploded = {"materials": {"EGG": 56.878, "CREAM": 2.5}, "used_by": {}}
        meta = {
            "EGG": {"item_name": "Eggs", "stock_uom": "piece", "whole_number": True},
            "CREAM": {"item_name": "Cream", "stock_uom": "Kg"},
        }
        rows = {r["item_code"]: r for r in rp.build_material_rows(exploded, {"EGG": 56, "CREAM": 2.0}, {}, meta)}
        self.assertEqual(57.0, rows["EGG"]["required"])
        self.assertEqual(1.0, rows["EGG"]["missing"])
        self.assertEqual(0.5, rows["CREAM"]["missing"])
        rows = {r["item_code"]: r for r in rp.build_material_rows(exploded, {"EGG": 60}, {}, meta)}
        self.assertEqual(0.0, rows["EGG"]["missing"])


class TestBuildProductionRound(unittest.TestCase):
    NASR, DOKKI = "Nasr city - J", "Dokki - J"
    ITEMS = [
        {"item_code": "BLU-L", "item_name": "Blueberry Large", "item_group": "Large"},
        {"item_code": "TIRA-M", "item_name": "Tiramisu Medium", "item_group": "Medium"},
        {"item_code": "OLD-M", "item_name": "Old Medium", "item_group": "Medium"},
    ]
    BOMS = {
        "BLU-L": {"name": "BOM-BLU-L", "quantity": 1, "lines": [
            _line("LID", 1), _line("BISCUIT", 0.1, dne=1),
        ]},
        "BISCUIT": {"name": "BOM-BISCUIT", "quantity": 10, "lines": [_line("FLOUR", 5)]},
    }

    def _round(self, **overrides):
        params = dict(
            generated_on="2026-09-23 18:00:00",
            company="Jarz",
            source_warehouse="Finished Goods - J",
            cycle_days=14,
            backup_days=7,
            sales_weeks=8,
            sales_from="2026-07-29",
            sales_to="2026-09-22",
            batch_sizes={"Medium": 120, "Large": 77},
            items=self.ITEMS,
            branches=[
                {"warehouse": self.NASR, "branch": "Nasr city"},
                {"warehouse": self.DOKKI, "branch": "Dokki"},
            ],
            weekly_sales={
                (self.NASR, "BLU-L"): {i: 35.8 for i in range(8)},
                (self.DOKKI, "BLU-L"): {i: 20 for i in range(8)},
                (self.NASR, "TIRA-M"): {i: 10 for i in range(8)},
            },
            branch_stock={
                (self.NASR, "BLU-L"): 1.0,
                (self.DOKKI, "BLU-L"): 28.0,
                (self.NASR, "TIRA-M"): 26.0,
                (self.NASR, "OLD-M"): -3.0,
            },
            factory_stock={"BLU-L": 76.0},
            boms=self.BOMS,
            material_stock={"LID": 30.0, "BISCUIT": 2.7, "FLOUR": 10.0},
            alternatives={},
            item_meta={"LID": {"item_name": "Jar Lid 330", "item_group": "Packaging", "stock_uom": "Nos"}},
        )
        params.update(overrides)
        return rp.build_production_round(**params)

    def _item(self, payload, code):
        return next(i for i in payload["items"] if i["item_code"] == code)

    def test_top_level_shape(self):
        payload = self._round()
        self.assertEqual(
            {
                "generated_on", "company", "source_warehouse", "cycle_days", "backup_days",
                "cover_days", "sales_weeks", "sales_from", "sales_to", "batch_sizes",
                "summary", "branches", "items", "prep", "materials", "notices",
            },
            set(payload),
        )
        self.assertEqual(21, payload["cover_days"])
        self.assertEqual({"Medium": 120, "Large": 77}, payload["batch_sizes"])

    def test_items_ordered_medium_first_then_flavour(self):
        self.assertEqual(
            ["OLD-M", "TIRA-M", "BLU-L"], [i["item_code"] for i in self._round()["items"]]
        )

    def test_a_sliver_short_is_missing_but_not_blocking(self):
        # 77 lids needed, 76 on hand: listed as missing, jar not flagged.
        payload = self._round(material_stock={"LID": 76.0, "BISCUIT": 2.7, "FLOUR": 10.0})
        lid = next(m for m in payload["materials"] if m["item_code"] == "LID")
        self.assertEqual(1.0, lid["missing"])
        self.assertEqual([], self._item(payload, "BLU-L")["blocked_by"])
        self.assertEqual(0, payload["summary"]["blocked_count"])
        self.assertEqual(1, payload["summary"]["missing_count"])

    def test_the_contract_example_jar(self):
        blu = self._item(self._round(), "BLU-L")
        self.assertEqual("Blueberry", blu["flavour"])
        self.assertEqual("Large", blu["size"])
        self.assertEqual(77, blu["batch_size"])
        self.assertAlmostEqual(55.8, blu["weekly_sales"])
        self.assertEqual(76.0, blu["factory_on_hand"])
        self.assertEqual(139, blu["total_fill"])   # 107 + 32
        self.assertEqual(63, blu["net_need"])
        self.assertEqual(1.0, blu["batches"])
        self.assertEqual(77, blu["jars"])
        self.assertEqual("now", blu["status"])
        self.assertEqual(["LID"], blu["blocked_by"])
        nasr = blu["branches"][0]
        self.assertEqual(
            {
                "warehouse": self.NASR, "label": "Nasr city", "weekly_sales": 35.8,
                "par": 108, "on_hand": 1.0, "fill": 107, "days_of_cover": 0.2,
                "below_backup": True, "stock_is_negative": False,
            },
            nasr,
        )

    def test_covered_and_no_sales(self):
        payload = self._round()
        tira = self._item(payload, "TIRA-M")
        self.assertEqual(4, tira["net_need"])       # inside Medium's tolerance of 8
        self.assertEqual("covered", tira["status"])
        old = self._item(payload, "OLD-M")
        self.assertEqual("no_sales", old["status"])
        self.assertIsNone(old["branches"][0]["days_of_cover"])
        self.assertTrue(old["branches"][0]["stock_is_negative"])
        self.assertEqual(0, old["branches"][0]["fill"])

    def test_prep_and_materials(self):
        payload = self._round()
        self.assertEqual(1, len(payload["prep"]))
        prep = payload["prep"][0]
        self.assertEqual("BISCUIT", prep["item_code"])
        self.assertAlmostEqual(7.7, prep["required"])
        self.assertAlmostEqual(5.0, prep["to_make"])
        self.assertAlmostEqual(0.5, prep["batches"])
        materials = {m["item_code"]: m for m in payload["materials"]}
        self.assertEqual(77.0, materials["LID"]["required"])
        self.assertEqual(47.0, materials["LID"]["missing"])
        self.assertEqual(["BLU-L"], materials["LID"]["used_by"])
        self.assertAlmostEqual(2.5, materials["FLOUR"]["required"])
        self.assertEqual(0.0, materials["FLOUR"]["missing"])
        self.assertEqual("LID", payload["materials"][0]["item_code"])

    def test_summary(self):
        self.assertEqual(
            {
                "batches": {"Medium": 0.0, "Large": 1.0},
                "jars": {"Medium": 0, "Large": 77},
                "jars_total": 77,
                "items_to_make": 1,
                "needed_now_count": 1,
                "missing_count": 1,
                "blocked_count": 1,
            },
            self._round()["summary"],
        )

    def test_branch_totals(self):
        nasr = self._round()["branches"][0]
        self.assertEqual(self.NASR, nasr["warehouse"])
        self.assertEqual("Nasr city", nasr["label"])
        self.assertAlmostEqual(45.8, nasr["weekly_sales"])
        self.assertEqual(138, nasr["par_total"])       # 108 + 30 + 0
        self.assertEqual(27.0, nasr["on_hand_total"])  # 1 + 26, the -3 floored
        self.assertEqual(111, nasr["fill_total"])      # 107 + 4
        self.assertEqual(1, nasr["below_backup_count"])

    def test_notices(self):
        notices = self._round()["notices"]
        self.assertTrue(any("Nasr city" in n and "negative" in n for n in notices), notices)
        self.assertFalse(any("Dokki" in n for n in notices), notices)

    def test_a_jar_without_a_bom_is_named(self):
        payload = self._round(boms={})
        self.assertTrue(any("Blueberry Large" in n and "BOM" in n for n in payload["notices"]))
        self.assertEqual([], payload["materials"])
        self.assertEqual(77, self._item(payload, "BLU-L")["jars"])

    def test_empty_inputs_are_well_formed(self):
        payload = self._round(items=[], branches=[], weekly_sales={}, branch_stock={})
        self.assertEqual([], payload["items"])
        self.assertEqual([], payload["branches"])
        self.assertEqual([], payload["prep"])
        self.assertEqual([], payload["materials"])
        self.assertEqual(0, payload["summary"]["jars_total"])


if __name__ == "__main__":
    unittest.main()

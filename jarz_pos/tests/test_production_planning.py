"""Unit tests for the Production Board planning maths.

Everything under test here is a pure function over plain numbers, so these
tests patch nothing at all — which is the point of keeping the arithmetic out
of the API layer.
"""

import unittest


class TestSuggestedBatches(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.production_planning import suggested_batches

        params = {
            "target_days": 10,
            "velocity": 5.0,
            "season_multiplier": 1.0,
            "on_hand": 0.0,
            "bom_yield": 10.0,
        }
        params.update(kwargs)
        return suggested_batches(**params)

    def test_exact_multiple_does_not_round_up(self):
        # need 50, yield 10 -> exactly 5 batches
        self.assertEqual(5, self._call())

    def test_partial_batch_rounds_up(self):
        # need 50, have 5 -> deficit 45, yield 10 -> 4.5 batches -> 5
        self.assertEqual(5, self._call(on_hand=5.0))

    def test_sufficient_stock_suggests_nothing(self):
        self.assertEqual(0, self._call(on_hand=50.0))
        self.assertEqual(0, self._call(on_hand=999.0))

    def test_zero_bom_yield_is_not_a_division_error(self):
        self.assertEqual(0, self._call(bom_yield=0))
        self.assertEqual(0, self._call(bom_yield=None))

    def test_season_multiplier_scales_demand(self):
        # 1.8x Ramadan on a 10-day target at velocity 5 -> need 90, yield 10
        self.assertEqual(9, self._call(season_multiplier=1.8))
        # a slow season pulls the suggestion down
        self.assertEqual(5, self._call(season_multiplier=0.9, bom_yield=9.0))

    def test_zero_velocity_suggests_nothing(self):
        self.assertEqual(0, self._call(velocity=0))

    def test_negative_stock_does_not_inflate_the_suggestion(self):
        # Subtracting a negative would add the phantom hole to real demand.
        # Staging had Redvelvet Medium at -36 asking for 51 batches where seven
        # days of actual sales needs 15.
        with_hole = self._call(on_hand=-100.0)
        empty = self._call(on_hand=0.0)
        self.assertEqual(empty, with_hole)
        self.assertEqual(5, with_hole)

    def test_float_noise_does_not_add_a_phantom_batch(self):
        # A 0.7 kg batch BOM selling 2.1/day on a 1-day target: the deficit is
        # 2.1 and 2.1 / 0.7 lands on 3.0000000000000004 in float, so a naive
        # ceil() asks for a 4th batch nobody needs.  This is the epsilon's job.
        self.assertEqual(3, self._call(target_days=1, velocity=2.1, on_hand=0.0, bom_yield=0.7))
        # ...and the same shape at a different scale
        self.assertEqual(7, self._call(target_days=1, velocity=2.1, on_hand=0.0, bom_yield=0.3))


class TestDaysOfCover(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.production_planning import days_of_cover

        params = {"on_hand": 100.0, "velocity": 10.0, "season_multiplier": 1.0}
        params.update(kwargs)
        return days_of_cover(**params)

    def test_basic_cover(self):
        self.assertAlmostEqual(10.0, self._call())

    def test_season_shortens_cover(self):
        self.assertAlmostEqual(5.0, self._call(season_multiplier=2.0))

    def test_zero_velocity_returns_none_not_a_sentinel(self):
        # The stored jarz_days_of_stock field writes 999 here, which makes
        # "never sold" indistinguishable from "enormous pile".
        self.assertIsNone(self._call(velocity=0))
        self.assertIsNone(self._call(velocity=None))

    def test_zero_stock_covers_nothing(self):
        self.assertEqual(0.0, self._call(on_hand=0))

    def test_negative_stock_covers_zero_days_not_negative_days(self):
        # ERPNext permits negative Bin quantities and staging has several.
        # "-17 days of cover" means nothing to somebody deciding what to make;
        # the on-hand figure shown beside it carries the hole.
        self.assertEqual(0.0, self._call(on_hand=-137.0))
        self.assertEqual(0.0, self._call(on_hand=-0.5))

    def test_a_negative_stock_item_still_reads_as_critical(self):
        from jarz_pos.services.production_planning import status_for_days_of_cover

        days = self._call(on_hand=-137.0)
        status = status_for_days_of_cover(
            days, critical_days=5, watch_days=14, overstock_days=90
        )
        self.assertEqual("critical", status)


class TestStatusForDaysOfCover(unittest.TestCase):
    THRESHOLDS = {"critical_days": 5, "watch_days": 14, "overstock_days": 90}

    def _call(self, days):
        from jarz_pos.services.production_planning import status_for_days_of_cover

        return status_for_days_of_cover(days, **self.THRESHOLDS)

    def test_none_is_no_velocity(self):
        self.assertEqual("no_velocity", self._call(None))

    def test_boundaries_are_inclusive_on_the_worse_side(self):
        self.assertEqual("critical", self._call(5))
        self.assertEqual("low", self._call(5.01))
        self.assertEqual("low", self._call(14))
        self.assertEqual("ok", self._call(14.01))
        self.assertEqual("ok", self._call(90))
        self.assertEqual("overstocked", self._call(90.01))

    def test_typical_values(self):
        self.assertEqual("critical", self._call(0))
        self.assertEqual("critical", self._call(2))
        self.assertEqual("low", self._call(8))
        self.assertEqual("ok", self._call(30))
        self.assertEqual("overstocked", self._call(400))


class TestCanMakeNow(unittest.TestCase):
    @staticmethod
    def _component(code, required, available, warehouse="Raw Material - J"):
        return {
            "item_code": code,
            "item_name": code,
            "uom": "Kg",
            "required_qty": required,
            "available_qty": available,
            "source_warehouse": warehouse,
        }

    def _call(self, components):
        from jarz_pos.services.production_planning import can_make_now

        return can_make_now(components)

    def test_scarcest_component_caps_the_batch_count(self):
        count, limiting = self._call(
            [
                self._component("FLOUR", required=2.0, available=100.0),  # 50 batches
                self._component("NUTS", required=1.0, available=7.0),  # 7 batches  <- cap
                self._component("SUGAR", required=0.5, available=40.0),  # 80 batches
            ]
        )
        self.assertEqual(7, count)
        self.assertEqual("NUTS", limiting["item_code"])
        self.assertEqual("insufficient_stock", limiting["reason"])

    def test_partial_batches_are_floored(self):
        count, _ = self._call([self._component("FLOUR", required=3.0, available=8.0)])
        self.assertEqual(2, count)

    def test_zero_required_components_place_no_constraint(self):
        count, limiting = self._call(
            [
                self._component("PACKAGING", required=0.0, available=0.0),
                self._component("FLOUR", required=1.0, available=4.0),
            ]
        )
        self.assertEqual(4, count)
        self.assertEqual("FLOUR", limiting["item_code"])

    def test_missing_source_warehouse_blocks_outright(self):
        count, limiting = self._call(
            [
                self._component("FLOUR", required=1.0, available=1000.0),
                self._component("NUTS", required=1.0, available=1000.0, warehouse=None),
            ]
        )
        self.assertEqual(0, count)
        self.assertEqual("missing_source_warehouse", limiting["reason"])
        self.assertEqual("NUTS", limiting["item_code"])

    def test_no_consuming_components_is_unbounded_not_zero(self):
        self.assertEqual((None, None), self._call([]))
        self.assertEqual((None, None), self._call([self._component("X", 0, 0)]))

    def test_no_stock_yields_zero_batches(self):
        count, limiting = self._call([self._component("FLOUR", required=1.0, available=0.0)])
        self.assertEqual(0, count)
        self.assertEqual("FLOUR", limiting["item_code"])


class TestAggregateBasketMaterials(unittest.TestCase):
    @staticmethod
    def _line(index, item_code, components):
        return {"line_index": index, "item_code": item_code, "components": components}

    @staticmethod
    def _component(code, required, available, warehouse="Raw Material - J"):
        return {
            "item_code": code,
            "item_name": code.title(),
            "uom": "Kg",
            "required_qty": required,
            "available_qty": available,
            "source_warehouse": warehouse,
        }

    def _call(self, sets):
        from jarz_pos.services.production_planning import aggregate_basket_materials

        return aggregate_basket_materials(sets)

    def test_shared_material_across_lines_sums_into_one_shortage(self):
        # THE regression test for the per-line shortage hole: each line needs 6
        # of the 10 available, so both pass individually and the pair is short.
        result = self._call(
            [
                self._line(0, "CAKE-A", [self._component("FLOUR", 6.0, 10.0)]),
                self._line(1, "CAKE-B", [self._component("FLOUR", 6.0, 10.0)]),
            ]
        )

        self.assertFalse(result["ok"])
        self.assertEqual(1, len(result["components"]))
        self.assertAlmostEqual(12.0, result["components"][0]["required_qty"])
        self.assertAlmostEqual(10.0, result["components"][0]["available_qty"])
        self.assertEqual(1, len(result["shortages"]))
        self.assertAlmostEqual(2.0, result["shortages"][0]["missing_qty"])
        self.assertEqual("insufficient_stock", result["shortages"][0]["reason"])

    def test_same_material_in_different_warehouses_stays_separate(self):
        result = self._call(
            [
                self._line(0, "CAKE-A", [self._component("FLOUR", 6.0, 10.0, "Store A - J")]),
                self._line(1, "CAKE-B", [self._component("FLOUR", 6.0, 10.0, "Store B - J")]),
            ]
        )

        self.assertTrue(result["ok"])
        self.assertEqual(2, len(result["components"]))
        for row in result["components"]:
            self.assertAlmostEqual(6.0, row["required_qty"])

    def test_contributing_lines_are_tracked(self):
        result = self._call(
            [
                self._line(0, "CAKE-A", [self._component("FLOUR", 4.0, 100.0)]),
                self._line(1, "CAKE-B", [self._component("FLOUR", 3.0, 100.0)]),
            ]
        )

        contributors = result["components"][0]["contributing_lines"]
        self.assertEqual(2, len(contributors))
        self.assertEqual([0, 1], [c["line_index"] for c in contributors])
        self.assertEqual(["CAKE-A", "CAKE-B"], [c["item_code"] for c in contributors])
        self.assertAlmostEqual(4.0, contributors[0]["required_qty"])

    def test_a_feasible_basket_is_ok(self):
        result = self._call(
            [
                self._line(0, "CAKE-A", [self._component("FLOUR", 4.0, 100.0)]),
                self._line(1, "CAKE-B", [self._component("NUTS", 3.0, 100.0)]),
            ]
        )
        self.assertTrue(result["ok"])
        self.assertEqual([], result["shortages"])
        self.assertAlmostEqual(1.0, result["max_feasible_scale"])

    def test_missing_source_warehouse_is_a_shortage(self):
        result = self._call(
            [self._line(0, "CAKE-A", [self._component("FLOUR", 4.0, 100.0, None)])]
        )
        self.assertFalse(result["ok"])
        self.assertEqual("missing_source_warehouse", result["shortages"][0]["reason"])
        self.assertEqual(0.0, result["max_feasible_scale"])

    def test_max_feasible_scale_reports_how_far_the_basket_must_shrink(self):
        # needs 20, has 10 -> the whole basket fits at half size
        result = self._call([self._line(0, "CAKE-A", [self._component("FLOUR", 20.0, 10.0)])])
        self.assertAlmostEqual(0.5, result["max_feasible_scale"])

    def test_empty_basket_is_ok(self):
        result = self._call([])
        self.assertTrue(result["ok"])
        self.assertEqual([], result["components"])
        self.assertIsNone(result["max_feasible_scale"])


class TestResolveTargetDays(unittest.TestCase):
    def _call(self, override, default):
        from jarz_pos.services.production_planning import resolve_target_days

        return resolve_target_days(override, default)

    def test_item_override_wins(self):
        self.assertEqual((3, "item"), self._call(3, 10))

    def test_blank_override_falls_back_to_the_settings_default(self):
        self.assertEqual((10, "default"), self._call(None, 10))
        self.assertEqual((10, "default"), self._call("", 10))

    def test_zero_override_means_unset_not_zero_target(self):
        # A zero target would suppress every suggestion, which is never what an
        # empty Int field is trying to say.
        self.assertEqual((10, "default"), self._call(0, 10))

    def test_empty_settings_falls_back_to_the_module_default(self):
        from jarz_pos.services.production_planning import DEFAULT_TARGET_DAYS

        # The Settings field ships via DocType JSON rather than a fixture, so
        # the existing Single row reads empty until somebody saves it.
        self.assertEqual((DEFAULT_TARGET_DAYS, "fallback"), self._call(None, None))
        self.assertEqual((DEFAULT_TARGET_DAYS, "fallback"), self._call(0, 0))

    def test_garbage_values_do_not_raise(self):
        from jarz_pos.services.production_planning import DEFAULT_TARGET_DAYS

        self.assertEqual((DEFAULT_TARGET_DAYS, "fallback"), self._call("abc", "xyz"))
        self.assertEqual((10, "default"), self._call("abc", 10))


class TestScaleComponents(unittest.TestCase):
    def test_scaling_multiplies_requirements_only(self):
        from jarz_pos.services.production_planning import scale_components

        rows = [{"item_code": "FLOUR", "required_qty": 2.5, "available_qty": 100.0}]
        scaled = scale_components(rows, factor=4)

        self.assertAlmostEqual(10.0, scaled[0]["required_qty"])
        self.assertAlmostEqual(100.0, scaled[0]["available_qty"])
        # the source rows must not be mutated in place
        self.assertAlmostEqual(2.5, rows[0]["required_qty"])


if __name__ == "__main__":
    unittest.main()

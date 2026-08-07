"""Unit tests for the daily production plan maths.

Everything under test is a pure function over plain numbers, so these tests
patch nothing — same contract as ``test_production_planning``.
"""

import unittest


class TestPlanMixerRuns(unittest.TestCase):
    def _call(self, batches, **kwargs):
        from jarz_pos.services.daily_production_plan import plan_mixer_runs

        return plan_mixer_runs(batches, **kwargs)

    def test_five_batches_prefers_the_size_that_mixes_properly(self):
        # The floor's rule: 1.5 is the right quantity, 2 stretches the mixer,
        # 1 does not mix well.  A fewest-runs rule would answer 2+2+1 here —
        # same three runs and the same total, but it schedules the one size
        # that comes out badly for no gain at all.
        result = self._call(5.0)
        self.assertEqual([2.0, 1.5, 1.5], result["runs"])
        self.assertEqual(5.0, result["planned_batches"])
        self.assertEqual(0.0, result["overproduction_batches"])

    def test_exact_cover_by_preferred_runs_beats_fewer_stretched_runs(self):
        # 6.0 is four clean 1.5s.  Fewest-runs would say 2+2+2.
        result = self._call(6.0)
        self.assertEqual([1.5, 1.5, 1.5, 1.5], result["runs"])
        self.assertEqual(0.0, result["overproduction_batches"])
        self.assertEqual(0.0, result["quality_penalty"])

    def test_never_schedules_a_poor_run_when_demand_is_realistic(self):
        value = 0.05
        while value <= 20.0:
            runs = self._call(value)["runs"]
            self.assertNotIn(
                1.0, runs, f"scheduled a 1.0 run at {value} batches: {runs}"
            )
            value = round(value + 0.05, 2)

    def test_a_whole_wasted_batch_outweighs_run_quality(self):
        # 4.0 is two stretched runs exactly, or three preferred runs with half
        # a batch thrown away.  Mix is never stored, so the waste wins.
        self.assertEqual([2.0, 2.0], self._call(4.0)["runs"])

    def test_exact_multiple_does_not_add_a_phantom_run(self):
        # 4.0/2.0 lands on 2.0000000000000004 without the epsilon.
        self.assertEqual(2, self._call(4.0)["run_count"])

    def test_sub_batch_requirement_still_costs_a_whole_run(self):
        result = self._call(0.1)
        self.assertEqual([1.5], result["runs"])
        self.assertAlmostEqual(1.4, result["overproduction_batches"], places=9)

    def test_zero_plans_nothing(self):
        result = self._call(0)
        self.assertEqual([], result["runs"])
        self.assertEqual(0, result["run_count"])
        self.assertFalse(result["capped"])

    def test_no_run_sizes_configured_is_flagged_not_guessed(self):
        result = self._call(3.0, run_quality={})
        self.assertEqual([], result["runs"])
        self.assertTrue(result["capped"])
        self.assertEqual(3.0, result["required_batches"])

    def test_custom_run_quality_is_honoured(self):
        result = self._call(
            4.0, run_quality={1.0: "preferred", 3.0: "poor"}
        )
        self.assertEqual([1.0, 1.0, 1.0, 1.0], result["runs"])

    def test_run_detail_labels_each_run(self):
        detail = self._call(5.0)["run_detail"]
        self.assertEqual(
            [("2", "acceptable"), ("1.5", "preferred"), ("1.5", "preferred")],
            [(f"{d['size']:g}", d["quality"]) for d in detail],
        )

    def test_runs_are_returned_largest_first(self):
        runs = self._call(7.5)["runs"]
        self.assertEqual(sorted(runs, reverse=True), runs)

    def test_never_under_produces(self):
        value = 0.05
        while value <= 20.0:
            result = self._call(value)
            self.assertGreaterEqual(
                result["planned_batches"] + 1e-9, value, f"under-produced at {value}"
            )
            self.assertTrue(
                all(run >= 1.0 for run in result["runs"]), f"sub-batch run at {value}"
            )
            value = round(value + 0.05, 2)


class TestBatchesNeeded(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.daily_production_plan import batches_needed

        return batches_needed(**kwargs)

    def test_divides_by_batch_yield(self):
        self.assertAlmostEqual(2.0, self._call(total_mix_qty=19.036, batch_qty=9.518))

    def test_zero_batch_qty_is_not_a_division_error(self):
        self.assertEqual(0.0, self._call(total_mix_qty=10.0, batch_qty=0))
        self.assertEqual(0.0, self._call(total_mix_qty=10.0, batch_qty=None))

    def test_negative_demand_floors_at_zero(self):
        self.assertEqual(0.0, self._call(total_mix_qty=-5.0, batch_qty=9.518))


class TestAggregateMixDemand(unittest.TestCase):
    def _call(self, lines):
        from jarz_pos.services.daily_production_plan import aggregate_mix_demand

        return aggregate_mix_demand(lines)

    def test_sums_across_flavours(self):
        result = self._call(
            [
                {"item_code": "Blueberry Medium", "planned_qty": 120, "mix_qty_per_unit": 0.079333},
                {"item_code": "Lotus Large", "planned_qty": 77, "mix_qty_per_unit": 0.123650},
            ]
        )
        self.assertAlmostEqual(19.041, result["total_mix_qty"], places=3)
        self.assertEqual(2, len(result["breakdown"]))

    def test_mixless_flavour_is_kept_in_the_breakdown(self):
        # Molten uses no cheesecake mix; omitting it from the breakdown would
        # read as "forgotten" rather than "zero".
        result = self._call(
            [{"item_code": "Molten Medium", "planned_qty": 60, "mix_qty_per_unit": 0}]
        )
        self.assertEqual(0.0, result["total_mix_qty"])
        self.assertEqual(1, len(result["breakdown"]))
        self.assertEqual(0.0, result["breakdown"][0]["mix_qty"])

    def test_negative_quantities_do_not_reduce_demand(self):
        result = self._call(
            [{"item_code": "X", "planned_qty": -50, "mix_qty_per_unit": 0.08}]
        )
        self.assertEqual(0.0, result["total_mix_qty"])

    def test_empty_input(self):
        result = self._call([])
        self.assertEqual(0.0, result["total_mix_qty"])
        self.assertEqual([], result["breakdown"])


class TestJarsPerBatch(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.daily_production_plan import jars_per_batch

        return jars_per_batch(**kwargs)

    def test_matches_the_numbers_the_floor_knows(self):
        # 9.518 Kg per batch is 120 medium or 77 large — the figures the BOMs
        # already encode, which is how the floor sanity-checks a recipe change.
        self.assertAlmostEqual(
            120.0, self._call(batch_qty=9.518, mix_qty_per_unit=0.079333), places=0
        )
        self.assertAlmostEqual(
            77.0, self._call(batch_qty=9.518, mix_qty_per_unit=0.123650), places=0
        )

    def test_mixless_item_returns_none_not_infinity(self):
        self.assertIsNone(self._call(batch_qty=9.518, mix_qty_per_unit=0))


class TestSummariseActuals(unittest.TestCase):
    def _call(self, lines):
        from jarz_pos.services.daily_production_plan import summarise_actuals

        return summarise_actuals(lines)

    def test_uncounted_line_is_not_a_zero(self):
        result = self._call(
            [
                {"item_code": "A", "planned_qty": 100, "actual_qty": 96},
                {"item_code": "B", "planned_qty": 50, "actual_qty": None},
            ]
        )
        self.assertEqual(96.0, result["actual_total"])
        self.assertEqual(1, result["lines_counted"])
        self.assertEqual(1, result["lines_uncounted"])
        self.assertIsNone(result["lines"][1]["variance_qty"])

    def test_counted_zero_is_a_real_failure(self):
        result = self._call([{"item_code": "A", "planned_qty": 100, "actual_qty": 0}])
        self.assertEqual(1, result["lines_counted"])
        self.assertEqual(-100.0, result["lines"][0]["variance_qty"])
        self.assertEqual(-100.0, result["lines"][0]["variance_pct"])

    def test_overproduction_reads_positive(self):
        result = self._call([{"item_code": "A", "planned_qty": 100, "actual_qty": 118}])
        self.assertEqual(18.0, result["variance_qty"])
        self.assertAlmostEqual(18.0, result["variance_pct"])

    def test_zero_planned_does_not_divide(self):
        result = self._call([{"item_code": "A", "planned_qty": 0, "actual_qty": 12}])
        self.assertIsNone(result["lines"][0]["variance_pct"])
        self.assertIsNone(result["variance_pct"])


class TestRealisedYield(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.daily_production_plan import realised_yield

        return realised_yield(**kwargs)

    def test_reports_units_per_batch_actually_obtained(self):
        # 5 batches run, 588 medium counted -> 117.6 per batch against a BOM
        # that claims 120.  That gap is the whole reason for the evening count.
        self.assertAlmostEqual(117.6, self._call(actual_mix_batches=5.0, actual_units=588))

    def test_no_batches_run_returns_none(self):
        self.assertIsNone(self._call(actual_mix_batches=0, actual_units=100))

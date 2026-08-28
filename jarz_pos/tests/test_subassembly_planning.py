"""Unit tests for the Bases (sub-assembly) planning maths.

Everything under test is a pure function over plain numbers and plain dicts, so
these tests patch nothing at all — same contract as ``test_production_planning``
and ``test_daily_production_plan``.
"""

import unittest


class TestBatchConversions(unittest.TestCase):
    def _batches(self, **kwargs):
        from jarz_pos.services.subassembly_planning import batches_from_qty

        return batches_from_qty(**kwargs)

    def _on_hand(self, **kwargs):
        from jarz_pos.services.subassembly_planning import batches_on_hand

        return batches_on_hand(**kwargs)

    def test_whole_batches(self):
        self.assertEqual(3.0, self._batches(qty=120.0, batch_yield=40.0))

    def test_fractional_batches_are_reported_not_rounded_to_a_whole_run(self):
        # "2.5 batches in the freezer" is the honest answer; rounding it to 2 or
        # 3 would misstate the one figure the floor is reading.
        self.assertEqual(2.5, self._batches(qty=100.0, batch_yield=40.0))
        self.assertEqual(3.333, self._batches(qty=10.0, batch_yield=3.0))

    def test_float_noise_does_not_leak_into_the_reported_figure(self):
        # 2.1 / 0.7 lands on 2.9999999999999996 in float.
        self.assertEqual(3.0, self._batches(qty=2.1, batch_yield=0.7))

    def test_missing_bom_yield_is_not_a_division_error(self):
        # A zero-quantity BOM is a setup problem. Substituting a yield of 1
        # would report a 40 Kg mix as "40 batches on hand".
        self.assertEqual(0.0, self._batches(qty=120.0, batch_yield=0))
        self.assertEqual(0.0, self._batches(qty=120.0, batch_yield=None))
        self.assertEqual(0.0, self._batches(qty=120.0, batch_yield=""))
        self.assertEqual(0.0, self._batches(qty=120.0, batch_yield=-5))

    def test_zero_stock_is_zero_batches(self):
        self.assertEqual(0.0, self._on_hand(on_hand=0.0, batch_yield=40.0))

    def test_negative_stock_floors_at_zero_batches(self):
        # ERPNext permits negative Bin quantities and they are almost always a
        # counting lag. "-1.4 batches on hand" is not actionable.
        self.assertEqual(0.0, self._on_hand(on_hand=-56.0, batch_yield=40.0))

    def test_on_hand_batches_and_raw_qty_are_separate_answers(self):
        # The batch figure floors the hole away; the caller still reports the
        # raw quantity beside it so somebody counts the item.
        self.assertEqual(0.0, self._on_hand(on_hand=-56.0, batch_yield=40.0))
        self.assertEqual(1.4, self._on_hand(on_hand=56.0, batch_yield=40.0))


class TestShortfallBatches(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.subassembly_planning import shortfall_batches

        params = {"qty_required": 100.0, "on_hand": 0.0, "batch_yield": 40.0}
        params.update(kwargs)
        return shortfall_batches(**params)

    def test_full_requirement_when_nothing_on_hand(self):
        self.assertEqual(2.5, self._call())

    def test_stock_reduces_the_shortfall(self):
        self.assertEqual(1.5, self._call(on_hand=40.0))

    def test_sufficient_stock_is_zero_not_negative(self):
        self.assertEqual(0.0, self._call(on_hand=100.0))
        self.assertEqual(0.0, self._call(on_hand=500.0))

    def test_negative_stock_does_not_inflate_the_shortfall(self):
        # Subtracting a negative would add the phantom hole on top of real
        # demand — the exact defect the board's suggestion maths already guards.
        self.assertEqual(self._call(on_hand=0.0), self._call(on_hand=-200.0))
        self.assertEqual(2.5, self._call(on_hand=-200.0))

    def test_negative_requirement_is_not_a_shortfall(self):
        self.assertEqual(0.0, self._call(qty_required=-30.0))

    def test_missing_bom_yield_reports_zero_rather_than_raising(self):
        self.assertEqual(0.0, self._call(batch_yield=0))
        self.assertEqual(0.0, self._call(batch_yield=None))


class TestParseRunSizes(unittest.TestCase):
    def _call(self, raw):
        from jarz_pos.services.subassembly_planning import parse_run_sizes

        return parse_run_sizes(raw)

    def test_full_settings_grammar_with_qualities(self):
        self.assertEqual([1.0, 1.5, 2.0], self._call("1:poor, 1.5:preferred, 2:acceptable"))

    def test_bare_sizes_are_accepted(self):
        self.assertEqual([1.0, 2.0], self._call("2, 1"))

    def test_blank_is_not_configured_rather_than_empty(self):
        # None and [] would mean the same thing to a caller and only one of them
        # survives JSON honestly, so this never returns an empty list.
        self.assertIsNone(self._call(None))
        self.assertIsNone(self._call(""))
        self.assertIsNone(self._call("   "))
        self.assertIsNone(self._call(", ,"))

    def test_unparseable_entries_are_dropped_not_guessed(self):
        self.assertEqual([2.0], self._call("abc, 2, :preferred"))
        self.assertIsNone(self._call("abc"))

    def test_non_positive_sizes_are_rejected(self):
        self.assertEqual([2.0], self._call("0, -1, 2"))

    def test_duplicates_collapse(self):
        self.assertEqual([1.5], self._call("1.5, 1.5, 1.50"))


class TestRunSizesForItem(unittest.TestCase):
    def _call(self, item_code, **kwargs):
        from jarz_pos.services.subassembly_planning import run_sizes_for_item

        params = {"mix_item": "Cheesecake Mix", "mix_run_sizes": [1.0, 1.5, 2.0]}
        params.update(kwargs)
        return run_sizes_for_item(item_code, **params)

    def test_mix_item_gets_the_configured_sizes(self):
        self.assertEqual([1.0, 1.5, 2.0], self._call("Cheesecake Mix"))

    def test_every_other_base_is_unconstrained(self):
        self.assertIsNone(self._call("Sponge Cake"))

    def test_mix_item_with_nothing_configured_is_unconstrained(self):
        self.assertIsNone(self._call("Cheesecake Mix", mix_run_sizes=None))
        self.assertIsNone(self._call("Cheesecake Mix", mix_run_sizes=[]))

    def test_blank_item_code(self):
        self.assertIsNone(self._call(""))
        self.assertIsNone(self._call(None))

    def test_a_future_per_item_override_slots_in_without_reshaping(self):
        self.assertEqual(
            [2.0, 4.0],
            self._call("Sponge Cake", overrides={"Sponge Cake": "2, 4"}),
        )
        self.assertEqual(
            [3.0, 6.0],
            self._call("Sponge Cake", overrides={"Sponge Cake": [6, 3]}),
        )

    def test_an_override_wins_over_the_mix_default(self):
        self.assertEqual(
            [5.0],
            self._call("Cheesecake Mix", overrides={"Cheesecake Mix": "5"}),
        )


class TestMatchesRunSize(unittest.TestCase):
    def _call(self, batches, run_sizes):
        from jarz_pos.services.subassembly_planning import matches_run_size

        return matches_run_size(batches, run_sizes)

    def test_exact_match(self):
        self.assertTrue(self._call(1.5, [1.0, 1.5, 2.0]))

    def test_off_grid_batch_count_is_rejected(self):
        self.assertFalse(self._call(1.25, [1.0, 1.5, 2.0]))
        self.assertFalse(self._call(3.0, [1.0, 1.5, 2.0]))

    def test_an_unconfigured_item_is_unconstrained_not_blocked(self):
        # Defaulting this the other way would make every base but the mix
        # un-startable.
        self.assertTrue(self._call(7.0, None))
        self.assertTrue(self._call(7.0, []))

    def test_client_float_round_trip_still_matches(self):
        self.assertTrue(self._call(1.4999999999999998, [1.5]))
        self.assertTrue(self._call("1.5", [1.5]))

    def test_junk_batch_count_does_not_raise(self):
        self.assertFalse(self._call(None, [1.5]))
        self.assertFalse(self._call("abc", [1.5]))


class TestDeriveBaseDemand(unittest.TestCase):
    BASES = ("Sponge Cake", "Fudge Cake", "Cheesecake Mix")

    def _call(self, targets, bom_rows, base_codes=None):
        from jarz_pos.services.subassembly_planning import derive_base_demand

        return derive_base_demand(
            targets, bom_rows, self.BASES if base_codes is None else base_codes
        )

    def _row(self, bom_name, item_code, qty, bom_quantity=1.0):
        return {
            "bom_name": bom_name,
            "item_code": item_code,
            "qty": qty,
            "bom_quantity": bom_quantity,
        }

    def test_single_jar_pulls_its_base_through(self):
        demand = self._call(
            [{"item_code": "Fudge Medium", "qty": 120, "bom_name": "BOM-FUDGE-M"}],
            [self._row("BOM-FUDGE-M", "Fudge Cake", 0.25)],
        )
        self.assertEqual({"Fudge Cake": 30.0}, demand)

    def test_bom_quantity_scales_per_unit_rather_than_per_run(self):
        # A BOM that yields 120 jars and consumes 30 Kg is the same 0.25/jar as
        # a quantity-1 BOM consuming 0.25. Ignoring BOM.quantity would overstate
        # this by 120x.
        demand = self._call(
            [{"item_code": "Fudge Medium", "qty": 120, "bom_name": "BOM-FUDGE-M"}],
            [self._row("BOM-FUDGE-M", "Fudge Cake", 30.0, bom_quantity=120.0)],
        )
        self.assertAlmostEqual(30.0, demand["Fudge Cake"], places=9)

    def test_two_jars_sharing_one_base_accumulate(self):
        demand = self._call(
            [
                {"item_code": "Fudge Medium", "qty": 100, "bom_name": "BOM-FUDGE-M"},
                {"item_code": "Fudge Large", "qty": 50, "bom_name": "BOM-FUDGE-L"},
            ],
            [
                self._row("BOM-FUDGE-M", "Fudge Cake", 0.25),
                self._row("BOM-FUDGE-L", "Fudge Cake", 0.40),
            ],
        )
        self.assertAlmostEqual(45.0, demand["Fudge Cake"], places=9)

    def test_one_jar_pulling_two_bases(self):
        demand = self._call(
            [{"item_code": "Tiramisu", "qty": 100, "bom_name": "BOM-TIRA"}],
            [
                self._row("BOM-TIRA", "Savoiardi", 0.10),
                self._row("BOM-TIRA", "Cheesecake Mix", 0.30),
            ],
            base_codes=("Savoiardi", "Cheesecake Mix"),
        )
        self.assertAlmostEqual(10.0, demand["Savoiardi"], places=9)
        self.assertAlmostEqual(30.0, demand["Cheesecake Mix"], places=9)

    def test_a_base_listed_twice_on_one_bom_accumulates_both_rows(self):
        demand = self._call(
            [{"item_code": "Fudge Medium", "qty": 10, "bom_name": "BOM-FUDGE-M"}],
            [
                self._row("BOM-FUDGE-M", "Fudge Cake", 0.25),
                self._row("BOM-FUDGE-M", "Fudge Cake", 0.10),
            ],
        )
        self.assertAlmostEqual(3.5, demand["Fudge Cake"], places=9)

    def test_raw_materials_are_ignored_only_bases_count(self):
        demand = self._call(
            [{"item_code": "Fudge Medium", "qty": 100, "bom_name": "BOM-FUDGE-M"}],
            [
                self._row("BOM-FUDGE-M", "Cream Cheese", 5.0),
                self._row("BOM-FUDGE-M", "Fudge Cake", 0.25),
            ],
        )
        self.assertEqual({"Fudge Cake": 25.0}, demand)

    def test_empty_demand_when_nothing_is_planned(self):
        self.assertEqual({}, self._call([], [self._row("BOM-FUDGE-M", "Fudge Cake", 0.25)]))
        self.assertEqual({}, self._call(None, None))

    def test_empty_demand_when_no_jar_bom_lists_a_base(self):
        # The signature of a catalogue that predates the sub-assembly migration:
        # the jar BOM still carries flour and cream directly.
        self.assertEqual(
            {},
            self._call(
                [{"item_code": "Fudge Medium", "qty": 100, "bom_name": "BOM-FUDGE-M"}],
                [self._row("BOM-FUDGE-M", "Cream Cheese", 5.0)],
            ),
        )

    def test_no_base_items_means_no_demand(self):
        self.assertEqual(
            {},
            self._call(
                [{"item_code": "Fudge Medium", "qty": 100, "bom_name": "BOM-FUDGE-M"}],
                [self._row("BOM-FUDGE-M", "Fudge Cake", 0.25)],
                base_codes=[],
            ),
        )

    def test_missing_bom_yield_skips_the_bom_rather_than_guessing_one(self):
        # Substituting 1 would multiply the day's demand by the real batch size.
        self.assertEqual(
            {},
            self._call(
                [{"item_code": "Fudge Medium", "qty": 120, "bom_name": "BOM-FUDGE-M"}],
                [self._row("BOM-FUDGE-M", "Fudge Cake", 30.0, bom_quantity=0)],
            ),
        )
        self.assertEqual(
            {},
            self._call(
                [{"item_code": "Fudge Medium", "qty": 120, "bom_name": "BOM-FUDGE-M"}],
                [self._row("BOM-FUDGE-M", "Fudge Cake", 30.0, bom_quantity=None)],
            ),
        )

    def test_a_jar_with_no_bom_contributes_nothing(self):
        self.assertEqual(
            {},
            self._call(
                [{"item_code": "Fudge Medium", "qty": 120, "bom_name": ""}],
                [self._row("BOM-FUDGE-M", "Fudge Cake", 0.25)],
            ),
        )

    def test_a_negative_or_zero_target_never_subtracts_demand(self):
        demand = self._call(
            [
                {"item_code": "Fudge Medium", "qty": 100, "bom_name": "BOM-FUDGE-M"},
                {"item_code": "Fudge Large", "qty": -500, "bom_name": "BOM-FUDGE-L"},
                {"item_code": "Fudge Mini", "qty": 0, "bom_name": "BOM-FUDGE-S"},
            ],
            [
                self._row("BOM-FUDGE-M", "Fudge Cake", 0.25),
                self._row("BOM-FUDGE-L", "Fudge Cake", 0.40),
                self._row("BOM-FUDGE-S", "Fudge Cake", 0.10),
            ],
        )
        self.assertEqual({"Fudge Cake": 25.0}, demand)

    def test_bom_rows_for_an_unplanned_jar_are_ignored(self):
        demand = self._call(
            [{"item_code": "Fudge Medium", "qty": 100, "bom_name": "BOM-FUDGE-M"}],
            [
                self._row("BOM-FUDGE-M", "Fudge Cake", 0.25),
                self._row("BOM-SOMETHING-ELSE", "Sponge Cake", 9.0),
            ],
        )
        self.assertEqual({"Fudge Cake": 25.0}, demand)


class TestBuildDemandBlock(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.subassembly_planning import build_demand_block

        params = {
            "qty_required": 100.0,
            "on_hand": 40.0,
            "batch_yield": 40.0,
            "driver": "today's plan",
        }
        params.update(kwargs)
        return build_demand_block(**params)

    def test_all_three_figures_agree_with_the_conversions(self):
        block = self._call()
        self.assertEqual(100.0, block["qty_required"])
        self.assertEqual(2.5, block["batches_required"])
        self.assertEqual(1.5, block["shortfall_batches"])
        self.assertEqual("today's plan", block["driver"])

    def test_covered_demand_still_reports_the_requirement(self):
        # 0 shortfall is a real answer — "we need it and we have it" — and is
        # not the same as having no demand signal at all, which the caller
        # renders as a blank demand block.
        block = self._call(on_hand=500.0)
        self.assertEqual(100.0, block["qty_required"])
        self.assertEqual(0.0, block["shortfall_batches"])

    def test_missing_bom_yield_zeroes_the_batch_figures_only(self):
        block = self._call(batch_yield=0)
        self.assertEqual(100.0, block["qty_required"])
        self.assertEqual(0.0, block["batches_required"])
        self.assertEqual(0.0, block["shortfall_batches"])


class TestSummariseBases(unittest.TestCase):
    def _call(self, items):
        from jarz_pos.services.subassembly_planning import summarise_bases

        return summarise_bases(items)

    def _item(self, shortfall=None, capacity=None):
        return {
            "demand": None if shortfall is None else {"shortfall_batches": shortfall},
            "can_make_now_batches": capacity,
        }

    def test_empty_screen(self):
        self.assertEqual(
            {"total": 0, "short_of_demand": 0, "blocked_by_materials": 0}, self._call([])
        )
        self.assertEqual(
            {"total": 0, "short_of_demand": 0, "blocked_by_materials": 0}, self._call(None)
        )

    def test_counts_only_a_positive_shortfall(self):
        summary = self._call(
            [self._item(shortfall=2.5), self._item(shortfall=0.0), self._item()]
        )
        self.assertEqual(3, summary["total"])
        self.assertEqual(1, summary["short_of_demand"])

    def test_unknown_capacity_is_not_reported_as_blocked(self):
        # None means capacity was never computed for that item, which is not the
        # same as "the store cannot cover it".
        summary = self._call([self._item(capacity=None), self._item(capacity=0)])
        self.assertEqual(1, summary["blocked_by_materials"])

    def test_available_capacity_is_not_blocked(self):
        summary = self._call([self._item(capacity=4), self._item(capacity=1)])
        self.assertEqual(0, summary["blocked_by_materials"])

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


class TestPickBatchUnit(unittest.TestCase):
    """Which component the floor counts a batch by — or none at all."""

    def _call(self, rows):
        from jarz_pos.services.subassembly_planning import pick_batch_unit

        return pick_batch_unit(rows)

    def _row(self, item_code, qty, uom, item_name=None):
        return {
            "item_code": item_code,
            "item_name": item_name or item_code,
            "qty": qty,
            "uom": uom,
        }

    def test_eggs_beat_the_kilo_components(self):
        # Fudge Cake: the BOM yields 9.258 Kg, the floor says "30 eggs".
        unit = self._call(
            [
                self._row("Flour", 2.5, "Kg"),
                self._row("Sugar", 1.8, "Kg"),
                self._row("Eggs", 30, "piece", item_name="Fresh Eggs"),
            ]
        )
        self.assertEqual("Eggs", unit["item_code"])
        self.assertEqual("Fresh Eggs", unit["item_name"])
        self.assertEqual("piece", unit["uom"])
        self.assertEqual(30.0, unit["qty_per_batch"])

    def test_a_mix_has_no_batch_unit_and_returns_none(self):
        # Blueberry mix is 1 Kg fruit + 1 Kg jelly and is made by the kilo.
        # None must survive to the client as null; an empty dict would render as
        # a batch unit with no name.
        self.assertIsNone(
            self._call([self._row("Blueberry", 1.0, "Kg"), self._row("Jelly", 1.0, "Kg")])
        )
        self.assertIsNone(self._call([]))
        self.assertIsNone(self._call(None))

    def test_volume_units_are_divisible_too(self):
        self.assertIsNone(
            self._call([self._row("Cream", 2.0, "Litre"), self._row("Water", 500, "Millilitre")])
        )

    def test_uom_matching_ignores_case_and_padding(self):
        self.assertIsNone(self._call([self._row("Flour", 2.5, "  KG ")]))

    def test_an_undeclared_unit_reads_as_countable(self):
        # The blacklist is divisible units on purpose: guessing "divisible" for a
        # packaging unit nobody declared is what offers 0.37 of an egg.
        unit = self._call([self._row("Cake Sheet", 4, "Tray")])
        self.assertEqual("Cake Sheet", unit["item_code"])
        self.assertEqual(4.0, unit["qty_per_batch"])

    def test_largest_countable_qty_wins(self):
        unit = self._call(
            [
                self._row("Vanilla Pod", 2, "piece"),
                self._row("Eggs", 30, "piece"),
                self._row("Flour", 900, "Kg"),
            ]
        )
        self.assertEqual("Eggs", unit["item_code"])

    def test_a_tie_resolves_by_item_code_so_two_requests_agree(self):
        rows = [self._row("Zaatar", 12, "piece"), self._row("Almond", 12, "piece")]
        self.assertEqual("Almond", self._call(rows)["item_code"])
        self.assertEqual("Almond", self._call(list(reversed(rows)))["item_code"])

    def test_a_non_positive_qty_never_qualifies(self):
        self.assertIsNone(self._call([self._row("Eggs", 0, "piece")]))
        self.assertIsNone(self._call([self._row("Eggs", -30, "piece")]))
        self.assertIsNone(self._call([self._row("Eggs", None, "piece")]))
        unit = self._call([self._row("Eggs", 0, "piece"), self._row("Vanilla Pod", 2, "piece")])
        self.assertEqual("Vanilla Pod", unit["item_code"])

    def test_a_blank_item_code_or_uom_never_qualifies(self):
        # A blank unit is unknown, not countable.
        self.assertIsNone(self._call([self._row("", 30, "piece")]))
        self.assertIsNone(self._call([self._row("Eggs", 30, "")]))
        self.assertIsNone(self._call([self._row("Eggs", 30, None)]))

    def test_item_name_falls_back_to_the_code(self):
        unit = self._call([{"item_code": "Eggs", "qty": 30, "uom": "piece"}])
        self.assertEqual("Eggs", unit["item_name"])

    def test_the_unit_carries_exactly_the_documented_keys(self):
        self.assertEqual(
            {"item_code", "item_name", "uom", "qty_per_batch"},
            set(self._call([self._row("Eggs", 30, "piece")]).keys()),
        )


class TestQtyForJarCounts(unittest.TestCase):
    """Jar counts in, Kg of base out — the by-the-kilo screen's arithmetic."""

    CONSUMERS = (
        {"item_code": "Blueberry Medium", "qty_per_jar": 0.030},
        {"item_code": "Blueberry Large", "qty_per_jar": 0.040},
    )

    def _call(self, counts, consumers=None):
        from jarz_pos.services.subassembly_planning import qty_for_jar_counts

        return qty_for_jar_counts(counts, self.CONSUMERS if consumers is None else consumers)

    def test_one_jar_size(self):
        self.assertEqual(0.36, self._call({"Blueberry Medium": 12}))

    def test_two_jar_sizes_accumulate(self):
        self.assertEqual(0.56, self._call({"Blueberry Medium": 12, "Blueberry Large": 5}))

    def test_a_count_for_an_item_nobody_consumes_is_ignored_not_guessed(self):
        # The only honest answer for a jar whose BOM does not list this base is
        # that it needs none of it.
        self.assertEqual(0.36, self._call({"Blueberry Medium": 12, "Mango Large": 40}))

    def test_a_negative_count_never_subtracts_another_jars_demand(self):
        self.assertEqual(
            self._call({"Blueberry Medium": 12}),
            self._call({"Blueberry Medium": 12, "Blueberry Large": -500}),
        )

    def test_a_zero_or_blank_count_contributes_nothing(self):
        self.assertEqual(0.0, self._call({"Blueberry Medium": 0}))
        self.assertEqual(0.0, self._call({"Blueberry Medium": None}))
        self.assertEqual(0.0, self._call({"Blueberry Medium": ""}))

    def test_counts_arriving_as_strings_over_http_still_count(self):
        self.assertEqual(0.36, self._call({"Blueberry Medium": "12"}))

    def test_a_consumer_with_no_rate_contributes_nothing(self):
        self.assertEqual(
            0.0,
            self._call(
                {"Blueberry Medium": 12},
                consumers=[{"item_code": "Blueberry Medium", "qty_per_jar": 0}],
            ),
        )

    def test_nothing_typed_is_zero(self):
        self.assertEqual(0.0, self._call({}))
        self.assertEqual(0.0, self._call(None))
        self.assertEqual(0.0, self._call({"Blueberry Medium": 12}, consumers=[]))

    def test_float_noise_does_not_leak_into_the_quantity(self):
        # 3 x 0.030 lands on 0.09000000000000001 in float.
        self.assertEqual(0.09, self._call({"Blueberry Medium": 3}))


class TestJarsFromQty(unittest.TestCase):
    def _call(self, qty, qty_per_jar=0.030):
        from jarz_pos.services.subassembly_planning import jars_from_qty

        return jars_from_qty(qty, qty_per_jar)

    def test_whole_jars(self):
        self.assertEqual(12, self._call(0.36))

    def test_a_part_jar_is_not_a_jar(self):
        # Rounding up would tell the floor it has stock for an order it cannot
        # fill.
        self.assertEqual(19, self._call(0.58))
        self.assertEqual(0, self._call(0.02))

    def test_float_error_never_loses_a_jar(self):
        # 0.3 / 0.1 lands on 2.9999999999999996 and 0.7 / 0.1 on
        # 6.999999999999999; a bare floor reports 2 and 6.
        self.assertEqual(3, self._call(0.3, qty_per_jar=0.1))
        self.assertEqual(7, self._call(0.7, qty_per_jar=0.1))
        self.assertEqual(2, self._call(0.06))
        self.assertEqual(3, self._call(0.09))

    def test_the_relative_epsilon_does_not_invent_a_jar(self):
        self.assertEqual(19, self._call(0.5999))
        self.assertEqual(2, self._call(0.29999, qty_per_jar=0.1))

    def test_a_missing_rate_is_not_a_division_error(self):
        self.assertEqual(0, self._call(0.36, qty_per_jar=0))
        self.assertEqual(0, self._call(0.36, qty_per_jar=None))
        self.assertEqual(0, self._call(0.36, qty_per_jar=-0.03))
        self.assertEqual(0, self._call(0.36, qty_per_jar="abc"))

    def test_negative_or_junk_stock_fills_no_jars(self):
        self.assertEqual(0, self._call(-5.0))
        self.assertEqual(0, self._call(None))
        self.assertEqual(0, self._call("abc"))

    def test_the_result_is_a_whole_int_not_a_float(self):
        self.assertIsInstance(self._call(0.36), int)

    def test_it_round_trips_with_qty_for_jar_counts(self):
        from jarz_pos.services.subassembly_planning import qty_for_jar_counts

        qty = qty_for_jar_counts(
            {"Blueberry Medium": 12}, [{"item_code": "Blueberry Medium", "qty_per_jar": 0.030}]
        )
        self.assertEqual(12, self._call(qty))


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


class TestDeriveBaseConsumptionPerDay(unittest.TestCase):
    """Velocities in, per-day base consumption out — the same BOM walk.

    A base is never sold, so it has no velocity of its own; feeding the jars'
    daily velocity through the demand walk as ``qty`` is what turns "what the
    plan needs" arithmetic into "what the freezer burns per day".
    """

    def _call(self, targets, bom_rows, base_codes=("Butter Biscuit",)):
        from jarz_pos.services.subassembly_planning import derive_base_consumption_per_day

        return derive_base_consumption_per_day(targets, bom_rows, base_codes)

    def _row(self, bom_name, item_code, qty, bom_quantity=1.0):
        return {
            "bom_name": bom_name,
            "item_code": item_code,
            "qty": qty,
            "bom_quantity": bom_quantity,
        }

    def test_one_jar_burns_its_base_at_velocity_times_qty_per_jar(self):
        consumption = self._call(
            [{"item_code": "Lotus Medium", "qty": 8.0, "bom_name": "BOM-LOTUS-M"}],
            [self._row("BOM-LOTUS-M", "Butter Biscuit", 0.06)],
        )
        self.assertAlmostEqual(0.48, consumption["Butter Biscuit"], places=9)

    def test_every_jar_using_the_base_accumulates(self):
        consumption = self._call(
            [
                {"item_code": "Lotus Medium", "qty": 8.0, "bom_name": "BOM-LOTUS-M"},
                {"item_code": "Lotus Large", "qty": 3.0, "bom_name": "BOM-LOTUS-L"},
                # A BOM that yields 20 jars off 0.5 Kg is 0.025 Kg/jar.
                {"item_code": "Biscoff Mini", "qty": 12.0, "bom_name": "BOM-BISCOFF"},
            ],
            [
                self._row("BOM-LOTUS-M", "Butter Biscuit", 0.06),
                self._row("BOM-LOTUS-L", "Butter Biscuit", 0.09),
                self._row("BOM-BISCOFF", "Butter Biscuit", 0.5, bom_quantity=20.0),
            ],
        )
        self.assertAlmostEqual(1.05, consumption["Butter Biscuit"], places=9)

    def test_a_base_no_jar_uses_is_absent_not_zero(self):
        # Absent is what the caller renders as ``null``/``no_velocity``. A zero
        # here would read as "we know this base needs nothing".
        consumption = self._call(
            [{"item_code": "Lotus Medium", "qty": 8.0, "bom_name": "BOM-LOTUS-M"}],
            [self._row("BOM-LOTUS-M", "Butter Biscuit", 0.06)],
            base_codes=("Butter Biscuit", "Mango mix"),
        )
        self.assertIn("Butter Biscuit", consumption)
        self.assertNotIn("Mango mix", consumption)

    def test_a_catalogue_predating_the_sub_assembly_migration_yields_nothing(self):
        # The jar BOM still lists flour and butter directly, so no base appears
        # one level down and every base reports "no signal".
        self.assertEqual(
            {},
            self._call(
                [{"item_code": "Lotus Medium", "qty": 8.0, "bom_name": "BOM-LOTUS-M"}],
                [self._row("BOM-LOTUS-M", "Butter", 0.4)],
            ),
        )

    def test_a_jar_that_never_sells_contributes_nothing(self):
        self.assertEqual(
            {},
            self._call(
                [{"item_code": "Lotus Medium", "qty": 0.0, "bom_name": "BOM-LOTUS-M"}],
                [self._row("BOM-LOTUS-M", "Butter Biscuit", 0.06)],
            ),
        )


class TestCoverDays(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.subassembly_planning import cover_days

        params = {"on_hand": 42.0, "consumption_per_day": 3.0}
        params.update(kwargs)
        return cover_days(**params)

    def test_stock_over_rate(self):
        self.assertEqual(14.0, self._call())

    def test_unknown_consumption_is_none_not_a_huge_number(self):
        # The stored ``jarz_days_of_stock`` field writes 999 here, which makes
        # "never consumed" and "huge pile" indistinguishable downstream.
        self.assertIsNone(self._call(consumption_per_day=None))
        self.assertIsNone(self._call(consumption_per_day=0))
        self.assertIsNone(self._call(consumption_per_day="abc"))

    def test_empty_freezer_covers_zero_days(self):
        self.assertEqual(0.0, self._call(on_hand=0.0))

    def test_negative_stock_never_reports_negative_cover(self):
        # "-17 days of cover" means nothing to somebody deciding what to make.
        self.assertEqual(0.0, self._call(on_hand=-56.0))

    def test_matches_the_jar_boards_answer_for_the_same_situation(self):
        from jarz_pos.services.production_planning import days_of_cover

        jar = days_of_cover(on_hand=42.0, velocity=3.0, season_multiplier=1.0)
        self.assertEqual(jar, self._call())
        self.assertEqual(
            days_of_cover(on_hand=-5.0, velocity=3.0, season_multiplier=1.0),
            self._call(on_hand=-5.0),
        )
        self.assertEqual(
            days_of_cover(on_hand=42.0, velocity=0.0, season_multiplier=1.0),
            self._call(consumption_per_day=0.0),
        )


class TestCoverSuggestedQty(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.subassembly_planning import cover_suggested_qty

        params = {"consumption_per_day": 1.05, "target_days": 14, "on_hand": 4.2}
        params.update(kwargs)
        return cover_suggested_qty(**params)

    def test_fills_the_gap_to_the_target(self):
        # 1.05 x 14 = 14.7 needed, 4.2 in the freezer.
        self.assertEqual(10.5, self._call())

    def test_a_covered_base_is_asked_for_nothing(self):
        self.assertEqual(0.0, self._call(on_hand=14.7))
        self.assertEqual(0.0, self._call(on_hand=500.0))

    def test_negative_on_hand_is_treated_as_zero_never_subtracted(self):
        # THE rule of this feature. Subtracting a -2 Kg counting error would ask
        # for 16.7 Kg — the phantom hole on top of real demand. Production
        # carries 26 negative bins, so this path is live.
        self.assertEqual(self._call(on_hand=0.0), self._call(on_hand=-2.0))
        self.assertEqual(14.7, self._call(on_hand=-2.0))
        self.assertEqual(14.7, self._call(on_hand=-2000.0))

    def test_unknown_consumption_asks_for_nothing(self):
        # With no consumption signal there is no defensible quantity; inventing
        # one from the target alone fills a freezer nobody empties.
        self.assertEqual(0.0, self._call(consumption_per_day=None))
        self.assertEqual(0.0, self._call(consumption_per_day=0.0))

    def test_a_zero_target_suppresses_the_suggestion(self):
        self.assertEqual(0.0, self._call(target_days=0))
        self.assertEqual(0.0, self._call(target_days=None))

    def test_float_noise_does_not_leak_into_the_quantity(self):
        # 35.0 - 12.3 lands on 22.700000000000003.
        self.assertEqual(22.7, self._call(consumption_per_day=2.5, target_days=14, on_hand=12.3))

    def test_a_very_slow_consumer_still_reports_a_real_quantity(self):
        self.assertEqual(0.0056, self._call(consumption_per_day=0.0004, on_hand=0.0))


class TestCoverSuggestedBatches(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.subassembly_planning import cover_suggested_batches

        params = {"suggested_qty": 10.5, "batch_yield": 13.674}
        params.update(kwargs)
        return cover_suggested_batches(**params)

    def test_any_shortfall_is_at_least_one_whole_batch(self):
        # Half a mixer bowl is not a thing anyone can make.
        self.assertEqual(1, self._call())
        self.assertEqual(1, self._call(suggested_qty=0.001))

    def test_rounds_up_to_whole_batches(self):
        self.assertEqual(2, self._call(suggested_qty=14.7))
        self.assertEqual(3, self._call(suggested_qty=28.0))

    def test_an_exact_multiple_does_not_ask_for_one_more(self):
        # 27.348 / 13.674 lands on 2.0000000000000004 in float.
        self.assertEqual(2, self._call(suggested_qty=27.348))
        self.assertEqual(1, self._call(suggested_qty=13.674))

    def test_nothing_to_make_is_zero_batches(self):
        self.assertEqual(0, self._call(suggested_qty=0.0))
        self.assertEqual(0, self._call(suggested_qty=None))

    def test_missing_batch_yield_is_not_a_division_error(self):
        # A zero-yield BOM is a setup problem the caller surfaces; inventing a
        # yield of 1 would ask for 11 batches of a 13.674 Kg mix.
        self.assertEqual(0, self._call(batch_yield=0))
        self.assertEqual(0, self._call(batch_yield=None))
        self.assertEqual(0, self._call(batch_yield=""))
        self.assertEqual(0, self._call(batch_yield=-13.674))

    def test_the_result_is_a_whole_int_not_a_float(self):
        # It goes on a Work Order, unlike the fractional batches_on_hand figure.
        self.assertIsInstance(self._call(), int)


class TestBuildCoverBlock(unittest.TestCase):
    def _call(self, **kwargs):
        from jarz_pos.services.subassembly_planning import build_cover_block

        params = {
            "consumption_per_day": 1.05,
            "on_hand": 4.2,
            "target_days": 14,
            "batch_yield": 13.674,
        }
        params.update(kwargs)
        return build_cover_block(**params)

    def test_butter_biscuit_end_to_end(self):
        # Three jars burn 1.05 Kg/day of Butter Biscuit between them; 4.2 Kg is
        # in the freezer; the target is a fortnight; one batch is 13.674 Kg.
        block = self._call()
        self.assertEqual(1.05, block["consumption_per_day"])
        self.assertEqual(4.0, block["days_of_cover"])  # 4.2 / 1.05
        self.assertEqual(14, block["target_days"])
        self.assertEqual(10.5, block["suggested_qty"])  # 1.05*14 - 4.2
        self.assertEqual(1, block["suggested_batches"])  # 10.5 / 13.674 -> 1

    def test_butter_biscuit_end_to_end_with_a_negative_bin(self):
        # The same base after a miscount. The suggestion must be the full 14
        # days (14.7 Kg -> 2 batches), NOT 16.7 Kg, and cover reads 0 days.
        block = self._call(on_hand=-2.0)
        self.assertEqual(0.0, block["days_of_cover"])
        self.assertEqual(14.7, block["suggested_qty"])
        self.assertEqual(2, block["suggested_batches"])

    def test_a_base_nothing_consumes_is_blank_not_zero(self):
        block = self._call(consumption_per_day=None)
        self.assertIsNone(block["consumption_per_day"])
        self.assertIsNone(block["days_of_cover"])
        # The quantities still report 0: the caller pairs them with a
        # ``no_velocity`` status, which is what carries "we do not know".
        self.assertEqual(0.0, block["suggested_qty"])
        self.assertEqual(0, block["suggested_batches"])
        self.assertEqual(14, block["target_days"])

    def test_a_known_zero_rate_is_reported_as_zero(self):
        block = self._call(consumption_per_day=0.0)
        self.assertEqual(0.0, block["consumption_per_day"])
        self.assertIsNone(block["days_of_cover"])

    def test_zero_batch_yield_still_reports_the_quantity(self):
        # The Kg figure is real and actionable even when nobody can say how many
        # batches it is; only the batch count degrades.
        block = self._call(batch_yield=0)
        self.assertEqual(10.5, block["suggested_qty"])
        self.assertEqual(0, block["suggested_batches"])
        self.assertEqual(4.0, block["days_of_cover"])

    def test_an_item_target_override_drives_the_quantity(self):
        block = self._call(target_days=28)
        self.assertAlmostEqual(25.2, block["suggested_qty"], places=6)
        self.assertEqual(28, block["target_days"])

    def test_junk_target_does_not_raise(self):
        block = self._call(target_days="abc")
        self.assertEqual(0, block["target_days"])
        self.assertEqual(0.0, block["suggested_qty"])

    def test_the_block_carries_exactly_the_documented_keys(self):
        self.assertEqual(
            {
                "consumption_per_day",
                "days_of_cover",
                "target_days",
                "suggested_qty",
                "suggested_batches",
            },
            set(self._call().keys()),
        )


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

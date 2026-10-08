"""Unit tests for SOP work-instruction rendering.

Everything under test is a pure function over plain dicts and numbers, so —
like ``test_production_planning`` — these tests patch nothing at all.  That is
the point of keeping the token substitution out of the API layer.
"""

import unittest

# One BOM batch of a pistachio cake: this is what the author typed the
# instruction against.
QTY = {"PIST-SPR": 1.83, "FLOUR": 0.5, "SUGAR": 0.25}
UOM = {"PIST-SPR": "Kg", "FLOUR": "Kg", "SUGAR": "Kg"}
NAME = {"PIST-SPR": "Pistachio spread", "FLOUR": "Cake flour", "SUGAR": "Caster sugar"}


def render(text, batches=1, **kwargs):
    from jarz_pos.services.sop_rendering import render_instruction

    params = {
        "component_qty_map": QTY,
        "uom_map": UOM,
        "name_map": NAME,
        "batches": batches,
    }
    params.update(kwargs)
    return render_instruction(text, **params)


class TestRenderInstruction(unittest.TestCase):
    def test_full_token_renders_qty_uom_and_name(self):
        out, unresolved = render("Add {{item:PIST-SPR}} to the bowl.")
        self.assertEqual("Add 1.830 Kg Pistachio spread to the bowl.", out)
        self.assertEqual([], unresolved)

    def test_qty_variant_renders_only_the_number(self):
        out, unresolved = render("Weigh {{item:PIST-SPR|qty}} kg.")
        self.assertEqual("Weigh 1.830 kg.", out)
        self.assertEqual([], unresolved)

    def test_name_variant_renders_only_the_name(self):
        out, _ = render("Fetch the {{item:PIST-SPR|name}}.")
        self.assertEqual("Fetch the Pistachio spread.", out)

    def test_uom_variant_renders_only_the_unit(self):
        out, _ = render("Measured in {{item:PIST-SPR|uom}}.")
        self.assertEqual("Measured in Kg.", out)

    def test_unknown_code_is_left_verbatim_and_reported(self):
        # Never silently dropped: "add   of sugar" is a food-safety problem,
        # a visible {{item:NOPE}} is a bug somebody fixes.
        out, unresolved = render("Add {{item:NOPE}} now.")
        self.assertEqual("Add {{item:NOPE}} now.", out)
        self.assertIn("NOPE", unresolved)

    def test_unknown_code_with_a_variant_is_also_reported(self):
        out, unresolved = render("Weigh {{item:NOPE|qty}}.")
        self.assertEqual("Weigh {{item:NOPE|qty}}.", out)
        self.assertIn("NOPE", unresolved)

    def test_unknown_variant_is_left_verbatim_and_reported(self):
        out, unresolved = render("Use {{item:PIST-SPR|weight}}.")
        self.assertEqual("Use {{item:PIST-SPR|weight}}.", out)
        self.assertEqual(["PIST-SPR|weight"], unresolved)

    def test_multiple_tokens_in_one_string(self):
        out, unresolved = render(
            "Mix {{item:FLOUR}} with {{item:SUGAR|qty}} kg and {{item:PIST-SPR|name}}."
        )
        self.assertEqual(
            "Mix 0.500 Kg Cake flour with 0.250 kg and Pistachio spread.",
            out,
        )
        self.assertEqual([], unresolved)

    def test_a_resolved_and_an_unresolved_token_can_coexist(self):
        out, unresolved = render("{{item:FLOUR|qty}} then {{item:GHOST}}")
        self.assertEqual("0.500 then {{item:GHOST}}", out)
        self.assertEqual(["GHOST"], unresolved)

    def test_whitespace_inside_the_token_is_tolerated(self):
        out, unresolved = render("Add {{ item : PIST-SPR }} slowly.")
        self.assertEqual("Add 1.830 Kg Pistachio spread slowly.", out)
        self.assertEqual([], unresolved)

    def test_whitespace_around_the_variant_is_tolerated(self):
        out, _ = render("Weigh {{ item : PIST-SPR | qty }}.")
        self.assertEqual("Weigh 1.830.", out)

    def test_nbsp_is_normalised_before_matching(self):
        # A Text Editor happily emits &nbsp; where the author pressed space.
        out, unresolved = render("Add&nbsp;{{item:PIST-SPR|qty}}&nbsp;kg.")
        self.assertEqual("Add 1.830 kg.", out)
        self.assertEqual([], unresolved)

    def test_nbsp_inside_the_token_still_resolves(self):
        out, unresolved = render("Add {{item:&nbsp;PIST-SPR&nbsp;|qty}}.")
        self.assertEqual("Add 1.830.", out)
        self.assertEqual([], unresolved)

    def test_html_escaped_braces_are_normalised(self):
        # Some editors escape the braces themselves; the token still has to work.
        out, unresolved = render("Add &#123;&#123;item:PIST-SPR|qty&#125;&#125; kg.")
        self.assertEqual("Add 1.830 kg.", out)
        self.assertEqual([], unresolved)

    def test_token_wrapped_in_strong_still_resolves(self):
        out, unresolved = render("<p>Add <strong>{{item:PIST-SPR}}</strong>.</p>")
        self.assertEqual("<p>Add <strong>1.830 Kg Pistachio spread</strong>.</p>", out)
        self.assertEqual([], unresolved)

    def test_markup_inside_the_token_is_stripped_before_lookup(self):
        out, unresolved = render("Add {{item:<strong>PIST-SPR</strong>|qty}}.")
        self.assertEqual("Add 1.830.", out)
        self.assertEqual([], unresolved)

    def test_three_batches_triple_every_quantity(self):
        out, _ = render("{{item:PIST-SPR|qty}} + {{item:FLOUR|qty}}", batches=3)
        self.assertEqual("5.490 + 1.500", out)

    def test_zero_batches_render_zero_not_the_authored_figure(self):
        out, _ = render("{{item:PIST-SPR|qty}}", batches=0)
        self.assertEqual("0.000", out)

    def test_fractional_batches_are_supported(self):
        out, _ = render("{{item:FLOUR|qty}}", batches=0.5)
        self.assertEqual("0.250", out)

    def test_a_blank_batch_count_falls_back_to_one(self):
        out, _ = render("{{item:FLOUR|qty}}", batches=None)
        self.assertEqual("0.500", out)

    def test_code_lookup_is_case_insensitive(self):
        out, unresolved = render("{{item:pist-spr|name}}")
        self.assertEqual("Pistachio spread", out)
        self.assertEqual([], unresolved)

    def test_decimals_are_configurable(self):
        out, _ = render("{{item:PIST-SPR|qty}}", decimals=1)
        self.assertEqual("1.8", out)

    def test_text_without_tokens_is_returned_untouched(self):
        out, unresolved = render("<p>Preheat the oven to 180C.</p>")
        self.assertEqual("<p>Preheat the oven to 180C.</p>", out)
        self.assertEqual([], unresolved)

    def test_empty_text_is_safe(self):
        self.assertEqual(("", []), render(None))
        self.assertEqual(("", []), render(""))

    def test_the_same_unknown_code_is_reported_once(self):
        _, unresolved = render("{{item:GHOST}} and {{item:GHOST|qty}}")
        self.assertEqual(["GHOST"], unresolved)

    def test_a_component_with_no_uom_does_not_leave_a_double_space(self):
        out, _ = render(
            "{{item:WATER}}",
            component_qty_map={"WATER": 2.0},
            uom_map={"WATER": ""},
            name_map={"WATER": "Water"},
        )
        self.assertEqual("2.000 Water", out)


# The Tiramisu Large jar: 8 g of grinds, in Kg because that is the stock UOM.
COFFEE = {
    "component_qty_map": {"COFFEE": 0.008, "SUGAR-G": 7.2, "WATER": 1.5},
    "uom_map": {"COFFEE": "Kg", "SUGAR-G": "Gram", "WATER": "Litre"},
    "name_map": {"COFFEE": "Coffee beans", "SUGAR-G": "Sugar", "WATER": "Water"},
}


class TestGramsVariant(unittest.TestCase):
    def test_kg_is_converted_to_grams_and_scaled_by_batches(self):
        out, unresolved = render("{{item:COFFEE|grams}}", batches=10, **COFFEE)
        self.assertEqual("80 g", out)
        self.assertEqual([], unresolved)

    def test_one_decimal_is_kept_when_it_is_not_zero(self):
        out, _ = render("{{item:COFFEE|grams}}", batches=6.6666667, **COFFEE)
        self.assertEqual("53.3 g", out)

    def test_a_trailing_point_zero_is_dropped_even_for_thousands(self):
        out, _ = render("{{item:COFFEE|grams}}", batches=1122.5, **COFFEE)
        self.assertEqual("8980 g", out)

    def test_float_noise_does_not_leak(self):
        # 0.0072 Kg x 10 x 1000 is 72.00000000000001 in binary floating point.
        out, _ = render(
            "{{item:S|grams}}",
            batches=10,
            component_qty_map={"S": 0.0072},
            uom_map={"S": "Kg"},
            name_map={"S": "Sugar"},
        )
        self.assertEqual("72 g", out)

    def test_gram_uoms_are_not_multiplied_by_a_thousand(self):
        for uom in ("Gram", "Gm", "g", "gram", "G"):
            out, unresolved = render(
                "{{item:X|grams}}",
                component_qty_map={"X": 7.2},
                uom_map={"X": uom},
                name_map={"X": "Sugar"},
                batches=1,
            )
            self.assertEqual("7.2 g", out, uom)
            self.assertEqual([], unresolved)

    def test_kilogram_spellings_are_all_accepted(self):
        for uom in ("Kg", "kg", "KG", "Kilogram", "kilogram"):
            out, _ = render(
                "{{item:X|grams}}",
                component_qty_map={"X": 0.5},
                uom_map={"X": uom},
                name_map={"X": "Flour"},
                batches=1,
            )
            self.assertEqual("500 g", out, uom)

    def test_a_non_mass_uom_is_left_verbatim_and_reported(self):
        out, unresolved = render("Add {{item:WATER|grams}}.", **COFFEE)
        self.assertEqual("Add {{item:WATER|grams}}.", out)
        self.assertEqual(["WATER|grams"], unresolved)

    def test_a_missing_uom_is_left_verbatim_and_reported(self):
        out, unresolved = render(
            "{{item:X|grams}}",
            component_qty_map={"X": 1.0},
            uom_map={"X": ""},
            name_map={"X": "Thing"},
        )
        self.assertEqual("{{item:X|grams}}", out)
        self.assertEqual(["X|grams"], unresolved)

    def test_an_unknown_code_is_reported_by_code(self):
        out, unresolved = render("{{item:GHOST|grams}}")
        self.assertEqual("{{item:GHOST|grams}}", out)
        self.assertEqual(["GHOST"], unresolved)

    def test_the_variant_name_is_case_insensitive(self):
        out, _ = render("{{item:COFFEE|Grams}}", batches=10, **COFFEE)
        self.assertEqual("80 g", out)

    def test_zero_batches_render_zero_grams(self):
        out, _ = render("{{item:COFFEE|grams}}", batches=0, **COFFEE)
        self.assertEqual("0 g", out)


class TestMultiplierSegment(unittest.TestCase):
    def test_grams_multiplier_scales_the_figure(self):
        out, unresolved = render("{{item:COFFEE|grams|x3}}", batches=10, **COFFEE)
        self.assertEqual("240 g", out)
        self.assertEqual([], unresolved)

    def test_a_decimal_multiplier_is_supported(self):
        out, _ = render("{{item:COFFEE|grams|x0.3}}", batches=10, **COFFEE)
        self.assertEqual("24 g", out)
        out, _ = render("{{item:COFFEE|grams|x1.3}}", batches=10, **COFFEE)
        self.assertEqual("104 g", out)

    def test_a_leading_point_multiplier_is_supported(self):
        out, _ = render("{{item:COFFEE|grams|x.5}}", batches=10, **COFFEE)
        self.assertEqual("40 g", out)

    def test_an_uppercase_x_is_accepted(self):
        out, _ = render("{{item:COFFEE|grams|X3}}", batches=10, **COFFEE)
        self.assertEqual("240 g", out)

    def test_qty_accepts_a_multiplier_too(self):
        out, _ = render("{{item:FLOUR|qty|x2}}", batches=1)
        self.assertEqual("1.000", out)

    def test_whitespace_around_segments_is_tolerated(self):
        out, _ = render("{{ item : COFFEE | grams | x3 }}", batches=10, **COFFEE)
        self.assertEqual("240 g", out)

    def test_the_multiplier_never_changes_the_unmultiplied_token(self):
        out, _ = render("{{item:COFFEE|grams}} / {{item:COFFEE|grams|x3}}", batches=10, **COFFEE)
        self.assertEqual("80 g / 240 g", out)

    def test_a_multiplier_on_the_full_form_is_invalid(self):
        # Three segments with an empty middle one.
        out, unresolved = render("{{item:FLOUR||x3}}")
        self.assertEqual("{{item:FLOUR||x3}}", out)
        self.assertEqual(["FLOUR||x3"], unresolved)

    def test_a_multiplier_on_name_or_uom_is_invalid(self):
        for variant in ("name", "uom"):
            token = "{{item:FLOUR|%s|x3}}" % variant
            out, unresolved = render(token)
            self.assertEqual(token, out)
            self.assertEqual(["FLOUR|%s|x3" % variant], unresolved)

    def test_a_bare_multiplier_without_a_variant_is_invalid(self):
        out, unresolved = render("{{item:FLOUR|x3}}")
        self.assertEqual("{{item:FLOUR|x3}}", out)
        self.assertEqual(["FLOUR|x3"], unresolved)

    def test_zero_negative_and_garbage_multipliers_are_invalid(self):
        for segment in ("x0", "x0.0", "x-3", "x", "x3x", "3", "xabc", "x1.2.3", "x 3 4", ""):
            token = "{{item:COFFEE|grams|%s}}" % segment
            out, unresolved = render(token, batches=10, **COFFEE)
            self.assertEqual(token, out, segment)
            self.assertEqual(1, len(unresolved), segment)

    def test_a_fourth_segment_is_invalid(self):
        out, unresolved = render("{{item:COFFEE|grams|x3|x2}}", batches=10, **COFFEE)
        self.assertEqual("{{item:COFFEE|grams|x3|x2}}", out)
        self.assertEqual(1, len(unresolved))

    def test_a_multiplied_non_mass_uom_is_still_refused(self):
        out, unresolved = render("{{item:WATER|grams|x3}}", **COFFEE)
        self.assertEqual("{{item:WATER|grams|x3}}", out)
        self.assertEqual(["WATER|grams|x3"], unresolved)

    def test_existing_two_segment_tokens_are_unchanged(self):
        out, _ = render("{{item:PIST-SPR|qty}} {{item:PIST-SPR|uom}} {{item:PIST-SPR|name}}")
        self.assertEqual("1.830 Kg Pistachio spread", out)


class TestScaleDuration(unittest.TestCase):
    def _call(self, duration=10.0, mode="Fixed", batches=3, units=30):
        from jarz_pos.services.sop_rendering import scale_duration

        return scale_duration(duration, mode, batches, units)

    def test_fixed_is_unchanged(self):
        # Preheating an oven takes as long for one batch as for four.
        self.assertEqual(10.0, self._call(mode="Fixed"))

    def test_per_batch_multiplies_by_batches(self):
        self.assertEqual(30.0, self._call(mode="Per Batch"))

    def test_per_unit_multiplies_by_units(self):
        self.assertEqual(300.0, self._call(mode="Per Unit"))

    def test_zero_batches_collapses_a_per_batch_step(self):
        self.assertEqual(0.0, self._call(mode="Per Batch", batches=0))

    def test_zero_units_collapses_a_per_unit_step(self):
        self.assertEqual(0.0, self._call(mode="Per Unit", units=0))

    def test_fractional_batches_scale_proportionally(self):
        self.assertAlmostEqual(5.0, self._call(mode="Per Batch", batches=0.5))

    def test_an_unknown_mode_is_treated_as_fixed(self):
        # Under-stating a duration is a scheduling annoyance; multiplying by a
        # mode nobody meant is a wrong number on a wall board.
        self.assertEqual(10.0, self._call(mode="Per Moon Phase"))
        self.assertEqual(10.0, self._call(mode=None))
        self.assertEqual(10.0, self._call(mode=""))

    def test_a_missing_duration_is_zero(self):
        self.assertEqual(0.0, self._call(duration=None, mode="Per Batch"))
        self.assertEqual(0.0, self._call(duration="", mode="Fixed"))


class TestRenderSop(unittest.TestCase):
    SOP = {
        "name": "SOP-0001",
        "item_code": "PIST-CAKE",
        "steps": [
            {
                "step_no": 1,
                "title": "Weigh {{item:PIST-SPR|qty}} kg of spread",
                "instruction": "<p>Weigh {{item:PIST-SPR}} into the bowl.</p>",
                "duration_mins": 4,
                "scaling_mode": "Per Batch",
                "requires_confirmation": 1,
                "capture_type": "Number",
                "capture_label": "Weighed (kg)",
                "capture_min": 1.5,
                "capture_max": 2.5,
                "image": "/files/step1.png",
            },
            {
                "step_no": 2,
                "title": "Preheat",
                "instruction": "<p>Preheat to 180C.</p>",
                "duration_mins": 15,
                "scaling_mode": "Fixed",
                "requires_confirmation": 0,
                "capture_type": "None",
            },
            {
                "step_no": 3,
                "title": "Finish",
                "instruction": "<p>Glaze with {{item:GHOST}} and {{item:NOPE|qty}}.</p>",
                "duration_mins": 0.5,
                "scaling_mode": "Per Unit",
            },
        ],
    }

    def _render(self, batches=3, units=30, sop=None):
        from jarz_pos.services.sop_rendering import render_sop

        return render_sop(
            sop or self.SOP,
            batches=batches,
            units=units,
            component_qty_map=QTY,
            uom_map=UOM,
            name_map=NAME,
        )

    def test_total_duration_is_the_sum_of_the_scaled_durations(self):
        result = self._render()
        durations = [s["duration_mins"] for s in result["steps"]]
        # 4 x 3 batches, 15 fixed, 0.5 x 30 units
        self.assertEqual([12.0, 15.0, 15.0], durations)
        self.assertAlmostEqual(sum(durations), result["total_duration_mins"])
        self.assertEqual(42.0, result["total_duration_mins"])

    def test_quantities_are_scaled_by_the_batch_count(self):
        result = self._render(batches=3)
        self.assertIn("5.490 Kg Pistachio spread", result["steps"][0]["instruction_html"])

    def test_titles_are_rendered_too(self):
        result = self._render(batches=2)
        self.assertEqual("Weigh 3.660 kg of spread", result["steps"][0]["title"])

    def test_unresolved_tokens_are_aggregated_across_steps(self):
        result = self._render()
        self.assertEqual(["GHOST", "NOPE"], result["unresolved_tokens"])
        # ...and left verbatim in the step itself.
        self.assertIn("{{item:GHOST}}", result["steps"][2]["instruction_html"])

    def test_capture_configuration_is_carried_through(self):
        step = self._render()["steps"][0]
        self.assertEqual("Number", step["capture_type"])
        self.assertEqual("Weighed (kg)", step["capture_label"])
        self.assertEqual(1.5, step["capture_min"])
        self.assertEqual(2.5, step["capture_max"])
        self.assertTrue(step["requires_confirmation"])
        self.assertEqual("/files/step1.png", step["image_url"])

    def test_defaults_are_applied_to_a_bare_step(self):
        step = self._render()["steps"][2]
        self.assertEqual("None", step["capture_type"])
        self.assertIsNone(step["capture_label"])
        self.assertFalse(step["requires_confirmation"])
        self.assertIsNone(step["image_url"])

    def test_step_numbers_fall_back_to_position_when_missing(self):
        sop = {"steps": [{"title": "A"}, {"title": "B"}]}
        result = self._render(sop=sop)
        self.assertEqual([1, 2], [s["step_no"] for s in result["steps"]])

    def test_an_sop_with_no_steps_is_not_an_error(self):
        result = self._render(sop={"steps": []})
        self.assertEqual([], result["steps"])
        self.assertEqual(0, result["total_duration_mins"])
        self.assertEqual([], result["unresolved_tokens"])

    def test_the_run_size_is_echoed_back(self):
        result = self._render(batches=2.5, units=25)
        self.assertEqual(2.5, result["batches"])
        self.assertEqual(25.0, result["units"])


class TestVersionStamp(unittest.TestCase):
    def test_round_trip(self):
        from jarz_pos.services.sop_rendering import format_version_stamp, parse_version_stamp

        stamp = format_version_stamp("SOP-0007", 3)
        self.assertEqual("SOP-0007#3", stamp)
        self.assertEqual(("SOP-0007", 3), parse_version_stamp(stamp))

    def test_a_blank_stamp_parses_to_nothing(self):
        from jarz_pos.services.sop_rendering import parse_version_stamp

        self.assertEqual((None, None), parse_version_stamp(""))
        self.assertEqual((None, None), parse_version_stamp(None))

    def test_a_stamp_without_a_version_still_yields_the_name(self):
        from jarz_pos.services.sop_rendering import parse_version_stamp

        self.assertEqual(("SOP-0007", None), parse_version_stamp("SOP-0007"))

    def test_a_garbage_version_does_not_raise(self):
        from jarz_pos.services.sop_rendering import parse_version_stamp

        self.assertEqual(("SOP-0007", None), parse_version_stamp("SOP-0007#latest"))


if __name__ == "__main__":
    unittest.main()

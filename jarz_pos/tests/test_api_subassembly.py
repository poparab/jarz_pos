"""Unit tests for the Bases (sub-assembly) API.

Follows the ``test_api_production`` pattern: pure ``unittest.TestCase``, the
module imported lazily inside the test body, and every resolver patched so
nothing reaches a database.

What is deliberately **not** patched is the derivation itself — the jar board
payload and the BOM rows go in, and the real ``_resolve_velocity_targets`` ->
``derive_base_consumption_per_day`` -> ``build_cover_block`` chain produces the
numbers under assertion.  That chain is the feature; stubbing it would test the
stub.
"""

import unittest
from unittest.mock import patch

CONTEXT = {
    "company": "Jarz Co",
    "season": {"name": "Ramadan", "multiplier": 1.8},
    "default_target_days": 14,
    "thresholds": {"critical_days": 5, "watch_days": 14, "overstock_days": 90},
}


def _base(item_code="Butter Biscuit", **overrides):
    """A producible row for a freezer sub-assembly (never a finished jar)."""
    row = {
        "item_code": item_code,
        "item_name": item_code,
        "item_group": "Sub Assemblies",
        "stock_uom": "Kg",
        "default_bom": f"BOM-{item_code}",
        # Butter Biscuit's real batch yield.
        "bom_qty": 13.674,
        "company": "Jarz Co",
        "velocity_30d": 0.0,
        # A base is never sold, which is the whole reason this feature exists.
        "velocity_60d": 0.0,
        "velocity_trend": None,
        "velocity_updated_on": None,
        "target_days_override": None,
    }
    row.update(overrides)
    return row


def _jar(item_code, effective_velocity, *, item_group="Medium", **overrides):
    """One row of the jar board's own payload."""
    row = {
        "item_code": item_code,
        "item_name": item_code,
        "item_group": item_group,
        "default_bom": f"BOM-{item_code}",
        "velocity_60d": effective_velocity,
        "season_multiplier": 1.0,
        "effective_velocity": effective_velocity,
        "suggested_batches": 0,
        "suggested_units": 0.0,
    }
    row.update(overrides)
    return row


def _bom_row(bom_name, item_code, qty, bom_quantity=1.0):
    return {
        "bom_name": bom_name,
        "item_code": item_code,
        "qty": qty,
        "bom_quantity": bom_quantity,
    }


# Three jars burning Butter Biscuit: 8 x 0.06 + 3 x 0.09 + 12 x (0.5/20)
# = 0.48 + 0.27 + 0.30 = 1.05 Kg/day.
JARS = [
    _jar("Lotus Medium", 8.0),
    _jar("Lotus Large", 3.0, item_group="Large"),
    _jar("Biscoff Mini", 12.0),
]
JAR_BOM_ROWS = [
    _bom_row("BOM-Lotus Medium", "Butter Biscuit", 0.06),
    _bom_row("BOM-Lotus Large", "Butter Biscuit", 0.09),
    _bom_row("BOM-Biscoff Mini", "Butter Biscuit", 0.5, bom_quantity=20.0),
    # A raw material on the same jar BOM: it is not a base and must not appear.
    _bom_row("BOM-Lotus Medium", "Butter", 0.4),
]


class TestGetBaseItems(unittest.TestCase):
    def _run(
        self,
        rows,
        on_hand,
        *,
        jars=JARS,
        bom_rows=JAR_BOM_ROWS,
        demand=({}, "none", None),
        context=CONTEXT,
        **kwargs,
    ):
        from jarz_pos.api import subassembly

        with patch(
            "jarz_pos.api.subassembly._ensure_production_view_access"
        ), patch(
            "jarz_pos.api.subassembly.planning._resolve_default_company",
            return_value="Jarz Co",
        ), patch(
            "jarz_pos.api.subassembly.planning.get_planning_context", return_value=context
        ), patch(
            "jarz_pos.api.subassembly._resolve_base_rows", return_value=rows
        ), patch(
            "jarz_pos.api.subassembly.planning._resolve_on_hand_map", return_value=on_hand
        ), patch(
            "jarz_pos.api.subassembly.planning.build_capacity_map", return_value={}
        ), patch(
            "jarz_pos.api.subassembly._resolve_sop_index", return_value={}
        ), patch(
            "jarz_pos.api.subassembly._resolve_mix_item", return_value=""
        ), patch(
            "jarz_pos.api.subassembly._resolve_mix_run_sizes", return_value=None
        ), patch(
            "jarz_pos.api.subassembly._resolve_demand", return_value=demand
        ), patch(
            "jarz_pos.api.subassembly._resolve_jar_board", return_value={"items": jars}
        ) as mock_board, patch(
            "jarz_pos.api.subassembly._resolve_jar_bom_rows", return_value=bom_rows
        ), patch(
            "jarz_pos.api.subassembly.frappe"
        ):
            payload = subassembly.get_base_items(**kwargs)

        return payload, mock_board

    def _item(self, payload, item_code="Butter Biscuit"):
        return next(i for i in payload["items"] if i["item_code"] == item_code)

    # ── Shape ───────────────────────────────────────────────────────────

    def test_requires_production_access(self):
        from jarz_pos.api import subassembly

        with patch(
            "jarz_pos.api.subassembly._ensure_production_view_access",
            side_effect=PermissionError("nope"),
        ), patch("jarz_pos.api.subassembly.frappe"):
            with self.assertRaises(PermissionError):
                subassembly.get_base_items()

    def test_every_item_carries_the_cover_keys(self):
        payload, _ = self._run([_base()], {"Butter Biscuit": 4.2})
        item = self._item(payload)

        for key in (
            "consumption_per_day",
            "days_of_cover",
            "target_days",
            "status",
            "suggested_qty",
            "suggested_batches",
        ):
            self.assertIn(key, item)

    def test_the_existing_payload_shape_is_untouched(self):
        # The Flutter client parses these; the cover fields are additive.
        payload, _ = self._run([_base()], {"Butter Biscuit": 4.2})
        item = self._item(payload)

        for key in (
            "item_code",
            "item_name",
            "item_group",
            "stock_uom",
            "default_bom",
            "batch_yield",
            "on_hand",
            "stock_is_negative",
            "batches_on_hand",
            "can_make_now_batches",
            "limiting_component",
            "run_sizes",
            "has_sop",
            "sop_total_duration_mins",
            "demand",
        ):
            self.assertIn(key, item)

        for key in ("company", "generated_on", "demand_source", "items", "summary"):
            self.assertIn(key, payload)

    # ── The worked example ──────────────────────────────────────────────

    def test_butter_biscuit_end_to_end(self):
        payload, _ = self._run([_base()], {"Butter Biscuit": 4.2})
        item = self._item(payload)

        self.assertAlmostEqual(1.05, item["consumption_per_day"], places=6)
        self.assertEqual(4.0, item["days_of_cover"])  # 4.2 / 1.05
        self.assertEqual(14, item["target_days"])
        self.assertEqual("default", item["target_days_source"])
        self.assertEqual("critical", item["status"])  # 4 days <= critical 5
        self.assertEqual(10.5, item["suggested_qty"])  # 1.05*14 - 4.2
        self.assertEqual(1, item["suggested_batches"])  # 10.5 Kg / 13.674

    def test_a_well_stocked_base_is_asked_for_nothing(self):
        payload, _ = self._run([_base()], {"Butter Biscuit": 21.0})
        item = self._item(payload)

        self.assertEqual(20.0, item["days_of_cover"])
        self.assertEqual("ok", item["status"])
        self.assertEqual(0.0, item["suggested_qty"])
        self.assertEqual(0, item["suggested_batches"])

    def test_a_freezer_full_of_a_base_reads_overstocked(self):
        payload, _ = self._run([_base()], {"Butter Biscuit": 200.0})
        self.assertEqual("overstocked", self._item(payload)["status"])

    # ── The rules ───────────────────────────────────────────────────────

    def test_a_negative_bin_is_floored_never_subtracted(self):
        # Production carries 26 negative bins. Subtracting -2 would ask for
        # 16.7 Kg — the phantom counting hole on top of real consumption.
        payload, _ = self._run([_base()], {"Butter Biscuit": -2.0})
        item = self._item(payload)

        self.assertEqual(-2.0, item["on_hand"])  # the hole stays visible
        self.assertTrue(item["stock_is_negative"])
        self.assertEqual(0.0, item["days_of_cover"])
        self.assertEqual(14.7, item["suggested_qty"])  # 1.05 * 14, no -2
        self.assertEqual(2, item["suggested_batches"])

    def test_a_base_no_jar_uses_reports_no_signal(self):
        payload, _ = self._run(
            [_base(), _base("Mango mix", bom_qty=40.0)],
            {"Butter Biscuit": 4.2, "Mango mix": 0.0},
        )
        item = self._item(payload, "Mango mix")

        self.assertIsNone(item["consumption_per_day"])
        self.assertIsNone(item["days_of_cover"])
        self.assertEqual("no_velocity", item["status"])
        self.assertEqual(0.0, item["suggested_qty"])
        self.assertEqual(0, item["suggested_batches"])
        # The target is still resolved and reported: it is what the base would
        # be planned to the moment a jar starts using it.
        self.assertEqual(14, item["target_days"])

    def test_a_catalogue_predating_the_sub_assembly_migration(self):
        # Jar BOMs still list flour and butter directly, so no base is found one
        # level down. Every base reports "no signal", not a demand of zero.
        payload, _ = self._run(
            [_base()],
            {"Butter Biscuit": 4.2},
            bom_rows=[_bom_row("BOM-Lotus Medium", "Butter", 0.4)],
        )
        item = self._item(payload)

        self.assertIsNone(item["consumption_per_day"])
        self.assertEqual("no_velocity", item["status"])
        self.assertEqual(0, item["suggested_batches"])

    def test_a_zero_yield_bom_still_reports_the_quantity(self):
        payload, _ = self._run([_base(bom_qty=0)], {"Butter Biscuit": 4.2})
        item = self._item(payload)

        self.assertEqual(10.5, item["suggested_qty"])
        self.assertEqual(0, item["suggested_batches"])
        self.assertEqual(4.0, item["days_of_cover"])

    def test_an_item_level_target_override_wins(self):
        payload, _ = self._run(
            [_base(target_days_override=28)], {"Butter Biscuit": 4.2}
        )
        item = self._item(payload)

        self.assertEqual(28, item["target_days"])
        self.assertEqual("item", item["target_days_source"])
        self.assertAlmostEqual(25.2, item["suggested_qty"], places=6)
        self.assertEqual(2, item["suggested_batches"])

    def test_the_settings_default_drives_the_target_for_both_boards(self):
        # One setting, one number: the same resolver the jar board calls.
        payload, _ = self._run(
            [_base()],
            {"Butter Biscuit": 4.2},
            context=dict(CONTEXT, default_target_days=7),
        )

        item = self._item(payload)
        self.assertEqual(7, item["target_days"])
        self.assertAlmostEqual(3.15, item["suggested_qty"], places=6)

    # ── Demand and cover are different questions ────────────────────────

    def test_demand_and_cover_are_both_reported(self):
        payload, _ = self._run(
            [_base()],
            {"Butter Biscuit": 4.2},
            demand=({"Butter Biscuit": 6.0}, "plan", "today's plan"),
        )
        item = self._item(payload)

        # Today's plan needs 6 Kg and 4.2 is in the freezer...
        self.assertEqual(6.0, item["demand"]["qty_required"])
        self.assertEqual("today's plan", item["demand"]["driver"])
        # ...and it is still four days from empty. Neither answer replaces the
        # other, which is why both appear.
        self.assertEqual(4.0, item["days_of_cover"])
        self.assertEqual(10.5, item["suggested_qty"])

    def test_include_cover_zero_blanks_the_cover_and_skips_the_board_read(self):
        payload, mock_board = self._run(
            [_base()], {"Butter Biscuit": 4.2}, include_cover=0
        )
        item = self._item(payload)

        self.assertFalse(payload["cover_included"])
        self.assertIsNone(item["consumption_per_day"])
        self.assertIsNone(item["days_of_cover"])
        mock_board.assert_not_called()

    def test_cover_survives_include_demand_zero(self):
        # The two flags are independent: cover is driven by velocity, not by
        # whatever plan somebody has saved.
        payload, _ = self._run([_base()], {"Butter Biscuit": 4.2}, include_demand=0)
        item = self._item(payload)

        self.assertTrue(payload["cover_included"])
        self.assertAlmostEqual(1.05, item["consumption_per_day"], places=6)
        self.assertIsNone(item["demand"])

    def test_the_header_carries_the_boards_own_legend(self):
        payload, _ = self._run([_base()], {"Butter Biscuit": 4.2})

        self.assertEqual(14, payload["default_target_days"])
        self.assertEqual(CONTEXT["thresholds"], payload["thresholds"])
        self.assertEqual(CONTEXT["season"], payload["season"])


class TestResolveVelocityTargets(unittest.TestCase):
    """The jar board payload -> per-day targets, before the BOM walk."""

    def _call(self, items):
        from jarz_pos.api import subassembly

        with patch(
            "jarz_pos.api.subassembly._resolve_jar_board", return_value={"items": items}
        ):
            return subassembly._resolve_velocity_targets("Jarz Co")

    def test_a_jar_the_board_wants_no_batch_of_still_eats_its_base(self):
        # ``suggested_batches`` is 0 because the jar is well stocked today; it
        # will still consume its base tomorrow. Dropping it would understate the
        # freezer's burn rate.
        targets = self._call([_jar("Lotus Medium", 8.0, suggested_batches=0)])
        self.assertEqual([{"item_code": "Lotus Medium", "qty": 8.0, "bom_name": "BOM-Lotus Medium"}], targets)

    def test_a_jar_that_never_sells_is_dropped(self):
        self.assertEqual([], self._call([_jar("Dead Jar", 0.0)]))

    def test_non_finished_groups_are_dropped(self):
        # A base appearing as a target would double count it against itself.
        self.assertEqual(
            [], self._call([_jar("Butter Biscuit", 5.0, item_group="Sub Assemblies")])
        )

    def test_a_jar_with_no_bom_is_dropped(self):
        self.assertEqual([], self._call([_jar("No BOM Jar", 5.0, default_bom="")]))

    def test_effective_velocity_is_recomputed_when_a_cached_payload_omits_it(self):
        # A payload served from cache by an older build mid-deploy. Degrading to
        # zero here would blank the whole Bases screen for two minutes.
        targets = self._call(
            [
                dict(
                    _jar("Lotus Medium", 5.0, season_multiplier=1.8),
                    effective_velocity=None,
                )
            ]
        )
        self.assertAlmostEqual(9.0, targets[0]["qty"], places=9)

    def test_a_negative_velocity_never_subtracts(self):
        self.assertEqual([], self._call([_jar("Weird Jar", -5.0)]))


class TestResolveJarBoard(unittest.TestCase):
    def test_a_board_failure_degrades_to_no_signal(self):
        # The Bases screen must still render its stock and capacity figures; the
        # cover half reports "unknown" rather than a screen of zeroes.
        from jarz_pos.api import subassembly

        with patch(
            "jarz_pos.api.production.get_production_suggestions",
            side_effect=RuntimeError("boom"),
        ), patch("jarz_pos.api.subassembly._log_failure") as mock_log, patch(
            "jarz_pos.api.subassembly.frappe"
        ):
            self.assertEqual({}, subassembly._resolve_jar_board("Jarz Co"))

        mock_log.assert_called_once()


class TestResolveConsumption(unittest.TestCase):
    def _call(self, base_codes, targets, bom_rows):
        from jarz_pos.api import subassembly

        with patch(
            "jarz_pos.api.subassembly._resolve_velocity_targets", return_value=targets
        ), patch(
            "jarz_pos.api.subassembly._resolve_jar_bom_rows", return_value=bom_rows
        ) as mock_rows:
            result = subassembly._resolve_consumption("Jarz Co", base_codes)
        return result, mock_rows

    def test_no_bases_reads_nothing_at_all(self):
        result, mock_rows = self._call(set(), [], [])
        self.assertEqual({}, result)
        mock_rows.assert_not_called()

    def test_no_selling_jars_reads_no_boms(self):
        result, mock_rows = self._call({"Butter Biscuit"}, [], JAR_BOM_ROWS)
        self.assertEqual({}, result)
        mock_rows.assert_not_called()

    def test_the_derivation_runs_over_the_jar_boms(self):
        result, _ = self._call(
            {"Butter Biscuit"},
            [{"item_code": "Lotus Medium", "qty": 8.0, "bom_name": "BOM-Lotus Medium"}],
            JAR_BOM_ROWS,
        )
        self.assertAlmostEqual(0.48, result["Butter Biscuit"], places=9)

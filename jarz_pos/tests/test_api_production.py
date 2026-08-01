"""Unit tests for the Production Board API.

Follows the ``test_api_manufacturing_precheck`` pattern: pure
``unittest.TestCase``, module imported lazily inside each test body, and the
module-level ``frappe`` symbol patched wholesale so nothing reaches a database.
"""

import unittest
from unittest.mock import patch

def passthrough_translate():
    """Neutralise ``from frappe import _`` for tests that trip ``frappe.throw``.

    ``patch("...production.frappe")`` does not cover that binding, and the real
    translator needs a request context to resolve a language.  Returns a fresh
    patcher each call so nested/reused contexts stay independent.
    """
    return patch("jarz_pos.api.production._", new=lambda msg: msg)

CONTEXT = {
    "company": "Jarz Co",
    "season": {"name": "Ramadan", "multiplier": 1.8},
    "default_target_days": 10,
    "thresholds": {"critical_days": 5, "watch_days": 14, "overstock_days": 90},
}


def _row(item_code="PIST-CAKE", **overrides):
    row = {
        "item_code": item_code,
        "item_name": "Pistachio cake",
        "stock_uom": "Nos",
        "item_group": "Cakes",
        "default_bom": f"BOM-{item_code}",
        "bom_qty": 10.0,
        "company": "Jarz Co",
        "velocity_30d": 4.0,
        "velocity_60d": 5.0,
        "velocity_trend": "Rising",
        "velocity_updated_on": "2026-07-28 03:00:00",
        "target_days_override": None,
    }
    row.update(overrides)
    return row


class TestGetProductionSuggestions(unittest.TestCase):
    def _run(self, rows, on_hand, capacity=None, **kwargs):
        """Drive the endpoint with every resolver stubbed out."""
        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_production_view_access"), patch(
            "jarz_pos.api.production.planning.get_planning_context", return_value=CONTEXT
        ), patch(
            "jarz_pos.api.production.planning._resolve_default_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.production.planning._resolve_producible_rows", return_value=rows
        ), patch(
            "jarz_pos.api.production.planning._resolve_on_hand_map", return_value=on_hand
        ), patch(
            "jarz_pos.api.production.planning.build_capacity_map", return_value=capacity or {}
        ) as mock_capacity, patch(
            "jarz_pos.api.production.planning._resolve_cache"
        ) as mock_cache, patch(
            "jarz_pos.api.production.frappe"
        ) as mock_frappe:
            # A cold cache, so every run recomputes.
            mock_cache.return_value.get_value.return_value = None
            mock_frappe.utils.now_datetime.return_value = "2026-08-01 09:00:00"

            payload = production.get_production_suggestions(**kwargs)

        return payload, mock_capacity, mock_cache

    def test_requires_production_access(self):
        from jarz_pos.api import production

        with patch(
            "jarz_pos.api.production._ensure_production_view_access",
            side_effect=PermissionError("nope"),
        ), patch("jarz_pos.api.production.frappe"):
            with self.assertRaises(PermissionError):
                production.get_production_suggestions()

    def test_returns_the_documented_shape(self):
        payload, _, _ = self._run([_row()], {"PIST-CAKE": 20.0})

        for key in (
            "generated_on",
            "company",
            "season",
            "default_target_days",
            "thresholds",
            "velocity_updated_on",
            "items",
            "summary",
        ):
            self.assertIn(key, payload)

        item = payload["items"][0]
        for key in (
            "item_code",
            "on_hand",
            "velocity_60d",
            "season_multiplier",
            "effective_velocity",
            "target_days",
            "target_days_source",
            "days_of_cover",
            "status",
            "suggested_batches",
            "suggested_units",
            "can_make_now_batches",
            "limiting_component",
        ):
            self.assertIn(key, item)

    def test_suggestion_uses_live_season_not_the_stored_days_of_stock(self):
        # velocity 5 x season 1.8 = 9/day.  20 on hand is 2.22 days of cover,
        # and a 10-day target needs 90 - 20 = 70 more, i.e. 7 batches of 10.
        payload, _, _ = self._run([_row()], {"PIST-CAKE": 20.0})

        item = payload["items"][0]
        self.assertAlmostEqual(9.0, item["effective_velocity"])
        self.assertAlmostEqual(20.0 / 9.0, item["days_of_cover"])
        self.assertEqual("critical", item["status"])
        self.assertEqual(7, item["suggested_batches"])
        self.assertAlmostEqual(70.0, item["suggested_units"])
        self.assertEqual(1.8, item["season_multiplier"])
        self.assertEqual("Ramadan", payload["season"]["name"])

    def test_item_override_beats_the_settings_default(self):
        payload, _, _ = self._run(
            [_row(target_days_override=2)],
            {"PIST-CAKE": 0.0},
        )
        item = payload["items"][0]
        self.assertEqual(2, item["target_days"])
        self.assertEqual("item", item["target_days_source"])
        # 2 days x 9/day = 18, yield 10 -> 2 batches
        self.assertEqual(2, item["suggested_batches"])

    def test_zero_velocity_item_reports_no_velocity_and_sorts_last(self):
        payload, _, _ = self._run(
            [_row("DEAD-SKU", velocity_60d=0.0), _row("LIVE-SKU")],
            {"DEAD-SKU": 500.0, "LIVE-SKU": 1.0},
        )

        codes = [i["item_code"] for i in payload["items"]]
        self.assertEqual(["LIVE-SKU", "DEAD-SKU"], codes)

        dead = payload["items"][1]
        self.assertIsNone(dead["days_of_cover"])
        self.assertEqual("no_velocity", dead["status"])
        self.assertEqual(0, dead["suggested_batches"])

    def test_items_are_ranked_by_urgency(self):
        payload, _, _ = self._run(
            [_row("COMFY"), _row("URGENT"), _row("MIDDLING")],
            {"COMFY": 900.0, "URGENT": 1.0, "MIDDLING": 90.0},
        )
        self.assertEqual(
            ["URGENT", "MIDDLING", "COMFY"],
            [i["item_code"] for i in payload["items"]],
        )

    def test_capacity_is_attached_when_requested(self):
        capacity = {
            "PIST-CAKE": {
                "can_make_now_batches": 3,
                "limiting_component": {"item_code": "PIST-SPR", "reason": "insufficient_stock"},
            }
        }
        payload, mock_capacity, _ = self._run([_row()], {"PIST-CAKE": 0.0}, capacity=capacity)

        mock_capacity.assert_called_once()
        item = payload["items"][0]
        self.assertEqual(3, item["can_make_now_batches"])
        self.assertEqual("PIST-SPR", item["limiting_component"]["item_code"])

    def test_include_capacity_zero_skips_the_bom_explosion_entirely(self):
        # Guards the perf contract: this is the escape hatch if the board ever
        # gets slow, and it is worthless if the explosion still runs.
        payload, mock_capacity, _ = self._run(
            [_row()], {"PIST-CAKE": 0.0}, include_capacity=0
        )

        mock_capacity.assert_not_called()
        self.assertIsNone(payload["items"][0]["can_make_now_batches"])
        self.assertFalse(payload["capacity_included"])

    def test_include_capacity_accepts_the_string_form_http_sends(self):
        _, mock_capacity, _ = self._run([_row()], {"PIST-CAKE": 0.0}, include_capacity="0")
        mock_capacity.assert_not_called()

    def test_summary_counts_statuses_and_material_caps(self):
        capacity = {
            "PIST-CAKE": {"can_make_now_batches": 1, "limiting_component": {"item_code": "X"}},
        }
        payload, _, _ = self._run(
            [_row("PIST-CAKE"), _row("COMFY")],
            {"PIST-CAKE": 0.0, "COMFY": 900.0},
            capacity=capacity,
        )

        summary = payload["summary"]
        self.assertEqual(1, summary["critical"])
        self.assertEqual(1, summary["overstocked"])
        self.assertIn("total_suggested_batches", summary)
        # PIST-CAKE wants 9 batches but materials only allow 1
        self.assertEqual(1, summary["capped_by_materials"])

    def test_status_filter_narrows_the_returned_items(self):
        payload, _, _ = self._run(
            [_row("PIST-CAKE"), _row("COMFY")],
            {"PIST-CAKE": 0.0, "COMFY": 900.0},
            status="critical",
        )
        self.assertEqual(["PIST-CAKE"], [i["item_code"] for i in payload["items"]])
        # the summary still describes the whole board, not the filtered slice
        self.assertEqual(1, payload["summary"]["overstocked"])

    def test_cached_payload_short_circuits_recomputation(self):
        from jarz_pos.api import production

        cached = {"items": [], "summary": {}, "company": "Jarz Co"}
        with patch("jarz_pos.api.production._ensure_production_view_access"), patch(
            "jarz_pos.api.production.planning._resolve_default_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.production.planning.get_planning_context"
        ) as mock_context, patch(
            "jarz_pos.api.production.planning._resolve_cache"
        ) as mock_cache, patch("jarz_pos.api.production.frappe"):
            mock_cache.return_value.get_value.return_value = cached

            payload = production.get_production_suggestions()

        self.assertIs(cached, payload)
        mock_context.assert_not_called()

    def test_force_refresh_bypasses_the_cache(self):
        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_production_view_access"), patch(
            "jarz_pos.api.production.planning.get_planning_context", return_value=CONTEXT
        ), patch(
            "jarz_pos.api.production.planning._resolve_default_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.production.planning._resolve_producible_rows", return_value=[]
        ), patch(
            "jarz_pos.api.production.planning._resolve_on_hand_map", return_value={}
        ), patch(
            "jarz_pos.api.production.planning.build_capacity_map", return_value={}
        ), patch(
            "jarz_pos.api.production.planning._resolve_cache"
        ) as mock_cache, patch("jarz_pos.api.production.frappe"):
            mock_cache.return_value.get_value.return_value = {"items": ["stale"]}

            payload = production.get_production_suggestions(force_refresh=1)

        mock_cache.return_value.get_value.assert_not_called()
        self.assertEqual([], payload["items"])

    def test_a_dead_cache_degrades_to_slow_not_broken(self):
        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_production_view_access"), patch(
            "jarz_pos.api.production.planning.get_planning_context", return_value=CONTEXT
        ), patch(
            "jarz_pos.api.production.planning._resolve_default_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.production.planning._resolve_producible_rows", return_value=[_row()]
        ), patch(
            "jarz_pos.api.production.planning._resolve_on_hand_map", return_value={"PIST-CAKE": 0.0}
        ), patch(
            "jarz_pos.api.production.planning.build_capacity_map", return_value={}
        ), patch(
            "jarz_pos.api.production.planning._resolve_cache", side_effect=Exception("redis down")
        ), patch("jarz_pos.api.production.frappe"):
            payload = production.get_production_suggestions()

        self.assertEqual(1, len(payload["items"]))


class TestGetBasketMaterialRollup(unittest.TestCase):
    LINES = [
        {"item_code": "CAKE-A", "bom_name": "BOM-A", "item_qty": 10},
        {"item_code": "CAKE-B", "bom_name": "BOM-B", "item_qty": 10},
    ]

    def test_shared_material_across_lines_is_reported_short(self):
        from jarz_pos.api import production

        rollup = {
            "ok": False,
            "components": [],
            "shortages": [{"item_code": "FLOUR", "missing_qty": 2.0}],
            "max_feasible_scale": 0.8,
        }

        with patch("jarz_pos.api.production._ensure_production_view_access"), patch(
            "jarz_pos.api.production.planning._resolve_default_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.production.planning.build_basket_rollup", return_value=rollup
        ) as mock_rollup, patch("jarz_pos.api.production.frappe"):
            result = production.get_basket_material_rollup(self.LINES)

        mock_rollup.assert_called_once_with(self.LINES, "Jarz Co")
        self.assertFalse(result["ok"])
        self.assertEqual(2, result["line_count"])
        self.assertEqual("Jarz Co", result["company"])

    def test_json_encoded_lines_are_accepted(self):
        import json

        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_production_view_access"), patch(
            "jarz_pos.api.production.planning._resolve_default_company", return_value="Jarz Co"
        ), patch(
            "jarz_pos.api.production.planning.build_basket_rollup",
            return_value={"ok": True, "components": [], "shortages": []},
        ) as mock_rollup, patch("jarz_pos.api.production.frappe"):
            production.get_basket_material_rollup(json.dumps(self.LINES))

        self.assertEqual(self.LINES, mock_rollup.call_args.args[0])

    def test_a_line_missing_required_fields_is_rejected(self):
        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_production_view_access"), patch(
            "jarz_pos.api.production.planning.build_basket_rollup"
        ) as mock_rollup, passthrough_translate(), patch(
            "jarz_pos.api.production.frappe"
        ) as mock_frappe:
            mock_frappe.throw.side_effect = ValueError("missing field")

            with self.assertRaises(ValueError):
                production.get_basket_material_rollup([{"item_code": "CAKE-A"}])

        mock_rollup.assert_not_called()

    def test_requires_production_access(self):
        from jarz_pos.api import production

        with patch(
            "jarz_pos.api.production._ensure_production_view_access",
            side_effect=PermissionError("nope"),
        ), patch("jarz_pos.api.production.planning.build_basket_rollup") as mock_rollup, patch(
            "jarz_pos.api.production.frappe"
        ):
            with self.assertRaises(PermissionError):
                production.get_basket_material_rollup(self.LINES)

        mock_rollup.assert_not_called()


class TestSetItemTargetDays(unittest.TestCase):
    def test_manager_gate_is_enforced_before_any_write(self):
        from jarz_pos.api import production

        with patch(
            "jarz_pos.api.production._ensure_manager_access",
            side_effect=PermissionError("managers only"),
        ), patch("jarz_pos.api.production.frappe") as mock_frappe:
            with self.assertRaises(PermissionError):
                production.set_item_target_days("PIST-CAKE", 5)

        mock_frappe.db.set_value.assert_not_called()

    def test_sets_the_override(self):
        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_manager_access"), patch(
            "jarz_pos.api.production.frappe"
        ) as mock_frappe:
            mock_frappe.db.exists.return_value = True

            result = production.set_item_target_days("PIST-CAKE", 5)

        mock_frappe.db.set_value.assert_called_once_with(
            "Item", "PIST-CAKE", "jarz_target_days_of_cover", 5
        )
        self.assertEqual(5, result["target_days"])
        self.assertFalse(result["uses_default"])

    def test_zero_clears_the_override_back_to_the_default(self):
        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_manager_access"), patch(
            "jarz_pos.api.production.frappe"
        ) as mock_frappe:
            mock_frappe.db.exists.return_value = True

            result = production.set_item_target_days("PIST-CAKE", 0)

        mock_frappe.db.set_value.assert_called_once_with(
            "Item", "PIST-CAKE", "jarz_target_days_of_cover", None
        )
        self.assertIsNone(result["target_days"])
        self.assertTrue(result["uses_default"])

    def test_unknown_item_is_rejected(self):
        from jarz_pos.api import production

        with patch("jarz_pos.api.production._ensure_manager_access"), passthrough_translate(), patch(
            "jarz_pos.api.production.frappe"
        ) as mock_frappe:
            mock_frappe.db.exists.return_value = False
            mock_frappe.throw.side_effect = ValueError("not found")

            with self.assertRaises(ValueError):
                production.set_item_target_days("NOPE", 5)

        mock_frappe.db.set_value.assert_not_called()


class TestAccessGates(unittest.TestCase):
    def test_production_view_admits_jarz_manager(self):
        from jarz_pos.api import production

        # The whole reason PRODUCTION_VIEW exists: ROLES.MANUFACTURING does not
        # contain "JARZ Manager", but the mobile screen has always been gated
        # client-side on exactly that role.
        with patch("jarz_pos.api.production.frappe") as mock_frappe:
            mock_frappe.get_roles.return_value = ["JARZ Manager"]
            production._ensure_production_view_access()

        mock_frappe.throw.assert_not_called()

    def test_production_view_admits_manufacturing_manager(self):
        from jarz_pos.api import production

        with patch("jarz_pos.api.production.frappe") as mock_frappe:
            mock_frappe.get_roles.return_value = ["Manufacturing Manager"]
            production._ensure_production_view_access()

        mock_frappe.throw.assert_not_called()

    def test_production_view_rejects_an_unrelated_role(self):
        from jarz_pos.api import production

        with passthrough_translate(), patch("jarz_pos.api.production.frappe") as mock_frappe:
            mock_frappe.get_roles.return_value = ["Sales User"]
            mock_frappe.throw.side_effect = PermissionError("denied")

            with self.assertRaises(PermissionError):
                production._ensure_production_view_access()

    def test_manager_gate_stays_narrower_than_the_view_gate(self):
        from jarz_pos.api import production

        with passthrough_translate(), patch("jarz_pos.api.production.frappe") as mock_frappe:
            mock_frappe.get_roles.return_value = ["JARZ Manager"]
            mock_frappe.throw.side_effect = PermissionError("denied")

            with self.assertRaises(PermissionError):
                production._ensure_manager_access()


if __name__ == "__main__":
    unittest.main()

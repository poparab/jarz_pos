"""Who may run production, when, and up to what value.

Three separate gates, tested separately:

* ``_ensure_production_view_access`` / ``_ensure_production_execute_access`` —
  who may see the board and who may touch it.
* ``_assert_posting_date_allowed`` — *when* a batch may be posted.  The whole
  matrix, because the interesting cases are the combinations.
* ``_assert_batch_value_within_threshold`` — how much material one operator may
  commit in a single go.
"""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from jarz_pos.constants import ROLES

NOW = datetime(2026, 8, 2, 10, 0, 0)
TODAY = NOW.date()

OPERATOR = {ROLES.PRODUCTION_OPERATOR}
MANAGER = {"Manufacturing Manager"}
JARZ_MANAGER = {ROLES.JARZ_MANAGER}
SYSTEM_MANAGER = {"System Manager"}
NOBODY = {"POS User"}


class Thrown(Exception):
    """What a wired-up ``frappe.throw`` raises in these tests."""


def passthrough_translate():
    return patch("jarz_pos.api.manufacturing._", new=lambda msg: msg)


def wire_throw(mock_frappe):
    """``frappe.throw`` must raise, and carry its message.

    ``side_effect = Thrown`` alone would raise the class with no arguments, so
    the message never reaches the assertion.
    """

    def _throw(message, *_args, **_kwargs):
        raise Thrown(str(message))

    mock_frappe.throw.side_effect = _throw
    return mock_frappe


class TestRoleSets(unittest.TestCase):
    def test_operators_can_see_and_run_but_never_backdate(self):
        """The floor role is the whole reason the three sets are separate.

        Backdating is how stock and COGS land in a closed period; the point of
        an operator account is that it cannot choose when a thing happened.
        """
        self.assertIn(ROLES.PRODUCTION_OPERATOR, ROLES.PRODUCTION_VIEW)
        self.assertIn(ROLES.PRODUCTION_OPERATOR, ROLES.PRODUCTION_EXECUTE)
        self.assertNotIn(ROLES.PRODUCTION_OPERATOR, ROLES.PRODUCTION_BACKDATE)

    def test_managers_hold_all_three(self):
        for role in ("System Manager", "Manufacturing Manager", ROLES.JARZ_MANAGER):
            with self.subTest(role=role):
                self.assertIn(role, ROLES.PRODUCTION_VIEW)
                self.assertIn(role, ROLES.PRODUCTION_EXECUTE)
                self.assertIn(role, ROLES.PRODUCTION_BACKDATE)


class TestAccessGates(unittest.TestCase):
    def _call(self, gate, roles):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_user_roles", return_value=set(roles)
        ), passthrough_translate(), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            wire_throw(mock_frappe)
            getattr(manufacturing, gate)()

    def test_execute_gate_admits_operators_and_managers(self):
        for roles in (OPERATOR, MANAGER, JARZ_MANAGER, SYSTEM_MANAGER):
            with self.subTest(roles=sorted(roles)):
                self._call("_ensure_production_execute_access", roles)

    def test_execute_gate_refuses_everyone_else(self):
        for roles in (NOBODY, set()):
            with self.subTest(roles=sorted(roles)):
                with self.assertRaises(Thrown):
                    self._call("_ensure_production_execute_access", roles)

    def test_view_gate_admits_operators(self):
        """An operator who can start a batch must be able to read the board."""
        self._call("_ensure_production_view_access", OPERATOR)

    def test_view_gate_refuses_everyone_else(self):
        with self.assertRaises(Thrown):
            self._call("_ensure_production_view_access", NOBODY)


class TestPostingDateMatrix(unittest.TestCase):
    """operator / manager / system manager × today / yesterday / tomorrow / −10d."""

    def _assert_allowed(self, roles, target, max_days=3):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime", return_value=NOW
        ), patch(
            "jarz_pos.api.manufacturing._resolve_user_roles", return_value=set(roles)
        ), patch(
            "jarz_pos.api.manufacturing._resolve_settings",
            return_value=SimpleNamespace(production_max_backdate_days=max_days),
        ), passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            manufacturing._assert_posting_date_allowed(target)

    def _assert_rejected(self, roles, target, max_days=3):
        with self.assertRaises(Thrown):
            self._assert_allowed(roles, target, max_days=max_days)

    # ── today ───────────────────────────────────────────────────────────
    def test_today_is_allowed_for_everyone_who_got_past_the_execute_gate(self):
        for roles in (OPERATOR, MANAGER, SYSTEM_MANAGER):
            with self.subTest(roles=sorted(roles)):
                self._assert_allowed(roles, NOW)

    # ── tomorrow ────────────────────────────────────────────────────────
    def test_the_future_is_refused_for_absolutely_everyone(self):
        """Stock that has not been made must not exist.

        A System Manager is refused too: the overwhelmingly common cause is a
        wrong tablet clock, not a deliberate act.
        """
        tomorrow = NOW + timedelta(days=1)
        for roles in (OPERATOR, MANAGER, JARZ_MANAGER, SYSTEM_MANAGER):
            with self.subTest(roles=sorted(roles)):
                self._assert_rejected(roles, tomorrow)

    # ── yesterday ───────────────────────────────────────────────────────
    def test_operators_cannot_backdate_even_one_day(self):
        self._assert_rejected(OPERATOR, NOW - timedelta(days=1))

    def test_managers_can_backdate_within_the_configured_window(self):
        for roles in (MANAGER, JARZ_MANAGER, SYSTEM_MANAGER):
            with self.subTest(roles=sorted(roles)):
                self._assert_allowed(roles, NOW - timedelta(days=1))

    def test_the_window_boundary_is_inclusive(self):
        self._assert_allowed(MANAGER, NOW - timedelta(days=3), max_days=3)
        self._assert_rejected(MANAGER, NOW - timedelta(days=4), max_days=3)

    # ── ten days back ───────────────────────────────────────────────────
    def test_beyond_the_window_only_a_system_manager_may_go(self):
        """That range reaches a closed accounting period."""
        ten_days_back = NOW - timedelta(days=10)

        self._assert_rejected(OPERATOR, ten_days_back)
        self._assert_rejected(MANAGER, ten_days_back)
        self._assert_rejected(JARZ_MANAGER, ten_days_back)
        self._assert_allowed(SYSTEM_MANAGER, ten_days_back)

    # ── clock tampering ─────────────────────────────────────────────────
    def test_today_comes_from_the_server_never_from_the_caller(self):
        """The reason ``_resolve_now_datetime`` exists at all.

        If "today" were read off the request payload, moving a phone's clock
        forward would let an operator post tomorrow's production, and moving it
        back would walk straight through the backdating gate.
        """
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime", return_value=NOW
        ) as mock_now, patch(
            "jarz_pos.api.manufacturing._resolve_user_roles", return_value=set(OPERATOR)
        ), patch(
            "jarz_pos.api.manufacturing._resolve_settings",
            return_value=SimpleNamespace(production_max_backdate_days=3),
        ), passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            with self.assertRaises(Thrown):
                manufacturing._assert_posting_date_allowed(datetime(2026, 12, 25, 9, 0, 0))

        mock_now.assert_called()

    def test_an_unsaved_settings_row_falls_back_to_the_documented_default(self):
        """The Single reads blank until somebody saves it after a migrate."""
        from jarz_pos.api import manufacturing

        with patch("jarz_pos.api.manufacturing._resolve_settings", return_value=None):
            self.assertEqual(
                manufacturing.DEFAULT_MAX_BACKDATE_DAYS,
                manufacturing._setting_int("production_max_backdate_days", manufacturing.DEFAULT_MAX_BACKDATE_DAYS),
            )

        self._assert_allowed(MANAGER, NOW - timedelta(days=3), max_days=None)
        self._assert_rejected(MANAGER, NOW - timedelta(days=4), max_days=None)

    def test_a_plain_date_string_is_accepted(self):
        """Whitelisted args arrive over HTTP as strings, not datetimes."""
        self._assert_allowed(OPERATOR, TODAY.strftime("%Y-%m-%d"))


class TestBatchValueThreshold(unittest.TestCase):
    LINE = {"item_code": "PIST-CAKE", "bom_name": "BOM-PIST-CAKE", "item_qty": 61}

    def _run(self, roles, total_value, limit, components=None):
        from jarz_pos.api import manufacturing

        priced = {
            "components": components
            if components is not None
            else [{"item_code": "FLOUR", "required_qty": 12.0, "estimated_amount": total_value}],
            "total_value": total_value,
        }

        with patch(
            "jarz_pos.api.manufacturing._price_batch_components", return_value=priced
        ) as mock_price, patch(
            "jarz_pos.api.manufacturing._resolve_user_roles", return_value=set(roles)
        ), patch(
            "jarz_pos.api.manufacturing._resolve_settings",
            return_value=SimpleNamespace(production_operator_batch_value_limit=limit),
        ), passthrough_translate(), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            wire_throw(mock_frappe)
            out = manufacturing._assert_batch_value_within_threshold(self.LINE, "Jarz Co")

        return out, mock_price, mock_frappe

    def test_operator_under_the_limit_passes(self):
        out, _, _ = self._run(OPERATOR, total_value=800.0, limit=1000.0)
        self.assertEqual(800.0, out["total_value"])

    def test_operator_over_the_limit_is_stopped(self):
        with self.assertRaises(Thrown):
            self._run(OPERATOR, total_value=1200.0, limit=1000.0)

    def test_the_boundary_itself_passes(self):
        self._run(OPERATOR, total_value=1000.0, limit=1000.0)

    def test_managers_bypass_the_limit_entirely(self):
        """The cap bounds an operator mistake; it is not there to slow a
        manager down, and a manager blocked mid-shift has no way around it."""
        for roles in (MANAGER, SYSTEM_MANAGER):
            with self.subTest(roles=sorted(roles)):
                self._run(roles, total_value=999_999.0, limit=1000.0)

    def test_a_zero_limit_means_no_limit(self):
        self._run(OPERATOR, total_value=999_999.0, limit=0)

    def test_an_unset_limit_means_no_limit(self):
        self._run(OPERATOR, total_value=999_999.0, limit=None)

    def test_a_batch_that_values_at_zero_is_allowed_but_logged(self):
        """Items never purchased carry ``valuation_rate`` 0.

        A whole batch of them values at nothing and sails under any threshold.
        Blocking production over missing master data would be worse — but it
        must not pass quietly, or the cap looks like it is working when it
        cannot bind at all.
        """
        _, _, mock_frappe = self._run(
            OPERATOR,
            total_value=0.0,
            limit=1000.0,
            components=[{"item_code": "FLOUR", "required_qty": 12.0, "estimated_amount": 0.0}],
        )

        mock_frappe.log_error.assert_called_once()
        self.assertIn("valued 0", mock_frappe.log_error.call_args.kwargs["message"])

    def test_a_bom_with_no_components_does_not_trip_the_zero_warning(self):
        _, _, mock_frappe = self._run(OPERATOR, total_value=0.0, limit=1000.0, components=[])

        mock_frappe.log_error.assert_not_called()

    def test_a_precomputed_explosion_is_reused_not_repeated(self):
        """``start_production_batch`` prices the BOM once and passes it in."""
        from jarz_pos.api import manufacturing

        priced = {"components": [], "total_value": 10.0}

        with patch("jarz_pos.api.manufacturing._price_batch_components") as mock_price, patch(
            "jarz_pos.api.manufacturing._resolve_user_roles", return_value=set(OPERATOR)
        ), patch(
            "jarz_pos.api.manufacturing._resolve_settings",
            return_value=SimpleNamespace(production_operator_batch_value_limit=1000.0),
        ), passthrough_translate(), patch("jarz_pos.api.manufacturing.frappe") as mock_frappe:
            wire_throw(mock_frappe)
            out = manufacturing._assert_batch_value_within_threshold(
                self.LINE, "Jarz Co", priced=priced
            )

        mock_price.assert_not_called()
        self.assertIs(priced, out)


class TestPriceBatchComponents(unittest.TestCase):
    def test_values_the_one_level_bom_at_valuation_rates(self):
        from jarz_pos.api import manufacturing

        rows = [
            {
                "item_code": "FLOUR",
                "item_name": "Flour",
                "uom": "Kg",
                "required_qty": 12.0,
                "source_warehouse": "Raw Material - J",
            },
            {
                "item_code": "PIST-SPR",
                "item_name": "Pistachio spread",
                "uom": "Kg",
                "required_qty": 2.0,
                "source_warehouse": "Raw Material - J",
            },
        ]

        with patch(
            "jarz_pos.api.manufacturing._get_required_material_rows", return_value=rows
        ), patch(
            "jarz_pos.api.manufacturing._resolve_valuation_rate", side_effect=[20.0, 350.0]
        ), patch("jarz_pos.api.manufacturing.frappe"):
            priced = manufacturing._price_batch_components(
                {"bom_name": "BOM-PIST-CAKE", "item_qty": 61}, "Jarz Co"
            )

        self.assertAlmostEqual(12 * 20.0 + 2 * 350.0, priced["total_value"])
        self.assertEqual(240.0, priced["components"][0]["estimated_amount"])
        self.assertEqual(350.0, priced["components"][1]["valuation_rate"])

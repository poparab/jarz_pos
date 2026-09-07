"""Moving a short component into the warehouse its recipe draws from.

``transfer_material_for_production`` is reachable by a Production Operator, so
every one of its refusals is load-bearing: they are what keeps it a production
tool rather than a general "move any stock anywhere" endpoint on the widest
role the board has.  The happy-path test therefore asserts the *shape* of the
Stock Entry as much as the fact that one was submitted — a transfer built with
the warehouses the wrong way round would still "succeed".

Also pins ``get_production_policy``, which exists because the app used to
hardcode a backdating window the server did not agree with.
"""

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jarz_pos.constants import ROLES

NOW = datetime(2026, 9, 7, 10, 0, 0)

OPERATOR = {ROLES.PRODUCTION_OPERATOR}
JARZ_MANAGER = {ROLES.JARZ_MANAGER}
SYSTEM_MANAGER = {"System Manager"}
NOBODY = {"POS User"}

COMPANY = "JARZ"
ITEM = "Butter Biscuit"
HOME = "Work In Progress - J"
ELSEWHERE = "Raw Material - J"


class Thrown(Exception):
    """What a wired-up ``frappe.throw`` raises in these tests."""


def _throw(message, *_args, **_kwargs):
    raise Thrown(str(message))


class FakeStockEntry:
    """Just enough Stock Entry to see what the endpoint built."""

    def __init__(self):
        self.doctype = "Stock Entry"
        self.items = []
        self.flags = SimpleNamespace(ignore_permissions=False)
        self.name = "MAT-STE-jarz-2026-99999"
        self.inserted = False
        self.submitted = False
        self.stock_entry_type = None
        self.purpose = None
        self.company = None

    def append(self, field, row):
        assert field == "items"
        self.items.append(dict(row))

    def insert(self):
        self.inserted = True

    def submit(self):
        assert self.inserted, "submitted without inserting"
        self.submitted = True


class MoveTestCase(unittest.TestCase):
    """Shared wiring: a company, a BOM component, and stock in the wrong store."""

    def setUp(self):
        self.entry = FakeStockEntry()
        self.invalidate = MagicMock()
        self.stock = {(ITEM, ELSEWHERE): 4.336, (ITEM, HOME): 0.0}

    def _call(self, *, roles=OPERATOR, demanded=(HOME,), is_component=True, **kwargs):
        from jarz_pos.api import manufacturing

        payload = {"item_code": ITEM, "from_warehouse": ELSEWHERE, "qty": 4.0}
        payload.update(kwargs)

        with patch(
            "jarz_pos.api.manufacturing._resolve_user_roles", return_value=set(roles)
        ), patch(
            "jarz_pos.api.manufacturing._get_default_company", return_value=COMPANY
        ), patch(
            "jarz_pos.api.manufacturing._is_bom_component", return_value=is_component
        ), patch(
            "jarz_pos.api.manufacturing._component_demand_warehouses",
            return_value=list(demanded),
        ), patch(
            "jarz_pos.api.manufacturing._assert_pickable_warehouse", return_value=None
        ), patch(
            "jarz_pos.api.manufacturing._get_live_stock_qty",
            side_effect=lambda item, wh: self.stock.get((item, wh), 0.0),
        ), patch(
            "jarz_pos.api.manufacturing._get_item_stock_uom", return_value="Kg"
        ), patch(
            "jarz_pos.api.manufacturing._resolve_invalidate_suggestions_cache",
            return_value=self.invalidate,
        ), patch(
            "jarz_pos.api.manufacturing._debug_log", return_value=None
        ), patch(
            "jarz_pos.api.manufacturing._", new=lambda msg: msg
        ), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            mock_frappe.throw.side_effect = _throw
            mock_frappe.new_doc.return_value = self.entry
            return manufacturing.transfer_material_for_production(**payload)


class TestMoveHappyPath(MoveTestCase):
    def test_builds_a_material_transfer_from_the_holder_to_the_demand_warehouse(self):
        result = self._call()

        self.assertEqual(self.entry.stock_entry_type, "Material Transfer")
        self.assertEqual(self.entry.purpose, "Material Transfer")
        self.assertEqual(self.entry.company, COMPANY)
        self.assertEqual(len(self.entry.items), 1)

        row = self.entry.items[0]
        # Direction is the whole point: source is where the stock is, target is
        # where the recipe looks for it.  Reversed, this endpoint would empty
        # the warehouse the shortage was measured in.
        self.assertEqual(row["s_warehouse"], ELSEWHERE)
        self.assertEqual(row["t_warehouse"], HOME)
        self.assertEqual(row["item_code"], ITEM)
        self.assertEqual(row["qty"], 4.0)
        self.assertEqual(row["uom"], "Kg")
        self.assertEqual(row["conversion_factor"], 1)

        self.assertTrue(self.entry.submitted)
        self.assertEqual(result["stock_entry"], self.entry.name)
        self.assertEqual(result["to_warehouse"], HOME)
        self.assertEqual(result["from_warehouse"], ELSEWHERE)

    def test_target_defaults_to_the_only_warehouse_the_component_is_drawn_from(self):
        result = self._call(to_warehouse=None)
        self.assertEqual(result["to_warehouse"], HOME)

    def test_busts_the_board_cache(self):
        """Or the operator moves the stock, still reads "Cannot start", and
        moves it again."""
        self._call()
        self.invalidate.assert_called_once_with()

    def test_moving_the_whole_available_quantity_is_allowed(self):
        """The float tolerance exists so "move all" is not off by a hair."""
        self._call(qty=4.336)
        self.assertTrue(self.entry.submitted)

    def test_a_manager_may_use_it_too(self):
        for roles in (JARZ_MANAGER, SYSTEM_MANAGER):
            with self.subTest(roles=sorted(roles)):
                self.setUp()
                self._call(roles=roles)
                self.assertTrue(self.entry.submitted)


class TestMoveRefusals(MoveTestCase):
    def _refused(self, **kwargs):
        with self.assertRaises(Thrown) as ctx:
            self._call(**kwargs)
        self.assertFalse(self.entry.submitted)
        self.invalidate.assert_not_called()
        return str(ctx.exception)

    def test_a_role_without_production_access_is_refused(self):
        self._refused(roles=NOBODY)

    def test_an_item_no_recipe_consumes_is_refused(self):
        """The gate that keeps this from being a general stock-moving tool."""
        message = self._refused(is_component=False)
        self.assertIn(ITEM, message)

    def test_a_component_with_no_demand_warehouse_is_refused(self):
        """A move cannot fix "nobody said where this is kept" — there is no
        destination to move it to, and the message has to say which fix is."""
        message = self._refused(demanded=())
        self.assertIn("default warehouse", message)

    def test_a_target_the_component_is_not_drawn_from_is_refused(self):
        """Otherwise the move succeeds and the board still says "Cannot start"."""
        message = self._refused(to_warehouse="Finished Goods - J")
        self.assertIn("Finished Goods - J", message)

    def test_an_ambiguous_target_must_be_chosen_explicitly(self):
        message = self._refused(
            to_warehouse=None, demanded=(HOME, "Goods In Transit - J")
        )
        self.assertIn("more than one warehouse", message)

    def test_moving_stock_onto_itself_is_refused(self):
        self._refused(from_warehouse=HOME, to_warehouse=HOME)

    def test_more_than_the_source_holds_is_refused(self):
        message = self._refused(qty=10)
        # Both numbers, because "insufficient stock" alone sends somebody to
        # count a shelf they can already see.
        self.assertIn("4.336", message)
        self.assertIn("10", message)

    def test_a_non_positive_quantity_is_refused(self):
        for qty in (0, -1, "abc"):
            with self.subTest(qty=qty):
                self.setUp()
                self._refused(qty=qty)


class TestProductionPolicy(unittest.TestCase):
    def _call(self, *, roles=JARZ_MANAGER, max_days=30):
        from jarz_pos.api import manufacturing

        with patch(
            "jarz_pos.api.manufacturing._resolve_user_roles", return_value=set(roles)
        ), patch(
            "jarz_pos.api.manufacturing._resolve_settings",
            return_value=SimpleNamespace(production_max_backdate_days=max_days),
        ), patch(
            "jarz_pos.api.manufacturing._resolve_now_datetime", return_value=NOW
        ), patch(
            "jarz_pos.api.manufacturing._", new=lambda msg: msg
        ), patch(
            "jarz_pos.api.manufacturing.frappe"
        ) as mock_frappe:
            mock_frappe.throw.side_effect = _throw
            return manufacturing.get_production_policy()

    def test_reports_the_window_the_server_will_actually_accept(self):
        policy = self._call(max_days=30)
        self.assertEqual(policy["max_backdate_days"], 30)
        self.assertTrue(policy["can_backdate"])

    def test_a_stored_zero_stays_zero(self):
        """The bug this endpoint exists to expose, and it must not paper over it.

        Production's Settings record holds 0 because the Single predates the
        field, so the DocType's default of 3 was never written.  Substituting
        the default here would tell the client it may backdate three days while
        ``_assert_posting_date_allowed`` — reading the same 0 — refuses every
        one of them.  Wrong in the same direction as the bug.
        """
        self.assertEqual(self._call(max_days=0)["max_backdate_days"], 0)

    def test_a_negative_setting_floors_at_zero(self):
        self.assertEqual(self._call(max_days=-5)["max_backdate_days"], 0)

    def test_an_operator_may_execute_but_not_backdate(self):
        policy = self._call(roles=OPERATOR)
        self.assertTrue(policy["can_execute"])
        self.assertFalse(policy["can_backdate"])
        self.assertFalse(policy["unlimited_backdate"])

    def test_a_system_manager_is_not_bound_by_the_day_ceiling(self):
        self.assertTrue(self._call(roles=SYSTEM_MANAGER)["unlimited_backdate"])

    def test_reports_the_servers_date_not_the_callers(self):
        """The gate compares against the server clock, so the picker must be
        built from it — a tablet a day fast would otherwise offer "the future"
        as its last selectable day."""
        self.assertEqual(self._call()["server_date"], "2026-09-07")

    def test_a_role_without_board_access_is_refused(self):
        with self.assertRaises(Thrown):
            self._call(roles=NOBODY)


if __name__ == "__main__":
    unittest.main()

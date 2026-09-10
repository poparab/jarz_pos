"""Unit tests for the branch replenishment plan.

The arithmetic under test is pure — no ``frappe``, no site — which is the point
of keeping it in ``services/replenishment_planning``.  The endpoint tests at the
bottom patch the five resolvers in ``api/replenishment`` and assert the payload
shape, so a change to the wire contract fails here rather than on a phone.

Run site-less::

    PYTHONPATH="<stub>;<apps>/jarz_pos" python -m unittest jarz_pos.tests.test_replenishment
"""

import datetime
import sys
import types
import unittest
from unittest import mock


def _install_frappe_utils_stub() -> None:
    """Make ``from frappe.utils import ...`` resolve against the site-less stub.

    The stub ``frappe`` is a plain module, not a package, so Python cannot
    import ``frappe.utils`` as a submodule and every ``from frappe.utils
    import`` in the app chain would fail at import time.  Registering a real
    module object in ``sys.modules`` fixes that for the whole process.

    A no-op under a real bench: the genuine ``frappe`` package has ``__path__``
    and must never be shadowed by this.
    """
    import frappe

    if hasattr(frappe, "__path__"):  # the real package — leave it alone
        return
    if "frappe.utils" in sys.modules:
        return

    def _getdate(value=None):
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if not value:
            return datetime.date.today()
        return datetime.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()

    utils = types.ModuleType("frappe.utils")
    utils.getdate = _getdate
    utils.nowdate = lambda: datetime.date.today().isoformat()
    utils.now_datetime = lambda: datetime.datetime.now()
    utils.add_days = lambda date, days: _getdate(date) + datetime.timedelta(days=days)
    utils.flt = lambda value, precision=None: float(value or 0)
    utils.cint = lambda value: int(float(value or 0))
    utils.cstr = lambda value: "" if value is None else str(value)
    utils.get_datetime = lambda value=None: value
    utils.today = utils.nowdate
    sys.modules["frappe.utils"] = utils
    frappe.utils = utils


_install_frappe_utils_stub()

from jarz_pos.services import replenishment_planning as plan  # noqa: E402


class TestSellsPerDay(unittest.TestCase):
    def test_rate_is_this_branch_own_sales_over_the_window(self):
        # Nasr city's real 30-day figure: 1,466 jars across the catalogue.
        self.assertEqual(10.0, plan.sells_per_day(qty_sold=300, sales_days=30))
        self.assertEqual(5.0, plan.sells_per_day(qty_sold=150, sales_days=30))

    def test_no_sales_history_is_zero_not_an_error(self):
        self.assertEqual(0.0, plan.sells_per_day(qty_sold=0, sales_days=30))
        self.assertEqual(0.0, plan.sells_per_day(qty_sold=None, sales_days=30))

    def test_net_negative_sales_do_not_become_a_negative_rate(self):
        # More returned than sold in the window.  "-0.4 jars/day" is not a
        # demand signal; it would also flip the suggestion arithmetic.
        self.assertEqual(0.0, plan.sells_per_day(qty_sold=-12, sales_days=30))

    def test_zero_window_is_not_a_division_error(self):
        self.assertEqual(0.0, plan.sells_per_day(qty_sold=100, sales_days=0))


class TestSuggestedQty(unittest.TestCase):
    def _call(self, **kwargs):
        params = {"rate": 10.0, "cover_days": 14, "on_hand": 0.0}
        params.update(kwargs)
        return plan.suggested_qty(**params)

    def test_covers_the_target_from_an_empty_shelf(self):
        self.assertEqual(140, self._call())

    def test_existing_stock_is_deducted(self):
        self.assertEqual(100, self._call(on_hand=40))

    def test_a_full_shelf_asks_for_nothing(self):
        self.assertEqual(0, self._call(on_hand=140))
        self.assertEqual(0, self._call(on_hand=500))

    def test_negative_stock_is_floored_and_never_subtracted(self):
        # Nasr city Chocolate Hazelnut Large is at -55 because jars were sold
        # that were never booked in.  Subtracting that would ship 55 phantom
        # jars on top of the 140 the branch actually needs.
        self.assertEqual(140, self._call(on_hand=-55))
        self.assertEqual(self._call(on_hand=0), self._call(on_hand=-55))

    def test_partial_jar_rounds_up(self):
        # 4.5/day over 14 days = 63 exactly; 4.55/day = 63.7 -> 64 jars.
        self.assertEqual(63, self._call(rate=4.5))
        self.assertEqual(64, self._call(rate=4.55))

    def test_float_noise_does_not_add_a_phantom_jar(self):
        # 0.4 * 35 floats to 14.000000000000002; a naive ceil asks for 15.
        self.assertEqual(14, self._call(rate=0.4, cover_days=35))

    def test_no_sales_history_suggests_nothing(self):
        self.assertEqual(0, self._call(rate=0))


class TestDaysOfCover(unittest.TestCase):
    def test_cover_is_stock_over_this_branch_rate(self):
        self.assertEqual(4.0, plan.days_of_cover(on_hand=40, rate=10.0))

    def test_never_sold_here_is_none_not_a_big_number(self):
        # None, never 999: "never sold here" and "huge pile" must stay
        # distinguishable downstream.
        self.assertIsNone(plan.days_of_cover(on_hand=40, rate=0))

    def test_negative_stock_covers_zero_days_not_negative_days(self):
        self.assertEqual(0.0, plan.days_of_cover(on_hand=-55, rate=10.0))


class TestAllocateProportionally(unittest.TestCase):
    def test_everyone_is_filled_when_the_factory_has_enough(self):
        self.assertEqual(
            {"nasr": 140, "dokki": 60},
            plan.allocate_proportionally({"nasr": 140, "dokki": 60}, 500),
        )

    def test_short_supply_is_split_in_proportion_to_demand(self):
        self.assertEqual(
            {"nasr": 70, "dokki": 30},
            plan.allocate_proportionally({"nasr": 140, "dokki": 60}, 100),
        )

    def test_first_come_would_starve_the_smallest_branch(self):
        # The contrast this rule exists for: walking the branches in order and
        # filling each one would hand Nasr city all 100 and 6th of october
        # nothing -- every day, because the order never changes.
        granted = plan.allocate_proportionally(
            {"nasr": 140, "dokki": 60, "october": 40}, 100
        )
        self.assertEqual(100, sum(granted.values()))
        self.assertTrue(all(qty > 0 for qty in granted.values()), granted)
        self.assertGreater(granted["nasr"], granted["dokki"])
        self.assertGreater(granted["dokki"], granted["october"])

    def test_leftover_units_go_to_the_largest_remainder(self):
        # 10 and 5 out of 7: exact shares 4.667 and 2.333 -> 4 and 2, and the
        # spare jar goes to the bigger remainder.
        self.assertEqual(
            {"a": 5, "b": 2}, plan.allocate_proportionally({"a": 10, "b": 5}, 7)
        )

    def test_nobody_is_allocated_more_than_they_asked_for(self):
        granted = plan.allocate_proportionally({"a": 1, "b": 1, "c": 1}, 99)
        self.assertEqual({"a": 1, "b": 1, "c": 1}, granted)

    def test_half_jars_are_not_shipped(self):
        # A Bin holding 7.9 can send 7 countable jars, not 7.9.
        self.assertEqual(7, sum(plan.allocate_proportionally({"a": 5, "b": 5}, 7.9).values()))

    def test_an_empty_factory_grants_nothing_but_still_answers(self):
        self.assertEqual(
            {"nasr": 0, "dokki": 0},
            plan.allocate_proportionally({"nasr": 140, "dokki": 60}, 0),
        )

    def test_a_negative_source_bin_is_treated_as_empty(self):
        self.assertEqual({"nasr": 0}, plan.allocate_proportionally({"nasr": 10}, -12))

    def test_no_demand_allocates_nothing(self):
        self.assertEqual({"a": 0, "b": 0}, plan.allocate_proportionally({"a": 0, "b": 0}, 50))

    def test_allocation_is_deterministic(self):
        demands = {"a": 7, "b": 7, "c": 7}
        first = plan.allocate_proportionally(demands, 10)
        for _ in range(5):
            self.assertEqual(first, plan.allocate_proportionally(demands, 10))
        self.assertEqual(10, sum(first.values()))


class TestPosProfileDecidesTheBranches(unittest.TestCase):
    """An enabled POS Profile's warehouse IS a branch, whatever it is called.

    The fallback rule below decides by name, and one of its factory-side hints
    is "stores" — so a real branch called "Maadi Stores - J" would have been
    silently absent from every replenishment plan, with nothing on screen to
    say a whole shop was missing. Profiles are also self-maintaining: opening a
    branch means creating its profile, and the plan finds it with no deploy.
    """

    ROWS = [
        {"name": "Finished Goods - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Nasr city - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Maadi Stores - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Raw Material - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Shut Branch - J", "warehouse_type": "", "is_group": 0, "disabled": 1},
        {"name": "All Warehouses - J", "warehouse_type": "", "is_group": 1, "disabled": 0},
    ]

    def _select(self, labels):
        from jarz_pos.services import replenishment_planning as rp

        return [
            entry["warehouse"]
            for entry in rp.select_selling_warehouses(
                self.ROWS,
                source_warehouse="Finished Goods - J",
                branch_labels=labels,
            )
        ]

    def test_a_branch_the_name_rule_would_hide_is_kept(self):
        selected = self._select(
            {"Nasr city - J": "Nasr city", "Maadi Stores - J": "Maadi"}
        )
        self.assertIn("Maadi Stores - J", selected)
        self.assertIn("Nasr city - J", selected)

    def test_a_warehouse_with_no_profile_is_not_a_branch(self):
        # Raw Material passes nothing anyway, but the point is the rule: with
        # profiles present, "not in the list" is the whole test. A back store
        # that happens to look like a shop must not receive a delivery.
        selected = self._select({"Nasr city - J": "Nasr city"})
        self.assertEqual(["Nasr city - J"], selected)

    def test_the_source_is_never_its_own_branch(self):
        selected = self._select(
            {"Finished Goods - J": "Factory", "Nasr city - J": "Nasr city"}
        )
        self.assertEqual(["Nasr city - J"], selected)

    def test_group_and_disabled_still_lose_even_with_a_profile(self):
        selected = self._select(
            {
                "Nasr city - J": "Nasr city",
                "Shut Branch - J": "Shut",
                "All Warehouses - J": "Group",
            }
        )
        self.assertEqual(["Nasr city - J"], selected)

    def test_no_profiles_at_all_falls_back_to_the_name_rule(self):
        # A site that has not configured POS yet still gets a usable plan.
        selected = self._select({})
        self.assertIn("Nasr city - J", selected)
        self.assertNotIn("Raw Material - J", selected)
        self.assertNotIn("Shut Branch - J", selected)


class TestSelectSellingWarehouses(unittest.TestCase):
    ROWS = [
        {"name": "Finished Goods - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Nasr city - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Dokki - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "6th of october - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Raw Material - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Work In Progress - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Consumables - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
        {"name": "Goods In Transit - J", "warehouse_type": "Transit", "is_group": 0, "disabled": 0},
        {"name": "All Warehouses - J", "warehouse_type": "", "is_group": 1, "disabled": 0},
        {"name": "Old Branch - J", "warehouse_type": "", "is_group": 0, "disabled": 1},
    ]

    def _select(self, **kwargs):
        from jarz_pos.services.production_planning import is_non_sellable_warehouse

        params = {
            "source_warehouse": "Finished Goods - J",
            "also_excluded": is_non_sellable_warehouse,
        }
        params.update(kwargs)
        return plan.select_selling_warehouses(self.ROWS, **params)

    def test_only_the_three_selling_branches_survive(self):
        self.assertEqual(
            ["6th of october - J", "Dokki - J", "Nasr city - J"],
            [entry["warehouse"] for entry in self._select()],
        )

    def test_branch_label_falls_back_to_the_stripped_warehouse_name(self):
        # Exactly the Branch record names on this site.
        self.assertEqual(
            ["6th of october", "Dokki", "Nasr city"],
            [entry["branch"] for entry in self._select()],
        )

    def test_pos_profile_supplies_the_branch_label_when_there_is_one(self):
        selected = self._select(branch_labels={"Dokki - J": "Dokki Branch"})
        labels = {entry["warehouse"]: entry["branch"] for entry in selected}
        self.assertEqual("Dokki Branch", labels["Dokki - J"])

    def test_a_new_branch_needs_no_code_change(self):
        rows = self.ROWS + [
            {"name": "Heliopolis - J", "warehouse_type": "", "is_group": 0, "disabled": 0}
        ]
        selected = plan.select_selling_warehouses(rows, source_warehouse="Finished Goods - J")
        self.assertIn("Heliopolis - J", [entry["warehouse"] for entry in selected])


class TestBuildPlan(unittest.TestCase):
    """The whole payload, from the real shape of this site's data."""

    ITEMS = [
        {"item_code": "CHOC-L", "item_name": "Chocolate Hazelnut Large", "stock_uom": "Nos"},
        {"item_code": "TIRA-M", "item_name": "Tiramisu Medium", "stock_uom": "Nos"},
    ]
    BRANCHES = [
        {"warehouse": "Nasr city - J", "branch": "Nasr city"},
        {"warehouse": "Dokki - J", "branch": "Dokki"},
        {"warehouse": "6th of october - J", "branch": "6th of october"},
    ]

    def _plan(self, **kwargs):
        params = dict(
            generated_on="2026-09-10 09:00:00",
            company="Jarz",
            source_warehouse="Finished Goods - J",
            cover_days=14,
            sales_days=30,
            items=self.ITEMS,
            branches=self.BRANCHES,
            # The factory has 100 Chocolate Hazelnut and NONE of the Tiramisu.
            source_available={"CHOC-L": 100, "TIRA-M": 0},
            on_hand={
                # The real negative bin.
                ("Nasr city - J", "CHOC-L"): -55,
                ("Dokki - J", "CHOC-L"): 10,
                ("6th of october - J", "CHOC-L"): 5,
            },
            sold={
                ("Nasr city - J", "CHOC-L"): 300,   # 10/day
                ("Dokki - J", "CHOC-L"): 150,       # 5/day
                ("Nasr city - J", "TIRA-M"): 60,    # 2/day
                # 6th of october has never sold either jar.
            },
        )
        params.update(kwargs)
        return plan.build_plan(**params)

    def _row(self, payload, warehouse, item_code):
        for branch in payload["branches"]:
            if branch["warehouse"] == warehouse:
                for row in branch["items"]:
                    if row["item_code"] == item_code:
                        return row
        return None

    def test_top_level_shape(self):
        payload = self._plan()
        self.assertEqual(
            {
                "generated_on", "company", "cover_days", "sales_days",
                "source", "branches", "summary", "notice",
            },
            set(payload),
        )
        self.assertEqual(14, payload["cover_days"])
        self.assertEqual(30, payload["sales_days"])
        self.assertEqual("Finished Goods - J", payload["source"]["warehouse"])
        self.assertEqual({"CHOC-L": 100.0, "TIRA-M": 0.0}, payload["source"]["available"])
        self.assertEqual(3, len(payload["branches"]))

    def test_row_shape(self):
        row = self._row(self._plan(), "Dokki - J", "CHOC-L")
        self.assertEqual(
            {
                "item_code", "item_name", "stock_uom", "on_hand", "stock_is_negative",
                "sells_per_day", "days_of_cover", "target_days", "suggested_qty",
                "available_at_source", "send_now", "short_by",
            },
            set(row),
        )

    def test_negative_bin_is_reported_raw_and_floored_in_the_maths(self):
        row = self._row(self._plan(), "Nasr city - J", "CHOC-L")
        # Reported raw so somebody goes and counts the shelf...
        self.assertEqual(-55, row["on_hand"])
        self.assertTrue(row["stock_is_negative"])
        self.assertEqual(0.0, row["days_of_cover"])
        # ...and floored to zero in the arithmetic: 10/day x 14 days = 140,
        # not 195.
        self.assertEqual(140, row["suggested_qty"])

    def test_each_branch_uses_its_own_sales_rate(self):
        payload = self._plan()
        self.assertEqual(10.0, self._row(payload, "Nasr city - J", "CHOC-L")["sells_per_day"])
        self.assertEqual(5.0, self._row(payload, "Dokki - J", "CHOC-L")["sells_per_day"])
        # 5/day x 14 = 70, less the 10 on the shelf.
        self.assertEqual(60, self._row(payload, "Dokki - J", "CHOC-L")["suggested_qty"])

    def test_short_supply_is_split_proportionally_and_the_shortfall_is_stated(self):
        payload = self._plan()
        nasr = self._row(payload, "Nasr city - J", "CHOC-L")
        dokki = self._row(payload, "Dokki - J", "CHOC-L")
        # 200 wanted, 100 in the factory -> half each, in proportion.
        self.assertEqual((140, 70, 70), (nasr["suggested_qty"], nasr["send_now"], nasr["short_by"]))
        self.assertEqual((60, 30, 30), (dokki["suggested_qty"], dokki["send_now"], dokki["short_by"]))
        self.assertEqual(100.0, nasr["available_at_source"])
        self.assertEqual(
            100, sum(row["send_now"] for row in [nasr, dokki])
        )

    def test_a_jar_the_factory_has_none_of_is_still_asked_for(self):
        row = self._row(self._plan(), "Nasr city - J", "TIRA-M")
        # 2/day x 14 = 28 needed, 0 available: the need is not silently zeroed,
        # it is reported as a shortfall so somebody makes more.
        self.assertEqual(28, row["suggested_qty"])
        self.assertEqual(0.0, row["available_at_source"])
        self.assertEqual(0, row["send_now"])
        self.assertEqual(28, row["short_by"])

    def test_a_branch_with_no_sales_history_asks_for_nothing(self):
        payload = self._plan()
        october = [b for b in payload["branches"] if b["warehouse"] == "6th of october - J"][0]
        self.assertEqual("6th of october", october["branch"])
        self.assertEqual(
            {
                "items_below_cover": 0,
                "total_suggested": 0.0,
                "total_send_now": 0.0,
                "negative_bins": 0,
            },
            october["summary"],
        )
        # The jar it holds is still listed, with an unknown cover rather than a
        # fabricated one...
        stocked = self._row(payload, "6th of october - J", "CHOC-L")
        self.assertEqual(5, stocked["on_hand"])
        self.assertIsNone(stocked["days_of_cover"])
        self.assertEqual(0, stocked["suggested_qty"])
        # ...and the jar it has never stocked or sold is not listed at all.
        self.assertIsNone(self._row(payload, "6th of october - J", "TIRA-M"))

    def test_branch_summaries(self):
        payload = self._plan()
        nasr = [b for b in payload["branches"] if b["warehouse"] == "Nasr city - J"][0]
        self.assertEqual(
            {
                "items_below_cover": 2,       # both jars
                "total_suggested": 168.0,     # 140 + 28
                "total_send_now": 70.0,       # only the chocolate exists
                "negative_bins": 1,
            },
            nasr["summary"],
        )

    def test_overall_summary(self):
        summary = self._plan()["summary"]
        self.assertEqual(3, summary["branches"])
        self.assertEqual(3, summary["items_below_cover"])
        self.assertEqual(228.0, summary["total_suggested"])   # 140 + 28 + 60
        self.assertEqual(100.0, summary["total_send_now"])
        self.assertEqual(128.0, summary["total_short_by"])
        self.assertEqual(1, summary["negative_bins"])

    def test_rows_are_ordered_by_what_to_send_first(self):
        rows = [b for b in self._plan()["branches"] if b["warehouse"] == "Nasr city - J"][0]["items"]
        self.assertEqual(["CHOC-L", "TIRA-M"], [row["item_code"] for row in rows])

    def test_send_now_lines_are_the_transfer_write_shape(self):
        payload = self._plan()
        for branch in payload["branches"]:
            lines = [
                {"item_code": row["item_code"], "qty": row["send_now"]}
                for row in branch["items"]
                if row["send_now"] > 0
            ]
            for line in lines:
                self.assertTrue(line["item_code"])
                self.assertGreater(line["qty"], 0)
                self.assertEqual(int(line["qty"]), line["qty"])

    def test_no_branches_still_returns_a_well_formed_payload(self):
        payload = self._plan(branches=[])
        self.assertEqual([], payload["branches"])
        self.assertEqual(0, payload["summary"]["branches"])
        self.assertEqual(0.0, payload["summary"]["total_suggested"])


class TestCoerceDays(unittest.TestCase):
    def _call(self, value):
        return plan.coerce_days(value, default=14, minimum=1, maximum=90)

    def test_a_query_string_number_is_accepted(self):
        self.assertEqual(21, self._call("21"))

    def test_blank_and_junk_fall_back_to_the_default(self):
        self.assertEqual(14, self._call(None))
        self.assertEqual(14, self._call(""))
        self.assertEqual(14, self._call("soon"))

    def test_zero_means_unset_not_suppress_everything(self):
        self.assertEqual(14, self._call(0))

    def test_absurd_values_are_clamped(self):
        self.assertEqual(90, self._call(3650))


class TestEndpoint(unittest.TestCase):
    """The API layer, with every database read patched out."""

    def _call(self, **overrides):
        from jarz_pos.api import replenishment as api

        patches = {
            "_ensure_transfer_access": lambda: None,
            "_resolve_company": lambda company: "Jarz",
            "_resolve_warehouse_rows": lambda company: [
                {"name": "Finished Goods - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
                {"name": "Nasr city - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
                {"name": "Dokki - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
                {"name": "Raw Material - J", "warehouse_type": "", "is_group": 0, "disabled": 0},
            ],
            "_resolve_source_warehouse": lambda source, rows: "Finished Goods - J",
            "_resolve_branch_labels": lambda: {},
            "_resolve_jar_items": lambda: [
                {"item_code": "CHOC-L", "item_name": "Chocolate Hazelnut Large", "stock_uom": "Nos"},
            ],
            "_resolve_stock": lambda warehouses, codes: {
                ("Finished Goods - J", "CHOC-L"): 100.0,
                ("Nasr city - J", "CHOC-L"): -55.0,
                ("Dokki - J", "CHOC-L"): 10.0,
            },
            "_resolve_sales": lambda warehouses, codes, days: {
                ("Nasr city - J", "CHOC-L"): 300.0,
                ("Dokki - J", "CHOC-L"): 150.0,
            },
            "_generated_on": lambda: "2026-09-10 09:00:00",
        }
        patches.update(overrides)

        with mock.patch.multiple(api, **{k: mock.Mock(side_effect=v) for k, v in patches.items()}):
            return api.get_branch_replenishment()

    def test_the_endpoint_composes_the_same_plan(self):
        payload = self._call()
        self.assertEqual("Finished Goods - J", payload["source"]["warehouse"])
        self.assertEqual(14, payload["cover_days"])
        self.assertEqual(30, payload["sales_days"])
        self.assertEqual(
            ["Dokki - J", "Nasr city - J"],
            sorted(branch["warehouse"] for branch in payload["branches"]),
        )
        self.assertEqual(100.0, payload["summary"]["total_send_now"])
        self.assertEqual(1, payload["summary"]["negative_bins"])

    def test_the_factory_store_is_not_listed_as_a_branch(self):
        warehouses = [b["warehouse"] for b in self._call()["branches"]]
        self.assertNotIn("Finished Goods - J", warehouses)
        self.assertNotIn("Raw Material - J", warehouses)

    def test_a_failed_stock_read_degrades_to_nothing_to_send(self):
        payload = self._call(_resolve_stock=lambda warehouses, codes: {})
        self.assertEqual(0.0, payload["summary"]["total_send_now"])
        self.assertEqual(2, len(payload["branches"]))

    def test_no_factory_store_returns_a_well_formed_empty_payload(self):
        payload = self._call(_resolve_source_warehouse=lambda source, rows: None)
        self.assertIsNone(payload["source"]["warehouse"])
        self.assertEqual([], payload["branches"])
        self.assertIn("factory store", payload["notice"])

    def test_the_permission_gate_is_the_transfer_gate(self):
        from jarz_pos.api import replenishment as api
        from jarz_pos.api import transfer

        self.assertIs(transfer._ensure_transfer_access, api._ensure_transfer_access)

    def test_the_gate_runs_before_any_read(self):
        from jarz_pos.api import replenishment as api

        class Denied(Exception):
            pass

        with mock.patch.object(api, "_ensure_transfer_access", side_effect=Denied), \
                mock.patch.object(api, "_resolve_company") as company:
            with self.assertRaises(Denied):
                api.get_branch_replenishment()
            company.assert_not_called()


if __name__ == "__main__":
    unittest.main()

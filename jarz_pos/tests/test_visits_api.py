"""Visit planner API tests (light-DB / unittest).

Exercises ``jarz_pos.api.visits`` — the day-route layer over the lead catalog —
and the parts of ``jarz_pos.services.visit_planning`` that need a real site to
mean anything: door-level targeting off the branch child table, the priority
score's CRM inputs, and the leads-catalog ``locations`` enrichment.

Why plain ``unittest.TestCase`` (not FrappeTestCase): identical reasoning to
``test_journey_notes`` / ``test_leads_api`` — on ERPNext v16 FrappeTestCase
imports ``erpnext.tests.utils`` whose module-level BootStrapTestData() collides
with the populated CI ``frontend`` clone. Docs are inserted on the live
connection and rolled back in tearDown, so the module is non-destructive.

IMPORTANT — pre-migrate skip. CI's logic gate runs the suite against staging
WITHOUT running ``bench migrate`` first, so on the very first run after this
lands ``Jarz Visit Plan`` does not exist and every case here would fail on a
missing table. :meth:`VisitTestCase.setUp` skips the module in that window
instead. **A green run on the landing commit therefore proves nothing** — the
suite has to be re-run after the staging deploy has migrated.
"""

from __future__ import annotations

import unittest

import frappe

from jarz_pos.api import leads as leads_api
from jarz_pos.api import visits as visits_api
from jarz_pos.services import visit_planning
from jarz_pos.services.route_planner import RoutePoint

_B2B_ROLE = "B2B Sales Rep"
_COFFEE = "Coffee"

#: Three real-ish Cairo locations, far enough apart that a wrong order costs
#: measurably more than the right one.
_MAADI = (29.9601, 31.2569)
_ZAMALEK = (30.0614, 31.2197)
_HELIOPOLIS = (30.0808, 31.3228)


def _planner_installed():
    try:
        return bool(frappe.db.exists("DocType", visits_api.PLAN_DOCTYPE))
    except Exception:
        return False


def _ensure_b2b_role():
    if not frappe.db.exists("Role", _B2B_ROLE):
        frappe.get_doc(
            {"doctype": "Role", "role_name": _B2B_ROLE, "desk_access": 1, "disabled": 0}
        ).insert(ignore_permissions=True)


def _ensure_category(name=_COFFEE):
    if not frappe.db.exists("Jarz Lead Category", name):
        frappe.get_doc(
            {"doctype": "Jarz Lead Category", "category_name": name}
        ).insert(ignore_permissions=True)


def _days(offset):
    from frappe.utils import add_days, today

    return add_days(today(), offset)


def _make_lead(lead_name, branches=None, fit_score=50):
    """A catalog Lead with located branches, inserted directly.

    Goes through ``frappe.get_doc`` rather than ``leads.save_lead`` because the
    branch child table is not part of that endpoint's writable surface — the
    importer owns it — and these tests need doors at known coordinates.
    """
    doc = frappe.get_doc({
        "doctype": "Lead",
        "lead_name": lead_name,
        "company_name": lead_name,
        "custom_lead_category": _COFFEE,
        "custom_fit_score": fit_score,
        "custom_b2b_stage": "Lead",
    })
    for branch in branches or []:
        doc.append("custom_branches", branch)
    doc.insert(ignore_permissions=True)
    return doc.name


def _branch(name, coords, area="Test Area"):
    return {
        "branch_name": name,
        "area": area,
        "latitude": coords[0],
        "longitude": coords[1],
        "address": f"{name} street",
        "phone": "0100000000",
    }


def _stop(reference_name, coords, title="Stop", branch_name="", **extra):
    payload = {
        "reference_doctype": "Lead",
        "reference_name": reference_name,
        "title": title,
        "branch_name": branch_name,
        "latitude": coords[0],
        "longitude": coords[1],
    }
    payload.update(extra)
    return payload


class VisitTestCase(unittest.TestCase):
    """Shared setUp/tearDown: fixtures present, every write rolled back."""

    def setUp(self):
        if not _planner_installed():
            self.skipTest(
                f"{visits_api.PLAN_DOCTYPE} not installed on this site yet "
                "(pre-migrate run); re-run after the staging deploy."
            )
        _ensure_b2b_role()
        _ensure_category()

    def tearDown(self):
        frappe.db.rollback()


# ---------------------------------------------------------------------------
# Plan lifecycle
# ---------------------------------------------------------------------------
class TestVisitPlanCrud(VisitTestCase):
    def test_create_plan_orders_and_costs_it(self):
        lead = _make_lead("_TEST Visit Cafe A")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1),
            title="Test run",
            stops=[
                _stop(lead, _HELIOPOLIS, title="Heliopolis"),
                _stop(lead, _MAADI, title="Maadi", branch_name="Maadi"),
                _stop(lead, _ZAMALEK, title="Zamalek", branch_name="Zamalek"),
            ],
            start_latitude=_MAADI[0],
            start_longitude=_MAADI[1],
        )
        self.assertEqual(plan["total_stops"], 3)
        self.assertEqual(len(plan["stops"]), 3)
        self.assertGreater(plan["total_distance_km"], 0)
        self.assertGreater(plan["total_duration_minutes"], 0)
        self.assertEqual(plan["rep"], frappe.session.user)
        # Starting in Maadi, the Maadi door must be first.
        self.assertEqual(plan["stops"][0]["title"], "Maadi")

    def test_row_order_is_the_visiting_order(self):
        lead = _make_lead("_TEST Visit Cafe B")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1),
            stops=[
                _stop(lead, _MAADI, title="A", branch_name="A"),
                _stop(lead, _ZAMALEK, title="B", branch_name="B"),
            ],
        )
        self.assertEqual([row["idx"] for row in plan["stops"]], [1, 2])

    def test_totals_are_the_sum_of_the_legs(self):
        lead = _make_lead("_TEST Visit Cafe C")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1),
            stops=[
                _stop(lead, _MAADI, branch_name="A"),
                _stop(lead, _ZAMALEK, branch_name="B"),
                _stop(lead, _HELIOPOLIS, branch_name="C"),
            ],
            start_latitude=_MAADI[0],
            start_longitude=_MAADI[1],
        )
        legs = sum(row["leg_km"] for row in plan["stops"])
        self.assertAlmostEqual(plan["total_distance_km"], legs, places=1)

    def test_arrival_times_walk_the_route(self):
        lead = _make_lead("_TEST Visit Cafe D")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1),
            planned_start_time="09:00:00",
            default_visit_minutes=30,
            stops=[
                _stop(lead, _MAADI, branch_name="A"),
                _stop(lead, _ZAMALEK, branch_name="B"),
            ],
            start_latitude=_MAADI[0],
            start_longitude=_MAADI[1],
        )
        times = [row["planned_time"] for row in plan["stops"]]
        self.assertTrue(all(times), f"expected arrival estimates, got {times}")
        self.assertLess(times[0], times[1])

    def test_a_stop_without_coordinates_is_refused(self):
        lead = _make_lead("_TEST Visit Cafe E")
        with self.assertRaises(frappe.ValidationError):
            visits_api.create_visit_plan(
                visit_date=_days(1),
                stops=[{
                    "reference_doctype": "Lead",
                    "reference_name": lead,
                    "title": "Nowhere",
                    "latitude": 0,
                    "longitude": 0,
                }],
            )

    def test_the_same_door_twice_is_collapsed(self):
        lead = _make_lead("_TEST Visit Cafe F")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1),
            stops=[
                _stop(lead, _MAADI, branch_name="Maadi"),
                _stop(lead, _MAADI, branch_name="Maadi"),
            ],
        )
        self.assertEqual(plan["total_stops"], 1)

    def test_two_branches_of_one_brand_both_survive(self):
        """A brand is not a place. Two doors is two visits."""
        lead = _make_lead("_TEST Visit Chain")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1),
            stops=[
                _stop(lead, _MAADI, branch_name="Maadi"),
                _stop(lead, _ZAMALEK, branch_name="Zamalek"),
            ],
        )
        self.assertEqual(plan["total_stops"], 2)

    def test_delete_removes_the_plan(self):
        lead = _make_lead("_TEST Visit Cafe G")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1), stops=[_stop(lead, _MAADI)]
        )
        visits_api.delete_visit_plan(plan["name"])
        self.assertFalse(frappe.db.exists(visits_api.PLAN_DOCTYPE, plan["name"]))


# ---------------------------------------------------------------------------
# Editing the route
# ---------------------------------------------------------------------------
class TestVisitStops(VisitTestCase):
    def _three_stop_plan(self, label="_TEST Visit Edit"):
        lead = _make_lead(label)
        return lead, visits_api.create_visit_plan(
            visit_date=_days(1),
            stops=[
                _stop(lead, _MAADI, title="Maadi", branch_name="Maadi"),
                _stop(lead, _ZAMALEK, title="Zamalek", branch_name="Zamalek"),
                _stop(lead, _HELIOPOLIS, title="Heliopolis", branch_name="Heliopolis"),
            ],
        )

    def test_reorder_is_honoured_when_optimise_is_off(self):
        """A rep who drags a stop has overruled the optimiser."""
        _, plan = self._three_stop_plan()
        reversed_rows = list(reversed(plan["stops"]))
        updated = visits_api.set_visit_stops(
            plan["name"],
            [{"name": row["name"], **_stop(
                row["reference_name"],
                (row["latitude"], row["longitude"]),
                title=row["title"],
                branch_name=row["branch_name"],
            )} for row in reversed_rows],
            optimize=0,
        )
        self.assertEqual(
            [row["title"] for row in updated["stops"]],
            [row["title"] for row in reversed_rows],
        )

    def test_rows_keep_their_identity_across_a_reorder(self):
        """Reordering a half-driven day must not lose the check-ins."""
        _, plan = self._three_stop_plan("_TEST Visit Identity")
        visits_api.set_visit_stop_status(
            plan["name"], plan["stops"][0]["name"], "Visited", outcome="Bought samples"
        )
        rows = visits_api.get_visit_plan(plan["name"])["stops"]
        payload = [{"name": row["name"], **_stop(
            row["reference_name"], (row["latitude"], row["longitude"]),
            title=row["title"], branch_name=row["branch_name"],
            status=row["status"], outcome=row["outcome"],
        )} for row in reversed(rows)]
        updated = visits_api.set_visit_stops(plan["name"], payload, optimize=0)

        visited = [row for row in updated["stops"] if row["status"] == "Visited"]
        self.assertEqual(len(visited), 1)
        self.assertEqual(visited[0]["outcome"], "Bought samples")
        self.assertTrue(visited[0]["arrived_at"])

    def test_add_stops_appends_and_reoptimises(self):
        lead, plan = self._three_stop_plan("_TEST Visit Append")
        updated = visits_api.add_stops_to_plan(
            plan["name"],
            [_stop(lead, (30.0444, 31.2357), title="Downtown", branch_name="Downtown")],
        )
        self.assertEqual(updated["total_stops"], 4)
        self.assertIn("Downtown", [row["title"] for row in updated["stops"]])

    def test_optimise_never_lengthens_the_day(self):
        _, plan = self._three_stop_plan("_TEST Visit Optimise")
        before = visits_api.get_visit_plan(plan["name"])["total_drive_minutes"]
        after = visits_api.optimize_visit_plan(plan["name"])["total_drive_minutes"]
        self.assertLessEqual(after, before)

    def test_optimise_with_a_live_position_uses_it_as_the_start(self):
        _, plan = self._three_stop_plan("_TEST Visit Live Start")
        updated = visits_api.optimize_visit_plan(
            plan["name"],
            start_latitude=_HELIOPOLIS[0],
            start_longitude=_HELIOPOLIS[1],
        )
        self.assertEqual(updated["stops"][0]["title"], "Heliopolis")
        self.assertAlmostEqual(updated["start_latitude"], _HELIOPOLIS[0], places=4)

    def test_a_pinned_stop_survives_optimisation(self):
        lead = _make_lead("_TEST Visit Pinned")
        plan = visits_api.create_visit_plan(
            visit_date=_days(1),
            optimize=0,
            stops=[
                _stop(lead, _HELIOPOLIS, title="First", branch_name="H"),
                _stop(lead, _ZAMALEK, title="Appointment", branch_name="Z", locked=1),
                _stop(lead, _MAADI, title="Last", branch_name="M"),
            ],
            start_latitude=_MAADI[0],
            start_longitude=_MAADI[1],
        )
        updated = visits_api.optimize_visit_plan(plan["name"])
        self.assertEqual(updated["stops"][1]["title"], "Appointment")

    def test_cancelled_stops_do_not_inflate_the_totals(self):
        _, plan = self._three_stop_plan("_TEST Visit Cancelled")
        full = visits_api.get_visit_plan(plan["name"])["total_distance_km"]
        visits_api.set_visit_stop_status(
            plan["name"], plan["stops"][-1]["name"], "Cancelled"
        )
        trimmed = visits_api.get_visit_plan(plan["name"])["total_distance_km"]
        self.assertLessEqual(trimmed, full)


# ---------------------------------------------------------------------------
# Check-in
# ---------------------------------------------------------------------------
class TestCheckIn(VisitTestCase):
    def test_marking_visited_stamps_the_arrival(self):
        lead = _make_lead("_TEST Visit CheckIn")
        plan = visits_api.create_visit_plan(
            visit_date=_days(0), stops=[_stop(lead, _MAADI, title="Maadi")]
        )
        updated = visits_api.set_visit_stop_status(
            plan["name"], plan["stops"][0]["name"], "Visited", outcome="Manager away"
        )
        row = updated["stops"][0]
        self.assertEqual(row["status"], "Visited")
        self.assertTrue(row["arrived_at"])
        self.assertEqual(row["outcome"], "Manager away")

    def test_resolving_every_stop_completes_the_day(self):
        lead = _make_lead("_TEST Visit Complete")
        plan = visits_api.create_visit_plan(
            visit_date=_days(0),
            stops=[
                _stop(lead, _MAADI, branch_name="A"),
                _stop(lead, _ZAMALEK, branch_name="B"),
            ],
        )
        first = visits_api.set_visit_stop_status(
            plan["name"], plan["stops"][0]["name"], "Visited"
        )
        self.assertEqual(first["status"], "In Progress")
        second = visits_api.set_visit_stop_status(
            plan["name"], plan["stops"][1]["name"], "Skipped"
        )
        self.assertEqual(second["status"], "Completed")

    def test_logging_a_note_writes_a_visit_diary_entry(self):
        if not frappe.db.exists("DocType", "Jarz Journey Note"):
            self.skipTest("journey notes not migrated on this site")
        lead = _make_lead("_TEST Visit Note")
        plan = visits_api.create_visit_plan(
            visit_date=_days(0), stops=[_stop(lead, _MAADI, title="Maadi")]
        )
        updated = visits_api.set_visit_stop_status(
            plan["name"],
            plan["stops"][0]["name"],
            "Visited",
            log_note=1,
            note_text="Dropped two sample jars with the head barista.",
        )
        note_name = updated["stops"][0]["journey_note"]
        self.assertTrue(note_name, "expected the stop to link its diary entry")
        note = frappe.get_doc("Jarz Journey Note", note_name)
        self.assertEqual(note.entry_type, "Visit")
        self.assertEqual(note.reference_name, lead)

    def test_an_unknown_status_is_refused(self):
        lead = _make_lead("_TEST Visit Bad Status")
        plan = visits_api.create_visit_plan(
            visit_date=_days(0), stops=[_stop(lead, _MAADI)]
        )
        with self.assertRaises(frappe.ValidationError):
            visits_api.set_visit_stop_status(
                plan["name"], plan["stops"][0]["name"], "Teleported"
            )

    def test_a_stop_from_another_plan_is_refused(self):
        lead = _make_lead("_TEST Visit Cross Plan")
        first = visits_api.create_visit_plan(
            visit_date=_days(0), stops=[_stop(lead, _MAADI, branch_name="A")]
        )
        second = visits_api.create_visit_plan(
            visit_date=_days(1), stops=[_stop(lead, _ZAMALEK, branch_name="B")]
        )
        with self.assertRaises(frappe.ValidationError):
            visits_api.set_visit_stop_status(
                first["name"], second["stops"][0]["name"], "Visited"
            )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
class TestVisitPlanList(VisitTestCase):
    def test_plans_come_back_in_their_date_range(self):
        lead = _make_lead("_TEST Visit List")
        visits_api.create_visit_plan(
            visit_date=_days(2), title="In range", stops=[_stop(lead, _MAADI)]
        )
        listed = visits_api.get_visit_plans(from_date=_days(0), to_date=_days(5))
        titles = [row["title"] for row in listed["plans"]]
        self.assertIn("In range", titles)

    def test_a_plan_outside_the_range_is_not_listed(self):
        lead = _make_lead("_TEST Visit Out Of Range")
        visits_api.create_visit_plan(
            visit_date=_days(60), title="Far future", stops=[_stop(lead, _MAADI)]
        )
        listed = visits_api.get_visit_plans(from_date=_days(0), to_date=_days(5))
        self.assertNotIn("Far future", [row["title"] for row in listed["plans"]])

    def test_list_rows_carry_no_stops(self):
        lead = _make_lead("_TEST Visit Light List")
        visits_api.create_visit_plan(
            visit_date=_days(1), stops=[_stop(lead, _MAADI)]
        )
        listed = visits_api.get_visit_plans(from_date=_days(0), to_date=_days(5))
        for row in listed["plans"]:
            self.assertNotIn("stops", row)


# ---------------------------------------------------------------------------
# Targets and suggestions
# ---------------------------------------------------------------------------
class TestVisitTargets(VisitTestCase):
    def test_one_target_per_branch_not_per_brand(self):
        lead = _make_lead(
            "_TEST Visit Multi Branch",
            branches=[
                _branch("Maadi", _MAADI),
                _branch("Zamalek", _ZAMALEK),
                _branch("Heliopolis", _HELIOPOLIS),
            ],
        )
        targets = visit_planning.lead_targets()
        mine = [t for t in targets if t.reference_name == lead]
        self.assertEqual(len(mine), 3)
        self.assertEqual(
            sorted(t.branch_name for t in mine),
            ["Heliopolis", "Maadi", "Zamalek"],
        )

    def test_a_branch_without_a_pin_is_not_a_target(self):
        lead = _make_lead(
            "_TEST Visit Unlocated",
            branches=[
                _branch("Located", _MAADI),
                {"branch_name": "Unlocated", "area": "Nowhere"},
            ],
        )
        mine = [t for t in visit_planning.lead_targets() if t.reference_name == lead]
        self.assertEqual([t.branch_name for t in mine], ["Located"])

    def test_never_visited_scores_above_recently_visited(self):
        if not frappe.db.exists("DocType", "Jarz Journey Note"):
            self.skipTest("journey notes not migrated on this site")
        from jarz_pos.api import journey as journey_api

        fresh = _make_lead(
            "_TEST Visit Fresh", branches=[_branch("Maadi", _MAADI)], fit_score=50
        )
        stale = _make_lead(
            "_TEST Visit Stale", branches=[_branch("Zamalek", _ZAMALEK)], fit_score=50
        )
        journey_api.add_journey_note(
            reference_doctype="Lead",
            reference_name=stale,
            note="Called in yesterday.",
            entry_date=_days(-1),
            entry_type="Visit",
        )
        by_lead = {
            t.reference_name: t
            for t in visit_planning.lead_targets()
            if t.reference_name in (fresh, stale)
        }
        self.assertGreater(by_lead[fresh].priority, by_lead[stale].priority)
        self.assertIn("never visited", by_lead[fresh].reasons)

    def test_a_not_suitable_lead_is_never_a_target(self):
        if not frappe.get_meta("Lead").get_field("custom_not_suitable"):
            self.skipTest("suitability verdict not migrated on this site")
        lead = _make_lead(
            "_TEST Visit Rejected", branches=[_branch("Maadi", _MAADI)]
        )
        frappe.db.set_value("Lead", lead, "custom_not_suitable", 1)
        names = {t.reference_name for t in visit_planning.lead_targets()}
        self.assertNotIn(lead, names)

    def test_targets_endpoint_ranks_best_first(self):
        _make_lead("_TEST Visit Rank A", branches=[_branch("A", _MAADI)], fit_score=10)
        _make_lead("_TEST Visit Rank B", branches=[_branch("B", _ZAMALEK)], fit_score=95)
        payload = visits_api.get_visit_targets(include_customers=0, limit=50)
        priorities = [row["priority"] for row in payload["targets"]]
        self.assertEqual(priorities, sorted(priorities, reverse=True))

    def test_suggestion_clusters_and_fits_the_day(self):
        for index in range(6):
            _make_lead(
                f"_TEST Visit Cluster {index}",
                branches=[_branch(
                    f"B{index}",
                    (_MAADI[0] + index * 0.004, _MAADI[1] + index * 0.004),
                )],
                fit_score=80,
            )
        proposal = visits_api.suggest_visit_plan(
            visit_date=_days(1),
            max_stops=4,
            start_latitude=_MAADI[0],
            start_longitude=_MAADI[1],
            include_customers=0,
            day_minutes=300,
        )
        self.assertLessEqual(len(proposal["targets"]), 4)
        self.assertLessEqual(proposal["total_duration_minutes"], 300)
        self.assertTrue(proposal["targets"], "expected a suggestion")
        for row in proposal["targets"]:
            self.assertTrue(row["reasons"], "every suggestion must explain itself")

    def test_suggestion_writes_nothing(self):
        _make_lead("_TEST Visit Dry Run", branches=[_branch("A", _MAADI)])
        before = frappe.db.count(visits_api.PLAN_DOCTYPE)
        visits_api.suggest_visit_plan(visit_date=_days(1), include_customers=0)
        self.assertEqual(frappe.db.count(visits_api.PLAN_DOCTYPE), before)


# ---------------------------------------------------------------------------
# Catalog enrichment
# ---------------------------------------------------------------------------
class TestLeadLocations(VisitTestCase):
    def test_catalog_rows_carry_their_located_branches(self):
        lead = _make_lead(
            "_TEST Visit Catalog",
            branches=[_branch("Maadi", _MAADI), _branch("Zamalek", _ZAMALEK)],
        )
        catalog = leads_api.get_leads()
        row = next(r for r in catalog["leads"] if r["name"] == lead)
        self.assertEqual(len(row["locations"]), 2)
        self.assertAlmostEqual(row["locations"][0]["latitude"], _MAADI[0], places=4)

    def test_every_row_has_the_key_even_with_no_branches(self):
        lead = _make_lead("_TEST Visit No Branches")
        catalog = leads_api.get_leads()
        row = next(r for r in catalog["leads"] if r["name"] == lead)
        self.assertEqual(row["locations"], [])

    def test_unlocated_branches_are_dropped_not_zeroed(self):
        """A (0, 0) marker would be plotted in the Atlantic and routed to."""
        lead = _make_lead(
            "_TEST Visit Zero Pin",
            branches=[
                _branch("Real", _MAADI),
                {"branch_name": "Zero", "latitude": 0, "longitude": 0},
            ],
        )
        catalog = leads_api.get_leads()
        row = next(r for r in catalog["leads"] if r["name"] == lead)
        self.assertEqual([loc["branch_name"] for loc in row["locations"]], ["Real"])


# ---------------------------------------------------------------------------
# Engine reporting
# ---------------------------------------------------------------------------
class TestRouteEngineStatus(VisitTestCase):
    def test_status_reports_the_engine_and_the_tunables(self):
        status = visits_api.get_route_engine_status()
        self.assertIn(status["engine"], ("osrm", "straight_line"))
        self.assertIn("configured", status)
        self.assertGreater(status["avg_speed_kmh"], 0)
        self.assertGreater(status["road_factor"], 0)

    def test_an_unconfigured_server_is_reported_as_configuration_not_failure(self):
        from jarz_pos.services import osrm_client

        if osrm_client.base_url():
            self.skipTest("this site has an OSRM server configured")
        status = visits_api.get_route_engine_status()
        self.assertFalse(status["configured"])
        self.assertEqual(status["engine"], "straight_line")
        self.assertIn("No OSRM base URL", status["reason"])

    def test_planning_works_with_no_routing_server(self):
        """The whole point of the fallback: a route must still plan."""
        config = visit_planning.route_config()
        config.use_osrm = False
        points = [
            RoutePoint(key="a", lat=_MAADI[0], lng=_MAADI[1]),
            RoutePoint(key="b", lat=_ZAMALEK[0], lng=_ZAMALEK[1]),
        ]
        self.assertIsNone(config.osrm_provider())
        from jarz_pos.services.route_planner import plan_route

        result = plan_route(points, road_factor=config.road_factor)
        self.assertEqual(result.engine, "haversine")
        self.assertGreater(result.total_distance_m, 0)


if __name__ == "__main__":
    unittest.main()

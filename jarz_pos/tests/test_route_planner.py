"""Route optimiser tests (pure -- no site, no database, no network).

:mod:`jarz_pos.services.route_planner` imports no Frappe, which is what lets
this whole module run on a laptop and in CI's pre-migrate logic gate alike. The
solver is the one part of the visit planner whose correctness cannot be checked
by looking at it: a route that is merely *plausible* looks exactly like a route
that is optimal.

So the assertions are structural rather than numeric wherever possible — a
known-answer ring, a monotonic improvement over the input order, a pinned stop
that must not move — because hard-coding "27.4 km" would only pin today's
constants in place and would fail the moment somebody tunes the road factor.
"""

from __future__ import annotations

import math
import unittest

from jarz_pos.services.route_planner import (
    DEFAULT_SPEED_KMH,
    MAX_STOPS,
    CostMatrix,
    RoutePoint,
    build_matrix,
    cost_fixed_order,
    haversine_m,
    haversine_matrix,
    plan_route,
)

#: Roughly central Cairo, so the numbers are in the range the app actually sees.
_ORIGIN_LAT = 30.0444
_ORIGIN_LNG = 31.2357


def ring(count: int, radius_deg: float = 0.03):
    """``count`` points evenly spaced on a circle.

    A ring has exactly one optimal visiting order (go round it), which makes it
    the cleanest known-answer case for a travelling-salesman solver: any
    crossing at all shows up as a longer route.
    """
    points = []
    for index in range(count):
        angle = 2 * math.pi * index / count
        points.append(
            RoutePoint(
                key=f"S{index}",
                lat=_ORIGIN_LAT + radius_deg * math.cos(angle),
                lng=_ORIGIN_LNG + radius_deg * math.sin(angle),
            )
        )
    return points


def scramble(points, seed=7):
    """Deterministic shuffle -- a flaky optimiser test is worse than none."""
    import random

    shuffled = list(points)
    random.Random(seed).shuffle(shuffled)
    return shuffled


def keys(points, order):
    return [points[index].key for index in order]


class TestHaversine(unittest.TestCase):
    def test_zero_distance_to_itself(self):
        self.assertAlmostEqual(haversine_m(30.0, 31.0, 30.0, 31.0), 0.0, places=6)

    def test_one_degree_of_latitude_is_about_111km(self):
        metres = haversine_m(30.0, 31.0, 31.0, 31.0)
        self.assertAlmostEqual(metres / 1000.0, 111.2, delta=1.0)

    def test_symmetric(self):
        forward = haversine_m(30.0, 31.0, 30.1, 31.1)
        backward = haversine_m(30.1, 31.1, 30.0, 31.0)
        self.assertAlmostEqual(forward, backward, places=6)


class TestMatrix(unittest.TestCase):
    def test_road_factor_inflates_straight_line(self):
        points = ring(4)
        plain = haversine_matrix(points, road_factor=1.0)
        inflated = haversine_matrix(points, road_factor=1.5)
        self.assertAlmostEqual(
            inflated.distances_m[0][1], plain.distances_m[0][1] * 1.5, places=3
        )

    def test_duration_follows_the_configured_speed(self):
        points = ring(2)
        matrix = haversine_matrix(points, road_factor=1.0, speed_kmh=DEFAULT_SPEED_KMH)
        expected = matrix.distances_m[0][1] / (DEFAULT_SPEED_KMH * 1000.0 / 3600.0)
        self.assertAlmostEqual(matrix.durations_s[0][1], expected, places=3)

    def test_diagonal_is_zero(self):
        matrix = haversine_matrix(ring(5))
        for index in range(matrix.size):
            self.assertEqual(matrix.distances_m[index][index], 0.0)

    def test_engine_is_reported(self):
        self.assertEqual(haversine_matrix(ring(3)).engine, "haversine")


class TestEngineFallback(unittest.TestCase):
    """The OSRM ladder. A route must still plan when the routing box is away."""

    def test_no_provider_uses_the_estimate(self):
        matrix = build_matrix(ring(3))
        self.assertEqual(matrix.engine, "haversine")
        self.assertIsNone(matrix.note)

    def test_provider_returning_none_falls_back_with_a_reason(self):
        matrix = build_matrix(ring(3), osrm_provider=lambda points: None)
        self.assertEqual(matrix.engine, "haversine")
        self.assertIn("straight-line", matrix.note)

    def test_provider_raising_falls_back_rather_than_propagating(self):
        def explode(points):
            raise ConnectionError("routing box is down")

        matrix = build_matrix(ring(3), osrm_provider=explode)
        self.assertEqual(matrix.engine, "haversine")
        self.assertIn("ConnectionError", matrix.note)

    def test_wrong_sized_osrm_matrix_is_rejected(self):
        """A short matrix would be silently mis-indexed -- worse than no matrix."""

        def truncated(points):
            return CostMatrix([[0.0, 1.0], [1.0, 0.0]], [[0.0, 1.0], [1.0, 0.0]], "osrm")

        matrix = build_matrix(ring(5), osrm_provider=truncated)
        self.assertEqual(matrix.engine, "haversine")
        self.assertIn("5", matrix.note)

    def test_good_osrm_matrix_is_used_as_is(self):
        points = ring(3)
        canned = CostMatrix(
            [[0.0, 10.0, 20.0], [10.0, 0.0, 30.0], [20.0, 30.0, 0.0]],
            [[0.0, 1.0, 2.0], [1.0, 0.0, 3.0], [2.0, 3.0, 0.0]],
            "osrm",
        )
        matrix = build_matrix(points, osrm_provider=lambda p: canned)
        self.assertEqual(matrix.engine, "osrm")
        self.assertEqual(matrix.distances_m[0][2], 20.0)


class TestSolver(unittest.TestCase):
    def test_a_scrambled_ring_is_restored_to_the_ring(self):
        """The known-answer case: the only good route round a circle is round it."""
        points = ring(10)
        shuffled = scramble(points)
        result = plan_route(shuffled)
        ordered = keys(shuffled, result.order)

        # Consecutive labels, modulo direction and starting point.
        indices = [int(key[1:]) for key in ordered]
        steps = {
            (indices[i + 1] - indices[i]) % 10 for i in range(len(indices) - 1)
        }
        self.assertTrue(
            steps <= {1} or steps <= {9},
            f"expected a clean traversal, got {ordered}",
        )

    def test_optimising_never_loses_to_the_order_given(self):
        points = scramble(ring(12), seed=11)
        start = RoutePoint(key="HOME", lat=30.0, lng=31.2)
        optimised = plan_route(points, start=start)
        as_given = cost_fixed_order(points, start=start)
        self.assertLessEqual(optimised.total_drive_s, as_given.total_drive_s + 1e-6)

    def test_every_stop_is_visited_exactly_once(self):
        points = scramble(ring(9), seed=5)
        result = plan_route(points, start=RoutePoint(key="H", lat=30.0, lng=31.2))
        self.assertEqual(sorted(result.order), list(range(len(points))))

    def test_deterministic(self):
        """Two taps of Optimise must produce the same route, or nobody trusts it."""
        points = scramble(ring(11), seed=2)
        first = plan_route(points)
        second = plan_route(points)
        self.assertEqual(first.order, second.order)
        self.assertAlmostEqual(first.total_distance_m, second.total_distance_m, places=6)

    def test_legs_line_up_with_the_order(self):
        points = scramble(ring(6), seed=4)
        start = RoutePoint(key="HOME", lat=30.0, lng=31.2)
        result = plan_route(points, start=start)
        self.assertEqual(len(result.legs), len(points))
        self.assertEqual(result.legs[0].from_key, "HOME")
        expected = ["HOME"] + keys(points, result.order)
        for index, leg in enumerate(result.legs):
            self.assertEqual(leg.from_key, expected[index])
            self.assertEqual(leg.to_key, expected[index + 1])

    def test_total_distance_is_the_sum_of_its_legs(self):
        points = scramble(ring(7), seed=9)
        result = plan_route(points, start=RoutePoint(key="H", lat=30.0, lng=31.2))
        self.assertAlmostEqual(
            result.total_distance_m, sum(leg.distance_m for leg in result.legs), places=6
        )

    def test_returning_to_start_adds_the_closing_leg(self):
        points = scramble(ring(6), seed=1)
        start = RoutePoint(key="HOME", lat=30.0, lng=31.2)
        open_route = plan_route(points, start=start)
        loop = plan_route(points, start=start, return_to_start=True)
        self.assertEqual(len(loop.legs), len(open_route.legs) + 1)
        self.assertEqual(loop.legs[-1].to_key, "HOME")
        self.assertGreater(loop.total_distance_m, open_route.total_distance_m)

    def test_no_start_point_beats_a_distant_one(self):
        """Free choice of opener cannot be worse than being dragged out to a start."""
        points = scramble(ring(8), seed=6)
        anchored = plan_route(points, start=RoutePoint(key="FAR", lat=29.5, lng=30.5))
        free = plan_route(points)
        self.assertLess(free.total_drive_s, anchored.total_drive_s)

    def test_service_time_counts_toward_the_day(self):
        points = [
            RoutePoint(key="A", lat=30.04, lng=31.23, service_minutes=30),
            RoutePoint(key="B", lat=30.06, lng=31.25, service_minutes=45),
        ]
        result = plan_route(points, default_visit_minutes=20)
        self.assertEqual(result.total_service_s, (30 + 45) * 60)
        self.assertEqual(
            result.total_duration_s, result.total_drive_s + result.total_service_s
        )

    def test_default_visit_minutes_fills_in_for_unset_stops(self):
        points = [
            RoutePoint(key="A", lat=30.04, lng=31.23),
            RoutePoint(key="B", lat=30.06, lng=31.25),
        ]
        result = plan_route(points, default_visit_minutes=15)
        self.assertEqual(result.total_service_s, 2 * 15 * 60)


class TestPinnedStops(unittest.TestCase):
    """A booked appointment must not be reshuffled, however good the alternative."""

    def test_a_pinned_stop_keeps_its_position(self):
        points = scramble(ring(10), seed=8)
        pinned_index = 3
        points[pinned_index] = RoutePoint(
            key=points[pinned_index].key,
            lat=points[pinned_index].lat,
            lng=points[pinned_index].lng,
            locked=True,
        )
        result = plan_route(points, start=RoutePoint(key="H", lat=30.0, lng=31.2))
        ordered = keys(points, result.order)
        self.assertEqual(ordered[pinned_index], points[pinned_index].key)

    def test_several_pins_all_hold(self):
        points = scramble(ring(12), seed=12)
        for index in (0, 4, 9):
            points[index] = RoutePoint(
                key=points[index].key,
                lat=points[index].lat,
                lng=points[index].lng,
                locked=True,
            )
        result = plan_route(points, start=RoutePoint(key="H", lat=30.0, lng=31.2))
        ordered = keys(points, result.order)
        for index in (0, 4, 9):
            self.assertEqual(ordered[index], points[index].key)

    def test_pinning_everything_preserves_the_given_order(self):
        points = scramble(ring(6), seed=13)
        pinned = [
            RoutePoint(key=p.key, lat=p.lat, lng=p.lng, locked=True) for p in points
        ]
        result = plan_route(pinned, start=RoutePoint(key="H", lat=30.0, lng=31.2))
        self.assertEqual(keys(pinned, result.order), [p.key for p in points])

    def test_pins_still_hold_without_a_start_point(self):
        points = scramble(ring(8), seed=14)
        points[2] = RoutePoint(
            key=points[2].key, lat=points[2].lat, lng=points[2].lng, locked=True
        )
        result = plan_route(points)
        self.assertEqual(keys(points, result.order)[2], points[2].key)
        self.assertEqual(sorted(result.order), list(range(len(points))))

    def test_cost_fixed_order_changes_nothing(self):
        """The 'I dragged it, leave it alone' path."""
        points = scramble(ring(9), seed=15)
        result = cost_fixed_order(points, start=RoutePoint(key="H", lat=30.0, lng=31.2))
        self.assertEqual(keys(points, result.order), [p.key for p in points])
        self.assertGreater(result.total_distance_m, 0)


class TestEdges(unittest.TestCase):
    def test_no_stops(self):
        result = plan_route([])
        self.assertEqual(result.order, [])
        self.assertEqual(result.total_distance_m, 0.0)

    def test_one_stop_with_a_start(self):
        result = plan_route(
            [RoutePoint(key="A", lat=30.06, lng=31.25)],
            start=RoutePoint(key="H", lat=30.04, lng=31.23),
        )
        self.assertEqual(result.order, [0])
        self.assertEqual(len(result.legs), 1)
        self.assertGreater(result.total_distance_m, 0)

    def test_one_stop_without_a_start_has_no_legs(self):
        result = plan_route([RoutePoint(key="A", lat=30.06, lng=31.25)])
        self.assertEqual(result.order, [0])
        self.assertEqual(result.legs, [])
        self.assertEqual(result.total_distance_m, 0.0)

    def test_two_stops_at_the_same_place(self):
        points = [
            RoutePoint(key="A", lat=30.04, lng=31.23),
            RoutePoint(key="B", lat=30.04, lng=31.23),
        ]
        result = plan_route(points)
        self.assertEqual(sorted(result.order), [0, 1])
        self.assertEqual(result.total_distance_m, 0.0)

    def test_too_many_stops_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            plan_route(ring(MAX_STOPS + 1))

    def test_the_ceiling_itself_is_allowed(self):
        result = plan_route(ring(MAX_STOPS))
        self.assertEqual(len(result.order), MAX_STOPS)


class TestPerformance(unittest.TestCase):
    def test_a_full_day_solves_fast_enough_for_a_button_press(self):
        """Twenty stops is a long day; the solver sits inside a tap."""
        import time

        points = scramble(ring(20), seed=21)
        started = time.time()
        plan_route(points, start=RoutePoint(key="H", lat=30.0, lng=31.2))
        elapsed = time.time() - started
        self.assertLess(elapsed, 2.0, f"solver took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()

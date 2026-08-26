"""Order a day's visits into the shortest practical route.

This is deliberately a *pure* module: it imports no Frappe, touches no
document, and does no I/O of its own. Everything it needs arrives as plain
dataclasses, which is what lets the whole optimizer run in a unit test on a
laptop with no site — the same reason ``utils/geo.py`` is separate from
``api/geo.py``.

Two halves:

* **A cost matrix.** How far, and how long, between every pair of stops. Two
  providers implement it — :func:`haversine_matrix` (free, offline, always
  available) and the OSRM ``/table`` service in
  :mod:`jarz_pos.services.osrm_client` (real road distances). The OSRM one is
  the preferred engine and the haversine one is its *fallback*, not its rival:
  a route must still be plannable when OSRM is down, unreachable, or simply
  not configured yet. :func:`build_matrix` encodes that ladder and reports
  which engine actually answered, because a rep reading "34 km" deserves to
  know whether that is a road distance or a straight-line estimate.

* **A solver.** Nearest-neighbour construction, then 2-opt and Or-opt
  improvement. For the sizes a human day actually holds — a dozen stops, two
  dozen at the outside — this lands on the optimum or within a percent of it
  in single-digit milliseconds, and it is deterministic, which matters more
  than the last percent: a rep who taps "optimize" twice on the same stops and
  gets two different routes stops trusting the button.

**Pinned stops.** A stop marked ``locked`` keeps its *position number* in the
route (1st, 2nd, ...) and the optimizer reorders everything else around it.
That is what a booked 11:00 appointment means in practice. The solver honours
it by construction rather than by penalty: reserved positions are filled first
and every improvement move permutes only the free stops, so a locked stop
cannot drift no matter how many passes run.

Distances are metres and durations are seconds throughout; conversion to the
km/minutes a screen shows happens once, at the edge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Straight-line kilometres are not driven kilometres. Streets bend, the Nile
#: has a countable number of bridges, and one-way systems double back. This
#: multiplier turns a great-circle distance into a plausible driving distance.
#: 1.35 is the middle of the range usually measured for dense cities; Cairo's
#: river crossings push it higher on some pairs, which is precisely why OSRM is
#: the preferred engine and this is the fallback.
DEFAULT_ROAD_FACTOR = 1.35

#: Average door-to-door speed including traffic, parking and the walk in.
#: Deliberately low: an optimizer that promises motorway speeds inside Cairo
#: produces a day plan that cannot be executed, and an unachievable plan is
#: worse than no plan because the rep abandons it by 1pm.
DEFAULT_SPEED_KMH = 22.0

#: How long a rep is actually inside a venue. Used when neither the stop nor
#: the plan says otherwise.
DEFAULT_VISIT_MINUTES = 20

#: Guard rail on matrix size. n stops means n^2 matrix cells and the OSRM
#: ``/table`` URL carries every coordinate, so this bounds both the solver's
#: work and the request line. A day with more stops than this is not a routing
#: problem, it is a planning mistake.
MAX_STOPS = 60

_EARTH_RADIUS_M = 6371008.8


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass
class RoutePoint:
    """One place the route passes through.

    ``key`` is opaque to this module — the caller uses it to map a solved
    position back to whatever it came from (a visit stop row, a lead branch).
    ``locked`` pins the point to its current position number; see the module
    docstring.
    """

    key: str
    lat: float
    lng: float
    locked: bool = False
    service_minutes: int = 0
    label: str = ""

    def as_coord(self) -> Tuple[float, float]:
        return (self.lat, self.lng)


@dataclass
class CostMatrix:
    """Pairwise distance/duration, plus who computed it.

    ``engine`` is part of the payload rather than a side channel because it
    changes what the numbers *mean*, and the UI has to say so.
    """

    distances_m: List[List[float]]
    durations_s: List[List[float]]
    engine: str
    note: Optional[str] = None

    @property
    def size(self) -> int:
        return len(self.distances_m)


@dataclass
class RouteLeg:
    """The drive from one point to the next."""

    from_key: str
    to_key: str
    distance_m: float
    duration_s: float


@dataclass
class RouteResult:
    """A solved route.

    ``order`` holds indices into the point list handed to :func:`plan_route`,
    in visiting order, and *excludes* the start point: the start is where the
    rep already is, not somewhere to be scheduled.
    """

    order: List[int]
    legs: List[RouteLeg] = field(default_factory=list)
    total_distance_m: float = 0.0
    total_drive_s: float = 0.0
    total_service_s: float = 0.0
    engine: str = "haversine"
    note: Optional[str] = None
    improved: bool = False

    @property
    def total_duration_s(self) -> float:
        return self.total_drive_s + self.total_service_s


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres.

    Duplicated from :func:`jarz_pos.utils.geo.haversine_m` on purpose: that
    module is the door-pin parser's home and importing it here would drag the
    URL-parsing machinery into a solver that only ever needs trigonometry. The
    formula is not going to drift.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def haversine_matrix(
    points: Sequence[RoutePoint],
    road_factor: float = DEFAULT_ROAD_FACTOR,
    speed_kmh: float = DEFAULT_SPEED_KMH,
) -> CostMatrix:
    """Straight-line matrix, inflated to a driving estimate.

    Always succeeds. This is the floor the whole feature stands on: with it,
    a rep can plan a route on a plane, in a dead zone, or with the OSRM box
    switched off.
    """
    factor = max(1.0, float(road_factor or DEFAULT_ROAD_FACTOR))
    speed = max(1.0, float(speed_kmh or DEFAULT_SPEED_KMH))
    metres_per_second = speed * 1000.0 / 3600.0

    size = len(points)
    distances = [[0.0] * size for _ in range(size)]
    durations = [[0.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            straight = haversine_m(
                points[i].lat, points[i].lng, points[j].lat, points[j].lng
            )
            metres = straight * factor
            seconds = metres / metres_per_second
            distances[i][j] = distances[j][i] = metres
            durations[i][j] = durations[j][i] = seconds
    return CostMatrix(distances, durations, engine="haversine")


def build_matrix(
    points: Sequence[RoutePoint],
    road_factor: float = DEFAULT_ROAD_FACTOR,
    speed_kmh: float = DEFAULT_SPEED_KMH,
    osrm_provider: Optional[Callable[[Sequence[RoutePoint]], Optional[CostMatrix]]] = None,
) -> CostMatrix:
    """The engine ladder: real road distances if we can get them, else honest estimates.

    ``osrm_provider`` is injected rather than imported so this module stays
    pure and the fallback path is trivially testable — hand it a provider that
    returns ``None`` (unconfigured) or raises (box down) and assert the caller
    still gets a usable matrix.

    A provider that returns a matrix of the wrong size is treated as a failure,
    not trusted: a partial ``/table`` response silently mis-indexed would put
    the rep on a route built from another day's coordinates.
    """
    if osrm_provider is not None:
        try:
            matrix = osrm_provider(points)
        except Exception as exc:  # pragma: no cover - exercised via injected raiser
            matrix = None
            fallback_note = f"OSRM unavailable ({exc.__class__.__name__}); used straight-line estimate"
        else:
            fallback_note = "OSRM returned no result; used straight-line estimate"
        if matrix is not None and matrix.size == len(points):
            return matrix
        if matrix is not None:
            fallback_note = (
                f"OSRM returned {matrix.size} points for {len(points)}; "
                "used straight-line estimate"
            )
        result = haversine_matrix(points, road_factor, speed_kmh)
        result.note = fallback_note
        return result

    return haversine_matrix(points, road_factor, speed_kmh)


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------


def _route_cost(
    sequence: Sequence[int], matrix: CostMatrix, origin: Optional[int], close: bool
) -> float:
    """Total drive time of a visiting ``sequence``.

    Optimising on *duration* rather than distance is the deliberate choice:
    the rep's binding constraint is the working day, not the odometer.
    """
    if not sequence:
        return 0.0
    total = 0.0
    previous = origin if origin is not None else sequence[0]
    start_at = 0 if origin is not None else 1
    for position in range(start_at, len(sequence)):
        total += matrix.durations_s[previous][sequence[position]]
        previous = sequence[position]
    if close and origin is not None:
        total += matrix.durations_s[previous][origin]
    return total


def _assemble(free_order: Sequence[int], reserved: Dict[int, int], length: int) -> List[int]:
    """Interleave the solver's free stops with the pinned ones.

    ``reserved`` maps a position number to the point index nailed to it. The
    free stops fill what is left, in the order the solver chose. This is the
    single place locking is enforced, which is why no improvement move needs to
    know about it.
    """
    assembled: List[int] = []
    free_iter = iter(free_order)
    for position in range(length):
        if position in reserved:
            assembled.append(reserved[position])
        else:
            assembled.append(next(free_iter))
    return assembled


def _nearest_neighbour(
    free: Sequence[int], matrix: CostMatrix, origin: Optional[int], reserved: Dict[int, int], length: int
) -> List[int]:
    """Greedy construction: from where you are, go to the closest place left.

    Walks the *assembled* route so a pinned stop genuinely influences what gets
    picked next — otherwise the greedy chain would ignore the appointment
    sitting in the middle of the day and hand 2-opt a poor starting point.
    """
    remaining = list(free)
    chosen: List[int] = []
    current = origin
    for position in range(length):
        if position in reserved:
            current = reserved[position]
            continue
        if not remaining:
            break
        if current is None:
            # No start point given: seed from the first free stop and let the
            # multi-start wrapper in plan_route try the alternatives.
            pick = remaining[0]
        else:
            pick = min(remaining, key=lambda idx: matrix.durations_s[current][idx])
        remaining.remove(pick)
        chosen.append(pick)
        current = pick
    return chosen


def _two_opt(
    free_order: List[int],
    matrix: CostMatrix,
    origin: Optional[int],
    reserved: Dict[int, int],
    length: int,
    close: bool,
) -> Tuple[List[int], bool]:
    """Uncross the route.

    2-opt reverses a run of stops; the classic win is undoing the X-shaped
    crossing a greedy chain leaves behind. Reversal happens on the *free*
    subsequence, so pinned positions are untouched by construction.
    """
    best = list(free_order)
    best_cost = _route_cost(_assemble(best, reserved, length), matrix, origin, close)
    improved_any = False
    size = len(best)
    if size < 3:
        return best, False

    improved = True
    while improved:
        improved = False
        for i in range(size - 1):
            for j in range(i + 1, size):
                candidate = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                cost = _route_cost(
                    _assemble(candidate, reserved, length), matrix, origin, close
                )
                if cost < best_cost - 1e-6:
                    best, best_cost = candidate, cost
                    improved = improved_any = True
    return best, improved_any


def _or_opt(
    free_order: List[int],
    matrix: CostMatrix,
    origin: Optional[int],
    reserved: Dict[int, int],
    length: int,
    close: bool,
    max_segment: int = 3,
) -> Tuple[List[int], bool]:
    """Relocate short runs of stops.

    2-opt cannot express "this one shop belongs at the other end of the day";
    reversing a segment to move a single stop drags its neighbours with it.
    Or-opt lifts runs of one to three stops and reinserts them elsewhere, which
    is the move that cleans up a lone outlier — exactly the shape a
    geographically-seeded candidate list produces.
    """
    best = list(free_order)
    best_cost = _route_cost(_assemble(best, reserved, length), matrix, origin, close)
    improved_any = False
    size = len(best)
    if size < 3:
        return best, False

    improved = True
    while improved:
        improved = False
        for seg_len in range(1, min(max_segment, size - 1) + 1):
            for start in range(size - seg_len + 1):
                segment = best[start : start + seg_len]
                rest = best[:start] + best[start + seg_len :]
                for insert_at in range(len(rest) + 1):
                    if insert_at == start:
                        continue
                    candidate = rest[:insert_at] + segment + rest[insert_at:]
                    cost = _route_cost(
                        _assemble(candidate, reserved, length), matrix, origin, close
                    )
                    if cost < best_cost - 1e-6:
                        best, best_cost = candidate, cost
                        improved = improved_any = True
                        break
                if improved:
                    break
            if improved:
                break
    return best, improved_any


def _solve(
    matrix: CostMatrix,
    stop_indices: Sequence[int],
    origin: Optional[int],
    reserved: Dict[int, int],
    close: bool,
) -> Tuple[List[int], float]:
    """Construct then improve. Returns the assembled order and its drive time."""
    length = len(stop_indices)
    free = [idx for idx in stop_indices if idx not in reserved.values()]

    order = _nearest_neighbour(free, matrix, origin, reserved, length)
    order, _ = _two_opt(order, matrix, origin, reserved, length, close)
    order, _ = _or_opt(order, matrix, origin, reserved, length, close)
    # A relocation can expose a fresh crossing, so one more uncrossing pass is
    # worth its microseconds.
    order, _ = _two_opt(order, matrix, origin, reserved, length, close)

    assembled = _assemble(order, reserved, length)
    return assembled, _route_cost(assembled, matrix, origin, close)


def plan_route(
    stops: Sequence[RoutePoint],
    start: Optional[RoutePoint] = None,
    return_to_start: bool = False,
    road_factor: float = DEFAULT_ROAD_FACTOR,
    speed_kmh: float = DEFAULT_SPEED_KMH,
    default_visit_minutes: int = DEFAULT_VISIT_MINUTES,
    osrm_provider: Optional[Callable[[Sequence[RoutePoint]], Optional[CostMatrix]]] = None,
    matrix: Optional[CostMatrix] = None,
) -> RouteResult:
    """Order ``stops`` into the fastest route and cost it.

    Args:
        stops: the places to visit. Order is only meaningful for stops marked
            ``locked``, whose position number is preserved.
        start: where the day begins — a branch, home, or the rep's live
            position. When ``None`` the solver tries every stop as the opening
            one and keeps the best, which is the right answer for "I'll be
            somewhere in Maadi anyway".
        return_to_start: close the loop back to ``start``. Ignored without a
            start point, since there is nowhere to return to.
        matrix: precomputed costs, mainly for tests and for re-costing an order
            the user dragged into place by hand.

    Returns a :class:`RouteResult` whose ``order`` indexes ``stops``.

    Raises:
        ValueError: more than :data:`MAX_STOPS` stops.
    """
    stops = list(stops)
    if len(stops) > MAX_STOPS:
        raise ValueError(f"A route may hold at most {MAX_STOPS} stops; got {len(stops)}.")

    if not stops:
        return RouteResult(order=[], engine=(matrix.engine if matrix else "haversine"))

    points: List[RoutePoint] = ([start] if start else []) + stops
    if matrix is None:
        matrix = build_matrix(points, road_factor, speed_kmh, osrm_provider)

    offset = 1 if start else 0
    stop_indices = list(range(offset, offset + len(stops)))

    # Position numbers are over the STOPS, so a lock reserves position p in the
    # visiting sequence regardless of whether a start point exists.
    reserved = {
        position: offset + position
        for position, stop in enumerate(stops)
        if stop.locked
    }

    close = bool(return_to_start and start is not None)

    if start is not None:
        assembled, drive_s = _solve(matrix, stop_indices, 0, reserved, close)
    else:
        # No fixed origin: every stop is a candidate opener. n solves of an
        # n-stop problem is nothing at these sizes and removes the single
        # biggest source of a bad greedy start.
        best_assembled: Optional[List[int]] = None
        best_cost = float("inf")
        if 0 in reserved:
            # The rep pinned what they open with; there is nothing to search.
            candidates = [reserved[0]]
        else:
            # A stop pinned to position 3 cannot also be the opener, so only
            # genuinely free stops are candidates.
            pinned_indices = set(reserved.values())
            candidates = [idx for idx in stop_indices if idx not in pinned_indices]
        for candidate in candidates:
            others = [idx for idx in stop_indices if idx != candidate]
            sub_reserved = {
                position - 1: idx
                for position, idx in reserved.items()
                if position > 0
            }
            tail, _ = _solve(matrix, others, candidate, sub_reserved, False)
            assembled_try = [candidate] + tail
            cost = _route_cost(assembled_try, matrix, None, False)
            if cost < best_cost:
                best_assembled, best_cost = assembled_try, cost
        assembled, drive_s = (best_assembled or stop_indices), best_cost

    legs: List[RouteLeg] = []
    previous = 0 if start else assembled[0]
    sequence_start = 0 if start else 1
    for position in range(sequence_start, len(assembled)):
        current = assembled[position]
        legs.append(
            RouteLeg(
                from_key=points[previous].key,
                to_key=points[current].key,
                distance_m=matrix.distances_m[previous][current],
                duration_s=matrix.durations_s[previous][current],
            )
        )
        previous = current
    if close:
        legs.append(
            RouteLeg(
                from_key=points[previous].key,
                to_key=points[0].key,
                distance_m=matrix.distances_m[previous][0],
                duration_s=matrix.durations_s[previous][0],
            )
        )

    total_distance = sum(leg.distance_m for leg in legs)
    service_s = sum(
        (stop.service_minutes or default_visit_minutes) * 60 for stop in stops
    )

    return RouteResult(
        order=[idx - offset for idx in assembled],
        legs=legs,
        total_distance_m=total_distance,
        total_drive_s=drive_s,
        total_service_s=float(service_s),
        engine=matrix.engine,
        note=matrix.note,
    )


def cost_fixed_order(
    stops: Sequence[RoutePoint],
    start: Optional[RoutePoint] = None,
    return_to_start: bool = False,
    road_factor: float = DEFAULT_ROAD_FACTOR,
    speed_kmh: float = DEFAULT_SPEED_KMH,
    default_visit_minutes: int = DEFAULT_VISIT_MINUTES,
    osrm_provider: Optional[Callable[[Sequence[RoutePoint]], Optional[CostMatrix]]] = None,
) -> RouteResult:
    """Cost ``stops`` exactly as given, optimising nothing.

    This is what runs after a rep drags a stop into a different slot: they have
    overruled the optimizer and the screen owes them honest totals for the
    order they chose, not a quiet re-shuffle back to the machine's preference.
    """
    stops = list(stops)
    if not stops:
        return RouteResult(order=[], engine="haversine")
    points = ([start] if start else []) + stops
    matrix = build_matrix(points, road_factor, speed_kmh, osrm_provider)
    pinned = [
        RoutePoint(
            key=stop.key,
            lat=stop.lat,
            lng=stop.lng,
            locked=True,
            service_minutes=stop.service_minutes,
            label=stop.label,
        )
        for stop in stops
    ]
    return plan_route(
        pinned,
        start=start,
        return_to_start=return_to_start,
        road_factor=road_factor,
        speed_kmh=speed_kmh,
        default_visit_minutes=default_visit_minutes,
        matrix=matrix,
    )

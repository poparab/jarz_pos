"""Talk to an OSRM routing server for real road distances.

OSRM (`project-osrm.org <https://project-osrm.org>`_) is the free, self-hosted
half of the routing story: given a set of coordinates it returns the driving
distance and duration between every pair. That is strictly better than the
straight-line estimate in :mod:`jarz_pos.services.route_planner` — it knows
where the bridges are — but it is a *network dependency*, and this app has been
bitten before by a feature that silently degrades when its dependency is away.

So the contract here is narrow and pessimistic:

* **Never raise into the caller.** Every failure returns ``None``, which the
  route planner reads as "use the estimate" and reports to the UI as such.
* **Fail fast, then stop trying.** A short timeout, and a circuit breaker that
  remembers a failure for :data:`_BREAKER_SECONDS` so a dead box costs one
  slow request per minute rather than one per plan.
* **All-or-nothing matrices.** OSRM returns ``null`` for a coordinate it cannot
  snap to a road. A matrix with holes in it is worse than no matrix, because
  the solver would route around a fabricated zero, so any hole rejects the
  whole response.

Coordinates go out as ``lng,lat``. This is the opposite of every other
coordinate pair in this codebase and the single most common way to break an
OSRM integration — a Cairo café sent lat-first lands in the Indian Ocean, and
the distances come back plausible-looking and wrong.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import frappe

from jarz_pos.services.route_planner import CostMatrix, RoutePoint

#: How long a failure suppresses further attempts.
_BREAKER_SECONDS = 60

#: Cache key for the breaker. Site-scoped by frappe.cache() automatically.
_BREAKER_KEY = "jarz_visit_osrm_down"

#: OSRM's own demo server caps table size at 100; a self-hosted one is
#: configurable but the default build is the same. We stay well under.
_MAX_TABLE_POINTS = 90

SETTINGS_DOCTYPE = "Jarz POS Settings"


def _logger():
    return frappe.logger("jarz_pos.visits", allow_site=True, file_count=5)


def _settings_value(fieldname: str, default: Any = None) -> Any:
    """One settings field, guarded.

    Reads through ``db.get_single_value`` rather than loading the Single: this
    runs on every plan optimisation and the document carries fifty-odd fields.
    """
    try:
        value = frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
    except Exception:
        return default
    if value in (None, ""):
        return default
    return value


def base_url() -> Optional[str]:
    """The configured OSRM root, or ``None`` when the feature is unconfigured.

    An unconfigured URL is the normal state on a fresh site and on any site
    whose operator has not stood a routing box up yet. It is not an error and
    must not be logged as one.
    """
    url = str(_settings_value("visit_osrm_base_url", "") or "").strip()
    if not url:
        return None
    return url.rstrip("/")


def engine_preference() -> str:
    """``auto`` | ``osrm`` | ``straight_line``.

    ``auto`` (the default) means "OSRM when it is configured and answering".
    ``straight_line`` is the kill switch: it needs no deploy and no restart,
    which is the point — if the routing box starts returning nonsense, an
    operator can take it out of the loop from Desk.
    """
    raw = str(_settings_value("visit_route_engine", "") or "auto").strip().lower()
    return raw if raw in ("auto", "osrm", "straight_line") else "auto"


def _timeout() -> float:
    try:
        seconds = float(_settings_value("visit_osrm_timeout_seconds", 5) or 5)
    except Exception:
        seconds = 5.0
    # A routing call sits inside a user-facing request. Anything above a few
    # seconds and the rep taps the button again, which is how you get two
    # concurrent optimisations of the same plan.
    return max(1.0, min(seconds, 15.0))


def _breaker_open() -> bool:
    try:
        return bool(frappe.cache().get_value(_BREAKER_KEY))
    except Exception:
        return False


def _trip_breaker(reason: str) -> None:
    try:
        frappe.cache().set_value(_BREAKER_KEY, "1", expires_in_sec=_BREAKER_SECONDS)
    except Exception:
        pass
    _logger().warning(f"OSRM unavailable, falling back for {_BREAKER_SECONDS}s: {reason}")


def reset_breaker() -> None:
    """Clear the failure memory. Used by the health check and by tests."""
    try:
        frappe.cache().delete_value(_BREAKER_KEY)
    except Exception:
        pass


def is_enabled() -> bool:
    """Whether an OSRM attempt should even be made right now."""
    if engine_preference() == "straight_line":
        return False
    if not base_url():
        return False
    return not _breaker_open()


def _coordinate_string(points: Sequence[RoutePoint]) -> str:
    """``lng,lat;lng,lat`` — OSRM's order, not ours. See the module docstring."""
    return ";".join(f"{point.lng:.6f},{point.lat:.6f}" for point in points)


def _get(path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One guarded GET against the configured server."""
    root = base_url()
    if not root:
        return None
    try:
        import requests

        response = requests.get(f"{root}{path}", params=params, timeout=_timeout())
        if response.status_code != 200:
            _trip_breaker(f"HTTP {response.status_code}")
            return None
        payload = response.json()
    except Exception as exc:
        _trip_breaker(f"{exc.__class__.__name__}: {exc}")
        return None

    if str(payload.get("code") or "").lower() != "ok":
        # A NoRoute / NoSegment answer is about *these coordinates*, not about
        # the server's health, so it must not trip the breaker — that would
        # punish every other plan for one unmappable café.
        _logger().info(f"OSRM {path} returned code={payload.get('code')}")
        return None
    return payload


def table(points: Sequence[RoutePoint]) -> Optional[CostMatrix]:
    """Road distance/duration matrix for ``points``, or ``None``.

    Returned in the same shape as :func:`route_planner.haversine_matrix` so the
    two are interchangeable at the call site — which is what makes the fallback
    a one-line decision instead of a branch through the whole solver.
    """
    if not points or not is_enabled():
        return None
    if len(points) > _MAX_TABLE_POINTS:
        _logger().info(
            f"OSRM table skipped: {len(points)} points exceeds {_MAX_TABLE_POINTS}"
        )
        return None

    payload = _get(
        f"/table/v1/driving/{_coordinate_string(points)}",
        {"annotations": "distance,duration"},
    )
    if not payload:
        return None

    distances = payload.get("distances")
    durations = payload.get("durations")
    size = len(points)
    if not _is_full_matrix(distances, size) or not _is_full_matrix(durations, size):
        _logger().info("OSRM table returned an incomplete matrix; using the estimate")
        return None

    return CostMatrix(
        distances_m=[[float(cell) for cell in row] for row in distances],
        durations_s=[[float(cell) for cell in row] for row in durations],
        engine="osrm",
    )


def _is_full_matrix(matrix: Any, size: int) -> bool:
    """A square matrix of ``size`` with no ``None`` cells."""
    if not isinstance(matrix, list) or len(matrix) != size:
        return False
    for row in matrix:
        if not isinstance(row, list) or len(row) != size:
            return False
        for cell in row:
            if cell is None or isinstance(cell, bool):
                return False
            try:
                float(cell)
            except (TypeError, ValueError):
                return False
    return True


def route_geometry(points: Sequence[RoutePoint]) -> Optional[List[List[float]]]:
    """The drawn road path through ``points`` in order, as ``[[lat, lng], ...]``.

    Purely cosmetic — the plan's totals come from :func:`table` — but a route
    drawn along the actual streets is the difference between a map a rep reads
    and a map they ignore. Returns ``None`` freely; the client falls back to
    straight segments between stops.

    Note the flip back to ``lat, lng`` on the way out: OSRM speaks GeoJSON,
    which is ``lng, lat``, and every consumer in this codebase is ``lat, lng``.
    """
    if len(points) < 2 or not is_enabled():
        return None

    payload = _get(
        f"/route/v1/driving/{_coordinate_string(points)}",
        {"overview": "simplified", "geometries": "geojson"},
    )
    if not payload:
        return None

    try:
        coordinates = payload["routes"][0]["geometry"]["coordinates"]
    except (KeyError, IndexError, TypeError):
        return None

    path: List[List[float]] = []
    for pair in coordinates:
        try:
            path.append([float(pair[1]), float(pair[0])])
        except (TypeError, ValueError, IndexError):
            return None
    return path or None


def health() -> Dict[str, Any]:
    """Is the routing box reachable? Answers without tripping the breaker.

    Exposed through the visits API so an operator can tell "my ETAs are
    estimates because OSRM is off" apart from "...because it is broken",
    which are the same symptom and different fixes.
    """
    root = base_url()
    if not root:
        return {"configured": False, "reachable": False, "engine": "straight_line",
                "reason": "No OSRM base URL configured in Jarz POS Settings."}

    preference = engine_preference()
    if preference == "straight_line":
        return {"configured": True, "reachable": None, "engine": "straight_line",
                "reason": "Route engine is pinned to straight-line in settings."}

    probe = [
        RoutePoint(key="a", lat=30.0444, lng=31.2357),
        RoutePoint(key="b", lat=30.0561, lng=31.2394),
    ]
    reset_breaker()
    matrix = table(probe)
    if matrix is None:
        return {"configured": True, "reachable": False, "engine": "straight_line",
                "reason": f"{root} did not return a usable matrix."}
    return {
        "configured": True,
        "reachable": True,
        "engine": "osrm",
        "reason": None,
        "probe_distance_m": matrix.distances_m[0][1],
    }

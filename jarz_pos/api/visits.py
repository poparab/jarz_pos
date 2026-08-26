"""Whitelisted endpoints for the B2B visit planner.

Transport only. Every rule lives one layer down — routing in
:mod:`jarz_pos.services.route_planner`, targeting and scoring in
:mod:`jarz_pos.services.visit_planning`, the road-distance dependency in
:mod:`jarz_pos.services.osrm_client` — so the whole feature can be exercised
from a bench console or a staging harness without going through HTTP. Same
shape as ``api/geo.py`` and ``api/journey.py``.

**Access.** Gated by ``crm._ensure_b2b_access`` (B2B Sales Rep or a manager),
then narrowed per document: a rep works on their own days, a manager on
anyone's. Reading is deliberately wider than writing — the pipeline board is
already fully visible to every rep, so hiding a colleague's route would be
theatre. Writing someone else's day is not.

**Every read is guarded on the DocType existing.** CI's logic gate runs
entirely pre-migrate, so an endpoint that assumed ``Jarz Visit Plan`` were
present would fail the run that ships it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, nowdate

from jarz_pos.api.crm import _ensure_b2b_access, _manager_roles
from jarz_pos.services import osrm_client, visit_planning
from jarz_pos.services.route_planner import MAX_STOPS, RoutePoint

PLAN_DOCTYPE = "Jarz Visit Plan"

#: Statuses a stop may be moved to from the field.
STOP_STATUSES = ("Planned", "Visited", "Skipped", "Cancelled")

#: Guard rail on the calendar feed, mirroring ``journey._CALENDAR_LIMIT``.
_LIST_LIMIT = 2000


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _logger():
    return frappe.logger("jarz_pos.visits", allow_site=True, file_count=5)


def planner_enabled() -> bool:
    """Whether the site has migrated the visit DocTypes. Guarded -> False."""
    try:
        return bool(frappe.db.exists("DocType", PLAN_DOCTYPE))
    except Exception:
        return False


def _ensure_planner() -> None:
    if not planner_enabled():
        frappe.throw(_("Visit planning is not available on this site yet."))


def _is_manager() -> bool:
    try:
        return bool(
            set(frappe.get_roles(frappe.session.user) or []).intersection(_manager_roles())
        )
    except Exception:
        return False


def _ensure_can_write(plan) -> None:
    """A rep owns their own days; a manager owns everyone's."""
    if _is_manager():
        return
    if plan.rep == frappe.session.user:
        return
    frappe.throw(
        _("This route belongs to {0}. Only a manager can change someone else's day.").format(
            plan.rep_name or plan.rep
        )
    )


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    text = str(value).strip().lower()
    return text not in ("0", "false", "no", "none")


def _parse_rows(value: Any) -> List[Dict[str, Any]]:
    """Frappe delivers list arguments as JSON strings; accept both shapes.

    The Flutter repository ``jsonEncode``s its payloads for exactly this
    reason — see ``leads.save_lead_contacts`` and ``mergeLeads``.
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            frappe.throw(_("Could not read the stop list."))
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        frappe.throw(_("Stops must be a list."))
    return [row for row in value if isinstance(row, dict)]


def _float_or_none(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _map_stop(row) -> Dict[str, Any]:
    return {
        "name": row.name,
        "idx": row.idx,
        "reference_doctype": row.reference_doctype,
        "reference_name": row.reference_name,
        "title": row.title,
        "branch_name": row.branch_name,
        "area": row.area,
        "status": row.status,
        "latitude": _float_or_none(row.latitude),
        "longitude": _float_or_none(row.longitude),
        "address": row.address,
        "phone": row.phone,
        "maps_url": row.maps_url,
        "planned_time": str(row.planned_time) if row.planned_time else None,
        "visit_minutes": int(row.visit_minutes or 0),
        "locked": bool(row.locked),
        "leg_km": _float_or_none(row.leg_km) or 0.0,
        "leg_minutes": int(row.leg_minutes or 0),
        "arrived_at": str(row.arrived_at) if row.arrived_at else None,
        "outcome": row.outcome,
        "journey_note": row.journey_note,
    }


def _map_plan(plan, with_stops: bool = True) -> Dict[str, Any]:
    payload = {
        "name": plan.name,
        "visit_date": str(plan.visit_date) if plan.visit_date else None,
        "rep": plan.rep,
        "rep_name": plan.rep_name,
        "title": plan.title,
        "status": plan.status,
        "start_mode": plan.start_mode,
        "start_label": plan.start_label,
        "start_latitude": _float_or_none(plan.start_latitude),
        "start_longitude": _float_or_none(plan.start_longitude),
        "planned_start_time": (
            str(plan.planned_start_time) if plan.planned_start_time else None
        ),
        "default_visit_minutes": int(plan.default_visit_minutes or 0),
        "return_to_start": bool(plan.return_to_start),
        "total_stops": int(plan.total_stops or 0),
        "total_distance_km": _float_or_none(plan.total_distance_km) or 0.0,
        "total_drive_minutes": int(plan.total_drive_minutes or 0),
        "total_duration_minutes": int(plan.total_duration_minutes or 0),
        "route_engine": plan.route_engine or "haversine",
        "optimized_on": str(plan.optimized_on) if plan.optimized_on else None,
        "notes": plan.notes,
        "can_edit": _is_manager() or plan.rep == frappe.session.user,
    }
    if with_stops:
        payload["stops"] = [_map_stop(row) for row in (plan.get("stops") or [])]
        counts = {"planned": 0, "visited": 0, "skipped": 0, "cancelled": 0}
        for row in plan.get("stops") or []:
            counts[str(row.status or "Planned").lower()] = (
                counts.get(str(row.status or "Planned").lower(), 0) + 1
            )
        payload["counts"] = counts
    return payload


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_visit_plans(from_date=None, to_date=None, scope="mine", status=None):
    """Visit plans in a date range, for the calendar and the plan list.

    Args:
        from_date/to_date: ISO ``yyyy-mm-dd``. Default to the current month,
            matching ``journey.get_action_calendar`` so the two feeds can be
            drawn on one grid.
        scope: ``"mine"`` (default) or ``"all"``.
        status: optional single status filter.

    Summary rows only — no stops. A month of routes with every door attached
    would be a large payload for a screen that draws chips.
    """
    _ensure_b2b_access()
    if not planner_enabled():
        return {"plans": [], "count": 0, "from_date": from_date, "to_date": to_date}

    start, end = _month_range(from_date, to_date)
    filters: Dict[str, Any] = {"visit_date": ["between", [start, end]]}
    if str(scope or "").strip().lower() != "all":
        filters["rep"] = frappe.session.user
    if status:
        filters["status"] = status

    rows = frappe.get_all(
        PLAN_DOCTYPE,
        filters=filters,
        fields=[
            "name", "visit_date", "rep", "rep_name", "title", "status",
            "total_stops", "total_distance_km", "total_drive_minutes",
            "total_duration_minutes", "route_engine", "planned_start_time",
        ],
        order_by="visit_date asc, name asc",
        limit_page_length=_LIST_LIMIT,
    ) or []

    plans = []
    for row in rows:
        plans.append({
            "name": row["name"],
            "visit_date": str(row["visit_date"]) if row.get("visit_date") else None,
            "rep": row.get("rep"),
            "rep_name": row.get("rep_name"),
            "title": row.get("title"),
            "status": row.get("status"),
            "total_stops": int(row.get("total_stops") or 0),
            "total_distance_km": float(row.get("total_distance_km") or 0),
            "total_drive_minutes": int(row.get("total_drive_minutes") or 0),
            "total_duration_minutes": int(row.get("total_duration_minutes") or 0),
            "route_engine": row.get("route_engine") or "haversine",
            "planned_start_time": (
                str(row["planned_start_time"]) if row.get("planned_start_time") else None
            ),
            "can_edit": _is_manager() or row.get("rep") == frappe.session.user,
        })
    return {
        "plans": plans,
        "count": len(plans),
        "from_date": start,
        "to_date": end,
        "scope": "all" if str(scope or "").lower() == "all" else "mine",
    }


def _month_range(from_date, to_date):
    start = str(from_date or "").strip()
    end = str(to_date or "").strip()
    if start and end:
        return start, end
    today = nowdate()
    try:
        from frappe.utils import get_first_day, get_last_day

        return start or str(get_first_day(today)), end or str(get_last_day(today))
    except Exception:
        return start or str(today), end or str(today)


@frappe.whitelist()
def get_visit_plan(name, with_geometry=0):
    """One plan with its ordered stops.

    ``with_geometry`` asks OSRM for the drawn road path through the stops. Off
    by default because it is a second network call whose only job is to make
    the map prettier — the route, the order and the totals are all present
    without it.
    """
    _ensure_b2b_access()
    _ensure_planner()
    plan = frappe.get_doc(PLAN_DOCTYPE, name)
    payload = _map_plan(plan)

    if _bool(with_geometry):
        payload["geometry"] = _plan_geometry(plan)
    return payload


def _plan_geometry(plan) -> Optional[List[List[float]]]:
    """Road path through the plan, or ``None`` to let the client draw straight legs."""
    points: List[RoutePoint] = []
    start = visit_planning.start_point_for(plan)
    if start:
        points.append(start)
    for row in plan.get("stops") or []:
        if row.status in ("Cancelled",):
            continue
        lat = _float_or_none(row.latitude)
        lng = _float_or_none(row.longitude)
        if lat and lng:
            points.append(RoutePoint(key=row.name, lat=lat, lng=lng))
    if len(points) < 2:
        return None
    try:
        return osrm_client.route_geometry(points)
    except Exception:
        _logger().error("visit geometry failed", exc_info=True)
        return None


@frappe.whitelist()
def get_route_engine_status():
    """Which engine is answering, and why.

    Exists so a rep can tell "these are estimates because we have no routing
    server" from "these are estimates because the routing server is down" —
    identical symptoms, completely different fixes.
    """
    _ensure_b2b_access()
    try:
        status = osrm_client.health()
    except Exception:
        _logger().error("route engine health failed", exc_info=True)
        status = {
            "configured": False,
            "reachable": False,
            "engine": "straight_line",
            "reason": "Health check failed; see the error log.",
        }
    config = visit_planning.route_config()
    status.update({
        "road_factor": config.road_factor,
        "avg_speed_kmh": config.speed_kmh,
        "default_visit_minutes": config.visit_minutes,
        "max_stops": config.max_stops,
        "day_minutes": config.day_minutes,
        "visit_days": visit_planning.visit_days(),
    })
    return status


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_visit_targets(category=None, tier=None, area=None, specialty_only=0,
                      min_fit_score=0, include_customers=1, limit=500):
    """Rankable doors matching a coarse filter, best first.

    One row per BRANCH, not per brand: the catalog's 2,396 brands hold ~2,970
    located doors, and a route visits doors. ``limit`` keeps the payload sane —
    this endpoint answers "who should I go and see", and nobody scrolls 3,000
    of anything.
    """
    _ensure_b2b_access()
    targets = visit_planning.lead_targets(
        category=category,
        tier=tier,
        area=area,
        specialty_only=_bool(specialty_only),
        min_fit_score=float(min_fit_score or 0),
    )
    if _bool(include_customers):
        targets.extend(visit_planning.customer_targets(area=area))
    targets.sort(key=lambda t: t.priority, reverse=True)
    capped = targets[: max(1, int(limit or 500))]
    return {
        "targets": [target.as_dict() for target in capped],
        "count": len(capped),
        "total_matching": len(targets),
    }


@frappe.whitelist()
def suggest_visit_plan(visit_date=None, max_stops=None, start_latitude=None,
                       start_longitude=None, radius_km=None, category=None,
                       tier=None, area=None, specialty_only=0, min_fit_score=0,
                       include_customers=1, day_minutes=None):
    """Propose a day's route without saving anything.

    This is the part that makes a 2,900-door corpus workable: rather than
    hand-picking, the planner takes what is *due* (overdue follow-ups, doors
    nobody has walked into for months) crossed with what is *good* (fit score,
    pipeline stage), clusters it geographically, and orders it.

    Returns the proposal plus its reasoning, so the rep can argue with it.
    Nothing is written until :func:`create_visit_plan` is called with the
    targets they kept.
    """
    _ensure_b2b_access()

    anchor = None
    lat = _float_or_none(start_latitude)
    lng = _float_or_none(start_longitude)
    if lat and lng:
        anchor = (lat, lng)

    proposal = visit_planning.suggest(
        max_stops=int(max_stops) if max_stops else None,
        anchor=anchor,
        radius_km=float(radius_km) if radius_km else None,
        category=category,
        tier=tier,
        area=area,
        specialty_only=_bool(specialty_only),
        min_fit_score=float(min_fit_score or 0),
        include_customers=_bool(include_customers),
        day_minutes=int(day_minutes) if day_minutes else None,
    )
    proposal["visit_date"] = str(visit_date or nowdate())
    return proposal


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _apply_plan_fields(plan, values: Dict[str, Any]) -> None:
    """Copy the writable header fields a client may set."""
    simple = (
        "title", "status", "notes", "start_mode", "start_label",
        "planned_start_time",
    )
    for fieldname in simple:
        if fieldname in values and values[fieldname] is not None:
            plan.set(fieldname, values[fieldname])

    if values.get("visit_date"):
        plan.visit_date = getdate(values["visit_date"])
    if values.get("rep"):
        plan.rep = values["rep"]
    for fieldname in ("start_latitude", "start_longitude"):
        if fieldname in values:
            plan.set(fieldname, _float_or_none(values[fieldname]) or 0.0)
    if "default_visit_minutes" in values and values["default_visit_minutes"] is not None:
        plan.default_visit_minutes = int(values["default_visit_minutes"] or 0)
    if "return_to_start" in values:
        plan.return_to_start = 1 if _bool(values["return_to_start"]) else 0


def _stop_row(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one client-supplied stop into child-row fields.

    Coordinates are taken from the payload and stored on the row rather than
    resolved from the lead at read time. The catalog importer rewrites the
    branch table wholesale on every re-import, so a stop that looked its pin up
    later could move between planning and driving.
    """
    reference_doctype = str(payload.get("reference_doctype") or "Lead").strip()
    if reference_doctype not in ("Lead", "Customer", "Opportunity"):
        frappe.throw(_("A stop must reference a Lead, Customer or Opportunity."))
    reference_name = str(payload.get("reference_name") or "").strip()
    if not reference_name:
        frappe.throw(_("A stop needs a target."))

    return {
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "title": payload.get("title") or "",
        "branch_name": payload.get("branch_name") or "",
        "area": payload.get("area") or "",
        "status": (
            payload.get("status")
            if payload.get("status") in STOP_STATUSES
            else "Planned"
        ),
        "latitude": _float_or_none(payload.get("latitude")) or 0.0,
        "longitude": _float_or_none(payload.get("longitude")) or 0.0,
        "address": payload.get("address") or "",
        "phone": payload.get("phone") or "",
        "maps_url": payload.get("maps_url") or "",
        "visit_minutes": int(payload.get("visit_minutes") or 0),
        "locked": 1 if _bool(payload.get("locked")) else 0,
        "outcome": payload.get("outcome") or "",
    }


@frappe.whitelist()
def create_visit_plan(visit_date=None, title=None, rep=None, stops=None,
                      start_latitude=None, start_longitude=None, start_label=None,
                      start_mode=None, planned_start_time=None,
                      default_visit_minutes=None, return_to_start=0,
                      optimize=1, status=None, notes=None):
    """Create a route for a day.

    ``rep`` defaults to the caller. A manager may plan someone else's day; a
    rep may not, which is checked *after* the document exists so the error
    names the owner.

    ``optimize`` (default on) orders the stops before the first save. Turning
    it off preserves the order given, which is what the "accept my own
    sequence" path in the app sends.
    """
    _ensure_b2b_access()
    _ensure_planner()

    rows = _parse_rows(stops)
    if len(rows) > MAX_STOPS:
        frappe.throw(
            _("A route may hold at most {0} stops; {1} were sent.").format(MAX_STOPS, len(rows))
        )

    config = visit_planning.route_config()
    plan = frappe.new_doc(PLAN_DOCTYPE)
    plan.rep = rep or frappe.session.user
    plan.visit_date = getdate(visit_date or nowdate())
    plan.status = status or "Draft"
    plan.default_visit_minutes = int(default_visit_minutes or config.visit_minutes)
    plan.planned_start_time = planned_start_time or visit_planning.default_start_time()
    _apply_plan_fields(plan, {
        "title": title,
        "notes": notes,
        "start_mode": start_mode,
        "start_label": start_label,
        "start_latitude": start_latitude,
        "start_longitude": start_longitude,
        "return_to_start": return_to_start,
    })

    if rep and rep != frappe.session.user and not _is_manager():
        frappe.throw(_("Only a manager can plan another rep's day."))

    for row in rows:
        plan.append("stops", _stop_row(row))

    plan.insert()
    if _bool(optimize) and plan.get("stops"):
        _optimize(plan, reorder=True)
        plan.save()
    else:
        _optimize(plan, reorder=False)
        plan.save()
    return _map_plan(plan)


@frappe.whitelist()
def update_visit_plan(name, visit_date=None, title=None, rep=None, status=None,
                      notes=None, start_mode=None, start_label=None,
                      start_latitude=None, start_longitude=None,
                      planned_start_time=None, default_visit_minutes=None,
                      return_to_start=None):
    """Change a plan's header fields. Stops are set through :func:`set_visit_stops`.

    Parameters are spelled out rather than collected with ``**kwargs``: Frappe
    binds form keys to parameter *names*, and a signature that swallows
    everything also swallows ``cmd`` and any stray key a caller sends. Naming
    them is also the only place the writable surface is documented.
    """
    _ensure_b2b_access()
    _ensure_planner()
    plan = frappe.get_doc(PLAN_DOCTYPE, name)
    _ensure_can_write(plan)

    if rep and rep != plan.rep and not _is_manager():
        frappe.throw(_("Only a manager can hand a route to another rep."))

    values = {
        "visit_date": visit_date, "title": title, "rep": rep, "status": status,
        "notes": notes, "start_mode": start_mode, "start_label": start_label,
        "planned_start_time": planned_start_time,
        "default_visit_minutes": default_visit_minutes,
    }
    # Only pass through the keys the caller actually sent: _apply_plan_fields
    # treats a present-but-None coordinate as "clear it".
    if start_latitude is not None:
        values["start_latitude"] = start_latitude
    if start_longitude is not None:
        values["start_longitude"] = start_longitude
    if return_to_start is not None:
        values["return_to_start"] = return_to_start

    _apply_plan_fields(plan, values)
    # Moving the start point invalidates every leg, so re-cost without
    # reordering: the rep's sequence is theirs until they ask for a re-optimise.
    _optimize(plan, reorder=False)
    plan.save()
    return _map_plan(plan)


@frappe.whitelist()
def set_visit_stops(name, stops, optimize=0):
    """Replace the whole stop list — add, remove and reorder in one write.

    Whole-list rather than per-stop deliberately: the client already holds the
    route as an ordered list and a drag reorders several rows at once. Sending
    the list is one atomic write; sending five moves is a race with whoever
    else has the plan open.

    Rows carrying a ``name`` keep their identity — and with it their check-in,
    outcome and journey note. That is what lets a rep reorder a half-driven day
    without losing what they already recorded.
    """
    _ensure_b2b_access()
    _ensure_planner()
    plan = frappe.get_doc(PLAN_DOCTYPE, name)
    _ensure_can_write(plan)

    rows = _parse_rows(stops)
    if len(rows) > MAX_STOPS:
        frappe.throw(
            _("A route may hold at most {0} stops; {1} were sent.").format(MAX_STOPS, len(rows))
        )

    existing = {row.name: row for row in (plan.get("stops") or [])}
    rebuilt = []
    for payload in rows:
        row_name = payload.get("name")
        if row_name and row_name in existing:
            row = existing[row_name]
            row.update(_stop_row(payload))
            rebuilt.append(row)
        else:
            rebuilt.append(plan.append("stops", _stop_row(payload)))

    # append() already put the new rows on the table; rebuild it in the order
    # the client sent so row order is exactly the visiting order.
    plan.stops = rebuilt
    _optimize(plan, reorder=_bool(optimize))
    plan.save()
    return _map_plan(plan)


@frappe.whitelist()
def optimize_visit_plan(name, start_latitude=None, start_longitude=None):
    """Reorder the stops into the fastest sequence and re-cost the day.

    ``start_latitude``/``start_longitude`` let the phone hand in the rep's live
    position without persisting it as the plan's fixed start — "start from
    where I am right now" is the common case and it is different every morning.
    """
    _ensure_b2b_access()
    _ensure_planner()
    plan = frappe.get_doc(PLAN_DOCTYPE, name)
    _ensure_can_write(plan)

    lat = _float_or_none(start_latitude)
    lng = _float_or_none(start_longitude)
    if lat and lng:
        plan.start_latitude = lat
        plan.start_longitude = lng
        if not plan.start_label:
            plan.start_label = _("Current location")

    result = _optimize(plan, reorder=True)
    plan.save()
    payload = _map_plan(plan)
    payload["engine_note"] = result.note
    return payload


def _optimize(plan, reorder: bool):
    """Run the router over a plan and stamp the answer onto its rows."""
    result, points = visit_planning.route_plan(plan, optimize=reorder)
    visit_planning.apply_route(plan, result, points, reorder=reorder)
    return result


@frappe.whitelist()
def set_visit_stop_status(name, stop, status, outcome=None, log_note=0,
                          note_text=None, next_action=None, next_action_date=None):
    """Check in (or out of) one stop, optionally writing the diary entry.

    A visit that produces no record is a visit that did not happen as far as
    the pipeline is concerned, so ``log_note`` writes a real
    ``Jarz Journey Note`` of type Visit against the same target — the entry the
    lead page, the B2B account screen and the follow-up reminders all already
    read. The note is linked back onto the stop so the route stays the index of
    the day.

    Note writing is best-effort on purpose: a rep standing in a café with one
    bar of signal must be able to mark a stop Visited even if the diary write
    fails. Losing the check-in to protect the note would be the wrong trade.
    """
    _ensure_b2b_access()
    _ensure_planner()
    if status not in STOP_STATUSES:
        frappe.throw(_("Unknown stop status: {0}").format(status))

    plan = frappe.get_doc(PLAN_DOCTYPE, name)
    _ensure_can_write(plan)

    row = next((r for r in (plan.get("stops") or []) if r.name == stop), None)
    if row is None:
        frappe.throw(_("That stop is not on this route."))

    row.status = status
    if outcome is not None:
        row.outcome = outcome
    if status == "Visited" and not row.arrived_at:
        row.arrived_at = now_datetime()
    if status == "Planned":
        row.arrived_at = None

    note_name = None
    if _bool(log_note) and status == "Visited":
        note_name = _log_visit_note(row, note_text, next_action, next_action_date)
        if note_name:
            row.journey_note = note_name

    # A day whose stops are all resolved is a day that is over.
    if plan.status in ("Draft", "Planned", "In Progress"):
        outstanding = [
            r for r in (plan.get("stops") or []) if r.status in ("Planned",)
        ]
        if not outstanding and plan.get("stops"):
            plan.status = "Completed"
        elif plan.status in ("Draft", "Planned"):
            plan.status = "In Progress"

    plan.save()
    payload = _map_plan(plan)
    payload["journey_note"] = note_name
    return payload


def _log_visit_note(row, note_text, next_action, next_action_date) -> Optional[str]:
    """Write the diary entry for a completed stop. Never raises."""
    try:
        from jarz_pos.api.journey import add_journey_note, journey_enabled

        if not journey_enabled():
            return None
        result = add_journey_note(
            reference_doctype=row.reference_doctype,
            reference_name=row.reference_name,
            entry_type="Visit",
            note=note_text or row.outcome or _("Visited on the planned route."),
            outcome=row.outcome or None,
            next_action=next_action or None,
            next_action_date=next_action_date or None,
        )
        return result.get("name") if isinstance(result, dict) else None
    except Exception:
        _logger().error(
            f"visit note failed for {row.reference_doctype} {row.reference_name}",
            exc_info=True,
        )
        return None


@frappe.whitelist()
def delete_visit_plan(name):
    """Remove a route. Journey notes it produced are left alone.

    A visit that happened, happened — deleting the day's plan must not erase
    the record of the calls made on it.
    """
    _ensure_b2b_access()
    _ensure_planner()
    plan = frappe.get_doc(PLAN_DOCTYPE, name)
    _ensure_can_write(plan)
    frappe.delete_doc(PLAN_DOCTYPE, name, ignore_permissions=False)
    return {"success": True, "name": name}


@frappe.whitelist()
def add_stops_to_plan(name, stops, optimize=1):
    """Append doors to an existing route — the "add these to Saturday" path.

    Duplicate doors are dropped by the document's own normalisation rather than
    here, so the same rule applies however a stop arrives.
    """
    _ensure_b2b_access()
    _ensure_planner()
    plan = frappe.get_doc(PLAN_DOCTYPE, name)
    _ensure_can_write(plan)

    rows = _parse_rows(stops)
    if len(plan.get("stops") or []) + len(rows) > MAX_STOPS:
        frappe.throw(
            _("A route may hold at most {0} stops.").format(MAX_STOPS)
        )
    for payload in rows:
        plan.append("stops", _stop_row(payload))

    _optimize(plan, reorder=_bool(optimize))
    plan.save()
    return _map_plan(plan)

"""Turn a catalog of prospects into a driveable day.

This is the layer that knows about Frappe. :mod:`route_planner` knows only
geometry; :mod:`osrm_client` knows only HTTP. Here is where a Lead's branch
rows, a Customer's address pin, the diary of past visits and the follow-up
calendar all become :class:`~jarz_pos.services.route_planner.RoutePoint` objects
with a reason to be visited.

Three jobs:

* **Targets.** A visit is to a *door*, not to a brand. One Lead with six
  branches is six candidate stops, only one of which is on today's route, and
  each carries its own coordinates. Customers join the same pool through their
  primary address, because a rep passing an active account on the way to a
  prospect should stop in.

* **Priority.** With 2,900-odd doors in the corpus, "which ones" is a harder
  question than "in what order". A target's score blends how good a fit it is
  (the catalog's own number) with how *overdue* it is — an overdue follow-up
  and a lead nobody has walked into for four months both outrank a fresh
  high-fit name, because the first two are decaying and the third is not.

* **Selection.** A day is a geographic problem before it is a scheduling one:
  nine stops in Maadi beat nine of the best-scoring leads in the country. The
  builder grows a cluster around a seed, trading score against detour, then
  hands the chosen set to the optimiser and trims until the day actually fits
  between the start time and the end of the working day.

Nothing here writes a document. Persisting a suggestion is the caller's choice
— a rep should see the proposed day before it becomes a plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import frappe
from frappe.utils import date_diff, getdate, nowdate

from jarz_pos.services import osrm_client
from jarz_pos.services.route_planner import (
    DEFAULT_ROAD_FACTOR,
    DEFAULT_SPEED_KMH,
    DEFAULT_VISIT_MINUTES,
    MAX_STOPS,
    RoutePoint,
    RouteResult,
    cost_fixed_order,
    haversine_m,
    plan_route,
)

SETTINGS_DOCTYPE = "Jarz POS Settings"

#: Stage bonuses. A prospect mid-cycle is worth more of the day than an
#: untouched name: the sample is already out, the momentum is real, and a
#: stalled Sample is the single most expensive thing in the pipeline to leave
#: alone.
STAGE_PRIORITY = {
    "Lead": 0.0,
    "Qualify": 20.0,
    "Sample": 30.0,
    "Approved": 25.0,
    "Trial": 25.0,
    "Check-up": 20.0,
    "Active": 10.0,
    "Lost/On-hold": -40.0,
}

#: Never-visited beats long-ago-visited: an unopened door is unknown, and
#: unknown is where the upside is.
NEVER_VISITED_BONUS = 40.0

#: An overdue promise outranks everything else on the board. A rep who said
#: "I'll come back Tuesday" and did not is a lost deal in slow motion.
OVERDUE_BONUS = 50.0
DUE_SOON_BONUS = 25.0
DUE_SOON_DAYS = 7

#: How hard a detour is punished when growing a cluster, in score points per
#: kilometre from the cluster's centre. Tuned so a target has to be roughly
#: twice as good to justify being 10 km further out.
DETOUR_PENALTY_PER_KM = 4.0

#: Ceiling on the candidate pool handed to the cluster builder. The corpus is
#: ~2,900 doors; scoring all of them is cheap, but the O(n) growth loop runs
#: once per chosen stop, so the pool is trimmed to the best of them first.
CANDIDATE_POOL = 400

#: Ceiling on the visit-history rows read in one pass. The diary is one row per
#: real touch, so the whole table is small next to the catalog — but an
#: unbounded read on a table that only ever grows is the kind of thing that is
#: fine for two years and then is not.
VISIT_HISTORY_LIMIT = 20000


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RouteConfig:
    """Everything the optimiser needs, resolved from settings once per call."""

    road_factor: float = DEFAULT_ROAD_FACTOR
    speed_kmh: float = DEFAULT_SPEED_KMH
    visit_minutes: int = DEFAULT_VISIT_MINUTES
    max_stops: int = 12
    day_minutes: int = 360
    stale_days: int = 60
    use_osrm: bool = True

    def osrm_provider(self):
        """The provider handed to the matrix builder, or ``None`` to skip OSRM."""
        if not self.use_osrm:
            return None
        return osrm_client.table


def _single(fieldname: str, default: Any) -> Any:
    try:
        value = frappe.db.get_single_value(SETTINGS_DOCTYPE, fieldname)
    except Exception:
        return default
    if value in (None, ""):
        return default
    return value


def _float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _int(value: Any, default: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def route_config() -> RouteConfig:
    """Read the tunables from Desk, falling back to the module defaults.

    Every one of these is an operator dial rather than a constant because they
    are all *local* facts: Cairo traffic, how long a rep actually spends in a
    café, how long a working day is. A code change to adjust them would be
    absurd.
    """
    return RouteConfig(
        road_factor=_float(_single("visit_road_factor", None), DEFAULT_ROAD_FACTOR),
        speed_kmh=_float(_single("visit_avg_speed_kmh", None), DEFAULT_SPEED_KMH),
        visit_minutes=_int(_single("visit_default_minutes", None), DEFAULT_VISIT_MINUTES),
        max_stops=_int(_single("visit_max_stops", None), 12),
        day_minutes=_int(_single("visit_day_minutes", None), 360),
        stale_days=_int(_single("visit_stale_days", None), 60),
        use_osrm=osrm_client.is_enabled(),
    )


def default_start_time() -> str:
    """When the working day starts, as ``HH:MM:SS``.

    A plan created without one gets this; a rep who leaves at 08:00 on Saturdays
    changes it once in Desk rather than on every route.
    """
    raw = _single("visit_default_start_time", None)
    return str(raw) if raw else "10:00:00"


def visit_days() -> List[str]:
    """Weekday names the team does field visits on. Empty means "any day"."""
    raw = str(_single("visit_days", "") or "").strip()
    if not raw:
        return []
    valid = {
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    }
    days = []
    for part in raw.replace(";", ",").split(","):
        name = part.strip().capitalize()
        if name.lower() in valid and name not in days:
            days.append(name)
    return days


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass
class VisitTarget:
    """A door that could be visited, with everything needed to rank and route it."""

    reference_doctype: str
    reference_name: str
    title: str
    branch_name: str = ""
    area: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    address: str = ""
    phone: str = ""
    maps_url: str = ""
    # Ranking inputs
    fit_score: float = 0.0
    stage: str = ""
    tier: str = ""
    category: str = ""
    is_specialty: bool = False
    last_visit_date: Optional[str] = None
    days_since_visit: Optional[int] = None
    next_followup_date: Optional[str] = None
    followup_overdue: bool = False
    priority: float = 0.0
    reasons: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable identity for one door across a planning call.

        Keyed on POSITION, not on the branch label. Chains name every branch
        after the chain: production carries 7 distinct T-LAB locations all
        called "T-LAB", and 114 of its 2,645 doors share a name with a
        different door. Keying on the label collapsed them, so a rep who
        selected nine stops got eight and one real address vanished from the
        route with nothing to show it had.

        Five decimal places is about a metre — fine enough that two branches on
        the same street stay separate, coarse enough that two rows describing
        the SAME door (the corpus was scraped per location, so a handful of
        exact duplicates exist) collapse, which is what should happen.
        """
        return (
            f"{self.reference_doctype}:{self.reference_name}:"
            f"{self.latitude:.5f},{self.longitude:.5f}"
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "reference_doctype": self.reference_doctype,
            "reference_name": self.reference_name,
            "title": self.title,
            "branch_name": self.branch_name,
            "area": self.area,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "address": self.address,
            "phone": self.phone,
            "maps_url": self.maps_url,
            "fit_score": self.fit_score,
            "stage": self.stage,
            "tier": self.tier,
            "category": self.category,
            "is_specialty": self.is_specialty,
            "last_visit_date": self.last_visit_date,
            "days_since_visit": self.days_since_visit,
            "next_followup_date": self.next_followup_date,
            "followup_overdue": self.followup_overdue,
            "priority": round(self.priority, 1),
            "reasons": self.reasons,
        }


def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).get_field(fieldname))
    except Exception:
        return False


def _coord(value: Any) -> Optional[float]:
    """A usable coordinate, or ``None``.

    Rejects exact zero. (0, 0) is in the Atlantic and is what an unset Float
    column reads as, so treating it as a real location is how a route quietly
    becomes 5,000 km long.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed == 0.0 or not (-90.0 <= parsed <= 180.0):
        return None
    return parsed


def last_visit_map(reference_doctype: str, names: Sequence[str]) -> Dict[str, str]:
    """``{reference_name: last visit date}`` from the journey diary.

    ONE query for the whole set — the same discipline the leads catalog uses
    for contacts and journey summaries. Only ``Visit`` and ``Sample Drop``
    entries count: a phone call is a touch but it is not a visit, and treating
    it as one would let a rep who rings everybody look like they cover the
    ground.

    The obvious shape for this is ``max(entry_date)`` with a ``group_by``.
    Frappe v16 **refuses** a SQL function written as a string in ``fields``
    ("SQL functions are not allowed as strings in SELECT"), and because this
    lookup is best-effort the rejection was swallowed by the guard and every
    lead came back looking never-visited — inflating its priority by the
    never-visited bonus and putting long-dead doors at the top of every
    suggestion. Caught only by ``test_never_visited_scores_above_recently_
    visited``; nothing about it was visible at runtime.

    So the reduction happens in Python instead. The notes table is one row per
    real-world touch, which is small next to the catalog it describes.
    """
    result: Dict[str, str] = {}
    if not names:
        return result
    try:
        if not frappe.db.exists("DocType", "Jarz Journey Note"):
            return result
        rows = frappe.get_all(
            "Jarz Journey Note",
            filters={
                "reference_doctype": reference_doctype,
                "reference_name": ["in", list(names)],
                "entry_type": ["in", ["Visit", "Sample Drop"]],
            },
            fields=["reference_name", "entry_date"],
            order_by="entry_date desc",
            limit_page_length=VISIT_HISTORY_LIMIT,
        ) or []
    except Exception:
        frappe.log_error(
            title="visits: last visit lookup failed", message=frappe.get_traceback()
        )
        return result
    # Rows arrive newest first, so the first sighting of a reference is its
    # latest visit and later rows are older by construction.
    for row in rows:
        name = row.get("reference_name")
        if name and name not in result and row.get("entry_date"):
            result[name] = str(row["entry_date"])
    return result


def lead_targets(
    category: Optional[str] = None,
    tier: Optional[str] = None,
    area: Optional[str] = None,
    stages: Optional[Sequence[str]] = None,
    specialty_only: bool = False,
    min_fit_score: float = 0.0,
) -> List[VisitTarget]:
    """Every routable door on the lead catalog, coarsely filtered.

    Filtering is deliberately coarse and mirrors ``leads.get_leads``: the
    catalog's fine-grained filtering lives on the client, and the auto-planner
    only needs enough of a cut to keep the candidate pool honest.

    Excludes what the rest of the app excludes — leads marked not suitable and
    leads merged away as duplicates. Sending a rep to a door a colleague
    already rejected is the fastest way to make them stop trusting the planner.
    """
    filters: Dict[str, Any] = {}
    if category:
        filters["custom_lead_category"] = category
    if tier:
        filters["custom_fit_tier"] = tier
    if specialty_only and _has_field("Lead", "custom_is_specialty"):
        filters["custom_is_specialty"] = 1
    if _has_field("Lead", "custom_not_suitable"):
        filters["custom_not_suitable"] = 0
    if _has_field("Lead", "custom_merged_into"):
        filters["custom_merged_into"] = ["is", "not set"]
    if stages and _has_field("Lead", "custom_b2b_stage"):
        filters["custom_b2b_stage"] = ["in", list(stages)]

    fields = ["name", "lead_name", "company_name", "phone", "mobile_no"]
    for optional in (
        "custom_fit_score", "custom_fit_tier", "custom_b2b_stage",
        "custom_lead_category", "custom_primary_area", "custom_is_specialty",
        "custom_latitude", "custom_longitude", "custom_maps_url",
        "custom_next_followup_date", "custom_followup_done",
    ):
        if _has_field("Lead", optional):
            fields.append(optional)

    try:
        rows = frappe.get_all(
            "Lead", filters=filters or None, fields=fields, limit_page_length=0
        ) or []
    except Exception:
        frappe.log_error(
            title="visits: lead target query failed", message=frappe.get_traceback()
        )
        return []

    by_name = {row["name"]: row for row in rows}
    branches = _lead_branches(list(by_name.keys()))
    visits = last_visit_map("Lead", list(by_name.keys()))
    today = getdate(nowdate())

    targets: List[VisitTarget] = []
    for name, row in by_name.items():
        fit = float(row.get("custom_fit_score") or 0)
        if fit < min_fit_score:
            continue
        doors = branches.get(name) or []
        if not doors:
            # Fall back to the brand pin: a lead added by hand has no branch
            # rows, and it is still somewhere.
            lat = _coord(row.get("custom_latitude"))
            lng = _coord(row.get("custom_longitude"))
            if lat is None or lng is None:
                continue
            doors = [{
                "branch_name": "",
                "area": row.get("custom_primary_area") or "",
                "latitude": lat,
                "longitude": lng,
                "address": "",
                "phone": row.get("phone") or row.get("mobile_no") or "",
                "maps_url": row.get("custom_maps_url") or "",
            }]

        for door in doors:
            if area and str(door.get("area") or "").strip().lower() != area.strip().lower():
                continue
            target = VisitTarget(
                reference_doctype="Lead",
                reference_name=name,
                title=row.get("company_name") or row.get("lead_name") or name,
                branch_name=door.get("branch_name") or "",
                area=door.get("area") or row.get("custom_primary_area") or "",
                latitude=door["latitude"],
                longitude=door["longitude"],
                address=door.get("address") or "",
                phone=door.get("phone") or row.get("phone") or row.get("mobile_no") or "",
                maps_url=door.get("maps_url") or "",
                fit_score=fit,
                stage=row.get("custom_b2b_stage") or "",
                tier=row.get("custom_fit_tier") or "",
                category=row.get("custom_lead_category") or "",
                is_specialty=bool(row.get("custom_is_specialty")),
                last_visit_date=visits.get(name),
                next_followup_date=(
                    str(row.get("custom_next_followup_date"))
                    if row.get("custom_next_followup_date")
                    and not row.get("custom_followup_done")
                    else None
                ),
            )
            _score(target, today)
            targets.append(target)
    return targets


def _lead_branches(names: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    """``{lead: [door, ...]}`` — one query for the whole catalog.

    The branch child table is where the coordinates actually live. Every one of
    the corpus's ~2,970 rows carries a pin, which is what makes door-level
    routing possible at all.
    """
    result: Dict[str, List[Dict[str, Any]]] = {}
    if not names or not _has_field("Lead", "custom_branches"):
        return result
    try:
        rows = frappe.get_all(
            "Jarz Lead Branch",
            filters={"parenttype": "Lead", "parentfield": "custom_branches"},
            fields=[
                "parent", "idx", "branch_name", "area", "latitude", "longitude",
                "address", "phone", "maps_url",
            ],
            order_by="parent asc, idx asc",
            limit_page_length=0,
        ) or []
    except Exception:
        frappe.log_error(
            title="visits: branch lookup failed", message=frappe.get_traceback()
        )
        return result

    wanted = set(names)
    for row in rows:
        if row.get("parent") not in wanted:
            continue
        lat = _coord(row.get("latitude"))
        lng = _coord(row.get("longitude"))
        if lat is None or lng is None:
            continue
        result.setdefault(row["parent"], []).append({
            "branch_name": row.get("branch_name") or "",
            "area": row.get("area") or "",
            "latitude": lat,
            "longitude": lng,
            "address": row.get("address") or "",
            "phone": row.get("phone") or "",
            "maps_url": row.get("maps_url") or "",
        })
    return result


def customer_targets(area: Optional[str] = None) -> List[VisitTarget]:
    """Active B2B accounts, located through their primary address.

    Far fewer of these than leads, and they matter for a different reason: a
    check-up on a paying account that is drifting is worth more than a cold
    call, and it is usually two streets from one.
    """
    if not _has_field("Address", "custom_latitude"):
        return []
    try:
        customers = frappe.get_all(
            "Customer",
            filters={"disabled": 0, "customer_type": "Company"},
            fields=["name", "customer_name", "mobile_no", "territory"],
            limit_page_length=0,
        ) or []
    except Exception:
        frappe.log_error(
            title="visits: customer target query failed",
            message=frappe.get_traceback(),
        )
        return []
    if not customers:
        return []

    names = [row["name"] for row in customers]
    pins = _customer_pins(names)
    visits = last_visit_map("Customer", names)
    today = getdate(nowdate())

    targets: List[VisitTarget] = []
    for row in customers:
        pin = pins.get(row["name"])
        if not pin:
            continue
        if area and str(pin.get("area") or "").strip().lower() != area.strip().lower():
            continue
        target = VisitTarget(
            reference_doctype="Customer",
            reference_name=row["name"],
            title=row.get("customer_name") or row["name"],
            branch_name=pin.get("branch_name") or "",
            area=pin.get("area") or row.get("territory") or "",
            latitude=pin["latitude"],
            longitude=pin["longitude"],
            address=pin.get("address") or "",
            phone=pin.get("phone") or row.get("mobile_no") or "",
            stage="Active",
            last_visit_date=visits.get(row["name"]),
        )
        _score(target, today)
        targets.append(target)
    return targets


def _customer_pins(names: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """``{customer: pin}`` via the Dynamic Link table, one query per side.

    Preferring the primary address, then any located one. A customer with three
    addresses and a pin on only the warehouse still gets routed somewhere real.
    """
    result: Dict[str, Dict[str, Any]] = {}
    if not names:
        return result
    try:
        links = frappe.get_all(
            "Dynamic Link",
            filters={
                "link_doctype": "Customer",
                "link_name": ["in", list(names)],
                "parenttype": "Address",
            },
            fields=["parent", "link_name"],
            limit_page_length=0,
        ) or []
        if not links:
            return result
        addresses = frappe.get_all(
            "Address",
            filters={"name": ["in", [row["parent"] for row in links]]},
            fields=[
                "name", "address_line1", "city", "phone",
                "is_primary_address", "custom_latitude", "custom_longitude",
            ],
            limit_page_length=0,
        ) or []
    except Exception:
        frappe.log_error(
            title="visits: customer pin lookup failed", message=frappe.get_traceback()
        )
        return result

    by_address = {row["name"]: row for row in addresses}
    for link in links:
        address = by_address.get(link["parent"])
        if not address:
            continue
        lat = _coord(address.get("custom_latitude"))
        lng = _coord(address.get("custom_longitude"))
        if lat is None or lng is None:
            continue
        existing = result.get(link["link_name"])
        if existing and not address.get("is_primary_address"):
            continue
        result[link["link_name"]] = {
            "branch_name": address.get("address_line1") or "",
            "area": address.get("city") or "",
            "latitude": lat,
            "longitude": lng,
            "address": address.get("address_line1") or "",
            "phone": address.get("phone") or "",
        }
    return result


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def _score(target: VisitTarget, today) -> None:
    """Rank a door by how much it is worth a slot in the day.

    Written as additive bonuses with a human-readable ``reasons`` trail rather
    than one opaque formula: a rep who disagrees with the suggestion is
    entitled to see why it was made, and "overdue follow-up + not visited in
    134 days" is an argument, where "score 91.4" is not.
    """
    reasons: List[str] = []
    score = float(target.fit_score or 0)
    if score:
        reasons.append(f"fit {int(score)}")

    stage_bonus = STAGE_PRIORITY.get(target.stage or "", 0.0)
    if stage_bonus:
        score += stage_bonus
        reasons.append(f"stage {target.stage}")

    if target.last_visit_date:
        try:
            days = int(date_diff(today, getdate(target.last_visit_date)))
        except Exception:
            days = None
        target.days_since_visit = days
        if days is not None and days > 0:
            # Decay bonus, capped: at some point "very stale" stops getting
            # staler, and an uncapped term would let one forgotten lead
            # dominate every plan forever.
            score += min(days / 2.0, 40.0)
            if days >= 60:
                reasons.append(f"not visited in {days}d")
    else:
        score += NEVER_VISITED_BONUS
        reasons.append("never visited")

    if target.next_followup_date:
        try:
            due_in = int(date_diff(getdate(target.next_followup_date), today))
        except Exception:
            due_in = None
        if due_in is not None:
            if due_in < 0:
                target.followup_overdue = True
                score += OVERDUE_BONUS
                reasons.append(f"follow-up {abs(due_in)}d overdue")
            elif due_in <= DUE_SOON_DAYS:
                score += DUE_SOON_BONUS
                reasons.append("follow-up due soon")

    target.priority = score
    target.reasons = reasons


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_cluster(
    candidates: Sequence[VisitTarget],
    max_stops: int,
    anchor: Optional[Tuple[float, float]] = None,
    radius_km: Optional[float] = None,
) -> List[VisitTarget]:
    """Grow a geographically tight, high-value set of stops.

    The greedy rule is ``priority - DETOUR_PENALTY_PER_KM * km from the running
    centroid``. Seeding matters: with an anchor (the rep's location, or an area
    they picked) the seed is the best target near it; without one it is simply
    the best target anywhere, and the cluster forms around whatever that is.

    This is the step that makes 2,900 doors tractable. Ordering them comes
    after, and is the easy half.
    """
    pool = [t for t in candidates if t.latitude and t.longitude]
    if not pool:
        return []

    if anchor:
        anchored = []
        for target in pool:
            km = haversine_m(anchor[0], anchor[1], target.latitude, target.longitude) / 1000.0
            if radius_km is not None and km > radius_km:
                continue
            anchored.append((target, km))
        if not anchored:
            return []
        # Near AND good, not merely near: a 200-metre radius of dead leads is
        # a wasted Saturday.
        anchored.sort(key=lambda pair: pair[0].priority - DETOUR_PENALTY_PER_KM * pair[1], reverse=True)
        pool = [pair[0] for pair in anchored]
    else:
        pool.sort(key=lambda t: t.priority, reverse=True)

    pool = pool[:CANDIDATE_POOL]
    chosen: List[VisitTarget] = [pool[0]]
    remaining = pool[1:]
    centroid_lat, centroid_lng = pool[0].latitude, pool[0].longitude

    while remaining and len(chosen) < max_stops:
        best_target = None
        best_value = float("-inf")
        for target in remaining:
            km = haversine_m(centroid_lat, centroid_lng, target.latitude, target.longitude) / 1000.0
            value = target.priority - DETOUR_PENALTY_PER_KM * km
            if value > best_value:
                best_value, best_target = value, target
        if best_target is None:
            break
        remaining.remove(best_target)
        chosen.append(best_target)
        count = len(chosen)
        centroid_lat += (best_target.latitude - centroid_lat) / count
        centroid_lng += (best_target.longitude - centroid_lng) / count

    return chosen


def targets_to_points(
    targets: Sequence[VisitTarget], visit_minutes: int
) -> List[RoutePoint]:
    return [
        RoutePoint(
            key=target.key,
            lat=target.latitude,
            lng=target.longitude,
            service_minutes=visit_minutes,
            label=target.title,
        )
        for target in targets
    ]


def suggest(
    max_stops: Optional[int] = None,
    anchor: Optional[Tuple[float, float]] = None,
    radius_km: Optional[float] = None,
    category: Optional[str] = None,
    tier: Optional[str] = None,
    area: Optional[str] = None,
    stages: Optional[Sequence[str]] = None,
    specialty_only: bool = False,
    min_fit_score: float = 0.0,
    include_customers: bool = True,
    day_minutes: Optional[int] = None,
    config: Optional[RouteConfig] = None,
) -> Dict[str, Any]:
    """Propose a day: which doors, in what order, and why.

    Returns a preview, not a saved plan. The rep sees the reasoning, drops what
    they disagree with, and only then commits — which is the difference between
    a tool that helps and one that reads as an instruction from a machine.

    The trim loop at the end is the honest part: a route is only a plan if it
    fits in the day. Stops are dropped lowest-priority-first and the survivors
    re-optimised, because dropping the *last* stop of an optimised route is not
    the same as dropping the least valuable one.
    """
    config = config or route_config()
    limit = min(int(max_stops or config.max_stops), MAX_STOPS)
    budget = int(day_minutes or config.day_minutes)

    candidates = lead_targets(
        category=category,
        tier=tier,
        area=area,
        stages=stages,
        specialty_only=specialty_only,
        min_fit_score=min_fit_score,
    )
    if include_customers:
        candidates.extend(customer_targets(area=area))

    if not candidates:
        return {
            "targets": [],
            "order": [],
            "engine": "haversine",
            "total_distance_km": 0.0,
            "total_drive_minutes": 0,
            "total_duration_minutes": 0,
            "considered": 0,
            "dropped_for_time": 0,
            "note": "No routable targets matched those filters.",
        }

    chosen = select_cluster(candidates, limit, anchor=anchor, radius_km=radius_km)
    start_point = (
        RoutePoint(key="__start__", lat=anchor[0], lng=anchor[1], label="Start")
        if anchor
        else None
    )

    dropped = 0
    while chosen:
        result = plan_route(
            targets_to_points(chosen, config.visit_minutes),
            start=start_point,
            road_factor=config.road_factor,
            speed_kmh=config.speed_kmh,
            default_visit_minutes=config.visit_minutes,
            osrm_provider=config.osrm_provider(),
        )
        total_minutes = int(round(result.total_duration_s / 60.0))
        if total_minutes <= budget or len(chosen) == 1:
            ordered = [chosen[i] for i in result.order]
            return {
                "targets": [target.as_dict() for target in ordered],
                "order": result.order,
                "engine": result.engine,
                "engine_note": result.note,
                "total_distance_km": round(result.total_distance_m / 1000.0, 2),
                "total_drive_minutes": int(round(result.total_drive_s / 60.0)),
                "total_duration_minutes": total_minutes,
                "legs": [
                    {
                        "from": leg.from_key,
                        "to": leg.to_key,
                        "km": round(leg.distance_m / 1000.0, 2),
                        "minutes": int(round(leg.duration_s / 60.0)),
                    }
                    for leg in result.legs
                ],
                "considered": len(candidates),
                "dropped_for_time": dropped,
                "day_minutes": budget,
            }
        # Over budget: shed the least valuable stop and try again.
        chosen.remove(min(chosen, key=lambda t: t.priority))
        dropped += 1

    return {
        "targets": [], "order": [], "engine": "haversine",
        "total_distance_km": 0.0, "total_drive_minutes": 0,
        "total_duration_minutes": 0, "considered": len(candidates),
        "dropped_for_time": dropped,
        "note": "Nothing fits the day budget from that starting point.",
    }


# ---------------------------------------------------------------------------
# Costing an existing plan
# ---------------------------------------------------------------------------


def points_from_stops(stops: Sequence[Any], default_minutes: int) -> List[RoutePoint]:
    """Route points for the child rows of a plan, skipping cancelled stops."""
    points = []
    for row in stops:
        if getattr(row, "status", None) in ("Cancelled",):
            continue
        lat = _coord(getattr(row, "latitude", None))
        lng = _coord(getattr(row, "longitude", None))
        if lat is None or lng is None:
            continue
        points.append(
            RoutePoint(
                key=row.name,
                lat=lat,
                lng=lng,
                locked=bool(getattr(row, "locked", 0)),
                service_minutes=int(getattr(row, "visit_minutes", 0) or 0) or default_minutes,
                label=getattr(row, "title", "") or "",
            )
        )
    return points


def start_point_for(plan) -> Optional[RoutePoint]:
    """The plan's fixed start, or ``None`` when it starts wherever the rep is.

    "Current Location" is resolved on the phone at drive time, not here: a
    server has no idea where the rep is, and guessing (the branch? the office?)
    would produce a first leg that is confidently wrong.
    """
    lat = _coord(getattr(plan, "start_latitude", None))
    lng = _coord(getattr(plan, "start_longitude", None))
    if lat is None or lng is None:
        return None
    return RoutePoint(
        key="__start__", lat=lat, lng=lng, label=plan.get("start_label") or "Start"
    )


def apply_route(
    plan, result: RouteResult, points: Sequence[RoutePoint], reorder: bool = True
) -> None:
    """Write a solved route back onto the plan's child rows.

    Sets each row's leg distance/time and, when ``reorder`` is set, the row
    order itself. The document's own ``validate`` then derives arrival times
    and day totals from these — one place computes each number.

    ``result.order`` indexes ``points``, NOT the child table: the solver never
    saw the rows it could not route (cancelled, or with an unusable pin). The
    mapping therefore goes through ``RoutePoint.key``, which is the row name.
    Indexing into the child table directly would work right up until the first
    plan with a skipped row, and then reorder every stop onto the wrong leg.
    """
    all_rows = list(plan.get("stops") or [])
    cancelled = [row for row in all_rows if row.status in ("Cancelled",)]
    active = [row for row in all_rows if row.status not in ("Cancelled",)]
    by_name = {row.name: row for row in active}

    ordered_rows = []
    if reorder:
        for index in result.order:
            if 0 <= index < len(points):
                row = by_name.get(points[index].key)
                if row is not None:
                    ordered_rows.append(row)
    else:
        ordered_rows = list(active)

    # Any row the solver could not place must still survive the write, or
    # optimising would silently delete stops.
    placed = {row.name for row in ordered_rows}
    ordered_rows.extend(row for row in active if row.name not in placed)

    legs_by_to = {}
    for leg in result.legs:
        legs_by_to.setdefault(leg.to_key, leg)

    for position, row in enumerate(ordered_rows):
        leg = legs_by_to.get(row.name)
        row.leg_km = round(leg.distance_m / 1000.0, 2) if leg else 0.0
        row.leg_minutes = int(round(leg.duration_s / 60.0)) if leg else 0
        row.idx = position + 1

    for offset, row in enumerate(cancelled, start=len(ordered_rows) + 1):
        row.leg_km = 0.0
        row.leg_minutes = 0
        row.idx = offset

    plan.stops = ordered_rows + cancelled
    plan.route_engine = result.engine
    plan.optimized_on = frappe.utils.now()


def route_plan(
    plan, optimize: bool = True, config: Optional[RouteConfig] = None
) -> Tuple[RouteResult, List[RoutePoint]]:
    """Cost a plan — reordering it or accepting the order it already has.

    Returns the solved route *and* the points it was solved over, because
    :func:`apply_route` needs the second to interpret the first.
    """
    config = config or route_config()
    default_minutes = int(plan.get("default_visit_minutes") or config.visit_minutes)
    points = points_from_stops(plan.get("stops") or [], default_minutes)
    start = start_point_for(plan)
    return_to_start = bool(plan.get("return_to_start"))

    if not points:
        return RouteResult(order=[], engine="haversine"), []

    solver = plan_route if optimize else cost_fixed_order
    result = solver(
        points,
        start=start,
        return_to_start=return_to_start,
        road_factor=config.road_factor,
        speed_kmh=config.speed_kmh,
        default_visit_minutes=default_minutes,
        osrm_provider=config.osrm_provider(),
    )
    return result, points

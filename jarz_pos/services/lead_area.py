"""Name the area a coordinate falls in, using the lead corpus we already own.

``custom_primary_area`` is the *main* filter on the leads catalog, so a lead
saved without one is effectively invisible to the people who work the list. It
is also free text, which means a rep who types "new cairo" produces a value that
matches none of the filter chips, which are built from the catalog's own values.

Google's Places Details API would answer this directly -- it returns the
neighbourhood component of an address -- but it is a paid, keyed service and no
key is configured (:func:`jarz_pos.services.lead_maps._places_key` returns ``""``
on both servers). Rather than leave the field blank until someone buys a key,
this module answers from data the site already holds: **5,440 places in Egypt
whose geography a human has already named**, 3,009 of them ``Jarz Lead Branch``
rows and 2,431 ``Lead`` rows, each carrying a latitude and a longitude.

The method is a distance-weighted k-nearest-neighbour vote over that gazetteer.
Measured on production with a leak-free leave-one-out -- a place's own parent
lead is excluded from its own gazetteer, so a branch can never be labelled by
its sibling branch -- it is **86% correct overall** on ``area``, and **92%
correct over the 83% of places** that clear the high-confidence gate below. That
is not exact and is not sold as exact: the caller gets a confidence and the top
three candidates, so the UI can prefill the likely answer while keeping a
one-tap correction obvious.

Three levels are resolved, because the corpus carries three and a rep should not
retype what we can infer: ``area`` (60 labels, "Zamalek"), ``region`` (17,
"Cairo/Giza") and ``governorate`` (309, "Cairo").

Three deliberate choices.

**Junk labels are suppressed by the vote, not by a blocklist.** The branch table
holds labels that are not places at all -- a stray Plus Code, a street name, a
comma-mangled Arabic fragment left by whatever geocoder produced the sweep.
An isolated point cannot win a k=5 vote, because nothing near it agrees. A
hand-maintained blocklist would need updating every time the corpus grew; the
vote does not. The coarse levels get one extra filter, :func:`_is_place_label`,
because there the noise is not isolated -- the same mangled string repeats
across a whole neighbourhood and could out-vote the clean name.

**A point outside the corpus gets no answer.** Confidently naming the nearest
Cairo neighbourhood for a lead 150km away is worse than a blank field, because
blank invites the rep to type and a wrong value does not.

**The gazetteer is memoised per worker, not in Redis.** It is ~5,400 rows and
changes only when leads are imported or edited. Holding it in Redis would put a
few hundred KB on every cache round-trip for a value each worker can rebuild in
one query; holding it in the process costs one query per worker per TTL.
"""

from __future__ import annotations

import math
import time
from typing import Any

import frappe


#: How long a worker keeps its gazetteer before rebuilding. Leads are imported
#: in batches and edited by hand, so an hour-stale area vocabulary is harmless
#: while a per-request query over 5,400 rows would not be.
CACHE_TTL_SEC = 3600

#: Votes come from the five nearest known places. Raising k to 9 moved accuracy
#: by 0.3pp on production and widens the radius a vote can reach across, which
#: is the wrong trade in a city whose areas are a kilometre or two wide.
NEIGHBOURS = 5

#: Gate thresholds, read off the measured coverage/accuracy curve: ``high``
#: covers 83% of places at 92% accuracy, ``medium`` 95% at 89%.
HIGH_AGREEMENT = 4
HIGH_NEAREST_M = 1500.0
MEDIUM_AGREEMENT = 3
MEDIUM_NEAREST_M = 2000.0

#: A place further than this from anything we know is not in a covered area.
MAX_USEFUL_M = 15000.0

#: Distance at which a neighbour's vote has halved. Keeps a door 50m away worth
#: markedly more than one 5km away without letting a single point dictate.
VOTE_HALF_LIFE_M = 100.0

#: Minimum corpus support for a coarse label to be offered. Every mangled
#: governorate string in the corpus sits below this; every real one is far above.
MIN_COARSE_SUPPORT = 25

#: The levels resolved, ordered fine to coarse.
LEVELS = ("area", "region", "governorate")

_EARTH_RADIUS_M = 6371000.0

#: ``(lat, lng, area, region, governorate)`` per known place.
_gazetteer: list[tuple[float, float, str, str, str]] = []
_gazetteer_built_at: float = 0.0


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _clean_label(value: object) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text if 1 <= len(text) <= 140 else ""


def _is_place_label(text: str) -> bool:
    """Reject raw geocoder output masquerading as a place name.

    A governorate is called "Cairo", never "qism awwal al-Qahirah al-Jadidah,
    Cairo Governorate". The comma is what separates a *name* from an address
    line, and it is the one signal that holds across Arabic and English alike.
    """
    return bool(text) and "," not in text and len(text) <= 60


def _valid_point(lat: object, lng: object) -> tuple[float, float] | None:
    try:
        latitude = float(lat)
        longitude = float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    # (0, 0) is the Frappe Float default, not the Gulf of Guinea.
    if latitude == 0.0 and longitude == 0.0:
        return None
    return latitude, longitude


def _load_points() -> list[tuple[float, float, str, str, str]]:
    """Read every place whose geography a human has already named.

    Branch rows come first and outnumber leads, which is what we want: a brand
    with six doors contributes six pins in six areas rather than one blurred
    average of the six.
    """
    points: list[tuple[float, float, str, str, str]] = []
    queries = (
        (
            "select area as area, region as region, governorate as governorate,"
            " latitude as lat, longitude as lng from `tabJarz Lead Branch`"
            " where ifnull(area,'')!='' and ifnull(latitude,0)!=0"
            " and ifnull(longitude,0)!=0"
        ),
        (
            "select custom_primary_area as area, '' as region, '' as governorate,"
            " custom_latitude as lat, custom_longitude as lng from `tabLead`"
            " where ifnull(custom_primary_area,'')!='' and ifnull(custom_latitude,0)!=0"
            " and ifnull(custom_longitude,0)!=0"
        ),
    )
    for query in queries:
        try:
            rows = frappe.db.sql(query, as_dict=True)
        except Exception:
            # A missing custom field or a site-less context must not take the
            # lead form down; an empty gazetteer simply means "no suggestion".
            continue
        for row in rows or []:
            area = _clean_label(row.get("area"))
            point = _valid_point(row.get("lat"), row.get("lng"))
            if area and point:
                points.append(
                    (
                        point[0],
                        point[1],
                        area,
                        _clean_label(row.get("region")),
                        _clean_label(row.get("governorate")),
                    )
                )
    return points


def gazetteer(*, refresh: bool = False) -> list[tuple[float, float, str, str, str]]:
    """Return the memoised ``(lat, lng, area, region, governorate)`` corpus."""
    global _gazetteer, _gazetteer_built_at

    now = time.monotonic()
    if refresh or not _gazetteer or (now - _gazetteer_built_at) > CACHE_TTL_SEC:
        loaded = _load_points()
        # Keep the previous corpus when a rebuild finds nothing: a transient DB
        # error should degrade to slightly stale areas, not to no areas.
        if loaded or not _gazetteer:
            _gazetteer = loaded
            _gazetteer_built_at = now
    return _gazetteer


def _support(level: str) -> dict[str, int]:
    index = LEVELS.index(level) + 2
    counts: dict[str, int] = {}
    for row in gazetteer():
        label = row[index]
        if label:
            counts[label] = counts.get(label, 0) + 1
    return counts


def known_areas() -> list[str]:
    """The area vocabulary, most-used first.

    This is what the catalog's filter chips are built from, so it is also the
    list a form should offer: an area typed outside it can never be filtered on.
    """
    counts = _support("area")
    return [
        label for label, _count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _nearest(
    latitude: float,
    longitude: float,
    points: list[tuple[float, float, str, str, str]],
    limit: int,
) -> list[tuple[float, tuple[float, float, str, str, str]]]:
    """The ``limit`` closest known places, nearest first.

    A bounding box prunes the scan first. Areas are small, so the box that holds
    five neighbours is normally a couple of kilometres wide; widening only when
    it comes up short keeps the common case cheap without ever failing to find a
    neighbour that exists.
    """
    for degrees in (0.03, 0.12, 0.5, 180.0):
        lat_lo, lat_hi = latitude - degrees, latitude + degrees
        lng_lo, lng_hi = longitude - degrees, longitude + degrees
        near = [
            (_haversine_m(latitude, longitude, row[0], row[1]), row)
            for row in points
            if lat_lo <= row[0] <= lat_hi and lng_lo <= row[1] <= lng_hi
        ]
        if len(near) >= limit or degrees >= 180.0:
            near.sort(key=lambda item: item[0])
            return near[:limit]
    return []


def _vote(
    neighbours: list[tuple[float, tuple[float, float, str, str, str]]],
    level: str,
    *,
    require_support: bool,
) -> dict[str, Any]:
    index = LEVELS.index(level) + 2
    allowed: set[str] | None = None
    if require_support:
        allowed = {
            label
            for label, count in _support(level).items()
            if count >= MIN_COARSE_SUPPORT and _is_place_label(label)
        }

    votes: dict[str, float] = {}
    for distance, row in neighbours:
        label = row[index]
        if not label or (allowed is not None and label not in allowed):
            continue
        votes[label] = votes.get(label, 0.0) + 1.0 / (1.0 + distance / VOTE_HALF_LIFE_M)
    if not votes:
        return {"value": "", "confidence": "none", "candidates": []}

    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    winner = ranked[0][0]
    agreement = sum(1 for _distance, row in neighbours if row[index] == winner)
    nearest_m = neighbours[0][0]

    if agreement >= HIGH_AGREEMENT and nearest_m <= HIGH_NEAREST_M:
        confidence = "high"
    elif agreement >= MEDIUM_AGREEMENT and nearest_m <= MEDIUM_NEAREST_M:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "value": winner,
        "confidence": confidence,
        # Alternatives turn a wrong guess into one tap rather than a retype.
        "candidates": [label for label, _score in ranked[:3]],
    }


def resolve_area(latitude: object, longitude: object) -> dict[str, Any]:
    """Name the area, region and governorate a coordinate sits in.

    Always returns a dict. ``area`` is ``""`` when the point is outside the
    corpus entirely -- the honest answer for a lead in a city we have never
    swept.
    """
    empty: dict[str, Any] = {
        "area": "",
        "region": "",
        "governorate": "",
        "confidence": "none",
        "candidates": [],
        "nearest_m": None,
        "source": "nearby_leads",
    }

    point = _valid_point(latitude, longitude)
    if not point:
        return empty
    lat, lng = point

    try:
        points = gazetteer()
    except Exception:
        return empty
    if not points:
        return empty

    neighbours = _nearest(lat, lng, points, NEIGHBOURS)
    if not neighbours:
        return empty
    nearest_m = neighbours[0][0]
    if nearest_m > MAX_USEFUL_M:
        return {**empty, "nearest_m": round(nearest_m)}

    area = _vote(neighbours, "area", require_support=False)
    region = _vote(neighbours, "region", require_support=True)
    governorate = _vote(neighbours, "governorate", require_support=True)

    return {
        "area": area["value"],
        "region": region["value"],
        "governorate": governorate["value"],
        "confidence": area["confidence"],
        "candidates": area["candidates"],
        "nearest_m": round(nearest_m),
        "source": "nearby_leads",
    }

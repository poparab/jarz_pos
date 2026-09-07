"""Recognise a pasted Maps link as a place the lead catalog already holds.

The single richest source of information about a pasted link is not Google and
not OSM -- it is the catalog itself. 2,433 Leads and 3,009 branches were swept
with their phone, website, rating, review count, price band, opening hours,
area and Talabat presence already filled in. When a rep pastes a link to one of
them, every one of those fields is free and exact, and the lead should not be
created a second time.

**The join is exact, not fuzzy.** Every swept row stores its Google place as
``https://maps.google.com/?cid=<decimal>``. A share link a rep copies from the
Maps app expands to a URL carrying ``!1s0x<hex>:0x<hex>`` -- and the second hex
word of that FTID *is* the CID. Verified 2026-09-07 by opening a stored
``?cid=8118497466869163611`` and reading back the place URL Maps navigated to:
``!1s0x1458413a6bccb49d:0x70aab26eb6a5265b``, and
``int("0x70aab26eb6a5265b", 16) == 8118497466869163611``. So a link and a swept
row can be matched with certainty rather than by name similarity.

Proximity is the fallback, and it is deliberately conservative: same normalised
name within 120m. Name-only matching would collapse the 14 Costa Coffee
branches into one lead; distance-only matching would call the cafe next door a
duplicate. Requiring both is what makes a positive worth acting on.

A match is reported, never applied. Two doors of one brand genuinely are two
branches of one lead, and only the rep knows which case they are in.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from typing import Any
from urllib.parse import unquote

import frappe


#: ``!1s0x<hex>:0x<hex>`` -- the Maps "feature id". The second word is the CID.
FTID_RE = re.compile(r"!1s(0x[0-9a-f]{6,20}):(0x[0-9a-f]{6,20})", re.IGNORECASE)
#: ``!16s%2Fg%2F1tdtyp2w`` -- the Knowledge Graph id, stable across locales.
MID_RE = re.compile(r"!16s(?:%2F|/)([A-Za-z0-9._/%-]{2,80})")
CID_IN_URL_RE = re.compile(r"[?&]cid=(\d{1,25})")

#: How close two pins must be to be the same door, for a same-name match.
SAME_PLACE_M = 120.0

CACHE_TTL_SEC = 3600
_EARTH_RADIUS_M = 6371000.0

_index: dict[str, Any] = {}
_index_built_at: float = 0.0


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


def cid_from_url(url: object) -> str:
    """The Google CID a Maps URL refers to, as a decimal string, or ``""``.

    Handles both spellings: the ``?cid=`` form the sweep stored, and the FTID
    embedded in the share link a rep pastes.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    for candidate in (text, unquote(text)):
        found = CID_IN_URL_RE.search(candidate)
        if found:
            return found.group(1)
        ftid = FTID_RE.search(candidate)
        if ftid:
            try:
                value = int(ftid.group(2), 16)
            except ValueError:
                continue
            # 0 is the "no place" FTID half; treat it as absent.
            if value:
                return str(value)
    return ""


def mid_from_url(url: object) -> str:
    """The Knowledge Graph id (``/g/1tdtyp2w``) a Maps URL carries, or ``""``."""
    text = str(url or "").strip()
    for candidate in (text, unquote(text)):
        found = MID_RE.search(candidate)
        if found:
            value = unquote(found.group(1)).strip("/")
            if value:
                return "/" + value
    return ""


def normalise_name(value: object) -> str:
    """Fold a place name to something two spellings of it can share.

    Decomposing first and dropping the combining marks is what makes
    ``Lucaffe``/``Lucaffe-with-acute`` and ``Cafe``/``Cafe-with-acute`` the same
    key -- the corpus carries both spellings of the same brand. It folds Arabic
    tashkeel by the same rule, since those are combining marks too.

    The ranges are written as escapes and this module is kept pure ASCII on
    purpose: a literal Arabic range here reaches the servers through tooling
    that reads UTF-8 as ANSI, and arrives as a regex whose character range no
    longer parses.
    """
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\u0640", "")  # tatweel is a stretch, not a letter
    text = re.sub(r"[^0-9a-z\u0600-\u06ff]+", " ", text)
    return " ".join(text.split())


#: Fields worth surfacing when the place turns out to be known. Every one is
#: checked against the live schema first: code is deployed before ``bench
#: migrate`` finishes, and a SELECT naming a column that does not exist yet
#: fails the whole query with "Unknown column" -- which here would silently turn
#: duplicate detection off rather than degrade it. Same guard as
#: ``api/leads._lead_query_fields``.
_LEAD_FIELDS = (
    "name",
    "lead_name",
    "company_name",
    "custom_maps_url",
    "custom_primary_area",
    "custom_latitude",
    "custom_longitude",
    "phone",
    "mobile_no",
    "email_id",
    "website",
    "custom_instagram",
    "custom_lead_category",
    "custom_fit_tier",
    "custom_avg_rating",
    "custom_total_reviews",
    "custom_branch_count",
)

_BRANCH_FIELDS = (
    "parent",
    "branch_name",
    "area",
    "region",
    "governorate",
    "rating",
    "reviews",
    "price",
    "status",
    "hours",
    "phone",
    "website",
    "maps_url",
    "address",
    "latitude",
    "longitude",
    "on_talabat",
)


def _existing(doctype: str, fields: tuple[str, ...]) -> list[str]:
    try:
        meta = frappe.get_meta(doctype)
    except Exception:
        return []
    kept = []
    for field in fields:
        # `name` and `parent` are always present and have no DocField.
        if field in ("name", "parent") or meta.get_field(field):
            kept.append(field)
    return kept


def _rows(doctype: str, table: str, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    names = _existing(doctype, fields)
    if not names:
        return []
    columns = ", ".join("`%s`" % name for name in names)
    try:
        return frappe.db.sql(
            "select %s from `%s`" % (columns, table), as_dict=True
        )
    except Exception:
        return []


def _lead_rows() -> list[dict[str, Any]]:
    return _rows("Lead", "tabLead", _LEAD_FIELDS)


def _branch_rows() -> list[dict[str, Any]]:
    return _rows("Jarz Lead Branch", "tabJarz Lead Branch", _BRANCH_FIELDS)


def index(*, refresh: bool = False) -> dict[str, Any]:
    """Memoised per-worker lookup structures over the catalog.

    Same reasoning as the area gazetteer: a few thousand rows that change only
    on import or edit, rebuilt once an hour per worker rather than queried per
    keystroke.
    """
    global _index, _index_built_at

    now = time.monotonic()
    if not refresh and _index and (now - _index_built_at) <= CACHE_TTL_SEC:
        return _index

    leads = _lead_rows()
    branches = _branch_rows()
    if not leads and not branches and _index:
        # A transient DB failure should leave the previous index in place.
        return _index

    by_lead = {row["name"]: row for row in leads}
    by_cid: dict[str, dict[str, Any]] = {}
    points: list[tuple[float, float, str, str, dict[str, Any] | None]] = []

    for row in leads:
        cid = cid_from_url(row.get("custom_maps_url"))
        if cid:
            by_cid.setdefault(cid, {"lead": row, "branch": None})
        try:
            lat = float(row.get("custom_latitude") or 0)
            lng = float(row.get("custom_longitude") or 0)
        except (TypeError, ValueError):
            lat = lng = 0.0
        if lat and lng:
            points.append((lat, lng, row["name"], normalise_name(row.get("lead_name")), None))

    for row in branches:
        parent = by_lead.get(row.get("parent"))
        cid = cid_from_url(row.get("maps_url"))
        if cid:
            # A branch row is the more specific answer; let it win the key.
            by_cid[cid] = {"lead": parent, "branch": row}
        try:
            lat = float(row.get("latitude") or 0)
            lng = float(row.get("longitude") or 0)
        except (TypeError, ValueError):
            lat = lng = 0.0
        if lat and lng:
            points.append(
                (lat, lng, row.get("parent") or "", normalise_name(row.get("branch_name")), row)
            )

    _index = {"by_cid": by_cid, "by_lead": by_lead, "points": points}
    _index_built_at = now
    return _index


def _known_fields(lead: dict[str, Any] | None, branch: dict[str, Any] | None) -> dict[str, Any]:
    """Everything the catalog already knows about this place, blanks dropped."""
    lead = lead or {}
    branch = branch or {}
    values = {
        "lead_name": lead.get("lead_name") or branch.get("branch_name"),
        "company_name": lead.get("company_name"),
        "primary_area": branch.get("area") or lead.get("custom_primary_area"),
        "region": branch.get("region"),
        "governorate": branch.get("governorate"),
        "phone": branch.get("phone") or lead.get("phone") or lead.get("mobile_no"),
        "email": lead.get("email_id"),
        "website": branch.get("website") or lead.get("website"),
        "instagram": lead.get("custom_instagram"),
        "formatted_address": branch.get("address"),
        "rating": branch.get("rating") or lead.get("custom_avg_rating"),
        "reviews": branch.get("reviews") or lead.get("custom_total_reviews"),
        "price_band": branch.get("price"),
        "opening_hours": branch.get("hours"),
        "category": lead.get("custom_lead_category"),
        "tier": lead.get("custom_fit_tier"),
        "branch_count": lead.get("custom_branch_count"),
        "on_talabat": branch.get("on_talabat"),
    }
    return {key: value for key, value in values.items() if value not in (None, "", 0)}


def match(
    url: object = None,
    latitude: object = None,
    longitude: object = None,
    place_name: object = None,
) -> dict[str, Any]:
    """Identify a pasted link as a catalog place, if it is one.

    Returns ``{"matched": False}`` when it is not, which is the normal case for
    a genuinely new lead.
    """
    miss: dict[str, Any] = {"matched": False, "how": "", "known": {}}

    try:
        data = index()
    except Exception:
        return miss
    if not data:
        return miss

    cid = cid_from_url(url)
    if cid:
        hit = data["by_cid"].get(cid)
        if hit:
            lead = hit.get("lead") or {}
            branch = hit.get("branch") or {}
            name = lead.get("name") or (branch.get("parent") if branch else "") or ""
            if not name:
                return miss
            return {
                "matched": True,
                "how": "cid",
                "confidence": "exact",
                "lead": name,
                "branch_name": branch.get("branch_name") or "",
                "distance_m": 0,
                "known": _known_fields(lead, branch),
            }

    # Fallback: the same name at the same door.
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return miss
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0) or (lat == 0.0 and lng == 0.0):
        return miss
    wanted = normalise_name(place_name)
    if not wanted:
        return miss

    best = None
    best_distance = SAME_PLACE_M
    for point_lat, point_lng, lead_name, name, branch in data["points"]:
        if name != wanted:
            continue
        distance = _haversine_m(lat, lng, point_lat, point_lng)
        if distance <= best_distance:
            best_distance = distance
            best = (lead_name, branch)
    if not best:
        return miss

    lead_key, branch = best
    lead = data["by_lead"].get(lead_key) or {}
    name = lead.get("name") or lead_key
    if not name:
        # An orphaned branch row (its parent Lead is gone) would otherwise be a
        # match with an empty `lead`. The client suppresses the duplicate banner
        # when it cannot name the lead, but the caller still prefills that row's
        # phone and website -- so the rep would silently save a NEW lead wearing
        # another one's contact details. No name, no match.
        return miss
    return {
        "matched": True,
        "how": "proximity",
        "confidence": "likely",
        "lead": name,
        "branch_name": (branch or {}).get("branch_name") or "",
        "distance_m": round(best_distance),
        "known": _known_fields(lead, branch),
    }

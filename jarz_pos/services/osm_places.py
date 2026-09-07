"""Reverse-geocode a lead's pin against OpenStreetMap.

Google's Places Details API answers this better, but it is keyed and paid and no
key is configured on either server (see :mod:`jarz_pos.services.lead_area` for
the same constraint driving the area vote). Nominatim is the free, keyless
alternative from the same OSM stack the visit planner's OSRM already runs on.

Measured on production against 12 real branch pins, it returned a postcode for
12, a city for 11, a street for 9, a suburb for 9 and a house number for 5 --
and in several cases a *cleaner* address than the one the original sweep stored
(``44, Al Hegaz Street, Heliopolis, Cairo, 11843`` against a stored
``Mahkama, 44 شارع الحجاز، An Nuzhah, Qesm an Nuzhah, محافظة القاهرة‬ 4470035``).

When the pin lands on a business OSM has mapped rather than on a building, the
reverse also carries ``phone``, ``website``, ``opening_hours`` and ``cuisine``.
That happened for 1 pin in 5, so it is a bonus, never the plan. Overpass would
raise that rate but the public instance answered a 20-point query with a 504,
and a lead form cannot depend on a service that does that.

**Nominatim's usage policy is a hard constraint, not advice.** It permits at most
one request per second from an application, requires a User-Agent that identifies
it, and forbids bulk use. Two mechanisms keep us inside it and are the reason
this module is not simply a ``urlopen`` at the call site:

* a **cross-worker 1/sec gate** in Redis, which *fails closed* -- no Redis means
  no request, because exceeding the policy risks the block that would take the
  feature away from everyone;
* a **30-day cache keyed on the rounded coordinate**, so re-previewing the same
  link, or two reps adding neighbouring doors, costs nothing.

Everything here is best-effort and returns ``{}`` on any failure. An address we
could not look up must never be the reason a lead cannot be saved.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode

import frappe


#: Nominatim's public endpoint. Fixed host, HTTPS, no redirects followed.
NOMINATIM_HOST = "nominatim.openstreetmap.org"
NOMINATIM_URL = "https://" + NOMINATIM_HOST + "/reverse"

#: Their policy requires an identifying agent. Overridable so a self-hosted
#: instance or a changed contact address does not need a code change.
DEFAULT_USER_AGENT = "jarz-pos-lead-enrichment/1.0 (+https://erp.orderjarz.com)"

#: One request per second, application-wide.
RATE_KEY = "jarz_pos:osm:reverse:gate"
MIN_INTERVAL_MS = 1100

#: ~11m of latitude. Two pins that round together are the same doorway for
#: addressing purposes, so they may share a cached answer.
COORD_PRECISION = 4
CACHE_TTL_SEC = 30 * 24 * 3600

MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_TIMEOUT = 6.0


def enabled() -> bool:
    """Off switch that does not need a deploy.

    Set ``osm_reverse_geocode_disabled`` in site config to stop every lookup --
    the right lever if Nominatim ever asks us to.
    """
    try:
        return not bool(frappe.conf.get("osm_reverse_geocode_disabled"))
    except Exception:
        return False


def _user_agent() -> str:
    try:
        return str(frappe.conf.get("osm_user_agent") or "").strip() or DEFAULT_USER_AGENT
    except Exception:
        return DEFAULT_USER_AGENT


def _cache_key(lat: float, lng: float) -> str:
    return "jarz_pos:osm:reverse:%.*f:%.*f" % (
        COORD_PRECISION,
        lat,
        COORD_PRECISION,
        lng,
    )


def _cached(key: str) -> dict[str, Any] | None:
    try:
        raw = frappe.cache().get_value(key, use_local_cache=False)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _store(key: str, value: dict[str, Any]) -> None:
    try:
        frappe.cache().set_value(key, json.dumps(value), expires_in_sec=CACHE_TTL_SEC)
    except Exception:
        pass


def _claim_rate_slot() -> bool:
    """Take the application's one-request-per-second slot, or decline.

    Fails closed on purpose: without Redis we cannot prove we are inside
    Nominatim's policy, and guessing wrong risks the whole application being
    blocked rather than one lookup failing.
    """
    try:
        cache = frappe.cache()
        key = cache.make_key(RATE_KEY)
        now_ms = int(time.time() * 1000)
        last = cache.get_value(key, use_local_cache=False)
        if last is not None:
            try:
                if now_ms - int(last) < MIN_INTERVAL_MS:
                    return False
            except (TypeError, ValueError):
                pass
        cache.set_value(key, str(now_ms), expires_in_sec=60)
        return True
    except Exception:
        return False


def _valid_point(lat: object, lng: object) -> tuple[float, float] | None:
    try:
        latitude = float(lat)
        longitude = float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None
    return latitude, longitude


class _NoRedirect:
    """Nominatim answers directly; a redirect would be a different service."""

    @staticmethod
    def opener():
        import urllib.request

        class Handler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        return urllib.request.build_opener(Handler)


def _request(latitude: float, longitude: float, timeout: float) -> dict[str, Any]:
    import urllib.request

    query = urlencode(
        {
            "format": "jsonv2",
            "lat": "%.7f" % latitude,
            "lon": "%.7f" % longitude,
            "zoom": 18,
            "addressdetails": 1,
            "extratags": 1,
            "namedetails": 1,
            # Latin script keeps the value usable in an ERPNext Address field
            # and consistent with the rest of the lead catalog.
            "accept-language": "en",
        }
    )
    request = urllib.request.Request(
        NOMINATIM_URL + "?" + query,
        headers={"Accept": "application/json", "User-Agent": _user_agent()},
    )
    response = _NoRedirect.opener().open(
        request, timeout=max(0.5, min(float(timeout), 10.0))
    )
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        response.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        return {}
    data = json.loads(raw.decode("utf-8", "replace"))
    return data if isinstance(data, dict) else {}


def _text(value: object, limit: int = 140) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def _map_response(data: dict[str, Any]) -> dict[str, Any]:
    """Project Nominatim's answer onto the lead form's fields."""
    if not data or data.get("error"):
        return {}
    address = data.get("address") or {}
    if not isinstance(address, dict):
        address = {}
    extra = data.get("extratags") or {}
    if not isinstance(extra, dict):
        extra = {}

    road = _text(address.get("road"))
    house_number = _text(address.get("house_number"), 30)
    line1 = " ".join(part for part in (house_number, road) if part).strip()

    result = {
        "formatted_address": _text(data.get("display_name"), 300),
        "address_line1": line1,
        # The suburb is a real neighbourhood name but in OSM's vocabulary, not
        # the lead catalog's -- so it is address detail, never the filter area.
        "address_line2": _text(
            address.get("suburb")
            or address.get("neighbourhood")
            or address.get("city_district")
        ),
        "city": _text(
            address.get("city") or address.get("town") or address.get("village")
        ),
        "state": _text(address.get("state")),
        "country": _text(address.get("country")),
        "pincode": _text(address.get("postcode"), 20),
        "phone": _text(extra.get("phone") or extra.get("contact:phone"), 60),
        "website": _text(extra.get("website") or extra.get("contact:website"), 200),
        "opening_hours": _text(extra.get("opening_hours"), 200),
        "cuisine": _text(extra.get("cuisine"), 140),
        # Only meaningful when the pin landed on a mapped business rather than
        # on the building containing it.
        "osm_place_name": _text(data.get("name"), 200),
        "osm_category": _text(data.get("category"), 60),
        "osm_type": _text(data.get("type"), 60),
    }
    return {key: value for key, value in result.items() if value}


def reverse(
    latitude: object, longitude: object, *, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Best-effort address for a coordinate. Returns ``{}`` rather than raising.

    Safe to call from a background job. Do NOT call it from a web request: a
    stalled Nominatim would hold the worker, which is the same reason short-link
    expansion lives in a job.
    """
    point = _valid_point(latitude, longitude)
    if not point or not enabled():
        return {}
    lat, lng = point

    key = _cache_key(lat, lng)
    cached = _cached(key)
    if cached is not None:
        return cached

    if not _claim_rate_slot():
        return {}

    try:
        mapped = _map_response(_request(lat, lng, timeout))
    except Exception:
        # Do not cache a failure: the next lead at this door should retry.
        return {}

    _store(key, mapped)
    return mapped

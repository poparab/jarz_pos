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
from urllib.parse import urlencode, urlparse

import frappe


#: Nominatim's public endpoint. Fixed host, HTTPS, no redirects followed.
NOMINATIM_HOST = "nominatim.openstreetmap.org"
NOMINATIM_URL = "https://" + NOMINATIM_HOST + "/reverse"

#: Their policy requires an identifying agent. Overridable so a self-hosted
#: instance or a changed contact address does not need a code change.
UA_PRODUCT = "jarz-pos-lead-enrichment/1.0"
DEFAULT_USER_AGENT = UA_PRODUCT + " (+https://orderjarz.com)"

#: One request per second, application-wide.
RATE_KEY = "jarz_pos:osm:reverse:gate"
MIN_INTERVAL_MS = 1100

#: ~11m of latitude. Two pins that round together are the same doorway for
#: addressing purposes, so they may share a cached answer.
COORD_PRECISION = 4
CACHE_TTL_SEC = 30 * 24 * 3600

#: An empty answer is cached far more briefly. Nominatim reports "unable to
#: geocode" as a 200 with an error body, which is indistinguishable here from a
#: transient upstream problem -- and holding that for 30 days would poison a
#: coordinate that is merely unmapped today.
EMPTY_CACHE_TTL_SEC = 3600

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
    """Identify this environment, not just this application.

    The rate slot lives in Redis, and staging and production have separate
    Redis instances -- so the gate is per environment while the User-Agent, if
    left as one constant, is not. Nominatim would then see one application
    making up to two requests a second, which is precisely the thing that earns
    a block. Staging is an AMI clone of production, so its site name is also
    ``frontend``; the site URL is the only part that actually differs.
    """
    try:
        configured = str(frappe.conf.get("osm_user_agent") or "").strip()
        if configured:
            return configured
    except Exception:
        return DEFAULT_USER_AGENT
    try:
        from frappe.utils import get_url

        host = (urlparse(get_url()).hostname or "").strip()
        if host:
            return "%s (+https://%s)" % (UA_PRODUCT, host)
    except Exception:
        pass
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
    ttl = CACHE_TTL_SEC if value else EMPTY_CACHE_TTL_SEC
    try:
        frappe.cache().set_value(key, json.dumps(value), expires_in_sec=ttl)
    except Exception:
        pass


def _claim_rate_slot() -> bool:
    """Take the application's one-request-per-second slot, or decline.

    **The claim must be atomic.** A read-compare-write would let two RQ workers
    entering this in the same millisecond both observe the same timestamp, both
    pass, and both call Nominatim -- and Frappe runs more than one worker on the
    ``short`` queue, so that is reachable rather than theoretical. ``SET NX EX``
    is the only form where exactly one caller can win: the key's existence *is*
    the slot, and Redis expires it a second later.

    Fails closed on purpose: without Redis we cannot prove we are inside
    Nominatim's policy, and guessing wrong risks the whole application being
    blocked rather than one lookup failing.
    """
    try:
        cache = frappe.cache()
        key = cache.make_key(RATE_KEY)
        # px/nx are milliseconds and set-if-absent. A falsy return means another
        # worker holds the slot right now.
        return bool(cache.set(key, str(int(time.time() * 1000)), px=MIN_INTERVAL_MS, nx=True))
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
            # Matches COORD_PRECISION: querying finer than we cache would send
            # a third party a more precise fix than the answer is keyed on.
            "lat": "%.*f" % (COORD_PRECISION, latitude),
            "lon": "%.*f" % (COORD_PRECISION, longitude),
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


def _note_failure() -> None:
    """Record that the lookup is failing, at most once an hour.

    Every path in this module swallows its exception, which is right -- an
    address must never block saving a lead -- but it also means a Nominatim
    block or a DNS change would present as "addresses quietly stopped being
    filled" and nothing anywhere would say so. One throttled line is the
    difference between a diagnosable outage and a mystery.

    ``frappe.log_error`` can itself raise, so the whole thing is guarded.
    """
    try:
        cache = frappe.cache()
        key = cache.make_key("jarz_pos:osm:reverse:failure-logged")
        if not cache.set(key, "1", ex=3600, nx=True):
            return
        frappe.log_error(
            title="osm_places.reverse failed",
            message=frappe.get_traceback(with_context=False),
        )
    except Exception:
        pass


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
        _note_failure()
        return {}

    _store(key, mapped)
    return mapped

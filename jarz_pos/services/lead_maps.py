"""Safe, best-effort Google Maps suggestions for the B2B lead editor.

Long URLs are parsed without I/O. Short links are expanded only in a background
job so an authenticated request cannot occupy a web worker while Google stalls.
All returned metadata is optional and reviewable before it is saved.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, unquote_plus, urljoin, urlparse

import frappe

from jarz_pos.utils import geo


MAX_URL_LENGTH = 4096
MAX_RESPONSE_BYTES = 256 * 1024
TICKET_TTL_SEC = 5 * 60
DEDUPE_TTL_SEC = 60
RATE_WINDOW_SEC = 60
RATE_MAX_REQUESTS = 8
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9]{32}$")
PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,300}$")
EMBEDDED_PLACE_ID_RE = re.compile(
    r"!1s([A-Za-z0-9_-]{10,300})(?=[!/?&#]|$)", re.IGNORECASE
)
COORD_TEXT_RE = re.compile(
    r"^[-+]?[0-9]{1,3}(?:\.[0-9]+)?\s*,\s*[-+]?[0-9]{1,3}(?:\.[0-9]+)?$"
)

# Explicit destinations prevent patterns such as google.<anything> from
# accepting unrelated registries such as google.zip.
MAP_HOSTS = frozenset(
    {
        "google.com",
        "www.google.com",
        "maps.google.com",
        "google.com.eg",
        "www.google.com.eg",
        "maps.google.com.eg",
    }
)
MAP_SUBDOMAIN_HOSTS = frozenset({"maps.google.com", "maps.google.com.eg"})
SHORT_HOSTS = frozenset({"maps.app.goo.gl", "goo.gl", "g.co"})
DIRECT_HOSTS = frozenset({"plus.codes"})


def _base(original: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "pending": False,
        "url": original,
        "canonical_url": original,
        "resolved": False,
        "short_link": False,
        "metadata_source": "none",
        "warnings": [],
        "suggestions": {"maps_url": original} if original else {},
    }
    result.update(extra)
    return result


def _validated_maps_url(value: object, *, short_only: bool = False) -> str:
    """Return a permitted HTTPS Maps URL, or an empty string."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_URL_LENGTH:
        return ""
    try:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return ""

    path = parsed.path or "/"
    if host in MAP_HOSTS:
        maps_path = path == "/maps" or path.startswith("/maps/")
        maps_subdomain_query = host in MAP_SUBDOMAIN_HOSTS and path == "/" and bool(parsed.query)
        if short_only or not (maps_path or maps_subdomain_query):
            return ""
    elif host == "maps.app.goo.gl":
        if not path.strip("/"):
            return ""
    elif host in {"goo.gl", "g.co"}:
        if not (path == "/maps" or path.startswith("/maps/")):
            return ""
    elif host in DIRECT_HOSTS:
        if short_only or not path.strip("/"):
            return ""
    else:
        return ""
    if short_only and host not in SHORT_HOSTS:
        return ""
    return text


def _is_short_url(url: object) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
        return host in SHORT_HOSTS
    except Exception:
        return False


class _NoRedirect:
    @staticmethod
    def opener():
        import urllib.request

        class Handler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        return urllib.request.build_opener(Handler)


def _request_once(url: str, method: str, timeout: float):
    """Return status and headers without following redirects or reading a body."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url, method=method, headers={"User-Agent": "jarz-pos-lead-maps/1.0"}
    )
    try:
        response = _NoRedirect.opener().open(request, timeout=timeout)
        try:
            return int(getattr(response, "status", 200)), response.headers
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), dict(exc.headers or {})
        finally:
            exc.close()


def _expand_short_link(url: str, *, timeout: float = 4.0, max_hops: int = 4) -> str:
    """Expand through permitted HTTPS hosts within one total deadline."""
    current = _validated_maps_url(url, short_only=True)
    if not current:
        return ""
    deadline = time.monotonic() + max(0.25, min(float(timeout), 5.0))
    for _ in range(max(1, min(int(max_hops), 5)) + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        try:
            status, headers = _request_once(current, "HEAD", remaining)
            if status in (405, 501):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ""
                status, headers = _request_once(current, "GET", remaining)
        except Exception:
            return ""
        if 300 <= status < 400:
            current = _validated_maps_url(
                urljoin(current, str(headers.get("Location") or ""))
            )
            if not current:
                return ""
            continue
        return current if 200 <= status < 300 and not _is_short_url(current) else ""
    return ""


def _clean_hint(value: object, *, limit: int = 300) -> str:
    text = " ".join(str(value or "").replace("+", " ").split()).strip()
    if not text or len(text) > limit or COORD_TEXT_RE.fullmatch(text):
        return ""
    return text


def _url_hints(url: str) -> dict[str, str]:
    """Extract an explicit Place ID and a human-readable name from a Maps URL."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query or "", keep_blank_values=False)
    except Exception:
        return {}

    hints: dict[str, str] = {}
    for key in ("query_place_id", "place_id"):
        candidate = str((params.get(key) or [""])[0]).strip()
        if PLACE_ID_RE.fullmatch(candidate):
            hints["place_id"] = candidate
            break
    if "place_id" not in hints:
        for candidate_url in (url, unquote(url)):
            match = EMBEDDED_PLACE_ID_RE.search(candidate_url)
            if match and PLACE_ID_RE.fullmatch(match.group(1)):
                hints["place_id"] = match.group(1)
                break

    parts = [unquote_plus(part) for part in (parsed.path or "").split("/")]
    try:
        index = [part.lower() for part in parts].index("place")
        candidate = parts[index + 1] if len(parts) > index + 1 else ""
    except ValueError:
        candidate = ""
    name = _clean_hint(candidate, limit=200)
    if not name:
        for key in ("query", "q"):
            name = _clean_hint((params.get(key) or [""])[0], limit=200)
            if name:
                break
    if name:
        hints["place_name"] = name
    return hints


def _places_key() -> str:
    """Read existing site config without returning or logging the secret."""
    try:
        return str(
            frappe.conf.get("google_places_api_key")
            or frappe.conf.get("google_maps_api_key")
            or ""
        ).strip()
    except Exception:
        return ""


def _fetch_place_details(
    place_id: str, api_key: str, *, timeout: float = 4.0
) -> dict[str, Any]:
    """Call Places Details (New) at one fixed host without following redirects."""
    if not PLACE_ID_RE.fullmatch(str(place_id or "")) or not api_key:
        return {}
    try:
        import urllib.request

        fields = ",".join(
            (
                "id",
                "displayName",
                "formattedAddress",
                "addressComponents",
                "internationalPhoneNumber",
                "nationalPhoneNumber",
                "websiteUri",
                "location",
            )
        )
        request = urllib.request.Request(
            "https://places.googleapis.com/v1/places/" + quote(place_id, safe=""),
            headers={
                "Accept": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": fields,
                "User-Agent": "jarz-pos-lead-maps/1.0",
            },
        )
        response = _NoRedirect.opener().open(
            request, timeout=max(0.25, min(timeout, 5.0))
        )
        try:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            response.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _component(details: dict[str, Any], *types: str) -> str:
    wanted = set(types)
    for row in details.get("addressComponents") or []:
        if not isinstance(row, dict) or not wanted.intersection(row.get("types") or []):
            continue
        value = row.get("longText") or row.get("shortText")
        if value:
            return str(value).strip()
    return ""


def _map_place_details(details: dict[str, Any]) -> dict[str, Any]:
    if not details:
        return {}
    display_name = details.get("displayName") or {}
    location = details.get("location") or {}
    if not isinstance(display_name, dict):
        display_name = {}
    if not isinstance(location, dict):
        location = {}
    formatted = str(details.get("formattedAddress") or "").strip()
    result = {
        "place_id": str(details.get("id") or "").strip(),
        "place_name": str(display_name.get("text") or "").strip(),
        "phone": str(
            details.get("internationalPhoneNumber")
            or details.get("nationalPhoneNumber")
            or ""
        ).strip(),
        "website": str(details.get("websiteUri") or "").strip(),
        "formatted_address": formatted,
        "address_line1": formatted,
        "primary_area": _component(
            details, "neighborhood", "sublocality_level_1", "sublocality", "locality"
        ),
        "city": _component(
            details, "locality", "postal_town", "administrative_area_level_2"
        ),
        "state": _component(details, "administrative_area_level_1"),
        "country": _component(details, "country"),
        "pincode": _component(details, "postal_code"),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _preview_canonical(
    original: str, canonical: str, *, include_place_details: bool = False
) -> dict[str, Any]:
    result = _base(
        original, canonical_url=canonical, short_link=_is_short_url(original)
    )
    hints = _url_hints(canonical)
    api_key = _places_key()
    mapped = _map_place_details(
        _fetch_place_details(hints.get("place_id", ""), api_key)
        if include_place_details
        else {}
    )
    if mapped:
        hints.update(mapped)
        result["metadata_source"] = "google_places"
    elif hints.get("place_name"):
        result["metadata_source"] = "url"
    if hints.get("place_id") and not api_key:
        result["warnings"].append(
            "Google Places metadata is not configured; URL details were used instead."
        )

    # Places location wins. Otherwise accept a selected pin, explicit coordinate
    # query, or full Plus Code. @lat,lng is only the viewport centre.
    latitude = mapped.get("latitude")
    longitude = mapped.get("longitude")
    precision = "place" if geo.is_valid_coordinate(latitude, longitude) else None
    if precision is None:
        parsed = geo.parse_maps_link(canonical)
        if parsed:
            parsed_lat, parsed_lng, parsed_precision = parsed
            if parsed_precision == geo.PRECISION_VIEWPORT:
                result["reason"] = "viewport_only"
                result["warnings"].append(
                    "The link only exposes the map view, not the selected place pin."
                )
            else:
                latitude, longitude, precision = parsed_lat, parsed_lng, parsed_precision

    if precision and geo.is_valid_coordinate(latitude, longitude):
        result.update(
            {
                "resolved": True,
                "latitude": float(latitude),
                "longitude": float(longitude),
                "precision": precision,
                "accuracy_m": geo.accuracy_for_precision(precision, 25.0),
            }
        )
    elif "reason" not in result:
        result["reason"] = "no_coordinates_in_link"

    for key in (
        "place_id",
        "place_name",
        "phone",
        "website",
        "formatted_address",
        "address_line1",
        "primary_area",
        "city",
        "state",
        "country",
        "pincode",
    ):
        if hints.get(key) not in (None, ""):
            result[key] = hints[key]

    # Google's neighbourhood component is the exact answer, but it only arrives
    # with a configured Places key, and neither server has one. Without a
    # fallback the leads catalog's MAIN filter is simply blank on every lead a
    # rep adds by pasting a link. Deriving it from the places we have already
    # classified is inexact and says so -- see jarz_pos.services.lead_area for
    # the measured accuracy and why the caller gets candidates as well.
    if not result.get("primary_area") and result.get("resolved"):
        try:
            from jarz_pos.services import lead_area

            derived = lead_area.resolve_area(
                result.get("latitude"), result.get("longitude")
            )
        except Exception:
            derived = {}
        if derived.get("area"):
            result["primary_area"] = derived["area"]
            result["primary_area_source"] = derived.get("source") or "nearby_leads"
            result["primary_area_confidence"] = derived.get("confidence") or "low"
            result["area_candidates"] = list(derived.get("candidates") or [])
            if result["metadata_source"] == "none":
                result["metadata_source"] = "nearby_leads"
        # An Egyptian address's city is its governorate. Only ever fill a blank:
        # a city Google actually returned is better than one we inferred.
        if derived.get("governorate") and not result.get("city"):
            result["city"] = derived["governorate"]

    suggestions = result["suggestions"]
    if result.get("place_name"):
        suggestions["lead_name"] = result["place_name"]
        suggestions["place_name"] = result["place_name"]
    for key in (
        "latitude",
        "longitude",
        "phone",
        "website",
        "formatted_address",
        "address_line1",
        "primary_area",
        "city",
        "state",
        "country",
        "pincode",
    ):
        if result.get(key) not in (None, ""):
            suggestions[key] = result[key]
    return result


def _ticket_key(request_id: str) -> str:
    return f"jarz_pos:lead_maps:ticket:{request_id}"


def _dedupe_key(user: str, link: str) -> str:
    digest = hashlib.sha256(f"{user}\0{link}".encode("utf-8")).hexdigest()
    return f"jarz_pos:lead_maps:dedupe:{digest}"


def _cache_set(key: str, value: dict[str, Any], ttl: int = TICKET_TTL_SEC) -> bool:
    try:
        frappe.cache().set_value(key, json.dumps(value), expires_in_sec=ttl)
        return True
    except Exception:
        return False


def _cache_get(key: str) -> dict[str, Any]:
    try:
        # Polling may happen repeatedly in one Frappe request (release probes
        # do this). Bypass request-local Redis caching so a worker's completed
        # ticket is visible immediately.
        raw = frappe.cache().get_value(key, use_local_cache=False)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _consume_enqueue_budget(user: str) -> bool:
    """Bound background work per authenticated user; fail closed without Redis."""
    try:
        cache = frappe.cache()
        key = cache.make_key(f"jarz_pos:lead_maps:rate:{user}")
        count = int(cache.incrby(key, 1))
        if count == 1:
            cache.expire(key, RATE_WINDOW_SEC)
        return count <= RATE_MAX_REQUESTS
    except Exception:
        return False


def _poll(request_id: object, user: str) -> dict[str, Any]:
    token = str(request_id or "").strip()
    if not REQUEST_ID_RE.fullmatch(token):
        return _base("", success=False, reason="invalid_request_id")
    ticket = _cache_get(_ticket_key(token))
    if not ticket or ticket.get("user") != user:
        return _base("", success=False, reason="request_expired")
    result = ticket.get("result")
    if isinstance(result, dict):
        return {**result, "request_id": token, "pending": False}
    pending = ticket.get("initial")
    if not isinstance(pending, dict):
        pending = _base(
            str(ticket.get("url") or ""),
            short_link=True,
            reason="short_link_pending",
        )
    pending = {
        **pending,
        "pending": True,
        "reason": (
            "place_details_pending"
            if ticket.get("canonical_url")
            else "short_link_pending"
        ),
    }
    return {
        **pending,
        "request_id": token,
        "retry_after_ms": 500,
        "expires_in": TICKET_TTL_SEC,
    }


def request_preview(
    link: object = None, *, request_id: object = None, user: str = ""
) -> dict[str, Any]:
    """Preview a long URL immediately or queue/poll a short-link preview."""
    owner = str(user or getattr(frappe.session, "user", "") or "").strip()
    if request_id not in (None, ""):
        return _poll(request_id, owner)

    original = str(link or "").strip()
    safe_url = _validated_maps_url(original)
    if not safe_url:
        return _base(original, success=False, reason="invalid_maps_url")
    if not _is_short_url(safe_url):
        initial = _preview_canonical(original, safe_url)
        if initial.get("place_id") and _places_key():
            return _enqueue_preview(
                original,
                owner,
                canonical_url=safe_url,
                initial=initial,
            )
        return initial

    return _enqueue_preview(original, owner)


def _enqueue_preview(
    original: str,
    owner: str,
    *,
    canonical_url: str = "",
    initial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or reuse one user-scoped background preview ticket."""
    is_short = _is_short_url(original)

    dedupe = _dedupe_key(owner, original)
    existing = _cache_get(dedupe).get("request_id")
    if existing:
        current = _poll(existing, owner)
        if current.get("reason") != "request_expired":
            return current
    if not _consume_enqueue_budget(owner):
        return _base(
            original,
            success=False,
            short_link=is_short,
            reason="preview_rate_limited",
            warnings=["Too many map previews; keep the link or try again shortly."],
        )

    token = frappe.generate_hash(length=32)
    ticket = {
        "user": owner,
        "url": original,
        "canonical_url": canonical_url,
        "initial": initial,
    }
    if not _cache_set(_ticket_key(token), ticket) or not _cache_set(
        dedupe, {"request_id": token}, DEDUPE_TTL_SEC
    ):
        return _base(
            original,
            success=False,
            short_link=is_short,
            reason="preview_unavailable",
        )
    try:
        frappe.enqueue(
            "jarz_pos.services.lead_maps.resolve_preview_job",
            queue="short",
            timeout=30,
            job_id=f"lead-map-preview-{token}",
            request_id=token,
            link=original,
            user=owner,
        )
    except Exception:
        failed = _base(
            original,
            success=False,
            short_link=is_short,
            reason="queue_unavailable",
            warnings=["Could not inspect this link; it can still be saved manually."],
        )
        ticket["result"] = failed
        _cache_set(_ticket_key(token), ticket)
        return {**failed, "request_id": token}
    return _poll(token, owner)


def resolve_preview_job(request_id: str, link: str, user: str) -> dict[str, Any]:
    """Background worker for one validated short Maps URL."""
    token = str(request_id or "").strip()
    ticket = _cache_get(_ticket_key(token))
    if not REQUEST_ID_RE.fullmatch(token) or not ticket or ticket.get("user") != user:
        return {"success": False, "reason": "request_expired"}
    original = str(link or "").strip()
    expanded = str(ticket.get("canonical_url") or "").strip()
    if not expanded:
        expanded = _expand_short_link(original)
    if expanded:
        result = _preview_canonical(
            original, expanded, include_place_details=True
        )
    else:
        result = _base(
            original,
            short_link=True,
            reason="short_link_unresolved",
            warnings=["Could not expand the short Google Maps link; it can still be saved."],
        )
    ticket["result"] = result
    _cache_set(_ticket_key(token), ticket)
    return result


def preview(link: object) -> dict[str, Any]:
    """Compatibility helper for deterministic, non-network long-link previews."""
    original = str(link or "").strip()
    safe_url = _validated_maps_url(original)
    if not safe_url:
        return _base(original, success=False, reason="invalid_maps_url")
    if _is_short_url(safe_url):
        return _base(
            original, short_link=True, reason="short_link_needs_expansion"
        )
    return _preview_canonical(original, safe_url)

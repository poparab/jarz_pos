"""Pure geo primitives: Maps-link parsing, distance, and the confidence ladder.

Deliberately dependency-free. Nothing in here touches the database, the session
or Frappe's document layer, so it can be unit-tested without a site and reused
from a scheduled job, a whitelisted call or a backfill script alike. ``frappe``
is imported defensively (mirroring ``utils/cleanup.py``) purely so a logging or
translation call can degrade to a no-op outside a bench.

Why this module exists at all: Jarz captures delivery locations as pasted Google
Maps links (``api/customer.py`` writes ``address_line2 = "Location: <url>"``),
never as coordinates. Turning a link into a usable pin means understanding that
one URL can carry up to four different coordinate pairs which are **not**
equally trustworthy:

``!3d<lat>!4d<lng>``
    The actual place/pin the user selected. This is the answer.
``@<lat>,<lng>,<zoom>z``
    The *viewport centre* at the moment the link was produced. It can sit
    hundreds of metres from the pin — panning the map changes it and nothing
    else. Only a fallback.
``q=<lat>,<lng>`` / ``ll=<lat>,<lng>``
    An explicit coordinate query. Precise when present, but the classic
    ``?q=<place name>`` form carries no coordinates at all.
Plus Code (Open Location Code)
    Self-describing and decodable offline, but only in its *full* form. A short
    code (``4CX2+M7 Cairo``) needs a reference location we do not have, so it is
    rejected rather than guessed.

Short links (``maps.app.goo.gl/...``) carry no coordinates whatsoever — they
must be expanded over HTTP first. :func:`expand_short_link` does that and is
**never** called from :func:`parse_maps_link`, because an inline network call in
a whitelisted endpoint hands every caller a way to hang a gunicorn worker.
"""

from __future__ import annotations

import math
import re
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

try:  # pragma: no cover - during static analysis or docs build
    import frappe
except Exception:
    frappe = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Confidence ladder (COURIER_CONTRACTS §4)
# ─────────────────────────────────────────────────────────────────────────────

#: Integer ranks. **Never compare sources as strings.** Alphabetical ordering
#: puts ``courier_verified`` below ``customer_pin`` and ``pos_link`` above
#: ``manual_override``, which silently inverts the never-downgrade rule.
#:
#: ``manual_override`` sits highest deliberately: an authorised human with
#: context must be able to correct a bad pin and have it stick against consensus.
CONFIDENCE_RANK: Dict[str, int] = {
    "territory_centroid": 10,
    "pos_link": 20,
    "customer_pin": 30,
    "courier_verified": 40,
    "manual_override": 50,
}

#: Convenience aliases so callers never spell a source by hand.
SOURCE_TERRITORY_CENTROID = "territory_centroid"
SOURCE_POS_LINK = "pos_link"
SOURCE_CUSTOMER_PIN = "customer_pin"
SOURCE_COURIER_VERIFIED = "courier_verified"
SOURCE_MANUAL_OVERRIDE = "manual_override"

#: Rank of "no pin at all". Any real source beats it, so a first write always
#: lands.
UNKNOWN_RANK = 0


def normalize_source(source: object) -> str:
    """Lower-case/trim *source* to a ladder key, or ``""`` if it is not one."""
    key = str(source or "").strip().lower()
    return key if key in CONFIDENCE_RANK else ""


def confidence_rank(source: object) -> int:
    """Integer rank of *source*; :data:`UNKNOWN_RANK` for empty/unknown values.

    An unknown label deliberately ranks 0 rather than raising: a Woo re-sync or a
    hand-edited Address must not be able to abort a write path by carrying a
    label this build has never heard of.
    """
    return CONFIDENCE_RANK.get(normalize_source(source), UNKNOWN_RANK)


def accepts_write(current_source: object, incoming_source: object) -> bool:
    """True when a pin sourced from *incoming_source* may overwrite *current_source*.

    The rule (contract §4): accepted only when ``incoming_rank >= current_rank``.
    Equal ranks are accepted — a fresher pin of the same class wins. Lower ranks
    are rejected, and the caller must treat that as a *silent no-op*, not an
    error: a routine Woo re-sync must not fail because the address already
    carries a better pin.
    """
    return confidence_rank(incoming_source) >= confidence_rank(current_source)


def clamp_confidence(
    current_source: object, incoming_source: object
) -> Tuple[str, int]:
    """Return the ``(source, rank)`` that must survive the write.

    The single decision point for the ladder. Returns the incoming pair when the
    write is accepted, otherwise the current pair unchanged — so a caller can
    apply the result blindly and compare it against what it asked for to learn
    whether it won.
    """
    incoming = normalize_source(incoming_source)
    current = normalize_source(current_source)
    if accepts_write(current, incoming):
        return incoming, confidence_rank(incoming)
    return current, confidence_rank(current)


# ─────────────────────────────────────────────────────────────────────────────
# Parse precision labels
# ─────────────────────────────────────────────────────────────────────────────

#: What the parser actually matched. Not the same axis as the confidence ladder:
#: the ladder says *who* supplied the link, this says *how exactly* the link
#: pinpoints a spot. Both matter — a pos_link that only yielded a viewport
#: centre is worth much less than one that yielded a real pin.
PRECISION_PIN = "pin"  # !3d/!4d — the place the user actually selected
PRECISION_VIEWPORT = "viewport"  # @lat,lng — map centre, can be far off
PRECISION_QUERY = "query"  # q=/ll= — explicit coordinate query
PRECISION_PLUS_CODE = "plus_code"  # decoded Open Location Code

#: Rough accuracy radius, metres, implied by each parse precision. Used to seed
#: ``Address.custom_geo_accuracy_m`` when the caller supplies nothing better.
#: These are order-of-magnitude judgements, not measurements — the viewport
#: figure is generous on purpose so a ``@``-only link never reads as a door pin.
PRECISION_ACCURACY_M: Dict[str, float] = {
    PRECISION_PIN: 25.0,
    PRECISION_QUERY: 50.0,
    PRECISION_PLUS_CODE: 15.0,
    PRECISION_VIEWPORT: 500.0,
}

#: Hosts whose links carry no coordinates at all until expanded over HTTP.
SHORT_LINK_HOSTS = frozenset({"maps.app.goo.gl", "app.goo.gl", "goo.gl", "g.co"})


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate validation
# ─────────────────────────────────────────────────────────────────────────────

#: ASCII digits ONLY, deliberately. ``\d`` in Python matches every Unicode
#: decimal digit, and ``float()`` happily parses them — so ``@٣٠.٠,٣١.٢`` would
#: silently become a coordinate. Real Maps URLs are always ASCII here, so an
#: Arabic-Indic numeral in this position is a mangled or hostile input, not a
#: location.
_COORD = r"[-+]?[0-9]{1,3}(?:\.[0-9]+)?"


def is_valid_coordinate(lat: object, lng: object) -> bool:
    """True when *lat*/*lng* are finite, in range, and not Null Island.

    ``(0, 0)`` is rejected because it is overwhelmingly a parse artefact or an
    uninitialised field rather than a genuine delivery in the Gulf of Guinea.
    """
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return False
    if math.isnan(lat_f) or math.isnan(lng_f):
        return False
    if math.isinf(lat_f) or math.isinf(lng_f):
        return False
    if not (-90.0 <= lat_f <= 90.0):
        return False
    if not (-180.0 <= lng_f <= 180.0):
        return False
    if abs(lat_f) < 1e-9 and abs(lng_f) < 1e-9:
        return False
    return True


def _coords(lat: object, lng: object, precision: str) -> Optional[Tuple[float, float, str]]:
    if not is_valid_coordinate(lat, lng):
        return None
    return (float(lat), float(lng), precision)


# ─────────────────────────────────────────────────────────────────────────────
# Open Location Code (Plus Code) decoding
# ─────────────────────────────────────────────────────────────────────────────

_OLC_ALPHABET = "23456789CFGHJMPQRVWX"
_OLC_BASE = 20
_OLC_SEPARATOR = "+"
_OLC_SEPARATOR_POSITION = 8
_OLC_PADDING = "0"
_OLC_PAIR_CODE_LENGTH = 10
_OLC_MAX_DIGITS = 15
_OLC_GRID_ROWS = 5
_OLC_GRID_COLUMNS = 4
_OLC_PAIR_PRECISION = _OLC_BASE**3
_OLC_PAIR_FIRST_PLACE_VALUE = _OLC_BASE ** (_OLC_PAIR_CODE_LENGTH // 2 - 1)
_OLC_GRID_CODE_LENGTH = _OLC_MAX_DIGITS - _OLC_PAIR_CODE_LENGTH
_OLC_GRID_LAT_FIRST_PLACE_VALUE = _OLC_GRID_ROWS ** (_OLC_GRID_CODE_LENGTH - 1)
_OLC_GRID_LNG_FIRST_PLACE_VALUE = _OLC_GRID_COLUMNS ** (_OLC_GRID_CODE_LENGTH - 1)
_OLC_FINAL_LAT_PRECISION = _OLC_PAIR_PRECISION * _OLC_GRID_ROWS**_OLC_GRID_CODE_LENGTH
_OLC_FINAL_LNG_PRECISION = _OLC_PAIR_PRECISION * _OLC_GRID_COLUMNS**_OLC_GRID_CODE_LENGTH

#: A *full* Plus Code: 8 characters, the separator at index 8, then 2+ more.
#: Short codes (``4CX2+M7``) are deliberately NOT matched — recovering them
#: needs a reference location, which is a geocode we are not going to make.
_PLUS_CODE_RE = re.compile(
    r"(?<![0-9A-Za-z+])([23456789CFGHJMPQRVWX]{8}\+[23456789CFGHJMPQRVWX]{2,7})(?![0-9A-Za-z+])",
    re.IGNORECASE,
)


def is_full_plus_code(code: object) -> bool:
    """True when *code* is a decodable full Open Location Code."""
    text = str(code or "").strip().upper()
    if _OLC_SEPARATOR not in text:
        return False
    if text.find(_OLC_SEPARATOR) != _OLC_SEPARATOR_POSITION:
        return False

    body = text.replace(_OLC_SEPARATOR, "")
    # 10 pair digits, then 0–5 grid digits. Enforced here rather than left to the
    # decoder because the pair loop reads ``body[index + 1]``: a 9-character body
    # — trivially constructed from hostile paste — would walk off the end.
    if not (_OLC_PAIR_CODE_LENGTH <= len(body) <= _OLC_MAX_DIGITS):
        return False

    if _OLC_PADDING in body:
        # Padded ("8FVC0000+") codes describe an area kilometres across. Useless
        # as a door pin, so treated as undecodable rather than "decoded badly".
        return False
    return all(ch in _OLC_ALPHABET for ch in body)


def decode_plus_code(code: object) -> Optional[Tuple[float, float]]:
    """Decode a full Plus Code to the centre of the area it names.

    Returns ``None`` for short, padded or malformed codes rather than raising —
    every caller here is parsing untrusted user paste.
    """
    text = str(code or "").strip().upper()
    if not is_full_plus_code(text):
        return None

    body = text.replace(_OLC_SEPARATOR, "")[:_OLC_MAX_DIGITS]

    normal_lat = -90 * _OLC_PAIR_PRECISION
    normal_lng = -180 * _OLC_PAIR_PRECISION
    extra_lat = 0
    extra_lng = 0

    pair_digits = min(len(body), _OLC_PAIR_CODE_LENGTH)
    place_value = _OLC_PAIR_FIRST_PLACE_VALUE
    for index in range(0, pair_digits, 2):
        normal_lat += _OLC_ALPHABET.index(body[index]) * place_value
        normal_lng += _OLC_ALPHABET.index(body[index + 1]) * place_value
        if index < pair_digits - 2:
            place_value //= _OLC_BASE

    lat_resolution = place_value / _OLC_PAIR_PRECISION
    lng_resolution = place_value / _OLC_PAIR_PRECISION

    if len(body) > _OLC_PAIR_CODE_LENGTH:
        row_place_value = _OLC_GRID_LAT_FIRST_PLACE_VALUE
        col_place_value = _OLC_GRID_LNG_FIRST_PLACE_VALUE
        grid_digits = min(len(body), _OLC_MAX_DIGITS)
        for index in range(_OLC_PAIR_CODE_LENGTH, grid_digits):
            digit = _OLC_ALPHABET.index(body[index])
            extra_lat += (digit // _OLC_GRID_COLUMNS) * row_place_value
            extra_lng += (digit % _OLC_GRID_COLUMNS) * col_place_value
            if index < grid_digits - 1:
                row_place_value //= _OLC_GRID_ROWS
                col_place_value //= _OLC_GRID_COLUMNS
        lat_resolution = row_place_value / _OLC_FINAL_LAT_PRECISION
        lng_resolution = col_place_value / _OLC_FINAL_LNG_PRECISION

    lat = normal_lat / _OLC_PAIR_PRECISION + extra_lat / _OLC_FINAL_LAT_PRECISION
    lng = normal_lng / _OLC_PAIR_PRECISION + extra_lng / _OLC_FINAL_LNG_PRECISION

    # Centre of the named cell, clamped to the poles.
    lat_centre = min(lat + lat_resolution / 2, 90.0)
    lng_centre = min(lng + lng_resolution / 2, 180.0)
    return (lat_centre, lng_centre)


# ─────────────────────────────────────────────────────────────────────────────
# Maps link parsing
# ─────────────────────────────────────────────────────────────────────────────

#: ``!3d<lat>!4d<lng>`` inside the ``data=`` protobuf blob. This is the pin.
_PIN_RE = re.compile(rf"!3d({_COORD})!4d({_COORD})")

#: ``@<lat>,<lng>`` — viewport centre. Anything may follow (zoom, tilt, heading).
_AT_RE = re.compile(rf"@({_COORD}),({_COORD})")

#: A bare ``<lat>,<lng>`` value used by ``q=``/``ll=``/``query=``/``center=``.
_PAIR_RE = re.compile(rf"^\s*({_COORD})\s*,\s*({_COORD})\s*$")

#: Query parameters that may hold an explicit coordinate pair.
_COORD_PARAMS = ("q", "ll", "query", "center", "daddr", "destination", "sll", "mlat")


def is_short_maps_link(url: object) -> bool:
    """True when *url* is a redirect shortener that hides its coordinates.

    Callers must resolve these in a **background job** — see
    :func:`expand_short_link`.
    """
    text = str(url or "").strip()
    if not text:
        return False
    try:
        host = (urlparse(text).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return host in SHORT_LINK_HOSTS or host.endswith(".app.goo.gl")


def parse_maps_link(url: object) -> Optional[Tuple[float, float, str]]:
    """Extract ``(lat, lng, precision)`` from a Maps URL, or ``None``.

    **Parse order is load-bearing** (COURIER_CONTRACTS §A7):

    1. ``!3d``/``!4d`` — the pin the user actually chose;
    2. ``@lat,lng`` — the viewport centre, which can be far from the pin;
    3. ``q=``/``ll=`` and friends — an explicit coordinate query;
    4. a full Plus Code anywhere in the URL.

    Never raises and never performs I/O. A short link returns ``None`` here by
    design: expanding it is a network round-trip and belongs in a background
    job, never inline in a whitelisted call.
    """
    text = str(url or "").strip()
    if not text:
        return None

    # Percent-decoding matters: a pasted link is frequently URL-encoded once
    # (``%2C`` for the comma, ``%40`` for ``@``) by whatever chat app carried it.
    # Two passes covers the common double-encoding without looping forever on a
    # crafted input.
    candidates = [text]
    decoded = text
    for _ in range(2):
        try:
            nxt = unquote(decoded)
        except Exception:
            break
        if nxt == decoded:
            break
        decoded = nxt
        candidates.append(decoded)

    for haystack in candidates:
        match = _PIN_RE.search(haystack)
        if match:
            found = _coords(match.group(1), match.group(2), PRECISION_PIN)
            if found:
                return found

    for haystack in candidates:
        match = _AT_RE.search(haystack)
        if match:
            found = _coords(match.group(1), match.group(2), PRECISION_VIEWPORT)
            if found:
                return found

    for haystack in candidates:
        found = _parse_query_coords(haystack)
        if found:
            return found

    for haystack in candidates:
        match = _PLUS_CODE_RE.search(haystack)
        if match:
            decoded_code = decode_plus_code(match.group(1))
            if decoded_code:
                found = _coords(decoded_code[0], decoded_code[1], PRECISION_PLUS_CODE)
                if found:
                    return found

    return None


def _parse_query_coords(url: str) -> Optional[Tuple[float, float, str]]:
    """``?q=<lat>,<lng>`` / ``?ll=`` / ``?query=`` and the other coordinate params."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query or "", keep_blank_values=False)
    except Exception:
        return None

    for key in _COORD_PARAMS:
        for value in params.get(key, []) or []:
            match = _PAIR_RE.match(str(value or ""))
            if not match:
                continue
            found = _coords(match.group(1), match.group(2), PRECISION_QUERY)
            if found:
                return found
    return None


def accuracy_for_precision(precision: object, fallback: Optional[float] = None) -> Optional[float]:
    """Rough accuracy radius in metres implied by a parse *precision*."""
    key = str(precision or "").strip().lower()
    if key in PRECISION_ACCURACY_M:
        return PRECISION_ACCURACY_M[key]
    return fallback


def extract_location_link(text: object) -> str:
    """Pull the URL out of an ``address_line2`` written as ``"Location: <url>"``.

    ``api/customer.py`` has been writing that string since the POS shipped, and
    it is the entire backfill corpus. Returns ``""`` when the line holds no URL.

    This function **reads only**. ``address_line2`` is part of the WooCommerce
    address-dedup signature *and* of the Woo outbound-push trigger set, so
    rewriting it forks duplicate Addresses and fans a customer+invoice sync out
    to WooCommerce per record.
    """
    line = str(text or "").strip()
    if not line:
        return ""
    match = re.search(r"https?://\S+", line)
    return match.group(0).rstrip(".,;)") if match else ""


# ─────────────────────────────────────────────────────────────────────────────
# Distance
# ─────────────────────────────────────────────────────────────────────────────

#: Mean Earth radius, metres (IUGG). Haversine on a sphere is good to ~0.5% —
#: far tighter than GPS noise at the scales this app cares about (tens to
#: thousands of metres), and it needs no projection or library.
EARTH_RADIUS_M = 6371008.8


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points, in metres.

    Raises ``ValueError`` on an out-of-range coordinate: silently returning 0.0
    would read as "delivered exactly on the pin", which is precisely the
    conclusion the anomaly work must never reach by accident.
    """
    if not is_valid_coordinate(lat1, lng1) or not is_valid_coordinate(lat2, lng2):
        raise ValueError("haversine_m requires two valid coordinate pairs")

    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    d_phi = math.radians(float(lat2) - float(lat1))
    d_lambda = math.radians(float(lng2) - float(lng1))

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def distance_m_or_none(
    lat1: object, lng1: object, lat2: object, lng2: object
) -> Optional[float]:
    """:func:`haversine_m` that returns ``None`` instead of raising."""
    try:
        return haversine_m(float(lat1), float(lng1), float(lat2), float(lng2))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Short-link expansion — BACKGROUND JOBS ONLY
# ─────────────────────────────────────────────────────────────────────────────

def expand_short_link(url: object, *, timeout: float = 5.0, max_hops: int = 5) -> str:
    """Follow a ``maps.app.goo.gl`` redirect chain to the real URL.

    **Never call this from a whitelisted endpoint or a document hook.** It makes
    a blocking HTTP request; inline, it hands any caller a way to occupy a
    gunicorn worker for the length of the timeout, and one slow shortener
    becomes a site-wide stall. ``services.geo_resolution`` enqueues it instead.

    Returns the expanded URL, or ``""`` on any failure. Never raises.
    """
    text = str(url or "").strip()
    if not text or not text.lower().startswith(("http://", "https://")):
        return ""

    try:
        import urllib.request

        request = urllib.request.Request(
            text,
            method="HEAD",
            headers={"User-Agent": "jarz-pos-geo/1.0"},
        )

        class _Limited(urllib.request.HTTPRedirectHandler):
            max_redirections = max_hops

        opener = urllib.request.build_opener(_Limited)
        with opener.open(request, timeout=timeout) as response:
            final = getattr(response, "url", "") or response.geturl()
        return str(final or "")
    except Exception:
        # HEAD is refused by some shorteners; retry once with GET before giving
        # up, since the redirect header is all we need either way.
        try:
            import urllib.request

            request = urllib.request.Request(
                text, headers={"User-Agent": "jarz-pos-geo/1.0"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return str(getattr(response, "url", "") or response.geturl() or "")
        except Exception:
            return ""

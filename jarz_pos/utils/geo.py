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
import time
from collections.abc import Iterable
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

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
CONFIDENCE_RANK: dict[str, int] = {
	"territory_centroid": 10,
	"pos_link": 20,
	"customer_pin": 30,
	# A courier standing at the door beats a customer's remote pin, but a web
	# capture carries no mock-GPS evidence (geolocator_web has no isMocked, and
	# iOS exposes no mock-provider signal at all), so it must never outrank a
	# verified Android capture. Deliberately between the two.
	"courier_web": 35,
	"courier_verified": 40,
	"manual_override": 50,
}

#: Convenience aliases so callers never spell a source by hand.
SOURCE_TERRITORY_CENTROID = "territory_centroid"
SOURCE_POS_LINK = "pos_link"
SOURCE_CUSTOMER_PIN = "customer_pin"
SOURCE_COURIER_WEB = "courier_web"
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


#: Highest rank an explicit POS staff edit may replace: a customer's own pin
#: (what the website or a Woo order supplied). Courier-sourced pins
#: (``courier_web`` and up) and ``manual_override`` are never replaceable by it.
STAFF_CORRECTABLE_MAX_RANK = CONFIDENCE_RANK["customer_pin"]


def accepts_staff_correction(current_source: object, incoming_source: object) -> bool:
	"""True when an explicit POS staff edit may replace *current_source* below the ladder.

	Only a ``pos_link`` write qualifies (the POS app's stored source), and only
	over a pin at or below ``customer_pin``: staff looking at a customer's wrong
	pin may replace it, but never a courier's door-step fix or a manager's
	override. This widens the ladder for ONE case; every other caller keeps
	:func:`accepts_write`. The caller is responsible for requiring an explicit
	opt-in from a whitelisted POS endpoint and for checking the coordinates
	actually changed.
	"""
	return (
		normalize_source(incoming_source) == SOURCE_POS_LINK
		and confidence_rank(current_source) <= STAFF_CORRECTABLE_MAX_RANK
	)


def clamp_confidence(current_source: object, incoming_source: object) -> tuple[str, int]:
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
PRECISION_ACCURACY_M: dict[str, float] = {
	PRECISION_PIN: 25.0,
	PRECISION_QUERY: 50.0,
	PRECISION_PLUS_CODE: 15.0,
	PRECISION_VIEWPORT: 500.0,
}

#: Hosts whose links carry no coordinates at all until expanded over HTTP.
#:
#: ``share.google`` is the shortener the current Android share sheet emits. It
#: was missing here, so every link staff produced the normal way (share -> copy)
#: failed the "is this expandable?" check and was reported as an unreadable
#: link rather than queued for expansion.
SHORT_LINK_HOSTS = frozenset({"maps.app.goo.gl", "app.goo.gl", "goo.gl", "g.co", "share.google"})

#: Hosts a redirect chain may land on. Expansion follows hops one at a time and
#: re-checks the target against this set plus :data:`SHORT_LINK_HOSTS`, so a
#: shortener that has been made to point somewhere else cannot turn a POS
#: request into a fetch of an arbitrary URL.
MAPS_LINK_HOSTS = frozenset(
	{
		"google.com",
		"www.google.com",
		"maps.google.com",
		"google.com.eg",
		"www.google.com.eg",
		"maps.google.com.eg",
		"consent.google.com",
		"plus.codes",
		"www.plus.codes",
	}
)


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate validation
# ─────────────────────────────────────────────────────────────────────────────

#: ASCII digits ONLY, deliberately. ``\d`` in Python matches every Unicode
#: decimal digit, and ``float()`` happily parses them — so fullwidth or other
#: exotic numerals would silently become a coordinate. Arabic-Indic numerals are
#: the one exception staff genuinely type (an Arabic keyboard produces
#: ``٣٠٫٠٤٤٤، ٣١٫٢٣٥٧``), and :data:`_ARABIC_NUMERALS` translates exactly those
#: to ASCII *before* matching, so the pattern itself never widens.
_COORD = r"[-+]?[0-9]{1,3}(?:\.[0-9]+)?"

#: Arabic-Indic (U+0660–0669) and Extended/Persian (U+06F0–06F9) digits, the
#: Arabic decimal separator ``٫`` and the Arabic comma ``،``, mapped to ASCII.
_ARABIC_NUMERALS = str.maketrans(
	{
		**{chr(0x0660 + i): str(i) for i in range(10)},
		**{chr(0x06F0 + i): str(i) for i in range(10)},
		"٫": ".",
		"،": ",",
	}
)


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


#: Egypt's bounding box (lat_min, lat_max, lng_min, lng_max), with a little
#: margin. Every Jarz branch delivers inside it, so a link that resolves
#: elsewhere (Paris, a swapped lat/lng, a coordinate pasted from another app)
#: is a mistake, never a delivery. The same box guards the Woo inbound parser.
SERVICE_AREA = (22.0, 32.0, 24.5, 37.0)


def in_service_area(lat: object, lng: object) -> bool:
	"""True when *lat*/*lng* is a valid coordinate inside :data:`SERVICE_AREA`.

	Kept out of :func:`parse_maps_link` on purpose: the parser answers "what
	point does this link name", and this answers "can we deliver there". Callers
	that take a pin from a pasted link apply it; trusted sources (courier GPS,
	manual override) are not gated by it.
	"""
	if not is_valid_coordinate(lat, lng):
		return False
	lat_min, lat_max, lng_min, lng_max = SERVICE_AREA
	return lat_min <= float(lat) <= lat_max and lng_min <= float(lng) <= lng_max


#: Google Maps "search" URL. The documented, stable form for "drop a pin here":
#: it opens the app on Android/iOS and the web map everywhere else.
_MAPS_SEARCH_URL = "https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"


def maps_link_for(lat: object, lng: object) -> str:
	"""A tappable Google Maps link for *lat*/*lng*, or ``""`` when invalid.

	Validity is :func:`is_valid_coordinate` — finite, in range, not Null Island
	— so a half-written or zeroed Address can never produce a link to the Gulf
	of Guinea. Six decimals is ~11 cm, finer than any source we store.
	"""
	if not is_valid_coordinate(lat, lng):
		return ""
	return _MAPS_SEARCH_URL.format(lat=float(lat), lng=float(lng))


def _coords(lat: object, lng: object, precision: str) -> tuple[float, float, str] | None:
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


def decode_plus_code(code: object) -> tuple[float, float] | None:
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
#: ``loc:`` is the legacy "drop a pin" prefix (``?q=loc:30.04,31.23``); ``~`` is
#: Bing's separator (``cp=30.04~31.23``). A ``+`` before the longitude is what a
#: share sheet leaves when it encodes the space after the comma.
_PAIR_RE = re.compile(rf"^\s*(?:loc:)?\s*({_COORD})\s*[,~]\s*\+?({_COORD})\s*$", re.IGNORECASE)

#: Query parameters that may hold an explicit coordinate pair. ``coordinate``
#: is Apple Maps; ``cp`` is Bing.
_COORD_PARAMS = ("q", "ll", "query", "center", "daddr", "destination", "sll", "coordinate", "cp")

#: ``/maps/place/<lat>,<lng>`` and ``/maps/search/<lat>,+<lng>`` — the pair sits
#: in the path, not a parameter. This is the point Maps was asked to show, so it
#: outranks the ``@`` viewport that usually follows it.
_PATH_PAIR_RE = re.compile(rf"/maps/(?:place|search)/({_COORD})\s*,\s*\+?\s*({_COORD})(?![0-9.])")

#: OpenStreetMap: ``#map=<zoom>/<lat>/<lng>`` — a viewport, like Google's ``@``.
_OSM_HASH_RE = re.compile(rf"#map=[0-9]{{1,2}}/({_COORD})/({_COORD})")

#: A whole input that is nothing but a coordinate pair, as typed or copied from
#: the Maps "what's here?" card: ``30.0444, 31.2357`` / ``30.0444 31.2357`` /
#: ``30.0444;31.2357`` / ``30.0444° N, 31.2357° E`` / ``(30.0444, 31.2357)``.
#: Anchored at both ends on purpose: a pair embedded in prose ("Villa 12, 3rd
#: floor") must never become a pin, and both halves need a decimal point, so a
#: pair of house or block numbers ("12 5", "30, 31") never reads as one either.
_HEMI = r"\s*°?\s*([NSEW])?"
_DECIMAL = r"[-+]?[0-9]{1,3}\.[0-9]+"
_BARE_PAIR_RE = re.compile(
	rf"^\s*\(?\s*({_DECIMAL}){_HEMI}\s*(?:[,;]\s*|\s+)({_DECIMAL}){_HEMI}\s*\)?\s*$",
	re.IGNORECASE,
)

#: Degrees/minutes/seconds as Google copies it: ``30°02'39.8"N 31°14'08.6"E``.
_DMS = r"([0-9]{1,3})\s*°\s*([0-9]{1,2})\s*['′]\s*([0-9]{1,2}(?:\.[0-9]+)?)\s*(?:\"|″|'')?\s*([NSEW])"
_DMS_PAIR_RE = re.compile(rf"^\s*{_DMS}\s*,?\s*{_DMS}\s*$", re.IGNORECASE)


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


#: A place's feature id inside a Maps ``data=`` blob (``!1s0x<hex>:0x<hex>``).
_PLACE_FEATURE_ID_RE = re.compile(r"!1s0x[0-9a-f]+:0x[0-9a-f]+", re.IGNORECASE)


def is_place_only_link(url: object) -> bool:
	"""True when *url* names a Google Maps **place** but carries no coordinates.

	Sharing a shop or landmark from Maps (rather than a dropped pin) produces a
	``/maps/place/<name>/data=...!1s0x..:0x..`` URL, or ``?q=<name>&ftid=``, or
	``?cid=``. Those identify the place by Google's own id, which only the
	Places API can turn into a point. Callers use this to tell staff to share a
	dropped pin instead of reporting a generic "no coordinates".

	Only meaningful after :func:`parse_maps_link` has returned ``None``: a place
	URL that also carries ``!3d``/``!4d`` or ``@lat,lng`` is parseable and is
	not a place-only link.
	"""
	text = str(url or "").strip()
	if not text:
		return False
	try:
		parts = urlparse(text)
	except Exception:
		return False
	host = (parts.hostname or "").lower()
	if host not in MAPS_LINK_HOSTS:
		return False
	path = unquote(parts.path or "")
	if "/maps/place/" in path or "/maps/search/" in path:
		return True
	if _PLACE_FEATURE_ID_RE.search(path):
		return True
	query = parse_qs(parts.query or "")
	return bool(query.get("cid") or query.get("ftid"))


def parse_maps_link(url: object) -> tuple[float, float, str] | None:
	"""Extract ``(lat, lng, precision)`` from a Maps URL, or ``None``.

	**Parse order is load-bearing** (COURIER_CONTRACTS §A7):

	1. ``!3d``/``!4d`` — the pin the user actually chose;
	2. ``@lat,lng`` — the viewport centre, which can be far from the pin;
	3. ``q=``/``ll=`` and friends — an explicit coordinate query;
	4. a full Plus Code anywhere in the URL;
	5. an input that is *only* a coordinate pair (typed, or copied from the
	   Maps card), in decimal or degrees/minutes/seconds.

	A ``/maps/place/<lat>,<lng>`` path pair is checked between 1 and 2: it is
	the point Maps was asked to show, and the ``@`` that follows is only where
	the map happened to be centred.

	Never raises and never performs I/O. A short link returns ``None`` here by
	design: expanding it is a network round-trip and belongs in a background
	job, never inline in a whitelisted call.
	"""
	text = str(url or "").strip().translate(_ARABIC_NUMERALS)
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
		match = _PATH_PAIR_RE.search(haystack)
		if match:
			found = _coords(match.group(1), match.group(2), PRECISION_QUERY)
			if found:
				return found

	for haystack in candidates:
		match = _AT_RE.search(haystack) or _OSM_HASH_RE.search(haystack)
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

	for haystack in candidates:
		found = _parse_bare_pair(haystack)
		if found:
			return found

	return None


def _signed(value: float, hemisphere: str | None) -> float:
	"""Apply an ``S``/``W`` hemisphere letter as a negative sign."""
	return -abs(value) if (hemisphere or "").upper() in ("S", "W") else value


def _ordered_pair(
	first: float, first_hemi: str | None, second: float, second_hemi: str | None
) -> tuple[float, float] | None:
	"""``(lat, lng)`` from two values whose hemisphere letters may say which is which.

	No letters means "latitude first", the order every map app writes. Letters
	that name the same axis twice (``N … N``) are contradictory and rejected.
	"""
	a = (first_hemi or "").upper()
	b = (second_hemi or "").upper()
	lat_letters, lng_letters = ("N", "S"), ("E", "W")
	if (a in lat_letters and b in lat_letters) or (a in lng_letters and b in lng_letters):
		return None
	if a in lng_letters or b in lat_letters:
		return (_signed(second, b), _signed(first, a))
	return (_signed(first, a), _signed(second, b))


def _parse_bare_pair(text: str) -> tuple[float, float, str] | None:
	"""An input that is nothing but a decimal or DMS coordinate pair."""
	match = _BARE_PAIR_RE.match(text)
	if match:
		pair = _ordered_pair(float(match.group(1)), match.group(2), float(match.group(3)), match.group(4))
		if pair:
			return _coords(pair[0], pair[1], PRECISION_QUERY)
		return None

	match = _DMS_PAIR_RE.match(text)
	if match:
		g = match.groups()
		try:
			first = int(g[0]) + int(g[1]) / 60 + float(g[2]) / 3600
			second = int(g[4]) + int(g[5]) / 60 + float(g[6]) / 3600
		except ValueError:
			return None
		if int(g[1]) >= 60 or float(g[2]) >= 60 or int(g[5]) >= 60 or float(g[6]) >= 60:
			return None
		pair = _ordered_pair(first, g[3], second, g[7])
		if pair:
			return _coords(pair[0], pair[1], PRECISION_QUERY)
	return None


def _parse_query_coords(url: str) -> tuple[float, float, str] | None:
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

	# OpenStreetMap marker: the pair is split across ``mlat=`` and ``mlon=``.
	for lat_value in params.get("mlat", []) or []:
		for lng_value in params.get("mlon", []) or []:
			lat_match = re.fullmatch(_COORD, str(lat_value or "").strip())
			lng_match = re.fullmatch(_COORD, str(lng_value or "").strip())
			if lat_match and lng_match:
				found = _coords(lat_match.group(0), lng_match.group(0), PRECISION_QUERY)
				if found:
					return found
	return None


def accuracy_for_precision(precision: object, fallback: float | None = None) -> float | None:
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


def stored_location_link(location_link: object, address_line2: object = None) -> str:
	"""The Maps link kept on an Address, for when it has no usable pin.

	``custom_location_link`` (the raw link POS staff pasted) wins; older rows
	only have the legacy ``"Location: <url>"`` text in ``address_line2``.
	Reads only.
	"""
	return extract_location_link(location_link) or extract_location_link(address_line2)


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

	a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
	return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def distance_m_or_none(lat1: object, lng1: object, lat2: object, lng2: object) -> float | None:
	""":func:`haversine_m` that returns ``None`` instead of raising."""
	try:
		return haversine_m(float(lat1), float(lng1), float(lat2), float(lng2))
	except Exception:
		return None


# ─────────────────────────────────────────────────────────────────────────────
# Delivery reference points — swapped-pair detection and distance
# ─────────────────────────────────────────────────────────────────────────────

#: Greater Cairo (Tahrir). The reference used when the site holds no delivery
#: coordinates at all (no Territory / POS Profile carries a centroid), which is
#: the case today: the business delivers in Greater Cairo.
GREATER_CAIRO = (30.0444, 31.2357)

#: A swapped reading must land this close to a delivery reference...
SWAP_NEAR_M = 40_000.0
#: ...and the typed reading must be this far from every one of them, otherwise
#: the typed order is kept. The gap between the two is the "ambiguous" band.
SWAP_FAR_M = 80_000.0


def is_unlabelled_bare_pair(text: object) -> bool:
	"""True when *text* is nothing but a typed ``a, b`` decimal pair, no letters.

	Only this shape can be a latitude/longitude transposition. A URL names its
	axes (``!3d``/``!4d``, ``@lat,lng``, ``q=``) and a pair carrying hemisphere
	letters (``30.04 N, 31.23 E``, DMS) states its own order, so neither is ever
	reordered. Pure; never raises.
	"""
	try:
		match = _BARE_PAIR_RE.match(str(text or "").strip().translate(_ARABIC_NUMERALS))
	except Exception:
		return False
	return bool(match) and not (match.group(2) or match.group(4))


def nearest_reference(
	lat: object, lng: object, points: Iterable[tuple[float, float]] | None
) -> tuple[float, tuple[float, float]] | None:
	"""``(distance_m, point)`` for the *points* entry nearest to *lat*/*lng*, or ``None``."""
	best: tuple[float, tuple[float, float]] | None = None
	for point in points or ():
		gap = distance_m_or_none(lat, lng, point[0], point[1])
		if gap is not None and (best is None or gap < best[0]):
			best = (gap, (float(point[0]), float(point[1])))
	return best


def maybe_swap_bare_pair(
	lat: object, lng: object, points: Iterable[tuple[float, float]] | None
) -> tuple[float, float, bool]:
	"""``(lat, lng, swapped)`` for a typed pair that may have its axes transposed.

	``31.2357, 30.0444`` (Cairo typed longitude-first) is a *valid* Egypt point
	near Alexandria, so the service-area box cannot tell. When BOTH orders fall
	inside :data:`SERVICE_AREA`, the delivery reference *points* decide: swap only
	if the swapped order is within :data:`SWAP_NEAR_M` of a reference AND the
	typed order is farther than :data:`SWAP_FAR_M` from all of them. Anything
	less clear-cut keeps the typed order. Pure; never raises; with no *points*
	nothing is swapped (the caller supplies the Cairo fallback).
	"""
	try:
		typed = (float(lat), float(lng))
		swapped = (typed[1], typed[0])
		refs = list(points or [])
		if not refs or typed == swapped:
			return typed[0], typed[1], False
		if not (in_service_area(*typed) and in_service_area(*swapped)):
			return typed[0], typed[1], False
		typed_near = nearest_reference(typed[0], typed[1], refs)
		swapped_near = nearest_reference(swapped[0], swapped[1], refs)
		if typed_near is None or swapped_near is None:
			return typed[0], typed[1], False
		if swapped_near[0] <= SWAP_NEAR_M and typed_near[0] > SWAP_FAR_M:
			return swapped[0], swapped[1], True
	except Exception:
		pass
	return lat, lng, False  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# Short-link expansion — BACKGROUND JOBS ONLY
# ─────────────────────────────────────────────────────────────────────────────


def _hop_is_permitted(url: str) -> bool:
	"""True when *url* is an HTTPS address on a host expansion may fetch."""
	try:
		parsed = urlparse(url)
	except Exception:
		return False
	if (parsed.scheme or "").lower() != "https":
		return False
	host = (parsed.hostname or "").lower().rstrip(".")
	if not host:
		return False
	return host in SHORT_LINK_HOSTS or host in MAPS_LINK_HOSTS or host.endswith(".app.goo.gl")


def _https_upgraded(url: str) -> str:
	"""Rewrite a plain-HTTP shortener to HTTPS. Never downgrades.

	The legacy ``address_line2`` corpus still holds ``http://goo.gl/maps/...``
	links from before the shorteners moved. Refusing them outright would drop
	rows the backfill can still resolve, and every one of these hosts serves the
	same redirect over TLS.
	"""
	if not url.lower().startswith("http://"):
		return url
	upgraded = "https://" + url[len("http://") :]
	return upgraded if _hop_is_permitted(upgraded) else url


def _redirect_status_and_headers(url: str, method: str, timeout: float):
	"""One request, redirects NOT followed, body never read."""
	import urllib.error
	import urllib.request

	class _NoRedirect(urllib.request.HTTPRedirectHandler):
		def redirect_request(self, req, fp, code, msg, headers, newurl):
			return None

	request = urllib.request.Request(url, method=method, headers={"User-Agent": "jarz-pos-geo/1.0"})
	try:
		response = urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
		try:
			return int(getattr(response, "status", 200)), response.headers
		finally:
			response.close()
	except urllib.error.HTTPError as exc:
		try:
			return int(exc.code), dict(exc.headers or {})
		finally:
			exc.close()


def expand_short_link(url: object, *, timeout: float = 5.0, max_hops: int = 5) -> str:
	"""Follow a Maps shortener's redirect chain to the real URL.

	**Never call this from a whitelisted endpoint or a document hook.** It makes
	a blocking HTTP request; inline, it hands any caller a way to occupy a
	gunicorn worker for the length of the timeout, and one slow shortener
	becomes a site-wide stall. ``services.geo_resolution`` enqueues it instead.

	Redirects are followed **one hop at a time** and every hop is re-checked
	against :func:`_hop_is_permitted`, rather than handed to urllib's automatic
	follower. The starting host is constrained by the caller, but the hops are
	chosen by a remote server: letting urllib chase them turns "expand this
	short link" into "fetch whatever Google's shortener now points at", which is
	a server-side request forgery with a POS user's session behind it. The body
	is never read for the same reason — only the ``Location`` header matters.

	Returns the expanded URL, or ``""`` on any failure. Never raises.
	"""
	current = _https_upgraded(str(url or "").strip())
	if not _hop_is_permitted(current):
		return ""

	# One deadline for the whole chain, not per hop: five hops each granted the
	# full timeout is a 25-second job holding a worker in the short queue.
	deadline = time.monotonic() + max(0.25, min(float(timeout), 8.0))

	for _ in range(max(1, min(int(max_hops), 8)) + 1):
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			return ""
		try:
			status, headers = _redirect_status_and_headers(current, "HEAD", remaining)
			# Some shorteners refuse HEAD outright; the redirect header is all
			# we need either way, so retry the same hop with GET.
			if status in (405, 501):
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					return ""
				status, headers = _redirect_status_and_headers(current, "GET", remaining)
		except Exception:
			return ""

		if 300 <= status < 400:
			location = str((headers or {}).get("Location") or "").strip()
			if not location:
				return ""
			nxt = urljoin(current, location)
			if not _hop_is_permitted(nxt):
				return ""
			current = nxt
			continue

		if not (200 <= status < 300):
			return ""
		# Landing back on a shortener means the chain never resolved.
		return "" if is_short_maps_link(current) else current

	return ""

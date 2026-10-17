"""The unrestricted writer of the six ``Address`` geo fields.

``custom_latitude``, ``custom_longitude``, ``custom_geo_source``,
``custom_geo_confidence``, ``custom_geo_accuracy_m`` and
``custom_geo_verified_on`` have exactly **two** authorised writers (contract §3):
this module, which may write any source on the ladder, and the WooCommerce app's
``geo_passthrough``, which is hard-capped at ``customer_pin``. Nothing else
writes them — not a Desk form, not a patch, not ``api/customer.py``, which routes
through here precisely so the ladder cannot be bypassed. Concentrating the writes
is what makes the confidence ladder enforceable at all: "never downgrade a pin"
is only true if the places that could break it are few and deliberate.

There is deliberately **no** "sole writer" test. It would fail against the Woo
lane by design.

Two design decisions worth stating plainly.

**Ranks, never string comparison.** ``courier_verified`` sorts *below*
``customer_pin`` alphabetically, and ``pos_link`` sorts *above*
``manual_override``. A lexicographic "is the new source better?" check inverts
the ladder for two of the five sources and does so silently. Everything here
goes through :data:`jarz_pos.utils.geo.CONFIDENCE_RANK`.

**A rejected write is a no-op, not an error.** Woo re-syncs an address every
time an order changes. If a lower-confidence write raised, a customer whose door
pin a courier had already verified would start failing order sync. Rejections
return ``{"success": True, "accepted": False, ...}`` and say why.

**Writes go through ``frappe.db.set_value``, deliberately, not ``doc.save()``.**
``address_line2`` sits in both the WooCommerce address-dedup signature and the
Woo outbound-push trigger set; a full save re-runs Address validation and every
registered ``on_update`` hook over a document we only want to stamp six numbers
onto. ``db.set_value`` fires no document events and touches nothing else. It is
called with ``update_modified=False`` for the same reason
``invoice_return._park_return_in_returned_column`` is: leaving ``modified``
alone keeps any concurrently-loaded copy of the Address saveable instead of
handing it a ``TimestampMismatchError``.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import frappe
from frappe import _
from frappe.utils import now_datetime

from jarz_pos.constants import ROLES
from jarz_pos.utils import geo

#: The six fields this module owns. Nothing outside may write them.
GEO_FIELDS = (
	"custom_latitude",
	"custom_longitude",
	"custom_geo_source",
	"custom_geo_confidence",
	"custom_geo_accuracy_m",
	"custom_geo_verified_on",
)

#: Optional seventh column: when the pin was last written. ``Address.modified``
#: cannot say (the writes below pass ``update_modified=False``), and the order
#: board needs it to tell a staff correction made AFTER an order's pin snapshot
#: from the pin the order was taken with (``utils.address_pins``). Not part of
#: :data:`GEO_FIELDS`: a site that has not migrated it still writes pins, it just
#: cannot date them. Stamped on every accepted write, whatever the source.
GEO_UPDATED_FIELD = "custom_geo_updated_on"

#: Fields that must never appear in a geo write. ``address_line2`` carries the
#: pasted Maps link and is part of Woo's dedup signature and outbound trigger
#: set; the rest are the remainder of that trigger set. Listed explicitly so the
#: guard below can assert rather than rely on everyone remembering.
WOO_TRIGGER_FIELDS = frozenset(
	{
		"address_line1",
		"address_line2",
		"city",
		"state",
		"pincode",
		"country",
		"phone",
		"email_id",
		"address_type",
		"is_shipping_address",
	}
)

#: Roles allowed to plant a ``manual_override`` pin. Highest rank on the ladder,
#: so it beats a courier-verified consensus — that is the point, and it is why it
#: is gated and logged rather than open to every POS user.
MANUAL_OVERRIDE_ROLES = ROLES.LINE_MANAGER_TIER_LOWER


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def geo_fields_present() -> bool:
	"""True when this site has already migrated the six Address geo fields."""
	try:
		meta = frappe.get_meta("Address")
	except Exception:
		return False
	return all(meta.get_field(field) for field in GEO_FIELDS)


def _has_address_field(fieldname: str) -> bool:
	"""True when Address carries *fieldname* on this site. Never raises."""
	try:
		return bool(frappe.get_meta("Address").has_field(fieldname))
	except Exception:
		return False


def _assert_geo_fields() -> None:
	"""Fail loudly when the schema is behind the code.

	Without this the ``db.set_value`` below would raise a raw column error, or —
	worse on a partially migrated site — write some fields and skip others,
	leaving ``custom_geo_source`` and ``custom_geo_confidence`` disagreeing.
	"""
	try:
		meta = frappe.get_meta("Address")
	except Exception as exc:  # pragma: no cover - meta failure is environmental
		frappe.throw(_("Could not read Address metadata: {0}").format(str(exc)))

	missing = [field for field in GEO_FIELDS if not meta.get_field(field)]
	if missing:
		frappe.throw(
			_(
				"Address is missing the geo fields {0}. Run `bench migrate` — this "
				"deployment is ahead of the site's schema."
			).format(", ".join(missing))
		)


def _assert_no_woo_trigger_fields(updates: dict[str, Any]) -> None:
	"""Refuse any write that would reach WooCommerce.

	The geo fields are safe by construction because Woo's Address outbound hooks
	are field-gated on a list containing no ``custom_*`` field. That safety
	disappears the instant a text edit is bundled into the same write, so the
	bundle is made impossible rather than merely discouraged.
	"""
	trespass = sorted(set(updates) & WOO_TRIGGER_FIELDS)
	if trespass:
		frappe.throw(
			_(
				"geo_resolution may not write {0}: those fields are in the "
				"WooCommerce dedup signature and outbound trigger set."
			).format(", ".join(trespass))
		)


def get_address_geo(address_name: str) -> dict[str, Any]:
	"""Current geo state of an Address, plus its derived rank. Read-only.

	Returns ``{}`` — falsy — when the Address does not exist, so callers can
	tell "no such address" from "an address with no pin yet". Those two must not
	collapse: the second is the normal first-write case and must be accepted,
	the first must be refused.
	"""
	name = str(address_name or "").strip()
	if not name:
		return {}
	row = frappe.db.get_value("Address", name, list(GEO_FIELDS), as_dict=True)
	if not row:
		return {}
	data = dict(row)
	data["rank"] = geo.confidence_rank(data.get("custom_geo_source"))
	return data


def _can_manual_override(user: str | None = None) -> bool:
	resolved = user or frappe.session.user
	if resolved == ROLES.ADMINISTRATOR:
		return True
	try:
		roles = {str(r or "").strip().lower() for r in frappe.get_roles(resolved)}
	except Exception:
		return False
	return not roles.isdisjoint(MANUAL_OVERRIDE_ROLES)


# ─────────────────────────────────────────────────────────────────────────────
# The ladder decision — shared by the write path and the dry run
# ─────────────────────────────────────────────────────────────────────────────


def evaluate_pin_write(
	address_name: str,
	*,
	latitude: Any,
	longitude: Any,
	source: str,
	accuracy_m: Any = None,
	staff_correction: bool = False,
) -> dict[str, Any]:
	"""Decide what a pin write *would* do. Writes nothing.

	Shared by :func:`set_address_pin` and the API dry-run so the two can never
	disagree about the outcome — the failure mode a separate "preview"
	implementation always eventually produces.

	``staff_correction`` is the one deliberate widening of the ladder: an
	EXPLICIT edit made by POS staff in the app may replace a ``customer_pin``
	(or a lower pin) with a ``pos_link`` one, because staff looking at the map
	know better than the pin a customer's phone sent. It never replaces a
	``courier_*`` or ``manual_override`` pin, and only when the coordinates
	actually change (re-saving the same point must not relabel its source).
	Only whitelisted POS endpoints pass it; backfills, the short-link job and the
	Woo lane never do. The stored source stays ``pos_link`` and the rank written
	is the incoming one, so the ladder is not otherwise touched.
	"""
	name = str(address_name or "").strip()
	incoming_source = geo.normalize_source(source)

	if not name:
		return {"accepted": False, "reason": "missing_address"}
	if not incoming_source:
		return {"accepted": False, "reason": "unknown_source", "source": str(source or "")}
	if not geo.is_valid_coordinate(latitude, longitude):
		return {"accepted": False, "reason": "invalid_coordinates"}

	current = get_address_geo(name)
	if not current:
		return {"accepted": False, "reason": "address_not_found"}

	current_source = geo.normalize_source(current.get("custom_geo_source"))
	incoming_rank = geo.confidence_rank(incoming_source)
	current_rank = geo.confidence_rank(current_source)
	winner_source, winner_rank = geo.clamp_confidence(current_source, incoming_source)

	accepted = winner_source == incoming_source and geo.accepts_write(current_source, incoming_source)

	changed = _coordinates_changed(
		current.get("custom_latitude"),
		current.get("custom_longitude"),
		latitude,
		longitude,
	)
	corrected = False
	if (
		not accepted
		and staff_correction
		and changed
		and geo.accepts_staff_correction(current_source, incoming_source)
	):
		accepted = True
		corrected = True
		winner_source, winner_rank = incoming_source, incoming_rank

	resolved_accuracy = None
	try:
		if accuracy_m is not None and str(accuracy_m).strip() != "":
			resolved_accuracy = max(0.0, float(accuracy_m))
	except (TypeError, ValueError):
		resolved_accuracy = None

	return {
		"accepted": accepted,
		"reason": "staff_correction" if corrected else ("accepted" if accepted else "lower_confidence"),
		"staff_correction": corrected,
		"address": name,
		"source": incoming_source,
		"incoming_rank": incoming_rank,
		"current_source": current_source,
		"current_rank": current_rank,
		"resulting_source": winner_source,
		"resulting_rank": winner_rank,
		"latitude": float(latitude),
		"longitude": float(longitude),
		"accuracy_m": resolved_accuracy,
		"current_latitude": current.get("custom_latitude"),
		"current_longitude": current.get("custom_longitude"),
		"coordinates_changed": changed,
		"moved_m": geo.distance_m_or_none(
			current.get("custom_latitude"),
			current.get("custom_longitude"),
			latitude,
			longitude,
		),
	}


#: Decimal places on ``Address.custom_latitude`` / ``custom_longitude``. Compared
#: at the field's own precision so a float round-trip cannot report a move the
#: database will not actually store.
_COORD_PLACES = 6


def _coordinates_changed(current_lat: Any, current_lng: Any, incoming_lat: Any, incoming_lng: Any) -> bool:
	"""True when the incoming pin differs from the stored one at stored precision.

	Drives the accuracy invariant in :func:`set_address_pin` (contract §3): an
	accuracy radius describes *a specific measurement*. Carrying an old radius
	over onto new coordinates asserts a confidence nobody measured — a 5 m
	figure from a courier's GPS fix silently re-labelling a 500 m viewport
	centre. So accuracy may only be preserved when the coordinates did not move.
	"""
	try:
		if current_lat is None or current_lng is None:
			return True
		return round(float(current_lat), _COORD_PLACES) != round(float(incoming_lat), _COORD_PLACES) or round(
			float(current_lng), _COORD_PLACES
		) != round(float(incoming_lng), _COORD_PLACES)
	except (TypeError, ValueError):
		return True


# ─────────────────────────────────────────────────────────────────────────────
# The write
# ─────────────────────────────────────────────────────────────────────────────


def accuracy_is_known(value: Any) -> bool:
	"""True when ``custom_geo_accuracy_m`` carries a real measurement.

	The column is NOT NULL DEFAULT 0 (Frappe builds every Float that way), so
	"no accuracy reported" and "accurate to 0 m" are indistinguishable in
	storage. We resolve that ambiguity in favour of *unknown*: a parsed maps link
	and a WooCommerce pin both arrive with no accuracy at all, whereas a genuine
	0 m GPS fix does not exist.

	Anything asking "was this delivered near the pin?" must call this first,
	rather than reading 0 as a tight radius and drawing a confident wrong
	conclusion.
	"""
	try:
		return float(value or 0) > 0
	except (TypeError, ValueError):
		return False


def set_address_pin(
	address_name: str,
	*,
	latitude: Any,
	longitude: Any,
	source: str,
	accuracy_m: Any = None,
	note: str | None = None,
	staff_correction: bool = False,
) -> dict[str, Any]:
	"""Write a pin onto an Address, subject to the never-downgrade ladder.

	``staff_correction=True`` is the explicit opt-in described on
	:func:`evaluate_pin_write` (POS staff replacing a ``customer_pin``); it is
	logged on the Address like a manual override.

	Returns ``{"success": True, "accepted": bool, ...}``. A rejected write is a
	success with ``accepted=False`` — see the module docstring for why a routine
	Woo re-sync must not be able to fail on an address that already has a better
	pin.

	``custom_geo_source`` and ``custom_geo_confidence`` are always written in the
	same statement, so they can never drift apart.
	"""
	_assert_geo_fields()

	decision = evaluate_pin_write(
		address_name,
		latitude=latitude,
		longitude=longitude,
		source=source,
		accuracy_m=accuracy_m,
		staff_correction=staff_correction,
	)
	if not decision.get("accepted"):
		return {"success": True, **decision}

	resolved_source = decision["source"]
	if resolved_source == geo.SOURCE_MANUAL_OVERRIDE and not _can_manual_override():
		frappe.throw(
			_("Only a manager may set a manual override pin."),
			frappe.PermissionError,
		)

	updates: dict[str, Any] = {
		"custom_latitude": decision["latitude"],
		"custom_longitude": decision["longitude"],
		"custom_geo_source": resolved_source,
		"custom_geo_confidence": decision["incoming_rank"],
	}

	# Accuracy invariant (contract §3): a write that MOVES the pin must state the
	# new accuracy or explicitly NULL it. Accuracy describes one measurement, so
	# leaving a stale radius attached to new coordinates asserts a precision
	# nobody took — a 5 m courier fix silently vouching for a 500 m viewport
	# centre, which is exactly backwards for anything that later asks "was this
	# delivered near the pin?". Preserved only when the coordinates are unchanged.
	#
	# Cleared to 0, NOT None. Frappe creates Float columns as NOT NULL DEFAULT 0,
	# so writing None raises
	#     (1048) Column 'custom_geo_accuracy_m' cannot be null
	# and takes the whole pin write down with it. Every POS-link pin would have
	# failed, since a parsed link never carries an accuracy — the only writes
	# that survived were ones passing an explicit measurement.
	#
	# 0 therefore means "no accuracy reported", not "accurate to 0 m". Consumers
	# must treat it as unknown; see accuracy_is_known() below.
	if decision.get("accuracy_m") is not None:
		updates["custom_geo_accuracy_m"] = decision["accuracy_m"]
	elif decision.get("coordinates_changed"):
		updates["custom_geo_accuracy_m"] = 0.0

	if resolved_source == geo.SOURCE_COURIER_VERIFIED:
		updates["custom_geo_verified_on"] = now_datetime()

	# Date every accepted write (see GEO_UPDATED_FIELD). Meta-guarded: the column
	# arrives with `bench migrate`, and a pin write must not fail before then.
	if _has_address_field(GEO_UPDATED_FIELD):
		updates[GEO_UPDATED_FIELD] = now_datetime()

	_assert_no_woo_trigger_fields(updates)

	frappe.db.set_value("Address", decision["address"], updates, update_modified=False)

	# manual_override outranks courier consensus by design, so every use of it is
	# recorded with who did it. Best-effort: an audit-trail failure must not undo
	# a legitimate pin correction.
	if resolved_source == geo.SOURCE_MANUAL_OVERRIDE:
		try:
			frappe.get_doc(
				{
					"doctype": "Comment",
					"comment_type": "Info",
					"reference_doctype": "Address",
					"reference_name": decision["address"],
					"content": (
						f"Manual geo override by {frappe.session.user}: "
						f"{decision['latitude']}, {decision['longitude']}" + (f" — {note}" if note else "")
					),
				}
			).insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "geo_resolution: manual override audit failed")

	# A staff correction replaces a customer's pin, so who did it and what it
	# replaced is recorded, same best-effort shape as the manual override above.
	if decision.get("staff_correction"):
		try:
			frappe.get_doc(
				{
					"doctype": "Comment",
					"comment_type": "Info",
					"reference_doctype": "Address",
					"reference_name": decision["address"],
					"content": (
						f"POS staff geo correction by {frappe.session.user}: replaced "
						f"{decision.get('current_source') or 'unknown'} pin "
						f"{decision.get('current_latitude')}, {decision.get('current_longitude')} with "
						f"{decision['latitude']}, {decision['longitude']}" + (f" — {note}" if note else "")
					),
				}
			).insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "geo_resolution: staff correction audit failed")

	return {"success": True, **decision, "written_fields": sorted(updates)}


# ─────────────────────────────────────────────────────────────────────────────
# Delivery reference points — swapped typed pairs and distance from the branch
# ─────────────────────────────────────────────────────────────────────────────

#: Where each reference basis comes from, reported next to the numbers so the
#: client (and a reader of the Error Log) knows how much weight to give them.
REFERENCE_POS_PROFILE = "pos_profile"
REFERENCE_TERRITORY = "territory"
REFERENCE_CAIRO_FALLBACK = "cairo_fallback"

#: Latitude/longitude field pairs probed on POS Profile and Territory, in
#: order. Neither stock DocType has coordinates and this app adds none, so on a
#: site where nobody has added such a field the answer is the Cairo fallback.
_REFERENCE_FIELD_PAIRS = (
	("custom_latitude", "custom_longitude"),
	("latitude", "longitude"),
)

#: Reference points change when someone edits a Territory, not per request, and
#: this is read on every preview paste: cache per process for a while.
_REFERENCE_CACHE_TTL_SEC = 10 * 60
_reference_cache: dict[str, tuple[float, list[tuple[float, float]]]] = {}


def clear_reference_cache() -> None:
	"""Forget cached reference points (tests, and after bulk-editing Territories)."""
	_reference_cache.clear()


def _load_reference_points(doctype: str) -> list[tuple[float, float]]:
	"""Valid in-Egypt ``(lat, lng)`` pairs held on *doctype* rows. Never raises."""
	try:
		meta = frappe.get_meta(doctype)
		pair = next((p for p in _REFERENCE_FIELD_PAIRS if meta.has_field(p[0]) and meta.has_field(p[1])), None)
		if not pair:
			return []
		filters = {"disabled": 0} if meta.has_field("disabled") else {}
		rows = frappe.get_all(doctype, filters=filters, fields=list(pair), limit=0) or []
		points: list[tuple[float, float]] = []
		for row in rows:
			lat, lng = row.get(pair[0]), row.get(pair[1])
			if geo.in_service_area(lat, lng):
				points.append((float(lat), float(lng)))
		return points
	except Exception:
		return []


def _cached_reference_points(doctype: str) -> list[tuple[float, float]]:
	now = time.monotonic()
	hit = _reference_cache.get(doctype)
	if hit and now - hit[0] < _REFERENCE_CACHE_TTL_SEC:
		return hit[1]
	points = _load_reference_points(doctype)
	_reference_cache[doctype] = (now, points)
	return points


def territory_reference_points() -> tuple[list[tuple[float, float]], str]:
	"""Delivery Territory centroids, else Greater Cairo. ``(points, basis)``.

	The reference used to decide whether a typed pair is transposed. With no
	centroids on the site the answer is Greater Cairo alone (the area Jarz
	delivers in), under the same thresholds.
	"""
	points = _cached_reference_points("Territory")
	if points:
		return points, REFERENCE_TERRITORY
	return [geo.GREATER_CAIRO], REFERENCE_CAIRO_FALLBACK


def branch_reference_points() -> tuple[list[tuple[float, float]], str]:
	"""POS Profile branch coordinates, else Territory centroids, else Greater Cairo."""
	points = _cached_reference_points("POS Profile")
	if points:
		return points, REFERENCE_POS_PROFILE
	return territory_reference_points()


def distance_from_branch_m(latitude: Any, longitude: Any) -> tuple[int | None, str]:
	"""``(metres, basis)``: how far a point is from the nearest delivery reference.

	Semantics, which the client relies on (``LocationLinkField`` rejects a
	preview above 150 km): the great-circle distance in metres to the NEAREST of
	(1) the POS Profile branch coordinates, if any POS Profile carries them;
	otherwise (2) the delivery Territory centroids, if any carry them; otherwise
	(3) Greater Cairo. It is a plausibility check on a pasted pin ("is this even
	our part of the country"), not a measured delivery distance, so ``basis``
	says which of the three answered. Never raises; ``None`` when the point
	cannot be measured.
	"""
	try:
		points, basis = branch_reference_points()
		nearest = geo.nearest_reference(latitude, longitude, points)
		if nearest is None:
			return None, basis
		return int(round(nearest[0])), basis
	except Exception:
		return None, REFERENCE_CAIRO_FALLBACK


def apply_bare_pair_swap(link: Any, latitude: float, longitude: float) -> dict[str, Any]:
	"""Correct a typed ``a, b`` pair whose latitude and longitude are transposed.

	Only for an input that is nothing but an unlabelled decimal pair
	(:func:`geo.is_unlabelled_bare_pair`): a URL or a hemisphere-labelled pair
	names its own axes. See :func:`geo.maybe_swap_bare_pair` for the rule
	(swapped within 40 km of a delivery reference, typed farther than 80 km from
	every one; anything else keeps the typed order). Returns
	``{"latitude", "longitude", "swapped", ...}``; when swapped it also carries
	``typed_latitude``/``typed_longitude``, ``swap_reference`` and a
	``precision_note`` for the UI. Never raises.
	"""
	result: dict[str, Any] = {"latitude": latitude, "longitude": longitude, "swapped": False}
	try:
		if not geo.is_unlabelled_bare_pair(link):
			return result
		points, basis = territory_reference_points()
		new_lat, new_lng, swapped = geo.maybe_swap_bare_pair(latitude, longitude, points)
		if not swapped:
			return result
		result.update(
			{
				"latitude": new_lat,
				"longitude": new_lng,
				"swapped": True,
				"typed_latitude": latitude,
				"typed_longitude": longitude,
				"swap_reference": basis,
				"precision_note": _(
					"The typed pair looked like longitude, latitude (it lands far from the delivery "
					"area), so it was read as {0}, {1}. Check the pin on the map."
				).format(new_lat, new_lng),
			}
		)
	except Exception:
		pass
	return result


#: Reason returned when a pasted link names a real point outside Egypt
#: (:data:`geo.SERVICE_AREA`). Only link-derived pins are gated: a courier's GPS
#: fix or a manager's override is trusted as given.
OUTSIDE_SERVICE_AREA = "outside_service_area"


def resolve_address_from_link(
	address_name: str,
	url: str,
	*,
	source: str = geo.SOURCE_POS_LINK,
	accuracy_m: Any = None,
	staff_correction: bool = False,
) -> dict[str, Any]:
	"""Parse a Maps link and, if it yields coordinates, pin the Address.

	``staff_correction`` is forwarded to :func:`set_address_pin`; the short-link
	job it may queue never receives it (a background redirect fetch is not an
	explicit staff edit).

	A short link (``maps.app.goo.gl/...``) carries no coordinates until it is
	expanded over HTTP, so it is queued for a background worker and reported as
	``pending`` rather than resolved inline — an inline redirect fetch in a
	request path is a worker-blocking network call.
	"""
	name = str(address_name or "").strip()
	link = str(url or "").strip()
	if not name:
		return {"success": False, "error": _("address is required")}
	if not link:
		return {"success": False, "error": _("A maps link is required")}

	parsed = geo.parse_maps_link(link)
	if not parsed:
		if geo.is_short_maps_link(link):
			enqueued = enqueue_short_link_resolution(name, link, source=source)
			return {
				"success": True,
				"accepted": False,
				"reason": "short_link_pending" if enqueued else "short_link_unqueued",
				"address": name,
				"url": link,
			}
		return {
			"success": True,
			"accepted": False,
			"reason": "no_coordinates_in_link",
			"address": name,
			"url": link,
		}

	latitude, longitude, precision = parsed
	# Same correction the preview applied, so what staff confirmed is what is saved.
	swap = apply_bare_pair_swap(link, latitude, longitude)
	latitude, longitude = swap["latitude"], swap["longitude"]
	if not geo.in_service_area(latitude, longitude):
		return {
			"success": True,
			"accepted": False,
			"reason": OUTSIDE_SERVICE_AREA,
			"address": name,
			"url": link,
		}
	result = set_address_pin(
		name,
		latitude=latitude,
		longitude=longitude,
		source=source,
		accuracy_m=(accuracy_m if accuracy_m not in (None, "") else geo.accuracy_for_precision(precision)),
		staff_correction=staff_correction,
	)
	result["precision"] = precision
	result["url"] = link
	if swap["swapped"]:
		result.update({k: v for k, v in swap.items() if k not in ("latitude", "longitude")})
	return result


# ─────────────────────────────────────────────────────────────────────────────
# Short-link expansion — background only
# ─────────────────────────────────────────────────────────────────────────────


def enqueue_short_link_resolution(address_name: str, url: str, *, source: str = geo.SOURCE_POS_LINK) -> bool:
	"""Queue the HTTP redirect resolve. Returns whether it was queued.

	Never resolved inline. ``expand_short_link`` blocks on the network, and a
	blocking call inside a whitelisted endpoint lets any caller pin a gunicorn
	worker for the length of the timeout.
	"""
	try:
		frappe.enqueue(
			"jarz_pos.services.geo_resolution.resolve_short_link_job",
			queue="short",
			timeout=60,
			address_name=str(address_name or "").strip(),
			url=str(url or "").strip(),
			source=source,
		)
		return True
	except Exception:
		frappe.log_error(frappe.get_traceback(), "geo_resolution: could not enqueue short link resolve")
		return False


def resolve_short_link_job(address_name: str, url: str, source: str = geo.SOURCE_POS_LINK) -> dict[str, Any]:
	"""Background worker: expand a short link, then pin the Address.

	Runs in a queue, so the HTTP round-trip is fine here. Never raises — a
	shortener being down must not fill the Error Log with tracebacks from a
	retryable job.
	"""
	name = str(address_name or "").strip()
	link = str(url or "").strip()
	if not name or not link:
		return {"success": False, "error": "address and url are required"}

	expanded = geo.expand_short_link(link)
	if not expanded:
		return {"success": True, "accepted": False, "reason": "short_link_unresolved"}

	parsed = geo.parse_maps_link(expanded)
	if not parsed:
		return {
			"success": True,
			"accepted": False,
			"reason": "no_coordinates_in_expanded_link",
			"expanded_url": expanded,
		}

	latitude, longitude, precision = parsed
	if not geo.in_service_area(latitude, longitude):
		return {
			"success": True,
			"accepted": False,
			"reason": OUTSIDE_SERVICE_AREA,
			"expanded_url": expanded,
		}
	try:
		result = set_address_pin(
			name,
			latitude=latitude,
			longitude=longitude,
			source=source,
			accuracy_m=geo.accuracy_for_precision(precision),
		)
	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), f"geo_resolution: short link pin failed for {name}")
		return {"success": False, "error": str(exc)}

	result["expanded_url"] = expanded
	result["precision"] = precision
	frappe.db.commit()
	return result


# ─────────────────────────────────────────────────────────────────────────────
# Read-only link preview — ticket + poll
# ─────────────────────────────────────────────────────────────────────────────
#
# The POS "paste a Maps link" field needs an answer for the link staff actually
# have, and the share sheet hands them a shortener every time. A long URL parses
# with no I/O and is answered inline; a short one carries no coordinates at all,
# so the only route to an answer is the redirect chain — which must not be
# followed inside the request (see ``geo.expand_short_link``).
#
# So the endpoint hands back a ticket and the client polls it, exactly as
# ``services.lead_maps`` does for the leads editor. Before this existed the
# endpoint reported every short link as unresolved, which is why the field said
# "could not read a location from this link" for the one input it gets most.

#: How long a preview ticket survives. Generous: the client gives up long
#: before this, and a stale ticket costs one small Redis key.
PREVIEW_TICKET_TTL_SEC = 5 * 60

#: Per-user budget on *background* expansions. Polling is free — only creating a
#: new ticket spends from it.
PREVIEW_RATE_WINDOW_SEC = 60
PREVIEW_RATE_MAX_REQUESTS = 12

#: Ticket ids are generated here; the pattern is what a *poll* is checked
#: against, so a hand-made id cannot be used to probe the cache namespace.
_PREVIEW_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9]{32}$")


def _preview_ticket_key(request_id: str) -> str:
	return f"jarz_pos:geo_preview:ticket:{request_id}"


def _preview_cache_set(key: str, value: dict[str, Any]) -> bool:
	try:
		frappe.cache().set_value(key, json.dumps(value), expires_in_sec=PREVIEW_TICKET_TTL_SEC)
		return True
	except Exception:
		return False


def _preview_cache_get(key: str) -> dict[str, Any]:
	try:
		# ``use_local_cache=False`` deliberately: a poll and the worker's write
		# can land in the same Frappe request during a harness sweep, and the
		# request-local copy would hide the completed result.
		raw = frappe.cache().get_value(key, use_local_cache=False)
		if isinstance(raw, bytes):
			raw = raw.decode("utf-8")
		value = json.loads(raw) if isinstance(raw, str) else raw
		return value if isinstance(value, dict) else {}
	except Exception:
		return {}


def _consume_preview_budget(user: str) -> bool:
	"""Bound background expansions per user. Fails **closed** without Redis.

	Failing closed costs only the short-link path: a long URL never reaches
	here, and a client that cannot get a ticket is told the link is unresolved
	rather than left waiting on a job nobody will run.
	"""
	try:
		cache = frappe.cache()
		key = cache.make_key(f"jarz_pos:geo_preview:rate:{user}")
		count = int(cache.incrby(key, 1))
		if count == 1:
			cache.expire(key, PREVIEW_RATE_WINDOW_SEC)
		return count <= PREVIEW_RATE_MAX_REQUESTS
	except Exception:
		return False


def _preview_resolved(link: str, parsed: Any, *, short_link: bool = False) -> dict[str, Any]:
	latitude, longitude, precision = parsed
	# A typed pair can have its axes transposed; a URL never is (see
	# apply_bare_pair_swap). Short-link expansions are URLs, so the helper is a
	# no-op for them.
	swap = apply_bare_pair_swap(link, latitude, longitude)
	latitude, longitude = swap["latitude"], swap["longitude"]
	if not geo.in_service_area(latitude, longitude):
		# The point is read correctly but undeliverable. Say so rather than
		# show staff a pin in Paris they might confirm by reflex. The point is
		# kept under its own keys, never `latitude`/`longitude`: an installed
		# app that predates `resolved` treats any success-plus-coordinates
		# answer as a confirmed pin and would save it.
		return {
			**_preview_unresolved(link, OUTSIDE_SERVICE_AREA, short_link=short_link),
			"outside_latitude": latitude,
			"outside_longitude": longitude,
		}
	# The inline and the short-link-job paths both end here, so both carry it.
	distance_m, distance_basis = distance_from_branch_m(latitude, longitude)
	resolved = {
		"success": True,
		"resolved": True,
		"pending": False,
		"short_link": False,
		"url": link,
		"latitude": latitude,
		"longitude": longitude,
		"precision": precision,
		"accuracy_m": geo.accuracy_for_precision(precision),
		# Metres to the nearest delivery reference (semantics on
		# distance_from_branch_m); the client rejects a preview above 150 km.
		"distance_from_branch_m": distance_m,
		"distance_reference": distance_basis,
		"swapped": bool(swap["swapped"]),
	}
	if swap["swapped"]:
		resolved.update({k: v for k, v in swap.items() if k not in ("latitude", "longitude", "swapped")})
	return resolved


def _preview_unresolved(link: str, reason: str, *, short_link: bool) -> dict[str, Any]:
	return {
		"success": True,
		"resolved": False,
		"pending": False,
		"short_link": short_link,
		"reason": reason,
		"url": link,
	}


def _preview_failed(reason: str) -> dict[str, Any]:
	return {
		"success": False,
		"resolved": False,
		"pending": False,
		"reason": reason,
		"url": "",
	}


def _preview_poll(request_id: object, user: str) -> dict[str, Any]:
	token = str(request_id or "").strip()
	if not _PREVIEW_REQUEST_ID_RE.fullmatch(token):
		return _preview_failed("invalid_request_id")

	ticket = _preview_cache_get(_preview_ticket_key(token))
	# The owner check is what stops one session polling another's ticket, and
	# what makes a guessed id useless.
	if not ticket or ticket.get("user") != user:
		return _preview_failed("request_expired")

	result = ticket.get("result")
	if isinstance(result, dict):
		return {**result, "request_id": token, "pending": False}

	return {
		**_preview_unresolved(str(ticket.get("url") or ""), "short_link_pending", short_link=True),
		"pending": True,
		"request_id": token,
		"retry_after_ms": 500,
		"expires_in": PREVIEW_TICKET_TTL_SEC,
	}


def preview_link(link: object = None, *, request_id: object = None, user: str = "") -> dict[str, Any]:
	"""Resolve a pasted Maps link to a point. Reads and writes no document.

	Three outcomes, and a caller must handle all three:

	* a long URL parses inline and comes back ``resolved``;
	* a short link comes back ``pending`` with a ``request_id`` to poll, its
	  expansion having been queued;
	* anything else comes back ``resolved=False`` with a ``reason``.
	"""
	owner = str(user or getattr(frappe.session, "user", "") or "").strip()
	if request_id not in (None, ""):
		return _preview_poll(request_id, owner)

	text = str(link or "").strip()
	if not text:
		return {**_preview_failed("missing_link"), "error": _("A maps link is required")}

	parsed = geo.parse_maps_link(text)
	if parsed:
		return _preview_resolved(text, parsed)

	if not geo.is_short_maps_link(text):
		return _preview_unresolved(text, "no_coordinates_in_link", short_link=False)

	if not _consume_preview_budget(owner):
		return _preview_unresolved(text, "rate_limited", short_link=True)

	token = frappe.generate_hash(length=32)
	if not _preview_cache_set(_preview_ticket_key(token), {"user": owner, "url": text, "result": None}):
		return _preview_unresolved(text, "short_link_unqueued", short_link=True)

	try:
		frappe.enqueue(
			"jarz_pos.services.geo_resolution.preview_short_link_job",
			queue="short",
			timeout=30,
			request_id=token,
			url=text,
			user=owner,
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "geo_resolution: could not enqueue link preview")
		return _preview_unresolved(text, "short_link_unqueued", short_link=True)

	return {
		**_preview_unresolved(text, "short_link_pending", short_link=True),
		"pending": True,
		"request_id": token,
		"retry_after_ms": 500,
		"expires_in": PREVIEW_TICKET_TTL_SEC,
	}


def preview_short_link_job(request_id: str, url: str, user: str = "") -> dict[str, Any]:
	"""Background worker: expand one short link and park the answer on its ticket.

	Never raises. A shortener being slow or down is an ordinary outcome of a
	paste, not something worth a traceback in the Error Log — the client sees
	the ticket resolve to ``short_link_unresolved`` and says so inline.
	"""
	token = str(request_id or "").strip()
	link = str(url or "").strip()
	if not _PREVIEW_REQUEST_ID_RE.fullmatch(token) or not link:
		return {"success": False, "reason": "invalid_request"}

	key = _preview_ticket_key(token)
	ticket = _preview_cache_get(key)
	# A ticket that expired mid-flight is not an error, but there is nowhere
	# left to put the answer, so do not spend the network call.
	if not ticket:
		return {"success": False, "reason": "request_expired"}

	expanded = geo.expand_short_link(link)
	if not expanded:
		result = _preview_unresolved(link, "short_link_unresolved", short_link=True)
	else:
		parsed = geo.parse_maps_link(expanded)
		if parsed:
			result = _preview_resolved(link, parsed, short_link=True)
		else:
			result = _preview_unresolved(link, "no_coordinates_in_expanded_link", short_link=True)
		result["expanded_url"] = expanded

	_preview_cache_set(key, {**ticket, "result": result})
	return result

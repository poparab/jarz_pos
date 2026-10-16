"""Read-only map-pin projection for order cards.

Website orders carry the customer's map pin on the order's Address
(``custom_latitude`` / ``custom_longitude`` / ``custom_geo_source`` /
``custom_geo_confidence``). The order board's "Pinned / No map pin" badge and
its Google Maps link read a flat set of keys from each invoice dict; this module
builds those keys in one batched query for the whole board.

The Address pin is SHARED by every order of a customer, so a later order that
re-pins it used to move every earlier order's map location. Each invoice may
therefore carry its own snapshot (``custom_delivery_pin_*``, written by the Woo
inbound sync); :func:`invoice_pin_override` lays that snapshot over the Address
keys, and the board selects those columns in its existing single query.

Three rules, all deliberate:

* **Read only.** Nothing here writes to Address. ``address_line2`` in
  particular is part of the WooCommerce address-dedup signature and of the Woo
  outbound-push trigger set, so it is parsed, never rewritten.
* **Meta-guarded.** The geo columns are optional: a site that has not migrated
  them must still get a working board, so each one is selected only when
  ``frappe.get_meta("Address").has_field`` says it exists.
* **Never fails the caller.** Any error degrades to :data:`EMPTY_PIN_FIELDS`
  and is logged once per call — never once per row, and the log call itself is
  wrapped because ``frappe.log_error`` can raise.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any, Optional

import frappe

from jarz_pos.utils.geo import (
	CONFIDENCE_RANK,
	SOURCE_COURIER_VERIFIED,
	SOURCE_CUSTOMER_PIN,
	SOURCE_MANUAL_OVERRIDE,
	distance_m_or_none,
	extract_location_link,
	is_valid_coordinate,
	maps_link_for,
)

#: Optional Address columns carrying the pin. Selected only when present.
GEO_COLUMNS = (
	"custom_latitude",
	"custom_longitude",
	"custom_geo_source",
	"custom_geo_confidence",
)

#: Optional Sales Invoice columns carrying THIS order's pin snapshot. The
#: shared Address pin is re-pinned by every later order of the same customer,
#: so an invoice that carries its own snapshot must show that, not the Address.
#: (Not ``custom_delivery_latitude``: that is the courier's proof-of-delivery GPS.)
INVOICE_PIN_COLUMNS = (
	"custom_delivery_pin_latitude",
	"custom_delivery_pin_longitude",
	"custom_delivery_pin_source",
	"custom_delivery_pin_flag",
)

#: Confidence a per-order snapshot is shown at: it is a customer's remote pin.
SNAPSHOT_CONFIDENCE = CONFIDENCE_RANK[SOURCE_CUSTOMER_PIN]

#: A higher-confidence Address pin (courier_verified / manual_override) within
#: this distance of the snapshot is a verified refinement of the same door and
#: wins over the snapshot. Farther away, it belongs to a different delivery.
VERIFIED_NEAR_M = 150.0

#: Staff-confirmed Address sources. They win over the snapshot at ANY distance:
#: a far-away verified pin is precisely "the customer's pin was wrong and staff
#: fixed it" (the area_mismatch case), not a different delivery.
STAFF_CONFIRMED_SOURCES = frozenset({SOURCE_COURIER_VERIFIED, SOURCE_MANUAL_OVERRIDE})

#: What a card carries when there is no address, no pin, or the lookup failed.
#: ``pin_source`` / ``pin_flag`` come from the invoice snapshot only; the flag
#: can be set even when no usable pin exists (``outside_egypt``).
EMPTY_PIN_FIELDS: dict[str, Any] = {
	"address_latitude": None,
	"address_longitude": None,
	"geo_source": "",
	"geo_confidence": 0,
	"has_location_pin": False,
	"location_link": "",
	"pin_source": "",
	"pin_flag": "",
}


def empty_pin_fields() -> dict[str, Any]:
	"""A fresh copy of :data:`EMPTY_PIN_FIELDS` (callers mutate card dicts)."""
	return dict(EMPTY_PIN_FIELDS)


def _finite_float(value: Any) -> float | None:
	if value is None or value == "" or isinstance(value, bool):
		return None
	try:
		number = float(value)
	except (TypeError, ValueError):
		return None
	if math.isnan(number) or math.isinf(number):
		return None
	return number


def _safe_int(value: Any) -> int:
	try:
		return int(float(value or 0))
	except (TypeError, ValueError, OverflowError):
		return 0


def pin_fields_from_address(row: dict[str, Any] | None) -> dict[str, Any]:
	"""Project one Address row onto the card's pin keys. Pure; never raises.

	The coordinates are emitted only when they form a usable pin. A ``0, 0``
	or out-of-range pair comes back as ``null`` so a client that tests "are the
	coordinates present?" can never show "Pinned" for a placeholder.

	``location_link`` prefers the stored pin; otherwise it falls back to the
	legacy pasted link in ``address_line2`` (``"Location: <url>"``).
	"""
	out = empty_pin_fields()
	if not row:
		return out
	try:
		lat = _finite_float(row.get("custom_latitude"))
		lng = _finite_float(row.get("custom_longitude"))
		has_pin = lat is not None and lng is not None and is_valid_coordinate(lat, lng)
		if has_pin:
			out["address_latitude"] = lat
			out["address_longitude"] = lng
			out["has_location_pin"] = True
			out["location_link"] = maps_link_for(lat, lng)
		else:
			out["location_link"] = extract_location_link(row.get("address_line2"))
		out["geo_source"] = str(row.get("custom_geo_source") or "").strip()
		out["geo_confidence"] = _safe_int(row.get("custom_geo_confidence"))
	except Exception:
		return empty_pin_fields()
	return out


def _safe_log_error(title: str) -> None:
	try:
		frappe.log_error(frappe.get_traceback(), title)
	except Exception:
		pass


def get_address_pin_map(address_names: Iterable[Any]) -> dict[str, dict[str, Any]]:
	"""Address name -> pin keys, for every name in *address_names*, in ONE query.

	Names that are missing from the result (blank, deleted, or the query failed)
	should be read through :func:`pin_fields_for`, which supplies the empty set.
	"""
	names = sorted({str(n or "").strip() for n in (address_names or [])} - {""})
	if not names:
		return {}

	try:
		fields = ["name", "address_line2"]
		try:
			meta = frappe.get_meta("Address")
			for column in GEO_COLUMNS:
				if meta.has_field(column):
					fields.append(column)
		except Exception:
			# Without meta we cannot tell which geo columns exist; selecting a
			# missing one is an SQL error, so fall back to the legacy link only.
			pass

		rows = (
			frappe.get_all(
				"Address",
				filters={"name": ["in", names]},
				fields=fields,
				limit=0,
			)
			or []
		)

		out: dict[str, dict[str, Any]] = {}
		for row in rows:
			name = str(row.get("name") or "").strip()
			if name:
				out[name] = pin_fields_from_address(row)
		return out
	except Exception:
		_safe_log_error("Jarz POS: address pin lookup failed")
		return {}


def pin_fields_for(address_name: Any, pin_map: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
	"""The pin keys for *address_name* from *pin_map*, or the empty set."""
	try:
		found = (pin_map or {}).get(str(address_name or "").strip())
	except Exception:
		found = None
	return dict(found) if found else empty_pin_fields()


def invoice_pin_columns() -> list[str]:
	"""The :data:`INVOICE_PIN_COLUMNS` that exist on this site's Sales Invoice.

	A site that has not migrated them yet gets ``[]`` (selecting a missing
	column is an SQL error that would cost the whole board). Never raises.
	"""
	try:
		meta = frappe.get_meta("Sales Invoice")
		return [column for column in INVOICE_PIN_COLUMNS if meta.has_field(column)]
	except Exception:
		return []


def _clean_text(value: Any) -> str:
	return str(value or "").strip()


def _clean_flag(value: Any) -> str:
	"""Normalise a comma-separated flag string: trimmed tokens, no blanks."""
	tokens = [t.strip() for t in str(value or "").split(",")]
	return ",".join(t for t in tokens if t)


def invoice_pin_override(si_row: Any, address_fields: dict[str, Any] | None) -> dict[str, Any]:
	"""Merge THIS invoice's pin snapshot over the Address pin keys. Pure; never raises.

	* The invoice carries a valid snapshot pin -> it wins, shown as
	  ``customer_pin`` at :data:`SNAPSHOT_CONFIDENCE` — UNLESS the Address
	  carries a higher-confidence pin (courier_verified / manual_override)
	  within :data:`VERIFIED_NEAR_M` of it, which is a verified refinement of
	  the same door and is kept.
	  A staff-confirmed Address pin (:data:`STAFF_CONFIRMED_SOURCES`) wins at
	  any distance.
	* No usable snapshot (blank, ``0,0``, out of range, columns not migrated)
	  -> the Address pin exactly as before, EXCEPT when the snapshot carries a
	  flag (e.g. ``outside_egypt``): the website rejected this order's pin, so
	  the shared Address pin belongs to some other order and is blanked unless
	  staff confirmed it.

	``pin_source`` and ``pin_flag`` always come from the snapshot (``""`` when
	absent); the flag passes through even when there is no usable pin.
	"""
	base = dict(address_fields) if address_fields else empty_pin_fields()
	base.setdefault("pin_source", "")
	base.setdefault("pin_flag", "")
	if not si_row:
		return base
	try:
		getter = si_row.get
		out = dict(base)
		out["pin_source"] = _clean_text(getter("custom_delivery_pin_source"))
		out["pin_flag"] = _clean_flag(getter("custom_delivery_pin_flag"))

		staff_confirmed = bool(out.get("has_location_pin")) and (
			_clean_text(out.get("geo_source")).lower() in STAFF_CONFIRMED_SOURCES
		)

		lat = _finite_float(getter("custom_delivery_pin_latitude"))
		lng = _finite_float(getter("custom_delivery_pin_longitude"))
		if lat is None or lng is None or not is_valid_coordinate(lat, lng):
			if out["pin_flag"] and not staff_confirmed:
				blank = empty_pin_fields()
				blank["pin_source"] = out["pin_source"]
				blank["pin_flag"] = out["pin_flag"]
				return blank
			return out

		if staff_confirmed:
			return out

		if out.get("has_location_pin") and _safe_int(out.get("geo_confidence")) > SNAPSHOT_CONFIDENCE:
			gap = distance_m_or_none(out.get("address_latitude"), out.get("address_longitude"), lat, lng)
			if gap is not None and gap <= VERIFIED_NEAR_M:
				return out

		out["address_latitude"] = lat
		out["address_longitude"] = lng
		out["geo_source"] = SOURCE_CUSTOMER_PIN
		out["geo_confidence"] = SNAPSHOT_CONFIDENCE
		out["has_location_pin"] = True
		out["location_link"] = maps_link_for(lat, lng)
		return out
	except Exception:
		return base

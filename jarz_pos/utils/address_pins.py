"""Read-only map-pin projection for order cards.

Website orders carry the customer's map pin on the order's Address
(``custom_latitude`` / ``custom_longitude`` / ``custom_geo_source`` /
``custom_geo_confidence``). The order board's "Pinned / No map pin" badge and
its Google Maps link read a flat set of keys from each invoice dict; this module
builds those keys in one batched query for the whole board.

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
from typing import Any, Dict, Iterable, Optional

import frappe

from jarz_pos.utils.geo import extract_location_link, is_valid_coordinate, maps_link_for

#: Optional Address columns carrying the pin. Selected only when present.
GEO_COLUMNS = (
    "custom_latitude",
    "custom_longitude",
    "custom_geo_source",
    "custom_geo_confidence",
)

#: What a card carries when there is no address, no pin, or the lookup failed.
EMPTY_PIN_FIELDS: Dict[str, Any] = {
    "address_latitude": None,
    "address_longitude": None,
    "geo_source": "",
    "geo_confidence": 0,
    "has_location_pin": False,
    "location_link": "",
}


def empty_pin_fields() -> Dict[str, Any]:
    """A fresh copy of :data:`EMPTY_PIN_FIELDS` (callers mutate card dicts)."""
    return dict(EMPTY_PIN_FIELDS)


def _finite_float(value: Any) -> Optional[float]:
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


def pin_fields_from_address(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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


def get_address_pin_map(address_names: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
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

        rows = frappe.get_all(
            "Address",
            filters={"name": ["in", names]},
            fields=fields,
            limit=0,
        ) or []

        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name:
                out[name] = pin_fields_from_address(row)
        return out
    except Exception:
        _safe_log_error("Jarz POS: address pin lookup failed")
        return {}


def pin_fields_for(address_name: Any, pin_map: Optional[Dict[str, Dict[str, Any]]]) -> Dict[str, Any]:
    """The pin keys for *address_name* from *pin_map*, or the empty set."""
    try:
        found = (pin_map or {}).get(str(address_name or "").strip())
    except Exception:
        found = None
    return dict(found) if found else empty_pin_fields()

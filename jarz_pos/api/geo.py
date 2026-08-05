"""Whitelisted endpoints for the Address door-pin database.

Thin transport only: every rule — the confidence ladder, the manual-override
gate, the never-write-``address_line2`` guard — lives in
``jarz_pos.services.geo_resolution`` so the staging harness can exercise it
directly without going through HTTP.

Follows the ``api/returns.py`` shape exactly, including the feature trio:

* **preview** — parse a link and report what it yields. Read-only, no address
  needed, so the POS "paste a Maps link" field can validate as the user types.
* **execute** — write the pin, subject to the ladder.
* **dry-run** — resolve the link *and* run the ladder decision against a real
  address, writing nothing. This is what lets a staging harness sweep a
  production clone and prove the parser copes with the real link corpus before
  a single row is touched.

``frappe.has_permission("Address", ...)`` rather than a role list: an Address is
already permission-controlled and the geo fields are just more columns on it.
Escalating to a role check for the one privileged case (``manual_override``)
happens in the service, where the rule belongs.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_pos.services import geo_resolution as _geo_service
from jarz_pos.utils import geo as _geo


def _ensure_geo_read_permission() -> None:
    """Read gate. First statement of every read endpoint."""
    frappe.has_permission("Address", ptype="read", throw=True)


def _ensure_geo_write_permission() -> None:
    """Write gate. First statement of every write endpoint."""
    frappe.has_permission("Address", ptype="write", throw=True)


@frappe.whitelist(allow_guest=False)
def preview_maps_link(
    link: Optional[str] = None, url: Optional[str] = None
) -> Dict[str, Any]:
    """Parse *link* and report the coordinates it yields. Reads no document.

    The parameter is ``link`` — frozen in COURIER_CONTRACTS §10a and the name the
    Flutter client sends. ``url`` is kept as an accepted alias so an older or
    server-side caller does not break, but ``link`` is the contract.

    This signature is load-bearing in a way that is easy to miss: Frappe binds
    form keys to parameter *names* and silently discards anything the signature
    does not declare. A mismatch here is not a soft failure — the argument
    vanishes and the endpoint raises ``TypeError`` on a missing positional, so
    the feature simply never works.

    Deliberately does no network I/O: a short link comes back as
    ``short_link=True`` with no coordinates, and the caller is expected to save
    it (which queues a background expand) rather than block on a redirect chain
    inside a request.
    """
    _ensure_geo_read_permission()
    try:
        link = str(link or url or "").strip()
        if not link:
            return {"success": False, "error": _("A maps link is required")}

        parsed = _geo.parse_maps_link(link)
        if not parsed:
            return {
                "success": True,
                "resolved": False,
                "short_link": _geo.is_short_maps_link(link),
                "reason": (
                    "short_link_needs_expansion"
                    if _geo.is_short_maps_link(link)
                    else "no_coordinates_in_link"
                ),
                "url": link,
            }

        latitude, longitude, precision = parsed
        return {
            "success": True,
            "resolved": True,
            "url": link,
            "latitude": latitude,
            "longitude": longitude,
            "precision": precision,
            "accuracy_m": _geo.accuracy_for_precision(precision),
            "short_link": False,
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "preview_maps_link failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_address_pin(address: str) -> Dict[str, Any]:
    """Everything the pin editor needs about one Address. Read-only."""
    _ensure_geo_read_permission()
    try:
        name = str(address or "").strip()
        if not name:
            return {"success": False, "error": _("address is required")}
        frappe.has_permission("Address", ptype="read", doc=name, throw=True)

        current = _geo_service.get_address_geo(name)
        if not current:
            return {"success": False, "error": _("Address {0} not found").format(name)}

        line2 = frappe.db.get_value("Address", name, "address_line2") or ""
        return {
            "success": True,
            "address": name,
            "latitude": current.get("custom_latitude"),
            "longitude": current.get("custom_longitude"),
            "source": current.get("custom_geo_source") or "",
            "confidence": current.get("custom_geo_confidence") or 0,
            "accuracy_m": current.get("custom_geo_accuracy_m"),
            "verified_on": current.get("custom_geo_verified_on"),
            # Read-only echo. The legacy corpus of pasted links lives here and
            # the client may want to show it; nothing in this app rewrites it.
            "stored_link": _geo.extract_location_link(line2),
            "ladder": _geo.CONFIDENCE_RANK,
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "get_address_pin failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def set_address_pin(
    address: str,
    link: Optional[str] = None,
    url: Optional[str] = None,
    latitude: Optional[Any] = None,
    longitude: Optional[Any] = None,
    source: str = _geo.SOURCE_POS_LINK,
    accuracy_m: Optional[Any] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute: write a pin onto *address*.

    Accepts either a Maps *link* to parse, or an explicit *latitude*/*longitude*
    pair. ``url`` is an accepted alias for ``link`` (§10a freezes ``link``).

    A write whose confidence sits below what the address already carries is
    reported as ``accepted=False`` and changes nothing — that is not an error,
    and the client should say "kept the better existing pin".
    """
    _ensure_geo_write_permission()
    try:
        name = str(address or "").strip()
        if not name:
            return {"success": False, "error": _("address is required")}
        frappe.has_permission("Address", ptype="write", doc=name, throw=True)

        link = str(link or url or "").strip()
        if link:
            return _geo_service.resolve_address_from_link(
                name, link, source=source, accuracy_m=accuracy_m
            )

        if latitude in (None, "") or longitude in (None, ""):
            return {
                "success": False,
                "error": _("Supply either a maps link or a latitude/longitude pair"),
            }

        return _geo_service.set_address_pin(
            name,
            latitude=latitude,
            longitude=longitude,
            source=source,
            accuracy_m=accuracy_m,
            note=note,
        )
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "set_address_pin failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def dry_run_address_pin(
    address: str,
    link: Optional[str] = None,
    url: Optional[str] = None,
    latitude: Optional[Any] = None,
    longitude: Optional[Any] = None,
    source: str = _geo.SOURCE_POS_LINK,
    accuracy_m: Optional[Any] = None,
) -> Dict[str, Any]:
    """Resolve the link and run the full ladder decision. Writes nothing.

    Exposed so a staging harness can sweep every production-cloned Address, prove
    the parser handles the real link corpus and show exactly which pins the
    ladder would accept — before anything is written anywhere.
    """
    _ensure_geo_read_permission()
    try:
        name = str(address or "").strip()
        if not name:
            return {"success": False, "error": _("address is required")}
        frappe.has_permission("Address", ptype="read", doc=name, throw=True)

        precision = None
        link = str(link or url or "").strip()
        if link:
            parsed = _geo.parse_maps_link(link)
            if not parsed:
                return {
                    "success": True,
                    "accepted": False,
                    "reason": (
                        "short_link_needs_expansion"
                        if _geo.is_short_maps_link(link)
                        else "no_coordinates_in_link"
                    ),
                    "address": name,
                    "url": link,
                }
            latitude, longitude, precision = parsed
            if accuracy_m in (None, ""):
                accuracy_m = _geo.accuracy_for_precision(precision)

        if latitude in (None, "") or longitude in (None, ""):
            return {
                "success": False,
                "error": _("Supply either a maps link or a latitude/longitude pair"),
            }

        decision = _geo_service.evaluate_pin_write(
            name,
            latitude=latitude,
            longitude=longitude,
            source=source,
            accuracy_m=accuracy_m,
        )
        decision["precision"] = precision
        decision["dry_run"] = True
        return {"success": True, **decision}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "dry_run_address_pin failed")
        return {"success": False, "error": str(exc)}

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

from typing import Any, Dict, Optional

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
MANUAL_OVERRIDE_ROLES = {
    ROLES.ADMINISTRATOR.lower(),
    ROLES.SYSTEM_MANAGER.lower(),
    ROLES.JARZ_MANAGER.lower(),
    ROLES.JARZ_LINE_MANAGER.lower(),
}


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


def _assert_geo_fields() -> None:
    """Fail loudly when the schema is behind the code.

    Without this the ``db.set_value`` below would raise a raw column error, or —
    worse on a partially migrated site — write some fields and skip others,
    leaving ``custom_geo_source`` and ``custom_geo_confidence`` disagreeing.
    """
    try:
        meta = frappe.get_meta("Address")
    except Exception as exc:  # pragma: no cover - meta failure is environmental
        frappe.throw(_("Could not read Address metadata: {0}").format(exc))

    missing = [field for field in GEO_FIELDS if not meta.get_field(field)]
    if missing:
        frappe.throw(
            _(
                "Address is missing the geo fields {0}. Run `bench migrate` — this "
                "deployment is ahead of the site's schema."
            ).format(", ".join(missing))
        )


def _assert_no_woo_trigger_fields(updates: Dict[str, Any]) -> None:
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


def get_address_geo(address_name: str) -> Dict[str, Any]:
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


def _can_manual_override(user: Optional[str] = None) -> bool:
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
) -> Dict[str, Any]:
    """Decide what a pin write *would* do. Writes nothing.

    Shared by :func:`set_address_pin` and the API dry-run so the two can never
    disagree about the outcome — the failure mode a separate "preview"
    implementation always eventually produces.
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

    accepted = winner_source == incoming_source and geo.accepts_write(
        current_source, incoming_source
    )

    resolved_accuracy = None
    try:
        if accuracy_m is not None and str(accuracy_m).strip() != "":
            resolved_accuracy = max(0.0, float(accuracy_m))
    except (TypeError, ValueError):
        resolved_accuracy = None

    return {
        "accepted": accepted,
        "reason": "accepted" if accepted else "lower_confidence",
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
        "coordinates_changed": _coordinates_changed(
            current.get("custom_latitude"),
            current.get("custom_longitude"),
            latitude,
            longitude,
        ),
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


def _coordinates_changed(
    current_lat: Any, current_lng: Any, incoming_lat: Any, incoming_lng: Any
) -> bool:
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
        return (
            round(float(current_lat), _COORD_PLACES)
            != round(float(incoming_lat), _COORD_PLACES)
            or round(float(current_lng), _COORD_PLACES)
            != round(float(incoming_lng), _COORD_PLACES)
        )
    except (TypeError, ValueError):
        return True


# ─────────────────────────────────────────────────────────────────────────────
# The write
# ─────────────────────────────────────────────────────────────────────────────

def set_address_pin(
    address_name: str,
    *,
    latitude: Any,
    longitude: Any,
    source: str,
    accuracy_m: Any = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a pin onto an Address, subject to the never-downgrade ladder.

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
    )
    if not decision.get("accepted"):
        return {"success": True, **decision}

    resolved_source = decision["source"]
    if resolved_source == geo.SOURCE_MANUAL_OVERRIDE and not _can_manual_override():
        frappe.throw(
            _("Only a manager may set a manual override pin."),
            frappe.PermissionError,
        )

    updates: Dict[str, Any] = {
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
    if decision.get("accuracy_m") is not None:
        updates["custom_geo_accuracy_m"] = decision["accuracy_m"]
    elif decision.get("coordinates_changed"):
        updates["custom_geo_accuracy_m"] = None

    if resolved_source == geo.SOURCE_COURIER_VERIFIED:
        updates["custom_geo_verified_on"] = now_datetime()

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
                        f"{decision['latitude']}, {decision['longitude']}"
                        + (f" — {note}" if note else "")
                    ),
                }
            ).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "geo_resolution: manual override audit failed"
            )

    return {"success": True, **decision, "written_fields": sorted(updates)}


def resolve_address_from_link(
    address_name: str,
    url: str,
    *,
    source: str = geo.SOURCE_POS_LINK,
    accuracy_m: Any = None,
) -> Dict[str, Any]:
    """Parse a Maps link and, if it yields coordinates, pin the Address.

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
    result = set_address_pin(
        name,
        latitude=latitude,
        longitude=longitude,
        source=source,
        accuracy_m=(
            accuracy_m
            if accuracy_m not in (None, "")
            else geo.accuracy_for_precision(precision)
        ),
    )
    result["precision"] = precision
    result["url"] = link
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Short-link expansion — background only
# ─────────────────────────────────────────────────────────────────────────────

def enqueue_short_link_resolution(
    address_name: str, url: str, *, source: str = geo.SOURCE_POS_LINK
) -> bool:
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
        frappe.log_error(
            frappe.get_traceback(), "geo_resolution: could not enqueue short link resolve"
        )
        return False


def resolve_short_link_job(
    address_name: str, url: str, source: str = geo.SOURCE_POS_LINK
) -> Dict[str, Any]:
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
    try:
        result = set_address_pin(
            name,
            latitude=latitude,
            longitude=longitude,
            source=source,
            accuracy_m=geo.accuracy_for_precision(precision),
        )
    except Exception as exc:
        frappe.log_error(
            frappe.get_traceback(), f"geo_resolution: short link pin failed for {name}"
        )
        return {"success": False, "error": str(exc)}

    result["expanded_url"] = expanded
    result["precision"] = precision
    frappe.db.commit()
    return result

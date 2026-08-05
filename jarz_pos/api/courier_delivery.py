"""Whitelisted endpoints for the per-invoice courier transitions.

Thin transport only. Every rule — the feature flag, the schema assertion, the
access gate, the idempotency pair, the "a failure never moves the board state"
invariant — lives in ``jarz_pos.services.courier_delivery`` so the staging
harness and ``jarz_courier`` can call it directly without going through HTTP.

Follows ``api/returns.py`` exactly, including re-raising ``PermissionError``
*before* the generic handler: a permission failure must surface as a real 403,
not be flattened into ``{"success": False}``. That distinction is load-bearing
for a courier app with an offline queue — a 403 must not be retried forever,
while a 500 must be.

HTTP parameters arrive as strings, so every numeric and boolean argument is
coerced here rather than in the service, which is typed.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional

import frappe
from frappe import _

from jarz_pos.services import courier_delivery as _courier
from jarz_pos.services import courier_identity as _identity

#: Window inside which an omitted ``request_id`` is derived deterministically.
#:
#: A failed attempt has no durable idempotency token — repeated failures are
#: legitimate — so ``request_id`` is the only guard, and a client that omits it
#: would double-count its own offline retry. Deriving one from the call's own
#: arguments plus a coarse time bucket closes that: a retry storm (seconds to
#: minutes) collapses to one attempt, while a genuine second attempt hours or
#: days later gets its own token. Clients that send a real ``request_id`` are
#: unaffected and should always do so.
_DERIVED_REQUEST_WINDOW_SEC = 15 * 60


def _ensure_courier_delivery_permission() -> None:
    """Permission gate. First statement of every endpoint in this module."""
    frappe.has_permission("Sales Invoice", ptype="write", throw=True)


def _ensure_courier_read_permission() -> None:
    """Read gate for the informational endpoints."""
    frappe.has_permission("Sales Invoice", ptype="read", throw=True)


def _coerce_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _derive_request_id(action: str, invoice_id: str, *parts: Any) -> str:
    """Deterministic fallback token — see :data:`_DERIVED_REQUEST_WINDOW_SEC`."""
    bucket = int(time.time()) // _DERIVED_REQUEST_WINDOW_SEC
    payload = "::".join(
        [action, str(invoice_id), str(bucket), *(str(p) for p in parts)]
    )
    return "auto-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@frappe.whitelist(allow_guest=False)
def get_delivery_failure_reasons() -> Dict[str, Any]:
    """Active failure reasons for the courier's picker. Read-only."""
    _ensure_courier_read_permission()
    try:
        return {"success": True, "reasons": _courier.list_failure_reasons()}
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "get_delivery_failure_reasons failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_stop_outcome(invoice_id: str) -> Dict[str, Any]:
    """Preview: the delivery outcome already recorded on one invoice.

    Read-only, and deliberately cheap — the courier app polls it to reconcile an
    offline queue against what the server actually accepted.
    """
    _ensure_courier_read_permission()
    try:
        name = str(invoice_id or "").strip()
        if not name:
            return {"success": False, "error": _("invoice_id is required")}

        fields = [
            "name",
            "custom_sales_invoice_state",
            "custom_courier_party_type",
            "custom_courier_party",
            "custom_delivery_sequence",
            "custom_arrived_at",
            "custom_delivered_at",
            "custom_delivery_latitude",
            "custom_delivery_longitude",
            "custom_delivery_accuracy_m",
            "custom_delivery_attempt_no",
            "custom_delivery_failure_reason",
        ]
        meta = frappe.get_meta("Sales Invoice")
        available = [f for f in fields if f == "name" or meta.get_field(f)]
        row = frappe.db.get_value("Sales Invoice", name, available, as_dict=True)
        if not row:
            return {"success": False, "error": _("Sales Invoice {0} not found").format(name)}

        frappe.has_permission("Sales Invoice", ptype="read", doc=name, throw=True)

        return {
            "success": True,
            "invoice_id": name,
            "feature_enabled": _courier.courier_delivery_enabled(),
            "missing_fields": sorted(set(fields) - set(available)),
            "outcome": dict(row),
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "get_stop_outcome failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def get_my_courier_identity() -> Dict[str, Any]:
    """Which courier party the caller resolves to, if any. Read-only.

    ``is_courier`` means "is in the delivery Employee Group", not "has an
    Employee record" — in any real site most staff have the latter, and
    conflating the two is what would block a dispatcher from stamping a
    delivery.
    """
    _ensure_courier_read_permission()
    try:
        employee = _identity.resolve_courier_party()
        return {
            "success": True,
            "is_courier": bool(employee and employee.get("is_delivery_courier")),
            "has_employee_record": bool(employee),
            "courier": employee,
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "get_my_courier_identity failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def mark_arrived(
    invoice_id: str,
    latitude: Optional[Any] = None,
    longitude: Optional[Any] = None,
    accuracy_m: Optional[Any] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute: stamp arrival at the customer's door."""
    _ensure_courier_delivery_permission()
    try:
        return _courier.mark_invoice_arrived(
            invoice_id,
            latitude=_coerce_float(latitude),
            longitude=_coerce_float(longitude),
            accuracy_m=_coerce_float(accuracy_m),
            request_id=(
                str(request_id or "").strip()
                or _derive_request_id(_courier.ACTION_ARRIVED, invoice_id)
            ),
        )
    except frappe.PermissionError:
        raise
    except _courier.CourierSchemaError:
        # Surfaces as a real error so the client shows "app is ahead of the
        # server" rather than silently reporting a delivery that never landed.
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "mark_arrived failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def mark_delivered(
    invoice_id: str,
    latitude: Optional[Any] = None,
    longitude: Optional[Any] = None,
    accuracy_m: Optional[Any] = None,
    collected_amount: Optional[Any] = None,
    recipient_name: Optional[str] = None,
    is_mocked: Any = False,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute: record a successful hand-over and move the card to Delivered.

    Posts no accounting — the response carries ``posted_accounting: False`` to
    say so explicitly. Cash settles through the existing settlement path.
    """
    _ensure_courier_delivery_permission()
    try:
        return _courier.mark_invoice_delivered(
            invoice_id,
            latitude=_coerce_float(latitude),
            longitude=_coerce_float(longitude),
            accuracy_m=_coerce_float(accuracy_m),
            collected_amount=_coerce_float(collected_amount),
            recipient_name=recipient_name,
            is_mocked=_coerce_bool(is_mocked),
            request_id=(
                str(request_id or "").strip()
                or _derive_request_id(_courier.ACTION_DELIVERED, invoice_id)
            ),
        )
    except frappe.PermissionError:
        raise
    except _courier.CourierSchemaError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "mark_delivered failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def mark_failed(
    invoice_id: str,
    failure_reason: str,
    latitude: Optional[Any] = None,
    longitude: Optional[Any] = None,
    accuracy_m: Optional[Any] = None,
    notes: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute: record a failed attempt.

    The board state is untouched by design — the order stays Out for Delivery
    and carries a reason plus an attempt count.
    """
    _ensure_courier_delivery_permission()
    try:
        return _courier.mark_invoice_failed(
            invoice_id,
            failure_reason=failure_reason,
            latitude=_coerce_float(latitude),
            longitude=_coerce_float(longitude),
            accuracy_m=_coerce_float(accuracy_m),
            notes=notes,
            request_id=(
                str(request_id or "").strip()
                or _derive_request_id(
                    _courier.ACTION_FAILED, invoice_id, failure_reason, notes
                )
            ),
        )
    except frappe.PermissionError:
        raise
    except _courier.CourierSchemaError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "mark_failed failed")
        return {"success": False, "error": str(exc)}

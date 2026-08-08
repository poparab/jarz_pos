"""The one guest-callable endpoint in Jarz POS: customer delivery tracking.

Thin transport, following ``api/returns.py``: every rule — the master switch, the
allow-list, the token→invoice resolution, the expiry clock, the courier-phone
gate — lives in ``jarz_pos.services.tracking`` so the staging harness can
exercise all of it without an HTTP request and without a guest session.

**Read this before adding anything to this module.** It is reachable by anybody
on the internet with no session. Two properties must hold for every endpoint
added here:

1. it is authorised solely by an unguessable per-order token, never by an
   identifier a customer can construct; and
2. it is rate limited.

The endpoint below carries two limiters, on purpose:

* ``frappe.rate_limiter.rate_limit`` keyed on (request IP, ``token``) — the
  transport-edge limiter, applied here because that is where Frappe puts it and
  because it produces a real HTTP rejection rather than a JSON body; and
* ``services.tracking.consume_rate_budget`` keyed on the token alone — the layer
  that still bounds a *distributed* scrape of one leaked link, and the layer that
  survives a Frappe build without ``rate_limiter``.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import frappe

from jarz_pos.services import tracking as _tracking

try:  # pragma: no cover - import shape depends on the Frappe build
    from frappe.rate_limiter import rate_limit as _frappe_rate_limit
except Exception:  # pragma: no cover
    _frappe_rate_limit = None  # type: ignore[assignment]

#: Per (IP, token) budget at the transport edge. The page polls every 15 s, so a
#: legitimate reader spends 4/min; 30 leaves room for a hard refresh, a second
#: tab and a flaky connection retrying.
RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_WINDOW_SEC = 60


def _rate_limited(fn: Callable) -> Callable:
    """Apply Frappe's rate limiter when this build has one.

    Keyed on ``token``, which is why the parameter below is named exactly that:
    the decorator reads ``frappe.form_dict[key]``, so renaming the parameter
    silently degrades this to an IP-only limit.
    """
    if _frappe_rate_limit is None:  # pragma: no cover - depends on Frappe build
        return fn
    return _frappe_rate_limit(
        key="token",
        limit=RATE_LIMIT_MAX_REQUESTS,
        seconds=RATE_LIMIT_WINDOW_SEC,
        ip_based=True,
    )(fn)


def _ensure_public_tracking_permission() -> None:
    """The gate for the guest endpoint. First statement, like every other module.

    There is deliberately no ``frappe.has_permission`` call, and that absence is a
    decision rather than an omission — which is exactly why this function exists
    with this name and this comment. ``frappe.session.user`` here is ``Guest``;
    the credential is the token, and the token is checked in the service.

    What it *does* enforce is the one thing a permissionless per-token endpoint
    genuinely needs: the response must never be cached. Without this a reverse
    proxy or a CDN can store one customer's payload against a URL and hand it to
    the next caller — a cross-customer leak manufactured entirely by
    infrastructure.

    Note it sets a real ``Cache-Control`` **header** and ``frappe.local.no_cache``,
    and deliberately writes nothing into ``frappe.local.response``: for a JSON
    response that dict *is* the response body (``utils.response.as_json``
    serialises the whole thing), so a stray key there ends up in the payload the
    allow-list test is guarding.
    """
    try:
        frappe.local.no_cache = 1
    except Exception:
        pass
    try:
        frappe.local.response_headers["Cache-Control"] = "no-store, private, max-age=0"
    except Exception:
        pass


def _ensure_tracking_link_permission() -> None:
    """Gate for the staff-only link lookup. First statement of that endpoint.

    A real permission check, unlike the guest gate above: this endpoint takes an
    invoice *name*, so without it any authenticated user could mint a shareable
    link for an order in another branch.
    """
    frappe.has_permission("Sales Invoice", ptype="read", throw=True)


@frappe.whitelist(allow_guest=True)
@_rate_limited
def get_public_status(token: Optional[str] = None) -> Dict[str, Any]:
    """Customer-visible state of one order, addressed by its tracking token.

    Returns either the full allow-listed payload
    (``services.tracking.PUBLIC_STATUS_KEYS``) or one of exactly two failure
    envelopes:

    * ``{"success": false, "error": "not found"}`` — unknown token, expired
      token, tracking switched off, or a site whose schema predates the token
      field. **All four collapse to one message on purpose.** A distinguishable
      "expired" confirms the token was once real, which is the one piece of
      information an enumeration attempt actually wants.
    * ``{"success": false, "error": "too many requests"}`` — the per-token budget
      is spent. Safe to distinguish: it is decided before any lookup, so it
      reveals nothing about the token.

    The parameter name ``token`` is load-bearing twice over: Frappe binds form
    keys to parameter names and silently discards anything the signature does not
    declare, and :func:`_rate_limited` keys its bucket off the same name.
    """
    _ensure_public_tracking_permission()
    try:
        result = _tracking.resolve_public_status(token)
        if (
            not result.get("success")
            and result.get("error") == _tracking.RATE_LIMITED_ERROR
        ):
            # A real 429 so a polling client backs off instead of treating a
            # throttle as "your order vanished".
            try:
                frappe.local.response["http_status_code"] = 429
            except Exception:
                pass
        return result
    except frappe.PermissionError:
        raise
    except Exception:
        # The traceback goes to the Error Log; the caller gets the same opaque
        # answer as a wrong token. Echoing str(exc) to a guest would leak
        # doctype names, field names and occasionally SQL.
        frappe.log_error(frappe.get_traceback(), "get_public_status failed")
        return _tracking.not_found()


@frappe.whitelist(allow_guest=False)
def get_tracking_link(invoice_id: str) -> Dict[str, Any]:
    """Absolute tracking URL for an order. Staff-only.

    The internal counterpart to the public read: it exists so the POS/Desk side
    can copy a link into a WhatsApp message without knowing how the URL is built.
    Never guest-callable — it takes an invoice name, so allowing guests would
    hand out a link for any order number.

    Returns ``{"success": True, "url": None}`` when the order has not been
    dispatched, which the client should render as "the link appears once the
    order goes out" rather than as an error.

    **Mints on demand for an already-dispatched order.** The normal mint point is
    the OFD transition, but that only fires for orders dispatched *after* this
    code shipped — so every order already out with a courier would otherwise
    never have a link, and neither would one whose hook did not fire. Since this
    endpoint exists precisely to produce a sendable link, returning None for a
    van that is already on the road makes it useless exactly when it is needed.

    Gated on ``custom_was_out_for_delivery``, the permanent one-way stamp set by
    ``events.sales_invoice.stamp_out_for_delivery_flag``. Without that gate this
    would mint a public token for an order nobody has dispatched yet, which is a
    live endpoint for an order still being picked.
    """
    _ensure_tracking_link_permission()
    try:
        name = str(invoice_id or "").strip()
        if not name:
            return {"success": False, "error": "invoice_id is required"}
        # Document-level too: the doctype check above says "may read invoices",
        # this one says "may read THIS invoice", which is what branch scoping and
        # user permissions hang off.
        frappe.has_permission("Sales Invoice", ptype="read", doc=name, throw=True)

        dispatched = False
        try:
            dispatched = bool(
                frappe.db.get_value("Sales Invoice", name, "custom_was_out_for_delivery")
            )
        except Exception:
            dispatched = False

        return {
            "success": True,
            "invoice_id": name,
            "url": _tracking.get_tracking_url(name, ensure=dispatched),
            "enabled": _tracking.tracking_enabled(),
            "dispatched": dispatched,
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "get_tracking_link failed")
        return {"success": False, "error": str(exc)}

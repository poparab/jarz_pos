"""Customer-facing delivery tracking: the opaque token and the public read.

One link, sent to the customer, that shows a customer-friendly status, the
destination pin and — while the order is actually out — where the courier is.
Everything about this module is shaped by one fact: **the only credential is a
string in a URL**, and URLs get forwarded, screenshotted and pasted into group
chats. So the design is defensive in four specific ways.

**1. The token is opaque, never derived from the invoice.**
``secrets.token_urlsafe(16)`` — 128 bits from the OS CSPRNG. If the token were
``ACC-SINV-2026-00042`` or any hash of it, one customer with one link could walk
the whole order book. Nothing in this module accepts an invoice name from the
public side.

**2. Wrong token and expired token are the same answer.**
Both return ``{"success": False, "error": "not found"}``. Distinguishing them
turns the endpoint into an oracle: "expired" confirms the token was once real,
which is exactly the signal a brute-force needs to know it is in the right
keyspace.

**3. The response is an allow-list, not a filtered document.**
:data:`PUBLIC_STATUS_KEYS` is built key by key and a test asserts the response
matches it exactly. A deny-list ("strip the cost fields") fails the day somebody
adds a field; an allow-list fails the day somebody adds a field *and forgets the
test*, which is a much smaller window. Note what is absent by design: the
invoice name, the customer name, any item name, any address text, any cost,
margin or courier-fee figure, the courier's full name, and the raw internal
board state.

**4. The internal state string never leaves the building.**
The board options are frozen at ``Recieved\\nIn Progress\\nReady\\nOut for
Delivery\\nDelivered\\nCancelled\\nReturned`` (COURIER_CONTRACTS §1) — including
the live misspelling, which this project does not touch. Publishing those to
customers would leak internal vocabulary *and* ship a typo to every buyer, so
they are mapped to a small customer-facing vocabulary here.

**Why the courier position comes from Redis and not the DB.** A ping every few
seconds per courier is write amplification the ORM should never see; lane B7
writes the last-known fix to a cache key and nothing else. This module reads
that key and degrades to null coordinates when it is missing — which is the
normal case today, since B7 is not built yet. It must never error on absence, or
the tracking page would break for every order until background tracking ships.

**Feature flag read HERE, in the service**, matching
``invoice_return.returns_enabled`` and
``courier_delivery.courier_delivery_enabled``. This is the app's only
guest-callable surface; a switch checked in the transport layer is a switch a
background caller or a harness bypasses.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe.utils import cint, flt, get_datetime, get_url, now_datetime

# ─────────────────────────────────────────────────────────────────────────────
# Schema / routing constants
# ─────────────────────────────────────────────────────────────────────────────

#: The Sales Invoice column holding the token.
#:
#: **Small Text, NOT Data, and this cannot be changed.** ``tabSales Invoice``
#: carries 247 columns and sits at MariaDB's hard 65,535-byte row limit
#: (COURIER_CONTRACTS §2); a ``Data`` field is ``varchar(140)`` ≈ 560 inline
#: bytes at utf8mb4 and the ALTER is rejected outright with
#: "(1118) Row size too large". ``Small Text`` maps to TEXT, which stores an
#: ~20-byte pointer inline.
#:
#: The cost of that is real and is why :func:`_resolve_token_to_invoice` exists:
#: a TEXT column cannot carry a plain index, so the DB lookup by token is a
#: table scan. A Redis index absorbs it.
TOKEN_FIELD = "custom_tracking_token"

#: Bytes of entropy handed to ``secrets.token_urlsafe`` → a 22-character
#: URL-safe string. 128 bits is not a compromise for a shortener-friendly link:
#: it is unguessable, and the rate limiter below means even a lucky attacker
#: cannot sweep.
TOKEN_BYTES = 16

#: Public route prefix. The page lives at ``www/track.html`` and the dynamic
#: segment is wired in ``hooks.website_route_rules``; both must agree with this.
TRACKING_ROUTE_PREFIX = "/track"

#: How often the page re-polls the JSON. Guests cannot join Frappe socketio
#: rooms (room membership is granted per authenticated user), so websockets are
#: not an option here and polling is not laziness — it is the only mechanism.
DEFAULT_POLL_INTERVAL_SEC = 15

#: Fallback when ``tracking_link_ttl_hours`` is unset.
DEFAULT_TTL_HOURS = 24

#: Per-token cap, independent of IP: a family link legitimately gets opened from
#: several phones. Sits underneath the per-(IP, token) limiter on the endpoint;
#: this one is the layer that survives a Frappe without ``rate_limiter`` and the
#: layer a distributed scrape hits.
RATE_LIMIT_MAX_REQUESTS = 120
RATE_LIMIT_WINDOW_SEC = 60

_RATE_KEY_PREFIX = "jarz_pos:track:rl:"
_TOKEN_INDEX_PREFIX = "jarz_pos:track:tok:"

#: How long the token→invoice index entry lives. Comfortably longer than any
#: link's useful life, so the scan below is a cold-start cost and not a
#: per-poll cost.
_TOKEN_INDEX_TTL_SEC = 30 * 24 * 3600

#: Last-known courier position, written by lane B7 (``jarz_courier``) and only
#: ever read here. Frozen shape, so B7 has something to conform to:
#:
#:   key   ``courier:loc:{branch}:{party}``   (through ``frappe.cache()``, so it
#:                                             is site-namespaced automatically)
#:   value ``{"lat": 30.0444, "lng": 31.2357, "ts": "<datetime>",
#:            "accuracy_m": 12.0}``
#:
#: ``latitude``/``longitude`` are accepted as aliases and a bare ``"lat,lng"``
#: string is accepted too, because guessing wrong about a not-yet-written
#: producer should degrade to "no live position", never to an exception.
COURIER_LOCATION_KEY_TEMPLATE = "courier:loc:{branch}:{party}"


# ─────────────────────────────────────────────────────────────────────────────
# Board state → customer vocabulary
# ─────────────────────────────────────────────────────────────────────────────

STATUS_RECEIVED = "received"
STATUS_PREPARING = "preparing"
STATUS_READY = "ready"
STATUS_ON_THE_WAY = "on_the_way"
STATUS_DELIVERED = "delivered"
STATUS_CANCELLED = "cancelled"
STATUS_RETURNED = "returned"
#: Anything unrecognised. Deliberately vague rather than echoing the raw value:
#: an unmapped state is usually a new internal column, and internal columns are
#: not customer vocabulary.
STATUS_PROCESSING = "processing"

#: Internal board state (``_state_key``-normalised) → customer status code.
#: ``recieved`` is the misspelling that is live production data in every
#: historical row (COURIER_CONTRACTS §1); the correctly spelled key is mapped too
#: so a future data migration does not silently drop every order into
#: ``processing``.
_STATE_TO_STATUS: Dict[str, str] = {
    "recieved": STATUS_RECEIVED,
    "received": STATUS_RECEIVED,
    "in_progress": STATUS_PREPARING,
    "ready": STATUS_READY,
    "out_for_delivery": STATUS_ON_THE_WAY,
    "delivered": STATUS_DELIVERED,
    "cancelled": STATUS_CANCELLED,
    "canceled": STATUS_CANCELLED,
    "returned": STATUS_RETURNED,
}

#: ``code -> (english, arabic)``.
#:
#: Deliberately **not** run through ``frappe._()``. The page shows both languages
#: and lets the reader pick, so there is no single request language to translate
#: into — and ``frappe.local.lang`` for a guest is the site default, which would
#: hand an Arabic-speaking customer English. Returning the pair and choosing in
#: the browser is the only version that works for a link forwarded to somebody
#: whose phone is set to the other language.
STATUS_LABELS: Dict[str, Tuple[str, str]] = {
    STATUS_RECEIVED: ("We have your order", "استلمنا طلبك"),
    STATUS_PREPARING: ("Preparing your order", "جاري تحضير طلبك"),
    STATUS_READY: ("Ready to leave the branch", "جاهز للخروج من الفرع"),
    STATUS_ON_THE_WAY: ("On the way to you", "في الطريق إليك"),
    STATUS_DELIVERED: ("Delivered", "تم التسليم"),
    STATUS_CANCELLED: ("Cancelled", "تم إلغاء الطلب"),
    STATUS_RETURNED: ("Returned", "تم إرجاع الطلب"),
    STATUS_PROCESSING: ("Processing your order", "جاري معالجة طلبك"),
}

#: Statuses that start the TTL clock. Cancelled and Returned are included
#: alongside Delivered: an order that will never move again has no reason to
#: keep a public endpoint answering about it, and the customer has already been
#: told the outcome.
TERMINAL_STATUSES = frozenset({STATUS_DELIVERED, STATUS_CANCELLED, STATUS_RETURNED})

#: Board-state aliases in the order ``api.kanban`` probes them.
_STATE_FIELD_ALIASES = (
    "custom_sales_invoice_state",
    "sales_invoice_state",
    "custom_state",
    "state",
)

#: The exact key set of a successful :func:`resolve_public_status` payload.
#:
#: A test asserts equality against a literal copy of this tuple, so adding a key
#: here is a two-file change and a reviewer sees it. That is the whole point:
#: the risk on a public endpoint is not a deliberate leak, it is a convenient
#: ``**invoice_dict`` somebody adds on a Friday.
PUBLIC_STATUS_KEYS: Tuple[str, ...] = (
    "success",
    "status",
    "status_label_en",
    "status_label_ar",
    "courier_first_name",
    "courier_phone",
    "courier_latitude",
    "courier_longitude",
    "courier_updated_at",
    "destination_latitude",
    "destination_longitude",
    "item_count",
    "total_qty",
    "order_total",
    "currency",
    "poll_interval_sec",
)

#: The single public failure payload. One string for "no such token", "expired",
#: "feature off" and "malformed" — see the module docstring on oracles.
NOT_FOUND_ERROR = "not found"
RATE_LIMITED_ERROR = "too many requests"


def not_found() -> Dict[str, Any]:
    """The generic public failure. Never says which of the reasons applied."""
    return {"success": False, "error": NOT_FOUND_ERROR}


def rate_limited() -> Dict[str, Any]:
    """Public failure for a token being polled too hard.

    Distinct from :func:`not_found` on purpose and safely so: the limiter runs
    *before* any lookup, so this answer reveals nothing about whether the token
    exists.
    """
    return {"success": False, "error": RATE_LIMITED_ERROR}


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

def tracking_enabled() -> bool:
    """Master switch (``Jarz POS Settings.enable_customer_tracking``). Off by default."""
    try:
        return bool(int(frappe.db.get_single_value("Jarz POS Settings", "enable_customer_tracking") or 0))
    except Exception:
        return False


def expose_courier_phone() -> bool:
    """Whether the courier's number may appear on the customer page. Off by default."""
    try:
        return bool(
            int(
                frappe.db.get_single_value(
                    "Jarz POS Settings", "expose_courier_phone_to_customer"
                )
                or 0
            )
        )
    except Exception:
        return False


def tracking_link_ttl_hours() -> int:
    """Hours a link keeps working past a terminal state. ``0`` means never expire."""
    try:
        raw = frappe.db.get_single_value("Jarz POS Settings", "tracking_link_ttl_hours")
    except Exception:
        return DEFAULT_TTL_HOURS
    if raw in (None, ""):
        return DEFAULT_TTL_HOURS
    try:
        hours = int(cint(raw))
    except Exception:
        return DEFAULT_TTL_HOURS
    return max(0, hours)


# ─────────────────────────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────────────────────────

def token_field_present() -> bool:
    """True when this site has migrated :data:`TOKEN_FIELD`.

    Checked before every write so a deployment that lands ahead of ``bench
    migrate`` mints nothing rather than raising inside an OFD transition — a
    tracking link is a nice-to-have and must never be able to fail a dispatch.
    """
    try:
        return bool(frappe.get_meta("Sales Invoice").get_field(TOKEN_FIELD))
    except Exception:
        return False


def _state_fields_present() -> List[str]:
    """Whichever board-state aliases this site carries, in probe order."""
    try:
        meta = frappe.get_meta("Sales Invoice")
    except Exception:
        return [_STATE_FIELD_ALIASES[0]]
    present = [name for name in _STATE_FIELD_ALIASES if meta.get_field(name)]
    return present or [_STATE_FIELD_ALIASES[0]]


def _state_key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


# ─────────────────────────────────────────────────────────────────────────────
# Minting
# ─────────────────────────────────────────────────────────────────────────────

def new_token() -> str:
    """A fresh opaque token. ``secrets``, never ``random``."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def ensure_tracking_token(invoice: Any) -> Optional[str]:
    """Mint the token for *invoice* if it has none. Idempotent.

    Called from ``events.sales_invoice.mint_tracking_token_on_ofd`` on
    ``on_update_after_submit``, which is the one place every dispatch path
    converges: the Kanban drag, the Delivery Trip bulk send and
    ``delivery_handling.mark_courier_outstanding`` all persist the state through
    a ``save()`` on the submitted invoice.

    **Regenerating is the bug to avoid, not failing to generate.** A customer who
    already has the link keeps it for the life of the order, so an existing token
    is returned untouched — including when it was written by an earlier release.

    Writes with ``frappe.db.set_value(..., update_modified=False)`` rather than
    ``update_submitted_sales_invoice_fields``, deliberately and for two reasons.
    First, this runs *inside* ``on_update_after_submit``; a nested ``doc.save()``
    would re-enter the whole hook chain (realtime publish, consumable deduction,
    CRM follow-up). Second, leaving ``modified`` alone means the copy of the
    invoice the caller is still holding stays saveable instead of being handed a
    ``TimestampMismatchError`` — the same reasoning as
    ``geo_resolution.set_address_pin`` and
    ``invoice_return._park_return_in_returned_column``.

    Never raises: it is called from a document event, and a tracking link must
    not be able to break a dispatch.
    """
    try:
        name = str(getattr(invoice, "name", "") or "").strip()
        if not name:
            return None
        if not token_field_present():
            return None

        existing = str(invoice.get(TOKEN_FIELD) or "").strip()
        if existing:
            _index_token(existing, name)
            return existing

        # Re-read rather than trusting the in-memory doc: this hook can run on a
        # document loaded before another worker minted the token.
        stored = str(frappe.db.get_value("Sales Invoice", name, TOKEN_FIELD) or "").strip()
        if stored:
            _index_token(stored, name)
            return stored

        token = new_token()
        frappe.db.set_value("Sales Invoice", name, TOKEN_FIELD, token, update_modified=False)
        try:
            invoice.set(TOKEN_FIELD, token)
        except Exception:
            setattr(invoice, TOKEN_FIELD, token)
        _index_token(token, name)
        return token
    except Exception:
        frappe.log_error(frappe.get_traceback(), "tracking: ensure_tracking_token failed")
        return None


def get_tracking_token(invoice_name: str, *, ensure: bool = False) -> Optional[str]:
    """The token for *invoice_name*, or ``None``.

    ``ensure=True`` mints one if the invoice has none. Off by default so a read
    stays a read: the mint point is the OFD transition, and a caller quietly
    creating tokens for orders that were never dispatched would hand out links to
    pages that show nothing.
    """
    name = str(invoice_name or "").strip()
    if not name or not token_field_present():
        return None
    try:
        token = str(frappe.db.get_value("Sales Invoice", name, TOKEN_FIELD) or "").strip()
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"tracking: token read failed for {name}")
        return None
    if token:
        return token
    if not ensure:
        return None
    try:
        return ensure_tracking_token(frappe.get_doc("Sales Invoice", name))
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"tracking: token mint failed for {name}")
        return None


def get_tracking_url(invoice_name: str, *, ensure: bool = False) -> Optional[str]:
    """Absolute customer tracking URL for *invoice_name*, or ``None``.

    **This is the seam the WooCommerce app consumes.** Woo needs to put a "track
    your order" link in a customer email or order note, and it must not have to
    know the route, the token field or the site URL — all three are jarz_pos
    implementation detail and all three can change. It calls this and gets a
    string. Nothing Woo-side is imported here, and nothing here reaches into
    Woo: the two apps are independent (CLAUDE.md domain isolation) and this
    function is deliberately usable by anything that can import ``jarz_pos``.

    Returns ``None`` — not a broken URL — when the order has no token yet, so a
    caller can tell "not dispatched" from "here is the link".
    """
    token = get_tracking_token(invoice_name, ensure=ensure)
    if not token:
        return None
    base = str(get_url() or "").rstrip("/")
    return f"{base}{TRACKING_ROUTE_PREFIX}/{token}"


# ─────────────────────────────────────────────────────────────────────────────
# Token index + rate limit (both cache-backed, both best-effort)
# ─────────────────────────────────────────────────────────────────────────────

def _index_token(token: str, invoice_name: str) -> None:
    """Remember ``token -> invoice`` so the public read is an O(1) cache hit.

    Best-effort. Losing the index costs one table scan per token (see
    :func:`_resolve_token_to_invoice`), never correctness — the DB remains the
    source of truth and every cache hit is re-validated against it.
    """
    try:
        frappe.cache().set_value(
            f"{_TOKEN_INDEX_PREFIX}{token}",
            invoice_name,
            expires_in_sec=_TOKEN_INDEX_TTL_SEC,
        )
    except Exception:
        pass


def _forget_token(token: str) -> None:
    try:
        frappe.cache().delete_value(f"{_TOKEN_INDEX_PREFIX}{token}")
    except Exception:
        pass


def consume_rate_budget(token: str) -> bool:
    """Per-token request budget. ``False`` means "refuse this call".

    A Redis counter with a fixed window: ``incrby`` then ``expire`` on first
    write. The endpoint additionally carries ``frappe.rate_limiter.rate_limit``
    keyed on (IP, token); this one exists because that decorator only engages
    for an HTTP request and only bounds a single IP, while a scrape of a leaked
    link is distributed by nature.

    **Fails open on a cache error.** Redis being down is an outage, and turning
    an outage into "nobody can see where their order is" adds a second one.
    """
    try:
        cache = frappe.cache()
        # ``incrby``/``expire`` come from the raw redis client and do NOT
        # site-namespace the key, unlike get_value/set_value. ``make_key`` is
        # what frappe.rate_limiter does for exactly this reason: without it two
        # sites on one Redis would share one counter.
        key = cache.make_key(f"{_RATE_KEY_PREFIX}{token}")
        count = cache.incrby(key, 1)
        if count is None:
            return True
        count = int(count)
        if count == 1:
            try:
                cache.expire(key, RATE_LIMIT_WINDOW_SEC)
            except Exception:
                pass
        return count <= RATE_LIMIT_MAX_REQUESTS
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────────────────────
# The public read
# ─────────────────────────────────────────────────────────────────────────────

def _invoice_fields() -> List[str]:
    """Columns the public read needs. Everything else stays unread on purpose."""
    fields = [
        "name",
        "docstatus",
        "grand_total",
        "currency",
        "shipping_address_name",
        "customer_address",
        "custom_courier_party_type",
        "custom_courier_party",
        "custom_kanban_profile",
        "pos_profile",
        "custom_delivered_at",
        "modified",
        TOKEN_FIELD,
    ]
    fields.extend(_state_fields_present())
    # De-duplicate while keeping order; a site carrying only the custom alias
    # would otherwise ask for the same column twice.
    seen: List[str] = []
    for field in fields:
        if field not in seen:
            seen.append(field)
    return seen


def _resolve_token_to_invoice(token: str) -> Optional[Dict[str, Any]]:
    """Load the invoice row a token points at, or ``None``.

    Two-step by necessity. :data:`TOKEN_FIELD` is a TEXT column (see its
    docstring) so it cannot carry a plain B-tree index, which makes the
    ``WHERE custom_tracking_token = %s`` form a table scan on a table with
    hundreds of thousands of rows. On a *public, polled* endpoint that is a
    denial-of-service primitive, not a slow query. So:

    1. the Redis index gives the invoice name directly, and
    2. the row is then loaded by primary key and its token compared to the
       presented one.

    Step 2 is not redundant. A stale index entry — an amended invoice, a
    hand-edited field, a token that has since changed — would otherwise let one
    order's link render another order's data, which is precisely the cross-
    customer leak this whole module exists to prevent. A mismatch drops the index
    entry and falls through to the authoritative scan.
    """
    fields = _invoice_fields()

    cached_name = None
    try:
        cached_name = frappe.cache().get_value(f"{_TOKEN_INDEX_PREFIX}{token}")
    except Exception:
        cached_name = None
    if isinstance(cached_name, bytes):
        try:
            cached_name = cached_name.decode("utf-8")
        except Exception:
            cached_name = None

    if cached_name:
        row = frappe.db.get_value("Sales Invoice", str(cached_name), fields, as_dict=True)
        if row and str(row.get(TOKEN_FIELD) or "").strip() == token:
            return dict(row)
        _forget_token(token)

    row = frappe.db.get_value(
        "Sales Invoice", {TOKEN_FIELD: token}, fields, as_dict=True
    )
    if not row:
        return None
    _index_token(token, str(row.get("name") or ""))
    return dict(row)


def _status_for(row: Dict[str, Any]) -> str:
    for field in _STATE_FIELD_ALIASES:
        if field in row and row.get(field):
            mapped = _STATE_TO_STATUS.get(_state_key(row.get(field)))
            if mapped:
                return mapped
    # A cancelled invoice may carry a stale board state, so docstatus wins over
    # an unmapped state string.
    if int(flt(row.get("docstatus"))) == 2:
        return STATUS_CANCELLED
    return STATUS_PROCESSING


def _terminal_reference_time(row: Dict[str, Any], status: str) -> Any:
    """When the TTL clock started for a terminal order.

    ``custom_delivered_at`` when the courier stamped one, otherwise ``modified``
    — which is the last time anything touched the order and therefore the best
    available proxy. ``modified`` can be bumped by an unrelated edit, which only
    ever *extends* a link's life; erring towards a link that works slightly too
    long beats one that dies while the customer is still looking at it.
    """
    if status == STATUS_DELIVERED and row.get("custom_delivered_at"):
        return row.get("custom_delivered_at")
    return row.get("modified")


def _is_expired(row: Dict[str, Any], status: str) -> bool:
    if status not in TERMINAL_STATUSES:
        return False
    ttl_hours = tracking_link_ttl_hours()
    if ttl_hours <= 0:
        return False
    reference = _terminal_reference_time(row, status)
    if not reference:
        return False
    try:
        age_hours = (
            now_datetime() - get_datetime(reference)
        ).total_seconds() / 3600.0
    except Exception:
        # An unparseable timestamp must not silently make links immortal.
        frappe.log_error(
            frappe.get_traceback(), "tracking: could not evaluate link expiry"
        )
        return False
    return age_hours > ttl_hours


def _destination_pin(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """The delivery address's door pin, or ``(None, None)``."""
    from jarz_pos.utils.geo import is_valid_coordinate

    address = (
        str(row.get("shipping_address_name") or "").strip()
        or str(row.get("customer_address") or "").strip()
    )
    if not address:
        return (None, None)
    try:
        pin = frappe.db.get_value(
            "Address", address, ["custom_latitude", "custom_longitude"], as_dict=True
        )
    except Exception:
        # Geo fields not migrated on this site yet. No pin, no error.
        return (None, None)
    if not pin:
        return (None, None)
    lat, lng = pin.get("custom_latitude"), pin.get("custom_longitude")
    if not is_valid_coordinate(lat, lng):
        return (None, None)
    return (float(lat), float(lng))


def _item_summary(invoice_name: str) -> Tuple[int, float]:
    """``(line_count, total_qty)`` — deliberately no item names or codes.

    "Two items, four units" is enough for a customer to recognise their own
    order. A basket list on a forwardable public link is a description of
    somebody's household, and it buys nothing the customer does not already have
    in their confirmation email.
    """
    try:
        rows = frappe.get_all(
            "Sales Invoice Item",
            filters={"parent": invoice_name},
            fields=["qty"],
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"tracking: item summary failed for {invoice_name}"
        )
        return (0, 0.0)
    total_qty = 0.0
    for item in rows or []:
        total_qty += flt(item.get("qty"))
    return (len(rows or []), round(total_qty, 3))


def _courier_identity(row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """``(first_name, phone)`` for the assigned courier.

    **First name only, always.** A full name plus a live position is a
    surveillance package on an employee handed to whoever holds the link; a first
    name is what the customer needs to greet the person at the door. The phone is
    additionally gated on ``expose_courier_phone_to_customer`` (default off),
    because publishing a staff mobile to a forwardable URL is a policy decision.
    """
    party_type = str(row.get("custom_courier_party_type") or "").strip()
    party = str(row.get("custom_courier_party") or "").strip()
    if not party:
        return (None, None)

    full_name = ""
    phone = ""
    try:
        if party_type == "Supplier":
            data = frappe.db.get_value(
                "Supplier", party, ["supplier_name", "mobile_no"], as_dict=True
            )
            if data:
                full_name = str(data.get("supplier_name") or "")
                phone = str(data.get("mobile_no") or "")
        else:
            data = frappe.db.get_value(
                "Employee", party, ["employee_name", "cell_number"], as_dict=True
            )
            if data:
                full_name = str(data.get("employee_name") or "")
                phone = str(data.get("cell_number") or "")
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), f"tracking: courier identity failed for {party}"
        )
        return (None, None)

    first_name = (full_name.strip().split() or [""])[0] or None
    if not expose_courier_phone():
        phone = ""
    return (first_name, phone.strip() or None)


def courier_location_key(branch: str, party: str) -> str:
    """The frozen Redis key lane B7 writes and this module reads."""
    return COURIER_LOCATION_KEY_TEMPLATE.format(
        branch=str(branch or "").strip(), party=str(party or "").strip()
    )


def courier_last_known_position(branch: str, party: str) -> Dict[str, Any]:
    """Last-known courier fix from the cache, or ``{}``.

    Tolerant by design, because the producer (lane B7) does not exist yet: a
    dict, a JSON string, or a bare ``"lat,lng"`` all parse, an absent key returns
    ``{}``, and nothing here raises. Returning ``{}`` is the *expected* path
    today and the page must render fine on it.
    """
    from jarz_pos.utils.geo import is_valid_coordinate

    if not party:
        return {}
    try:
        raw = frappe.cache().get_value(courier_location_key(branch, party))
    except Exception:
        return {}
    if not raw:
        return {}

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            return {}

    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        try:
            payload = json.loads(text)
        except Exception:
            parts = [p.strip() for p in text.split(",")]
            if len(parts) != 2:
                return {}
            payload = {"lat": parts[0], "lng": parts[1]}

    if not isinstance(payload, dict):
        return {}

    lat = payload.get("lat", payload.get("latitude"))
    lng = payload.get("lng", payload.get("longitude"))
    if not is_valid_coordinate(lat, lng):
        return {}
    return {
        "latitude": float(lat),
        "longitude": float(lng),
        "updated_at": str(payload.get("ts") or payload.get("updated_at") or "") or None,
    }


def resolve_public_status(token: Any) -> Dict[str, Any]:
    """Everything the customer page may know about one order.

    The entire public contract of this feature. Returns either the full
    :data:`PUBLIC_STATUS_KEYS` payload or one of the two failure envelopes; there
    is no third shape and no partial payload, so the client has exactly two cases
    to render.
    """
    presented = str(token or "").strip()
    if not presented:
        return not_found()

    # Order matters: the budget is spent before anything is looked up, so a
    # caller hammering random tokens cannot use response timing or the
    # rate-limit boundary to tell a real token from a fake one.
    if not consume_rate_budget(presented):
        return rate_limited()

    if not tracking_enabled():
        return not_found()
    if not token_field_present():
        return not_found()

    try:
        row = _resolve_token_to_invoice(presented)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "tracking: token resolution failed")
        return not_found()

    if not row:
        return not_found()

    status = _status_for(row)
    if _is_expired(row, status):
        return not_found()

    label_en, label_ar = STATUS_LABELS.get(status, STATUS_LABELS[STATUS_PROCESSING])
    destination_lat, destination_lng = _destination_pin(row)
    item_count, total_qty = _item_summary(str(row.get("name") or ""))
    courier_first_name, courier_phone = _courier_identity(row)

    # Live position ONLY while the order is genuinely out. Before dispatch there
    # is nothing to show; after delivery the courier has moved on to other
    # customers, and continuing to stream their position would turn a delivered
    # order's link into a permanent staff tracker.
    position: Dict[str, Any] = {}
    if status == STATUS_ON_THE_WAY:
        position = courier_last_known_position(
            str(row.get("custom_kanban_profile") or row.get("pos_profile") or ""),
            str(row.get("custom_courier_party") or ""),
        )

    payload = {
        "success": True,
        "status": status,
        "status_label_en": label_en,
        "status_label_ar": label_ar,
        "courier_first_name": courier_first_name,
        "courier_phone": courier_phone,
        "courier_latitude": position.get("latitude"),
        "courier_longitude": position.get("longitude"),
        "courier_updated_at": position.get("updated_at"),
        "destination_latitude": destination_lat,
        "destination_longitude": destination_lng,
        "item_count": item_count,
        "total_qty": total_qty,
        "order_total": flt(row.get("grand_total")),
        "currency": str(row.get("currency") or "") or None,
        "poll_interval_sec": DEFAULT_POLL_INTERVAL_SEC,
    }

    # Belt and braces on the allow-list. If a future edit adds a key to the
    # literal above without adding it to PUBLIC_STATUS_KEYS, it is dropped here
    # rather than shipped to a guest — the test catches the mistake, this catches
    # the mistake that reaches production before the test runs.
    return {key: payload[key] for key in PUBLIC_STATUS_KEYS if key in payload}

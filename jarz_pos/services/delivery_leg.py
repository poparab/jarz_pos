"""The trip leg: which single order a courier is driving toward right now.

Why this exists
---------------
A courier leaves the branch carrying several orders from the same delivery slot.
Dispatch is a *bulk* action — ``api.trips.send_trip_for_delivery`` flips every
invoice in a trip to ``Out for Delivery`` atomically — so "the courier is out"
is true of three or four customers at the same instant.

That is fine for a status. It is not fine for a *position*. Before this module,
``services.tracking`` returned the courier's live coordinates to every customer
whose invoice read ``Out for Delivery``, keyed on ``(branch, courier_party)``
with no per-order gate at all. Every customer in the slot watched the same
marker drive to everyone else's house first, in visiting order, with the
courier's first name beside it. The leg closes that hole: the position is
released only between :func:`mark_invoice_leg_started` and the end of that leg,
and only for the one order the courier is actually driving toward.

The same signal is what the WooCommerce tracking page needs to open its live
map, so the two consumers share one definition rather than each inventing a
notion of "on the way".

Storage — two Datetime fields, and why not a DocType
---------------------------------------------------
``tabSales Invoice`` sits at InnoDB's 65,535-byte row limit with 247 columns, so
no app can add another ``varchar`` to it. ``Datetime`` is fixed-width and small,
which is the whole reason the leg is expressed as two timestamps on the invoice
rather than as a side document:

* the tracking read already loads the invoice row, so the gate costs no query;
* there is no second document to fall out of step with the invoice; and
* re-opening a leg is one write, which matters because DropPin's protocol
  explicitly allows an order to be skipped and returned to later.

The cost is that only the *current* leg is retained — a leg re-opened after a
skip overwrites the first one's start. That is deliberate: leg history is not a
question anybody asks, and per-leg rows would be a document write per stop per
courier per shift to answer it.

The open/closed rule
--------------------
A leg is open when it has a start and that start is **strictly newer** than its
end. Comparing timestamps rather than asking "is the end null" is deliberate:
re-opening writes a new start and clears the end, but a clock that stepped
backwards, or a queued ``ended`` arriving after a newer ``started``, must not
silently reopen a closed leg. Timestamps decide, not field presence.

A tie resolves to **closed**, which matters more than it looks: a terminal
outcome writes the delivery time and the leg end in the same save, so equal
timestamps are the normal shape of a leg that has just finished — not one still
running.

Exactly one open leg per courier
--------------------------------
:func:`open_legs_for_courier` exists so the write path can close a courier's
other legs when a new one starts. Without that, a courier who taps Start on the
next order without ever ending the last one leaks two live maps — which is the
exact failure this module was built to prevent, arrived at by a different route.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import get_datetime, now_datetime

#: Datetime. When the courier set off **to this order**.
LEG_STARTED_FIELD = "custom_leg_started_at"

#: Datetime. When that leg ended — by hand, by a newer leg starting elsewhere,
#: or automatically by a terminal outcome.
LEG_ENDED_FIELD = "custom_leg_ended_at"

LEG_FIELDS = (LEG_STARTED_FIELD, LEG_ENDED_FIELD)


# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

def leg_fields_present() -> bool:
    """True when this site has migrated both leg fields.

    Every caller degrades rather than raises: a deployment that lands ahead of
    ``bench migrate`` must not take down the public tracking page or block a
    courier's delivery. What each caller degrades *to* differs, and that is the
    interesting part — see :func:`live_map_gate_open`.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        return all(meta.get_field(field) for field in LEG_FIELDS)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# The open/closed question
# ─────────────────────────────────────────────────────────────────────────────

def _as_datetime(value: Any):
    if value in (None, ""):
        return None
    try:
        return get_datetime(value)
    except Exception:
        return None


def is_leg_open(row: Any) -> bool:
    """Whether *row* — a dict or a Document — has an open leg right now.

    Pure: no queries, no settings, no clock. Every caller that needs a policy
    decision layered on top gets it somewhere else, so this stays the one
    definition of "open" and stays trivially testable.
    """
    if row is None:
        return False
    getter = row.get if hasattr(row, "get") else (lambda key: getattr(row, key, None))

    started = _as_datetime(getter(LEG_STARTED_FIELD))
    if started is None:
        return False

    ended = _as_datetime(getter(LEG_ENDED_FIELD))
    if ended is None:
        return True

    # `>` not `>=`: a start and an end landing in the same second is a leg that
    # was opened and closed, not one that is still running. Preferring "closed"
    # on a tie is the safe direction — this gate guards a live location, and a
    # terminal outcome writes both timestamps in the same save.
    return started > ended


def open_legs_for_courier(
    party_type: str,
    party: str,
    *,
    exclude_invoice: Optional[str] = None,
    limit: int = 20,
) -> List[str]:
    """Submitted invoices with an open leg for this courier.

    Used to enforce one open leg per courier at the moment a new one starts.
    The SQL filter is deliberately loose — it finds every invoice with a start
    and no end, or an end at-or-before the start — and :func:`is_leg_open` makes
    the final call, so the rule lives in exactly one place.
    """
    if not (party_type and party) or not leg_fields_present():
        return []

    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "custom_courier_party_type": party_type,
                "custom_courier_party": party,
                LEG_STARTED_FIELD: ["is", "set"],
            },
            fields=["name", LEG_STARTED_FIELD, LEG_ENDED_FIELD],
            order_by=f"{LEG_STARTED_FIELD} desc",
            limit=limit,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "delivery_leg: open leg lookup failed")
        return []

    excluded = str(exclude_invoice or "").strip()
    return [
        str(row["name"])
        for row in rows
        if str(row["name"]) != excluded and is_leg_open(row)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Write helpers — dicts, so callers merge them into one save
# ─────────────────────────────────────────────────────────────────────────────

def start_updates(at: Any = None) -> Dict[str, Any]:
    """Field updates that open a leg. Clearing the end is half the operation."""
    return {LEG_STARTED_FIELD: at or now_datetime(), LEG_ENDED_FIELD: None}


def end_updates(at: Any = None) -> Dict[str, Any]:
    """Field updates that close a leg.

    Returned as a dict rather than written here so the close rides along in the
    *same* save as the outcome that caused it. A delivered order whose leg
    closed in a second write can be observed, between the two, as delivered with
    a live map still open.
    """
    return {LEG_ENDED_FIELD: at or now_datetime()}


# ─────────────────────────────────────────────────────────────────────────────
# Policy: may the public tracking page show a live position?
# ─────────────────────────────────────────────────────────────────────────────

def allow_live_map_without_leg() -> bool:
    """Escape hatch for sites whose courier app cannot mark a leg yet.

    **Read the polarity before changing it.** The flag is phrased as *allow
    without* rather than *require*, so that its unset value — which is what
    every already-populated Single returns for a newly added ``Check`` field,
    since ``get_single_value`` casts through ``cint()`` — is ``0``, meaning the
    gate is ON. A field named ``require_delivery_leg_for_live_map`` defaulting
    to 1 would read as 0 on every existing site and quietly leave the leak in
    place on exactly the deployments that already have it.
    """
    try:
        return bool(
            int(
                frappe.db.get_single_value(
                    "Jarz POS Settings", "allow_live_map_without_leg"
                )
                or 0
            )
        )
    except Exception:
        return False


def live_map_gate_open(row: Any) -> bool:
    """Whether the courier's live position may be released for *row*.

    Three ways this answers yes, and the middle one is the compromise worth
    understanding:

    1. the order has an open leg — the intended path;
    2. the site has not migrated the leg fields yet — an un-migrated site cannot
       express a leg, so gating on one would silently kill live tracking on
       every deployment that is a commit behind. It degrades to the old
       behaviour, which is the leak, so the deploy asserts the fields landed;
    3. an operator has explicitly set :func:`allow_live_map_without_leg`.

    Callers must still confirm the order is actually out for delivery. This
    function answers only the leg question.
    """
    if not leg_fields_present():
        return True
    if is_leg_open(row):
        return True
    return allow_live_map_without_leg()

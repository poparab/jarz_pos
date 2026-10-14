"""F1 -- a POS delivery order must be taken against a pinned address.

The courier launch makes the door pin the thing a courier navigates to, the
"arrived" gate measures against, and the ETA is computed to. An order saved
against an address that only carries a territory centroid sends the courier to
the middle of a district, so the POS refuses to take it until the cashier has
saved a real pin (``api.geo.set_address_pin``).

**Scope.** Orders created or edited from the POS app only. The check sits in
``services.invoice_creation.create_pos_invoice`` (the single function behind the
POS create endpoint AND the amendment job) and, for a synchronous refusal, in
``api.manager.submit_invoice_amendment``. It is deliberately NOT a Sales Invoice
doc_event: a doc_event also fires for the Woo sync, and Woo orders arrive with
whatever address the shop gave us -- refusing them would drop customer orders.
Woo-origin orders, pickup/takeaway orders and deliver-at-branch (staff) orders
are exempt.

**Pin-grade** follows the existing geo ladder (``utils.geo``): a valid
coordinate whose recorded source ranks at least ``pos_link``. A
``territory_centroid`` is a guess at the district, not a door, so it does not
count. An address whose source is blank or unrecognised but which carries a
valid coordinate (pins saved before the ladder existed) is accepted -- refusing
those would block every legacy customer for no gain.

**Flag read HERE**, with its own default (Singles ignore JSON defaults), so a
background caller and the staging harness see the same switch as the API.
Off by default: landing this changes nothing until the owner arms it.
"""

from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe import _

from jarz_pos.utils.settings_utils import single_flag

SETTING_FIELDNAME = "require_address_pin_for_delivery"


class AddressPinRequired(frappe.ValidationError):
    """Raised for a POS delivery order whose address has no pin-grade location."""


def pin_required_enabled() -> bool:
    """Master switch (``Jarz POS Settings.require_address_pin_for_delivery``)."""
    try:
        return single_flag("Jarz POS Settings", SETTING_FIELDNAME, False)
    except Exception:
        return False


def pin_fields_are_pin_grade(latitude: Any, longitude: Any, source: Any) -> bool:
    """True when these three Address values describe a pin-grade location."""
    from jarz_pos.utils.geo import (
        SOURCE_POS_LINK,
        CONFIDENCE_RANK,
        is_valid_coordinate,
        normalize_source,
    )

    if not is_valid_coordinate(latitude, longitude):
        return False
    known = normalize_source(source)
    if not known:
        # Blank or unrecognised label: a legacy pin. The coordinate is valid, and
        # nothing says it is a centroid, so it passes.
        return True
    return CONFIDENCE_RANK.get(known, 0) >= CONFIDENCE_RANK[SOURCE_POS_LINK]


def address_is_pin_grade(address_name: Optional[str]) -> bool:
    """True when the named Address carries a pin-grade location."""
    name = str(address_name or "").strip()
    if not name:
        return False
    row = frappe.db.get_value(
        "Address",
        name,
        ["custom_latitude", "custom_longitude", "custom_geo_source"],
        as_dict=True,
    )
    if not row:
        return False
    return pin_fields_are_pin_grade(
        row.get("custom_latitude"), row.get("custom_longitude"), row.get("custom_geo_source")
    )


def pin_missing_message() -> str:
    """The refusal text. Names the fix, not just the problem."""
    return _(
        "This delivery order needs a location pin. Paste the customer's Google "
        "Maps link on the delivery address (or choose a pinned address) before "
        "saving the order."
    )


def assert_amendment_pin(
    source_invoice: Any,
    shipping_address_name: Optional[str] = None,
    pickup: Any = None,
) -> None:
    """Synchronous pre-check for ``submit_invoice_amendment``.

    The amendment runs as a job, and a refusal raised inside it is easy to lose
    on the way back to the app; refusing here, before anything is enqueued, hands
    the client a real ``AddressPinRequired``. Mirrors the exemptions of
    :func:`assert_delivery_pin` from the SOURCE invoice's fields. An order with a
    commercial policy (staff/employee and other non-standard purposes) is left to
    the check inside invoice creation, which sees the resolved policy.
    """
    if not pin_required_enabled():
        return

    def _get(field: str) -> Any:
        try:
            return source_invoice.get(field)
        except Exception:
            return None

    from jarz_pos.utils.employee_link import EMPLOYEE_ORDER_PURPOSE

    if _get("custom_commercial_policy") or str(_get("custom_order_purpose") or "").strip() == EMPLOYEE_ORDER_PURPOSE:
        return
    is_pickup = _truthy(pickup) or _truthy(_get("custom_is_pickup"))
    address = (
        str(shipping_address_name or "").strip()
        or str(_get("shipping_address_name") or "").strip()
        or str(_get("customer_address") or "").strip()
    )
    assert_delivery_pin(address, pickup=is_pickup, woo_order_id=_get("woo_order_id"))


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def assert_delivery_pin(
    address_name: Optional[str],
    *,
    pickup: bool = False,
    woo_order_id: Any = None,
    deliver_at_branch: bool = False,
) -> None:
    """Raise :class:`AddressPinRequired` when a POS delivery order has no pin.

    No-op when the switch is off, or the order is Woo-origin (*woo_order_id*
    set), a pickup/takeaway, or handed over at the branch. A lookup failure
    (schema behind the code) logs and lets the order through: a quality gate
    must not take order-taking down.
    """
    if pickup or deliver_at_branch or woo_order_id:
        return
    if not pin_required_enabled():
        return
    try:
        pinned = address_is_pin_grade(address_name)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "delivery_pin_requirement: pin lookup failed, gate skipped",
        )
        return
    if not pinned:
        frappe.throw(pin_missing_message(), AddressPinRequired)

"""A6 — the Out-for-Delivery pin gate.

One rule, one implementation: an order may not be dispatched when its delivery
address carries no usable coordinate. It lives here rather than in either API
module because there are **two** copies of ``_build_ofd_preview_errors`` —
``api/kanban.py`` (the single-invoice drag) and ``api/trips.py`` (the bulk trip
send) — and the whole point of A6 is that both enforce it. A rule added to one
copy is a rule the other path silently bypasses, which is the exact failure the
spec calls out. So the rule is written once and each copy appends one obvious
line calling it.

**Why it hangs off the shortage preview.** Both copies already receive the dict
from ``services.delivery_handling.get_ofd_shortage_preview``, and that dict's
``invoices`` key is the authoritative list of invoices *this dispatch is about
to move*. Deriving the list from anywhere else would risk gating a different set
of invoices than the one being sent — e.g. the trip path deliberately excludes
stops that are already Out for Delivery, and re-gating those would make a retry
of a half-finished trip permanently un-sendable.

**Fail-open, loudly.** If the Address geo fields have not migrated yet, or a
lookup raises, no error is produced and the failure is written to the Error Log.
Blocking every dispatch in the company because a schema sync is one deploy
behind is a far worse outcome than a soft-launch quality gate not firing — and
this gate's value is that staff paste a maps link, not that the ledger stays
correct. The silence is never total: it is logged, because a bare
``except: pass`` here is how a v16 rejection once hid for a month.

**Flag read HERE, in the service** — mirroring
``services.invoice_return.returns_enabled`` and
``services.courier_delivery.courier_delivery_enabled``. A flag checked in the
API layer is a flag the staging harness and any background caller bypass.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import frappe
from frappe import _

#: Settings check that arms the gate. Default 0 — the no-deploy rollback lever.
SETTING_FIELDNAME = "require_delivery_pin_for_ofd"

#: The two Address columns that hold the door pin. ``geo_resolution`` owns the
#: writes; this module only ever reads them.
PIN_FIELDS = ("custom_latitude", "custom_longitude")


def pin_gate_enabled() -> bool:
    """Master switch (``Jarz POS Settings.require_delivery_pin_for_ofd``).

    Off by default, so landing this code changes no behaviour anywhere until
    somebody deliberately arms it. Flipping it needs no deploy and no restart,
    which is what makes it a usable rollback lever rather than a code change.

    Returns ``False`` on any failure — including "there is no site / no DB",
    which is the case in the mock test suites of the two API modules that call
    into here. Those suites patch ``api.kanban.frappe`` and ``api.trips.frappe``
    but not this module's, so a hard failure here would break tests that have
    nothing to do with the gate.
    """
    try:
        return bool(int(frappe.db.get_single_value("Jarz POS Settings", SETTING_FIELDNAME) or 0))
    except Exception:
        return False


def invoice_delivery_address(invoice_name: str) -> Optional[str]:
    """The Address an invoice will actually be delivered to, or ``None``.

    ``shipping_address_name`` first, ``customer_address`` as the fallback — the
    same precedence every other reader in this app uses
    (``utils/invoice_utils``, ``api/kanban``, ``api/customer``). Getting the
    precedence wrong here would gate on the billing address, which for a B2B
    order is a different place entirely.
    """
    name = str(invoice_name or "").strip()
    if not name:
        return None
    row = frappe.db.get_value(
        "Sales Invoice",
        name,
        ["shipping_address_name", "customer_address"],
        as_dict=True,
    )
    if not row:
        return None
    return (
        str(row.get("shipping_address_name") or "").strip()
        or str(row.get("customer_address") or "").strip()
        or None
    )


def address_has_pin(address_name: Optional[str]) -> bool:
    """True when *address_name* carries a usable coordinate pair.

    Delegates the "usable" judgement to
    :func:`jarz_pos.utils.geo.is_valid_coordinate`, which already rejects the
    two cases that matter: an unset Float column (Frappe creates them
    ``NOT NULL DEFAULT 0``, so "no pin" reads back as ``0.0``, not ``NULL``) and
    Null Island ``(0, 0)``, which is a parse artefact rather than a delivery in
    the Gulf of Guinea. That is exactly the "both zero/empty" condition the spec
    describes, and reusing the shared predicate keeps this gate and the pin
    writer agreeing on what a pin is.
    """
    from jarz_pos.utils.geo import is_valid_coordinate

    name = str(address_name or "").strip()
    if not name:
        return False
    row = frappe.db.get_value("Address", name, list(PIN_FIELDS), as_dict=True)
    if not row:
        return False
    return is_valid_coordinate(row.get("custom_latitude"), row.get("custom_longitude"))


def invoice_has_delivery_pin(invoice_name: str) -> bool:
    """True when the invoice's delivery address carries a usable pin."""
    return address_has_pin(invoice_delivery_address(invoice_name))


def missing_pin_error(invoice_name: str) -> str:
    """The blocker text. Names the fix, not just the problem.

    Wrapped in ``_()`` so the Arabic-speaking half of the floor staff gets it in
    Arabic once the string is in the translation file: a blocker that says only
    "no pin" sends staff to a manager, whereas one that says *add a location pin
    to the customer's address* sends them to the field they can actually fix.
    """
    return _(
        "Invoice {0}: the delivery address has no location pin. Add a location "
        "pin to the customer's address before dispatch."
    ).format(invoice_name)


def build_missing_pin_errors(preview: Optional[Dict[str, Any]]) -> List[str]:
    """Blockers for every invoice in *preview* whose address has no pin.

    ``preview`` is the dict from
    ``services.delivery_handling.get_ofd_shortage_preview``; only its
    ``invoices`` key is read. Returns ``[]`` when the gate is disarmed, when the
    preview covers no invoices, or when anything at all goes wrong — see the
    module docstring on failing open.
    """
    if not pin_gate_enabled():
        return []

    invoice_names = _invoice_names(preview)
    if not invoice_names:
        return []

    errors: List[str] = []
    for invoice_name in invoice_names:
        try:
            if invoice_has_delivery_pin(invoice_name):
                continue
        except Exception:
            # Schema behind the code, or a lookup failure. Fail open on THIS
            # invoice and say so in the Error Log; do not take the dispatch down
            # and do not silently pretend it passed without a trace.
            frappe.log_error(
                frappe.get_traceback(),
                f"ofd_pin_gate: pin lookup failed for {invoice_name}, gate skipped",
            )
            continue
        errors.append(missing_pin_error(invoice_name))

    return errors


def _invoice_names(preview: Optional[Dict[str, Any]]) -> List[str]:
    """Invoice names from a shortage preview, tolerant of a partial dict.

    Both API modules' unit suites hand ``_build_ofd_preview_errors`` a
    hand-written preview that omits ``invoices`` entirely, so this must treat a
    missing key as "nothing to gate" rather than raise.
    """
    if not isinstance(preview, dict):
        return []
    raw: Iterable[Any] = preview.get("invoices") or []
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    out: List[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
    return out

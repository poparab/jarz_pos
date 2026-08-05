"""Address ``before_save`` hook: keep the confidence ladder self-consistent.

This handler fires on **every Address save on the site** — the Desk form, the
POS customer flow, the CRM, and, critically, the WooCommerce bulk customer sync
which can save thousands of Addresses in one job. That blast radius dictates
three hard constraints, and they are constraints, not preferences:

1. **Zero database queries.** Anything that reads the DB here multiplies by
   every address in a Woo sync batch, turning one job into thousands of extra
   round-trips. Everything below reads the in-memory document only.
2. **Zero network.** Same reason, worse consequences.
3. **The entire body is wrapped in try/except.** A raise here would abort the
   save of a document this app does not own. A geo field being briefly out of
   step is a cosmetic problem; a customer address that will not save is an
   outage.

**What it does NOT do:** it never enforces the never-downgrade rule. That needs
the previously-stored source, which needs a DB read, which constraint 1
forbids — and it belongs in ``services/geo_resolution`` anyway, the single
writer that exists precisely so the rule has one home.

**What it does:** clamps ``custom_geo_confidence`` to the integer rank of
``custom_geo_source`` so the pair can never be observed disagreeing, and blanks
a coordinate pair that is not a real position. Someone editing the Desk form, or
an import setting a source by hand, cannot leave a stale rank behind.

**Fields it must never touch:** ``address_line1``, ``address_line2``, ``city``,
``state``, ``pincode``, ``country``, ``phone``, ``email_id``, ``address_type``,
``is_shipping_address``. That is the WooCommerce outbound-push trigger set (and
``address_line2`` is also in the dedup signature). Writing any of them from here
would fan a customer sync *and* an invoice sync out to WooCommerce for every
address saved, and fork duplicate Address records on the next dedup pass.
"""

from __future__ import annotations

from typing import Any, Optional

try:  # pragma: no cover - import safety outside a bench
    import frappe
except Exception:
    frappe = None  # type: ignore


def clamp_geo_confidence(doc: Any, method: Optional[str] = None) -> None:
    """Force ``custom_geo_confidence`` to agree with ``custom_geo_source``.

    In-memory only, no queries, never raises. See the module docstring for why
    each of those three is non-negotiable.
    """
    try:
        # Cheap structural bail-out first: on a site that has not migrated the
        # geo fields yet this exits before importing anything.
        if not hasattr(doc, "get"):
            return
        if doc.get("custom_geo_source") is None and doc.get("custom_geo_confidence") is None:
            return

        from jarz_pos.utils.geo import (
            confidence_rank,
            is_valid_coordinate,
            normalize_source,
        )

        source = normalize_source(doc.get("custom_geo_source"))

        # An unrecognised label is cleared rather than kept: a source the ladder
        # cannot rank is a source the never-downgrade rule cannot protect, and a
        # silently unranked pin is worse than no label at all.
        if str(doc.get("custom_geo_source") or "").strip() and not source:
            doc.custom_geo_source = None

        latitude = doc.get("custom_latitude")
        longitude = doc.get("custom_longitude")
        has_pin = is_valid_coordinate(latitude, longitude)

        if not has_pin:
            # No usable position ⇒ no source and no rank. Leaving a source behind
            # on a blank pin would make the address outrank a genuine later fix.
            if source:
                doc.custom_geo_source = None
            doc.custom_geo_confidence = 0
            return

        doc.custom_geo_confidence = confidence_rank(source)
    except Exception:
        try:
            if frappe:
                frappe.log_error(
                    frappe.get_traceback(), "Address clamp_geo_confidence failed"
                )
        except Exception:
            pass

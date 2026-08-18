"""Catch orders whose delivery territory disagrees with the branch that shipped.

The problem, verified on staging (2026-08-19): **1,285 submitted invoices
(~EGP 520k)** have ``Sales Invoice.pos_profile <> Territory.pos_profile`` for
their own territory, and the crossings are systematic and bidirectional (Nasr
city <-> Dokki <-> 6th of October). A further 262 invoices carry no territory
at all (221 in 2025, 41 in 2026).

The owner's ruling, which this module implements literally:

* **The branch that shipped is the fact.** The territory is what may need
  correcting. So nothing here changes ``pos_profile``, changes how a profile is
  chosen, blocks an order, or writes to a Sales Invoice *at all* -- every path
  over Sales Invoice in this module is read-only.
* Record the crossing so a human can review it and fix the territory afterwards.

**Not every mismatch is a defect.** Broken down by lane on production, 1,260 of
the 1,285 are Woo-origin and only 25 are POS-origin. A Woo order derives its
``pos_profile`` FROM ``Territory.pos_profile`` at creation, so the two agreed
when it shipped: a disagreement today means the Territory was re-pointed at
another branch afterwards. Those rows are historical facts, and they outnumber
the live ones 50:1. They are still recorded -- detection is exactly as specced
and filters nothing -- but they are flagged ``historical_repointing`` and the
backfill is windowed by default so the 25 live rows are not buried.

Guarantees (mirrored from ``services/label_stock`` and ``services/consumable_deduction``):

* No top-level ``frappe`` calls -- the module imports cleanly at boot.
* ``record_invoice_territory_exception`` **never raises**. It runs inside
  invoice submission; an invoice must never fail because logging failed. The
  insert is fenced behind a savepoint so a failure cannot poison the caller's
  transaction either.
* Idempotent per ``(sales_invoice, exception_type)``. Re-running the backfill
  over the same invoices creates nothing new.
* Failures are reported with ``frappe.log_error``. ``frappe.logger().info()`` is
  invisible on staging and production (default level is ERROR off a dev server),
  so the module logger pins its own level for the summary lines.

Public API (this is what WP-3b and the Desk rely on -- do not rename)::

    record_invoice_territory_exception(invoice_doc) -> str | None
    record_territory_exception_on_submit(doc, method=None) -> None   # doc_event
    resolve_territory_exception(exception_name, status=..., user=None) -> bool
    backfill_territory_exceptions(...) -> dict                       # @frappe.whitelist
    close_resolved_territory_exceptions(limit=...) -> dict
    run_territory_exception_sweep(days=...) -> dict                  # scheduler entry

Trigger the historical backfill by hand with::

    bench --site <site> execute jarz_pos.services.territory_exceptions.backfill_territory_exceptions
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import frappe

from jarz_pos.constants import ROLES

EXCEPTION_DOCTYPE = "Jarz Territory Exception"
INVOICE_DOCTYPE = "Sales Invoice"

#: Kept in step with the ``exception_type`` Select options in the DocType JSON.
#: Frozen at two values by the shared spec (§3) -- the lane split below is a
#: separate field precisely so this enum stays exactly as specced.
TYPE_BRANCH_MISMATCH = "Branch Territory Mismatch"
TYPE_TERRITORY_UNRESOLVED = "Territory Unresolved"
EXCEPTION_TYPES = (TYPE_BRANCH_MISMATCH, TYPE_TERRITORY_UNRESOLVED)

#: Kept in step with the ``order_lane`` Select options in the DocType JSON.
LANE_POS = "POS"
LANE_WOO = "Woo"

#: Kept in step with the ``status`` Select options in the DocType JSON.
STATUS_OPEN = "Open"
STATUS_TERRITORY_CORRECTED = "Territory Corrected"
STATUS_ACCEPTED = "Accepted"
STATUS_IGNORED = "Ignored"
RESOLVED_STATUSES = (STATUS_TERRITORY_CORRECTED, STATUS_ACCEPTED, STATUS_IGNORED)

#: Territory values that mean "nothing was actually resolved". ``All Territories``
#: is the ERPNext root node -- carrying it is the same as carrying nothing: no
#: POS Profile, no delivery income, no branch.
_UNRESOLVED_TERRITORY_VALUES = {"", "all territories"}

#: Standard Sales Invoice columns only. ``custom_*`` columns are read through a
#: separate guarded call: the backend CI gate runs BEFORE ``bench migrate``, so a
#: custom field can be missing from the table while this code is already loaded.
_INVOICE_FIELDS = (
    "name",
    "docstatus",
    "posting_date",
    "customer",
    "territory",
    "pos_profile",
    "shipping_address_name",
    "customer_address",
    "grand_total",
    "currency",
)

#: Optional Sales Invoice columns. ``custom_kanban_profile`` is this app's;
#: ``woo_source_type`` / ``woo_transaction_id`` belong to the WooCommerce app and
#: are referenced BY NAME STRING ONLY -- the two apps never import each other, so
#: every one of these is read through a ``has_column`` guard and is simply absent
#: when that app is not installed.
_OPTIONAL_INVOICE_FIELDS = (
    "custom_kanban_profile",
    "woo_source_type",
    "woo_transaction_id",
)

#: Per-process memo of which optional columns actually exist, keyed by
#: ``(site, fieldname)``. The schema only changes at ``bench migrate``, which
#: restarts the workers -- but one worker serves every site on the bench, and
#: the WooCommerce app is not necessarily installed on all of them, so a
#: site-blind memo would answer for the wrong database.
_OPTIONAL_FIELD_CACHE: Dict[tuple, bool] = {}

#: Rows fetched (and committed) per backfill batch.
DEFAULT_BATCH_SIZE = 500

#: Default backfill window. On production 1,260 of the 1,285 known mismatches are
#: Woo-lane historical re-pointings stretching back to 2024, and only 25 are live
#: POS-lane divergences. A full-history default would bury those 25 and the owner
#: would stop reading the queue -- so history is opt-in via ``all_history=1``.
DEFAULT_BACKFILL_DAYS = 90

#: How far back the daily sweep looks. The ``on_submit`` hook is the primary
#: catch; this window exists to pick up invoices written while that hook was
#: failing, and to drive the auto-close pass.
DEFAULT_SWEEP_DAYS = 7

#: Hard ceiling on one auto-close pass so the scheduler slot stays bounded.
DEFAULT_CLOSE_LIMIT = 2000

#: Who may fire a backfill or close a row. Deliberately narrower than the
#: DocType's read permission: a backfill walks every submitted invoice on site.
BACKFILL_ROLES = ROLES.ADMIN | {ROLES.JARZ_MANAGER, ROLES.ADMINISTRATOR}


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _logger():
    """Module logger that is actually audible on staging and production.

    ``frappe.logger()`` inherits the site's default level (ERROR off a dev
    server), which is how a month of ``.info()`` diagnostics once vanished. The
    level is pinned here so the sweep/backfill summaries reach the log file;
    anything that must be seen regardless goes through ``log_error``.
    """
    logger = frappe.logger("jarz_pos.territory_exceptions", allow_site=True)
    try:
        logger.setLevel(logging.INFO)
    except Exception:
        pass
    return logger


def _log_failure(title: str, message: Optional[str] = None) -> None:
    """Record a failure without ever raising out of the error path itself."""
    try:
        frappe.log_error(
            message or frappe.get_traceback(),
            f"territory_exceptions: {title}"[:140],
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(value: Any) -> str:
    return str(value or "").strip()


def _val(source: Any, field: str, default: Any = None) -> Any:
    """Read ``field`` off a Document, a dict, or a plain object."""
    if source is None:
        return default
    if isinstance(source, dict):
        value = source.get(field, default)
    else:
        try:
            value = source.get(field)
        except Exception:
            value = getattr(source, field, default)
    return default if value is None else value


def _exception_doctype_ready() -> bool:
    """False before this DocType's first ``bench migrate`` on a site."""
    try:
        return bool(frappe.db.table_exists(EXCEPTION_DOCTYPE))
    except Exception:
        return False


def _safe_get_value(doctype: str, name: str, fields: Any, as_dict: bool = False) -> Any:
    try:
        return frappe.db.get_value(doctype, name, fields, as_dict=as_dict)
    except Exception:
        return {} if as_dict else None


def _territory_has_pos_profile_field() -> bool:
    """``Territory.pos_profile`` is a Custom Field this app seeds; it can be absent."""
    try:
        return bool(frappe.get_meta("Territory").get_field("pos_profile"))
    except Exception:
        return False


def _territory_pos_profile(territory: Any) -> Optional[str]:
    """The branch a territory points at.

    Deliberately a direct read rather than
    ``invoice_utils.resolve_pos_profile_for_territory``: an invoice's stored
    ``territory`` is already a canonical Territory name, so the fuzzy code/label
    resolution that helper runs first would only add failure modes -- and this
    must mirror the staging SQL exactly (``pos_profile <> Territory.pos_profile``).
    """
    territory_name = _clean(territory)
    if not territory_name or not _territory_has_pos_profile_field():
        return None
    return _clean(_safe_get_value("Territory", territory_name, "pos_profile")) or None


def _address_snapshot(address_name: Any) -> Dict[str, Any]:
    name = _clean(address_name)
    if not name:
        return {}
    row = _safe_get_value("Address", name, ["city", "state"], as_dict=True) or {}
    return dict(row)


def _address_territory(address_name: Any) -> Optional[str]:
    """What the shipping address says the territory should have been.

    ``Address.city`` is free text on this site (a WooCommerce label), never a
    Territory name -- so this must go through the shared resolver rather than
    ``db.exists("Territory", city)``. Imported lazily and fenced: a change in
    that module must never be able to break invoice submission through here.
    """
    name = _clean(address_name)
    if not name:
        return None
    try:
        from jarz_pos.utils.invoice_utils import resolve_address_territory

        return _clean(resolve_address_territory(name)) or None
    except Exception:
        return None


def _sole_territory_for_profile(pos_profile: Any) -> Optional[str]:
    """The territory a branch implies -- only when exactly one points at it."""
    profile = _clean(pos_profile)
    if not profile or not _territory_has_pos_profile_field():
        return None
    try:
        rows = frappe.get_all(
            "Territory",
            filters={"pos_profile": profile, "is_group": 0},
            pluck="name",
            limit_page_length=2,
        ) or []
    except Exception:
        return None
    return rows[0] if len(rows) == 1 else None


def _current_site() -> str:
    try:
        return str(getattr(frappe.local, "site", "") or "")
    except Exception:
        return ""


def _optional_invoice_fields() -> List[str]:
    """The optional columns that actually exist on this site's Sales Invoice."""
    site = _current_site()
    available: List[str] = []
    for field in _OPTIONAL_INVOICE_FIELDS:
        key = (site, field)
        if key not in _OPTIONAL_FIELD_CACHE:
            try:
                _OPTIONAL_FIELD_CACHE[key] = bool(frappe.db.has_column(INVOICE_DOCTYPE, field))
            except Exception:
                # Unknown rather than absent -- do not memoise a transient failure.
                continue
        if _OPTIONAL_FIELD_CACHE.get(key):
            available.append(field)
    return available


def _optional_invoice_values(source: Any, invoice_name: str) -> Dict[str, Any]:
    """Read the optional columns, with at most ONE extra query.

    During the batched backfill the columns are already in the fetched row, so
    this makes no query at all -- which is the difference between 2 queries and
    11,000 on a full-history sweep.
    """
    fields = _optional_invoice_fields()
    values: Dict[str, Any] = {}
    missing: List[str] = []

    for field in fields:
        if isinstance(source, dict):
            if field in source:
                values[field] = source.get(field)
            else:
                missing.append(field)
        else:
            # A Document carries its custom fields; .get() returns None if unset.
            values[field] = _val(source, field)

    if missing and invoice_name:
        row = _safe_get_value(INVOICE_DOCTYPE, invoice_name, missing, as_dict=True) or {}
        for field in missing:
            values[field] = row.get(field)

    return values


def detect_order_lane(*, woo_source_type: Any, woo_transaction_id: Any) -> str:
    """Which lane created this order: ``Woo`` or ``POS``.

    The heuristic is the coordinator's, unchanged, so the counts here line up
    with the production breakdown it was derived from: an invoice is Woo-origin
    when it carries a WooCommerce source type or transaction id.

    One carve-out, and it matters: spec §6 has WP-3b stamp
    ``woo_source_type = "pos"`` on POS orders so they stop reading as "unknown
    source" in channel analytics. A naive non-empty test would then reclassify
    every future POS order as Woo-origin and silently mark its mismatches as
    historical re-pointings -- exactly the rows the owner needs to see. The
    literal ``"pos"`` is therefore never a Woo marker. Today no invoice carries
    it, so this changes none of the verified production numbers.
    """
    source_type = _clean(woo_source_type)
    if source_type and source_type.casefold() != "pos":
        return LANE_WOO
    if _clean(woo_transaction_id):
        return LANE_WOO
    return LANE_POS


def is_historical_repointing(exception_type: str, lane: str) -> bool:
    """True when a mismatch is a stale Territory re-pointing rather than a defect.

    A Woo-origin order derives its ``pos_profile`` FROM ``Territory.pos_profile``
    at creation time. So on that lane the two agreed when the order shipped, and
    a disagreement today means the Territory was re-pointed afterwards: the
    invoice still records the branch that really shipped, and there is nothing
    to correct. On production that is 1,260 of the 1,285 mismatches.

    Never true for ``Territory Unresolved``: an order with no territory is
    actionable whichever lane created it (262 of them on production).
    """
    return exception_type == TYPE_BRANCH_MISMATCH and lane == LANE_WOO


def _money(amount: Any, currency: Any = "") -> str:
    try:
        text = f"{float(amount or 0):,.2f}"
    except Exception:
        text = str(amount or 0)
    code = _clean(currency)
    return f"{code} {text}".strip()


# ─────────────────────────────────────────────────────────────────────────────
# Detection (pure -- no DB access, unit-testable without a bench)
# ─────────────────────────────────────────────────────────────────────────────

def detect_exception_type(
    *,
    territory: Any,
    pos_profile: Any,
    territory_pos_profile: Any,
) -> Optional[str]:
    """Classify one order. Returns an exception type, or None when it is clean.

    * ``Territory Unresolved`` -- no territory, or the root ``All Territories``,
      which carries no branch, no delivery income and no routing.
    * ``Branch Territory Mismatch`` -- both branches are known and they differ.

    A missing ``pos_profile`` (543 invoices site-wide, nearly all Woo-created)
    or a territory with no POS Profile configured is deliberately NOT an
    exception: there is no second opinion to disagree with, so a reviewer has
    nothing to reconcile. Raising one would bury the 1,285 real crossings in
    noise.

    The branch comparison is case-folded. Two POS Profiles differing only in
    case cannot both exist, so a case difference is a storage artefact, never a
    real crossing.
    """
    territory_name = _clean(territory)
    if not territory_name or territory_name.lower() in _UNRESOLVED_TERRITORY_VALUES:
        return TYPE_TERRITORY_UNRESOLVED

    used = _clean(pos_profile)
    expected = _clean(territory_pos_profile)
    if used and expected and used.casefold() != expected.casefold():
        return TYPE_BRANCH_MISMATCH

    return None


def build_detail(snapshot: Dict[str, Any], exception_type: str) -> str:
    """A reviewer must be able to act on this row without opening the invoice."""
    invoice = snapshot.get("sales_invoice") or "?"
    posting_date = snapshot.get("posting_date") or "?"
    total = _money(snapshot.get("grand_total"), snapshot.get("currency") or "")
    used = snapshot.get("pos_profile_used") or "(none)"
    customer = snapshot.get("customer") or "(none)"
    address = snapshot.get("shipping_address") or "(none)"
    city = snapshot.get("address_city") or "(blank)"
    state = snapshot.get("address_state") or "(blank)"
    expected_territory = snapshot.get("expected_territory")

    lane = snapshot.get("order_lane") or "?"

    parts: List[str] = []
    if exception_type == TYPE_BRANCH_MISMATCH:
        parts.append(
            f"[{lane} lane] Order {invoice} ({posting_date}, {total}) shipped from POS Profile "
            f"'{used}', but its Territory '{snapshot.get('invoice_territory') or '(none)'}' "
            f"points at POS Profile '{snapshot.get('territory_pos_profile') or '(none)'}'."
        )
    else:
        parts.append(
            f"[{lane} lane] Order {invoice} ({posting_date}, {total}) carries no usable delivery "
            f"Territory (stored value: '{snapshot.get('raw_territory') or ''}'). "
            f"Shipped from POS Profile '{used}'."
        )

    parts.append(f"Customer {customer}; Address {address} (city='{city}', state='{state}').")

    if expected_territory:
        parts.append(f"Best guess at the correct Territory: '{expected_territory}'.")
    else:
        parts.append("No unambiguous Territory could be derived from the address or the branch.")

    if is_historical_repointing(exception_type, snapshot.get("order_lane") or ""):
        parts.append(
            "HISTORICAL: a Woo order takes its POS Profile FROM the Territory at "
            "creation, so these two agreed when it shipped. The Territory has since "
            "been re-pointed at another branch. The invoice still records the branch "
            "that really shipped -- nothing to correct here."
        )
    elif exception_type == TYPE_BRANCH_MISMATCH:
        parts.append(
            "LIVE: the operator's branch differed from the Territory's branch at the "
            "moment of sale. The branch that shipped is the fact -- correct the "
            "Territory (or the Territory's POS Profile), never the invoice."
        )
    else:
        parts.append(
            "Set the Territory on the customer's address so branch routing and "
            "shipping income stop guessing. The invoice itself is not editable."
        )
    return " ".join(parts)[:1000]


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot
# ─────────────────────────────────────────────────────────────────────────────

def _load_invoice_row(invoice_name: str) -> Dict[str, Any]:
    fields = list(_INVOICE_FIELDS) + _optional_invoice_fields()
    row = _safe_get_value(INVOICE_DOCTYPE, invoice_name, fields, as_dict=True) or {}
    return dict(row)


def build_snapshot(invoice_doc: Any) -> Dict[str, Any]:
    """Collect everything the exception row stores. Reads only; never writes."""
    if isinstance(invoice_doc, str):
        source: Any = _load_invoice_row(invoice_doc)
        if not source:
            return {}
    else:
        source = invoice_doc

    invoice_name = _clean(_val(source, "name"))
    if not invoice_name:
        return {}

    optional = _optional_invoice_values(source, invoice_name)

    raw_territory = _clean(_val(source, "territory"))
    pos_profile = _clean(_val(source, "pos_profile"))
    if not pos_profile:
        # ``custom_kanban_profile`` is seeded from ``pos_profile`` and preserved
        # across board reassignments, so on the 533 invoices that carry no
        # ``pos_profile`` at all it is the only surviving record of which branch
        # owned the order.
        pos_profile = _clean(optional.get("custom_kanban_profile"))

    shipping_address = _clean(_val(source, "shipping_address_name")) or _clean(
        _val(source, "customer_address")
    )
    address = _address_snapshot(shipping_address)
    resolved_territory = (
        raw_territory if raw_territory.lower() not in _UNRESOLVED_TERRITORY_VALUES else None
    )

    snapshot: Dict[str, Any] = {
        "sales_invoice": invoice_name,
        "docstatus": int(_val(source, "docstatus", 0) or 0),
        "posting_date": _val(source, "posting_date"),
        "customer": _clean(_val(source, "customer")) or None,
        "raw_territory": raw_territory,
        "invoice_territory": resolved_territory,
        "pos_profile_used": pos_profile or None,
        "territory_pos_profile": _territory_pos_profile(raw_territory),
        "shipping_address": shipping_address or None,
        "address_city": _clean(address.get("city")) or None,
        "address_state": _clean(address.get("state")) or None,
        "grand_total": _val(source, "grand_total", 0) or 0,
        "currency": _clean(_val(source, "currency")),
        "order_lane": detect_order_lane(
            woo_source_type=optional.get("woo_source_type"),
            woo_transaction_id=optional.get("woo_transaction_id"),
        ),
    }

    snapshot["expected_territory"] = _resolve_expected_territory(snapshot)
    return snapshot


def _resolve_expected_territory(snapshot: Dict[str, Any]) -> Optional[str]:
    """What the territory probably should have been.

    Order matters: the shipping address is the customer's actual location and
    therefore the strongest evidence. Falling back to "the only Territory that
    points at the shipping branch" is only safe when it genuinely is the only
    one -- several territories per branch is the normal shape here.
    """
    address_territory = _address_territory(snapshot.get("shipping_address"))
    if address_territory and address_territory != snapshot.get("invoice_territory"):
        return address_territory

    sole = _sole_territory_for_profile(snapshot.get("pos_profile_used"))
    if sole and sole != snapshot.get("invoice_territory"):
        return sole

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def record_invoice_territory_exception(invoice_doc) -> Optional[str]:
    """Inspect a Sales Invoice and log an exception row if branch<>territory or
    territory unresolved. Idempotent per (sales_invoice, exception_type).
    Returns the exception name, or None when the invoice is clean.
    NEVER raises -- invoice creation must not fail because logging failed.

    ``invoice_doc`` may be a Sales Invoice Document, a dict of its fields, or an
    invoice name.
    """
    try:
        return _record(invoice_doc)
    except Exception:
        _log_failure(f"record failed for {_describe(invoice_doc)}")
        return None


def record_territory_exception_on_submit(doc, method: Optional[str] = None) -> None:
    """``Sales Invoice.on_submit`` hook.

    Wired in ``hooks.py`` rather than left to the callers so that BOTH lanes are
    covered -- the POS create path and the WooCommerce inbound path, which never
    goes through ``services/invoice_creation``. Double-calling is harmless: the
    recorder is idempotent per (invoice, type).
    """
    record_invoice_territory_exception(doc)


def _describe(invoice_doc: Any) -> str:
    try:
        if isinstance(invoice_doc, str):
            return invoice_doc
        return _clean(_val(invoice_doc, "name")) or "?"
    except Exception:
        return "?"


def _record(invoice_doc: Any) -> Optional[str]:
    if not _exception_doctype_ready():
        return None

    snapshot = build_snapshot(invoice_doc)
    if not snapshot:
        return None

    # A cancelled invoice shipped nothing; there is no territory to correct.
    if snapshot.get("docstatus") == 2:
        return None

    exception_type = detect_exception_type(
        territory=snapshot.get("raw_territory"),
        pos_profile=snapshot.get("pos_profile_used"),
        territory_pos_profile=snapshot.get("territory_pos_profile"),
    )
    if not exception_type:
        return None

    return _insert_exception(snapshot, exception_type)


def _existing_exception(invoice_name: str, exception_type: str) -> Optional[str]:
    try:
        found = frappe.db.exists(
            EXCEPTION_DOCTYPE,
            {"sales_invoice": invoice_name, "exception_type": exception_type},
        )
    except Exception:
        return None
    return found or None


def _insert_exception(snapshot: Dict[str, Any], exception_type: str) -> Optional[str]:
    """Create the row, or return the one that is already there.

    Fenced behind a savepoint: this runs inside the invoice's own submit
    transaction, and an audit row failing to insert must never be able to take
    the invoice down with it.
    """
    invoice_name = snapshot["sales_invoice"]

    existing = _existing_exception(invoice_name, exception_type)
    if existing:
        return existing

    savepoint = "jarz_territory_exception"
    try:
        frappe.db.savepoint(savepoint)
    except Exception:
        savepoint = ""

    try:
        doc = frappe.get_doc(
            {
                "doctype": EXCEPTION_DOCTYPE,
                "sales_invoice": invoice_name,
                "exception_type": exception_type,
                "status": STATUS_OPEN,
                "order_lane": snapshot.get("order_lane"),
                "historical_repointing": int(
                    is_historical_repointing(exception_type, snapshot.get("order_lane") or "")
                ),
                "posting_date": snapshot.get("posting_date"),
                "customer": snapshot.get("customer"),
                "shipping_address": snapshot.get("shipping_address"),
                "address_city": snapshot.get("address_city"),
                "address_state": snapshot.get("address_state"),
                "invoice_territory": snapshot.get("invoice_territory"),
                "expected_territory": snapshot.get("expected_territory"),
                "pos_profile_used": snapshot.get("pos_profile_used"),
                "territory_pos_profile": snapshot.get("territory_pos_profile"),
                "grand_total": snapshot.get("grand_total"),
                "detail": build_detail(snapshot, exception_type),
            }
        )
        # Link validation is a liability here, not a safety net: a dangling
        # Territory or Address on a historical invoice is precisely the kind of
        # thing this record exists to surface, and must not stop it being filed.
        doc.flags.ignore_links = True
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        if savepoint:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
        # Lost a race with a concurrent writer? Then the row exists and we are done.
        existing = _existing_exception(invoice_name, exception_type)
        if existing:
            return existing
        _log_failure(f"insert failed for {invoice_name}")
        return None


@frappe.whitelist()
def resolve_territory_exception(
    exception_name: str,
    status: str = STATUS_TERRITORY_CORRECTED,
    user: Optional[str] = None,
) -> bool:
    """Mark one exception row reviewed: sets ``status``/``resolved_on``/``resolved_by``.

    Raises on a bad status or a missing row -- this entry point IS interactive,
    and a silent no-op would leave a manager believing they had closed something.
    """
    _assert_backfill_access()

    new_status = _clean(status) or STATUS_TERRITORY_CORRECTED
    if new_status not in RESOLVED_STATUSES:
        frappe.throw(
            f"Invalid status '{new_status}'. Expected one of: {', '.join(RESOLVED_STATUSES)}"
        )

    doc = frappe.get_doc(EXCEPTION_DOCTYPE, _clean(exception_name))
    doc.status = new_status
    doc.resolved_on = frappe.utils.now_datetime()
    doc.resolved_by = _clean(user) or frappe.session.user
    doc.flags.ignore_links = True
    doc.save(ignore_permissions=True)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Backfill / sweep
# ─────────────────────────────────────────────────────────────────────────────

def _assert_backfill_access() -> None:
    roles = {str(role or "").strip() for role in (frappe.get_roles() or [])}
    if not roles.intersection(BACKFILL_ROLES):
        frappe.throw("Not permitted: manager access required", frappe.PermissionError)


@frappe.whitelist()
def backfill_territory_exceptions(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    all_history: int = 0,
    limit: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: int = 0,
) -> Dict[str, Any]:
    """Sweep existing SUBMITTED invoices and file the exceptions already on the books.

    **Windowed by default (last 90 days), on purpose.** The production
    breakdown of the 1,285 branch mismatches is 1,260 Woo-lane historical
    re-pointings (947 in 2025, 311 in 2026, 2 in 2024) against just 25 live
    POS-lane divergences. Filing all of history at once with equal weight would
    bury those 25, and a queue nobody reads catches nothing. History is
    therefore opt-in: pass ``all_history=1`` (or an explicit ``from_date``).

    Whatever the window, the summary reports ``skipped_by_window`` -- the count
    of submitted invoices deliberately left unscanned -- so a partial run can
    never look silently complete.

    Batched, idempotent and safe to re-run: every row is a create-or-noop keyed
    on ``(sales_invoice, exception_type)``.

    **Reads Sales Invoice only.** Nothing in this path writes to, re-saves or
    re-submits an invoice -- those documents are submitted and carry GL entries.

    Args:
        from_date / to_date: explicit ``posting_date`` window. An explicit
            ``from_date`` overrides the 90-day default.
        all_history: 1 to sweep the entire book (no date filter at all).
        limit: stop after this many invoices (a rehearsal aid, not a filter).
        batch_size: invoices fetched and committed per round trip.
        dry_run: 1 to count what WOULD be filed without inserting anything.

    Returns a summary dict, which is also written to the log.
    """
    _assert_backfill_access()
    return _run_backfill(
        from_date=from_date,
        to_date=to_date,
        all_history=bool(int(all_history or 0)),
        limit=limit,
        batch_size=batch_size,
        dry_run=bool(int(dry_run or 0)),
    )


def _resolve_backfill_window(
    from_date: Optional[str],
    to_date: Optional[str],
    all_history: bool,
) -> tuple:
    """(from_date, to_date). Defaults to the last ``DEFAULT_BACKFILL_DAYS`` days."""
    if all_history:
        return None, to_date
    if from_date:
        return from_date, to_date
    try:
        return (
            frappe.utils.add_days(frappe.utils.nowdate(), -DEFAULT_BACKFILL_DAYS),
            to_date,
        )
    except Exception:
        return None, to_date


def _count_outside_window(from_date: Optional[str], to_date: Optional[str]) -> int:
    """Submitted invoices the window deliberately leaves unscanned."""
    skipped = 0
    try:
        if from_date:
            skipped += frappe.db.count(
                INVOICE_DOCTYPE, {"docstatus": 1, "posting_date": ("<", from_date)}
            )
        if to_date:
            skipped += frappe.db.count(
                INVOICE_DOCTYPE, {"docstatus": 1, "posting_date": (">", to_date)}
            )
    except Exception:
        _log_failure("window skip count failed")
        return -1
    return skipped


def _run_backfill(
    *,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    all_history: bool = False,
    limit: Optional[int] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "scanned": 0,
        "clean": 0,
        "created": 0,
        "already_recorded": 0,
        "failed": 0,
        "by_type": {TYPE_BRANCH_MISMATCH: 0, TYPE_TERRITORY_UNRESOLVED: 0},
        "by_lane": {LANE_POS: 0, LANE_WOO: 0},
        "historical_repointings": 0,
        "live": 0,
        "last_invoice": None,
        "dry_run": bool(dry_run),
    }

    if not _exception_doctype_ready():
        summary["skipped"] = "doctype not migrated yet"
        return summary

    from_date, to_date = _resolve_backfill_window(from_date, to_date, all_history)
    summary["window_from"] = from_date or "(all history)"
    summary["window_to"] = to_date or "(today)"
    summary["all_history"] = bool(all_history)
    summary["skipped_by_window"] = _count_outside_window(from_date, to_date)

    try:
        page_size = max(1, min(int(batch_size or DEFAULT_BATCH_SIZE), 5000))
    except Exception:
        page_size = DEFAULT_BATCH_SIZE

    remaining: Optional[int] = None
    if limit:
        try:
            remaining = max(0, int(limit))
        except Exception:
            remaining = None

    cursor = ""
    while True:
        if remaining is not None and remaining <= 0:
            break

        page = page_size if remaining is None else min(page_size, remaining)
        rows = _fetch_invoice_batch(cursor, page, from_date, to_date)
        if not rows:
            break

        for row in rows:
            cursor = row.get("name") or cursor
            summary["scanned"] += 1
            if remaining is not None:
                remaining -= 1
            _backfill_one(row, summary, dry_run)

        summary["last_invoice"] = cursor

        if not dry_run:
            try:
                frappe.db.commit()
            except Exception:
                _log_failure(f"backfill commit failed at {cursor}")

        if len(rows) < page:
            break

    _logger().info(
        "territory_exceptions backfill: window=%s..%s all_history=%s skipped_by_window=%s "
        "scanned=%s created=%s already=%s clean=%s failed=%s by_type=%s by_lane=%s "
        "historical=%s live=%s dry_run=%s",
        summary["window_from"],
        summary["window_to"],
        summary["all_history"],
        summary["skipped_by_window"],
        summary["scanned"],
        summary["created"],
        summary["already_recorded"],
        summary["clean"],
        summary["failed"],
        summary["by_type"],
        summary["by_lane"],
        summary["historical_repointings"],
        summary["live"],
        summary["dry_run"],
    )
    return summary


def _fetch_invoice_batch(
    cursor: str,
    page: int,
    from_date: Optional[str],
    to_date: Optional[str],
) -> List[Dict[str, Any]]:
    """Keyset pagination on ``name``.

    Deliberately not ``limit_start``: this sweep inserts rows while it runs, and
    an offset walk over a table being written to skips records. Ordering by the
    primary key with a ``>`` cursor is stable and makes a resumed run cheap.
    """
    filters: Dict[str, Any] = {"docstatus": 1}
    if cursor:
        filters["name"] = (">", cursor)

    if from_date and to_date:
        filters["posting_date"] = ("between", [from_date, to_date])
    elif from_date:
        filters["posting_date"] = (">=", from_date)
    elif to_date:
        filters["posting_date"] = ("<=", to_date)

    try:
        return frappe.get_all(
            INVOICE_DOCTYPE,
            filters=filters,
            # Optional columns come along for the ride so classifying the lane
            # costs zero extra queries per invoice.
            fields=list(_INVOICE_FIELDS) + _optional_invoice_fields(),
            order_by="name asc",
            limit_page_length=page,
        ) or []
    except Exception:
        _log_failure(f"backfill fetch failed after {cursor or '(start)'}")
        return []


def _backfill_one(row: Dict[str, Any], summary: Dict[str, Any], dry_run: bool) -> None:
    try:
        snapshot = build_snapshot(row)
        if not snapshot:
            summary["failed"] += 1
            return

        exception_type = detect_exception_type(
            territory=snapshot.get("raw_territory"),
            pos_profile=snapshot.get("pos_profile_used"),
            territory_pos_profile=snapshot.get("territory_pos_profile"),
        )
        if not exception_type:
            summary["clean"] += 1
            return

        lane = snapshot.get("order_lane") or ""
        summary["by_type"][exception_type] = summary["by_type"].get(exception_type, 0) + 1
        if lane:
            summary["by_lane"][lane] = summary["by_lane"].get(lane, 0) + 1
        if is_historical_repointing(exception_type, lane):
            summary["historical_repointings"] += 1
        else:
            summary["live"] += 1

        if _existing_exception(snapshot["sales_invoice"], exception_type):
            summary["already_recorded"] += 1
            return

        if dry_run:
            summary["created"] += 1
            return

        if _insert_exception(snapshot, exception_type):
            summary["created"] += 1
        else:
            summary["failed"] += 1
    except Exception:
        summary["failed"] += 1
        name = row.get("name") if isinstance(row, dict) else "?"
        _log_failure(f"backfill row failed for {name}")


def close_resolved_territory_exceptions(limit: int = DEFAULT_CLOSE_LIMIT) -> Dict[str, Any]:
    """Auto-close Open rows whose condition no longer holds.

    Repointing one ``Territory.pos_profile`` fixes the crossing for every order
    in that territory at once -- which is the whole point of correcting the
    territory rather than the invoices. Without this pass a manager would have
    to close hundreds of rows by hand after a one-field fix.

    Re-checks from the stored snapshot, not by re-reading each invoice. Both
    ``Sales Invoice.territory`` and ``Sales Invoice.pos_profile`` are immutable
    once submitted, so the ONLY thing that can have changed is
    ``Territory.pos_profile`` -- one read per distinct territory instead of
    three or four per row. On production that is ~58 queries a night rather than
    ~4,000, which matters because the 1,260 historical rows are re-checked every
    time and none of them will ever clear.

    ``Territory Unresolved`` rows can never clear this way either (nothing about
    a submitted invoice's territory can change), so they are not even fetched.
    """
    summary: Dict[str, Any] = {"checked": 0, "closed": 0, "failed": 0}
    if not _exception_doctype_ready():
        return summary

    try:
        page = max(1, int(limit or DEFAULT_CLOSE_LIMIT))
    except Exception:
        page = DEFAULT_CLOSE_LIMIT

    try:
        rows = frappe.get_all(
            EXCEPTION_DOCTYPE,
            filters={"status": STATUS_OPEN, "exception_type": TYPE_BRANCH_MISMATCH},
            fields=["name", "sales_invoice", "invoice_territory", "pos_profile_used"],
            order_by="creation asc",
            limit_page_length=page,
        ) or []
    except Exception:
        _log_failure("auto-close fetch failed")
        return summary

    territory_profiles: Dict[str, Optional[str]] = {}

    for row in rows:
        summary["checked"] += 1
        try:
            territory = _clean(row.get("invoice_territory"))
            if not territory:
                continue
            if territory not in territory_profiles:
                territory_profiles[territory] = _territory_pos_profile(territory)

            still_bad = detect_exception_type(
                territory=territory,
                pos_profile=row.get("pos_profile_used"),
                territory_pos_profile=territory_profiles[territory],
            )
            if still_bad == TYPE_BRANCH_MISMATCH:
                continue

            doc = frappe.get_doc(EXCEPTION_DOCTYPE, row["name"])
            doc.status = STATUS_TERRITORY_CORRECTED
            doc.resolved_on = frappe.utils.now_datetime()
            doc.resolved_by = frappe.session.user
            doc.detail = (
                f"{doc.detail or ''}\n[auto-closed] The territory no longer "
                f"disagrees with the branch that shipped."
            ).strip()[:1000]
            doc.flags.ignore_links = True
            doc.save(ignore_permissions=True)
            summary["closed"] += 1
        except Exception:
            summary["failed"] += 1
            _log_failure(f"auto-close failed for {row.get('name')}")

    if summary["closed"]:
        try:
            frappe.db.commit()
        except Exception:
            _log_failure("auto-close commit failed")

    _logger().info(
        "territory_exceptions auto-close: checked=%s closed=%s failed=%s",
        summary["checked"],
        summary["closed"],
        summary["failed"],
    )
    return summary


def run_territory_exception_sweep(days: int = DEFAULT_SWEEP_DAYS) -> Dict[str, Any]:
    """Daily scheduler entry: catch recent stragglers, then auto-close what is fixed.

    Bounded on purpose. The ``on_submit`` hook is the primary catch; this exists
    so a window where that hook was failing does not go unrecorded, and so a
    corrected territory stops showing as an open exception without anyone
    clicking through hundreds of rows. Never raises -- a scheduler job that
    throws takes the rest of the daily slot with it.
    """
    result: Dict[str, Any] = {"backfill": None, "auto_close": None}

    try:
        window = abs(int(days or DEFAULT_SWEEP_DAYS))
        from_date = frappe.utils.add_days(frappe.utils.nowdate(), -window)
        result["backfill"] = _run_backfill(from_date=from_date)
    except Exception:
        _log_failure("sweep backfill failed")

    try:
        result["auto_close"] = close_resolved_territory_exceptions()
    except Exception:
        _log_failure("sweep auto-close failed")

    return result

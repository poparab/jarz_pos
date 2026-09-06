"""Detect WooCommerce orders stuck in ERPNext, without ever touching one.

**The problem.** ``jarz_pos.scripts.backlog_migration`` documents (and fixes,
under human supervision) a real defect: the POS ops pipeline stopped being
driven around 2026-06-06, and WooCommerce orders kept arriving, kept being
invoiced, kept being delivered in real life -- while the ERPNext side stayed
frozen in an early ops state with the cash never booked. That script found
roughly 13 orders and 12.7k EGP a day quietly joining the backlog, for months,
before anyone noticed.

**Why this module cannot fix anything.** ``backlog_migration.py`` explains it
best (read its module docstring): there is exactly one door from an ERPNext
state change to a real person -- the WooCommerce status PUT that fires on a
transition and would re-send a "your order is complete" email to a customer
whose order arrived months ago. That script refuses to run at all while
WooCommerce outbound is armed, and re-arming it is a deliberate human action.
A daily scheduled job is the opposite of supervised, so it must never be able
to open that door even by accident. This module therefore:

* never writes to a Sales Invoice, Payment Entry, Delivery Note, Courier
  Transaction, or any WooCommerce record;
* never calls anything in ``backlog_migration.py``;
* never reads or depends on the WooCommerce outbound switches;
* never calls the WooCommerce REST API, and never reads a hand-uploaded
  snapshot CSV -- unlike ``backlog_migration.classify()``, which trusts a
  human to keep ``woo_snapshot.csv`` fresh before every run and silently
  buckets anything newer than that file as "missing in Woo". That is fine for
  a supervised one-off; it is unfit for something that runs unattended every
  night.

**How detection works instead.** Purely from ERPNext's own state, which is
exactly the symptom the migration exists to clean up:

* *Woo-originated* -- ``Sales Invoice.woo_order_id`` is set and non-zero.
  Same test ``backlog_migration.classify()`` uses to group invoices by order.
* *Not finished* -- ``docstatus == 1`` (submitted, not cancelled) and the ops
  state (``custom_sales_invoice_state``, falling back to the legacy
  ``sales_invoice_state`` -- see ``jarz_pos.api.kanban``, the authoritative
  reader of these two fields) is not one of the TERMINAL_STATES below. Mirrors
  ``backlog_migration.TERMINAL_STATES`` / ``MID_STATES`` exactly, but the sets
  are duplicated here rather than imported: that script is a manual,
  supervised migration tool, this is an unattended daily job, and the two must
  never become import-coupled. Keep them in sync by eye if the board's state
  options ever change.
* *Well past when it should have finished* -- ``posting_date``/``posting_time``
  plus :data:`DEFAULT_STUCK_AFTER_HOURS`. This is deliberately simple rather
  than slot-aware: a scheduled next-day order can occasionally surface a few
  hours early, but the row costs a reviewer a five-second glance and a
  dismissal, while under-detecting an abandoned order is what let 13
  orders/day vanish for months. False positives are cheap here; false
  negatives are exactly the defect this exists to catch.

**The review queue.** ``Jarz Woo Backlog Exception`` -- a new, minimal
DocType, not a reuse of ``Jarz Territory Exception``: the two catch unrelated
symptoms (a branch/territory disagreement vs. an order that never left the
kanban board) and forcing one schema to describe both would mean either empty
fields on every row or a shared ``exception_type`` that means something
different each time it is read. A new table costs one migration and stays
legible on its own.

Idempotent per ``sales_invoice`` -- re-running the sweep the same day (or any
day) touches, at most, ``last_seen_on``/``ops_state``/the money snapshot on an
existing row; it never inserts a second one. An order that leaves its
non-terminal state is auto-closed the next sweep and stops appearing in the
open queue -- see :func:`close_resolved_backlog_exceptions`.

Public API::

    run_woo_backlog_sweep(limit=..., stuck_after_hours=...) -> dict   # scheduler entry
    close_resolved_backlog_exceptions(limit=...) -> dict
    is_terminal_state(value) -> bool
    is_stuck(...) -> bool                                             # pure, unit-testable
    build_snapshot(row, now=...) -> dict                               # pure, unit-testable
    build_detail(snapshot) -> str                                      # pure, unit-testable
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

import frappe

EXCEPTION_DOCTYPE = "Jarz Woo Backlog Exception"
INVOICE_DOCTYPE = "Sales Invoice"

#: Mirrors ``jarz_pos.scripts.backlog_migration.TERMINAL_STATES`` /
#: ``MID_STATES`` -- and, transitively, the authoritative state list read by
#: ``jarz_pos.api.kanban._get_state_field_options``. Duplicated on purpose; see
#: the module docstring for why these are never imported from the migration
#: script.
TERMINAL_STATES = frozenset({"Delivered", "Cancelled", "Returned"})
MID_STATES = frozenset({"Recieved", "Received", "In Progress", "Ready", "Out for Delivery"})

#: Kept in step with the ``status`` Select options in the DocType JSON.
STATUS_OPEN = "Open"
STATUS_RESOLVED = "Resolved"

#: How long a Woo order may sit in a non-terminal state before it counts as
#: "should have finished by now". One full calendar day: every branch's normal
#: cadence is same-day or next-day local delivery, so an order that has not
#: moved at all in 24h is off the normal cadence regardless of which slot it
#: was promised. See the module docstring for why this is deliberately
#: posting-time-based rather than delivery-slot-aware.
DEFAULT_STUCK_AFTER_HOURS = 24

#: Rows the candidate query may return in one sweep. Oldest-first ordering (see
#: ``_fetch_candidate_rows``) means a capped run still surfaces the worst of
#: the backlog rather than an arbitrary slice of it.
DEFAULT_SWEEP_LIMIT = 3000

#: Open rows re-checked for auto-close in one sweep.
DEFAULT_CLOSE_LIMIT = 3000


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _logger():
    """Module logger that is actually audible on staging and production.

    ``frappe.logger()`` inherits the site's default level (ERROR off a dev
    server) -- pin INFO here so the nightly summary reaches the log file. See
    ``services.territory_exceptions`` for the same fix and the same reason.
    """
    logger = frappe.logger("jarz_pos.woo_backlog_watch", allow_site=True)
    try:
        logger.setLevel(logging.INFO)
    except Exception:
        pass
    return logger


def _log_failure(title: str, message: Optional[str] = None) -> None:
    try:
        frappe.log_error(message or frappe.get_traceback(), f"woo_backlog_watch: {title}"[:140])
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(value: Any) -> str:
    return str(value or "").strip()


def _money(amount: Any, currency: Any = "") -> str:
    try:
        text = f"{float(amount or 0):,.2f}"
    except Exception:
        text = str(amount or 0)
    code = _clean(currency)
    return f"{code} {text}".strip()


def normalize_state(value: Any) -> str:
    return _clean(value)


def is_terminal_state(value: Any) -> bool:
    """True when *value* is one of the board's finished states.

    Anything else -- a MID_STATES value, a blank state, or a legacy/garbage
    value that matches neither list -- is treated as "not finished", mirroring
    ``backlog_migration``'s own framing: TERMINAL_STATES are the ones left
    alone, everything else is a candidate.
    """
    return normalize_state(value) in TERMINAL_STATES


# ─────────────────────────────────────────────────────────────────────────────
# Pure detection (no DB access, unit-testable without a bench)
# ─────────────────────────────────────────────────────────────────────────────

def _combine_date_time(date_val: Any, time_val: Any) -> Optional[datetime.datetime]:
    """Best-effort ``posting_date``/``posting_time`` -> ``datetime``.

    Handles every shape these two can actually arrive in: ``posting_date`` as a
    ``date``/``datetime`` (raw SQL) or an ISO string (a test double);
    ``posting_time`` as a ``datetime.timedelta`` -- MySQL's ``TIME`` column
    comes back as one via the DB driver, a documented trap in this codebase
    (see ``frappe-time-field-is-timedelta`` in project memory) -- a plain
    number of seconds, or an ``"HH:MM[:SS]"`` string.
    """
    if date_val is None:
        return None

    if isinstance(date_val, datetime.datetime):
        date_part = date_val.date()
    elif isinstance(date_val, datetime.date):
        date_part = date_val
    elif isinstance(date_val, str) and date_val.strip():
        try:
            date_part = datetime.date.fromisoformat(date_val.strip()[:10])
        except Exception:
            return None
    else:
        return None

    seconds = 0.0
    if isinstance(time_val, datetime.timedelta):
        seconds = time_val.total_seconds()
    elif isinstance(time_val, (int, float)):
        seconds = float(time_val)
    elif isinstance(time_val, str) and time_val.strip():
        parts = time_val.strip().split(":")
        try:
            numbers = [float(part) for part in parts]
        except Exception:
            numbers = []
        while len(numbers) < 3:
            numbers.append(0.0)
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]

    return datetime.datetime.combine(date_part, datetime.time()) + datetime.timedelta(seconds=seconds)


def expected_finish_by(
    posting_date: Any,
    posting_time: Any,
    *,
    stuck_after_hours: int = DEFAULT_STUCK_AFTER_HOURS,
) -> Optional[datetime.datetime]:
    """The timestamp past which an order that has not finished counts as stuck."""
    posting_dt = _combine_date_time(posting_date, posting_time)
    if posting_dt is None:
        return None
    return posting_dt + datetime.timedelta(hours=abs(stuck_after_hours))


def is_stuck(
    *,
    docstatus: Any,
    ops_state: Any,
    posting_date: Any,
    posting_time: Any,
    now: datetime.datetime,
    stuck_after_hours: int = DEFAULT_STUCK_AFTER_HOURS,
) -> bool:
    """The whole rule, in one pure function: submitted, not finished, overdue."""
    try:
        if int(docstatus or 0) != 1:
            return False
    except Exception:
        return False
    if is_terminal_state(ops_state):
        return False
    finish_by = expected_finish_by(posting_date, posting_time, stuck_after_hours=stuck_after_hours)
    if finish_by is None:
        return False
    return now > finish_by


def build_snapshot(row: Dict[str, Any], *, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Turn one raw candidate row into the payload the exception row stores.

    Pure -- no DB access, no frappe calls. ``row`` is a dict carrying the
    columns :func:`_fetch_candidate_rows` selects.
    """
    now = now or datetime.datetime.now()
    invoice_name = _clean(row.get("name") or row.get("sales_invoice"))
    branch = _clean(row.get("pos_profile")) or _clean(row.get("custom_kanban_profile")) or None
    finish_by = expected_finish_by(row.get("posting_date"), row.get("posting_time"))

    age_hours: Optional[float] = None
    if finish_by is not None:
        age_hours = round((now - finish_by).total_seconds() / 3600.0, 1)

    return {
        "sales_invoice": invoice_name or None,
        "woo_order_id": row.get("woo_order_id"),
        "customer": _clean(row.get("customer")) or None,
        "posting_date": row.get("posting_date"),
        "pos_profile": branch,
        "ops_state": normalize_state(row.get("ops_state")) or None,
        "expected_finish_by": finish_by,
        "age_hours": age_hours,
        "outstanding_amount": row.get("outstanding_amount") or 0,
        "grand_total": row.get("grand_total") or 0,
        "currency": _clean(row.get("currency")),
    }


def build_detail(snapshot: Dict[str, Any]) -> str:
    """Everything a reviewer needs to triage without opening the invoice."""
    invoice = snapshot.get("sales_invoice") or "?"
    woo_order_id = snapshot.get("woo_order_id") or "?"
    state = snapshot.get("ops_state") or "(blank)"
    branch = snapshot.get("pos_profile") or "(no branch)"
    amount = _money(snapshot.get("outstanding_amount"), snapshot.get("currency"))

    age_hours = snapshot.get("age_hours")
    if isinstance(age_hours, (int, float)) and age_hours > 0:
        age_text = f"{age_hours:.1f}h past its expected finish"
    else:
        age_text = "past its expected finish"

    return (
        f"Order {invoice} (Woo #{woo_order_id}) is still '{state}' at branch {branch}, "
        f"{age_text}. Outstanding: {amount}. This is a detection-only record -- "
        "WooCommerce outbound must stay disarmed while a fix is applied by hand; "
        "see jarz_pos.scripts.backlog_migration."
    )[:1000]


# ─────────────────────────────────────────────────────────────────────────────
# DB access
# ─────────────────────────────────────────────────────────────────────────────

def _exception_doctype_ready() -> bool:
    """False before this DocType's first ``bench migrate`` on a site."""
    try:
        return bool(frappe.db.table_exists(EXCEPTION_DOCTYPE))
    except Exception:
        return False


def _terminal_state_sql_list() -> str:
    """A safe, literal SQL ``IN`` list. ``TERMINAL_STATES`` is an internal
    constant, never user input, so inlining it (rather than a bound parameter)
    is safe and keeps the query text readable.
    """
    return ", ".join("'{0}'".format(state.replace("'", "''")) for state in sorted(TERMINAL_STATES))


def _state_expr() -> str:
    """The ops-state SQL expression, guarding the legacy column's existence.

    Mirrors ``jarz_pos.api.kanban``'s own fallback: prefer
    ``custom_sales_invoice_state``, fall back to the legacy
    ``sales_invoice_state`` when that column still exists on this site.
    """
    try:
        legacy_exists = bool(frappe.db.has_column(INVOICE_DOCTYPE, "sales_invoice_state"))
    except Exception:
        legacy_exists = False
    if legacy_exists:
        return "COALESCE(NULLIF(custom_sales_invoice_state, ''), sales_invoice_state, '')"
    return "COALESCE(custom_sales_invoice_state, '')"


def _fetch_candidate_rows(cutoff: Any, limit: int) -> List[Dict[str, Any]]:
    """Woo-linked, submitted, non-terminal invoices posted before *cutoff*.

    Raw SQL, mirroring the precedent already set by
    ``backlog_migration.preflight``/``classify`` for this exact table: a
    ``COALESCE``/``NULLIF`` state expression and an ``IN`` list over a fixed
    constant are far plainer as SQL text than as ``frappe.get_all`` filters.
    Bounded by ``LIMIT`` and ordered oldest-first, so a capped run always
    surfaces the longest-stuck orders rather than an arbitrary slice.
    """
    query = f"""
        SELECT name, woo_order_id, customer, posting_date, posting_time,
               {_state_expr()} AS ops_state,
               pos_profile, custom_kanban_profile,
               outstanding_amount, grand_total, currency
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND woo_order_id IS NOT NULL AND woo_order_id <> 0
          AND {_state_expr()} NOT IN ({_terminal_state_sql_list()})
          AND TIMESTAMP(posting_date, posting_time) <= %(cutoff)s
        ORDER BY posting_date ASC, posting_time ASC
        LIMIT %(limit)s
    """
    try:
        return frappe.db.sql(
            query,
            {"cutoff": cutoff, "limit": int(limit)},
            as_dict=True,
        ) or []
    except Exception:
        _log_failure("candidate fetch failed")
        return []


def _existing_exception(invoice_name: str) -> Optional[str]:
    try:
        found = frappe.db.exists(EXCEPTION_DOCTYPE, {"sales_invoice": invoice_name})
    except Exception:
        return None
    return found or None


def _insert_exception(snapshot: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """Create the row, or return the one that is already there.

    Returns ``(name, created)``. Fenced behind a savepoint the same way
    ``services.territory_exceptions`` does, so a write failure here can never
    poison a caller's transaction -- though this is only ever called from the
    scheduler's own, otherwise-empty transaction.
    """
    invoice_name = snapshot.get("sales_invoice")
    if not invoice_name:
        return None, False

    existing = _existing_exception(invoice_name)
    if existing:
        return existing, False

    savepoint = "jarz_woo_backlog_exception"
    try:
        frappe.db.savepoint(savepoint)
    except Exception:
        savepoint = ""

    try:
        now = frappe.utils.now_datetime()
        doc = frappe.get_doc(
            {
                "doctype": EXCEPTION_DOCTYPE,
                "sales_invoice": invoice_name,
                "woo_order_id": snapshot.get("woo_order_id"),
                "customer": snapshot.get("customer"),
                "posting_date": snapshot.get("posting_date"),
                "pos_profile": snapshot.get("pos_profile"),
                "ops_state": snapshot.get("ops_state"),
                "expected_finish_by": snapshot.get("expected_finish_by"),
                "outstanding_amount": snapshot.get("outstanding_amount"),
                "grand_total": snapshot.get("grand_total"),
                "currency": snapshot.get("currency"),
                "status": STATUS_OPEN,
                "first_detected_on": now,
                "last_seen_on": now,
                "detail": build_detail(snapshot),
            }
        )
        # A dangling customer/POS Profile on an old order must not stop the row
        # being filed -- surfacing the crossing is the whole point.
        doc.flags.ignore_links = True
        doc.insert(ignore_permissions=True)
        return doc.name, True
    except Exception:
        if savepoint:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
        existing = _existing_exception(invoice_name)
        if existing:
            return existing, False
        _log_failure(f"insert failed for {invoice_name}")
        return None, False


# ─────────────────────────────────────────────────────────────────────────────
# Auto-close
# ─────────────────────────────────────────────────────────────────────────────

def close_resolved_backlog_exceptions(limit: int = DEFAULT_CLOSE_LIMIT) -> Dict[str, Any]:
    """Auto-close Open rows whose invoice has since left its non-terminal state.

    Unlike ``territory_exceptions`` (where the invoice's own territory/branch
    are immutable once submitted, so only the *Territory* record needs
    re-checking), the ops state here is exactly what is expected to change --
    that is the whole point of the order becoming unstuck -- so this re-reads
    each open row's Sales Invoice. That is a READ only: docstatus, the two
    state columns, and the current money totals, so the queue's amounts stay
    live without a second write path. Bounded by ``limit`` so a large open
    queue cannot make one sweep run indefinitely.
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
            filters={"status": STATUS_OPEN},
            fields=["name", "sales_invoice"],
            order_by="creation asc",
            limit_page_length=page,
        ) or []
    except Exception:
        _log_failure("close fetch failed")
        return summary

    now = frappe.utils.now_datetime()

    for row in rows:
        summary["checked"] += 1
        try:
            invoice = frappe.db.get_value(
                INVOICE_DOCTYPE,
                row.get("sales_invoice"),
                [
                    "docstatus",
                    "custom_sales_invoice_state",
                    "sales_invoice_state",
                    "outstanding_amount",
                    "grand_total",
                ],
                as_dict=True,
            )

            if not invoice:
                # Renamed/deleted -- nothing left on the books to track.
                still_stuck = False
                state = None
            else:
                state = invoice.get("custom_sales_invoice_state") or invoice.get("sales_invoice_state")
                still_stuck = int(invoice.get("docstatus") or 0) == 1 and not is_terminal_state(state)

            if still_stuck:
                frappe.db.set_value(
                    EXCEPTION_DOCTYPE,
                    row.get("name"),
                    {
                        "ops_state": normalize_state(state),
                        "outstanding_amount": invoice.get("outstanding_amount"),
                        "grand_total": invoice.get("grand_total"),
                        "last_seen_on": now,
                    },
                    update_modified=False,
                )
                continue

            doc = frappe.get_doc(EXCEPTION_DOCTYPE, row.get("name"))
            doc.status = STATUS_RESOLVED
            doc.resolved_on = now
            doc.resolved_by = frappe.session.user
            doc.detail = (
                f"{doc.detail or ''}\n[auto-closed] The order left its non-terminal ops state."
            ).strip()[:1000]
            doc.flags.ignore_links = True
            doc.save(ignore_permissions=True)
            summary["closed"] += 1
        except Exception:
            summary["failed"] += 1
            _log_failure(f"close failed for {row.get('name')}")

    if summary["closed"] or summary["checked"]:
        try:
            frappe.db.commit()
        except Exception:
            _log_failure("close commit failed")

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler entry
# ─────────────────────────────────────────────────────────────────────────────

def run_woo_backlog_sweep(
    limit: int = DEFAULT_SWEEP_LIMIT,
    stuck_after_hours: int = DEFAULT_STUCK_AFTER_HOURS,
) -> Dict[str, Any]:
    """Daily scheduler entry. Never raises -- a scheduler job that throws takes
    the rest of the daily slot with it, the same discipline every sibling job
    in ``hooks.py`` follows.

    Bounded: the candidate query carries its own ``LIMIT`` (oldest-first, so a
    capped run still surfaces the worst of the backlog), and the
    reconciliation pass is separately capped by :data:`DEFAULT_CLOSE_LIMIT`.
    Detection-only: reads Sales Invoice, writes ONLY to
    ``Jarz Woo Backlog Exception``.
    """
    summary: Dict[str, Any] = {
        "scanned": 0,
        "created": 0,
        "already_open": 0,
        "failed": 0,
        "closed": 0,
        "checked_for_close": 0,
    }

    try:
        if not _exception_doctype_ready():
            summary["skipped"] = "doctype not migrated yet"
            return summary

        close_summary = close_resolved_backlog_exceptions()
        summary["closed"] = close_summary.get("closed", 0)
        summary["checked_for_close"] = close_summary.get("checked", 0)

        page = max(1, min(int(limit or DEFAULT_SWEEP_LIMIT), 20000))
        hours = abs(int(stuck_after_hours or DEFAULT_STUCK_AFTER_HOURS))
        now = frappe.utils.now_datetime()
        cutoff = frappe.utils.add_to_date(now, hours=-hours)

        rows = _fetch_candidate_rows(cutoff, page)

        for row in rows:
            summary["scanned"] += 1
            try:
                snapshot = build_snapshot(row, now=now)
                if not snapshot.get("sales_invoice"):
                    summary["failed"] += 1
                    continue
                name, created = _insert_exception(snapshot)
                if name and created:
                    summary["created"] += 1
                elif name:
                    summary["already_open"] += 1
                else:
                    summary["failed"] += 1
            except Exception:
                summary["failed"] += 1
                _log_failure(f"sweep row failed for {row.get('name')}")

        try:
            frappe.db.commit()
        except Exception:
            _log_failure("sweep commit failed")

        _logger().info(
            "woo_backlog_watch sweep: scanned=%s created=%s already_open=%s failed=%s "
            "closed=%s checked_for_close=%s cutoff=%s",
            summary["scanned"],
            summary["created"],
            summary["already_open"],
            summary["failed"],
            summary["closed"],
            summary["checked_for_close"],
            cutoff,
        )
    except Exception:
        _log_failure("sweep failed entirely")

    return summary

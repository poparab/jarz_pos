"""Settlement terms agreed with a Lead, and their hand-over to the Customer.

The rep records the agreed payment terms on the B2B account screen during the
deal, before the first order -- when the account is still a Lead. A Lead cannot
hold a ``Jarz Settlement Terms`` record (named after its Customer), so the terms
live as JSON on ``Lead.custom_settlement_terms`` (seeded by
``setup/settlement_terms_leads``), in exactly the normalised shape
``settlement_schedule.normalize_terms_input`` produces.

CARRY-OVER
----------
When the Lead becomes a Customer the JSON is copied into a real ``Jarz
Settlement Terms`` record, from which point reminders and the collections list
see it. Entry points, all funnelled through :func:`safe_carry_over`:

* ``Customer`` ``after_insert`` (``hooks.doc_events``): every conversion that
  CREATES the Customer with ``lead_name`` set -- ``api/customer.create_customer``
  with ``source_lead`` (``crm.request_sample`` / ``crm.place_b2b_order``), and
  ERPNext's own Lead -> Customer ``make_customer`` in Desk.
* ``api/crm.link_existing_customer``: a Lead linked to an EXISTING Customer via
  ``Lead.customer`` (no insert happens, so the doc event never fires).
* lazily in ``api/settlement_terms`` -- ``get_settlement_terms`` and
  ``save_settlement_terms`` on a Customer that has no terms record yet (the app
  sends ``customer=`` for a converted lead), and ``get_settlement_terms(lead=)``
  on a converted lead. Covers everything else (the B2B branch merge, a hand
  edit in Desk, a carry-over that failed or raced a save).

CONCURRENCY. MariaDB runs REPEATABLE READ: taking a row lock does NOT refresh
the transaction's snapshot, so a plain read after ``FOR UPDATE`` can still see
the pre-lock value. Every read here that decides something after the Lead row is
locked is itself a locking read (``for_update=True``), and the stamp written
after a carry is built from that locked read -- never from an older dict -- so
it cannot overwrite newer terms a concurrent save committed.

Rules: never raises (except a deadlock / lock-wait timeout, which has already
rolled back the whole transaction and must reach the caller), never blocks the
customer creation otherwise (savepoint-fenced, any queued ``frappe.throw``
message withdrawn), only creates when the Customer has no terms yet, and never
twice for the same (Lead, Customer) -- once copied, the Lead JSON is stamped
``carried_to_customer`` so deleting the customer's terms later is not undone by
the lazy fallback. The JSON itself is kept (history).
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

import frappe
from frappe.exceptions import QueryDeadlockError, QueryTimeoutError

from jarz_pos.services import settlement_schedule as ss

TERMS_DOCTYPE = "Jarz Settlement Terms"

#: Mirrored by ``setup/settlement_terms_leads.LEAD_FIELD``.
LEAD_FIELD = "custom_settlement_terms"

#: The normalised terms keys stored in the Lead JSON, in
#: ``normalize_terms_input``'s vocabulary.
STORED_KEYS = (
    "cycle",
    "enabled",
    "weekdays",
    "week_interval",
    "month_days",
    "interval_days",
    "anchor_date",
    "remind_days_before",
    "overdue_repeat_days",
    "responsible_user",
    "notes",
)

#: Bookkeeping keys written next to the terms (never part of the terms).
CARRIED_TO_KEY = "carried_to_customer"
CARRIED_AT_KEY = "carried_at"
CARRY_OUTCOME_KEY = "carry_outcome"

#: :func:`carry_over_lead_terms` outcomes; both WROTE something.
CARRY_CREATED = "created"
CARRY_SUPERSEDED = "superseded"

#: Errors that have already rolled back the WHOLE transaction (the Customer
#: insert included). Swallowing one would let the caller carry on as if its
#: earlier writes still existed, so :func:`safe_carry_over` re-raises them.
FATAL_DB_ERRORS = (QueryDeadlockError, QueryTimeoutError)

_HOOK_SAVEPOINT = "jarz_settlement_lead_carry"


def _now_text() -> str:
    """Site-local "now" as text; the machine clock if the site's is unavailable."""
    try:
        from frappe.utils import now

        return str(now())
    except Exception:
        return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")


def _today_date() -> datetime.date:
    try:
        from frappe.utils import getdate, nowdate

        return getdate(nowdate())
    except Exception:
        return datetime.date.today()


# ---------------------------------------------------------------------------
# Lead JSON
# ---------------------------------------------------------------------------


def lead_field_ready() -> bool:
    """False until ``bench migrate`` has created the Lead column on this site."""
    try:
        return bool(frappe.db.has_column("Lead", LEAD_FIELD))
    except Exception:
        return False


def decode_lead_terms(raw: Any) -> Optional[Dict[str, Any]]:
    """Stored column value -> dict, or ``None`` when empty / unreadable / cycle-less."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict) or not data.get("cycle"):
        return None
    return data


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def encode_lead_terms(values: Dict[str, Any], user: Optional[str] = None, modified: Optional[str] = None) -> str:
    """A ``normalize_terms_input`` result -> the JSON text stored on the Lead."""
    data: Dict[str, Any] = {key: _json_value(values.get(key)) for key in STORED_KEYS}
    data["modified"] = modified
    data["modified_by"] = user
    return json.dumps(data, sort_keys=True, default=str)


def lock_lead(lead: str) -> None:
    """``SELECT ... FOR UPDATE`` on the Lead row (the lock the conversion path takes)."""
    frappe.db.sql("SELECT name FROM `tabLead` WHERE name = %s FOR UPDATE", (lead,))


def read_lead_terms(lead: str, for_update: bool = False) -> Optional[Dict[str, Any]]:
    """The terms stored on *lead*, or ``None``.

    ``for_update=True`` is a LOCKING read -- required after the Lead row is
    locked, because under REPEATABLE READ a plain read still returns the
    snapshot taken before the lock. A failed plain read degrades to ``None``;
    a failed locking read raises (the caller is deciding a write on it).
    """
    if not lead or not lead_field_ready():
        return None
    if for_update:
        raw = frappe.db.get_value("Lead", lead, LEAD_FIELD, for_update=True)
    else:
        try:
            raw = frappe.db.get_value("Lead", lead, LEAD_FIELD)
        except Exception:
            return None
    return decode_lead_terms(raw)


def _write_lead_column(lead: str, text: Optional[str]) -> None:
    # update_modified=True on purpose: a Desk user holding the Lead form open
    # then gets Frappe's normal "modified after you opened it" refusal instead
    # of silently saving the stale (read-only) JSON back over these terms.
    frappe.db.set_value("Lead", lead, LEAD_FIELD, text, update_modified=True)


def write_lead_terms(lead: str, values: Optional[Dict[str, Any]]) -> None:
    """Store (or clear, with ``None``) the terms on *lead*.

    ``db.set_value`` on purpose: this is not a Lead edit, so it must not run the
    Lead's own validation. ``modified`` IS bumped (see ``_write_lead_column``).
    """
    text = None
    if values is not None:
        text = encode_lead_terms(
            values,
            user=str(getattr(frappe.session, "user", "") or "") or None,
            modified=_now_text(),
        )
    _write_lead_column(lead, text)


def _terms_doctype_ready() -> bool:
    try:
        return bool(frappe.db.exists("DocType", TERMS_DOCTYPE))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Carry-over
# ---------------------------------------------------------------------------


def _stamp_carried(lead: str, stored: Dict[str, Any], customer: str, outcome: str) -> None:
    """Mark *stored* (a dict read FOR UPDATE under the Lead lock) as carried."""
    stamped = dict(stored)
    stamped[CARRIED_TO_KEY] = customer
    stamped[CARRIED_AT_KEY] = _now_text()
    stamped[CARRY_OUTCOME_KEY] = outcome
    _write_lead_column(lead, json.dumps(stamped, sort_keys=True, default=str))


def carry_over_lead_terms(lead: str, customer: str) -> Optional[str]:
    """Copy *lead*'s stored terms onto *customer*.

    Returns :data:`CARRY_CREATED` when a ``Jarz Settlement Terms`` record was
    made; :data:`CARRY_SUPERSEDED` when the Customer already had terms (theirs
    win -- they are newer and manager-visible -- and the Lead JSON is stamped so
    deleting the Customer's terms later does not resurrect it); ``None`` when
    nothing was to do (nothing stored, or already carried to this Customer).

    Locks the Lead row and re-reads the JSON FOR UPDATE before deciding, so the
    copy and the stamp are both made from the latest committed terms.
    May raise -- use :func:`safe_carry_over`.
    """
    lead = str(lead or "").strip()
    customer = str(customer or "").strip()
    if not lead or not customer or not _terms_doctype_ready() or not lead_field_ready():
        return None
    # Cheap pre-check without a lock: nothing stored, nothing to lock for.
    if not read_lead_terms(lead):
        return None

    lock_lead(lead)
    stored = read_lead_terms(lead, for_update=True)
    if not stored:
        return None
    if str(stored.get(CARRIED_TO_KEY) or "") == customer:
        return None
    if frappe.db.exists(TERMS_DOCTYPE, {"customer": customer}):
        _stamp_carried(lead, stored, customer, CARRY_SUPERSEDED)
        return CARRY_SUPERSEDED

    # Re-validated through the one strict gate: the JSON came from it, but a
    # hand edit in the database must not become a record the reminder pass
    # chokes on.
    values = ss.normalize_terms_input({key: stored.get(key) for key in STORED_KEYS})
    if values["anchor_date"] is None and ss.needs_anchor(values):
        values["anchor_date"] = _today_date()

    doc = frappe.new_doc(TERMS_DOCTYPE)
    doc.customer = customer
    for field, value in values.items():
        doc.set(field, value)
    doc.insert(ignore_permissions=True)

    # History stays; the stamp stops the lazy fallback from resurrecting terms
    # somebody later deletes on the Customer.
    _stamp_carried(lead, stored, customer, CARRY_CREATED)

    try:
        from jarz_pos.api.settlement_terms import invalidate_collections_count_cache

        invalidate_collections_count_cache()
    except Exception:
        pass
    return CARRY_CREATED


def leads_of_customer(customer: str) -> List[str]:
    """Every Lead tied to *customer* either way round (``Customer.lead_name`` /
    ``Lead.customer``). Never raises."""
    names: List[str] = []
    try:
        direct = frappe.db.get_value("Customer", customer, "lead_name")
        if direct:
            names.append(str(direct))
    except Exception:
        pass
    try:
        for name in frappe.get_all("Lead", filters={"customer": customer}, pluck="name", limit_page_length=20) or []:
            if name and str(name) not in names:
                names.append(str(name))
    except Exception:
        pass
    return names


def mark_leads_superseded(customer: str) -> int:
    """Stamp every un-carried Lead JSON of *customer* so the lazy carry-over
    cannot bring deleted terms back. Returns how many were stamped.

    Never raises, except a deadlock / lock-wait timeout (see ``FATAL_DB_ERRORS``).
    """
    count = 0
    if not customer or not lead_field_ready():
        return 0
    for lead in leads_of_customer(customer):
        try:
            if not read_lead_terms(lead):
                continue
            lock_lead(lead)
            stored = read_lead_terms(lead, for_update=True)
            if stored and str(stored.get(CARRIED_TO_KEY) or "") != customer:
                _stamp_carried(lead, stored, customer, CARRY_SUPERSEDED)
                count += 1
        except FATAL_DB_ERRORS:
            raise
        except Exception:
            try:
                frappe.log_error(frappe.get_traceback(), f"settlement_terms_lead_stamp_failed:{lead}"[:140])
            except Exception:
                pass
    return count


def _message_log_length() -> Optional[int]:
    try:
        message_log = getattr(frappe.local, "message_log", None)
        return len(message_log) if isinstance(message_log, list) else None
    except Exception:
        return None


def _withdraw_messages_since(count: Optional[int]) -> None:
    if count is None:
        return
    try:
        del frappe.local.message_log[count:]
    except Exception:
        pass


def safe_carry_over(lead: str, customer: str, defer_log: bool = False) -> Optional[str]:
    """:func:`carry_over_lead_terms`, fenced.

    Returns the outcome (truthy when something was written, so a GET caller
    knows to commit) or ``None``. A savepoint undoes a half-written record, and
    any message ``frappe.throw`` queued for the browser is withdrawn, so a
    failure here cannot surface as a red dialog on a Customer (or order) that
    was created fine. Failures go to the Error Log -- with ``defer_log=True``
    (the GET path) through Frappe's deferred insert, so the log survives the
    rollback that ends every GET request.

    RE-RAISES a deadlock or lock-wait timeout: by then the database has rolled
    back the whole transaction, Customer insert included, and pretending
    otherwise would let the caller commit half a conversion.
    """
    try:
        if not lead or not customer:
            return None
    except Exception:
        return None

    messages_before = _message_log_length()
    savepoint = _HOOK_SAVEPOINT
    try:
        frappe.db.savepoint(savepoint)
    except Exception:
        savepoint = ""

    try:
        return carry_over_lead_terms(lead, customer)
    except FATAL_DB_ERRORS:
        raise
    except Exception:
        if savepoint:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
        _withdraw_messages_since(messages_before)
        try:
            frappe.log_error(
                title=f"settlement_terms_lead_carry_failed:{lead}->{customer}"[:140],
                message=frappe.get_traceback(),
                defer_insert=bool(defer_log),
            )
        except Exception:
            pass
        return None


def on_customer_after_insert(doc, method=None) -> None:
    """``Customer`` ``after_insert``: carry the source Lead's agreed terms.

    Never raises, except a deadlock / lock-wait timeout (already fatal to the
    Customer insert). Fast exit, with zero queries, for every Customer without
    ``lead_name`` -- nearly all of them (POS walk-ins, the WooCommerce bulk sync).
    """
    try:
        lead = str(getattr(doc, "lead_name", None) or "").strip()
        customer = str(getattr(doc, "name", None) or "").strip()
    except Exception:
        return
    if not lead or not customer:
        return
    safe_carry_over(lead, customer)

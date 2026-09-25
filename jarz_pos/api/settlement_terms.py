"""B2B settlement terms: when each shop is expected to pay its credit balance.

Endpoints (all ``allow_guest=False``):

* :func:`get_settlement_terms` -- one customer's (or lead's) terms + where they stand.
* :func:`save_settlement_terms` -- upsert a customer's (or lead's) terms.
* :func:`delete_settlement_terms` -- drop a customer's (or lead's) terms (back to "unscheduled").
* :func:`get_collections_due` -- every customer with something to collect.

REMINDERS, NOT ENFORCEMENT. Nothing here blocks an order; the owner asked for a
schedule, reminders and a list (2026-09-25). The rules live in the pure
``services/settlement_schedule``; this module reads rows and shapes payloads.

Credit invoices are identified EXACTLY as ``api/credit`` identifies them, by
reusing its ``_open_credit_invoices`` (the OR over the mutable payment method
and the frozen ``custom_credit_terms_days`` stamp -- see ``utils/credit_utils``).
Keying on ``custom_payment_method`` alone would lose every debt a manager has
relabelled with "Change collection method".

Access mirrors the credit screens: reading is the credit-ledger gate, writing
is the credit-payment gate (``ROLES.ADMIN | ROLES.LINE_MANAGER_TIER``).

B2B SALES REP (feature settlement-terms-leads, owner decision 2026-09-26): the
rep negotiates the deal, so a holder of ``B2B Sales Rep`` may READ and SAVE
terms -- but only for (a) Leads and (b) Customers whose ``customer_type`` is
``Company``. For any other Customer the rep meets the original gates (i.e. is
refused). A rep may NOT delete terms: ``delete_settlement_terms`` keeps the
original manager write gate. ``can_edit`` is computed per party the same way.
On top of the role gate, a Lead needs Lead WRITE permission to save or delete
terms (for everyone) and Lead READ permission to read them.
``get_collections_due`` and the approvals badge keep the manager gates.
See ``_admit`` / ``_check_rep_party`` / ``_can_manage_terms``.

PARTIES (additive, old clients unchanged). ``get/save/delete_settlement_terms``
take exactly one of ``customer`` / ``lead``:

* ``customer`` -- the original behaviour, byte for byte, plus
  ``party_type: "Customer"`` and ``party: <customer>`` in the response.
* ``lead`` already converted (``crm._resolve_lead_customer``; a locking
  re-resolve for writes) -- acts on that Customer; same response, plus
  ``lead: <lead>``.

Whenever a Customer is read or saved and has NO terms record yet, any terms
still sitting on its Lead(s) are carried over first (the app sends
``customer=`` for a converted lead, so this is the path that actually runs;
``services/settlement_lead_terms``). A GET that carried flags the request for
commit.
* ``lead`` not converted -- the terms live as JSON on
  ``Lead.custom_settlement_terms``, validated by the SAME
  ``normalize_terms_input``. Response: same shape with ``party_type: "Lead"``,
  ``party``/``lead``: the lead, ``customer: null``, and ``status`` computed with
  no invoices (state ``none``, zeros, ``upcoming_dates`` from today).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

from jarz_pos.api.credit import (
    _allowed_profiles,
    _credit_currency,
    _ensure_credit_ledger_access,
    _ensure_credit_payment_access,
    _log_credit_error,
    _open_credit_invoices,
)
from jarz_pos.api.crm import _lock_relationship_row, _resolve_lead_customer
from jarz_pos.constants import ROLES
from jarz_pos.services import settlement_lead_terms as slt
from jarz_pos.services import settlement_schedule as ss

TERMS_DOCTYPE = "Jarz Settlement Terms"

#: The B2B field-sales role (``setup/b2b_master_data``, ``api/crm``). Holders
#: may read and write settlement terms -- see the module docstring.
B2B_SALES_REP_ROLE = "B2B Sales Rep"

PARTY_CUSTOMER = "Customer"
PARTY_LEAD = "Lead"

_TERMS_FIELDS = [
    "name",
    "customer",
    "customer_name",
    "enabled",
    "cycle",
    "weekdays",
    "week_interval",
    "month_days",
    "interval_days",
    "anchor_date",
    "remind_days_before",
    "overdue_repeat_days",
    "responsible_user",
    "notes",
    "last_reminder_on",
    "last_reminder_kind",
    "creation",
    "modified",
]

#: Collections "due soon" look-ahead bounds (days).
_DAYS_AHEAD_DEFAULT = 7
_DAYS_AHEAD_MAX = 90

#: The approvals badge is polled every minute by every manager; the count
#: walks every open credit invoice, so it is cached per user this long.
_COUNT_CACHE_PREFIX = "jarz_pos:credit_collections_count:"
_COUNT_CACHE_SECONDS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today():
    return getdate(nowdate())


def doctype_ready() -> bool:
    """False until ``bench migrate`` has created the DocType on this site."""
    try:
        return bool(frappe.db.exists("DocType", TERMS_DOCTYPE))
    except Exception:
        return False


def _session_roles() -> set:
    try:
        return {str(r or "").strip() for r in (frappe.get_roles() or []) if str(r or "").strip()}
    except Exception:
        return set()


#: Roles the original credit gates admit (ledger read and payment write are
#: the same set today).
_MANAGER_TERMS_ROLES = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER


def _is_b2b_rep() -> bool:
    """True when the caller holds the ``B2B Sales Rep`` role. Never raises."""
    return B2B_SALES_REP_ROLE in _session_roles()


def _is_company_customer(customer: Optional[str]) -> bool:
    if not customer:
        return False
    try:
        return str(frappe.db.get_value("Customer", customer, "customer_type") or "") == "Company"
    except Exception:
        return False


def _has_lead_permission(lead: str, ptype: str) -> bool:
    try:
        return bool(frappe.has_permission("Lead", ptype, doc=lead))
    except Exception:
        return False


def _admit(write: bool) -> bool:
    """The endpoint-level gate for get / save.

    A caller holding a manager role meets the ORIGINAL gate (credit ledger for
    reads, credit payment for writes), which raises when refused. A caller who
    is ONLY a B2B Sales Rep is admitted provisionally and this returns ``True``:
    the endpoint must then run :func:`_check_rep_party` once it knows the party.
    """
    if _is_b2b_rep() and not _session_roles().intersection(_MANAGER_TERMS_ROLES):
        return True
    if write:
        _ensure_credit_payment_access()
    else:
        _ensure_credit_ledger_access()
    return False


def _check_rep_party(rep_only: bool, customer: Optional[str] = None) -> None:
    """A rep-only caller may act on Leads and on Company customers, nothing else."""
    if rep_only and customer and not _is_company_customer(customer):
        frappe.throw(
            _("B2B sales reps may only manage payment terms of leads and company customers."),
            frappe.PermissionError,
        )


def _can_manage_terms(customer: Optional[str] = None, lead: Optional[str] = None) -> bool:
    """May the caller SAVE terms for this party? (``can_edit`` on the wire.)

    Managers: any Customer. Reps: Company customers only. A Lead party
    additionally needs Lead write permission, for everyone.
    """
    roles = _session_roles()
    manager = bool(roles.intersection(_MANAGER_TERMS_ROLES))
    rep = B2B_SALES_REP_ROLE in roles
    if not (manager or rep):
        return False
    if lead:
        return _has_lead_permission(lead, "write")
    if manager:
        return True
    return _is_company_customer(customer)


def _can_edit(customer: Optional[str] = None, lead: Optional[str] = None) -> bool:
    """``can_edit`` on the wire. Kept by name (tests and callers patch it)."""
    return _can_manage_terms(customer=customer, lead=lead)


def _load_terms_row(customer: str) -> Optional[Dict[str, Any]]:
    if not doctype_ready():
        return None
    rows = frappe.get_all(
        TERMS_DOCTYPE,
        filters={"customer": customer},
        fields=_TERMS_FIELDS,
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _load_terms_rows(customers: List[str]) -> Dict[str, Dict[str, Any]]:
    if not customers or not doctype_ready():
        return {}
    try:
        rows = frappe.get_all(
            TERMS_DOCTYPE,
            filters={"customer": ["in", customers]},
            fields=_TERMS_FIELDS,
            limit_page_length=0,
        )
    except Exception:
        _log_credit_error("Settlement terms: terms query failed")
        return {}
    return {str(r.get("customer")): r for r in rows or [] if r.get("customer")}


def _invoice_inputs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "name": r.get("name"),
            "posting_date": r.get("posting_date"),
            "outstanding_amount": r.get("outstanding_amount"),
        }
        for r in rows or []
    ]


def _user_full_name(user: Optional[str]) -> Optional[str]:
    if not user:
        return None
    try:
        return frappe.db.get_value("User", user, "full_name") or user
    except Exception:
        return user


def _serialize_terms(row: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    month_days = [str(d) for d in parsed.get("month_days") or []]
    if parsed.get("month_last"):
        month_days.append(ss.MONTH_LAST)
    anchor = row.get("anchor_date")
    return {
        "name": row.get("name"),
        "customer": row.get("customer"),
        "customer_name": row.get("customer_name") or row.get("customer"),
        "enabled": bool(parsed.get("enabled")),
        "cycle": parsed.get("cycle"),
        "weekdays": [ss.WEEKDAY_CODES[d] for d in parsed.get("weekdays") or []],
        "week_interval": int(parsed.get("week_interval") or 1),
        "month_days": month_days,
        "interval_days": parsed.get("interval_days"),
        "anchor_date": str(anchor) if anchor else None,
        "remind_days_before": int(parsed.get("remind_days_before") or 0),
        "overdue_repeat_days": int(parsed.get("overdue_repeat_days") or 1),
        "responsible_user": parsed.get("responsible_user"),
        "responsible_user_name": _user_full_name(parsed.get("responsible_user")),
        "notes": parsed.get("notes"),
        "last_reminder_on": str(row.get("last_reminder_on")) if row.get("last_reminder_on") else None,
        "last_reminder_kind": row.get("last_reminder_kind") or None,
        "modified": str(row.get("modified")) if row.get("modified") else None,
        "exists": True,
    }


def _terms_payload(customer: str) -> Dict[str, Any]:
    row = _load_terms_row(customer)
    parsed = ss.parse_terms(row) if row else None
    # All branches, deliberately -- the same scope get_customer_credit_profile
    # uses. What a shop owes is one debt, not one per branch.
    open_rows = _open_credit_invoices(customers=[customer])
    status = ss.compute_status(parsed, _invoice_inputs(open_rows), _today())
    customer_name = (row or {}).get("customer_name") or (
        frappe.db.get_value("Customer", customer, "customer_name") or customer
    )
    return {
        "success": True,
        "party_type": PARTY_CUSTOMER,
        "party": customer,
        "customer": customer,
        "customer_name": customer_name,
        "terms": _serialize_terms(row, parsed) if row else None,
        "description": ss.describe(parsed) if parsed else None,
        "description_ar": ss.describe(parsed, "ar") if parsed else None,
        "status": status,
        "currency": _credit_currency(),
        "can_edit": _can_edit(customer=customer),
    }


def _require_customer(customer: Any) -> str:
    name = str(customer or "").strip()
    if not name:
        frappe.throw(_("customer is required"))
    if not frappe.db.exists("Customer", name):
        frappe.throw(_("Customer {0} was not found").format(name))
    frappe.has_permission("Customer", "read", doc=name, throw=True)
    return name


# ---------------------------------------------------------------------------
# Lead party
# ---------------------------------------------------------------------------


def _party_args(customer: Any, lead: Any) -> tuple:
    """``(customer, lead)`` stripped; exactly one must be given. No DB work."""
    customer_name = str(customer or "").strip()
    lead_name = str(lead or "").strip()
    if customer_name and lead_name:
        frappe.throw(_("Pass either customer or lead, not both"))
    if not customer_name and not lead_name:
        frappe.throw(_("customer is required"))
    return customer_name, lead_name


def _require_lead(lead: Any, ptype: str = "read") -> str:
    """The Lead must exist and the caller needs *ptype* permission on it
    (``read`` to see terms, ``write`` to save or delete them)."""
    name = str(lead or "").strip()
    if not name:
        frappe.throw(_("lead is required"))
    if not frappe.db.exists("Lead", name):
        frappe.throw(_("Lead {0} was not found").format(name))
    frappe.has_permission("Lead", ptype, doc=name, throw=True)
    return name


def _resolve_lead_customer_locked(lead: str) -> Optional[str]:
    """``crm._resolve_lead_customer(strict=True)`` with LOCKING reads.

    Call after ``_lock_relationship_row("Lead", lead)``. Under REPEATABLE READ
    the row lock does not refresh the snapshot, so a plain read here could miss
    a conversion committed while this request waited for the lock -- and then
    write terms onto a Lead that is already a Customer. Both relationship
    directions are therefore read FOR UPDATE.
    """
    direct = frappe.db.get_value("Lead", lead, "customer", for_update=True) or None
    rows = frappe.db.sql(
        "SELECT name FROM `tabCustomer` WHERE lead_name = %s ORDER BY creation ASC LIMIT 3 FOR UPDATE",
        (lead,),
        as_dict=True,
    ) or []
    converted_names = sorted({str(r.get("name") or "").strip() for r in rows} - {""})
    if len(converted_names) > 1:
        frappe.throw(
            _("Lead relationship conflict: more than one Customer refers to this Lead. "
              "Ask a manager to repair the Customer.lead_name records.")
        )
    converted = converted_names[0] if converted_names else None
    if direct and converted and direct != converted:
        frappe.throw(
            _("Lead relationship conflict: the Lead and converted Customer point to "
              "different accounts. Ask a manager to repair the relationship.")
        )
    return direct or converted


def _carry_lead_terms_to(customer: str, get_request: bool, prefer_lead: Optional[str] = None) -> Optional[str]:
    """Lazy carry-over: when *customer* has NO terms record, copy the terms still
    stored on its Lead(s). Returns the outcome that wrote, else ``None``.

    Never raises except a deadlock / lock-wait timeout (``slt.FATAL_DB_ERRORS``).
    On a GET, a write flags the request for commit (Frappe rolls back a GET
    otherwise) and failures are logged with a deferred insert so the Error Log
    row survives that rollback.
    """
    try:
        if not doctype_ready() or frappe.db.exists(TERMS_DOCTYPE, {"customer": customer}):
            return None
        leads = slt.leads_of_customer(customer)
    except Exception:
        return None
    if prefer_lead:
        leads = [prefer_lead] + [name for name in leads if name != prefer_lead]
    wrote = None
    for lead in leads:
        outcome = slt.safe_carry_over(lead, customer, defer_log=get_request)
        if outcome:
            wrote = wrote or outcome
            if get_request:
                try:
                    frappe.local.flags.commit = True
                except Exception:
                    pass
        if outcome == slt.CARRY_CREATED:
            wrote = outcome
            break
    return wrote


def _lead_title(lead: str) -> str:
    try:
        row = frappe.db.get_value("Lead", lead, ["company_name", "lead_name"], as_dict=True) or {}
    except Exception:
        row = {}
    return str(row.get("company_name") or row.get("lead_name") or lead)


def _lead_payload(lead: str) -> Dict[str, Any]:
    """The getter's shape for an UNCONVERTED Lead (terms stored as JSON)."""
    title = _lead_title(lead)
    stored = slt.read_lead_terms(lead)
    row: Optional[Dict[str, Any]] = None
    parsed: Optional[Dict[str, Any]] = None
    if stored:
        row = dict(stored)
        row["name"] = lead
        row["customer"] = None
        row["customer_name"] = title
        row["last_reminder_on"] = None
        row["last_reminder_kind"] = None
        parsed = ss.parse_terms(row)
    # A Lead owes nothing: status is computed with no invoices (state "none",
    # zeros) so upcoming_dates still shows the rep what the schedule means.
    status = ss.compute_status(parsed, [], _today())
    terms = None
    if row is not None:
        terms = _serialize_terms(row, parsed)
        terms["customer"] = None
    return {
        "success": True,
        "party_type": PARTY_LEAD,
        "party": lead,
        "lead": lead,
        "customer": None,
        "customer_name": title,
        "terms": terms,
        "description": ss.describe(parsed) if parsed else None,
        "description_ar": ss.describe(parsed, "ar") if parsed else None,
        "status": status,
        "currency": _credit_currency(),
        "can_edit": _can_edit(lead=lead),
    }


def _check_responsible_user(user: Optional[str]) -> None:
    """The DocType controller's user check, for terms stored on a Lead."""
    if not user:
        return
    enabled = frappe.db.get_value("User", user, "enabled")
    if enabled is None:
        frappe.throw(_("User {0} does not exist.").format(user))
    if not int(enabled or 0):
        frappe.throw(_("User {0} is disabled; choose someone who can receive reminders.").format(user))


def _via_lead(payload: Dict[str, Any], lead: str) -> Dict[str, Any]:
    payload["lead"] = lead
    return payload


def invalidate_collections_count_cache() -> None:
    """Drop every cached approvals-badge count (after a terms change). Never raises."""
    try:
        frappe.cache().delete_keys(_COUNT_CACHE_PREFIX)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=False)
def get_settlement_terms(customer: Optional[str] = None, lead: Optional[str] = None) -> Dict[str, Any]:
    """One party's settlement terms and where they stand today.

    Exactly one of ``customer`` / ``lead``. Returns ``{success, party_type,
    party, customer, customer_name, terms, description, description_ar, status,
    currency, can_edit}`` (+ ``lead`` when called with ``lead``). ``terms`` is
    ``null`` when nothing is stored (``status.state`` is then ``unscheduled``
    with a balance, ``none`` without). An unconverted Lead has ``customer:
    null`` and ``party_type: "Lead"``; see the module docstring.
    """
    rep_only = _admit(write=False)
    customer_name, lead_name = _party_args(customer, lead)
    if customer_name:
        name = _require_customer(customer_name)
        _check_rep_party(rep_only, customer=name)
        # Lazy carry-over when the Customer has no terms yet: covers every
        # conversion path the after_insert hook cannot see. The app sends
        # customer= for a converted lead, so this is the path that runs.
        _carry_lead_terms_to(name, get_request=True)
        return _terms_payload(name)

    lead_name = _require_lead(lead_name, "read")
    converted = _resolve_lead_customer(lead_name, strict=False)
    if not converted:
        return _lead_payload(lead_name)

    name = _require_customer(converted)
    _check_rep_party(rep_only, customer=name)
    _carry_lead_terms_to(name, get_request=True, prefer_lead=lead_name)
    return _via_lead(_terms_payload(name), lead_name)


@frappe.whitelist(allow_guest=False)
def save_settlement_terms(
    customer: Optional[str] = None,
    cycle: Optional[str] = None,
    weekdays: Union[str, List[str], None] = None,
    week_interval: Union[int, str, None] = 1,
    month_days: Union[str, List[Any], None] = None,
    interval_days: Union[int, str, None] = None,
    anchor_date: Optional[str] = None,
    remind_days_before: Union[int, str, None] = 1,
    overdue_repeat_days: Union[int, str, None] = 2,
    responsible_user: Optional[str] = None,
    notes: Optional[str] = None,
    enabled: Union[int, str, bool, None] = 1,
    lead: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or replace a party's settlement terms. Same shape as the getter.

    Exactly one of ``customer`` / ``lead`` (``lead`` is last so positional
    callers of the original signature are unaffected). ``weekdays`` /
    ``month_days`` accept a comma string (``"Thu"``, ``"15,last"``) or a JSON
    list. Fields that do not belong to the chosen cycle are cleared.
    ``anchor_date`` omitted keeps the stored one, else today.
    """
    rep_only = _admit(write=True)

    name, lead_name = _party_args(customer, lead)

    # Validate the terms BEFORE touching the database: a bad weekday is the
    # caller's mistake whatever state the customer is in.
    try:
        values = ss.normalize_terms_input(
            {
                "cycle": cycle,
                "enabled": enabled,
                "weekdays": weekdays,
                "week_interval": week_interval,
                "month_days": month_days,
                "interval_days": interval_days,
                "anchor_date": anchor_date,
                "remind_days_before": remind_days_before,
                "overdue_repeat_days": overdue_repeat_days,
                "responsible_user": responsible_user,
                "notes": notes,
            }
        )
    except ss.SettlementTermsError as exc:
        frappe.throw(_(str(exc)), title=_("Invalid settlement terms"))
        raise  # unreachable with the real frappe.throw; keeps a mocked throw honest

    if lead_name:
        lead_name = _require_lead(lead_name, "write")
        # Same row lock the conversion path takes (crm._resolve_order_binding /
        # customer.create_customer), so a save and a conversion serialise. The
        # lock alone is not enough under REPEATABLE READ -- it does not refresh
        # the snapshot -- so the conversion check and the JSON read that follow
        # are LOCKING reads. Either the terms land on the Lead before the
        # Customer's after_insert copies them, or this save sees the Customer
        # and writes there.
        _lock_relationship_row("Lead", lead_name)
        converted = _resolve_lead_customer_locked(lead_name)
        if not converted:
            return _save_lead_terms(lead_name, values)
        converted = _require_customer(converted)
        _check_rep_party(rep_only, customer=converted)
        return _via_lead(_save_customer_terms(converted, values), lead_name)

    name = _require_customer(name)
    _check_rep_party(rep_only, customer=name)
    return _save_customer_terms(name, values)


def _save_lead_terms(lead: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Store validated *values* as JSON on an unconverted Lead."""
    if not slt.lead_field_ready():
        frappe.throw(
            _("Settlement terms for leads are not installed on this site yet. Ask an administrator to run the update (bench migrate).")
        )
    _check_responsible_user(values.get("responsible_user"))
    if values["anchor_date"] is None and ss.needs_anchor(values):
        # Locking read: the Lead row is locked, and a plain read would return
        # the pre-lock snapshot.
        stored = slt.read_lead_terms(lead, for_update=True) or {}
        # Re-saving a fortnightly schedule must not silently shift its grid --
        # the same rule the Customer path applies; the DocType's default
        # (today) otherwise.
        try:
            kept = ss.to_date(stored.get("anchor_date"))
        except ss.SettlementTermsError:
            kept = None
        values["anchor_date"] = kept or _today()
    slt.write_lead_terms(lead, values)
    return _lead_payload(lead)


def _save_customer_terms(name: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert the ``Jarz Settlement Terms`` record of Customer *name* (original path)."""
    if not doctype_ready():
        frappe.throw(
            _("Settlement terms are not installed on this site yet. Ask an administrator to run the update (bench migrate).")
        )

    # Terms still sitting on the Customer's Lead are carried first, so this
    # save updates that record (keeping its anchor) and the Lead JSON is
    # stamped instead of lingering un-carried. No-op when a record exists.
    _carry_lead_terms_to(name, get_request=False)

    existing = frappe.db.get_value(TERMS_DOCTYPE, {"customer": name}, "name")
    if existing:
        doc = frappe.get_doc(TERMS_DOCTYPE, existing)
        if values["anchor_date"] is None and ss.needs_anchor(values) and doc.get("anchor_date"):
            # Re-saving a fortnightly schedule must not silently shift its grid.
            values["anchor_date"] = getdate(doc.get("anchor_date"))
    else:
        doc = frappe.new_doc(TERMS_DOCTYPE)
        doc.customer = name

    for field, value in values.items():
        doc.set(field, value)

    if existing:
        doc.save(ignore_permissions=True)
    else:
        doc.insert(ignore_permissions=True)

    invalidate_collections_count_cache()
    return _terms_payload(name)


@frappe.whitelist(allow_guest=False)
def delete_settlement_terms(customer: Optional[str] = None, lead: Optional[str] = None) -> Dict[str, Any]:
    """Remove a party's terms. Returns the getter's shape (``terms: null``).

    Exactly one of ``customer`` / ``lead``. A converted lead deletes its
    Customer's record (the Lead JSON is history and stays, stamped as carried,
    so the lazy carry-over does not bring it back); an unconverted lead clears
    its JSON.

    The ORIGINAL manager write gate only -- a B2B Sales Rep may not delete
    terms (owner decision 2026-09-26). A Lead also needs Lead write permission.
    """
    _ensure_credit_payment_access()
    customer_name, lead_name = _party_args(customer, lead)
    if lead_name:
        lead_name = _require_lead(lead_name, "write")
        _lock_relationship_row("Lead", lead_name)
        converted = _resolve_lead_customer_locked(lead_name)
        if not converted:
            if slt.lead_field_ready() and slt.read_lead_terms(lead_name, for_update=True):
                slt.write_lead_terms(lead_name, None)
            return _lead_payload(lead_name)
        return _via_lead(_delete_customer_terms(_require_customer(converted)), lead_name)
    return _delete_customer_terms(_require_customer(customer_name))


def _delete_customer_terms(name: str) -> Dict[str, Any]:
    """Drop Customer *name*'s ``Jarz Settlement Terms`` record (original path)."""
    if doctype_ready():
        existing = frappe.db.get_value(TERMS_DOCTYPE, {"customer": name}, "name")
        if existing:
            frappe.delete_doc(TERMS_DOCTYPE, existing, ignore_permissions=True)
            # The daily pass only walks existing terms, so close our tagged
            # ToDos now or they stay open forever.
            from jarz_pos.services.settlement_reminders import sync_settlement_todo

            try:
                sync_settlement_todo(name, None, [], "")
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"settlement_todo_close_failed:{name}"[:140])
            # Terms a rep once agreed on this customer's Lead must not come back
            # through the lazy carry-over in get_settlement_terms(lead=...).
            slt.mark_leads_superseded(name)
    invalidate_collections_count_cache()
    return _terms_payload(name)


def _collection_entries(profiles: List[str]) -> List[Dict[str, Any]]:
    """Per-customer inputs for ``ss.build_collection_rows``, within *profiles*.

    Driven by what is OWED: a customer with terms and nothing open is in state
    ``none`` and would be dropped anyway, so only customers with an open credit
    invoice in scope are loaded.
    """
    open_rows = _open_credit_invoices(profiles=profiles)
    by_customer: Dict[str, Dict[str, Any]] = {}
    for row in open_rows or []:
        customer = str(row.get("customer") or "")
        if not customer:
            continue
        bucket = by_customer.setdefault(
            customer,
            {
                "customer": customer,
                "customer_name": row.get("customer_name") or customer,
                "invoices": [],
            },
        )
        bucket["invoices"].append(
            {
                "name": row.get("name"),
                "posting_date": row.get("posting_date"),
                "outstanding_amount": row.get("outstanding_amount"),
            }
        )
    terms_by_customer = _load_terms_rows(list(by_customer))
    entries: List[Dict[str, Any]] = []
    for customer, bucket in by_customer.items():
        row = terms_by_customer.get(customer)
        bucket["terms"] = ss.parse_terms(row) if row else None
        entries.append(bucket)
    return entries


def _days_ahead(value: Any) -> int:
    try:
        days = int(float(value)) if value not in (None, "") else _DAYS_AHEAD_DEFAULT
    except (TypeError, ValueError):
        days = _DAYS_AHEAD_DEFAULT
    return max(0, min(days, _DAYS_AHEAD_MAX))


@frappe.whitelist(allow_guest=False)
def get_collections_due(
    days_ahead: Union[int, str, None] = _DAYS_AHEAD_DEFAULT,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Every customer with an open credit balance, by urgency.

    Sorted overdue -> due_today -> due_soon -> unscheduled -> ok; customers with
    nothing open are left out. ``days_ahead`` widens "due soon" for this list
    (a row is ``due_soon`` when its next due date is within
    ``max(remind_days_before, days_ahead)`` days).

    Branch-scoped exactly like ``credit.get_credit_ledger``: the caller sees
    orders belonging to the POS Profiles they are assigned to; ``branch`` narrows
    to one of them. A shop that orders from two branches therefore shows only
    the part a branch manager is responsible for.
    """
    _ensure_credit_ledger_access()

    days = _days_ahead(days_ahead)
    selected = str(branch or "").strip()
    currency = _credit_currency()
    filters_echo = {"branch": selected or None, "days_ahead": days}

    def _empty(code: str, notice: str) -> Dict[str, Any]:
        return {
            "success": True,
            "currency": currency,
            "filters": filters_echo,
            "rows": [],
            "counts": {"overdue": 0, "due_today": 0, "due_soon": 0, "unscheduled": 0},
            "notice_code": code,
            "notice": notice,
        }

    allowed = _allowed_profiles()
    if not allowed:
        return _empty(
            "no_branch_assigned",
            _("You are not assigned to any branch (POS Profile). Ask an administrator to add you to the branches you manage."),
        )
    profiles = list(allowed)
    if selected and selected.lower() != "all":
        if selected not in allowed:
            return _empty("branch_not_permitted", _("You are not assigned to branch {0}.").format(selected))
        profiles = [selected]

    rows, counts = ss.build_collection_rows(_collection_entries(profiles), _today(), soon_days=days)
    return {
        "success": True,
        "currency": currency,
        "filters": filters_echo,
        "rows": rows,
        "counts": counts,
    }


def count_collections_needing_attention() -> int:
    """Customers in state overdue / due_today within the caller's branches.

    For the approvals badge (``api/approvals``). Cached per user for
    ``_COUNT_CACHE_SECONDS`` because the badge is polled every minute and this
    walks every open credit invoice. Raises on a real failure so the approvals
    endpoint logs and skips the queue, like every other queue there.
    """
    user = str(getattr(frappe.session, "user", "") or "")
    cache_key = f"{_COUNT_CACHE_PREFIX}{user}"
    try:
        cached = frappe.cache().get_value(cache_key)
        if cached is not None:
            return int(cached)
    except Exception:
        pass

    profiles = _allowed_profiles()
    if not profiles:
        count = 0
    else:
        rows, _counts = ss.build_collection_rows(_collection_entries(profiles), _today())
        count = sum(1 for row in rows if ss.needs_attention(row.get("state")))

    try:
        frappe.cache().set_value(cache_key, count, expires_in_sec=_COUNT_CACHE_SECONDS)
    except Exception:
        pass
    return count

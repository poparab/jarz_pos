"""Employee cash advances: a line manager requests, a JARZ Manager pays.

The flow this module implements, end to end:

  1. A LINE MANAGER opens the Expenses screen, picks an employee, an amount, a
     reason and the branch account the cash would come out of, and files a
     request. Nothing is submitted and no money moves — the ``Employee Advance``
     sits at docstatus 0.
  2. A JARZ MANAGER approves it. On approval the advance is submitted **and** a
     Payment Entry is built, inserted and submitted in the same request, so the
     cash actually leaves the branch account immediately.

Why the Payment Entry and not a Journal Entry
---------------------------------------------
``Employee Advance`` posts NO GL of its own. The only thing that moves money and
that HRMS recognises is a Payment Entry built by
``hrms.overrides.employee_payment_entry.get_payment_entry_for_employee``:
submitting it fires ``EmployeeAdvance.set_total_advance_paid`` through the
``advance_payment_payable_doctypes`` hook, which is what writes ``paid_amount``
and lets ``set_status()`` derive ``Paid``. A hand-written Journal Entry would
debit and credit the right accounts and still leave ``paid_amount = 0`` with the
status stuck at ``Unpaid`` — a document that says the employee was never paid
sitting next to an empty till.

Why HRMS is imported lazily
---------------------------
``hooks.py`` declares no ``required_apps``, so this module has to *import* on a
bench without HRMS even though it cannot *work* there. Every HRMS import is
therefore inside the function that needs it, and every entry point asks
``hrms_available()`` first. The bootstrap degrades to an empty, explained
payload rather than an exception, so the client renders an empty state instead
of an error tile.

Permissions
-----------
No JARZ role holds any DocPerm on ``Employee Advance``, and none is added: a
single custom DocPerm row on a DocType REVOKES the standard permissions of every
other role on it, which would take Employee Advance away from HRMS's own
``Employee`` and ``Expense Approver`` roles. Writes therefore go through
``ignore_permissions``, exactly as ``api/expenses.py`` does, and the one place
where that is not enough (HRMS's own ``frappe.has_permission`` guards) is handled
by :func:`_hrms_permission_bypass` with its reasoning written out in full.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, get_last_day, getdate, now_datetime, today

# These helpers carry a leading underscore, i.e. they are private by convention.
# They are imported anyway, deliberately: the Expenses screen and the Advances
# screen render the SAME payment-source picker off the SAME
# {id, account, label, label_en, label_ar, category, balance, pos_profile}
# payload, and the Flutter client localises off the label/label_en/label_ar
# triple. Re-implementing them here would fork that contract, and the first
# divergence — a new label column, a new account category, a changed sort — would
# surface as two adjacent screens listing different accounts with no error
# anywhere. One definition, imported.
from jarz_pos.api.expenses import (
    PaymentSource,
    _account_label_map,
    _bilingual_label_from_account,
    _cashlike_accounts,
    _current_user_pos_profile_names,
    _default_company,
    _fallback_label,
    _manager_pos_profiles,
    _month_label,
    _parse_filters,
    _pos_profile_accounts,
    _serialize_payment_sources,
)
from jarz_pos.constants import ROLES
from jarz_pos.utils.employee_link import (
    ADVANCE_DOCTYPE,
    hrms_available,
    list_active_employees,
)

# ── The jarz_pos-owned columns on Employee Advance ───────────────────────────
# Seeded by ``jarz_pos.setup.employee_link_setup.ensure_employee_link_fields``.
# Named here rather than inline so a rename is one edit and so every read can be
# guarded by ``_advance_has_field`` — between deploying this code and the first
# migrate these columns do not exist, and selecting a missing column raises.
F_PAYING_ACCOUNT = "custom_jarz_paying_account"
F_POS_PROFILE = "custom_jarz_pos_profile"
F_REQUESTED_BY = "custom_jarz_requested_by"
F_APPROVED_BY = "custom_jarz_approved_by"
F_APPROVED_ON = "custom_jarz_approved_on"
F_PAYMENT_ENTRY = "custom_jarz_payment_entry"

JARZ_FIELDS = (
    F_PAYING_ACCOUNT,
    F_POS_PROFILE,
    F_REQUESTED_BY,
    F_APPROVED_BY,
    F_APPROVED_ON,
    F_PAYMENT_ENTRY,
)

#: Columns read straight off ``Employee Advance``. ``status`` is READ-ONLY and
#: derived by ``EmployeeAdvance.set_status()`` — it is read, never written.
BASE_ADVANCE_FIELDS = (
    "name",
    "employee",
    "employee_name",
    "posting_date",
    "currency",
    "advance_amount",
    "paid_amount",
    "claimed_amount",
    "return_amount",
    "purpose",
    "status",
    "docstatus",
    "advance_account",
    "company",
    # ``owner`` is the fallback for ``requested_by`` on rows that predate the
    # custom_jarz_requested_by column — without it those rows render with an
    # empty requester rather than the person who actually filed them.
    "owner",
    "creation",
    "modified",
)

#: Who may file a request. ``LINE_MANAGER_TIER`` already contains JARZ Manager,
#: Administrator, System Manager and BOTH real spellings of the line-manager
#: role, so gating on the set rather than naming roles by hand is what keeps
#: half the line managers from being silently excluded.
REQUEST_ROLES = ROLES.LINE_MANAGER_TIER

#: Who may approve — i.e. who may make cash leave a drawer. Deliberately a
#: DIFFERENT set, not a widening of the one above: the whole point of the flow is
#: that the person who asks is not the person who pays, so neither spelling of
#: the line-manager role appears here. (``ROLES.ADMIN`` also carries POS Manager,
#: who can therefore approve without being able to request — harmless, and left
#: alone rather than special-cased, since ADMIN is this app's standing answer to
#: "who runs the money screens".)
APPROVE_ROLES = ROLES.ADMIN | {ROLES.JARZ_MANAGER}


# ── Diagnostics: logging must never change an outcome ────────────────────────


def _log(*args: Any, **kwargs: Any) -> None:
    """``frappe.log_error`` that cannot itself raise.

    Not defensive padding: ``frappe.log_error`` really can throw. It calls
    ``sentry.capture_exception``, whose first statement —
    ``frappe.get_system_settings("enable_telemetry")`` — sits outside its own
    ``try``. A cache outage there turns a diagnostic into the caller's
    exception. Every call site below is explaining something that already
    happened; none of them is the operation.
    """
    try:
        frappe.log_error(*args, **kwargs)
    except Exception:  # pragma: no cover - the logger of last resort
        pass


# ── Guards ───────────────────────────────────────────────────────────────────


def _roles() -> set:
    return {str(r or "").strip() for r in (frappe.get_roles(frappe.session.user) or []) if str(r or "").strip()}


def _can_request() -> bool:
    return bool(_roles().intersection(REQUEST_ROLES))


def _can_approve() -> bool:
    return bool(_roles().intersection(APPROVE_ROLES))


def _ensure_can_request() -> None:
    if not _can_request():
        frappe.throw(
            _("Not permitted: only line managers and above can request an employee advance."),
            frappe.PermissionError,
        )


def _ensure_can_approve() -> None:
    # NOTE: deliberately NOT ``api/expenses.py::_is_manager()``. That helper is
    # JARZ-Manager-only and ``scripts/role_matrix_validation.py`` asserts line
    # managers are denied there; reusing it would couple two unrelated gates so
    # that widening one silently widens the other.
    if not _can_approve():
        frappe.throw(
            _("Not permitted: only managers can approve an employee advance."),
            frappe.PermissionError,
        )


def _ensure_hrms() -> None:
    if not hrms_available():
        frappe.throw(
            _("Employee Advances are unavailable: the HRMS app is not installed on this site.")
        )


def _advance_has_field(fieldname: str) -> bool:
    """True when ``Employee Advance`` actually carries the given column.

    The ``custom_jarz_*`` fields are seeded on ``after_migrate``. Between
    deploying this code and the first migrate they do not exist, and both
    selecting and filtering on a missing column raise — so every touch of them
    goes through this guard. Mirrors ``employee_link.customer_has_employee_field``.
    """
    try:
        return bool(frappe.get_meta(ADVANCE_DOCTYPE).get_field(fieldname))
    except Exception:
        return False


def _present_jarz_fields() -> List[str]:
    return [f for f in JARZ_FIELDS if _advance_has_field(f)]


@contextmanager
def _hrms_permission_bypass() -> Iterator[None]:
    """Run HRMS's Payment Entry path as Administrator, then hand identity back.

    Why this is necessary, and why ``ignore_permissions`` is not enough:
    ``hrms.overrides.employee_payment_entry`` guards BOTH
    ``get_payment_entry_for_employee`` and ``get_payment_reference_details`` with
    ``frappe.has_permission("Employee Advance", "read", ..., throw=True)``, and
    ``PaymentEntry.validate()`` re-runs ``set_missing_ref_details(force=True)``
    on every insert AND every submit. ``frappe.has_permission`` is a pure role
    check: it never consults ``doc.flags.ignore_permissions``, so the
    ``ignore_permissions`` this app uses everywhere else cannot reach it, and a
    JARZ Manager (who holds no DocPerm on Employee Advance) hits PermissionError
    the moment the Payment Entry is built.

    The alternative — giving a JARZ role a DocPerm on Employee Advance — is
    worse: one custom DocPerm row on a DocType revokes the standard permissions
    of every OTHER role on it, which would take Employee Advance away from
    HRMS's own ``Employee`` and ``Expense Approver`` roles.

    ``frappe.set_user`` is deliberately NOT used: besides the user it also
    blanks ``local.form_dict`` and rewrites ``session.sid``, which is live
    request state we must not lose mid-request.
    ``frappe.permissions.has_permission`` short-circuits to True on
    ``user == "Administrator"`` before it touches any role cache, so swapping the
    user and clearing the two derived caches is exactly enough — and exactly
    reversible in the ``finally``.
    """
    original = frappe.session.user
    try:
        frappe.session.user = "Administrator"
        frappe.local.role_permissions = {}
        frappe.local.user_perms = None
        yield
    finally:
        frappe.session.user = original
        frappe.local.role_permissions = {}
        frappe.local.user_perms = None


# ── Serialisation ────────────────────────────────────────────────────────────


def _date_str(value: Any) -> str:
    """Dates and datetimes go over the wire as strings, never as objects."""
    if value in (None, ""):
        return ""
    return str(value)


def _company_currency(company: str) -> str:
    try:
        return str(frappe.db.get_value("Company", company, "default_currency") or "")
    except Exception:
        return ""


def _employee_meta(employees: Sequence[str]) -> Dict[str, Dict[str, str]]:
    """``employee -> {employee_name, branch, department}`` for rows we already have.

    Complements ``employee_link.employee_display_names`` rather than duplicating
    it: that helper answers names only, and an advance row also needs the branch
    so the board can group by it. Not filtered on ``status="Active"`` — an
    advance taken by someone who has since resigned still has to render.
    """
    wanted = sorted({str(e or "").strip() for e in employees if str(e or "").strip()})
    if not wanted:
        return {}
    try:
        rows = (
            frappe.get_all(
                "Employee",
                filters={"name": ["in", wanted]},
                fields=["name", "employee_name", "branch", "department"],
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        _log(frappe.get_traceback(), "employee_advances._employee_meta")
        return {}
    return {
        r["name"]: {
            "employee_name": str(r.get("employee_name") or r["name"]),
            "branch": str(r.get("branch") or ""),
            "department": str(r.get("department") or ""),
        }
        for r in rows
    }


def _user_full_names(users: Sequence[str]) -> Dict[str, str]:
    wanted = sorted({str(u or "").strip() for u in users if str(u or "").strip()})
    if not wanted:
        return {}
    try:
        rows = (
            frappe.get_all(
                "User",
                filters={"name": ["in", wanted]},
                fields=["name", "full_name"],
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        return {}
    return {r["name"]: str(r.get("full_name") or r["name"]) for r in rows}


def _status_for(row: Dict[str, Any]) -> str:
    """``status`` as HRMS derived it, with a docstatus fallback.

    Never computed and never written by this app: ``status`` is read-only and
    ``EmployeeAdvance.set_status()`` owns it. The fallback exists only for a row
    read before HRMS had a chance to set it (a brand-new in-memory doc).
    """
    status = str(row.get("status") or "").strip()
    if status:
        return status
    return {0: "Draft", 1: "Unpaid", 2: "Cancelled"}.get(int(row.get("docstatus") or 0), "Draft")


def _serialize_advance(
    row: Dict[str, Any],
    account_labels: Optional[Dict[str, Dict[str, str]]] = None,
    employee_meta: Optional[Dict[str, Dict[str, str]]] = None,
    user_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """The single wire shape for an advance. Every endpoint returns exactly this.

    ``amount`` maps from ``advance_amount`` — the client never sees an HRMS
    fieldname — and ``balance`` is what the employee still owes back:
    ``paid_amount - (claimed_amount + return_amount)``.
    """
    row = dict(row or {})
    employee = str(row.get("employee") or "")
    meta = (employee_meta or {}).get(employee) or {}

    paying_account = str(row.get(F_PAYING_ACCOUNT) or "")
    labels = (account_labels or {}).get(paying_account) or {}
    payment_label = _fallback_label(labels.get("label"), paying_account)
    bilingual = _bilingual_label_from_account(paying_account, payment_label, account_labels)

    requested_by = str(row.get(F_REQUESTED_BY) or row.get("owner") or "")

    amount = flt(row.get("advance_amount"), 2)
    paid_amount = flt(row.get("paid_amount"), 2)
    claimed_amount = flt(row.get("claimed_amount"), 2)
    return_amount = flt(row.get("return_amount"), 2)

    return {
        "name": row.get("name"),
        "employee": employee,
        "employee_name": str(row.get("employee_name") or meta.get("employee_name") or employee),
        "branch": meta.get("branch", ""),
        "pos_profile": str(row.get(F_POS_PROFILE) or ""),
        "posting_date": _date_str(row.get("posting_date")),
        "currency": str(row.get("currency") or ""),
        "amount": amount,
        "paid_amount": paid_amount,
        "claimed_amount": claimed_amount,
        "return_amount": return_amount,
        "balance": flt(paid_amount - (claimed_amount + return_amount), 2),
        "purpose": str(row.get("purpose") or ""),
        "status": _status_for(row),
        "docstatus": int(row.get("docstatus") or 0),
        "paying_account": paying_account,
        "payment_label": payment_label,
        "payment_label_en": bilingual["label_en"],
        "payment_label_ar": bilingual["label_ar"],
        "requested_by": requested_by,
        "requested_by_name": (user_names or {}).get(requested_by, requested_by),
        "approved_by": str(row.get(F_APPROVED_BY) or ""),
        "approved_on": _date_str(row.get(F_APPROVED_ON)),
        "payment_entry": str(row.get(F_PAYMENT_ENTRY) or ""),
        "company": str(row.get("company") or ""),
        "creation": _date_str(row.get("creation")),
        "modified": _date_str(row.get("modified")),
    }


def _serialize_advances(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bulk wrapper: builds the three lookup maps once, then delegates per row.

    Same split as ``api/expenses.py::_serialize_expenses`` — the per-row helper
    stays the single definition of the shape, and the N+1 lookups it would
    otherwise do are hoisted here.
    """
    rows = list(rows or [])
    if not rows:
        return []

    fallback_labels: Dict[str, str] = {}
    for row in rows:
        account = _fallback_label(row.get(F_PAYING_ACCOUNT))
        if account and account not in fallback_labels:
            fallback_labels[account] = account

    account_labels = _account_label_map(list(fallback_labels), fallback_labels) if fallback_labels else {}
    employee_meta = _employee_meta([r.get("employee") for r in rows])
    user_names = _user_full_names(
        [r.get(F_REQUESTED_BY) or r.get("owner") for r in rows]
        + [r.get(F_APPROVED_BY) for r in rows]
    )

    return [
        _serialize_advance(
            row,
            account_labels=account_labels,
            employee_meta=employee_meta,
            user_names=user_names,
        )
        for row in rows
    ]


def _serialize_one(row: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise a single advance, building its (tiny) lookup maps on the fly."""
    return _serialize_advances([row])[0]


# ── Reads ────────────────────────────────────────────────────────────────────


def _collect_advances(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = list(BASE_ADVANCE_FIELDS) + _present_jarz_fields()
    try:
        return (
            frappe.get_all(
                ADVANCE_DOCTYPE,
                filters=filters,
                fields=fields,
                order_by="posting_date desc, creation desc",
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        _log(frappe.get_traceback(), "employee_advances._collect_advances")
        return []


def _load_months() -> List[str]:
    """Distinct ``YYYY-MM`` buckets that have at least one advance.

    NOTE: no SQL function in the SELECT (no ``DATE_FORMAT``, no ``DISTINCT`` on
    an expression) — ERPNext v16's query engine rejects function-strings in
    ``fields``, and the failure mode is a silently wrong answer rather than an
    error. The posting_date column is pulled and bucketed in Python, exactly as
    ``api/price_lists.py::_pricing_categories`` counts in Python.
    """
    months: List[str] = []
    seen = set()
    try:
        rows = (
            frappe.get_all(
                ADVANCE_DOCTYPE,
                fields=["posting_date"],
                order_by="posting_date desc",
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        _log(frappe.get_traceback(), "employee_advances._load_months")
        rows = []

    for row in rows:
        value = row.get("posting_date")
        if not value:
            continue
        key = str(value)[:7]
        if key and key not in seen:
            seen.add(key)
            months.append(key)

    current = getdate().strftime("%Y-%m")
    if current not in seen:
        months.insert(0, current)
    return months


def _month_bounds(month_key: str) -> Optional[tuple]:
    """``("YYYY-MM-01", "YYYY-MM-<last>")`` for a month key, or ``None``.

    ``None`` for anything unparseable so a malformed ``month`` filter widens the
    result set instead of throwing at the caller.
    """
    key = str(month_key or "").strip()
    if not key:
        return None
    try:
        anchor = getdate(f"{key}-01")
        return (str(get_first_day(anchor)), str(get_last_day(anchor)))
    except Exception:
        return None


def _payment_sources(company: str, can_approve: bool) -> List[Dict[str, Any]]:
    """The branch/cash accounts this user may name as the source of the cash.

    Mirrors ``api/expenses.py::get_expense_bootstrap``: branch (POS Profile)
    accounts first, then every other cash/bank/wallet ledger for the people who
    actually release money.

    The fallback in the middle is deliberate. A line manager whose POS Profile
    User rows were never filled in resolves to zero profiles, and an empty
    picker reads to them as "the feature is broken" with nothing to click — the
    same trap ``employee_link.list_active_employees`` documents for an unset
    ``Employee.branch``. Falling back to the company's profiles keeps the screen
    usable; the approval step, not this picker, is where the money is actually
    controlled.
    """
    sources: List[PaymentSource] = []

    if can_approve:
        profiles: Sequence[str] = _manager_pos_profiles(company)
    else:
        profiles = _current_user_pos_profile_names() or _manager_pos_profiles(company)

    if profiles:
        sources.extend(_pos_profile_accounts(company, profiles))

    if can_approve:
        excluded = {s.account for s in sources if s.account}
        sources.extend(_cashlike_accounts(company, excluded_accounts=excluded))

    return _serialize_payment_sources(sources)


@frappe.whitelist(allow_guest=False)
def get_employee_advance_bootstrap(filters: Optional[str] = None) -> Dict[str, Any]:
    """Everything the Advances screen needs in one round trip.

    ``filters`` is a JSON string or a dict with optional ``month`` (``YYYY-MM``),
    ``status``, ``employee`` and ``branch`` (a POS Profile name — "branch" means
    the POS Profile everywhere in this app).

    On a bench without HRMS this returns ``success: True`` with
    ``hrms_available: False``, empty lists and a ``notice``. It never throws:
    the client renders an explanatory empty state, not an error tile.
    """
    payload = _parse_filters(filters)
    requested_month = str(payload.get("month") or "").strip()
    status_filter = str(payload.get("status") or "").strip()
    employee_filter = str(payload.get("employee") or "").strip()
    branch_filter = str(payload.get("branch") or "").strip()

    can_request = _can_request()
    can_approve = _can_approve()
    current_month = getdate().strftime("%Y-%m")

    if not hrms_available():
        return {
            "success": True,
            "hrms_available": False,
            "notice": _(
                "Employee Advances need the HRMS app, which is not installed on this site."
            ),
            "can_request": can_request,
            "can_approve": can_approve,
            "company": "",
            "currency": "",
            "current_month": current_month,
            "requested_month": requested_month or current_month,
            "months": [],
            "employees": [],
            "payment_sources": [],
            "advances": [],
            "summary": {
                "total_amount": 0.0,
                "pending_count": 0,
                "pending_amount": 0.0,
                "approved_count": 0,
                "outstanding_amount": 0.0,
            },
            "applied_filters": {
                "month": requested_month or current_month,
                "status": status_filter,
                "employee": employee_filter,
                "branch": branch_filter,
            },
        }

    company = _default_company()

    months = _load_months()
    month_to_use = requested_month or current_month
    if month_to_use not in months:
        months.insert(0, month_to_use)

    # Cancelled advances are excluded by default, the same way the Expenses
    # bootstrap does: a rejection deletes the draft, so a docstatus-2 row here is
    # an unusual, after-the-fact cancellation rather than routine traffic.
    advance_filters: Dict[str, Any] = {"docstatus": ["in", [0, 1]]}
    month_bounds = _month_bounds(month_to_use)
    if month_bounds:
        # A real date range, not a LIKE on a Date column: LIKE forces MariaDB to
        # cast every row's date to a string, which drops the index and — worse —
        # silently matches nothing on a driver that renders dates differently.
        advance_filters["posting_date"] = ["between", list(month_bounds)]
    if employee_filter:
        advance_filters["employee"] = employee_filter
    if status_filter:
        advance_filters["status"] = status_filter
    if branch_filter and _advance_has_field(F_POS_PROFILE):
        advance_filters[F_POS_PROFILE] = branch_filter

    rows = _collect_advances(advance_filters)
    advances = _serialize_advances(rows)

    # Counted and summed in Python on purpose — see _load_months: v16 rejects SQL
    # functions in a query-builder SELECT, and the set here is a single month's
    # advances, which is small.
    pending = [a for a in advances if a["docstatus"] == 0]
    approved = [a for a in advances if a["docstatus"] == 1]
    summary = {
        "total_amount": flt(sum(a["amount"] for a in advances), 2),
        "pending_count": len(pending),
        "pending_amount": flt(sum(a["amount"] for a in pending), 2),
        "approved_count": len(approved),
        "outstanding_amount": flt(sum(a["balance"] for a in approved), 2),
    }

    # Only pass ``branch`` down to the employee picker when Employee.branch is
    # actually populated with it. ``employee_link.list_active_employees`` warns
    # about exactly this: an unset Employee.branch is common here, and filtering
    # on it then returns nothing, which the screen renders as "no employees
    # exist" rather than "your filter matched nothing".
    employee_branch: Optional[str] = None
    if branch_filter:
        try:
            if frappe.db.exists("Employee", {"branch": branch_filter, "status": "Active"}):
                employee_branch = branch_filter
        except Exception:
            employee_branch = None

    return {
        "success": True,
        "hrms_available": True,
        "can_request": can_request,
        "can_approve": can_approve,
        "company": company,
        "currency": _company_currency(company),
        "current_month": current_month,
        "requested_month": month_to_use,
        "months": [{"id": m, "label": _month_label(m)} for m in months],
        "employees": list_active_employees(branch=employee_branch, company=company),
        "payment_sources": _payment_sources(company, can_approve),
        "advances": advances,
        "summary": summary,
        "applied_filters": {
            "month": month_to_use,
            "status": status_filter,
            "employee": employee_filter,
            "branch": branch_filter,
        },
    }


# ── Validation helpers for the write paths ───────────────────────────────────


def _validate_employee(employee: str) -> Dict[str, Any]:
    """The employee must exist and still be Active.

    Same rule as ``employee_link.resolve_employee_for_user`` and
    ``services.courier_identity.resolve_courier_party``: a resigned employee
    must not keep drawing advances. HRMS's own ``validate_active_employee``
    would also catch it, but not until insert — and the message a manager gets
    here names the person.
    """
    row = frappe.db.get_value(
        "Employee",
        employee,
        ["name", "employee_name", "status", "company", "branch", "salary_currency"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Employee {0} does not exist.").format(employee))
    if str(row.get("status") or "") != "Active":
        frappe.throw(
            _("Employee {0} is {1}, not Active, and cannot be given an advance.").format(
                str(row.get("employee_name") or employee), str(row.get("status") or "unknown")
            )
        )
    return row


def _validate_paying_account(company: str, account: str) -> Dict[str, Any]:
    """The source of the cash must be a real, non-group, cash-like company ledger.

    The cash-like test mirrors ``api/expenses.py::_cashlike_accounts`` — Cash and
    Bank by ``account_type``, plus the Mobile/Wallet name sweep — rather than
    checking ``account_type`` alone. The picker offers wallet accounts that carry
    no Cash/Bank ``account_type``, and a validator stricter than the picker
    rejects a choice the UI just presented, which is indistinguishable from a
    bug. Done as a name test rather than by re-running ``_cashlike_accounts``
    because that helper computes a GL balance per account, which is far too much
    work for a yes/no answer.
    """
    row = frappe.db.get_value(
        "Account",
        account,
        ["name", "company", "is_group", "account_type", "account_name", "account_currency"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Paying account {0} does not exist.").format(account))
    if int(row.get("is_group") or 0):
        frappe.throw(_("Paying account {0} is a group account and cannot hold a payment.").format(account))
    if str(row.get("company") or "") != company:
        frappe.throw(
            _("Paying account {0} does not belong to company {1}.").format(account, company)
        )

    account_type = str(row.get("account_type") or "").strip()
    haystack = f"{row.get('account_name') or ''} {row.get('name') or ''}".lower()
    is_cashlike = account_type in ("Cash", "Bank") or "mobile" in haystack or "wallet" in haystack
    if not is_cashlike:
        frappe.throw(
            _("Paying account {0} is not a cash, bank or wallet account.").format(account)
        )
    return row


def _resolve_advance_account(company: str) -> str:
    """The Receivable ledger the advance is booked against.

    ``EmployeeAdvance.before_submit`` already falls back to
    ``Company.default_employee_advance_account`` and throws a good message when
    it is empty — but only that field. Resolving here as well means a site that
    has an ``Employee Advances`` ledger but never set the Company default still
    works, instead of failing at the moment of approval.

    Nothing is written back to Company: pointing the company default at an
    account is ``setup/employee_link_setup.py``'s job, at migrate time, where an
    operator reads the summary. An API call is not the place to reconfigure the
    chart of accounts.
    """
    account = ""
    try:
        if frappe.db.has_column("Company", "default_employee_advance_account"):
            account = str(
                frappe.db.get_value("Company", company, "default_employee_advance_account") or ""
            ).strip()
    except Exception:
        account = ""

    if not account:
        try:
            account = str(
                frappe.db.get_value(
                    "Account",
                    {
                        "company": company,
                        "is_group": 0,
                        "account_name": "Employee Advances",
                        "account_type": "Receivable",
                    },
                    "name",
                )
                or ""
            ).strip()
        except Exception:
            account = ""

    if not account:
        frappe.throw(
            _(
                "No employee advance account is configured for {0}. Create a "
                "Receivable account named 'Employee Advances' and set it as the "
                "company's Default Employee Advance Account."
            ).format(company)
        )
    return account


def _linked_payment_entries(advance_name: str) -> List[str]:
    """Submitted Payment Entries that reference this advance.

    Read off ``Payment Entry Reference`` rather than off
    ``custom_jarz_payment_entry``: a Payment Entry raised by hand from the Desk
    (which is how HRMS expects an advance to be paid) never touches our column,
    so trusting the column alone would report "unpaid" for an advance that was.
    """
    try:
        rows = (
            frappe.get_all(
                "Payment Entry Reference",
                filters={
                    "reference_doctype": ADVANCE_DOCTYPE,
                    "reference_name": advance_name,
                    "docstatus": 1,
                },
                fields=["parent"],
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        _log(frappe.get_traceback(), "employee_advances._linked_payment_entries")
        return []
    return sorted({str(r["parent"]) for r in rows if r.get("parent")})


def _mode_of_payment_for_account(company: str, account: str) -> str:
    """A Mode of Payment whose default account for this company IS this account.

    Purely informational on the Payment Entry (bank reconciliation and reports
    read it). Resolved AFTER the Payment Entry is built, never set on the
    Employee Advance: ``get_bank_cash_account`` lets ``doc.mode_of_payment``
    override the ``bank_account`` we pass, so a mode of payment on the advance
    would silently redirect the payout to a different drawer than the manager
    chose.
    """
    try:
        rows = (
            frappe.get_all(
                "Mode of Payment Account",
                filters={"company": company, "default_account": account},
                fields=["parent"],
                limit=1,
            )
            or []
        )
    except Exception:
        return ""
    return str(rows[0]["parent"]) if rows else ""


# ── Writes ───────────────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=False)
def create_employee_advance_request(payload: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """File a cash-advance request for an employee. Creates a DRAFT; pays nothing.

    Accepts either a JSON ``payload`` string or flat keyword arguments, exactly
    like ``api/expenses.py::create_expense`` — the mobile client posts flat
    fields, the harness posts a payload, and both have to work.

    Params: ``employee`` (required), ``amount`` (required, > 0), ``purpose``
    (required), ``paying_account`` (required), ``pos_profile`` (optional),
    ``posting_date`` (optional, defaults to today).
    """
    _ensure_can_request()
    _ensure_hrms()

    # The SCHEMA precondition comes first, ahead of every input check. Without
    # the column that records which drawer to pay from, the approve step has no
    # idea where the cash would come from, so a request created now would be
    # unapprovable later — a queue of dead rows instead of one clear error. It
    # is checked before the field guards because nothing the caller types can
    # fix it: answering "Purpose is required" here would send a manager editing
    # their form when the real action is a deploy.
    if not _advance_has_field(F_PAYING_ACCOUNT):
        frappe.throw(
            _(
                "Employee Advance is missing the Jarz paying-account field. "
                "Run `bench migrate` so jarz_pos.setup.employee_link_setup can "
                "create it, then try again."
            )
        )

    data = _parse_filters(payload)
    data.update(kwargs)

    employee = str(data.get("employee") or "").strip()
    purpose = str(data.get("purpose") or "").strip()
    paying_account = str(
        data.get("paying_account") or data.get("payment_account") or ""
    ).strip()
    raw_amount = data.get("amount")

    # Presence is swept for EVERY field at once rather than one short-circuiting
    # guard at a time. Two reasons, and the second is the one that bit:
    #   * a form with two empty boxes should be told about both in one reply,
    #     not send the manager round the loop once per field;
    #   * a chain of single guards reports whichever happens to be FIRST, so a
    #     request missing only its purpose can be answered with an error about
    #     the amount. Each field now names itself, whatever else is also empty.
    missing: List[str] = []
    if not employee:
        missing.append(_("Employee"))
    if raw_amount is None or str(raw_amount).strip() == "":
        missing.append(_("Amount"))
    if not purpose:
        missing.append(_("Purpose"))
    if not paying_account:
        missing.append(_("Paying account"))
    if missing:
        frappe.throw(_("Missing required field(s): {0}.").format(", ".join(missing)))

    # Kept separate from the presence sweep above on purpose: a present-but-zero
    # amount is a different diagnosis from an empty box. This is the guard that
    # stops a Payment Entry that moves no money — or, negative, moves it the
    # wrong way, taking cash FROM the employee.
    amount = flt(raw_amount, 2)
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero."))

    employee_row = _validate_employee(employee)
    company = str(employee_row.get("company") or "") or _default_company()
    _validate_paying_account(company, paying_account)

    pos_profile = str(data.get("pos_profile") or "").strip()
    if pos_profile and not frappe.db.exists("POS Profile", pos_profile):
        frappe.throw(_("POS Profile {0} does not exist.").format(pos_profile))

    posting_date = str(data.get("posting_date") or "").strip() or today()

    # ``currency`` is reqd on Employee Advance and normally fetched from
    # ``employee.salary_currency``. Plenty of Employee records here have that
    # field empty, and the fetch then leaves the document invalid at insert —
    # so fall back to the company currency explicitly.
    currency = str(employee_row.get("salary_currency") or "") or _company_currency(company)

    doc = frappe.get_doc(
        {
            "doctype": ADVANCE_DOCTYPE,
            "employee": employee,
            "posting_date": posting_date,
            "purpose": purpose,
            "advance_amount": amount,
            "currency": currency,
            "company": company,
            # ``status`` is deliberately absent: it is read-only and derived by
            # EmployeeAdvance.set_status(). Setting it by hand produces a row
            # whose status disagrees with its own amounts.
        }
    )

    stamps = {
        F_PAYING_ACCOUNT: paying_account,
        F_POS_PROFILE: pos_profile or None,
        F_REQUESTED_BY: frappe.session.user,
    }
    for fieldname, value in stamps.items():
        if _advance_has_field(fieldname):
            doc.set(fieldname, value)

    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    doc.reload()

    return {"success": True, "advance": _serialize_one(doc.as_dict())}


def _build_and_submit_payment_entry(advance: Any, paying_account: str) -> Any:
    """Build, insert and submit the Payment Entry that hands the cash over.

    ``get_payment_entry_for_employee`` is HRMS's own builder and is used rather
    than a hand-rolled Payment Entry because it fills the reference row that
    ``set_total_advance_paid`` keys off — the ONLY thing that writes
    ``Employee Advance.paid_amount`` and lets the status become ``Paid``.

    ``bank_account`` is passed in rather than assigning ``paid_from``
    afterwards so ERPNext derives ``paid_from_account_currency`` and the
    exchange rate for the account we actually want. The assignment below is a
    belt-and-braces assertion for the case where a Mode of Payment on the
    advance would have overridden it.
    """
    # Lazy import: this module must import cleanly on a bench without HRMS.
    from hrms.overrides.employee_payment_entry import get_payment_entry_for_employee

    approver = frappe.session.user

    with _hrms_permission_bypass():
        pe = get_payment_entry_for_employee(
            ADVANCE_DOCTYPE, advance.name, bank_account=paying_account
        )

        if str(pe.paid_from or "") != paying_account:
            # Only reachable if something upstream redirected the source. Force
            # the manager's choice; PaymentEntry.validate() recomputes the
            # exchange rate and the amounts from these two fields on insert.
            pe.paid_from = paying_account
            pe.paid_from_account_currency = (
                frappe.db.get_value("Account", paying_account, "account_currency")
                or pe.paid_from_account_currency
            )

        account_type = str(
            frappe.db.get_value("Account", paying_account, "account_type") or ""
        ).strip()
        if account_type == "Bank" and not pe.get("reference_no"):
            # reference_no/reference_date are mandatory_depends_on
            # "paid_from_account_type == 'Bank' || paid_to_account_type == 'Bank'".
            # For a cash drawer they must stay empty; for a bank account the
            # insert is refused without them, so the advance's own name is used
            # as the reference rather than inventing a number.
            pe.reference_no = advance.name
            pe.reference_date = pe.get("reference_date") or advance.posting_date or today()

        if not pe.get("mode_of_payment"):
            mode_of_payment = _mode_of_payment_for_account(advance.company, paying_account)
            if mode_of_payment:
                pe.mode_of_payment = mode_of_payment

        pe.flags.ignore_permissions = True
        pe.insert(ignore_permissions=True)
        pe.submit()

    # The document was created while the session was temporarily Administrator,
    # so `owner` records the bypass rather than the person who released the cash.
    # Repoint it at the real approver: this is a payout, and "who paid" must not
    # be an implementation detail of how the permission check was satisfied.
    try:
        frappe.db.set_value("Payment Entry", pe.name, "owner", approver, update_modified=False)
    except Exception:
        _log(frappe.get_traceback(), "employee_advances: could not stamp PE owner")

    return pe


@frappe.whitelist(allow_guest=False)
def approve_employee_advance(name: str) -> Dict[str, Any]:
    """Approve a draft advance, submit it, and pay it out in the same request.

    "Approved" and "paid" are one step on purpose. The manager approves with the
    employee in front of them and hands over cash; an approval that submitted the
    advance but deferred the payout would leave a document saying the employee
    was never paid next to a till that is short.
    """
    _ensure_can_approve()
    _ensure_hrms()

    name = str(name or "").strip()
    if not name:
        frappe.throw(_("Employee Advance name is required."))

    doc = frappe.get_doc(ADVANCE_DOCTYPE, name)
    if int(doc.docstatus or 0) != 0:
        frappe.throw(
            _("Only a draft employee advance can be approved. {0} is {1}.").format(
                name, str(doc.get("status") or "already submitted")
            )
        )

    paying_account = str(doc.get(F_PAYING_ACCOUNT) or "").strip() if _advance_has_field(F_PAYING_ACCOUNT) else ""
    if not paying_account:
        frappe.throw(
            _(
                "Advance {0} has no paying account, so there is nothing to pay it "
                "from. Reject it and file the request again."
            ).format(name)
        )
    company = str(doc.company or "") or _default_company()
    _validate_paying_account(company, paying_account)

    # 1) Stamp the approval before the submit so it is persisted by the same
    #    write, not by a second one that could fail on its own.
    if _advance_has_field(F_APPROVED_BY):
        doc.set(F_APPROVED_BY, frappe.session.user)
    if _advance_has_field(F_APPROVED_ON):
        doc.set(F_APPROVED_ON, now_datetime())

    # 2) advance_account is effectively mandatory at submit.
    if not doc.get("advance_account"):
        doc.advance_account = _resolve_advance_account(company)

    # 3) Submit. ignore_permissions because no JARZ role holds a DocPerm on
    #    Employee Advance — see the module docstring.
    doc.flags.ignore_permissions = True
    doc.submit()

    # 4) Pay. Deliberately NOT wrapped in try/except: if the Payment Entry
    #    cannot be built or submitted, the exception must reach the request
    #    boundary so Frappe rolls the WHOLE transaction back, including the
    #    submit above. Logging and continuing would leave a submitted advance
    #    with paid_amount = 0 and status stuck at "Unpaid" — the books saying
    #    nothing was paid while the manager has already handed over the cash.
    #    That inconsistency is the entire reason this endpoint pays now, so a
    #    half-done approval must not be allowed to survive.
    pe = _build_and_submit_payment_entry(doc, paying_account)

    # 5) Record which Payment Entry did it. db_set because the advance is
    #    submitted by now; the field is allow_on_submit for exactly this write.
    if _advance_has_field(F_PAYMENT_ENTRY):
        doc.db_set(F_PAYMENT_ENTRY, pe.name, update_modified=False)

    doc.reload()
    return {
        "success": True,
        "advance": _serialize_one(doc.as_dict()),
        "payment_entry": pe.name,
    }


@frappe.whitelist(allow_guest=False)
def reject_employee_advance(name: str, reason: str) -> Dict[str, Any]:
    """Turn down a request, recording why.

    A rejected draft is deleted rather than parked in a "Rejected" state:
    ``Employee Advance.status`` is read-only and derived by HRMS from the
    amounts, so there is no such state to move it to, and inventing one would
    mean writing a field HRMS owns.

    The reason is written into ``purpose`` FIRST and only then deleted. That
    ordering is the point: a Comment is deleted along with its document, while
    ``purpose`` travels into the ``Deleted Document`` snapshot, so the record of
    why a manager said no outlives the row.

    An advance that was already approved is cancelled instead of deleted, and one
    that was already PAID is refused outright — see the comment on that branch.
    """
    _ensure_can_approve()
    _ensure_hrms()

    name = str(name or "").strip()
    if not name:
        frappe.throw(_("Employee Advance name is required."))

    reason = str(reason or "").strip()
    if not reason:
        frappe.throw(_("A reason is required to reject an advance request."))

    doc = frappe.get_doc(ADVANCE_DOCTYPE, name)
    docstatus = int(doc.docstatus or 0)

    if docstatus == 2:
        frappe.throw(_("Advance {0} is already cancelled.").format(name))

    stamp = "[REJECTED by {0} on {1}] {2}".format(
        frappe.session.user, now_datetime(), reason
    )

    if docstatus == 0:
        try:
            doc.db_set("purpose", f"{doc.purpose or ''}\n\n{stamp}".strip(), update_modified=False)
        except Exception:
            # Losing the annotation must not block the rejection itself.
            _log(frappe.get_traceback(), f"employee_advances: could not annotate {name}")
        try:
            doc.add_comment("Comment", stamp)
        except Exception:
            _log(frappe.get_traceback(), f"employee_advances: could not comment on {name}")

        frappe.delete_doc(ADVANCE_DOCTYPE, name, ignore_permissions=True)
        return {"success": True, "name": name, "action": "deleted", "reason": reason}

    # docstatus == 1: somebody already approved it. Cancel rather than delete —
    # but only if no money actually moved.
    #
    # The explicit refusal below matters more than it looks. HRMS's
    # ``EmployeeAdvance.on_cancel`` calls ``check_linked_payment_entry()``, which
    # — when ``HR Settings.unlink_payment_on_cancellation_of_employee_advance``
    # is on — QUIETLY UNLINKS the Payment Entry instead of refusing. That would
    # cancel the advance and leave a submitted payout sitting in the ledger with
    # nothing to justify it: cash out of the drawer, no document saying why.
    # Unwinding a paid advance is an accounting reversal, not a "reject" button,
    # so this endpoint declines and names the Payment Entry to reverse.
    paid_by = _linked_payment_entries(name)
    if paid_by:
        frappe.throw(
            _(
                "Advance {0} has already been paid by {1}. Reverse that Payment "
                "Entry first — cancelling the advance on its own would leave the "
                "payout in the ledger with nothing to justify it."
            ).format(name, ", ".join(paid_by))
        )

    try:
        doc.add_comment("Comment", stamp)
    except Exception:
        _log(frappe.get_traceback(), f"employee_advances: could not comment on {name}")

    doc.flags.ignore_permissions = True
    doc.cancel()
    doc.reload()
    return {
        "success": True,
        "name": name,
        "action": "cancelled",
        "reason": reason,
        "advance": _serialize_one(doc.as_dict()),
    }

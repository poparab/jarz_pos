"""Cash custody (عهدة) endpoints.

URL prefix: ``/api/method/jarz_pos.api.cash_custody.<function>``

Who may do what:

* ``MANAGE`` (``ROLES.MANAGER``, the same gate as Cash Transfer) sees every
  holder, issues custody from any branch drawer / cash / bank / wallet
  account, and takes it back into any of them.
* ``HOLDER_ADMIN`` (JARZ Manager, System Manager) adds and disables holders.
* A holder (an enabled ``Jarz Custody Holder`` whose ``user`` is the caller)
  sees their own custody, may draw custody from a drawer of THEIR OWN POS
  profiles, and may return it to ANY enabled branch drawer.

No approval on issue or return, no cap. The custody account can never go
negative: ``services.cash_custody.guard_custody_balance`` refuses any voucher
that would take it below zero, on every path that can spend it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, today

from jarz_pos.api import expenses as _expenses
from jarz_pos.constants import ROLES
from jarz_pos.services import cash_custody
from jarz_pos.utils.posting_datetime import apply_ledger_posting_datetime

MANAGE = set(ROLES.MANAGER)
HOLDER_ADMIN = {ROLES.JARZ_MANAGER, ROLES.SYSTEM_MANAGER}

HOLDER_DOCTYPE = cash_custody.HOLDER_DOCTYPE

DEFAULT_STATEMENT_DAYS = 30


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _roles() -> set:
    return set(frappe.get_roles(frappe.session.user) or [])


def _can_manage() -> bool:
    return bool(_roles() & MANAGE)


def _can_manage_holders() -> bool:
    return bool(_roles() & HOLDER_ADMIN)


def _require_holder_admin() -> None:
    if not _can_manage_holders():
        frappe.throw(_("Not permitted: only JARZ Managers can manage custody holders."), frappe.PermissionError)


def _load_holder(holder: Optional[str]):
    name = str(holder or "").strip()
    if not name:
        frappe.throw(_("Custody holder is required."))
    if not frappe.db.exists(HOLDER_DOCTYPE, name):
        frappe.throw(_("Custody holder {0} not found.").format(name), frappe.DoesNotExistError)
    return frappe.get_doc(HOLDER_DOCTYPE, name)


def _is_own(holder_doc) -> bool:
    return bool(holder_doc.get("user")) and holder_doc.get("user") == frappe.session.user


def _parse_amount(amount: Any) -> float:
    try:
        value = flt(amount)
    except Exception:
        value = 0.0
    if value <= 0:
        frappe.throw(_("Amount must be greater than zero."))
    return value


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(cint(value)) if not isinstance(value, bool) else value


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _holder_stats(accounts: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """``{account: {balance, last_movement}}`` in one query."""
    accounts = [a for a in accounts if a]
    if not accounts:
        return {}
    rows = frappe.db.sql(
        """
        SELECT account,
               COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0) AS balance,
               MAX(posting_date) AS last_movement
        FROM `tabGL Entry`
        WHERE is_cancelled = 0 AND account IN %(accounts)s
        GROUP BY account
        """,
        {"accounts": tuple(accounts)},
        as_dict=True,
    )
    return {
        row.get("account"): {
            "balance": flt(row.get("balance")),
            "last_movement": str(row.get("last_movement")) if row.get("last_movement") else None,
        }
        for row in rows or []
    }


def _serialize_holder(doc, stats: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    account = doc.get("account")
    if stats is None:
        stats = _holder_stats([account] if account else [])
    stat = stats.get(account) or {}
    labels = cash_custody.custody_account_labels(account, doc.get("employee_name"))
    return {
        "name": doc.get("name"),
        "employee": doc.get("employee"),
        "employee_name": doc.get("employee_name"),
        "user": doc.get("user"),
        "company": doc.get("company"),
        "account": account,
        "label_en": labels["label_en"],
        "label_ar": labels["label_ar"],
        "balance": flt(stat.get("balance")),
        "enabled": bool(cint(doc.get("enabled"))),
        "last_movement": stat.get("last_movement"),
    }


def _serialize_holders(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    stats = _holder_stats([r.get("account") for r in rows if r.get("account")])
    return [_serialize_holder(r, stats) for r in rows]


def _serialize_option(src) -> Dict[str, Any]:
    return {
        "account": src.account,
        "label": src.label,
        "label_en": src.label_en or src.label,
        "label_ar": src.label_ar or src.label,
        "category": src.category,
        "balance": flt(src.balance),
        "pos_profile": src.pos_profile or None,
    }


# ---------------------------------------------------------------------------
# Account option lists
# ---------------------------------------------------------------------------


def _without_custody(sources, custody: Dict[str, str]):
    return [s for s in sources if s.account and s.account not in custody]


def _manager_source_accounts(company: str):
    """Every enabled POS-profile drawer plus cash/bank/wallet — never custody."""
    custody = cash_custody.custody_accounts()
    pos = _without_custody(
        _expenses._pos_profile_accounts(company, _expenses._manager_pos_profiles(company)),
        custody,
    )
    excluded = {s.account for s in pos} | set(custody)
    cash = _without_custody(_expenses._cashlike_accounts(company, excluded_accounts=excluded), custody)
    return pos + cash


def _holder_source_accounts(company: str):
    """The drawers of the caller's own POS profiles."""
    custody = cash_custody.custody_accounts()
    return _without_custody(
        _expenses._pos_profile_accounts(company, _expenses._current_user_pos_profile_names()),
        custody,
    )


def _return_accounts(company: str, can_manage: bool):
    """ANY enabled branch drawer; managers also get cash/bank/wallet."""
    custody = cash_custody.custody_accounts()
    pos = _without_custody(
        _expenses._pos_profile_accounts(company, _expenses._manager_pos_profiles(company)),
        custody,
    )
    if not can_manage:
        return pos
    excluded = {s.account for s in pos} | set(custody)
    cash = _without_custody(_expenses._cashlike_accounts(company, excluded_accounts=excluded), custody)
    return pos + cash


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def _validate_counter_account(account: str, holder_doc) -> None:
    if not account:
        frappe.throw(_("Account is required."))
    if account == holder_doc.get("account"):
        frappe.throw(_("The other account must differ from the custody account."))
    row = frappe.db.get_value("Account", account, ["company", "is_group"], as_dict=True)
    if not row:
        frappe.throw(_("Account not found: {0}").format(account))
    if cint(row.get("is_group")):
        frappe.throw(_("Account must be a ledger (not group): {0}").format(account))
    if holder_doc.get("company") and row.get("company") != holder_doc.get("company"):
        frappe.throw(
            _("Account {0} belongs to {1}, but this custody is held in {2}.").format(
                account, row.get("company"), holder_doc.get("company")
            )
        )


def _post_custody_je(
    holder_doc,
    debit_account: str,
    credit_account: str,
    amount: float,
    tag_type: str,
    human: str,
    posting_date: Optional[str] = None,
):
    from jarz_pos.services.delivery_handling import _strip_je_tag_lookalikes, _tag_journal_entry

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = holder_doc.get("company")
    je.posting_date = today()
    # Optional "YYYY-MM-DD HH:MM:SS"; the time lands on custom_jarz_posting_time.
    apply_ledger_posting_datetime(je, posting_date)
    # The key is unique per posting: these entries are not deduplicated, the
    # tag is provenance for the statement's issue/return classification.
    key = f"{holder_doc.get('name')}:{frappe.generate_hash(length=10)}"
    _tag_journal_entry(je, key, tag_type, _strip_je_tag_lookalikes(human) or human)
    je.append(
        "accounts",
        {"account": debit_account, "debit_in_account_currency": amount, "credit_in_account_currency": 0},
    )
    je.append(
        "accounts",
        {"account": credit_account, "credit_in_account_currency": amount, "debit_in_account_currency": 0},
    )
    je.flags.ignore_permissions = True
    je.insert()
    je.submit()
    return je


def _human(prefix: str, employee_name: str, remark: Optional[str]) -> str:
    text = f"{prefix} {employee_name}".strip()
    remark = str(remark or "").strip()
    return f"{text}: {remark}" if remark else text


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_custody_overview() -> Dict[str, Any]:
    """Everything the custody screen needs, scoped to the caller."""
    company = _expenses._default_company()
    can_manage = _can_manage()
    can_manage_holders = _can_manage_holders()
    my_doc = cash_custody.holder_for_user(frappe.session.user)
    my_holder = _serialize_holder(my_doc) if my_doc else None

    if can_manage:
        rows = frappe.get_all(
            HOLDER_DOCTYPE,
            fields=["name", "employee", "employee_name", "user", "company", "account", "enabled"],
            order_by="enabled desc, employee_name asc",
            limit_page_length=0,
        )
        holders = _serialize_holders(rows)
    else:
        holders = [my_holder] if my_holder else []

    if can_manage:
        sources = _manager_source_accounts(company)
    elif my_doc:
        sources = _holder_source_accounts(company)
    else:
        sources = []

    returns = _return_accounts(company, can_manage) if (can_manage or my_doc) else []

    return {
        "success": True,
        "company": company,
        "can_manage": can_manage,
        "can_manage_holders": can_manage_holders,
        "my_holder": my_holder,
        "holders": holders,
        "source_accounts": [_serialize_option(s) for s in sources],
        "return_accounts": [_serialize_option(s) for s in returns],
    }


@frappe.whitelist()
def list_custody_candidates(search: Optional[str] = None) -> Dict[str, Any]:
    """Active employees with a user who are not holders yet (max 50)."""
    _require_holder_admin()
    existing = [
        e for e in (frappe.get_all(HOLDER_DOCTYPE, pluck="employee", limit_page_length=0) or []) if e
    ]
    filters: Dict[str, Any] = {"status": "Active", "user_id": ["is", "set"]}
    if existing:
        filters["name"] = ["not in", existing]
    or_filters = None
    search = str(search or "").strip()
    if search:
        like = f"%{search}%"
        or_filters = [["Employee", "employee_name", "like", like], ["Employee", "name", "like", like]]
    rows = frappe.get_all(
        "Employee",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "employee_name", "user_id"],
        order_by="employee_name asc",
        limit_page_length=50,
    )
    return {
        "success": True,
        "employees": [
            {"employee": r.get("name"), "employee_name": r.get("employee_name"), "user": r.get("user_id")}
            for r in rows or []
        ],
    }


@frappe.whitelist()
def add_custody_holder(employee: str) -> Dict[str, Any]:
    """Make *employee* a custody holder (creates their custody account).

    Re-adding a disabled holder re-enables it rather than failing.
    """
    _require_holder_admin()
    employee = str(employee or "").strip()
    if not employee:
        frappe.throw(_("Employee is required."))

    existing = frappe.db.get_value(HOLDER_DOCTYPE, {"employee": employee}, "name")
    if existing:
        doc = frappe.get_doc(HOLDER_DOCTYPE, existing)
        if cint(doc.enabled):
            frappe.throw(_("{0} already holds custody.").format(doc.employee_name or employee))
        doc.enabled = 1
        doc.flags.ignore_permissions = True
        doc.save()
    else:
        doc = frappe.get_doc({"doctype": HOLDER_DOCTYPE, "employee": employee, "enabled": 1})
        doc.flags.ignore_permissions = True
        doc.insert()
    cash_custody.clear_custody_cache()
    return {"success": True, "holder": _serialize_holder(doc)}


@frappe.whitelist()
def set_custody_holder_enabled(holder: str, enabled: Any) -> Dict[str, Any]:
    """Enable or disable a holder. Disabling is refused while money is held."""
    _require_holder_admin()
    doc = _load_holder(holder)
    doc.enabled = 1 if _truthy(enabled) else 0
    doc.flags.ignore_permissions = True
    doc.save()
    cash_custody.clear_custody_cache()
    return {"success": True, "holder": _serialize_holder(doc)}


@frappe.whitelist()
def issue_custody(
    holder: str,
    from_account: str,
    amount: Any,
    posting_date: Optional[str] = None,
    remark: Optional[str] = None,
) -> Dict[str, Any]:
    """Move *amount* from *from_account* into the holder's custody.

    Dr custody / Cr from_account. A manager may draw on any source account; the
    holder may draw only on a drawer of their own POS profiles.
    """
    doc = _load_holder(holder)
    can_manage = _can_manage()
    own = _is_own(doc)
    if not can_manage and not own:
        frappe.throw(_("Not permitted to issue custody to {0}.").format(doc.employee_name), frappe.PermissionError)
    if not cint(doc.enabled):
        frappe.throw(_("Custody holder {0} is disabled.").format(doc.employee_name))
    if not doc.account:
        frappe.throw(_("Custody holder {0} has no custody account.").format(doc.employee_name))

    value = _parse_amount(amount)
    from_account = str(from_account or "").strip()
    company = doc.company

    allowed = False
    if can_manage and from_account in {s.account for s in _manager_source_accounts(company)}:
        allowed = True
    elif own and from_account in {s.account for s in _holder_source_accounts(company)}:
        allowed = True
    if not allowed:
        frappe.throw(
            _("You cannot issue custody from {0}.").format(from_account or _("an empty account")),
            frappe.PermissionError,
        )
    _validate_counter_account(from_account, doc)

    je = _post_custody_je(
        doc,
        debit_account=doc.account,
        credit_account=from_account,
        amount=value,
        tag_type=cash_custody.CUSTODY_ISSUE_TAG,
        human=_human("Custody issue to", doc.employee_name or doc.employee, remark),
        posting_date=posting_date,
    )
    return {"success": True, "journal_entry": je.name, "holder": _serialize_holder(doc)}


@frappe.whitelist()
def return_custody(
    holder: str,
    to_account: str,
    amount: Any,
    posting_date: Optional[str] = None,
    remark: Optional[str] = None,
) -> Dict[str, Any]:
    """Return *amount* of custody into *to_account* (any branch drawer).

    Dr to_account / Cr custody. Refused beyond the locked balance; the ledger
    guard re-checks the same thing at submit.
    """
    doc = _load_holder(holder)
    can_manage = _can_manage()
    own = _is_own(doc)
    if not can_manage and not own:
        frappe.throw(_("Not permitted to return custody of {0}.").format(doc.employee_name), frappe.PermissionError)
    if not doc.account:
        frappe.throw(_("Custody holder {0} has no custody account.").format(doc.employee_name))

    value = _parse_amount(amount)
    to_account = str(to_account or "").strip()
    company = doc.company

    if to_account not in {s.account for s in _return_accounts(company, can_manage)}:
        frappe.throw(
            _("You cannot return custody into {0}.").format(to_account or _("an empty account")),
            frappe.PermissionError,
        )
    _validate_counter_account(to_account, doc)

    cash_custody.ensure_custody_can_cover(
        doc.account, value, company, for_update=True, holder=doc.name
    )

    je = _post_custody_je(
        doc,
        debit_account=to_account,
        credit_account=doc.account,
        amount=value,
        tag_type=cash_custody.CUSTODY_RETURN_TAG,
        human=_human("Custody return from", doc.employee_name or doc.employee, remark),
        posting_date=posting_date,
    )
    return {"success": True, "journal_entry": je.name, "holder": _serialize_holder(doc)}


# ---------------------------------------------------------------------------
# Statement
# ---------------------------------------------------------------------------


def _je_tags(names: Sequence[str]) -> Dict[str, Optional[str]]:
    """Trusted tag text per Journal Entry (provenance field, legacy remark)."""
    if not names:
        return {}
    from jarz_pos.services.delivery_handling import (
        _je_classification_fields,
        _trusted_je_tag_source,
    )

    rows = frappe.get_all(
        "Journal Entry",
        filters={"name": ["in", list(names)]},
        fields=_je_classification_fields(),
        limit_page_length=0,
    )
    return {row.get("name"): _trusted_je_tag_source(row) for row in rows or []}


def _voucher_kinds(rows: Sequence[Dict[str, Any]]) -> Dict[tuple, str]:
    """``{(voucher_type, voucher_no): kind}`` for every non-transfer voucher."""
    je_names = cash_custody.unique(r.get("voucher_no") for r in rows if r.get("voucher_type") == "Journal Entry")
    pe_names = cash_custody.unique(r.get("voucher_no") for r in rows if r.get("voucher_type") == "Payment Entry")
    kinds: Dict[tuple, str] = {}

    if je_names:
        for name, tag in _je_tags(je_names).items():
            kind = cash_custody.custody_kind_from_tag(tag)
            if kind:
                kinds[("Journal Entry", name)] = kind
        expense_jes = frappe.get_all(
            "Jarz Expense Request",
            filters={"journal_entry": ["in", je_names]},
            pluck="journal_entry",
            limit_page_length=0,
        ) or []
        for name in expense_jes:
            kinds.setdefault(("Journal Entry", name), "expense")

    if pe_names:
        purchase_pes = frappe.get_all(
            "Payment Entry Reference",
            filters={
                "parenttype": "Payment Entry",
                "parent": ["in", pe_names],
                "reference_doctype": "Purchase Invoice",
            },
            pluck="parent",
            limit_page_length=0,
        ) or []
        for name in purchase_pes:
            kinds[("Payment Entry", name)] = "purchase"

    for r in rows:
        if r.get("voucher_type") == "Purchase Invoice":
            kinds[("Purchase Invoice", r.get("voucher_no"))] = "purchase"
    return kinds


def _classify(row: Dict[str, Any], kinds: Dict[tuple, str]) -> str:
    kind = kinds.get((row.get("voucher_type"), row.get("voucher_no")))
    if kind:
        return kind
    if flt(row.get("debit")) > 0:
        return "transfer_in"
    if flt(row.get("credit")) > 0:
        return "transfer_out"
    return "other"


def _first_counter(against: Optional[str]) -> Optional[str]:
    for part in str(against or "").split(","):
        part = part.strip()
        if part:
            return part
    return None


def build_statement_entries(
    rows: Sequence[Dict[str, Any]],
    opening_balance: float,
    kinds: Dict[tuple, str],
    counter_labels: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Chronological rows -> entries with a running balance (chronological)."""
    counter_labels = counter_labels or {}
    running = flt(opening_balance)
    out: List[Dict[str, Any]] = []
    for row in rows:
        debit = flt(row.get("debit"))
        credit = flt(row.get("credit"))
        running = flt(running + debit - credit, 2)
        counter = _first_counter(row.get("against"))
        label = (counter_labels.get(counter) or {}).get("label") if counter else None
        out.append(
            {
                "posting_date": str(row.get("posting_date")) if row.get("posting_date") else None,
                "voucher_type": row.get("voucher_type"),
                "voucher_no": row.get("voucher_no"),
                "kind": _classify(row, kinds),
                "debit": debit,
                "credit": credit,
                "balance": running,
                "remark": cash_custody.strip_tag(row.get("remarks")),
                "counter_account": counter,
                "counter_label": label or counter,
            }
        )
    return out


@frappe.whitelist()
def get_custody_statement(
    holder: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: Any = 200,
) -> Dict[str, Any]:
    """Custody ledger for a window (default: the last 30 days).

    ``entries`` are NEWEST FIRST; each carries the running balance after that
    line. At most ``limit`` (max 1000) of the most recent lines are returned;
    ``opening_balance`` / ``closing_balance`` always cover the whole window.
    """
    doc = _load_holder(holder)
    if not _can_manage() and not _is_own(doc):
        frappe.throw(_("Not permitted to view the custody of {0}.").format(doc.employee_name), frappe.PermissionError)

    end = getdate(to_date) if to_date else getdate(today())
    start = getdate(from_date) if from_date else getdate(add_days(end, -DEFAULT_STATEMENT_DAYS))
    if start > end:
        frappe.throw(_("From date must not be after To date."))
    limit = max(1, min(cint(limit) or 200, 1000))

    account = doc.account
    opening = 0.0
    rows: List[Dict[str, Any]] = []
    if account:
        opening_rows = frappe.db.sql(
            """
            SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0)
            FROM `tabGL Entry`
            WHERE account = %s AND is_cancelled = 0 AND posting_date < %s
            """,
            (account, start),
        )
        opening = flt(opening_rows[0][0]) if opening_rows else 0.0
        rows = frappe.db.sql(
            """
            SELECT posting_date, voucher_type, voucher_no, debit, credit, remarks, against
            FROM `tabGL Entry`
            WHERE account = %s AND is_cancelled = 0 AND posting_date BETWEEN %s AND %s
            ORDER BY posting_date ASC, creation ASC, name ASC
            """,
            (account, start, end),
            as_dict=True,
        ) or []

    kinds = _voucher_kinds(rows)
    counters = cash_custody.unique(_first_counter(r.get("against")) for r in rows)
    counter_labels = _expenses._account_label_map(counters) if counters else {}
    entries = build_statement_entries(rows, opening, kinds, counter_labels)
    closing = entries[-1]["balance"] if entries else flt(opening, 2)
    entries = list(reversed(entries))[:limit]

    return {
        "success": True,
        "holder": _serialize_holder(doc),
        "from_date": str(start),
        "to_date": str(end),
        "opening_balance": flt(opening, 2),
        "closing_balance": closing,
        "entries": entries,
    }

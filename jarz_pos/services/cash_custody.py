"""Cash custody (عهدة): company cash held by a named employee.

One leaf ``Account`` per holder (``account_type = "Cash"``), grouped under
``Staff Custody - <abbr>`` beneath the company's ``Cash In Hand`` group. The
holder record (``Jarz Custody Holder``) is the only thing that marks a Cash
account as custody; everything else — the expense flow, purchases, cash
transfers — treats it as an ordinary cash account.

The one rule this module exists to enforce is that a custody account can
never go negative. It is enforced at the ledger boundary, on
``before_submit`` of every voucher type that can credit a cash account
(Journal Entry, Payment Entry, Purchase Invoice paid on the document), rather
than in each endpoint that happens to spend from custody: a manager approving
an expense days after it was filed, a Desk user posting a Journal Entry by
hand and the mobile app all go through the same check.

The check is a MONEY GUARD, so it fails closed. It locks the holder row and
then reads the balance with a LOCKING read: MariaDB runs REPEATABLE READ and a
plain SELECT after a FOR UPDATE still answers from the snapshot taken before
the concurrent spender committed, which is exactly how two simultaneous
expenses would each see the whole balance.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional, Tuple

import frappe
from frappe import _
from frappe.utils import cint, flt

from jarz_pos.constants import ACCOUNTS

HOLDER_DOCTYPE = "Jarz Custody Holder"

#: ``account_name`` of the group every custody leaf lives under.
CUSTODY_GROUP_NAME = "Staff Custody"

#: Journal Entry tag types (``[JARZ-JE:<type>:<key>]``). Upper-case with
#: underscores like every other tag type in this app; ``_je_dedup_tag`` refuses
#: ``[``, ``]`` and ``:`` in a type.
CUSTODY_ISSUE_TAG = "CUSTODY_ISSUE"
CUSTODY_RETURN_TAG = "CUSTODY_RETURN"

#: Rounding tolerance for every money comparison here.
EPSILON = 0.005

_ACCOUNTS_CACHE_FLAG = "jarz_custody_accounts_cache"
_TABLE_FLAG = "jarz_custody_table_exists"
_LABEL_COLUMNS_FLAG = "jarz_custody_account_label_columns"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def custody_label_en(employee_name: Optional[str]) -> str:
    return f"Custody - {(employee_name or '').strip()}".strip()


def custody_label_ar(employee_name: Optional[str]) -> str:
    return f"عهدة - {(employee_name or '').strip()}".strip()


def _label_columns() -> Tuple[bool, bool]:
    """Whether Account carries the bilingual label columns on this site."""
    cached = getattr(frappe.flags, _LABEL_COLUMNS_FLAG, None)
    if isinstance(cached, tuple):
        return cached
    has_en = has_ar = False
    try:
        has_en = bool(frappe.db.has_column("Account", "custom_account_name_en"))
    except Exception:
        pass
    try:
        has_ar = bool(frappe.db.has_column("Account", "custom_account_name_ar"))
    except Exception:
        pass
    cached = (has_en, has_ar)
    try:
        setattr(frappe.flags, _LABEL_COLUMNS_FLAG, cached)
    except Exception:
        pass
    return cached


def custody_account_labels(account: Optional[str], employee_name: Optional[str]) -> Dict[str, str]:
    """``{label_en, label_ar}`` for a custody account.

    The account's own bilingual columns win when they are filled; otherwise the
    canonical ``Custody - <name>`` / ``عهدة - <name>`` pair is returned, so the
    Arabic UI never falls back to the English account name.
    """
    en = custody_label_en(employee_name)
    ar = custody_label_ar(employee_name)
    has_en, has_ar = _label_columns()
    fields = []
    if has_en:
        fields.append("custom_account_name_en")
    if has_ar:
        fields.append("custom_account_name_ar")
    if account and fields:
        row = frappe.db.get_value("Account", account, fields, as_dict=True) or {}
        en = str(row.get("custom_account_name_en") or "").strip() or en
        ar = str(row.get("custom_account_name_ar") or "").strip() or ar
    return {"label_en": en, "label_ar": ar}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _holder_table_exists() -> bool:
    """False until the DocType has been migrated onto this site.

    Code routinely reaches a server before ``bench migrate`` does, and the
    guard below runs on EVERY Journal Entry submit — so a missing table must
    mean "there are no custody accounts", not a failed submit.
    """
    cached = getattr(frappe.flags, _TABLE_FLAG, None)
    if isinstance(cached, bool):
        return cached
    exists = bool(frappe.db.table_exists(HOLDER_DOCTYPE))
    try:
        setattr(frappe.flags, _TABLE_FLAG, exists)
    except Exception:
        pass
    return exists


def _accounts_cache() -> Dict[str, Dict[str, str]]:
    cache = getattr(frappe.flags, _ACCOUNTS_CACHE_FLAG, None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            setattr(frappe.flags, _ACCOUNTS_CACHE_FLAG, cache)
        except Exception:
            pass
    return cache


def clear_custody_cache() -> None:
    """Drop the per-request holder cache (called whenever a holder changes)."""
    try:
        setattr(frappe.flags, _ACCOUNTS_CACHE_FLAG, {})
        setattr(frappe.flags, _TABLE_FLAG, None)
    except Exception:
        pass


def custody_accounts(company: Optional[str] = None) -> Dict[str, str]:
    """``{account: holder name}`` for every holder, enabled or not.

    Disabled holders are included on purpose: a disabled holder's account is
    still a custody account, and the negative-balance guard must keep holding
    for it. Cached for the life of the request in ``frappe.flags``.
    """
    key = company or "*"
    cache = _accounts_cache()
    if key in cache:
        return dict(cache[key])

    result: Dict[str, str] = {}
    if _holder_table_exists():
        filters: Dict[str, Any] = {"account": ["is", "set"]}
        if company:
            filters["company"] = company
        rows = frappe.get_all(
            HOLDER_DOCTYPE,
            filters=filters,
            fields=["name", "account"],
            limit_page_length=0,
        )
        for row in rows or []:
            account = str(row.get("account") or "").strip()
            if account:
                result[account] = row.get("name")
    cache[key] = result
    return dict(result)


def is_custody_account(account: Optional[str]) -> bool:
    return bool(account) and account in custody_accounts()


def holder_for_user(user: Optional[str]):
    """The ENABLED holder document for *user*, or None."""
    if not user or user == "Guest":
        return None
    if not _holder_table_exists():
        return None
    name = frappe.db.get_value(HOLDER_DOCTYPE, {"user": user, "enabled": 1}, "name")
    if not name:
        return None
    return frappe.get_doc(HOLDER_DOCTYPE, name)


def holder_employee_name(holder: Optional[str]) -> str:
    if not holder:
        return ""
    return str(frappe.db.get_value(HOLDER_DOCTYPE, holder, "employee_name") or holder)


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


def custody_balance(account: str, company: Optional[str] = None, for_update: bool = False) -> float:
    """Debit-minus-credit of *account* over every live GL Entry.

    ``for_update=True`` is for callers that DECIDE something about money from
    the answer. It first locks the holder row — so two spenders of the same
    custody serialise — and then reads the sum with ``FOR UPDATE`` too. Both
    halves are needed: the row lock orders the writers, and only a locking
    read sees what the writer ahead of us committed (a plain read answers from
    this transaction's older snapshot). ``tabGL Entry.account`` is indexed, so
    the locking read touches this account's rows only.
    """
    if not account:
        return 0.0
    conditions = "account = %s AND is_cancelled = 0"
    params: list = [account]
    if company:
        conditions += " AND company = %s"
        params.append(company)

    if for_update:
        frappe.db.sql(
            "SELECT name FROM `tabJarz Custody Holder` WHERE account = %s FOR UPDATE",
            (account,),
        )
        rows = frappe.db.sql(
            f"SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0) FROM `tabGL Entry` "
            f"WHERE {conditions} FOR UPDATE",
            tuple(params),
        )
    else:
        rows = frappe.db.sql(
            f"SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0) FROM `tabGL Entry` "
            f"WHERE {conditions}",
            tuple(params),
        )
    if not rows:
        return 0.0
    return flt(rows[0][0])


def _fmt(value: float) -> str:
    return f"{flt(value):,.2f}"


def overdraw_message(employee_name: str, balance: float, amount: float) -> str:
    return _("Custody {0} holds {1}; this entry would take {2} out of it.").format(
        employee_name, _fmt(balance), _fmt(amount)
    )


def ensure_custody_can_cover(
    account: str,
    amount: float,
    company: Optional[str] = None,
    for_update: bool = False,
    holder: Optional[str] = None,
) -> float:
    """Throw the standard overdraw message if *account* cannot pay *amount*.

    Returns the balance that was read.
    """
    balance = custody_balance(account, company, for_update=for_update)
    if balance - flt(amount) < -EPSILON:
        if holder is None:
            holder = custody_accounts().get(account)
        frappe.throw(overdraw_message(holder_employee_name(holder) or account, balance, amount))
    return balance


# ---------------------------------------------------------------------------
# The ledger guard (doc_events)
# ---------------------------------------------------------------------------


def _add(out: Dict[str, float], account: Any, amount: float) -> None:
    account = str(account or "").strip()
    if not account:
        return
    out[account] = out.get(account, 0.0) + flt(amount)


def net_credits(doc) -> Dict[str, float]:
    """Net CREDIT *doc* puts on each account it touches (debits offset)."""
    out: Dict[str, float] = {}
    doctype = doc.get("doctype") if isinstance(doc, dict) else getattr(doc, "doctype", None)

    if doctype == "Journal Entry":
        for row in doc.get("accounts") or []:
            _add(
                out,
                row.get("account"),
                flt(row.get("credit_in_account_currency")) - flt(row.get("debit_in_account_currency")),
            )
    elif doctype == "Payment Entry":
        payment_type = doc.get("payment_type")
        if payment_type in ("Pay", "Internal Transfer"):
            _add(out, doc.get("paid_from"), flt(doc.get("paid_amount")))
        # paid_to is always the DEBITED side; on a Pay it is the party
        # account, which is never custody, so applying it unconditionally is
        # harmless and keeps an Internal Transfer between custody accounts net.
        _add(out, doc.get("paid_to"), -flt(doc.get("received_amount")))
    elif doctype == "Purchase Invoice":
        if cint(doc.get("is_paid")):
            paid = flt(doc.get("base_paid_amount")) or flt(doc.get("paid_amount"))
            _add(out, doc.get("cash_bank_account"), paid)
    return out


def _check(doc, sign: int) -> None:
    moves = net_credits(doc)
    # sign=+1: submitting, the voucher's net credit leaves the account.
    # sign=-1: cancelling, the voucher's net DEBIT is taken back out.
    outflows = {acc: amt * sign for acc, amt in moves.items() if amt * sign > EPSILON}
    if not outflows:
        return
    holders = custody_accounts()
    involved = sorted(acc for acc in outflows if acc in holders)
    if not involved:
        return
    company = doc.get("company")
    # Sorted, so two vouchers touching the same pair of custody accounts
    # always lock them in the same order.
    for account in involved:
        balance = custody_balance(account, company, for_update=True)
        if balance - outflows[account] < -EPSILON:
            frappe.throw(
                overdraw_message(
                    holder_employee_name(holders[account]) or account,
                    balance,
                    outflows[account],
                ),
                title=_("Custody balance"),
            )


def guard_custody_balance(doc, method=None) -> None:
    """``before_submit`` on Journal Entry / Payment Entry / Purchase Invoice.

    Refuses a voucher that would take a custody account below zero. Cheap when
    no custody account is credited: the voucher's own rows are summed first
    and the function returns before any query that could lock. Exceptions
    propagate on purpose — this is a money guard and must fail closed.
    """
    _check(doc, +1)


def verify_custody_after_posting(doc, method=None) -> None:
    """``on_submit`` / ``on_cancel`` backstop, read from the voucher's own GL.

    ``guard_custody_balance`` reads only a voucher's main cash line. A Payment
    Entry deduction, a tax "Deduct" row or a write-off can credit an account
    too, so after the ledger is written this re-reads every custody account
    the voucher actually touched and refuses (rolling the whole voucher back)
    if any is now below zero. Returns before any lock when no custody account
    is involved.
    """
    holders = custody_accounts()
    if not holders:
        return
    touched = frappe.db.sql(
        "SELECT DISTINCT account FROM `tabGL Entry` WHERE voucher_type = %s AND voucher_no = %s",
        (doc.get("doctype"), doc.get("name")),
    )
    involved = sorted({row[0] for row in touched or [] if row[0] in holders})
    company = doc.get("company")
    for account in involved:
        balance = custody_balance(account, company, for_update=True)
        if balance < -EPSILON:
            frappe.throw(
                _("Custody {0} would be left at {1}. A custody account cannot go below zero.").format(
                    holder_employee_name(holders[account]) or account, _fmt(balance)
                ),
                title=_("Custody balance"),
            )


def guard_custody_balance_on_cancel(doc, method=None) -> None:
    """``before_cancel`` twin: cancelling a voucher that PUT money into custody
    takes it back out, and must not leave the account negative either (issue
    100, spend 80, cancel the issue -> -80 without this)."""
    _check(doc, -1)


# ---------------------------------------------------------------------------
# Account provisioning
# ---------------------------------------------------------------------------


def _company_currency(company: str) -> Optional[str]:
    return frappe.db.get_value("Company", company, "default_currency")


def custody_parent_group(company: str) -> str:
    """``Staff Custody - <abbr>``, created on demand under Cash In Hand."""
    existing = frappe.db.get_value(
        "Account",
        {"company": company, "is_group": 1, "account_name": CUSTODY_GROUP_NAME},
        "name",
    )
    if existing:
        return existing

    parent = frappe.db.get_value(
        "Account",
        {"company": company, "is_group": 1, "account_name": ACCOUNTS.CASH_IN_HAND},
        "name",
    )
    if not parent:
        default_cash = frappe.db.get_value("Company", company, "default_cash_account")
        if default_cash:
            parent = frappe.db.get_value("Account", default_cash, "parent_account")
    if not parent:
        frappe.throw(
            _("Cannot create custody accounts for {0}: no '{1}' group and no default cash account.").format(
                company, ACCOUNTS.CASH_IN_HAND
            )
        )

    doc = frappe.get_doc(
        {
            "doctype": "Account",
            "account_name": CUSTODY_GROUP_NAME,
            "parent_account": parent,
            "company": company,
            "is_group": 1,
            "root_type": "Asset",
            "report_type": "Balance Sheet",
            "account_currency": _company_currency(company),
        }
    )
    doc.flags.ignore_permissions = True
    try:
        doc.insert()
    except frappe.DuplicateEntryError:
        # Two holders added at the same moment: the other request created it.
        frappe.clear_messages()
        existing = frappe.db.get_value(
            "Account",
            {"company": company, "is_group": 1, "account_name": CUSTODY_GROUP_NAME},
            "name",
        )
        if not existing:
            raise
        return existing
    return doc.name


def _account_held_by_other(account: str, holder_name: Optional[str]) -> bool:
    other = frappe.db.get_value(HOLDER_DOCTYPE, {"account": account}, "name") if _holder_table_exists() else None
    return bool(other) and other != holder_name


def ensure_custody_account(
    employee: str,
    employee_name: Optional[str],
    company: str,
    holder_name: Optional[str] = None,
) -> str:
    """Return the custody leaf for *employee*, creating it if needed.

    Idempotent: a leaf ``Custody - <name>`` (or ``Custody - <name> (<id>)``)
    that is a Cash account of this company and belongs to no OTHER holder is
    reused — which is what makes removing and re-adding a holder safe.
    """
    display = (employee_name or employee or "").strip()
    base = custody_label_en(display)
    candidates = [base, f"{base} ({employee})"]
    parent = None

    for account_name in candidates:
        existing = frappe.db.get_value(
            "Account",
            {"company": company, "account_name": account_name},
            ["name", "is_group", "account_type"],
            as_dict=True,
        )
        if existing:
            if (
                not cint(existing.get("is_group"))
                and (existing.get("account_type") or "") == "Cash"
                and not _account_held_by_other(existing.get("name"), holder_name)
            ):
                return existing.get("name")
            continue

        if parent is None:
            parent = custody_parent_group(company)
        payload: Dict[str, Any] = {
            "doctype": "Account",
            "account_name": account_name,
            "parent_account": parent,
            "company": company,
            "is_group": 0,
            "root_type": "Asset",
            "report_type": "Balance Sheet",
            "account_type": "Cash",
            "account_currency": _company_currency(company),
        }
        has_en, has_ar = _label_columns()
        if has_en:
            payload["custom_account_name_en"] = custody_label_en(display)
        if has_ar:
            payload["custom_account_name_ar"] = custody_label_ar(display)
        doc = frappe.get_doc(payload)
        doc.flags.ignore_permissions = True
        doc.insert()
        return doc.name

    frappe.throw(
        _("Could not allocate a custody account for {0}: both '{1}' and '{2}' are taken.").format(
            employee, candidates[0], candidates[1]
        )
    )
    return ""


def validate_custody_account(account: str, company: Optional[str]) -> None:
    row = frappe.db.get_value(
        "Account", account, ["company", "is_group", "account_type"], as_dict=True
    )
    if not row:
        frappe.throw(_("Account {0} does not exist.").format(account))
    if cint(row.get("is_group")):
        frappe.throw(_("Custody account {0} must be a ledger, not a group.").format(account))
    if company and row.get("company") != company:
        frappe.throw(_("Custody account {0} belongs to another company.").format(account))
    if (row.get("account_type") or "") != "Cash":
        frappe.throw(_("Custody account {0} must be of type Cash.").format(account))
    # Only a ledger under Staff Custody may be a custody. Anything else — a
    # branch drawer, the main safe — would vanish from every issue/return
    # list and be fenced by the negative-balance guard.
    parent = frappe.db.get_value("Account", account, "parent_account")
    parent_name = frappe.db.get_value("Account", parent, "account_name") if parent else None
    if parent_name != CUSTODY_GROUP_NAME:
        frappe.throw(
            _("Custody account {0} must sit under the '{1}' group.").format(account, CUSTODY_GROUP_NAME)
        )


def account_has_gl_entries(account: str) -> bool:
    return bool(frappe.db.exists("GL Entry", {"account": account}))


# ---------------------------------------------------------------------------
# Remark helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"\s*\[JARZ-JE:[^\[\]]*\]")


def strip_tag(text: Optional[str]) -> str:
    """Remove ``[JARZ-JE:...]`` tags from a remark for display."""
    return _TAG_RE.sub("", str(text or "")).strip()


def custody_kind_from_tag(tag_text: Optional[str]) -> Optional[str]:
    text = str(tag_text or "")
    if f"[JARZ-JE:{CUSTODY_ISSUE_TAG}:" in text:
        return "issue"
    if f"[JARZ-JE:{CUSTODY_RETURN_TAG}:" in text:
        return "return"
    return None


def unique(values: Iterable[Any]) -> list:
    seen: set = set()
    out: list = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import frappe
from frappe import _
from frappe.utils import cint, getdate
from jarz_pos.constants import ROLES


def _ensure_manager_access() -> None:
    roles = set(frappe.get_roles())
    allowed = ROLES.MANAGER
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _resolve_named_account(company: str, label: str) -> Optional[str]:
    """Find an Account name for a given human label under a company.

    Strategy:
      1) account_name == label
      2) name == label (rare but possible)
      3) f"{label} - {abbr}" exact match by name
      4) fuzzy: account_name LIKE "%label%" limited to leaf accounts
    Returns the Account.name or None.
    """
    # 1) account_name exact
    acc = frappe.db.get_value("Account", {"company": company, "is_group": 0, "account_name": label}, "name")
    if acc:
        return acc
    # 2) name exact
    if frappe.db.exists("Account", {"company": company, "is_group": 0, "name": label}):
        return label
    # 3) name with company abbr
    abbr = frappe.db.get_value("Company", company, "abbr")
    if abbr:
        candidate = f"{label} - {abbr}"
        if frappe.db.exists("Account", {"company": company, "is_group": 0, "name": candidate}):
            return candidate
    # 4) fuzzy by account_name LIKE
    try:
        like = f"%{label}%"
        rows = frappe.get_all(
            "Account",
            filters={"company": company, "is_group": 0},
            or_filters=[["Account", "account_name", "like", like]],
            fields=["name"],
            limit=1,
        )
        if rows:
            return rows[0]["name"]
    except Exception:
        pass
    return None


def _get_cashlike_accounts(company: str) -> List[Dict[str, Any]]:
    """Return core cash/bank/mobile accounts for the company.

    Strategy:
      - Fetch Accounts with account_type IN ("Cash", "Bank") or names matching common mobile wallet keywords.
    """
    accounts: List[Dict[str, Any]] = []
    # Core cash and bank by account_type
    rows = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0, "account_type": ["in", ["Cash", "Bank"]]},
        fields=["name", "account_name", "account_type", "company"],
        order_by="account_name asc",
    )
    # Tag categories
    for r in rows:
        at = (r.get("account_type") or "").lower()
        if at == "cash":
            r["category"] = "cash"
        elif at == "bank":
            r["category"] = "bank"
        accounts.append(r)

    # Heuristic: mobile wallet accounts by name contains keywords
    mobile_rows = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0},
        or_filters=[
            ["Account", "account_name", "like", "%Mobile%"],
            ["Account", "account_name", "like", "%Wallet%"],
            ["Account", "name", "like", "%Mobile%"],
            ["Account", "name", "like", "%Wallet%"],
        ],
        fields=["name", "account_name", "account_type", "company"],
        order_by="account_name asc",
    )
    # Deduplicate by name
    seen = {a["name"] for a in accounts}
    for r in mobile_rows:
        if r["name"] not in seen:
            r["category"] = "mobile"
            accounts.append(r)
            seen.add(r["name"])
    return accounts


def _get_pos_profile_accounts(company: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    filters: Dict[str, Any] = {"company": company}
    try:
        if frappe.db.has_column("POS Profile", "disabled"):
            filters["disabled"] = 0
    except Exception:
        pass
    rows = frappe.get_all("POS Profile", filters=filters, fields=["name"])
    # Expect accounts named like POS Profile names
    for r in rows:
        name_label = r["name"]
        acc_name = _resolve_named_account(company, name_label)
        if acc_name:
            out.append({"name": acc_name, "account_name": name_label, "account_type": None, "company": company, "category": "pos_profile"})
    return out


def _get_sales_partner_accounts(company: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    # Some setups may not have a dedicated 'disabled' column on Sales Partner
    filters: Dict[str, Any] = {}
    try:
        if frappe.db.has_column("Sales Partner", "disabled"):
            filters["disabled"] = 0
    except Exception:
        pass
    rows = frappe.get_all("Sales Partner", filters=filters, fields=["name"])
    for r in rows:
        name_label = r["name"]
        acc_name = _resolve_named_account(company, name_label)
        if acc_name:
            out.append({"name": acc_name, "account_name": name_label, "account_type": None, "company": company, "category": "sales_partner"})
    return out


def _get_balance_on(account: str, date: Optional[str] = None) -> float:
    # ERPNext helper report util
    from erpnext.accounts.utils import get_balance_on

    try:
        bal = get_balance_on(account, date=date)
    except Exception:
        # Fallback to 0 on any error
        bal = 0.0
    return float(bal or 0)


@frappe.whitelist()
def list_accounts(company: Optional[str] = None, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all relevant cash transfer accounts with current balance.

    Includes:
      - Cash, Bank, Mobile Wallet accounts (by type and name heuristic)
      - Accounts named exactly like POS Profiles
      - Accounts named exactly like Sales Partners
    """
    _ensure_manager_access()
    if not company:
        company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
    if not company:
        frappe.throw(_("Company is required"))

    accs: List[Dict[str, Any]] = []
    accs.extend(_get_cashlike_accounts(company))
    accs.extend(_get_pos_profile_accounts(company))
    accs.extend(_get_sales_partner_accounts(company))

    # De-duplicate by account name
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for a in accs:
        name = a["name"]
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "account": name,
            "label": a.get("account_name") or name,
            "company": a.get("company"),
            "type": a.get("account_type"),
            "category": a.get("category") or "other",
            "balance": _get_balance_on(name, as_of),
        })
    # Sort for stable UI: Cash/Bank first, then others alpha by label
    def _sort_key(x: Dict[str, Any]):
        cat = (x.get("category") or "other").lower()
        cat_priority = {
            "cash": 0,
            "bank": 1,
            "mobile": 2,
            "pos_profile": 3,
            "sales_partner": 4,
        }.get(cat, 9)
        return (cat_priority, (x.get("label") or x.get("account") or ""))
    out.sort(key=_sort_key)
    return out


@frappe.whitelist()
def submit_transfer(from_account: str, to_account: str, amount: float, posting_date: Optional[str] = None, remark: Optional[str] = None) -> Dict[str, Any]:
    """Create a Journal Entry to move funds between two asset accounts.

    Both accounts must be leaf accounts and (typically) of Asset type; we will not enforce root type here but ensure they are not the same.
    """
    _ensure_manager_access()
    try:
        amount = float(amount)
    except Exception:
        frappe.throw(_("Invalid amount"))
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero"))
    if not from_account or not to_account:
        frappe.throw(_("Both from_account and to_account are required"))
    if from_account == to_account:
        frappe.throw(_("From and To accounts must be different"))

    # Validate accounts exist and are leaf
    for acc in (from_account, to_account):
        exists = frappe.db.exists("Account", acc)
        if not exists:
            frappe.throw(_("Account not found: {0}").format(acc))
        is_group = frappe.db.get_value("Account", acc, "is_group")
        if int(is_group or 0) == 1:
            frappe.throw(_("Account must be a ledger (not group): {0}").format(acc))
    # Ensure both accounts belong to the same company
    company_from = frappe.db.get_value("Account", from_account, "company")
    company_to = frappe.db.get_value("Account", to_account, "company")
    if company_from and company_to and company_from != company_to:
        frappe.throw(_("From and To accounts must be in the same Company: {0} vs {1}").format(company_from, company_to))

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    # Set company from from_account for consistency
    try:
        je.company = company_from or frappe.defaults.get_user_default("Company")
    except Exception:
        pass
    # Ensure posting_date is set (DocType may require it)
    if posting_date:
        je.posting_date = posting_date
    else:
        from frappe.utils import today
        je.posting_date = today()
    je.set_posting_time = 1
    if remark:
        je.user_remark = remark

    # Credit from_account, Debit to_account
    je.append("accounts", {"account": from_account, "credit_in_account_currency": amount})
    je.append("accounts", {"account": to_account, "debit_in_account_currency": amount})

    je.flags.ignore_permissions = True
    je.insert()
    je.flags.ignore_permissions = True
    je.submit()
    frappe.db.commit()

    return {"ok": True, "journal_entry": je.name}


def _transferable_account_names(company: str) -> Set[str]:
    """Every account this module can move money between, by name.

    Same membership as :func:`list_accounts` exposes, minus the per-account
    balance lookups — the history only needs to recognise an account, not
    price it, and ``get_balance_on`` is one query per account.
    """
    names: Set[str] = set()
    for group in (
        _get_cashlike_accounts(company),
        _get_pos_profile_accounts(company),
        _get_sales_partner_accounts(company),
    ):
        for row in group:
            if row.get("name"):
                names.add(row["name"])
    return names


@frappe.whitelist()
def list_transfers(
    limit: Any = 30,
    page: Any = 0,
    company: Optional[str] = None,
    account: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """Return submitted cash transfers — the history view.

    :func:`submit_transfer` writes a plain Journal Entry with no marker on it,
    so there is nothing to filter on directly.  A transfer is instead
    recognised by its *shape*: a submitted two-line Journal Entry whose both
    lines sit on accounts this module can transfer between.  That definition
    is retroactive — it finds the transfers posted before this endpoint
    existed — and it deliberately also matches an equivalent entry made by
    hand in Desk, which is a cash transfer whoever typed it.

    ``account`` matches either side.  ``search`` matches the entry name or the
    remark.
    """
    _ensure_manager_access()
    limit = max(1, min(cint(limit) or 30, 200))
    start = max(0, cint(page)) * limit

    if not company:
        company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
            "Global Defaults", "default_company"
        )
    if not company:
        frappe.throw(_("Company is required"))

    empty = {"transfers": [], "total": 0}
    transferable = _transferable_account_names(company)
    if not transferable:
        return empty

    # Child-first: only entries that touch a transferable account can qualify,
    # and that is a far smaller set than "every Journal Entry".
    candidates = {
        row["parent"]
        for row in frappe.get_all(
            "Journal Entry Account",
            filters={
                "parenttype": "Journal Entry",
                "docstatus": 1,
                "account": ["in", sorted(transferable)],
            },
            fields=["parent"],
            limit_page_length=0,
        )
    }
    if not candidates:
        return empty

    # Now the shape test, which needs *every* row of each candidate — a shift
    # discrepancy entry also touches a cash account, but its other leg is Cash
    # Over/Short, so it is not a transfer.
    rows_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for row in frappe.get_all(
        "Journal Entry Account",
        filters={"parent": ["in", sorted(candidates)], "parenttype": "Journal Entry"},
        fields=[
            "parent", "account", "debit_in_account_currency",
            "credit_in_account_currency", "idx",
        ],
        order_by="parent asc, idx asc",
        limit_page_length=0,
    ):
        rows_by_parent.setdefault(row["parent"], []).append(row)

    legs_by_parent: Dict[str, Dict[str, Any]] = {}
    for parent, rows in rows_by_parent.items():
        if len(rows) != 2:
            continue
        if any((r.get("account") or "") not in transferable for r in rows):
            continue
        debits = [r for r in rows if float(r.get("debit_in_account_currency") or 0) > 0]
        credits = [r for r in rows if float(r.get("credit_in_account_currency") or 0) > 0]
        if len(debits) != 1 or len(credits) != 1:
            continue
        if account and account not in (debits[0]["account"], credits[0]["account"]):
            continue
        legs_by_parent[parent] = {
            "from_account": credits[0]["account"],
            "to_account": debits[0]["account"],
            "amount": float(debits[0].get("debit_in_account_currency") or 0),
        }

    if not legs_by_parent:
        return empty

    filters: Dict[str, Any] = {
        "name": ["in", sorted(legs_by_parent)],
        "docstatus": 1,
        "company": company,
    }
    if from_date and to_date:
        filters["posting_date"] = ["between", [getdate(from_date), getdate(to_date)]]
    elif from_date:
        filters["posting_date"] = [">=", getdate(from_date)]
    elif to_date:
        filters["posting_date"] = ["<=", getdate(to_date)]

    or_filters: List[Any] = []
    search = (search or "").strip()
    if search:
        like = f"%{search}%"
        or_filters = [["name", "like", like], ["user_remark", "like", like]]

    fields = [
        "name", "posting_date", "total_debit", "user_remark",
        "owner", "creation", "voucher_type",
    ]
    entries = frappe.get_all(
        "Journal Entry",
        filters=filters,
        or_filters=or_filters,
        fields=fields,
        order_by="posting_date desc, creation desc",
        limit_page_length=limit,
        limit_start=start,
    )
    # Counting through the same filters, not `len(legs_by_parent)` — the client
    # pages off this number and a filtered count that ignored the filters would
    # make "load more" ask for rows that are not there.
    total = len(
        frappe.get_all(
            "Journal Entry",
            filters=filters,
            or_filters=or_filters,
            fields=["name"],
            limit_page_length=0,
        )
    )

    owners = {e["owner"] for e in entries if e.get("owner")}
    full_names = {
        row["name"]: row.get("full_name") or row["name"]
        for row in frappe.get_all(
            "User", filters={"name": ["in", list(owners)]}, fields=["name", "full_name"]
        )
    } if owners else {}

    labels = {
        row["name"]: row.get("account_name") or row["name"]
        for row in frappe.get_all(
            "Account",
            filters={"name": ["in", sorted(transferable)]},
            fields=["name", "account_name"],
            limit_page_length=0,
        )
    }

    out: List[Dict[str, Any]] = []
    for entry in entries:
        legs = legs_by_parent[entry["name"]]
        out.append({
            **entry,
            "owner_name": full_names.get(entry.get("owner"), entry.get("owner")),
            "from_account": legs["from_account"],
            "to_account": legs["to_account"],
            "from_label": labels.get(legs["from_account"], legs["from_account"]),
            "to_label": labels.get(legs["to_account"], legs["to_account"]),
            "amount": legs["amount"],
        })

    return {"transfers": out, "total": total}

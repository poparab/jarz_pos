from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe import _


def _ensure_manager_access() -> None:
    roles = set(frappe.get_roles())
    allowed = {"System Manager", "Accounts Manager", "Stock Manager", "Manufacturing Manager", "Purchase Manager"}
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _resolve_named_account(company: str, label: str) -> str | None:
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


def _get_cashlike_accounts(company: str) -> list[dict[str, Any]]:
    """Return core cash/bank/mobile accounts for the company.

    Strategy:
      - Fetch Accounts with account_type IN ("Cash", "Bank") or names matching common mobile wallet keywords.
    """
    accounts: list[dict[str, Any]] = []
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


def _get_pos_profile_accounts(company: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    filters: dict[str, Any] = {"company": company}
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


def _get_sales_partner_accounts(company: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Some setups may not have a dedicated 'disabled' column on Sales Partner
    filters: dict[str, Any] = {}
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


def _get_balance_on(account: str, date: str | None = None) -> float:
    # ERPNext helper report util
    from erpnext.accounts.utils import get_balance_on

    try:
        bal = get_balance_on(account, date=date)
    except Exception:
        # Fallback to 0 on any error
        bal = 0.0
    return float(bal or 0)


@frappe.whitelist()
def list_accounts(company: str | None = None, as_of: str | None = None) -> list[dict[str, Any]]:
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

    accs: list[dict[str, Any]] = []
    accs.extend(_get_cashlike_accounts(company))
    accs.extend(_get_pos_profile_accounts(company))
    accs.extend(_get_sales_partner_accounts(company))

    # De-duplicate by account name
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
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
    def _sort_key(x: dict[str, Any]):
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
def submit_transfer(from_account: str, to_account: str, amount: float, posting_date: str | None = None, remark: str | None = None) -> dict[str, Any]:
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

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe
from frappe import _
from frappe.utils import flt, formatdate, getdate, now_datetime

from jarz_pos.api.pos import get_pos_profiles


@dataclass
class PaymentSource:
    account: str
    label: str
    category: str
    balance: float
    pos_profile: Optional[str] = None


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    if "JARZ Manager" in roles:
        return True
    privileged = {"System Manager", "Accounts Manager", "Account Manager", "Accounts User"}
    return bool(roles.intersection(privileged))


def _default_company() -> str:
    company = frappe.defaults.get_user_default("Company")
    if company:
        return company
    company = frappe.db.get_single_value("Global Defaults", "default_company")
    if company:
        return company
    # Fallback to first company
    row = frappe.get_all("Company", fields=["name"], limit=1)
    if row:
        return row[0]["name"]
    frappe.throw(_("Company is required but not configured."))
    return ""


def _resolve_named_account(company: str, label: str) -> Optional[str]:
    cleaned = (label or "").strip()
    if not cleaned:
        return None
    # 1) account_name exact match
    account = frappe.db.get_value(
        "Account",
        {"company": company, "is_group": 0, "account_name": cleaned},
        "name",
    )
    if account:
        return account
    # 2) name exact match
    if frappe.db.exists("Account", {"company": company, "is_group": 0, "name": cleaned}):
        return cleaned
    # 3) name with company abbreviation
    abbr = frappe.db.get_value("Company", company, "abbr")
    if abbr:
        candidate = f"{cleaned} - {abbr}"
        if frappe.db.exists("Account", {"company": company, "is_group": 0, "name": candidate}):
            return candidate
    # 4) fuzzy lookup by account_name like
    like = f"%{cleaned}%"
    rows = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0},
        or_filters=[["Account", "account_name", "like", like]],
        fields=["name"],
        limit=1,
    )
    if rows:
        return rows[0]["name"]
    return None


def _balance_on(account: str, company: Optional[str] = None) -> float:
    from erpnext.accounts.utils import get_balance_on

    try:
        return flt(get_balance_on(account, company=company))
    except Exception:
        return 0.0


def _indirect_expense_accounts(company: str) -> List[Dict[str, Any]]:
    parents = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 1},
        or_filters=[
            ["Account", "account_name", "in", ["Indirect Expenses", "Indirect Expense"]],
            ["Account", "name", "like", "%Indirect Expenses%"],
        ],
        fields=["name", "lft", "rgt"],
        order_by="lft asc",
    )
    if not parents:
        return []
    bounds: List[Tuple[int, int]] = [(p["lft"], p["rgt"]) for p in parents if p.get("lft") and p.get("rgt")]
    if not bounds:
        return []
    clauses = ["(lft > {0} and rgt < {1})".format(lft, rgt) for lft, rgt in bounds]
    condition = " or ".join(clauses)
    sql = f"""
        select name, account_name
        from `tabAccount`
        where company = %s and is_group = 0 and ({condition})
        order by account_name asc
    """
    rows = frappe.db.sql(sql, company, as_dict=True) if condition else []
    return [
        {
            "account": r["name"],
            "label": r.get("account_name") or r["name"],
        }
        for r in rows
    ]


def _manager_pos_profiles(company: str) -> List[str]:
    filters: Dict[str, Any] = {"company": company}
    try:
        if frappe.db.has_column("POS Profile", "disabled"):
            filters["disabled"] = 0
    except Exception:
        pass
    rows = frappe.get_all("POS Profile", filters=filters, fields=["name"], order_by="name asc")
    return [row["name"] for row in rows]


def _pos_profile_accounts(company: str, profiles: Sequence[str]) -> List[PaymentSource]:
    result: List[PaymentSource] = []
    seen: set[str] = set()
    for profile in profiles:
        account = _resolve_named_account(company, profile)
        if not account or account in seen:
            continue
        seen.add(account)
        balance = _balance_on(account, company)
        result.append(PaymentSource(account=account, label=profile, category="pos_profile", balance=balance, pos_profile=profile))
    return result


def _cashlike_accounts(company: str) -> List[PaymentSource]:
    accounts: List[PaymentSource] = []
    rows = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0, "account_type": ["in", ["Cash", "Bank"]]},
        fields=["name", "account_name", "account_type"],
        order_by="account_name asc",
    )
    for row in rows:
        category = "cash" if (row.get("account_type") or "").lower() == "cash" else "bank"
        accounts.append(
            PaymentSource(
                account=row["name"],
                label=row.get("account_name") or row["name"],
                category=category,
                balance=_balance_on(row["name"], company),
            )
        )
    mobile_rows = frappe.get_all(
        "Account",
        filters={"company": company, "is_group": 0},
        or_filters=[
            ["Account", "account_name", "like", "%Mobile%"],
            ["Account", "account_name", "like", "%Wallet%"],
            ["Account", "name", "like", "%Mobile%"],
            ["Account", "name", "like", "%Wallet%"],
        ],
        fields=["name", "account_name"],
    )
    seen_accounts = {ps.account for ps in accounts}
    for row in mobile_rows:
        if row["name"] in seen_accounts:
            continue
        accounts.append(
            PaymentSource(
                account=row["name"],
                label=row.get("account_name") or row["name"],
                category="mobile",
                balance=_balance_on(row["name"], company),
            )
        )
        seen_accounts.add(row["name"])
    return accounts


def _serialize_payment_sources(sources: Sequence[PaymentSource]) -> List[Dict[str, Any]]:
    serialised: List[Dict[str, Any]] = []
    for src in sources:
        serialised.append(
            {
                "id": src.account,
                "account": src.account,
                "label": src.label,
                "category": src.category,
                "balance": src.balance,
                "pos_profile": src.pos_profile,
            }
        )
    serialised.sort(key=lambda s: (s.get("category") or "", s.get("label") or ""))
    return serialised


def _month_label(month_key: str) -> str:
    try:
        date_obj = getdate(f"{month_key}-01")
    except Exception:
        return month_key
    return formatdate(date_obj, "MMMM yyyy")


def _load_months() -> List[str]:
    rows = frappe.db.get_all(
        "Jarz Expense Request",
        fields=["distinct expense_month as month"],
        order_by="month desc",
    )
    months = [r["month"] for r in rows if r.get("month")]
    current_month = getdate().strftime("%Y-%m")
    if current_month not in months:
        months.insert(0, current_month)
    return months


def _serialize_expense(doc: Dict[str, Any]) -> Dict[str, Any]:
    status_map = {
        0: "Pending Approval" if doc.get("requires_approval") else "Draft",
        1: "Approved",
        2: "Cancelled",
    }
    timeline: List[Dict[str, Any]] = []
    timeline.append(
        {
            "label": "Created",
            "timestamp": doc.get("creation"),
            "user": doc.get("requested_by") or doc.get("owner"),
        }
    )
    if doc.get("docstatus") == 0 and doc.get("requires_approval"):
        timeline.append(
            {
                "label": "Awaiting Approval",
                "timestamp": doc.get("modified"),
                "user": doc.get("requested_by") or doc.get("owner"),
            }
        )
    if doc.get("docstatus") == 1:
        timeline.append(
            {
                "label": "Approved",
                "timestamp": doc.get("approved_on"),
                "user": doc.get("approved_by"),
            }
        )
    payload = {
        "name": doc.get("name"),
        "expense_date": doc.get("expense_date"),
        "amount": flt(doc.get("amount")),
        "currency": doc.get("currency"),
        "reason_account": doc.get("reason_account"),
        "reason_label": doc.get("reason_label") or doc.get("reason_account"),
        "paying_account": doc.get("paying_account"),
        "payment_label": doc.get("payment_source_label") or doc.get("paying_account"),
        "payment_source_type": doc.get("payment_source_type"),
        "pos_profile": doc.get("pos_profile"),
        "requires_approval": bool(doc.get("requires_approval")),
        "docstatus": doc.get("docstatus"),
        "status": doc.get("status") or status_map.get(doc.get("docstatus"), "Draft"),
        "requested_by": doc.get("requested_by"),
        "approved_by": doc.get("approved_by"),
        "approved_on": doc.get("approved_on"),
        "remarks": doc.get("remarks"),
        "journal_entry": doc.get("journal_entry"),
        "company": doc.get("company"),
        "creation": doc.get("creation"),
        "modified": doc.get("modified"),
        "timeline": [t for t in timeline if t.get("timestamp")],
    }
    return payload


def _collect_expenses(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = frappe.get_all(
        "Jarz Expense Request",
        filters=filters,
        fields=[
            "name",
            "expense_date",
            "expense_month",
            "amount",
            "currency",
            "reason_account",
            "reason_label",
            "paying_account",
            "payment_source_label",
            "payment_source_type",
            "pos_profile",
            "requires_approval",
            "docstatus",
            "status",
            "requested_by",
            "approved_by",
            "approved_on",
            "remarks",
            "journal_entry",
            "company",
            "creation",
            "modified",
        ],
        order_by="expense_date desc, creation desc",
    )
    return rows


def _parse_filters(filters: Optional[str | Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except Exception:
            filters = {}
    elif filters is None:
        filters = {}
    assert isinstance(filters, dict)
    return filters


def _normalize_payment_filter(payment_ids: Optional[Iterable[str]]) -> List[str]:
    if not payment_ids:
        return []
    accounts = [pid for pid in payment_ids if pid]
    return accounts


@frappe.whitelist(allow_guest=False)
def get_expense_bootstrap(filters: Optional[str] = None):
    filter_payload = _parse_filters(filters)
    requested_month = filter_payload.get("month")
    payment_ids = filter_payload.get("payment_ids")
    if isinstance(payment_ids, str):
        try:
            payment_ids = json.loads(payment_ids)
        except Exception:
            payment_ids = [payment_ids]
    payment_ids = _normalize_payment_filter(payment_ids)

    is_manager = _is_manager()
    company = _default_company()

    manager_profiles: Sequence[str] = []
    user_profiles: Sequence[str] = []
    if is_manager:
        manager_profiles = _manager_pos_profiles(company)
    else:
        try:
            user_profiles = get_pos_profiles()
        except Exception:
            user_profiles = []

    payment_sources: List[PaymentSource] = []
    source_profiles: Sequence[str] = manager_profiles if is_manager and manager_profiles else user_profiles
    if source_profiles:
        payment_sources.extend(_pos_profile_accounts(company, source_profiles))

    if is_manager:
        payment_sources.extend(_cashlike_accounts(company))

    serialized_sources = _serialize_payment_sources(payment_sources)

    months = _load_months()
    current_month = getdate().strftime("%Y-%m")
    month_to_use = requested_month or current_month
    if month_to_use not in months:
        months.insert(0, month_to_use)

    frappe_filters: Dict[str, Any] = {"docstatus": ["in", [0, 1]]}
    if month_to_use:
        frappe_filters["expense_month"] = month_to_use
    if payment_ids:
        frappe_filters["paying_account"] = ["in", list(set(payment_ids))]

    expenses = _collect_expenses(frappe_filters)
    summary_total = sum(flt(exp.get("amount")) for exp in expenses)
    pending = [exp for exp in expenses if exp.get("docstatus") == 0]
    approved = [exp for exp in expenses if exp.get("docstatus") == 1]
    response = {
        "success": True,
        "is_manager": is_manager,
        "company": company,
        "current_month": current_month,
        "requested_month": month_to_use,
        "months": [
            {"id": m, "label": _month_label(m)}
            for m in months
        ],
        "payment_sources": serialized_sources,
        "reasons": _indirect_expense_accounts(company),
        "expenses": [_serialize_expense(exp) for exp in expenses],
        "summary": {
            "total_amount": summary_total,
            "pending_count": len(pending),
            "pending_amount": sum(flt(exp.get("amount")) for exp in pending),
            "approved_count": len(approved),
        },
        "applied_filters": {
            "payment_ids": payment_ids,
        },
    }
    return response


@frappe.whitelist(allow_guest=False)
def create_expense(payload: Optional[str] = None, **kwargs):
    data = _parse_filters(payload)
    data.update(kwargs)

    amount = flt(data.get("amount"))
    if amount <= 0:
        frappe.throw(_("Amount must be greater than zero."))

    reason_account = (data.get("reason_account") or "").strip()
    if not reason_account:
        frappe.throw(_("Reason (expense account) is required."))

    expense_date = data.get("expense_date") or formatdate(getdate(), "yyyy-MM-dd")
    remarks = data.get("remarks")

    is_manager = _is_manager()
    company = _default_company()

    payment_type = None
    payment_label = None
    pos_profile = None
    paying_account: Optional[str] = None

    if is_manager:
        paying_account = data.get("paying_account") or data.get("payment_account")
        payment_type = data.get("payment_source_type") or data.get("category") or "Account"
        if not paying_account:
            frappe.throw(_("Paying account is required."))
        payment_label = data.get("payment_label") or frappe.db.get_value("Account", paying_account, "account_name") or paying_account
    else:
        pos_profile = data.get("pos_profile") or data.get("payment_label") or data.get("payment_source")
        if not pos_profile:
            frappe.throw(_("POS profile is required for expense."))
        accessible = set(get_pos_profiles())
        if pos_profile not in accessible:
            frappe.throw(_("You do not have access to POS Profile: {0}").format(pos_profile))
        paying_account = _resolve_named_account(company, pos_profile)
        if not paying_account:
            frappe.throw(_("Could not resolve a paying account for POS Profile {0}").format(pos_profile))
        payment_type = "POS Profile"
        payment_label = pos_profile

    doc = frappe.get_doc(
        {
            "doctype": "Jarz Expense Request",
            "expense_date": expense_date,
            "amount": amount,
            "reason_account": reason_account,
            "paying_account": paying_account,
            "payment_source_type": payment_type,
            "payment_source_label": payment_label,
            "pos_profile": pos_profile,
            "requires_approval": 0 if is_manager else 1,
            "remarks": remarks,
            "requested_by": frappe.session.user,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()

    if is_manager:
        doc.approved_by = frappe.session.user
        doc.approved_on = now_datetime()
        doc.flags.ignore_permissions = True
        doc.submit()
        doc.reload()
    else:
        doc.reload()

    return {
        "success": True,
        "expense": _serialize_expense(doc.as_dict()),
    }


@frappe.whitelist(allow_guest=False)
def approve_expense(name: str):
    if not _is_manager():
        frappe.throw(_("Only managers can approve expenses."), frappe.PermissionError)

    if not name:
        frappe.throw(_("Expense document name is required."))

    doc = frappe.get_doc("Jarz Expense Request", name)
    if doc.docstatus != 0:
        frappe.throw(_("Only draft expense requests can be approved."))

    doc.approved_by = frappe.session.user
    doc.approved_on = now_datetime()
    doc.flags.ignore_permissions = True
    doc.submit()
    doc.reload()

    return {
        "success": True,
        "expense": _serialize_expense(doc.as_dict()),
    }

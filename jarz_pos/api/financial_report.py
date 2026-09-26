"""Profit & Loss report — the mobile Reports hub's financial statement.

One question: *what did the business earn, what did it spend, and what is left?*
Every figure is read from the General Ledger, so the report's net profit is the
ledger's net profit by construction, and the response carries the check.

Three places where the ledger alone would mislead, and how each is handled:

1. **Channel (B2B vs B2C).** The GL has one ``Sales`` account, so the split
   comes from submitted Sales Invoices (returns included, as negatives) keyed on
   ``custom_order_purpose`` — the same field that drives B2B pricing. Customer
   group is NOT used: on production 19 of 36 ``B2B Supply`` orders in September
   2026 went to customers in the ``Individual`` group. Any gap between the
   invoice sum and the ledger's sales income is reported as ``adjustments`` so
   the channels always add up to the ledger.
2. **Shipping income.** The ``Shipping Income (...)`` tax row on each invoice is
   the only record of what an order billed for delivery (see
   ``utils/invoice_utils.read_invoice_shipping_income``). Until 2026-09 those rows
   were credited to the Freight *expense* account, netting income against
   courier cost. The report therefore takes income from the tax rows and adds
   the part that sits in Freight back onto the shipping expense
   (``shipping_in_freight``). Both sides move by the same amount, so net profit
   still equals the ledger.
3. **Recurring vs other.** "Recurring" means an account that carries a
   ``Jarz Recurring Expense`` registry item or payroll. Everything else under
   Expense that is not cost of sales or shipping is "other".

Read-only. Requires the JARZ Manager role.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, getdate, nowdate

from jarz_pos.constants import ROLES

# ── channel mapping ───────────────────────────────────────────────────────

CHANNEL_B2B = "b2b"
CHANNEL_B2C = "b2c"
CHANNEL_STAFF = "staff"
CHANNEL_SAMPLES = "samples"
CHANNEL_ADJUSTMENTS = "adjustments"
CHANNELS = (CHANNEL_B2C, CHANNEL_B2B, CHANNEL_STAFF, CHANNEL_SAMPLES)

# Account types that make up cost of sales, rather than operating expense.
COST_OF_SALES_TYPES = {
    "Cost of Goods Sold",
    "Stock Adjustment",
    "Expenses Included In Valuation",
}

# Anything wider than this is bucketed by week, then by month.
DAILY_MAX_DAYS = 45
WEEKLY_MAX_DAYS = 184

# Below this the invoice/ledger gap is rounding, not a finding.
TOLERANCE = 1.0

# Under this share of orders with stock posted (or of recorded courier cost
# reaching the ledger), the period's costs are incomplete and profit overstated.
COVERAGE_WARN_PCT = 90.0


def channel_for_purpose(purpose: Optional[str]) -> str:
    """Map ``Sales Invoice.custom_order_purpose`` to a reporting channel."""
    p = (purpose or "").strip()
    if p == "B2B Supply":
        return CHANNEL_B2B
    if p == "Employee":
        return CHANNEL_STAFF
    if p.startswith("Sample"):
        return CHANNEL_SAMPLES
    # Standard, Free Shipping Waiver, and legacy blanks are consumer orders.
    return CHANNEL_B2C


# ── access / dates ────────────────────────────────────────────────────────


def _ensure_jarz_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access the profit & loss report"), frappe.PermissionError)


def _parse_dates(date_from: Optional[str], date_to: Optional[str]) -> Tuple[date, date]:
    today = getdate(nowdate())
    fd = getdate(date_from) if date_from else getdate(get_first_day(today))
    td = getdate(date_to) if date_to else today
    if fd > td:
        fd, td = td, fd
    return fd, td


def granularity_for(fd: date, td: date) -> str:
    days = (td - fd).days + 1
    if days <= DAILY_MAX_DAYS:
        return "day"
    if days <= WEEKLY_MAX_DAYS:
        return "week"
    return "month"


def bucket_key(d: date, granularity: str) -> str:
    """ISO date of the first day of ``d``'s bucket."""
    d = getdate(d)
    if granularity == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if granularity == "month":
        return date(d.year, d.month, 1).isoformat()
    return d.isoformat()


def bucket_keys(fd: date, td: date, granularity: str) -> List[str]:
    """Every bucket in the range, so a quiet day still plots as zero."""
    keys: List[str] = []
    d = fd
    while d <= td:
        k = bucket_key(d, granularity)
        if not keys or keys[-1] != k:
            keys.append(k)
        d += timedelta(days=1)
    return keys


def months_in_range(fd: date, td: date) -> List[Tuple[date, date]]:
    """(first, last) day of every calendar month the range touches."""
    out: List[Tuple[date, date]] = []
    cur = date(fd.year, fd.month, 1)
    while cur <= td:
        last = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        out.append((cur, last))
        cur = last + timedelta(days=1)
    return out


# ── classification (pure) ─────────────────────────────────────────────────


def classify_expense_account(
    account: str,
    account_type: Optional[str],
    freight_account: Optional[str],
    recurring_accounts: Iterable[str],
) -> str:
    """Which P&L section an Expense-root account belongs to."""
    if freight_account and account == freight_account:
        return "shipping"
    if (account_type or "") in COST_OF_SALES_TYPES:
        return "cost_of_sales"
    if account in set(recurring_accounts):
        return "recurring"
    return "other"


def account_label(account: str) -> str:
    """``Rent - Dokki - J`` → ``Rent - Dokki`` (drop the company abbreviation)."""
    parts = (account or "").rsplit(" - ", 1)
    return parts[0] if len(parts) == 2 and len(parts[1]) <= 5 else account


def pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


# ── data loaders ──────────────────────────────────────────────────────────


def _default_company() -> Optional[str]:
    company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
        "Global Defaults", "default_company"
    )
    if company:
        return company
    companies = frappe.get_all("Company", pluck="name", limit=2)
    return companies[0] if len(companies) == 1 else None


def _accounts(company: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(shipping income account, freight expense account) for the company."""
    if not company:
        return None, None
    from jarz_pos.utils.account_utils import (
        get_freight_expense_account,
        get_shipping_income_account,
    )

    freight = None
    shipping = None
    try:
        freight = get_freight_expense_account(company)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "financial_report: freight account")
    try:
        shipping = get_shipping_income_account(company)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "financial_report: shipping income account")
    # Before the ledger split the "shipping income" account resolves to Freight
    # itself; treat that as "no separate income account".
    if shipping and shipping == freight:
        shipping = None
    return shipping, freight


def _recurring_accounts(company: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Accounts that hold recurring spend, with what each is expected to cost.

    Returns ``{account: {"category": ..., "items": [...], "monthly_due": float}}``.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if frappe.db.table_exists("Jarz Recurring Expense"):
        filters: Dict[str, Any] = {}
        if company and frappe.get_meta("Jarz Recurring Expense").has_field("company"):
            filters["company"] = company
        for row in frappe.get_all(
            "Jarz Recurring Expense",
            filters=filters,
            fields=[
                "name", "expense_name", "category", "expense_account", "amount",
                "frequency", "status", "start_date", "end_date",
            ],
        ):
            acct = row.get("expense_account")
            if not acct:
                continue
            bucket = out.setdefault(acct, {"category": row.get("category"), "items": []})
            bucket["items"].append(row)

    try:
        from jarz_pos.api.recurring_expenses import _payroll_expense_accounts

        for acct in _payroll_expense_accounts(company):
            out.setdefault(acct, {"category": "Payroll", "items": []})["payroll"] = True
    except Exception:
        frappe.log_error(frappe.get_traceback(), "financial_report: payroll accounts")
    return out


def _due_for_range(
    recurring: Dict[str, Dict[str, Any]], fd: date, td: date, company: Optional[str]
) -> Dict[str, float]:
    """What each recurring account is scheduled to cost over the months touched."""
    from jarz_pos.api.recurring_expenses import _is_due_in_month

    due: Dict[str, float] = defaultdict(float)
    months = months_in_range(fd, td)
    for acct, info in recurring.items():
        for item in info.get("items", []):
            for m_start, m_end in months:
                if _is_due_in_month(item, m_start, m_end):
                    due[acct] += flt(item.get("amount"))

    payroll_accounts = [a for a, i in recurring.items() if i.get("payroll")]
    if payroll_accounts:
        try:
            from jarz_pos.api.recurring_expenses import _load_payroll

            monthly = sum(
                flt(_load_payroll(company, m_end).get("monthly_total")) for _s, m_end in months
            )
            # Payroll posts to one ledger in practice; if several exist the
            # figure goes on the first so it is counted once, not per account.
            due[payroll_accounts[0]] += monthly
        except Exception:
            frappe.log_error(frappe.get_traceback(), "financial_report: payroll due")
    return dict(due)


def _gl_rows(fd: date, td: date, company: Optional[str]) -> List[Dict[str, Any]]:
    """Net P&L movement per (account, bucket day) inside the range."""
    cond = "AND gle.company = %(company)s" if company else ""
    return frappe.db.sql(
        f"""
        SELECT gle.account, gle.posting_date, acc.root_type, acc.account_type,
               SUM(gle.debit) AS debit, SUM(gle.credit) AS credit
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(fd)s AND %(td)s
          AND acc.root_type IN ('Income', 'Expense')
          {cond}
        GROUP BY gle.account, gle.posting_date, acc.root_type, acc.account_type
        """,
        {"fd": fd, "td": td, "company": company},
        as_dict=True,
    )


def _invoice_rows(fd: date, td: date, company: Optional[str]) -> List[Dict[str, Any]]:
    """Net sales per (purpose, day), returns included as negatives."""
    cond = "AND company = %(company)s" if company else ""
    return frappe.db.sql(
        f"""
        SELECT custom_order_purpose AS purpose, posting_date,
               SUM(base_net_total) AS net,
               SUM(CASE WHEN is_return = 0 THEN 1 ELSE 0 END) AS orders,
               SUM(CASE WHEN is_return = 1 THEN 1 ELSE 0 END) AS returns,
               SUM(CASE WHEN is_return = 1 THEN base_net_total ELSE 0 END) AS returns_value
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY custom_order_purpose, posting_date
        """,
        {"fd": fd, "td": td, "company": company},
        as_dict=True,
    )


def _shipping_rows(
    fd: date, td: date, company: Optional[str], shipping_account: Optional[str]
) -> List[Dict[str, Any]]:
    """Delivery charges billed, per (purpose, day), from the invoice tax rows."""
    cond = "AND si.company = %(company)s" if company else ""
    return frappe.db.sql(
        f"""
        SELECT si.custom_order_purpose AS purpose, si.posting_date,
               SUM(stc.base_tax_amount) AS amount
        FROM `tabSales Taxes and Charges` stc
        JOIN `tabSales Invoice` si
          ON si.name = stc.parent AND stc.parenttype = 'Sales Invoice'
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND (stc.description LIKE 'Shipping Income%%' OR stc.account_head = %(ship)s)
          {cond}
        GROUP BY si.custom_order_purpose, si.posting_date
        """,
        {"fd": fd, "td": td, "company": company, "ship": shipping_account or ""},
        as_dict=True,
    )


def _shipping_order_basis(fd: date, td: date, company: Optional[str]) -> Dict[str, Any]:
    """What the orders placed in the window say, independent of the ledger.

    Two cross-checks come from here. Courier cost recorded on the orders
    (``custom_shipping_expense``) against the courier JEs, and how many orders
    have had their stock posted. Cost of goods only reaches the ledger through a
    Delivery Note (or an invoice with ``update_stock``), and on production July
    2026 had none for 472 orders, so a month can look nearly cost-free when it
    is simply unposted.
    """
    cond = "AND si.company = %(company)s" if company else ""
    row = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(si.custom_shipping_expense), 0) AS expense,
               COUNT(*) AS orders,
               SUM(CASE WHEN IFNULL(si.custom_is_pickup, 0) = 0 THEN 1 ELSE 0 END) AS delivery_orders,
               SUM(CASE WHEN IFNULL(si.custom_is_pickup, 0) = 1 THEN 1 ELSE 0 END) AS pickup_orders,
               SUM(CASE WHEN si.update_stock = 1
                          OR EXISTS (
                              SELECT 1 FROM `tabDelivery Note Item` dni
                              WHERE dni.against_sales_invoice = si.name AND dni.docstatus = 1)
                          OR EXISTS (
                              SELECT 1 FROM `tabSales Invoice Item` sii
                              WHERE sii.parent = si.name AND IFNULL(sii.delivery_note, '') != '')
                        THEN 1 ELSE 0 END) AS stock_posted_orders
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        """,
        {"fd": fd, "td": td, "company": company},
        as_dict=True,
    )[0]
    return {
        "expense": flt(row.get("expense")),
        "orders": int(row.get("orders") or 0),
        "delivery_orders": int(row.get("delivery_orders") or 0),
        "pickup_orders": int(row.get("pickup_orders") or 0),
        "stock_posted_orders": int(row.get("stock_posted_orders") or 0),
    }


# ── assembly (pure: takes rows, returns the payload) ──────────────────────


def build_report(
    *,
    fd: date,
    td: date,
    gl_rows: List[Dict[str, Any]],
    invoice_rows: List[Dict[str, Any]],
    shipping_rows: List[Dict[str, Any]],
    shipping_account: Optional[str],
    freight_account: Optional[str],
    recurring: Dict[str, Dict[str, Any]],
    due: Dict[str, float],
    order_basis: Dict[str, Any],
) -> Dict[str, Any]:
    granularity = granularity_for(fd, td)
    keys = bucket_keys(fd, td, granularity)

    def blank() -> Dict[str, float]:
        return defaultdict(float)

    trend: Dict[str, Dict[str, float]] = {k: blank() for k in keys}

    # ── ledger ──
    ledger_income = 0.0
    ledger_expense = 0.0
    ledger_shipping_income = 0.0
    sales_income = 0.0
    other_income: Dict[str, float] = defaultdict(float)
    sections: Dict[str, Dict[str, float]] = {
        "cost_of_sales": defaultdict(float),
        "shipping": defaultdict(float),
        "recurring": defaultdict(float),
        "other": defaultdict(float),
    }
    account_types: Dict[str, str] = {}
    recurring_accounts = set(recurring)

    for r in gl_rows:
        acct = r["account"]
        k = bucket_key(r["posting_date"], granularity)
        t = trend.setdefault(k, blank())
        if r["root_type"] == "Income":
            amt = flt(r.get("credit")) - flt(r.get("debit"))
            ledger_income += amt
            if shipping_account and acct == shipping_account:
                ledger_shipping_income += amt
                t["ledger_shipping_income"] += amt
            else:
                sales_income += amt
                t["ledger_sales"] += amt
                other_income[acct] += amt
        else:
            amt = flt(r.get("debit")) - flt(r.get("credit"))
            ledger_expense += amt
            account_types[acct] = r.get("account_type") or ""
            section = classify_expense_account(
                acct, r.get("account_type"), freight_account, recurring_accounts
            )
            sections[section][acct] += amt
            t[section] += amt

    # ── channels (invoices) ──
    channels: Dict[str, Dict[str, float]] = {
        c: {"sales": 0.0, "shipping_income": 0.0, "orders": 0, "returns": 0, "returns_value": 0.0}
        for c in CHANNELS
    }
    invoice_sales = 0.0
    for r in invoice_rows:
        c = channel_for_purpose(r.get("purpose"))
        net = flt(r.get("net"))
        channels[c]["sales"] += net
        channels[c]["orders"] += int(r.get("orders") or 0)
        channels[c]["returns"] += int(r.get("returns") or 0)
        channels[c]["returns_value"] += flt(r.get("returns_value"))
        invoice_sales += net
        t = trend.setdefault(bucket_key(r["posting_date"], granularity), blank())
        t[c] += net
        t["invoice_sales"] += net

    shipping_income = 0.0
    for r in shipping_rows:
        c = channel_for_purpose(r.get("purpose"))
        amt = flt(r.get("amount"))
        channels[c]["shipping_income"] += amt
        shipping_income += amt
        trend.setdefault(bucket_key(r["posting_date"], granularity), blank())["shipping_income"] += amt

    adjustments = round(sales_income - invoice_sales, 2)

    # Shipping income that the ledger still holds inside Freight (pre-split
    # history, or a site where the income account does not exist yet).
    shipping_in_freight = round(shipping_income - ledger_shipping_income, 2)

    # ── sections ──
    def rows_for(section: str) -> List[Dict[str, Any]]:
        return sorted(
            (
                {"account": a, "label": account_label(a), "amount": round(v, 2)}
                for a, v in sections[section].items()
                if abs(v) >= 0.005
            ),
            key=lambda x: -abs(x["amount"]),
        )

    cogs_rows = rows_for("cost_of_sales")
    cost_of_goods = round(
        sum(v for a, v in sections["cost_of_sales"].items() if account_types.get(a) == "Cost of Goods Sold"), 2
    )
    cost_of_sales_total = round(sum(sections["cost_of_sales"].values()), 2)

    freight_ledger = round(sum(sections["shipping"].values()), 2)
    shipping_expense = round(freight_ledger + shipping_in_freight, 2)

    recurring_rows = []
    all_recurring = set(sections["recurring"]) | {a for a, v in due.items() if v}
    for acct in all_recurring:
        info = recurring.get(acct, {})
        posted = round(sections["recurring"].get(acct, 0.0), 2)
        d = round(flt(due.get(acct)), 2)
        recurring_rows.append({
            "account": acct,
            "label": account_label(acct),
            "category": info.get("category") or "",
            "posted": posted,
            "due": d,
            "remaining": round(max(d - posted, 0.0), 2),
        })
    recurring_rows.sort(key=lambda x: -max(x["posted"], x["due"]))
    recurring_total = round(sum(sections["recurring"].values()), 2)
    recurring_due = round(sum(r["due"] for r in recurring_rows), 2)

    other_rows = rows_for("other")
    other_total = round(sum(sections["other"].values()), 2)

    # ── totals ──
    sales_total = round(sales_income, 2)
    total_revenue = round(sales_total + shipping_income, 2)
    gross_profit = round(sales_total - cost_of_sales_total, 2)
    operating_expenses = round(shipping_expense + recurring_total + other_total, 2)
    total_expenses = round(cost_of_sales_total + operating_expenses, 2)
    net_profit = round(total_revenue - total_expenses, 2)
    ledger_net = round(ledger_income - ledger_expense, 2)

    channel_rows = []
    for c in CHANNELS:
        ch = channels[c]
        channel_rows.append({
            "channel": c,
            "sales": round(ch["sales"], 2),
            "shipping_income": round(ch["shipping_income"], 2),
            "revenue": round(ch["sales"] + ch["shipping_income"], 2),
            "orders": int(ch["orders"]),
            "returns": int(ch["returns"]),
            "returns_value": round(ch["returns_value"], 2),
            "share_pct": pct(ch["sales"] + ch["shipping_income"], total_revenue),
        })
    if abs(adjustments) >= TOLERANCE:
        channel_rows.append({
            "channel": CHANNEL_ADJUSTMENTS, "sales": adjustments, "shipping_income": 0.0,
            "revenue": adjustments, "orders": 0, "returns": 0, "returns_value": 0.0,
            "share_pct": pct(adjustments, total_revenue),
        })

    # ── trend ──
    trend_rows = []
    for k in sorted(trend):
        t = trend[k]
        bucket_ship_in_freight = t["shipping_income"] - t["ledger_shipping_income"]
        adj = t["ledger_sales"] - t["invoice_sales"]
        revenue = t["ledger_sales"] + t["shipping_income"]
        shipping_exp = t["shipping"] + bucket_ship_in_freight
        expenses = t["cost_of_sales"] + shipping_exp + t["recurring"] + t["other"]
        trend_rows.append({
            "date": k,
            "b2c": round(t[CHANNEL_B2C], 2),
            "b2b": round(t[CHANNEL_B2B], 2),
            "staff": round(t[CHANNEL_STAFF], 2),
            "samples": round(t[CHANNEL_SAMPLES], 2),
            "adjustments": round(adj, 2),
            "shipping_income": round(t["shipping_income"], 2),
            "revenue": round(revenue, 2),
            "cost_of_sales": round(t["cost_of_sales"], 2),
            "shipping_expense": round(shipping_exp, 2),
            "recurring": round(t["recurring"], 2),
            "other_expenses": round(t["other"], 2),
            "expenses": round(expenses, 2),
            "net_profit": round(revenue - expenses, 2),
        })

    notes: List[str] = []
    if abs(adjustments) >= TOLERANCE:
        notes.append("sales_adjustments")
    if abs(shipping_in_freight) >= TOLERANCE:
        notes.append("shipping_income_in_freight")

    # ── completeness of the books (the report can only be as right as they are) ──
    orders_placed = int(order_basis.get("orders") or 0)
    stock_posted = int(order_basis.get("stock_posted_orders") or 0)
    cogs_coverage = pct(stock_posted, orders_placed) if orders_placed else 100.0
    expense_order_basis = flt(order_basis.get("expense"))
    shipping_coverage = pct(shipping_expense, expense_order_basis) if expense_order_basis else 100.0
    warnings: List[str] = []
    if cogs_coverage < COVERAGE_WARN_PCT:
        warnings.append("cogs_incomplete")
    if shipping_coverage < COVERAGE_WARN_PCT:
        warnings.append("shipping_expense_incomplete")
    if recurring_due and recurring_total < recurring_due - TOLERANCE:
        warnings.append("recurring_not_fully_posted")

    delivery_orders = int(order_basis.get("delivery_orders") or 0)
    return {
        "period": {"date_from": fd.isoformat(), "date_to": td.isoformat(), "granularity": granularity},
        "summary": {
            "total_revenue": total_revenue,
            "sales": sales_total,
            "shipping_income": round(shipping_income, 2),
            "b2b_revenue": next(r["revenue"] for r in channel_rows if r["channel"] == CHANNEL_B2B),
            "b2c_revenue": next(r["revenue"] for r in channel_rows if r["channel"] == CHANNEL_B2C),
            "cost_of_sales": cost_of_sales_total,
            "gross_profit": gross_profit,
            "gross_margin_pct": pct(gross_profit, sales_total),
            "shipping_expense": shipping_expense,
            "shipping_net": round(shipping_income - shipping_expense, 2),
            "recurring_expenses": recurring_total,
            "other_expenses": other_total,
            "operating_expenses": operating_expenses,
            "total_expenses": total_expenses,
            "net_profit": net_profit,
            "net_margin_pct": pct(net_profit, total_revenue),
            "orders": sum(int(r["orders"]) for r in channel_rows),
        },
        "channels": channel_rows,
        "cost_of_sales": {"total": cost_of_sales_total, "cost_of_goods": cost_of_goods, "rows": cogs_rows},
        "shipping": {
            "income": round(shipping_income, 2),
            "expense": shipping_expense,
            "net": round(shipping_income - shipping_expense, 2),
            "expense_ledger": freight_ledger,
            "income_in_expense_account": shipping_in_freight,
            "expense_order_basis": round(flt(order_basis.get("expense")), 2),
            "delivery_orders": delivery_orders,
            "pickup_orders": int(order_basis.get("pickup_orders") or 0),
            "income_per_delivery": round(shipping_income / delivery_orders, 2) if delivery_orders else 0.0,
            "expense_per_delivery": round(shipping_expense / delivery_orders, 2) if delivery_orders else 0.0,
        },
        "recurring": {"total": recurring_total, "due": recurring_due, "rows": recurring_rows},
        "other_expenses": {"total": other_total, "rows": other_rows},
        "trend": trend_rows,
        "reconciliation": {
            "ledger_income": round(ledger_income, 2),
            "ledger_expense": round(ledger_expense, 2),
            "ledger_net_profit": ledger_net,
            "report_net_profit": net_profit,
            "difference": round(net_profit - ledger_net, 2),
            "matches": abs(net_profit - ledger_net) < TOLERANCE,
            "invoice_sales": round(invoice_sales, 2),
            "ledger_sales": sales_total,
            "notes": notes,
        },
        "data_quality": {
            "orders": orders_placed,
            "stock_posted_orders": stock_posted,
            "cogs_coverage_pct": cogs_coverage,
            "shipping_expense_coverage_pct": shipping_coverage,
            "warnings": warnings,
        },
    }


@frappe.whitelist()
def get_profit_and_loss(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    company: Optional[str] = None,
) -> Dict[str, Any]:
    """Ledger-reconciled profit & loss for the range, split B2B / B2C."""
    _ensure_jarz_manager()
    fd, td = _parse_dates(date_from, date_to)
    company = company or _default_company()
    shipping_account, freight_account = _accounts(company)
    recurring = _recurring_accounts(company)

    return build_report(
        fd=fd,
        td=td,
        gl_rows=_gl_rows(fd, td, company),
        invoice_rows=_invoice_rows(fd, td, company),
        shipping_rows=_shipping_rows(fd, td, company, shipping_account),
        shipping_account=shipping_account,
        freight_account=freight_account,
        recurring=recurring,
        due=_due_for_range(recurring, fd, td, company),
        order_basis=_shipping_order_basis(fd, td, company),
    )

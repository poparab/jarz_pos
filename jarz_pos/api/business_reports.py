"""Business reports for the mobile Reports hub (Feature: reports-overhaul).

Four read-only reports, all gated to JARZ Manager or Administrator:

* ``get_unit_economics``     what one more order earns, and what it takes to break even.
* ``get_promo_performance``  what promo codes cost against what they bring in.
* ``get_stock_adjustments``  waste and count adjustments, valued from the stock ledger.
* ``get_labour_cost``        payroll against sales and hours worked, per branch.

"Branch" means **POS Profile** everywhere (``Sales Invoice.pos_profile``); a
branch's stock is that POS Profile's ``warehouse``. Dates are inclusive
``YYYY-MM-DD`` strings and default to month-to-date; a range longer than
``MAX_RANGE_DAYS`` (366) is refused with a ValidationError. Money is company
currency.

Data sources (confirmed in code / fixtures, not guessed):

* Unit economics reuses ``api/financial_report`` (GL-reconciled P&L) for sales,
  COGS, shipping, recurring and other expense. Kashier receipts are GL debits on
  the Kashier gateway ledger (Asset leaf named like "Kashier", the same match
  ``api/invoices._resolve_kashier_account`` and ``api/cash_transfer`` use).
  Talabat revenue is Sales Invoices whose ``sales_partner`` is Talabat (the
  partner flow in ``api/sales_partners``). Ad spend and extra fixed cost are
  owner inputs; Meta ads are not booked in the ledger.
* Promo codes: ``Sales Invoice.custom_promo_discount_total`` /
  ``custom_promo_codes`` and the ``Jarz Promo Redemption`` rows
  (``services/promo_codes.record_redemptions_on_submit``).
* Stock adjustments: Stock Ledger Entries of Stock Reconciliations (not Opening
  Stock) and Material Issue Stock Entries, excluding the automatic consumable
  deductions (``services/consumable_deduction``), which are consumption, not waste.
* Labour: HRMS Salary Slips when any exist (none have ever been produced here),
  otherwise Salary Structure Assignments via ``recurring_expenses._load_payroll``
  (the same payroll the Monthly Expenses board pays), otherwise the payroll GL
  accounts. Hours come from Employee Checkin grouped exactly as
  ``services/attendance`` does; employee -> branch is the Shift Assignment's
  ``shift_location`` mapped to the POS Profile carrying it in
  ``custom_shift_location``.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

from jarz_pos.api import financial_report as fr
from jarz_pos.constants import ROLES

#: ``custom_order_purpose`` values that are not consumer orders (see
#: ``financial_report.channel_for_purpose``). Promo comparisons are consumer-only.
_B2C_COND = (
    "IFNULL({a}custom_order_purpose, '') NOT IN ('B2B Supply', 'Employee') "
    "AND IFNULL({a}custom_order_purpose, '') NOT LIKE 'Sample%%'"
)

#: Remarks prefix ``services/consumable_deduction`` writes on its Material Issues.
CONSUMABLE_REMARKS_PREFIX = "Auto consumable deduction"

#: Waste/adjustment rows returned in the listing and the item ranking.
MAX_ENTRIES = 200
MAX_ITEMS = 50

_ADS_RE = re.compile(r"\bads?\b|advertis|marketing", re.IGNORECASE)


# ── access / common ───────────────────────────────────────────────────────


def _ensure_jarz_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access business reports"), frappe.PermissionError)


def _clean_branch(branch: Any) -> Optional[str]:
    """``None``/empty = all branches; anything else must be a real POS Profile."""
    text = str(branch or "").strip()
    if not text:
        return None
    if not frappe.db.exists("POS Profile", text):
        frappe.throw(_("Unknown branch: {0}").format(text))
    return text


#: Longest range any report here accepts, inclusive (same cap as
#: ``ops_reports.get_fulfilment_sla``). Labour loads payroll once per month and
#: every check-in in the range, so an open-ended range is a heavy request.
MAX_RANGE_DAYS = 366


def ensure_max_range(fd: date, td: date, max_days: int = MAX_RANGE_DAYS) -> None:
    """Refuse a range longer than ``max_days`` (inclusive) with a ValidationError."""
    if (td - fd).days + 1 > max_days:
        frappe.throw(_("Choose a range of at most {0} days").format(max_days), frappe.ValidationError)


def _parse_range(date_from: Optional[str], date_to: Optional[str]) -> Tuple[date, date]:
    """``fr._parse_dates`` plus the :data:`MAX_RANGE_DAYS` cap."""
    fd, td = fr._parse_dates(date_from, date_to)
    ensure_max_range(fd, td)
    return fd, td


def _finite_input(value: Any, default: float, lo: float = 0.0, hi: Optional[float] = None) -> float:
    """An owner input as a finite float clamped to ``[lo, hi]``.

    ``flt("NaN")`` is NaN and ``flt("inf")`` is inf; NaN passes ``max``/``min``
    untouched and inf reaches ``math.ceil`` (OverflowError). NaN takes
    ``default``; an infinity is clamped to ``hi``, or takes ``default`` when the
    input has no upper bound.
    """
    v = flt(value)
    if math.isnan(v):
        v = default
    v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return v if math.isfinite(v) else default


def _r2(value: Any) -> float:
    return round(flt(value), 2)


def _ratio_pct(part: float, whole: float) -> Optional[float]:
    return round(part / whole * 100, 1) if whole else None


def _base_filters(fd: date, td: date, branch: Optional[str]) -> Dict[str, Any]:
    return {"date_from": fd.isoformat(), "date_to": td.isoformat(), "branch": branch}


# ═════════════════════════════════════════════════════════════════════════
# B1  Unit economics
# ═════════════════════════════════════════════════════════════════════════


def _profit_and_loss(fd: date, td: date, company: Optional[str]) -> Dict[str, Any]:
    """The P&L payload, assembled exactly as ``get_profit_and_loss`` does (no gate)."""
    shipping_account, freight_account = fr._accounts(company)
    recurring = fr._recurring_accounts(company)
    return fr.build_report(
        fd=fd,
        td=td,
        gl_rows=fr._gl_rows(fd, td, company),
        invoice_rows=fr._invoice_rows(fd, td, company),
        shipping_rows=fr._shipping_rows(fd, td, company, shipping_account),
        shipping_account=shipping_account,
        freight_account=freight_account,
        recurring=recurring,
        due=fr._due_for_range(recurring, fd, td, company),
        order_basis=fr._shipping_order_basis(fd, td, company),
    )


def _accounts_like(company: Optional[str], root_type: str, pattern: str) -> List[str]:
    cond = "AND company = %(company)s" if company else ""
    return [
        r[0]
        for r in frappe.db.sql(
            f"""
            SELECT name FROM `tabAccount`
            WHERE root_type = %(root)s AND is_group = 0
              AND (account_name LIKE %(p)s OR name LIKE %(p)s)
              {cond}
            """,
            {"root": root_type, "p": pattern, "company": company},
        )
    ]


def _kashier_receipts(fd: date, td: date, company: Optional[str]) -> float:
    """Money received into the Kashier gateway ledger in the range (GL debits).

    Debits are inflows whatever the voucher (Payment Entry from the POS, or
    however the web checkout books it); transfers out and refunds are credits
    and do not count. Cancelled entries are excluded.
    """
    accounts = _accounts_like(company, "Asset", "%kashier%")
    if not accounts:
        return 0.0
    row = frappe.db.sql(
        """
        SELECT COALESCE(SUM(debit), 0)
        FROM `tabGL Entry`
        WHERE is_cancelled = 0
          AND posting_date BETWEEN %(fd)s AND %(td)s
          AND account IN %(accounts)s
          AND voucher_type != 'Period Closing Voucher'
        """,
        {"fd": fd, "td": td, "accounts": tuple(accounts)},
    )
    return flt(row[0][0]) if row else 0.0


def _talabat_sales(fd: date, td: date, company: Optional[str]) -> Tuple[float, int]:
    """Net sales (returns as negatives) and order count of Talabat-partner invoices."""
    cond = "AND company = %(company)s" if company else ""
    row = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(base_net_total), 0),
               COALESCE(SUM(CASE WHEN is_return = 0 THEN 1 ELSE 0 END), 0)
        FROM `tabSales Invoice`
        WHERE docstatus = 1
          AND posting_date BETWEEN %(fd)s AND %(td)s
          AND IFNULL(sales_partner, '') LIKE %(partner)s
          {cond}
        """,
        {"fd": fd, "td": td, "partner": "%talabat%", "company": company},
    )
    if not row:
        return 0.0, 0
    return flt(row[0][0]), cint(row[0][1])


def build_unit_economics(
    *,
    pnl: Dict[str, Any],
    kashier_receipts: float,
    talabat_sales: float,
    commission_accounts: Iterable[str],
    kashier_fee_accounts: Iterable[str],
    ad_spend: float,
    kashier_pct: float,
    talabat_pct: float,
    extra_fixed: float,
) -> Dict[str, Any]:
    """Pure: P&L payload + gateway/partner volumes + owner inputs -> unit economics.

    * revenue = ledger sales income (delivery income is netted into shipping).
    * cogs = "Cost of Goods Sold" ledgers only. Stock Adjustment and other
      cost-of-sales ledgers are moved into fixed ``other`` (the owner asked for
      them out of the per-order model while stock practice is being fixed), so
      the ledger total is still fully accounted for.
    * shipping_net = courier/freight expense minus delivery income billed.
    * Kashier fees and Talabat commission are estimates from the owner's
      percentages. Anything already booked to a "Commission on Sales" or
      "Kashier" expense ledger is taken out of fixed costs so it is not counted
      twice.
    """
    summary = pnl.get("summary") or {}
    revenue = flt(summary.get("sales"))
    orders = cint(summary.get("orders"))

    cos = pnl.get("cost_of_sales") or {}
    cogs = flt(cos.get("cost_of_goods"))
    stock_adjustment = flt(cos.get("total")) - cogs
    shipping_net = -flt(summary.get("shipping_net"))

    kashier_fees = flt(kashier_receipts) * flt(kashier_pct) / 100.0
    talabat_commission = flt(talabat_sales) * flt(talabat_pct) / 100.0
    variable_total = cogs + shipping_net + kashier_fees + talabat_commission
    contribution = revenue - variable_total

    commission_set = set(commission_accounts or [])
    kashier_fee_set = set(kashier_fee_accounts or [])
    recurring_rows = (pnl.get("recurring") or {}).get("rows") or []
    other_rows = (pnl.get("other_expenses") or {}).get("rows") or []

    def booked(accounts: Set[str]) -> Tuple[float, float]:
        rec = sum(flt(r.get("posted")) for r in recurring_rows if r.get("account") in accounts)
        oth = sum(flt(r.get("amount")) for r in other_rows if r.get("account") in accounts)
        return rec, oth

    com_rec, com_oth = booked(commission_set)
    kas_rec, kas_oth = booked(kashier_fee_set)
    booked_commission = com_rec + com_oth
    booked_kashier_fees = kas_rec + kas_oth

    recurring = flt((pnl.get("recurring") or {}).get("total")) - com_rec - kas_rec
    other = flt((pnl.get("other_expenses") or {}).get("total")) - com_oth - kas_oth + stock_adjustment
    ad_spend = flt(ad_spend)
    extra_fixed = flt(extra_fixed)
    fixed_total = recurring + other + ad_spend + extra_fixed
    net_profit = contribution - fixed_total

    aov = revenue / orders if orders else 0.0
    break_even_revenue = break_even_orders = gap = None
    if revenue > 0 and contribution > 0 and math.isfinite(fixed_total):
        ratio = contribution / revenue
        break_even_revenue = round(fixed_total / ratio, 2)
        gap = round(break_even_revenue - revenue, 2)
        if aov > 0 and math.isfinite(break_even_revenue / aov):
            break_even_orders = int(math.ceil(break_even_revenue / aov))

    def per_order(value: float) -> float:
        return round(value / orders, 2) if orders else 0.0

    notes: List[str] = []
    if ad_spend > 0:
        notes.append("ad_spend_is_owner_input")
    else:
        notes.append("ad_spend_not_entered")
    if extra_fixed > 0:
        notes.append("extra_fixed_is_owner_input")
    if kashier_receipts:
        notes.append("kashier_fees_estimated")
    if talabat_sales:
        notes.append("talabat_commission_estimated")
    if abs(booked_commission) >= fr.TOLERANCE:
        notes.append("booked_partner_commission_replaced_by_estimate")
    if abs(booked_kashier_fees) >= fr.TOLERANCE:
        notes.append("booked_kashier_fees_replaced_by_estimate")
    if abs(stock_adjustment) >= fr.TOLERANCE:
        notes.append("stock_adjustment_in_other")
    warnings = (pnl.get("data_quality") or {}).get("warnings") or []
    for w in ("cogs_incomplete", "shipping_expense_incomplete", "recurring_not_fully_posted"):
        if w in warnings:
            notes.append(w)
    if ad_spend > 0 and any(
        _ADS_RE.search(str(r.get("label") or r.get("account") or "")) and abs(flt(r.get("amount"))) >= fr.TOLERANCE
        for r in other_rows
    ):
        notes.append("ads_also_booked_in_ledger")
    if revenue > 0 and contribution <= 0:
        notes.append("contribution_not_positive")

    return {
        "revenue": _r2(revenue),
        "orders": orders,
        "avg_order_value": _r2(aov),
        "variable_costs": {
            "cogs": _r2(cogs),
            "shipping_net": _r2(shipping_net),
            "kashier_fees": _r2(kashier_fees),
            "talabat_commission": _r2(talabat_commission),
            "total": _r2(variable_total),
        },
        "contribution": _r2(contribution),
        "contribution_margin_pct": round(contribution / revenue * 100, 1) if revenue else 0.0,
        "fixed_costs": {
            "recurring": _r2(recurring),
            "other": _r2(other),
            "ad_spend": _r2(ad_spend),
            "extra_fixed": _r2(extra_fixed),
            "total": _r2(fixed_total),
        },
        "net_profit": _r2(net_profit),
        "break_even_revenue": break_even_revenue,
        "break_even_orders": break_even_orders,
        "gap_to_break_even": gap,
        "per_order": {
            "revenue": per_order(revenue),
            "cogs": per_order(cogs),
            "shipping": per_order(shipping_net),
            "fees": per_order(kashier_fees + talabat_commission),
            "contribution": per_order(contribution),
        },
        "notes": notes,
        # Additive detail: the volumes and ledger figures behind the estimates.
        "basis": {
            "kashier_receipts": _r2(kashier_receipts),
            "talabat_sales": _r2(talabat_sales),
            "shipping_income": _r2(summary.get("shipping_income")),
            "shipping_expense": _r2(summary.get("shipping_expense")),
            "stock_adjustment": _r2(stock_adjustment),
            "booked_partner_commission": _r2(booked_commission),
            "booked_kashier_fees": _r2(booked_kashier_fees),
            "recurring_due": _r2((pnl.get("recurring") or {}).get("due")),
            "cogs_coverage_pct": flt((pnl.get("data_quality") or {}).get("cogs_coverage_pct")),
            "ledger_net_profit": _r2(summary.get("net_profit")),
        },
    }


def owner_inputs(ad_spend: Any, kashier_pct: Any, talabat_pct: Any, extra_fixed: Any) -> Dict[str, float]:
    """The four owner inputs of :func:`get_unit_economics`, finite and clamped."""
    return {
        "ad_spend": _finite_input(ad_spend, 0.0),
        "kashier_pct": _finite_input(kashier_pct, 3.0, hi=100.0),
        "talabat_pct": _finite_input(talabat_pct, 30.0, hi=100.0),
        "extra_fixed": _finite_input(extra_fixed, 0.0),
    }


@frappe.whitelist()
def get_unit_economics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    ad_spend: Any = 0,
    kashier_pct: Any = 3,
    talabat_pct: Any = 30,
    extra_fixed: Any = 0,
) -> Dict[str, Any]:
    """Contribution per order, fixed costs and break-even for the range.

    Ledger figures come from the P&L report; ``ad_spend`` and ``extra_fixed``
    (owner inputs, not booked) are amounts for the range; ``kashier_pct`` is
    applied to money received into Kashier, ``talabat_pct`` to Talabat-partner
    net sales. Non-finite inputs ("NaN", "inf") are clamped: amounts fall back
    to 0, percentages to their defaults (+inf to 100). Ranges over
    :data:`MAX_RANGE_DAYS` days are refused.
    """
    _ensure_jarz_manager()
    fd, td = _parse_range(date_from, date_to)
    company = fr._default_company()
    inputs = owner_inputs(ad_spend, kashier_pct, talabat_pct, extra_fixed)

    pnl = _profit_and_loss(fd, td, company)
    talabat_sales, _talabat_orders = _talabat_sales(fd, td, company)
    payload = build_unit_economics(
        pnl=pnl,
        kashier_receipts=_kashier_receipts(fd, td, company),
        talabat_sales=talabat_sales,
        commission_accounts=_accounts_like(company, "Expense", "Commission on Sales%"),
        kashier_fee_accounts=_accounts_like(company, "Expense", "%kashier%"),
        **inputs,
    )
    payload["basis"]["talabat_orders"] = _talabat_orders
    return {
        "filters": {"date_from": fd.isoformat(), "date_to": td.isoformat(), "company": company, **inputs},
        **payload,
    }


# ═════════════════════════════════════════════════════════════════════════
# B2  Promo performance
# ═════════════════════════════════════════════════════════════════════════


def aggregate_promo(
    *,
    invoices: List[Dict[str, Any]],
    redemptions: List[Dict[str, Any]],
    baseline_orders: int,
    baseline_revenue: float,
) -> Dict[str, Any]:
    """Pure: promo invoices + redemption rows + all-orders baseline -> payload.

    ``invoices``: one row per promo invoice {invoice, customer, revenue,
    discount, codes (list), is_new, repeated}. ``redemptions``: {invoice, code,
    discount}. An invoice with no redemption row falls back to its own code
    list, with the discount split evenly.
    """
    by_invoice: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for r in redemptions or []:
        if r.get("invoice") and r.get("code"):
            by_invoice[r["invoice"]].append((str(r["code"]), flt(r.get("discount"))))

    codes: Dict[str, Dict[str, Any]] = {}

    def bucket(code: str) -> Dict[str, Any]:
        return codes.setdefault(
            code,
            {"invoices": set(), "discount": 0.0, "revenue": 0.0, "new": set(), "repeat": set()},
        )

    promo_orders = 0
    promo_revenue = 0.0
    discount_total = 0.0
    new_customers: Set[str] = set()
    for inv in invoices or []:
        name = inv.get("invoice")
        if not name:
            continue
        promo_orders += 1
        revenue = flt(inv.get("revenue"))
        promo_revenue += revenue
        discount_total += flt(inv.get("discount"))
        customer = inv.get("customer")
        if customer and cint(inv.get("is_new")):
            new_customers.add(customer)

        attributed = by_invoice.get(name)
        if not attributed:
            fallback = list(inv.get("codes") or []) or ["(unrecorded)"]
            share = flt(inv.get("discount")) / len(fallback)
            attributed = [(c, share) for c in fallback]
        for code, disc in attributed:
            b = bucket(code)
            if name not in b["invoices"]:
                b["invoices"].add(name)
                b["revenue"] += revenue
            b["discount"] += disc
            if customer and cint(inv.get("is_new")):
                b["new"].add(customer)
            if customer and cint(inv.get("repeated")):
                b["repeat"].add(customer)

    code_rows = []
    for code, b in codes.items():
        n = len(b["invoices"])
        code_rows.append({
            "code": code,
            "orders": n,
            "discount": _r2(b["discount"]),
            "revenue": _r2(b["revenue"]),
            "aov": _r2(b["revenue"] / n) if n else 0.0,
            "new_customers": len(b["new"]),
            "repeat_after": len(b["repeat"]),
        })
    code_rows.sort(key=lambda r: (-r["orders"], -r["discount"], r["code"]))

    non_promo_orders = max(int(baseline_orders) - promo_orders, 0)
    non_promo_revenue = flt(baseline_revenue) - promo_revenue
    return {
        "totals": {
            "orders_with_promo": promo_orders,
            "orders_total": int(baseline_orders),
            "discount_total": _r2(discount_total),
            "promo_revenue": _r2(promo_revenue),
            "promo_aov": _r2(promo_revenue / promo_orders) if promo_orders else 0.0,
            "non_promo_aov": _r2(non_promo_revenue / non_promo_orders) if non_promo_orders else 0.0,
            "new_customers_via_promo": len(new_customers),
        },
        "codes": code_rows,
    }


@frappe.whitelist()
def get_promo_performance(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Promo code cost against what the orders brought in.

    Scope: submitted, non-return **consumer** orders (B2B, staff and sample
    orders are excluded so they do not distort the AOV comparison).
    Revenue = ``base_grand_total`` (what the customer paid, after the promo
    discount, delivery included). A promo order = ``custom_promo_discount_total
    > 0``. ``new_customers``: the promo order is the customer's first submitted
    order ever. ``repeat_after``: the customer placed a later order without a
    promo (any time up to today, not only inside the range).
    """
    _ensure_jarz_manager()
    fd, td = _parse_range(date_from, date_to)
    branch = _clean_branch(branch)

    params = {"fd": fd, "td": td, "branch": branch}
    branch_cond = "AND si.pos_profile = %(branch)s" if branch else ""
    b2c = _B2C_COND.format(a="si.")

    base = frappe.db.sql(
        f"""
        SELECT COUNT(*) AS orders, COALESCE(SUM(si.base_grand_total), 0) AS revenue
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND {b2c}
          {branch_cond}
        """,
        params,
        as_dict=True,
    )[0]

    invoice_rows = frappe.db.sql(
        f"""
        SELECT si.name AS invoice, si.customer, si.base_grand_total AS revenue,
               IFNULL(si.custom_promo_discount_total, 0) AS discount,
               si.custom_promo_codes AS codes_raw,
               CASE WHEN NOT EXISTS (
                   SELECT 1 FROM `tabSales Invoice` p
                   WHERE p.customer = si.customer AND p.docstatus = 1 AND p.is_return = 0
                     AND p.name != si.name
                     AND (p.posting_date < si.posting_date
                          OR (p.posting_date = si.posting_date AND p.creation < si.creation))
               ) THEN 1 ELSE 0 END AS is_new,
               CASE WHEN EXISTS (
                   SELECT 1 FROM `tabSales Invoice` n
                   WHERE n.customer = si.customer AND n.docstatus = 1 AND n.is_return = 0
                     AND n.name != si.name
                     AND IFNULL(n.custom_promo_discount_total, 0) = 0
                     AND (n.posting_date > si.posting_date
                          OR (n.posting_date = si.posting_date AND n.creation > si.creation))
               ) THEN 1 ELSE 0 END AS repeated
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND IFNULL(si.custom_promo_discount_total, 0) > 0
          AND {b2c}
          {branch_cond}
        """,
        params,
        as_dict=True,
    )

    from jarz_pos.services import promo_codes as promo_service

    for row in invoice_rows:
        try:
            row["codes"] = promo_service._normalize_codes(row.pop("codes_raw", None))
        except Exception:
            row["codes"] = []

    redemptions: List[Dict[str, Any]] = []
    names = [r["invoice"] for r in invoice_rows]
    if names:
        redemptions = frappe.db.sql(
            """
            SELECT sales_invoice AS invoice, promo_code AS code, discount_applied AS discount
            FROM `tabJarz Promo Redemption`
            WHERE status = 'Active' AND sales_invoice IN %(names)s
            """,
            {"names": tuple(names)},
            as_dict=True,
        )

    payload = aggregate_promo(
        invoices=invoice_rows,
        redemptions=redemptions,
        baseline_orders=cint(base.get("orders")),
        baseline_revenue=flt(base.get("revenue")),
    )
    filters = _base_filters(fd, td, branch)
    filters["scope"] = "b2c"
    return {"filters": filters, **payload}


# ═════════════════════════════════════════════════════════════════════════
# B3  Stock adjustments (waste & count corrections)
# ═════════════════════════════════════════════════════════════════════════


def _profile_warehouses() -> Dict[str, str]:
    """{warehouse: POS Profile} for enabled profiles; first by name wins."""
    out: Dict[str, str] = {}
    for row in frappe.get_all(
        "POS Profile",
        filters={"disabled": 0},
        fields=["name", "warehouse"],
        order_by="name asc",
    ):
        wh = row.get("warehouse")
        if wh and wh not in out:
            out[wh] = row["name"]
    return out


def _consumable_entries(fd: date, td: date) -> List[str]:
    """Stock Entries the consumable deduction created for invoices near the range."""
    try:
        return [
            r[0]
            for r in frappe.db.sql(
                """
                SELECT custom_consumable_stock_entry FROM `tabSales Invoice`
                WHERE posting_date BETWEEN %(a)s AND %(b)s
                  AND IFNULL(custom_consumable_stock_entry, '') != ''
                """,
                {"a": fd - timedelta(days=31), "b": td + timedelta(days=31)},
            )
        ]
    except Exception:
        return []


def _adjustment_scope(fd: date, td: date, branch: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """Shared FROM/WHERE for every stock-adjustment query."""
    params: Dict[str, Any] = {
        "fd": fd,
        "td": td,
        "consumables": tuple(_consumable_entries(fd, td) or [""]),
        "remarks_prefix": CONSUMABLE_REMARKS_PREFIX + "%",
    }
    wh_cond = ""
    if branch:
        wh = frappe.db.get_value("POS Profile", branch, "warehouse")
        if not wh:
            frappe.throw(_("Branch {0} has no warehouse").format(branch))
        bounds = frappe.db.get_value("Warehouse", wh, ["lft", "rgt"], as_dict=True) or {}
        if bounds.get("lft") and bounds.get("rgt"):
            wh_cond = (
                "AND sle.warehouse IN (SELECT w.name FROM `tabWarehouse` w "
                "WHERE w.lft >= %(lft)s AND w.rgt <= %(rgt)s)"
            )
            params.update(lft=bounds["lft"], rgt=bounds["rgt"])
        else:
            wh_cond = "AND sle.warehouse = %(wh)s"
            params["wh"] = wh

    sql = f"""
        FROM `tabStock Ledger Entry` sle
        LEFT JOIN `tabStock Entry` se
          ON sle.voucher_type = 'Stock Entry' AND se.name = sle.voucher_no
        LEFT JOIN `tabStock Reconciliation` sr
          ON sle.voucher_type = 'Stock Reconciliation' AND sr.name = sle.voucher_no
        LEFT JOIN `tabStock Reconciliation Item` sri
          ON sle.voucher_type = 'Stock Reconciliation' AND sri.name = sle.voucher_detail_no
        WHERE sle.is_cancelled = 0
          AND sle.posting_date BETWEEN %(fd)s AND %(td)s
          AND sle.voucher_type IN ('Stock Entry', 'Stock Reconciliation')
          AND (
                (sle.voucher_type = 'Stock Reconciliation'
                 AND IFNULL(sr.purpose, '') != 'Opening Stock')
             OR (sle.voucher_type = 'Stock Entry'
                 AND se.purpose = 'Material Issue'
                 AND IFNULL(se.remarks, '') NOT LIKE %(remarks_prefix)s
                 AND se.name NOT IN %(consumables)s)
          )
          {wh_cond}
    """
    return sql, params


#: Quantity moved by an adjustment row. A non-batch Stock Reconciliation SLE
#: carries actual_qty = 0 and only the new balance, so the change is read off
#: its reconciliation row (counted qty minus the qty before the count).
_QTY_EXPR = (
    "CASE WHEN sle.voucher_type = 'Stock Reconciliation' AND IFNULL(sle.actual_qty, 0) = 0 "
    "THEN IFNULL(sri.qty, 0) - IFNULL(sri.current_qty, 0) ELSE sle.actual_qty END"
)

_TYPE_EXPR = (
    "CASE WHEN sle.voucher_type = 'Stock Reconciliation' THEN 'Stock Reconciliation' "
    "ELSE COALESCE(NULLIF(se.stock_entry_type, ''), se.purpose) END"
)


@frappe.whitelist()
def get_stock_adjustments(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Waste and count adjustments, valued at ``stock_value_difference``.

    Included: Stock Reconciliations (not Opening Stock) and Material Issue
    Stock Entries (any Stock Entry Type whose purpose is Material Issue), minus
    the automatic consumable deductions posted for invoices (packaging used by
    an order is consumption, not waste). ``loss_value`` and ``gain_value`` are
    positive magnitudes; ``net_value`` = gain - loss (negative = net loss).
    With ``branch`` only that POS Profile's warehouse (and its children) count.
    """
    _ensure_jarz_manager()
    fd, td = _parse_range(date_from, date_to)
    branch = _clean_branch(branch)
    scope, params = _adjustment_scope(fd, td, branch)

    totals = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(CASE WHEN sle.stock_value_difference < 0
                                 THEN -sle.stock_value_difference ELSE 0 END), 0) AS loss,
               COALESCE(SUM(CASE WHEN sle.stock_value_difference > 0
                                 THEN sle.stock_value_difference ELSE 0 END), 0) AS gain,
               COALESCE(SUM(sle.stock_value_difference), 0) AS net,
               COUNT(*) AS entries
        {scope}
        """,
        params,
        as_dict=True,
    )[0]

    by_type = frappe.db.sql(
        f"""
        SELECT {_TYPE_EXPR} AS type, COALESCE(SUM(sle.stock_value_difference), 0) AS value,
               COUNT(*) AS entries
        {scope}
        GROUP BY 1
        ORDER BY value ASC
        """,
        params,
        as_dict=True,
    )

    by_wh = frappe.db.sql(
        f"""
        SELECT sle.warehouse,
               COALESCE(SUM(CASE WHEN sle.stock_value_difference < 0
                                 THEN -sle.stock_value_difference ELSE 0 END), 0) AS loss,
               COALESCE(SUM(CASE WHEN sle.stock_value_difference > 0
                                 THEN sle.stock_value_difference ELSE 0 END), 0) AS gain,
               COALESCE(SUM(sle.stock_value_difference), 0) AS net
        {scope}
        GROUP BY sle.warehouse
        ORDER BY net ASC
        """,
        params,
        as_dict=True,
    )

    item_scope = scope.replace(
        "WHERE sle.is_cancelled",
        "LEFT JOIN `tabItem` i ON i.name = sle.item_code\n        WHERE sle.is_cancelled",
        1,
    )
    by_item = frappe.db.sql(
        f"""
        SELECT sle.item_code, MAX(i.item_name) AS item_name, MAX(i.stock_uom) AS uom,
               COALESCE(SUM({_QTY_EXPR}), 0) AS qty,
               COALESCE(SUM(sle.stock_value_difference), 0) AS value
        {item_scope}
        GROUP BY sle.item_code
        ORDER BY value ASC
        LIMIT {MAX_ITEMS}
        """,
        params,
        as_dict=True,
    )

    entries = frappe.db.sql(
        f"""
        SELECT sle.voucher_type, sle.voucher_no, sle.posting_date, sle.warehouse, sle.item_code,
               {_QTY_EXPR} AS qty, sle.stock_value_difference AS value,
               CASE WHEN sle.voucher_type = 'Stock Entry' THEN se.remarks ELSE NULL END AS remarks
        {scope}
        ORDER BY sle.posting_date DESC, sle.posting_time DESC, sle.creation DESC
        LIMIT {MAX_ENTRIES}
        """,
        params,
        as_dict=True,
    )

    wh_profile = _profile_warehouses()
    return {
        "filters": _base_filters(fd, td, branch),
        "totals": {
            "loss_value": _r2(totals.get("loss")),
            "gain_value": _r2(totals.get("gain")),
            "net_value": _r2(totals.get("net")),
            "entries": cint(totals.get("entries")),
        },
        "by_type": [
            {"type": r.get("type") or "", "value": _r2(r.get("value")), "entries": cint(r.get("entries"))}
            for r in by_type
        ],
        "by_branch": [
            {
                "branch": wh_profile.get(r.get("warehouse")),
                "warehouse": r.get("warehouse"),
                "loss_value": _r2(r.get("loss")),
                "gain_value": _r2(r.get("gain")),
                "net_value": _r2(r.get("net")),
            }
            for r in by_wh
        ],
        "by_item": [
            {
                "item_code": r.get("item_code"),
                "item_name": r.get("item_name") or r.get("item_code"),
                "qty": round(flt(r.get("qty")), 3),
                "uom": r.get("uom") or "",
                "value": _r2(r.get("value")),
            }
            for r in by_item
        ],
        "entries": [
            {
                "voucher_type": r.get("voucher_type"),
                "voucher_no": r.get("voucher_no"),
                "posting_date": str(r.get("posting_date") or ""),
                "warehouse": r.get("warehouse"),
                "item_code": r.get("item_code"),
                "qty": round(flt(r.get("qty")), 3),
                "value": _r2(r.get("value")),
                "remarks": r.get("remarks") or "",
            }
            for r in entries
        ],
    }


# ═════════════════════════════════════════════════════════════════════════
# B4  Labour cost
# ═════════════════════════════════════════════════════════════════════════


def prorated_month_cost(
    monthly: float,
    month_start: date,
    month_end: date,
    fd: date,
    td: date,
    joined: Any = None,
    relieved: Any = None,
) -> float:
    """Pure: the part of one month's salary that falls inside ``[fd, td]``.

    Daily rate = monthly / days in the month, over the days the person was
    employed. A leaver relieved inside the month already has ``monthly`` scaled
    down to the days worked by ``_load_payroll``, so their daily rate is that
    amount over the days actually worked (no double proration).
    """
    monthly = flt(monthly)
    if monthly <= 0:
        return 0.0
    month_start, month_end, fd, td = map(getdate, (month_start, month_end, fd, td))
    active_start = max(month_start, getdate(joined)) if joined else month_start
    active_end = month_end
    leaver = False
    if relieved:
        r = getdate(relieved)
        if r < month_start:
            return 0.0
        if r <= month_end:
            active_end, leaver = r, True
    if active_start > active_end:
        return 0.0
    lo, hi = max(active_start, fd), min(active_end, td)
    if lo > hi:
        return 0.0
    overlap = (hi - lo).days + 1
    if leaver:
        # _load_payroll scaled ``monthly`` to the days from the 1st to relieving.
        daily = monthly / ((active_end - month_start).days + 1)
    else:
        daily = monthly / ((month_end - month_start).days + 1)
    return daily * overlap


def allocation_shares(
    hours_by_loc: Dict[Optional[str], float],
    days_by_loc: Dict[Optional[str], int],
    assigned_days_by_loc: Dict[Optional[str], int],
    fallback_loc: Optional[str],
) -> Dict[Optional[str], float]:
    """Pure: how one person's cost splits across shift locations.

    Worked hours first, then attended days, then rostered days, then the
    standing schedule location, else unassigned (``None``).
    """
    for basis in (hours_by_loc, days_by_loc, assigned_days_by_loc):
        total = sum(flt(v) for v in (basis or {}).values())
        if total > 0:
            return {loc: flt(v) / total for loc, v in basis.items() if flt(v) > 0}
    return {fallback_loc: 1.0}


def build_labour_report(
    *,
    records: List[Dict[str, Any]],
    sales_by_branch: Dict[Optional[str], float],
    branch: Optional[str],
    cost_basis: str,
) -> Dict[str, Any]:
    """Pure: per (employee, location) allocations + branch sales -> payload.

    ``records``: {employee, employee_name, branch, shift_location, hours, days,
    cost}. ``branch`` None = every branch; otherwise only that branch's rows.
    A record with no POS Profile (e.g. the factory) keeps ``branch`` None and
    names its ``shift_location``.
    """
    def key_of(b: Optional[str], loc: Optional[str]) -> str:
        return b if b else "~" + (loc or "")

    if branch:
        records = [r for r in records if r.get("branch") == branch]
        sales_by_branch = {branch: flt(sales_by_branch.get(branch))}

    rows: Dict[str, Dict[str, Any]] = {}

    def row_for(b: Optional[str], loc: Optional[str]) -> Dict[str, Any]:
        k = key_of(b, loc)
        if k not in rows:
            rows[k] = {
                "branch": b,
                "shift_location": None if b else loc,
                "labour_cost": 0.0,
                "sales": 0.0,
                "hours": 0.0,
                "_people": set(),
            }
        return rows[k]

    for b, amount in (sales_by_branch or {}).items():
        row_for(b or None, None)["sales"] += flt(amount)

    people: Set[str] = set()
    per_employee: Dict[str, Dict[str, Any]] = {}
    for r in records:
        row = row_for(r.get("branch"), r.get("shift_location"))
        row["labour_cost"] += flt(r.get("cost"))
        row["hours"] += flt(r.get("hours"))
        emp = r.get("employee")
        if not emp:
            continue
        if flt(r.get("cost")) > 0 or flt(r.get("hours")) > 0:
            row["_people"].add(emp)
            people.add(emp)
        e = per_employee.setdefault(emp, {
            "employee": emp,
            "name": r.get("employee_name") or emp,
            "hours": 0.0,
            "days": 0,
            "cost": 0.0,
            "_best": (-1.0, -1.0),
            "branch": None,
            "shift_location": None,
        })
        e["hours"] += flt(r.get("hours"))
        e["days"] += cint(r.get("days"))
        e["cost"] += flt(r.get("cost"))
        rank = (flt(r.get("cost")), flt(r.get("hours")))
        if rank > e["_best"]:
            e["_best"] = rank
            e["branch"] = r.get("branch")
            e["shift_location"] = r.get("shift_location")

    branch_rows = []
    for row in rows.values():
        cost, sales = row["labour_cost"], row["sales"]
        if not cost and not sales and not row["hours"]:
            continue
        branch_rows.append({
            "branch": row["branch"],
            "shift_location": row["shift_location"],
            "labour_cost": _r2(cost),
            "sales": _r2(sales),
            "labour_pct": _ratio_pct(cost, sales) if sales > 0 else None,
            "hours": round(row["hours"], 2),
            "headcount": len(row["_people"]),
        })
    branch_rows.sort(key=lambda r: (-r["labour_cost"], -r["sales"], str(r["branch"] or r["shift_location"] or "")))

    employees = []
    for e in per_employee.values():
        if not e["cost"] and not e["hours"] and not e["days"]:
            continue
        employees.append({
            "employee": e["employee"],
            "name": e["name"],
            "branch": e["branch"],
            "shift_location": e["shift_location"],
            "hours": round(e["hours"], 2),
            "days": int(e["days"]),
            "cost": _r2(e["cost"]),
        })
    employees.sort(key=lambda r: (-r["cost"], -r["hours"], r["name"]))

    labour = sum(flt(r.get("cost")) for r in records)
    hours = sum(flt(r.get("hours")) for r in records)
    sales = sum(flt(v) for v in (sales_by_branch or {}).values())
    return {
        "totals": {
            "labour_cost": _r2(labour),
            "sales": _r2(sales),
            "labour_pct": _ratio_pct(labour, sales) if sales > 0 else None,
            "hours": round(hours, 2),
            "sales_per_hour": _r2(sales / hours) if hours > 0 else None,
            "headcount": len(people),
        },
        "branches": branch_rows,
        "employees": employees,
        "cost_basis": cost_basis,
    }


def _employee_index(company: Optional[str]) -> Dict[str, Dict[str, Any]]:
    filters = {"company": company} if company else {}
    return {
        e["name"]: e
        for e in frappe.get_all(
            "Employee",
            filters=filters,
            fields=["name", "employee_name", "date_of_joining", "relieving_date"],
            limit_page_length=0,
        )
    }


def _salary_slip_costs(fd: date, td: date, company: Optional[str]) -> Dict[str, float]:
    """Gross pay of submitted Salary Slips, prorated to the range by slip days."""
    try:
        if not frappe.db.table_exists("Salary Slip"):
            return {}
        cond = "AND company = %(company)s" if company else ""
        rows = frappe.db.sql(
            f"""
            SELECT employee, start_date, end_date, base_gross_pay AS gross
            FROM `tabSalary Slip`
            WHERE docstatus = 1 AND start_date <= %(td)s AND end_date >= %(fd)s {cond}
            """,
            {"fd": fd, "td": td, "company": company},
            as_dict=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "business_reports: salary slips")
        return {}
    out: Dict[str, float] = defaultdict(float)
    for r in rows:
        s, e = getdate(r["start_date"]), getdate(r["end_date"])
        span = (e - s).days + 1
        lo, hi = max(s, fd), min(e, td)
        if span <= 0 or lo > hi:
            continue
        out[r["employee"]] += flt(r.get("gross")) * ((hi - lo).days + 1) / span
    return dict(out)


def _payroll_costs(
    fd: date, td: date, company: Optional[str], employees: Dict[str, Dict[str, Any]]
) -> Tuple[Dict[str, float], str]:
    """Per-employee labour cost for the range and the basis it came from."""
    slips = _salary_slip_costs(fd, td, company)
    costs: Dict[str, float] = defaultdict(float)
    configured = False
    try:
        from jarz_pos.api.recurring_expenses import _load_payroll

        for m_start, m_end in fr.months_in_range(fd, td):
            payroll = _load_payroll(company, m_end)
            for row in payroll.get("rows") or []:
                emp = row.get("employee")
                if not emp or emp in slips:
                    continue
                info = employees.get(emp) or {}
                cost = prorated_month_cost(
                    flt(row.get("monthly")), m_start, m_end, fd, td,
                    joined=info.get("date_of_joining"),
                    relieved=row.get("relieving_date") or info.get("relieving_date"),
                )
                if flt(row.get("monthly")) > 0:
                    configured = True
                costs[emp] += cost
    except Exception:
        frappe.log_error(frappe.get_traceback(), "business_reports: payroll load")
    for emp, cost in slips.items():
        costs[emp] += cost
    if slips:
        return dict(costs), "salary_slips"
    if configured:
        return dict(costs), "recurring_expense"
    return {}, "estimate"


def _payroll_ledger_total(fd: date, td: date, company: Optional[str]) -> float:
    """Net debits on payroll expense ledgers in the range (the estimate basis)."""
    try:
        from jarz_pos.api.recurring_expenses import _payroll_expense_accounts

        candidates = _payroll_expense_accounts(company)
        accounts = frappe.get_all(
            "Account",
            filters={"name": ["in", candidates or [""]], "root_type": "Expense"},
            pluck="name",
        )
        if not accounts:
            return 0.0
        row = frappe.db.sql(
            """
            SELECT COALESCE(SUM(debit - credit), 0) FROM `tabGL Entry`
            WHERE is_cancelled = 0 AND posting_date BETWEEN %(fd)s AND %(td)s
              AND account IN %(accounts)s AND voucher_type != 'Period Closing Voucher'
            """,
            {"fd": fd, "td": td, "accounts": tuple(accounts)},
        )
        return flt(row[0][0]) if row else 0.0
    except Exception:
        frappe.log_error(frappe.get_traceback(), "business_reports: payroll ledger")
        return 0.0


def _shift_assignments(fd: date, td: date) -> List[Dict[str, Any]]:
    try:
        from jarz_pos.services import roster as roster_service

        if not roster_service.hrms_available():
            return []
        return frappe.get_all(
            "Shift Assignment",
            filters={
                "docstatus": 1,
                "status": ["in", list(roster_service.READABLE_ASSIGNMENT_STATUSES)],
                "start_date": ["<=", td],
            },
            or_filters=[["end_date", ">=", fd], ["end_date", "is", "not set"]],
            fields=["employee", "shift_location", "start_date", "end_date"],
            order_by="start_date desc",
            limit_page_length=0,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "business_reports: shift assignments")
        return []


def _schedule_locations() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for row in frappe.get_all(
            "Shift Schedule Assignment",
            filters={"enabled": 1},
            fields=["employee", "shift_location"],
            limit_page_length=0,
        ):
            if row.get("shift_location") and row["employee"] not in out:
                out[row["employee"]] = row["shift_location"]
    except Exception:
        pass
    return out


def _worked_groups(fd: date, td: date) -> List[Dict[str, Any]]:
    """Check-in day groups inside the range, dated as the attendance board dates them."""
    try:
        if not frappe.db.table_exists("Employee Checkin"):
            return []
        from jarz_pos.services import attendance as attendance_service

        rows = frappe.db.sql(
            """
            SELECT employee, time, shift_start FROM `tabEmployee Checkin`
            WHERE time >= %(a)s AND time < %(b)s
            ORDER BY time ASC
            """,
            {
                "a": datetime.combine(fd - timedelta(days=1), datetime.min.time()),
                "b": datetime.combine(td + timedelta(days=2), datetime.min.time()),
            },
            as_dict=True,
        )
        out = []
        for (employee, day_key), group in attendance_service.group_checkins(rows).items():
            day = getdate(day_key)
            if day < fd or day > td:
                continue
            out.append({
                "employee": employee,
                "date": day,
                "hours": flt(attendance_service.worked_hours_for(group) or 0.0),
            })
        return out
    except Exception:
        frappe.log_error(frappe.get_traceback(), "business_reports: check-ins")
        return []


def _branch_sales(fd: date, td: date, company: Optional[str]) -> Dict[Optional[str], float]:
    cond = "AND company = %(company)s" if company else ""
    return {
        (r[0] or None): flt(r[1])
        for r in frappe.db.sql(
            f"""
            SELECT pos_profile, COALESCE(SUM(base_net_total), 0)
            FROM `tabSales Invoice`
            WHERE docstatus = 1 AND posting_date BETWEEN %(fd)s AND %(td)s {cond}
            GROUP BY pos_profile
            """,
            {"fd": fd, "td": td, "company": company},
        )
    }


def _labour_records(fd: date, td: date, company: Optional[str]) -> Tuple[List[Dict[str, Any]], str]:
    """Per (employee, shift location) hours, days and allocated cost."""
    from jarz_pos.services import branch_access

    employees = _employee_index(company)
    loc_to_profile = branch_access.profiles_by_shift_location()
    assignments = _shift_assignments(fd, td)
    schedule_loc = _schedule_locations()
    groups = _worked_groups(fd, td)
    costs, basis = _payroll_costs(fd, td, company, employees)

    by_emp_assign: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in assignments:
        by_emp_assign[a["employee"]].append(a)

    def location_on(emp: str, day: date) -> Optional[str]:
        for a in by_emp_assign.get(emp, []):  # newest start first
            start = getdate(a["start_date"])
            end = getdate(a["end_date"]) if a.get("end_date") else None
            if start <= day and (end is None or end >= day):
                return a.get("shift_location") or None
        return None

    hours: Dict[str, Dict[Optional[str], float]] = defaultdict(lambda: defaultdict(float))
    days: Dict[str, Dict[Optional[str], int]] = defaultdict(lambda: defaultdict(int))
    for g in groups:
        emp = g["employee"]
        if employees and emp not in employees:
            continue
        loc = location_on(emp, g["date"]) or schedule_loc.get(emp)
        hours[emp][loc] += g["hours"]
        days[emp][loc] += 1

    def assigned_days(emp: str) -> Dict[Optional[str], int]:
        out: Dict[Optional[str], int] = defaultdict(int)
        for a in by_emp_assign.get(emp, []):
            start = max(getdate(a["start_date"]), fd)
            end = min(getdate(a["end_date"]) if a.get("end_date") else td, td)
            if start <= end:
                out[a.get("shift_location") or None] += (end - start).days + 1
        return out

    total_hours = sum(sum(v.values()) for v in hours.values())
    if basis == "estimate":
        # No payroll configured: spread the payroll ledger over hours worked.
        ledger = _payroll_ledger_total(fd, td, company)
        if ledger and total_hours > 0:
            costs = {emp: ledger * sum(h.values()) / total_hours for emp, h in hours.items()}
        elif ledger:
            costs = {"": ledger}

    records: List[Dict[str, Any]] = []
    for emp in set(costs) | set(hours) | set(days):
        cost = flt(costs.get(emp))
        if not emp:
            records.append({"employee": None, "employee_name": None, "branch": None,
                            "shift_location": None, "hours": 0.0, "days": 0, "cost": cost})
            continue
        shares = allocation_shares(hours.get(emp, {}), days.get(emp, {}), assigned_days(emp),
                                   schedule_loc.get(emp))
        locs = set(shares) | set(hours.get(emp, {})) | set(days.get(emp, {}))
        for loc in locs:
            records.append({
                "employee": emp,
                "employee_name": (employees.get(emp) or {}).get("employee_name") or emp,
                "branch": loc_to_profile.get(loc) if loc else None,
                "shift_location": loc,
                "hours": flt(hours.get(emp, {}).get(loc)),
                "days": cint(days.get(emp, {}).get(loc)),
                "cost": cost * flt(shares.get(loc)),
            })
    return records, basis


@frappe.whitelist()
def get_labour_cost(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Labour cost against sales and hours, per branch and per person.

    Cost (``cost_basis``): submitted Salary Slips prorated by slip days if any
    exist; else each employee's Salary Structure Assignment (base + variable,
    as the Monthly Expenses board pays it) prorated by calendar day, clipped to
    joining/relieving dates; else ("estimate") the payroll expense ledgers in
    the range spread by hours worked. Off-payroll pay is not included.

    Hours: last check-in minus first per shift day (``services/attendance``
    rules). A person's cost is split across branches by hours worked there,
    then by days attended, then by rostered days, then their standing schedule
    location. A location with no POS Profile (e.g. the factory) appears with
    ``branch`` null and its ``shift_location`` name. Sales = submitted invoice
    net totals by ``pos_profile`` (returns as negatives).
    """
    _ensure_jarz_manager()
    fd, td = _parse_range(date_from, date_to)
    branch = _clean_branch(branch)
    company = fr._default_company()

    records, basis = _labour_records(fd, td, company)
    payload = build_labour_report(
        records=records,
        sales_by_branch=_branch_sales(fd, td, company),
        branch=branch,
        cost_basis=basis,
    )
    return {"filters": _base_filters(fd, td, branch), **payload}

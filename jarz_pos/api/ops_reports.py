"""Operations reports: daily close, cash over/short and fulfilment SLA.

Endpoints (all ``@frappe.whitelist()``):

* ``jarz_pos.api.ops_reports.get_report_branches``
* ``jarz_pos.api.ops_reports.get_daily_close``
* ``jarz_pos.api.ops_reports.get_cash_over_short``
* ``jarz_pos.api.ops_reports.get_fulfilment_sla``

Access: JARZ Manager, Administrator, System Manager and the line-manager tier
(``ROLES.LINE_MANAGER_TIER``). JARZ Manager / Administrator / System Manager
see every branch. A user who holds only the line-manager role is scoped to the
POS Profiles they are assigned to (``get_user_pos_profiles``): asking for any
other branch is a PermissionError, and "all branches" means "all of mine".

Conventions shared by every endpoint:

* Branch = POS Profile. ``branch`` is optional; empty / None = all branches.
* ``date_from`` / ``date_to`` are ``YYYY-MM-DD``, inclusive. Missing values
  default to the first of this month .. today, and a reversed range is swapped.
* Money is company currency, plain floats rounded to 2 places. Rounding uses
  Python ``round`` on purpose: ``flt(x, 2)`` goes through the precision lookup,
  which raises without a site and is swallowed into ``0.0``.
* Every response carries ``filters`` echoing what was actually applied.

The ``build_*`` functions are pure (rows in, payload out) so they can be tested
without a site; the whitelisted functions only gather rows with set-based SQL.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import frappe
from frappe import _
from frappe.utils import cint, get_first_day, getdate, now_datetime, nowdate

from jarz_pos.constants import ACCOUNTS, ROLES

# ── constants ─────────────────────────────────────────────────────────────

NO_BRANCH = "(No Profile)"

#: Channel keys the spec guarantees, always emitted in this order.
SPEC_CHANNELS: Tuple[str, ...] = ("pos", "woo", "talabat", "b2b", "staff")
#: Extra channels, emitted only when they carry data (additive keys).
EXTRA_CHANNELS: Tuple[str, ...] = ("samples", "partner")

STATE_OFD = "Out for Delivery"
STATE_DELIVERED = "Delivered"
STATE_CANCELLED = "Cancelled"
STATE_RETURNED = "Returned"

DEFAULT_LATE_THRESHOLD_MIN = 90
SHIFTS_CAP = 300
LATE_CAP = 100
CANCEL_REASONS_CAP = 50
SLA_MAX_DAYS = 366
_CHUNK = 1000

#: Roles that see every branch.
_ALL_BRANCH_ROLES_LOWER = {
    r.lower() for r in (ROLES.JARZ_MANAGER, ROLES.ADMINISTRATOR, ROLES.SYSTEM_MANAGER)
}

#: "Shift cash discrepancy for {opening} → {closing|Opening}. Expected ..."
_REMARK_RE = re.compile(r"(\S+)\s*→\s*(\S+)")


# ── small helpers ─────────────────────────────────────────────────────────


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _r(value: Any, places: int = 2) -> float:
    out = round(_f(value), places)
    return 0.0 if out == 0 else out


def _iso(value: Any) -> Optional[str]:
    dt = _dt(value)
    return dt.replace(microsecond=0).isoformat() if dt else None


def _dt(value: Any) -> Optional[datetime]:
    """Coerce a DB value to a naive datetime (server local time)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    try:
        return datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _seconds_of_day(value: Any) -> Optional[float]:
    """A Frappe Time value (timedelta, time or 'HH:MM[:SS]') as seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second
    try:
        parts = [float(p) for p in str(value).strip().split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.append(0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _minutes(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 60.0


def median(values: Sequence[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return round(vals[mid], 1)
    return round((vals[mid - 1] + vals[mid]) / 2.0, 1)


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile (``pct`` in 0..100)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    rank = max(1, int(math.ceil(pct / 100.0 * len(vals))))
    return round(vals[min(rank, len(vals)) - 1], 1)


def _chunks(seq: Sequence[str], size: int = _CHUNK) -> Iterable[Sequence[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _parse_dates(date_from: Optional[str], date_to: Optional[str]) -> Tuple[date, date]:
    today = getdate(nowdate())
    fd = getdate(date_from) if date_from else getdate(get_first_day(today))
    td = getdate(date_to) if date_to else today
    if fd > td:
        fd, td = td, fd
    return fd, td


def _day_bounds(fd: date, td: date) -> Tuple[datetime, datetime]:
    """[fd 00:00, td+1 00:00) -- a sargable range for Datetime columns."""
    return datetime.combine(fd, time.min), datetime.combine(td + timedelta(days=1), time.min)


def _has_col(doctype: str, column: str) -> bool:
    try:
        return bool(frappe.db.has_column(doctype, column))
    except Exception:  # noqa: BLE001 - a missing table reads as a missing column
        return False


def _si_col(column: str, alias: str = "si", default: str = "NULL") -> str:
    """``alias.column`` when Sales Invoice has it, else a literal default."""
    return f"{alias}.`{column}`" if _has_col("Sales Invoice", column) else default


# ── access ────────────────────────────────────────────────────────────────


def _ensure_ops_report_access() -> Optional[List[str]]:
    """Gate every ops report; return the branch scope.

    Returns ``None`` when the user may see every branch, otherwise the list of
    POS Profiles a line manager is assigned to (possibly empty).
    """
    user = frappe.session.user
    roles = {str(r).strip().lower() for r in (frappe.get_roles(user) or [])}
    if user == "Administrator" or roles & _ALL_BRANCH_ROLES_LOWER:
        return None
    if roles & ROLES.LINE_MANAGER_TIER_LOWER:
        from jarz_pos.utils.access_control import get_user_pos_profiles

        return sorted(set(get_user_pos_profiles(user) or []))
    frappe.throw(_("You are not permitted to access this report"), frappe.PermissionError)
    return []  # unreachable; keeps type checkers quiet


def _resolve_scope(branch: Optional[str], allowed: Optional[List[str]]) -> Tuple[Optional[str], Optional[List[str]]]:
    """Return (branch, scope). ``scope`` None = unrestricted; a list = only these."""
    branch = (str(branch).strip() if branch else "") or None
    if branch:
        if allowed is not None and branch not in allowed:
            frappe.throw(
                _("You are not permitted to view branch {0}").format(branch), frappe.PermissionError
            )
        return branch, [branch]
    return None, allowed


def _scope_sql(column_sql: str, scope: Optional[List[str]], params: Dict[str, Any]) -> str:
    if scope is None:
        return ""
    if not scope:
        return " AND 1 = 0"
    params["scope_branches"] = tuple(scope)
    return f" AND {column_sql} IN %(scope_branches)s"


def _filters_echo(fd: date, td: date, branch: Optional[str], allowed: Optional[List[str]], **extra) -> Dict[str, Any]:
    out = {
        "date_from": fd.isoformat(),
        "date_to": td.isoformat(),
        "branch": branch,
        # Additive: the branches a line manager is limited to (None = all).
        "scoped_to": list(allowed) if allowed is not None else None,
    }
    out.update(extra)
    return out


# ── A0: branches ──────────────────────────────────────────────────────────


@frappe.whitelist()
def get_report_branches():
    """Non-disabled POS Profiles (= branches), sorted by name.

    A line-manager-only user receives just the branches assigned to them.
    """
    allowed = _ensure_ops_report_access()
    rows = frappe.get_all(
        "POS Profile",
        filters={"disabled": 0},
        fields=["name", "warehouse", "company"],
        order_by="name asc",
        limit_page_length=0,
    ) or []
    if allowed is not None:
        keep = set(allowed)
        rows = [r for r in rows if r.get("name") in keep]
    return {
        "branches": [
            {"name": r.get("name"), "warehouse": r.get("warehouse"), "company": r.get("company")}
            for r in rows
        ]
    }


# ── A1: daily close ───────────────────────────────────────────────────────


def _woo_origin_sql(alias: str = "si") -> str:
    """SQL truth for "this invoice is a Woo-origin (online) order".

    Mirrors ``services.territory_exceptions.detect_order_lane``:
    * ``custom_order_channel`` (stamped on POS-created orders since 2026-08-19,
      values flutter/web/desk/woo) wins when present;
    * otherwise the Woo app's own markers: ``woo_source_type`` not in ('', 'pos')
      or a non-empty ``woo_transaction_id``.
    ``woo_order_id`` alone does NOT mean Woo origin (POS orders pushed to Woo
    carry one too); it is used only when neither Woo marker column exists.
    Woo-app columns are referenced by name only, behind ``has_column``.
    """
    a = alias
    origin: List[str] = []
    if _has_col("Sales Invoice", "woo_source_type"):
        origin.append(f"LOWER(TRIM(IFNULL({a}.`woo_source_type`, ''))) NOT IN ('', 'pos')")
    if _has_col("Sales Invoice", "woo_transaction_id"):
        origin.append(f"TRIM(IFNULL({a}.`woo_transaction_id`, '')) != ''")
    if not origin and _has_col("Sales Invoice", "woo_order_id"):
        origin.append(f"IFNULL({a}.`woo_order_id`, 0) != 0")
    origin_sql = "(" + " OR ".join(origin) + ")" if origin else "0"
    if _has_col("Sales Invoice", "custom_order_channel"):
        ch = f"LOWER(TRIM(IFNULL({a}.`custom_order_channel`, '')))"
        return f"({ch} = 'woo' OR ({ch} = '' AND {origin_sql}))"
    return origin_sql


def _channel_sql(alias: str = "si") -> str:
    """CASE expression mapping an invoice to its reporting channel.

    Precedence (first match wins):
      b2b      custom_order_purpose = 'B2B Supply'
      staff    custom_order_purpose = 'Employee'
      samples  custom_order_purpose LIKE 'Sample%'          (extra key)
      talabat  sales_partner contains 'talabat'
      partner  any other sales_partner                       (extra key)
      woo      Woo-origin order (see ``_woo_origin_sql``)
      pos      everything else (walk-in / phone / app orders)
    """
    a = alias
    purpose = _si_col("custom_order_purpose", a, "''")
    return f"""CASE
        WHEN IFNULL({purpose}, '') = 'B2B Supply' THEN 'b2b'
        WHEN IFNULL({purpose}, '') = 'Employee' THEN 'staff'
        WHEN IFNULL({purpose}, '') LIKE 'Sample%%' THEN 'samples'
        WHEN LOWER(IFNULL({a}.sales_partner, '')) LIKE '%%talabat%%' THEN 'talabat'
        WHEN IFNULL({a}.sales_partner, '') != '' THEN 'partner'
        WHEN {_woo_origin_sql(a)} THEN 'woo'
        ELSE 'pos'
    END"""


def _channel_list(acc: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
    out = []
    for ch in SPEC_CHANNELS:
        row = acc.get(ch) or {}
        out.append({"channel": ch, "orders": int(row.get("orders", 0)), "net_sales": _r(row.get("net_sales"))})
    for ch in EXTRA_CHANNELS:
        row = acc.get(ch)
        if row and (row.get("orders") or _r(row.get("net_sales"))):
            out.append({"channel": ch, "orders": int(row.get("orders", 0)), "net_sales": _r(row.get("net_sales"))})
    return out


def _payment_list(acc: Dict[str, float]) -> List[Dict[str, Any]]:
    rows = [{"mode": m, "amount": _r(v)} for m, v in acc.items() if _r(v) != 0]
    rows.sort(key=lambda r: (-abs(r["amount"]), r["mode"]))
    return rows


def build_daily_close(
    filters: Dict[str, Any],
    invoice_rows: List[Dict[str, Any]],
    shipping_rows: List[Dict[str, Any]],
    cancelled_rows: List[Dict[str, Any]],
    payment_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Assemble the daily-close payload from grouped rows.

    ``invoice_rows``: branch, channel, hour, orders, return_count, net_sales,
    gross_net, returns_net, grand_total, outstanding.
    ``shipping_rows``: branch, amount. ``cancelled_rows``: branch, cancelled.
    ``payment_rows``: branch, mode, amount.
    """
    totals = {"orders": 0, "net_sales": 0.0, "grand_total": 0.0, "gross": 0.0,
              "returns_net": 0.0, "return_count": 0, "outstanding": 0.0}
    channels: Dict[str, Dict[str, float]] = {}
    by_hour = {h: {"orders": 0, "net_sales": 0.0} for h in range(24)}
    branches: Dict[str, Dict[str, Any]] = {}

    def branch_acc(name: str) -> Dict[str, Any]:
        return branches.setdefault(
            name, {"orders": 0, "net_sales": 0.0, "shipping_income": 0.0, "channels": {}, "payments": {}}
        )

    for row in invoice_rows:
        b = row.get("branch") or NO_BRANCH
        ch = row.get("channel") or "pos"
        orders = int(row.get("orders") or 0)
        net = _f(row.get("net_sales"))
        totals["orders"] += orders
        totals["net_sales"] += net
        totals["grand_total"] += _f(row.get("grand_total"))
        totals["gross"] += _f(row.get("gross_net"))
        totals["returns_net"] += _f(row.get("returns_net"))
        totals["return_count"] += int(row.get("return_count") or 0)
        totals["outstanding"] += _f(row.get("outstanding"))

        c = channels.setdefault(ch, {"orders": 0, "net_sales": 0.0})
        c["orders"] += orders
        c["net_sales"] += net

        hour = row.get("hour")
        try:
            hour = int(hour)
        except (TypeError, ValueError):
            hour = None
        if hour is not None and 0 <= hour <= 23:
            by_hour[hour]["orders"] += orders
            by_hour[hour]["net_sales"] += net

        ba = branch_acc(b)
        ba["orders"] += orders
        ba["net_sales"] += net
        bc = ba["channels"].setdefault(ch, {"orders": 0, "net_sales": 0.0})
        bc["orders"] += orders
        bc["net_sales"] += net

    shipping_total = 0.0
    for row in shipping_rows:
        amt = _f(row.get("amount"))
        shipping_total += amt
        branch_acc(row.get("branch") or NO_BRANCH)["shipping_income"] += amt

    cancelled_total = 0
    cancelled_by_branch: Dict[str, int] = {}
    for row in cancelled_rows:
        n = int(row.get("cancelled") or 0)
        cancelled_total += n
        b = row.get("branch") or NO_BRANCH
        cancelled_by_branch[b] = cancelled_by_branch.get(b, 0) + n

    payments: Dict[str, float] = {}
    for row in payment_rows:
        mode = (row.get("mode") or "").strip() or "Unspecified"
        amt = _f(row.get("amount"))
        payments[mode] = payments.get(mode, 0.0) + amt
        bp = branch_acc(row.get("branch") or NO_BRANCH)["payments"]
        bp[mode] = bp.get(mode, 0.0) + amt

    branch_list = []
    for name, acc in branches.items():
        branch_list.append({
            "branch": name,
            "orders": int(acc["orders"]),
            "net_sales": _r(acc["net_sales"]),
            "shipping_income": _r(acc["shipping_income"]),
            "channels": _channel_list(acc["channels"]),
            "payments": _payment_list(acc["payments"]),
            "cancelled": int(cancelled_by_branch.get(name, 0)),  # additive
        })
    for name, n in cancelled_by_branch.items():
        if name not in branches:
            branch_list.append({
                "branch": name, "orders": 0, "net_sales": 0.0, "shipping_income": 0.0,
                "channels": _channel_list({}), "payments": [], "cancelled": int(n),
            })
    branch_list.sort(key=lambda r: (-r["net_sales"], r["branch"]))

    orders = int(totals["orders"])
    return {
        "filters": filters,
        "totals": {
            "orders": orders,
            "net_sales": _r(totals["net_sales"]),
            "grand_total": _r(totals["grand_total"]),
            "shipping_income": _r(shipping_total),
            # Positive magnitude of credit notes; already netted out of net_sales.
            "returns": _r(-totals["returns_net"]),
            "cancelled": int(cancelled_total),
            "avg_order_value": _r(totals["gross"] / orders) if orders else 0.0,
            # additive
            "gross_sales": _r(totals["gross"]),
            "returns_count": int(totals["return_count"]),
            "outstanding": _r(totals["outstanding"]),
        },
        "channels": _channel_list(channels),
        "branches": branch_list,
        "payments": _payment_list(payments),
        "by_hour": [
            {"hour": h, "orders": int(v["orders"]), "net_sales": _r(v["net_sales"])}
            for h, v in sorted(by_hour.items())
        ],
    }


@frappe.whitelist()
def get_daily_close(date_from=None, date_to=None, branch=None):
    """Sales by branch x channel for a date range (submitted Sales Invoices).

    * Selection: ``docstatus = 1`` and ``posting_date`` in range; credit notes
      (``is_return = 1``) are included with their negative values, so
      ``net_sales`` is net of returns. Branch = ``Sales Invoice.pos_profile``.
    * ``orders`` counts non-return invoices; ``avg_order_value`` = non-return
      net sales / orders; ``returns`` = credit-note net value as a positive
      number; ``grand_total`` includes tax and shipping.
    * ``shipping_income`` = invoice tax rows described ``Shipping Income%``
      (``base_tax_amount``), the same source as the P&L report.
    * ``cancelled`` = cancelled (``docstatus = 2``) non-return invoices in range,
      excluding amendments (``custom_cancellation_type = 'Amended'`` or an
      invoice that has an ``amended_from`` successor).
    * ``payments`` by Mode of Payment = POS payment rows (``is_pos`` invoices,
      ``Sales Invoice Payment.base_amount``) + submitted Payment Entries
      allocated against the invoices in range (``Pay`` entries count negative),
      whatever the Payment Entry date. Money settled by Journal Entry (partner /
      courier settlements) is not a Mode of Payment and is not listed; it shows
      in ``totals.outstanding`` until settled.
    * ``by_hour`` buckets on ``HOUR(posting_time)``.
    * Channels: see ``_channel_sql``.
    """
    allowed = _ensure_ops_report_access()
    fd, td = _parse_dates(date_from, date_to)
    branch, scope = _resolve_scope(branch, allowed)

    params: Dict[str, Any] = {"fd": fd, "td": td, "nb": NO_BRANCH}
    cond = _scope_sql("si.pos_profile", scope, params)
    branch_expr = "IFNULL(NULLIF(si.pos_profile, ''), %(nb)s)"
    channel = _channel_sql("si")

    invoice_rows = frappe.db.sql(
        f"""
        SELECT {branch_expr} AS branch,
               {channel} AS channel,
               HOUR(si.posting_time) AS `hour`,
               SUM(CASE WHEN si.is_return = 0 THEN 1 ELSE 0 END) AS orders,
               SUM(CASE WHEN si.is_return = 1 THEN 1 ELSE 0 END) AS return_count,
               SUM(si.base_net_total) AS net_sales,
               SUM(CASE WHEN si.is_return = 0 THEN si.base_net_total ELSE 0 END) AS gross_net,
               SUM(CASE WHEN si.is_return = 1 THEN si.base_net_total ELSE 0 END) AS returns_net,
               SUM(si.base_grand_total) AS grand_total,
               SUM(CASE WHEN si.is_return = 0 THEN si.outstanding_amount ELSE 0 END) AS outstanding
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY `branch`, `channel`, `hour`
        """,
        params,
        as_dict=True,
    )

    shipping_rows = frappe.db.sql(
        f"""
        SELECT {branch_expr} AS branch, SUM(stc.base_tax_amount) AS amount
        FROM `tabSales Taxes and Charges` stc
        JOIN `tabSales Invoice` si
          ON si.name = stc.parent AND stc.parenttype = 'Sales Invoice'
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND stc.description LIKE 'Shipping Income%%'
          {cond}
        GROUP BY branch
        """,
        params,
        as_dict=True,
    )

    cancel_type = _si_col("custom_cancellation_type", "si", "''")
    cancelled_rows = frappe.db.sql(
        f"""
        SELECT {branch_expr} AS branch, COUNT(*) AS cancelled
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 2
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND IFNULL({cancel_type}, '') != 'Amended'
          AND si.name NOT IN (
              SELECT a.amended_from FROM `tabSales Invoice` a
              WHERE IFNULL(a.amended_from, '') != ''
          )
          {cond}
        GROUP BY branch
        """,
        params,
        as_dict=True,
    )

    pos_payment_rows = frappe.db.sql(
        f"""
        SELECT {branch_expr} AS branch, sip.mode_of_payment AS mode, SUM(sip.base_amount) AS amount
        FROM `tabSales Invoice Payment` sip
        JOIN `tabSales Invoice` si
          ON si.name = sip.parent AND sip.parenttype = 'Sales Invoice'
        WHERE si.docstatus = 1
          AND si.is_pos = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY branch, mode
        """,
        params,
        as_dict=True,
    )

    pe_rows = frappe.db.sql(
        f"""
        SELECT {branch_expr} AS branch,
               IFNULL(NULLIF(pe.mode_of_payment, ''), 'Unspecified') AS mode,
               SUM(CASE WHEN pe.payment_type = 'Pay' THEN -ABS(per.allocated_amount)
                        ELSE per.allocated_amount END) AS amount
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent AND per.parenttype = 'Payment Entry'
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE per.reference_doctype = 'Sales Invoice'
          AND pe.docstatus = 1
          AND si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY branch, mode
        """,
        params,
        as_dict=True,
    )

    return build_daily_close(
        _filters_echo(fd, td, branch, allowed),
        invoice_rows or [],
        shipping_rows or [],
        cancelled_rows or [],
        list(pos_payment_rows or []) + list(pe_rows or []),
    )


# ── A2: cash over / short ─────────────────────────────────────────────────


def parse_discrepancy_remark(remark: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (opening_entry, closing_label) from a shift discrepancy JE remark.

    ``closing_label`` is a POS Closing Entry name, or ``"Opening"`` for the
    variance posted when the shift was opened.
    """
    if not remark:
        return None, None
    m = _REMARK_RE.search(str(remark))
    if not m:
        return None, None
    opening = m.group(1).strip().rstrip(".,;:")
    closing = m.group(2).strip().rstrip(".,;:")
    return (opening or None), (closing or None)


def _over_short(diff: float) -> Tuple[float, float]:
    """(over, short) for one signed difference; short is a positive number."""
    return (diff, 0.0) if diff > 0 else (0.0, -diff)


def build_cash_over_short(
    filters: Dict[str, Any],
    fd: date,
    shift_rows: List[Dict[str, Any]],
    ledger_rows: List[Dict[str, Any]],
    opening_branch: Dict[str, str],
    scope: Optional[List[str]],
) -> Dict[str, Any]:
    """Assemble the cash over/short payload.

    ``shift_rows`` (newest first): closing, opening, branch, user, full_name,
    opened, closed, expected, counted, difference.
    ``ledger_rows``: voucher_no, posting_date, user_remark, credit, debit on the
    Cash Over/Short account(s). ``opening_branch``: opening entry -> branch.
    """
    totals = {"shifts": 0, "over": 0.0, "short": 0.0, "net": 0.0, "shifts_with_diff": 0}
    by_branch: Dict[str, Dict[str, float]] = {}
    by_user: Dict[str, Dict[str, Any]] = {}

    # Opening-time variances (closing label "Opening") per opening entry.
    opening_var: Dict[str, float] = {}
    ledger = {
        "over": 0.0, "short": 0.0, "net": 0.0,
        "at_close_net": 0.0, "at_open_net": 0.0, "unlinked_net": 0.0, "entries": 0,
    }
    for row in ledger_rows:
        credit, debit = _f(row.get("credit")), _f(row.get("debit"))
        net = credit - debit  # surplus credits over/short, shortage debits it
        opening, closing = parse_discrepancy_remark(row.get("user_remark"))
        if closing == "Opening" and opening:
            opening_var[opening] = opening_var.get(opening, 0.0) + net
        if getdate(row.get("posting_date")) < fd:
            continue  # fetched one day early only to map opening variances
        b = opening_branch.get(opening) if opening else None
        if scope is not None and b not in scope:
            continue
        ledger["entries"] += 1
        ledger["over"] += credit
        ledger["short"] += debit
        ledger["net"] += net
        if not opening:
            ledger["unlinked_net"] += net
        elif closing == "Opening":
            ledger["at_open_net"] += net
        else:
            ledger["at_close_net"] += net

    shifts_out: List[Dict[str, Any]] = []
    for row in shift_rows:
        diff = _f(row.get("difference"))
        over, short = _over_short(diff)
        b = row.get("branch") or NO_BRANCH
        user = row.get("user") or ""
        totals["shifts"] += 1
        totals["over"] += over
        totals["short"] += short
        totals["net"] += diff
        if abs(round(diff, 2)) >= 0.01:
            totals["shifts_with_diff"] += 1

        ba = by_branch.setdefault(b, {"shifts": 0, "over": 0.0, "short": 0.0, "net": 0.0})
        ba["shifts"] += 1
        ba["over"] += over
        ba["short"] += short
        ba["net"] += diff

        ua = by_user.setdefault(
            user, {"full_name": row.get("full_name") or user, "shifts": 0, "over": 0.0, "short": 0.0, "net": 0.0}
        )
        ua["shifts"] += 1
        ua["over"] += over
        ua["short"] += short
        ua["net"] += diff

        if len(shifts_out) < SHIFTS_CAP:
            shifts_out.append({
                "name": row.get("closing"),
                "branch": b,
                "user": user,
                "full_name": row.get("full_name") or user,
                "opened": _iso(row.get("opened")),
                "closed": _iso(row.get("closed")),
                "expected": _r(row.get("expected")),
                "counted": _r(row.get("counted")),
                "difference": _r(diff),
                # additive
                "opening_entry": row.get("opening"),
                "opening_difference": _r(opening_var.get(row.get("opening") or "", 0.0)),
            })

    return {
        "filters": filters,
        "totals": {
            "shifts": int(totals["shifts"]),
            "over": _r(totals["over"]),
            "short": _r(totals["short"]),
            "net": _r(totals["net"]),
            "shifts_with_diff": int(totals["shifts_with_diff"]),
        },
        "branches": sorted(
            (
                {"branch": b, "shifts": int(v["shifts"]), "over": _r(v["over"]),
                 "short": _r(v["short"]), "net": _r(v["net"])}
                for b, v in by_branch.items()
            ),
            key=lambda r: r["branch"],
        ),
        "cashiers": sorted(
            (
                {"user": u, "full_name": v["full_name"], "shifts": int(v["shifts"]),
                 "over": _r(v["over"]), "short": _r(v["short"]), "net": _r(v["net"])}
                for u, v in by_user.items()
            ),
            key=lambda r: (-r["short"], r["full_name"] or ""),
        ),
        "shifts": shifts_out,
        # additive: what the ledger booked on the Cash Over/Short account(s)
        "ledger": {k: (_r(v) if isinstance(v, float) else v) for k, v in ledger.items()},
    }


def _over_short_accounts() -> List[str]:
    names = set()
    try:
        from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings

        settings = get_jarz_settings()
        configured = (getattr(settings, "cash_over_short_account", None) or "").strip() if settings else ""
        if configured:
            names.add(configured)
    except Exception:  # noqa: BLE001 - settings are optional here
        pass
    names.update(
        frappe.get_all(
            "Account",
            filters={"account_name": ACCOUNTS.CASH_OVER_SHORT, "is_group": 0},
            pluck="name",
        ) or []
    )
    return sorted(names)


@frappe.whitelist()
def get_cash_over_short(date_from=None, date_to=None, branch=None):
    """Shift-close cash discrepancies by branch and cashier.

    * A shift = a submitted POS Closing Entry whose ``period_end_date`` (the
      close, when the variance is booked) falls in the range. Branch =
      ``POS Closing Entry.pos_profile``; cashier = the shift's opening user.
    * ``expected`` / ``counted`` / ``difference`` are summed over the closing
      entry's ``POS Closing Entry Detail`` rows (``expected_amount``,
      ``closing_amount``, ``difference = closing - expected``), the same figure
      the Shift Monitor shows.
    * over = positive differences (counted > expected); short = negative
      differences reported as a positive number; net = signed sum.
    * ``shifts`` is newest first, capped at 300; totals cover every shift.
    * Additive ``ledger`` block: Journal Entries on the Cash Over/Short
      account(s) dated in range (over = credit, short = debit), split into
      variances posted at close, at shift open (remark "→ Opening"; these are
      usually timing pairs that net out with a close) and unlinked manual JEs.
      Each shift also carries ``opening_difference`` from its opening JE.
    """
    allowed = _ensure_ops_report_access()
    fd, td = _parse_dates(date_from, date_to)
    branch, scope = _resolve_scope(branch, allowed)
    start, end = _day_bounds(fd, td)

    params: Dict[str, Any] = {"start": start, "end": end}
    cond = _scope_sql("pce.pos_profile", scope, params)

    shift_rows = frappe.db.sql(
        f"""
        SELECT pce.name AS closing,
               pce.pos_opening_entry AS opening,
               pce.pos_profile AS branch,
               COALESCE(NULLIF(poe.user, ''), pce.user) AS `user`,
               u.full_name AS full_name,
               poe.period_start_date AS opened,
               pce.period_end_date AS closed,
               COALESCE(d.expected, 0) AS expected,
               COALESCE(d.counted, 0) AS counted,
               COALESCE(d.difference, 0) AS difference
        FROM `tabPOS Closing Entry` pce
        LEFT JOIN `tabPOS Opening Entry` poe ON poe.name = pce.pos_opening_entry
        LEFT JOIN `tabUser` u ON u.name = COALESCE(NULLIF(poe.user, ''), pce.user)
        LEFT JOIN (
            SELECT parent,
                   SUM(expected_amount) AS expected,
                   SUM(closing_amount) AS counted,
                   SUM(difference) AS difference
            FROM `tabPOS Closing Entry Detail`
            WHERE parenttype = 'POS Closing Entry'
            GROUP BY parent
        ) d ON d.parent = pce.name
        WHERE pce.docstatus = 1
          AND pce.period_end_date >= %(start)s
          AND pce.period_end_date < %(end)s
          {cond}
        ORDER BY pce.period_end_date DESC, pce.name DESC
        """,
        params,
        as_dict=True,
    ) or []

    ledger_rows: List[Dict[str, Any]] = []
    opening_branch: Dict[str, str] = {}
    accounts = _over_short_accounts()
    if accounts:
        ledger_rows = frappe.db.sql(
            """
            SELECT gle.voucher_no, MIN(gle.posting_date) AS posting_date,
                   MAX(je.user_remark) AS user_remark,
                   SUM(gle.credit) AS credit, SUM(gle.debit) AS debit
            FROM `tabGL Entry` gle
            LEFT JOIN `tabJournal Entry` je
              ON je.name = gle.voucher_no AND gle.voucher_type = 'Journal Entry'
            WHERE gle.is_cancelled = 0
              AND gle.account IN %(accounts)s
              AND gle.posting_date BETWEEN %(fd)s AND %(td)s
            GROUP BY gle.voucher_no
            """,
            {"accounts": tuple(accounts), "fd": fd - timedelta(days=1), "td": td},
            as_dict=True,
        ) or []
        openings = sorted({
            o for o, _c in (parse_discrepancy_remark(r.get("user_remark")) for r in ledger_rows) if o
        })
        for chunk in _chunks(openings):
            for r in frappe.get_all(
                "POS Opening Entry",
                filters={"name": ["in", list(chunk)]},
                fields=["name", "pos_profile"],
                limit_page_length=0,
            ) or []:
                opening_branch[r["name"]] = r.get("pos_profile")

    return build_cash_over_short(
        _filters_echo(fd, td, branch, allowed),
        fd,
        shift_rows,
        ledger_rows,
        opening_branch,
        scope,
    )


# ── A3: fulfilment SLA ────────────────────────────────────────────────────


def slot_window(
    delivery_date: Any, time_from: Any, duration_seconds: Any
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """(slot_start, slot_end) from the Sales Invoice slot fields.

    * date + time + duration -> [date time, date time + duration]
    * date + time, no duration -> (start, None): caller applies the threshold
    * date only -> the whole delivery day
    * no date -> (None, None)
    """
    if not delivery_date:
        return None, None
    try:
        d = getdate(delivery_date)
    except Exception:  # noqa: BLE001
        return None, None
    secs = _seconds_of_day(time_from)
    if secs is None:
        start = datetime.combine(d, time.min)
        return start, start + timedelta(days=1)
    start = datetime.combine(d, time.min) + timedelta(seconds=secs)
    dur = _f(duration_seconds)
    return start, (start + timedelta(seconds=dur) if dur > 0 else None)


def _slot_label(row: Dict[str, Any], start: Optional[datetime], end: Optional[datetime]) -> Optional[str]:
    label = (row.get("slot_label") or "").strip()
    if label:
        return label
    if not start:
        return None
    if end and end.date() == start.date():
        return f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}"
    if end and end - start >= timedelta(days=1) and start.time() == time.min:
        return f"{start:%Y-%m-%d}"
    return f"{start:%Y-%m-%d %H:%M}"


def _cancel_reason(row: Dict[str, Any]) -> str:
    reason = str(row.get("cancellation_reason") or "").strip()
    if reason:
        # kanban.cancel_invoice stores "reason\nNotes: ..." -- group on the reason.
        first = reason.splitlines()[0].strip()
        if first:
            return first[:140]
    ctype = str(row.get("cancellation_type") or "").strip()
    return ctype or "Unspecified"


def build_fulfilment_sla(
    filters: Dict[str, Any],
    fd: date,
    td: date,
    rows: List[Dict[str, Any]],
    dispatch_at: Dict[str, Any],
    delivered_log_at: Dict[str, Any],
    returned: Iterable[str],
    threshold_min: int,
    now: datetime,
) -> Dict[str, Any]:
    """Assemble the SLA payload. See ``get_fulfilment_sla`` for the rules.

    ``rows``: name, customer, customer_name, creation, docstatus, branch, state,
    purpose, is_pickup, delivery_date, time_from, duration, slot_label,
    delivered_at, cancellation_type, cancellation_reason.
    """
    returned = set(returned)
    totals = {"orders": 0, "delivered": 0, "cancelled": 0, "returned": 0, "late": 0, "open_late": 0}
    disp_all: List[float] = []
    deliv_all: List[float] = []
    per_branch: Dict[str, Dict[str, Any]] = {}
    per_day: Dict[str, Dict[str, Any]] = {}
    d = fd
    while d <= td:
        per_day[d.isoformat()] = {"orders": 0, "deliver": [], "late": 0}
        d += timedelta(days=1)
    late_rows: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}

    for row in rows:
        name = row.get("name")
        created = _dt(row.get("creation"))
        if not name or not created:
            continue
        b = row.get("branch") or NO_BRANCH
        state = str(row.get("state") or "").strip()
        day_key = created.date().isoformat()
        day = per_day.setdefault(day_key, {"orders": 0, "deliver": [], "late": 0})
        br = per_branch.setdefault(
            b, {"orders": 0, "disp": [], "deliv": [], "late": 0, "cancelled": 0, "returned": 0}
        )
        totals["orders"] += 1
        day["orders"] += 1
        br["orders"] += 1

        is_cancelled = int(row.get("docstatus") or 0) == 2 or state == STATE_CANCELLED
        if is_cancelled:
            totals["cancelled"] += 1
            br["cancelled"] += 1
            key = _cancel_reason(row)
            reasons[key] = reasons.get(key, 0) + 1
            continue

        is_returned = name in returned or state == STATE_RETURNED
        if is_returned:
            totals["returned"] += 1
            br["returned"] += 1

        delivered = _dt(row.get("delivered_at")) or _dt(delivered_log_at.get(name))
        if delivered or state == STATE_DELIVERED:
            totals["delivered"] += 1

        # Timings cover delivery orders only: pickups and staff orders (which
        # jump Out for Delivery -> Delivered at the branch) are excluded.
        if cint(row.get("is_pickup")) or str(row.get("purpose") or "").strip() == "Employee":
            continue

        slot_start, slot_end = slot_window(row.get("delivery_date"), row.get("time_from"), row.get("duration"))
        clock = max(created, slot_start) if slot_start else created
        if slot_start and slot_end:
            deadline, rule = slot_end, "slot"
        elif slot_start:
            deadline, rule = slot_start + timedelta(minutes=threshold_min), "slot_start"
        else:
            deadline, rule = created + timedelta(minutes=threshold_min), "threshold"

        dispatched = _dt(dispatch_at.get(name))
        if dispatched:
            v = max(0.0, _minutes(dispatched, clock))
            disp_all.append(v)
            br["disp"].append(v)

        to_deliver = None
        if delivered:
            to_deliver = max(0.0, _minutes(delivered, clock))
            deliv_all.append(to_deliver)
            br["deliv"].append(to_deliver)
            day["deliver"].append(to_deliver)

        late_by = None
        if delivered:
            if delivered > deadline:
                late_by = _minutes(delivered, deadline)
        elif state not in (STATE_DELIVERED, STATE_RETURNED) and int(row.get("docstatus") or 0) == 1:
            # Still open past its deadline: late now.
            if now > deadline:
                late_by = _minutes(now, deadline)
                totals["open_late"] += 1
        if late_by is None:
            continue
        totals["late"] += 1
        br["late"] += 1
        day["late"] += 1
        late_rows.append({
            "invoice": name,
            "branch": b,
            "customer": row.get("customer_name") or row.get("customer"),
            "created": _iso(created),
            "delivered": _iso(delivered),
            "minutes": round(late_by, 1),
            "slot": _slot_label(row, slot_start, slot_end),
            # additive
            "deadline": _iso(deadline),
            "rule": rule,
            "state": state or None,
            "to_deliver_min": round(to_deliver, 1) if to_deliver is not None else None,
        })

    late_rows.sort(key=lambda r: -r["minutes"])
    return {
        "filters": filters,
        "totals": {
            "orders": int(totals["orders"]),
            "delivered": int(totals["delivered"]),
            "cancelled": int(totals["cancelled"]),
            "returned": int(totals["returned"]),
            "median_to_dispatch_min": median(disp_all),
            "median_to_deliver_min": median(deliv_all),
            "p90_to_deliver_min": percentile(deliv_all, 90),
            "late_orders": int(totals["late"]),
            "late_threshold_min": int(threshold_min),
            # additive
            "open_late_orders": int(totals["open_late"]),
            "timed_dispatch": len(disp_all),
            "timed_delivery": len(deliv_all),
        },
        "branches": sorted(
            (
                {
                    "branch": b,
                    "orders": int(v["orders"]),
                    "median_to_dispatch_min": median(v["disp"]),
                    "median_to_deliver_min": median(v["deliv"]),
                    "late_orders": int(v["late"]),
                    "cancelled": int(v["cancelled"]),
                    "returned": int(v["returned"]),
                }
                for b, v in per_branch.items()
            ),
            key=lambda r: r["branch"],
        ),
        "by_day": [
            {
                "date": k,
                "orders": int(v["orders"]),
                "median_to_deliver_min": median(v["deliver"]),
                "late_orders": int(v["late"]),
            }
            for k, v in sorted(per_day.items())
        ],
        "late": late_rows[:LATE_CAP],
        "cancellations": [
            {"reason": k, "count": int(v)}
            for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:CANCEL_REASONS_CAP]
        ],
    }


@frappe.whitelist()
def get_fulfilment_sla(date_from=None, date_to=None, branch=None, late_threshold_min=None):
    """Order -> dispatch -> delivered timings, lateness, cancellations, returns.

    Population: non-return Sales Invoices created (``creation``) in range,
    submitted or cancelled, excluding amendments (an invoice cancelled as
    ``Amended`` or replaced by an ``amended_from`` successor). ``orders`` counts
    all of them, cancelled included. Branch = the board that held the order
    (``custom_kanban_profile``, else ``pos_profile``).

    Timestamps:
      * created   = ``Sales Invoice.creation``.
      * dispatched = first ``Invoice State Log`` move to "Out for Delivery"
        (migration rows excluded), else the first submitted Delivery Note
        (created at the Out-for-Delivery transition).
      * delivered = ``custom_delivered_at`` (courier app), else the first
        ``Invoice State Log`` move to "Delivered". The State Log exists only
        from 2026-10-13, so older orders have a delivered time only when the
        courier app stamped it.

    Durations are measured from the clock start = the later of creation and the
    slot start, so a pre-ordered (scheduled) order is not charged for the time
    before its slot opened; negatives are clamped to 0. Timings cover delivery
    orders only (pickups and staff/Employee orders are excluded).

    LATE RULE: an order is late when it is delivered after its deadline, or is
    still open (not delivered / returned / cancelled) and the deadline has
    passed. Deadline = the delivery slot end (``custom_delivery_date`` +
    ``custom_delivery_time_from`` + ``custom_delivery_duration``) when the order
    has a slot; a slot with no duration uses slot start + ``late_threshold_min``;
    a date-only slot ends at midnight of that day; with no slot the deadline is
    ``creation`` + ``late_threshold_min`` (default 90). ``late[].minutes`` is how
    far past the deadline the order was delivered (or is, if still open);
    ``late`` is worst first, capped at 100.

    ``returned`` = orders with a submitted credit note or in the Returned state.
    ``cancellations`` groups cancelled orders on the first line of
    ``custom_cancellation_reason``, else ``custom_cancellation_type``.
    """
    allowed = _ensure_ops_report_access()
    fd, td = _parse_dates(date_from, date_to)
    branch, scope = _resolve_scope(branch, allowed)
    if (td - fd).days + 1 > SLA_MAX_DAYS:
        frappe.throw(_("Choose a range of at most {0} days").format(SLA_MAX_DAYS), frappe.ValidationError)
    threshold = cint(late_threshold_min) if late_threshold_min not in (None, "") else DEFAULT_LATE_THRESHOLD_MIN
    threshold = max(5, min(threshold, 1440))
    start, end = _day_bounds(fd, td)

    kanban = _si_col("custom_kanban_profile", "si", "NULL")
    branch_expr = f"COALESCE(NULLIF({kanban}, ''), NULLIF(si.pos_profile, ''))"
    params: Dict[str, Any] = {"start": start, "end": end, "nb": NO_BRANCH}
    cond = _scope_sql(branch_expr, scope, params)
    cancel_type = _si_col("custom_cancellation_type", "si", "''")

    rows = frappe.db.sql(
        f"""
        SELECT si.name, si.customer, si.customer_name, si.creation, si.docstatus,
               IFNULL({branch_expr}, %(nb)s) AS branch,
               {_si_col("custom_sales_invoice_state")} AS state,
               {_si_col("custom_order_purpose")} AS purpose,
               {_si_col("custom_is_pickup", "si", "0")} AS is_pickup,
               {_si_col("custom_delivery_date")} AS delivery_date,
               {_si_col("custom_delivery_time_from")} AS time_from,
               {_si_col("custom_delivery_duration")} AS duration,
               {_si_col("custom_delivery_slot_label")} AS slot_label,
               {_si_col("custom_delivered_at")} AS delivered_at,
               {cancel_type} AS cancellation_type,
               {_si_col("custom_cancellation_reason")} AS cancellation_reason
        FROM `tabSales Invoice` si
        WHERE si.is_return = 0
          AND si.docstatus IN (1, 2)
          AND si.creation >= %(start)s
          AND si.creation < %(end)s
          AND (
              si.docstatus = 1
              OR (
                  IFNULL({cancel_type}, '') != 'Amended'
                  AND si.name NOT IN (
                      SELECT a.amended_from FROM `tabSales Invoice` a
                      WHERE IFNULL(a.amended_from, '') != ''
                  )
              )
          )
          {cond}
        ORDER BY si.creation
        """,
        params,
        as_dict=True,
    ) or []

    names = [r["name"] for r in rows if int(r.get("docstatus") or 0) == 1]
    dispatch_at: Dict[str, Any] = {}
    delivered_log_at: Dict[str, Any] = {}
    returned: set = set()
    has_log = False
    try:
        has_log = bool(frappe.db.table_exists("Invoice State Log"))
    except Exception:  # noqa: BLE001
        has_log = False

    for chunk in _chunks(names):
        tup = tuple(chunk)
        if has_log:
            for r in frappe.db.sql(
                """
                SELECT sales_invoice, to_state, MIN(changed_at) AS at
                FROM `tabInvoice State Log`
                WHERE sales_invoice IN %(names)s
                  AND to_state IN (%(ofd)s, %(delivered)s)
                  AND IFNULL(source, '') NOT LIKE 'migration%%'
                GROUP BY sales_invoice, to_state
                """,
                {"names": tup, "ofd": STATE_OFD, "delivered": STATE_DELIVERED},
                as_dict=True,
            ) or []:
                if r.get("to_state") == STATE_OFD:
                    dispatch_at[r["sales_invoice"]] = r.get("at")
                else:
                    delivered_log_at[r["sales_invoice"]] = r.get("at")

        missing = tuple(n for n in chunk if n not in dispatch_at)
        if missing:
            for r in frappe.db.sql(
                """
                SELECT dni.against_sales_invoice AS invoice, MIN(dn.creation) AS at
                FROM `tabDelivery Note Item` dni
                JOIN `tabDelivery Note` dn ON dn.name = dni.parent
                WHERE dn.docstatus = 1
                  AND dn.is_return = 0
                  AND dni.against_sales_invoice IN %(names)s
                GROUP BY dni.against_sales_invoice
                """,
                {"names": missing},
                as_dict=True,
            ) or []:
                dispatch_at.setdefault(r["invoice"], r.get("at"))

        for r in frappe.db.sql(
            """
            SELECT DISTINCT return_against
            FROM `tabSales Invoice`
            WHERE is_return = 1 AND docstatus = 1 AND return_against IN %(names)s
            """,
            {"names": tup},
            as_dict=True,
        ) or []:
            returned.add(r["return_against"])

    return build_fulfilment_sla(
        _filters_echo(fd, td, branch, allowed, late_threshold_min=threshold),
        fd,
        td,
        rows,
        dispatch_at,
        delivered_log_at,
        returned,
        threshold,
        now_datetime(),
    )

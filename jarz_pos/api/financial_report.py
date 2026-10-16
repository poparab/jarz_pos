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
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any, Optional

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


def channel_for_purpose(purpose: str | None) -> str:
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


def _parse_dates(date_from: str | None, date_to: str | None) -> tuple[date, date]:
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


def bucket_keys(fd: date, td: date, granularity: str) -> list[str]:
	"""Every bucket in the range, so a quiet day still plots as zero."""
	keys: list[str] = []
	d = fd
	while d <= td:
		k = bucket_key(d, granularity)
		if not keys or keys[-1] != k:
			keys.append(k)
		d += timedelta(days=1)
	return keys


def covers_whole_months(fd: date, td: date) -> bool:
	"""True when the range starts on a 1st and ends on a month's last day."""
	last = calendar.monthrange(td.year, td.month)[1]
	return fd.day == 1 and td.day == last


def months_in_range(fd: date, td: date) -> list[tuple[date, date]]:
	"""(first, last) day of every calendar month the range touches."""
	out: list[tuple[date, date]] = []
	cur = date(fd.year, fd.month, 1)
	while cur <= td:
		last = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
		out.append((cur, last))
		cur = last + timedelta(days=1)
	return out


# ── classification (pure) ─────────────────────────────────────────────────


def classify_expense_account(
	account: str,
	account_type: str | None,
	freight_account: str | None,
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


def _default_company() -> str | None:
	company = frappe.defaults.get_user_default("Company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
	if company:
		return company
	companies = frappe.get_all("Company", pluck="name", limit=2)
	return companies[0] if len(companies) == 1 else None


def _accounts(company: str | None) -> tuple[str | None, str | None]:
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


def _recurring_accounts(company: str | None) -> dict[str, dict[str, Any]]:
	"""Accounts that hold recurring spend, with what each is expected to cost.

	Returns ``{account: {"category": ..., "items": [...], "monthly_due": float}}``.
	"""
	out: dict[str, dict[str, Any]] = {}
	if frappe.db.table_exists("Jarz Recurring Expense"):
		filters: dict[str, Any] = {}
		if company and frappe.get_meta("Jarz Recurring Expense").has_field("company"):
			filters["company"] = company
		for row in frappe.get_all(
			"Jarz Recurring Expense",
			filters=filters,
			fields=[
				"name",
				"expense_name",
				"category",
				"expense_account",
				"amount",
				"frequency",
				"status",
				"start_date",
				"end_date",
			],
		):
			acct = row.get("expense_account")
			if not acct:
				continue
			bucket = out.setdefault(acct, {"category": row.get("category"), "items": []})
			bucket["items"].append(row)

	try:
		from jarz_pos.api.recurring_expenses import _payroll_expense_accounts

		# Salary Component Accounts also list deduction and payable ledgers;
		# only an Expense account can carry payroll cost.
		candidates = _payroll_expense_accounts(company)
		expense_accounts = set(
			frappe.get_all(
				"Account",
				filters={"name": ["in", candidates or [""]], "root_type": "Expense"},
				pluck="name",
			)
		)
		for acct in candidates:
			if acct in expense_accounts:
				out.setdefault(acct, {"category": "Payroll", "items": []})["payroll"] = True
	except Exception:
		frappe.log_error(frappe.get_traceback(), "financial_report: payroll accounts")
	return out


def _due_for_range(
	recurring: dict[str, dict[str, Any]], fd: date, td: date, company: str | None
) -> dict[str, float]:
	"""What each recurring account is scheduled to cost over the months touched."""
	from jarz_pos.api.recurring_expenses import _is_due_in_month

	due: dict[str, float] = defaultdict(float)
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

			monthly = sum(flt(_load_payroll(company, m_end).get("monthly_total")) for _s, m_end in months)
			# Payroll posts to one ledger in practice; if several exist the
			# figure goes on the first so it is counted once, not per account.
			due[payroll_accounts[0]] += monthly
		except Exception:
			frappe.log_error(frappe.get_traceback(), "financial_report: payroll due")
	return dict(due)


def _gl_rows(fd: date, td: date, company: str | None) -> list[dict[str, Any]]:
	"""Net P&L movement per (account, bucket day) inside the range."""
	cond = "AND gle.company = %(company)s" if company else ""
	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
	return frappe.db.sql(
		f"""
        SELECT gle.account, gle.posting_date, acc.root_type, acc.account_type,
               SUM(gle.debit) AS debit, SUM(gle.credit) AS credit
        FROM `tabGL Entry` gle
        JOIN `tabAccount` acc ON acc.name = gle.account
        WHERE gle.is_cancelled = 0
          AND gle.posting_date BETWEEN %(fd)s AND %(td)s
          AND acc.root_type IN ('Income', 'Expense')
          -- A Period Closing Voucher zeroes every P&L account on the closing
          -- date; counting it would report a closed year as ~0 profit.
          AND gle.voucher_type != 'Period Closing Voucher'
          {cond}
        GROUP BY gle.account, gle.posting_date, acc.root_type, acc.account_type
        """,
		{"fd": fd, "td": td, "company": company},
		as_dict=True,
	)


def branch_condition(branch: str | None, alias: str = "si", param: str = "branch") -> str:
	"""``AND <alias>.pos_profile = %(branch)s`` when a branch is given, else ``""``.

	"Branch" is the POS Profile on the invoice. The value is always passed as a
	named parameter; only the alias/param name (constants) are spliced in.
	"""
	if not branch:
		return ""
	prefix = f"{alias}." if alias else ""
	return f"AND {prefix}pos_profile = %({param})s"


def actual_line_cost_sql(
	fd_param: str = "fd",
	td_param: str = "td",
	si_alias: str = "si",
	sii_alias: str = "sii",
	net_returns: bool = False,
) -> tuple[str, str]:
	"""``(join_sql, cost_expr)`` giving the real cost of goods of an invoice line.

	Cost of goods reaches the ledger one of two ways, and each leaves the
	valuation on a different row:

	* an invoice with ``update_stock = 1`` — ERPNext sets
	  ``Sales Invoice Item.incoming_rate`` (``SellingController.set_incoming_rate``
	  returns early for a Sales Invoice without ``update_stock``, so the column is
	  0 on every other invoice);
	* a Delivery Note made against the invoice — the cost sits on
	  ``Delivery Note Item.incoming_rate``, linked back by ``si_detail`` (stamped
	  on legacy DNs by ``Patches/v1_7/backfill_dn_against_sales_invoice``).

	``cost_expr`` is the line's cost in company currency (``incoming_rate ×
	stock_qty``), or NULL when neither path has posted a positive valuation —
	the caller decides the fallback (BOM, or nothing). Return DNs are excluded
	unless ``net_returns`` is set (below).
	Jarz bundles are separate invoice lines, not Packed Items, so no Packed Item
	path is needed (no ``Product Bundle`` is used anywhere in the app).

	The derived table is bounded to invoices posted inside
	``%(<fd_param>)s``..``%(<td_param>)s`` so it never scans the whole DN history.

	``net_returns`` (default ``False``, the gross cost product analytics reports)
	nets stock that came back against the line it was sold on. A Jarz return
	books a credit note with ``update_stock = 0`` and a separate **return**
	Delivery Note whose rows carry the original invoice line in ``si_detail``
	and a negative qty (``services/invoice_return._build_return_delivery_note``).
	With ``net_returns`` those rows are subtracted from the line's cost, and a
	line whose net cost is ≤ 0 (fully returned) costs 0 — never negative — while
	still counting as costed (not NULL). Only return DNs **posted inside the
	same range** are netted: a return in a later period leaves this period's
	cost (and its full revenue) untouched, and in the later period the original
	line is outside the range, so neither period can go negative. The credit
	note's own lines carry no cost (no stock moves on them).
	"""
	if net_returns:
		join_sql = f"""
        LEFT JOIN (
            SELECT dni.si_detail AS si_detail,
                   SUM(CASE WHEN IFNULL(dn.is_return, 0) = 0
                            THEN dni.incoming_rate * dni.stock_qty ELSE 0 END) AS cost,
                   SUM(CASE WHEN IFNULL(dn.is_return, 0) = 1
                            THEN ABS(dni.incoming_rate * dni.stock_qty) ELSE 0 END) AS returned_cost
            FROM `tabDelivery Note Item` dni
            JOIN `tabDelivery Note` dn ON dn.name = dni.parent
            JOIN `tabSales Invoice` dsi ON dsi.name = dni.against_sales_invoice
            WHERE dni.docstatus = 1
              AND IFNULL(dni.si_detail, '') != ''
              AND dsi.docstatus = 1
              AND dsi.posting_date BETWEEN %({fd_param})s AND %({td_param})s
              AND (IFNULL(dn.is_return, 0) = 0
                   OR dn.posting_date BETWEEN %({fd_param})s AND %({td_param})s)
            GROUP BY dni.si_detail
        ) dnc ON dnc.si_detail = {sii_alias}.name
        """
		cost_expr = (
			f"(CASE WHEN {si_alias}.update_stock = 1 AND IFNULL({sii_alias}.incoming_rate, 0) > 0 "
			f"THEN GREATEST({sii_alias}.incoming_rate * {sii_alias}.stock_qty "
			f"- IFNULL(dnc.returned_cost, 0), 0) "
			f"WHEN IFNULL(dnc.cost, 0) > 0 THEN GREATEST(dnc.cost - IFNULL(dnc.returned_cost, 0), 0) "
			f"ELSE NULL END)"
		)
		return join_sql, cost_expr

	join_sql = f"""
        LEFT JOIN (
            SELECT dni.si_detail AS si_detail,
                   SUM(dni.incoming_rate * dni.stock_qty) AS cost
            FROM `tabDelivery Note Item` dni
            JOIN `tabDelivery Note` dn ON dn.name = dni.parent
            JOIN `tabSales Invoice` dsi ON dsi.name = dni.against_sales_invoice
            WHERE dni.docstatus = 1
              AND IFNULL(dn.is_return, 0) = 0
              AND IFNULL(dni.si_detail, '') != ''
              AND dsi.docstatus = 1
              AND dsi.posting_date BETWEEN %({fd_param})s AND %({td_param})s
            GROUP BY dni.si_detail
        ) dnc ON dnc.si_detail = {sii_alias}.name
    """
	cost_expr = (
		f"(CASE WHEN {si_alias}.update_stock = 1 AND IFNULL({sii_alias}.incoming_rate, 0) > 0 "
		f"THEN {sii_alias}.incoming_rate * {sii_alias}.stock_qty "
		f"WHEN IFNULL(dnc.cost, 0) > 0 THEN dnc.cost "
		f"ELSE NULL END)"
	)
	return join_sql, cost_expr


def _invoice_rows(fd: date, td: date, company: str | None, branch: str | None = None) -> list[dict[str, Any]]:
	"""Net sales per (purpose, day), returns included as negatives."""
	cond = "AND company = %(company)s" if company else ""
	cond += " " + branch_condition(branch, alias="")
	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
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
		{"fd": fd, "td": td, "company": company, "branch": branch},
		as_dict=True,
	)


def _shipping_rows(
	fd: date,
	td: date,
	company: str | None,
	shipping_account: str | None,
	branch: str | None = None,
) -> list[dict[str, Any]]:
	"""Delivery charges billed, per (purpose, day), from the invoice tax rows."""
	cond = "AND si.company = %(company)s" if company else ""
	cond += " " + branch_condition(branch)
	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
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
		{"fd": fd, "td": td, "company": company, "ship": shipping_account or "", "branch": branch},
		as_dict=True,
	)


NO_BRANCH_LABEL = "(No Profile)"


def validate_branch(branch: Any) -> str | None:
	"""``None``/blank = all branches; otherwise it must be an existing POS Profile.

	An unknown branch used to return an empty, all-zero branch view that looked
	like a quiet day. The ``(No Profile)`` bucket label names invoices without a
	POS Profile; it is a row in ``branches``, not a branch you can filter on.
	"""
	text = str(branch or "").strip()
	if not text:
		return None
	if text == NO_BRANCH_LABEL or not frappe.db.exists("POS Profile", text):
		frappe.throw(_("Unknown branch: {0}").format(text), frappe.ValidationError)
	return text


def _branch_rows(
	fd: date,
	td: date,
	company: str | None,
	shipping_account: str | None,
	branch: str | None = None,
) -> list[dict[str, Any]]:
	"""Per-branch (POS Profile) sales, actual COGS and shipping income, from invoices.

	* ``revenue`` — net sales (``base_net_total``), returns included as negatives,
	  the same basis as the channel split.
	* ``cogs`` — real cost of goods from :func:`actual_line_cost_sql` with
	  ``net_returns`` (``incoming_rate × stock_qty``, less stock that came back
	  on a return Delivery Note in the same range, floored at 0 per line); lines
	  with no posted valuation add 0, and ``cogs_coverage_pct`` says how much of
	  the stock-item sales was costed. Credit-note lines are left out of that
	  percentage (their stock is netted on the original line instead).
	* ``shipping_income`` — the ``Shipping Income`` tax rows.
	* ``b2b_revenue`` / ``b2c_revenue`` (additive) — the branch's own invoices
	  split by ``custom_order_purpose`` (:func:`channel_for_purpose`), on the
	  same basis as ``summary.b2b_revenue``: net sales **plus** billed shipping.
	"""
	cond = "AND si.company = %(company)s" if company else ""
	cond += " " + branch_condition(branch)
	params = {"fd": fd, "td": td, "company": company, "branch": branch, "ship": shipping_account or ""}
	label = f"COALESCE(NULLIF(si.pos_profile, ''), '{NO_BRANCH_LABEL}')"

	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
	sales = frappe.db.sql(
		f"""
        SELECT {label} AS branch, si.custom_order_purpose AS purpose,
               SUM(CASE WHEN si.is_return = 0 THEN 1 ELSE 0 END) AS orders,
               SUM(si.base_net_total) AS revenue
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY branch, si.custom_order_purpose
        """,
		params,
		as_dict=True,
	)

	join_sql, cost_expr = actual_line_cost_sql(net_returns=True)
	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
	costs = frappe.db.sql(
		f"""
        SELECT {label} AS branch,
               SUM(COALESCE({cost_expr}, 0)) AS cogs,
               SUM(CASE WHEN si.is_return = 0 AND IFNULL(it.is_stock_item, 0) = 1
                        THEN sii.base_net_amount ELSE 0 END) AS stock_sales,
               SUM(CASE WHEN si.is_return = 0 AND IFNULL(it.is_stock_item, 0) = 1
                         AND {cost_expr} IS NOT NULL
                        THEN sii.base_net_amount ELSE 0 END) AS costed_sales
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent AND sii.parenttype = 'Sales Invoice'
        LEFT JOIN `tabItem` it ON it.name = sii.item_code
        {join_sql}
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {cond}
        GROUP BY branch
        """,
		params,
		as_dict=True,
	)

	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
	shipping = frappe.db.sql(
		f"""
        SELECT {label} AS branch, si.custom_order_purpose AS purpose,
               SUM(stc.base_tax_amount) AS amount
        FROM `tabSales Taxes and Charges` stc
        JOIN `tabSales Invoice` si
          ON si.name = stc.parent AND stc.parenttype = 'Sales Invoice'
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          AND (stc.description LIKE 'Shipping Income%%' OR stc.account_head = %(ship)s)
          {cond}
        GROUP BY branch, si.custom_order_purpose
        """,
		params,
		as_dict=True,
	)
	return assemble_branch_rows(sales, costs, shipping)


def assemble_branch_rows(
	sales: Iterable[dict[str, Any]],
	costs: Iterable[dict[str, Any]],
	shipping: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
	"""Merge the three per-branch aggregates (pure), biggest revenue first.

	``sales`` and ``shipping`` rows may carry ``purpose`` (one row per branch and
	order purpose); it feeds the additive ``b2b_revenue`` / ``b2c_revenue``
	(net sales + billed shipping, as :func:`channel_for_purpose` maps it). A
	row without ``purpose`` is a legacy blank, i.e. B2C.
	"""
	out: dict[str, dict[str, Any]] = {}

	def row(name: str) -> dict[str, Any]:
		return out.setdefault(
			name or NO_BRANCH_LABEL,
			{
				"orders": 0,
				"revenue": 0.0,
				"cogs": 0.0,
				"shipping_income": 0.0,
				"stock_sales": 0.0,
				"costed_sales": 0.0,
				CHANNEL_B2B: 0.0,
				CHANNEL_B2C: 0.0,
			},
		)

	def add_channel(b: dict[str, Any], purpose: str | None, amount: float) -> None:
		c = channel_for_purpose(purpose)
		if c in (CHANNEL_B2B, CHANNEL_B2C):
			b[c] += amount

	for r in sales or []:
		b = row(r.get("branch"))
		b["orders"] += int(r.get("orders") or 0)
		b["revenue"] += flt(r.get("revenue"))
		add_channel(b, r.get("purpose"), flt(r.get("revenue")))
	for r in costs or []:
		b = row(r.get("branch"))
		# A line's cost is floored at 0 in SQL; a branch never shows negative COGS.
		b["cogs"] += max(flt(r.get("cogs")), 0.0)
		b["stock_sales"] += flt(r.get("stock_sales"))
		b["costed_sales"] += flt(r.get("costed_sales"))
	for r in shipping or []:
		b = row(r.get("branch"))
		b["shipping_income"] += flt(r.get("amount"))
		add_channel(b, r.get("purpose"), flt(r.get("amount")))

	rows: list[dict[str, Any]] = []
	for name, b in out.items():
		revenue = round(b["revenue"], 2)
		cogs = round(b["cogs"], 2)
		rows.append(
			{
				"branch": name,
				"orders": int(b["orders"]),
				"revenue": revenue,
				"cogs": cogs,
				"gross_profit": round(revenue - cogs, 2),
				"gross_margin_pct": pct(revenue - cogs, revenue),
				"shipping_income": round(b["shipping_income"], 2),
				"cogs_coverage_pct": pct(b["costed_sales"], b["stock_sales"]) if b["stock_sales"] else 100.0,
				"b2b_revenue": round(b[CHANNEL_B2B], 2),
				"b2c_revenue": round(b[CHANNEL_B2C], 2),
			}
		)
	rows.sort(key=lambda x: (-x["revenue"], x["branch"]))
	return rows


def _shipping_order_basis(
	fd: date, td: date, company: str | None, branch: str | None = None
) -> dict[str, Any]:
	"""What the orders placed in the window say, independent of the ledger.

	Two cross-checks come from here. Courier cost recorded on the orders
	(``custom_shipping_expense``) against the courier JEs, and how many orders
	have had their stock posted. Cost of goods only reaches the ledger through a
	Delivery Note (or an invoice with ``update_stock``), and on production July
	2026 had none for 472 orders, so a month can look nearly cost-free when it
	is simply unposted.
	"""
	cond = "AND si.company = %(company)s" if company else ""
	cond += " " + branch_condition(branch)
	# nosemgrep: frappe-sql-format-injection -- only constant fragments and constant alias/param names interpolated; values are bound parameters
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
		{"fd": fd, "td": td, "company": company, "branch": branch},
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
	gl_rows: list[dict[str, Any]],
	invoice_rows: list[dict[str, Any]],
	shipping_rows: list[dict[str, Any]],
	shipping_account: str | None,
	freight_account: str | None,
	recurring: dict[str, dict[str, Any]],
	due: dict[str, float],
	order_basis: dict[str, Any],
	branch: str | None = None,
	branch_rows: list[dict[str, Any]] | None = None,
	branch_invoice_rows: list[dict[str, Any]] | None = None,
	branch_shipping_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	"""Assemble the payload.

	With ``branch`` set, ``invoice_rows``/``shipping_rows``/``order_basis`` must
	still be the **company-wide** rows: every GL-coupled figure (summary totals,
	shipping section, trend, reconciliation) is computed exactly as without a
	branch, because the ledger has no branch dimension. ``summary`` is entirely
	company-wide, including ``orders``, ``b2b_revenue`` and ``b2c_revenue`` (one
	scope per block: before 2026-10-15 those three followed the branch while
	``sales`` and profit did not). Only the invoice-based ``channels`` list is
	rebuilt from ``branch_invoice_rows`` / ``branch_shipping_rows``; the
	branch's own orders and B2B/B2C revenue are on its ``branches`` row.
	``data_quality`` says so.
	"""
	granularity = granularity_for(fd, td)
	keys = bucket_keys(fd, td, granularity)

	def blank() -> dict[str, float]:
		return defaultdict(float)

	trend: dict[str, dict[str, float]] = {k: blank() for k in keys}

	# ── ledger ──
	ledger_income = 0.0
	ledger_expense = 0.0
	ledger_shipping_income = 0.0
	sales_income = 0.0
	other_income: dict[str, float] = defaultdict(float)
	sections: dict[str, dict[str, float]] = {
		"cost_of_sales": defaultdict(float),
		"shipping": defaultdict(float),
		"recurring": defaultdict(float),
		"other": defaultdict(float),
	}
	account_types: dict[str, str] = {}
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
	channels: dict[str, dict[str, float]] = {
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
	def rows_for(section: str) -> list[dict[str, Any]]:
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
		sum(v for a, v in sections["cost_of_sales"].items() if account_types.get(a) == "Cost of Goods Sold"),
		2,
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
		recurring_rows.append(
			{
				"account": acct,
				"label": account_label(acct),
				"category": info.get("category") or "",
				"posted": posted,
				"due": d,
				"remaining": round(max(d - posted, 0.0), 2),
			}
		)
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
		channel_rows.append(
			{
				"channel": c,
				"sales": round(ch["sales"], 2),
				"shipping_income": round(ch["shipping_income"], 2),
				"revenue": round(ch["sales"] + ch["shipping_income"], 2),
				"orders": int(ch["orders"]),
				"returns": int(ch["returns"]),
				"returns_value": round(ch["returns_value"], 2),
				"share_pct": pct(ch["sales"] + ch["shipping_income"], total_revenue),
			}
		)
	if abs(adjustments) >= TOLERANCE:
		channel_rows.append(
			{
				"channel": CHANNEL_ADJUSTMENTS,
				"sales": adjustments,
				"shipping_income": 0.0,
				"revenue": adjustments,
				"orders": 0,
				"returns": 0,
				"returns_value": 0.0,
				"share_pct": pct(adjustments, total_revenue),
			}
		)
	# ``summary`` is company-wide whatever the branch, so its channel figures
	# are read from the company rows before a branch replaces ``channels``.
	company_channel_rows = channel_rows
	if branch:
		# The ledger cannot be split by branch, so there is no "adjustments"
		# row here: the branch's channels are its invoices, and their shares
		# are of the branch's own invoiced revenue.
		channel_rows = channel_rows_from_invoices(branch_invoice_rows or [], branch_shipping_rows or [])

	# ── trend ──
	trend_rows = []
	for k in sorted(trend):
		t = trend[k]
		bucket_ship_in_freight = t["shipping_income"] - t["ledger_shipping_income"]
		adj = t["ledger_sales"] - t["invoice_sales"]
		revenue = t["ledger_sales"] + t["shipping_income"]
		shipping_exp = t["shipping"] + bucket_ship_in_freight
		expenses = t["cost_of_sales"] + shipping_exp + t["recurring"] + t["other"]
		trend_rows.append(
			{
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
			}
		)

	notes: list[str] = []
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
	warnings: list[str] = []
	if cogs_coverage < COVERAGE_WARN_PCT:
		warnings.append("cogs_incomplete")
	if shipping_coverage < COVERAGE_WARN_PCT:
		warnings.append("shipping_expense_incomplete")
	# "Due" is whole months (a bill is due or it is not), so it only compares
	# fairly with what was posted when the range is itself whole months. On a
	# partial range (e.g. the last 30 days) the shortfall is expected, not a gap.
	if covers_whole_months(fd, td) and recurring_due and recurring_total < recurring_due - TOLERANCE:
		warnings.append("recurring_not_fully_posted")

	delivery_orders = int(order_basis.get("delivery_orders") or 0)
	data_quality: dict[str, Any] = {
		"orders": orders_placed,
		"stock_posted_orders": stock_posted,
		"cogs_coverage_pct": cogs_coverage,
		"shipping_expense_coverage_pct": shipping_coverage,
		"warnings": warnings,
		"gl_scope": "company",
		"notes": [],
	}
	if branch:
		data_quality["gl_scope"] = "company"
		data_quality["branch"] = branch
		data_quality["notes"] = ["gl_company_wide"]
		data_quality["note"] = (
			"Ledger (GL) figures are company-wide: the General Ledger has no branch "
			"dimension. Only channels and branches are filtered to this branch; "
			"the summary is company-wide."
		)
	return {
		"period": {"date_from": fd.isoformat(), "date_to": td.isoformat(), "granularity": granularity},
		"summary": {
			"total_revenue": total_revenue,
			"sales": sales_total,
			"shipping_income": round(shipping_income, 2),
			"b2b_revenue": next(r["revenue"] for r in company_channel_rows if r["channel"] == CHANNEL_B2B),
			"b2c_revenue": next(r["revenue"] for r in company_channel_rows if r["channel"] == CHANNEL_B2C),
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
			"orders": sum(int(r["orders"]) for r in company_channel_rows),
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
		"data_quality": data_quality,
		"branches": list(branch_rows or []),
		"filters": {
			"date_from": fd.isoformat(),
			"date_to": td.isoformat(),
			"branch": branch or None,
		},
	}


def channel_rows_from_invoices(
	invoice_rows: Iterable[dict[str, Any]], shipping_rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
	"""Channel rows from invoice aggregates alone (pure), shares of invoiced revenue."""
	channels: dict[str, dict[str, float]] = {
		c: {"sales": 0.0, "shipping_income": 0.0, "orders": 0, "returns": 0, "returns_value": 0.0}
		for c in CHANNELS
	}
	for r in invoice_rows:
		ch = channels[channel_for_purpose(r.get("purpose"))]
		ch["sales"] += flt(r.get("net"))
		ch["orders"] += int(r.get("orders") or 0)
		ch["returns"] += int(r.get("returns") or 0)
		ch["returns_value"] += flt(r.get("returns_value"))
	for r in shipping_rows:
		channels[channel_for_purpose(r.get("purpose"))]["shipping_income"] += flt(r.get("amount"))
	total = sum(ch["sales"] + ch["shipping_income"] for ch in channels.values())
	return [
		{
			"channel": c,
			"sales": round(channels[c]["sales"], 2),
			"shipping_income": round(channels[c]["shipping_income"], 2),
			"revenue": round(channels[c]["sales"] + channels[c]["shipping_income"], 2),
			"orders": int(channels[c]["orders"]),
			"returns": int(channels[c]["returns"]),
			"returns_value": round(channels[c]["returns_value"], 2),
			"share_pct": pct(channels[c]["sales"] + channels[c]["shipping_income"], total),
		}
		for c in CHANNELS
	]


@frappe.whitelist()
def get_profit_and_loss(
	date_from: str | None = None,
	date_to: str | None = None,
	company: str | None = None,
	branch: str | None = None,
) -> dict[str, Any]:
	"""Ledger-reconciled profit & loss for the range, split B2B / B2C.

	``branch`` (a POS Profile, optional) filters the invoice-based parts only:
	``channels`` and ``branches``. Ledger figures stay company-wide and
	``data_quality.notes`` carries ``gl_company_wide``. ``branches`` lists every
	branch (or just the chosen one) with orders, net revenue, actual COGS
	(``incoming_rate × stock_qty``, returns netted), gross profit, shipping
	income and (additive) ``b2b_revenue`` / ``b2c_revenue``. ``summary`` is
	always company-wide. A ``branch`` that is not an existing POS Profile is
	refused with a ``ValidationError``.
	"""
	_ensure_jarz_manager()
	branch = validate_branch(branch)
	fd, td = _parse_dates(date_from, date_to)
	company = company or _default_company()
	shipping_account, freight_account = _accounts(company)
	recurring = _recurring_accounts(company)

	try:
		branch_rows = _branch_rows(fd, td, company, shipping_account, branch)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "financial_report: branch rows")
		branch_rows = []

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
		branch=branch,
		branch_rows=branch_rows,
		branch_invoice_rows=_invoice_rows(fd, td, company, branch) if branch else None,
		branch_shipping_rows=_shipping_rows(fd, td, company, shipping_account, branch) if branch else None,
	)

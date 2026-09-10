"""Branch replenishment — what the factory should send each branch today.

READ-ONLY.  This module computes a plan and nothing else.  The write already
exists: :func:`jarz_pos.api.transfer.submit_transfer` creates the Material
Transfer Stock Entry, and every ``send_now`` line in this payload is already in
the ``{"item_code": ..., "qty": ...}`` shape it expects.  A second write path
for the same movement is how two screens end up disagreeing about what a
transfer is, so there is deliberately not one here.

Why this endpoint exists.  There has never been a factory-to-branch transfer on
this site: branches have been selling jars that were never booked in to them,
which is why 26 branch bins are negative today.  The screen this feeds is the
first place anybody can see, per branch, "you sell 4.2 of these a day, you have
1 on the shelf, here are 58".

Query budget.  Two batched SQL reads carry the whole payload — one for stock
across every warehouse at once, one for sales grouped by warehouse and item.
Neither loops.  The three remaining reads (the jar catalogue, the warehouse
list, the POS Profile labels) are fixed-cost metadata lookups that do not grow
with the number of items or branches; there is no per-item or per-branch read
anywhere in this module.

Every read fails soft.  A failed query logs and returns empty, so a broken read
degrades the screen to "nothing to send" rather than a stack trace on a phone
in a van.  The one thing that is *not* soft is the permission gate.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate

# The gate on the transfer this plan feeds, imported rather than restated.  A
# plan the caller may read but not act on is a screen that ends in "Not
# permitted", and a second role set would drift from the first one silently.
from jarz_pos.api.transfer import _ensure_transfer_access
# THE definition of "which item groups are a sellable jar", shared with the
# daily plan and the bases screen.
from jarz_pos.api.daily_plan import FINISHED_GOODS_GROUPS
# THE definition of "this warehouse's stock is not sellable" (WIP / Rejected),
# injected into the pure selector rather than copied into it.
from jarz_pos.services.production_planning import is_non_sellable_warehouse
from jarz_pos.services import replenishment_planning as plan

#: The owner's standing answer to "how much cover should a branch hold".
DEFAULT_COVER_DAYS = 14
#: Long enough to smooth a slow week, short enough to still be this season.
DEFAULT_SALES_DAYS = 30

MIN_COVER_DAYS, MAX_COVER_DAYS = 1, 90
MIN_SALES_DAYS, MAX_SALES_DAYS = 1, 365

#: Name hints used only to *guess* the factory store when neither the caller
#: nor Manufacturing Settings names one.
SOURCE_WAREHOUSE_NAME_HINTS = ("finished goods", "finished good")


def _log_failure(title: str, message: str) -> None:
    """Log a degraded read without ever masking the failure that caused it.

    ``frappe.log_error`` reads System Settings *outside* its own try block, so
    it can raise from inside an ``except`` and replace the caller's real
    traceback with its own.  Same guard as
    ``services/production_planning._log_failure``.
    """
    title = (title or "JARZ Replenishment")[:140]
    try:
        frappe.log_error(title=title, message=message)
        return
    except Exception as logging_error:  # noqa: BLE001 — see docstring
        fallback = f"{title}: {message} (log_error itself failed: {logging_error})"

    try:
        frappe.logger().error(fallback)
    except Exception:  # noqa: BLE001
        return


def _traceback() -> str:
    try:
        return frappe.get_traceback()
    except Exception:  # noqa: BLE001
        return ""


# ── Resolvers ───────────────────────────────────────────────────────────
# Everything that touches the database sits behind one of these, so the tests
# patch a single symbol instead of the world — and so the fail-soft rule is
# stated once per read rather than once per caller.


def _resolve_company(company: Optional[str]) -> Optional[str]:
    """The company this plan belongs to, or ``None`` when it is ambiguous.

    ``None`` is a usable answer: it simply stops filtering warehouses by
    company, which on a single-company site is the same set.
    """
    company = (company or "").strip()
    if company:
        return company
    try:
        default = frappe.db.get_single_value("Global Defaults", "default_company")
        if default:
            return str(default)
    except Exception:
        _log_failure("JARZ Replenishment - default company read failed", _traceback())
    try:
        names = frappe.get_all("Company", pluck="name", limit=2)
        if len(names) == 1:
            return str(names[0])
    except Exception:
        _log_failure("JARZ Replenishment - company list read failed", _traceback())
    return None


def _resolve_jar_items() -> List[Dict[str, Any]]:
    """Every finished jar: the ``Medium`` and ``Large`` item groups.

    Restricted to the jars on purpose — a replenishment plan that also offered
    to ship flour and cream to a shop would bury the jars nobody can sell
    without.
    """
    try:
        rows = frappe.get_all(
            "Item",
            filters={
                "item_group": ["in", list(FINISHED_GOODS_GROUPS)],
                "disabled": 0,
                "has_variants": 0,
            },
            fields=["name as item_code", "item_name", "stock_uom"],
            order_by="item_name asc",
            limit_page_length=0,
        )
    except Exception:
        _log_failure("JARZ Replenishment - jar catalogue read failed", _traceback())
        return []
    return [
        {
            "item_code": str(row.get("item_code")),
            "item_name": str(row.get("item_name") or row.get("item_code")),
            "stock_uom": str(row.get("stock_uom") or ""),
        }
        for row in rows or []
        if row.get("item_code")
    ]


def _resolve_warehouse_rows(company: Optional[str]) -> List[Dict[str, Any]]:
    """Every leaf warehouse of the company, with the fields the selector needs."""
    filters: Dict[str, Any] = {"is_group": 0, "disabled": 0}
    if company:
        filters["company"] = company
    try:
        rows = frappe.get_all(
            "Warehouse",
            filters=filters,
            fields=["name", "warehouse_type", "is_group", "disabled"],
            order_by="name asc",
            limit_page_length=0,
        )
    except Exception:
        _log_failure("JARZ Replenishment - warehouse read failed", _traceback())
        return []
    return [dict(row) for row in rows or []]


def _resolve_branch_labels() -> Dict[str, str]:
    """``{warehouse: branch name}`` from the enabled POS Profiles.

    The POS Profile is this app's operational branch — the same mapping
    ``api/manager._resolve_pos_profile_warehouse`` uses in the other direction.
    A warehouse with no profile still appears on the plan; it just falls back to
    its own name with the company abbreviation stripped.
    """
    try:
        rows = frappe.get_all(
            "POS Profile",
            filters={"disabled": 0},
            fields=["name", "warehouse"],
            order_by="name asc",
            limit_page_length=0,
        )
    except Exception:
        _log_failure("JARZ Replenishment - POS Profile read failed", _traceback())
        return {}

    labels: Dict[str, str] = {}
    for row in rows or []:
        warehouse = str(row.get("warehouse") or "").strip()
        if warehouse and warehouse not in labels:
            labels[warehouse] = str(row.get("name") or warehouse)
    return labels


def _resolve_source_warehouse(
    source_warehouse: Optional[str],
    warehouse_rows: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    """The factory store: the caller's choice, then the configured one, then a guess."""
    chosen = (source_warehouse or "").strip()
    if chosen:
        return chosen

    try:
        configured = frappe.db.get_single_value(
            "Manufacturing Settings", "default_fg_warehouse"
        )
    except Exception:
        _log_failure("JARZ Replenishment - FG warehouse read failed", _traceback())
        configured = None
    if configured:
        return str(configured)

    # Last resort, from warehouses already in hand — never an extra query.
    for row in warehouse_rows:
        name = str(row.get("name") or "")
        if any(hint in name.lower() for hint in SOURCE_WAREHOUSE_NAME_HINTS):
            return name
    return None


def _resolve_stock(
    warehouses: Sequence[str],
    item_codes: Sequence[str],
) -> Dict[Tuple[str, str], float]:
    """ONE query: on-hand per (warehouse, item) for every branch and the source.

    Quantities come back **raw**, negatives included.  The floor belongs to the
    arithmetic in ``services/replenishment_planning``, not to the read: this
    layer must be able to report that a bin is at -55, and a read that silently
    clamped it would hide the counting error the whole screen exists to surface.
    """
    warehouses = sorted({str(w) for w in warehouses if w})
    item_codes = sorted({str(c) for c in item_codes if c})
    if not warehouses or not item_codes:
        return {}

    try:
        rows = frappe.db.sql(
            """
            SELECT b.warehouse AS warehouse,
                   b.item_code AS item_code,
                   SUM(b.actual_qty) AS qty
            FROM `tabBin` b
            WHERE b.warehouse IN %(warehouses)s
              AND b.item_code IN %(codes)s
            GROUP BY b.warehouse, b.item_code
            """,
            {"warehouses": warehouses, "codes": item_codes},
            as_dict=True,
        )
    except Exception:
        _log_failure(
            "JARZ Replenishment - stock read failed",
            f"warehouses={warehouses}\n{_traceback()}",
        )
        return {}

    return {
        (str(row["warehouse"]), str(row["item_code"])): plan.to_float(row.get("qty"), 0.0)
        for row in rows or []
    }


def _resolve_sales(
    warehouses: Sequence[str],
    item_codes: Sequence[str],
    sales_days: int,
) -> Dict[Tuple[str, str], float]:
    """ONE query: net quantity sold per (warehouse, item) over the window.

    ``Sales Invoice Item.warehouse`` is where a sale is recorded, and it is the
    only per-branch demand signal on this site — which is the whole reason this
    plan can tell Nasr city (1,466 jars a month) apart from 6th of october
    (418) instead of averaging them into one useless number.

    Submitted invoices only.  Returns are **not** excluded: a return carries a
    negative ``qty`` on a submitted invoice, so summing nets it out, and jars
    that came back are not demand that has to be re-covered.

    The window is whole completed days, ending yesterday.  Dividing today's
    part-day by a whole day would drag every rate down as the morning wears on.
    """
    warehouses = sorted({str(w) for w in warehouses if w})
    item_codes = sorted({str(c) for c in item_codes if c})
    if not warehouses or not item_codes or sales_days <= 0:
        return {}

    try:
        today = getdate(nowdate())
        to_date = add_days(today, -1)
        from_date = add_days(today, -sales_days)
        rows = frappe.db.sql(
            """
            SELECT sii.warehouse AS warehouse,
                   sii.item_code AS item_code,
                   SUM(sii.stock_qty) AS qty
            FROM `tabSales Invoice Item` sii
            INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE si.docstatus = 1
              AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
              AND sii.warehouse IN %(warehouses)s
              AND sii.item_code IN %(codes)s
            GROUP BY sii.warehouse, sii.item_code
            """,
            {
                "from_date": from_date,
                "to_date": to_date,
                "warehouses": warehouses,
                "codes": item_codes,
            },
            as_dict=True,
        )
    except Exception:
        _log_failure(
            "JARZ Replenishment - sales read failed",
            f"warehouses={warehouses} days={sales_days}\n{_traceback()}",
        )
        return {}

    return {
        (str(row["warehouse"]), str(row["item_code"])): plan.to_float(row.get("qty"), 0.0)
        for row in rows or []
    }


def _generated_on() -> str:
    try:
        return str(now_datetime())
    except Exception:  # noqa: BLE001
        return ""


# ── Endpoint ────────────────────────────────────────────────────────────


@frappe.whitelist()
def get_branch_replenishment(
    company: Optional[str] = None,
    source_warehouse: Optional[str] = None,
    cover_days: Any = DEFAULT_COVER_DAYS,
    sales_days: Any = DEFAULT_SALES_DAYS,
) -> Dict[str, Any]:
    """How many of each jar the factory should send to each branch today.

    Read-only.  Nothing here inserts, submits or commits; the resulting lines
    are handed to :func:`jarz_pos.api.transfer.submit_transfer` by the caller.

    Args:
        company: optional; defaults to the global default company.
        source_warehouse: the factory store; defaults to Manufacturing
            Settings' ``default_fg_warehouse``.
        cover_days: days of cover each branch should hold (default 14).
        sales_days: how many completed days of sales set the rate (default 30).

    Returns the payload documented in ``services.replenishment_planning.build_plan``.
    When the factory store cannot be resolved the payload is well-formed and
    empty with a ``notice`` explaining why, rather than an exception — the
    screen has to render something either way.
    """
    _ensure_transfer_access()

    cover_days = plan.coerce_days(
        cover_days, default=DEFAULT_COVER_DAYS, minimum=MIN_COVER_DAYS, maximum=MAX_COVER_DAYS
    )
    sales_days = plan.coerce_days(
        sales_days, default=DEFAULT_SALES_DAYS, minimum=MIN_SALES_DAYS, maximum=MAX_SALES_DAYS
    )

    company = _resolve_company(company)
    warehouse_rows = _resolve_warehouse_rows(company)
    source = _resolve_source_warehouse(source_warehouse, warehouse_rows)

    empty: Dict[str, Any] = {
        "generated_on": _generated_on(),
        "company": company,
        "source_warehouse": source,
        "cover_days": cover_days,
        "sales_days": sales_days,
        "items": [],
        "branches": [],
        "source_available": {},
        "on_hand": {},
        "sold": {},
    }

    if not source:
        return plan.build_plan(
            **empty,
            notice="No factory store is configured. Set Manufacturing Settings "
                   "> Default Finished Goods Warehouse, or pass source_warehouse.",
        )

    branches = plan.select_selling_warehouses(
        warehouse_rows,
        source_warehouse=source,
        also_excluded=is_non_sellable_warehouse,
        branch_labels=_resolve_branch_labels(),
    )
    items = _resolve_jar_items()
    if not branches or not items:
        # An empty screen always says why.  "Nothing to send" and "I could not
        # read the catalogue" look identical otherwise, and the second one is
        # the failure this endpoint is most likely to hit.
        if not items:
            notice = "No finished jars found in the Medium or Large item groups."
        else:
            notice = (
                f"No selling branch warehouses were found besides {source}. "
                "A branch needs a non-group, enabled warehouse of its own."
            )
        return plan.build_plan(
            **{**empty, "branches": branches, "items": items},
            notice=notice,
        )

    item_codes = [item["item_code"] for item in items]
    branch_warehouses = [entry["warehouse"] for entry in branches]

    # The two batched reads that carry the payload.
    stock = _resolve_stock([source] + branch_warehouses, item_codes)
    sold = _resolve_sales(branch_warehouses, item_codes, sales_days)

    source_available = {code: stock.get((source, code), 0.0) for code in item_codes}

    return plan.build_plan(
        generated_on=_generated_on(),
        company=company,
        source_warehouse=source,
        cover_days=cover_days,
        sales_days=sales_days,
        items=items,
        branches=branches,
        source_available=source_available,
        on_hand=stock,
        sold=sold,
    )

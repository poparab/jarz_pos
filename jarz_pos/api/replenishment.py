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

The same module also serves :func:`get_production_round` — the step before a
delivery: how many batches of every jar to make, which bases/mixes first, and
which materials are short.  It reuses the resolvers above and adds a handful of
batched reads (weekly sales, the BOM tree one level per query pair, material
stock, item alternatives, item metadata); the maths lives in
``services/production_round_planning``.
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
from jarz_pos.services import production_round_planning as round_plan
# The Production Board's read gate.  ``api/production`` imports nothing from
# this module, so a top-level import is cycle-free (``api/manufacturing`` does
# the same).
from jarz_pos.api.production import _ensure_production_view_access

#: The owner's standing answer to "how much cover should a branch hold": a
#: 14-day delivery cycle plus a 7-day backup week — the same stock level the
#: production round (``get_production_round``) makes for.
DEFAULT_COVER_DAYS = 21
#: Eight weeks: the same sales history the production round reads, so the two
#: screens agree on what a branch sells.
DEFAULT_SALES_DAYS = 56

MIN_COVER_DAYS, MAX_COVER_DAYS = 1, 90
MIN_SALES_DAYS, MAX_SALES_DAYS = 1, 365

#: Name hints used only to *guess* the factory store when neither the caller
#: nor the Company's Default Finished Goods Warehouse names one.
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
            fields=["name as item_code", "item_name", "stock_uom", "item_group"],
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
            # The jar size (``Medium`` / ``Large``); the production round
            # picks the batch size from it.
            "item_group": str(row.get("item_group") or ""),
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
    company: Optional[str] = None,
) -> Optional[str]:
    """The factory store: the caller's choice, then the configured one, then a guess.

    "Configured" is the **Company's** ``default_fg_warehouse``, not Manufacturing
    Settings: v16's ``set_company_wise_warehouses`` patch moved the field, and
    ``get_single_value`` *raises* on a field the meta no longer has, so the old
    read failed on every call and always fell through to the name guess. Same
    read as ``_get_mfg_defaults`` in ``api/manufacturing.py``.
    """
    chosen = (source_warehouse or "").strip()
    if chosen:
        return chosen

    configured = None
    if company:
        try:
            configured = frappe.db.get_value("Company", company, "default_fg_warehouse")
        except Exception:
            _log_failure("JARZ Replenishment - FG warehouse read failed", _traceback())
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
        source_warehouse: the factory store; defaults to the company's
            ``default_fg_warehouse``.
        cover_days: days of cover each branch should hold (default 21: the
            14-day delivery cycle plus a 7-day backup).
        sales_days: how many completed days of sales set the rate (default 56).

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
    source = _resolve_source_warehouse(source_warehouse, warehouse_rows, company)

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
            notice="No factory store is configured. Set Company "
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


# ── Production round resolvers ──────────────────────────────────────────
# Each takes a ``notices`` list and appends one plain-English line when its
# read fails, so a degraded round says what it could not see.

#: Guard on the BOM walk.  Real trees here are 2-3 levels deep.
MAX_BOM_LEVELS = 12


def _unique(values: Any) -> List[str]:
    return sorted({str(v) for v in values or [] if v})


def _resolve_weekly_sales(
    warehouses: Sequence[str],
    item_codes: Sequence[str],
    from_date: Any,
    to_date: Any,
    notices: List[str],
) -> Dict[Tuple[str, str], Dict[int, float]]:
    """ONE query: net qty sold per (warehouse, item, week) over the window.

    Week 0 is the 7 days ending ``to_date`` (yesterday).  Submitted invoices
    only, returns included so they net — same rule as :func:`_resolve_sales`.
    """
    warehouses = _unique(warehouses)
    item_codes = _unique(item_codes)
    if not warehouses or not item_codes:
        return {}

    try:
        rows = frappe.db.sql(
            """
            SELECT sii.warehouse AS warehouse,
                   sii.item_code AS item_code,
                   FLOOR(DATEDIFF(%(to_date)s, si.posting_date) / 7) AS week,
                   SUM(sii.stock_qty) AS qty
            FROM `tabSales Invoice Item` sii
            INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE si.docstatus = 1
              AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
              AND sii.warehouse IN %(warehouses)s
              AND sii.item_code IN %(codes)s
            GROUP BY sii.warehouse, sii.item_code, week
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
        _log_failure("JARZ Production Round - weekly sales read failed", _traceback())
        notices.append("Sales history could not be read; every jar shows as not selling.")
        return {}

    out: Dict[Tuple[str, str], Dict[int, float]] = {}
    for row in rows or []:
        try:
            week = int(plan.to_float(row.get("week"), -1))
        except (TypeError, ValueError):
            continue
        key = (str(row.get("warehouse")), str(row.get("item_code")))
        bucket = out.setdefault(key, {})
        bucket[week] = bucket.get(week, 0.0) + plan.to_float(row.get("qty"), 0.0)
    return out


def _resolve_bom_tree(
    item_codes: Sequence[str],
    notices: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Every item's chosen BOM, walked down level by level.

    Two queries per level (headers, then lines) — never one per item.  The
    walk stops when a level finds no new item with a BOM.  Returns
    ``{item_code: {"name", "quantity", "lines": [...]}}``.
    """
    boms: Dict[str, Dict[str, Any]] = {}
    probed: set = set()
    frontier = set(_unique(item_codes))

    for _level in range(MAX_BOM_LEVELS):
        frontier -= probed
        if not frontier:
            break
        probed |= frontier
        try:
            header_rows = frappe.db.sql(
                """
                SELECT b.name AS name, b.item AS item, b.quantity AS quantity,
                       b.is_default AS is_default, i.default_bom AS default_bom
                FROM `tabBOM` b
                INNER JOIN `tabItem` i ON i.name = b.item
                WHERE b.item IN %(codes)s
                  AND b.docstatus = 1
                  AND b.is_active = 1
                """,
                {"codes": sorted(frontier)},
                as_dict=True,
            )
            headers = round_plan.pick_default_boms(header_rows or [])
            if not headers:
                break
            line_rows = frappe.db.sql(
                """
                SELECT bi.parent AS parent, bi.item_code AS item_code,
                       bi.stock_qty AS stock_qty, bi.stock_uom AS stock_uom,
                       bi.do_not_explode AS do_not_explode, bi.idx AS idx
                FROM `tabBOM Item` bi
                WHERE bi.parenttype = 'BOM'
                  AND bi.parent IN %(boms)s
                ORDER BY bi.parent, bi.idx
                """,
                {"boms": sorted({h["name"] for h in headers.values()})},
                as_dict=True,
            )
        except Exception:
            _log_failure("JARZ Production Round - BOM read failed", _traceback())
            notices.append("Some BOMs could not be read; materials and bases may be incomplete.")
            break

        lines_by_bom: Dict[str, List[Dict[str, Any]]] = {}
        for row in line_rows or []:
            lines_by_bom.setdefault(str(row.get("parent")), []).append(
                {
                    "item_code": str(row.get("item_code") or ""),
                    "stock_qty": plan.to_float(row.get("stock_qty"), 0.0),
                    "stock_uom": str(row.get("stock_uom") or ""),
                    "do_not_explode": int(plan.to_float(row.get("do_not_explode"), 0.0)),
                }
            )

        next_frontier: set = set()
        for item, header in headers.items():
            lines = lines_by_bom.get(header["name"], [])
            boms[item] = {"name": header["name"], "quantity": header["quantity"], "lines": lines}
            next_frontier.update(line["item_code"] for line in lines if line["item_code"])
        frontier = next_frontier
    else:
        if frontier - probed:
            notices.append(f"BOM tree deeper than {MAX_BOM_LEVELS} levels; the rest was not read.")

    return boms


def _resolve_item_alternatives(
    item_codes: Sequence[str],
    notices: List[str],
) -> List[Dict[str, Any]]:
    """ONE query: ``Item Alternative`` rows touching any of these items."""
    item_codes = _unique(item_codes)
    if not item_codes:
        return []
    try:
        rows = frappe.db.sql(
            """
            SELECT ia.item_code AS item_code,
                   ia.alternative_item_code AS alternative_item_code,
                   ia.two_way AS two_way
            FROM `tabItem Alternative` ia
            WHERE ia.item_code IN %(codes)s
               OR (ia.two_way = 1 AND ia.alternative_item_code IN %(codes)s)
            """,
            {"codes": item_codes},
            as_dict=True,
        )
    except Exception:
        _log_failure("JARZ Production Round - item alternative read failed", _traceback())
        notices.append("Item alternatives could not be read; substitutes are not counted.")
        return []
    return [dict(row) for row in rows or []]


def _resolve_material_stock(
    warehouses: Sequence[str],
    item_codes: Sequence[str],
    notices: List[str],
) -> Dict[str, float]:
    """ONE query: on-hand per item summed over the given (factory-side) warehouses.

    Each bin is floored at zero BEFORE summing: a negative bin in one store is
    a counting error there, and summing it raw would cancel real stock in
    another store and invent a shortage.
    """
    warehouses = _unique(warehouses)
    item_codes = _unique(item_codes)
    if not warehouses or not item_codes:
        return {}
    try:
        rows = frappe.db.sql(
            """
            SELECT b.item_code AS item_code, SUM(GREATEST(b.actual_qty, 0)) AS qty
            FROM `tabBin` b
            WHERE b.warehouse IN %(warehouses)s
              AND b.item_code IN %(codes)s
            GROUP BY b.item_code
            """,
            {"warehouses": warehouses, "codes": item_codes},
            as_dict=True,
        )
    except Exception:
        _log_failure("JARZ Production Round - material stock read failed", _traceback())
        notices.append("Factory material stock could not be read; everything shows as missing.")
        return {}
    return {str(row["item_code"]): plan.to_float(row.get("qty"), 0.0) for row in rows or []}


def _resolve_item_meta(
    item_codes: Sequence[str],
    notices: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Name, group and stock unit for every component, plus whether that unit
    only comes whole (``UOM.must_be_whole_number``) — two fixed-cost reads."""
    item_codes = _unique(item_codes)
    if not item_codes:
        return {}
    try:
        rows = frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes]},
            fields=["name", "item_name", "item_group", "stock_uom", "is_stock_item"],
            limit_page_length=0,
        )
    except Exception:
        _log_failure("JARZ Production Round - item read failed", _traceback())
        notices.append("Item names could not be read; codes are shown instead.")
        return {}
    whole: set = set()
    try:
        whole = set(
            frappe.get_all("UOM", filters={"must_be_whole_number": 1}, pluck="name", limit_page_length=0)
            or []
        )
    except Exception:
        _log_failure("JARZ Production Round - UOM read failed", _traceback())
    return {
        str(row.get("name")): {
            "item_name": str(row.get("item_name") or row.get("name")),
            "item_group": str(row.get("item_group") or ""),
            "stock_uom": str(row.get("stock_uom") or ""),
            "whole_number": str(row.get("stock_uom") or "") in whole,
            "is_stock_item": bool(int(plan.to_float(row.get("is_stock_item"), 1.0))),
        }
        for row in rows or []
        if row.get("name")
    }


# ── Production round endpoint ───────────────────────────────────────────


@frappe.whitelist()
def get_production_round(
    company: Optional[str] = None,
    cycle_days: Any = round_plan.DEFAULT_CYCLE_DAYS,
    backup_days: Any = round_plan.DEFAULT_BACKUP_DAYS,
    sales_weeks: Any = round_plan.DEFAULT_SALES_WEEKS,
    batch_medium: Any = round_plan.DEFAULT_BATCH_MEDIUM,
    batch_large: Any = round_plan.DEFAULT_BATCH_LARGE,
) -> Dict[str, Any]:
    """How many batches of every jar to make now, and what that needs.

    Read-only.  Each branch holds ``cycle_days + backup_days`` of its own
    weekly sales (more for a volatile jar); the factory makes, in quarter
    batches, what the branches need beyond what the factory store holds.  The
    BOM explosion then lists the bases/mixes to make first and the raw
    materials and packaging that are short.

    Payload documented in ``services.production_round_planning.build_production_round``
    and ``.scratch/production-round/CONTRACT.md``.
    """
    _ensure_production_view_access()

    params = round_plan.coerce_parameters(
        cycle_days=cycle_days,
        backup_days=backup_days,
        sales_weeks=sales_weeks,
        batch_medium=batch_medium,
        batch_large=batch_large,
    )
    notices: List[str] = []

    company = _resolve_company(company)
    warehouse_rows = _resolve_warehouse_rows(company)
    source = _resolve_source_warehouse(None, warehouse_rows, company)
    if not source:
        notices.append(
            "No factory store is configured (Company > Default Finished Goods "
            "Warehouse); factory jar stock is counted as zero."
        )

    branches = plan.select_selling_warehouses(
        warehouse_rows,
        source_warehouse=source,
        also_excluded=is_non_sellable_warehouse,
        branch_labels=_resolve_branch_labels(),
    )
    items = _resolve_jar_items()
    if not items:
        notices.append("No finished jars found in the Medium or Large item groups.")
    if not branches:
        notices.append("No selling branch warehouses were found; nothing sells, so nothing is made.")

    today = getdate(nowdate())
    sales_from, sales_to = round_plan.sales_window(today, params["sales_weeks"])

    item_codes = [item["item_code"] for item in items]
    branch_warehouses = [entry["warehouse"] for entry in branches]

    stock = _resolve_stock(([source] if source else []) + branch_warehouses, item_codes)
    weekly = _resolve_weekly_sales(branch_warehouses, item_codes, sales_from, sales_to, notices)
    factory_stock = {code: stock.get((source, code), 0.0) for code in item_codes} if source else {}

    boms = _resolve_bom_tree(item_codes, notices)
    components = set(boms)
    for bom in boms.values():
        components.update(line["item_code"] for line in bom.get("lines") or [] if line.get("item_code"))
    alternatives = round_plan.alternatives_map(_resolve_item_alternatives(sorted(components), notices))
    stock_codes = set(components)
    for alts in alternatives.values():
        stock_codes.update(alts)

    # Material stock: every factory-side warehouse — not the selling branches,
    # not WIP / Rejected (already inside a Work Order or scrapped).
    branch_set = set(branch_warehouses)
    material_warehouses = [
        str(row.get("name"))
        for row in warehouse_rows
        if row.get("name")
        and str(row.get("name")) not in branch_set
        and not is_non_sellable_warehouse(row.get("name"), row.get("warehouse_type"))
    ]
    material_stock = _resolve_material_stock(material_warehouses, sorted(stock_codes), notices)
    item_meta = _resolve_item_meta(sorted(stock_codes), notices)

    return round_plan.build_production_round(
        generated_on=_generated_on()[:19],
        company=company,
        source_warehouse=source,
        cycle_days=params["cycle_days"],
        backup_days=params["backup_days"],
        sales_weeks=params["sales_weeks"],
        sales_from=sales_from.isoformat(),
        sales_to=sales_to.isoformat(),
        batch_sizes={"Medium": params["batch_medium"], "Large": params["batch_large"]},
        items=items,
        branches=branches,
        weekly_sales=weekly,
        branch_stock=stock,
        factory_stock=factory_stock,
        boms=boms,
        material_stock=material_stock,
        alternatives=alternatives,
        item_meta=item_meta,
        notices=notices,
    )

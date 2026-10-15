"""
Product Analytics API — Jarz POS ERPNext Desk page.

Data source: tabSales Invoice (is_pos=1 individual invoices, docstatus=1 = submitted/non-cancelled)
             tabSales Invoice Item (line items per invoice)

Product types:
  - Bundle  : invoices that contain a Jarz Bundle erpnext_item header row
  - Small   : item_group = "Small"
  - Medium  : item_group in {"Medium", "Meduim"}  (typo alias exists in data)
  - Large   : item_group = "Large"

Cancelled invoice filter: docstatus = 1 (Submitted).
  docstatus=0 = Draft, docstatus=1 = Submitted, docstatus=2 = Cancelled.
  is_return = 0 additionally excludes credit notes/returns.

Cost of goods (since 2026-10): the cost actually posted for the line —
``Sales Invoice Item.incoming_rate × stock_qty`` on an ``update_stock`` invoice,
else the Delivery Note Item made against it (see
``financial_report.actual_line_cost_sql``). Only a line with no posted cost
falls back to the default BOM's cost per unit × stock qty. Each product row says
which source dominated (``cost_source``) and the response carries
``cost_coverage_pct`` = share of revenue costed from posted cost of goods.

Aggregation is done in SQL (two GROUP BY queries over the classified lines);
Python only shapes the aggregated rows.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set

import frappe
from frappe import _

from jarz_pos.constants import ROLES
from jarz_pos.utils.invoice_utils import get_area_label

# ── Item group sets ──────────────────────────────────────────────────────
_SMALL_GROUPS: Set[str] = {"Small"}
_MEDIUM_GROUPS: Set[str] = {"Medium", "Meduim"}
_LARGE_GROUPS: Set[str] = {"Large"}

_TYPE_BUNDLE = "Bundle"
_TYPE_SMALL = "Small"
_TYPE_MEDIUM = "Medium"
_TYPE_LARGE = "Large"


def _ensure_jarz_manager() -> None:
    roles = set(frappe.get_roles(frappe.session.user))
    if ROLES.JARZ_MANAGER not in roles and ROLES.ADMINISTRATOR not in roles:
        frappe.throw(_("Only JARZ Manager can access product analytics"), frappe.PermissionError)


def _default_dates() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=29)).isoformat(), today.isoformat()


def _get_bundle_item_codes() -> Set[str]:
    rows = frappe.get_all(
        "Jarz Bundle",
        filters={"disabled": 0},
        fields=["erpnext_item"],
    )
    return {r["erpnext_item"] for r in rows if r.get("erpnext_item")}


def _get_bom_costs(item_codes: List[str]) -> Dict[str, float]:
    """Default BOM cost per unit (``total_cost / quantity``) per item.

    No longer used by :func:`get_product_analytics` (the BOM fallback is joined
    in SQL); kept for callers that import it.
    """
    if not item_codes:
        return {}
    boms = frappe.db.sql(
        """
        SELECT item, total_cost, quantity
        FROM `tabBOM`
        WHERE is_active = 1
          AND is_default = 1
          AND item IN %(codes)s
        """,
        {"codes": tuple(item_codes)},
        as_dict=True,
    )
    out: Dict[str, float] = {}
    for b in boms:
        qty = float(b.get("quantity") or 0) or 1.0
        out[b["item"]] = float(b.get("total_cost") or 0) / qty
    return out


_TYPE_ORDER = [_TYPE_BUNDLE, _TYPE_SMALL, _TYPE_MEDIUM, _TYPE_LARGE]
_ALL_SIZE_GROUPS = tuple(sorted(_SMALL_GROUPS | _MEDIUM_GROUPS | _LARGE_GROUPS))
# ``IN ()`` is a syntax error, so an empty bundle list is sent as a code no item has.
_NO_CODE = "__jarz_no_item__"


def _lines_sql(branch: Optional[str]) -> str:
    """Classified invoice lines in the range, with their actual and BOM cost.

    One row per Sales Invoice Item that the report counts: a bundle header, any
    line on an invoice that has one, or a line whose group is a jar size.
    ``actual_cost`` is the posted cost of goods (see
    ``financial_report.actual_line_cost_sql``) or NULL; ``bom_unit`` is the
    default BOM's cost per unit of stock UOM (``total_cost / quantity``).
    """
    from jarz_pos.api.financial_report import actual_line_cost_sql

    dn_join, cost_expr = actual_line_cost_sql("fd", "td")
    branch_cond = "AND si.pos_profile = %(branch)s" if branch else ""
    return f"""
        SELECT sii.item_code,
               sii.item_name,
               TRIM(IFNULL(sii.item_group, '')) AS item_group,
               IFNULL(sii.qty, 0) AS qty,
               IFNULL(sii.stock_qty, 0) AS stock_qty,
               -- Net of the invoice-level discount, so product revenue agrees with
               -- the Sales ledger (Sep 2026: gross line amounts overstated it by 816).
               IFNULL(sii.base_net_amount, 0) AS amount,
               si.name AS invoice,
               COALESCE(NULLIF(si.territory, ''), 'Unassigned') AS territory,
               si.posting_date,
               CASE WHEN sii.item_code IN %(bundles)s THEN 1 ELSE 0 END AS is_header,
               CASE WHEN bi.parent IS NULL THEN 0 ELSE 1 END AS in_bundle,
               {cost_expr} AS actual_cost,
               bom.unit_cost AS bom_unit
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si
          ON si.name = sii.parent AND sii.parenttype = 'Sales Invoice'
        LEFT JOIN (
            SELECT DISTINCT b.parent
            FROM `tabSales Invoice Item` b
            JOIN `tabSales Invoice` bsi ON bsi.name = b.parent
            WHERE b.parenttype = 'Sales Invoice'
              AND b.item_code IN %(bundles)s
              AND bsi.docstatus = 1
              AND bsi.posting_date BETWEEN %(fd)s AND %(td)s
        ) bi ON bi.parent = si.name
        LEFT JOIN (
            SELECT item, MAX(total_cost / NULLIF(quantity, 0)) AS unit_cost
            FROM `tabBOM`
            WHERE is_active = 1 AND is_default = 1
            GROUP BY item
        ) bom ON bom.item = sii.item_code
        {dn_join}
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(fd)s AND %(td)s
          {branch_cond}
          AND (
              sii.item_code IN %(bundles)s
              OR bi.parent IS NOT NULL
              OR TRIM(IFNULL(sii.item_group, '')) IN %(groups)s
          )
    """


# Line cost: the posted cost of goods, else BOM × stock qty, else nothing.
_LINE_COST = "COALESCE(l.actual_cost, l.bom_unit * l.stock_qty, 0)"


def _product_rows(params: Dict[str, Any], lines: str) -> List[Dict[str, Any]]:
    return frappe.db.sql(
        f"""
        SELECT l.item_code,
               MAX(l.item_name) AS item_name,
               l.item_group,
               l.is_header,
               l.in_bundle,
               SUM(l.qty) AS qty,
               SUM(l.amount) AS amount,
               SUM({_LINE_COST}) AS cost,
               SUM(IFNULL(l.actual_cost, 0)) AS actual_cost,
               SUM(CASE WHEN l.actual_cost IS NOT NULL THEN l.qty ELSE 0 END) AS actual_qty,
               SUM(CASE WHEN l.actual_cost IS NOT NULL THEN l.amount ELSE 0 END) AS actual_amount,
               SUM(CASE WHEN l.actual_cost IS NULL AND IFNULL(l.bom_unit, 0) > 0
                        THEN l.qty ELSE 0 END) AS bom_qty,
               SUM(CASE WHEN l.actual_cost IS NULL AND IFNULL(l.bom_unit, 0) > 0
                        THEN l.amount ELSE 0 END) AS bom_amount,
               MAX(IFNULL(l.bom_unit, 0)) AS bom_unit
        FROM ({lines}) l
        GROUP BY l.item_code, l.item_group, l.is_header, l.in_bundle
        """,
        params,
        as_dict=True,
    ) or []


def _territory_day_rows(params: Dict[str, Any], lines: str) -> List[Dict[str, Any]]:
    # An invoice has one territory and one posting date, so distinct counts per
    # (territory, day) add up to distinct counts per territory, per day, and overall.
    return frappe.db.sql(
        f"""
        SELECT l.territory,
               l.posting_date,
               COUNT(DISTINCT l.invoice) AS orders,
               SUM(CASE WHEN l.is_header = 0 THEN l.amount ELSE 0 END) AS revenue,
               SUM(CASE WHEN l.is_header = 0 THEN {_LINE_COST} ELSE 0 END) AS cost
        FROM ({lines}) l
        GROUP BY l.territory, l.posting_date
        """,
        params,
        as_dict=True,
    ) or []


def _intrinsic_type(item_group: str) -> Optional[str]:
    if item_group in _SMALL_GROUPS:
        return _TYPE_SMALL
    if item_group in _MEDIUM_GROUPS:
        return _TYPE_MEDIUM
    if item_group in _LARGE_GROUPS:
        return _TYPE_LARGE
    return None


def _cost_source(actual_qty: float, bom_qty: float) -> str:
    """Where most of a product's cost came from."""
    if actual_qty > 0 and actual_qty >= bom_qty:
        return "actual"
    if bom_qty > 0:
        return "bom"
    return "none"


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


@frappe.whitelist()
def get_product_analytics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return aggregated product analytics for the given date range.

    ``branch`` (optional POS Profile) limits it to invoices of that branch.

    Response shape:
    {
      "period": {"date_from": str, "date_to": str},
      "filters": {"date_from": str, "date_to": str, "branch": str|null},
      "summary": {
        "total_revenue": float, "total_orders": int,
        "total_gross_profit": float, "avg_order_value": float,
        "best_selling_product": {"item_name": str, "total_qty": float},
        "top_territory": {"territory": str, "revenue": float}
      },
      "cost_coverage_pct": float,          # revenue costed from posted COGS, %
      "cost_sources": {"actual": float, "bom": float, "none": float},  # revenue by cost source
      "by_product_type": [
        {"type": str, "units": int, "revenue": float,
         "cost": float, "profit": float, "margin_pct": float}
      ],
      "top_products": [
        {"item_code": str, "item_name": str, "type": str,
         "total_qty": float, "total_revenue": float,
         "bom_cost_per_unit": float, "total_cost": float,
         "gross_profit": float, "margin_pct": float,
         "cost_source": "actual"|"bom"|"none",
         "actual_cost_per_unit": float|null, "cost_coverage_pct": float}
      ],
      "by_territory": [
        {"territory": str, "orders": int, "revenue": float, "profit": float}
      ],
      "trend": [{"date": str, "revenue": float, "orders": int}],
      "bundle_composition": [
        {"item_code": str, "item_name": str, "item_group": str,
         "times_in_bundle": float, "revenue": float}
      ]
    }
    """
    _ensure_jarz_manager()

    if not date_from or not date_to:
        date_from, date_to = _default_dates()
    branch = (branch or "").strip() or None

    bundle_item_codes = _get_bundle_item_codes()
    params: Dict[str, Any] = {
        "fd": date_from,
        "td": date_to,
        "branch": branch,
        "bundles": tuple(sorted(bundle_item_codes)) or (_NO_CODE,),
        "groups": _ALL_SIZE_GROUPS,
    }
    lines = _lines_sql(branch)
    product_rows = _product_rows(params, lines)
    if not product_rows:
        return _empty_response(date_from, date_to, branch)
    territory_day_rows = _territory_day_rows(params, lines)

    # ── Aggregation accumulators ─────────────────────────────────────────
    type_agg: Dict[str, Dict[str, float]] = {
        t: {"units": 0.0, "revenue": 0.0, "cost": 0.0} for t in _TYPE_ORDER
    }
    product_agg: Dict[str, Dict[str, Any]] = {}
    bundle_comp: Dict[str, Dict[str, Any]] = {}
    sources = {"actual": 0.0, "bom": 0.0, "none": 0.0}

    for row in product_rows:
        item_code = row.get("item_code") or ""
        item_name = (row.get("item_name") or item_code).strip()
        item_group = (row.get("item_group") or "").strip()
        qty = _f(row.get("qty"))
        amount = _f(row.get("amount"))
        cost = _f(row.get("cost"))
        is_bundle_header = bool(int(row.get("is_header") or 0))
        in_bundle_invoice = bool(int(row.get("in_bundle") or 0))

        # Intrinsic type (item group) labels the product; context type (bundle
        # or not) attributes its revenue — same rules as before the SQL rewrite.
        intrinsic_type = _intrinsic_type(item_group)
        if is_bundle_header or in_bundle_invoice:
            item_type = _TYPE_BUNDLE
        elif intrinsic_type:
            item_type = intrinsic_type
        else:
            continue  # unrecognised (shipping fees, etc.) — the SQL already drops these

        if is_bundle_header:
            # Only count units from the header; revenue comes from child rows
            type_agg[_TYPE_BUNDLE]["units"] += qty
            continue

        type_agg[item_type]["revenue"] += amount
        type_agg[item_type]["cost"] += cost
        if not in_bundle_invoice:
            type_agg[item_type]["units"] += qty

        actual_amount = _f(row.get("actual_amount"))
        bom_amount = _f(row.get("bom_amount"))
        sources["actual"] += actual_amount
        sources["bom"] += bom_amount
        sources["none"] += amount - actual_amount - bom_amount

        p = product_agg.get(item_code)
        if p is None:
            p = product_agg[item_code] = {
                "item_code": item_code,
                "item_name": item_name,
                "type": intrinsic_type,
                "total_qty": 0.0,
                "total_revenue": 0.0,
                "total_cost": 0.0,
                "bom_cost_per_unit": 0.0,
                "actual_cost": 0.0,
                "actual_qty": 0.0,
                "actual_amount": 0.0,
                "bom_qty": 0.0,
            }
        elif p["type"] is None and intrinsic_type:
            p["type"] = intrinsic_type
        p["total_qty"] += qty
        p["total_revenue"] += amount
        p["total_cost"] += cost
        p["bom_cost_per_unit"] = max(p["bom_cost_per_unit"], _f(row.get("bom_unit")))
        p["actual_cost"] += _f(row.get("actual_cost"))
        p["actual_qty"] += _f(row.get("actual_qty"))
        p["actual_amount"] += actual_amount
        p["bom_qty"] += _f(row.get("bom_qty"))

        if in_bundle_invoice:
            comp = bundle_comp.setdefault(item_code, {
                "item_code": item_code,
                "item_name": item_name,
                "item_group": item_group,
                "times_in_bundle": 0.0,
                "revenue": 0.0,
            })
            comp["times_in_bundle"] += qty
            comp["revenue"] += amount

    # ── by_product_type ──────────────────────────────────────────────────
    by_product_type = []
    for ptype in _TYPE_ORDER:
        agg = type_agg[ptype]
        revenue = agg["revenue"]
        cost = agg["cost"]
        profit = revenue - cost
        by_product_type.append({
            "type": ptype,
            "units": int(agg["units"]),
            "revenue": round(revenue, 2),
            "cost": round(cost, 2),
            "profit": round(profit, 2),
            "margin_pct": round((profit / revenue * 100) if revenue > 0 else 0.0, 1),
        })

    # ── top_products ─────────────────────────────────────────────────────
    top_products = []
    for p in sorted(product_agg.values(), key=lambda x: -x["total_revenue"]):
        revenue = p["total_revenue"]
        cost = p["total_cost"]
        profit = revenue - cost
        top_products.append({
            "item_code": p["item_code"],
            "item_name": p["item_name"],
            # Lines without a size group only ever appear inside bundles.
            "type": p["type"] or _TYPE_BUNDLE,
            "total_qty": round(p["total_qty"], 2),
            "total_revenue": round(revenue, 2),
            "bom_cost_per_unit": round(p["bom_cost_per_unit"], 2),
            "total_cost": round(cost, 2),
            "gross_profit": round(profit, 2),
            "margin_pct": round((profit / revenue * 100) if revenue > 0 else 0.0, 1),
            "cost_source": _cost_source(p["actual_qty"], p["bom_qty"]),
            "actual_cost_per_unit": (
                round(p["actual_cost"] / p["actual_qty"], 2) if p["actual_qty"] else None
            ),
            "cost_coverage_pct": _pct(p["actual_amount"], revenue),
        })

    # ── by_territory / trend (from the territory × day aggregate) ────────
    territory_agg: Dict[str, Dict[str, float]] = {}
    trend_agg: Dict[str, Dict[str, float]] = {}
    for row in territory_day_rows:
        territory = (row.get("territory") or "Unassigned").strip()
        day = str(row.get("posting_date"))
        orders = int(row.get("orders") or 0)
        revenue = _f(row.get("revenue"))
        cost = _f(row.get("cost"))
        t = territory_agg.setdefault(territory, {"orders": 0, "revenue": 0.0, "cost": 0.0})
        t["orders"] += orders
        t["revenue"] += revenue
        t["cost"] += cost
        d = trend_agg.setdefault(day, {"orders": 0, "revenue": 0.0})
        d["orders"] += orders
        d["revenue"] += revenue

    # Territories are *named* by their WooCommerce area code, so the raw group
    # key is "EGNASRCITY" — a chart axis full of those is unreadable. Group on
    # the key, label with the Arabic name.
    by_territory = [
        {
            "territory": get_area_label(t),
            "orders": int(agg["orders"]),
            "revenue": round(agg["revenue"], 2),
            "profit": round(agg["revenue"] - agg["cost"], 2),
        }
        for t, agg in sorted(territory_agg.items(), key=lambda x: -x[1]["revenue"])
    ]
    trend = [
        {"date": d, "revenue": round(agg["revenue"], 2), "orders": int(agg["orders"])}
        for d, agg in sorted(trend_agg.items())
    ]

    # ── Summary ──────────────────────────────────────────────────────────
    # Orders = invoices with at least one classified line (headers included).
    total_orders = sum(int(agg["orders"]) for agg in territory_agg.values())
    total_revenue = sum(p["total_revenue"] for p in top_products)
    total_cost = sum(p["total_cost"] for p in top_products)
    total_gross_profit = total_revenue - total_cost
    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else 0.0

    best_selling = max(top_products, key=lambda x: x["total_qty"]) if top_products else None
    top_territory = by_territory[0] if by_territory else None

    summary = {
        "total_revenue": round(total_revenue, 2),
        "total_orders": total_orders,
        "total_gross_profit": round(total_gross_profit, 2),
        "avg_order_value": round(avg_order_value, 2),
        "best_selling_product": {
            "item_name": best_selling["item_name"] if best_selling else "",
            "total_qty": best_selling["total_qty"] if best_selling else 0.0,
        },
        "top_territory": {
            "territory": top_territory["territory"] if top_territory else "",
            "revenue": top_territory["revenue"] if top_territory else 0.0,
        },
    }

    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "filters": {"date_from": date_from, "date_to": date_to, "branch": branch},
        "summary": summary,
        "cost_coverage_pct": _pct(sources["actual"], total_revenue),
        "cost_sources": {k: round(v, 2) for k, v in sources.items()},
        "by_product_type": by_product_type,
        "top_products": top_products,
        "by_territory": by_territory,
        "trend": trend,
        "bundle_composition": sorted(
            bundle_comp.values(), key=lambda x: -x["times_in_bundle"]
        ),
    }


def _empty_response(date_from: str, date_to: str, branch: Optional[str] = None) -> Dict[str, Any]:
    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "filters": {"date_from": date_from, "date_to": date_to, "branch": branch},
        "summary": {
            "total_revenue": 0.0,
            "total_orders": 0,
            "total_gross_profit": 0.0,
            "avg_order_value": 0.0,
            "best_selling_product": {"item_name": "", "total_qty": 0.0},
            "top_territory": {"territory": "", "revenue": 0.0},
        },
        "cost_coverage_pct": 0.0,
        "cost_sources": {"actual": 0.0, "bom": 0.0, "none": 0.0},
        "by_product_type": [],
        "top_products": [],
        "by_territory": [],
        "trend": [],
        "bundle_composition": [],
    }

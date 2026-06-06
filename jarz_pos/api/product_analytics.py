"""
Product Analytics API for the Jarz POS mobile app.

Aggregates sales, profit, and territory data from Sales Invoice records
to power the Product Analytics Dashboard.

Product types:
  - Bundle  : invoices that contain a Jarz Bundle erpnext_item header row
  - Medium  : item_group in {"Medium", "Meduim"}
  - Large   : item_group = "Large"
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set

import frappe
from frappe import _

from jarz_pos.constants import ROLES

# ── Item group sets ──────────────────────────────────────────────────────
_MEDIUM_GROUPS: Set[str] = {"Medium", "Meduim"}
_LARGE_GROUPS: Set[str] = {"Large"}

_TYPE_BUNDLE = "Bundle"
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
    if not item_codes:
        return {}
    boms = frappe.db.sql(
        """
        SELECT item, total_cost
        FROM `tabBOM`
        WHERE is_active = 1
          AND is_default = 1
          AND item IN %(codes)s
        """,
        {"codes": item_codes},
        as_dict=True,
    )
    return {b["item"]: float(b["total_cost"] or 0) for b in boms}


@frappe.whitelist()
def get_product_analytics(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Return aggregated product analytics for the given date range.

    Response shape:
    {
      "period": {"date_from": str, "date_to": str},
      "summary": {
        "total_revenue": float, "total_orders": int,
        "total_gross_profit": float, "avg_order_value": float,
        "best_selling_product": {"item_name": str, "total_qty": float},
        "top_territory": {"territory": str, "revenue": float}
      },
      "by_product_type": [
        {"type": str, "units": int, "revenue": float,
         "cost": float, "profit": float, "margin_pct": float}
      ],
      "top_products": [
        {"item_code": str, "item_name": str, "type": str,
         "total_qty": float, "total_revenue": float,
         "bom_cost_per_unit": float, "total_cost": float,
         "gross_profit": float, "margin_pct": float}
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

    bundle_item_codes = _get_bundle_item_codes()

    # ── Fetch invoice line items in date range ───────────────────────────
    rows = frappe.db.sql(
        """
        SELECT
            sii.item_code,
            sii.item_name,
            sii.item_group,
            sii.qty,
            sii.amount,
            si.name   AS invoice_name,
            si.territory,
            si.posting_date
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND si.is_return = 0
          AND si.posting_date BETWEEN %(date_from)s AND %(date_to)s
        ORDER BY si.posting_date ASC
        """,
        {"date_from": date_from, "date_to": date_to},
        as_dict=True,
    )

    if not rows:
        return _empty_response(date_from, date_to)

    # ── Find invoices that contain a bundle header ───────────────────────
    bundle_invoice_set: Set[str] = set()
    if bundle_item_codes:
        for row in rows:
            if row["item_code"] in bundle_item_codes:
                bundle_invoice_set.add(row["invoice_name"])

    # ── BOM cost lookup ──────────────────────────────────────────────────
    all_item_codes = list({row["item_code"] for row in rows})
    bom_cost_map = _get_bom_costs(all_item_codes)

    # ── Aggregation accumulators ─────────────────────────────────────────
    type_agg: Dict[str, Dict[str, Any]] = {
        _TYPE_BUNDLE: {"units": 0.0, "revenue": 0.0, "cost": 0.0},
        _TYPE_MEDIUM: {"units": 0.0, "revenue": 0.0, "cost": 0.0},
        _TYPE_LARGE:  {"units": 0.0, "revenue": 0.0, "cost": 0.0},
    }
    product_agg: Dict[str, Dict[str, Any]] = {}
    territory_agg: Dict[str, Dict[str, Any]] = {}
    trend_agg: Dict[str, Dict[str, Any]] = {}
    bundle_comp: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        item_code = row["item_code"] or ""
        item_name = (row["item_name"] or item_code).strip()
        item_group = (row["item_group"] or "").strip()
        qty = float(row["qty"] or 0)
        amount = float(row["amount"] or 0)
        invoice = row["invoice_name"] or ""
        territory = (row["territory"] or "Unassigned").strip()
        posting_date = str(row["posting_date"])

        in_bundle_invoice = invoice in bundle_invoice_set
        is_bundle_header = item_code in bundle_item_codes

        # Determine type for non-header items used in standalone invoices
        if is_bundle_header:
            item_type = _TYPE_BUNDLE
        elif in_bundle_invoice:
            item_type = _TYPE_BUNDLE
        elif item_group in _MEDIUM_GROUPS:
            item_type = _TYPE_MEDIUM
        elif item_group in _LARGE_GROUPS:
            item_type = _TYPE_LARGE
        else:
            # Skip items we don't recognise (shipping, etc.)
            continue

        bom_unit = bom_cost_map.get(item_code, 0.0)
        item_cost = bom_unit * qty

        # ── Type-level aggregation ────────────────────────────────────────
        if is_bundle_header:
            # Only count units from the header; revenue comes from child rows
            type_agg[_TYPE_BUNDLE]["units"] += qty
        else:
            type_agg[item_type]["revenue"] += amount
            type_agg[item_type]["cost"] += item_cost
            if not in_bundle_invoice:
                # Count individual units for non-bundle products
                type_agg[item_type]["units"] += qty

        # ── Per-product aggregation (skip bundle headers) ─────────────────
        if not is_bundle_header:
            disp_type = _TYPE_BUNDLE if in_bundle_invoice else item_type
            if item_code not in product_agg:
                product_agg[item_code] = {
                    "item_code": item_code,
                    "item_name": item_name,
                    "type": disp_type,
                    "total_qty": 0.0,
                    "total_revenue": 0.0,
                    "total_cost": 0.0,
                    "bom_cost_per_unit": bom_unit,
                }
            product_agg[item_code]["total_qty"] += qty
            product_agg[item_code]["total_revenue"] += amount
            product_agg[item_code]["total_cost"] += item_cost

        # ── Bundle composition ────────────────────────────────────────────
        if in_bundle_invoice and not is_bundle_header:
            if item_code not in bundle_comp:
                bundle_comp[item_code] = {
                    "item_code": item_code,
                    "item_name": item_name,
                    "item_group": item_group,
                    "times_in_bundle": 0.0,
                    "revenue": 0.0,
                }
            bundle_comp[item_code]["times_in_bundle"] += qty
            bundle_comp[item_code]["revenue"] += amount

        # ── Territory aggregation ─────────────────────────────────────────
        if territory not in territory_agg:
            territory_agg[territory] = {"invoices": set(), "revenue": 0.0, "cost": 0.0}
        if not is_bundle_header:
            territory_agg[territory]["revenue"] += amount
            territory_agg[territory]["cost"] += item_cost
        territory_agg[territory]["invoices"].add(invoice)

        # ── Trend aggregation ─────────────────────────────────────────────
        if posting_date not in trend_agg:
            trend_agg[posting_date] = {"revenue": 0.0, "invoices": set()}
        if not is_bundle_header:
            trend_agg[posting_date]["revenue"] += amount
        trend_agg[posting_date]["invoices"].add(invoice)

    # ── Build output: by_product_type ────────────────────────────────────
    by_product_type = []
    for ptype in [_TYPE_BUNDLE, _TYPE_MEDIUM, _TYPE_LARGE]:
        agg = type_agg[ptype]
        revenue = agg["revenue"]
        cost = agg["cost"]
        profit = revenue - cost
        margin_pct = (profit / revenue * 100) if revenue > 0 else 0.0
        by_product_type.append({
            "type": ptype,
            "units": int(agg["units"]),
            "revenue": round(revenue, 2),
            "cost": round(cost, 2),
            "profit": round(profit, 2),
            "margin_pct": round(margin_pct, 1),
        })

    # ── Build output: top_products ────────────────────────────────────────
    top_products = []
    for p in sorted(product_agg.values(), key=lambda x: -x["total_revenue"]):
        revenue = p["total_revenue"]
        cost = p["total_cost"]
        profit = revenue - cost
        margin_pct = (profit / revenue * 100) if revenue > 0 else 0.0
        top_products.append({
            "item_code": p["item_code"],
            "item_name": p["item_name"],
            "type": p["type"],
            "total_qty": round(p["total_qty"], 2),
            "total_revenue": round(revenue, 2),
            "bom_cost_per_unit": round(p["bom_cost_per_unit"], 2),
            "total_cost": round(cost, 2),
            "gross_profit": round(profit, 2),
            "margin_pct": round(margin_pct, 1),
        })

    # ── Build output: by_territory ────────────────────────────────────────
    by_territory = [
        {
            "territory": t,
            "orders": len(agg["invoices"]),
            "revenue": round(agg["revenue"], 2),
            "profit": round(agg["revenue"] - agg["cost"], 2),
        }
        for t, agg in sorted(territory_agg.items(), key=lambda x: -x[1]["revenue"])
    ]

    # ── Build output: trend ───────────────────────────────────────────────
    trend = [
        {
            "date": d,
            "revenue": round(agg["revenue"], 2),
            "orders": len(agg["invoices"]),
        }
        for d, agg in sorted(trend_agg.items())
    ]

    # ── Summary ───────────────────────────────────────────────────────────
    total_orders = len({row["invoice_name"] for row in rows})
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
        "summary": summary,
        "by_product_type": by_product_type,
        "top_products": top_products,
        "by_territory": by_territory,
        "trend": trend,
        "bundle_composition": sorted(
            bundle_comp.values(), key=lambda x: -x["times_in_bundle"]
        ),
    }


def _empty_response(date_from: str, date_to: str) -> Dict[str, Any]:
    return {
        "period": {"date_from": date_from, "date_to": date_to},
        "summary": {
            "total_revenue": 0.0,
            "total_orders": 0,
            "total_gross_profit": 0.0,
            "avg_order_value": 0.0,
            "best_selling_product": {"item_name": "", "total_qty": 0.0},
            "top_territory": {"territory": "", "revenue": 0.0},
        },
        "by_product_type": [],
        "top_products": [],
        "by_territory": [],
        "trend": [],
        "bundle_composition": [],
    }

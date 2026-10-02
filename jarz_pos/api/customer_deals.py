"""Customer deals API: time-limited special prices for one B2B customer.

Backs the "Deals" card on the B2B account screen. The pricing itself lives in
``services/customer_deals`` and is applied by the order rate resolver, so a deal
reaches the cart (catalog prices) and the booked invoice through the same path
every other B2B price already takes. Nothing here runs on a schedule: a deal
stops applying the day after ``valid_upto`` because the resolver stops matching
it.

Access mirrors ``api/price_lists``: reads for managers and B2B Sales Reps,
writes for FULL managers only (a deal is a price change).
"""

from __future__ import annotations

import json

import frappe

from jarz_pos.api.price_lists import (
    _category_rate,
    _ensure_full_manager_pricing_access,
    _ensure_pricing_read_access,
    _has_manager_pricing_access,
    _can_access_b2b,
    _pricing_categories,
)
from jarz_pos.services.customer_deals import (
    DEAL_DOCTYPE,
    deal_status,
)

_B2B_ORDER_PURPOSE = "B2B Supply"


def _today():
    return frappe.utils.getdate(frappe.utils.today())


def _require_customer(customer) -> str:
    customer = str(customer or "").strip()
    if not customer or not frappe.db.exists("Customer", customer):
        frappe.throw(f"Customer '{customer}' not found.")
    return customer


def _normal_price_list(customer: str) -> str | None:
    """The list a B2B Supply order for this customer prices from (no deal)."""
    from jarz_pos.api.pos import resolve_customer_price_list

    return (resolve_customer_price_list(customer, order_purpose=_B2B_ORDER_PURPOSE) or {}).get(
        "price_list"
    )


def _normal_item_rate(customer: str, price_list: str | None, item_code: str):
    """What this customer pays for one item when no deal is running."""
    if not price_list:
        return None
    from jarz_pos.services.invoice_creation import _resolve_item_rate_with_provenance

    rate, provenance = _resolve_item_rate_with_provenance(
        item_code, price_list, fallback_rate=0.0, customer=customer, include_deals=False
    )
    return None if provenance == "client" else rate


def _bundle_item_codes() -> set[str]:
    try:
        return set(
            frappe.get_all("Jarz Bundle", filters={"erpnext_item": ["is", "set"]}, pluck="erpnext_item")
        )
    except Exception:
        return set()


def _catalog(customer: str, price_list: str | None) -> dict:
    """Categories and items a deal can price, each with its normal rate."""
    bundles = _bundle_item_codes()
    groups = []
    for cat in _pricing_categories():
        normal = _category_rate(price_list, cat["item_group"]) if price_list else None
        groups.append(
            {
                "item_group": cat["item_group"],
                "item_count": cat["item_count"],
                "normal_rate": normal,
            }
        )
    items = []
    for row in frappe.get_all(
        "Item",
        filters={"disabled": 0, "is_sales_item": 1},
        fields=["name", "item_name", "item_group"],
        order_by="item_group asc, item_name asc",
    ):
        if row["name"] in bundles:
            continue
        items.append(
            {
                "item_code": row["name"],
                "item_name": row.get("item_name") or row["name"],
                "item_group": row.get("item_group"),
                "normal_rate": _normal_item_rate(customer, price_list, row["name"]),
            }
        )
    # Categories nobody prices on this list are noise in the picker; keep them
    # only when nothing is priced at all (e.g. a list that is not set up yet).
    priced_groups = [g for g in groups if g["normal_rate"] is not None]
    return {"categories": priced_groups or groups, "items": items}


def _serialize(doc, customer: str, price_list: str | None) -> dict:
    today = _today()
    status = deal_status(doc.as_dict(), today)
    lines = []
    for row in doc.items:
        if row.item_code:
            normal = _normal_item_rate(customer, price_list, row.item_code)
            label = row.item_name or frappe.db.get_value("Item", row.item_code, "item_name") or row.item_code
        else:
            normal = _category_rate(price_list, row.item_group) if price_list else None
            label = row.item_group
        lines.append(
            {
                "item_group": row.item_group or None,
                "item_code": row.item_code or None,
                "label": label,
                "rate": float(row.rate or 0),
                "normal_rate": normal,
            }
        )
    return {
        "name": doc.name,
        "customer": doc.customer,
        "valid_from": str(frappe.utils.getdate(doc.valid_from)),
        "valid_upto": str(frappe.utils.getdate(doc.valid_upto)),
        "status": status,
        "editable": status in ("upcoming", "active"),
        "notes": doc.notes or None,
        "created_by": frappe.utils.get_fullname(doc.owner) if doc.owner else None,
        "items": lines,
    }


def _parse_items(items) -> list[dict]:
    if isinstance(items, str):
        try:
            items = json.loads(items or "[]")
        except ValueError:
            frappe.throw("Deal items are not valid JSON.")
    if not isinstance(items, list) or not items:
        frappe.throw("Add at least one deal price.")
    rows = []
    for raw in items:
        if not isinstance(raw, dict):
            frappe.throw("Each deal price must be an object.")
        code = str(raw.get("item_code") or "").strip() or None
        group = str(raw.get("item_group") or "").strip() or None
        rate = raw.get("rate")
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            frappe.throw(f"'{code or group}': the deal rate must be a number.")
        if rate < 0:
            frappe.throw(f"'{code or group}': the deal rate must be zero or more.")
        rows.append({"item_code": code, "item_group": None if code else group, "rate": rate})
    return rows


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@frappe.whitelist()
def get_customer_deals(customer, include_catalog=1):
    """All deals of one customer, newest first, plus what a new deal can price.

    Shape::

        {"customer", "customer_name", "price_list": str|null, "today",
         "can_edit": bool,
         "deals": [{"name", "valid_from", "valid_upto",
                    "status": "active"|"upcoming"|"expired"|"cancelled",
                    "editable", "notes", "created_by",
                    "items": [{"item_group", "item_code", "label", "rate",
                               "normal_rate": num|null}]}],
         "catalog": {"categories": [{"item_group", "item_count", "normal_rate"}],
                     "items": [{"item_code", "item_name", "item_group", "normal_rate"}]}}
    """
    _ensure_pricing_read_access()
    customer = _require_customer(customer)
    price_list = _normal_price_list(customer)
    names = frappe.get_all(
        DEAL_DOCTYPE,
        filters={"customer": customer},
        order_by="valid_from desc, creation desc",
        pluck="name",
    )
    deals = [_serialize(frappe.get_doc(DEAL_DOCTYPE, n), customer, price_list) for n in names]
    # Live and upcoming deals first; history after.
    order = {"active": 0, "upcoming": 1, "expired": 2, "cancelled": 3}
    deals.sort(key=lambda d: order.get(d["status"], 9))
    out = {
        "customer": customer,
        "customer_name": frappe.db.get_value("Customer", customer, "customer_name") or customer,
        "price_list": price_list,
        "today": str(_today()),
        "can_edit": bool(_has_manager_pricing_access() and _can_access_b2b()),
        "deals": deals,
    }
    if frappe.utils.cint(include_catalog):
        out["catalog"] = _catalog(customer, price_list)
    return out


# ---------------------------------------------------------------------------
# Writes (full managers)
# ---------------------------------------------------------------------------
@frappe.whitelist(methods=["POST"])
def save_customer_deal(customer, valid_from, valid_upto, items, notes=None, deal=None):
    """Create a deal, or replace an upcoming/active one's dates and prices.

    An expired or cancelled deal is history — orders were booked against it —
    so it cannot be edited; create a new deal instead.
    """
    _ensure_full_manager_pricing_access()
    customer = _require_customer(customer)
    rows = _parse_items(items)
    start = frappe.utils.getdate(valid_from)
    end = frappe.utils.getdate(valid_upto)

    if deal:
        doc = frappe.get_doc(DEAL_DOCTYPE, deal)
        if doc.customer != customer:
            frappe.throw("This deal belongs to a different customer.")
        status = deal_status(doc.as_dict(), _today())
        if status not in ("upcoming", "active"):
            frappe.throw(f"A {status} deal cannot be edited. Create a new deal instead.")
        if status == "active" and start != frappe.utils.getdate(doc.valid_from):
            # Orders already priced from the original start; moving it would
            # rewrite which of them "were" on the deal.
            frappe.throw("A running deal keeps its start date. Change the end date or the prices.")
        if end < _today():
            frappe.throw("The end date cannot be in the past. Use End deal instead.")
        doc.items = []
    else:
        if end < _today():
            frappe.throw("The deal's end date is already in the past.")
        doc = frappe.new_doc(DEAL_DOCTYPE)
        doc.customer = customer

    doc.valid_from = start
    doc.valid_upto = end
    doc.notes = (str(notes).strip() or None) if notes is not None else doc.get("notes")
    for row in rows:
        doc.append("items", row)
    # The role gate above is the permission check, as in api/price_lists.
    doc.save(ignore_permissions=True)
    return _serialize(doc, customer, _normal_price_list(customer))


@frappe.whitelist(methods=["POST"])
def end_customer_deal(deal):
    """Stop a deal from today on.

    A deal that already ran keeps its history: its end date moves to yesterday,
    so amending an order placed during it still finds it. One that has not
    priced a single day yet (upcoming, or starting today) is cancelled outright.
    """
    _ensure_full_manager_pricing_access()
    doc = frappe.get_doc(DEAL_DOCTYPE, deal)
    today = _today()
    status = deal_status(doc.as_dict(), today)
    if status not in ("upcoming", "active"):
        frappe.throw(f"This deal is already {status}.")
    if frappe.utils.getdate(doc.valid_from) < today:
        doc.valid_upto = frappe.utils.add_days(today, -1)
    else:
        doc.disabled = 1
    # The role gate above is the permission check, as in api/price_lists.
    doc.save(ignore_permissions=True)
    return _serialize(doc, doc.customer, _normal_price_list(doc.customer))

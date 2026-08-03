"""Warehouse resolution for purchasing and item requests.

Purchases used to be created with ``update_stock=1`` and **no warehouse at all**,
which left the destination to ERPNext's own fallback chain.  In a multi-branch
setup that silently posts receipts into whichever warehouse happens to resolve
first, so goods received at one branch could increase another branch's stock.

The rule here is deliberately declarative and configurable, because "where does
this land?" is a business question that changes without a deploy:

1. an explicit warehouse on the line (carried from an item request, or chosen
   by the buyer as an override)
2. the Item's own ``Item Default.default_warehouse`` for the company — the
   standard ERPNext mechanism, already used by
   :func:`jarz_pos.api.manufacturing._get_item_default_warehouse`
3. the Item Group routes in Jarz POS Settings (Raw Material → raw store,
   Consumable → consumables store, …), most specific group first
4. ``Jarz POS Settings.default_purchase_warehouse``
5. the company's default receiving warehouse, then any leaf warehouse

Step 3 walks *up* the Item Group tree, so a route on "Raw Material" also covers
every group nested under it and a route on a child group overrides its parent.
"""
from __future__ import annotations

import frappe
from frappe import _
from typing import Any, Dict, List, Optional


def _settings():
    """Return the Jarz POS Settings doc, or None before migration."""
    try:
        from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings

        return get_jarz_settings()
    except Exception:
        return None


def _coerce(value: Any) -> str:
    return str(value or "").strip()


def warehouse_belongs_to_company(warehouse: str, company: str) -> bool:
    warehouse, company = _coerce(warehouse), _coerce(company)
    if not warehouse or not company:
        return False
    return _coerce(frappe.db.get_value("Warehouse", warehouse, "company")) == company


def _is_usable(warehouse: str, company: str) -> bool:
    """A warehouse is usable as a receipt target if it belongs to the company,
    is a leaf (stock cannot post into a group node) and is not disabled."""
    warehouse = _coerce(warehouse)
    if not warehouse:
        return False
    row = frappe.db.get_value(
        "Warehouse", warehouse, ["company", "is_group", "disabled"], as_dict=True
    )
    if not row:
        return False
    if _coerce(row.get("company")) != _coerce(company):
        return False
    return not int(row.get("is_group") or 0) and not int(row.get("disabled") or 0)


def _item_default_warehouse(item_code: str, company: str) -> Optional[str]:
    warehouse = _coerce(
        frappe.db.get_value(
            "Item Default",
            {"parent": item_code, "parenttype": "Item", "company": company},
            "default_warehouse",
        )
    )
    return warehouse if _is_usable(warehouse, company) else None


def _item_group_ancestry(item_group: str) -> List[str]:
    """Return *item_group* then each ancestor, nearest first.

    Uses the nested-set columns so one query covers the whole chain; falls back
    to walking ``parent_item_group`` if the tree bounds are missing.
    """
    item_group = _coerce(item_group)
    if not item_group:
        return []

    bounds = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"], as_dict=True)
    if bounds and bounds.get("lft") is not None and bounds.get("rgt") is not None:
        rows = frappe.get_all(
            "Item Group",
            filters={
                "lft": ["<=", bounds["lft"]],
                "rgt": [">=", bounds["rgt"]],
            },
            fields=["name", "lft"],
            order_by="lft desc",  # deepest (most specific) first
        )
        return [r["name"] for r in rows]

    chain: List[str] = []
    current = item_group
    seen = set()
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        current = _coerce(frappe.db.get_value("Item Group", current, "parent_item_group"))
    return chain


def _routed_warehouse(item_code: str, company: str) -> Optional[str]:
    settings = _settings()
    routes = list(getattr(settings, "purchase_warehouse_routes", None) or []) if settings else []
    if not routes:
        return None

    item_group = _coerce(frappe.db.get_value("Item", item_code, "item_group"))
    if not item_group:
        return None

    route_map: Dict[str, str] = {}
    for row in routes:
        group = _coerce(getattr(row, "item_group", None))
        warehouse = _coerce(getattr(row, "warehouse", None))
        if group and warehouse and group not in route_map:
            route_map[group] = warehouse

    for group in _item_group_ancestry(item_group):
        warehouse = route_map.get(group)
        if warehouse and _is_usable(warehouse, company):
            return warehouse
    return None


def _company_fallback_warehouse(company: str) -> Optional[str]:
    """Last resort: ERPNext's own global stock default, then any leaf warehouse.

    Deliberately *not* ``Company.default_in_transit_warehouse`` or
    ``default_warehouse_for_sales_return`` — both exist but mean something else
    (transfers in flight, and returned sales stock), so using either as a
    receipt target would quietly file purchases in the wrong place.
    """
    candidate = _coerce(frappe.db.get_single_value("Stock Settings", "default_warehouse"))
    if _is_usable(candidate, company):
        return candidate

    row = frappe.get_all(
        "Warehouse",
        filters={"company": company, "is_group": 0, "disabled": 0},
        fields=["name"],
        order_by="creation asc",
        limit=1,
    )
    return row[0]["name"] if row else None


def resolve_purchase_warehouse(
    item_code: str,
    company: str,
    explicit: Optional[str] = None,
) -> str:
    """Resolve the receiving warehouse for one purchased line.

    Raises rather than returning blank: a blank warehouse on an
    ``update_stock=1`` Purchase Invoice is exactly the silent mis-post this
    function exists to prevent, so the caller must not be able to ignore it.
    """
    item_code, company = _coerce(item_code), _coerce(company)
    if not company:
        frappe.throw(_("Company is required to resolve a receiving warehouse."))

    explicit = _coerce(explicit)
    if explicit:
        if not _is_usable(explicit, company):
            frappe.throw(
                _("Warehouse {0} is not a usable stock warehouse for company {1}.").format(
                    explicit, company
                )
            )
        return explicit

    for candidate in (
        _item_default_warehouse(item_code, company),
        _routed_warehouse(item_code, company),
    ):
        if candidate:
            return candidate

    settings = _settings()
    configured = _coerce(getattr(settings, "default_purchase_warehouse", None)) if settings else ""
    if configured:
        if not _is_usable(configured, company):
            frappe.throw(
                _(
                    "Jarz POS Settings → Default Purchase Warehouse is set to {0}, "
                    "which is not a usable stock warehouse for company {1}."
                ).format(configured, company)
            )
        return configured

    fallback = _company_fallback_warehouse(company)
    if fallback:
        return fallback

    frappe.throw(
        _(
            "No receiving warehouse could be resolved for item {0}. "
            "Set Jarz POS Settings → Default Purchase Warehouse."
        ).format(item_code or "?")
    )


def resolve_request_warehouse(
    item_code: str,
    company: str,
    explicit: Optional[str] = None,
) -> str:
    """Warehouse a team request targets.

    Same chain as :func:`resolve_purchase_warehouse` — a request should name the
    place the goods will actually arrive, otherwise the Material Request line and
    the Purchase Invoice line disagree and ERPNext cannot match them.
    """
    return resolve_purchase_warehouse(item_code, company, explicit)

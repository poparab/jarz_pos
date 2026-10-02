"""Time-limited special prices for one B2B customer ("deals").

A deal says: from ``valid_from`` to ``valid_upto`` (both inclusive) this customer
pays these rates — per category (``item_group``) or per flavour (``item_code``).
Outside the window nothing is stored or reverted: the deal simply stops matching
and ``invoice_creation._resolve_item_rate_with_provenance`` falls through to the
customer's normal price again. That is the whole "back to normal" mechanism, so
there is no scheduler job that could fail to run.

Precedence inside the resolver, highest first:

  1. a deal row naming this exact item;
  2. a deal row for the item's category;
  3. everything that existed before deals (customer Item Price, generic Item
     Price, category rate, client fallback).

A deal deliberately beats a customer-scoped Item Price: the Item Price is the
standing negotiated rate, the deal is the temporary exception on top of it.

The date a deal is judged on is the ORDER date (today for a new order). An
amendment is judged on its source order's date — see
``invoice_creation._deal_pricing_date`` — so editing an order after the deal
ended does not silently re-price it to normal, and an old order cannot pick up
a deal that started after it was placed.
"""

from __future__ import annotations

import frappe

DEAL_DOCTYPE = "Jarz Customer Deal"
DEAL_ITEM_DOCTYPE = "Jarz Customer Deal Item"


def _as_date(value=None):
    if value in (None, ""):
        return frappe.utils.getdate(frappe.utils.today())
    return frappe.utils.getdate(value)


def deal_status(deal: dict, on_date=None) -> str:
    """``cancelled`` | ``upcoming`` | ``active`` | ``expired`` for one deal row."""
    if int(deal.get("disabled") or 0):
        return "cancelled"
    today = _as_date(on_date)
    if _as_date(deal.get("valid_from")) > today:
        return "upcoming"
    if _as_date(deal.get("valid_upto")) < today:
        return "expired"
    return "active"


def _active_deal_rows(customer: str, on_date) -> list[dict]:
    """Every deal line live for ``customer`` on ``on_date``, newest deal first.

    Returns ``[]`` when the deal tables do not exist yet (code deployed, migrate
    not run), so pricing behaves exactly as before deals existed.
    """
    try:
        return frappe.db.sql(
            """
            SELECT d.name AS deal, d.valid_from, i.item_code, i.item_group, i.rate
            FROM `tabJarz Customer Deal` d
            JOIN `tabJarz Customer Deal Item` i
              ON i.parent = d.name AND i.parenttype = 'Jarz Customer Deal'
            WHERE d.customer = %(customer)s
              AND IFNULL(d.disabled, 0) = 0
              AND d.valid_from <= %(on_date)s
              AND d.valid_upto >= %(on_date)s
            ORDER BY d.valid_from DESC, d.creation DESC, i.idx ASC
            """,
            {"customer": customer, "on_date": on_date},
            as_dict=True,
        )
    except Exception as exc:
        if frappe.db.is_table_missing(exc):
            return []
        raise


def find_deal_rate(customer, item_code, on_date=None, item_group=None):
    """``(rate, deal_name)`` when an active deal prices this item, else ``None``."""
    customer = str(customer or "").strip()
    item_code = str(item_code or "").strip()
    if not customer or not item_code:
        return None
    rows = _active_deal_rows(customer, _as_date(on_date))
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    for row in rows:
        if row.get("item_code") == item_code and row.get("rate") is not None:
            return float(row["rate"]), row["deal"]
    if item_group is None:
        item_group = frappe.db.get_value("Item", item_code, "item_group")
    if item_group:
        for row in rows:
            if (
                not row.get("item_code")
                and row.get("item_group") == item_group
                and row.get("rate") is not None
            ):
                return float(row["rate"]), row["deal"]
    return None


def deal_has_orders(deal_name: str) -> bool:
    """True once a submitted invoice was priced from this deal.

    ``create_pos_invoice`` stamps ``[CUSTOMER DEAL] <names>`` into the
    invoice's audit markers whenever a line took a deal price, so this is the
    exact "orders were booked at it" test. Until it is true a deal -- even one
    running today -- may still be corrected, cancelled or deleted; after it, it
    is history. When the answer cannot be read, assume it HAS orders: locking a
    deal is recoverable, re-pricing booked orders is not.
    """
    name = str(deal_name or "").strip()
    if not name:
        return False
    try:
        return bool(
            frappe.db.exists(
                "Sales Invoice",
                {
                    "docstatus": 1,
                    "custom_pos_audit_markers": ["like", f"%[CUSTOMER DEAL]%{name}%"],
                },
            )
        )
    except Exception:
        return True


def row_target(row) -> tuple[str, str] | None:
    """``("item", code)`` / ``("group", name)`` for a deal line, ``None`` if empty."""
    code = str((row.get("item_code") if isinstance(row, dict) else row.item_code) or "").strip()
    group = str((row.get("item_group") if isinstance(row, dict) else row.item_group) or "").strip()
    if code:
        return ("item", code)
    if group:
        return ("group", group)
    return None


def find_conflicting_deal(customer, valid_from, valid_upto, targets, exclude=None):
    """Name of another live deal for ``customer`` whose window overlaps this one AND
    prices one of the same ``targets``; ``None`` when there is no conflict.

    Two overlapping deals on DIFFERENT targets are fine (Large jars in October,
    Medium jars for one week of it). Two on the same target would leave the
    resolver to pick one silently, so it is refused at save time instead.
    """
    if not targets:
        return None
    filters = {
        "customer": customer,
        "disabled": 0,
        "valid_from": ["<=", valid_upto],
        "valid_upto": [">=", valid_from],
    }
    if exclude:
        filters["name"] = ["!=", exclude]
    others = frappe.get_all(DEAL_DOCTYPE, filters=filters, pluck="name")
    if not others:
        return None
    lines = frappe.get_all(
        DEAL_ITEM_DOCTYPE,
        filters={"parent": ["in", others], "parenttype": DEAL_DOCTYPE},
        fields=["parent", "item_code", "item_group"],
    )
    for line in lines:
        if row_target(line) in targets:
            return line["parent"]
    return None

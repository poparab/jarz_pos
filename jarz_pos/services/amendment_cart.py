"""Rebuild a POS cart payload from an already-submitted Sales Invoice.

The amendment flow normally receives ``cart_json`` from the Flutter client, which
reconstructs the cart from the invoice rows.  That reconstruction is lossy for
bundles: the client can only guess which Jarz Bundle Item Group row a child came
from by scanning group membership, so a bundle that lists the *same* item group
twice (e.g. ``Medium x8`` + ``Medium x2``) collapses every child into the first
row and the backend rejects it with
``expected 8 selection(s) from 'Medium', received 10``.

The invoice itself already stores the answer — ``bundle_group_key`` on every
bundle-child row points at the exact Jarz Bundle Item Group row.  When an
amendment only changes invoice-level data (delivery income, address, slot) the
cart does not need to round-trip through the client at all: this module rebuilds
it verbatim from the persisted rows, which is both unambiguous and immune to
catalog drift.

Two things stop that being the whole story, because most bundle invoices are now
created by WooCommerce rather than by this app:

* ``bundle_code`` on the parent row can point at a Jarz Bundle that has since
  been deleted, so it is verified against the database and re-derived from the
  parent ERPNext item when it is stale.
* ``bundle_group_key`` is missing on the great majority of bundle-child rows.
  It can only be *derived* by asking which group of the bundle contains the
  item, and that answer is ambiguous exactly when the bundle repeats an item
  group.  So a child with no stored key is keyed by its group NAME instead and
  the split across the repeated rows is left to
  :class:`jarz_pos.services.bundle_processing.BundleProcessor`, which is the one
  place that knows each row's required quantity.
"""

from __future__ import annotations

from typing import Any, Dict, List

import frappe
from frappe import _
from frappe.utils import flt

from jarz_pos.utils.invoice_utils import (
    _derive_bundle_code_from_parent_item,
    _derive_bundle_group_metadata,
)


def _flag(row: Any, fieldname: str) -> bool:
    """Return a boolean for a custom flag field that may be None on legacy rows."""
    value = getattr(row, fieldname, None)
    if value in (None, ""):
        return False
    return bool(int(value)) if isinstance(value, (int, float)) else bool(value)


def _text(row: Any, fieldname: str) -> str:
    return str(getattr(row, fieldname, None) or "").strip()


def _is_bundle_parent_row(row: Any) -> bool:
    return _flag(row, "is_bundle_parent") or bool(_text(row, "bundle_code"))


def _is_bundle_child_row(row: Any) -> bool:
    return _flag(row, "is_bundle_child") or bool(_text(row, "parent_bundle"))


def _bundle_exists(bundle_code: str, cache: Dict[str, bool]) -> bool:
    """Return whether ``bundle_code`` still names a live Jarz Bundle record."""
    if bundle_code not in cache:
        try:
            cache[bundle_code] = bool(frappe.db.exists("Jarz Bundle", bundle_code))
        except Exception:
            # A lookup failure must not turn a rebuildable invoice into an error:
            # trust the stored code, which is the pre-existing behaviour.
            cache[bundle_code] = True
    return cache[bundle_code]


def _resolve_bundle_code(
    row: Any,
    bundle_code_cache: Dict[str, str],
    bundle_exists_cache: Dict[str, bool],
) -> str:
    """Return the Jarz Bundle this parent row should be rebuilt against.

    The stored ``bundle_code`` is preferred but only when the record still
    exists.  Woo-created invoices routinely carry the id of a bundle that was
    later deleted and recreated; trusting it blindly makes every group lookup
    return nothing and the rebuild fails with "has no bundle group recorded"
    even though the parent item still maps to a perfectly good bundle.
    """
    stored_code = _text(row, "bundle_code")
    if stored_code and _bundle_exists(stored_code, bundle_exists_cache):
        return stored_code
    return _derive_bundle_code_from_parent_item(row.item_code, bundle_code_cache)


def build_amendment_cart_from_invoice(invoice: Any) -> List[Dict[str, Any]]:
    """Return the POS cart payload that reproduces ``invoice`` line-for-line.

    Bundle parents become a single ``is_bundle`` cart row whose ``selected_items``
    are keyed by the persisted ``bundle_group_key`` — the same key
    :func:`jarz_pos.services.bundle_processing.BundleProcessor._aggregate_selected_items`
    matches on — so bundles that repeat an item group survive the round trip.
    Rows that never stored a key (nearly every Woo-created invoice) fall back to
    the group name, which the same method splits across the repeated rows.

    Raises a ValidationError when the invoice cannot be expressed as a cart
    (missing bundle code, orphaned children, fractional bundle quantities) rather
    than silently emitting a cart that would rebuild the invoice incorrectly.
    """
    rows = list(getattr(invoice, "items", None) or [])
    if not rows:
        frappe.throw(_("Invoice {0} has no items to rebuild.").format(getattr(invoice, "name", "")))

    bundle_code_cache: Dict[str, str] = {}
    bundle_exists_cache: Dict[str, bool] = {}
    group_metadata_cache: Dict[str, Dict[str, Dict[str, str]]] = {}

    # Resolve each parent row's bundle code up front so children can be attached.
    parent_bundle_codes: Dict[int, str] = {}
    # Children point at whatever code the parent row stored. When that code is
    # stale it is replaced above, so keep the translation to re-attach them.
    resolved_by_stored_code: Dict[str, str] = {}
    for index, row in enumerate(rows):
        if _is_bundle_child_row(row) or not _is_bundle_parent_row(row):
            continue
        bundle_code = _resolve_bundle_code(row, bundle_code_cache, bundle_exists_cache)
        if not bundle_code:
            frappe.throw(
                _(
                    "Bundle row '{0}' on invoice {1} has no linked Jarz Bundle, "
                    "so the order cannot be rebuilt automatically."
                ).format(row.item_code, getattr(invoice, "name", ""))
            )
        parent_bundle_codes[index] = bundle_code
        stored_code = _text(row, "bundle_code")
        if stored_code:
            resolved_by_stored_code[stored_code] = bundle_code

    resolved_codes = set(parent_bundle_codes.values())
    children_by_bundle: Dict[str, List[Any]] = {}
    orphan_children: List[Any] = []
    for row in rows:
        if not _is_bundle_child_row(row):
            continue
        parent_bundle = _text(row, "parent_bundle")
        parent_bundle = resolved_by_stored_code.get(parent_bundle, parent_bundle)
        # A child pointing at a bundle no parent row resolved to is treated as an
        # orphan rather than quietly dropped from the rebuilt cart — dropping it
        # would produce a replacement invoice missing a line the customer paid for.
        if parent_bundle and parent_bundle in resolved_codes:
            children_by_bundle.setdefault(parent_bundle, []).append(row)
        else:
            orphan_children.append(row)

    # Legacy rows sometimes lost `parent_bundle`. That is only recoverable when the
    # invoice holds exactly one bundle — anything else would be a guess.
    if orphan_children:
        if len(parent_bundle_codes) != 1:
            frappe.throw(
                _(
                    "Invoice {0} has bundle child rows with no parent bundle recorded, "
                    "so the order cannot be rebuilt automatically."
                ).format(getattr(invoice, "name", ""))
            )
        only_bundle_code = next(iter(parent_bundle_codes.values()))
        children_by_bundle.setdefault(only_bundle_code, []).extend(orphan_children)

    cart: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        if _is_bundle_child_row(row):
            continue  # emitted as part of its bundle parent

        if index in parent_bundle_codes:
            cart.append(
                _build_bundle_cart_row(
                    invoice=invoice,
                    parent_row=row,
                    bundle_code=parent_bundle_codes[index],
                    children=children_by_bundle.get(parent_bundle_codes[index], []),
                    group_metadata_cache=group_metadata_cache,
                )
            )
            continue

        item_code = str(row.item_code or "").strip()
        if not item_code:
            continue
        unit_rate = flt(row.price_list_rate) or flt(row.rate)
        plain_row: Dict[str, Any] = {
            "item_code": item_code,
            "qty": flt(row.qty),
            "rate": unit_rate,
        }
        # Preserve a manual line discount; the catalog rate is re-resolved by the
        # invoice engine, so only the discount has to be carried across.
        discount_percentage = flt(getattr(row, "discount_percentage", None))
        if discount_percentage > 0:
            plain_row["discount_percentage"] = discount_percentage
        cart.append(plain_row)

    if not cart:
        frappe.throw(
            _("Invoice {0} produced an empty cart when rebuilt.").format(getattr(invoice, "name", ""))
        )
    return cart


def _build_bundle_cart_row(
    *,
    invoice: Any,
    parent_row: Any,
    bundle_code: str,
    children: List[Any],
    group_metadata_cache: Dict[str, Dict[str, Dict[str, str]]],
) -> Dict[str, Any]:
    """Return one ``is_bundle`` cart row rebuilt from its persisted invoice rows."""
    invoice_name = getattr(invoice, "name", "")
    if not children:
        frappe.throw(
            _("Bundle '{0}' on invoice {1} has no child rows, so it cannot be rebuilt.").format(
                parent_row.item_code, invoice_name
            )
        )

    bundle_qty = int(flt(parent_row.qty) or 1)
    if bundle_qty <= 0:
        bundle_qty = 1

    selected_items: Dict[str, List[Dict[str, Any]]] = {}
    for child in children:
        group_key = _text(child, "bundle_group_key")
        group_name = _text(child, "bundle_group_name")
        if group_key:
            # The invoice recorded the exact Jarz Bundle Item Group row: use it.
            selection_key = group_key
        else:
            derived_key, derived_name = _derive_bundle_group_metadata(
                bundle_code, child.item_code, group_metadata_cache
            )
            group_name = group_name or derived_name
            # Nothing was recorded, so the row can only be inferred from group
            # membership — and that inference maps an item to ONE group row, so on
            # a bundle repeating an item group it always answers with the last of
            # them (4 selections would be posted against a row needing 1). Key by
            # the group NAME instead and let BundleProcessor, which knows each
            # row's required quantity, split the list across the rows.
            selection_key = group_name or derived_key
        if not selection_key:
            frappe.throw(
                _(
                    "Bundle child '{0}' on invoice {1} has no bundle group recorded, "
                    "so the order cannot be rebuilt automatically."
                ).format(child.item_code, invoice_name)
            )

        total_qty = flt(child.qty)
        # Child quantities are stored as (per-bundle qty x bundle qty). A remainder
        # means the invoice was hand-edited and the split is not recoverable.
        if total_qty <= 0 or (total_qty % bundle_qty) != 0:
            frappe.throw(
                _(
                    "Bundle child '{0}' on invoice {1} has quantity {2}, which is not a "
                    "multiple of the bundle quantity {3}."
                ).format(child.item_code, invoice_name, total_qty, bundle_qty)
            )
        per_bundle_qty = int(total_qty // bundle_qty)

        selected_items.setdefault(selection_key, []).append(
            {
                "id": child.item_code,
                "item_code": child.item_code,
                "name": child.item_code,
                "selected_quantity": per_bundle_qty,
                # Reuse the stored unit price so the child discount split reproduces
                # the source invoice exactly instead of drifting with the catalog.
                "price": flt(child.price_list_rate) or flt(child.rate),
            }
        )

    bundle_price = flt(parent_row.price_list_rate) or flt(parent_row.rate)
    if bundle_price <= 0:
        frappe.throw(
            _("Bundle '{0}' on invoice {1} has no price recorded, so it cannot be rebuilt.").format(
                parent_row.item_code, invoice_name
            )
        )

    return {
        "item_code": bundle_code,
        "qty": bundle_qty,
        "rate": bundle_price,
        "price_list_rate": bundle_price,
        "is_bundle": True,
        "selected_items": selected_items,
    }

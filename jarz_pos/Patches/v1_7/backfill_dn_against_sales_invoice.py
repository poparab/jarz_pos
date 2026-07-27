"""Backfill ``Delivery Note Item.against_sales_invoice`` / ``si_detail`` on legacy DNs.

Until 2026-07-20 the auto-created Delivery Note recorded its source invoice only
in the DN's ``remarks`` text. ERPNext v16 removed that column, so the link was
lost and ``_find_existing_delivery_note_for_invoice`` — which now detects solely
via ``Delivery Note Item.against_sales_invoice`` — cannot see those DNs at all.

That is invisible for normal operations but fatal for the return workflow: a
return has to reverse stock against the *original* Delivery Note, and it cannot
locate one for any pre-fix order. This patch restores the link.

Matching is deliberately conservative. A DN is only linked when exactly ONE
submitted, non-return Sales Invoice for the same customer, within a +/- 2 day
window, has an identical multiset of (item_code, qty). Anything ambiguous is
counted and skipped rather than guessed — mislinking a Delivery Note to the
wrong invoice would let a return reverse stock against another customer's order,
which is far worse than leaving a DN unlinked.

Run report-only first::

    bench --site <site> execute \
        jarz_pos.Patches.v1_7.backfill_dn_against_sales_invoice.execute \
        --kwargs "{'dry_run': True}"

Idempotent: rows that already carry ``against_sales_invoice`` are skipped.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

import frappe

#: Days either side of the DN posting date to consider when matching invoices.
_DATE_WINDOW_DAYS = 2

#: Safety ceiling so a first run on a large site cannot hold the DB for hours.
_MAX_DELIVERY_NOTES = 5000


def _item_signature(rows: List[Dict[str, Any]]) -> Counter:
    """Multiset of (item_code, rounded qty) — order-independent identity."""
    return Counter(
        (str(row.get("item_code") or ""), round(float(row.get("qty") or 0), 3))
        for row in rows
        if row.get("item_code")
    )


def _candidate_invoices(dn: Dict[str, Any]) -> List[str]:
    """Submitted, non-return invoices for the same customer near the DN's date."""
    return frappe.get_all(
        "Sales Invoice",
        filters={
            "customer": dn.get("customer"),
            "docstatus": 1,
            "is_return": 0,
            "posting_date": [
                "between",
                [
                    frappe.utils.add_days(dn.get("posting_date"), -_DATE_WINDOW_DAYS),
                    frappe.utils.add_days(dn.get("posting_date"), _DATE_WINDOW_DAYS),
                ],
            ],
        },
        pluck="name",
        limit_page_length=50,
    ) or []


def _resolve_source_invoice(dn: Dict[str, Any], dn_items: List[Dict[str, Any]]) -> Optional[str]:
    """Return the single invoice whose items match *dn* exactly, else None."""
    signature = _item_signature(dn_items)
    if not signature:
        return None

    matches: List[str] = []
    for invoice_name in _candidate_invoices(dn):
        si_items = frappe.get_all(
            "Sales Invoice Item",
            filters={"parent": invoice_name, "parenttype": "Sales Invoice"},
            fields=["name", "item_code", "qty"],
            limit_page_length=200,
        ) or []
        if _item_signature(si_items) == signature:
            matches.append(invoice_name)
            if len(matches) > 1:
                # Ambiguous — two invoices are indistinguishable by this rule.
                return None

    return matches[0] if len(matches) == 1 else None


def _si_row_for_item(invoice_name: str, item_code: str, used: set) -> Optional[str]:
    """Pick an unused Sales Invoice Item row name for *item_code*.

    v16 rejects a Delivery Note Item that sets ``against_sales_invoice`` without
    the paired ``si_detail``, so both must be stamped together.
    """
    rows = frappe.get_all(
        "Sales Invoice Item",
        filters={"parent": invoice_name, "item_code": item_code, "parenttype": "Sales Invoice"},
        pluck="name",
        limit_page_length=50,
    ) or []
    for row_name in rows:
        if row_name not in used:
            used.add(row_name)
            return row_name
    return None


def execute(dry_run: bool = False) -> Dict[str, Any]:
    """Link legacy Delivery Notes back to their source Sales Invoice."""
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in {"", "0", "false", "no"}

    if not frappe.db.has_column("Delivery Note Item", "against_sales_invoice"):
        return {"skipped": "column_missing"}

    unlinked = frappe.db.sql(
        """
        SELECT DISTINCT dn.name, dn.customer, dn.posting_date
        FROM `tabDelivery Note` dn
        JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
        WHERE dn.docstatus = 1
          AND IFNULL(dn.is_return, 0) = 0
          AND IFNULL(dni.against_sales_invoice, '') = ''
        ORDER BY dn.posting_date DESC
        LIMIT %s
        """,
        (_MAX_DELIVERY_NOTES,),
        as_dict=True,
    ) or []

    stats = {
        "dry_run": bool(dry_run),
        "scanned": len(unlinked),
        "linked": 0,
        "ambiguous": 0,
        "no_match": 0,
        "row_gap": 0,
        "samples": [],
    }

    for dn in unlinked:
        dn_items = frappe.get_all(
            "Delivery Note Item",
            filters={"parent": dn["name"], "parenttype": "Delivery Note"},
            fields=["name", "item_code", "qty", "against_sales_invoice"],
            limit_page_length=200,
        ) or []
        if not dn_items:
            stats["no_match"] += 1
            continue

        invoice_name = _resolve_source_invoice(dn, dn_items)
        if not invoice_name:
            # Distinguish "two equally good matches" from "nothing matched" so a
            # report-only run tells us which rule to loosen, if any.
            if _candidate_invoices(dn):
                stats["ambiguous"] += 1
            else:
                stats["no_match"] += 1
            continue

        used_rows: set = set()
        pending: List[tuple] = []
        complete = True
        for row in dn_items:
            si_row = _si_row_for_item(invoice_name, row.get("item_code"), used_rows)
            if not si_row:
                complete = False
                break
            pending.append((row.get("name"), si_row))

        if not complete:
            # Never stamp a partial link: a DN item with against_sales_invoice
            # but no si_detail is exactly what v16 refuses to save.
            stats["row_gap"] += 1
            continue

        if len(stats["samples"]) < 20:
            stats["samples"].append({"delivery_note": dn["name"], "sales_invoice": invoice_name})

        if not dry_run:
            for dni_name, si_row in pending:
                frappe.db.set_value(
                    "Delivery Note Item",
                    dni_name,
                    {"against_sales_invoice": invoice_name, "si_detail": si_row},
                    update_modified=False,
                )
        stats["linked"] += 1

    if not dry_run:
        frappe.db.commit()

    frappe.logger("jarz_pos.backfill").info(f"v1_7 DN backfill: {stats}")
    print(
        f"v1_7 DN backfill ({'DRY RUN' if dry_run else 'APPLIED'}): "
        f"scanned={stats['scanned']} linked={stats['linked']} "
        f"ambiguous={stats['ambiguous']} no_match={stats['no_match']} "
        f"row_gap={stats['row_gap']}"
    )
    return stats

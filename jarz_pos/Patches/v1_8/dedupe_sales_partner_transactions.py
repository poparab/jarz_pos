"""Remove duplicate Sales Partner Transactions minted for one invoice.

A paid-online sales-partner order got one transaction at invoice creation
(``<invoice>::sales_partner_paid``) and a second one when the card was dragged
Out for Delivery (``SPTRN::<invoice>``): each path checked only for its own
token. ``settle_sales_partner`` sums every Unsettled row, so the commission was
posted twice for those orders. The code now checks per invoice; this patch
clears the rows already minted.

Rules, in order of caution:

* Groups with more than one row keep the OLDEST row.
* An extra row that is still ``Unsettled`` is deleted — no fee has been posted
  for it, so nothing in the ledger refers to it.
* An extra row that is already ``Settled`` is NEVER touched: its fee journal
  entry is in the books and reversing money is not a migration's decision. It
  is reported in an Error Log titled "Sales Partner Transaction: settled
  duplicate needs manual reversal" for an accountant to handle.

Idempotent: a second run finds no duplicates and does nothing.
"""
from __future__ import annotations

import frappe


def execute():
    if not frappe.db.table_exists("Sales Partner Transactions"):
        return

    groups = frappe.db.sql(
        """
        select reference_invoice
        from `tabSales Partner Transactions`
        where ifnull(reference_invoice, '') != ''
        group by reference_invoice
        having count(*) > 1
        """,
        as_dict=True,
    )
    removed: list[str] = []
    flagged: list[dict] = []
    for group in groups:
        invoice = group.get("reference_invoice")
        rows = frappe.get_all(
            "Sales Partner Transactions",
            filters={"reference_invoice": invoice},
            fields=["name", "status", "partner_fees", "idempotency_token", "journal_entry", "creation"],
            order_by="creation asc",
        )
        keep, extras = rows[0], rows[1:]
        for row in extras:
            if str(row.get("status") or "").strip() == "Unsettled" and not row.get("journal_entry"):
                frappe.delete_doc(
                    "Sales Partner Transactions", row["name"],
                    ignore_permissions=True, force=True,
                )
                removed.append(f"{invoice}:{row['name']}")
            else:
                flagged.append({
                    "invoice": invoice,
                    "kept": keep["name"],
                    "settled_duplicate": row["name"],
                    "partner_fees": row.get("partner_fees"),
                    "journal_entry": row.get("journal_entry"),
                })

    if flagged:
        lines = [
            f"{f['invoice']}: duplicate {f['settled_duplicate']} (kept {f['kept']}), "
            f"fee {f['partner_fees']}, journal entry {f['journal_entry'] or '-'}"
            for f in flagged
        ]
        frappe.log_error(
            title="Sales Partner Transaction: settled duplicate needs manual reversal",
            message="These invoices carry a SECOND, already-settled partner transaction. "
            "The fee was posted twice; reverse one fee journal by hand.\n\n" + "\n".join(lines),
        )
    frappe.db.commit()
    print(f"dedupe_sales_partner_transactions: removed {len(removed)} unsettled duplicate(s), "
          f"flagged {len(flagged)} settled duplicate(s) for manual reversal")

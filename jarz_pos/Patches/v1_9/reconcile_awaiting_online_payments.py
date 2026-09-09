"""Bring the existing ``Awaiting Payment`` backlog in step with the ledger.

The awaiting flag is set at dispatch and, until now, cleared by exactly one
function. Every other way an order could be paid left it behind, and nothing
ever filed the ``POS Payment Receipt`` row that makes an awaiting order visible
on the receipts list at all. Production on 2026-09-09 held 29 orders in that
state, the oldest since 2026-08-26.

This runs the same reconciler the hourly job now runs, so the backlog is healed
by exactly the code that keeps it healed:

* **Paid already** — flag flipped to Payment Confirmed, any receipt still asking
  for a screenshot retired. No money moves; the Payment Entry already exists.
* **Still unpaid** — the pending receipt is filed, so the order finally appears
  on the receipts list and somebody can act on it.

Deliberately NOT healed: an order whose receipt a manager already CONFIRMED
while the invoice stayed unpaid. Posting those is a real 2,460 EGP of customer
receivable and it is the owner's call, not a migration's — they are printed at
the end so the decision is in front of somebody. To book one, use the InstaPay
reconciliation screen: the receipts list hides Confirm on an already-confirmed
row, while the reconciliation sheet re-submits the existing receipt through
``confirm_online_payment``, which posts the payment entry.

Idempotent: re-running finds nothing left to do -- and now says so. The
reconciler reports only what it changed, so a second pass lists no receipts
under "filed" instead of re-listing every unpaid order it merely looked at.
"""

import frappe


def execute():
    from jarz_pos.services.delivery_handling import reconcile_payment_confirmation

    rows = frappe.get_all(
        "Sales Invoice",
        filters={"docstatus": 1, "custom_payment_confirmation_status": "Awaiting Payment"},
        fields=["name", "outstanding_amount", "grand_total", "custom_kanban_profile"],
        limit_page_length=0,
    )
    if not rows:
        print("reconcile_awaiting_online_payments: nothing awaiting payment")
        return

    confirmed_from_ledger = []
    receipts_filed = []
    amounts_resynced = []
    needs_a_decision = []

    for row in rows:
        name = row["name"]

        # A confirmed receipt on an unpaid invoice is the one case a migration
        # must not touch: somebody already looked at proof of transfer, so the
        # question is whether to book the money, and that is not ours to answer.
        if float(row.get("outstanding_amount") or 0) > 0.01:
            confirmed_receipts = frappe.get_all(
                "POS Payment Receipt",
                filters={"sales_invoice": name, "status": "Confirmed"},
                pluck="name",
            )
            if confirmed_receipts:
                needs_a_decision.append((name, row.get("outstanding_amount"), confirmed_receipts))
                continue

        try:
            result = reconcile_payment_confirmation(name)
        except Exception:
            frappe.logger().error(
                f"reconcile_awaiting_online_payments: failed on {name}"
            )
            continue

        if not result:
            continue
        if result.get("action") == "confirmed_from_ledger":
            confirmed_from_ledger.append((name, result.get("payment_entry")))
        elif result.get("action") == "receipt_filed":
            receipts_filed.append((name, result.get("receipt")))
        elif result.get("action") == "receipt_amount_synced":
            amounts_resynced.append((name, result.get("receipt")))

    frappe.db.commit()

    print(
        f"reconcile_awaiting_online_payments: {len(rows)} awaiting; "
        f"{len(confirmed_from_ledger)} already paid and now confirmed; "
        f"{len(receipts_filed)} pending receipts filed; "
        f"{len(amounts_resynced)} receipt amounts re-synced; "
        f"{len(needs_a_decision)} left for a human."
    )
    for name, pe in confirmed_from_ledger:
        print(f"  confirmed from ledger: {name} (payment entry {pe})")
    for name, receipt in receipts_filed:
        print(f"  receipt filed: {name} -> {receipt}")
    for name, receipt in amounts_resynced:
        print(f"  receipt amount re-synced: {name} -> {receipt}")
    if needs_a_decision:
        print(
            "  NOT touched - a manager confirmed the transfer but no payment was "
            "ever booked. Book it from the InstaPay reconciliation screen, or "
            "reverse it if the money never arrived:"
        )
        for name, outstanding, receipts in needs_a_decision:
            print(f"    {name}: {outstanding} outstanding, receipts {receipts}")

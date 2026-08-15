# Backlog migration runbook — bringing ERPNext in line with WooCommerce

The POS ops pipeline stopped being driven around **2026-06-06**. Orders kept
arriving from WooCommerce, kept being invoiced, kept being delivered in real
life — and then sat frozen in an early ERPNext state with the cash never booked.

This runbook closes that gap. Code: `jarz_pos/scripts/backlog_migration.py`.

---

## The one thing that can hurt a customer

These apps contain **no customer notification path at all** — no SMS, no
WhatsApp client, no mail to a customer address. There is exactly one door from
an ERPNext state change to a real person:

```
Sales Invoice change
  └─ jarz_woocommerce_integration doc_events (on_submit / on_update_after_submit / on_cancel)
      └─ outbound_sync.enqueue_invoice_sync
          └─ [GATE: WooCommerce Settings.enable_outbound_orders]
              └─ PUT https://orderjarz.com/wp-json/wc/v3/orders/{id}
                  └────► WORDPRESS — emails, n8n → WhatsApp, AutomateWoo
```

**WooCommerce emails on status *transitions*.** Every order in the backlog is
already `completed` in Woo. Walking it through the real state machine would push
`out-for-delivery` and then `completed` again — firing the completed-order email
a second time at a customer whose order arrived months ago. That is the failure
mode this whole design exists to prevent.

Three independent things keep the door shut:

1. **The settings switch.** Every `enqueue_*` entry point checks
   `enable_outbound_orders` **first**, before it writes an outbox row — so
   nothing is left queued for a worker to send later either. The script
   *refuses to start* unless all four switches are `0`.
2. **`frappe.flags.ignore_woo_outbound`**, set for the whole run. This is the
   only guard that covers the Payment Entry hook:
   `enqueue_linked_invoice_sync_for_payment_entry` re-enters
   `enqueue_invoice_sync` with an invoice **name string**, so flags on the
   Payment Entry document are never consulted.
3. **`frappe.db.set_value` for the state change**, which fires no `doc_events`
   at all. Submitting a Payment Entry is the only hook-firing operation.

### Two traps found while building this

- **`_collect_invoice_states` reads BOTH state fields** and matches `cancelled`
  *before* `delivered`. An invoice left with a legacy `sales_invoice_state =
  Cancelled` would push **`cancelled`** to Woo the moment outbound came back on
  — a cancellation email for an order the customer already received. Those are
  held back in `MANUAL_legacy_cancelled`, and the script writes *both* state
  fields so it never creates this situation itself.
- **An unmapped payment method is never guessed.** `kashier_*`, `wallet` and
  blank get no account, so no payment *and no state change* — a Delivered column
  hiding an unpaid order is worse than a stale one.

---

## Decisions already taken

| Decision | Value |
|---|---|
| Scope | Orders stuck in a middle state (not Delivered / Cancelled / Returned) |
| Depth | Book the cash + advance the state |
| Delivery Notes / stock | **None.** See below |
| Posting date | **Today.** No backdating, no valuation reposting, no reopening closed months |
| `custom_delivered_at` | The real Woo `date_completed` — a data field, drives no posting |

**Why no Delivery Notes.** `Manufacture` stock entries stopped in May 2026, so
finished goods were never booked *in* either. Deducting ~6,263 units now would
drive finished-goods bins negative against single-digit quantities;
`allow_negative_stock = 0` would block it anyway, and fake negative stock is not
more true than today's fiction. Stock is corrected separately by a physical
count plus one Stock Reconciliation.

Skipping the DN also removes the whole Out-for-Delivery machinery from the blast
radius: no consumable deduction, no courier record, no fake courier liability,
no tracking token minted for a months-old order.

---

## Production projection

Computed offline from the read-only production invoice extract crossed with a
complete live Woo snapshot (11,792 orders, validated against the store's own
per-status counts).

| Bucket | Invoices | Outstanding | Action |
|---|---|---|---|
| `B1_pay_and_state` | **1,070** | **657,213 EGP** | Book cash + set Delivered |
| `B3_state_only` | 236 | 0 | Set Delivered only |
| `B4_cancel_in_erp` | 47 | 24,435 EGP | Cancel in ERPNext |
| `B1b_pay_only` | 120 | 68,025 EGP | **Decision needed** — already Delivered, so outside the agreed scope |
| `B5_in_flight_SKIP` | 32 | 22,265 EGP | **Do not touch** — live orders |
| `MANUAL_duplicate_live` | 3 | 1,920 EGP | Human — >1 live invoice on one Woo order |
| `MANUAL_erp_cancelled_woo_completed` | 2 | 1,138 EGP | Human — no live invoice to migrate |
| `MANUAL_unmapped_payment` | 3 | 1,405 EGP | Human — `kashier_card` with a balance |

B1 payment split: **859 cod → `Cash - J`**, **211 instapay → `Bank Account - J`**.

Cancel-and-reissue resolves automatically: one live invoice among cancelled
siblings is unambiguous and flows into its normal bucket. Only genuinely
ambiguous cases (more than one live invoice) go to a human — 3 of them.

---

## Staging rehearsal — completed 2026-08-15

Staging ran the identical commit. It was found **armed and firing** at
`demo.orderjarz.com` (`enable_outbound_orders = 1`, shadow mode off, worker on)
and was hardened first.

| Check | Result |
|---|---|
| B1 processed | 87 / 87, 0 failed |
| B3 processed | 8 / 8, 0 failed |
| B4 cancel | 13 cancelled, 11 correctly refused (dispatched / has DN), 0 failed |
| Payment Entries | 73 × `Cash - J` (27,276) + 14 × `Bank Account - J` (4,738) = **32,014 EGP**, matching the bucket total exactly |
| Idempotency | Re-running the same batch → **5 skipped, 0 double payments** |
| Rollback integrity | A PE cancelled just before a failing invoice cancel came back **submitted**, invoice still Paid, outstanding 0 |
| **Outbound log** | **109 lines before, 109 lines after — zero new** |
| **Email Queue (24h)** | **0** |
| **Outbound sync events** | **none** |
| `Recieved` backlog | 111 → 11 |

---

## Production procedure

### Pre-flight
1. **Full database dump**, taken by the owner. ERPNext cannot delete a submitted
   document — rollback means cancelling everything the script created, so the
   dump is the only true undo.
2. Deploy: `.\scripts\deploy_backend.ps1 -Environment production`
3. Upload the Woo snapshot to `sites/frontend/private/files/woo_snapshot.csv`
   (owned by uid 1000).
4. Record the baseline — you will compare against these afterwards:
   ```bash
   docker exec erp-backend-1 sh -c 'wc -l < /home/frappe/frappe-bench/sites/frontend/logs/jarz_woocommerce.outbound.log'
   ```
   plus the WooCommerce per-status counts.

### Close the door
```bash
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.harden_outbound --kwargs "{'confirm': 'DISABLE-OUTBOUND'}"
```
This turns off `enable_outbound_orders`, `enable_outbound_customers`,
`enable_inbound_orders` and `enable_outbound_tracking_url`.

> Inbound is disabled too, so the 2-minute poller cannot race the script.
> `order_reconcile_lookback_minutes = 1440` makes that gap recoverable on
> re-enable.

### Run
```bash
# 1. classify
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.classify

# 2. dry run — builds and validates every Payment Entry, writes nothing
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.run --kwargs "{'bucket': 'B1_pay_and_state', 'dry_run': True}"

# 3. first live batch, then STOP and read the audit CSV
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.run --kwargs "{'bucket': 'B1_pay_and_state', 'dry_run': False, 'limit': 25}"

# 4. the rest, then the other buckets
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.run --kwargs "{'bucket': 'B1_pay_and_state', 'dry_run': False}"
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.run --kwargs "{'bucket': 'B3_state_only', 'dry_run': False}"
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.run_cancel --kwargs "{'dry_run': False}"
```

`kwargs` is `eval`'d as Python — `True`, not `true`.

Every run writes a per-invoice audit CSV to `private/files`. The script halts
after 3 unexpected failures and is idempotent, so re-running is safe.

### Verify — before re-enabling anything
```bash
docker exec erp-backend-1 bench --site frontend execute jarz_pos.scripts.backlog_migration.verify
```
Acceptance criteria, in order of importance:

1. **WooCommerce per-status counts identical to the baseline** (allowing for
   genuinely new orders). Any movement in `completed` means something escaped.
2. **Zero new lines** in `jarz_woocommerce.outbound.log`.
3. **Zero rows** in `tabEmail Queue`.
4. Spot-check 10 migrated Woo orders: `date_modified` unchanged, no new order
   notes, status still `completed`.
5. B1 outstanding → ~0; invoice status Overdue → Paid.

### Re-enabling outbound — the highest-risk moment

Deliberately **not** scriptable; do it by hand in the Desk UI, after `verify`.

The hourly `outbound_sync.reconcile_outbound_state` sweep targets invoices in an
error state, and **an invoice with a blank `woo_order_id` makes it `POST /orders`
— creating a brand new order on the live store, with the customer email that
follows.** Fix those first. Then re-enable in this order, checking the outbound
log after each: `enable_inbound_orders` → `enable_outbound_customers` →
`enable_outbound_orders`. Watch through one full hourly sweep.

---

## Follow-ups this migration does not address

1. **The root cause is untouched.** Thousands of inbound `order_webhook` /
   `order_poll` events sit `Skipped`, with `NeedsReview` and `DeadLetter` rows
   behind them. Migrating the backlog without fixing ingestion means the backlog
   rebuilds. Separate workstream, and arguably the more valuable one.
2. **Stock.** Physical count + one Stock Reconciliation, per the decision above.
3. **Staging re-arms itself.** Staging inherits production's `base_url` and
   consumer key on every refresh, and a previous manual hardening was silently
   wiped by a later restore. Add the `harden_outbound` call to the
   staging-refresh script.
4. **August will look anomalous** — ~657K EGP of collections landing in one day.
   Anyone reading the numbers needs to know it is a backlog correction.

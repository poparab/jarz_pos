"""Bring the finished WooCommerce order backlog to a finished state in ERPNext.

Run with::

    bench --site frontend execute jarz_pos.scripts.backlog_migration.preflight
    bench --site frontend execute jarz_pos.scripts.backlog_migration.harden_outbound --kwargs "{'confirm': 'DISABLE-OUTBOUND'}"
    bench --site frontend execute jarz_pos.scripts.backlog_migration.classify
    bench --site frontend execute jarz_pos.scripts.backlog_migration.run --kwargs "{'bucket': 'B1_pay_and_state', 'dry_run': True}"
    bench --site frontend execute jarz_pos.scripts.backlog_migration.run --kwargs "{'bucket': 'B1_pay_and_state', 'dry_run': False, 'limit': 25}"
    bench --site frontend execute jarz_pos.scripts.backlog_migration.run_cancel --kwargs "{'dry_run': True}"
    bench --site frontend execute jarz_pos.scripts.backlog_migration.verify

The POS ops pipeline stopped being driven around 2026-06-06. Orders kept arriving
from WooCommerce, kept being invoiced, kept being delivered in real life -- and
then sat frozen in an early ERPNext state with the cash never booked. This script
closes that gap for the orders WooCommerce says are ``completed``.

**The one thing that can hurt a customer.** These apps contain no customer
notification path at all -- no SMS, no WhatsApp, no mail to a customer address.
There is exactly one door from an ERPNext state change to a real person: the
WooCommerce status PUT in ``jarz_woocommerce_integration.services.outbound_sync``.
WooCommerce emails on *transitions*, so re-pushing ``completed`` onto an order
that is already ``completed`` would fire the completed-order email a second time
at a customer whose order arrived months ago.

Three independent things keep that door shut, and the script refuses to run
unless the first is already true:

1. ``WooCommerce Settings.enable_outbound_orders`` (and friends) must be ``0``.
   Every ``enqueue_*`` entry point checks this **first**, before it writes an
   outbox row -- so nothing is queued for a worker to send later either.
2. ``frappe.flags.ignore_woo_outbound`` is set for the whole run.
   ``outbound_sync._is_outbound_suppressed`` honours it, and it is the only
   guard that covers the Payment Entry path: that hook re-enters
   ``enqueue_invoice_sync`` with an invoice *name string*, so the flags on the
   Payment Entry document itself are never consulted.
3. The state change is written with ``frappe.db.set_value``, which does not fire
   ``doc_events`` at all. Submitting a Payment Entry is the only hook-firing
   operation this script performs.

**No Delivery Notes, no stock movement** (decided 2026-08-15). ``Manufacture``
stock entries stopped in May 2026, so finished goods were never booked *in*
either. Deducting them now would drive ~6,263 units of finished goods negative
against bins holding single digits -- ``allow_negative_stock = 0`` would block it
anyway, and fake negative stock is not more true than today's fiction. Stock is
corrected separately by a physical count plus one Stock Reconciliation.

Skipping the Delivery Note also removes the whole Out-for-Delivery machinery from
the blast radius: no consumable deduction, no courier record and no fake courier
liability, no tracking token minted for a months-old order.

**Posting date is today.** No backdating, no valuation reposting, no reopening of
closed months. ``custom_delivered_at`` still carries the real Woo completion
timestamp -- it is a data field and drives no posting.
"""

from __future__ import annotations

import csv
import datetime
import os
import traceback
from collections import defaultdict
from typing import Any, Dict, List, Optional

import frappe

#: Stamped into every Payment Entry this script creates, in ``remarks`` and
#: ``reference_no``. It is what makes a re-run a no-op instead of a double
#: payment, and what lets the whole migration be found again afterwards.
MARKER = "BACKLOG-MIGRATION-2026-08"

#: Woo status snapshot, uploaded to ``<site>/private/files`` before classifying.
#: Columns: id,status,date_created,date_completed,date_paid,payment_method,total
SNAPSHOT_FILE = "woo_snapshot.csv"

#: ERPNext ops states that mean "not finished". Note ``Recieved`` -- the
#: misspelling is the value actually stored in the database, and both spellings
#: are accepted here so this keeps working if it is ever corrected.
MID_STATES = frozenset({"Recieved", "Received", "In Progress", "Ready", "Out for Delivery"})

#: Terminal states. An invoice already here is left alone.
TERMINAL_STATES = frozenset({"Delivered", "Cancelled", "Returned"})

#: Woo statuses that mean the order is still moving. Never touched -- these are
#: live orders the normal pipeline still owns.
WOO_IN_FLIGHT = frozenset({
    "processing", "preparing", "pending", "on-hold",
    "pre-nasrcity", "pre-hadayk", "pre-ismailia", "out-for-delivery",
})

#: Woo statuses that mean the order is dead and the ERPNext invoice should be too.
WOO_DEAD = frozenset({"cancelled", "refunded", "failed"})

#: How the customer actually paid -> which account receives the cash. Derived
#: from 7,304 historical Payment Entries, not from ``mode_of_payment``: the
#: historical entries were created programmatically with the account set
#: directly and carry no mode. Anything not in this map is never guessed -- it
#: goes to a manual bucket.
PAYMENT_ACCOUNTS = {
    "cod": "Cash - {abbr}",
    "instapay": "Bank Account - {abbr}",
}

#: Stop the run rather than grind through a failure mode nobody has looked at.
MAX_FAILURES = 3


# --------------------------------------------------------------------- paths --
def _files_dir() -> str:
    return frappe.get_site_path("private", "files")


def _path(filename: str) -> str:
    return os.path.join(_files_dir(), filename)


def _bucket_path(bucket: str) -> str:
    return _path("bucket_{0}.csv".format(bucket))


# -------------------------------------------------------------------- safety --
#: Every switch that can put an HTTP request on the wire to the live store.
OUTBOUND_SWITCHES = (
    "enable_outbound_orders",
    "enable_outbound_customers",
    "enable_inbound_orders",
    "enable_outbound_tracking_url",
)


def _outbound_state() -> Dict[str, Any]:
    return {
        field: frappe.db.get_single_value("WooCommerce Settings", field)
        for field in OUTBOUND_SWITCHES
    }


def _is_on(value: Any) -> bool:
    return str(value or "0").strip() not in ("0", "", "None", "False")


def _assert_safe() -> None:
    """Refuse to run while ERPNext can still push a status to the live store.

    Checked against the settings rather than assumed, because the cost of being
    wrong is a completed-order email to every customer in the backlog.
    """
    live = {field: value for field, value in _outbound_state().items() if _is_on(value)}
    if live:
        frappe.throw(
            "ABORT: WooCommerce outbound is still armed ({0}). Run "
            "harden_outbound first -- otherwise every Payment Entry pushes a "
            "status change to the live store and emails a real customer.".format(
                ", ".join("{0}={1}".format(k, v) for k, v in sorted(live.items()))
            )
        )
    # Independent of the settings, and the only guard that covers the Payment
    # Entry hook -- it re-enters enqueue_invoice_sync with an invoice *name*,
    # so no document-level flag is ever consulted on that path.
    frappe.flags.ignore_woo_outbound = True


def harden_outbound(confirm: str = "") -> Dict[str, Any]:
    """Turn every outbound switch off. Disable-only, on purpose.

    There is deliberately no counterpart that turns them back on. Re-enabling is
    the single highest-risk moment of the migration -- the hourly
    ``reconcile_outbound_state`` sweep re-pushes anything left in an error state,
    and an invoice with a blank ``woo_order_id`` makes it ``POST /orders``, i.e.
    a brand new order on the live store with the customer email that follows.
    That step stays a deliberate human action in the Desk UI, after ``verify``.
    """
    if confirm != "DISABLE-OUTBOUND":
        frappe.throw("Refusing: pass confirm='DISABLE-OUTBOUND' to disable Woo outbound.")

    before = _outbound_state()
    for field in OUTBOUND_SWITCHES:
        frappe.db.set_single_value("WooCommerce Settings", field, 0)
    frappe.db.commit()
    after = _outbound_state()
    print("outbound switches: {0} -> {1}".format(before, after))
    return {"before": before, "after": after}


# ------------------------------------------------------------------ accounts --
def _company_abbr(company: str) -> str:
    return frappe.db.get_value("Company", company, "abbr") or ""


def _resolve_account(company: str, woo_payment_method: str) -> Optional[str]:
    """The account that receives this payment, or None if we will not guess.

    ``kashier_card`` / ``kashier_wallet`` deliberately return None: those orders
    are collected by the gateway and already carry an automatic Payment Entry,
    so an outstanding balance on one means something unusual that a human should
    look at.
    """
    template = PAYMENT_ACCOUNTS.get((woo_payment_method or "").strip().lower())
    if not template:
        return None
    account = template.format(abbr=_company_abbr(company))
    if not frappe.db.exists("Account", account):
        return None
    is_group, acc_company = frappe.db.get_value("Account", account, ["is_group", "company"])
    if is_group or acc_company != company:
        return None
    return account


# ----------------------------------------------------------------- snapshot --
def _load_snapshot() -> Dict[int, Dict[str, str]]:
    path = _path(SNAPSHOT_FILE)
    if not os.path.exists(path):
        frappe.throw(
            "Missing Woo snapshot at {0}. Upload the woo_snapshot.csv produced by "
            "the read-only store crawl before classifying.".format(path)
        )
    snapshot: Dict[int, Dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                snapshot[int(row["id"])] = row
            except (TypeError, ValueError):
                continue
    if not snapshot:
        frappe.throw("Woo snapshot at {0} is empty.".format(path))
    return snapshot


def _clean_datetime(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    return value.replace("T", " ")[:19]


# ---------------------------------------------------------------- preflight --
def preflight() -> Dict[str, Any]:
    """Read-only. Prints what the run would face; changes nothing."""
    state = _outbound_state()
    print("WooCommerce outbound switches:")
    for field, value in sorted(state.items()):
        print("  {0:32} {1!r:>8}  {2}".format(field, value, "ARMED" if _is_on(value) else "off"))

    rows = frappe.db.sql(
        """
        SELECT COALESCE(NULLIF(custom_sales_invoice_state, ''), '(empty)') AS state,
               COUNT(*) AS n,
               ROUND(SUM(outstanding_amount)) AS outstanding
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND woo_order_id IS NOT NULL AND woo_order_id <> 0
        GROUP BY state ORDER BY n DESC
        """,
        as_dict=True,
    )
    print("\nLive Woo-linked invoices by ops state:")
    for row in rows:
        print("  {0:24} {1:>6}  {2:>12,} EGP".format(row.state, row.n, row.outstanding or 0))

    snapshot_path = _path(SNAPSHOT_FILE)
    print("\nsnapshot: {0} ({1})".format(
        snapshot_path, "present" if os.path.exists(snapshot_path) else "MISSING"
    ))
    return {"outbound": state, "states": rows, "snapshot": os.path.exists(snapshot_path)}


# ----------------------------------------------------------------- classify --
#: Written to every bucket CSV. ``classify`` is the only thing that decides what
#: happens to an invoice; ``run`` just executes a bucket and re-checks the guards.
BUCKET_COLUMNS = [
    "name", "woo_order_id", "woo_status", "woo_date_completed", "woo_payment_method",
    "docstatus", "status", "ops_state", "legacy_state", "outstanding_amount",
    "grand_total", "posting_date", "company", "customer", "live_siblings",
]


def classify() -> Dict[str, int]:
    """Sort every Woo-linked invoice into a bucket, grounded in live Woo status.

    Read-only apart from the bucket CSVs it writes.
    """
    snapshot = _load_snapshot()
    print("Woo snapshot: {0} orders".format(len(snapshot)))

    invoices = frappe.db.sql(
        """
        SELECT name, woo_order_id, docstatus, status,
               custom_sales_invoice_state AS ops_state,
               sales_invoice_state AS legacy_state,
               outstanding_amount, grand_total, posting_date, company, customer
        FROM `tabSales Invoice`
        WHERE woo_order_id IS NOT NULL AND woo_order_id <> 0
        """,
        as_dict=True,
    )
    print("ERPNext Woo-linked invoices: {0}".format(len(invoices)))

    by_order: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for invoice in invoices:
        by_order[int(invoice.woo_order_id)].append(invoice)

    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for woo_id, rows in by_order.items():
        woo = snapshot.get(woo_id)
        woo_status = (woo.get("status") if woo else "MISSING_IN_WOO") or "MISSING_IN_WOO"
        live = [r for r in rows if r.docstatus == 1]

        for row in rows:
            row["woo_status"] = woo_status
            row["woo_date_completed"] = (woo or {}).get("date_completed", "")
            row["woo_payment_method"] = (woo or {}).get("payment_method", "")
            row["live_siblings"] = len(live)

        # An order we cannot see in Woo must never be auto-processed: a deleted
        # order would otherwise masquerade as completed.
        if woo_status == "MISSING_IN_WOO":
            buckets["MANUAL_missing_in_woo"].extend(rows)
            continue

        # Duplicates poison every other rule, so they are resolved first and by
        # hand. One live invoice among cancelled siblings is the ordinary
        # cancel-and-reissue pattern and is not ambiguous.
        if len(live) > 1:
            buckets["MANUAL_duplicate_live"].extend(rows)
            continue

        if not live:
            # Cancelled on both sides is simply correct; nothing to do.
            if woo_status == "completed":
                buckets["MANUAL_erp_cancelled_woo_completed"].extend(rows)
            continue

        invoice = live[0]
        state = (invoice.ops_state or "").strip()

        if woo_status in WOO_IN_FLIGHT:
            buckets["B5_in_flight_SKIP"].append(invoice)
            continue

        if woo_status in WOO_DEAD:
            buckets["B4_cancel_in_erp"].append(invoice)
            continue

        if woo_status != "completed":
            buckets["MANUAL_unknown_woo_status"].append(invoice)
            continue

        # `outbound_sync._collect_invoice_states` reads BOTH state fields and
        # matches "cancelled" before "delivered". An invoice still carrying a
        # legacy Cancelled would therefore push `cancelled` to Woo the moment
        # outbound is switched back on -- a cancellation email for an order the
        # customer already received. Never bulk-process one.
        if (invoice.legacy_state or "").strip().lower() in ("cancelled", "canceled"):
            buckets["MANUAL_legacy_cancelled"].append(invoice)
            continue

        if state in TERMINAL_STATES and invoice.outstanding_amount <= 0:
            buckets["ALIGNED"].append(invoice)
            continue

        needs_payment = invoice.outstanding_amount > 0
        needs_state = state != "Delivered"

        if needs_payment and not _resolve_account(invoice.company, invoice.woo_payment_method):
            buckets["MANUAL_unmapped_payment"].append(invoice)
            continue

        if needs_payment and needs_state:
            buckets["B1_pay_and_state"].append(invoice)
        elif needs_payment:
            buckets["B1b_pay_only"].append(invoice)
        elif needs_state:
            buckets["B3_state_only"].append(invoice)
        else:
            buckets["ALIGNED"].append(invoice)

    print("\n{0:38} {1:>9} {2:>17}".format("bucket", "invoices", "outstanding EGP"))
    print("-" * 68)
    counts: Dict[str, int] = {}
    for bucket in sorted(buckets):
        rows = buckets[bucket]
        counts[bucket] = len(rows)
        total = sum(float(r.outstanding_amount or 0) for r in rows)
        print("{0:38} {1:>9} {2:>17,.0f}".format(bucket, len(rows), total))
        if bucket == "ALIGNED":
            continue
        with open(_bucket_path(bucket), "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=BUCKET_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    print("-" * 68)
    print("bucket CSVs written to {0}".format(_files_dir()))
    return counts


# --------------------------------------------------------------------- audit --
def _write_audit(rows: List[Dict[str, Any]], bucket: str, dry_run: bool) -> Optional[str]:
    if not rows:
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _path("migration_{0}_{1}_{2}.csv".format(bucket, "DRY" if dry_run else "LIVE", stamp))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


# ------------------------------------------------------------------ payments --
def _already_paid_by_migration(invoice: str) -> bool:
    """True if a submitted migration Payment Entry already covers this invoice."""
    references = frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice, "docstatus": 1},
        fields=["parent"],
    )
    for reference in references:
        remarks = frappe.db.get_value("Payment Entry", reference.parent, "remarks") or ""
        if MARKER in remarks:
            return True
    return False


def _build_payment_entry(invoice: Any, account: str) -> Any:
    payment = frappe.new_doc("Payment Entry")
    payment.payment_type = "Receive"
    payment.company = invoice.company
    payment.posting_date = frappe.utils.today()
    payment.party_type = "Customer"
    payment.party = invoice.customer
    payment.paid_from = invoice.debit_to  # the receivable this clears
    payment.paid_to = account
    payment.paid_amount = invoice.outstanding_amount
    payment.received_amount = invoice.outstanding_amount
    payment.source_exchange_rate = 1
    payment.target_exchange_rate = 1
    payment.reference_no = MARKER
    payment.reference_date = frappe.utils.today()
    payment.remarks = "{0} | woo_order_id={1} | invoice={2}".format(
        MARKER, invoice.woo_order_id, invoice.name
    )
    payment.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": invoice.name,
        "total_amount": invoice.grand_total,
        "outstanding_amount": invoice.outstanding_amount,
        "allocated_amount": invoice.outstanding_amount,
    })
    payment.flags.ignore_woo_outbound = True
    return payment


def _receive_payment(invoice: Any, woo_payment_method: str, dry_run: bool):
    """Book the outstanding amount into the account matching how they paid."""
    account = _resolve_account(invoice.company, woo_payment_method)
    if not account:
        return None, "SKIP", "no account mapping for payment_method={0!r}".format(woo_payment_method)

    amount = invoice.outstanding_amount
    if amount <= 0:
        return None, "OK", "already settled"

    payment = _build_payment_entry(invoice, account)

    if dry_run:
        # Build and validate for real, but never insert. This catches a missing
        # account, a party mismatch or a bad allocation without writing a row
        # and without firing a single hook.
        payment.set_missing_values()
        payment.validate()
        return None, "DRY", "WOULD receive {0:,.2f} -> {1}".format(amount, account)

    payment.insert(ignore_permissions=True)
    payment.submit()
    return payment.name, "OK", "received {0:,.2f} -> {1}".format(amount, account)


def _set_delivered(invoice_name: str, delivered_at: Optional[str], dry_run: bool) -> str:
    """Advance the ops state without touching the document.

    ``frappe.db.set_value`` does not fire ``doc_events``, so this cannot reach
    WooCommerce, cannot deduct consumables, cannot create a Courier Transaction
    and cannot mint a tracking token -- which is exactly the intent. Both state
    fields are written: ``_collect_invoice_states`` reads both, and leaving the
    legacy one stale is how a delivered order ends up pushing ``processing``.
    """
    if dry_run:
        return "WOULD set Delivered"
    payload: Dict[str, Any] = {
        "custom_sales_invoice_state": "Delivered",
        "sales_invoice_state": "Delivered",
    }
    if delivered_at:
        payload["custom_delivered_at"] = delivered_at
    frappe.db.set_value("Sales Invoice", invoice_name, payload, update_modified=False)
    return "state=Delivered"


# ----------------------------------------------------------------------- run --
def run(bucket: str = "B1_pay_and_state", dry_run: bool = True,
        limit: Optional[int] = None, csv_path: Optional[str] = None) -> Dict[str, Any]:
    """Process one bucket: book the cash, advance the state.

    Idempotent -- a second run finds the payment marker and the terminal state
    and does nothing. Resumable: pass ``limit`` to work in batches.
    """
    _assert_safe()

    path = csv_path or _bucket_path(bucket)
    if not os.path.exists(path):
        frappe.throw("No bucket file at {0}. Run classify first.".format(path))
    with open(path, newline="", encoding="utf-8") as handle:
        todo = list(csv.DictReader(handle))
    if limit:
        todo = todo[: int(limit)]

    print("{0}: {1} invoices | dry_run={2}".format(bucket, len(todo), dry_run))
    audit: List[Dict[str, Any]] = []
    failures = 0

    for index, row in enumerate(todo, 1):
        name = row["name"]
        record = {
            "invoice": name,
            "woo_order_id": row["woo_order_id"],
            "woo_status": row["woo_status"],
            "before_state": row["ops_state"],
            "outstanding": row["outstanding_amount"],
            "payment_entry": "",
            "outcome": "",
            "note": "",
        }
        try:
            # Only ever act on an order Woo still calls finished.
            if row["woo_status"] != "completed":
                record["outcome"] = "SKIP"
                record["note"] = "woo_status={0}".format(row["woo_status"])
                audit.append(record)
                continue

            invoice = frappe.get_doc("Sales Invoice", name)

            # The live document must still look like what classify() saw. Anything
            # else means a human or the pipeline touched it in between, and their
            # change wins.
            if invoice.docstatus != 1:
                record["outcome"] = "SKIP"
                record["note"] = "docstatus={0}".format(invoice.docstatus)
                audit.append(record)
                continue
            if (row["ops_state"] or "") != (invoice.custom_sales_invoice_state or ""):
                record["outcome"] = "SKIP"
                record["note"] = "state moved since classification: {0}".format(
                    invoice.custom_sales_invoice_state
                )
                audit.append(record)
                continue

            notes: List[str] = []
            skipped = False

            if float(row["outstanding_amount"] or 0) > 0 and invoice.outstanding_amount > 0:
                if _already_paid_by_migration(name):
                    notes.append("payment already migrated")
                else:
                    payment_name, status, message = _receive_payment(
                        invoice, row["woo_payment_method"], dry_run
                    )
                    record["payment_entry"] = payment_name or ""
                    notes.append(message)
                    if status == "SKIP":
                        skipped = True

            if skipped:
                # Never advance the state on an invoice whose cash we could not
                # book -- that would hide an unpaid order in a Delivered column.
                record["outcome"] = "SKIP"
                record["note"] = "; ".join(notes)
                audit.append(record)
                continue

            if invoice.custom_sales_invoice_state != "Delivered":
                notes.append(_set_delivered(name, _clean_datetime(row["woo_date_completed"]), dry_run))

            record["outcome"] = "DRY" if dry_run else "OK"
            record["note"] = "; ".join(notes)
            if not dry_run:
                frappe.db.commit()

        except Exception as exc:  # noqa: BLE001
            failures += 1
            record["outcome"] = "FAIL"
            record["note"] = "{0}: {1}".format(type(exc).__name__, exc)
            if not dry_run:
                frappe.db.rollback()
            print("  [{0}/{1}] FAIL {2}: {3}".format(index, len(todo), name, exc))
            traceback.print_exc()
            if failures >= MAX_FAILURES:
                audit.append(record)
                print("HALTING: {0} failures -- investigate before continuing.".format(failures))
                break

        audit.append(record)
        if index % 25 == 0:
            print("  [{0}/{1}] ...".format(index, len(todo)))

    return _summarise(audit, bucket, dry_run, failures)


def _summarise(audit: List[Dict[str, Any]], bucket: str, dry_run: bool,
               failures: int) -> Dict[str, Any]:
    path = _write_audit(audit, bucket, dry_run)
    processed = sum(1 for r in audit if r["outcome"] in ("OK", "DRY"))
    skipped = sum(1 for r in audit if r["outcome"] == "SKIP")
    print("\ndone: {0} processed, {1} failed, {2} skipped".format(processed, failures, skipped))
    print("audit: {0}".format(path))
    return {"processed": processed, "failed": failures, "skipped": skipped, "audit": path}


# ------------------------------------------------------------------- cancels --
def run_cancel(dry_run: bool = True, limit: Optional[int] = None,
               csv_path: Optional[str] = None) -> Dict[str, Any]:
    """B4: WooCommerce says the order is dead, ERPNext still has it live.

    ``before_cancel -> block_cancel_if_dispatched`` refuses a dispatched invoice.
    Those are recorded as SKIP for a human rather than forced -- an invoice that
    physically went out of the door is not a clerical error.
    """
    _assert_safe()

    path = csv_path or _bucket_path("B4_cancel_in_erp")
    if not os.path.exists(path):
        frappe.throw("No bucket file at {0}. Run classify first.".format(path))
    with open(path, newline="", encoding="utf-8") as handle:
        todo = list(csv.DictReader(handle))
    if limit:
        todo = todo[: int(limit)]

    print("B4 cancel: {0} invoices | dry_run={1}".format(len(todo), dry_run))
    audit: List[Dict[str, Any]] = []
    failures = 0

    for index, row in enumerate(todo, 1):
        name = row["name"]
        record = {
            "invoice": name,
            "woo_order_id": row["woo_order_id"],
            "woo_status": row["woo_status"],
            "outstanding": row["outstanding_amount"],
            "payments_cancelled": "",
            "outcome": "",
            "note": "",
        }
        try:
            invoice = frappe.get_doc("Sales Invoice", name)
            if invoice.docstatus != 1:
                record["outcome"] = "SKIP"
                record["note"] = "docstatus={0}".format(invoice.docstatus)
                audit.append(record)
                continue

            # A submitted Payment Entry blocks the invoice cancellation, so it
            # has to be unwound first.
            references = frappe.get_all(
                "Payment Entry Reference",
                filters={"reference_doctype": "Sales Invoice", "reference_name": name, "docstatus": 1},
                fields=["parent"],
            )
            payments = sorted({r.parent for r in references})

            if dry_run:
                record["payments_cancelled"] = ",".join(payments)
                record["outcome"] = "DRY"
                record["note"] = "WOULD cancel invoice" + (
                    " and {0} payment entry(s)".format(len(payments)) if payments else ""
                )
                audit.append(record)
                continue

            for payment_name in payments:
                payment = frappe.get_doc("Payment Entry", payment_name)
                payment.flags.ignore_woo_outbound = True
                payment.cancel()
            record["payments_cancelled"] = ",".join(payments)

            frappe.db.set_value("Sales Invoice", name, {
                "custom_cancellation_type": "Cancelled by Customer",
                "custom_cancellation_reason": "{0} | Woo status={1}".format(MARKER, row["woo_status"]),
            }, update_modified=False)

            invoice.reload()
            invoice.flags.ignore_woo_outbound = True
            invoice.cancel()

            record["outcome"] = "OK"
            record["note"] = "cancelled"
            frappe.db.commit()

        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "dispatch" in message.lower():
                record["outcome"] = "SKIP"
                record["note"] = "blocked (dispatched) -- manual review: {0}".format(message)
                frappe.db.rollback()
                audit.append(record)
                continue
            failures += 1
            record["outcome"] = "FAIL"
            record["note"] = "{0}: {1}".format(type(exc).__name__, exc)
            frappe.db.rollback()
            print("  [{0}/{1}] FAIL {2}: {3}".format(index, len(todo), name, exc))
            traceback.print_exc()
            if failures >= MAX_FAILURES:
                audit.append(record)
                print("HALTING: {0} failures -- investigate before continuing.".format(failures))
                break

        audit.append(record)

    return _summarise(audit, "B4_cancel", dry_run, failures)


# ------------------------------------------------------------------- verify --
def verify() -> Dict[str, Any]:
    """Post-run evidence. Read-only.

    The customer-safety checks matter more than the financial ones: the books
    can be corrected afterwards, an email to 1,192 customers cannot.
    """
    payments = frappe.db.sql(
        """
        SELECT COUNT(*) AS n, ROUND(SUM(paid_amount)) AS total, paid_to AS account
        FROM `tabPayment Entry`
        WHERE docstatus = 1 AND remarks LIKE %s
        GROUP BY paid_to
        """,
        ("%{0}%".format(MARKER),),
        as_dict=True,
    )
    print("Migration Payment Entries:")
    for row in payments:
        print("  {0:28} {1:>6}  {2:>12,} EGP".format(row.account, row.n, row.total or 0))

    remaining = frappe.db.sql(
        """
        SELECT COALESCE(NULLIF(custom_sales_invoice_state, ''), '(empty)') AS state,
               COUNT(*) AS n, ROUND(SUM(outstanding_amount)) AS outstanding
        FROM `tabSales Invoice`
        WHERE docstatus = 1 AND woo_order_id IS NOT NULL AND woo_order_id <> 0
        GROUP BY state ORDER BY n DESC
        """,
        as_dict=True,
    )
    print("\nLive Woo-linked invoices by ops state:")
    for row in remaining:
        print("  {0:24} {1:>6}  {2:>12,} EGP".format(row.state, row.n, row.outstanding or 0))

    # The acceptance criterion that matters most: nothing left ERPNext.
    emails = frappe.db.count("Email Queue", {"creation": (">", frappe.utils.add_days(None, -1))})
    pending = frappe.db.sql(
        """
        SELECT status, COUNT(*) AS n FROM `tabWooCommerce Sync Event`
        WHERE direction = 'Outbound' AND creation > %s GROUP BY status
        """,
        (frappe.utils.add_days(None, -1),),
        as_dict=True,
    ) if frappe.db.exists("DocType", "WooCommerce Sync Event") else []

    print("\nEmail Queue rows in the last 24h: {0}  (expected 0)".format(emails))
    print("Outbound sync events in the last 24h: {0}  (expected none)".format(
        {r.status: r.n for r in pending} or "none"
    ))
    print("Outbound switches: {0}".format(_outbound_state()))
    return {
        "payments": payments,
        "states": remaining,
        "email_queue_24h": emails,
        "outbound_events_24h": pending,
        "outbound": _outbound_state(),
    }

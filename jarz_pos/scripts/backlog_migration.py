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
from typing import Any, Optional

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
MID_STATES = frozenset({"Recieved", "Received", "In Progress", "Out for Delivery"})

#: Terminal states. An invoice already here is left alone.
TERMINAL_STATES = frozenset({"Delivered", "Cancelled", "Returned"})

#: Woo statuses that mean the order is still moving. Never touched -- these are
#: live orders the normal pipeline still owns.
WOO_IN_FLIGHT = frozenset(
	{
		"processing",
		"preparing",
		"pending",
		"on-hold",
		"pre-nasrcity",
		"pre-hadayk",
		"pre-ismailia",
		"out-for-delivery",
	}
)

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
	# Card/wallet money is collected by the Kashier gateway and sits in its
	# holding account until it settles to the bank. 507 historical Payment
	# Entries already land here, so this is the established treatment, not a
	# new one invented by the migration.
	"kashier_card": "kashier - {abbr}",
	"kashier_wallet": "kashier - {abbr}",
	"wallet": "kashier - {abbr}",
}

#: Methods where a gateway takes the money rather than a courier. For these the
#: migration books only when WooCommerce recorded an actual ``date_paid``: the
#: gateway either charged the card or it did not, and there is no courier whose
#: word could stand in for that. A COD order can legitimately be collected with
#: no ``date_paid`` recorded; a card order cannot.
GATEWAY_METHODS = frozenset({"kashier_card", "kashier_wallet", "wallet"})

#: Stop the run rather than grind through a failure mode nobody has looked at.
MAX_FAILURES = 3


# --------------------------------------------------------------------- paths --
def _files_dir() -> str:
	return frappe.get_site_path("private", "files")


def _path(filename: str) -> str:
	return os.path.join(_files_dir(), filename)


def _bucket_path(bucket: str) -> str:
	return _path(f"bucket_{bucket}.csv")


# -------------------------------------------------------------------- safety --
#: Every switch that can put an HTTP request on the wire to the live store.
OUTBOUND_SWITCHES = (
	"enable_outbound_orders",
	"enable_outbound_customers",
	"enable_inbound_orders",
	"enable_outbound_tracking_url",
)


def _outbound_state() -> dict[str, Any]:
	return {field: frappe.db.get_single_value("WooCommerce Settings", field) for field in OUTBOUND_SWITCHES}


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
			"ABORT: WooCommerce outbound is still armed ({}). Run "
			"harden_outbound first -- otherwise every Payment Entry pushes a "
			"status change to the live store and emails a real customer.".format(
				", ".join(f"{k}={v}" for k, v in sorted(live.items()))
			)
		)
	# Independent of the settings, and the only guard that covers the Payment
	# Entry hook -- it re-enters enqueue_invoice_sync with an invoice *name*,
	# so no document-level flag is ever consulted on that path.
	frappe.flags.ignore_woo_outbound = True


def harden_outbound(confirm: str = "") -> dict[str, Any]:
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
	print(f"outbound switches: {before} -> {after}")
	return {"before": before, "after": after}


# ------------------------------------------------------------------ accounts --
def _company_abbr(company: str) -> str:
	return frappe.db.get_value("Company", company, "abbr") or ""


def _resolve_account(company: str, woo_payment_method: str, woo_date_paid: str = "") -> str | None:
	"""The account that receives this payment, or None if we will not guess.

	A method with no mapping returns None and the invoice goes to a human --
	booking cash into a guessed account is worse than leaving the order stale.

	A gateway method with no ``date_paid`` also returns None: the usual reason a
	card order still shows a balance is that the gateway never charged it, and
	debiting the Kashier holding account for money it never took would invent an
	asset.
	"""
	method = (woo_payment_method or "").strip().lower()
	template = PAYMENT_ACCOUNTS.get(method)
	if not template:
		return None
	if method in GATEWAY_METHODS and not str(woo_date_paid or "").strip():
		return None
	account = template.format(abbr=_company_abbr(company))
	if not frappe.db.exists("Account", account):
		return None
	is_group, acc_company = frappe.db.get_value("Account", account, ["is_group", "company"])
	if is_group or acc_company != company:
		return None
	return account


# ----------------------------------------------------------------- snapshot --
def _load_snapshot() -> dict[int, dict[str, str]]:
	path = _path(SNAPSHOT_FILE)
	if not os.path.exists(path):
		frappe.throw(
			f"Missing Woo snapshot at {path}. Upload the woo_snapshot.csv produced by "
			"the read-only store crawl before classifying."
		)
	snapshot: dict[int, dict[str, str]] = {}
	with open(path, newline="", encoding="utf-8") as handle:
		for row in csv.DictReader(handle):
			try:
				snapshot[int(row["id"])] = row
			except (TypeError, ValueError):
				continue
	if not snapshot:
		frappe.throw(f"Woo snapshot at {path} is empty.")
	return snapshot


def _clean_datetime(value: str) -> str | None:
	value = (value or "").strip()
	if not value:
		return None
	return value.replace("T", " ")[:19]


# ---------------------------------------------------------------- preflight --
def preflight() -> dict[str, Any]:
	"""Read-only. Prints what the run would face; changes nothing."""
	state = _outbound_state()
	print("WooCommerce outbound switches:")
	for field, value in sorted(state.items()):
		print("  {:32} {!r:>8}  {}".format(field, value, "ARMED" if _is_on(value) else "off"))

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
		print(f"  {row.state:24} {row.n:>6}  {row.outstanding or 0:>12,} EGP")

	snapshot_path = _path(SNAPSHOT_FILE)
	print(
		"\nsnapshot: {} ({})".format(snapshot_path, "present" if os.path.exists(snapshot_path) else "MISSING")
	)
	return {"outbound": state, "states": rows, "snapshot": os.path.exists(snapshot_path)}


# ----------------------------------------------------------------- classify --
#: Written to every bucket CSV. ``classify`` is the only thing that decides what
#: happens to an invoice; ``run`` just executes a bucket and re-checks the guards.
BUCKET_COLUMNS = [
	"name",
	"woo_order_id",
	"woo_status",
	"woo_date_completed",
	"woo_date_paid",
	"woo_payment_method",
	"docstatus",
	"status",
	"ops_state",
	"legacy_state",
	"outstanding_amount",
	"grand_total",
	"posting_date",
	"company",
	"customer",
	"live_siblings",
]


def classify() -> dict[str, int]:
	"""Sort every Woo-linked invoice into a bucket, grounded in live Woo status.

	Read-only apart from the bucket CSVs it writes.
	"""
	snapshot = _load_snapshot()
	print(f"Woo snapshot: {len(snapshot)} orders")

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
	print(f"ERPNext Woo-linked invoices: {len(invoices)}")

	by_order: dict[int, list[dict[str, Any]]] = defaultdict(list)
	for invoice in invoices:
		by_order[int(invoice.woo_order_id)].append(invoice)

	buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

	for woo_id, rows in by_order.items():
		woo = snapshot.get(woo_id)
		woo_status = (woo.get("status") if woo else "MISSING_IN_WOO") or "MISSING_IN_WOO"
		live = [r for r in rows if r.docstatus == 1]

		for row in rows:
			row["woo_status"] = woo_status
			row["woo_date_completed"] = (woo or {}).get("date_completed", "")
			row["woo_date_paid"] = (woo or {}).get("date_paid", "")
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

		if needs_payment and not _resolve_account(
			invoice.company, invoice.woo_payment_method, invoice.woo_date_paid
		):
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

	print("\n{:38} {:>9} {:>17}".format("bucket", "invoices", "outstanding EGP"))
	print("-" * 68)
	counts: dict[str, int] = {}
	for bucket in sorted(buckets):
		rows = buckets[bucket]
		counts[bucket] = len(rows)
		total = sum(float(r.outstanding_amount or 0) for r in rows)
		print(f"{bucket:38} {len(rows):>9} {total:>17,.0f}")
		if bucket == "ALIGNED":
			continue
		with open(_bucket_path(bucket), "w", newline="", encoding="utf-8") as handle:
			writer = csv.DictWriter(handle, fieldnames=BUCKET_COLUMNS, extrasaction="ignore")
			writer.writeheader()
			writer.writerows(rows)
	print("-" * 68)
	print(f"bucket CSVs written to {_files_dir()}")
	return counts


# --------------------------------------------------------------------- audit --
def _write_audit(rows: list[dict[str, Any]], bucket: str, dry_run: bool) -> str | None:
	if not rows:
		return None
	stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
	path = _path("migration_{}_{}_{}.csv".format(bucket, "DRY" if dry_run else "LIVE", stamp))
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
	"""Build the Payment Entry the way the Desk's "Create > Payment" button does.

	Hand-constructing this document is a trap: HRMS replaces the Payment Entry
	class with its own ``EmployeePaymentEntry`` subclass, and a doc built field
	by field never gets ``party_account`` populated, so ``set_missing_values``
	dies on an AttributeError before any validation runs. ``get_payment_entry``
	is ERPNext's own builder -- it resolves the party account, the account
	currencies, the cost center, the exchange rate and the reference allocation
	from the invoice itself, and it already dates the entry ``nowdate()``, which
	is the posting date this migration wants.
	"""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	payment = get_payment_entry("Sales Invoice", invoice.name, bank_account=account)
	payment.reference_no = MARKER
	payment.reference_date = frappe.utils.today()
	payment.remarks = f"{MARKER} | woo_order_id={invoice.woo_order_id} | invoice={invoice.name}"
	payment.flags.ignore_woo_outbound = True
	return payment


def _receive_payment(invoice: Any, woo_payment_method: str, dry_run: bool, woo_date_paid: str = ""):
	"""Book the outstanding amount into the account matching how they paid."""
	account = _resolve_account(invoice.company, woo_payment_method, woo_date_paid)
	if not account:
		return (
			None,
			"SKIP",
			f"no account for payment_method={woo_payment_method!r} (date_paid={woo_date_paid!r})",
		)

	amount = invoice.outstanding_amount
	if amount <= 0:
		return None, "OK", "already settled"

	payment = _build_payment_entry(invoice, account)

	if dry_run:
		# Build and validate for real, but never insert. This catches a missing
		# account, a party mismatch or a bad allocation without writing a row
		# and without firing a single hook. `validate` runs the whole chain --
		# setup_party_account_field, set_missing_values, set_missing_ref_details
		# and the allocation checks.
		payment.validate()
		return None, "DRY", f"WOULD receive {amount:,.2f} -> {account} ({payment.paid_from})"

	payment.insert(ignore_permissions=True)
	payment.submit()
	return payment.name, "OK", f"received {amount:,.2f} -> {account}"


def _set_delivered(invoice_name: str, delivered_at: str | None, dry_run: bool) -> str:
	"""Advance the ops state without touching the document.

	``frappe.db.set_value`` does not fire ``doc_events``, so this cannot reach
	WooCommerce, cannot deduct consumables, cannot create a Courier Transaction
	and cannot mint a tracking token -- which is exactly the intent. Both state
	fields are written: ``_collect_invoice_states`` reads both, and leaving the
	legacy one stale is how a delivered order ends up pushing ``processing``.
	"""
	if dry_run:
		return "WOULD set Delivered"
	payload: dict[str, Any] = {
		"custom_sales_invoice_state": "Delivered",
		"sales_invoice_state": "Delivered",
	}
	if delivered_at:
		payload["custom_delivered_at"] = delivered_at
	frappe.db.set_value("Sales Invoice", invoice_name, payload, update_modified=False)
	return "state=Delivered"


# ----------------------------------------------------------------------- run --
def run(
	bucket: str = "B1_pay_and_state",
	dry_run: bool = True,
	limit: int | None = None,
	csv_path: str | None = None,
) -> dict[str, Any]:
	"""Process one bucket: book the cash, advance the state.

	Idempotent -- a second run finds the payment marker and the terminal state
	and does nothing. Resumable: pass ``limit`` to work in batches.
	"""
	_assert_safe()

	path = csv_path or _bucket_path(bucket)
	if not os.path.exists(path):
		frappe.throw(f"No bucket file at {path}. Run classify first.")
	with open(path, newline="", encoding="utf-8") as handle:
		todo = list(csv.DictReader(handle))
	if limit:
		todo = todo[: int(limit)]

	print(f"{bucket}: {len(todo)} invoices | dry_run={dry_run}")
	audit: list[dict[str, Any]] = []
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
				record["note"] = "woo_status={}".format(row["woo_status"])
				audit.append(record)
				continue

			invoice = frappe.get_doc("Sales Invoice", name)

			# The live document must still look like what classify() saw. Anything
			# else means a human or the pipeline touched it in between, and their
			# change wins.
			if invoice.docstatus != 1:
				record["outcome"] = "SKIP"
				record["note"] = f"docstatus={invoice.docstatus}"
				audit.append(record)
				continue
			if (row["ops_state"] or "") != (invoice.custom_sales_invoice_state or ""):
				record["outcome"] = "SKIP"
				record["note"] = f"state moved since classification: {invoice.custom_sales_invoice_state}"
				audit.append(record)
				continue

			notes: list[str] = []
			skipped = False

			if float(row["outstanding_amount"] or 0) > 0 and invoice.outstanding_amount > 0:
				if _already_paid_by_migration(name):
					notes.append("payment already migrated")
				else:
					payment_name, status, message = _receive_payment(
						invoice,
						row["woo_payment_method"],
						dry_run,
						row.get("woo_date_paid", ""),
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

		except Exception as exc:
			failures += 1
			record["outcome"] = "FAIL"
			record["note"] = f"{type(exc).__name__}: {exc}"
			if not dry_run:
				frappe.db.rollback()
			print(f"  [{index}/{len(todo)}] FAIL {name}: {exc}")
			traceback.print_exc()
			if failures >= MAX_FAILURES:
				audit.append(record)
				print(f"HALTING: {failures} failures -- investigate before continuing.")
				break

		audit.append(record)
		if index % 25 == 0:
			print(f"  [{index}/{len(todo)}] ...")

	return _summarise(audit, bucket, dry_run, failures)


def _summarise(audit: list[dict[str, Any]], bucket: str, dry_run: bool, failures: int) -> dict[str, Any]:
	path = _write_audit(audit, bucket, dry_run)
	processed = sum(1 for r in audit if r["outcome"] in ("OK", "DRY"))
	skipped = sum(1 for r in audit if r["outcome"] == "SKIP")
	print(f"\ndone: {processed} processed, {failures} failed, {skipped} skipped")
	print(f"audit: {path}")
	return {"processed": processed, "failed": failures, "skipped": skipped, "audit": path}


# ------------------------------------------------------------------- cancels --
def run_cancel(dry_run: bool = True, limit: int | None = None, csv_path: str | None = None) -> dict[str, Any]:
	"""B4: WooCommerce says the order is dead, ERPNext still has it live.

	``before_cancel -> block_cancel_if_dispatched`` refuses a dispatched invoice.
	Those are recorded as SKIP for a human rather than forced -- an invoice that
	physically went out of the door is not a clerical error.
	"""
	_assert_safe()

	path = csv_path or _bucket_path("B4_cancel_in_erp")
	if not os.path.exists(path):
		frappe.throw(f"No bucket file at {path}. Run classify first.")
	with open(path, newline="", encoding="utf-8") as handle:
		todo = list(csv.DictReader(handle))
	if limit:
		todo = todo[: int(limit)]

	print(f"B4 cancel: {len(todo)} invoices | dry_run={dry_run}")
	audit: list[dict[str, Any]] = []
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
				record["note"] = f"docstatus={invoice.docstatus}"
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
					f" and {len(payments)} payment entry(s)" if payments else ""
				)
				audit.append(record)
				continue

			for payment_name in payments:
				payment = frappe.get_doc("Payment Entry", payment_name)
				payment.flags.ignore_woo_outbound = True
				payment.cancel()
			record["payments_cancelled"] = ",".join(payments)

			frappe.db.set_value(
				"Sales Invoice",
				name,
				{
					"custom_cancellation_type": "Cancelled by Customer",
					"custom_cancellation_reason": "{} | Woo status={}".format(MARKER, row["woo_status"]),
				},
				update_modified=False,
			)

			invoice.reload()
			invoice.flags.ignore_woo_outbound = True
			invoice.cancel()

			record["outcome"] = "OK"
			record["note"] = "cancelled"
			frappe.db.commit()

		except frappe.ValidationError as exc:
			# A ValidationError here is a guard doing its job, not a defect:
			# `block_cancel_if_dispatched` refuses an invoice already Out for
			# Delivery, and ERPNext itself refuses one with a submitted Delivery
			# Note. Both mean goods physically moved, which is a business
			# decision and not a clerical error -- so it goes to a human with
			# the reason attached, rather than halting the whole run.
			#
			# Matching on the exception type rather than on words in the message
			# is deliberate: the first version of this looked for "dispatch",
			# which silently let the Delivery Note refusal count as a hard
			# failure and would have halted production after three of them.
			record["outcome"] = "SKIP"
			record["note"] = f"blocked -- manual review: {exc}"
			frappe.db.rollback()
			audit.append(record)
			continue

		except Exception as exc:
			failures += 1
			record["outcome"] = "FAIL"
			record["note"] = f"{type(exc).__name__}: {exc}"
			frappe.db.rollback()
			print(f"  [{index}/{len(todo)}] FAIL {name}: {exc}")
			traceback.print_exc()
			if failures >= MAX_FAILURES:
				audit.append(record)
				print(f"HALTING: {failures} failures -- investigate before continuing.")
				break

		audit.append(record)

	return _summarise(audit, "B4_cancel", dry_run, failures)


# ------------------------------------------------------------------- verify --
def verify() -> dict[str, Any]:
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
		(f"%{MARKER}%",),
		as_dict=True,
	)
	print("Migration Payment Entries:")
	for row in payments:
		print(f"  {row.account:28} {row.n:>6}  {row.total or 0:>12,} EGP")

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
		print(f"  {row.state:24} {row.n:>6}  {row.outstanding or 0:>12,} EGP")

	# The acceptance criterion that matters most: nothing left ERPNext.
	emails = frappe.db.count("Email Queue", {"creation": (">", frappe.utils.add_days(None, -1))})
	pending = (
		frappe.db.sql(
			"""
        SELECT status, COUNT(*) AS n FROM `tabWooCommerce Sync Event`
        WHERE direction = 'Outbound' AND creation > %s GROUP BY status
        """,
			(frappe.utils.add_days(None, -1),),
			as_dict=True,
		)
		if frappe.db.exists("DocType", "WooCommerce Sync Event")
		else []
	)

	print(f"\nEmail Queue rows in the last 24h: {emails}  (expected 0)")
	print(
		"Outbound sync events in the last 24h: {}  (expected none)".format(
			{r.status: r.n for r in pending} or "none"
		)
	)
	print(f"Outbound switches: {_outbound_state()}")
	return {
		"payments": payments,
		"states": remaining,
		"email_queue_24h": emails,
		"outbound_events_24h": pending,
		"outbound": _outbound_state(),
	}

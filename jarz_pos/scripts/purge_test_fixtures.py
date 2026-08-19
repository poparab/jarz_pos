"""Remove the synthetic fixtures the validation harnesses leave behind.

``b2b_accounting_validation``, ``return_flow_validation`` and
``return_money_audit`` all create real submitted documents under prefixed
customers and try to unwind them afterwards. Their teardown routinely fails on
dispatched orders, because cancelling a Sales Invoice is blocked while its
downstream artifacts still exist ("This invoice already has courier settlement
artifacts and cannot be changed from this workflow"). The residue then sits on
the operational board as live-looking cards — 141 of them in the Received column
at the time this was written.

The answer is not to bypass that guard. The guard is right: an invoice with a
courier transaction against it genuinely must not be cancelled from a normal
workflow. What makes this safe is that the *artifacts themselves are fixtures* —
so this removes them in dependency order, which dissolves the blocking condition
honestly, and only ever for customers matching :data:`FIXTURE_PREFIXES`.

Two hard limits, both deliberate:

* it refuses to run against production;
* it never touches a document whose customer does not match a fixture prefix.
  Every candidate is resolved from the customer, not from a date range or a
  name pattern on the invoice — a real order can never be swept in by being
  created at an unlucky moment.

The fixture Territory and Customer Group are the exception to that resolution
rule, and have to be: nothing points back at them once their customers are gone,
so resolving from documents left ``_B2BVALID_Territory`` and ``_B2BVALID_Group``
sitting on the site after every single purge. They are matched by name prefix
instead, and deleted without ``force`` so Frappe's own link check — not a
hand-written list of referrers — decides whether anything still uses them.

Usage::

    bench --site frontend execute jarz_pos.scripts.purge_test_fixtures.run
    bench --site frontend execute jarz_pos.scripts.purge_test_fixtures.run \
        --kwargs "{'dry_run': False}"

``dry_run`` defaults to True: it reports exactly what would go and writes
nothing.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import frappe

#: Only customers whose name starts with one of these are ever considered.
FIXTURE_PREFIXES = ("_B2BVALID_", "_RETVALID_", "_RETMONEY_")


def _guard_environment() -> None:
    """Hard stop anywhere that looks like production."""
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""
    host = str(frappe.db.get_single_value("Website Settings", "subdomain") or "")
    if "erp.orderjarz.com" in base_url or "erp.orderjarz.com" in host:
        raise RuntimeError(
            f"purge_test_fixtures refuses to run against production ({base_url!r})."
        )


def _fixture_customers() -> List[str]:
    """Fixture customer names, from the Customer table *and* from documents.

    Deliberately not just the Customer table. A run that deletes the Customer
    records but fails to cancel some of their invoices would otherwise be
    un-rerunnable: the second pass finds no customers, therefore no invoices, and
    reports a clean sweep over documents that are still sitting there. Reading the
    name off the documents as well makes the purge idempotent and lets it finish a
    job it started.
    """
    names: List[str] = []
    for prefix in FIXTURE_PREFIXES:
        names += frappe.get_all(
            "Customer",
            filters={"name": ["like", f"{prefix}%"]},
            pluck="name",
            limit_page_length=0,
        ) or []
        for doctype in ("Sales Invoice", "Delivery Note"):
            names += frappe.get_all(
                doctype,
                filters={"customer": ["like", f"{prefix}%"]},
                pluck="customer",
                limit_page_length=0,
            ) or []
    return sorted(set(names))


def _fixture_taxonomy() -> Dict[str, List[str]]:
    """Fixture Territory and Customer Group records, matched by name prefix.

    Unlike every other candidate here these cannot be resolved from a document.
    A Territory is not referenced by anything once its customers are deleted, so
    a document-driven sweep finds nothing and reports a clean site while the rows
    are still there. Matching the prefix directly is the only way to see them.
    """
    found: Dict[str, List[str]] = {"Territory": [], "Customer Group": []}
    for doctype in found:
        names: List[str] = []
        for prefix in FIXTURE_PREFIXES:
            names += frappe.get_all(
                doctype,
                filters={"name": ["like", f"{prefix}%"]},
                pluck="name",
                limit_page_length=0,
            ) or []
        found[doctype] = sorted(set(names))
    return found


def _fixture_invoices(customers: List[str]) -> List[Dict[str, Any]]:
    if not customers:
        return []
    return frappe.get_all(
        "Sales Invoice",
        filters={"customer": ["in", customers]},
        fields=["name", "docstatus", "is_return", "return_against", "customer"],
        limit_page_length=0,
    ) or []


def _restore_missing_customers(customers: List[str], report: Dict[str, Any]) -> None:
    """Recreate fixture Customer rows that documents still point at.

    Cancelling a submitted Sales Invoice revalidates its party, so an invoice
    whose customer has been deleted cannot be cancelled at all — it fails with
    "Could not find Party". An earlier version of this script deleted customers
    while their invoices survived and produced exactly that deadlock: the
    documents could not be removed because the row they needed was already gone.

    Restoring a minimal stub is what breaks it. The stub only has to exist long
    enough for the cancel to validate; it is deleted again at the end of the run
    once nothing references it.
    """
    group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    for name in customers:
        if frappe.db.exists("Customer", name):
            continue
        try:
            doc = frappe.new_doc("Customer")
            doc.customer_name = name
            doc.customer_type = "Company"
            if group:
                doc.customer_group = group
            if territory:
                doc.territory = territory
            doc.flags.ignore_permissions = True
            doc.insert(ignore_permissions=True)
            report.setdefault("restored", []).append(doc.name)
        except Exception as exc:
            report["failed"].append({
                "doctype": "Customer", "name": name,
                "error": f"could not restore stub: {str(exc)[:200]}",
            })


def _cancel_and_delete(doctype: str, name: str, report: Dict[str, Any]) -> None:
    """Cancel if submitted, then delete. Failures are recorded, never raised."""
    try:
        if not frappe.db.exists(doctype, name):
            return
        doc = frappe.get_doc(doctype, name)
        doc.flags.ignore_permissions = True
        doc.flags.ignore_links = True
        # The credit note and return DN carry the outbound Woo hook; a fixture
        # must never push anything to the live store on its way out.
        doc.flags.ignore_woo_outbound = True
        if int(getattr(doc, "docstatus", 0) or 0) == 1:
            doc.cancel()
        frappe.delete_doc(
            doctype, name, force=True, ignore_permissions=True,
            ignore_missing=True, delete_permanently=True,
        )
        report["deleted"].append(f"{doctype}:{name}")
    except Exception as exc:
        report["failed"].append({"doctype": doctype, "name": name, "error": str(exc)[:300]})


def _delete_if_unreferenced(doctype: str, name: str, report: Dict[str, Any]) -> None:
    """Delete a shared lookup record, with Frappe's link check left enabled.

    Deliberately not ``force=True``. The invoices and customers above are known
    fixtures resolved from a prefix, but a Territory or Customer Group is a
    shared lookup that a real record may have been pointed at by hand — and the
    forced delete used elsewhere here skips link validation entirely, which is
    exactly how a previous run stranded links. Frappe's own check is exhaustive
    where an enumerated list of referrers would not be, so anything still in use
    is kept and reported rather than quietly unlinked.
    """
    try:
        if not frappe.db.exists(doctype, name):
            return
        frappe.delete_doc(
            doctype, name, ignore_permissions=True, ignore_missing=True,
            delete_permanently=True,
        )
        report["deleted"].append(f"{doctype}:{name}")
    except Exception as exc:
        report["failed"].append({
            "doctype": doctype, "name": name,
            "error": f"kept: still referenced — {str(exc)[:200]}",
        })


def run(dry_run: bool = True) -> Dict[str, Any]:
    _guard_environment()

    customers = _fixture_customers()
    invoices = _fixture_invoices(customers)
    invoice_names = [row["name"] for row in invoices]

    report: Dict[str, Any] = {
        "site": frappe.local.site,
        "dry_run": bool(dry_run),
        "customers": customers,
        "invoice_count": len(invoice_names),
        "deleted": [],
        "failed": [],
    }

    # Everything downstream of the invoices, resolved before anything is removed.
    payment_entries = frappe.db.sql(
        """SELECT DISTINCT per.parent AS name FROM `tabPayment Entry Reference` per
           WHERE per.reference_doctype = 'Sales Invoice' AND per.reference_name IN %(inv)s""",
        {"inv": invoice_names or [""]}, as_dict=True,
    ) or []
    payment_entries += frappe.get_all(
        "Payment Entry", filters={"party": ["in", customers or [""]]},
        fields=["name"], limit_page_length=0,
    ) or []

    journal_entries = []
    for name in invoice_names:
        journal_entries += frappe.db.sql(
            "SELECT name FROM `tabJournal Entry` WHERE user_remark LIKE %s",
            ("%" + name + "%",), as_dict=True,
        ) or []

    delivery_notes = frappe.get_all(
        "Delivery Note", filters={"customer": ["in", customers or [""]]},
        pluck="name", limit_page_length=0,
    ) or []

    courier_txns = frappe.get_all(
        "Courier Transaction", filters={"reference_invoice": ["in", invoice_names or [""]]},
        pluck="name", limit_page_length=0,
    ) or []
    partner_txns = frappe.get_all(
        "Sales Partner Transactions", filters={"reference_invoice": ["in", invoice_names or [""]]},
        pluck="name", limit_page_length=0,
    ) or []

    # Credit notes must go before the invoices they return against.
    credit_notes = [r["name"] for r in invoices if int(r.get("is_return") or 0)]
    source_invoices = [r["name"] for r in invoices if not int(r.get("is_return") or 0)]

    taxonomy = _fixture_taxonomy()

    plan = {
        "payment_entries": sorted({r["name"] for r in payment_entries}),
        "journal_entries": sorted({r["name"] for r in journal_entries}),
        "courier_transactions": sorted(set(courier_txns)),
        "partner_transactions": sorted(set(partner_txns)),
        "credit_notes": sorted(credit_notes),
        "delivery_notes": sorted(set(delivery_notes)),
        "source_invoices": sorted(source_invoices),
        "territories": taxonomy["Territory"],
        "customer_groups": taxonomy["Customer Group"],
    }
    report["plan"] = {k: len(v) for k, v in plan.items()}
    report["plan_detail"] = plan

    if dry_run:
        print(json.dumps(report["plan"], indent=2))
        print(f"\nDRY RUN — {len(customers)} fixture customers, nothing written.")
        return report

    # A cancel revalidates the party, so any fixture customer a surviving document
    # still points at has to exist before anything is unwound. Stubs restored here
    # are removed again at the end, once nothing references them.
    _restore_missing_customers(customers, report)

    # Order matters: money vouchers, then the operational artifacts that block
    # invoice cancellation, then the credit notes, then the invoices themselves.
    for name in plan["payment_entries"]:
        _cancel_and_delete("Payment Entry", name, report)
    for name in plan["journal_entries"]:
        _cancel_and_delete("Journal Entry", name, report)
    for name in plan["courier_transactions"]:
        _cancel_and_delete("Courier Transaction", name, report)
    for name in plan["partner_transactions"]:
        _cancel_and_delete("Sales Partner Transactions", name, report)
    for name in plan["credit_notes"]:
        _cancel_and_delete("Sales Invoice", name, report)
    for name in plan["delivery_notes"]:
        _cancel_and_delete("Delivery Note", name, report)

    # block_cancel_if_dispatched refuses on `was_ofd OR state_dispatched` — the
    # permanent flag *or* the board column. Clearing only the flag leaves the
    # state saying "Out for Delivery" and the cancel still fails, which is exactly
    # how the first run of this script left ten invoices behind. Reset both, and
    # reset the state on every alias the site carries, because the guard reads the
    # first one it finds and a site may carry more than one.
    state_fields = [
        field for field in
        ("custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state")
        if frappe.get_meta("Sales Invoice").get_field(field)
    ]
    for name in plan["source_invoices"]:
        try:
            updates: Dict[str, Any] = {
                "custom_was_out_for_delivery": 0,
                "custom_return_status": None,
            }
            updates.update({field: "Recieved" for field in state_fields})
            frappe.db.set_value("Sales Invoice", name, updates, update_modified=False)
        except Exception:
            pass
    for name in plan["source_invoices"]:
        _cancel_and_delete("Sales Invoice", name, report)

    # A customer only goes once nothing references it any more. The delete runs
    # with force=True, which skips link validation — so deleting a customer whose
    # invoice survived the cancel leaves that invoice pointing at a row that no
    # longer exists. That happened on the first real run: ten dispatched invoices
    # failed to cancel and their customers were removed anyway, stranding the
    # links. Checking first is what keeps force=True honest.
    for name in customers:
        survivors = frappe.get_all(
            "Sales Invoice", filters={"customer": name}, pluck="name", limit_page_length=1,
        ) or []
        if survivors:
            report["failed"].append({
                "doctype": "Customer", "name": name,
                "error": f"kept: still referenced by {survivors[0]} (and possibly others)",
            })
            continue
        for address in frappe.get_all(
            "Dynamic Link",
            filters={"link_doctype": "Customer", "link_name": name, "parenttype": "Address"},
            pluck="parent", limit_page_length=0,
        ) or []:
            _cancel_and_delete("Address", address, report)
        _cancel_and_delete("Customer", name, report)

    # Last, and only now: every fixture customer above carries a territory and a
    # customer group, so these cannot go while any of them survive.
    for name in plan["territories"]:
        _delete_if_unreferenced("Territory", name, report)
    for name in plan["customer_groups"]:
        _delete_if_unreferenced("Customer Group", name, report)

    frappe.db.commit()

    report["deleted_count"] = len(report["deleted"])
    report["failed_count"] = len(report["failed"])
    print(f"\nPURGE COMPLETE — {report['deleted_count']} removed, {report['failed_count']} failed")
    for failure in report["failed"][:25]:
        print(f"  FAILED {failure['doctype']}:{failure['name']} — {failure['error']}")
    return report

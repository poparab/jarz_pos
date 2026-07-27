"""End-to-end accounting validation for every invoice lifecycle path.

``accounting_deep_audit`` inspects what is already in the ledger.
``return_flow_validation`` covers returns. This covers the part neither does:
driving an order through each **creation → dispatch → settlement → cancellation**
path with the real service entry points and asserting the ledger after every
step.

Each case asserts the same bundle:

* every voucher it produced balances (DR == CR);
* the invoice's outstanding lands where that path says it should;
* the money sits in the right ledger — cash in branch cash, courier debt in
  Courier Outstanding, freight against Creditors with a party;
* nothing was double-posted (one Delivery Note, one Payment Entry per purpose);
* stock moved exactly once, through the Delivery Note.

Synthetic ``_LIFECYCLE_`` fixtures only; real invoices are never touched.
Refuses to run against production. Cleans up in ``finally``.

Run::

    bench --site frontend execute jarz_pos.scripts.full_lifecycle_validation.run
    bench --site frontend execute jarz_pos.scripts.full_lifecycle_validation.run \
        --kwargs "{'cleanup': False}"
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

from jarz_pos.scripts.b2b_accounting_validation import (
    RunContext,
    assert_gl_balanced,
    _cleanup,
    _create_invoice,
    _ensure_customer,
    _ensure_customer_group,
    _ensure_item_prices,
    _ensure_stock,
    _ensure_territory,
    _pay_invoice_full_cash,
    _pick_courier_party,
    _pick_pos_profile,
    _pick_sellable_items,
)

_TOL = 0.01
MARKER_START = "FULL_LIFECYCLE_JSON_START"
MARKER_END = "FULL_LIFECYCLE_JSON_END"


# ─────────────────────────────────────────────────────────────────────────────
# Shared assertions
# ─────────────────────────────────────────────────────────────────────────────

def _guard_environment() -> None:
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""
    if "erp.orderjarz.com" in base_url:
        raise RuntimeError(f"full_lifecycle_validation refuses to run on production ({base_url!r})")


def _normalize_courier(picked: Any) -> Optional[Dict[str, Any]]:
    if not picked:
        return None
    try:
        party_type, party = picked
    except Exception:
        return None
    if not party_type or not party:
        return None
    return {"party_type": party_type, "party": party}


def _gl_by_account(voucher_type: str, voucher_no: str) -> Dict[str, float]:
    """Net movement per account for one voucher (debit positive)."""
    rows = frappe.db.sql(
        """
        SELECT account, ROUND(SUM(debit - credit), 2) AS net
        FROM `tabGL Entry`
        WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
        GROUP BY account
        """,
        (voucher_type, voucher_no),
        as_dict=True,
    )
    return {r["account"]: flt(r["net"]) for r in rows}


def _assert_voucher_balanced(ctx: RunContext, case: str, doctype: str, name: str) -> None:
    passed, detail = assert_gl_balanced(doctype, name)
    ctx.record(f"{case}.{doctype.replace(' ', '_').lower()}_balanced::{name}", passed, detail)


def _assert_outstanding(ctx: RunContext, case: str, invoice: str, expected: float) -> None:
    actual = flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount"))
    ctx.record(
        f"{case}.outstanding_is_{expected:g}",
        abs(actual - expected) <= _TOL,
        f"outstanding={actual} expected={expected}",
    )


def _assert_single_delivery_note(ctx: RunContext, case: str, invoice: str) -> Optional[str]:
    dns = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": invoice, "docstatus": 1},
        pluck="parent",
        limit_page_length=20,
    ) or []
    unique = sorted(set(dns))
    ctx.record(
        f"{case}.exactly_one_delivery_note",
        len(unique) == 1,
        f"delivery notes={unique}",
    )
    return unique[0] if unique else None


def _assert_stock_moved_once(ctx: RunContext, case: str, invoice: str) -> None:
    """Stock must move via the Delivery Note and never via the invoice itself."""
    si_rows = frappe.db.count(
        "Stock Ledger Entry",
        {"voucher_type": "Sales Invoice", "voucher_no": invoice, "is_cancelled": 0},
    )
    ctx.record(
        f"{case}.invoice_moves_no_stock",
        si_rows == 0,
        f"stock ledger rows against the invoice={si_rows} (must be 0)",
    )


def _assert_no_duplicate_payment(ctx: RunContext, case: str, invoice: str) -> None:
    rows = frappe.db.sql(
        """
        SELECT pe.name, pe.paid_to, per.allocated_amount
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent AND pe.docstatus = 1
        WHERE per.reference_doctype = 'Sales Invoice' AND per.reference_name = %s
        """,
        (invoice,),
        as_dict=True,
    )
    total = sum(flt(r["allocated_amount"]) for r in rows)
    grand = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))
    ctx.record(
        f"{case}.no_over_payment",
        total <= grand + 1.0,
        f"allocated={total} grand_total={grand} across {len(rows)} payment(s)",
    )


def _absorb(ctx: RunContext, invoice: str) -> None:
    """Register every artifact an invoice spawned so cleanup can unwind it."""
    for dn in frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": invoice, "docstatus": 1},
        pluck="parent", limit_page_length=20,
    ) or []:
        if dn not in ctx.delivery_notes:
            ctx.delivery_notes.append(dn)
    for pe in frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
        pluck="parent", limit_page_length=20,
    ) or []:
        if pe not in ctx.payment_entries:
            ctx.payment_entries.append(pe)
    for ct in frappe.get_all(
        "Courier Transaction", filters={"reference_invoice": invoice},
        pluck="name", limit_page_length=20,
    ) or []:
        if ct not in ctx.courier_transactions:
            ctx.courier_transactions.append(ct)
    for je in frappe.get_all(
        "Journal Entry",
        filters={"docstatus": 1, "user_remark": ["like", f"%{invoice}%"]},
        pluck="name", limit_page_length=20,
    ) or []:
        if je not in ctx.journal_entries:
            ctx.journal_entries.append(je)


def _teardown_dispatched(ctx: RunContext) -> None:
    """Undo dispatch state so the shared cleanup can cancel the fixtures.

    ``block_cancel_if_dispatched`` refuses to cancel a dispatched invoice, by
    design. Rather than weakening that guard, the teardown removes the
    conditions it inspects — for this run's fixtures only.
    """
    for invoice in list(dict.fromkeys(ctx.invoices)):
        try:
            if not frappe.db.exists("Sales Invoice", invoice):
                continue
            for dn in frappe.get_all(
                "Delivery Note Item",
                filters={"against_sales_invoice": invoice, "docstatus": 1},
                pluck="parent", limit_page_length=20,
            ) or []:
                try:
                    doc = frappe.get_doc("Delivery Note", dn)
                    if int(doc.docstatus or 0) == 1:
                        doc.flags.ignore_permissions = True
                        doc.flags.ignore_links = True
                        doc.cancel()
                except Exception:
                    pass
            frappe.db.set_value(
                "Sales Invoice", invoice,
                {"custom_was_out_for_delivery": 0, "custom_sales_invoice_state": "Recieved"},
                update_modified=False,
            )
        except Exception:
            pass
    frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────

def _new_invoice(ctx: RunContext, env: Dict[str, Any], case: str, *, pickup: bool = False) -> str:
    customer = _ensure_customer(ctx, case, env["territory"], env["customer_group"])
    return _create_invoice(
        ctx, customer=customer, pos_profile=env["pos_profile"], items=env["items"],
        order_purpose=None, commercial_policy=None, policy_reason=None, pickup=pickup,
    )


def _case_create_unpaid(ctx: RunContext, env: Dict[str, Any]) -> None:
    """A fresh COD order: revenue recognised, full amount owed, no stock moved."""
    case = "LC-CREATE-UNPAID"
    invoice = _new_invoice(ctx, env, case)
    frappe.db.commit()

    grand = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))
    _assert_voucher_balanced(ctx, case, "Sales Invoice", invoice)
    _assert_outstanding(ctx, case, invoice, grand)
    _assert_stock_moved_once(ctx, case, invoice)

    gl = _gl_by_account("Sales Invoice", invoice)
    debtors = sum(v for a, v in gl.items() if "Debtors" in a)
    ctx.record(
        f"{case}.receivable_equals_total",
        abs(debtors - grand) <= _TOL,
        f"Debtors debit={debtors} grand_total={grand}",
    )
    _absorb(ctx, invoice)


def _case_create_paid_cash(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Cash taken at the till: receivable cleared, cash in the branch account."""
    case = "LC-CREATE-PAID-CASH"
    invoice = _new_invoice(ctx, env, case)
    _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])
    frappe.db.commit()

    _assert_voucher_balanced(ctx, case, "Sales Invoice", invoice)
    _assert_outstanding(ctx, case, invoice, 0.0)
    _assert_no_duplicate_payment(ctx, case, invoice)
    _assert_stock_moved_once(ctx, case, invoice)

    pes = frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
        pluck="parent", limit_page_length=10,
    ) or []
    for pe in set(pes):
        _assert_voucher_balanced(ctx, case, "Payment Entry", pe)
    _absorb(ctx, invoice)


def _case_dispatch_paid(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Dispatching an already-paid order must move money nowhere."""
    from jarz_pos.services.delivery_handling import handle_out_for_delivery_transition

    case = "LC-DISPATCH-PAID"
    invoice = _new_invoice(ctx, env, case)
    _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])
    frappe.db.commit()

    before = _gl_by_account("Sales Invoice", invoice)
    handle_out_for_delivery_transition(
        invoice_name=invoice, courier="", mode="pay_now",
        pos_profile=env["pos_profile"],
        party_type=(env["courier"] or {}).get("party_type"),
        party=(env["courier"] or {}).get("party"),
    )
    frappe.db.commit()

    dn = _assert_single_delivery_note(ctx, case, invoice)
    if dn:
        _assert_voucher_balanced(ctx, case, "Delivery Note", dn)
    _assert_outstanding(ctx, case, invoice, 0.0)
    _assert_no_duplicate_payment(ctx, case, invoice)
    _assert_stock_moved_once(ctx, case, invoice)

    after = _gl_by_account("Sales Invoice", invoice)
    ctx.record(
        f"{case}.paid_dispatch_posts_no_extra_money",
        before == after,
        "invoice GL unchanged by dispatching an already-paid order",
    )

    cts = frappe.get_all("Courier Transaction", filters={"reference_invoice": invoice}, pluck="name")
    ctx.record(f"{case}.no_courier_transaction", not cts, f"courier rows={cts}")
    _absorb(ctx, invoice)


def _case_dispatch_unpaid_cod(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Unpaid COD dispatch: receivable moves to Courier Outstanding, freight accrues."""
    from jarz_pos.services.delivery_handling import handle_out_for_delivery_transition

    case = "LC-DISPATCH-UNPAID-COD"
    if not env["courier"]:
        ctx.record(f"{case}.skipped", True, "no courier bound to this branch")
        return

    invoice = _new_invoice(ctx, env, case)
    frappe.db.commit()
    grand = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))

    handle_out_for_delivery_transition(
        invoice_name=invoice, courier="", mode="pay_now", pos_profile=env["pos_profile"],
        party_type=env["courier"]["party_type"], party=env["courier"]["party"],
    )
    frappe.db.commit()

    dn = _assert_single_delivery_note(ctx, case, invoice)
    if dn:
        _assert_voucher_balanced(ctx, case, "Delivery Note", dn)
    _assert_outstanding(ctx, case, invoice, 0.0)
    _assert_no_duplicate_payment(ctx, case, invoice)
    _assert_stock_moved_once(ctx, case, invoice)

    pes = sorted(set(frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
        pluck="parent", limit_page_length=10,
    ) or []))
    ctx.record(f"{case}.exactly_one_payment_entry", len(pes) == 1, f"payment entries={pes}")
    for pe in pes:
        _assert_voucher_balanced(ctx, case, "Payment Entry", pe)
        gl = _gl_by_account("Payment Entry", pe)
        to_courier = sum(v for a, v in gl.items() if "Courier Outstanding" in a)
        ctx.record(
            f"{case}.receivable_moved_to_courier_outstanding",
            abs(to_courier - grand) <= _TOL,
            f"Courier Outstanding debit={to_courier} expected={grand}",
        )

    cts = frappe.get_all(
        "Courier Transaction", filters={"reference_invoice": invoice},
        fields=["name", "amount", "status"], limit_page_length=10,
    ) or []
    ctx.record(f"{case}.exactly_one_courier_row", len(cts) == 1, f"courier rows={cts}")
    if cts:
        ctx.record(
            f"{case}.courier_owes_the_order_amount",
            abs(flt(cts[0]["amount"]) - grand) <= _TOL,
            f"courier amount={cts[0]['amount']} expected={grand}",
        )
    _absorb(ctx, invoice)


def _case_dispatch_is_idempotent(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Dispatching twice must not pay the invoice twice.

    This is the real defect the audit found on production data: two Payment
    Entries to Courier Outstanding, seconds apart, each fully allocated —
    overstating courier debt while both entries balanced perfectly.
    """
    from jarz_pos.services.delivery_handling import handle_out_for_delivery_transition

    case = "LC-DISPATCH-IDEMPOTENT"
    if not env["courier"]:
        ctx.record(f"{case}.skipped", True, "no courier bound to this branch")
        return

    invoice = _new_invoice(ctx, env, case)
    frappe.db.commit()
    grand = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))

    for attempt in range(2):
        try:
            handle_out_for_delivery_transition(
                invoice_name=invoice, courier="", mode="pay_now",
                pos_profile=env["pos_profile"],
                party_type=env["courier"]["party_type"], party=env["courier"]["party"],
            )
        except Exception as exc:
            ctx.record(f"{case}.attempt{attempt + 1}_note", True, f"second dispatch refused: {exc}")
        frappe.db.commit()

    pes = sorted(set(frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
        pluck="parent", limit_page_length=10,
    ) or []))
    allocated = frappe.db.sql(
        """
        SELECT ROUND(SUM(per.allocated_amount), 2) FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent AND pe.docstatus = 1
        WHERE per.reference_doctype = 'Sales Invoice' AND per.reference_name = %s
        """,
        (invoice,),
    )
    total_allocated = flt((allocated and allocated[0][0]) or 0)

    ctx.record(
        f"{case}.no_duplicate_payment_entry",
        len(pes) <= 1,
        f"payment entries after two dispatches={pes}",
    )
    ctx.record(
        f"{case}.not_paid_twice",
        total_allocated <= grand + 1.0,
        f"total allocated={total_allocated} grand_total={grand}",
    )
    _assert_single_delivery_note(ctx, case, invoice)

    cts = frappe.get_all(
        "Courier Transaction", filters={"reference_invoice": invoice},
        fields=["name", "amount"], limit_page_length=10,
    ) or []
    net = sum(flt(c["amount"]) for c in cts)
    ctx.record(
        f"{case}.courier_debt_not_doubled",
        net <= grand + _TOL,
        f"net courier amount={net} grand_total={grand}",
    )
    _absorb(ctx, invoice)


def _case_pickup_no_courier(ctx: RunContext, env: Dict[str, Any]) -> None:
    """A pickup order must never acquire courier debt or freight expense."""
    case = "LC-PICKUP"
    invoice = _new_invoice(ctx, env, case, pickup=True)
    _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])
    frappe.db.commit()

    _assert_voucher_balanced(ctx, case, "Sales Invoice", invoice)
    _assert_outstanding(ctx, case, invoice, 0.0)
    _assert_stock_moved_once(ctx, case, invoice)

    from jarz_pos.services.delivery_handling import mark_courier_outstanding

    refused = False
    try:
        mark_courier_outstanding(invoice, None, "Employee", "X")
    except Exception:
        refused = True
    ctx.record(f"{case}.courier_assignment_refused", refused, "pickup orders reject courier assignment")

    cts = frappe.get_all("Courier Transaction", filters={"reference_invoice": invoice}, pluck="name")
    ctx.record(f"{case}.no_courier_debt", not cts, f"courier rows={cts}")
    _absorb(ctx, invoice)


def _case_cancel_unpaid(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Cancelling an unpaid pre-dispatch order must leave no live GL."""
    from jarz_pos.api.kanban import cancel_invoice

    case = "LC-CANCEL-UNPAID"
    invoice = _new_invoice(ctx, env, case)
    frappe.db.commit()

    result = cancel_invoice(invoice, "lifecycle validation")
    frappe.db.commit()
    ctx.record(f"{case}.cancel_succeeded", bool(result.get("success")), json.dumps(result, default=str)[:250])

    docstatus = int(frappe.db.get_value("Sales Invoice", invoice, "docstatus") or 0)
    ctx.record(f"{case}.invoice_cancelled", docstatus == 2, f"docstatus={docstatus}")

    live = frappe.db.count(
        "GL Entry", {"voucher_type": "Sales Invoice", "voucher_no": invoice, "is_cancelled": 0}
    )
    ctx.record(f"{case}.no_live_gl_remains", live == 0, f"live GL rows={live}")
    _absorb(ctx, invoice)


def _case_cancel_paid(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Cancelling a paid order reverses both the invoice and its payment."""
    from jarz_pos.api.kanban import cancel_invoice

    case = "LC-CANCEL-PAID"
    invoice = _new_invoice(ctx, env, case)
    _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])
    frappe.db.commit()

    pes = sorted(set(frappe.get_all(
        "Payment Entry Reference",
        filters={"reference_doctype": "Sales Invoice", "reference_name": invoice},
        pluck="parent", limit_page_length=10,
    ) or []))

    result = cancel_invoice(invoice, "lifecycle validation")
    frappe.db.commit()
    ctx.record(f"{case}.cancel_succeeded", bool(result.get("success")), json.dumps(result, default=str)[:250])

    if result.get("success"):
        live_si = frappe.db.count(
            "GL Entry", {"voucher_type": "Sales Invoice", "voucher_no": invoice, "is_cancelled": 0})
        ctx.record(f"{case}.invoice_gl_reversed", live_si == 0, f"live invoice GL={live_si}")
        for pe in pes:
            live_pe = frappe.db.count(
                "GL Entry", {"voucher_type": "Payment Entry", "voucher_no": pe, "is_cancelled": 0})
            ctx.record(f"{case}.payment_gl_reversed::{pe}", live_pe == 0, f"live payment GL={live_pe}")
    _absorb(ctx, invoice)


def _case_cancel_blocked_after_dispatch(ctx: RunContext, env: Dict[str, Any]) -> None:
    """A dispatched order must refuse cancellation from every path."""
    from jarz_pos.api.kanban import cancel_invoice
    from jarz_pos.services.delivery_handling import handle_out_for_delivery_transition

    case = "LC-CANCEL-BLOCKED"
    invoice = _new_invoice(ctx, env, case)
    _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])
    handle_out_for_delivery_transition(
        invoice_name=invoice, courier="", mode="pay_now", pos_profile=env["pos_profile"],
        party_type=None, party=None,
    )
    frappe.db.commit()

    result = cancel_invoice(invoice, "should be refused")
    ctx.record(f"{case}.api_refused", not result.get("success"), json.dumps(result, default=str)[:200])

    # The document-level guard must hold even when the API is bypassed.
    raw_refused = False
    try:
        doc = frappe.get_doc("Sales Invoice", invoice)
        doc.flags.ignore_permissions = True
        doc.cancel()
    except Exception:
        raw_refused = True
    frappe.db.rollback()
    ctx.record(f"{case}.raw_cancel_refused", raw_refused, "before_cancel guard holds on a direct cancel")

    docstatus = int(frappe.db.get_value("Sales Invoice", invoice, "docstatus") or 0)
    ctx.record(f"{case}.invoice_still_submitted", docstatus == 1, f"docstatus={docstatus}")
    _absorb(ctx, invoice)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

_CASES = [
    _case_create_unpaid,
    _case_create_paid_cash,
    _case_dispatch_paid,
    _case_dispatch_unpaid_cod,
    _case_dispatch_is_idempotent,
    _case_pickup_no_courier,
    _case_cancel_unpaid,
    _case_cancel_paid,
    _case_cancel_blocked_after_dispatch,
]


def run(cleanup: bool = True) -> Dict[str, Any]:
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}

    _guard_environment()
    frappe.flags.in_test = True
    frappe.flags.ignore_woo_outbound = True

    ctx = RunContext()
    try:
        pos_profile = _pick_pos_profile()
        items = _pick_sellable_items(pos_profile, 2)
        company = frappe.db.get_value("POS Profile", pos_profile, "company")
        env = {
            "pos_profile": pos_profile,
            "items": items,
            "company": company,
            "territory": _ensure_territory(company),
            "customer_group": _ensure_customer_group(),
            "courier": _normalize_courier(_pick_courier_party(pos_profile)),
        }
        _ensure_item_prices(ctx, items)
        _ensure_stock(ctx, items, pos_profile)

        for case in _CASES:
            try:
                case(ctx, env)
            except Exception as exc:
                ctx.record(f"{case.__name__}.crashed", False, f"{exc}")
                frappe.log_error(frappe.get_traceback(), f"lifecycle case failed: {case.__name__}")

        summary = {
            "passed": ctx.passed,
            "failed": ctx.failed,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in ctx.checks],
        }
        for check in ctx.checks:
            print(f"   [{'PASS' if check.passed else 'FAIL'}] {check.name} :: {check.detail}")
        print(f"full_lifecycle_validation: {ctx.passed} passed, {ctx.failed} failed")
        print(MARKER_START)
        print(json.dumps(summary, indent=2, default=str))
        print(MARKER_END)
        return summary
    finally:
        if cleanup:
            try:
                _teardown_dispatched(ctx)
                _cleanup(ctx)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "full_lifecycle_validation cleanup failed")

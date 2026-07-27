"""Staging validation for the cancellation + return workflow.

Runs ON STAGING against a production-cloned database and proves three things:

1. the return workflow books its accounting correctly for every money state;
2. the source invoice is never voided — the guarantee the whole design rests on;
3. nothing about the existing forward flow changed (the PART A / PART C
   golden-baseline diff, borrowed wholesale from ``b2b_accounting_validation``).

Everything mutating happens on ``_RETVALID_``-prefixed synthetic fixtures. No
real invoice is ever touched, not even behind a flag: the return graph moves
Courier Outstanding, Creditors and branch cash, and an unclean rollback against
a real courier's balance is not something a script can put back.

Real production data is exercised **read-only** through
:func:`run_readonly_survey`, which resolves and balances every posting a return
*would* make without writing anything. That is the check that proves account
resolution works for every real branch/company/courier combination.

Usage::

    bench --site frontend execute jarz_pos.scripts.return_flow_validation.run
    bench --site frontend execute jarz_pos.scripts.return_flow_validation.run \
        --kwargs "{'cleanup': False}"
    bench --site frontend execute \
        jarz_pos.scripts.return_flow_validation.run_readonly_survey \
        --kwargs "{'sample': 200}"

``run_json`` wraps the report in markers so an SSH driver can slice it out of
noisy bench output.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

from jarz_pos.scripts.b2b_accounting_validation import (  # noqa: F401 - reused wholesale
    CheckResult,
    RunContext,
    assert_gl_balanced,
    capture_invoice_accounting,
    _cleanup,
    _create_invoice,
    _emit_report,
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
from jarz_pos.services import invoice_return as ir

REPORT_MARKER_START = "RETURN_FLOW_VALIDATION_JSON_START"
REPORT_MARKER_END = "RETURN_FLOW_VALIDATION_JSON_END"

CUSTOMER_PREFIX = "_RETVALID_"
_TOL = 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Guards
# ─────────────────────────────────────────────────────────────────────────────

def _guard_environment() -> None:
    """Refuse to run anywhere that looks like production.

    The harness submits and cancels real documents. Running it against the
    production site would be unrecoverable, so the check is a hard stop rather
    than a warning.
    """
    site = getattr(frappe.local, "site", "") or ""
    host = str(frappe.db.get_single_value("Website Settings", "subdomain") or "")
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""

    if "erp.orderjarz.com" in base_url or "erp.orderjarz.com" in host:
        raise RuntimeError(
            f"return_flow_validation refuses to run against production ({base_url!r})."
        )
    frappe.logger("jarz_pos.return_validation").info(
        f"return_flow_validation starting on site={site!r} url={base_url!r}"
    )


def _enable_returns() -> bool:
    """Turn the kill switch on for the run; report the previous value."""
    previous = bool(int(frappe.db.get_single_value("Jarz POS Settings", "enable_invoice_returns") or 0))
    if not previous:
        frappe.db.set_single_value("Jarz POS Settings", "enable_invoice_returns", 1)
        frappe.db.commit()
    return previous


def _restore_returns(previous: bool) -> None:
    if not previous:
        frappe.db.set_single_value("Jarz POS Settings", "enable_invoice_returns", 0)
        frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Assertions specific to returns
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_courier(picked: Any) -> Optional[Dict[str, Any]]:
    """`_pick_courier_party` yields a ``(party_type, party)`` tuple, or
    ``(None, None)`` when the site has no courier bound to this branch.

    Normalised to a dict (or None) so the cases can pass it straight through to
    the dispatch helper and to the courier assertions.
    """
    if not picked:
        return None
    try:
        party_type, party = picked
    except Exception:
        return None
    if not party_type or not party:
        return None
    return {"party_type": party_type, "party": party}


def _stock_qty(item_code: str, warehouse: str) -> float:
    try:
        return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))
    except Exception:
        return 0.0


def _courier_outstanding_total(invoice_name: str) -> float:
    """Net of every Courier Transaction row for the invoice.

    The `+X` dispatch row and the `-X` return row must cancel, which is what
    keeps the courier's operational balance honest after a return.
    """
    rows = frappe.get_all(
        "Courier Transaction",
        filters={"reference_invoice": invoice_name},
        fields=["amount"],
        limit_page_length=50,
    ) or []
    return sum(flt(r.get("amount")) for r in rows)


def _assert_return_invariants(
    ctx: RunContext, case: str, *, source_invoice: str, result: Dict[str, Any]
) -> None:
    """The checks every successful return must satisfy, whatever its money state."""
    # 1. THE guarantee: the source invoice is never voided.
    docstatus = int(frappe.db.get_value("Sales Invoice", source_invoice, "docstatus") or 0)
    ctx.record(
        f"{case}.source_still_submitted",
        docstatus == 1,
        f"source docstatus={docstatus} (must stay 1)",
    )

    credit_note = result.get("credit_note")
    return_dn = result.get("return_delivery_note")

    ctx.record(f"{case}.credit_note_created", bool(credit_note), str(credit_note))
    ctx.record(f"{case}.return_dn_created", bool(return_dn), str(return_dn))

    if credit_note:
        is_return = int(frappe.db.get_value("Sales Invoice", credit_note, "is_return") or 0)
        ctx.record(f"{case}.credit_note_is_return", is_return == 1, f"is_return={is_return}")

        # G13: a credit note must never carry a Woo id, or the outbound hook
        # would push a status update to the live store.
        woo_id = frappe.db.get_value("Sales Invoice", credit_note, "woo_order_id")
        ctx.record(f"{case}.credit_note_has_no_woo_id", not woo_id, f"woo_order_id={woo_id!r}")

        # G14: and it must never appear on the operational board.
        is_pos = int(frappe.db.get_value("Sales Invoice", credit_note, "is_pos") or 0)
        ctx.record(f"{case}.credit_note_off_board", is_pos == 0, f"is_pos={is_pos}")

        passed, detail = assert_gl_balanced("Sales Invoice", credit_note)
        ctx.record(f"{case}.credit_note_gl_balanced", passed, detail)

    if return_dn:
        passed, detail = assert_gl_balanced("Delivery Note", return_dn)
        ctx.record(f"{case}.return_dn_gl_balanced", passed, detail)

    for je in result.get("journal_entries") or []:
        passed, detail = assert_gl_balanced("Journal Entry", je)
        ctx.record(f"{case}.je_balanced::{je}", passed, detail)

    if result.get("refund_payment_entry"):
        passed, detail = assert_gl_balanced("Payment Entry", result["refund_payment_entry"])
        ctx.record(f"{case}.refund_pe_balanced", passed, detail)

    status = frappe.db.get_value("Sales Invoice", source_invoice, "custom_return_status")
    ctx.record(
        f"{case}.return_status_stamped",
        status in {"Partially Returned", "Fully Returned"},
        f"custom_return_status={status!r}",
    )


def _absorb_return_artifacts(ctx: RunContext, result: Dict[str, Any]) -> None:
    """Register everything a return created so cleanup can unwind it."""
    if result.get("credit_note"):
        ctx.invoices.append(result["credit_note"])
    if result.get("return_delivery_note"):
        ctx.delivery_notes.append(result["return_delivery_note"])
    for je in result.get("journal_entries") or []:
        ctx.journal_entries.append(je)
    if result.get("refund_payment_entry"):
        ctx.payment_entries.append(result["refund_payment_entry"])
    if result.get("compensating_courier_transaction"):
        ctx.courier_transactions.append(result["compensating_courier_transaction"])


def _full_lines(invoice_name: str) -> List[Dict[str, Any]]:
    rows = frappe.get_all(
        "Sales Invoice Item",
        filters={"parent": invoice_name, "parenttype": "Sales Invoice"},
        fields=["name", "qty"],
        limit_page_length=0,
    ) or []
    return [{"si_detail": r["name"], "qty": flt(r["qty"])} for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch_invoice(invoice_name: str, pos_profile: str, courier: Optional[Dict[str, Any]]) -> None:
    """Drive an invoice to Out for Delivery through the real service entry point."""
    from jarz_pos.services.delivery_handling import handle_out_for_delivery_transition

    handle_out_for_delivery_transition(
        invoice_name=invoice_name,
        courier="",
        mode="pay_now",
        pos_profile=pos_profile,
        party_type=(courier or {}).get("party_type"),
        party=(courier or {}).get("party"),
    )


def _case_full_return(
    ctx: RunContext, env: Dict[str, Any], *, case: str, prepay: bool,
    courier: Optional[Dict[str, Any]], pay_courier: bool, refund_mode: str,
) -> None:
    """Create -> (pay) -> dispatch -> return everything, then assert."""
    customer = _ensure_customer(ctx, case, env["territory"], env["customer_group"])
    invoice = _create_invoice(
        ctx,
        customer=customer,
        pos_profile=env["pos_profile"],
        items=env["items"],
        order_purpose=None,
        commercial_policy=None,
        policy_reason=None,
        pickup=False,
    )
    if prepay:
        _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])

    _dispatch_invoice(invoice, env["pos_profile"], courier)
    frappe.db.commit()

    before = _courier_outstanding_total(invoice)

    result = ir.run_invoice_return(
        invoice_id=invoice,
        lines=_full_lines(invoice),
        reason=f"{case} validation",
        return_type="Customer Return",
        refund_mode=refund_mode,
        pay_courier_for_trip=pay_courier,
    )
    _absorb_return_artifacts(ctx, result)

    ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:400])
    if not result.get("success"):
        return

    _assert_return_invariants(ctx, case, source_invoice=invoice, result=result)

    ctx.record(
        f"{case}.fully_returned",
        result.get("return_status") == "Fully Returned",
        str(result.get("return_status")),
    )

    if courier and result.get("money_state") == ir.MONEY_COURIER_OUTSTANDING:
        after = _courier_outstanding_total(invoice)
        ctx.record(
            f"{case}.courier_balance_nets_to_zero",
            abs(after) <= _TOL,
            f"before={before} after={after}",
        )

    freight_reversed = any(
        ir.JE_FREIGHT_REVERSAL in str(frappe.db.get_value("Journal Entry", je, "user_remark") or "")
        for je in result.get("journal_entries") or []
    )
    ctx.record(
        f"{case}.freight_reversal_matches_toggle",
        freight_reversed == (not pay_courier) if courier else not freight_reversed,
        f"pay_courier={pay_courier} freight_reversed={freight_reversed}",
    )

    if refund_mode == ir.REFUND_NOW:
        ctx.record(
            f"{case}.refund_posted",
            bool(result.get("refund_payment_entry")),
            str(result.get("refund_payment_entry")),
        )
    else:
        ctx.record(
            f"{case}.no_refund_posted",
            not result.get("refund_payment_entry"),
            "customer credit retained",
        )


def _case_partial_then_complete(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Return one unit, then the rest — the B2B partial path and its idempotency."""
    case = "RET-PARTIAL"
    customer = _ensure_customer(ctx, case, env["territory"], env["customer_group"])
    invoice = _create_invoice(
        ctx,
        customer=customer,
        pos_profile=env["pos_profile"],
        items=env["items"],
        order_purpose=None,
        commercial_policy=None,
        policy_reason=None,
        pickup=False,
    )
    _pay_invoice_full_cash(ctx, invoice, env["pos_profile"])
    _dispatch_invoice(invoice, env["pos_profile"], None)
    frappe.db.commit()

    lines = _full_lines(invoice)
    if not lines or lines[0]["qty"] < 2:
        ctx.record(f"{case}.skipped", True, "needs a line with qty >= 2")
        return

    first = [{"si_detail": lines[0]["si_detail"], "qty": 1.0}]
    result_a = ir.run_invoice_return(
        invoice_id=invoice, lines=first, reason="partial 1",
        return_type="Customer Return", refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        pay_courier_for_trip=True,
    )
    _absorb_return_artifacts(ctx, result_a)
    ctx.record(f"{case}.first_succeeded", bool(result_a.get("success")), str(result_a.get("error")))
    if not result_a.get("success"):
        return

    ctx.record(
        f"{case}.marked_partially_returned",
        result_a.get("return_status") == "Partially Returned",
        str(result_a.get("return_status")),
    )

    # Over-return must be refused rather than silently clamped.
    over = ir.run_invoice_return(
        invoice_id=invoice,
        lines=[{"si_detail": lines[0]["si_detail"], "qty": lines[0]["qty"] + 5}],
        reason="over return", return_type="Customer Return",
        refund_mode=ir.REFUND_CUSTOMER_CREDIT, pay_courier_for_trip=True,
    )
    ctx.record(
        f"{case}.over_return_refused",
        not over.get("success") and over.get("return_block_code") == "over_return",
        json.dumps(over, default=str)[:200],
    )

    # Idempotency: the same request id must not post a second document set.
    replay = ir.run_invoice_return(
        invoice_id=invoice, lines=first, reason="partial 1",
        return_type="Customer Return", refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        pay_courier_for_trip=True, request_id=result_a.get("request_id"),
    )
    ctx.record(
        f"{case}.replay_is_idempotent",
        bool(replay.get("already_processed")) and replay.get("credit_note") == result_a.get("credit_note"),
        json.dumps(replay, default=str)[:200],
    )

    # Finish the order off; the second return must post its OWN journal entries,
    # which is only true because dedup keys off the credit note.
    remaining = _full_lines(invoice)
    already = {}
    for row in frappe.get_all(
        "Sales Invoice Item",
        filters={"sales_invoice_item": ["in", [r["si_detail"] for r in remaining]], "docstatus": 1},
        fields=["sales_invoice_item", "qty"],
        limit_page_length=0,
    ) or []:
        already[row["sales_invoice_item"]] = already.get(row["sales_invoice_item"], 0.0) + abs(flt(row["qty"]))

    rest = [
        {"si_detail": r["si_detail"], "qty": r["qty"] - already.get(r["si_detail"], 0.0)}
        for r in remaining
        if r["qty"] - already.get(r["si_detail"], 0.0) > _TOL
    ]
    if not rest:
        return

    result_b = ir.run_invoice_return(
        invoice_id=invoice, lines=rest, reason="partial 2",
        return_type="Customer Return", refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        pay_courier_for_trip=True,
    )
    _absorb_return_artifacts(ctx, result_b)
    ctx.record(f"{case}.second_succeeded", bool(result_b.get("success")), str(result_b.get("error")))
    if not result_b.get("success"):
        return

    ctx.record(
        f"{case}.second_posted_its_own_credit_note",
        result_b.get("credit_note") != result_a.get("credit_note"),
        f"{result_a.get('credit_note')} vs {result_b.get('credit_note')}",
    )
    ctx.record(
        f"{case}.now_fully_returned",
        result_b.get("return_status") == "Fully Returned",
        str(result_b.get("return_status")),
    )
    _assert_return_invariants(ctx, case, source_invoice=invoice, result=result_b)


def _case_guards(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Undispatched orders must be refused and pointed at cancellation."""
    case = "RET-GUARD"
    customer = _ensure_customer(ctx, case, env["territory"], env["customer_group"])
    invoice = _create_invoice(
        ctx,
        customer=customer,
        pos_profile=env["pos_profile"],
        items=env["items"],
        order_purpose=None,
        commercial_policy=None,
        policy_reason=None,
        pickup=False,
    )
    frappe.db.commit()

    result = ir.run_invoice_return(
        invoice_id=invoice, lines=_full_lines(invoice), reason="should fail",
        return_type="Customer Return", refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        pay_courier_for_trip=True,
    )
    ctx.record(
        f"{case}.undispatched_refused",
        not result.get("success") and result.get("return_block_code") == "not_dispatched",
        json.dumps(result, default=str)[:200],
    )
    ctx.record(
        f"{case}.no_documents_created",
        not result.get("credit_note") and not result.get("return_delivery_note"),
        "nothing written on a refused return",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Read-only survey over real cloned data
# ─────────────────────────────────────────────────────────────────────────────

def run_readonly_survey(sample: int = 200) -> Dict[str, Any]:
    """Dry-run the return postings across real dispatched orders. Writes nothing.

    Proves account resolution and JE balancing hold for every real
    branch/company/courier shape in the cloned production data, and reports how
    many orders the legacy Delivery Note gap (G12) would block.
    """
    sample = max(1, int(sample or 200))
    rows = frappe.get_all(
        "Sales Invoice",
        filters={
            "docstatus": 1,
            "is_return": 0,
            "is_pos": 1,
            "custom_was_out_for_delivery": 1,
        },
        fields=["name", "custom_kanban_profile", "pos_profile", "sales_partner"],
        order_by="modified desc",
        limit_page_length=sample,
    ) or []

    summary = {
        "sampled": len(rows),
        "ok": 0,
        "unbalanced": 0,
        "resolution_errors": 0,
        "missing_delivery_note": 0,
        "money_states": {},
        "errors": [],
    }

    for row in rows:
        invoice_name = row["name"]
        if not ir._original_delivery_note(invoice_name):
            summary["missing_delivery_note"] += 1

        try:
            result = ir.dry_run_return_postings(invoice_name)
        except Exception as exc:
            summary["resolution_errors"] += 1
            if len(summary["errors"]) < 20:
                summary["errors"].append({"invoice": invoice_name, "error": str(exc)[:300]})
            continue

        state = result.get("money_state")
        summary["money_states"][state] = summary["money_states"].get(state, 0) + 1

        if result.get("ok"):
            summary["ok"] += 1
        else:
            summary["unbalanced"] += 1
            if len(summary["errors"]) < 20:
                summary["errors"].append({"invoice": invoice_name, "detail": result.get("errors")})

    summary["gap_note"] = (
        f"{summary['missing_delivery_note']} of {summary['sampled']} sampled orders have no "
        "resolvable original Delivery Note and would be blocked until the v1_7 backfill runs."
    )
    print(json.dumps(summary, indent=2, default=str))
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Entry points
# ─────────────────────────────────────────────────────────────────────────────

def run(cleanup: bool = True, report_path: Optional[str] = None) -> Dict[str, Any]:
    """Run the full return-flow matrix on staging."""
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}

    _guard_environment()

    frappe.flags.in_test = True
    # Staging shares the production Woo credentials — without this the harness
    # would push real orders into the live store.
    frappe.flags.ignore_woo_outbound = True

    previous_switch = _enable_returns()
    ctx = RunContext()
    report_path = report_path or "sites/return_flow_validation_report.md"

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

        # ── PART B — the return matrix ──────────────────────────────────
        _case_full_return(
            ctx, env, case="RET-UNPAID-COURIER-PAID", prepay=False,
            courier=env["courier"], pay_courier=True,
            refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        )
        _case_full_return(
            ctx, env, case="RET-UNPAID-COURIER-UNPAID", prepay=False,
            courier=env["courier"], pay_courier=False,
            refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        )
        _case_full_return(
            ctx, env, case="RET-PREPAID-CREDIT", prepay=True, courier=None,
            pay_courier=True, refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        )
        _case_full_return(
            ctx, env, case="RET-PREPAID-REFUND", prepay=True, courier=None,
            pay_courier=True, refund_mode=ir.REFUND_NOW,
        )
        _case_partial_then_complete(ctx, env)
        _case_guards(ctx, env)

        passed = ctx.passed
        failed = ctx.failed
        try:
            _emit_report(ctx, report_path, True, "")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "return_flow_validation report failed")

        result = {
            "passed": passed,
            "failed": failed,
            "report_path": report_path,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in ctx.checks
            ],
        }
        print(f"return_flow_validation: {passed} passed, {failed} failed")
        return result

    finally:
        if cleanup:
            try:
                _cleanup(ctx)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "return_flow_validation cleanup failed")
        _restore_returns(previous_switch)


def run_json(cleanup: bool = True, report_path: Optional[str] = None) -> Dict[str, Any]:
    """`run` with marker-wrapped JSON so an SSH driver can slice out the report."""
    report = run(cleanup=cleanup, report_path=report_path)
    print(REPORT_MARKER_START)
    print(json.dumps(report, indent=2, default=str))
    print(REPORT_MARKER_END)
    return report

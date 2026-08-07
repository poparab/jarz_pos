"""Money-level audit of the post-dispatch return workflow.

``return_flow_validation`` proves the return graph is *structurally* sound: the
documents exist, the source invoice survives, every voucher balances. That is a
weaker statement than it looks — "GL balanced" only says debits equal credits
within a voucher, which is true of any entry ERPNext will accept, including a
completely wrong one.

This harness asks the question the accountant actually cares about: **after the
dust settles, does every account come back to where it should be?** It sums the
GL across *every* voucher a scenario produced — invoice, payment, delivery note,
credit note, return delivery note, journal entries, settlement — and compares the
per-account net against a hand-derived expectation.

The expectations are written out longhand in each case rather than computed, so a
bug in the code under test cannot quietly rewrite the thing it is being measured
against.

Fixtures only, ``_RETMONEY_`` prefixed, cleaned up at the end. Never run against
production — :func:`_guard_environment` is a hard stop.

Usage::

    bench --site frontend execute jarz_pos.scripts.return_money_audit.run
    bench --site frontend execute jarz_pos.scripts.return_money_audit.run \
        --kwargs "{'cleanup': False}"
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

from jarz_pos.scripts.b2b_accounting_validation import (
    RunContext,
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
from jarz_pos.scripts.return_flow_validation import (
    _dispatch_invoice,
    _enable_returns,
    _full_lines,
    _guard_environment,
    _normalize_courier,
    _restore_returns,
    _teardown_dispatched_fixtures,
)
from jarz_pos.services import invoice_return as ir
from jarz_pos.services.delivery_handling import _get_courier_outstanding_account
from jarz_pos.utils.account_utils import (
    get_company_receivable_account,
    get_creditors_account,
    get_freight_expense_account,
    get_pos_cash_account,
)

REPORT_MARKER_START = "RETURN_MONEY_AUDIT_JSON_START"
REPORT_MARKER_END = "RETURN_MONEY_AUDIT_JSON_END"

CUSTOMER_PREFIX = "_RETMONEY_"
_TOL = 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Ledger measurement
# ─────────────────────────────────────────────────────────────────────────────

def _vouchers_for(invoice: str, extra: Optional[List[tuple]] = None) -> List[tuple]:
    """Every voucher whose GL belongs to this order's lifecycle.

    Collected by *discovery* rather than from the return's return-value, so a
    posting the code makes but does not report still shows up in the audit. A
    silent extra journal entry is precisely the kind of thing this is for.
    """
    found: List[tuple] = [("Sales Invoice", invoice)]

    credit_notes = frappe.get_all(
        "Sales Invoice", filters={"return_against": invoice, "docstatus": 1},
        pluck="name", limit_page_length=0,
    ) or []
    found += [("Sales Invoice", cn) for cn in credit_notes]

    for dn in frappe.get_all(
        "Delivery Note Item", filters={"against_sales_invoice": invoice, "docstatus": 1},
        pluck="parent", limit_page_length=0,
    ) or []:
        if ("Delivery Note", dn) not in found:
            found.append(("Delivery Note", dn))

    # Journal Entries tag themselves with the invoice or credit-note name in
    # user_remark (title is unusable — v16 overwrites it on validate).
    like_keys = [invoice] + credit_notes
    for key in like_keys:
        for je in frappe.db.sql(
            "SELECT name FROM `tabJournal Entry` WHERE docstatus = 1 AND user_remark LIKE %s",
            ("%" + key + "%",), as_dict=True,
        ) or []:
            if ("Journal Entry", je["name"]) not in found:
                found.append(("Journal Entry", je["name"]))

    # Payment Entries reach the order either through their reference table or
    # through the RET- reference_no the refund stamps.
    for key in like_keys:
        for pe in frappe.db.sql(
            """SELECT DISTINCT per.parent AS name FROM `tabPayment Entry Reference` per
               JOIN `tabPayment Entry` pe ON pe.name = per.parent
               WHERE pe.docstatus = 1 AND per.reference_name = %s""",
            (key,), as_dict=True,
        ) or []:
            if ("Payment Entry", pe["name"]) not in found:
                found.append(("Payment Entry", pe["name"]))
        for pe in frappe.db.sql(
            "SELECT name FROM `tabPayment Entry` WHERE docstatus = 1 AND reference_no LIKE %s",
            ("%" + key + "%",), as_dict=True,
        ) or []:
            if ("Payment Entry", pe["name"]) not in found:
                found.append(("Payment Entry", pe["name"]))

    return found + list(extra or [])


def _net_by_account(vouchers: List[tuple]) -> Dict[str, float]:
    """Signed net (debit positive) per account across *vouchers*."""
    net: Dict[str, float] = {}
    for voucher_type, voucher_no in vouchers:
        for row in frappe.get_all(
            "GL Entry",
            filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
            fields=["account", "debit", "credit"], limit_page_length=0,
        ) or []:
            key = row["account"]
            net[key] = round(net.get(key, 0.0) + flt(row["debit"]) - flt(row["credit"]), 2)
    return {k: v for k, v in net.items() if abs(v) > _TOL}


class Accounts:
    """The accounts every expectation is written in terms of."""

    def __init__(self, company: str, pos_profile: str):
        self.company = company
        self.receivable = get_company_receivable_account(company)
        self.cash = get_pos_cash_account(pos_profile, company)
        self.courier_outstanding = _get_courier_outstanding_account(company)
        self.creditors = get_creditors_account(company)
        self.freight = get_freight_expense_account(company)


def _record_expectation(
    ctx: RunContext, case: str, actual: Dict[str, float], expected: Dict[str, float]
) -> None:
    """Compare a measured per-account net against the hand-derived expectation."""
    accounts = set(actual) | {a for a, v in expected.items() if abs(v) > _TOL}
    diffs = []
    for account in sorted(accounts):
        want = flt(expected.get(account, 0.0))
        got = flt(actual.get(account, 0.0))
        if abs(want - got) > _TOL:
            diffs.append(f"{account}: expected {want:+.2f}, got {got:+.2f}")
    ctx.record(
        f"{case}.ledger_nets_as_expected",
        not diffs,
        "; ".join(diffs) if diffs else f"all {len(accounts)} accounts as expected",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────

def _build_env() -> Dict[str, Any]:
    pos_profile = _pick_pos_profile()
    company = frappe.db.get_value("POS Profile", pos_profile, "company")
    return {
        "pos_profile": pos_profile,
        "company": company,
        "territory": _ensure_territory(company),
        "customer_group": _ensure_customer_group(),
        "items": _pick_sellable_items(pos_profile, count=2),
        "accounts": Accounts(company, pos_profile),
    }


def _shipping_income(invoice: str) -> float:
    """What the customer was charged for delivery — the tax row, nothing else.

    Never ``custom_delivery_income`` (NOT NULL DEFAULT 0) and never the Territory
    rate: no row means the customer was charged nothing.
    """
    total = 0.0
    for row in frappe.get_all(
        "Sales Taxes and Charges",
        filters={"parent": invoice, "parenttype": "Sales Invoice"},
        fields=["tax_amount"], limit_page_length=0,
    ) or []:
        total += flt(row["tax_amount"])
    return round(total, 2)


def _courier_shipping_expense(invoice: str) -> float:
    row = frappe.get_all(
        "Courier Transaction", filters={"reference_invoice": invoice},
        fields=["shipping_amount"], order_by="creation asc", limit_page_length=1,
    ) or []
    return round(flt(row[0]["shipping_amount"]), 2) if row else 0.0


def _dispatch_as_partner(invoice: str, profile: str, courier: Dict[str, Any]) -> None:
    """Dispatch through the delivery-partner path.

    ``handle_out_for_delivery_transition`` sends every unpaid order to
    ``mark_courier_outstanding``, which is the generic courier flow — it never
    stamps ``custom_is_partner_order`` however partner-linked the courier is. The
    partner strategies hang off ``dispatch_settlement``, which detects the link
    itself, so the partner case has to go in that way or it silently tests the
    ordinary path instead.
    """
    from jarz_pos.services.settlement_strategies import dispatch_settlement

    dispatch_settlement(
        invoice, mode="later", pos_profile=profile,
        party_type=courier.get("party_type"), party=courier.get("party"),
    )


def _setup_order(ctx: RunContext, env: Dict[str, Any], case: str, *, prepay: bool,
                 courier: Optional[Dict[str, Any]],
                 pos_profile: Optional[str] = None,
                 dispatch_fn: Optional[Any] = None) -> str:
    """Create -> (pay) -> dispatch, on *pos_profile* or the environment default.

    The profile override exists for the partner case: couriers are branch-scoped,
    and dispatching through a profile the courier does not belong to is refused by
    design (``Courier X belongs to POS Profile A, not B``).
    """
    profile = pos_profile or env["pos_profile"]
    customer = _ensure_customer(ctx, case, env["territory"], env["customer_group"])
    invoice = _create_invoice(
        ctx, customer=customer, pos_profile=profile, items=env["items"],
        order_purpose=None, commercial_policy=None, policy_reason=None, pickup=False,
    )
    if prepay:
        _pay_invoice_full_cash(ctx, invoice, profile)
    (dispatch_fn or _dispatch_invoice)(invoice, profile, courier)
    frappe.db.commit()
    return invoice


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────

def _case_cod_full_return(ctx: RunContext, env: Dict[str, Any], *, case: str,
                          pay_courier: bool, courier: Optional[Dict[str, Any]]) -> None:
    """COD order the courier still holds, returned in full.

    Expectation: revenue, receivable and Courier Outstanding all return to zero.
    The only residue is the trip fee — expensed and owed to the courier when we
    pay for the trip, and nothing at all when we do not.
    """
    acc: Accounts = env["accounts"]
    invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
    ship_expense = _courier_shipping_expense(invoice)

    result = ir.run_invoice_return(
        invoice_id=invoice, lines=_full_lines(invoice), reason=f"{case} money audit",
        refund_mode=ir.REFUND_CUSTOMER_CREDIT, pay_courier_for_trip=pay_courier,
    )
    ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
    if not result.get("success"):
        return

    expected = {
        acc.receivable: 0.0,
        acc.courier_outstanding: 0.0,
        acc.freight: ship_expense if pay_courier else 0.0,
        acc.creditors: -ship_expense if pay_courier else 0.0,
    }
    _record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)


def _case_prepaid_full_return(ctx: RunContext, env: Dict[str, Any], *, case: str,
                              refund_mode: str) -> None:
    """Prepaid pickup-style order returned in full.

    ``refund_now``  -> the cash goes back out; every account returns to zero.
    ``customer_credit`` -> the cash stays in the till and the customer is left
    holding a credit, i.e. a negative receivable of exactly the order total.
    """
    acc: Accounts = env["accounts"]
    invoice = _setup_order(ctx, env, case, prepay=True, courier=None)
    grand_total = flt(frappe.db.get_value("Sales Invoice", invoice, "grand_total"))
    ship_expense = _courier_shipping_expense(invoice)

    result = ir.run_invoice_return(
        invoice_id=invoice, lines=_full_lines(invoice), reason=f"{case} money audit",
        refund_mode=refund_mode, pay_courier_for_trip=True,
    )
    ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
    if not result.get("success"):
        return

    if refund_mode == ir.REFUND_NOW:
        expected = {acc.receivable: 0.0, acc.cash: 0.0}
    else:
        expected = {acc.receivable: -grand_total, acc.cash: grand_total}
    expected[acc.freight] = ship_expense
    expected[acc.creditors] = -ship_expense
    _record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)


def _case_partial_shipping(ctx: RunContext, env: Dict[str, Any],
                           courier: Optional[Dict[str, Any]]) -> None:
    """Return ONE line of a two-line order.

    The customer keeps the rest and the delivery happened, so the delivery charge
    is not refundable: the credit note must equal the returned goods and nothing
    more.
    """
    case = "MONEY-PARTIAL-SHIPPING"
    invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
    inv = frappe.get_doc("Sales Invoice", invoice)
    if len(inv.items) < 2:
        ctx.record(f"{case}.skipped", True, "fixture invoice has a single line")
        return

    first = inv.items[0]
    goods_only = round(flt(first.amount), 2)
    shipping = _shipping_income(invoice)
    ctx.record(f"{case}.fixture_has_shipping_income", shipping > _TOL, f"shipping income={shipping}")

    result = ir.run_invoice_return(
        invoice_id=invoice, lines=[{"si_detail": first.name, "qty": flt(first.qty)}],
        reason=f"{case} money audit", refund_mode=ir.REFUND_CUSTOMER_CREDIT,
        pay_courier_for_trip=True,
    )
    ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
    if not result.get("success"):
        return

    credited = round(abs(flt(result.get("return_total"))), 2)
    ctx.record(
        f"{case}.credits_goods_only",
        abs(credited - goods_only) <= _TOL,
        f"returned goods={goods_only}, credit note={credited}, "
        f"shipping income on the order={shipping} (a partial return must not refund delivery)",
    )


def _case_partial_twice_never_exceeds_invoice(ctx: RunContext, env: Dict[str, Any],
                                              courier: Optional[Dict[str, Any]]) -> None:
    """Return each line separately; the two credit notes must sum to the invoice.

    Anything more means the order was credited for money the customer never paid.
    """
    case = "MONEY-PARTIAL-TWICE"
    invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
    inv = frappe.get_doc("Sales Invoice", invoice)
    if len(inv.items) < 2:
        ctx.record(f"{case}.skipped", True, "fixture invoice has a single line")
        return
    grand_total = round(flt(inv.grand_total), 2)

    credited = 0.0
    for index, row in enumerate(inv.items):
        result = ir.run_invoice_return(
            invoice_id=invoice, lines=[{"si_detail": row.name, "qty": flt(row.qty)}],
            reason=f"{case} leg {index} money audit", refund_mode=ir.REFUND_CUSTOMER_CREDIT,
            pay_courier_for_trip=True,
        )
        ctx.record(f"{case}.leg{index}_succeeded", bool(result.get("success")),
                   json.dumps(result, default=str)[:300])
        if not result.get("success"):
            return
        credited += abs(flt(result.get("return_total")))

    credited = round(credited, 2)
    ctx.record(
        f"{case}.total_credited_equals_invoice",
        abs(credited - grand_total) <= _TOL,
        f"invoice grand total={grand_total}, total credited across both partial "
        f"returns={credited}, over-credit={round(credited - grand_total, 2)}",
    )


def _case_settle_after_return(ctx: RunContext, env: Dict[str, Any],
                              courier: Optional[Dict[str, Any]]) -> None:
    """Fully return a COD order, then run the one-by-one courier settlement.

    The return already released the courier's liability. Settling afterwards must
    not release it a second time — Courier Outstanding has to end at zero, and the
    branch must not bank cash for goods that came back.
    """
    case = "MONEY-SETTLE-AFTER-RETURN"
    if not courier:
        ctx.record(f"{case}.skipped", True, "no courier bound to this branch")
        return

    acc: Accounts = env["accounts"]
    invoice = _setup_order(ctx, env, case, prepay=False, courier=courier)
    ship_expense = _courier_shipping_expense(invoice)

    result = ir.run_invoice_return(
        invoice_id=invoice, lines=_full_lines(invoice), reason=f"{case} money audit",
        refund_mode=ir.REFUND_CUSTOMER_CREDIT, pay_courier_for_trip=True,
    )
    ctx.record(f"{case}.return_succeeded", bool(result.get("success")),
               json.dumps(result, default=str)[:300])
    if not result.get("success"):
        return

    from jarz_pos.services.delivery_handling import settle_single_invoice_paid

    settled: Dict[str, Any] = {}
    try:
        settled = settle_single_invoice_paid(
            invoice, env["pos_profile"], courier.get("party_type"), courier.get("party")
        ) or {}
        ctx.record(f"{case}.settlement_ran", True, json.dumps(settled, default=str)[:300])
    except Exception as exc:
        # Refusing to settle a returned order is a perfectly good outcome — better
        # than posting a wrong entry — so this is recorded, not failed.
        ctx.record(f"{case}.settlement_refused", True, f"settlement declined: {exc}")

    # The courier was paid for the trip, so once the settlement runs the fee has
    # left the till: freight is expensed and cash is down by the same amount. What
    # must NOT happen is the settlement releasing Courier Outstanding a second
    # time, or the branch banking the order value on returned goods — both are
    # pinned at zero here.
    expected = {
        acc.receivable: 0.0,
        acc.courier_outstanding: 0.0,
        acc.freight: ship_expense,
        acc.cash: -ship_expense,
    }
    _record_expectation(ctx, case, _net_by_account(_vouchers_for(invoice)), expected)


def _case_partner_return(ctx: RunContext, env: Dict[str, Any]) -> None:
    """Return a delivery-partner order.

    Partner orders never touch Courier Outstanding — dispatch moves the receivable
    to the partner's own settlement account. The return has to unwind *that*
    account, and must leave Courier Outstanding strictly alone.
    """
    case = "MONEY-PARTNER-RETURN"
    partner = frappe.db.get_value("Delivery Partner", {}, ["name", "settlement_account"], as_dict=True)
    if not partner or not partner.get("settlement_account"):
        ctx.record(f"{case}.skipped", True, "no Delivery Partner with a settlement account")
        return

    party = frappe.db.get_value(
        "Supplier", {"custom_delivery_partner": partner["name"]}, "name"
    ) or frappe.db.get_value("Employee", {"custom_delivery_partner": partner["name"]}, "name")
    if not party:
        ctx.record(f"{case}.skipped", True, f"no courier bound to partner {partner['name']}")
        return
    party_type = "Supplier" if frappe.db.exists("Supplier", party) else "Employee"

    # Couriers are branch-scoped, so the order has to be raised on the profile the
    # partner's courier actually belongs to, not whichever profile came first.
    from jarz_pos.utils.courier_visibility import resolve_courier_branch

    profile = resolve_courier_branch(party_type, party) or env["pos_profile"]
    if not frappe.db.exists("POS Profile", profile):
        ctx.record(f"{case}.skipped", True, f"courier branch {profile!r} is not a POS Profile")
        return

    company = frappe.db.get_value("POS Profile", profile, "company")
    acc = Accounts(company, profile)
    invoice = _setup_order(
        ctx, env, case, prepay=False,
        courier={"party_type": party_type, "party": party}, pos_profile=profile,
        dispatch_fn=_dispatch_as_partner,
    )
    is_partner = int(frappe.db.get_value("Sales Invoice", invoice, "custom_is_partner_order") or 0)
    ctx.record(f"{case}.dispatched_as_partner_order", is_partner == 1, f"custom_is_partner_order={is_partner}")
    if not is_partner:
        return

    result = ir.run_invoice_return(
        invoice_id=invoice, lines=_full_lines(invoice), reason=f"{case} money audit",
        refund_mode=ir.REFUND_CUSTOMER_CREDIT, pay_courier_for_trip=True,
    )
    ctx.record(f"{case}.succeeded", bool(result.get("success")), json.dumps(result, default=str)[:300])
    if not result.get("success"):
        return

    ship_expense = _courier_shipping_expense(invoice)
    actual = _net_by_account(_vouchers_for(invoice))
    # Dispatch left the partner owing us (order - fee) and expensed the fee. A full
    # return with the trip paid for cancels the order side and nothing else, so the
    # partner ends up owed exactly the fee — a payable, hence negative — and the
    # freight expense stands. Courier Outstanding stays untouched throughout, which
    # is the whole point of the partner path and is asserted separately below.
    expected = {
        acc.receivable: 0.0,
        acc.courier_outstanding: 0.0,
        partner["settlement_account"]: -ship_expense,
        acc.freight: ship_expense,
    }
    _record_expectation(ctx, case, actual, expected)
    ctx.record(
        f"{case}.courier_outstanding_untouched",
        abs(flt(actual.get(acc.courier_outstanding, 0.0))) <= _TOL,
        f"Courier Outstanding net={actual.get(acc.courier_outstanding, 0.0)} "
        f"(a partner order must never touch this account)",
    )

    reversal = frappe.get_all(
        "Courier Transaction",
        filters={"reference_invoice": invoice, "idempotency_token": ["like", "RET::%"]},
        fields=["name", "is_partner_order"], limit_page_length=5,
    ) or []
    ctx.record(
        f"{case}.reversal_row_flagged_as_partner",
        bool(reversal) and all(int(r.get("is_partner_order") or 0) == 1 for r in reversal),
        f"compensating rows={reversal} (must carry is_partner_order so the partner "
        f"settlement sweep sees the reversal and the generic courier sweep does not)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def run(cleanup: bool = True) -> Dict[str, Any]:
    _guard_environment()
    previous_flag = _enable_returns()
    ctx = RunContext()

    try:
        env = _build_env()
        _ensure_item_prices(ctx, env["items"])
        _ensure_stock(ctx, env["items"], env["pos_profile"])
        courier = _normalize_courier(_pick_courier_party(env["pos_profile"]))
        print(f"\nEnvironment: profile={env['pos_profile']} company={env['company']} "
              f"items={env['items']} courier={courier}")

        # Each case is isolated: one that blows up records a failure and the rest
        # still run. A single unhandled error used to abort the whole run before
        # the report was printed, which threw away every result already gathered.
        cases = [
            ("MONEY-COD-PAY-COURIER",
             lambda: _case_cod_full_return(ctx, env, case="MONEY-COD-PAY-COURIER",
                                           pay_courier=True, courier=courier)),
            ("MONEY-COD-NO-COURIER-FEE",
             lambda: _case_cod_full_return(ctx, env, case="MONEY-COD-NO-COURIER-FEE",
                                           pay_courier=False, courier=courier)),
            ("MONEY-PREPAID-REFUND",
             lambda: _case_prepaid_full_return(ctx, env, case="MONEY-PREPAID-REFUND",
                                               refund_mode=ir.REFUND_NOW)),
            ("MONEY-PREPAID-CREDIT",
             lambda: _case_prepaid_full_return(ctx, env, case="MONEY-PREPAID-CREDIT",
                                               refund_mode=ir.REFUND_CUSTOMER_CREDIT)),
            ("MONEY-PARTIAL-SHIPPING", lambda: _case_partial_shipping(ctx, env, courier)),
            ("MONEY-PARTIAL-TWICE",
             lambda: _case_partial_twice_never_exceeds_invoice(ctx, env, courier)),
            ("MONEY-SETTLE-AFTER-RETURN", lambda: _case_settle_after_return(ctx, env, courier)),
            ("MONEY-PARTNER-RETURN", lambda: _case_partner_return(ctx, env)),
        ]
        for name, run_case in cases:
            try:
                run_case()
            except Exception as exc:
                ctx.record(f"{name}.case_raised", False, f"{type(exc).__name__}: {exc}")
                frappe.log_error(frappe.get_traceback(), f"return_money_audit case {name}")

    finally:
        if cleanup:
            try:
                _teardown_dispatched_fixtures(ctx)
                _cleanup(ctx)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "return_money_audit cleanup failed")
        _restore_returns(previous_flag)
        frappe.db.commit()

    failures = [c for c in ctx.checks if not c.passed]
    print("\n" + "=" * 78)
    print(f"RETURN MONEY AUDIT — {ctx.passed} passed, {ctx.failed} failed")
    print("=" * 78)
    for check in failures:
        print(f"  FAIL  {check.name}\n        {check.detail}")

    report = {
        "passed": ctx.passed,
        "failed": ctx.failed,
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in ctx.checks],
    }
    print(REPORT_MARKER_START)
    print(json.dumps(report, default=str))
    print(REPORT_MARKER_END)
    return report

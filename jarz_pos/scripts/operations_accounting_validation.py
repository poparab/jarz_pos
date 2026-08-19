"""End-to-end accounting validation for the non-invoice money and stock paths.

``full_lifecycle_validation`` drives the invoice lifecycle. ``accounting_deep_audit``
inspects everything already in the ledger. Neither one *exercises* the other
modules that move money or stock: cash transfers between branches, expense
requests, warehouse transfers, inventory counts, manufacturing, and shift
open/close.

Those all pass the site-wide invariants today — every voucher they have ever
produced balances. But a balanced entry can still be wrong: a cash transfer that
credits the wrong branch's account balances perfectly, and so does an expense
booked to the wrong head or a shift discrepancy posted with the sign flipped.
Invariants cannot see any of that. Only calling the endpoint and checking where
the money actually landed can.

So each case here calls the real endpoint, then asserts the *specific* movement:
this exact amount left this exact account and arrived in that one.

Synthetic fixtures and small amounts only; cleans up in ``finally``; refuses to
run against production.

Run::

    bench --site frontend execute jarz_pos.scripts.operations_accounting_validation.run
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt, nowdate

from jarz_pos.scripts.b2b_accounting_validation import (
    RunContext,
    assert_gl_balanced,
    _cleanup,
    _ensure_item_prices,
    _ensure_stock,
    _pick_pos_profile,
    _pick_sellable_items,
)

_TOL = 0.01
MARKER_START = "OPERATIONS_VALIDATION_JSON_START"
MARKER_END = "OPERATIONS_VALIDATION_JSON_END"

#: Deliberately odd so it can never be confused with real traffic in the ledger.
_TRANSFER_AMOUNT = 13.37
_EXPENSE_AMOUNT = 7.77
_SHIFT_SURPLUS = 5.55


def _guard_environment() -> None:
    try:
        base_url = frappe.utils.get_url() or ""
    except Exception:
        base_url = ""
    if "erp.orderjarz.com" in base_url:
        raise RuntimeError(
            f"operations_accounting_validation refuses to run on production ({base_url!r})"
        )


def _gl_by_account(voucher_type: str, voucher_no: str) -> Dict[str, float]:
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


def _bin_qty(item_code: str, warehouse: str) -> float:
    return flt(
        frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
    )


class _Artifacts:
    """Documents this run created, unwound newest-first."""

    def __init__(self) -> None:
        self.rows: List[tuple] = []

    def add(self, doctype: str, name: str) -> None:
        if name and (doctype, name) not in self.rows:
            self.rows.append((doctype, name))

    def unwind(self) -> None:
        for doctype, name in reversed(self.rows):
            try:
                if not frappe.db.exists(doctype, name):
                    continue
                doc = frappe.get_doc(doctype, name)
                doc.flags.ignore_permissions = True
                doc.flags.ignore_links = True
                if int(getattr(doc, "docstatus", 0) or 0) == 1:
                    doc.cancel()
                frappe.delete_doc(
                    doctype, name, force=True, ignore_permissions=True, delete_permanently=True
                )
            except Exception as exc:
                print(f"   cleanup: could not remove {doctype} {name}: {exc}")
        frappe.db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Cases
# ─────────────────────────────────────────────────────────────────────────────

def _case_cash_transfer(ctx: RunContext, env: Dict[str, Any], art: _Artifacts) -> None:
    """Money must leave one account and arrive in the other, to the piastre."""
    case = "OPS-CASH-TRANSFER"
    from jarz_pos.api.cash_transfer import list_accounts, submit_transfer

    accounts = [a for a in (list_accounts(env["company"]) or []) if a.get("account")]
    if len(accounts) < 2:
        # WHY: recorded passed=True. A site with one cash account reported the
        # cash-transfer money path green without moving a piastre.
        ctx.skip(
            f"{case}.skipped",
            f"needs two cash/bank accounts, {env['company']} exposes {len(accounts)} — "
            "no transfer was performed and nothing was asserted",
        )
        return

    source = accounts[0]["account"]
    target = accounts[1]["account"]

    result = submit_transfer(
        from_account=source, to_account=target, amount=_TRANSFER_AMOUNT,
        remark="operations validation",
    )
    je = result.get("journal_entry") or result.get("name")
    art.add("Journal Entry", je)
    frappe.db.commit()

    ctx.record(f"{case}.transfer_created", bool(je), json.dumps(result, default=str)[:200])
    if not je:
        return

    passed, detail = assert_gl_balanced("Journal Entry", je)
    ctx.record(f"{case}.journal_balanced", passed, detail)

    gl = _gl_by_account("Journal Entry", je)
    ctx.record(
        f"{case}.exact_amount_left_source",
        abs(flt(gl.get(source)) + _TRANSFER_AMOUNT) <= _TOL,
        f"{source} moved {gl.get(source)} (expected -{_TRANSFER_AMOUNT})",
    )
    ctx.record(
        f"{case}.exact_amount_reached_target",
        abs(flt(gl.get(target)) - _TRANSFER_AMOUNT) <= _TOL,
        f"{target} moved {gl.get(target)} (expected +{_TRANSFER_AMOUNT})",
    )
    ctx.record(
        f"{case}.only_the_two_accounts_moved",
        len(gl) == 2,
        f"accounts touched={sorted(gl)}",
    )


def _case_expense_request(ctx: RunContext, env: Dict[str, Any], art: _Artifacts) -> None:
    """An approved expense debits the expense head and credits what paid it."""
    case = "OPS-EXPENSE"
    from jarz_pos.api.expenses import approve_expense, create_expense

    company = env["company"]
    # create_expense only accepts a reason account sitting under Indirect
    # Expenses, and it is right to: an expense request is an operating cost, not
    # a cost of goods. Picking the first Expense ledger on the site ignored that
    # and drew ``Label Cost - J``, so every run of this case died on the guard
    # instead of validating the money path — a crash that read as a product
    # failure when it was only a loose fixture.
    #
    # Scope by lft/rgt rather than parent_account: the guard walks the parent
    # chain to any depth (jarz_expense_request.py:45), so a direct-parent filter
    # would still miss valid ledgers nested further down.
    group = frappe.db.get_value(
        "Account",
        {
            "company": company, "is_group": 1,
            "account_name": ["in", ["Indirect Expenses", "Indirect Expense"]],
        },
        ["name", "lft", "rgt"],
        as_dict=True,
    )
    expense_head = frappe.db.get_value(
        "Account",
        {
            "company": company, "root_type": "Expense", "is_group": 0, "disabled": 0,
            "lft": [">", group.lft], "rgt": ["<", group.rgt],
            # Cash Over/Short is the shift-discrepancy ledger; posting test
            # expenses there would pollute a balance other checks reason about.
            "account_name": ["not like", "%Over%Short%"],
        },
        "name",
    ) if group else None
    paying = frappe.db.get_value(
        "Account",
        {"company": company, "account_type": "Cash", "is_group": 0, "disabled": 0},
        "name",
    )
    if not expense_head or not paying:
        # WHY: recorded passed=True — an unusable chart of accounts made the
        # whole expense money path score a pass.
        ctx.skip(
            f"{case}.skipped",
            f"no usable accounts: indirect_group={(group or {}).get('name')} "
            f"expense_head={expense_head} paying={paying} — no expense was created",
        )
        return

    created = create_expense(payload=json.dumps({
        "amount": _EXPENSE_AMOUNT,
        "reason_account": expense_head,
        "paying_account": paying,
        "expense_date": nowdate(),
        "remarks": "operations validation",
    }))
    # The endpoint answers {"success": true, "expense": {...}} rather than the
    # document itself.
    payload = created.get("expense") if isinstance(created, dict) else None
    name = (payload or {}).get("name") if isinstance(payload, dict) else None
    art.add("Jarz Expense Request", name)
    ctx.record(f"{case}.expense_created", bool(name), json.dumps(created, default=str)[:200])
    if not name:
        return

    docstatus = int(frappe.db.get_value("Jarz Expense Request", name, "docstatus") or 0)
    if docstatus == 0:
        try:
            approve_expense(name)
        except Exception as exc:
            ctx.record(f"{case}.approve_failed", False, str(exc))
            return
    else:
        # create_expense submits straight away when the creator may approve, so
        # a second approval is correctly refused — nothing to do here.
        # WHY the condition: this recorded True for ANY non-zero docstatus,
        # which includes 2 — a CANCELLED expense request scored "auto approved"
        # and the money assertions below then ran against a voided document.
        auto_approved = docstatus == 1
        ctx.record(
            f"{case}.auto_approved_on_create", auto_approved,
            f"docstatus={docstatus} (1=submitted; 2=CANCELLED, which is not an approval)",
        )
        if not auto_approved:
            return
    frappe.db.commit()

    je = frappe.db.get_value("Jarz Expense Request", name, "journal_entry")
    art.add("Journal Entry", je)
    ctx.record(f"{case}.journal_created", bool(je), str(je))
    if not je:
        return

    passed, detail = assert_gl_balanced("Journal Entry", je)
    ctx.record(f"{case}.journal_balanced", passed, detail)

    gl = _gl_by_account("Journal Entry", je)
    ctx.record(
        f"{case}.expense_head_debited",
        abs(flt(gl.get(expense_head)) - _EXPENSE_AMOUNT) <= _TOL,
        f"{expense_head} moved {gl.get(expense_head)} (expected +{_EXPENSE_AMOUNT})",
    )
    ctx.record(
        f"{case}.paying_account_credited",
        abs(flt(gl.get(paying)) + _EXPENSE_AMOUNT) <= _TOL,
        f"{paying} moved {gl.get(paying)} (expected -{_EXPENSE_AMOUNT})",
    )


def _case_stock_transfer(ctx: RunContext, env: Dict[str, Any], art: _Artifacts) -> None:
    """Quantity must leave the source warehouse and arrive in the target."""
    case = "OPS-STOCK-TRANSFER"
    from jarz_pos.api.transfer import submit_transfer

    item = env["items"][0] if env["items"] else None
    source = env["warehouse"]
    target = frappe.db.get_value(
        "Warehouse",
        {"company": env["company"], "is_group": 0, "disabled": 0, "name": ["!=", source]},
        "name",
    )
    if not item or not source or not target:
        # WHY: recorded passed=True — a site with a single warehouse "passed"
        # the warehouse-transfer case without transferring anything.
        ctx.skip(
            f"{case}.skipped",
            f"needs an item and two warehouses: item={item} source={source} target={target}",
        )
        return

    available = _bin_qty(item, source)
    if available < 1:
        # WHY: recorded passed=True. No stock means no transfer means no proof.
        ctx.skip(f"{case}.skipped", f"no stock of {item} in {source} (qty={available})")
        return

    before_src, before_tgt = available, _bin_qty(item, target)

    result = submit_transfer(
        source_warehouse=source, target_warehouse=target,
        lines=[{"item_code": item, "qty": 1}],
    )
    se = result.get("stock_entry") or result.get("name")
    art.add("Stock Entry", se)
    frappe.db.commit()

    ctx.record(f"{case}.transfer_created", bool(se), json.dumps(result, default=str)[:200])
    if not se:
        return

    after_src, after_tgt = _bin_qty(item, source), _bin_qty(item, target)
    ctx.record(
        f"{case}.qty_left_source",
        abs((before_src - after_src) - 1) <= 0.001,
        f"{source}: {before_src} -> {after_src}",
    )
    ctx.record(
        f"{case}.qty_reached_target",
        abs((after_tgt - before_tgt) - 1) <= 0.001,
        f"{target}: {before_tgt} -> {after_tgt}",
    )
    ctx.record(
        f"{case}.total_quantity_conserved",
        abs((after_src + after_tgt) - (before_src + before_tgt)) <= 0.001,
        "a transfer must not create or destroy stock",
    )


def _case_inventory_count(ctx: RunContext, env: Dict[str, Any], art: _Artifacts) -> None:
    """A count must move the book quantity to the counted quantity, and no further."""
    case = "OPS-INVENTORY-COUNT"
    from jarz_pos.api.inventory_count import submit_reconciliation

    item = env["items"][0] if env["items"] else None
    warehouse = env["warehouse"]
    if not item or not warehouse:
        # WHY: recorded passed=True — the inventory-count case scored a pass
        # with nothing to count.
        ctx.skip(f"{case}.skipped", f"no item or warehouse (item={item} warehouse={warehouse})")
        return

    before = _bin_qty(item, warehouse)
    counted = before + 1  # deliberate surplus of exactly one unit

    try:
        result = submit_reconciliation(
            warehouse=warehouse, posting_date=nowdate(),
            lines=[{"item_code": item, "counted_qty": counted}],
            enforce_all=0,
        )
    except Exception as exc:
        ctx.record(f"{case}.submit_failed", False, str(exc))
        return

    recon = result.get("stock_reconciliation") or result.get("name")
    art.add("Stock Reconciliation", recon)
    frappe.db.commit()

    ctx.record(f"{case}.reconciliation_created", bool(recon), json.dumps(result, default=str)[:200])
    if not recon:
        return

    after = _bin_qty(item, warehouse)
    ctx.record(
        f"{case}.book_qty_matches_count",
        abs(after - counted) <= 0.001,
        f"{warehouse}: {before} -> {after} (counted {counted})",
    )

    passed, detail = assert_gl_balanced("Stock Reconciliation", recon)
    ctx.record(f"{case}.variance_gl_balanced", passed, detail)


def _case_shift_discrepancy(ctx: RunContext, env: Dict[str, Any], art: _Artifacts) -> None:
    """Closing short or over must post a Cash Over/Short entry for exactly the gap.

    This path has failed silently before — a dangling account made the
    discrepancy entry skip entirely, so a shift closed with a cash difference
    and nothing recorded it. Assert both the amount and the direction.
    """
    case = "OPS-SHIFT-DISCREPANCY"
    from jarz_pos.api.shift import end_shift, get_shift_summary, start_shift

    # Any branch will do — pick one that is currently closed instead of assuming
    # the default profile happens to be free.
    user = frappe.session.user
    mine = frappe.get_all(
        "POS Profile User", filters={"user": user}, pluck="parent"
    ) or []
    profile = None
    for candidate in mine:
        if frappe.db.get_value("POS Profile", candidate, "disabled"):
            continue
        if not frappe.get_all(
            "POS Opening Entry",
            filters={"pos_profile": candidate, "status": "Open", "docstatus": 1},
            pluck="name", limit_page_length=1,
        ):
            profile = candidate
            break
    granted_on = None
    if not profile:
        # Opening a shift deliberately requires an explicit POS Profile User row
        # — whoever opens a branch is accountable for its cash, so there is no
        # Administrator bypass. That is correct behaviour, but it also means
        # this case would silently skip forever on a site where the runner has
        # no membership. Grant it for the duration and revoke it in the finally
        # below, so the one path with a history of failing silently is actually
        # exercised.
        for candidate in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or []:
            if frappe.get_all(
                "POS Opening Entry",
                filters={"pos_profile": candidate, "status": "Open", "docstatus": 1},
                pluck="name", limit_page_length=1,
            ):
                continue
            try:
                doc = frappe.get_doc("POS Profile", candidate)
                doc.append("applicable_for_users", {"user": user})
                doc.flags.ignore_permissions = True
                doc.save(ignore_permissions=True)
                frappe.db.commit()
                profile, granted_on = candidate, candidate
                break
            except Exception as exc:
                print(f"   {case}: could not grant access on {candidate}: {exc}")

    if not profile:
        # WHY: recorded passed=True. The shift-discrepancy path has a history of
        # failing SILENTLY (a dangling account made the Cash Over/Short entry
        # skip entirely) — the one case that most needs to be seen not running.
        ctx.skip(
            f"{case}.skipped",
            f"{user} has no closed branch to open ({len(mine)} membership(s)) and none "
            "could be granted — the Cash Over/Short path was NOT exercised",
        )
        return

    try:
        _run_shift_case(ctx, case, profile, art)
    finally:
        if granted_on:
            try:
                doc = frappe.get_doc("POS Profile", granted_on)
                doc.set(
                    "applicable_for_users",
                    [r for r in (doc.get("applicable_for_users") or []) if r.user != user],
                )
                doc.flags.ignore_permissions = True
                doc.save(ignore_permissions=True)
                frappe.db.commit()
                print(f"   {case}: revoked temporary access on {granted_on}")
            except Exception as exc:
                print(f"   {case}: FAILED to revoke access on {granted_on}: {exc}")


def _run_shift_case(ctx: RunContext, case: str, profile: str, art: _Artifacts) -> None:
    """Open a shift, close it over by a known amount, assert the discrepancy."""
    from jarz_pos.api.shift import (
        _get_account_balance,
        _resolve_pos_profile_account,
        end_shift,
        get_shift_summary,
        start_shift,
    )

    profile_doc = frappe.get_doc("POS Profile", profile)
    modes = [
        r.mode_of_payment
        for r in (profile_doc.get("payments") or [])
        if r.mode_of_payment
    ] or ["Cash"]

    # start_shift requires a declared opening count per payment mode, and books
    # any gap between that count and the account's book balance to Cash Over/
    # Short as an opening discrepancy. Declaring a flat 0 therefore writes the
    # whole drawer off: this harness zeroed a live 770.00 drawer on
    # ``Nasr city - J`` exactly that way, and the branch it picks is whichever
    # one happens to be closed — one of them holds five figures. Read the
    # balance through the very helpers start_shift will use, so the declared
    # count and the system balance are the same number and the opening posts
    # nothing at all.
    branch_account = _resolve_pos_profile_account(profile_doc.company, profile, None, modes[0])
    if not branch_account:
        # WHY: recorded passed=True — and an unresolvable branch cash account is
        # the very shape of the bug this case exists to catch.
        ctx.skip(f"{case}.skipped", f"no cash account resolves for POS Profile {profile}")
        return
    opening_balance = _get_account_balance(branch_account, profile_doc.company)
    if opening_balance < 0:
        # A declared count may not be negative, so an overdrawn drawer cannot be
        # opened cleanly at all. Skipping is honest; declaring 0 would book the
        # entire negative balance to Cash Over/Short, which is the bug above.
        # WHY: recorded passed=True.
        ctx.skip(
            f"{case}.skipped",
            f"{branch_account} book balance is negative ({opening_balance}); "
            "cannot open without a write-off",
        )
        return
    opening_rows = [{"mode_of_payment": modes[0], "opening_amount": opening_balance}]

    try:
        opened = start_shift(profile, opening_rows)
    except Exception as exc:
        # WHY: recorded passed=True for any failure to open a shift, which is
        # how a broken start_shift would have looked from here — green.
        ctx.skip(
            f"{case}.skipped",
            f"could not open a shift on {profile}: {type(exc).__name__}: {exc} — "
            "the discrepancy assertions did NOT run",
        )
        return

    opening_entry = opened.get("opening_entry") if isinstance(opened, dict) else None
    # The opening discrepancy JE is a separate document from the opening entry.
    # Registering only the latter left the JE orphaned in the ledger after
    # teardown deleted the entry its remark referenced — it had to be removed by
    # hand. Register it whether or not one is expected, so a regression here
    # cleans up after itself instead of leaking into the books.
    opening_je = opened.get("journal_entry") if isinstance(opened, dict) else None
    art.add("POS Opening Entry", opening_entry)
    art.add("Journal Entry", opening_je)
    frappe.db.commit()
    ctx.record(f"{case}.shift_opened", bool(opening_entry), str(opening_entry))
    ctx.record(
        f"{case}.opening_posted_no_discrepancy",
        not opening_je,
        f"declared {opening_balance} against {branch_account} -> JE {opening_je}",
    )
    if not opening_entry:
        return

    summary = get_shift_summary(opening_entry) or {}
    rows = summary.get("payment_reconciliation") or []
    cash_row = next(
        (r for r in rows if "cash" in str(r.get("mode_of_payment") or "").lower()),
        rows[0] if rows else None,
    )
    if not cash_row:
        # WHY: recorded passed=True — no payment row means the shift can never
        # be closed with a declared amount, so nothing below could run.
        ctx.skip(f"{case}.skipped", "shift summary exposes no payment rows")
        return

    # The summary deliberately withholds every amount: it answers
    # ``amounts_hidden: 1`` and each reconciliation row carries mode_of_payment
    # and nothing else, so the cashier counts the drawer blind. Reading
    # ``expected_amount`` off it therefore never found a key and fell through to
    # 0.0 on every run — and the case still passed, because the opening
    # write-off above had forced the real balance to 0.0 too. One harness bug
    # was manufacturing the precondition that made the other look correct;
    # fixing the opening is what exposed this.
    #
    # end_shift measures the gap against the account's book balance
    # (_close_shift: system_expected), so take the expectation from there.
    expected = flt(_get_account_balance(branch_account, profile_doc.company))
    declared = flt(expected + _SHIFT_SURPLUS, 2)

    closed = end_shift(opening_entry, [{
        "mode_of_payment": cash_row.get("mode_of_payment"),
        "closing_amount": declared,
    }])
    frappe.db.commit()

    closing_entry = closed.get("closing_entry") if isinstance(closed, dict) else None
    art.add("POS Closing Entry", closing_entry)
    ctx.record(f"{case}.shift_closed", bool(closing_entry), str(closing_entry))

    # The discrepancy entry references both shift documents in its remark.
    je = None
    for candidate in frappe.get_all(
        "Journal Entry",
        filters={"docstatus": 1, "user_remark": ["like", f"%{opening_entry}%"]},
        pluck="name", limit_page_length=5,
    ) or []:
        remark = frappe.db.get_value("Journal Entry", candidate, "user_remark") or ""
        if "discrepancy" in remark.lower():
            je = candidate
            break
    art.add("Journal Entry", je)

    ctx.record(
        f"{case}.discrepancy_journal_posted",
        bool(je),
        f"closing declared {declared} vs expected {expected} (gap {_SHIFT_SURPLUS}) -> JE {je}",
    )
    if not je:
        return

    passed, detail = assert_gl_balanced("Journal Entry", je)
    ctx.record(f"{case}.discrepancy_balanced", passed, detail)

    gl = _gl_by_account("Journal Entry", je)
    total = sum(abs(v) for v in gl.values()) / 2 if gl else 0
    ctx.record(
        f"{case}.discrepancy_equals_the_gap",
        abs(total - _SHIFT_SURPLUS) <= _TOL,
        f"entry amount={total} expected={_SHIFT_SURPLUS}",
    )

    over_short = [a for a in gl if "over" in a.lower() and "short" in a.lower()]
    ctx.record(
        f"{case}.posted_to_cash_over_short",
        bool(over_short),
        f"accounts touched={sorted(gl)}",
    )
    if over_short:
        # Declared MORE than expected is a surplus: the over/short account is
        # credited, cash is debited. A flipped sign here would quietly turn
        # every surplus into a loss.
        ctx.record(
            f"{case}.surplus_credits_over_short",
            flt(gl.get(over_short[0])) < 0,
            f"{over_short[0]} net={gl.get(over_short[0])} (must be negative for a surplus)",
        )


_CASES = [
    _case_cash_transfer,
    _case_expense_request,
    _case_stock_transfer,
    _case_inventory_count,
    _case_shift_discrepancy,
]


def run(cleanup: bool = True) -> Dict[str, Any]:
    if isinstance(cleanup, str):
        cleanup = cleanup.strip().lower() not in {"", "0", "false", "no"}

    _guard_environment()
    frappe.flags.in_test = True
    frappe.flags.ignore_woo_outbound = True

    ctx = RunContext()
    art = _Artifacts()
    summary: Dict[str, Any] = {}
    try:
        pos_profile = _pick_pos_profile()
        company = frappe.db.get_value("POS Profile", pos_profile, "company")
        warehouse = frappe.db.get_value("POS Profile", pos_profile, "warehouse")
        items = _pick_sellable_items(pos_profile, 2)
        env = {
            "pos_profile": pos_profile,
            "company": company,
            "warehouse": warehouse,
            "items": items,
        }
        # The stock cases need an item that actually has quantity AND a
        # valuation rate. Real catalogue items on this site can sit at negative
        # qty with no valuation, which makes a reconciliation impossible to
        # post — a fixture problem, not a product one.
        _ensure_item_prices(ctx, items)
        _ensure_stock(ctx, items, pos_profile)
        frappe.db.commit()

        for case in _CASES:
            try:
                case(ctx, env, art)
            except Exception as exc:
                ctx.record(f"{case.__name__}.crashed", False, str(exc))
                frappe.log_error(frappe.get_traceback(), f"operations case failed: {case.__name__}")

        for check in ctx.checks:
            status = "SKIP" if check.skipped else ("PASS" if check.passed else "FAIL")
            print(f"   [{status}] {check.name} :: {check.detail}")
        print(f"operations_accounting_validation: {ctx.passed} passed, {ctx.failed} failed, "
              f"{ctx.skipped} skipped")

        summary.update({
            "passed": ctx.passed,
            "failed": ctx.failed,
            # A site missing a second cash account / warehouse / stock now says
            # so out loud instead of returning all-green having exercised nothing.
            "skipped": ctx.skipped,
            "skipped_checks": ctx.summary_skipped(),
            "checks": ctx.summary_checks(),
        })
        print(MARKER_START)
        print(json.dumps(summary, indent=2, default=str))
        print(MARKER_END)
        return summary
    finally:
        if cleanup:
            try:
                art.unwind()      # documents these cases created
                _cleanup(ctx)     # the stock/price fixtures seeded for them
            except Exception as exc:
                frappe.log_error(frappe.get_traceback(), "operations validation cleanup failed")
                # WHY: swallowed. These cases post real Journal Entries, Stock
                # Entries and shift documents; a teardown that dies leaves them
                # live in the ledger while the run reports its counts unchanged.
                # ``summary`` is the dict already handed to the caller.
                ctx.record("cleanup.teardown_completed", False, f"{type(exc).__name__}: {exc}")
                summary.update({
                    "passed": ctx.passed,
                    "failed": ctx.failed,
                    "skipped": ctx.skipped,
                    "skipped_checks": ctx.summary_skipped(),
                    "checks": ctx.summary_checks(),
                })

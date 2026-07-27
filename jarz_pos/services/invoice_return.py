"""Post-dispatch order returns — the corrective counterpart to cancellation.

Cancellation voids an order that never left the branch. Once an order has been
dispatched, voiding it is not an option: stock has moved, a courier may be
holding cash, a partner may already have been paid commission. Reversing all of
that by flipping the source invoice to ``docstatus 2`` would rewrite history and
silently strand every downstream balance — which is exactly why
``events.sales_invoice.block_cancel_if_dispatched`` refuses to allow it.

So a return never touches the source invoice's docstatus. It posts *new*
documents that reverse the parts that need reversing:

* a **Sales Return Delivery Note** puts the stock back. This is not optional and
  not interchangeable with the credit note: POS invoices are created with
  ``update_stock = 0`` (see ``services.invoice_creation``), so stock moves
  exclusively through Delivery Notes. A credit note alone would credit revenue
  while leaving the goods written off.
* a **Credit Note** (``is_return`` Sales Invoice) reverses the revenue and puts
  a credit on the customer's account.
* **Journal Entries** move whatever money already moved, which depends entirely
  on how far through the money lifecycle the order got — see ``_money_state``.

Two things are operator decisions rather than derivable facts, so the caller
supplies them:

``pay_courier_for_trip``
    A failed delivery still cost the courier a trip. Whether they are paid for
    it is a commercial call, so the freight accrual is reversed only on request.

``refund_mode``
    Money already collected either goes back now (a Payment Entry out of branch
    cash, which needs an open shift) or stays as a credit on the customer.

Partial returns are supported per line (item + quantity) for B2B orders; the
document graph is identical, only the inputs shrink.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe import _
from frappe.utils import flt

from jarz_pos.utils.access_control import (
    ClosedShiftError,
    ShiftRequiredError,
    ensure_open_shift_for_invoice,
    ensure_profile_scoped_invoice_access,
    get_invoice_branch,
)
from jarz_pos.utils.account_utils import (
    get_company_receivable_account,
    get_creditors_account,
    get_freight_expense_account,
    get_pos_cash_account,
    validate_account_exists,
)
from jarz_pos.services.delivery_handling import (
    _find_existing_je_by_tag,
    _get_courier_outstanding_account,
    _je_user_remark,
    update_submitted_sales_invoice_fields,
)

# ── Journal Entry dedup types ────────────────────────────────────────────────
# Deliberately distinct from every forward-flow token (OFD, COURIER_*,
# PARTNER_*, SALES_PARTNER_SETTLEMENT) so a return can never be mistaken for a
# dispatch posting or vice versa.
JE_AR_KNOCKOFF = "RETURN_AR_KNOCKOFF"
JE_COURIER_OUTSTANDING = "RETURN_COURIER_OUTSTANDING"
JE_FREIGHT_REVERSAL = "RETURN_FREIGHT_REVERSAL"
JE_PARTNER_REVERSAL = "RETURN_PARTNER_REVERSAL"

# ── Money states ─────────────────────────────────────────────────────────────
MONEY_UNPAID = "unpaid"
MONEY_COURIER_OUTSTANDING = "courier_outstanding"
MONEY_COURIER_SETTLED = "courier_settled"
MONEY_PREPAID = "prepaid"

# ── Refund modes ─────────────────────────────────────────────────────────────
REFUND_NOW = "refund_now"
REFUND_CUSTOMER_CREDIT = "customer_credit"
REFUND_MODES = {REFUND_NOW, REFUND_CUSTOMER_CREDIT}

RETURN_TYPES = {"Customer Return", "Failed Delivery", "Damaged", "Wrong Item"}

#: Operational states from which a return makes sense — the order left the branch.
_RETURNABLE_STATES = {"out_for_delivery", "delivered", "completed"}

#: Currency comparison tolerance, matching the settlement helpers.
_TOL = 0.005


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def returns_enabled() -> bool:
    """Master switch (``Jarz POS Settings.enable_invoice_returns``).

    Off by default. This is the rollback lever: flipping it needs no deploy and
    no restart, so a bad return release can be stopped in seconds.
    """
    try:
        return bool(int(frappe.db.get_single_value("Jarz POS Settings", "enable_invoice_returns") or 0))
    except Exception:
        return False


def _state_key(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _invoice_state(inv: Any) -> str:
    return _state_key(
        inv.get("custom_sales_invoice_state")
        or inv.get("sales_invoice_state")
        or inv.get("custom_state")
        or inv.get("state")
        or ""
    )


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_lines(lines: Any) -> List[Dict[str, Any]]:
    """Normalise the requested return lines to ``[{si_detail, qty}]``."""
    if not lines:
        return []
    if isinstance(lines, str):
        try:
            lines = json.loads(lines)
        except Exception:
            return []
    parsed: List[Dict[str, Any]] = []
    for row in lines or []:
        if not isinstance(row, dict):
            continue
        si_detail = str(row.get("si_detail") or row.get("name") or "").strip()
        qty = flt(row.get("qty"))
        if si_detail and qty > 0:
            parsed.append({"si_detail": si_detail, "qty": qty})
    return parsed


def _returned_qty_by_row(invoice_name: str) -> Dict[str, float]:
    """Quantity already returned per source invoice row.

    Summed from prior credit notes rather than recomputed, so a second partial
    return sees what the first one took.
    """
    source_rows = frappe.get_all(
        "Sales Invoice Item",
        filters={"parent": invoice_name, "parenttype": "Sales Invoice"},
        pluck="name",
        limit_page_length=0,
    ) or []
    if not source_rows:
        return {}

    # `sales_invoice_item` is only ever populated on return rows (set by
    # ERPNext's make_return_doc), so this counts credit notes and nothing else.
    return_rows = frappe.get_all(
        "Sales Invoice Item",
        filters={"sales_invoice_item": ["in", source_rows], "docstatus": 1},
        fields=["sales_invoice_item", "qty"],
        limit_page_length=0,
    ) or []

    returned: Dict[str, float] = {}
    for row in return_rows:
        key = row.get("sales_invoice_item")
        if not key:
            continue
        # Credit-note rows carry negative qty; report the magnitude.
        returned[key] = returned.get(key, 0.0) + abs(flt(row.get("qty")))
    return returned


def _original_delivery_note(invoice_name: str) -> Optional[str]:
    """The submitted, non-return Delivery Note this invoice shipped on."""
    rows = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": invoice_name, "docstatus": 1},
        fields=["parent"],
        limit_page_length=20,
    ) or []
    for row in rows:
        parent = row.get("parent")
        if not parent:
            continue
        if not int(frappe.db.get_value("Delivery Note", parent, "is_return") or 0):
            return parent
    return None


def _unsettled_courier_transactions(invoice_name: str) -> List[Dict[str, Any]]:
    try:
        return frappe.get_all(
            "Courier Transaction",
            filters={"reference_invoice": invoice_name},
            fields=[
                "name", "party_type", "party", "amount", "shipping_amount",
                "status", "delivery_partner", "is_partner_order",
            ],
            limit_page_length=20,
        ) or []
    except Exception:
        return []


def _money_state(inv: Any) -> str:
    """Where the customer's money currently sits.

    Order matters: an invoice whose receivable was moved to Courier Outstanding
    reads as "paid" by ``outstanding_amount`` alone, so the courier transaction
    has to be inspected before falling through to ``prepaid``.
    """
    outstanding = flt(inv.get("outstanding_amount"))
    grand_total = flt(inv.get("grand_total"))

    if outstanding >= grand_total - _TOL and grand_total > _TOL:
        return MONEY_UNPAID

    transactions = _unsettled_courier_transactions(inv.get("name"))
    positive = [t for t in transactions if flt(t.get("amount")) > _TOL]
    if positive:
        if any(str(t.get("status") or "").strip() != "Settled" for t in positive):
            return MONEY_COURIER_OUTSTANDING
        return MONEY_COURIER_SETTLED

    return MONEY_PREPAID


# ─────────────────────────────────────────────────────────────────────────────
# Eligibility
# ─────────────────────────────────────────────────────────────────────────────

def get_invoice_return_eligibility(inv: Any) -> Dict[str, Any]:
    """Whether *inv* can go through the return workflow.

    The mirror image of cancellation eligibility: the artifacts that *block* a
    cancel (a submitted Delivery Note, a courier transaction) are precisely what
    a return needs in order to have something to reverse.
    """
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()

    def _blocked(code: str, reason: str, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "can_return": False,
            "return_block_code": code,
            "return_block_reason": reason,
        }
        payload.update(extra)
        return payload

    if not returns_enabled():
        return _blocked("returns_disabled", _("The return workflow is not enabled."))

    if not invoice_name:
        return _blocked("invoice_missing", _("Invoice was not found."))

    if int(inv.get("docstatus") or 0) != 1:
        return _blocked("invoice_not_submitted", _("Only submitted invoices can be returned."))

    if int(inv.get("is_return") or 0):
        return _blocked("return_invoice", _("A credit note cannot itself be returned."))

    state = _invoice_state(inv)
    was_dispatched = bool(int(inv.get("custom_was_out_for_delivery") or 0))
    if state not in _RETURNABLE_STATES and not was_dispatched:
        return _blocked(
            "not_dispatched",
            _("This order has not been dispatched yet — cancel it instead of returning it."),
        )

    if str(inv.get("custom_return_status") or "").strip() == "Fully Returned":
        return _blocked("already_returned", _("This order has already been fully returned."))

    if not _original_delivery_note(invoice_name):
        # Legacy orders (pre-2026-07-20) recorded the invoice link only in the
        # Delivery Note `remarks` column that v16 removed. Surfacing this as an
        # explicit blocker beats failing halfway through building the graph.
        return _blocked(
            "original_delivery_note_missing",
            _(
                "The original Delivery Note for this order cannot be identified, so "
                "stock cannot be returned automatically. Ask an administrator to run "
                "the Delivery Note backfill for this order."
            ),
        )

    return {"can_return": True, "return_block_code": None, "return_block_reason": None}


# ─────────────────────────────────────────────────────────────────────────────
# Preview
# ─────────────────────────────────────────────────────────────────────────────

def build_return_preview(invoice_id: str) -> Dict[str, Any]:
    """Everything the confirmation dialog needs. Writes nothing."""
    invoice_id = str(invoice_id or "").strip()
    if not invoice_id:
        frappe.throw(_("invoice_id is required"))

    inv = frappe.get_doc("Sales Invoice", invoice_id)
    ensure_profile_scoped_invoice_access(inv, action_label="returning this order")

    eligibility = get_invoice_return_eligibility(inv)

    already_returned = _returned_qty_by_row(invoice_id)
    lines: List[Dict[str, Any]] = []
    for row in inv.items:
        sold = flt(row.qty)
        done = flt(already_returned.get(row.name))
        lines.append({
            "si_detail": row.name,
            "item_code": row.item_code,
            "item_name": row.item_name,
            "uom": row.uom,
            "rate": flt(row.rate),
            "amount": flt(row.amount),
            "qty_sold": sold,
            "qty_already_returned": done,
            "qty_returnable": max(sold - done, 0.0),
        })

    money_state = _money_state(inv)
    branch = get_invoice_branch(inv)
    open_shift = None
    try:
        from jarz_pos.utils.access_control import get_open_shift_for_profile

        open_shift = get_open_shift_for_profile(branch)
    except Exception:
        open_shift = None

    transactions = _unsettled_courier_transactions(invoice_id)
    courier = next((t for t in transactions if flt(t.get("amount")) > _TOL), None)
    freight_je = None
    for je_type in ("OFD", "COURIER_EXPENSE"):
        try:
            freight_je = _find_existing_je_by_tag(inv.company, invoice_id, je_type)
        except Exception:
            freight_je = None
        if freight_je:
            break

    is_pickup = bool(int(inv.get("custom_is_pickup") or 0))
    refund_available = money_state in {MONEY_PREPAID, MONEY_COURIER_SETTLED}

    return {
        "invoice_id": invoice_id,
        "customer": inv.customer,
        "customer_name": inv.customer_name,
        "grand_total": flt(inv.grand_total),
        "currency": inv.currency,
        "branch": branch,
        "money_state": money_state,
        "is_pickup": is_pickup,
        "lines": lines,
        "delivery_note": _original_delivery_note(invoice_id),
        "courier": {
            "party_type": (courier or {}).get("party_type"),
            "party": (courier or {}).get("party"),
            "courier_transaction": (courier or {}).get("name"),
            "shipping_amount": flt((courier or {}).get("shipping_amount")),
            "freight_journal_entry": freight_je,
        },
        "toggles": {
            # A pickup order never had a courier, so the trip question is moot.
            "can_choose_courier_fee": bool(courier and not is_pickup),
            "can_refund_now": bool(refund_available and open_shift),
            "refund_blocked_reason": (
                None if not refund_available
                else (None if open_shift else _("Start a shift on this branch to refund cash."))
            ),
        },
        "side_effects": {
            "sales_partner": inv.get("sales_partner"),
            "promo_codes": inv.get("custom_promo_codes"),
            "consumable_stock_entry": inv.get("custom_consumable_stock_entry"),
            "woo_order_id": inv.get("woo_order_id"),
        },
        **eligibility,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Journal Entry posting — one door, so the v16 rules are enforced once
# ─────────────────────────────────────────────────────────────────────────────

def _post_return_je(
    *,
    company: str,
    dedup_key: str,
    je_type: str,
    human: str,
    legs: List[Dict[str, Any]],
) -> Optional[str]:
    """Post a balanced, idempotent Journal Entry for a return step.

    Every return posting goes through here so the three v16 rules that have
    already bitten this codebase are enforced in exactly one place:

    1. ``voucher_type`` is ``"Journal Entry"``. ``"Bank Entry"`` triggers
       ``validate_cheque_info`` and fails deterministically without a cheque no.
    2. Any line on a Receivable/Payable account must carry ``party_type`` and
       ``party``, or v16 rejects the entry.
    3. Dedup lives in ``user_remark``, never ``title`` — v16 overwrites ``title``
       with the first account name on validate, so a title-based guard never
       matches and the entry double-posts on retry.

    *dedup_key* is the **credit note** name, not the source invoice: a second
    partial return against the same invoice must post its own entries rather
    than silently matching the first return's.
    """
    existing = _find_existing_je_by_tag(company, dedup_key, je_type)
    if existing:
        return existing

    material = [leg for leg in legs if abs(flt(leg.get("amount"))) > _TOL]
    if not material:
        return None

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = frappe.utils.nowdate()
    je.title = human[:140]
    je.user_remark = _je_user_remark(dedup_key, je_type, human)

    total_debit = 0.0
    total_credit = 0.0
    for leg in material:
        account = leg.get("account")
        validate_account_exists(account)
        amount = flt(leg.get("amount"))
        is_debit = bool(leg.get("debit"))
        # A negative amount simply flips the side, which keeps callers free of
        # sign bookkeeping.
        if amount < 0:
            amount = abs(amount)
            is_debit = not is_debit

        row: Dict[str, Any] = {
            "account": account,
            "debit_in_account_currency": amount if is_debit else 0,
            "credit_in_account_currency": 0 if is_debit else amount,
        }

        party_type = leg.get("party_type")
        party = leg.get("party")
        account_type = frappe.db.get_value("Account", account, "account_type")
        if account_type in {"Receivable", "Payable"} and not (party_type and party):
            frappe.throw(
                _("Return posting error: account {0} needs a party but none was supplied.").format(account)
            )
        if party_type and party:
            row["party_type"] = party_type
            row["party"] = party
        if leg.get("reference_type") and leg.get("reference_name"):
            row["reference_type"] = leg["reference_type"]
            row["reference_name"] = leg["reference_name"]

        je.append("accounts", row)
        total_debit += amount if is_debit else 0
        total_credit += 0 if is_debit else amount

    if abs(total_debit - total_credit) > _TOL:
        frappe.throw(
            _("Return posting error: {0} is unbalanced (debit {1} vs credit {2}).").format(
                je_type, total_debit, total_credit
            )
        )

    je.flags.ignore_permissions = True
    je.save(ignore_permissions=True)
    je.submit()
    return je.name


def plan_return_journal_legs(
    *,
    inv: Any,
    money_state: str,
    return_total: float,
    credit_note_name: str,
    pay_courier_for_trip: bool,
    courier: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Describe every Journal Entry a return will post, without posting any.

    Shared by the real run and by ``dry_run`` previews, so a dry run exercises
    the same account resolution and the same balancing arithmetic that the live
    posting will — which is what makes it a meaningful check against real data.
    """
    company = inv.company
    customer = inv.customer
    receivable = get_company_receivable_account(company)
    plans: List[Dict[str, Any]] = []

    if money_state == MONEY_UNPAID:
        # Nothing was ever collected: the credit note's debit and the invoice's
        # remaining receivable cancel out. Two lines on the same account, each
        # referencing its own voucher, so both outstandings land on zero.
        plans.append({
            "je_type": JE_AR_KNOCKOFF,
            "human": f"Return knock-off – {inv.name} / {credit_note_name}",
            "legs": [
                {
                    "account": receivable, "amount": return_total, "debit": True,
                    "party_type": "Customer", "party": customer,
                    "reference_type": "Sales Invoice", "reference_name": credit_note_name,
                },
                {
                    "account": receivable, "amount": return_total, "debit": False,
                    "party_type": "Customer", "party": customer,
                    "reference_type": "Sales Invoice", "reference_name": inv.name,
                },
            ],
        })

    elif money_state == MONEY_COURIER_OUTSTANDING:
        # The receivable was moved to Courier Outstanding at dispatch and the
        # courier never remitted. Give the customer their credit and release the
        # courier's liability in one entry.
        plans.append({
            "je_type": JE_COURIER_OUTSTANDING,
            "human": f"Return courier reversal – {inv.name} / {credit_note_name}",
            "legs": [
                {
                    "account": receivable, "amount": return_total, "debit": True,
                    "party_type": "Customer", "party": customer,
                    "reference_type": "Sales Invoice", "reference_name": credit_note_name,
                },
                {
                    "account": _get_courier_outstanding_account(company),
                    "amount": return_total, "debit": False,
                },
            ],
        })

    # MONEY_PREPAID / MONEY_COURIER_SETTLED post no reversal here: the money is
    # already in the branch, so the only question is the refund, handled
    # separately as a Payment Entry (or left as customer credit).

    if not pay_courier_for_trip and courier:
        shipping = flt(courier.get("shipping_amount"))
        if shipping > _TOL:
            party_type = courier.get("party_type")
            party = courier.get("party")
            plans.append({
                "je_type": JE_FREIGHT_REVERSAL,
                "human": f"Return freight reversal – {inv.name} / {credit_note_name}",
                "legs": [
                    {
                        "account": get_creditors_account(company), "amount": shipping, "debit": True,
                        "party_type": party_type, "party": party,
                    },
                    {
                        "account": get_freight_expense_account(company),
                        "amount": shipping, "debit": False,
                    },
                ],
            })

    return plans


def dry_run_return_postings(invoice_id: str) -> Dict[str, Any]:
    """Resolve and balance every posting a return would make — without writing.

    The highest-value check to run against real production-cloned data: it
    proves account resolution succeeds for every real branch/company/courier
    combination, and that each entry balances, with zero ledger impact.
    """
    inv = frappe.get_doc("Sales Invoice", str(invoice_id or "").strip())
    money_state = _money_state(inv)
    transactions = _unsettled_courier_transactions(inv.name)
    courier = next((t for t in transactions if flt(t.get("amount")) > _TOL), None)

    result: Dict[str, Any] = {
        "invoice_id": inv.name,
        "money_state": money_state,
        "ok": True,
        "entries": [],
        "errors": [],
    }

    for pay_courier in (True, False):
        try:
            plans = plan_return_journal_legs(
                inv=inv,
                money_state=money_state,
                return_total=flt(inv.grand_total),
                credit_note_name=f"DRYRUN-{inv.name}",
                pay_courier_for_trip=pay_courier,
                courier=courier,
            )
        except Exception as exc:
            result["ok"] = False
            result["errors"].append({"pay_courier_for_trip": pay_courier, "error": str(exc)})
            continue

        for plan in plans:
            debit = sum(flt(l["amount"]) for l in plan["legs"] if l.get("debit"))
            credit = sum(flt(l["amount"]) for l in plan["legs"] if not l.get("debit"))
            balanced = abs(debit - credit) <= _TOL
            if not balanced:
                result["ok"] = False
                result["errors"].append({
                    "je_type": plan["je_type"], "debit": debit, "credit": credit,
                })
            result["entries"].append({
                "pay_courier_for_trip": pay_courier,
                "je_type": plan["je_type"],
                "debit": debit,
                "credit": credit,
                "balanced": balanced,
                "accounts": [l["account"] for l in plan["legs"]],
            })

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Document builders
# ─────────────────────────────────────────────────────────────────────────────

def _build_credit_note(inv: Any, requested: Dict[str, float], request_id: str) -> Any:
    """Map the source invoice to a credit note limited to the requested lines."""
    from erpnext.controllers.sales_and_purchase_return import make_return_doc

    cn = make_return_doc("Sales Invoice", inv.name)

    keep = []
    for row in cn.items:
        source_row = row.get("sales_invoice_item")
        if source_row in requested:
            # make_return_doc negates quantities; honour the operator's amount.
            row.qty = -abs(requested[source_row])
            keep.append(row)
    if not keep:
        frappe.throw(_("No returnable lines were selected."))
    # Re-index after filtering, otherwise the surviving rows keep the original
    # idx values and the child table has gaps.
    for position, row in enumerate(keep, start=1):
        row.idx = position
    cn.items = keep

    # A credit note is an accounting correction, not a till transaction. Leaving
    # is_pos set would drag POS payment validation and the POS payments table
    # into a document whose money movement we post explicitly instead.
    cn.is_pos = 0
    cn.set("payments", [])
    cn.update_stock = 0
    cn.disable_rounded_total = inv.get("disable_rounded_total")

    # G13: woo_order_id / woo_order_number are copyable custom fields, so the
    # credit note inherits them and the unconditional on_submit outbound hook
    # would push a status update to the live store — re-opening a cancelled or
    # completed order, or creating a brand-new one when the id is blank.
    cn.woo_order_id = None
    cn.woo_order_number = None
    cn.flags.ignore_woo_outbound = True

    # Keep the credit note off the operational board entirely.
    for field in ("custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"):
        if cn.meta.get_field(field):
            cn.set(field, None)

    if cn.meta.get_field("custom_return_request_id"):
        cn.custom_return_request_id = request_id

    cn.run_method("calculate_taxes_and_totals")
    return cn


def _build_return_delivery_note(
    original_dn: str, inv_name: str, requested: Dict[str, float]
) -> Optional[Any]:
    """Map the original Delivery Note to a return DN for the requested lines."""
    from erpnext.controllers.sales_and_purchase_return import make_return_doc

    # ERPNext v16's mapper leaves `si_detail` EMPTY on the rows it produces and
    # records the source row in `dn_detail` instead, so the invoice line has to
    # be recovered by walking back through the original Delivery Note. Matching
    # on `si_detail` directly silently matched nothing and every return failed
    # with "no rows matching the selected lines".
    source_rows = frappe.get_all(
        "Delivery Note Item",
        filters={"parent": original_dn, "parenttype": "Delivery Note"},
        fields=["name", "si_detail"],
        limit_page_length=0,
    ) or []
    si_detail_by_dn_row = {r["name"]: r.get("si_detail") for r in source_rows}

    rdn = make_return_doc("Delivery Note", original_dn)

    keep = []
    for row in rdn.items:
        source_row = row.get("si_detail") or si_detail_by_dn_row.get(row.get("dn_detail"))
        if source_row in requested:
            row.qty = -abs(requested[source_row])
            # v16 rejects `against_sales_invoice` without its paired `si_detail`,
            # so stamp both explicitly rather than trusting the mapper.
            row.against_sales_invoice = inv_name
            row.si_detail = source_row
            keep.append(row)
    if not keep:
        return None
    for position, row in enumerate(keep, start=1):
        row.idx = position
    rdn.items = keep
    rdn.flags.ignore_woo_outbound = True
    return rdn


def _compensating_courier_transaction(
    inv: Any, courier: Dict[str, Any], *, return_total: float,
    shipping_reversed: float, credit_note_name: str,
) -> Optional[str]:
    """Insert a negative-amount Courier Transaction that cancels the original.

    Courier Transaction is not submittable and has only Unsettled/Settled — there
    is no reversal state to move to. A compensating negative row is the idiom
    that already works everywhere else: ``_create_settlement_journal_entry``
    flips debit/credit per leg for negative amounts and
    ``_summarize_courier_transactions`` simply sums, so the ``+X`` / ``-X`` pair
    nets to zero and ``_requires_settlement_entry`` emits no settlement leg for
    it.

    That last part is load-bearing and easy to misread as a double reversal: the
    Journal Entry posted alongside this row is what actually moves the GL. This
    row exists so the courier's *operational* balance stops showing the order.
    """
    try:
        ct = frappe.new_doc("Courier Transaction")
        ct.party_type = courier.get("party_type")
        ct.party = courier.get("party")
        ct.date = frappe.utils.now_datetime()
        ct.reference_invoice = inv.name
        ct.amount = -abs(flt(return_total))
        ct.shipping_amount = -abs(flt(shipping_reversed))
        ct.status = "Unsettled"
        ct.payment_mode = "Deferred"
        ct.idempotency_token = f"RET::{credit_note_name}"
        ct.notes = f"Return reversal for {inv.name} (credit note {credit_note_name})"
        if courier.get("delivery_partner"):
            ct.delivery_partner = courier.get("delivery_partner")
        ct.flags.ignore_permissions = True
        ct.insert(ignore_permissions=True)
        return ct.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Return: compensating CT failed for {inv.name}")
        return None


def _compensating_sales_partner_transaction(
    inv: Any, *, ratio: float, credit_note_name: str
) -> Optional[str]:
    """Negate the partner's commission row in proportion to what came back."""
    partner = inv.get("sales_partner")
    if not partner:
        return None
    try:
        rows = frappe.get_all(
            "Sales Partner Transactions",
            filters={"reference_invoice": inv.name},
            fields=["name", "amount", "partner_fees", "base_fee", "vat_amount", "status", "pos_profile"],
            limit_page_length=5,
        ) or []
        original = next((r for r in rows if flt(r.get("amount")) > 0), None)
        if not original:
            return None

        # The token field is UNIQUE, so a compensating row must not reuse the
        # dispatch token (f"SPTRN::{invoice}") or the insert is rejected.
        token = f"SPTRN-RET::{credit_note_name}"
        if frappe.db.exists("Sales Partner Transactions", {"idempotency_token": token}):
            return None

        txn = frappe.new_doc("Sales Partner Transactions")
        txn.sales_partner = partner
        txn.pos_profile = original.get("pos_profile")
        txn.status = "Unsettled"
        txn.date = frappe.utils.now()
        txn.reference_invoice = inv.name
        txn.amount = -abs(flt(original.get("amount"))) * ratio
        txn.partner_fees = -abs(flt(original.get("partner_fees"))) * ratio
        txn.base_fee = -abs(flt(original.get("base_fee"))) * ratio
        txn.vat_amount = -abs(flt(original.get("vat_amount"))) * ratio
        txn.idempotency_token = token
        txn.notes = f"Return reversal for {inv.name} (credit note {credit_note_name})"
        txn.flags.ignore_permissions = True
        txn.insert(ignore_permissions=True)
        return txn.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Return: compensating SPT failed for {inv.name}")
        return None


def _create_refund_payment_entry(
    inv: Any, *, credit_note_name: str, amount: float, branch: str
) -> Optional[str]:
    """Pay the customer back out of branch cash against the credit note."""
    if amount <= _TOL:
        return None

    company = inv.company
    cash_account = get_pos_cash_account(branch, company)
    validate_account_exists(cash_account)
    receivable = get_company_receivable_account(company)

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Pay"
    pe.company = company
    pe.posting_date = frappe.utils.getdate()
    pe.posting_time = frappe.utils.nowtime()
    pe.party_type = "Customer"
    pe.party = inv.customer
    pe.paid_from = cash_account
    pe.paid_to = receivable
    pe.party_account = receivable
    pe.paid_amount = amount
    pe.received_amount = amount
    pe.reference_no = f"RET-{credit_note_name}"
    pe.reference_date = frappe.utils.getdate()

    try:
        if frappe.get_meta("Payment Entry").get_field("custom_kanban_profile"):
            pe.custom_kanban_profile = branch
    except Exception:
        pass

    pe.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": credit_note_name,
        "total_amount": -abs(amount),
        "outstanding_amount": -abs(amount),
        "allocated_amount": -abs(amount),
    })

    pe.flags.ignore_permissions = True
    try:
        pe.set_missing_values()
    except Exception:
        pass
    pe.insert(ignore_permissions=True)
    pe.submit()
    return pe.name


def _stamp_return_state(
    inv: Any, *, requested: Dict[str, float], returned_before: Dict[str, float],
    reason: str, notes: Optional[str], return_total: float,
) -> str:
    """Record the return on the source invoice and say whether it is now complete."""
    fully = True
    for row in inv.items:
        sold = flt(row.qty)
        done = flt(returned_before.get(row.name)) + flt(requested.get(row.name, 0.0))
        if done < sold - _TOL:
            fully = False
            break

    status = "Fully Returned" if fully else "Partially Returned"
    reason_text = reason if not notes else f"{reason}\nNotes: {notes}"
    prior = flt(inv.get("custom_returned_amount"))

    update_submitted_sales_invoice_fields(inv, {
        "custom_return_status": status,
        "custom_return_reason": reason_text,
        "custom_returned_amount": prior + abs(flt(return_total)),
    })
    return status


def run_invoice_return(
    *,
    invoice_id: str,
    lines: Any,
    reason: str,
    return_type: str = "Customer Return",
    refund_mode: str = REFUND_CUSTOMER_CREDIT,
    pay_courier_for_trip: bool = True,
    notes: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a return. Never raises to the caller; returns a result dict.

    Structured after ``api.manager._run_invoice_amendment_job``: advisory lock →
    re-verify under the lock → savepoint → build the graph → roll back wholesale
    on any failure. It deliberately shares the amendment's lock key so an amend
    and a return can never interleave on the same invoice.
    """
    invoice_id = str(invoice_id or "").strip()
    reason = str(reason or "").strip()
    notes = (notes or "").strip() or None
    refund_mode = str(refund_mode or "").strip() or REFUND_CUSTOMER_CREDIT
    return_type = str(return_type or "").strip() or "Customer Return"
    pay_courier_for_trip = _coerce_bool(pay_courier_for_trip)

    if not invoice_id:
        return {"success": False, "error": _("invoice_id is required")}
    if not reason:
        return {"success": False, "error": _("A return reason is required")}
    if refund_mode not in REFUND_MODES:
        return {"success": False, "error": _("Unknown refund mode: {0}").format(refund_mode)}
    if return_type not in RETURN_TYPES:
        return {"success": False, "error": _("Unknown return type: {0}").format(return_type)}
    if not returns_enabled():
        return {"success": False, "error": _("The return workflow is not enabled.")}

    requested_rows = _parse_lines(lines)
    if not requested_rows:
        return {"success": False, "error": _("Select at least one line to return")}

    inv = frappe.get_doc("Sales Invoice", invoice_id)
    try:
        ensure_profile_scoped_invoice_access(inv, action_label="returning this order")
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    request_id = str(request_id or "").strip() or "ret-" + hashlib.sha256(
        json.dumps({
            "invoice": invoice_id,
            "lines": sorted((r["si_detail"], r["qty"]) for r in requested_rows),
            "refund_mode": refund_mode,
            "pay_courier_for_trip": pay_courier_for_trip,
            "return_type": return_type,
        }, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]

    # Replay guard before taking any lock.
    existing = frappe.get_all(
        "Sales Invoice",
        filters={"custom_return_request_id": request_id, "docstatus": 1},
        pluck="name",
        limit_page_length=1,
    ) or []
    if existing:
        return {
            "success": True, "already_processed": True,
            "request_id": request_id, "credit_note": existing[0], "invoice_id": invoice_id,
        }

    logger = frappe.logger("jarz_pos.invoice_return")
    lock_key = f"inv:{invoice_id}"
    lock_taken = False
    savepoint = f"invoice_return_{hashlib.sha1(request_id.encode('utf-8')).hexdigest()[:10]}"

    try:
        try:
            lock_taken = bool(frappe.db.sql("SELECT GET_LOCK(%s, 5)", (lock_key,))[0][0])
        except Exception:
            lock_taken = False
        if not lock_taken:
            return {
                "success": False, "error": _("This order is being modified elsewhere; try again."),
                "return_block_code": "invoice_locked",
            }

        # Re-read and re-verify under the lock: eligibility computed before the
        # lock can be stale by the time we hold it.
        inv = frappe.get_doc("Sales Invoice", invoice_id)
        eligibility = get_invoice_return_eligibility(inv)
        if not eligibility.get("can_return"):
            return {
                "success": False,
                "error": eligibility.get("return_block_reason"),
                "return_block_code": eligibility.get("return_block_code"),
            }

        returned_before = _returned_qty_by_row(invoice_id)
        row_by_name = {row.name: row for row in inv.items}
        requested: Dict[str, float] = {}
        for entry in requested_rows:
            source_row = row_by_name.get(entry["si_detail"])
            if not source_row:
                return {"success": False, "error": _("Unknown invoice line: {0}").format(entry["si_detail"])}
            remaining = flt(source_row.qty) - flt(returned_before.get(source_row.name))
            if entry["qty"] > remaining + _TOL:
                return {
                    "success": False,
                    "error": _("Cannot return {0} of {1}: only {2} remain.").format(
                        entry["qty"], source_row.item_code, max(remaining, 0)
                    ),
                    "return_block_code": "over_return",
                }
            requested[source_row.name] = entry["qty"]

        money_state = _money_state(inv)
        branch = get_invoice_branch(inv)
        transactions = _unsettled_courier_transactions(invoice_id)
        courier = next((t for t in transactions if flt(t.get("amount")) > _TOL), None)

        if refund_mode == REFUND_NOW:
            if money_state not in {MONEY_PREPAID, MONEY_COURIER_SETTLED}:
                return {
                    "success": False,
                    "error": _("There is no collected payment to refund for this order."),
                    "return_block_code": "nothing_to_refund",
                }
            try:
                ensure_open_shift_for_invoice(inv, action_label="refunding a return")
            except (ShiftRequiredError, ClosedShiftError) as shift_err:
                return {
                    "success": False,
                    "error": str(getattr(shift_err, "message", None) or shift_err),
                    "return_block_code": "shift_required",
                }

        try:
            frappe.db.savepoint(savepoint)
        except Exception:
            savepoint = ""

        try:
            original_dn = _original_delivery_note(invoice_id)

            # 1. Stock back first — if this fails nothing else should exist.
            rdn = _build_return_delivery_note(original_dn, invoice_id, requested)
            if rdn is None:
                raise frappe.ValidationError(
                    _("The original Delivery Note has no rows matching the selected lines.")
                )
            rdn.flags.ignore_permissions = True
            rdn.insert(ignore_permissions=True)
            rdn.submit()

            # 2. Revenue reversal + customer credit.
            cn = _build_credit_note(inv, requested, request_id)
            if cn.meta.get_field("custom_return_type"):
                cn.custom_return_type = return_type
            if cn.meta.get_field("custom_return_courier_paid"):
                cn.custom_return_courier_paid = 1 if pay_courier_for_trip else 0
            if cn.meta.get_field("custom_return_refund_mode"):
                cn.custom_return_refund_mode = (
                    "Refund Now" if refund_mode == REFUND_NOW else "Customer Credit"
                )
            cn.flags.ignore_permissions = True
            cn.insert(ignore_permissions=True)
            cn.submit()

            return_total = abs(flt(cn.grand_total))
            ratio = (return_total / flt(inv.grand_total)) if flt(inv.grand_total) else 1.0
            ratio = min(max(ratio, 0.0), 1.0)

            # 3. Money that already moved.
            journal_entries: List[str] = []
            for plan in plan_return_journal_legs(
                inv=inv,
                money_state=money_state,
                return_total=return_total,
                credit_note_name=cn.name,
                pay_courier_for_trip=pay_courier_for_trip,
                courier=courier,
            ):
                posted = _post_return_je(
                    company=inv.company,
                    dedup_key=cn.name,
                    je_type=plan["je_type"],
                    human=plan["human"],
                    legs=plan["legs"],
                )
                if posted:
                    journal_entries.append(posted)

            # 4. Operational counter-rows.
            compensating_ct = None
            if courier and money_state == MONEY_COURIER_OUTSTANDING:
                compensating_ct = _compensating_courier_transaction(
                    inv, courier,
                    return_total=return_total,
                    shipping_reversed=(0.0 if pay_courier_for_trip else flt(courier.get("shipping_amount"))),
                    credit_note_name=cn.name,
                )
            compensating_spt = _compensating_sales_partner_transaction(
                inv, ratio=ratio, credit_note_name=cn.name
            )

            # 5. Refund or leave as credit.
            refund_pe = None
            if refund_mode == REFUND_NOW:
                refund_pe = _create_refund_payment_entry(
                    inv, credit_note_name=cn.name, amount=return_total, branch=branch
                )

            # 6. Source-invoice state.
            return_status = _stamp_return_state(
                inv, requested=requested, returned_before=returned_before,
                reason=reason, notes=notes, return_total=return_total,
            )

            # 7. Promo redemptions — only on a full return. On a partial the
            # order still met the promo's minimums, so the redemption stands.
            promo_reversed = False
            if return_status == "Fully Returned":
                try:
                    from jarz_pos.services.promo_codes import reverse_redemptions_on_cancel

                    reverse_redemptions_on_cancel(inv)
                    promo_reversed = True
                except Exception:
                    frappe.log_error(
                        frappe.get_traceback(), f"Return: promo reversal failed for {inv.name}"
                    )

            # Consumables are deliberately NOT reversed: the packaging was
            # physically used on the trip and stays a real cost.

            audit = (
                f"Return processed by {frappe.session.user}\n"
                f"Reason: {reason}\n"
                f"Type: {return_type} | Refund: {refund_mode} | "
                f"Courier paid for trip: {pay_courier_for_trip}\n"
                f"Credit note: {cn.name} | Return DN: {rdn.name}\n"
                f"Request ID: {request_id}"
            )
            for target in (inv.name, cn.name):
                try:
                    frappe.get_doc("Sales Invoice", target).add_comment("Comment", audit)
                except Exception:
                    pass

            result = {
                "success": True,
                "request_id": request_id,
                "invoice_id": inv.name,
                "credit_note": cn.name,
                "return_delivery_note": rdn.name,
                "journal_entries": journal_entries,
                "compensating_courier_transaction": compensating_ct,
                "compensating_sales_partner_transaction": compensating_spt,
                "refund_payment_entry": refund_pe,
                "return_status": return_status,
                "money_state": money_state,
                "return_total": return_total,
                "promo_reversed": promo_reversed,
            }

            try:
                from jarz_pos.utils.realtime import publish_invoice_event_by_name
                from jarz_pos.constants import WS_EVENTS

                publish_invoice_event_by_name(WS_EVENTS.KANBAN_UPDATE, result, inv.name)
            except Exception:
                logger.warning(f"realtime publish failed for return on {inv.name}")

            logger.info({"event": "invoice_return_completed", **result})
            return result

        except Exception as exc:
            if savepoint:
                try:
                    frappe.db.rollback(save_point=savepoint)
                except Exception:
                    pass
            frappe.log_error(frappe.get_traceback(), f"invoice_return failed for {invoice_id}")
            return {"success": False, "request_id": request_id, "error": str(exc)}

    finally:
        if lock_taken:
            try:
                frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_key,))
            except Exception:
                pass

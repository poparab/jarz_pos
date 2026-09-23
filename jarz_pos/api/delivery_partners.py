"""Jarz POS – Delivery Partner API endpoints.

A delivery partner is a courier COMPANY whose riders deliver for us. Its riders are
pure carriers: on a cash order the rider hands the branch the full amount he
collected, and on a prepaid order he carries nothing — in both cases he is paid no
fee out of that money. What the partner charges for the trip is a debt to the
company, accrued at dispatch and cleared by one weekly bank transfer.

So the partner ledger is one-directional: we always owe them. These endpoints read
that balance and pay it.

Two things the weekly run has to support, because the partner sends their own
invoice and it does not always agree with ours:

  * Reconciliation — settle only the trips you actually agree with, by passing an
    explicit ``courier_transactions`` list. Anything you leave out stays unbilled
    and shows up again next week.
  * Fixed charges — waiting time, a returned-trip charge. Those were never
    accrued per order, so they are expensed at payment time via ``extra_charges``.
  * Recurring fees — a fee the partner charges every day / week / month whether
    or not they delivered (Deliverk: 50 EGP a day). Those ARE accrued, one
    ``Delivery Partner Fee Accrual`` per period, by
    ``services/partner_recurring_fees.py``. Each period is listed next to the
    trips (``row_type = "recurring_fee"``) and is ticked and paid the same way:
    its name travels in the same ``courier_transactions`` list, so the app's
    settlement screen needs no change to pay it.
"""
from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _

from jarz_pos.constants import ROLES
from jarz_pos.services.delivery_handling import create_partner_settlement_je
from jarz_pos.services.partner_recurring_fees import (
    accrual_label,
    lock_accruals_for_settlement,
    mark_accruals_settled,
    split_accrual_names,
    unsettled_accruals,
)


def _ensure_delivery_partner_access() -> None:
    """Gate every Delivery Partner endpoint at the manager tier.

    A Delivery Partner is a courier COMPANY, and the balance these endpoints read and
    pay is a company-level payable — accrued at dispatch, cleared by one weekly bank
    transfer. There is no branch dimension to it at all: unlike courier-settlement
    reversal (branch-scoped floor work over a rider's cash), no branch "owns" a
    delivery partner, so there is no floor-supervisor slice of this to hand the
    line-manager tier the way ``STOCK_TRANSFER`` does. ``settle_delivery_partner``
    clears the payable and posts a real bank transfer, and its caller-supplied
    ``extra_charges`` are expensed at payment time — money leaving the company exactly
    like ``api/cash_transfer.py``'s transfers, which is why this mirrors
    ``ROLES.MANAGER`` rather than inventing a wider set. The two read endpoints are
    gated the same because they preview that same payable; a plain POS user has no
    business seeing what the company owes a courier partner either.
    """
    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.MANAGER):
        frappe.throw(_("Not permitted: Managers only"), frappe.PermissionError)


def _coerce_rows(value) -> list:
    """Accept a JSON string or a real list — Frappe sends list args as JSON over HTTP."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if isinstance(value, dict):
        value = [value]
    return list(value) if isinstance(value, list) else []


@frappe.whitelist()
def get_delivery_partner_balances(delivery_partner: str | None = None):
    """Return the unbilled fee total per Delivery Partner.

    Reads ``partner_fee`` on trips not yet marked ``partner_settled``. This is
    deliberately independent of the Courier Transaction's ``status``, which tracks
    the RIDER's cash: a prepaid partner trip has no cash position at all and is born
    Settled, yet its fee is still owed to the partner company.

    Returns rows of: {delivery_partner, partner_name, order_count, total_fee,
    total_shipping, unsettled_count, total_shipping_fee, oldest_date}. The last
    three are aliases kept so older dashboards keep rendering.
    """
    _ensure_delivery_partner_access()
    rows = frappe.db.sql("""
        SELECT
            ct.delivery_partner,
            dp.partner_name,
            COUNT(*) AS order_count,
            SUM(COALESCE(ct.partner_fee, 0)) AS total_fee,
            MIN(ct.date) AS oldest_date
        FROM `tabCourier Transaction` ct
        LEFT JOIN `tabDelivery Partner` dp ON dp.name = ct.delivery_partner
        WHERE COALESCE(ct.is_partner_order, 0) = 1
          AND COALESCE(ct.partner_settled, 0) = 0
          AND ct.delivery_partner IS NOT NULL
          {partner_filter}
        GROUP BY ct.delivery_partner, dp.partner_name
        ORDER BY total_fee DESC
    """.format(
        partner_filter=("AND ct.delivery_partner = %(delivery_partner)s" if delivery_partner else "")
    ), {"delivery_partner": delivery_partner} if delivery_partner else {}, as_dict=True)

    by_partner = {r["delivery_partner"]: r for r in rows}
    for r in rows:
        r["trip_count"] = int(r.get("order_count") or 0)
        r["trip_fee_total"] = float(r.get("total_fee") or 0)
        r["recurring_fee_count"] = 0
        r["recurring_fee_total"] = 0.0

    # Recurring fees are owed too. A partner with no trips at all this week still
    # has its daily fee outstanding, so it must appear on this list.
    for a in unsettled_accruals(delivery_partner):
        r = by_partner.get(a["delivery_partner"])
        if r is None:
            r = frappe._dict(
                delivery_partner=a["delivery_partner"],
                partner_name=frappe.db.get_value(
                    "Delivery Partner", a["delivery_partner"], "partner_name"
                ),
                order_count=0, total_fee=0.0, oldest_date=None,
                trip_count=0, trip_fee_total=0.0,
                recurring_fee_count=0, recurring_fee_total=0.0,
            )
            by_partner[a["delivery_partner"]] = r
            rows.append(r)
        r["recurring_fee_count"] += 1
        r["recurring_fee_total"] = round(r["recurring_fee_total"] + float(a.get("amount") or 0), 2)
        ps = a.get("period_start")
        if ps and (not r.get("oldest_date") or str(ps) < str(r["oldest_date"])[:10]):
            r["oldest_date"] = ps

    for r in rows:
        # ``order_count`` / ``total_fee`` are what the list screens show: every line
        # on the settlement screen and everything owed, recurring fees included.
        r["order_count"] = r["trip_count"] + r["recurring_fee_count"]
        total = round(r["trip_fee_total"] + r["recurring_fee_total"], 2)
        r["total_fee"] = total
        # Aliases for callers written against the older field names.
        r["total_shipping"] = total
        r["total_shipping_fee"] = total
        r["unsettled_count"] = r["order_count"]
    rows.sort(key=lambda r: r["total_fee"], reverse=True)
    return rows


@frappe.whitelist()
def get_delivery_partner_unsettled_details(delivery_partner: str):
    """Return the individual trips making up a partner's unbilled balance.

    This is the list you check against the partner's own invoice before paying.
    """
    _ensure_delivery_partner_access()
    if not delivery_partner:
        frappe.throw("delivery_partner is required")

    rows = frappe.get_all(
        "Courier Transaction",
        filters={
            "delivery_partner": delivery_partner,
            "is_partner_order": 1,
            "partner_settled": 0,
        },
        fields=[
            "name", "reference_invoice", "party_type", "party",
            "amount", "partner_fee", "shipping_amount", "date", "payment_mode", "status",
        ],
        order_by="date asc",
    )
    for r in rows:
        # ``invoice`` alias so the settlement screen can render one field name.
        r["invoice"] = r.get("reference_invoice")
        r["fee"] = float(r.get("partner_fee") or 0)
        r["row_type"] = "trip"

    # Recurring-fee periods ride in the same list, shaped like a trip so the
    # existing screens render and tick them unchanged. ``invoice`` carries the
    # human label ("Daily fee 2026-09-23") because it is the line's title there.
    for a in unsettled_accruals(delivery_partner):
        label = accrual_label(a)
        fee = float(a.get("amount") or 0)
        rows.append(frappe._dict(
            name=a["name"],
            row_type="recurring_fee",
            reference_invoice=None,
            invoice=label,
            description=label,
            party_type=None,
            party=None,
            amount=0.0,
            partner_fee=fee,
            fee=fee,
            shipping_amount=0.0,
            date=a.get("period_start"),
            period_start=a.get("period_start"),
            period_end=a.get("period_end"),
            frequency=a.get("frequency"),
            payment_mode=None,
            status="Recurring Fee",
        ))
    rows.sort(key=lambda r: str(r.get("date") or ""))
    return rows


@frappe.whitelist()
def settle_delivery_partner(
    delivery_partner: str,
    bank_account: str | None = None,
    courier_transactions=None,
    extra_charges=None,
):
    """Pay a Delivery Partner — the weekly bank transfer.

    One direction only: we owe them. The per-trip fees were expensed at dispatch, so
    paying them just clears the payable; the fixed charges off their invoice are
    expensed here because nothing accrued them earlier.

        DR Partner settlement_account (Σ selected trip fees)  [Supplier]
        DR Freight & Forwarding       (each fixed charge)
        CR Bank                        (total)

    Args:
        delivery_partner: the partner to pay.
        bank_account: ledger to pay from. Defaults to the partner's Bank Account,
            then the company default.
        courier_transactions: optional list of Courier Transaction names to settle —
            this is the reconciliation hook. Omit to settle everything unbilled.
            Names that are already settled, or belong to another partner, are
            refused rather than silently skipped.
        extra_charges: optional list of ``{"description", "amount", "account"}``
            rows for fixed charges on the partner's invoice.

    Returns a summary including the Journal Entry name.
    """
    _ensure_delivery_partner_access()
    if not delivery_partner:
        frappe.throw("delivery_partner is required")

    dp = frappe.get_doc("Delivery Partner", delivery_partner)

    selected = [str(n).strip() for n in _coerce_rows(courier_transactions) if str(n or "").strip()]
    charges = _coerce_rows(extra_charges)

    # Recurring-fee periods arrive in the same list as the trips (that is how the
    # screens tick them). Split them out by the table they live in.
    selected_trips, selected_fee_names = split_accrual_names(selected)

    filters = {
        "delivery_partner": delivery_partner,
        "is_partner_order": 1,
        "partner_settled": 0,
    }
    if selected:
        # All-fee selection: match no trip rather than falling through to "all".
        filters["name"] = ["in", selected_trips or [""]]

    unbilled = frappe.get_all(
        "Courier Transaction",
        filters=filters,
        fields=["name", "partner_fee", "reference_invoice"],
        order_by="date asc",
    )

    # The recurring-fee periods being paid, locked so two managers settling at
    # once cannot both clear the same day's fee.
    fee_rows = lock_accruals_for_settlement(
        delivery_partner, selected_fee_names if selected else None
    )

    if selected:
        # Say which names were rejected rather than quietly billing fewer trips than
        # the operator ticked — this screen exists precisely to make the total match
        # the partner's invoice.
        found = {r["name"] for r in unbilled} | {r["name"] for r in fee_rows}
        missing = [n for n in selected if n not in found]
        if missing:
            frappe.throw(
                "These are not unbilled trips or fee periods for {0}: {1}".format(
                    delivery_partner, ", ".join(missing[:10])
                )
            )

    charges_total = round(
        sum(float((c or {}).get("amount") or 0) for c in charges), 2
    )

    recurring_total = round(sum(float(r.get("amount") or 0) for r in fee_rows), 2)

    if not unbilled and not fee_rows and abs(charges_total) < 0.005:
        return {
            "success": True,
            "delivery_partner": delivery_partner,
            "order_count": 0,
            "total_fee": 0,
            "extra_charges_total": 0,
            "total_paid": 0,
            "message": "Nothing to settle",
        }

    if not dp.settlement_account:
        frappe.throw(
            "Delivery Partner has no settlement_account (Payable) configured. "
            "Set it on the Delivery Partner master."
        )

    fee_total = round(sum(float(ct.get("partner_fee") or 0) for ct in unbilled), 2)

    # Resolve bank ledger: explicit param > partner Bank Account > company default.
    if not bank_account and dp.bank_account:
        bank_account = frappe.db.get_value("Bank Account", dp.bank_account, "account")
    if not bank_account:
        first_inv = unbilled[0].get("reference_invoice") if unbilled else None
        company0 = frappe.db.get_value("Sales Invoice", first_inv, "company") if first_inv else None
        if not company0:
            company0 = frappe.db.get_value("Account", dp.settlement_account, "company")
        if company0:
            bank_account = frappe.db.get_value("Company", company0, "default_bank_account")
    if not bank_account:
        frappe.throw("No bank account found. Set it on the Delivery Partner or pass bank_account.")

    company = frappe.db.get_value("Account", bank_account, "company")
    if not company:
        frappe.throw(f"Cannot determine company from bank account {bank_account}")

    # Deterministic per-batch idempotency token (stable across retries of the same
    # set, and sensitive to the fixed charges so a corrected total posts its own entry).
    token_src = "|".join(sorted(str(ct["name"]) for ct in unbilled))
    if fee_rows:
        # Appended only when present so a trips-only batch keeps the token (and so
        # the idempotency key) it had before recurring fees existed.
        token_src += "||fees:" + "|".join(sorted(str(r["name"]) for r in fee_rows))
    token_src += "||" + json.dumps(
        [
            {
                "d": str((c or {}).get("description") or ""),
                "a": round(float((c or {}).get("amount") or 0), 2),
            }
            for c in charges
        ],
        sort_keys=True,
    )
    token = hashlib.sha1(token_src.encode("utf-8")).hexdigest()[:12]

    invoice_refs = ", ".join(
        str(ct.get("reference_invoice") or "") for ct in unbilled if ct.get("reference_invoice")
    )[:400]

    je_name = create_partner_settlement_je(
        delivery_partner=delivery_partner,
        company=company,
        bank_account=bank_account,
        order_fee_total=fee_total,
        extra_charges=charges,
        recurring_fee_total=recurring_total,
        token=token,
        human=(
            f"Delivery Partner settlement: {delivery_partner} "
            f"({len(unbilled)} trips, fees {fee_total}, "
            f"{len(fee_rows)} recurring {recurring_total}, fixed {charges_total}). "
            f"Invoices: {invoice_refs}"
        ),
    )

    # Mark the billed trips. This touches ONLY the partner-billing fields — the
    # rider's cash ``status`` is a separate question and is left exactly as it was.
    now = frappe.utils.now_datetime()
    for ct in unbilled:
        frappe.db.set_value(
            "Courier Transaction",
            ct["name"],
            {
                "partner_settled": 1,
                "partner_settlement_je": je_name,
                "partner_settled_on": now,
            },
            update_modified=False,
        )
    mark_accruals_settled([r["name"] for r in fee_rows], je_name, now)

    frappe.db.commit()

    return {
        "success": True,
        "delivery_partner": delivery_partner,
        "partner_name": dp.partner_name,
        "order_count": len(unbilled),
        "total_fee": fee_total,
        "extra_charges": charges,
        "extra_charges_total": charges_total,
        "recurring_fee_count": len(fee_rows),
        "recurring_fee_total": recurring_total,
        "total_paid": round(fee_total + recurring_total + charges_total, 2),
        "bank_account": bank_account,
        "journal_entry": je_name,
    }


@frappe.whitelist()
def accrue_delivery_partner_recurring_fees(delivery_partner: str | None = None):
    """Accrue any recurring-fee periods that are due, now, instead of on the hour.

    The same idempotent job the scheduler runs, for a manager who has just set a
    partner's fee (or back-dated its start) and wants the balance right away.
    Returns ``{partner: number of periods posted}``.
    """
    _ensure_delivery_partner_access()
    from jarz_pos.services.partner_recurring_fees import (
        accrue_partner,
        run_partner_recurring_fees,
    )

    if delivery_partner:
        created = accrue_partner(delivery_partner)
        frappe.db.commit()
        return {delivery_partner: len(created)}
    return run_partner_recurring_fees()

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
  * Fixed charges — a subscription, waiting time, a returned-trip charge. Those
    were never accrued per order, so they are expensed at payment time via
    ``extra_charges``.
"""
from __future__ import annotations

import hashlib
import json

import frappe

from jarz_pos.services.delivery_handling import create_partner_settlement_je


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

    for r in rows:
        total = float(r.get("total_fee") or 0)
        r["total_fee"] = total
        # Aliases for callers written against the older field names.
        r["total_shipping"] = total
        r["total_shipping_fee"] = total
        r["unsettled_count"] = r.get("order_count")
    return rows


@frappe.whitelist()
def get_delivery_partner_unsettled_details(delivery_partner: str):
    """Return the individual trips making up a partner's unbilled balance.

    This is the list you check against the partner's own invoice before paying.
    """
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
    if not delivery_partner:
        frappe.throw("delivery_partner is required")

    dp = frappe.get_doc("Delivery Partner", delivery_partner)

    selected = [str(n).strip() for n in _coerce_rows(courier_transactions) if str(n or "").strip()]
    charges = _coerce_rows(extra_charges)

    filters = {
        "delivery_partner": delivery_partner,
        "is_partner_order": 1,
        "partner_settled": 0,
    }
    if selected:
        filters["name"] = ["in", selected]

    unbilled = frappe.get_all(
        "Courier Transaction",
        filters=filters,
        fields=["name", "partner_fee", "reference_invoice"],
        order_by="date asc",
    )

    if selected:
        # Say which names were rejected rather than quietly billing fewer trips than
        # the operator ticked — this screen exists precisely to make the total match
        # the partner's invoice.
        found = {r["name"] for r in unbilled}
        missing = [n for n in selected if n not in found]
        if missing:
            frappe.throw(
                "These courier transactions are not unbilled trips for {0}: {1}".format(
                    delivery_partner, ", ".join(missing[:10])
                )
            )

    charges_total = round(
        sum(float((c or {}).get("amount") or 0) for c in charges), 2
    )

    if not unbilled and abs(charges_total) < 0.005:
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
        token=token,
        human=(
            f"Delivery Partner settlement: {delivery_partner} "
            f"({len(unbilled)} trips, fees {fee_total}, fixed {charges_total}). "
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

    frappe.db.commit()

    return {
        "success": True,
        "delivery_partner": delivery_partner,
        "partner_name": dp.partner_name,
        "order_count": len(unbilled),
        "total_fee": fee_total,
        "extra_charges": charges,
        "extra_charges_total": charges_total,
        "total_paid": round(fee_total + charges_total, 2),
        "bank_account": bank_account,
        "journal_entry": je_name,
    }

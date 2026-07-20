"""Jarz POS – Sales Partner API endpoints.

Implements the §5-E fix: recognise Sales Partner (e.g. Talabat) commission + 14% VAT
on the general ledger via a dedicated "Settle Sales Partner" batch action.

Timing / treatment (owner decisions):
  * The fee-recognition Journal Entry is posted ONLY at this settlement step — never
    auto-posted at Out For Delivery.
  * Commission (``base_fee``) is an EXPENSE to ``Commission on Sales - {abbr}``.
  * The 14% VAT (``vat_amount``) is RECOVERABLE INPUT VAT to ``Input VAT - {abbr}``.
  * ONLINE-order fees credit the partner receivable ``{partner} - {abbr}`` (reducing it
    to the net that the partner will remit to us).
  * CASH-order fees credit a per-partner payable ``{partner} Payable - {abbr}`` (the
    commission we owe the partner, cleared later by a Payment Entry).

This endpoint recognises the FEE only. The actual bank remittance (partner pays us the
net for online orders / we pay the fee for cash orders) is a SEPARATE normal Payment
Entry against ``{partner} - {abbr}`` / ``{partner} Payable - {abbr}`` and is out of scope.

Endpoints:
  - ``settle_sales_partner`` – post the batch fee JE and mark SPTs settled.
  - ``get_sales_partner_balances`` – read-only unsettled totals per partner (for a UI).
"""
from __future__ import annotations

import hashlib

import frappe

from jarz_pos.constants import PAYMENT_MODES
from jarz_pos.services.delivery_handling import (
    PARTNER_FEES_VAT_RATE,
    _compute_sales_partner_fees,
    _find_existing_je_by_tag,
    _je_user_remark,
)
from jarz_pos.utils.account_utils import (
    ensure_input_vat_account,
    ensure_partner_payable_subaccount,
    ensure_partner_receivable_subaccount,
    get_sales_commission_account,
)

# Stable Journal Entry dedup type embedded in ``user_remark`` (see delivery_handling FIX 3).
JE_TYPE = "SALES_PARTNER_SETTLEMENT"

_SPT_FIELDS = [
    "name",
    "sales_partner",
    "reference_invoice",
    "amount",
    "partner_fees",
    "base_fee",
    "vat_amount",
    "payment_mode",
    "pos_profile",
    "date",
]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _r2(value) -> float:
    return round(_safe_float(value), 2)


def _split_spt_fees(row: dict) -> tuple[float, float]:
    """Return ``(base, vat)`` for a Sales Partner Transaction row.

    Prefers the persisted ``base_fee`` / ``vat_amount`` columns (populated forward from the
    §5-E change). For legacy rows lacking the split:
      1. Decompose the stored combined ``partner_fees`` total (``base = total / (1+VAT_RATE)``),
         which is exact because ``total = base + base*VAT_RATE``.
      2. If ``partner_fees`` is also missing (e.g. old kanban-created rows), recompute from the
         source invoice via ``_compute_sales_partner_fees``.
    """
    base = _safe_float(row.get("base_fee"))
    vat = _safe_float(row.get("vat_amount"))
    if base > 0 or vat > 0:
        return base, vat

    total = _safe_float(row.get("partner_fees"))
    if total > 0:
        base = round(total / (1.0 + PARTNER_FEES_VAT_RATE), 2)
        vat = round(total - base, 2)
        return base, vat

    # Last resort: recompute from the invoice using the partner's current commission rate.
    try:
        ref = row.get("reference_invoice")
        if ref and frappe.db.exists("Sales Invoice", ref):
            inv = frappe.get_doc("Sales Invoice", ref)
            online = (row.get("payment_mode") or "").strip() == PAYMENT_MODES.ONLINE
            fees = _compute_sales_partner_fees(inv, row.get("sales_partner"), online=online)
            return _safe_float(fees.get("base_fees")), _safe_float(fees.get("vat"))
    except Exception as exc:  # pragma: no cover - defensive
        frappe.logger().warning(
            f"_split_spt_fees fallback failed for SPT {row.get('name')}: {exc}"
        )
    return base, vat


def _resolve_company(spts: list[dict], pos_profile: str | None) -> str:
    """Resolve the company for the settlement JE from POS profile, SPTs, or defaults."""
    if pos_profile:
        company = frappe.db.get_value("POS Profile", pos_profile, "company")
        if company:
            return company
    for row in spts:
        pp = row.get("pos_profile")
        if pp:
            company = frappe.db.get_value("POS Profile", pp, "company")
            if company:
                return company
    for row in spts:
        ref = row.get("reference_invoice")
        if ref:
            company = frappe.db.get_value("Sales Invoice", ref, "company")
            if company:
                return company
    company = (
        frappe.defaults.get_user_default("company")
        or frappe.db.get_single_value("Global Defaults", "default_company")
    )
    if not company:
        frappe.throw("Could not resolve company for Sales Partner settlement")
    return company


def _ensure_partner_customer(sales_partner: str) -> str:
    """Return (create on demand) a Customer representing the Sales Partner.

    A JE line against a Receivable account requires a Customer party in ERPNext, and the
    downstream remittance Payment Entry (partner pays us the net) also uses this party.
    """
    if frappe.db.exists("Customer", sales_partner):
        return sales_partner
    existing = frappe.db.get_value("Customer", {"customer_name": sales_partner}, "name")
    if existing:
        return existing
    cust = frappe.new_doc("Customer")
    cust.customer_name = sales_partner
    cust.customer_type = "Company"
    group = (
        frappe.db.get_single_value("Selling Settings", "customer_group")
        or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    )
    territory = (
        frappe.db.get_single_value("Selling Settings", "territory")
        or frappe.db.get_value("Territory", {"is_group": 0}, "name")
    )
    if group:
        cust.customer_group = group
    if territory:
        cust.territory = territory
    cust.insert(ignore_permissions=True)
    return cust.name


def _ensure_partner_supplier(sales_partner: str) -> str:
    """Return (create on demand) a Supplier representing the Sales Partner.

    A JE line against a Payable account requires a Supplier party in ERPNext, and the
    downstream remittance Payment Entry (we pay the partner the fee) also uses this party.
    """
    if frappe.db.exists("Supplier", sales_partner):
        return sales_partner
    existing = frappe.db.get_value("Supplier", {"supplier_name": sales_partner}, "name")
    if existing:
        return existing
    sup = frappe.new_doc("Supplier")
    sup.supplier_name = sales_partner
    sup.supplier_type = "Company"
    group = (
        frappe.db.get_single_value("Buying Settings", "supplier_group")
        or frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    )
    if group:
        sup.supplier_group = group
    sup.insert(ignore_permissions=True)
    return sup.name


def _je_party_for_account(account: str, sales_partner: str) -> dict:
    """Return the ``party_type``/``party`` dict a JE line needs for this account.

    ERPNext ``Journal Entry.validate_party`` mandates a party for Receivable/Payable
    accounts; for any other account type it must be omitted. This keeps the settlement JE
    valid regardless of how the partner sub-accounts are typed.
    """
    account_type = frappe.db.get_value("Account", account, "account_type")
    if account_type == "Receivable":
        return {"party_type": "Customer", "party": _ensure_partner_customer(sales_partner)}
    if account_type == "Payable":
        return {"party_type": "Supplier", "party": _ensure_partner_supplier(sales_partner)}
    return {}


@frappe.whitelist()
def get_sales_partner_balances(sales_partner: str | None = None):
    """Return unsettled Sales Partner fee totals, aggregated per partner (read-only).

    If ``sales_partner`` is provided, only that partner is returned. Each entry carries the
    commission (``total_base``), VAT (``total_vat``) and combined fee split by payment mode so
    a future UI can preview the settlement JE before posting.
    """
    filters = {"status": "Unsettled"}
    if sales_partner:
        filters["sales_partner"] = sales_partner

    rows = frappe.get_all(
        "Sales Partner Transactions",
        filters=filters,
        fields=_SPT_FIELDS,
        order_by="sales_partner asc, date asc",
    )

    agg: dict[str, dict] = {}
    for row in rows:
        partner = row.get("sales_partner") or "(none)"
        entry = agg.setdefault(partner, {
            "sales_partner": partner,
            "order_count": 0,
            "total_amount": 0.0,
            "total_base": 0.0,
            "total_vat": 0.0,
            "total_fees": 0.0,
            "online_count": 0,
            "online_fees": 0.0,
            "cash_count": 0,
            "cash_fees": 0.0,
            "oldest_date": None,
        })
        base, vat = _split_spt_fees(row)
        fee = round(base + vat, 2)
        entry["order_count"] += 1
        entry["total_amount"] += _safe_float(row.get("amount"))
        entry["total_base"] += base
        entry["total_vat"] += vat
        entry["total_fees"] += fee
        if (row.get("payment_mode") or "").strip() == PAYMENT_MODES.ONLINE:
            entry["online_count"] += 1
            entry["online_fees"] += fee
        else:
            entry["cash_count"] += 1
            entry["cash_fees"] += fee
        dt = row.get("date")
        if dt and (entry["oldest_date"] is None or dt < entry["oldest_date"]):
            entry["oldest_date"] = dt

    out = []
    for entry in agg.values():
        for key in ("total_amount", "total_base", "total_vat", "total_fees", "online_fees", "cash_fees"):
            entry[key] = round(entry[key], 2)
        out.append(entry)
    out.sort(key=lambda e: e["total_fees"], reverse=True)
    return out


@frappe.whitelist()
def settle_sales_partner(sales_partner: str, pos_profile: str | None = None):
    """Post the batch commission + VAT recognition Journal Entry for a Sales Partner.

    Aggregates all ``Unsettled`` Sales Partner Transactions for the partner (optionally scoped
    to ``pos_profile``), splits them by payment mode, and posts ONE fee-recognition JE:

      Online portion:  DR Commission on Sales  (Σ base_online)
                       DR Input VAT            (Σ vat_online)
                       CR {partner} - {abbr}   (Σ fee_online)     [partner receivable]

      Cash portion:    DR Commission on Sales  (Σ base_cash)
                       DR Input VAT            (Σ vat_cash)
                       CR {partner} Payable    (Σ fee_cash)       [partner payable]

    (Debit lines for the two portions are combined into single Commission / Input VAT lines.)

    The JE is idempotent: its ``user_remark`` carries the tag
    ``[JARZ-JE:SALES_PARTNER_SETTLEMENT:{partner}:{token}]`` where ``token`` is a deterministic
    hash of the settled transaction set — a retry of the same batch reuses the existing JE
    instead of double-posting.

    Returns the JE name, the settled transaction list, and per-mode totals.
    """
    if not sales_partner:
        frappe.throw("sales_partner is required")
    if not frappe.db.exists("Sales Partner", sales_partner):
        frappe.throw(f"Sales Partner '{sales_partner}' not found")

    filters = {"sales_partner": sales_partner, "status": "Unsettled"}
    if pos_profile:
        filters["pos_profile"] = pos_profile

    spts = frappe.get_all(
        "Sales Partner Transactions",
        filters=filters,
        fields=_SPT_FIELDS,
        order_by="date asc",
    )
    if not spts:
        return {
            "success": True,
            "sales_partner": sales_partner,
            "settled_count": 0,
            "journal_entry": None,
            "message": "No unsettled transactions found",
        }

    base_online = vat_online = base_cash = vat_cash = 0.0
    online_names: list[str] = []
    cash_names: list[str] = []
    for row in spts:
        base, vat = _split_spt_fees(row)
        if (row.get("payment_mode") or "").strip() == PAYMENT_MODES.ONLINE:
            base_online += base
            vat_online += vat
            online_names.append(row["name"])
        else:
            base_cash += base
            vat_cash += vat
            cash_names.append(row["name"])

    base_online, vat_online = _r2(base_online), _r2(vat_online)
    base_cash, vat_cash = _r2(base_cash), _r2(vat_cash)
    fee_online = _r2(base_online + vat_online)
    fee_cash = _r2(base_cash + vat_cash)
    base_total = _r2(base_online + base_cash)
    vat_total = _r2(vat_online + vat_cash)
    fee_total = _r2(fee_online + fee_cash)

    if fee_total <= 0:
        frappe.throw("Total Sales Partner fees is zero — nothing to settle")

    company = _resolve_company(spts, pos_profile)

    commission_acc = get_sales_commission_account(company)
    input_vat_acc = ensure_input_vat_account(company)

    settled_names = sorted(online_names + cash_names)
    token = hashlib.md5("|".join(settled_names).encode("utf-8")).hexdigest()[:10]
    dedup_key = f"{sales_partner}:{token}"

    # Idempotency: reuse an already-posted JE for this exact batch (see delivery_handling FIX 3).
    je_name = _find_existing_je_by_tag(company, dedup_key, JE_TYPE)
    if not je_name:
        title = f"Sales Partner Settlement – {sales_partner}"
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = company
        je.title = title
        je.user_remark = _je_user_remark(
            dedup_key, JE_TYPE, f"{title} ({len(settled_names)} txns)"
        )

        # Debit: commission expense + recoverable input VAT (combined across both portions)
        if base_total > 0:
            je.append("accounts", {
                "account": commission_acc,
                "debit_in_account_currency": base_total,
                "credit_in_account_currency": 0,
            })
        if vat_total > 0:
            je.append("accounts", {
                "account": input_vat_acc,
                "debit_in_account_currency": vat_total,
                "credit_in_account_currency": 0,
            })

        # Credit online fees against the partner receivable (reduces it to the net remittance)
        if fee_online > 0:
            recv_acc = ensure_partner_receivable_subaccount(company, sales_partner)
            line = {
                "account": recv_acc,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": fee_online,
            }
            line.update(_je_party_for_account(recv_acc, sales_partner))
            je.append("accounts", line)

        # Credit cash fees against the per-partner payable (the fee we owe the partner)
        if fee_cash > 0:
            pay_acc = ensure_partner_payable_subaccount(sales_partner, company)
            line = {
                "account": pay_acc,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": fee_cash,
            }
            line.update(_je_party_for_account(pay_acc, sales_partner))
            je.append("accounts", line)

        je.insert(ignore_permissions=True)
        je.submit()
        je_name = je.name

    # Mark every included SPT settled and link it to the JE.
    for name in settled_names:
        frappe.db.set_value(
            "Sales Partner Transactions",
            name,
            {"status": "Settled", "journal_entry": je_name},
            update_modified=False,
        )
    frappe.db.commit()

    return {
        "success": True,
        "sales_partner": sales_partner,
        "company": company,
        "journal_entry": je_name,
        "settled_count": len(settled_names),
        "settled_transactions": settled_names,
        "totals": {
            "online": {
                "count": len(online_names),
                "base": base_online,
                "vat": vat_online,
                "fee": fee_online,
            },
            "cash": {
                "count": len(cash_names),
                "base": base_cash,
                "vat": vat_cash,
                "fee": fee_cash,
            },
            "base_total": base_total,
            "vat_total": vat_total,
            "fee_total": fee_total,
        },
    }

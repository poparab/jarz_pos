"""Jarz POS – Courier workflow API endpoints.

This module exposes courier-related operations using the refactored
delivery handling services.
"""

from __future__ import annotations

import frappe

from jarz_pos.services.delivery_handling import (
    mark_courier_outstanding as _mark_courier_outstanding,
    pay_delivery_expense as _pay_delivery_expense,
    courier_delivery_expense_only as _courier_delivery_expense_only,
    get_courier_balances as _get_courier_balances,
    settle_courier as _settle_courier,
    settle_delivery_party as _settle_delivery_party,
    settle_courier_for_invoice as _settle_courier_for_invoice,
    handle_out_for_delivery_paid as _handle_out_for_delivery_paid,
    handle_out_for_delivery_transition as _handle_out_for_delivery_transition,
    settle_single_invoice_paid as _settle_single_invoice_paid,
    settle_courier_collected_payment as _settle_courier_collected_payment,
)
from jarz_pos.services.delivery_party import create_delivery_party as _create_delivery_party
from jarz_pos.api.invoices import pay_invoice as _pay_invoice  # reuse payment creation
from jarz_pos.services import delivery_handling as _delivery_services
from jarz_pos.services.settlement_strategies import dispatch_settlement as _dispatch_settlement
from jarz_pos.utils.account_utils import (
    get_freight_expense_account,
    get_pos_cash_account,
    validate_account_exists,
)
from frappe.utils import now_datetime, get_datetime


# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------


@frappe.whitelist()  # type: ignore[attr-defined]
def mark_courier_outstanding(invoice_name: str, courier: str | None = None, party_type: str | None = None, party: str | None = None):
    return _mark_courier_outstanding(invoice_name, courier, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def pay_delivery_expense(invoice_name: str, pos_profile: str):
    return _pay_delivery_expense(invoice_name, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def courier_delivery_expense_only(invoice_name: str, courier: str, party_type: str | None = None, party: str | None = None):
    return _courier_delivery_expense_only(invoice_name, courier, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def get_courier_balances():
    return _get_courier_balances()


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier(courier: str | None = None, pos_profile: str | None = None, party_type: str | None = None, party: str | None = None):
    """Backward-compatible settlement API.

    Preferred: pass party_type and party to settle unified delivery party.
    Fallback: pass courier (legacy label) which will settle legacy rows only.
    """
    if party_type and party:
        return _settle_delivery_party(party_type=party_type, party=party, pos_profile=pos_profile)
    if courier:
        return _settle_courier(courier, pos_profile)
    frappe.throw("Provide either party_type & party or courier")


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_delivery_party(party_type: str, party: str, pos_profile: str | None = None):
    return _settle_delivery_party(party_type=party_type, party=party, pos_profile=pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier_for_invoice(invoice_name: str, pos_profile: str | None = None):
    return _settle_courier_for_invoice(invoice_name, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def get_active_couriers():
    """Return unified list of delivery parties from Employee and Supplier groups named 'Delivery'.

    Output rows have shape:
      {"party_type": "Employee"|"Supplier", "party": name, "display_name": label}
    """
    out = []
    # Utility: check if a DocType has a given column in DB
    def _has_column(doctype: str, column: str) -> bool:
        try:
            return bool(frappe.db.has_column(doctype, column))
        except Exception:
            return False
    # Employees in Employee Group 'Delivery'
    emp_group = frappe.db.get_value("Employee Group", {"employee_group_name": "Delivery"}, "name")
    if emp_group:
        try:
            eg_doc = frappe.get_doc("Employee Group", emp_group)
            # Try common child table fieldnames first
            potential_tables = [
                "employees", "members", "employee_list", "employee_members",
                "employee_group_items", "employee_details",
            ]
            employee_names: set[str] = set()
            found_any = False
            for key in potential_tables:
                rows = eg_doc.get(key)
                if isinstance(rows, list) and rows:
                    for r in rows:
                        # child rows may have 'employee' link; fallback to 'employee_id'
                        emp = (r.get("employee") or r.get("employee_id") or "").strip()
                        if emp:
                            employee_names.add(emp)
                    found_any = True
                    break
            # If not found under known keys, scan any child list for 'employee' key
            if not found_any:
                data = eg_doc.as_dict() or {}
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict) and "employee" in v[0]:
                        for r in v:
                            emp = (r.get("employee") or r.get("employee_id") or "").strip()
                            if emp:
                                employee_names.add(emp)
                        found_any = True
                        break
            if employee_names:
                emp_fields = ["name", "employee_name"] + (["branch"] if _has_column("Employee", "branch") else [])
                emps = frappe.get_all(
                    "Employee",
                    fields=emp_fields,
                    filters={"name": ["in", list(employee_names)]},
                )
                out.extend({
                    "party_type": "Employee",
                    "party": e.name,
                    "display_name": (e.employee_name or e.name),
                    "branch": getattr(e, "branch", None) if hasattr(e, "branch") else (e.get("branch") if isinstance(e, dict) else None),
                } for e in emps)
        except Exception as err:
            frappe.log_error(f"Failed to read Employee Group members: {err}", "Jarz POS get_active_couriers")
    # Suppliers in Supplier Group 'Delivery'
    sup_group = frappe.db.get_value("Supplier Group", {"supplier_group_name": "Delivery"}, "name")
    if sup_group:
        sup_fields = ["name", "supplier_name"] + (["branch"] if _has_column("Supplier", "branch") else [])
        sups = frappe.get_all("Supplier", fields=sup_fields, filters={"supplier_group": sup_group})
        out.extend({
            "party_type": "Supplier",
            "party": s.name,
            "display_name": (s.supplier_name or s.name),
            "branch": getattr(s, "branch", None) if hasattr(s, "branch") else (s.get("branch") if isinstance(s, dict) else None),
        } for s in sups)
    return out


@frappe.whitelist()  # type: ignore[attr-defined]
def handle_out_for_delivery_paid(invoice_name: str, courier: str, settlement: str, pos_profile: str, party_type: str | None = None, party: str | None = None):
    # 'courier' kept only for backward compatibility; underlying service ignores legacy Courier DocType
    return _handle_out_for_delivery_paid(invoice_name, courier, settlement, pos_profile, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def handle_out_for_delivery_transition(invoice_name: str, courier: str, mode: str, pos_profile: str, idempotency_token: str | None = None, party_type: str | None = None, party: str | None = None):
    # 'courier' kept only for backward compatibility; underlying service ignores legacy Courier DocType
    return _handle_out_for_delivery_transition(invoice_name, courier, mode, pos_profile, idempotency_token, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_single_invoice_paid(invoice_name: str, pos_profile: str, party_type: str, party: str):
    """Settle a paid invoice's courier shipping fee individually (one-by-one settlement).

    Creates JE (DR Creditors [party] / CR Cash) and settles or creates Courier Transaction.
    Returns: { success, invoice, journal_entry, shipping_amount, party_type, party, courier_transactions }
    """
    return _settle_single_invoice_paid(invoice_name, pos_profile, party_type, party)

@frappe.whitelist(allow_guest=False)
def settle_courier_collected_payment(invoice_name: str, pos_profile: str, party_type: str, party: str):
    """Settle a courier collected payment.

    This function processes the collected payment for the courier.
    Returns: { success, invoice, payment_details }
    """
    return _settle_courier_collected_payment(invoice_name, pos_profile, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def debug_get_last_courier_transaction():
    """Return the most recent Courier Transaction (name, amounts, invoice) for debugging."""
    row = frappe.db.sql(
        """
        SELECT name, reference_invoice, amount, shipping_amount
        FROM `tabCourier Transaction`
        ORDER BY creation DESC
        LIMIT 1
        """,
        as_dict=True,
    )
    return row[0] if row else {}


@frappe.whitelist()  # type: ignore[attr-defined]
def debug_get_courier_transactions(invoice_name: str):
    rows = frappe.db.sql(
        """
        SELECT name, reference_invoice, amount, shipping_amount, status, payment_mode, notes
        FROM `tabCourier Transaction`
        WHERE reference_invoice=%s
        ORDER BY creation DESC
        """,
        (invoice_name,),
        as_dict=True,
    )
    return rows


@frappe.whitelist()  # type: ignore[attr-defined]
def create_delivery_party(
    party_type: str,
    name: str | None = None,
    phone: str | None = None,
    pos_profile: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
):
    """Create a new delivery party (Employee or Supplier) and return unified structure.

    Either provide (first_name & last_name) or a combined name.
    Returns: {party_type, party, display_name, phone}
    """
    # Use pos_profile as branch name per requirement
    return _create_delivery_party(
        party_type=party_type,
        name=name,
        phone=phone,
        branch=pos_profile,
        first_name=first_name,
        last_name=last_name,
    )


# ---------------------------------------------------------------------------
# Two-step settlement (Preview + Confirm) – server-driven, atomic on confirm
# ---------------------------------------------------------------------------

def _seconds_since(ts_str: str | None) -> int | None:
    if not ts_str:
        return None
    try:
        dt = get_datetime(ts_str)
        if not dt:
            return None
        return int((now_datetime() - dt).total_seconds())
    except Exception:
        return None


def _latest_payment_info(inv_name: str) -> dict | None:
    try:
        refs = frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Sales Invoice", "reference_name": inv_name},
            pluck="parent",
        )
        if not refs:
            return None
        rows = frappe.get_all(
            "Payment Entry",
            filters={"name": ["in", refs], "docstatus": 1, "payment_type": "Receive"},
            fields=["name", "creation", "posting_date", "posting_time", "modified"],
            order_by="creation desc",
            limit=1,
        )
        return rows[0] if rows else None
    except Exception:
        return None


@frappe.whitelist()  # type: ignore[attr-defined]
def generate_settlement_preview(invoice: str, party_type: str | None = None, party: str | None = None, mode: str = "pay_now", recent_payment_seconds: int = 30):
    """Produce a settlement preview and mint a short-lived token to be used on confirmation.

    Returns:
      {
        invoice, party_type, party, mode,
        order_amount, shipping_amount, net_amount,
        is_unpaid_effective, last_payment_seconds,
        preview_token, expires_in
      }
    """
    if not invoice:
        frappe.throw("invoice is required")

    inv = frappe.get_doc("Sales Invoice", invoice)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")

    # derive shipping via service helper (territory-aware in your services)
    try:
        shipping = float(_delivery_services._get_delivery_expense_amount(inv) or 0.0)  # type: ignore[attr-defined]
    except Exception:
        shipping = 0.0

    outstanding = float(inv.outstanding_amount or 0)
    status_l = (inv.status or "").strip().lower()

    last_pe = _latest_payment_info(inv.name)
    last_pe_seconds = _seconds_since(last_pe["creation"]) if last_pe else None

    # Effective unpaid when truly unpaid OR payment is too recent (within threshold)
    unpaid_status = status_l in {"unpaid", "overdue", "partially paid", "partly paid"}
    is_unpaid_effective = (outstanding > 0.009) or unpaid_status or (last_pe_seconds is not None and last_pe_seconds <= int(recent_payment_seconds))

    order_amount = float(inv.grand_total or 0) if is_unpaid_effective else 0.0
    net_amount = order_amount - shipping

    # include resolved party if not provided – from any existing CT linked to invoice
    if not (party_type and party):
        existing_party = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": inv.name,
                "party_type": ["not in", [None, ""]],
                "party": ["not in", [None, ""]],
            },
            fields=["party_type", "party"],
            limit=1,
        )
        if existing_party:
            party_type = existing_party[0].get("party_type")
            party = existing_party[0].get("party")

    token = frappe.generate_hash(length=16)
    cache_key = f"jarz_pos:settle_preview:{token}"
    frappe.cache().hset(
        cache_key,
        "data",
        {
            "invoice": inv.name,
            "party_type": party_type,
            "party": party,
            "mode": mode,
            "order_amount": order_amount,
            "shipping_amount": shipping,
            "net_amount": net_amount,
            "is_unpaid_effective": is_unpaid_effective,
            "last_payment_seconds": last_pe_seconds,
        },
    )
    # expire after 3 minutes
    frappe.cache().expire(cache_key, 180)

    return {
        "invoice": inv.name,
        "party_type": party_type,
        "party": party,
        "mode": mode,
        "order_amount": order_amount,
        "shipping_amount": shipping,
        "net_amount": net_amount,
        "is_unpaid_effective": is_unpaid_effective,
        "last_payment_seconds": last_pe_seconds,
        "preview_token": token,
        "expires_in": 180,
    }


@frappe.whitelist()  # type: ignore[attr-defined]
def confirm_settlement(invoice: str, preview_token: str, mode: str, pos_profile: str | None = None, party_type: str | None = None, party: str | None = None, payment_mode: str = "Cash"):
    """Confirm a previously previewed settlement atomically.

    If preview indicated unpaid and mode==pay_now, creates a Payment Entry, then performs
    Out For Delivery transition using unified delivery party details. All inside one transaction.
    """
    if not invoice:
        frappe.throw("invoice is required")
    if not preview_token:
        frappe.throw("preview_token is required")

    cache_key = f"jarz_pos:settle_preview:{preview_token}"
    data = frappe.cache().hget(cache_key, "data")
    if not data:
        frappe.throw("Preview expired or invalid. Please reopen the dialog.")
    if data.get("invoice") != invoice:
        frappe.throw("Preview does not match invoice. Please reopen the dialog.")

    # adopt party from preview if not provided
    party_type = party_type or data.get("party_type")
    party = party or data.get("party")

    # Build a non-empty courier label for legacy 'courier' arg required by services layer
    def _courier_label(pt: str | None, p: str | None) -> str:
        pt = (pt or "").strip()
        p = (p or "").strip()
        if not p:
            return "Courier"
        try:
            if pt == "Employee":
                return frappe.db.get_value("Employee", p, "employee_name") or p
            if pt == "Supplier":
                return frappe.db.get_value("Supplier", p, "supplier_name") or p
        except Exception:
            pass
        return p

    try:
        frappe.db.savepoint("confirm_settlement")
        # Map preview mode to our strategy mode keys
        strat_mode = "now" if (mode or data.get("mode")) in {"pay_now", "now"} else "later"
        # Use separated strategies to perform the correct accounting and CT/JE actions
        res = _dispatch_settlement(
            inv_name=invoice,
            mode=strat_mode,
            pos_profile=pos_profile,
            payment_type=payment_mode,
            party_type=party_type,
            party=party,
        )

        frappe.db.commit()
        # Invalidate token to prevent replays
        try:
            frappe.cache().delete_value(cache_key)
        except Exception:
            pass

        base = {
            "success": True,
            "invoice": invoice,
            "mode": mode,
            "order_amount": data.get("order_amount"),
            "shipping_amount": data.get("shipping_amount"),
            "net_amount": data.get("net_amount"),
            "is_unpaid_effective": data.get("is_unpaid_effective"),
            "party_type": party_type,
            "party": party,
        }
        base.update({k: v for k, v in (res or {}).items() if k not in base})
        return base
    except Exception as e:
        frappe.db.rollback(save_point="confirm_settlement")
        frappe.log_error(frappe.get_traceback(), "confirm_settlement failed")
        raise
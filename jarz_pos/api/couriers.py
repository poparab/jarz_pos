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
                emps = frappe.get_all(
                    "Employee",
                    fields=["name", "employee_name", "branch"],
                    filters={"name": ["in", list(employee_names)]},
                )
                out.extend({
                    "party_type": "Employee",
                    "party": e.name,
                    "display_name": e.employee_name or e.name,
                    "branch": getattr(e, "branch", None),
                } for e in emps)
        except Exception as err:
            frappe.log_error(f"Failed to read Employee Group members: {err}", "Jarz POS get_active_couriers")
    # Suppliers in Supplier Group 'Delivery'
    sup_group = frappe.db.get_value("Supplier Group", {"supplier_group_name": "Delivery"}, "name")
    if sup_group:
        sups = frappe.get_all("Supplier", fields=["name", "supplier_name", "branch"], filters={"supplier_group": sup_group})
        out.extend({
            "party_type": "Supplier",
            "party": s.name,
            "display_name": s.supplier_name or s.name,
            "branch": getattr(s, "branch", None),
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
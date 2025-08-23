"""
Delivery Handling Module for Jarz POS

This module handles all delivery and courier-related operations,
including outstanding management, expense tracking, and settlement.
"""

import frappe
from jarz_pos.jarz_pos.utils.account_utils import (
    get_freight_expense_account,
    get_courier_outstanding_account,
    get_pos_cash_account,
    validate_account_exists,
    get_creditors_account,
)


@frappe.whitelist()
def mark_courier_outstanding(invoice_name: str, courier: str | None = None, party_type: str | None = None, party: str | None = None):
    """Allocate outstanding to Courier Outstanding and create Courier Transaction atomically (relying on Frappe's request transaction)."""
    # Derive party if omitted
    if not (party_type and party):
        existing_party = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": invoice_name,
                "status": ["!=", "Settled"],
                "party_type": ["not in", [None, ""]],
                "party": ["not in", [None, ""]],
            },
            fields=["party_type", "party"],
            limit=1,
        )
        if existing_party:
            party_type = existing_party[0].party_type
            party = existing_party[0].party
        else:
            frappe.throw("party_type & party are required (courier must be an Employee or Supplier)")

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted before marking as courier outstanding.")
    if inv.outstanding_amount <= 0:
        frappe.throw("Invoice already paid – no outstanding amount to allocate.")

    company = inv.company
    outstanding = float(inv.outstanding_amount or 0)
    order_amount = float(inv.grand_total or outstanding)

    paid_to_account = _get_courier_outstanding_account(company)
    paid_from_account = _get_receivable_account(company)
    pe = _create_payment_entry(inv, paid_from_account, paid_to_account, outstanding)

    shipping_exp = _get_delivery_expense_amount(inv)
    je_name = None
    if shipping_exp and shipping_exp > 0:
        creditors_acc = get_creditors_account(company)
        je_name = _create_shipping_expense_to_creditors_je(inv, shipping_exp, creditors_acc, party_type, party)

    # Create Courier Transaction (values set before insert, no post set_value call)
    ct = frappe.new_doc("Courier Transaction")
    ct.party_type = party_type
    ct.party = party
    ct.date = frappe.utils.now_datetime()
    ct.reference_invoice = inv.name
    ct.amount = order_amount
    ct.shipping_amount = float(shipping_exp or 0)
    ct.status = "Unsettled"
    ct.payment_mode = "Deferred"
    ct.notes = "Courier Outstanding (collect order amount from courier)"
    ct.insert(ignore_permissions=True)

    # Update state (defer state commit to end of request)
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        try:
            inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
        except Exception:
            inv.set("custom_sales_invoice_state", "Out for Delivery")
            inv.save(ignore_permissions=True)

    frappe.publish_realtime(
        "jarz_pos_courier_outstanding",
        {
            "event": "jarz_pos_courier_outstanding",
            "invoice": inv.name,
            "courier": courier,
            "party_type": party_type,
            "party": party,
            "payment_entry": pe.name,
            "journal_entry": je_name,
            "courier_transaction": ct.name,
            "amount": order_amount,
            "shipping_amount": shipping_exp or 0,
            "net_to_collect": (order_amount - float(shipping_exp or 0)),
            "mode": "settle_later",
        },
    )
    return {
        "invoice": inv.name,
        "courier": courier,
        "party_type": party_type,
        "party": party,
        "payment_entry": pe.name,
        "journal_entry": je_name,
        "courier_transaction": ct.name,
        "amount": order_amount,
        "shipping_amount": shipping_exp or 0,
        "net_to_collect": (order_amount - float(shipping_exp or 0)),
        "mode": "settle_later",
    }


@frappe.whitelist()
def pay_delivery_expense(invoice_name: str, pos_profile: str):
    """
    Create (or return existing) Journal Entry for paying the courier's delivery
    expense in cash and, **atomically**, set the invoice operational state to
    "Out for delivery". This makes the endpoint idempotent – repeated calls for
    the same invoice will NOT generate duplicate Journal Entries.
    """
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")
    
    company = inv.company
    
    # Ensure the invoice is marked Out for delivery before proceeding.
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=False)
    
    # Determine expense amount based on invoice city
    amount = _get_delivery_expense_amount(inv)
    if amount <= 0:
        frappe.throw("No delivery expense configured for the invoice city.")
    
    # Idempotency guard – return existing submitted JE if already created
    existing_je = frappe.db.get_value(
        "Journal Entry",
        {
            "title": f"Courier Expense – {inv.name}",
            "company": company,
            "docstatus": 1,
        },
        "name",
    )
    if existing_je:
        return {"journal_entry": existing_je, "amount": amount}
    
    # Resolve ledgers for cash payment
    paid_from = get_pos_cash_account(pos_profile, company)
    paid_to = get_freight_expense_account(company)
    
    # Build Journal Entry (credit cash-in-hand, debit expense)
    je = _create_expense_journal_entry(inv, amount, paid_from, paid_to)
    
    # Fire realtime event so other sessions update cards instantly
    frappe.publish_realtime(
        "jarz_pos_courier_expense_paid",
        {"invoice": inv.name, "journal_entry": je.name, "amount": amount},
    )
    
    return {"journal_entry": je.name, "amount": amount}


@frappe.whitelist()
def courier_delivery_expense_only(invoice_name: str, courier: str, party_type: str | None = None, party: str | None = None):
    """
    Record courier delivery expense to be settled later.
    Creates a **Courier Transaction** of type *Pick-Up* with **negative** amount
    and note *delivery expense only* so that the courier's outstanding balance is
    reduced by the delivery fee they will collect from us.
    """
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")
    if not (party_type and party):
        existing_party = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": invoice_name,
                "status": ["!=", "Settled"],
                "party_type": ["not in", [None, ""]],
                "party": ["not in", [None, ""]],
            },
            fields=["party_type", "party"],
            limit=1,
        )
        if existing_party:
            party_type = existing_party[0].get("party_type")
            party = existing_party[0].get("party")
        else:
            frappe.throw("party_type & party are required (courier must be an Employee or Supplier)")
    
    # Ensure state is Out for delivery (idempotent)
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=False)
    
    amount = _get_delivery_expense_amount(inv)
    if amount <= 0:
        frappe.throw("No delivery expense configured for the invoice city.")
    
    # Idempotency – avoid duplicate CTs for same purpose
    existing_ct = frappe.db.get_value(
        "Courier Transaction",
        {
            "reference_invoice": inv.name,
            "type": "Pick-Up",
            "notes": ["like", "%delivery expense only%"],
        },
        "name",
    )
    if existing_ct:
        return {"courier_transaction": existing_ct, "amount": amount}
    
    # Insert Courier Transaction recording shipping expense separately (positive)
    ct = frappe.new_doc("Courier Transaction")
    ct.party_type = party_type
    ct.party = party
    ct.date = frappe.utils.now_datetime()
    ct.type = "Pick-Up"
    ct.reference_invoice = inv.name
    ct.amount = 0  # No principal amount involved – only shipping expense
    ct.shipping_amount = abs(amount)
    ct.notes = "delivery expense only (pay later)"
    ct.insert(ignore_permissions=True)
    
    frappe.publish_realtime(
        "jarz_pos_courier_expense_only",
        {
            "invoice": inv.name,
            "courier_transaction": ct.name,
            "shipping_amount": abs(amount),
        },
    )
    
    return {"courier_transaction": ct.name, "shipping_amount": abs(amount)}


@frappe.whitelist()
def get_courier_balances():
    """
    Return outstanding balances grouped by unified delivery party
    (Employee/Supplier) with a backward-compatible shape.

    Important: Avoid referencing a non-existent "Courier" DocType. We derive
    balances solely from `Courier Transaction` rows.

    Output rows include both the new unified keys and legacy keys for UI
    compatibility:
      {
        "party_type": "Employee"|"Supplier"|"",
        "party": "EMP-0001"|"SUP-0001"|"",
        "display_name": "John Doe"|"Vendor X"|"<Unknown>",
        "balance": 1250.0,
        "details": [ {"invoice": ..., "city": ..., "amount": ..., "shipping": ...}, ... ],
        # Legacy (kept for older clients):
        "courier": "<legacy courer id or party>",
        "courier_name": "<display_name>"
      }
    """
    # Fetch all unsettled transactions (party-based and legacy)
    rows = frappe.get_all(
        "Courier Transaction",
        filters={"status": ["!=", "Settled"]},
        fields=[
            "name",
            "reference_invoice",
            "amount",
            "shipping_amount",
            "party_type",
            "party",
            "courier",  # legacy field, may be None
        ],
    )

    # Group by party identity; fallback to legacy courier string
    groups: dict[tuple[str, str], dict] = {}

    def ensure_group(party_type: str, party: str, legacy_courier: str | None):
        key = (party_type or "", party or legacy_courier or "")
        if key not in groups:
            # Resolve display label
            label = None
            if key[0] == "Employee" and key[1]:
                try:
                    label = frappe.db.get_value("Employee", key[1], "employee_name") or key[1]
                except Exception:
                    label = key[1]
            elif key[0] == "Supplier" and key[1]:
                try:
                    label = frappe.db.get_value("Supplier", key[1], "supplier_name") or key[1]
                except Exception:
                    label = key[1]
            else:
                # Legacy or missing party – show the raw value or a placeholder
                label = (legacy_courier or party or "<Unknown>")

            groups[key] = {
                "party_type": key[0],
                "party": key[1],
                "display_name": label,
                # Legacy keys for older clients
                "courier": legacy_courier or key[1],
                "courier_name": label,
                "balance": 0.0,
                "details": [],
            }
        return groups[key]

    for r in rows:
        party_type = (r.get("party_type") or "").strip()
        party = (r.get("party") or "").strip()
        legacy_courier = (r.get("courier") or "").strip() or None

        grp = ensure_group(party_type, party, legacy_courier)
        amt = float(r.get("amount") or 0)
        ship = float(r.get("shipping_amount") or 0)
        grp["balance"] += amt - ship
        inv = r.get("reference_invoice")
        grp["details"].append({
            "invoice": inv,
            "city": _get_invoice_city(inv),
            "amount": amt,
            "shipping": ship,
        })

    # Render list sorted by balance desc
    data = list(groups.values())
    data.sort(key=lambda d: d.get("balance", 0.0), reverse=True)
    return data


@frappe.whitelist()
def settle_courier(courier: str, pos_profile: str | None = None):
    """Deprecated alias retained for compatibility. Uses party-based settlement when possible.

    """
    return settle_delivery_party(party_type="", party=courier, pos_profile=pos_profile)


@frappe.whitelist()
def settle_delivery_party(party_type: str | None = None, party: str | None = None, pos_profile: str | None = None):
    """Settle all Unsettled Courier Transaction rows for a delivery party (Employee/Supplier).

    Args:
        party_type: 'Employee' or 'Supplier' (optional if only legacy rows exist)
        party: party name/id; if omitted, function will settle all legacy rows without party
        pos_profile: POS Profile name to resolve Cash account
    """
    if not pos_profile:
        pos_profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
        if not pos_profile:
            frappe.throw("POS Profile is required to resolve Cash account")

    filters = {"status": ["!=", "Settled"]}
    if party_type and party:
        filters.update({"party_type": party_type, "party": party})
    else:
        # Legacy rows: no party info; settle those with empty party fields
        filters.update({"party_type": ["in", [None, ""]], "party": ["in", [None, ""]]})

    cts = frappe.get_all(
        "Courier Transaction",
        filters=filters,
        fields=["name", "amount", "shipping_amount"],
    )
    if not cts:
        frappe.throw("No unsettled courier transactions found for the selected party.")

    net_balance = 0.0
    for r in cts:
        net_balance += float(r.amount or 0) - float(r.shipping_amount or 0)

    company = frappe.defaults.get_global_default("company") or frappe.db.get_single_value("Global Defaults", "default_company")
    courier_outstanding_acc = _get_courier_outstanding_account(company)
    cash_acc = get_pos_cash_account(pos_profile, company)

    label = party or "<Legacy Courier>"
    je_name = None
    if abs(net_balance) > 0.005:
        je_name = _create_settlement_journal_entry(label, net_balance, company, cash_acc, courier_outstanding_acc)

    for r in cts:
        frappe.db.set_value("Courier Transaction", r.name, "status", "Settled")
    frappe.db.commit()

    frappe.publish_realtime(
        "jarz_pos_courier_settled",
        {"courier": label, "journal_entry": je_name, "net_balance": net_balance},
    )
    return {"journal_entry": je_name, "net_balance": net_balance}


@frappe.whitelist()
def settle_courier_for_invoice(invoice_name: str, pos_profile: str | None = None):
    """Settle courier outstanding for a single invoice."""
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")
    
    # Find related courier transactions
    cts = frappe.get_all(
        "Courier Transaction",
        filters={
            "reference_invoice": invoice_name,
            "status": ["!=", "Settled"]
        },
        fields=["name", "courier", "amount", "shipping_amount", "party_type", "party"],
    )
    
    if not cts:
        frappe.throw(f"No unsettled courier transactions found for invoice {invoice_name}")
    
    # Prefer unified party settlement if available
    first = cts[0]
    party_type = first.get("party_type")
    party = first.get("party")
    if party_type and party:
        return settle_delivery_party(party_type=party_type, party=party, pos_profile=pos_profile)
    
    # Fallback to legacy label-based settlement
    courier_label = first.get("courier") or ""
    return settle_courier(courier_label, pos_profile)


@frappe.whitelist()
def handle_out_for_delivery_paid(invoice_name: str, courier: str, settlement: str, pos_profile: str, party_type: str | None = None, party: str | None = None):
    """Handle transition to 'Out for Delivery' for a PAID invoice with courier settlement options.

    Idempotency:
        - Re-running will NOT duplicate Journal Entry (matched by title prefix).
        - Re-running will NOT duplicate Courier Transaction (matched by reference_invoice + courier + notes like '%Out For Delivery transition%').

    Permission:
        User must have one of roles: Administrator, Sales User, Accounts User (else PermissionError).

    Args:
        invoice_name: Sales Invoice name (submitted & already paid)
        courier: Courier DocType name
        settlement: 'cash_now' or 'later'
        pos_profile: POS Profile used to resolve cash account

    Behavior:
        cash_now -> JE: DR Freight & Forwarding Charges / CR Cash (POS Profile)
                     Courier Transaction status = Settled
        later    -> JE: DR Courier Outstanding / CR Cash (POS Profile)
                     Courier Transaction status = Unsettled
        In both cases create Courier Transaction with amount=0 & shipping_amount, link invoice.
    """
    # Permission / role guard
    roles = set(frappe.get_roles(frappe.session.user))
    allowed_roles = {"Administrator", "Sales User", "Accounts User"}
    if roles.isdisjoint(allowed_roles):
        frappe.throw("Not permitted to perform Out For Delivery transition (missing role)")

    invoice_name = (invoice_name or '').strip()
    courier = (courier or '').strip()
    settlement = (settlement or '').strip().lower()
    pos_profile = (pos_profile or '').strip()

    if not invoice_name:
        frappe.throw("invoice_name required")
    if not courier:
        frappe.throw("courier required (legacy label)")
    if not (party_type and party):
        fallback = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": invoice_name,
                "status": ["!=", "Settled"],
                "party_type": ["not in", [None, ""]],
                "party": ["not in", [None, ""]],
            },
            fields=["party_type", "party"],
            limit=1,
        )
        if fallback:
            party_type = fallback[0].get("party_type")
            party = fallback[0].get("party")
        else:
            frappe.throw("party_type & party required (must pass Employee/Supplier courier)")
    if settlement not in {"cash_now", "later"}:
        frappe.throw("Invalid settlement value (expected 'cash_now' or 'later')")
    if not pos_profile:
        frappe.throw("pos_profile required")

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")
    # Ensure already paid (allow tiny residual rounding)
    if float(inv.outstanding_amount or 0) > 0.01:
        frappe.throw("Invoice is not fully paid yet")

    company = inv.company
    shipping_exp = _get_delivery_expense_amount(inv) or 0.0

    # Update operational state idempotently
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        try:
            inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
        except Exception:
            inv.set("custom_sales_invoice_state", "Out for Delivery")
            inv.save(ignore_permissions=True)

    freight_acc = get_freight_expense_account(company)
    courier_outstanding_acc = get_courier_outstanding_account(company)
    cash_acc = get_pos_cash_account(pos_profile, company)
    creditors_acc = get_creditors_account(company)
    for acc in (freight_acc, courier_outstanding_acc, cash_acc, creditors_acc):
        validate_account_exists(acc)

    try:
        frappe.db.savepoint("ofdelivery_start")
        # Idempotency: check existing JE tagged via custom ref in user_remark / title
        existing = frappe.get_all(
            "Journal Entry",
            filters={
                "company": company,
                "title": ["like", f"Out For Delivery – {inv.name}%"],
                "docstatus": 1,
            },
            pluck="name",
            limit_page_length=1,
        )
        je_name = existing[0] if existing else None

        if not je_name and shipping_exp > 0:
            je = frappe.new_doc("Journal Entry")
            je.voucher_type = "Journal Entry"
            je.posting_date = frappe.utils.nowdate()
            je.company = company
            je.title = f"Out For Delivery – {inv.name}"
            if settlement == "cash_now":
                # DR Freight, CR Cash (pay courier now)
                je.append("accounts", {
                    "account": freight_acc,
                    "debit_in_account_currency": shipping_exp,
                    "credit_in_account_currency": 0,
                })
                je.append("accounts", {
                    "account": cash_acc,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": shipping_exp,
                })
            else:
                if not (party_type and party):
                    frappe.throw("party_type & party required when accruing courier payable (settlement 'later')")
                # DR Freight, CR Creditors (accrue payable to courier)
                je.append("accounts", {
                    "account": freight_acc,
                    "debit_in_account_currency": shipping_exp,
                    "credit_in_account_currency": 0,
                })
                je.append("accounts", {
                    "account": creditors_acc,
                    "party_type": party_type,
                    "party": party,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": shipping_exp,
                })
            je.save(ignore_permissions=True)
            je.submit()
            je_name = je.name

        # Courier Transaction idempotency
        # Idempotency now does not rely on legacy 'courier' link
        ct_filters = {
            "reference_invoice": inv.name,
            "notes": ["like", "%Out For Delivery transition%"],
        }
        existing_ct = frappe.get_all("Courier Transaction", filters=ct_filters, pluck="name", limit_page_length=1)
        if existing_ct:
            ct_name = existing_ct[0]
        else:
            ct = frappe.new_doc("Courier Transaction")
            # Do not set legacy 'courier' field (target DocType removed)
            ct.party_type = party_type
            ct.party = party
            ct.date = frappe.utils.now_datetime()
            ct.reference_invoice = inv.name
            ct.amount = 0
            ct.shipping_amount = shipping_exp
            ct.status = "Settled" if settlement == "cash_now" else "Unsettled"
            ct.payment_mode = settlement  # custom field expected
            ct.notes = "Out For Delivery transition - courier expense settlement" if shipping_exp else "Out For Delivery transition"
            ct.insert(ignore_permissions=True)
            ct_name = ct.name
        frappe.db.commit()
    except Exception as err:
        frappe.db.rollback(save_point="ofdelivery_start")
        frappe.log_error(f"Out For Delivery transition failed: {err}", "Jarz POS OutForDelivery")
        frappe.throw(f"Failed Out For Delivery transition: {err}")

    payload = {
        "invoice": inv.name,
        "courier": courier,
        "shipping_amount": shipping_exp,
        "journal_entry": je_name,
        "courier_transaction": ct_name,
        "settlement": settlement,
    }
    frappe.publish_realtime("jarz_pos_out_for_delivery_transition", payload, user="*")
    return {"success": True, **payload}


@frappe.whitelist()
def handle_out_for_delivery_transition(invoice_name: str, courier: str, mode: str, pos_profile: str, idempotency_token: str | None = None, party_type: str | None = None, party: str | None = None):
    """Unified Out For Delivery transition (Phase 2: add Journal Entry branch logic).

    Adds to Phase 1:
      * Determine shipping expense
      * Resolve accounts (freight, courier outstanding, POS cash)
      * Create single Journal Entry per invoice+mode (idempotent by title)

    (Courier Transaction creation deferred to Phase 3.)
    """
    try:

        # ---- Input Normalization & Validation ----
        invoice_name = (invoice_name or '').strip()
        courier = (courier or '').strip()
        mode = (mode or '').strip().lower()
        pos_profile = (pos_profile or '').strip()
        token = (idempotency_token or '').strip() or None

        if not invoice_name:
            frappe.throw("invoice_name required")
        if not courier:
            frappe.throw("courier required (legacy label)")
        if mode not in {"pay_now", "settle_later"}:
            frappe.throw("mode must be 'pay_now' or 'settle_later'")
        if not pos_profile:
            frappe.throw("pos_profile required")

        if not (party_type and party):
            fallback = frappe.get_all(
                "Courier Transaction",
                filters={
                    "reference_invoice": invoice_name,
                    "status": ["!=", "Settled"],
                    "party_type": ["not in", [None, ""]],
                    "party": ["not in", [None, ""]],
                },
                fields=["party_type", "party"],
                limit=1,
            )
            if fallback:
                party_type = fallback[0].get("party_type")
                party = fallback[0].get("party")
            else:
                frappe.throw("party_type & party required (Employee/Supplier)")

        inv = frappe.get_doc("Sales Invoice", invoice_name)
        if inv.docstatus != 1:
            frappe.throw("Invoice must be submitted")
        if float(inv.outstanding_amount or 0) > 0.01:
            frappe.throw("Invoice must be fully paid before marking Out for Delivery")

        # ---- Shipping Expense (from city) ----
        shipping_exp = _get_delivery_expense_amount(inv) or 0.0

        # ---- Account Resolution ----
        company = inv.company
        freight_acc = get_freight_expense_account(company)
        courier_outstanding_acc = get_courier_outstanding_account(company)
        cash_acc = get_pos_cash_account(pos_profile, company)
        creditors_acc = get_creditors_account(company)
        for acc in (freight_acc, courier_outstanding_acc, cash_acc, creditors_acc):
            validate_account_exists(acc)

        # ---- Journal Entry (idempotent) ----
        je_title = f"Out For Delivery – {inv.name}"
        existing_je = frappe.get_all(
            "Journal Entry",
            filters={"company": company, "title": je_title, "docstatus": 1},
            pluck="name",
            limit_page_length=1,
        )
        je_name = existing_je[0] if existing_je else None
        if not je_name and shipping_exp > 0:
            try:
                frappe.db.savepoint("ofdelivery_je")
                je = frappe.new_doc("Journal Entry")
                je.voucher_type = "Journal Entry"
                je.posting_date = frappe.utils.nowdate()
                je.company = company
                je.title = je_title
                if mode == "pay_now":
                    je.append("accounts", {
                        "account": freight_acc,
                        "debit_in_account_currency": shipping_exp,
                        "credit_in_account_currency": 0,
                    })
                    je.append("accounts", {
                        "account": cash_acc,
                        "debit_in_account_currency": 0,
                        "credit_in_account_currency": shipping_exp,
                    })
                else:  # settle_later
                    je.append("accounts", {
                        "account": freight_acc,
                        "debit_in_account_currency": shipping_exp,
                        "credit_in_account_currency": 0,
                    })
                    je.append("accounts", {
                        "account": creditors_acc,
                        "party_type": party_type,
                        "party": party,
                        "debit_in_account_currency": 0,
                        "credit_in_account_currency": shipping_exp,
                    })
                je.save(ignore_permissions=True)
                je.submit()
                je_name = je.name
                frappe.db.release_savepoint("ofdelivery_je")
            except Exception as err:
                frappe.db.rollback(save_point="ofdelivery_je")
                frappe.log_error(f"OFD JE creation failed: {err}", "Jarz POS OFD JE")
                frappe.throw(f"Failed creating Out For Delivery journal entry: {err}")

        # ---- Courier Transaction (idempotent) ----
        ct_name = None
        idempotent_flag = False
        existing_ct: list[str] = []
        try:
            columns = frappe.db.get_table_columns("Courier Transaction") or []
        except Exception:
            columns = []
        has_idem_col = "idempotency_token" in columns

        if token and has_idem_col:
            try:
                existing_ct = frappe.get_all(
                    "Courier Transaction",
                    filters={"reference_invoice": inv.name, "idempotency_token": token},
                    pluck="name", limit_page_length=1,
                )
            except Exception as err:
                if "Unknown column" not in str(err):
                    frappe.log_error(f"Courier Transaction token lookup failed: {err}", "Jarz POS OFD CT Lookup")
                existing_ct = []
        if not existing_ct:
            existing_ct = frappe.get_all(
                "Courier Transaction",
                filters={"reference_invoice": inv.name, "notes": ["like", "%Out For Delivery transition%"]},
                pluck="name", limit_page_length=1,
            )
        if existing_ct:
            ct_name = existing_ct[0]
            idempotent_flag = True
            try:
                ct_doc = frappe.get_doc("Courier Transaction", ct_name)
                if je_name and not getattr(ct_doc, "journal_entry", None):
                    ct_doc.db_set("journal_entry", je_name, update_modified=False)
                desired_amount = float(inv.grand_total or 0)
                if mode == "pay_now" and abs(float(ct_doc.get("amount") or 0) - desired_amount) > 0.005:
                    frappe.db.set_value("Courier Transaction", ct_name, {
                        "amount": desired_amount,
                        "shipping_amount": float(shipping_exp or 0),
                    }, update_modified=False)
            except Exception as err:
                frappe.log_error(f"Failed updating CT links/amounts: {err}", "Jarz POS OFD CT Backfill")
        else:
            try:
                frappe.db.savepoint("ofdelivery_ct")
                ct = frappe.new_doc("Courier Transaction")
                ct.party_type = party_type
                ct.party = party
                ct.date = frappe.utils.now_datetime()
                ct.reference_invoice = inv.name
                ct.amount = float(inv.grand_total or 0) if mode == "pay_now" else 0
                ct.shipping_amount = shipping_exp
                ct.status = "Settled" if mode == "pay_now" else "Unsettled"
                ct.payment_mode = "Cash" if mode == "pay_now" else "Deferred"
                ct.journal_entry = je_name
                if token and has_idem_col:
                    ct.idempotency_token = token
                ct.notes = f"Out For Delivery transition ({'pay now' if mode=='pay_now' else 'settle later'})"
                ct.insert(ignore_permissions=True)
                ct_name = ct.name
                frappe.db.release_savepoint("ofdelivery_ct")
            except Exception as err:
                frappe.db.rollback(save_point="ofdelivery_ct")
                frappe.log_error(f"OFD Courier Transaction failed: {err}", "Jarz POS OFD CT")
                frappe.throw(f"Failed creating courier transaction: {err}")

        # ---- State Update (post operations) ----
        state_now = inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state")
        if state_now != "Out for Delivery":
            try:
                inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
            except Exception:
                inv.set("custom_sales_invoice_state", "Out for Delivery")
                inv.save(ignore_permissions=True)

    # Frappe will commit automatically at end of request if no exception

        payload = {
            "invoice": inv.name,
            "courier": courier,
            "mode": mode,
            "payment_mode": mode,
            "shipping_amount": shipping_exp,
            "journal_entry": je_name,
            "courier_transaction": ct_name,
            "status": "Settled" if mode == "pay_now" else "Unsettled",
            "idempotent": idempotent_flag,
            "idempotency_token": token,
            "has_idempotency_column": has_idem_col,
        }
        try:
            payload["amount"] = float(inv.grand_total or 0)
            payload["net_to_collect"] = float(inv.grand_total or 0) - float(shipping_exp or 0)
        except Exception:
            pass
        frappe.publish_realtime("jarz_pos_out_for_delivery_transition", payload, user="*")
        return {"success": True, **payload}

    except Exception as err:
        frappe.log_error(f"Out For Delivery transition failed: {err}", "Jarz POS Out For Delivery")
        frappe.throw(f"Failed Out For Delivery transition: {err}")


@frappe.whitelist()
def settle_single_invoice_paid(invoice_name: str, pos_profile: str, party_type: str | None = None, party: str | None = None):
    """Settle a single *paid* invoice's courier shipping expense with the courier.

    Scenario:
      * Customer already paid the Sales Invoice (no outstanding)
      * We now pay the courier their shipping fee (delivery expense) one-by-one

    Accounting Entry (Journal Entry):
      DR Creditors (party line with party_type/party)   amount = shipping_expense
      CR Cash (POS Profile Cash / branch cash account)  amount = shipping_expense

    Additionally, any Unsettled Courier Transaction rows for this invoice
    (party-based) are marked Settled. If no CT exists yet (edge case), we
    create one with amount=0 & shipping_amount for traceability.
    """
    invoice_name = (invoice_name or '').strip()
    pos_profile = (pos_profile or '').strip()
    party_type = (party_type or '').strip()
    party = (party or '').strip()

    if not invoice_name:
        frappe.throw("invoice_name required")
    if not pos_profile:
        frappe.throw("pos_profile required to resolve cash account")

    if not (party_type and party):
        existing_party = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": invoice_name,
                "status": ["!=", "Settled"],
                "party_type": ["not in", [None, ""]],
                "party": ["not in", [None, ""]],
            },
            fields=["party_type", "party"],
            limit=1,
        )
        if not existing_party:
            existing_party = frappe.get_all(
                "Courier Transaction",
                filters={
                    "reference_invoice": invoice_name,
                    "party_type": ["not in", [None, ""]],
                    "party": ["not in", [None, ""]],
                },
                fields=["party_type", "party"],
                limit=1,
            )
        if existing_party:
            party_type = existing_party[0].get("party_type")
            party = existing_party[0].get("party")
        else:
            frappe.throw("party_type & party required (unable to derive from existing courier transactions)")

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")
    if float(inv.outstanding_amount or 0) > 0.01:
        frappe.throw("Invoice not fully paid; cannot one-by-one settle shipping")

    company = inv.company
    shipping_exp = _get_delivery_expense_amount(inv) or 0.0
    if shipping_exp <= 0:
        frappe.throw("No shipping expense configured for this invoice")

    cash_acc = get_pos_cash_account(pos_profile, company)
    creditors_acc = get_creditors_account(company)
    courier_outstanding_acc = _get_courier_outstanding_account(company)
    for acc in (cash_acc, creditors_acc, courier_outstanding_acc):
        validate_account_exists(acc)

    # Concurrency guard: lock invoice row
    try:
        frappe.db.sql("SELECT name FROM `tabSales Invoice` WHERE name=%s FOR UPDATE", (inv.name,))
    except Exception:
        pass

    # Determine if we have an outstanding-type courier transaction (amount > 0)
    outstanding_ct = frappe.get_all(
        "Courier Transaction",
        filters={
            "reference_invoice": inv.name,
            "party_type": party_type,
            "party": party,
            "status": ["!=", "Settled"],
            "amount": [">", 0],
        },
        fields=["name", "amount", "shipping_amount"],
        limit=1,
    )
    has_outstanding_mode = bool(outstanding_ct)
    order_amount = float(outstanding_ct[0].amount) if outstanding_ct else 0.0

    # Helper to find existing JE (idempotency)
    def _existing_je(title: str):
        rows = frappe.get_all(
            "Journal Entry",
            filters={"company": company, "title": title, "docstatus": 1},
            pluck="name",
            limit_page_length=1,
        )
        return rows[0] if rows else None

    if has_outstanding_mode:
        # Unpaid + settle later final settlement (cases based on order_amount vs shipping_exp)
        title = f"Courier Outstanding Settlement – {inv.name}"
        je_name = _existing_je(title)
        if not je_name:
            je = frappe.new_doc("Journal Entry")
            je.voucher_type = "Journal Entry"
            je.posting_date = frappe.utils.nowdate()
            je.company = company
            je.title = title
            if order_amount >= shipping_exp:
                # Common case
                net_branch = order_amount - shipping_exp
                if net_branch > 0.0001:
                    je.append("accounts", {
                        "account": cash_acc,
                        "debit_in_account_currency": net_branch,
                        "credit_in_account_currency": 0,
                    })
                # Debit Creditors with shipping
                je.append("accounts", {
                    "account": creditors_acc,
                    "party_type": party_type,
                    "party": party,
                    "debit_in_account_currency": shipping_exp,
                    "credit_in_account_currency": 0,
                })
                # Credit Courier Outstanding with full order amount
                je.append("accounts", {
                    "account": courier_outstanding_acc,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": order_amount,
                })
            else:
                # Shipping > Order Amount
                # Debit Creditors full shipping
                je.append("accounts", {
                    "account": creditors_acc,
                    "party_type": party_type,
                    "party": party,
                    "debit_in_account_currency": shipping_exp,
                    "credit_in_account_currency": 0,
                })
                # Credit Courier Outstanding with order amount
                je.append("accounts", {
                    "account": courier_outstanding_acc,
                    "debit_in_account_currency": 0,
                    "credit_in_account_currency": order_amount,
                })
                # Credit Cash with (shipping - order_amount)
                excess = shipping_exp - order_amount
                if excess > 0.0001:
                    je.append("accounts", {
                        "account": cash_acc,
                        "debit_in_account_currency": 0,
                        "credit_in_account_currency": excess,
                    })
            je.save(ignore_permissions=True)
            je.submit()
            je_name = je.name

        # Mark ALL related courier transactions (any amount/shipping) settled for this invoice & party
        cts = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": inv.name,
                "party_type": party_type,
                "party": party,
                "status": ["!=", "Settled"],
            },
            pluck="name",
        )
        for name in cts:
            frappe.db.set_value("Courier Transaction", name, "status", "Settled")

        payload = {
            "invoice": inv.name,
            "mode": "outstanding_settlement",
            "journal_entry": je_name,
            "order_amount": order_amount,
            "shipping_amount": shipping_exp,
            "party_type": party_type,
            "party": party,
            "courier_transactions": cts,
        }
        frappe.publish_realtime("jarz_pos_single_courier_settlement", payload, user="*")
        return {"success": True, **payload}
    else:
        # Paid + settle later shipping-only scenario (previous behavior)
        title = f"Courier Single Shipping Payment – {inv.name}"
        je_name = _existing_je(title)
        if not je_name:
            je = frappe.new_doc("Journal Entry")
            je.voucher_type = "Journal Entry"
            je.posting_date = frappe.utils.nowdate()
            je.company = company
            je.title = title
            # Shipping-only immediate payment: recognize expense now and credit cash
            freight_acc = get_freight_expense_account(company)
            validate_account_exists(freight_acc)
            je.append("accounts", {  # DR Freight Expense
                "account": freight_acc,
                "debit_in_account_currency": shipping_exp,
                "credit_in_account_currency": 0,
            })
            je.append("accounts", {  # CR Cash
                "account": cash_acc,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": shipping_exp,
            })
            je.save(ignore_permissions=True)
            je.submit()
            je_name = je.name

        # Settle / create CTs (shipping-only CT may exist with amount 0)
        cts = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": inv.name,
                "party_type": party_type,
                "party": party,
                "status": ["!=", "Settled"],
            },
            pluck="name",
        )
        if not cts:
            ct = frappe.new_doc("Courier Transaction")
            ct.party_type = party_type
            ct.party = party
            ct.date = frappe.utils.now_datetime()
            ct.reference_invoice = inv.name
            ct.amount = 0
            ct.shipping_amount = shipping_exp
            ct.status = "Settled"
            ct.payment_mode = "Cash"
            ct.notes = "Single courier shipping payment"
            ct.insert(ignore_permissions=True)
            cts = [ct.name]
        else:
            for name in cts:
                frappe.db.set_value("Courier Transaction", name, "status", "Settled")

        payload = {
            "invoice": inv.name,
            "mode": "shipping_only_settlement",
            "journal_entry": je_name,
            "order_amount": 0.0,
            "shipping_amount": shipping_exp,
            "party_type": party_type,
            "party": party,
            "courier_transactions": cts,
        }
        frappe.publish_realtime("jarz_pos_single_courier_settlement", payload, user="*")
        return {"success": True, **payload}


@frappe.whitelist()
def settle_courier_collected_payment(invoice_name: str, pos_profile: str, party_type: str, party: str):
    """Courier collected full order amount from customer; branch now recognizes net cash and shipping expense.

    Let GT = invoice grand total, SE = shipping expense.
    Case 1: GT > SE
        DR Cash (GT - SE)
        DR Creditors (SE)
        CR Courier Outstanding (GT)
    Case 2: SE > GT
        DR Creditors (SE)
        CR Courier Outstanding (GT)
        CR Cash (SE - GT)
    """
    invoice_name = (invoice_name or '').strip()
    pos_profile = (pos_profile or '').strip()
    party_type = (party_type or '').strip()
    party = (party or '').strip()
    if not invoice_name:
        frappe.throw("invoice_name required")
    if not pos_profile:
        frappe.throw("pos_profile required")
    if not (party_type and party):
        frappe.throw("party_type & party required")

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")

    company = inv.company
    order_amount = float(inv.grand_total or 0)
    shipping_exp = _get_delivery_expense_amount(inv) or 0.0
    if shipping_exp <= 0:
        frappe.throw("No shipping expense configured")

    cash_acc = get_pos_cash_account(pos_profile, company)
    creditors_acc = get_creditors_account(company)
    courier_outstanding_acc = _get_courier_outstanding_account(company)
    for acc in (cash_acc, creditors_acc, courier_outstanding_acc):
        validate_account_exists(acc)

    title = f"Courier Collected Settlement – {inv.name}"
    existing = frappe.get_all(
        "Journal Entry",
        filters={"company": company, "title": title, "docstatus": 1},
        pluck="name",
        limit_page_length=1,
    )
    je_name = existing[0] if existing else None
    if not je_name:
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = company
        je.title = title
        if order_amount >= shipping_exp:
            net_to_branch = order_amount - shipping_exp
            if net_to_branch > 0.0001:
                je.append("accounts", {"account": cash_acc, "debit_in_account_currency": net_to_branch, "credit_in_account_currency": 0})
            if shipping_exp > 0.0001:
                je.append("accounts", {"account": creditors_acc, "party_type": party_type, "party": party, "debit_in_account_currency": shipping_exp, "credit_in_account_currency": 0})
            if order_amount > 0.0001:
                je.append("accounts", {"account": courier_outstanding_acc, "debit_in_account_currency": 0, "credit_in_account_currency": order_amount})
        else:
            if shipping_exp > 0.0001:
                je.append("accounts", {"account": creditors_acc, "party_type": party_type, "party": party, "debit_in_account_currency": shipping_exp, "credit_in_account_currency": 0})
            if order_amount > 0.0001:
                je.append("accounts", {"account": courier_outstanding_acc, "debit_in_account_currency": 0, "credit_in_account_currency": order_amount})
            excess = shipping_exp - order_amount
            if excess > 0.0001:
                je.append("accounts", {"account": cash_acc, "debit_in_account_currency": 0, "credit_in_account_currency": excess})
        je.save(ignore_permissions=True)
        je.submit()
        je_name = je.name

    # Mark courier transactions settled for this invoice & party
    cts = frappe.get_all(
        "Courier Transaction",
        filters={
            "reference_invoice": inv.name,
            "party_type": party_type,
            "party": party,
            "status": ["!=", "Settled"],
        },
        pluck="name",
    )
    for name in cts:
        frappe.db.set_value("Courier Transaction", name, "status", "Settled")

    payload = {
        "invoice": inv.name,
        "journal_entry": je_name,
        "order_amount": order_amount,
        "shipping_amount": shipping_exp,
        "party_type": party_type,
        "party": party,
    }
    frappe.publish_realtime("jarz_pos_courier_collected_settlement", payload, user="*")
    return {"success": True, **payload}


# Helper functions

def _get_courier_outstanding_account(company: str) -> str:
    """Return the 'Courier Outstanding' ledger for the given company."""
    acc = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "account_name": ["like", "Courier Outstanding%"],
            "is_group": 0,
        },
        "name",
    )
    if acc:
        return acc
    frappe.throw(
        f"No 'Courier Outstanding' account found for company {company}.\n"
        "Please create a ledger named 'Courier Outstanding' (non-group) under Accounts Receivable."
    )


def _get_receivable_account(company):
    """Get the default receivable account for the company."""
    paid_from_account = frappe.get_value("Company", company, "default_receivable_account")
    if not paid_from_account:
        paid_from_account = frappe.get_value(
            "Account",
            {
                "account_type": "Receivable",
                "company": company,
                "is_group": 0,
            },
            "name",
        )
    if not paid_from_account:
        frappe.throw(f"No receivable account found for company {company}.")
    return paid_from_account


def _get_delivery_expense_amount(inv):
    """
    Return delivery expense amount (float) for the given invoice using its city.
    Tries to resolve city from the shipping / customer address linked to the invoice
    and then fetches the *delivery_expense* field from the **City** DocType.
    Returns ``0`` if city or expense could not be determined.
    """
    address_name = inv.get("shipping_address_name") or inv.get("customer_address")
    if not address_name:
        return 0.0
    
    try:
        addr = frappe.get_doc("Address", address_name)
    except Exception:
        return 0.0
    
    city_id = getattr(addr, "city", None)
    if not city_id:
        return 0.0
    
    # Primary direct lookup (assumes city field holds the City doc name/id)
    try:
        expense = frappe.db.get_value("City", city_id, "delivery_expense")
        if expense is not None:
            val = float(expense or 0)
            if val > 0:
                return val
    except Exception:
        pass

    # Fallback 1: match by city_name (case-insensitive) if Address.city stored a plain name
    try:
        fallback_name = frappe.db.get_value(
            "City",
            {"city_name": ["=", city_id]},
            "delivery_expense",
        )
        if fallback_name is not None and float(fallback_name or 0) > 0:
            return float(fallback_name or 0)
    except Exception:
        pass

    # Fallback 2: case-insensitive city_name search
    try:
        rows = frappe.get_all(
            "City",
            filters={"city_name": ["like", city_id]},
            fields=["delivery_expense"],
            limit_page_length=1,
        )
        if rows:
            val = float(rows[0].get("delivery_expense") or 0)
            if val > 0:
                return val
    except Exception:
        pass

    # Debug logging when expense not found – assists diagnosing zero shipping_amount
    try:
        frappe.log_error(
            title="Delivery Expense Resolution Miss",
            message=f"Invoice: {inv.name}\nAddress: {address_name}\nCity Raw: {city_id}\nResolved 0 expense via all strategies"
        )
    except Exception:
        pass
    return 0.0


def _get_invoice_city(invoice_name):
    """Get the city name for an invoice."""
    if not invoice_name:
        return ""
    
    # Fetch shipping or customer address linked to the invoice
    si_addr = frappe.db.get_value(
        "Sales Invoice",
        invoice_name,
        ["shipping_address_name", "customer_address"],
        as_dict=True,
    )
    
    addr_name = None
    if si_addr:
        addr_name = si_addr.get("shipping_address_name") or si_addr.get("customer_address")
    
    if addr_name:
        city_id = frappe.db.get_value("Address", addr_name, "city")
        if city_id:
            city_name = frappe.db.get_value("City", city_id, "city_name")
            return city_name or city_id or ""
    
    return ""


def _create_payment_entry(inv, paid_from_account, paid_to_account, outstanding):
    """Create and submit payment entry for courier outstanding."""
    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = inv.company
    pe.party_type = "Customer"
    pe.party = inv.customer
    pe.paid_from = paid_from_account  # Debtors (party account)
    pe.paid_to = paid_to_account      # Courier Outstanding (asset/receivable)
    pe.paid_amount = outstanding
    pe.received_amount = outstanding
    
    # Allocate full amount to invoice to close it
    pe.append(
        "references",
        {
            "reference_doctype": "Sales Invoice",
            "reference_name": inv.name,
            "due_date": inv.get("due_date"),
            "total_amount": inv.grand_total,
            "outstanding_amount": outstanding,
            "allocated_amount": outstanding,
        },
    )
    
    # Minimal bank fields placeholders
    pe.reference_no = f"COURIER-OUT-{inv.name}"
    pe.reference_date = frappe.utils.nowdate()
    pe.save(ignore_permissions=True)
    pe.submit()
    
    return pe


def _create_shipping_expense_to_creditors_je(inv, shipping_exp: float, creditors_acc: str, party_type: str, party: str) -> str:
    """Create JE: DR Freight & Forwarding Charges, CR Creditors (payable) with party assigned.

    Requires valid party_type (Supplier/Employee) & party (name).
    """
    company = inv.company
    freight_acc = get_freight_expense_account(company)

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = frappe.utils.nowdate()
    je.company = company
    je.title = f"Courier Expense – {inv.name}"

    # DR Freight Expense
    je.append("accounts", {
        "account": freight_acc,
        "debit_in_account_currency": shipping_exp,
        "credit_in_account_currency": 0,
    })

    # CR Creditors (with party reference)
    je.append("accounts", {
        "account": creditors_acc,
        "party_type": party_type,
        "party": party,
        "debit_in_account_currency": 0,
        "credit_in_account_currency": shipping_exp,
    })

    je.save(ignore_permissions=True)
    je.submit()
    return je.name


def _create_courier_transaction(inv, outstanding, shipping_exp, *, party_type: str | None, party: str | None, legacy_courier: str | None = None):
    """Create courier transaction log entry with Employee/Supplier party fields."""
    ct = frappe.new_doc("Courier Transaction")
    # Legacy 'courier' link is deprecated and must not be set (DocType removed)
    if party_type and party:
        ct.party_type = party_type
        ct.party = party
    ct.date = frappe.utils.now_datetime()
    ct.reference_invoice = inv.name
    ct.amount = float(outstanding or 0)
    ct.shipping_amount = float(shipping_exp or 0)
    # Explicit metadata for clarity in UI
    try:
        ct.status = "Unsettled"
    except Exception:
        pass
    try:
        ct.payment_mode = "Deferred"
    except Exception:
        pass
    try:
        ct.notes = "Courier Outstanding (collect order amount from courier)"
    except Exception:
        pass
    ct.insert(ignore_permissions=True)
    
    return ct


def _create_expense_journal_entry(inv, amount, paid_from, paid_to):
    """Create journal entry for delivery expense payment."""
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = frappe.utils.nowdate()
    je.company = inv.company
    je.title = f"Courier Expense – {inv.name}"
    
    je.append(
        "accounts",
        {
            "account": paid_from,
            "credit_in_account_currency": amount,
            "debit_in_account_currency": 0,
        },
    )
    
    je.append(
        "accounts",
        {
            "account": paid_to,
            "debit_in_account_currency": amount,
            "credit_in_account_currency": 0,
        },
    )
    
    je.save(ignore_permissions=True)
    je.submit()
    
    return je


def _create_settlement_journal_entry(courier, net_balance, company, cash_acc, courier_outstanding_acc):
    """Create journal entry for courier settlement."""
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = frappe.utils.nowdate()
    je.company = company
    je.title = f"Courier Settlement – {courier}"
    
    if net_balance > 0:
        # Courier owes us money – we RECEIVE cash
        je.append("accounts", {
            "account": cash_acc,
            "debit_in_account_currency": net_balance,
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": courier_outstanding_acc,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": net_balance,
        })
    else:
        amt = abs(net_balance)
        # We owe courier – PAY cash
        je.append("accounts", {
            "account": courier_outstanding_acc,
            "debit_in_account_currency": amt,
            "credit_in_account_currency": 0,
        })
        je.append("accounts", {
            "account": cash_acc,
            "debit_in_account_currency": 0,
            "credit_in_account_currency": amt,
        })
    
    je.save(ignore_permissions=True)
    je.submit()
    
    return je.name

"""
Delivery Handling Module for Jarz POS

This module handles all delivery and courier-related operations,
including outstanding management, expense tracking, and settlement.
"""

import frappe
from jarz_pos.utils.account_utils import (
    get_freight_expense_account,
    get_courier_outstanding_account,
    get_pos_cash_account,
    validate_account_exists,
    get_creditors_account,
)

# ---------------------------------------------------------------------------
# Delivery Note Auto-Creation Helper
# Centralized so ALL Out For Delivery transitions (courier endpoints, kanban,
# future automation) reuse identical logic & logging.
# ---------------------------------------------------------------------------

DN_LOGIC_VERSION = "2025-09-07a"

# Constant VAT rate on partner fees
PARTNER_FEES_VAT_RATE = 0.14  # 14%

def _compute_sales_partner_fees(inv, sales_partner: str, online: bool) -> dict:
    """Compute partner fees (commission + optional online fee) plus VAT.

    Args:
        inv: Sales Invoice doc
        sales_partner: Sales Partner name
        online: True if transaction is paid online (apply online_payment_fees), False for cash

    Returns:
        dict with keys: base_fees, vat, total_fees, commission_rate, online_rate
    """
    def _to_float(v, default=0.0):
        try:
            return float(v)
        except Exception:
            return float(default)
    try:
        sp = frappe.get_doc("Sales Partner", sales_partner)
        # Avoid treating 0.0 as falsy; pick first non-None attribute explicitly
        cr_raw = getattr(sp, "commission_rate", None)
        if cr_raw is None:
            cr_raw = getattr(sp, "commission_rate_percent", None)
        or_raw = getattr(sp, "online_payment_fees", None)
        if or_raw is None:
            or_raw = getattr(sp, "online_payment_fee", None)
        commission_rate = _to_float(cr_raw if cr_raw is not None else 0)
        online_rate = _to_float(or_raw if or_raw is not None else 0)
    except Exception:
        commission_rate = 0.0
        online_rate = 0.0

    amount = float(getattr(inv, "grand_total", 0) or 0)
    commission_fee = amount * (commission_rate / 100.0)
    online_fee = amount * (online_rate / 100.0) if online else 0.0
    base = commission_fee + online_fee
    vat = base * PARTNER_FEES_VAT_RATE
    total = base + vat
    # Round to 2 decimals for display/storage
    def r2(x: float) -> float:
        try:
            return round(float(x), 2)
        except Exception:
            return float(x)
    return {
        "base_fees": r2(base),
        "vat": r2(vat),
        "total_fees": r2(total),
        "commission_rate": commission_rate,
        "online_rate": online_rate,
    }

def ensure_delivery_note_for_invoice(invoice_name: str) -> dict:
    """Idempotently ensure a submitted Delivery Note exists for Sales Invoice.

    Strategy (in order):
      1. Reuse an existing submitted Delivery Note whose remarks already embeds invoice name
         (pattern used by earlier logic) OR that has a custom link field if present.
      2. Create new Delivery Note copying items (with per-row warehouse fallback):
            * Use first available item.warehouse as default (set_warehouse)
            * If no warehouses at all, leave blank (stock validation may still pass for non-stock items).

    Returns dict:
        {
          "delivery_note": str | None,
          "reused": bool,
          "error": str | None,
          "logic_version": DN_LOGIC_VERSION
        }
    Raises (propagates) only on unexpected internal errors AFTER logging.
    """
    out = {"delivery_note": None, "reused": False, "error": None, "logic_version": DN_LOGIC_VERSION}
    try:
        si = frappe.get_doc("Sales Invoice", invoice_name)
        if si.docstatus != 1:
            out["error"] = "Invoice must be submitted before creating Delivery Note"
            return out

        # Idempotency / reuse search sequence (broadened to avoid duplicate DN creation):
        #   a) Custom link field (if any of known candidates exists)
        #   b) Remarks contains invoice name (legacy pattern)
        #   c) Heuristic match: submitted DN for same customer, same total qty & amount (within recent 3 days)
        #      that already has 'Auto-created from Sales Invoice' in remarks (covers earlier creation path)
        try:
            # Try custom field first (if admin later adds one, this code adapts automatically)
            dn_link_field = None
            dn_meta = frappe.get_meta("Delivery Note")
            for candidate in ["sales_invoice", "against_sales_invoice", "reference_invoice", "jarz_sales_invoice_ref"]:
                if dn_meta.get_field(candidate):
                    dn_link_field = candidate
                    break
            existing = []
            if dn_link_field:
                existing = frappe.get_all(
                    "Delivery Note",
                    filters={dn_link_field: invoice_name, "docstatus": 1},
                    pluck="name",
                    limit_page_length=1,
                )
            if not existing:
                existing = frappe.get_all(
                    "Delivery Note",
                    filters={"docstatus": 1, "remarks": ["like", f"%{invoice_name}%"]},
                    pluck="name",
                    limit_page_length=1,
                )
            # Heuristic fallback (recent auto-created for same customer & qty/amount)
            if not existing:
                try:
                    total_qty = sum([float(it.qty or 0) for it in si.items])
                except Exception:
                    total_qty = None
                heuristics = frappe.get_all(
                    "Delivery Note",
                    filters={
                        "docstatus": 1,
                        "customer": si.customer,
                        "posting_date": [">=", frappe.utils.add_days(frappe.utils.today(), -3)],
                        "remarks": ["like", "%Auto-created from Sales Invoice%"],
                    },
                    fields=["name"],
                    limit_page_length=5,
                )
                for cand in heuristics:
                    # Light check: ensure not already matched by remarks but amounts align
                    try:
                        dn_doc = frappe.get_doc("Delivery Note", cand.name)
                        if abs(float(dn_doc.get("total_qty") or 0) - (total_qty or 0)) < 0.0001:
                            existing = [cand.name]
                            break
                    except Exception:
                        continue
            if existing:
                out["delivery_note"] = existing[0]
                out["reused"] = True
                try:
                    # Force completed state (order already shipped)
                    dn_doc = frappe.get_doc("Delivery Note", existing[0])
                    if int(getattr(dn_doc, "docstatus", 0) or 0) == 1:
                        try:
                            dn_doc.db_set("per_billed", 100, update_modified=False)
                        except Exception:
                            pass
                        try:
                            dn_doc.db_set("status", "Completed", update_modified=False)
                        except Exception:
                            pass
                except Exception as _mark_err:
                    frappe.logger().warning(f"AUTO_DN reuse mark-completed failed for {existing[0]}: {_mark_err}")
                frappe.logger().info(f"AUTO_DN reuse Delivery Note {existing[0]} for {invoice_name}")
                return out
        except Exception as reuse_err:
            # Non-fatal – continue to creation path
            frappe.logger().warning(f"AUTO_DN reuse lookup failed for {invoice_name}: {reuse_err}")

        # Build new Delivery Note
        frappe.logger().info(f"AUTO_DN creating Delivery Note for {invoice_name}")
        dn = frappe.new_doc("Delivery Note")
        dn.customer = si.customer
        dn.company = si.company
        dn.posting_date = frappe.utils.getdate()
        dn.posting_time = frappe.utils.nowtime()
        dn.remarks = f"Auto-created from Sales Invoice {si.name} (state -> Out for Delivery)"

        default_wh = None
        for it in si.items:
            if it.get("warehouse"):
                default_wh = it.get("warehouse")
                break
        if default_wh:
            dn.set_warehouse = default_wh

        for it in si.items:
            dn.append("items", {
                "item_code": it.item_code,
                "item_name": it.item_name,
                "description": it.description,
                "qty": it.qty,
                "uom": it.uom,
                "stock_uom": it.stock_uom,
                "conversion_factor": getattr(it, "conversion_factor", 1) or 1,
                "rate": it.rate,
                "amount": it.amount,
                "warehouse": it.get("warehouse") or default_wh,
            })
        # Attempt to set link field if exists (does not break if absent)
        try:
            for candidate in ["sales_invoice", "against_sales_invoice", "reference_invoice", "jarz_sales_invoice_ref"]:
                if hasattr(dn, candidate):
                    setattr(dn, candidate, si.name)
                    break
        except Exception:
            pass

        dn.flags.ignore_permissions = True
        dn.insert(ignore_permissions=True)
        dn.submit()
        # Mark completed (fully billed) per business rule
        try:
            dn.db_set("per_billed", 100, update_modified=False)
        except Exception:
            pass
        try:
            dn.db_set("status", "Completed", update_modified=False)
        except Exception:
            pass
        out["delivery_note"] = dn.name
        frappe.logger().info(f"AUTO_DN created Delivery Note {dn.name} for {invoice_name}")
        return out
    except Exception as err:
        out["error"] = str(err)
        frappe.logger().error(f"AUTO_DN failed for {invoice_name}: {err}\n{frappe.get_traceback()}")
        return out


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
    
    # Store party info for JE creation
    courier_party_type = party_type
    courier_party = party

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted before marking as courier outstanding.")
    # Re-check latest outstanding directly from DB to avoid stale cache
    try:
        latest_outstanding = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
    except Exception:
        latest_outstanding = float(inv.outstanding_amount or 0)
    if latest_outstanding <= 0.0001:
        # Do not hard-fail; continue with CT/JE to keep operational flow going (idempotent behavior)
        frappe.logger().warning(f"mark_courier_outstanding: Invoice {inv.name} appears fully paid (latest outstanding={latest_outstanding}). Skipping Payment Entry and proceeding with CT/JE if needed.")

    company = inv.company
    outstanding = latest_outstanding
    order_amount = float(inv.grand_total or (outstanding or 0))

    # Compute shipping first for CT and later JE
    shipping_exp = _get_delivery_expense_amount(inv)

    # Create Courier Transaction BEFORE creating Payment Entry so preview treats this as unpaid-effective
    # Idempotency: avoid duplicate CTs for same purpose
    existing_ct = frappe.get_all(
        "Courier Transaction",
        filters={
            "reference_invoice": inv.name,
            "party_type": party_type,
            "party": party,
            "status": ["!=", "Settled"],
            "notes": ["like", "%Courier Outstanding (%"],
        },
        pluck="name",
        limit_page_length=1,
    )
    if existing_ct:
        ct_name = existing_ct[0]
    else:
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
        ct_name = ct.name

    # Now move receivable to Courier Outstanding via Payment Entry (this will mark invoice Paid in ERP terms)
    # Reuse existing PE to Courier Outstanding if already booked
    paid_to_account = _get_courier_outstanding_account(company)
    paid_from_account = _get_receivable_account(company)
    pe_name = None
    try:
        ref_parents = frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Sales Invoice", "reference_name": inv.name},
            pluck="parent",
        )
        if ref_parents:
            rows = frappe.get_all(
                "Payment Entry",
                filters={"name": ["in", ref_parents], "docstatus": 1},
                fields=["name", "paid_to"],
            )
            for r in rows:
                if (r.get("paid_to") or "").startswith("Courier Outstanding"):
                    pe_name = r["name"]
                    break
    except Exception:
        pe_name = None
    if not pe_name and outstanding > 0.0001:
        try:
            # Pass courier party info to _create_payment_entry for JE creation
            pe = _create_payment_entry(inv, paid_from_account, paid_to_account, outstanding, courier_party_type, courier_party)
            pe_name = pe.name
        except Exception as pe_err:
            # Handle validation where SI is already fully paid; proceed without blocking
            msg = str(pe_err)
            if "already been fully paid" in msg or "already paid" in msg.lower():
                frappe.logger().warning(f"mark_courier_outstanding: Skipping PE creation for {inv.name} – {msg}")
            else:
                raise

    # Accrue courier shipping payable to Creditors (party line) if configured
    je_name = None
    if shipping_exp and shipping_exp > 0:
        creditors_acc = get_creditors_account(company)
        je_name = _create_shipping_expense_to_creditors_je(inv, shipping_exp, creditors_acc, party_type, party)

    # Update state (defer state commit to end of request)
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        try:
            inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
        except Exception:
            inv.set("custom_sales_invoice_state", "Out for Delivery")
            inv.save(ignore_permissions=True)

    # Mandatory Delivery Note creation (enforced across all flows)
    dn_result = ensure_delivery_note_for_invoice(inv.name)
    if dn_result.get("error"):
        frappe.throw(f"Failed auto-creating Delivery Note: {dn_result.get('error')}")

    payload = {
        "event": "jarz_pos_courier_outstanding",
        "invoice": inv.name,
        "courier": courier,
        "party_type": party_type,
        "party": party,
        "payment_entry": pe_name,
        "journal_entry": je_name,
        "courier_transaction": ct_name,
        "amount": order_amount,
        "shipping_amount": shipping_exp or 0,
        "net_to_collect": (order_amount - float(shipping_exp or 0)),
        "mode": "settle_later",
        "delivery_note": dn_result.get("delivery_note"),
        "delivery_note_reused": dn_result.get("reused"),
        "dn_logic_version": DN_LOGIC_VERSION,
    }
    frappe.publish_realtime("jarz_pos_courier_outstanding", payload)
    return payload


@frappe.whitelist()
def sales_partner_unpaid_out_for_delivery(invoice_name: str, pos_profile: str, mode_of_payment: str = "Cash"):
    """Handle Out For Delivery transition for an UNPAID Sales Invoice that has a Sales Partner.

    Business rule:
      * Skip courier selection / settlement logic entirely.
      * Immediately collect full outstanding amount in cash (Payment Entry to POS Profile cash account).
      * Transition operational state to 'Out for Delivery'.
      * Idempotent: if payment already created, reuse it; do not duplicate Delivery Note.
      * Always ensure Delivery Note exists (reuse/create via ensure_delivery_note_for_invoice).

    Args:
        invoice_name: Sales Invoice (submitted)
        pos_profile: POS Profile name (to resolve cash account)
        mode_of_payment: Mode of Payment label (default 'Cash')
    Returns:
        dict { success, payment_entry, delivery_note, delivery_note_reused, outstanding_settled, amount }
    """
    invoice_name = (invoice_name or '').strip()
    pos_profile = (pos_profile or '').strip()
    if not invoice_name:
        frappe.throw("invoice_name required")
    if not pos_profile:
        frappe.throw("pos_profile required")

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")

    # Confirm sales partner presence
    sales_partner = getattr(inv, "sales_partner", None) or getattr(inv, "sales_partner_name", None)
    if not sales_partner:
        frappe.throw("Invoice has no Sales Partner; use regular flow")

    # Determine outstanding (fresh from DB)
    try:
        outstanding = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
    except Exception:
        outstanding = float(inv.outstanding_amount or 0)
    if outstanding <= 0.0001:
        # Already paid – just ensure state + DN and return gracefully
        outstanding = 0.0

    company = inv.company

    # Resolve accounts
    cash_acc = get_pos_cash_account(pos_profile, company)
    receivable_acc = getattr(inv, "debit_to", None) or frappe.db.get_value("Company", company, "default_receivable_account")
    if not receivable_acc:
        frappe.throw("Could not resolve receivable account for invoice")
    validate_account_exists(cash_acc)
    validate_account_exists(receivable_acc)

    # Step 0: Proactively prompt UI to collect cash BEFORE creating Payment Entry (two-step UX)
    try:
        frappe.publish_realtime(
            "jarz_pos_sales_partner_collect_prompt",
            {
                "invoice": inv.name,
                "sales_partner": sales_partner,
                "amount": float(inv.grand_total or 0),
                "outstanding": float(outstanding or 0),
                "mode": "sales_partner_collect_prompt",
            },
            user="*",
        )
    except Exception:
        pass

    # Create / reuse Payment Entry if still outstanding
    pe_name = None
    if outstanding > 0.0001:
        # Look for existing PE that already allocated full amount to invoice & paid_to matches cash account
        ref_parents = frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Sales Invoice", "reference_name": inv.name},
            pluck="parent",
        )
        if ref_parents:
            rows = frappe.get_all(
                "Payment Entry",
                filters={"name": ["in", ref_parents], "docstatus": 1, "paid_to": cash_acc, "payment_type": "Receive"},
                fields=["name", "paid_amount"],
            )
            for r in rows:
                pe_name = r["name"]
                break
        if not pe_name:
            pe = frappe.new_doc("Payment Entry")
            pe.payment_type = "Receive"
            pe.company = company
            pe.posting_date = frappe.utils.nowdate()
            pe.mode_of_payment = mode_of_payment
            pe.party_type = "Customer"
            pe.party = inv.customer
            pe.paid_from = receivable_acc
            pe.paid_to = cash_acc
            pe.paid_amount = outstanding
            pe.received_amount = outstanding
            pe.append("references", {
                "reference_doctype": "Sales Invoice",
                "reference_name": inv.name,
                "allocated_amount": outstanding,
            })
            # Flags for silent operation
            pe.flags.ignore_permissions = True
            pe.insert(ignore_permissions=True)
            pe.submit()
            pe_name = pe.name

    # Update operational state
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        try:
            inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
        except Exception:
            inv.set("custom_sales_invoice_state", "Out for Delivery")
            inv.save(ignore_permissions=True)

    # Delivery Note
    dn_result = ensure_delivery_note_for_invoice(inv.name)
    if dn_result.get("error"):
        frappe.throw(f"Failed auto-creating Delivery Note: {dn_result.get('error')}")

    # Create/ensure Sales Partner Transaction record (idempotent)
    try:
        idemp = f"{inv.name}::sales_partner_unpaid_cash"
        existing_spt = frappe.get_all(
            "Sales Partner Transactions",
            filters={"idempotency_token": idemp},
            pluck="name",
            limit_page_length=1,
        )
        if not existing_spt:
            # Compute partner fees (Cash -> no online fee)
            fees = _compute_sales_partner_fees(inv, sales_partner, online=False)
            spt = frappe.new_doc("Sales Partner Transactions")
            spt.sales_partner = sales_partner
            spt.date = frappe.utils.now_datetime()
            spt.reference_invoice = inv.name
            spt.amount = float(inv.grand_total or 0)
            spt.partner_fees = fees.get("total_fees")
            spt.payment_mode = "Cash"
            spt.idempotency_token = idemp
            spt.status = "Unsettled"
            # Store POS Profile on the transaction (from SI if set, else the function arg)
            try:
                spt.pos_profile = getattr(inv, "pos_profile", None) or pos_profile
            except Exception:
                pass
            spt.notes = (
                "Unpaid partner OFD – cash collected by staff | "
                f"fees: base={fees.get('base_fees')} vat={fees.get('vat')} total={fees.get('total_fees')} | "
                f"rates: commission={fees.get('commission_rate')}% online={fees.get('online_rate')}%"
            )
            spt.insert(ignore_permissions=True)
        else:
            # Backfill pos_profile if missing on existing record
            try:
                doc = frappe.get_doc("Sales Partner Transactions", existing_spt[0])
                current_pp = getattr(doc, "pos_profile", None)
                if not current_pp:
                    pp_val = getattr(inv, "pos_profile", None) or pos_profile
                    if pp_val:
                        doc.db_set("pos_profile", pp_val, update_modified=False)
            except Exception as _spt_backfill_err:
                frappe.logger().warning(f"SPT pos_profile backfill (unpaid) failed for {inv.name}: {_spt_backfill_err}")
    except Exception as _spt_err:
        frappe.logger().warning(f"SPT create (unpaid) failed for {inv.name}: {_spt_err}")

    payload = {
        "success": True,
        "invoice": inv.name,
        "payment_entry": pe_name,
        "delivery_note": dn_result.get("delivery_note"),
        "delivery_note_reused": dn_result.get("reused"),
        "amount": float(inv.grand_total or 0),
        "outstanding_before": outstanding,
        "sales_partner": sales_partner,
        "mode": "sales_partner_unpaid_cash",
    }
    frappe.publish_realtime("jarz_pos_sales_partner_unpaid_ofd", payload, user="*")
    return payload


@frappe.whitelist()
def sales_partner_paid_out_for_delivery(invoice_name: str):
    """Handle Out For Delivery transition for a PAID Sales Partner invoice.

    Use case: Invoice already fully paid (e.g. online payment). We still need to:
      * Set operational state to 'Out for Delivery'.
      * Ensure Delivery Note exists (to effect stock movement if update_stock was disabled at SI creation time).
      * Publish realtime event for Kanban/UI patching.
    Idempotent: Re-running will NOT create duplicate Delivery Notes.
    """
    invoice_name = (invoice_name or '').strip()
    if not invoice_name:
        frappe.throw("invoice_name required")
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")

    sales_partner = getattr(inv, "sales_partner", None) or getattr(inv, "sales_partner_name", None)
    if not sales_partner:
        frappe.throw("Invoice has no Sales Partner; use regular flow")

    # Set state
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        try:
            inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
        except Exception:
            inv.set("custom_sales_invoice_state", "Out for Delivery")
            inv.save(ignore_permissions=True)

    # Ensure DN (handles idempotent reuse)
    dn_result = ensure_delivery_note_for_invoice(inv.name)
    if dn_result.get("error"):
        frappe.throw(f"Failed auto-creating Delivery Note: {dn_result.get('error')}")

    # Record Sales Partner Transaction for paid invoices as well
    try:
        idemp = f"{inv.name}::sales_partner_paid"
        existing_spt = frappe.get_all(
            "Sales Partner Transactions",
            filters={"idempotency_token": idemp},
            pluck="name",
            limit_page_length=1,
        )
        if not existing_spt:
            # Infer if paid online from either:
            #  1) SI POS payments child table (any non-cash payment)
            #  2) Linked submitted Payment Entries' mode_of_payment (any non-cash)
            is_online = False
            try:
                # Check POS payments table first (present on POS-style invoices)
                for p in (getattr(inv, "payments", []) or []):
                    mode = (p.get("mode_of_payment") or "").strip().lower()
                    amt = float(p.get("amount") or p.get("base_amount") or 0)
                    if amt > 0 and mode and mode != "cash":
                        is_online = True
                        break
                # If still undetermined, inspect linked Payment Entries
                if not is_online:
                    pe_parents = frappe.get_all(
                        "Payment Entry Reference",
                        filters={"reference_doctype": "Sales Invoice", "reference_name": inv.name},
                        pluck="parent",
                    )
                    if pe_parents:
                        rows = frappe.get_all(
                            "Payment Entry",
                            filters={"name": ["in", pe_parents], "docstatus": 1},
                            fields=["name", "mode_of_payment", "paid_amount"],
                        )
                        for r in rows:
                            mode = (r.get("mode_of_payment") or "").strip().lower()
                            amt = float(r.get("paid_amount") or 0)
                            if amt > 0 and mode and mode != "cash":
                                is_online = True
                                break
            except Exception:
                # Default to cash if uncertain to avoid applying extra fees by mistake
                is_online = False
            fees = _compute_sales_partner_fees(inv, sales_partner, online=is_online)
            spt = frappe.new_doc("Sales Partner Transactions")
            spt.sales_partner = sales_partner
            spt.date = frappe.utils.now_datetime()
            spt.reference_invoice = inv.name
            spt.amount = float(inv.grand_total or 0)
            spt.partner_fees = fees.get("total_fees")
            spt.payment_mode = "Online" if is_online else "Cash"
            spt.idempotency_token = idemp
            spt.status = "Unsettled"
            # Store POS Profile from the Sales Invoice if available
            try:
                spt.pos_profile = getattr(inv, "pos_profile", None)
            except Exception:
                pass
            spt.notes = (
                ("Paid partner OFD – online payment" if is_online else "Paid partner OFD – cash payment")
                + " | "
                + f"fees: base={fees.get('base_fees')} vat={fees.get('vat')} total={fees.get('total_fees')} | "
                + f"rates: commission={fees.get('commission_rate')}% online={fees.get('online_rate')}%"
            )
            spt.insert(ignore_permissions=True)
        else:
            # Backfill pos_profile if missing on existing record
            try:
                doc = frappe.get_doc("Sales Partner Transactions", existing_spt[0])
                if not getattr(doc, "pos_profile", None):
                    pp_val = getattr(inv, "pos_profile", None)
                    if pp_val:
                        doc.db_set("pos_profile", pp_val, update_modified=False)
            except Exception as _spt_backfill_err:
                frappe.logger().warning(f"SPT pos_profile backfill (paid) failed for {inv.name}: {_spt_backfill_err}")
    except Exception as _spt_err:
        frappe.logger().warning(f"SPT create (paid) failed for {inv.name}: {_spt_err}")

    payload = {
        "success": True,
        "invoice": inv.name,
        "delivery_note": dn_result.get("delivery_note"),
        "delivery_note_reused": dn_result.get("reused"),
        "sales_partner": sales_partner,
        "mode": "sales_partner_paid",
    }
    frappe.publish_realtime("jarz_pos_sales_partner_paid_ofd", payload, user="*")
    return payload


@frappe.whitelist()
def handle_out_for_delivery_paid(invoice_name: str, courier: str, settlement: str = "cash_now", pos_profile: str = "", party_type: str | None = None, party: str | None = None):
    """Handle Out For Delivery transition for PAID invoices with courier settlement.
    
    Args:
        invoice_name: Sales Invoice name
        courier: Courier identifier (legacy, not used for party operations)
        settlement: 'cash_now' (immediate settlement) or 'later' (deferred)
        pos_profile: POS Profile for account resolution
        party_type: 'Employee' or 'Supplier'
        party: Party identifier (Employee ID or Supplier name)
    
    Returns:
        dict with success, journal_entry (if cash_now), courier_transaction (if later), delivery_note, etc.
    """
    if not invoice_name:
        frappe.throw("invoice_name is required")
    if not (party_type and party):
        frappe.throw("party_type and party are required for courier operations")
    
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")
    
    company = inv.company
    
    # Get shipping expense
    shipping_exp = _get_delivery_expense_amount(inv)
    
    # Update state to Out for Delivery
    if inv.get("custom_sales_invoice_state") != "Out for Delivery":
        try:
            inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
        except Exception:
            inv.set("custom_sales_invoice_state", "Out for Delivery")
            inv.save(ignore_permissions=True)
    
    # Ensure Delivery Note
    dn_result = ensure_delivery_note_for_invoice(inv.name)
    if dn_result.get("error"):
        frappe.throw(f"Failed auto-creating Delivery Note: {dn_result.get('error')}")
    
    result = {
        "success": True,
        "invoice": inv.name,
        "delivery_note": dn_result.get("delivery_note"),
        "delivery_note_reused": dn_result.get("reused"),
        "shipping_amount": shipping_exp or 0,
    }
    
    if settlement == "cash_now" and shipping_exp and shipping_exp > 0:
        # Immediate settlement: Branch pays courier shipping expense via Journal Entry
        creditors_acc = get_creditors_account(company)
        je_name = _create_shipping_expense_to_creditors_je(inv, shipping_exp, creditors_acc, party_type, party)
        result["journal_entry"] = je_name
        
        # Create Settled Courier Transaction for record keeping
        ct = frappe.new_doc("Courier Transaction")
        ct.party_type = party_type
        ct.party = party
        ct.date = frappe.utils.now_datetime()
        ct.reference_invoice = inv.name
        ct.amount = 0  # Invoice already paid
        ct.shipping_amount = float(shipping_exp or 0)
        ct.status = "Settled"
        ct.payment_mode = "Cash"
        ct.notes = f"Paid invoice courier settlement (immediate) - JE: {je_name}"
        ct.insert(ignore_permissions=True)
        result["courier_transaction"] = ct.name
        
    elif settlement == "later":
        # Deferred settlement: Create Unsettled Courier Transaction
        existing_ct = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": inv.name,
                "party_type": party_type,
                "party": party,
                "status": ["!=", "Settled"],
            },
            pluck="name",
            limit_page_length=1,
        )
        if existing_ct:
            ct_name = existing_ct[0]
        else:
            ct = frappe.new_doc("Courier Transaction")
            ct.party_type = party_type
            ct.party = party
            ct.date = frappe.utils.now_datetime()
            ct.reference_invoice = inv.name
            ct.amount = 0  # Invoice already paid
            ct.shipping_amount = float(shipping_exp or 0)
            ct.status = "Unsettled"
            ct.payment_mode = "Deferred"
            ct.notes = "Paid invoice courier settlement (deferred)"
            ct.insert(ignore_permissions=True)
            ct_name = ct.name
        result["courier_transaction"] = ct_name
    
    # Publish realtime event
    frappe.publish_realtime("jarz_pos_ofd_paid", result, user="*")
    return result


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
    
    # Determine expense amount based on invoice territory
    amount = _get_delivery_expense_amount(inv)
    if amount <= 0:
        frappe.throw("No delivery expense configured for the invoice territory.")
    
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
        frappe.throw("No delivery expense configured for the invoice territory.")
    
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
        loc_label = _get_invoice_city(inv)
        grp["details"].append({
            "invoice": inv,
            "city": loc_label,       # back-compat key kept
            "territory": loc_label,  # new explicit key
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

    # Ensure / create Delivery Note (abort if failure per business requirement)
    dn_result = ensure_delivery_note_for_invoice(inv.name)
    if dn_result.get("error"):
        frappe.throw(f"Failed auto-creating Delivery Note: {dn_result.get('error')}")

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

        if je_name and shipping_exp > 0:
            try:
                existing_je = frappe.get_doc("Journal Entry", je_name)
                has_expected_freight = any(
                    acc.account == freight_acc and float(acc.debit_in_account_currency or 0) > 0
                    for acc in existing_je.accounts
                )
                if settlement == "cash_now":
                    has_expected_cash = any(
                        acc.account == cash_acc and float(acc.credit_in_account_currency or 0) > 0
                        for acc in existing_je.accounts
                    )
                    mismatch = not (has_expected_freight and has_expected_cash)
                else:
                    has_expected_creditors = any(
                        acc.account == creditors_acc and float(acc.credit_in_account_currency or 0) > 0
                        for acc in existing_je.accounts
                    )
                    mismatch = not (has_expected_freight and has_expected_creditors)
                if mismatch:
                    frappe.logger().info(
                        f"handle_out_for_delivery_paid correcting legacy JE {je_name} for settlement={settlement}"
                    )
                    existing_je.cancel()
                    existing_je.delete(ignore_permissions=True)
                    je_name = None
            except Exception:
                je_name = None

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
        # Desired amount logic (business rule update 2025-09-12):
        #   For immediate cash settlement (settlement == 'cash_now') the CT.amount
        #   should reflect the full invoice grand total so courier collection
        #   reporting shows the principal moved at this transition.
        #   For 'later' we continue to record 0 principal (only shipping expense accrued).
        desired_amount = float(inv.grand_total or 0) if settlement == "cash_now" else 0.0
        # Idempotency now does not rely on legacy 'courier' link
        ct_filters = {
            "reference_invoice": inv.name,
            "notes": ["like", "%Out For Delivery transition%"],
        }
        existing_ct = frappe.get_all("Courier Transaction", filters=ct_filters, pluck="name", limit_page_length=1)
        if existing_ct:
            ct_name = existing_ct[0]
            # Backfill amount / shipping if prior logic stored 0 for cash_now path
            if settlement == "cash_now":
                try:
                    ct_doc = frappe.get_doc("Courier Transaction", ct_name)
                    current_amt = float(ct_doc.get("amount") or 0)
                    if abs(current_amt - desired_amount) > 0.005:
                        # Update without bumping modified timestamp noisily
                        frappe.db.set_value(
                            "Courier Transaction",
                            ct_name,
                            {
                                "amount": desired_amount,
                                "shipping_amount": shipping_exp,
                                "status": "Settled",
                                "payment_mode": "cash_now",
                            },
                            update_modified=False,
                        )
                except Exception as _ct_update_err:
                    frappe.logger().warning(
                        f"OFD CT backfill failed for {ct_name}: {_ct_update_err}"
                    )
        else:
            ct = frappe.new_doc("Courier Transaction")
            # Do not set legacy 'courier' field (target DocType removed)
            ct.party_type = party_type
            ct.party = party
            ct.date = frappe.utils.now_datetime()
            ct.reference_invoice = inv.name
            ct.amount = desired_amount
            ct.shipping_amount = shipping_exp
            ct.status = "Settled" if settlement == "cash_now" else "Unsettled"
            # Normalize payment_mode values for consistency (legacy used settlement values)
            ct.payment_mode = "cash_now" if settlement == "cash_now" else "later"
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
        "delivery_note": dn_result.get("delivery_note"),
        "delivery_note_reused": dn_result.get("reused"),
        "dn_logic_version": DN_LOGIC_VERSION,
    }
    frappe.publish_realtime("jarz_pos_out_for_delivery_transition", payload, user="*")
    return {"success": True, **payload}


@frappe.whitelist()
def handle_out_for_delivery_transition(invoice_name: str, courier: str, mode: str, pos_profile: str, idempotency_token: str | None = None, party_type: str | None = None, party: str | None = None):
    """Unified Out For Delivery transition (Simplified per 2025-09 rules).

    New business rules:
      - "Settle Later" is not supported anymore (client always passes pay_now, but we ignore mode).
      - If invoice is already PAID: do NOT create any Journal Entry or Courier Transaction.
        Only update state to Out For Delivery and ensure a Delivery Note exists.
      - If invoice is UNPAID: create a Cash Payment Entry for the full outstanding amount
        to the POS Profile's cash account; then update state & ensure Delivery Note.
      - Courier info (party_type/party) is optional and ignored in this simplified flow.
      - A realtime "collect cash" prompt is published for unpaid invoices.
    """
    try:
        # ---- Normalize inputs ----
        invoice_name = (invoice_name or '').strip()
        courier = (courier or '').strip()
        pos_profile = (pos_profile or '').strip()
        token = (idempotency_token or '').strip() or None
        if not invoice_name:
            frappe.throw("invoice_name required")
        if not pos_profile:
            frappe.throw("pos_profile required")

        inv = frappe.get_doc("Sales Invoice", invoice_name)
        if inv.docstatus != 1:
            frappe.throw("Invoice must be submitted")

        # Determine outstanding (fresh from DB)
        try:
            outstanding = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
        except Exception:
            outstanding = float(inv.outstanding_amount or 0)

        company = inv.company
        cash_acc = get_pos_cash_account(pos_profile, company)
        validate_account_exists(cash_acc)

        # If UNPAID: publish prompt and create Payment Entry to POS cash account
        payment_entry_name = None
        if outstanding > 0.0001:
            try:
                # Publish a realtime prompt first so UI can show "Collect Cash"
                try:
                    frappe.publish_realtime(
                        "jarz_pos_collect_cash",
                        {
                            "invoice": inv.name,
                            "amount": float(inv.grand_total or 0),
                            "outstanding": float(outstanding or 0),
                            "mode": "collect_cash",
                        },
                        user="*",
                    )
                except Exception:
                    pass

                # Reuse existing cash PE if already allocated
                ref_parents = frappe.get_all(
                    "Payment Entry Reference",
                    filters={"reference_doctype": "Sales Invoice", "reference_name": inv.name},
                    pluck="parent",
                )
                if ref_parents:
                    rows = frappe.get_all(
                        "Payment Entry",
                        filters={"name": ["in", ref_parents], "docstatus": 1, "payment_type": "Receive", "paid_to": cash_acc},
                        fields=["name", "paid_amount"],
                    )
                    if rows:
                        payment_entry_name = rows[0]["name"]
                if not payment_entry_name:
                    # Resolve receivable
                    receivable_acc = getattr(inv, "debit_to", None) or frappe.db.get_value("Company", company, "default_receivable_account")
                    if not receivable_acc:
                        frappe.throw("Could not resolve receivable account for invoice")
                    pe = frappe.new_doc("Payment Entry")
                    pe.payment_type = "Receive"
                    pe.company = company
                    pe.posting_date = frappe.utils.nowdate()
                    pe.mode_of_payment = "Cash"
                    pe.party_type = "Customer"
                    pe.party = inv.customer
                    pe.paid_from = receivable_acc
                    pe.paid_to = cash_acc
                    pe.paid_amount = outstanding
                    pe.received_amount = outstanding
                    pe.append("references", {
                        "reference_doctype": "Sales Invoice",
                        "reference_name": inv.name,
                        "allocated_amount": outstanding,
                    })
                    pe.flags.ignore_permissions = True
                    pe.insert(ignore_permissions=True)
                    pe.submit()
                    payment_entry_name = pe.name
            except Exception as pe_err:
                # If invoice is already fully paid due to race, continue gracefully
                msg = str(pe_err)
                if "already been fully paid" not in msg and "already paid" not in msg.lower():
                    raise

        # Update operational state idempotently
        if inv.get("custom_sales_invoice_state") != "Out for Delivery":
            try:
                inv.db_set("custom_sales_invoice_state", "Out for Delivery", update_modified=True)
            except Exception:
                inv.set("custom_sales_invoice_state", "Out for Delivery")
                inv.save(ignore_permissions=True)

        # Ensure Delivery Note exists (reuse/create)
        dn_result = ensure_delivery_note_for_invoice(inv.name)
        if dn_result.get("error"):
            frappe.throw(f"Failed auto-creating Delivery Note: {dn_result.get('error')}")

        # Emit unified realtime event for Kanban patching
        payload = {
            "invoice": inv.name,
            "courier": courier or "",
            "mode": "collect_cash" if outstanding > 0.0001 else "paid_noops",
            "payment_entry": payment_entry_name,
            "amount": float(inv.grand_total or 0),
            "outstanding_before": float(outstanding or 0),
            "delivery_note": dn_result.get("delivery_note"),
            "delivery_note_reused": dn_result.get("reused"),
            "dn_logic_version": DN_LOGIC_VERSION,
        }
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
        frappe.throw("No shipping expense configured for this invoice's territory")

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
            # Always clear previously accrued payable: DR Creditors / CR Cash.
            # (Expense was recognized at Out For Delivery stage.)
            je.append("accounts", {
                "account": creditors_acc,
                "party_type": party_type,
                "party": party,
                "debit_in_account_currency": shipping_exp,
                "credit_in_account_currency": 0,
            })
            je.append("accounts", {
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
    Return delivery expense amount (float) for the given invoice.

    Waiver rule (highest priority): If the invoice contains a Jarz Bundle that
    has the Free Shipping flag enabled, return 0.0 regardless of Territory setup.

    Default strategy when no waiver applies:
      1) Resolve Territory from Sales Invoice (inv.territory), else from Customer.
      2) Discover available Territory columns and probe common field names for shipping/expense
         (both custom_ and standard-style) WITHOUT triggering unknown-column errors.
      3) If zero/None, walk up the territory tree via parent_territory until root.

    Returns 0.0 if not found.
    """
    # 0) Pickup short-circuit (highest priority overall)
    try:
        # Check common custom fields
        pickup_fields = ["custom_is_pickup", "is_pickup", "pickup"]
        for f in pickup_fields:
            try:
                val = getattr(inv, f, None)
                if val is None:
                    # Try DB value directly to avoid cache misses
                    val = frappe.db.get_value("Sales Invoice", inv.name, f)
                if (val is True) or (str(val).strip() in {"1", "True", "true", "YES", "yes"}):
                    return 0.0
            except Exception:
                continue
        # Fallback: remarks marker
        try:
            remarks = (getattr(inv, "remarks", "") or "")
            if "[PICKUP]" in remarks:
                return 0.0
        except Exception:
            pass
    except Exception:
        pass

    # 1) Free-shipping bundle short-circuit
    try:
        # Detect either parent or child lines associated with a Jarz Bundle that has free_shipping=1
        # Parent heuristic: Sales Invoice Item has is_bundle_parent flag or matches a Jarz Bundle's erpnext_item
        bundle_ids = set()
        for it in getattr(inv, 'items', []) or []:
            code = (getattr(it, 'item_code', None) or '').strip()
            if not code:
                continue
            # Direct match via bundle record (when parent/child helper stored bundle_code)
            try:
                bcode = getattr(it, 'bundle_code', None) or getattr(it, 'parent_bundle', None)
                if bcode and frappe.db.exists('Jarz Bundle', bcode):
                    bundle_ids.add(bcode)
            except Exception:
                pass
            # Heuristic: parent line is ERPNext item equal to bundle.erpnext_item
            try:
                rows = frappe.get_all('Jarz Bundle', filters={'erpnext_item': code}, pluck='name')
                for r in rows:
                    bundle_ids.add(r)
            except Exception:
                pass
        if bundle_ids:
            # Any of these bundles free_shipping?
            cols = set()
            try:
                cols = set(frappe.db.get_table_columns('Jarz Bundle') or [])
            except Exception:
                cols = set()
            has_flag = 'free_shipping' in cols
            if has_flag:
                flags = frappe.get_all('Jarz Bundle', filters={'name': ['in', list(bundle_ids)], 'free_shipping': 1}, pluck='name')
                if flags:
                    return 0.0
    except Exception:
        # If detection fails, fall back to territory logic
        pass
    # 2) Resolve territory from invoice, then customer as fallback
    territory = (inv.get("territory") or "").strip()
    if not territory:
        try:
            territory = (frappe.db.get_value("Customer", inv.customer, "territory") or "").strip()
        except Exception:
            territory = ""
    if not territory:
        # Nothing to resolve from
        return 0.0

    # Discover real columns on Territory to avoid unknown-column failures
    try:
        columns = set(frappe.db.get_table_columns("Territory") or [])
    except Exception:
        columns = set()

    # Priority-ordered candidate field names (first positive wins)
    candidate_fields = [
        "custom_delivery_expense",
        "custom_shipping_expense",
        "custom_delivery_fee",
        "custom_shipping_fee",
        "delivery_expense",
        "shipping_expense",
        "delivery_fee",
        "shipping_fee",
    ]
    valid_fields = [f for f in candidate_fields if f in columns]

    def first_positive_value(territory_name: str) -> float:
        # Probe one field at a time to avoid unknown-column errors
        for fld in valid_fields:
            try:
                val = frappe.db.get_value("Territory", territory_name, fld)
                val_f = float(val or 0)
                if val_f > 0:
                    return val_f
            except Exception:
                # Ignore conversion/errors and try next field
                continue
        return 0.0

    # Walk up the territory tree until we find a configured amount
    current = territory
    visited = set()
    while current and current not in visited:
        visited.add(current)
        amt = first_positive_value(current)
        if amt > 0:
            return amt
        try:
            current = frappe.db.get_value("Territory", current, "parent_territory") or None
        except Exception:
            current = None

    # Debug logging when expense not found – assists diagnosing zero shipping_amount
    try:
        frappe.log_error(
            title="Delivery Expense Resolution Miss (Territory)",
            message=(
                f"Invoice: {inv.name}\n"
                f"Resolved from Territory chain starting at: {territory}\n"
                f"Checked fields: {', '.join(valid_fields) or '<none>'}\n"
                f"No positive value found on any ancestor"
            ),
        )
    except Exception:
        pass
    return 0.0


def _get_invoice_city(invoice_name):
    """Back-compat: return a label for the invoice location; now uses Territory.

    We keep the function name and return value purpose the same to avoid breaking
    clients expecting a 'city' label. The value returned will be the Territory's
    display name.
    """
    if not invoice_name:
        return ""

    try:
        inv = frappe.db.get_value(
            "Sales Invoice",
            invoice_name,
            ["territory", "customer"],
            as_dict=True,
        )
    except Exception:
        inv = None
    territory = ""
    if inv:
        territory = (inv.get("territory") or "").strip()
        if not territory:
            try:
                territory = (frappe.db.get_value("Customer", inv.get("customer"), "territory") or "").strip()
            except Exception:
                territory = ""
    if not territory:
        return ""

    try:
        name, disp = frappe.db.get_value("Territory", territory, ["name", "territory_name"]) or (None, None)
        return (disp or name or "").strip()
    except Exception:
        return territory


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
        "details": [ {"invoice": ..., "city": ..., "territory": ..., "amount": ..., "shipping": ...}, ... ],
        # Legacy (kept for older clients):
        "courier": "<legacy courier id or party>",
        "courier_name": "<display_name>"
      }
    """
    # Determine available columns defensively – staging may have different customizations
    try:
        cols = set(frappe.db.get_table_columns("Courier Transaction") or [])
    except Exception:
        cols = set()

    fields = ["name", "reference_invoice"]
    if "amount" in cols:
        fields.append("amount")
    if "shipping_amount" in cols:
        fields.append("shipping_amount")
    if "party_type" in cols:
        fields.append("party_type")
    if "party" in cols:
        fields.append("party")
    if "courier" in cols:
        fields.append("courier")  # legacy label column, may not exist

    rows = frappe.get_all(
        "Courier Transaction",
        filters={"status": ["!=", "Settled"]},
        fields=fields,
    )

    groups = {}

    def _ensure_group(party_type: str, party: str, legacy_courier: str | None):
        key = (party_type or "", party or (legacy_courier or ""))
        if key not in groups:
            # Resolve display label from party when possible
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
                label = (legacy_courier or key[1] or "<Unknown>")

            groups[key] = {
                "party_type": key[0],
                "party": key[1],
                "display_name": label,
                # Legacy keys
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

        grp = _ensure_group(party_type, party, legacy_courier)
        # Numeric fields may be absent; coerce safely
        try:
            amt = float(r.get("amount") or 0)
        except Exception:
            amt = 0.0
        try:
            ship = float(r.get("shipping_amount") or 0)
        except Exception:
            ship = 0.0
        grp["balance"] += amt - ship
        inv = r.get("reference_invoice")
        loc_label = _get_invoice_city(inv)
        grp["details"].append({
            "invoice": inv,
            "city": loc_label,
            "territory": loc_label,
            "amount": amt,
            "shipping": ship,
        })

    data = list(groups.values())
    data.sort(key=lambda d: d.get("balance", 0.0), reverse=True)
    return data


def _create_payment_entry(inv, paid_from_account, paid_to_account, outstanding, courier_party_type=None, courier_party=None):
    """Create and submit payment entry for courier outstanding.
    
    Special handling: If paid_to is 'Courier Outstanding' (internal tracking account),
    use Journal Entry instead of Payment Entry, with courier as party on Courier Outstanding side.
    """
    # Check if target account is Courier Outstanding - use JE instead of PE
    is_courier_outstanding = "Courier Outstanding" in paid_to_account
    
    if is_courier_outstanding:
        # Use Journal Entry for Debtors → Courier Outstanding transfer
        # This allows us to specify different parties for each account
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = inv.company
        je.title = f"Courier Outstanding – {inv.name}"
        
        # CR Debtors (reduce receivable from customer)
        je.append("accounts", {
            "account": paid_from_account,
            "party_type": "Customer",
            "party": inv.customer,
            "credit_in_account_currency": outstanding,
            "debit_in_account_currency": 0,
            "reference_type": "Sales Invoice",
            "reference_name": inv.name,
        })
        
        # DR Courier Outstanding (track amount courier owes us - with courier as party)
        courier_entry = {
            "account": paid_to_account,
            "debit_in_account_currency": outstanding,
            "credit_in_account_currency": 0,
        }
        # Add courier party only when the ledger supports party assignments
        if courier_party_type and courier_party:
            account_type = frappe.db.get_value("Account", paid_to_account, "account_type")
            if account_type in {"Receivable", "Payable"}:
                courier_entry["party_type"] = courier_party_type
                courier_entry["party"] = courier_party
            else:
                frappe.logger().warning(
                    "Skipping party assignment for %s (type: %s) during courier outstanding transfer",
                    paid_to_account,
                    account_type,
                )
        je.append("accounts", courier_entry)
        
        je.user_remark = f"Transfer receivable to Courier Outstanding for {inv.name}"
        je.save(ignore_permissions=True)
        je.submit()
        return je
    else:
        # Normal Receive: from Customer to Cash/Bank
        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Receive"
        pe.company = inv.company
        pe.party_type = "Customer"
        pe.party = inv.customer
        pe.paid_from = paid_from_account  # Debtors (party account)
        pe.paid_to = paid_to_account      # Cash/Bank account
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
        pe.reference_no = f"PAY-{inv.name}"
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

"""Jarz POS – Invoice-related API endpoints.

Clean implementation using Frappe best practices.
Only handles cart items - never treats shipping as an item.
"""

from __future__ import annotations
import frappe
import json

# Import from the refactored services
from jarz_pos.services.invoice_creation import create_pos_invoice as _create_invoice
from jarz_pos.services import delivery_handling as _delivery


# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------


@frappe.whitelist()
def create_pos_invoice():
    """
    Create POS Sales Invoice - Clean implementation with comprehensive debugging
    
    This function reads parameters from frappe.form_dict instead of function parameters
    to avoid Frappe's parameter mapping issues.
    
    Returns:
        dict: Invoice creation result
    """
    
    # Get parameters from frappe.form_dict (Frappe's way)
    cart_json = frappe.form_dict.get('cart_json')
    customer_name = frappe.form_dict.get('customer_name')
    pos_profile_name = frappe.form_dict.get('pos_profile_name')
    delivery_charges_json = frappe.form_dict.get('delivery_charges_json')
    required_delivery_datetime = frappe.form_dict.get('required_delivery_datetime')
    sales_partner = frappe.form_dict.get('sales_partner')
    payment_type = frappe.form_dict.get('payment_type')  # 'cash' | 'online' (optional)
    # New: pickup flag (no delivery fee)
    raw_pickup = frappe.form_dict.get('pickup')
    is_pickup = False
    try:
        if isinstance(raw_pickup, (int, float)):
            is_pickup = int(raw_pickup) == 1
        elif isinstance(raw_pickup, str):
            is_pickup = raw_pickup.strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        is_pickup = False
    # New delivery slot fields (optional; preferred)
    delivery_date = frappe.form_dict.get('delivery_date')
    delivery_time_from = frappe.form_dict.get('delivery_time_from')
    delivery_duration = frappe.form_dict.get('delivery_duration')
    
    # Frappe best practice: Use frappe.logger() for structured logging
    logger = frappe.logger("jarz_pos.api.invoices", allow_site=frappe.local.site)
    
    # Always log API calls in development
    frappe.log_error(
        title="POS API Call Debug",
        message=f"""
API ENDPOINT: create_pos_invoice
TIMESTAMP: {frappe.utils.now()}
USER: {frappe.session.user}
SITE: {frappe.local.site}
METHOD: {getattr(frappe.local.request, 'method', 'N/A')}

FORM_DICT: {frappe.form_dict}

RAW PARAMETERS:
- cart_json (type: {type(cart_json)}): {cart_json}
- customer_name (type: {type(customer_name)}): {customer_name}
- pos_profile_name (type: {type(pos_profile_name)}): {pos_profile_name}
- delivery_charges_json (type: {type(delivery_charges_json)}): {delivery_charges_json}
- required_delivery_datetime (type: {type(required_delivery_datetime)}): {required_delivery_datetime}
        """.strip()
    )
    
    # Console output for development debugging
    print("\n" + "="*100)
    print("� JARZ POS API CALL")
    print("="*100)
    print(f"🕐 {frappe.utils.now()}")
    print(f"� User: {frappe.session.user}")
    print(f"🌐 Site: {frappe.local.site}")
    print(f"� Method: {getattr(frappe.local.request, 'method', 'N/A')}")
    print(f"🔗 Endpoint: /api/method/jarz_pos.api.invoices.create_pos_invoice")
    
    print(f"\n📋 INCOMING PARAMETERS:")
    print(f"   cart_json: {cart_json} (type: {type(cart_json)})")
    print(f"   customer_name: {customer_name} (type: {type(customer_name)})")
    print(f"   pos_profile_name: {pos_profile_name} (type: {type(pos_profile_name)})")
    print(f"   delivery_charges_json: {delivery_charges_json} (type: {type(delivery_charges_json)})")
    print(f"   required_delivery_datetime: {required_delivery_datetime} (type: {type(required_delivery_datetime)})")
    print(f"   delivery_date: {delivery_date} | delivery_time_from: {delivery_time_from} | delivery_duration: {delivery_duration}")
    print(f"   pickup: {is_pickup}")
    
    try:
        # Validate parameters before calling legacy function
        if not cart_json:
            error_msg = "cart_json parameter is required"
            print(f"❌ VALIDATION ERROR: {error_msg}")
            frappe.throw(error_msg)
        
        if not customer_name:
            error_msg = "customer_name parameter is required"
            print(f"❌ VALIDATION ERROR: {error_msg}")
            frappe.throw(error_msg)
        
        # Parse cart_json to validate it's proper JSON
        if isinstance(cart_json, str):
            try:
                parsed_cart = json.loads(cart_json)
                print(f"✅ Cart JSON parsed successfully: {len(parsed_cart)} items")
            except json.JSONDecodeError as e:
                error_msg = f"Invalid JSON in cart_json: {str(e)}"
                print(f"❌ JSON PARSE ERROR: {error_msg}")
                frappe.throw(error_msg)
        
        print(f"\n🔄 Calling core function...")
        
        # Prefer new delivery slot fields; if provided, synthesize a delivery_datetime for service layer
        if delivery_date and delivery_time_from:
            try:
                # Construct ISO string; duration is handled inside service/hook
                required_delivery_datetime = f"{delivery_date} {delivery_time_from}"
            except Exception:
                pass

        # Call the refactored service function
        result = _create_invoice(
            cart_json,
            customer_name,
            pos_profile_name,
            delivery_charges_json,
            required_delivery_datetime,
            sales_partner,
            payment_type,
            is_pickup,
        )
        
        # Log successful response
        print(f"\n✅ API CALL SUCCESSFUL!")
        print(f"📤 RESPONSE:")
        print(f"   Type: {type(result)}")
        if isinstance(result, dict):
            for key, value in result.items():
                print(f"   {key}: {value}")
        else:
            print(f"   Value: {result}")
        
        # Frappe best practice: Log success to error log for debugging
        frappe.log_error(
            title="POS API Success Debug",
            message=f"""
API ENDPOINT: create_pos_invoice
STATUS: SUCCESS
RESPONSE: {json.dumps(result, indent=2, default=str)}
            """.strip()
        )
        
        print("="*100)
        return result
        
    except Exception as e:
        # Comprehensive error logging
        error_details = f"""
API ENDPOINT: create_pos_invoice
STATUS: ERROR
ERROR TYPE: {type(e).__name__}
ERROR MESSAGE: {str(e)}
PARAMETERS:
- cart_json: {cart_json}
- customer_name: {customer_name}
- pos_profile_name: {pos_profile_name}
USER: {frappe.session.user}
SITE: {frappe.local.site}
        """.strip()
        
        print(f"\n❌ API ERROR:")
        print(f"   Type: {type(e).__name__}")
        print(f"   Message: {str(e)}")
        
        # Print full traceback for debugging
        import traceback
        print(f"   Traceback:")
        traceback.print_exc()
        
        # Frappe best practice: Log error with full context
        frappe.log_error(
            title=f"POS API Error: {type(e).__name__}",
            message=error_details + f"\n\nTRACEBACK:\n{traceback.format_exc()}"
        )
        
        print("="*100)
        
        # Re-raise the exception (don't suppress it)
        raise


@frappe.whitelist(allow_guest=False)
def pay_invoice(
    invoice_name: str,
    payment_mode: str,
    pos_profile: str | None = None,
    reference_no: str | None = None,
    reference_date: str | None = None,
):
    """Create a Payment Entry for a submitted Sales Invoice.

    Adds mandatory Reference No / Reference Date when payment_mode is Wallet or InstaPay.

    payment_mode:
      - Wallet   -> "Mobile Wallet - <COMPANY ABBR>" (requires reference_no & reference_date)
      - InstaPay -> "Bank Account - <COMPANY ABBR>" (requires reference_no & reference_date)
      - Cash     -> "<POS PROFILE NAME> - <COMPANY ABBR>" (requires pos_profile; reference fields optional/ignored)

    Args:
        invoice_name: Sales Invoice name (must be submitted, outstanding)
        payment_mode: wallet | instapay | cash (case-insensitive)
        pos_profile: POS Profile (required when payment_mode == cash)
        reference_no: External transaction / bank reference (required for wallet & instapay)
        reference_date: Date string (YYYY-MM-DD) of external transaction (required for wallet & instapay)
    """
    try:
        if not invoice_name:
            frappe.throw("invoice_name is required")
        if not payment_mode:
            frappe.throw("payment_mode is required")
        payment_mode = payment_mode.strip()

        inv = frappe.get_doc("Sales Invoice", invoice_name)
        if inv.docstatus != 1:
            frappe.throw("Invoice must be submitted before registering payment")
        # Always re-read the latest outstanding from the database to avoid cache/stale values
        try:
            latest_outstanding = frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount")
            outstanding = float(latest_outstanding or 0)
        except Exception:
            outstanding = float(inv.outstanding_amount or 0)
        if outstanding <= 0.0001:
            # Try to hint if a Payment Entry already exists
            try:
                ref_parents = frappe.get_all(
                    "Payment Entry Reference",
                    filters={"reference_doctype": "Sales Invoice", "reference_name": inv.name},
                    pluck="parent",
                )
                pe_list = []
                if ref_parents:
                    pe_list = frappe.get_all(
                        "Payment Entry",
                        filters={"name": ["in", ref_parents], "docstatus": 1, "payment_type": "Receive"},
                        pluck="name",
                    )
                if pe_list:
                    frappe.throw(f"Invoice already paid. Existing Payment Entry: {', '.join(pe_list)}")
            except Exception:
                pass
            frappe.throw("Invoice already paid (no outstanding amount)")
        company = inv.company
        company_abbr = frappe.db.get_value("Company", company, "abbr") or ""

        # Map payment mode to destination account base name
        mode_lower = payment_mode.lower()
        if mode_lower == "wallet":
            account_base = "Mobile Wallet"
            # Wallet payments require reference metadata – auto-generate if absent
            if not reference_no:
                reference_no = f"WAL-{frappe.generate_hash(length=8)}"
            if not reference_date:
                reference_date = frappe.utils.nowdate()
        elif mode_lower == "instapay":
            account_base = "Bank Account"
            # InstaPay payments require reference metadata – auto-generate if absent
            if not reference_no:
                reference_no = f"IPY-{frappe.generate_hash(length=8)}"
            if not reference_date:
                reference_date = frappe.utils.nowdate()
        elif mode_lower == "cash":
            if not pos_profile:
                frappe.throw("pos_profile is required for Cash payments")
            account_base = pos_profile  # POS profile name itself
        else:
            frappe.throw(f"Unsupported payment_mode: {payment_mode}")

        # Validate reference_date format if provided (allow strict date only)
        if reference_date:
            try:
                # getdate will raise for invalid formats; keep original string assignment later
                frappe.utils.getdate(reference_date)
            except Exception:
                frappe.throw("Invalid reference_date format. Use YYYY-MM-DD")

        paid_to_account = f"{account_base} - {company_abbr}".strip()
        if not frappe.db.exists("Account", paid_to_account):
            frappe.throw(f"Destination account not found: {paid_to_account}")

        # Determine receivable (paid_from) account
        receivable = frappe.get_cached_value("Company", company, "default_receivable_account")
        if not receivable:
            # Fallback: first non-group Receivable account for company
            receivable = frappe.db.get_value(
                "Account",
                {"company": company, "account_type": "Receivable", "is_group": 0},
                "name",
            )
        if not receivable:
            frappe.throw("Could not determine receivable account for company")

        # Create Payment Entry
        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Receive"
        pe.party_type = "Customer"
        pe.party = inv.customer
        pe.company = company
        pe.posting_date = frappe.utils.today()
        pe.mode_of_payment = payment_mode if frappe.db.exists("Mode of Payment", payment_mode) else None
        pe.paid_from = receivable
        pe.paid_to = paid_to_account
        pe.party_account = receivable  # ensure attribute for older meta / set_missing_values
        pe.paid_amount = outstanding
        pe.received_amount = outstanding
        pe.references = []
        pe.append("references", {
            "reference_doctype": "Sales Invoice",
            "reference_name": inv.name,
            "due_date": inv.get("due_date"),
            "total_amount": float(inv.grand_total or 0),
            "outstanding_amount": outstanding,
            "allocated_amount": outstanding,
        })
        # Set flags to bypass POS validations if needed
        pe.flags.ignore_permissions = True
        try:
            pe.set_missing_values()
        except AttributeError:
            # Older Payment Entry implementation expecting party_account
            if not getattr(pe, 'party_account', None):
                pe.party_account = receivable
        # Assign reference metadata (only set if supplied for clarity)
        if reference_no:
            pe.reference_no = reference_no
        if reference_date:
            pe.reference_date = reference_date

        try:
            pe.insert(ignore_permissions=True)
            pe.submit()
        except Exception as pe_err:
            # Provide a clearer message when invoice is already settled
            msg = str(pe_err)
            if "already been fully paid" in msg.lower() or "fully paid" in msg.lower():
                # Re-evaluate outstanding to report precise state
                try:
                    latest = float(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount") or 0)
                except Exception:
                    latest = outstanding
                hint = f"(current outstanding={latest:.2f})"
                # Include any existing Payment Entry name if present
                try:
                    ref_parents = frappe.get_all(
                        "Payment Entry Reference",
                        filters={"reference_doctype": "Sales Invoice", "reference_name": inv.name},
                        pluck="parent",
                    )
                    if ref_parents:
                        frappe.throw(
                            f"Invoice already paid. Existing Payment Entry: {', '.join(ref_parents)} {hint}"
                        )
                except Exception:
                    pass
                frappe.throw(f"Invoice already paid {hint}")
            # Bubble other errors unchanged after logging
            frappe.log_error(
                title="Pay Invoice Submission Error",
                message=f"Invoice: {invoice_name}\nError: {frappe.get_traceback()}",
            )
            raise

        return {
            "success": True,
            "payment_entry": pe.name,
            "invoice": inv.name,
            "allocated_amount": outstanding,
            "paid_to": paid_to_account,
            "receivable": receivable,
            "reference_no": getattr(pe, "reference_no", None),
            "reference_date": getattr(pe, "reference_date", None),
        }
    except Exception as e:
        frappe.log_error(
            title="Pay Invoice Error",
            message=f"Invoice: {invoice_name}\nMode: {payment_mode}\nError: {frappe.get_traceback()}"
        )
        raise


@frappe.whitelist()
def get_invoice_settlement_preview(invoice_name: str, party_type: str | None = None, party: str | None = None):
    """Return settlement preview for confirmation popup.

        Logic (updated):
            - Determine order_amount from first unsettled Courier Transaction amount (>0) else 0.
            - shipping_amount derived from helper.
            - net_amount = order_amount - shipping_amount.
                * If net_amount > 0 => branch collects that positive amount from courier.
                * If net_amount < 0 => branch pays ABS(net_amount) to courier.
                * If net_amount == 0 and shipping>0 with no order_amount => pay shipping (treat as negative net for clarity).
            - scenario kept for backward compatibility but not required by frontend; branch_action derives from sign.

    Returns:
      {
        invoice, party_type, party,
        order_amount, shipping_amount,
        scenario: shipping_only | collect | pay_excess,
        branch_action: pay | collect,
        courier_amount: numeric amount branch pays (+) or collects (+) expressed as positive number,
        message: human readable string
      }
    """
    if not invoice_name:
        frappe.throw("invoice_name required")

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")

    # Derive party if missing from any CT
    if not (party_type and party):
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

    shipping = _delivery._get_delivery_expense_amount(inv) or 0.0  # protected helper reused

    # Fetch unsettled CTs; when party not provided, do NOT filter by party to avoid excluding valid rows
    ct_filters = {
        "reference_invoice": invoice_name,
        "status": ["!=", "Settled"],
    }
    if party_type and party:
        ct_filters["party_type"] = party_type
        ct_filters["party"] = party
    cts = frappe.get_all(
        "Courier Transaction",
        filters=ct_filters,
        fields=["name", "amount", "shipping_amount", "party_type", "party"],
    )

    order_amount = 0.0
    any_ct_order = False
    # pick first unsettled CT amount>0
    for r in cts:
        amt = float(r.get("amount") or 0)
        if amt > 0:
            order_amount = amt
            any_ct_order = True
            break
    invoice_total = float(inv.grand_total or 0)
    has_ct_rows = bool(cts)
    status_l = (str(inv.get("status") or "") or "").strip().lower()
    outstanding = float(inv.outstanding_amount or 0)
    is_unpaid = (
        outstanding > 0.01
        or status_l in {"unpaid", "overdue", "partially paid"}
    )
    is_paid = (outstanding <= 0.01 and status_l in {"paid", "credit note issued"})

    # Determine if customer actually paid via Payment Entry (Receive)
    has_customer_payment = False
    pe_names = []
    pe_first_creation = None
    try:
        ref_parents = frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Sales Invoice", "reference_name": inv.name},
            pluck="parent",
        )
        if ref_parents:
            rows = frappe.get_all(
                "Payment Entry",
                filters={"name": ["in", ref_parents], "docstatus": 1, "payment_type": "Receive"},
                fields=["name", "creation", "posting_date", "reference_no"],
            )
            pe_names = [r["name"] for r in rows]
            has_customer_payment = bool(rows)
            if rows:
                pe_first_creation = min([r["creation"] for r in rows if r.get("creation")])
    except Exception:
        # Ignore lookup errors; default to False
        has_customer_payment = False

    # Detect if OFD JE/CT exists and its creation timestamp
    ofd_creation = None
    try:
        ofd_rows = frappe.get_all(
            "Journal Entry",
            filters={"title": ["like", f"Out For Delivery – {inv.name}%"], "docstatus": 1, "company": inv.company},
            fields=["name", "creation"],
            limit_page_length=1,
        )
        if ofd_rows:
            ofd_creation = ofd_rows[0].get("creation")
        if not ofd_creation:
            ct_rows = frappe.get_all(
                "Courier Transaction",
                filters={"reference_invoice": inv.name},
                fields=["name", "creation"],
                limit_page_length=1,
            )
            if ct_rows:
                ofd_creation = ct_rows[0].get("creation")
    except Exception:
        pass

    # Key rule: For unpaid/overdue invoices (settle now path), always collect total - shipping
    # regardless of existing shipping-only courier transactions.
    # If system says Paid but payment seems to have been created AFTER OFD transition, treat as unpaid for preview
    paid_after_ofd = False
    if not is_unpaid and is_paid and pe_first_creation and ofd_creation and pe_first_creation > ofd_creation:
        paid_after_ofd = True
        is_unpaid = True

    if is_unpaid:
        order_amount = invoice_total
    else:
        if any_ct_order:
            # respect explicit CT order amount
            pass
        elif has_customer_payment or is_paid:
            # Customer paid -> shipping only
            order_amount = 0.0
        else:
            # No evidence of customer payment -> treat as settle-now
            order_amount = invoice_total

    net_amount = order_amount - shipping
    # Special case: pure shipping (order_amount == 0 < shipping)
    if order_amount == 0 and shipping > 0:
        net_amount = -shipping

    # Debug trace for diagnostics
    try:
        frappe.log_error(
            title="Settlement Preview Trace",
            message=(
                f"Invoice: {inv.name}\n"
                f"Status: {status_l} | Outstanding: {outstanding}\n"
                f"is_unpaid: {is_unpaid} | is_paid: {is_paid}\n"
                f"invoice_total: {invoice_total} | order_amount: {order_amount} | shipping: {shipping}\n"
                f"net_amount: {net_amount} | CTs: {len(cts)}"
            ),
        )
    except Exception:
        pass

    paid_note = "Paid" if (is_paid and not paid_after_ofd) else ("Unpaid" if is_unpaid else status_l.capitalize() or "Unknown")

    if net_amount > 0:
        scenario = "collect"
        branch_action = "collect"
        msg = f"Collect {net_amount:.2f} from courier (order {order_amount:.2f} - shipping {shipping:.2f}) – Invoice: {paid_note}"
    elif net_amount < 0:
        scenario = "pay" if order_amount == 0 else "pay_excess"
        branch_action = "pay"
        msg = f"Pay courier {abs(net_amount):.2f} (shipping {shipping:.2f} - order {order_amount:.2f}) – Invoice: {paid_note}"
    else:  # net_amount == 0
        scenario = "even"
        branch_action = "none"
        msg = f"Nothing to pay or collect – Invoice: {paid_note}"

    return {
        "invoice": inv.name,
        "party_type": party_type,
        "party": party,
    "order_amount": order_amount,
    "invoice_total": invoice_total,
        "shipping_amount": shipping,
    "scenario": scenario,
    "branch_action": branch_action,
    "net_amount": net_amount,
    "collect_amount": net_amount if net_amount > 0 else 0,
    "pay_amount": abs(net_amount) if net_amount < 0 else 0,
    "invoice_status": status_l,
    "outstanding": outstanding,
    "is_unpaid_effective": is_unpaid,
    "is_paid_now": is_paid,
    "paid_after_ofd": paid_after_ofd,
    "payment_entries": pe_names,
    "payment_first_creation": pe_first_creation,
    "ofd_creation": ofd_creation,
    "message": msg,
    }

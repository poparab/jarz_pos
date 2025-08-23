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
        
        # Call the refactored service function
        result = _create_invoice(cart_json, customer_name, pos_profile_name, delivery_charges_json, required_delivery_datetime)
        
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
        outstanding = float(inv.outstanding_amount or 0)
        if outstanding <= 0.0001:
            frappe.throw("Invoice already paid")
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

        pe.insert(ignore_permissions=True)
        pe.submit()

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

    Logic:
      - Look for any unsettled Courier Transaction for this invoice & party.
      - If none with amount>0, it's a shipping-only scenario: branch will PAY courier shipping.
      - If amount>0 and amount >= shipping -> branch COLLECTS (amount - shipping) from courier.
      - If amount>0 and shipping > amount -> branch PAYS (shipping - amount) to courier.

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

    # Fetch unsettled CTs
    cts = frappe.get_all(
        "Courier Transaction",
        filters={
            "reference_invoice": invoice_name,
            "status": ["!=", "Settled"],
            "party_type": party_type if party_type else ["in", [party_type, None, ""]],
            "party": party if party else ["in", [party, None, ""]],
        },
        fields=["name", "amount", "shipping_amount"],
    )

    order_amount = 0.0
    # pick first with amount>0
    for r in cts:
        amt = float(r.get("amount") or 0)
        if amt > 0:
            order_amount = amt
            break

    if order_amount <= 0:  # shipping only
        scenario = "shipping_only"
        branch_action = "pay"
        courier_amount = shipping
        msg = f"Pay courier shipping expense {shipping:.2f}" if shipping > 0 else "No shipping expense to pay"
    else:
        if order_amount >= shipping:
            scenario = "collect"
            branch_action = "collect"
            courier_amount = order_amount - shipping
            msg = f"Collect {courier_amount:.2f} from courier (order {order_amount:.2f} - shipping {shipping:.2f})"
        else:
            scenario = "pay_excess"
            branch_action = "pay"
            courier_amount = shipping - order_amount
            msg = f"Pay courier {courier_amount:.2f} (shipping {shipping:.2f} - order {order_amount:.2f})"

    return {
        "invoice": inv.name,
        "party_type": party_type,
        "party": party,
        "order_amount": order_amount,
        "shipping_amount": shipping,
        "scenario": scenario,
        "branch_action": branch_action,
        "courier_amount": courier_amount,
        "message": msg,
    }

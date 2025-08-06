"""Jarz POS – Invoice-related API endpoints.

Clean implementation using Frappe best practices.
Only handles cart items - never treats shipping as an item.
"""

from __future__ import annotations
import frappe
import json

# Legacy implementation lives in the custom POS page controller
from jarz_pos.jarz_pos.page.custom_pos import custom_pos as _legacy


# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------


@frappe.whitelist()
def create_pos_invoice(cart_json, customer_name, pos_profile_name=None, delivery_charges_json=None, required_delivery_datetime=None):
    """
    Create POS Sales Invoice - Clean implementation with comprehensive debugging
    
    Args:
        cart_json (str): JSON string containing cart items
        customer_name (str): Name of the customer
        pos_profile_name (str, optional): POS Profile to use
        delivery_charges_json (str, optional): JSON string containing delivery charges
        required_delivery_datetime (str, optional): ISO datetime string for delivery slot
    
    Returns:
        dict: Invoice creation result
    """
    
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
        
        # Call the legacy function with proper error handling
        result = _legacy.create_pos_invoice(cart_json, customer_name, pos_profile_name, delivery_charges_json, required_delivery_datetime)
        
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


@frappe.whitelist()
def pay_invoice(invoice_name: str, payment_mode: str, pos_profile: str | None = None):
    """Register payment against a Sales Invoice."""
    return _legacy.pay_invoice(invoice_name, payment_mode, pos_profile)

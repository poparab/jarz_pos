import json
import frappe
import traceback

# Debug: File loaded timestamp
print(f"🔄 custom_pos.py loaded at {frappe.utils.now()}")

try:
    from erpnext.stock.stock_ledger import NegativeStockError
except ImportError:
    class NegativeStockError(Exception):
        pass


@frappe.whitelist()
def get_context(context):
    """Page context for custom POS"""
    context.title = "Jarz POS"
    return context


@frappe.whitelist()
def create_pos_invoice(cart_json, customer_name, pos_profile_name=None):
    """
    Create POS Sales Invoice using Frappe best practices
    - Uses only Frappe APIs (no direct database access)
    - Handles only cart items (no shipping)
    - Clean, simple implementation
    """
    try:
        frappe.logger().info("🚀 POS Invoice Creation Started")
        
        # Parse and validate input
        cart_items = frappe.parse_json(cart_json) if isinstance(cart_json, str) else cart_json
        
        if not cart_items:
            frappe.throw("Cart is empty")
        
        if not customer_name:
            frappe.throw("Customer name is required")
        
        frappe.logger().info(f"Creating invoice for customer: {customer_name}")
        frappe.logger().info(f"Cart has {len(cart_items)} items")
        
        # Ensure customer exists
        if not frappe.db.exists("Customer", customer_name):
            frappe.throw(f"Customer '{customer_name}' not found")
        
        # Get POS Profile (use default if not provided)
        if pos_profile_name:
            if not frappe.db.exists("POS Profile", pos_profile_name):
                frappe.throw(f"POS Profile '{pos_profile_name}' not found")
            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
        else:
            # Get default POS Profile
            pos_profile_name = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
            if not pos_profile_name:
                frappe.throw("No active POS Profile found")
            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
        
        frappe.logger().info(f"Using POS Profile: {pos_profile.name}")
        
        # Create Sales Invoice document
        invoice_doc = frappe.new_doc("Sales Invoice")
        
        # Set basic fields
        invoice_doc.customer = customer_name
        invoice_doc.pos_profile = pos_profile.name
        invoice_doc.is_pos = 1
        invoice_doc.company = pos_profile.company
        invoice_doc.selling_price_list = pos_profile.selling_price_list
        invoice_doc.currency = pos_profile.currency
        
        # Add cart items
        for item_data in cart_items:
            item_code = item_data.get("item_code")
            qty = item_data.get("qty", 1)
            rate = item_data.get("rate") or item_data.get("price", 0)
            
            if not item_code:
                continue
                
            # Validate item exists
            if not frappe.db.exists("Item", item_code):
                frappe.throw(f"Item '{item_code}' not found")
            
            # Add item to invoice
            invoice_doc.append("items", {
                "item_code": item_code,
                "qty": qty,
                "rate": rate
            })
        
        if not invoice_doc.items:
            frappe.throw("No valid items found in cart")
        
        frappe.logger().info(f"Added {len(invoice_doc.items)} items to invoice")
        
        # Save and submit using Frappe workflow
        invoice_doc.insert()
        invoice_doc.submit()
        
        frappe.logger().info(f"✅ Invoice created successfully: {invoice_doc.name}")
        
        # Return clean response
        return {
            "success": True,
            "invoice_name": invoice_doc.name,
            "grand_total": invoice_doc.grand_total,
            "customer": invoice_doc.customer,
            "items_count": len(invoice_doc.items)
        }
        
    except Exception as e:
        frappe.logger().error(f"❌ Error creating POS invoice: {str(e)}")
        frappe.throw(f"Failed to create invoice: {str(e)}")


@frappe.whitelist()
def create_sales_invoice(cart_json, customer_name, pos_profile_name, delivery_charges_json=None, required_delivery_datetime=None):
    """
    Create Sales Invoice using the definitive, correct ERPNext workflow
    """
    try:
        # 🔍 DEBUG: Log all incoming parameters
        print("\n" + "="*80)
        print("🚀 BUNDLE PRICING DEBUG - create_sales_invoice() CALLED")
        print("="*80)
        print(f"📍 File: {__file__}")
        print(f"📍 Function: create_sales_invoice")
        print(f"📍 Timestamp: {frappe.utils.now()}")
        print(f"📋 Parameters received:")
        print(f"   - cart_json: {cart_json}")
        print(f"   - customer_name: {customer_name}")
        print(f"   - pos_profile_name: {pos_profile_name}")
        print(f"   - delivery_charges_json: {delivery_charges_json}")
        
        cart = json.loads(cart_json)
        delivery_charges = json.loads(delivery_charges_json) if delivery_charges_json else {}

        print(f"\n🛒 Parsed cart data:")
        for i, item in enumerate(cart):
            print(f"   Item {i+1}: {json.dumps(item, indent=4)}")

        # Get POS profile and validate
        if pos_profile_name and frappe.db.exists("POS Profile", pos_profile_name):
            pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
        else:
            # Fallback: fetch first enabled POS Profile for the current user/company
            fallback_name = frappe.db.get_value(
                "POS Profile",
                {
                    "disabled": 0,
                    "company": ["!=", ""],  # ensure company is set
                },
                "name",
            )
            if not fallback_name:
                frappe.throw("No valid POS Profile found. Please create or enable one.")
            print(f"⚠️  Falling back to POS Profile: {fallback_name}")
            pos_profile_name = fallback_name
            pos_profile = frappe.get_doc("POS Profile", fallback_name)

        company = pos_profile.company
        selling_price_list = pos_profile.selling_price_list

        print(f"\n⚙️ POS Profile settings:")
        print(f"   - Company: {company}")
        print(f"   - Selling Price List: {selling_price_list}")
        print(f"   - Currency: {pos_profile.currency}")

        if not company:
            frappe.throw("Company not found in POS Profile")

        # Create new Sales Invoice document
        si = frappe.new_doc("Sales Invoice")

        # Set basic fields
        # Ensure customer exists or create a quick Walk-In customer record
        if not frappe.db.exists("Customer", customer_name):
            print(f"⚠️  Customer '{customer_name}' not found. Creating a temporary customer record…")
            
            # Get default customer group and territory
            default_customer_group = frappe.db.get_single_value("Selling Settings", "customer_group") or "All Customer Groups"
            default_territory = frappe.db.get_single_value("Selling Settings", "territory") or "All Territories"
            
            # Ensure the customer group exists
            if not frappe.db.exists("Customer Group", default_customer_group):
                default_customer_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
            
            # Ensure the territory exists  
            if not frappe.db.exists("Territory", default_territory):
                default_territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
            
            print(f"   - Using Customer Group: {default_customer_group}")
            print(f"   - Using Territory: {default_territory}")
            
            cust = frappe.new_doc("Customer")
            cust.customer_name = customer_name
            cust.customer_group = default_customer_group
            cust.territory = default_territory
            cust.customer_type = "Individual"
            
            # Set default price list if available
            if selling_price_list:
                cust.default_price_list = selling_price_list
            
            cust.insert(ignore_permissions=True)
            print(f"   ✅ Customer created: {cust.name}")
            
            # Commit the transaction to ensure customer is available
            frappe.db.commit()
        # Verify customer exists before proceeding
        customer_doc = frappe.get_doc("Customer", customer_name)
        print(f"   ✅ Customer verified: {customer_doc.name}")
        print(f"      - Customer Group: {customer_doc.customer_group}")
        print(f"      - Territory: {customer_doc.territory}")
        print(f"      - Customer Type: {customer_doc.customer_type}")
        
        si.customer = customer_name
        si.pos_profile = pos_profile_name
        si.is_pos = 1
        si.company = company
        si.selling_price_list = selling_price_list
        si.currency = pos_profile.currency or frappe.get_cached_value("Company", company, "default_currency")

        # VITAL: This flag tells ERPNext to not apply its own pricing rules
        # and to accept the rates we provide.
        si.ignore_pricing_rule = 1
        
        # Set required posting fields for proper document creation
        si.posting_date = frappe.utils.nowdate()
        si.posting_time = frappe.utils.nowtime()
        si.set_posting_time = 1

        print(f"\n📄 Sales Invoice created:")
        print(f"   - Customer: {si.customer}")
        print(f"   - Company: {si.company}")
        print(f"   - Currency: {si.currency}")
        print(f"   - ignore_pricing_rule: {si.ignore_pricing_rule}")
        print(f"   - posting_date: {si.posting_date}")
        print(f"   - posting_time: {si.posting_time}")

        # Process cart items
        bundle_count = 0
        regular_count = 0
        
        for item in cart:
            if item.get("is_bundle"):
                bundle_count += 1
                print(f"\n🎁 Processing BUNDLE item #{bundle_count}...")
                process_bundle_item(si, item, selling_price_list)
            else:
                regular_count += 1
                print(f"\n📦 Processing REGULAR item #{regular_count}...")
                process_regular_item(si, item)

        print(f"\n📊 Cart processing summary:")
        print(f"   - Bundle items: {bundle_count}")
        print(f"   - Regular items: {regular_count}")
        print(f"   - Total invoice items: {len(si.items)}")

        # Add delivery charges if any
        if delivery_charges:
            print(f"\n🚚 Adding delivery charges: {delivery_charges}")
            add_delivery_charges(si, delivery_charges, company)

        # Store requested delivery datetime (convert from ISO string)
        if required_delivery_datetime:
            try:
                from frappe.utils import get_datetime
                _dt = get_datetime(required_delivery_datetime)
                if getattr(_dt, 'tzinfo', None):
                    _dt = _dt.replace(tzinfo=None)
                si.required_delivery_datetime = _dt
                print(f"   🕒 Parsed delivery datetime (naive) set: {si.required_delivery_datetime}")
            except Exception as e:
                print(f"   ⚠️  Unable to parse or set delivery datetime: {str(e)}")

        # Log all items before ERPNext processing
        print(f"\n📋 Items added to Sales Invoice BEFORE ERPNext processing:")
        for i, item in enumerate(si.items):
            print(f"   Item {i+1}:")
            print(f"      - item_code: {item.item_code}")
            print(f"      - qty: {item.qty}")
            print(f"      - rate: {item.rate}")
            print(f"      - discount_amount: {getattr(item, 'discount_amount', 0)}")
            print(f"      - amount: {item.amount}")
            print(f"      - price_list_rate: {getattr(item, 'price_list_rate', 'N/A')}")
            print(f"      - ignore_pricing_rule: {getattr(item, 'ignore_pricing_rule', 'N/A')}")
            # Safely print description (handle None)
            _desc_val = getattr(item, 'description', None)
            _desc_snippet = str(_desc_val)[:50] if _desc_val else 'N/A'
            print(f"      - description: {_desc_snippet}...")

        # Set Sales Invoice State to "Received" for all new invoices
        print(f"\n📋 Setting Sales Invoice State to 'Received'...")
        si.sales_invoice_state = "Received"

        # Try to set a custom naming series to avoid locks
        print(f"\n🔧 Setting custom naming series...")
        try:
            # Use a simpler naming approach that's less likely to lock
            si.naming_series = "ACC-SINV-.YYYY.-"
        except Exception as naming_error:
            print(f"   ⚠️  Naming series warning: {naming_error}")

        # Follow ERPNext's standard workflow
        print(f"\n⚡ Running ERPNext standard workflow...")
        print(f"   1. set_missing_values()...")
        si.set_missing_values()
        
        print(f"   2. calculate_taxes_and_totals()...")
        si.calculate_taxes_and_totals()

        # Log all items AFTER ERPNext processing
        print(f"\n📋 Items AFTER ERPNext processing:")
        total_amount = 0
        for i, item in enumerate(si.items):
            print(f"   Item {i+1}:")
            print(f"      - item_code: {item.item_code}")
            print(f"      - qty: {item.qty}")
            print(f"      - rate: {item.rate}")
            print(f"      - discount_amount: {getattr(item, 'discount_amount', 0)}")
            print(f"      - amount: {item.amount}")
            print(f"      - price_list_rate: {getattr(item, 'price_list_rate', 'N/A')}")
            total_amount += item.amount

        print(f"\n💰 Invoice totals:")
        print(f"   - Net Total: {getattr(si, 'net_total', 'N/A')}")
        print(f"   - Grand Total: {getattr(si, 'grand_total', 'N/A')}")
        print(f"   - Calculated Total: {total_amount}")

        # Save first, then submit (ERPNext best practice)
        print(f"\n💾 Saving invoice (before submit)...")
        
        # Add retry logic for database locks
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # Add explicit transaction handling to prevent locks
                frappe.db.begin()
                
                si.insert(ignore_permissions=True)
                print(f"   ✅ Invoice inserted: {si.name}")
                
                # Reload to get the latest state
                si.reload()
                print(f"   🔄 Invoice reloaded after insert")
                
                print(f"\n📤 Submitting invoice...")
                si.submit()
                print(f"   ✅ Invoice submitted: {si.name}")
                
                # Commit the transaction
                frappe.db.commit()
                print(f"   ✅ Transaction committed successfully")
                break  # Success, exit retry loop
                
            except Exception as e:
                frappe.db.rollback()
                retry_count += 1
                error_msg = str(e)
                
                if "Lock wait timeout" in error_msg and retry_count < max_retries:
                    print(f"   ⚠️  Lock timeout (attempt {retry_count}/{max_retries}), retrying in 2 seconds...")
                    import time
                    time.sleep(2)
                    continue
                else:
                    print(f"   ❌ Transaction failed after {retry_count} attempts: {error_msg}")
                    raise

        print(f"\n🎉 SUCCESS! Sales Invoice created successfully!")
        print(f"   - Invoice Number: {si.name}")
        print(f"   - Final Grand Total: {si.grand_total}")
        print("="*80)

        # Clear caches to ensure UI refreshes properly
        frappe.clear_cache(doctype="Sales Invoice")
        
        print(f"🔄 Cleared caches for: {si.name}")

        # Reload one final time to ensure we have the committed state
        si.reload()
        return si.as_dict()

    except Exception as e:
        print(f"\n❌ ERROR in create_sales_invoice:")
        print(f"   - Error: {str(e)}")
        print(f"   - Type: {type(e).__name__}")
        import traceback
        print(f"   - Traceback: {traceback.format_exc()}")
        print("="*80)
        
        frappe.log_error(f"Error creating sales invoice: {str(e)}")
        frappe.throw(f"Error creating sales invoice: {str(e)}")


def process_bundle_item(si, bundle, selling_price_list):
    """
    Process bundle item using ERPNext-compliant discount_amount approach.
    
    Strategy:
    1. Add parent bundle item discounted to ZERO (rate shown but 100% discount applied).
    2. Calculate a UNIFORM discount percentage for all child items so that their combined
       amount equals the desired bundle price (i.e., same % discount applied to each child).
    3. This ensures proper stock tracking, accounting, and item profitability analysis
    """
    print(f"\n🎁 BUNDLE PROCESSING DEBUG")
    print(f"   📍 Function: process_bundle_item")
    print(f"   📋 Bundle data: {json.dumps(bundle, indent=4)}")
    
    bundle_items = bundle.get("items", [])
    parent_item_code = bundle.get("item_code")
    bundle_price = bundle.get("price", 0)  # Desired final bundle price
    bundle_name = bundle.get("bundle_name", "Bundle")

    print(f"   🔍 Extracted values:")
    print(f"      - parent_item_code: {parent_item_code}")
    print(f"      - bundle_price: {bundle_price}")
    print(f"      - bundle_name: {bundle_name}")
    print(f"      - child_items_count: {len(bundle_items)}")

    if not parent_item_code:
        frappe.throw(f"Bundle '{bundle_name}' is not linked to a parent ERPNext item")

    # Get parent item's original price
    parent_original_price = get_item_price(parent_item_code, selling_price_list)
    print(f"   💰 Parent item price: {parent_original_price}")
    
    if not parent_original_price:
        frappe.throw(f"No price found for parent bundle item '{parent_item_code}' in price list '{selling_price_list}'")

    # Calculate total original price of all child items
    child_items_total = 0
    child_items_data = []
    
    print(f"   🔍 Processing child items:")
    for i, sub_item in enumerate(bundle_items):
        item_code = sub_item.get("item_code")
        if not item_code:
            continue
            
        qty = sub_item.get("qty", 1)
        original_price = get_item_price(item_code, selling_price_list)
        
        print(f"      Child {i+1}: {item_code}")
        print(f"         - qty: {qty}")
        print(f"         - original_price: {original_price}")
        
        if not original_price:
            frappe.msgprint(f"Warning: No price found for child item '{item_code}' in price list '{selling_price_list}'. Using rate 0.")
            original_price = 0
            
        item_total = original_price * qty
        child_items_total += item_total
        
        print(f"         - item_total: {item_total}")
        
        child_items_data.append({
            "item_code": item_code,
            "qty": qty,
            "rate": original_price,
            "amount": item_total,
            "original_price": original_price
        })

    # Calculate total original bundle value (parent + children)
    print(f"\n   📊 Bundle calculations:")
    print(f"      - parent_original_price: {parent_original_price}")
    print(f"      - child_items_total: {child_items_total}")
    print(f"      - bundle_price: {bundle_price}")

    # ──────────────────────────────────────────────────────────────────
    # 1️⃣  Add PARENT item at ZERO amount (100% discount)
    # ──────────────────────────────────────────────────────────────────
    # Use discount_percentage to let ERPNext honour it
    parent_discount_percentage = 100  # full discount

    print(f"\n   👑 Adding parent item with 100% discount (final amount = 0):")
    print(f"      - rate: {parent_original_price}")
    print(f"      - discount_amount: {parent_original_price}") # This will be recalculated by ERPNext

    si.append("items", {
        "item_code": parent_item_code,
        "qty": 1,
        "rate": parent_original_price,
        "amount": 0,  # Will be 0 due to 100% discount
        "price_list_rate": parent_original_price,
        "discount_percentage": parent_discount_percentage,
        "ignore_pricing_rule": 1,
        "description": f"Bundle: {bundle_name} (parent item, discounted to 0)"
    })

    # ──────────────────────────────────────────────────────────────────
    # 2️⃣  Compute uniform DISCOUNTED RATE for child items so that
    #     their combined amount equals the bundle price.
    # ──────────────────────────────────────────────────────────────────
    child_discount_needed = max(child_items_total - bundle_price, 0)
    if child_items_total > 0:
        child_discount_percentage = child_discount_needed / child_items_total
    else:
        child_discount_percentage = 0

    print(f"\n   👶 Child discount calculations:")
    print(f"      - child_items_total: {child_items_total}")
    print(f"      - child_discount_needed: {child_discount_needed}")
    print(f"      - child_discount_percentage: {round(child_discount_percentage*100,4)}% (for info)")

    # Add child items with SAME discount rate calculated above
    children_final_total = 0
    for i, item_data in enumerate(child_items_data):
        final_rate = item_data["rate"] * (1 - child_discount_percentage)
        final_amount = final_rate * item_data["qty"]
        children_final_total += final_amount
        
        print(f"      Child {i+1}: {item_data['item_code']}")
        print(f"         - qty: {item_data['qty']}")
        print(f"         - original_rate: {item_data['rate']}")
        print(f"         - final_rate: {final_rate}")
        print(f"         - final_amount: {final_amount}")
        
        si.append("items", {
            "item_code": item_data["item_code"],
            "qty": item_data["qty"],
            "rate": final_rate,
            "amount": final_amount,
            "price_list_rate": item_data["original_price"],
            "discount_amount": (item_data["rate"] * item_data["qty"]) - final_amount,
            "ignore_pricing_rule": 1,
            "description": f"Part of bundle: {bundle_name} (discounted)"
        })

    # ──────────────────────────────────────────────────────────────────
    # 3️⃣  Verification & Debug
    # ──────────────────────────────────────────────────────────────────
    total_after_discounts = children_final_total  # parent amount is 0
    print(f"\n   ✅ Bundle verification:")
    print(f"      - children_final_total: {children_final_total}")
    print(f"      - expected_bundle_price: {bundle_price}")
    print(f"      - difference: {abs(children_final_total - bundle_price)}")


def process_regular_item(si, item):
    """
    Process a regular (non-bundle) item
    """
    print(f"\n📦 REGULAR ITEM PROCESSING:")
    print(f"   📋 Item data: {json.dumps(item, indent=4)}")
    
    qty = item.get("qty", 1)
    rate = item.get("price", 0)
    item_code = item.get("item_code")

    print(f"   🔍 Extracted values:")
    print(f"      - item_code: {item_code}")
    print(f"      - qty: {qty}")
    print(f"      - rate: {rate}")
    print(f"      - amount: {rate * qty}")

    si.append("items", {
        "item_code": item_code,
        "qty": qty,
        "rate": rate,
        "price_list_rate": rate,
        "amount": rate * qty,
        "ignore_pricing_rule": 1  # Ensure ERPNext respects the provided rate
    })


def add_delivery_charges(si, delivery_charges, company):
    """
    Add delivery charges as taxes
    """
    print(f"\n🚚 DELIVERY CHARGES PROCESSING:")
    print(f"   📋 Delivery data: {json.dumps(delivery_charges, indent=4)}")
    
    income = delivery_charges.get("income", 0)
    expense = delivery_charges.get("expense", 0)
    city = delivery_charges.get("city", "N/A")

    print(f"   🔍 Extracted values:")
    print(f"      - income: {income}")
    print(f"      - expense: {expense}")
    print(f"      - city: {city}")

    # Get freight account once (needed for both income & expense rows)
    freight_account = None
    if income > 0 or expense > 0:
        freight_account = get_account_for_company("Freight and Forwarding Charges", company)
        print(f"      - freight_account: {freight_account}")

    # 1️⃣  Delivery income (positive add-on)
    if income > 0 and freight_account:
        si.append("taxes", {
            "charge_type": "Actual",
            "account_head": freight_account,
            "description": f"Delivery to {city}",
            "tax_amount": income,
            "add_deduct_tax": "Add"
        })

    # 2️⃣  Delivery expense
    #
    #    As per latest requirement (July 2025) we no longer reflect the
    #    *expense* side of delivery charges on the Sales Invoice.  The
    #    courier cost will be captured through separate purchase/expense
    #    workflows instead.  Therefore **NO negative tax row or discount**
    #    is added here anymore – only the customer-facing delivery income
    #    (if any) is recorded above.
    if expense > 0:
        print("      ⚠️  Delivery expense provided but ignored on Sales Invoice per 2025-07 policy")


def get_account_for_company(account_name, company):
    """
    Get account for company with fallback options
    """
    print(f"   🔍 get_account_for_company({account_name}, {company})")
    
    # Try exact match first
    account = frappe.db.get_value("Account", {
        "account_name": account_name,
        "company": company,
        "is_group": 0
    }, "name")

    if account:
        print(f"      ✅ Exact match found: {account}")
        return account

    # Try partial match
    account = frappe.db.get_value("Account", {
        "account_name": ["like", f"{account_name}%"],
        "company": company,
        "is_group": 0
    }, "name")

    if account:
        print(f"      ⚠️ Partial match found: {account}")
        return account

    # Fallback to a generic expense account
    fallback_account = frappe.db.get_value("Account", {
        "account_name": ["like", "%Expense%"],
        "company": company,
        "is_group": 0
    }, "name")

    if fallback_account:
        print(f"      ⚠️ Fallback account found: {fallback_account}")
        return fallback_account

    print(f"      ❌ No account found!")
    frappe.throw(f"No suitable account found for delivery charges in company {company}")


def get_item_price(item_code, price_list):
    """
    Get item price from Item Price doctype or Item.standard_rate as fallback
    """
    print(f"      🔍 get_item_price({item_code}, {price_list})")
    
    # Try Item Price first
    price = frappe.db.get_value("Item Price", {
        "item_code": item_code,
        "price_list": price_list
    }, "price_list_rate")
    
    if price:
        print(f"         ✅ Found in Item Price: {price}")
        return price
    
    # Fallback to Item.standard_rate
    standard_rate = frappe.db.get_value("Item", item_code, "standard_rate")
    if standard_rate:
        print(f"         ⚠️ Fallback to standard_rate: {standard_rate}")
    else:
        print(f"         ❌ No price found anywhere!")
    
    return standard_rate if standard_rate else 0


# ────────────────────────────────────────────────────────────────────────────────
# 💳  ONLINE PAYMENT ENDPOINT
#     Handles single-shot payments (Instapay, Payment Gateway, Mobile Wallet)
#     Triggered from Kanban "Mark Paid" button.
# ────────────────────────────────────────────────────────────────────────────────


@frappe.whitelist()
def pay_invoice(invoice_name: str, payment_mode: str, pos_profile: str | None = None):
	"""Mark a *submitted* Sales Invoice as fully paid using the selected online
	payment mode. Creates and submits **one** Payment Entry that allocates the
	full outstanding amount to the invoice.

	Args:
	    invoice_name: The Sales Invoice ID (e.g. ``ACC-SINV-2025-00078``)
	    payment_mode: One of ``Instapay``, ``Payment Gateway``, ``Mobile Wallet``
	"""
	# Fetch and validate Invoice
	inv = frappe.get_doc("Sales Invoice", invoice_name)
	if inv.docstatus != 1:
	    frappe.throw("Invoice must be submitted before payment can be recorded.")
	if inv.outstanding_amount <= 0:
	    frappe.throw("Invoice already paid.")

	company = inv.company
	outstanding = inv.outstanding_amount

	pm_clean = payment_mode.strip().lower()
	# Resolve ledger for the chosen payment mode
	if pm_clean.startswith("cash"):
	    # If front-end didn’t supply POS profile, derive from invoice itself
	    if not pos_profile:
	        pos_profile = inv.get("pos_profile")
	    if not pos_profile:
	        frappe.throw("POS Profile name required for cash payments (could not infer from invoice).")
	    paid_to_account = _get_cash_account(pos_profile, company)
	else:
	    paid_to_account = _get_paid_to_account(payment_mode, company)

	# Resolve receivable (paid_from) account
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
	    frappe.throw("No receivable account found for company {0}".format(company))

	# Build Payment Entry
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.mode_of_payment = payment_mode
	pe.company = company
	pe.party_type = "Customer"
	pe.party = inv.customer
	pe.paid_from = paid_from_account
	pe.paid_to = paid_to_account
	pe.paid_amount = outstanding  # may be incremented below if deduction row added
	pe.received_amount = outstanding

	# ------------------------------------------------------------------
	# 🚚  Delivery expense deduction when paying courier in CASH
	# ------------------------------------------------------------------
	delivery_expense = 0
	if pm_clean.startswith("cash"):
	    delivery_expense = _get_delivery_expense_amount(inv)
	    print(f"💰 Delivery expense detected: {delivery_expense}")
	    if delivery_expense and delivery_expense > 0:
	        # Increase paid / received so that after deduction net equals outstanding
	        pe.paid_amount = outstanding + delivery_expense
	        pe.received_amount = outstanding + delivery_expense

	        expense_account = get_account_for_company("Freight and Forwarding Charges", company)
	        default_cc = frappe.db.get_value("Company", company, "cost_center")
	        pe.append(
	            "taxes",
	            {
	                "charge_type": "Actual",
	                "account_head": expense_account,
	                "cost_center": default_cc,
	                "add_deduct_tax": "Deduct",
	                "tax_amount": delivery_expense,
	                "description": f"Delivery Expense – {inv.name}",
	            },
	        )
	        print(
	            f"📌 Added tax deduction row (-{delivery_expense}) to account {expense_account} (cost center: {default_cc})"
	        )

	# Debug: show Payment Entry draft values before save
	print("\n📝 PAYMENT ENTRY DRAFT VALUES:")
	print(f"   Paid Amount: {pe.paid_amount}")
	print(f"   Received Amount: {pe.received_amount}")
	print(f"   Total Taxes rows: {len(pe.get('taxes') or [])}")
	for tx in pe.get("taxes", []) or []:
	    print(
	        f"   • {tx.account_head} | {tx.add_deduct_tax} | {tx.tax_amount} | Included: {tx.included_in_paid_amount}"
	    )

	# ------------------------------------------------------------------
	# Bank validation fields – Payment Entry requires Reference No & Date
	# for bank-type transactions. If the user hasn't provided them,
	# populate sensible placeholders so validation passes.
	# ------------------------------------------------------------------

	if not pe.get("reference_no"):
	    # e.g. POS-INSTAPAY-20250713-183623
	    timestamp = frappe.utils.now_datetime().strftime("%Y%m%d-%H%M%S")
	    pe.reference_no = f"POS-{payment_mode.upper().replace(' ', '')}-{timestamp}"

	if not pe.get("reference_date"):
	    pe.reference_date = frappe.utils.nowdate()

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

	# Stick to invoice currency
	pe.source_exchange_rate = 1
	pe.target_exchange_rate = 1

	# Validation chain inside save() handles remaining missing values automatically
	pe.save(ignore_permissions=True)
	pe.submit()

	# Debug: show Payment Entry final values after submit
	print("\n✅ PAYMENT ENTRY SUBMITTED:")
	print(f"   Name: {pe.name}")
	print(f"   Paid Amount: {pe.paid_amount} | Paid After Tax: {pe.paid_amount_after_tax}")
	print(f"   Received Amount: {pe.received_amount} | Received After Tax: {pe.received_amount_after_tax}")
	print(f"   Total Taxes & Charges: {pe.total_taxes_and_charges or 0}")
	for tx in pe.taxes:
	    print(
	        f"   • {tx.account_head} | {tx.add_deduct_tax} | {tx.tax_amount} | Included: {tx.included_in_paid_amount}"
	    )
	print("   ------------------------------------------------------------")

	# Realtime update – let Kanban board refresh card colour/state
	frappe.publish_realtime(
	    "jarz_pos_invoice_paid",
	    {"invoice": inv.name, "payment_entry": pe.name},
	)

	return {"payment_entry": pe.name}


def _get_paid_to_account(payment_mode: str, company: str) -> str:
	"""Return ledger to credit based on payment mode."""
	payment_mode = payment_mode.strip().lower()

	if payment_mode in {"instapay", "payment gateway"}:
	    # Try to find a leaf account under Bank Accounts
	    account = frappe.db.get_value(
	        "Account",
	        {
	            "company": company,
	            "parent_account": ["like", "%Bank Accounts%"],
	            "is_group": 0,
	        },
	        "name",
	    )
	    if account:
	        return account

	if payment_mode == "mobile wallet":
	    account = frappe.db.get_value(
	        "Account",
	        {
	            "company": company,
	            "account_name": ["like", "Mobile Wallet%"],
	            "is_group": 0,
	        },
	        "name",
	    )
	    if account:
	        return account

	frappe.throw(
	    f"No ledger found for payment mode '{payment_mode}' in company {company}.\n"
	    "Please create the appropriate account under Bank Accounts."
	)


# ---------------------------------------------------------------------------
# Helper: cash account for POS profile
# ---------------------------------------------------------------------------


def _get_cash_account(pos_profile: str, company: str) -> str:
    """Return Cash In Hand ledger for the given POS profile."""
    # Try exact child under Cash In Hand first: "{pos_profile} - <ABBR>"
    acc = frappe.db.get_value(
        "Account",
        {
            "company": company,
            "parent_account": ["like", "%Cash In Hand%"],
            "account_name": pos_profile,
            "is_group": 0,
        },
        "name",
    )

    # Fallback: partial match
    if not acc:
        acc = frappe.db.get_value(
            "Account",
            {
                "company": company,
                "parent_account": ["like", "%Cash In Hand%"],
                "account_name": ["like", f"%{pos_profile}%"],
                "is_group": 0,
            },
            "name",
        )

    if acc:
        return acc

    frappe.throw(
        f"No Cash In Hand account found for POS profile '{pos_profile}' in company {company}."
    )


# ---------------------------------------------------------------------------
# Helper: courier outstanding account
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 📦  COURIER OUTSTANDING ENDPOINT
#     Records courier pick-up, creates Payment Entry against Courier Outstanding
#     and logs a Courier Transaction document.
# ---------------------------------------------------------------------------


@frappe.whitelist()
def mark_courier_outstanding(invoice_name: str, courier: str):
    """Allocate the outstanding amount of a submitted Sales Invoice to the
    company's *Courier Outstanding* account and create a *Courier Transaction*
    log entry.

    Args:
        invoice_name: Sales Invoice ID that courier will collect payment for.
        courier: Selected courier (link to *Courier* doctype).
    """

    # ------------------------------------------------------------------
    # 1) Validate input & fetch documents
    # ------------------------------------------------------------------
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted before marking as courier outstanding.")
    if inv.outstanding_amount <= 0:
        frappe.throw("Invoice already paid – no outstanding amount to allocate.")

    company = inv.company
    outstanding = inv.outstanding_amount

    # ------------------------------------------------------------------
    # 2) Resolve ledger accounts
    # ------------------------------------------------------------------
    paid_to_account = _get_courier_outstanding_account(company)

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

    # ------------------------------------------------------------------
    # 3) Build & submit **Payment Entry** – marks invoice paid while
    #    parking the receivable in *Courier Outstanding*.
    # ------------------------------------------------------------------

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = company
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

    # ------------------------------------------------------------------
    # 4) Record SHIPPING EXPENSE via separate Journal Entry
    # ------------------------------------------------------------------
    shipping_exp = _get_delivery_expense_amount(inv)
    je_name = None
    if shipping_exp and shipping_exp > 0:
        freight_acc = get_account_for_company("Freight and Forwarding Charges", company)

        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = company
        je.title = f"Courier Expense – {inv.name}"

        # Debit Freight Expense
        je.append(
            "accounts",
            {
                "account": freight_acc,
                "debit_in_account_currency": shipping_exp,
                "credit_in_account_currency": 0,
            },
        )

        # Credit Courier Outstanding (reduces receivable)
        je.append(
            "accounts",
            {
                "account": paid_to_account,
                "debit_in_account_currency": 0,
                "credit_in_account_currency": shipping_exp,
            },
        )

        je.save(ignore_permissions=True)
        je.submit()
        je_name = je.name

    # ------------------------------------------------------------------
    # 5) Create Courier Transaction log
    # ------------------------------------------------------------------
    ct = frappe.new_doc("Courier Transaction")
    ct.courier = courier
    ct.date = frappe.utils.now_datetime()
    ct.type = "Pick-Up"
    ct.reference_invoice = inv.name
    ct.amount = outstanding
    ct.shipping_amount = shipping_exp or 0
    ct.insert(ignore_permissions=True)

    # ------------------------------------------------------------------
    # 6) Notify front-end & return (custom event)
    # ------------------------------------------------------------------
    frappe.publish_realtime(
        "jarz_pos_courier_outstanding",
        {
            "invoice": inv.name,
            "payment_entry": pe.name,
            "journal_entry": je_name,
            "courier_transaction": ct.name,
            "shipping_amount": shipping_exp or 0,
        },
    )

    return {
        "payment_entry": pe.name,
        "journal_entry": je_name,
        "courier_transaction": ct.name,
        "shipping_amount": shipping_exp or 0,
    }


def _get_delivery_expense_amount(inv):
    """Return delivery expense amount (float) for the given invoice using its city.

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

    try:
        expense = frappe.db.get_value("City", city_id, "delivery_expense")  # type: ignore[attr-defined]
        return float(expense or 0)
    except Exception:
        return 0.0


@frappe.whitelist()
def pay_delivery_expense(invoice_name: str, pos_profile: str):
    """Create (or return existing) Journal Entry for paying the courier’s delivery
    expense in cash and, **atomically**, set the invoice operational state to
    “Out for delivery”.  This makes the endpoint idempotent – repeated calls for
    the same invoice will NOT generate duplicate Journal Entries."""

    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")

    company = inv.company

    # Ensure the invoice is marked Out for delivery before proceeding.
    if inv.get("sales_invoice_state") != "Out for delivery":
        inv.db_set("sales_invoice_state", "Out for delivery", update_modified=False)

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
    paid_from = _get_cash_account(pos_profile, company)
    paid_to = get_account_for_company("Freight and Forwarding Charges", company)

    # Build Journal Entry (credit cash-in-hand, debit expense)
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.posting_date = frappe.utils.nowdate()
    je.company = company
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

    # Fire realtime event so other sessions update cards instantly
    frappe.publish_realtime(
        "jarz_pos_courier_expense_paid",
        {"invoice": inv.name, "journal_entry": je.name, "amount": amount},
    )

    return {"journal_entry": je.name, "amount": amount}


@frappe.whitelist()
def courier_delivery_expense_only(invoice_name: str, courier: str):
    """Record courier delivery expense to be settled later.

    Creates a **Courier Transaction** of type *Pick-Up* with **negative** amount
    and note *delivery expense only* so that the courier’s outstanding balance is
    reduced by the delivery fee they will collect from us.
    """
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted.")

    # Ensure state is Out for delivery (idempotent)
    if inv.get("sales_invoice_state") != "Out for delivery":
        inv.db_set("sales_invoice_state", "Out for delivery", update_modified=False)

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
    ct.courier = courier
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


# ---------------------------------------------------------------------------
# 📊  COURIER BALANCES ENDPOINT
#     Returns per-courier outstanding balance and detailed invoice list for UI
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_courier_balances():
    """Return list of couriers with their current balance (= Σ amounts – Σ shipping)
    together with per-invoice breakdown for popup view.

    Output structure::
        [
            {
                "courier": "COURIER-0001",
                "courier_name": "FastEx",
                "balance": 1250.0,
                "details": [
                    {"invoice": "ACC-SINV-0001", "city": "Downtown", "amount": 250.0, "shipping": 10.0},
                    ...
                ]
            },
            ...
        ]
    """

    data = []
    couriers = frappe.get_all("Courier", fields=["name", "courier_name"])
    for c in couriers:
        rows = frappe.get_all(
            "Courier Transaction",
            filters={
                "courier": c.name,
                "status": ["!=", "Settled"],  # Exclude settled transactions
            },
            fields=["reference_invoice", "amount", "shipping_amount"]
        )

        total_amount = sum(float(r.amount or 0) for r in rows)
        total_shipping = sum(float(r.shipping_amount or 0) for r in rows)
        balance = total_amount - total_shipping

        details = []
        for r in rows:
            city = ""
            if r.reference_invoice:
                # Fetch shipping or customer address linked to the invoice
                si_addr = frappe.db.get_value(
                    "Sales Invoice",
                    r.reference_invoice,
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
                        city = city_name or city_id or ""
            details.append({
                "invoice": r.reference_invoice,
                "city": city,
                "amount": float(r.amount or 0),
                "shipping": float(r.shipping_amount or 0)
            })

        data.append({
            "courier": c.name,
            "courier_name": c.courier_name or c.name,
            "balance": balance,
            "details": details,
        })

    # Sort by balance desc for nicer UI
    data.sort(key=lambda d: d["balance"], reverse=True)
    return data


def get_permission_query_conditions(user):
    """Permission check for accessing the page"""
    return ""


def has_permission(doc, user):
    """Check if user has permission to access the page"""
    return True

# ---------------------------------------------------------------------------
# 💵  COURIER SETTLEMENT ENDPOINT
#     Pays / collects money for ALL *Unsettled* Courier Transactions of a
#     specific courier. Handles both scenarios automatically:
#        1) Unpaid invoice → creates Payment Entry against Courier Outstanding.
#        2) Paid invoice (shipping-only) → creates Journal Entry paying courier.
# ---------------------------------------------------------------------------

@frappe.whitelist()
def settle_courier(courier: str, pos_profile: str | None = None):
    """Settle all *Unsettled* "Courier Transaction" rows for the given courier.

    Args:
        courier: Courier (DocType) ID.
        pos_profile: POS Profile whose cash account should be credited/used.

    Returns:
        dict: Summary of documents created keyed by invoice / CT.
    """
    if not courier:
        frappe.throw("Courier ID required")

    if not pos_profile:
        # Fallback: try to grab first enabled POS profile
        pos_profile = frappe.db.get_value(
            "POS Profile", {"disabled": 0}, "name"
        )
        if not pos_profile:
            frappe.throw("POS Profile is required to resolve Cash account")

    cts = frappe.get_all(
        "Courier Transaction",
        filters={"courier": courier, "status": ["!=", "Settled"]},
        fields=["name", "amount", "shipping_amount"],
    )

    if not cts:
        frappe.throw("No unsettled courier transactions found.")

    # Compute NET balance: (amount - shipping) per row
    net_balance = 0.0
    for r in cts:
        net_balance += float(r.amount or 0) - float(r.shipping_amount or 0)

    # Determine company (assume all CTs share same company via linked invoice or default)
    company = frappe.defaults.get_global_default("company") or frappe.db.get_single_value("Global Defaults", "default_company")

    courier_outstanding_acc = _get_courier_outstanding_account(company)
    cash_acc = _get_cash_account(pos_profile, company)

    je_name = None
    if abs(net_balance) > 0.005:
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
        je_name = je.name

    # Mark all CTs as settled
    for r in cts:
        frappe.db.set_value("Courier Transaction", r.name, "status", "Settled")

    frappe.db.commit()

    # Fire realtime event
    frappe.publish_realtime(
        "jarz_pos_courier_settled",
        {"courier": courier, "journal_entry": je_name, "net_balance": net_balance},
    )

    return {"journal_entry": je_name, "net_balance": net_balance}

# ---------------------------------------------------------------------------
# 💵  SINGLE-INVOICE COURIER SETTLEMENT
# ---------------------------------------------------------------------------

@frappe.whitelist()
def settle_courier_for_invoice(invoice_name: str, pos_profile: str | None = None):
    """Settle courier outstanding for a single invoice.

    Creates a Journal Entry between Cash-in-Hand and Courier Outstanding for the
    net balance of **all** unsettled *Courier Transaction* rows linked to the
    given invoice, then marks those rows as *Settled*.
    """

    if not invoice_name:
        frappe.throw("Invoice ID required")

    cts = frappe.get_all(
        "Courier Transaction",
        filters={"reference_invoice": invoice_name, "status": ["!=", "Settled"]},
        fields=["name", "courier", "amount", "shipping_amount"],
    )

    if not cts:
        frappe.throw("No unsettled courier transactions found for this invoice.")

    courier = cts[0].courier
    company = frappe.db.get_value("Sales Invoice", invoice_name, "company")
    if not company:
        frappe.throw("Could not determine company for invoice")

    if not pos_profile:
        pos_profile = frappe.db.get_value("POS Profile", {"disabled": 0}, "name")
        if not pos_profile:
            frappe.throw("POS Profile is required to resolve Cash account")

    courier_outstanding_acc = _get_courier_outstanding_account(company)
    cash_acc = _get_cash_account(pos_profile, company)

    net_balance = sum(float(r.amount or 0) - float(r.shipping_amount or 0) for r in cts)

    je_name = None
    if abs(net_balance) > 0.005:
        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Journal Entry"
        je.posting_date = frappe.utils.nowdate()
        je.company = company
        je.title = f"Courier Settlement – {invoice_name}"

        if net_balance > 0:
            # Courier owes us – receive cash
            je.append("accounts", {"account": cash_acc, "debit_in_account_currency": net_balance})
            je.append("accounts", {"account": courier_outstanding_acc, "credit_in_account_currency": net_balance})
        else:
            amt = abs(net_balance)
            je.append("accounts", {"account": courier_outstanding_acc, "debit_in_account_currency": amt})
            je.append("accounts", {"account": cash_acc, "credit_in_account_currency": amt})

        je.save(ignore_permissions=True)
        je.submit()
        je_name = je.name

    # Mark CTs settled
    settled_ids = []
    for r in cts:
        frappe.db.set_value("Courier Transaction", r.name, "status", "Settled")
        settled_ids.append(r.name)

    frappe.db.commit()

    frappe.publish_realtime("jarz_pos_courier_settled_single", {"invoice": invoice_name, "journal_entry": je_name, "net_balance": net_balance})

    return {"journal_entry": je_name, "net_balance": net_balance, "courier": courier, "transactions": settled_ids}

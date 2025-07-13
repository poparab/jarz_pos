import json
import frappe

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
            
            cust.save(ignore_permissions=True)
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

        print(f"\n📄 Sales Invoice created:")
        print(f"   - Customer: {si.customer}")
        print(f"   - Company: {si.company}")
        print(f"   - Currency: {si.currency}")
        print(f"   - ignore_pricing_rule: {si.ignore_pricing_rule}")

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

        # Save and submit
        print(f"\n💾 Saving and submitting invoice...")
        si.save(ignore_permissions=True)
        print(f"   ✅ Invoice saved: {si.name}")

        si.submit()
        print(f"   ✅ Invoice submitted: {si.name}")

        print(f"\n🎉 SUCCESS! Sales Invoice created successfully!")
        print(f"   - Invoice Number: {si.name}")
        print(f"   - Final Grand Total: {si.grand_total}")
        print("="*80)

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
            "price_list_rate": item_data["original_price"],
            "discount_amount": item_data["rate"] - final_rate,
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

    # 2️⃣  Delivery expense appears twice:
    #     a) Negative tax row in Sales Taxes and Charges
    #     b) Invoice-level discount on Grand Total
    if expense > 0 and freight_account:
        # a) Negative tax row (will reduce net total)
        si.append("taxes", {
            "charge_type": "Actual",
            "account_head": freight_account,
            "description": f"Delivery Expense - {city}",
            "tax_amount": -expense,  # negative amount reduces total
            "add_deduct_tax": "Add"
        })

        # Previous versions stored delivery expense in the invoice-level
        # `discount_amount` field (negative value). As per July-2025 requirement
        # we must NOT touch `discount_amount`; only a negative tax row will
        # reflect the expense.  This keeps the invoice "Discount" column clean
        # and avoids confusion during accounting and printing.

        # NOTE: We intentionally do **not** set `si.discount_amount` or
        # `apply_discount_on` anymore.
        print("      - Skipping invoice-level discount_amount (requirement v2025-07)")


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
	pe.paid_amount = outstanding
	pe.received_amount = outstanding

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


def get_permission_query_conditions(user):
    """Permission check for accessing the page"""
    return ""


def has_permission(doc, user):
    """Check if user has permission to access the page"""
    return True

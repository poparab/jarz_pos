# customer.py
import frappe
from frappe.utils import flt
from frappe.model.document import Document

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _augment_customer_with_territory(cust_row: dict[str, any]):
    """Populate *delivery_income* and *delivery_expense* on a
    Customer row using the customer's Territory information.

    Safe – all look-ups are wrapped in try/except and will silently continue
    if anything is missing so the caller does not need extra guards.
    """
    try:
        territory = cust_row.get("territory")
        if not territory:
            return  # nothing to augment

        if frappe.db.exists("Territory", territory):
            # Get custom fields from Territory
            territory_doc = frappe.get_doc("Territory", territory)
            
            # Check if custom fields exist (these should be added via fixtures)
            if hasattr(territory_doc, 'delivery_income'):
                cust_row["delivery_income"] = flt(territory_doc.delivery_income)
            else:
                cust_row["delivery_income"] = 0.0
                
            if hasattr(territory_doc, 'delivery_expense'):
                cust_row["delivery_expense"] = flt(territory_doc.delivery_expense)
            else:
                cust_row["delivery_expense"] = 0.0
                
            cust_row["territory_name"] = territory_doc.territory_name
        else:
            # Territory doesn't exist - set defaults
            cust_row["delivery_income"] = 0.0
            cust_row["delivery_expense"] = 0.0
            cust_row["territory_name"] = territory
    except Exception as _err:
        # Swallow – augmentation is best-effort; log for debugging
        frappe.logger().warning(f"Territory augmentation failed for customer {cust_row.get('name')}: {_err}")
        cust_row["delivery_income"] = 0.0
        cust_row["delivery_expense"] = 0.0

@frappe.whitelist()
def get_customers(search=None):
    """Get customers for POS with optional search"""
    try:
        filters = {}
        if search:
            filters["customer_name"] = ["like", f"%{search}%"]
            # Also search by mobile number
            mobile_filter = {"mobile_no": ["like", f"%{search}%"]}
            
        fields = [
            "name",
            "customer_name", 
            "mobile_no",
            "customer_primary_address",
            "customer_primary_contact",
            "territory",
            "customer_group"
        ]
        
        if search:
            # Search by name or mobile
            customers = frappe.get_all(
                "Customer",
                fields=fields,
                or_filters=[
                    {"customer_name": ["like", f"%{search}%"]},
                    {"mobile_no": ["like", f"%{search}%"]}
                ],
                limit=20
            )
        else:
            customers = frappe.get_all(
                "Customer", 
                fields=fields,
                limit=50
            )
            
        # Augment each customer with territory info
        for c in customers:
            _augment_customer_with_territory(c)

        return customers
        
    except Exception as e:
        frappe.log_error(f"Error fetching customers: {str(e)}")
        return []

@frappe.whitelist()
def get_recent_customers(limit=10):
    """Get recently created/modified customers for quick access"""
    try:
        fields = [
            "name",
            "customer_name", 
            "mobile_no",
            "customer_primary_address",
            "customer_primary_contact",
            "territory",
            "customer_group",
            "modified"
        ]
        
        customers = frappe.get_all(
            "Customer",
            fields=fields,
            order_by="modified desc",
            limit=int(limit)
        )
        
        # Augment with territory info
        for c in customers:
            _augment_customer_with_territory(c)
            
        return customers
        
    except Exception as e:
        frappe.log_error(f"Error fetching recent customers: {str(e)}")
        return []

@frappe.whitelist()
def search_customers(name=None, phone=None):
    """Enhanced search for customers by name or phone using direct SQL."""

    if not name and not phone:
        return []

    # Base fields available on the Customer doctype
    fields_to_select = [
        "`name`", "`customer_name`", "`mobile_no`",
        "`customer_primary_address`", "`customer_primary_contact`",
        "`territory`", "`customer_group`"
    ]
    
    # Conditionally add the 'phone' field to avoid errors if it doesn't exist
    if frappe.db.has_column("Customer", "phone"):
        fields_to_select.append("`phone`")

    query = f"SELECT {', '.join(fields_to_select)} FROM `tabCustomer` WHERE "
    
    conditions = []
    params = {}
    
    if name:
        conditions.append("(`customer_name` LIKE %(search_term)s OR `name` LIKE %(search_term)s)")
        params['search_term'] = f"%{name}%"
        frappe.logger().info(f"Searching customers by name: {name}")

    elif phone:
        phone_conditions = ["`mobile_no` LIKE %(search_term)s"]
        if frappe.db.has_column("Customer", "phone"):
            phone_conditions.append("`phone` LIKE %(search_term)s")
        conditions.append(f"({' OR '.join(phone_conditions)})")
        params['search_term'] = f"%{phone}%"
        frappe.logger().info(f"Searching customers by phone: {phone}")

    if not conditions:
        return []

    query += " OR ".join(conditions)
    query += " ORDER BY `customer_name` ASC LIMIT 20"
    
    try:
        customers = frappe.db.sql(query, params, as_dict=1)
        
        for c in customers:
            _augment_customer_with_territory(c)
            
        frappe.logger().info(f"Found {len(customers)} customers via SQL")
        return customers

    except Exception as e:
        frappe.log_error(f"Error in SQL customer search: {e}", frappe.get_traceback())
        return []


@frappe.whitelist()
def get_territories(search=None):
    """Get territories with custom delivery fields for customer creation"""
    try:
        filters = {}
        if search:
            filters["territory_name"] = ["like", f"%{search}%"]
            
        # Fetch from Territory doctype
        territories = frappe.get_all(
            "Territory",
            fields=["name", "territory_name", "is_group"],
            filters=filters,
            order_by="territory_name ASC",
            limit=50
        )
        
        # Add delivery income/expense if custom fields exist
        for territory in territories:
            try:
                territory_doc = frappe.get_doc("Territory", territory['name'])
                if hasattr(territory_doc, 'delivery_income'):
                    territory["delivery_income"] = flt(territory_doc.delivery_income)
                else:
                    territory["delivery_income"] = 0.0
                    
                if hasattr(territory_doc, 'delivery_expense'):
                    territory["delivery_expense"] = flt(territory_doc.delivery_expense)
                else:
                    territory["delivery_expense"] = 0.0
            except Exception:
                territory["delivery_income"] = 0.0
                territory["delivery_expense"] = 0.0
            
        return territories
        
    except Exception as e:
        frappe.log_error(f"Error fetching territories: {str(e)}")
        return []

@frappe.whitelist()
def create_customer(customer_name, mobile_no, customer_primary_address, territory_id, location_link=None):
    """Create a new customer quickly from POS with Territory integration"""
    try:
        # Debug: Log the received parameters
        frappe.logger().info(f"create_customer called with: customer_name={customer_name}, mobile_no={mobile_no}, territory_id={territory_id}, address={customer_primary_address}, location_link={location_link}")
        
        # Validate required parameters
        if not customer_name or not mobile_no or not customer_primary_address or not territory_id:
            frappe.throw("Missing required parameters: customer_name, mobile_no, customer_primary_address, territory_id")
        
        # Check if customer already exists with this name
        existing = frappe.db.exists("Customer", {"customer_name": customer_name})
        if existing:
            frappe.throw(f"Customer with name '{customer_name}' already exists")
        
        # Validate territory exists
        if not frappe.db.exists("Territory", territory_id):
            frappe.throw(f"Territory with ID '{territory_id}' does not exist")
            
        territory_doc = frappe.get_doc("Territory", territory_id)
        territory_name = territory_doc.territory_name
        
        # Create customer document with only essential fields
        customer_doc = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_type": "Individual",
            "customer_group": "Individual", 
            "territory": territory_name
        })
        
        frappe.logger().info(f"Creating customer with basic data")
        customer_doc.insert(ignore_permissions=True)
        frappe.logger().info(f"Customer created successfully: {customer_doc.name}")
        
        # Create address
        address_doc = frappe.get_doc({
            "doctype": "Address",
            "address_title": customer_name,
            "address_type": "Billing",
            "address_line1": customer_primary_address,
            "city": territory_name,  # Use territory name as city
            "links": [{
                "link_doctype": "Customer",
                "link_name": customer_doc.name
            }]
        })
        
        if location_link:
            address_doc.address_line2 = f"Location: {location_link}"
        
        frappe.logger().info(f"Creating address")
        address_doc.insert(ignore_permissions=True)
        frappe.logger().info(f"Address created successfully: {address_doc.name}")
        
        # Create contact
        contact_doc = frappe.get_doc({
            "doctype": "Contact",
            "first_name": customer_name,
            "mobile_no": mobile_no,
            "links": [{
                "link_doctype": "Customer",
                "link_name": customer_doc.name
            }]
        })
        
        frappe.logger().info(f"Creating contact")
        contact_doc.insert(ignore_permissions=True)
        frappe.logger().info(f"Contact created successfully: {contact_doc.name}")
        
        # Update customer with primary address and contact
        customer_doc.customer_primary_address = address_doc.name
        customer_doc.customer_primary_contact = contact_doc.name
        customer_doc.save(ignore_permissions=True)
        
        frappe.logger().info(f"Customer updated with address and contact")
        
        # Return the created customer data with territory info
        result = {
            "name": customer_doc.name,
            "customer_name": customer_doc.customer_name,
            "mobile_no": mobile_no,
            "customer_primary_address": customer_doc.customer_primary_address,
            "customer_primary_contact": customer_doc.customer_primary_contact,
            "territory": customer_doc.territory,
            "customer_group": customer_doc.customer_group
        }
        
        # Add territory delivery info
        _augment_customer_with_territory(result)
        return result
        
    except Exception as e:
        frappe.log_error(f"Error creating customer: {str(e)}")
        frappe.throw(f"Failed to create customer: {str(e)}")


# ---------------------------------------------------------------------------
# Territory details endpoint (mobile app convenience)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_territory(territory_id: str | None = None):
    """Return **Territory** doc fields needed by the mobile app.

    Args:
        territory_id: Name / ID of the *Territory* DocType.

    Returns:
        dict | None: ``{"id": name, "name": territory_name, "delivery_income": x, "delivery_expense": y}``
    """

    if not territory_id:
        frappe.throw("territory_id parameter required")

    if not frappe.db.exists("Territory", territory_id):
        frappe.throw(f"Territory '{territory_id}' not found")

    territory_doc = frappe.get_doc("Territory", territory_id)
    result = {
        "id": territory_doc.name,
        "name": territory_doc.territory_name,
        "delivery_income": 0.0,
        "delivery_expense": 0.0,
    }
    
    # Add custom fields if they exist
    if hasattr(territory_doc, 'delivery_income'):
        result["delivery_income"] = flt(territory_doc.delivery_income)
    if hasattr(territory_doc, 'delivery_expense'):
        result["delivery_expense"] = flt(territory_doc.delivery_expense)
    
    return result

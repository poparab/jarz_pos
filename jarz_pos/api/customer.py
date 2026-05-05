# customer.py
import frappe
from frappe import _
from frappe.utils import flt
from frappe.model.document import Document

from jarz_pos.utils.customer_address_utils import (
    ADDRESS_PHONE_FIELDS,
    _address_phone,
    ensure_shipping_address,
    format_address_text,
    get_customer_shipping_addresses as _get_customer_shipping_addresses,
    get_linked_customer_address_names,
    link_shipping_address_to_invoice,
    resolve_customer_shipping_address,
    set_customer_primary_shipping_address,
)

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
                
            cust_row["territory_name"] = _(territory_doc.territory_name)
            cust_row["territory_name_ar"] = getattr(territory_doc, 'custom_territory_name_ar', '') or ''
        else:
            # Territory doesn't exist - set defaults
            cust_row["delivery_income"] = 0.0
            cust_row["delivery_expense"] = 0.0
            cust_row["territory_name"] = _(territory)
    except Exception as _err:
        # Swallow – augmentation is best-effort; log for debugging
        frappe.logger().warning(f"Territory augmentation failed for customer {cust_row.get('name')}: {_err}")
        cust_row["delivery_income"] = 0.0
        cust_row["delivery_expense"] = 0.0


def _customer_phone(customer_doc: Document, selected_address: dict | None = None) -> str:
    if selected_address:
        phone = _address_phone(selected_address)
        if phone:
            return phone

    for fieldname in ("mobile_no", "phone"):
        value = str(getattr(customer_doc, fieldname, "") or "").strip()
        if value:
            return value

    if customer_doc.customer_primary_contact and frappe.db.exists("Contact", customer_doc.customer_primary_contact):
        contact_doc = frappe.get_doc("Contact", customer_doc.customer_primary_contact)
        for fieldname in ("mobile_no", "phone"):
            value = str(getattr(contact_doc, fieldname, "") or "").strip()
            if value:
                return value

    return ""


def _build_customer_shipping_address_book(customer: str, invoice: str | None = None) -> dict:
    customer_doc = frappe.get_doc("Customer", customer)
    preferred_address_name = ""
    if invoice and frappe.db.exists("Sales Invoice", invoice):
        preferred_address_name = str(
            frappe.db.get_value("Sales Invoice", invoice, "shipping_address_name")
            or frappe.db.get_value("Sales Invoice", invoice, "customer_address")
            or ""
        ).strip()

    addresses = _get_customer_shipping_addresses(customer)
    selected_address = resolve_customer_shipping_address(
        customer,
        preferred_address_name=preferred_address_name or customer_doc.customer_primary_address,
    )
    selected_address_name = str(selected_address.get("name") or "").strip() if selected_address else ""

    return {
        "customer": customer_doc.name,
        "customer_name": customer_doc.customer_name,
        "addresses": addresses,
        "selected_address_name": selected_address_name,
        "selected_address": dict(selected_address) if selected_address else None,
        "default_phone": _customer_phone(customer_doc, selected_address),
    }


def _sync_customer_phone(customer_doc: Document, phone: str) -> None:
    phone = str(phone or "").strip()
    if not phone:
        return

    if frappe.db.has_column("Customer", "mobile_no"):
        customer_doc.mobile_no = phone
    if frappe.db.has_column("Customer", "phone"):
        customer_doc.phone = phone

    if customer_doc.customer_primary_contact and frappe.db.exists("Contact", customer_doc.customer_primary_contact):
        contact_doc = frappe.get_doc("Contact", customer_doc.customer_primary_contact)
        contact_doc.mobile_no = phone
        contact_doc.save(ignore_permissions=True)
    else:
        contact_doc = frappe.get_doc({
            "doctype": "Contact",
            "first_name": customer_doc.customer_name,
            "mobile_no": phone,
            "is_primary_contact": 1,
            "links": [{
                "link_doctype": "Customer",
                "link_name": customer_doc.name,
            }],
        })
        contact_doc.insert(ignore_permissions=True)
        customer_doc.customer_primary_contact = contact_doc.name


def _apply_phone_to_address(address_doc: Document, phone: str) -> None:
    phone = str(phone or "").strip()
    if not phone:
        return

    for fieldname in ADDRESS_PHONE_FIELDS:
        if frappe.db.has_column("Address", fieldname):
            address_doc.set(fieldname, phone)


@frappe.whitelist()
def get_customer_shipping_addresses(customer, invoice=None):
    """Return the linked customer shipping-address book and currently selected address."""
    try:
        if not customer or not frappe.db.exists("Customer", customer):
            frappe.throw(_("Customer not found."))

        return _build_customer_shipping_address_book(customer, invoice=invoice)
    except Exception as e:
        frappe.log_error(f"get_customer_shipping_addresses: {str(e)}", frappe.get_traceback())
        frappe.throw(_("Failed to fetch customer shipping addresses."))


@frappe.whitelist()
def save_customer_shipping_address(
    customer,
    phone=None,
    invoice=None,
    address_name=None,
    address=None,
    set_as_primary=1,
):
    """Select an existing shipping address or create a new one for a customer."""
    try:
        if not customer or not frappe.db.exists("Customer", customer):
            frappe.throw(_("Customer not found."))

        customer_doc = frappe.get_doc("Customer", customer)
        normalized_address_name = str(address_name or "").strip()
        normalized_address = str(address or "").strip()
        use_as_primary = str(set_as_primary or "1").strip().lower() not in {"0", "false", "no", "off"}

        if normalized_address_name:
            linked_names = set(get_linked_customer_address_names(customer))
            if normalized_address_name not in linked_names:
                frappe.throw(_("Selected address does not belong to this customer."))
            address_doc = ensure_shipping_address(normalized_address_name)
            if address_doc is None:
                frappe.throw(_("Selected address was not found."))
        else:
            if not normalized_address:
                frappe.throw(_("Address is required."))

            address_payload = {
                "doctype": "Address",
                "address_title": customer_doc.customer_name,
                "address_type": "Shipping",
                "address_line1": normalized_address,
                "city": customer_doc.territory or "Unknown",
                "is_primary_address": 1 if use_as_primary else 0,
                "is_shipping_address": 1,
                "links": [{
                    "link_doctype": "Customer",
                    "link_name": customer_doc.name,
                }],
            }
            for fieldname in ADDRESS_PHONE_FIELDS:
                if frappe.db.has_column("Address", fieldname) and str(phone or "").strip():
                    address_payload[fieldname] = str(phone).strip()

            address_doc = frappe.get_doc(address_payload)
            address_doc.insert(ignore_permissions=True)

        _apply_phone_to_address(address_doc, str(phone or "").strip())
        address_doc.save(ignore_permissions=True)

        ensure_shipping_address(address_doc.name)
        if use_as_primary:
            set_customer_primary_shipping_address(customer_doc.name, address_doc.name)
            customer_doc.customer_primary_address = address_doc.name

        _sync_customer_phone(customer_doc, str(phone or "").strip())
        customer_doc.save(ignore_permissions=True)

        if invoice:
            link_shipping_address_to_invoice(invoice, address_doc.name)

        frappe.db.commit()
        return {
            "success": True,
            "message": "Customer shipping address updated successfully",
            "selected_address_name": address_doc.name,
            "selected_address": {
                "name": address_doc.name,
                "full_address": format_address_text(address_doc.as_dict()),
                "phone": _address_phone(address_doc.as_dict(), str(phone or "").strip()),
            },
            "address_book": _build_customer_shipping_address_book(customer_doc.name, invoice=invoice),
        }
    except Exception as e:
        frappe.log_error(f"save_customer_shipping_address: {str(e)}", frappe.get_traceback())
        frappe.throw(_("Failed to save customer shipping address."))

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
            territory["territory_name"] = _(territory.get("territory_name", ""))
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
def create_customer(customer_name, mobile_no, customer_primary_address, territory_id, location_link=None, secondary_mobile=None):
    """Create a new customer quickly from POS with Territory integration"""
    try:
        # Debug: Log the received parameters
        frappe.logger().info(f"create_customer called with: customer_name={customer_name}, mobile_no={mobile_no}, territory_id={territory_id}, address={customer_primary_address}, location_link={location_link}")
        
        # Validate required parameters
        if not customer_name or not mobile_no or not customer_primary_address or not territory_id:
            frappe.throw("Missing required parameters: customer_name, mobile_no, customer_primary_address, territory_id")
        
        # Allow duplicate names but block duplicate phone numbers to avoid merges/confusion
        if frappe.db.exists("Customer", {"mobile_no": mobile_no}):
            frappe.throw(f"Customer with mobile number '{mobile_no}' already exists")
        # Also guard against existing contacts with the same mobile
        if frappe.db.exists("Contact", {"mobile_no": mobile_no}):
            frappe.throw(f"Customer with mobile number '{mobile_no}' already exists")
        
        # Validate territory exists
        if not frappe.db.exists("Territory", territory_id):
            frappe.throw(f"Territory with ID '{territory_id}' does not exist")
            
        territory_doc = frappe.get_doc("Territory", territory_id)
        territory_name = territory_doc.territory_name
        
        # Create customer document with only essential fields
        customer_payload = {
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_type": "Individual",
            "customer_group": "Individual",
            "territory": territory_name,
        }
        # Store phone on the Customer itself when the field exists
        if frappe.db.has_column("Customer", "mobile_no"):
            customer_payload["mobile_no"] = mobile_no
        if frappe.db.has_column("Customer", "phone"):
            customer_payload["phone"] = mobile_no

        customer_doc = frappe.get_doc(customer_payload)
        
        frappe.logger().info(f"Creating customer with basic data")
        customer_doc.insert(ignore_permissions=True)
        frappe.logger().info(f"Customer created successfully: {customer_doc.name}")
        
        # Create address
        address_payload = {
            "doctype": "Address",
            "address_title": customer_name,
            "address_type": "Shipping",
            "address_line1": customer_primary_address,
            "city": territory_name,  # Use territory name as city
            "is_primary_address": 1,
            "is_shipping_address": 1,
            "links": [{
                "link_doctype": "Customer",
                "link_name": customer_doc.name
            }]
        }

        # Store phone on Address if the field exists
        if frappe.db.has_column("Address", "phone"):
            address_payload["phone"] = mobile_no
        if frappe.db.has_column("Address", "phone_number"):
            address_payload["phone_number"] = mobile_no
        if frappe.db.has_column("Address", "phone_no"):
            address_payload["phone_no"] = mobile_no
        if frappe.db.has_column("Address", "mobile_no"):
            address_payload["mobile_no"] = mobile_no

        address_doc = frappe.get_doc(address_payload)
        
        if location_link:
            address_doc.address_line2 = f"Location: {location_link}"
        
        frappe.logger().info(f"Creating address")
        address_doc.insert(ignore_permissions=True)
        frappe.logger().info(f"Address created successfully: {address_doc.name}")
        
        # Create contact
        contact_payload = {
            "doctype": "Contact",
            "first_name": customer_name,
            "mobile_no": mobile_no,
            "is_primary_contact": 1,
            "links": [{
                "link_doctype": "Customer",
                "link_name": customer_doc.name
            }]
        }
        if secondary_mobile:
            contact_payload["phone"] = secondary_mobile
        contact_doc = frappe.get_doc(contact_payload)
        
        frappe.logger().info(f"Creating contact")
        contact_doc.insert(ignore_permissions=True)
        frappe.logger().info(f"Contact created successfully: {contact_doc.name}")
        
        # Update customer with primary address, contact, and phone
        customer_doc.customer_primary_address = address_doc.name
        customer_doc.customer_primary_contact = contact_doc.name
        if frappe.db.has_column("Customer", "mobile_no"):
            customer_doc.mobile_no = mobile_no
        if frappe.db.has_column("Customer", "phone"):
            customer_doc.phone = mobile_no
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
        "name": _(territory_doc.territory_name),
        "territory_name_ar": getattr(territory_doc, 'custom_territory_name_ar', '') or '',
        "delivery_income": 0.0,
        "delivery_expense": 0.0,
    }
    
    # Add custom fields if they exist
    if hasattr(territory_doc, 'delivery_income'):
        result["delivery_income"] = flt(territory_doc.delivery_income)
    if hasattr(territory_doc, 'delivery_expense'):
        result["delivery_expense"] = flt(territory_doc.delivery_expense)
    
    return result


@frappe.whitelist()
def update_default_address(customer, address, phone, invoice=None):
    """Update customer's default address and phone number.
    
    Creates or updates the customer's primary address and contact.
    Sets the address as the default billing and shipping address.
    When an invoice is provided, also updates that Sales Invoice's linked
    address so the kanban board reflects the change immediately.
    
    Args:
        customer: Customer ID/name
        address: New address text
        phone: New phone number
        invoice: (optional) Sales Invoice name to update directly
        
    Returns:
        dict: {"success": True, "message": "Customer address updated successfully"}
    """
    try:
        # Validate customer exists
        if not frappe.db.exists("Customer", customer):
            frappe.throw(f"Customer '{customer}' not found")
            
        customer_doc = frappe.get_doc("Customer", customer)

        result = save_customer_shipping_address(
            customer=customer,
            phone=phone,
            invoice=invoice,
            address=address,
            set_as_primary=1,
        )
        result["message"] = "Customer address updated successfully"
        return result
        
    except Exception as e:
        frappe.log_error(f"Error updating customer address: {str(e)}", frappe.get_traceback())
        frappe.throw(f"Failed to update customer address: {str(e)}")

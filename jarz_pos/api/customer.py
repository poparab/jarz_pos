# customer.py
import frappe
from frappe import _
from frappe.utils import flt
from frappe.model.document import Document

from jarz_pos.utils.customer_address_utils import (
    ADDRESS_PHONE_FIELDS,
    _address_phone,
    ensure_shipping_address,
    find_matching_customer_address,
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
    territory=None,
    set_as_primary=1,
):
    """Select an existing shipping address or create a new one for a customer."""
    try:
        if not customer or not frappe.db.exists("Customer", customer):
            frappe.throw(_("Customer not found."))

        customer_doc = frappe.get_doc("Customer", customer)
        normalized_address_name = str(address_name or "").strip()
        normalized_address = str(address or "").strip()
        normalized_territory = str(territory or "").strip()
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

            matching_address = find_matching_customer_address(customer_doc.name, normalized_address)
            if matching_address:
                address_doc = ensure_shipping_address(str(matching_address.get("name") or ""))
                if address_doc is None:
                    frappe.throw(_("Matching customer address was not found."))
            else:
                address_payload = {
                    "doctype": "Address",
                    "address_title": customer_doc.customer_name,
                    "address_type": "Shipping",
                    "address_line1": normalized_address,
                    "city": normalized_territory or customer_doc.territory or "Unknown",
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
                "city": str(address_doc.city or "").strip(),
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
def search_customers(name=None, phone=None, customer_type=None):
    """Enhanced search for customers by name or phone using direct SQL.

    Optional ``customer_type`` ("Individual" | "Company") restricts results to that
    customer type — used to keep Company customers in the B2B flow and out of B2C.
    Default None leaves results unchanged (all types).
    """

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

    # Optional customer_type filter (parameterized). Constrains the name/phone match.
    resolved_type = (customer_type or "").strip()
    if resolved_type:
        if resolved_type not in ("Individual", "Company"):
            frappe.throw("customer_type must be 'Individual' or 'Company'")
        if frappe.db.has_column("Customer", "customer_type"):
            conditions.append("`customer_type` = %(customer_type)s")
            params['customer_type'] = resolved_type

    # The name/phone match is a single OR group; the optional customer_type filter
    # (when present) is ANDed so it always constrains the result set.
    type_condition = None
    if resolved_type and 'customer_type' in params:
        type_condition = conditions.pop()  # the customer_type clause appended above

    query += "(" + " OR ".join(conditions) + ")"
    if type_condition:
        query += f" AND {type_condition}"

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
                territory["territory_name_ar"] = getattr(
                    territory_doc,
                    "custom_territory_name_ar",
                    "",
                ) or ""
                if hasattr(territory_doc, 'delivery_income'):
                    territory["delivery_income"] = flt(territory_doc.delivery_income)
                else:
                    territory["delivery_income"] = 0.0
                    
                if hasattr(territory_doc, 'delivery_expense'):
                    territory["delivery_expense"] = flt(territory_doc.delivery_expense)
                else:
                    territory["delivery_expense"] = 0.0
            except Exception:
                territory["territory_name_ar"] = ""
                territory["delivery_income"] = 0.0
                territory["delivery_expense"] = 0.0
            
        return territories
        
    except Exception as e:
        frappe.log_error(f"Error fetching territories: {str(e)}")
        return []

def _contacts_block_lead_conversion(mobile_no, source_lead):
    """Return True if existing Contacts with ``mobile_no`` should block creating a
    Customer while converting ``source_lead`` (Lead -> Customer).

    When Frappe auto-creates a Contact for a Lead, that Contact carries the lead's
    mobile. Converting that Lead to a Customer must NOT be blocked by its own
    auto-created Contact. We only block when a *conflicting* third-party/customer
    Contact shares the mobile.

    A conflicting Contact is "OK to ignore" only if it is linked to ``source_lead``
    AND is not linked to any existing Customer. If every conflicting Contact is
    OK-to-ignore -> not blocked (return False). If any is a real third-party /
    Customer contact -> blocked (return True).

    On ANY error we fall back to the conservative behavior: a Contact with that
    mobile exists -> treat as blocked, so we never accidentally create duplicates.
    """
    try:
        contacts = frappe.get_all(
            "Contact", filters={"mobile_no": mobile_no}, pluck="name"
        )
        if not contacts:
            return False

        for contact_name in contacts:
            linked_to_lead = frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Contact",
                    "parent": contact_name,
                    "link_doctype": "Lead",
                    "link_name": source_lead,
                },
                limit=1,
            )
            if not linked_to_lead:
                # Contact belongs to some other party -> real conflict.
                return True

            linked_to_customer = frappe.get_all(
                "Dynamic Link",
                filters={
                    "parenttype": "Contact",
                    "parent": contact_name,
                    "link_doctype": "Customer",
                },
                limit=1,
            )
            if linked_to_customer:
                # Already tied to a Customer -> real conflict.
                return True

        # Every conflicting contact belongs only to source_lead -> safe to ignore.
        return False
    except Exception:
        # Conservative fallback: a Contact with this mobile exists -> block.
        return True


@frappe.whitelist()
def create_customer(customer_name, mobile_no, customer_primary_address, territory_id, location_link=None, secondary_mobile=None, customer_type=None, customer_group=None, source_lead=None):
    """Create a new customer quickly from POS with Territory integration.

    customer_type/customer_group default to "Individual" (unchanged retail behavior).
    Pass customer_type="Company" with a B2B/Distributor/Employee customer_group to
    create a business customer for the B2B sales flow.

    source_lead (optional): when converting a Lead -> Customer (B2B flow), pass the
    Lead name so the Contact-mobile guard ignores the Lead's own auto-created Contact.
    B2C retail callers omit this and get the unchanged strict guard.
    """
    try:
        # Debug: Log the received parameters
        frappe.logger().info(f"create_customer called with: customer_name={customer_name}, mobile_no={mobile_no}, territory_id={territory_id}, address={customer_primary_address}, location_link={location_link}")
        
        # Validate required parameters
        if not customer_name or not mobile_no or not customer_primary_address or not territory_id:
            frappe.throw("Missing required parameters: customer_name, mobile_no, customer_primary_address, territory_id")
        
        # Allow duplicate names but block duplicate phone numbers to avoid merges/confusion
        if frappe.db.exists("Customer", {"mobile_no": mobile_no}):
            frappe.throw(f"Customer with mobile number '{mobile_no}' already exists")
        # Also guard against existing contacts with the same mobile.
        # Lead-aware: when converting a Lead -> Customer, ignore the Lead's own
        # auto-created Contact; otherwise keep the strict B2C behavior.
        valid_source_lead = bool(source_lead) and bool(
            frappe.db.exists("Lead", source_lead)
        )
        if valid_source_lead:
            if _contacts_block_lead_conversion(mobile_no, source_lead):
                frappe.throw(f"Customer with mobile number '{mobile_no}' already exists")
        elif frappe.db.exists("Contact", {"mobile_no": mobile_no}):
            frappe.throw(f"Customer with mobile number '{mobile_no}' already exists")
        
        # Validate territory exists
        if not frappe.db.exists("Territory", territory_id):
            frappe.throw(f"Territory with ID '{territory_id}' does not exist")
            
        territory_doc = frappe.get_doc("Territory", territory_id)
        territory_name = territory_doc.territory_name

        # Resolve customer type/group. Defaults preserve retail behavior; validate any
        # caller-supplied overrides so an invalid value can't silently mis-classify.
        resolved_type = (customer_type or "").strip() or "Individual"
        if resolved_type not in ("Individual", "Company"):
            frappe.throw("customer_type must be 'Individual' or 'Company'")
        resolved_group = (customer_group or "").strip() or "Individual"
        if not frappe.db.exists("Customer Group", resolved_group):
            frappe.throw(f"Customer Group '{resolved_group}' does not exist")

        # Create customer document with only essential fields
        customer_payload = {
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_type": resolved_type,
            "customer_group": resolved_group,
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


# ---------------------------------------------------------------------------
# Address management — edit, delete, change on invoice
# ---------------------------------------------------------------------------

@frappe.whitelist()
def update_customer_shipping_address(
    customer,
    address_name,
    address_line1=None,
    address_line2=None,
    city=None,
    phone=None,
    pincode=None,
):
    """Edit fields on an existing shipping address owned by this customer.

    Args:
        customer: Customer ID.
        address_name: Name of the Address doc to update.
        address_line1: New first line (optional).
        address_line2: New second line (optional).
        city: Territory/city name (optional; used to resolve shipping costs).
        phone: New phone (optional).
        pincode: Postal code (optional).

    Returns:
        dict: Updated address-book payload identical to
              ``get_customer_shipping_addresses``.
    """
    try:
        if not customer or not frappe.db.exists("Customer", customer):
            frappe.throw(_("Customer not found."))

        linked_names = set(get_linked_customer_address_names(customer))
        if address_name not in linked_names:
            frappe.throw(_("Address does not belong to this customer."))

        if not frappe.db.exists("Address", address_name):
            frappe.throw(_("Address not found."))

        address_doc = frappe.get_doc("Address", address_name)

        if str(address_line1 or "").strip():
            address_doc.address_line1 = str(address_line1).strip()
        if address_line2 is not None:
            address_doc.address_line2 = str(address_line2).strip()
        if str(city or "").strip():
            address_doc.city = str(city).strip()
        if str(pincode or "").strip():
            address_doc.pincode = str(pincode).strip()

        _apply_phone_to_address(address_doc, str(phone or "").strip())

        address_doc.address_type = "Shipping"
        address_doc.is_shipping_address = 1
        address_doc.save(ignore_permissions=True)
        frappe.db.commit()

        return _build_customer_shipping_address_book(customer)
    except Exception as e:
        frappe.log_error(f"update_customer_shipping_address: {str(e)}", frappe.get_traceback())
        frappe.throw(_("Failed to update address."))


@frappe.whitelist()
def delete_customer_shipping_address(customer, address_name):
    """Delete a shipping address that belongs to this customer.

    Refuses deletion if any non-cancelled Sales Invoice still references the
    address (via ``shipping_address_name`` or ``customer_address``).

    Args:
        customer: Customer ID.
        address_name: Name of the Address doc to delete.

    Returns:
        dict: ``{"success": True}`` on success or raises with an error that
              includes an ``invoices`` key listing blocking invoice names.
    """
    try:
        if not customer or not frappe.db.exists("Customer", customer):
            frappe.throw(_("Customer not found."))

        linked_names = set(get_linked_customer_address_names(customer))
        if address_name not in linked_names:
            frappe.throw(_("Address does not belong to this customer."))

        if not frappe.db.exists("Address", address_name):
            frappe.throw(_("Address not found."))

        # Check if any non-cancelled invoices reference this address.
        blocking_invoices = frappe.get_all(
            "Sales Invoice",
            filters=[
                ["docstatus", "!=", 2],
                ["shipping_address_name", "=", address_name],
            ],
            fields=["name"],
            limit=20,
        ) or []
        also_blocking = frappe.get_all(
            "Sales Invoice",
            filters=[
                ["docstatus", "!=", 2],
                ["customer_address", "=", address_name],
                ["shipping_address_name", "!=", address_name],
            ],
            fields=["name"],
            limit=20,
        ) or []
        blocking = list({r["name"] for r in blocking_invoices + also_blocking})

        if blocking:
            frappe.throw(
                _("Cannot delete address: it is referenced by {0} invoice(s). "
                  "Reassign the address on these invoices first: {1}").format(
                    len(blocking), ", ".join(blocking[:10])
                )
            )

        # Clear customer_primary_address if it pointed to this address.
        customer_doc = frappe.get_doc("Customer", customer)
        if customer_doc.customer_primary_address == address_name:
            remaining = [n for n in linked_names if n != address_name]
            customer_doc.customer_primary_address = remaining[0] if remaining else ""
            customer_doc.save(ignore_permissions=True)

        frappe.delete_doc("Address", address_name, ignore_permissions=True, force=True)
        frappe.db.commit()

        return {"success": True, "address_book": _build_customer_shipping_address_book(customer)}
    except Exception as e:
        frappe.log_error(f"delete_customer_shipping_address: {str(e)}", frappe.get_traceback())
        frappe.throw(_("Failed to delete address."))


@frappe.whitelist()
def change_invoice_shipping_address(invoice_name, address_name):
    """Re-link a Sales Invoice to a different shipping address and optionally
    recompute shipping income / expense when the territory changes.

    Only allowed when the invoice has **not** yet gone Out for Delivery and
    has no downstream settlement artifacts (Courier Transaction, Sales Partner
    Transaction, Journal Entry from Settle Later).  Payment-only (paid but
    pre-OFD) invoices are explicitly allowed.

    Args:
        invoice_name: Name of the Sales Invoice.
        address_name: Name of the Address doc to link.

    Returns:
        dict with keys: success, territory_changed, old_territory, new_territory,
        old_expense, new_expense, old_income, new_income.
    """
    try:
        if not frappe.db.exists("Sales Invoice", invoice_name):
            frappe.throw(_("Sales Invoice not found."))
        if not frappe.db.exists("Address", address_name):
            frappe.throw(_("Address not found."))

        inv = frappe.get_doc("Sales Invoice", invoice_name)

        # Gate: check for settlement artifacts (OFD Delivery Note, trip, courier, partner, JE).
        try:
            from jarz_pos.api.manager import get_invoice_hard_mutation_blocker
            blocker = get_invoice_hard_mutation_blocker(inv)
            if blocker:
                frappe.throw(
                    blocker.get("mutation_block_reason")
                    or _("This invoice cannot have its address changed at this stage.")
                )
        except ImportError:
            pass

        # Re-link address fields on the SI (works on Submitted via ignore_validate_update_after_submit).
        link_shipping_address_to_invoice(invoice_name, address_name)

        old_territory = str(inv.territory or "").strip()
        address_doc = frappe.get_doc("Address", address_name)
        new_territory = str(address_doc.city or "").strip()  # city holds territory name by convention

        # Validate the city/territory value actually exists as a Territory.
        if new_territory and not frappe.db.exists("Territory", new_territory):
            new_territory = old_territory  # fall back silently

        territory_changed = bool(new_territory and new_territory != old_territory)

        # Helper to read shipping values from Territory.
        def _territory_values(territory: str):
            if not territory or not frappe.db.exists("Territory", territory):
                return 0.0, 0.0
            try:
                terr = frappe.get_doc("Territory", territory)
                income = flt(getattr(terr, "delivery_income", 0) or 0)
                expense = flt(getattr(terr, "delivery_expense", 0) or 0)
                return income, expense
            except Exception:
                return 0.0, 0.0

        old_income, old_expense = _territory_values(old_territory)
        new_income = old_income
        new_expense = old_expense

        if territory_changed:
            # Update territory on invoice.
            frappe.db.set_value(
                "Sales Invoice", invoice_name, "territory", new_territory, update_modified=False
            )

            new_income, new_expense = _territory_values(new_territory)

            # Recompute custom_shipping_expense unless an Approved override is in place.
            override_status = str(
                frappe.db.get_value("Sales Invoice", invoice_name, "custom_shipping_override_status") or ""
            ).strip()
            if override_status != "Approved":
                # Check sub-territory override.
                sub_terr = str(
                    frappe.db.get_value("Sales Invoice", invoice_name, "custom_sub_territory") or ""
                ).strip()
                if sub_terr and frappe.db.exists("Territory", sub_terr):
                    sub_expense = flt(frappe.db.get_value("Territory", sub_terr, "delivery_expense") or 0)
                    if sub_expense > 0:
                        new_expense = sub_expense
                frappe.db.set_value(
                    "Sales Invoice", invoice_name, "custom_shipping_expense", new_expense, update_modified=False
                )
                # Clear sub-territory since territory changed; user must re-select.
                frappe.db.set_value(
                    "Sales Invoice", invoice_name, "custom_sub_territory", "", update_modified=False
                )

            # Rebuild shipping income tax row to match new territory rate.
            # Reload first to pick up all db.set_value changes above.
            inv.reload()
            had_shipping_income_row = any(
                str(t.description or "").lower().startswith("shipping income")
                for t in (inv.get("taxes") or [])
            )
            if had_shipping_income_row:
                inv.set("taxes", [
                    t for t in (inv.get("taxes") or [])
                    if not str(t.description or "").lower().startswith("shipping income")
                ])
                if new_income > 0:
                    from jarz_pos.utils.delivery_utils import add_delivery_charges_to_taxes
                    add_delivery_charges_to_taxes(
                        inv,
                        new_income,
                        delivery_description=f"Shipping Income ({new_territory})",
                    )
                inv.calculate_taxes_and_totals()
                inv.flags.ignore_validate_update_after_submit = True
                inv.save(ignore_permissions=True)

            # Append a comment on the SI for audit trail.
            try:
                frappe.get_doc({
                    "doctype": "Comment",
                    "comment_type": "Info",
                    "reference_doctype": "Sales Invoice",
                    "reference_name": invoice_name,
                    "content": _(
                        "Shipping address changed to {0}. Territory updated from {1} to {2}. "
                        "Shipping expense: {3} → {4}. Shipping income: {5} → {6}."
                    ).format(address_name, old_territory, new_territory, old_expense, new_expense, old_income, new_income),
                }).insert(ignore_permissions=True)
            except Exception:
                pass

        frappe.db.commit()

        return {
            "success": True,
            "territory_changed": territory_changed,
            "old_territory": old_territory,
            "new_territory": new_territory if territory_changed else old_territory,
            "old_expense": old_expense,
            "new_expense": new_expense,
            "old_income": old_income,
            "new_income": new_income,
        }
    except Exception as e:
        frappe.log_error(f"change_invoice_shipping_address: {str(e)}", frappe.get_traceback())
        frappe.throw(_("Failed to change invoice shipping address."))

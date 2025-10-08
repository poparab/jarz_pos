"""Delivery Party creation utilities (Employee or Supplier) for Jarz POS.

Creates a new Employee or Supplier, adds it to the 'Delivery' group, and returns
standardized party data used by the POS frontend.
"""
from __future__ import annotations
import frappe

DELIVERY_EMP_GROUP_NAME = "Delivery"
DELIVERY_SUP_GROUP_NAME = "Delivery"


def _ensure_employee_group() -> str:
    name = frappe.db.get_value("Employee Group", {"employee_group_name": DELIVERY_EMP_GROUP_NAME}, "name")
    if name:
        return name
    doc = frappe.new_doc("Employee Group")
    doc.employee_group_name = DELIVERY_EMP_GROUP_NAME
    doc.save(ignore_permissions=True)
    return doc.name


def _ensure_supplier_group() -> str:
    name = frappe.db.get_value("Supplier Group", {"supplier_group_name": DELIVERY_SUP_GROUP_NAME}, "name")
    if name:
        return name
    doc = frappe.new_doc("Supplier Group")
    doc.supplier_group_name = DELIVERY_SUP_GROUP_NAME
    doc.save(ignore_permissions=True)
    return doc.name


def _ensure_branch(branch_name: str) -> str:
    if not branch_name:
        return ""
    existing = frappe.db.get_value("Branch", {"branch": branch_name}, "name")
    if existing:
        return existing
    # Create a Branch if DocType exists in this ERPNext version
    if frappe.db.exists("DocType", "Branch"):
        b = frappe.new_doc("Branch")
        b.branch = branch_name
        b.save(ignore_permissions=True)
        return b.name
    return ""  # silently ignore if Branch DocType not installed


def create_delivery_party(
    party_type: str,
    name: str | None = None,
    phone: str | None = None,
    branch: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> dict:
    """Create a new delivery party (Employee or Supplier) and add to Delivery group.

    Extended to support separate first_name / last_name for Employees. If both
    first_name & last_name provided they override *name* for display. Mandatory
    Employee fields auto-filled: gender='Male', date_of_joining=today,
    date_of_birth placeholder (1970-01-01) if field exists and is required.
    """
    try:
        party_type = (party_type or "").strip()
        if party_type not in {"Employee", "Supplier"}:
            frappe.throw(f"Invalid party_type '{party_type}'. Must be 'Employee' or 'Supplier'")

        first_name = (first_name or "").strip()
        last_name = (last_name or "").strip()
        name = (name or "").strip()
        # Determine display name precedence: first + last if both, else provided name
        if first_name and last_name:
            display_name = f"{first_name} {last_name}".strip()
        else:
            display_name = name or first_name or last_name
        if not display_name:
            frappe.throw("Courier name is required. Please provide first name and last name.")

        branch = (branch or "").strip()
        branch_name = _ensure_branch(branch) if branch else ""

        if party_type == "Employee":
            group_name = _ensure_employee_group()
            emp = frappe.new_doc("Employee")
            emp.first_name = first_name or display_name.split(" ")[0]
            if last_name:
                emp.last_name = last_name
            emp.employee_name = display_name
            # Mandatory defaults
            if emp.meta.get_field("gender"):
                emp.gender = "Male"
            if emp.meta.get_field("date_of_joining"):
                emp.date_of_joining = frappe.utils.today()
            if emp.meta.get_field("date_of_birth"):
                # Placeholder DOB - caller requested any placeholder
                emp.date_of_birth = "1970-01-01"
            if phone:
                if emp.meta.get_field("cell_number"):
                    emp.cell_number = phone
                elif emp.meta.get_field("mobile"):
                    emp.mobile = phone
            # Provide placeholder email if field exists and mandatory
            if emp.meta.get_field("personal_email"):
                emp.personal_email = f"{frappe.generate_hash(length=8)}@example.com"
            if branch_name and emp.meta.get_field("branch"):
                emp.branch = branch_name
            
            try:
                emp.save(ignore_permissions=True)
            except Exception as e:
                error_msg = str(e)
                frappe.logger().error(f"Failed to save Employee: {error_msg}")
                frappe.logger().error(frappe.get_traceback())
                # Provide user-friendly error message
                if "duplicate" in error_msg.lower():
                    frappe.throw(f"A courier with name '{display_name}' already exists. Please use a different name.")
                elif "mandatory" in error_msg.lower():
                    frappe.throw(f"Missing required field: {error_msg}")
                else:
                    frappe.throw(f"Failed to create Employee courier: {error_msg}")
            
            try:
                if frappe.db.exists("DocType", "Employee Group Member"):
                    member = frappe.new_doc("Employee Group Member")
                    member.employee_group = group_name
                    member.employee = emp.name
                    member.save(ignore_permissions=True)
            except Exception as e:
                frappe.logger().warning(f"Failed to add Employee to group: {str(e)}")
                pass
            party_id = emp.name
            final_display = emp.employee_name
        else:
            group_name = _ensure_supplier_group()
            sup = frappe.new_doc("Supplier")
            sup.supplier_name = display_name
            sup.supplier_group = group_name
            if phone:
                sup.mobile_no = phone
            if branch_name and sup.meta.get_field("branch"):
                sup.branch = branch_name
            
            try:
                sup.save(ignore_permissions=True)
            except Exception as e:
                error_msg = str(e)
                frappe.logger().error(f"Failed to save Supplier: {error_msg}")
                frappe.logger().error(frappe.get_traceback())
                # Provide user-friendly error message
                if "duplicate" in error_msg.lower():
                    frappe.throw(f"A courier with name '{display_name}' already exists. Please use a different name.")
                elif "mandatory" in error_msg.lower():
                    frappe.throw(f"Missing required field: {error_msg}")
                else:
                    frappe.throw(f"Failed to create Supplier courier: {error_msg}")
            
            party_id = sup.name
            final_display = sup.supplier_name or display_name

        return {
            "party_type": party_type,
            "party": party_id,
            "display_name": final_display,
            "phone": phone,
            "branch": branch,
        }
    except frappe.exceptions.ValidationError:
        # Re-raise validation errors as-is (already have good messages)
        raise
    except Exception as e:
        frappe.logger().error(f"Unexpected error in create_delivery_party: {str(e)}")
        frappe.logger().error(frappe.get_traceback())
        frappe.throw(f"Failed to create courier: {str(e)}")

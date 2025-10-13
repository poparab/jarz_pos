"""Delivery Party creation utilities (Employee or Supplier) for Jarz POS.

Creates a new Employee or Supplier, adds it to the 'Delivery' group, and returns
standardized party data used by the POS frontend.
"""
from __future__ import annotations

from typing import Optional

import frappe

try:  # frappe provides DuplicateEntryError in different modules across versions
    from frappe.model.naming import DuplicateEntryError  # type: ignore
except Exception:  # pragma: no cover - fallback for legacy releases
# Re-exported in frappe.exceptions in some versions
    from frappe import DuplicateEntryError  # type: ignore

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


def _ensure_employee_in_group(employee_name: str) -> None:
    """Add the given employee to the Delivery group if not already present."""

    group_name = _ensure_employee_group()
    try:
        group_doc = frappe.get_doc("Employee Group", group_name)
    except Exception:
        return
    rows = group_doc.get("employee_list") or []
    for row in rows:
        if row.get("employee") == employee_name:
            return
    group_doc.append("employee_list", {
        "employee": employee_name,
        "employee_name": frappe.db.get_value("Employee", employee_name, "employee_name") or employee_name,
    })
    group_doc.flags.ignore_permissions = True
    try:
        group_doc.save(ignore_permissions=True)
    except Exception:
        # Group membership issues should not block courier creation
        pass


def _normalize(value: Optional[str]) -> str:
    """Collapse whitespace and strip surrounding spaces."""

    return " ".join((value or "").split())


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

        first_name = _normalize(first_name)
        last_name = _normalize(last_name)
        name = _normalize(name)
        # Determine display name precedence: first + last if both, else provided name
        if first_name and last_name:
            display_name = f"{first_name} {last_name}".strip()
        else:
            display_name = name or first_name or last_name
        if not display_name:
            frappe.throw("Courier name is required. Please provide first name and last name.")
        display_name = _normalize(display_name)

        branch = _normalize(branch)
        branch_name = _ensure_branch(branch) if branch else ""

        def _reuse_existing(existing_doc: dict) -> dict:
            """Return existing party when duplicate name detected, keeping UX idempotent."""

            try:
                doc = frappe.get_doc(party_type, existing_doc["name"])
            except Exception:
                doc = None

            if doc:
                updated = False
                if phone:
                    for fld in ("cell_number", "mobile", "mobile_no"):
                        if doc.meta.get_field(fld) and doc.get(fld) != phone:
                            doc.set(fld, phone)
                            updated = True
                            break
                if branch_name and doc.meta.get_field("branch") and doc.get("branch") != branch_name:
                    doc.branch = branch_name
                    updated = True
                if updated:
                    try:
                        doc.save(ignore_permissions=True)
                    except Exception:
                        pass

            if party_type == "Employee":
                try:
                    _ensure_employee_in_group(existing_doc["name"])
                except Exception:
                    pass
            else:
                # ensure Supplier group exists for bookkeeping
                try:
                    group_name = _ensure_supplier_group()
                    doc = doc or frappe.get_doc(party_type, existing_doc["name"])
                    if doc and doc.meta.get_field("supplier_group") and doc.get("supplier_group") != group_name:
                        doc.supplier_group = group_name
                        doc.save(ignore_permissions=True)
                except Exception:
                    pass

            return {
                "party_type": party_type,
                "party": existing_doc["name"],
                "display_name": existing_doc.get("employee_name")
                or existing_doc.get("supplier_name")
                or display_name,
                "phone": phone or existing_doc.get("phone"),
                "branch": branch or existing_doc.get("branch"),
                "existing": True,
            }

        if party_type == "Employee":
            existing_emp = frappe.db.get_value(
                "Employee",
                {"employee_name": display_name},
                ["name", "employee_name", "branch"],
                as_dict=True,
            )
            if existing_emp:
                return _reuse_existing(existing_emp)
        else:
            existing_sup = frappe.db.get_value(
                "Supplier",
                {"supplier_name": display_name},
                ["name", "supplier_name", "branch"],
                as_dict=True,
            )
            if existing_sup:
                return _reuse_existing(existing_sup)

        if party_type == "Employee":
            _ensure_employee_group()
            emp = frappe.new_doc("Employee")
            emp.first_name = first_name or display_name.split(" ")[0]
            if last_name:
                emp.last_name = last_name
            emp.employee_name = display_name
            
            # MANDATORY FIELDS (must be set for Employee to save)
            # Naming series - Employee DocType uses autoname: naming_series: which requires this field
            # Prefer the first configured option so desk changes take effect, fall back to legacy prefix
            naming_series_field = emp.meta.get_field("naming_series")
            if naming_series_field:
                options = [o.strip() for o in (naming_series_field.options or "").split("\n") if o.strip()]
                if options:
                    emp.naming_series = options[0]
                else:
                    emp.naming_series = "HR-EMP-"
            else:
                emp.naming_series = "HR-EMP-"
            
            # Status field - set to Active (mandatory)
            emp.status = "Active"
            
            # Gender field - Link to Gender DocType (mandatory) - Always Male
            emp.gender = "Male"
            
            # Date of joining (mandatory)
            emp.date_of_joining = frappe.utils.today()
            
            # Date of birth (mandatory) - use a reasonable placeholder
            emp.date_of_birth = "1990-01-01"
            
            # Company field - mandatory for Employee
            try:
                default_company = frappe.db.get_single_value("Global Defaults", "default_company")
                if not default_company:
                    # Get first company if no default
                    default_company = frappe.db.get_value("Company", {}, "name")
                if default_company:
                    emp.company = default_company
                else:
                    frappe.logger().error("No company found - Employee creation will fail")
                    frappe.throw("No company configured. Please ask administrator to create a Company first.")
            except Exception as e:
                frappe.logger().error(f"Could not set company for Employee: {e}")
                frappe.throw("Could not determine company for Employee. Please contact administrator.")
            
            # Optional fields
            if phone:
                if emp.meta.get_field("cell_number"):
                    emp.cell_number = phone
                elif emp.meta.get_field("mobile"):
                    emp.mobile = phone
            
            # Provide placeholder email if field exists
            if emp.meta.get_field("personal_email"):
                emp.personal_email = f"{frappe.generate_hash(length=8)}@example.com"
            elif emp.meta.get_field("company_email"):
                emp.company_email = f"{frappe.generate_hash(length=8)}@example.com"
            
            if branch_name and emp.meta.get_field("branch"):
                emp.branch = branch_name
            
            # Log all fields being set for debugging
            frappe.logger().info(f"Creating Employee with fields: first_name={emp.first_name}, last_name={emp.get('last_name')}, employee_name={emp.employee_name}, company={emp.company}, gender={emp.gender}, status={emp.status}, date_of_joining={emp.date_of_joining}, date_of_birth={emp.date_of_birth}")
            
            try:
                emp.save(ignore_permissions=True)
                frappe.logger().info(f"Successfully created Employee: {emp.name}")
            except DuplicateEntryError:
                existing_emp = frappe.db.get_value(
                    "Employee",
                    {"employee_name": display_name},
                    ["name", "employee_name", "branch"],
                    as_dict=True,
                )
                if existing_emp:
                    return _reuse_existing(existing_emp)
                frappe.throw(f"A courier with name '{display_name}' already exists. Please use a different name.")
            except Exception as e:
                error_msg = str(e)
                frappe.logger().error(f"Failed to save Employee: {error_msg}")
                frappe.logger().error(frappe.get_traceback())
                # Provide user-friendly error message
                if "duplicate" in error_msg.lower():
                    existing_emp = frappe.db.get_value(
                        "Employee",
                        {"employee_name": display_name},
                        ["name", "employee_name", "branch"],
                        as_dict=True,
                    )
                    if existing_emp:
                        return _reuse_existing(existing_emp)
                    frappe.throw(f"A courier with name '{display_name}' already exists. Please use a different name.")
                elif "mandatory" in error_msg.lower():
                    frappe.throw(f"Missing required field: {error_msg}")
                elif "company" in error_msg.lower():
                    frappe.throw(f"Company field is required for Employee courier. Please contact administrator to set default company.")
                else:
                    frappe.throw(f"Failed to create Employee courier: {error_msg}")
            
            # Ensure employee is assigned to Delivery group (best effort)
            _ensure_employee_in_group(emp.name)
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
            except DuplicateEntryError:
                existing_sup = frappe.db.get_value(
                    "Supplier",
                    {"supplier_name": display_name},
                    ["name", "supplier_name", "branch"],
                    as_dict=True,
                )
                if existing_sup:
                    return _reuse_existing(existing_sup)
                frappe.throw(f"A courier with name '{display_name}' already exists. Please use a different name.")
            except Exception as e:
                error_msg = str(e)
                frappe.logger().error(f"Failed to save Supplier: {error_msg}")
                frappe.logger().error(frappe.get_traceback())
                # Provide user-friendly error message
                if "duplicate" in error_msg.lower():
                    existing_sup = frappe.db.get_value(
                        "Supplier",
                        {"supplier_name": display_name},
                        ["name", "supplier_name", "branch"],
                        as_dict=True,
                    )
                    if existing_sup:
                        return _reuse_existing(existing_sup)
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

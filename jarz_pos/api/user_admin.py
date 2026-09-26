"""User administration endpoints for the POS app (JARZ Manager only).

Thin wrappers: every rule lives in ``jarz_pos.services.user_admin``.
"""

from typing import Any, Optional

import frappe

from jarz_pos.services import user_admin as svc


@frappe.whitelist(allow_guest=False)
def get_context():
    return svc.get_context()


@frappe.whitelist(allow_guest=False)
def list_users(search: Optional[str] = None, include_disabled: Any = 1):
    return svc.list_users(search=search, include_disabled=include_disabled)


@frappe.whitelist(allow_guest=False)
def get_user(user: str):
    return svc.get_user(user)


@frappe.whitelist(allow_guest=False)
def list_employees(search: Optional[str] = None):
    return svc.list_employees(search=search)


@frappe.whitelist(allow_guest=False, methods=["POST"])
def create_user(
    email: str,
    first_name: str,
    password: str,
    role_profiles: Any,
    last_name: Optional[str] = None,
    mobile_no: Optional[str] = None,
    require_pos_shift: Any = 0,
    employee: Optional[str] = None,
):
    return svc.create_user(
        email=email,
        first_name=first_name,
        password=password,
        role_profiles=role_profiles,
        last_name=last_name,
        mobile_no=mobile_no,
        require_pos_shift=require_pos_shift,
        employee=employee,
    )


@frappe.whitelist(allow_guest=False, methods=["POST"])
def update_user(
    user: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    mobile_no: Optional[str] = None,
    role_profiles: Any = None,
    require_pos_shift: Any = None,
    employee: Any = None,
    clear_employee: Any = 0,
):
    return svc.update_user(
        user=user,
        first_name=first_name,
        last_name=last_name,
        mobile_no=mobile_no,
        role_profiles=role_profiles,
        require_pos_shift=require_pos_shift,
        employee=employee,
        clear_employee=clear_employee,
    )


@frappe.whitelist(allow_guest=False, methods=["POST"])
def set_enabled(user: str, enabled: Any):
    return svc.set_enabled(user=user, enabled=enabled)


@frappe.whitelist(allow_guest=False, methods=["POST"])
def reset_password(user: str, new_password: str, sign_out: Any = 1):
    return svc.reset_password(user=user, new_password=new_password, sign_out=sign_out)


@frappe.whitelist(allow_guest=False, methods=["POST"])
def delete_user(user: str):
    return svc.delete_user(user=user)

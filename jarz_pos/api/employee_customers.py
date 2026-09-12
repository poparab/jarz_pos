"""Staff-order employee picker for the POS.

A staff purchase is placed under the ``Employee Order`` commercial policy. The
cashier picks the EMPLOYEE, and the backend hands back that employee's one
canonical staff Customer (creating or adopting it if needed) in the same row
shape ``api/customer.search_customers`` returns, so the POS passes it straight
into its normal customer selection. See ``jarz_pos.services.employee_customers``
for how the customer is resolved and why.

Endpoints (all gated to ``ROLES.LINE_MANAGER_TIER`` through the same helper the
Employee Order policy's default gate uses)::

    GET  jarz_pos.api.employee_customers.list_staff_for_orders(search=None)
    POST jarz_pos.api.employee_customers.ensure_staff_customer(employee)
    POST jarz_pos.api.employee_customers.sync_staff_customers()

HRMS absent is a normal answer, not an error: every endpoint returns
``{"success": True, "hrms_available": False, ...empty}``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe

from jarz_pos.services import employee_customers as staff_customers
from jarz_pos.utils.employee_link import hrms_available, list_active_employees


def _has_staff_order_access() -> bool:
    """Same predicate as the Employee Order policy's default permission gate.

    ``services.commercial_policy._ensure_policy_permission`` falls back to
    ``services.invoice_creation._has_manager_pricing_access`` (LINE_MANAGER_TIER).
    Delegating instead of re-spelling the role set keeps "may pick a staff
    customer" and "may place the staff order" from drifting apart. Imported
    lazily: invoice_creation is heavy and imports commercial_policy.
    """
    from jarz_pos.services.invoice_creation import _has_manager_pricing_access

    return bool(_has_manager_pricing_access())


def _ensure_staff_order_access() -> None:
    if not _has_staff_order_access():
        frappe.throw(
            "Not permitted: manager access is required to place staff orders.",
            frappe.PermissionError,
        )


def _is_deadlock(exc: BaseException) -> bool:
    """True for an InnoDB deadlock, however Frappe surfaced it.

    ``frappe.db.sql`` re-raises a deadlock as ``frappe.QueryDeadlockError``;
    the raw driver error is recognised by ``frappe.db.is_deadlocked``.
    """
    try:
        if isinstance(exc, frappe.QueryDeadlockError):
            return True
    except Exception:
        pass
    try:
        return frappe.db.is_deadlocked(exc) is True
    except Exception:
        return False


def _customer_row(customer: str) -> Dict[str, Any]:
    """One Customer in exactly the shape a ``search_customers`` row has.

    Same columns, the same ``has_column`` probes for ``phone`` and
    ``custom_credit_allowed``, and the same ``_augment_customer_with_territory``
    pass, so the POS cannot tell this row from a search hit.
    """
    from jarz_pos.api.customer import _augment_customer_with_territory

    fields = [
        "name",
        "customer_name",
        "mobile_no",
        "customer_primary_address",
        "customer_primary_contact",
        "territory",
        "customer_group",
    ]
    if frappe.db.has_column("Customer", "phone"):
        fields.append("phone")
    has_credit_column = frappe.db.has_column("Customer", "custom_credit_allowed")
    if has_credit_column:
        fields.append("custom_credit_allowed")

    raw = frappe.db.get_value("Customer", customer, fields, as_dict=True)
    row: Dict[str, Any] = dict(raw or {"name": customer})

    _augment_customer_with_territory(row)
    row["credit_allowed"] = (
        bool(int(row.get("custom_credit_allowed") or 0)) if has_credit_column else False
    )
    # ``_augment_customer_with_territory`` only sets these when the customer HAS
    # a territory. A staff customer usually has none, and the POS model reads
    # both keys, so they are always present here.
    row.setdefault("territory_name", "")
    row.setdefault("territory_name_ar", "")
    return row


@frappe.whitelist()
def list_staff_for_orders(search: Optional[str] = None) -> Dict[str, Any]:
    """Active employees for the staff-order picker, with their staff customer.

    ``customer`` / ``customer_name`` are the canonical Employee-group customer
    (the one ``ensure_staff_customer`` would return as ``existing``), or null
    when the employee has none yet -- calling ``ensure_staff_customer`` creates
    or adopts it.
    """
    _ensure_staff_order_access()

    if not hrms_available():
        return {"success": True, "hrms_available": False, "employees": []}

    employees = list_active_employees(search=search)
    canonical = staff_customers.canonical_customers_for_employees(
        [e["employee"] for e in employees]
    )

    rows = []
    for emp in employees:
        match = canonical.get(emp["employee"]) or {}
        rows.append(
            {
                "employee": emp["employee"],
                "employee_name": emp["employee_name"],
                "branch": emp.get("branch") or "",
                "designation": emp.get("designation") or "",
                "customer": match.get("customer") or None,
                "customer_name": match.get("customer_name") or None,
            }
        )
    return {"success": True, "hrms_available": True, "employees": rows}


@frappe.whitelist(methods=["POST"])
def ensure_staff_customer(employee: Optional[str] = None) -> Dict[str, Any]:
    """Resolve (adopt / create if needed) the staff customer for ``employee``.

    Success: ``{"success": true, "hrms_available": true, "created": bool,
    "action": "existing"|"adopted"|"created", "customer": {search_customers row
    + "employee", "employee_name", "is_staff_customer": true}, "conflicts": [...]}``.

    Failure (unknown / inactive employee, schema not migrated, insert error):
    ``{"success": false, "error": "<message>"}``. A permission failure is NOT
    folded into that shape -- it raises ``frappe.PermissionError`` (HTTP 403),
    like every other manager-gated POS endpoint.
    """
    _ensure_staff_order_access()

    if not hrms_available():
        return {
            "success": True,
            "hrms_available": False,
            "created": False,
            "action": None,
            "customer": None,
            "conflicts": [],
        }

    employee = str(employee or "").strip()
    messages_before = staff_customers.message_log_length()
    attempts = 2
    for attempt in range(attempts):
        try:
            result = staff_customers.ensure_customer_for_employee(employee)
            row = _customer_row(result["customer"])
            employee_name = (
                frappe.db.get_value("Employee", employee, "employee_name") or employee
            )
            break
        except Exception as exc:
            # The resolution may have half-written (e.g. an insert that failed
            # after the group was created); none of it is wanted.
            try:
                frappe.db.rollback()
            except Exception:
                pass
            # Two managers creating staff customers for two DIFFERENT new
            # employees in the same instant can deadlock on the custom_employee
            # index gap (both locking reads take the gap, both then insert into
            # it). InnoDB kills one; a single retry resolves it.
            if attempt < attempts - 1 and _is_deadlock(exc):
                continue
            # The error travels in the payload; do not also pop a dialog for it.
            staff_customers.withdraw_messages_since(messages_before)
            message = str(exc).strip() or type(exc).__name__
            try:
                frappe.log_error(
                    title=f"ensure_staff_customer failed for {employee or '?'}"[:140],
                    message=frappe.get_traceback(),
                )
            except Exception:
                pass
            return {"success": False, "error": message}

    row["employee"] = employee
    row["employee_name"] = str(employee_name)
    row["is_staff_customer"] = True

    return {
        "success": True,
        "hrms_available": True,
        "created": result["action"] == staff_customers.ACTION_CREATED,
        "action": result["action"],
        "customer": row,
        "conflicts": list(result.get("conflicts") or []),
    }


@frappe.whitelist(methods=["POST"])
def sync_staff_customers() -> Dict[str, Any]:
    """Ensure a staff customer for every Active employee.

    ``{"success": true, "hrms_available": bool, "created": [...], "adopted":
    [...], "existing": [...], "skipped": [{"employee", "reason"}], "conflicts":
    [{"employee", "customers": [...]}]}``. The three action lists hold
    ``{"employee", "employee_name", "customer"}``.
    """
    _ensure_staff_order_access()

    if not hrms_available():
        return {
            "success": True,
            "hrms_available": False,
            "created": [],
            "adopted": [],
            "existing": [],
            "skipped": [],
            "conflicts": [],
        }

    summary = staff_customers.ensure_customers_for_all_employees()
    return {"success": True, "hrms_available": True, **summary}

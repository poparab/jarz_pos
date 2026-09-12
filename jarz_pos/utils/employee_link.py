"""Employee <-> User <-> Customer resolution for the employee ledger.

Two unrelated things in this app are "about an employee" and have to be added
up in one place: an **Employee Advance** (HRMS, party is the ``Employee``) and
an **Employee-purpose POS order** (jarz_pos, party is the ``Customer``). The
manager dashboard shows one balance per person, so something has to join those
two party spaces. That is this module, and it is deliberately the only place
that knows how.

The join is an explicit ``Customer.custom_employee`` Link, seeded by
``jarz_pos.setup.employee_link_setup``. Name matching is kept only as a
fallback for the customers that predate the field, and it is scoped to the
``Employee`` customer group so a same-named retail customer can never be
mistaken for staff.

**HRMS is not a hard dependency.** ``hooks.py`` declares no ``required_apps``
and this app already probes HRMS tables behind try/except in
``api/recurring_expenses.py``. Every entry point here answers safely when HRMS
is absent (``hrms_available()`` is False, lists come back empty) so a bench
without HRMS still migrates and still serves the POS.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import frappe

#: The ``Sales Invoice.custom_order_purpose`` value that marks a staff order.
#: Must stay in lockstep with the ``Jarz Commercial Policy.order_purpose``
#: option of the same name.
EMPLOYEE_ORDER_PURPOSE = "Employee"

#: Customer Group that scopes the name-matching fallback below.
EMPLOYEE_CUSTOMER_GROUP = "Employee"

#: The explicit join, added to Customer by ``setup/employee_link_setup.py``.
CUSTOMER_EMPLOYEE_FIELD = "custom_employee"

#: HRMS DocType this module reads. Its absence is what ``hrms_available()``
#: reports.
ADVANCE_DOCTYPE = "Employee Advance"

#: The two jarz-owned columns on ``Employee Advance`` that record a recovery
#: made from a salary payment on the Monthly Expenses board. They are CREATED by
#: ``jarz_pos.setup.employee_link_setup.ADVANCE_FIELDS`` and READ here and in
#: ``jarz_pos.api.monthly_expenses``; the names live in this module because this
#: is already the single place that knows how employee money is joined, and a
#: fieldname spelled in three files is a fieldname that will eventually be
#: spelled three ways.
#:
#: They exist at all because HRMS's own ``claimed_amount`` is not writable in
#: any lasting sense — ``EmployeeAdvance.update_claimed_amount`` recomputes it
#: from ``Expense Claim Advance`` rows on the next touch — and its supported
#: salary-recovery route runs through an ``Additional Salary`` consumed by a
#: Salary Slip, which this business has never produced one of.
F_SETTLED_AMOUNT = "custom_jarz_settled_amount"
F_SETTLED_VIA = "custom_jarz_settled_via"

#: HRMS's own columns that make up the rest of the balance below.
F_PAID_AMOUNT = "paid_amount"
F_CLAIMED_AMOUNT = "claimed_amount"
F_RETURN_AMOUNT = "return_amount"


def hrms_available() -> bool:
    """True when the HRMS app's Employee Advance DocType is present.

    Callers use this to degrade gracefully rather than throw: an endpoint
    returns ``hrms_available: False`` with an empty list, and the client renders
    an explanatory empty state instead of an error tile.
    """
    try:
        return bool(frappe.db.exists("DocType", ADVANCE_DOCTYPE))
    except Exception:
        return False


def customer_has_employee_field() -> bool:
    """True when ``Customer.custom_employee`` has actually been created.

    The field is seeded on ``after_migrate``. Between installing this code and
    the first migrate it does not exist, and filtering on a missing column
    raises -- so every read of it goes through this guard.
    """
    try:
        return bool(frappe.get_meta("Customer").get_field(CUSTOMER_EMPLOYEE_FIELD))
    except Exception:
        return False


def resolve_employee_for_user(user: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The active ``Employee`` behind a login, or ``None``.

    ``None`` is a normal answer -- plenty of logins have no Employee record.
    Mirrors ``services.courier_identity.resolve_courier_party`` in filtering on
    ``status="Active"``: a resigned employee must not keep drawing advances.
    """
    resolved = str(user or frappe.session.user or "").strip()
    if not resolved or resolved == "Guest":
        return None
    try:
        rows = (
            frappe.get_all(
                "Employee",
                filters={"user_id": resolved, "status": "Active"},
                fields=["name", "employee_name", "branch", "company", "department"],
                order_by="modified desc",
                limit=1,
            )
            or []
        )
    except Exception:
        # Never fail the caller on a lookup error, but never fail invisibly.
        frappe.log_error(
            frappe.get_traceback(), f"Failed to resolve Employee for {resolved}"
        )
        return None
    if not rows:
        return None
    row = rows[0]
    return {
        "employee": row["name"],
        "employee_name": str(row.get("employee_name") or row["name"]),
        "branch": str(row.get("branch") or ""),
        "company": str(row.get("company") or ""),
        "department": str(row.get("department") or ""),
        "user": resolved,
    }


def list_active_employees(
    branch: Optional[str] = None,
    search: Optional[str] = None,
    company: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Active employees, for the advance-request employee picker.

    ``branch`` filters on ``Employee.branch`` only when the caller asks for it.
    An unset ``branch`` on every Employee is common on this site, and filtering
    on it then returns nothing and reads as "no employees exist" -- so callers
    should pass it only when they know it is populated.
    """
    filters: Dict[str, Any] = {"status": "Active"}
    if company:
        filters["company"] = company
    if branch:
        filters["branch"] = branch
    or_filters = None
    if search:
        term = str(search).strip()
        if term:
            or_filters = [
                ["employee_name", "like", f"%{term}%"],
                ["name", "like", f"%{term}%"],
            ]
    try:
        rows = (
            frappe.get_all(
                "Employee",
                filters=filters,
                or_filters=or_filters,
                fields=[
                    "name",
                    "employee_name",
                    "branch",
                    "company",
                    "department",
                    "designation",
                    "user_id",
                ],
                order_by="employee_name asc",
                limit_page_length=max(1, min(int(limit or 500), 2000)),
            )
            or []
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Failed to list active employees")
        return []

    return [
        {
            "employee": r["name"],
            "employee_name": str(r.get("employee_name") or r["name"]),
            "branch": str(r.get("branch") or ""),
            "company": str(r.get("company") or ""),
            "department": str(r.get("department") or ""),
            "designation": str(r.get("designation") or ""),
            "user": str(r.get("user_id") or ""),
        }
        for r in rows
    ]


def customers_for_employees(employees: Iterable[str]) -> Dict[str, str]:
    """Map ``employee -> customer`` for the given employees.

    Explicit ``Customer.custom_employee`` links win. Anything still unmatched
    falls back to a name comparison inside the ``Employee`` customer group.

    When several Customers link the same Employee the choice is DETERMINISTIC:
    a Customer in the ``Employee`` group beats one outside it, then the oldest
    ``creation`` wins, then ``name``. That is exactly the order
    ``services.employee_customers.ensure_customer_for_employee`` uses to pick
    the staff customer the POS bills, so the customer an Employee Order is
    placed on and the customer the salary board attributes are the same one.
    (It used to be "first row the database returned", with no ``order_by``.)
    """
    wanted = [str(e).strip() for e in (employees or []) if str(e or "").strip()]
    if not wanted:
        return {}

    mapping: Dict[str, str] = {}

    if customer_has_employee_field():
        try:
            rows = (
                frappe.get_all(
                    "Customer",
                    filters={CUSTOMER_EMPLOYEE_FIELD: ["in", wanted]},
                    fields=["name", CUSTOMER_EMPLOYEE_FIELD, "customer_group"],
                    order_by="creation asc, name asc",
                    limit_page_length=0,
                )
                or []
            )
            # Stable sort: the database already ordered by creation, name, so
            # moving Employee-group rows to the front keeps that order inside
            # each partition.
            rows = sorted(
                rows,
                key=lambda r: 0
                if str(r.get("customer_group") or "").strip() == EMPLOYEE_CUSTOMER_GROUP
                else 1,
            )
            for row in rows:
                emp = str(row.get(CUSTOMER_EMPLOYEE_FIELD) or "").strip()
                # First link wins in the order above; a second Customer pointing
                # at the same Employee is a data problem, not something to
                # silently merge.
                if emp and emp not in mapping:
                    mapping[emp] = row["name"]
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "Failed to read Customer.custom_employee links"
            )

    missing = [e for e in wanted if e not in mapping]
    if not missing:
        return mapping

    try:
        names = frappe.get_all(
            "Employee",
            filters={"name": ["in", missing]},
            fields=["name", "employee_name"],
            limit_page_length=0,
        )
        by_lower_name = {
            str(r.get("employee_name") or "").strip().lower(): r["name"]
            for r in names
            if str(r.get("employee_name") or "").strip()
        }
        if by_lower_name:
            customers = frappe.get_all(
                "Customer",
                filters={"customer_group": EMPLOYEE_CUSTOMER_GROUP},
                fields=["name", "customer_name"],
                limit_page_length=0,
            )
            for cust in customers:
                key = str(cust.get("customer_name") or "").strip().lower()
                emp = by_lower_name.get(key)
                if emp and emp not in mapping:
                    mapping[emp] = cust["name"]
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "Failed name-matching employees to customers"
        )

    return mapping


def employees_for_customers(customers: Iterable[str]) -> Dict[str, str]:
    """Map ``customer -> employee``; the inverse of :func:`customers_for_employees`.

    Used when walking employee-purpose invoices, where the invoice knows the
    Customer and the ledger needs the Employee.
    """
    wanted = [str(c).strip() for c in (customers or []) if str(c or "").strip()]
    if not wanted:
        return {}

    mapping: Dict[str, str] = {}

    if customer_has_employee_field():
        try:
            rows = (
                frappe.get_all(
                    "Customer",
                    filters={"name": ["in", wanted]},
                    fields=["name", CUSTOMER_EMPLOYEE_FIELD],
                    limit_page_length=0,
                )
                or []
            )
            for row in rows:
                emp = str(row.get(CUSTOMER_EMPLOYEE_FIELD) or "").strip()
                if emp:
                    mapping[row["name"]] = emp
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), "Failed to read Customer.custom_employee links"
            )

    missing = [c for c in wanted if c not in mapping]
    if not missing:
        return mapping

    try:
        cust_rows = frappe.get_all(
            "Customer",
            filters={"name": ["in", missing]},
            fields=["name", "customer_name"],
            limit_page_length=0,
        )
        wanted_names = {
            str(r.get("customer_name") or "").strip().lower(): r["name"]
            for r in cust_rows
            if str(r.get("customer_name") or "").strip()
        }
        if wanted_names:
            emp_rows = frappe.get_all(
                "Employee",
                filters={"status": "Active"},
                fields=["name", "employee_name"],
                limit_page_length=0,
            )
            for emp in emp_rows:
                key = str(emp.get("employee_name") or "").strip().lower()
                cust = wanted_names.get(key)
                if cust and cust not in mapping:
                    mapping[cust] = emp["name"]
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "Failed name-matching customers to employees"
        )

    return mapping


def advance_has_field(fieldname: str) -> bool:
    """True when ``Employee Advance`` really carries ``fieldname``.

    Same guard, same reason as :func:`customer_has_employee_field`: the two
    ``custom_jarz_settled_*`` columns are seeded on ``after_migrate``, so between
    deploying this code and the first migrate they do not exist — and selecting
    or filtering on a missing column raises rather than returning nothing.
    """
    if not hrms_available():
        return False
    try:
        return bool(frappe.get_meta(ADVANCE_DOCTYPE).get_field(fieldname))
    except Exception:
        return False


def advance_balance_fields() -> List[str]:
    """The fields to select so :func:`open_advance_balance` can be computed.

    ``F_SETTLED_AMOUNT`` is included only when the column exists, which is what
    makes every caller safe on a bench that has the code but not yet the field.
    """
    fields = ["name", F_PAID_AMOUNT, F_CLAIMED_AMOUNT, F_RETURN_AMOUNT]
    if advance_has_field(F_SETTLED_AMOUNT):
        fields.append(F_SETTLED_AMOUNT)
    if advance_has_field(F_SETTLED_VIA):
        fields.append(F_SETTLED_VIA)
    return fields


def open_advance_balance(row: Dict[str, Any]) -> float:
    """What is still owed back on one submitted advance, floored at 0.

    ``paid - claimed - returned``. ``custom_jarz_settled_amount`` is NOT
    subtracted: HRMS's ``EmployeeAdvance.set_total_advance_paid`` derives
    ``return_amount`` from every Advance Payment Ledger Entry against the
    advance that is not an Expense Claim, and the salary-settlement Journal
    Entry in ``api/monthly_expenses`` creates one. Subtracting the jarz column
    too would count the same recovery twice — which a partial settlement would
    turn into writing off the unrecovered half. The column is provenance (how
    much of the return came from a salary deduction rather than returned cash),
    not a term in the balance.

    Floored because a negative "open balance" is not money the employee is
    owed; it is an over-recovery, and letting it go negative would silently
    cancel out another advance's real balance when the two are summed.

    A missing key reads as 0, so the same function works whether or not the
    jarz columns exist yet on this bench.
    """

    def _num(key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except Exception:
            return 0.0

    balance = (
        _num(F_PAID_AMOUNT)
        - _num(F_CLAIMED_AMOUNT)
        - _num(F_RETURN_AMOUNT)
    )
    return balance if balance > 0 else 0.0


def open_advance_balance_expr(alias: str = "") -> str:
    """The same arithmetic as SQL, for callers that aggregate in the database.

    ``alias`` is the table alias to qualify the columns with (``""`` for none).
    The settled term is dropped when the column is absent, so this expression is
    valid on a bench where the migrate has not run yet — which is exactly the
    case the Python helper above also covers.
    """
    prefix = f"{alias}." if alias else ""
    terms = [
        f"IFNULL({prefix}`{F_PAID_AMOUNT}`, 0)",
        f"IFNULL({prefix}`{F_CLAIMED_AMOUNT}`, 0)",
        f"IFNULL({prefix}`{F_RETURN_AMOUNT}`, 0)",
    ]
    # No settled term, for the reason spelled out in `open_advance_balance`:
    # HRMS has already folded the settlement into `return_amount`.
    return "GREATEST(" + " - ".join(terms) + ", 0)"


def employee_display_names(employees: Iterable[str]) -> Dict[str, str]:
    """``employee -> employee_name``, for labelling rows the caller already has."""
    wanted = [str(e).strip() for e in (employees or []) if str(e or "").strip()]
    if not wanted:
        return {}
    try:
        rows = frappe.get_all(
            "Employee",
            filters={"name": ["in", wanted]},
            fields=["name", "employee_name", "branch"],
            limit_page_length=0,
        )
    except Exception:
        return {}
    return {r["name"]: str(r.get("employee_name") or r["name"]) for r in rows}

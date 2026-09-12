"""One canonical Customer per Active Employee, for staff purchases.

WHY this exists
---------------
A staff purchase is an ``Employee Order`` (``Jarz Commercial Policy``): Employee
price list, no shipping, no courier, handed over at the counter, and left as an
unpaid receivable on the Customer so it can be recovered from salary. The money
is only recoverable if the Customer can be joined back to the Employee, and that
join is ``Customer.custom_employee`` (see ``jarz_pos.utils.employee_link``).

On production that join was close to useless: 48 links, many misused (one
employee linked to 25 customers who are not her), and only 2 customers in the
``Employee`` group. Cashiers picked whatever retail customer looked right, so
``api/monthly_expenses`` reported most staff orders as "unattributed" and
deducted nothing.

This module makes the right customer the easy one to pick: every Active employee
gets exactly one Customer in the ``Employee`` group carrying ``custom_employee``,
and the POS picks the *employee*, never a customer.

Resolution order (``ensure_customer_for_employee``)
---------------------------------------------------
a. **existing** -- a Customer with ``custom_employee = employee`` in the Employee
   group. Oldest ``creation`` wins (then ``name``), the same order
   ``employee_link.customers_for_employees`` now uses, so the customer the POS
   bills and the customer the salary board attributes are always the same one.
b. **adopted** -- else exactly ONE Employee-group Customer with an empty
   ``custom_employee`` whose ``customer_name`` matches the employee's name
   (trimmed, case-insensitive), and no other Active employee shares that name.
   Two or more candidates is a guess, so it falls through to (c).
c. **created** -- else a new Individual Customer in the Employee group.

Customers OUTSIDE the Employee group that carry ``custom_employee = employee``
are the misused links above. They are never modified -- only reported back as
``conflicts`` for a human to clean up.

Concurrency
-----------
Two cashiers can tap the same employee at once. Every resolution first takes a
row lock on the Employee (``SELECT ... FOR UPDATE``), so for one employee the
calls run one after another. MariaDB's REPEATABLE READ would still show the
second call the snapshot taken before the first one committed, so the rule (a)
read is itself a locking read, which always sees the latest committed rows.
``Customer.custom_employee`` carries ``search_index`` (seeded by
``setup/employee_link_setup.py``) so that locking read locks only this
employee's index entries, not every Customer row. Adoption is a compare-and-set
``UPDATE ... WHERE custom_employee is empty``, so two different employees with
the same name can never both claim one customer.

WooCommerce
-----------
A staff customer must never be pushed to the web store. The Woo app's outbound
Customer hooks honour ``doc.flags.ignore_woo_outbound`` and
``frappe.flags.ignore_woo_outbound``; both are set around the insert. Setting a
flag is not an import -- the two apps stay independent. Adoption writes one
column with SQL and fires no document hooks at all.

HRMS is not a hard dependency of this app: everything here is guarded on
``hrms_available()`` and ``customer_has_employee_field()``.

No top-level frappe calls -- the module imports cleanly at boot.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import frappe

from jarz_pos.utils.employee_link import (
    CUSTOMER_EMPLOYEE_FIELD,
    EMPLOYEE_CUSTOMER_GROUP,
    customer_has_employee_field,
    hrms_available,
)
from jarz_pos.utils.phone import normalize_phone, phone_variants

ACTION_EXISTING = "existing"
ACTION_ADOPTED = "adopted"
ACTION_CREATED = "created"

ACTIVE_STATUS = "Active"

#: Parent node for the Employee group when it has to be created here. Same
#: parent ``setup/b2b_master_data.py`` uses when it seeds the group.
ROOT_CUSTOMER_GROUP = "All Customer Groups"

_HOOK_SAVEPOINT = "jarz_staff_customer_hook"
_INSERT_SAVEPOINT = "jarz_staff_customer_insert"
_BULK_SAVEPOINT = "jarz_staff_customer_bulk"

_F = CUSTOMER_EMPLOYEE_FIELD


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(value: Any) -> str:
    return str(value or "").strip()


def _safe_log_error(message: str, title: str) -> None:
    """``frappe.log_error`` can itself raise (v16); a failure log must not."""
    try:
        frappe.log_error(title=title[:140], message=message or title)
    except Exception:
        pass


def _traceback() -> str:
    try:
        return frappe.get_traceback() or ""
    except Exception:
        return ""


def _exception_message(exc: BaseException) -> str:
    text = _clean(str(exc))
    return text or type(exc).__name__


def message_log_length() -> Optional[int]:
    """How many browser messages are queued right now (``None`` if unknown).

    ``frappe.throw`` queues its text in ``frappe.local.message_log`` before it
    raises. A caller that catches the exception and answers normally must take
    that message back, or the client shows an error dialog for a call that
    succeeded -- see :func:`withdraw_messages_since`.
    """
    try:
        message_log = getattr(frappe.local, "message_log", None)
        return len(message_log) if isinstance(message_log, list) else None
    except Exception:
        return None


def withdraw_messages_since(count: Optional[int]) -> None:
    """Drop browser messages queued after :func:`message_log_length` returned ``count``."""
    if count is None:
        return
    try:
        del frappe.local.message_log[count:]
    except Exception:
        pass


def _employee_has_column(fieldname: str) -> bool:
    try:
        return bool(frappe.db.has_column("Employee", fieldname))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Database access (kept small and named so the SQL is easy to audit)
# ─────────────────────────────────────────────────────────────────────────────

def _lock_employee(employee: str) -> Optional[Dict[str, Any]]:
    """Row-lock the Employee and return its current committed values."""
    columns = ["name", "employee_name", "status"]
    if _employee_has_column("cell_number"):
        columns.append("cell_number")
    select = ", ".join(f"`{c}`" for c in columns)
    rows = frappe.db.sql(
        f"SELECT {select} FROM `tabEmployee` WHERE `name` = %s FOR UPDATE",
        (employee,),
        as_dict=True,
    ) or []
    return dict(rows[0]) if rows else None


def _linked_customers_locked(employee: str) -> List[Dict[str, Any]]:
    """Every Customer pointing at ``employee``, oldest first, via a LOCKING read.

    The locking read is what defeats the REPEATABLE READ snapshot: it returns
    the latest committed rows even if this transaction's snapshot predates them.
    """
    rows = frappe.db.sql(
        f"SELECT `name`, `customer_name`, `customer_group` FROM `tabCustomer` "
        f"WHERE `{_F}` = %s ORDER BY `creation` ASC, `name` ASC FOR UPDATE",
        (employee,),
        as_dict=True,
    ) or []
    return [dict(r) for r in rows]


def _name_match_candidates(employee_name: str) -> List[str]:
    """Unlinked Employee-group customers named like the employee (max 2 -- enough
    to tell "exactly one" from "ambiguous")."""
    key = _clean(employee_name).lower()
    if not key:
        return []
    rows = frappe.db.sql(
        f"SELECT `name` FROM `tabCustomer` "
        f"WHERE `customer_group` = %s AND IFNULL(`{_F}`, '') = '' "
        f"AND LOWER(TRIM(`customer_name`)) = %s "
        f"ORDER BY `creation` ASC, `name` ASC LIMIT 2",
        (EMPLOYEE_CUSTOMER_GROUP, key),
        as_dict=True,
    ) or []
    return [r["name"] for r in rows]


def _name_shared_with_another_employee(employee: str, employee_name: str) -> bool:
    """True when another Active employee carries the same name.

    Adopting a customer by name is only safe when the name identifies one
    person; otherwise the customer might be the other one's.
    """
    try:
        others = frappe.get_all(
            "Employee",
            filters={
                "status": ACTIVE_STATUS,
                "name": ["!=", employee],
                "employee_name": _clean(employee_name),
            },
            pluck="name",
            limit=1,
        ) or []
    except Exception:
        # Unknown -> treat as ambiguous. Creating a fresh customer is always safe.
        return True
    return bool(others)


def _claim_customer(customer: str, employee: str) -> bool:
    """Compare-and-set ``custom_employee`` on an unlinked customer.

    ``UPDATE ... WHERE custom_employee is empty`` reads the latest committed row,
    so if anyone linked it first nothing is written. The locking re-read then
    tells us who actually holds it.
    """
    frappe.db.sql(
        f"UPDATE `tabCustomer` SET `{_F}` = %s, `modified` = %s, `modified_by` = %s "
        f"WHERE `name` = %s AND IFNULL(`{_F}`, '') = ''",
        (employee, frappe.utils.now(), frappe.session.user, customer),
    )
    rows = frappe.db.sql(
        f"SELECT `{_F}` FROM `tabCustomer` WHERE `name` = %s FOR UPDATE",
        (customer,),
        as_dict=True,
    ) or []
    return bool(rows) and _clean(rows[0].get(_F)) == employee


def _record_adoption(customer: str, employee: str, employee_name: str) -> None:
    """Best-effort audit trail on the adopted customer; never raises."""
    try:
        frappe.clear_document_cache("Customer", customer)
    except Exception:
        pass
    try:
        comment = frappe.get_doc(
            {
                "doctype": "Comment",
                "comment_type": "Info",
                "reference_doctype": "Customer",
                "reference_name": customer,
                "content": (
                    f"Linked to Employee {employee} ({employee_name}) as the staff "
                    f"customer used for Employee Orders."
                ),
            }
        )
        comment.insert(ignore_permissions=True)
    except Exception:
        pass


def _mobile_for_new_customer(cell_number: Any) -> Optional[str]:
    """The employee's number in canonical form -- only if nobody else holds it.

    Mirrors the duplicate-mobile guard in ``api/customer.create_customer``
    (every stored spelling, via ``utils.phone.phone_variants``). A Contact
    holding the number also counts: ERPNext creates a primary Contact from
    ``Customer.mobile_no`` on insert, and a second Contact with the same number
    is exactly the duplicate that guard exists to stop. Any doubt -> blank,
    which is always safe.
    """
    canonical = normalize_phone(_clean(cell_number))
    if not canonical:
        return None
    forms = phone_variants(cell_number) or [canonical]
    try:
        if frappe.get_all("Customer", filters={"mobile_no": ["in", forms]}, pluck="name", limit=1):
            return None
        if frappe.db.has_column("Customer", "phone") and frappe.get_all(
            "Customer", filters={"phone": ["in", forms]}, pluck="name", limit=1
        ):
            return None
        if frappe.get_all("Contact", filters={"mobile_no": ["in", forms]}, pluck="name", limit=1):
            return None
    except Exception:
        return None
    return canonical


def _ensure_employee_group() -> None:
    if frappe.db.exists("Customer Group", EMPLOYEE_CUSTOMER_GROUP):
        return
    parent = ROOT_CUSTOMER_GROUP if frappe.db.exists("Customer Group", ROOT_CUSTOMER_GROUP) else None
    doc = frappe.get_doc(
        {
            "doctype": "Customer Group",
            "customer_group_name": EMPLOYEE_CUSTOMER_GROUP,
            "parent_customer_group": parent,
            "is_group": 0,
        }
    )
    doc.insert(ignore_permissions=True)


def _insert_customer_doc(employee: str, employee_name: str, mobile: Optional[str]) -> str:
    payload: Dict[str, Any] = {
        "doctype": "Customer",
        "customer_name": employee_name,
        "customer_type": "Individual",
        "customer_group": EMPLOYEE_CUSTOMER_GROUP,
        _F: employee,
    }
    if mobile:
        payload["mobile_no"] = mobile
    # Territory is deliberately left to ERPNext's default: this app has no clean
    # Employee.branch -> Territory mapping, and a staff order never travels.
    doc = frappe.get_doc(payload)
    doc.flags.ignore_woo_outbound = True

    # Also the global flag: ERPNext creates the primary Contact from mobile_no
    # inside this insert, and that document does not carry our doc flag.
    previous = getattr(frappe.flags, "ignore_woo_outbound", None)
    frappe.flags.ignore_woo_outbound = True
    try:
        doc.insert(ignore_permissions=True)
    finally:
        frappe.flags.ignore_woo_outbound = previous
    return doc.name


def _create_customer(employee: str, employee_name: str, cell_number: Any) -> str:
    """Insert the staff customer. Retries once WITHOUT the mobile number if the
    first attempt fails with one: a malformed ``cell_number`` must not stop a
    staff member from being able to buy."""
    _ensure_employee_group()
    mobile = _mobile_for_new_customer(cell_number)
    attempts: List[Optional[str]] = [mobile, None] if mobile else [None]

    for index, candidate in enumerate(attempts):
        savepoint = _INSERT_SAVEPOINT
        try:
            frappe.db.savepoint(savepoint)
        except Exception:
            savepoint = ""
        try:
            return _insert_customer_doc(employee, employee_name, candidate)
        except Exception:
            if savepoint:
                try:
                    frappe.db.rollback(save_point=savepoint)
                except Exception:
                    pass
            if index == len(attempts) - 1:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


def _result(customer: str, action: str, conflicts: List[str]) -> Dict[str, Any]:
    return {"customer": customer, "action": action, "conflicts": list(conflicts)}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def ensure_customer_for_employee(employee: str) -> Dict[str, Any]:
    """Return (and if needed adopt or create) the staff customer of ``employee``.

    Returns ``{"customer": name, "action": "existing"|"adopted"|"created",
    "conflicts": [non-Employee-group customers also linked to this employee]}``.

    Raises (``frappe.throw``) when HRMS or the link field is missing, when the
    Employee does not exist, or when it is not Active. Callers gate roles; the
    customer is written with ``ignore_permissions``.
    """
    employee = _clean(employee)
    if not employee:
        frappe.throw("Employee is required.")
    if not hrms_available():
        frappe.throw("HRMS is not installed on this site, so staff customers cannot be resolved.")
    if not customer_has_employee_field():
        frappe.throw(
            "Customer.custom_employee does not exist on this site yet. "
            "Run bench migrate, then try again."
        )

    row = _lock_employee(employee)
    if not row:
        frappe.throw(f"Employee '{employee}' does not exist.")

    status = _clean(row.get("status"))
    if status != ACTIVE_STATUS:
        frappe.throw(
            f"Employee '{employee}' is {status or 'not Active'}. Staff orders can only "
            f"be placed for Active employees."
        )

    employee_name = _clean(row.get("employee_name")) or employee

    linked = _linked_customers_locked(employee)
    conflicts = [r["name"] for r in linked if _clean(r.get("customer_group")) != EMPLOYEE_CUSTOMER_GROUP]

    # (a) existing
    for r in linked:
        if _clean(r.get("customer_group")) == EMPLOYEE_CUSTOMER_GROUP:
            return _result(r["name"], ACTION_EXISTING, conflicts)

    # (b) adopted
    candidates = _name_match_candidates(employee_name)
    if len(candidates) == 1 and not _name_shared_with_another_employee(employee, employee_name):
        if _claim_customer(candidates[0], employee):
            _record_adoption(candidates[0], employee, employee_name)
            return _result(candidates[0], ACTION_ADOPTED, conflicts)

    # (c) created
    customer = _create_customer(employee, employee_name, row.get("cell_number"))
    return _result(customer, ACTION_CREATED, conflicts)


def canonical_customers_for_employees(employees: Iterable[str]) -> Dict[str, Dict[str, str]]:
    """``employee -> {"customer", "customer_name"}`` for rule (a) only.

    A plain (non-locking) read for listing screens. Employees with no
    Employee-group customer are simply absent from the map.
    """
    wanted = sorted({_clean(e) for e in (employees or []) if _clean(e)})
    if not wanted or not customer_has_employee_field():
        return {}
    try:
        rows = frappe.get_all(
            "Customer",
            filters={_F: ["in", wanted], "customer_group": EMPLOYEE_CUSTOMER_GROUP},
            fields=["name", "customer_name", _F],
            order_by="creation asc, name asc",
            limit_page_length=0,
        ) or []
    except Exception:
        _safe_log_error(_traceback(), "employee_customers: canonical customer read failed")
        return {}

    mapping: Dict[str, Dict[str, str]] = {}
    for r in rows:
        emp = _clean(r.get(_F))
        if emp and emp not in mapping:
            mapping[emp] = {
                "customer": r["name"],
                "customer_name": _clean(r.get("customer_name")) or r["name"],
            }
    return mapping


def ensure_customers_for_all_employees() -> Dict[str, Any]:
    """Run :func:`ensure_customer_for_employee` over every Active employee.

    Returns ``{"created", "adopted", "existing"}`` as lists of
    ``{"employee", "employee_name", "customer"}``, ``"skipped"`` as
    ``[{"employee", "reason"}]`` and ``"conflicts"`` as
    ``[{"employee", "customers": [...]}]``.

    One employee failing never stops the others: each is fenced by a savepoint
    and committed on success, which also keeps the Customer row locks short.
    Never raises.
    """
    summary: Dict[str, Any] = {
        ACTION_CREATED: [],
        ACTION_ADOPTED: [],
        ACTION_EXISTING: [],
        "skipped": [],
        "conflicts": [],
    }
    if not hrms_available():
        summary["skipped"].append({"employee": None, "reason": "HRMS is not installed"})
        return summary
    if not customer_has_employee_field():
        summary["skipped"].append(
            {"employee": None, "reason": "Customer.custom_employee does not exist yet (run bench migrate)"}
        )
        return summary

    try:
        employees = frappe.get_all(
            "Employee",
            filters={"status": ACTIVE_STATUS},
            fields=["name", "employee_name"],
            order_by="name asc",
            limit_page_length=0,
        ) or []
    except Exception as exc:
        _safe_log_error(_traceback(), "employee_customers: listing Active employees failed")
        summary["skipped"].append({"employee": None, "reason": _exception_message(exc)})
        return summary

    for emp in employees:
        employee = _clean(emp.get("name"))
        if not employee:
            continue
        employee_name = _clean(emp.get("employee_name")) or employee

        savepoint = _BULK_SAVEPOINT
        try:
            frappe.db.savepoint(savepoint)
        except Exception:
            savepoint = ""
        messages_before = message_log_length()

        try:
            result = ensure_customer_for_employee(employee)
        except Exception as exc:
            if savepoint:
                try:
                    frappe.db.rollback(save_point=savepoint)
                except Exception:
                    pass
            # Reported in the summary instead; do not also pop an error dialog.
            withdraw_messages_since(messages_before)
            summary["skipped"].append({"employee": employee, "reason": _exception_message(exc)})
            continue

        try:
            frappe.db.commit()
        except Exception:
            _safe_log_error(_traceback(), f"employee_customers: commit failed for {employee}")

        action = result.get("action")
        if action in (ACTION_CREATED, ACTION_ADOPTED, ACTION_EXISTING):
            summary[action].append(
                {"employee": employee, "employee_name": employee_name, "customer": result.get("customer")}
            )
        if result.get("conflicts"):
            summary["conflicts"].append({"employee": employee, "customers": list(result["conflicts"])})

    return summary


def ensure_customer_on_employee_save(doc, method: Optional[str] = None) -> None:
    """``Employee`` ``after_insert`` / ``on_update`` hook. NEVER raises.

    Saving an Employee must never fail because of a customer side effect. The
    work is fenced by a savepoint, and any message ``frappe.throw`` queued for
    the browser is withdrawn, so a failure here cannot surface as a red dialog on
    an Employee that actually saved fine. Failures go to the Error Log.
    """
    try:
        status = _clean(doc.get("status") if hasattr(doc, "get") else getattr(doc, "status", None))
        employee = _clean(getattr(doc, "name", None))
        if status != ACTIVE_STATUS or not employee:
            return
        if not hrms_available() or not customer_has_employee_field():
            return
    except Exception:
        return

    messages_before = message_log_length()

    savepoint = _HOOK_SAVEPOINT
    try:
        frappe.db.savepoint(savepoint)
    except Exception:
        savepoint = ""

    try:
        ensure_customer_for_employee(employee)
    except Exception:
        if savepoint:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                pass
        withdraw_messages_since(messages_before)
        _safe_log_error(
            _traceback(),
            f"employee_customers: staff customer not ensured for Employee {employee}",
        )

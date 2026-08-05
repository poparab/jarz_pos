"""Map a logged-in user to the courier party the invoice actually names.

The POS has never modelled "courier" as a login. It stores
``Sales Invoice.custom_courier_party_type`` + ``custom_courier_party``, pointing
at an ``Employee`` or a ``Supplier``. Neither of those is a session identity, so
the courier app's very first question — "which stops are *mine*?" — has no direct
answer in the data model.

The bridge is ``Employee.user_id``. An in-house courier is an Employee whose
``user_id`` is their ERPNext login, so the run sheet is
``Sales Invoice WHERE custom_courier_party = <that employee>``.

``Supplier``-typed couriers (third-party delivery companies) have **no**
``user_id`` and therefore no login, by design: they are billed through
``Courier Transaction`` and settled, not handed an app. So this module resolves
Employees only, and returns ``None`` for everyone else rather than guessing.

Branch resolution reuses ``utils.courier_visibility.resolve_courier_branch`` so
a courier's branch is derived exactly the same way here as it is on the
assignment path — two answers to "which branch is this courier?" is how a
courier ends up able to see a branch they cannot be assigned work in.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

import frappe
from frappe import _

from jarz_pos.constants import DELIVERY_GROUPS
from jarz_pos.utils.courier_visibility import resolve_courier_branch


# ─────────────────────────────────────────────────────────────────────────────
# "Is this person actually a courier?"
# ─────────────────────────────────────────────────────────────────────────────
#
# This distinction is load-bearing and easy to get catastrophically wrong.
#
# In any real ERPNext site *most staff* are Employees with ``user_id`` set —
# cashiers, line managers, accountants. Treating "has an Employee record" as
# "is a courier" would make the ownership gate below fire on a dispatcher and
# refuse to let them stamp a delivery for the courier standing in front of them.
# Membership of the delivery Employee Group is the same signal
# ``api/couriers.py`` uses to build the courier picker, so the two agree on who
# a courier is.
#
# Every lookup here fails OPEN. Wrongly blocking a completed delivery is worse
# than wrongly allowing one that branch scoping and the doctype permission have
# already authorised.

def _delivery_employee_group_name() -> str:
    """The configured delivery Employee Group, falling back to the constant."""
    try:
        from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings

        settings = get_jarz_settings()
        configured = str(getattr(settings, "delivery_employee_group", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    return DELIVERY_GROUPS.EMPLOYEE_GROUP


def delivery_group_employees() -> Set[str]:
    """Employee names in the delivery Employee Group.

    Read via ``as_dict()`` and a scan for any child list carrying an
    ``employee`` key rather than a hard-coded child fieldname: the Employee Group
    child table has been called several different things across ERPNext
    versions, and ``api/couriers.py`` already has to probe for it.

    Cached on ``frappe.local`` (never a module-level dict — that is shared across
    users inside one worker and has caused cross-user leakage in this codebase
    before).
    """
    cache_key = "jarz_delivery_group_employees"
    cached = getattr(frappe.local, cache_key, None)
    if isinstance(cached, set):
        return cached

    employees: Set[str] = set()
    try:
        group_label = _delivery_employee_group_name()
        group_name = frappe.db.get_value(
            "Employee Group", {"employee_group_name": group_label}, "name"
        )
        if not group_name and frappe.db.exists("Employee Group", group_label):
            group_name = group_label

        if group_name:
            data = frappe.get_doc("Employee Group", group_name).as_dict() or {}
            for value in data.values():
                if not isinstance(value, list) or not value:
                    continue
                for row in value:
                    if not isinstance(row, dict):
                        continue
                    employee = str(row.get("employee") or row.get("employee_id") or "").strip()
                    if employee:
                        employees.add(employee)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "delivery_group_employees failed")
        employees = set()

    try:
        setattr(frappe.local, cache_key, employees)
    except Exception:
        pass
    return employees


def is_delivery_courier(employee: Optional[str]) -> bool:
    """True when *employee* is in the delivery Employee Group."""
    name = str(employee or "").strip()
    if not name:
        return False
    return name in delivery_group_employees()


def resolve_courier_party(user: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Return the Employee party for *user*, or ``None`` if there is none.

    ``None`` is a normal answer: not every login has an Employee record.

    Only ``Active`` employees resolve. A resigned courier keeping the ability to
    stamp deliveries is exactly the loose end this filter closes.

    The result carries ``is_delivery_courier``. **Do not treat a non-``None``
    return as "this user is a courier"** — most staff have an Employee record.
    Use :func:`resolve_delivery_courier` for that question.
    """
    resolved = str(user or frappe.session.user or "").strip()
    if not resolved or resolved == "Guest":
        return None

    try:
        rows = (
            frappe.get_all(
                "Employee",
                filters={"user_id": resolved, "status": "Active"},
                fields=["name", "employee_name", "branch"],
                order_by="modified desc",
                limit=1,
            )
            or []
        )
    except Exception:
        # Never fail the caller on a lookup error, but never fail invisibly:
        # a silent None here reads as "not a courier" and would hand a courier
        # somebody else's whole branch.
        frappe.log_error(
            frappe.get_traceback(), f"Failed to resolve courier party for {resolved}"
        )
        return None

    if not rows:
        return None

    row = rows[0]
    branch = resolve_courier_branch("Employee", row["name"], row=row)
    return {
        "party_type": "Employee",
        "party": row["name"],
        "display_name": str(row.get("employee_name") or row["name"]),
        "branch": branch or "",
        "user": resolved,
        "is_delivery_courier": is_delivery_courier(row["name"]),
    }


def resolve_delivery_courier(user: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The caller as a *courier*, or ``None`` if they are not one.

    The narrow question. ``None`` here means "this login is not a courier" —
    a dispatcher, a branch manager, an accountant, or a courier who has not been
    added to the delivery Employee Group yet. Every one of those is a legitimate
    caller of the delivery transitions, so ``None`` must never be treated as a
    permission failure.
    """
    courier = resolve_courier_party(user)
    if not courier or not courier.get("is_delivery_courier"):
        return None
    return courier


def ensure_courier_party(user: Optional[str] = None) -> Dict[str, str]:
    """:func:`resolve_courier_party`, but throws when the user has no Employee."""
    courier = resolve_courier_party(user)
    if not courier:
        frappe.throw(
            _(
                "No active Employee record is linked to this login, so no courier "
                "identity could be resolved. Ask a manager to set Employee.user_id."
            ),
            frappe.PermissionError,
            title=_("Not a Courier"),
        )
    return courier


def invoice_courier_party(inv: Any) -> Dict[str, str]:
    """The courier named on *inv*, as ``{party_type, party}`` (blank when unassigned)."""
    if inv is None:
        return {"party_type": "", "party": ""}
    getter = getattr(inv, "get", None)
    if not callable(getter):
        return {"party_type": "", "party": ""}
    return {
        "party_type": str(getter("custom_courier_party_type") or "").strip(),
        "party": str(getter("custom_courier_party") or "").strip(),
    }


def user_is_assigned_courier(inv: Any, user: Optional[str] = None) -> Optional[bool]:
    """Is *user* the courier named on *inv*?

    Three-valued on purpose:

    * ``True``  — the caller is a courier and this is their stop;
    * ``False`` — the caller is a courier and this is somebody else's stop;
    * ``None``  — the caller is not a courier at all, so the question does not
      apply. Callers must treat ``None`` as "not my business to police here",
      because dispatchers and managers legitimately stamp deliveries from the
      POS board — and, in any real site, most of them have an Employee record
      too. That is why this asks :func:`resolve_delivery_courier` rather than
      :func:`resolve_courier_party`.
    """
    courier = resolve_delivery_courier(user)
    if not courier:
        return None

    assigned = invoice_courier_party(inv)
    if not assigned["party"]:
        # Unassigned stop. A courier acting on it is a dispatch mistake, not a
        # permission breach — let the caller decide.
        return False
    return (
        assigned["party_type"] == courier["party_type"]
        and assigned["party"] == courier["party"]
    )


def assert_invoice_assigned_to_courier(
    inv: Any, *, action_label: str, user: Optional[str] = None
) -> None:
    """Refuse when a *courier* acts on an invoice assigned to another courier.

    A no-op for non-courier users (see :func:`user_is_assigned_courier`), so
    wiring this in does not break the dispatcher path.
    """
    verdict = user_is_assigned_courier(inv, user)
    if verdict is None or verdict is True:
        return

    frappe.throw(
        _("Not permitted: {0} is limited to the courier this order is assigned to.").format(
            action_label
        ),
        frappe.PermissionError,
        title=_("Wrong Courier"),
    )


def get_courier_run_filters(user: Optional[str] = None) -> Dict[str, Any]:
    """Filters selecting the caller's live stops.

    Returned rather than executed so the run-sheet query itself can live in
    ``jarz_courier`` (contract §9: that app reads freely and writes only through
    ``jarz_pos`` services) without duplicating the identity rule.
    """
    courier = ensure_courier_party(user)
    return {
        "docstatus": 1,
        "is_return": 0,
        "custom_courier_party_type": courier["party_type"],
        "custom_courier_party": courier["party"],
        "custom_sales_invoice_state": "Out for Delivery",
    }


def list_courier_users(branch: Optional[str] = None) -> List[Dict[str, str]]:
    """Couriers who actually have a login, optionally narrowed to one branch.

    Restricted to the delivery Employee Group: without that filter this returns
    every member of staff with an ERPNext account, which is not a courier list.
    """
    members = delivery_group_employees()
    if not members:
        return []

    filters: Dict[str, Any] = {
        "status": "Active",
        "user_id": ["is", "set"],
        "name": ["in", sorted(members)],
    }
    cleaned_branch = str(branch or "").strip()
    if cleaned_branch:
        filters["branch"] = cleaned_branch

    try:
        rows = (
            frappe.get_all(
                "Employee",
                filters=filters,
                fields=["name", "employee_name", "user_id", "branch"],
                order_by="employee_name asc",
            )
            or []
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "list_courier_users failed")
        return []

    return [
        {
            "party_type": "Employee",
            "party": row["name"],
            "display_name": str(row.get("employee_name") or row["name"]),
            "user": str(row.get("user_id") or ""),
            "branch": str(row.get("branch") or ""),
        }
        for row in rows
    ]

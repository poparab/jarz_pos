"""Manager Dashboard APIs for Branch Live Feed.

Endpoints:
- get_manager_dashboard_summary: list accessible POS Profiles with cash account and current balance.
- get_manager_orders: recent invoices feed filtered by branch (POS Profile) or all, optional by state.
- get_manager_states: return available Sales Invoice state options (same as Kanban columns).
- update_invoice_branch: change custom_kanban_profile for a submitted Sales Invoice (reassign branch).
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional, Union
import frappe

try:
    # ERPNext helper to get account balance as of today
    from erpnext.accounts.utils import get_balance_on  # type: ignore
except Exception:
    get_balance_on = None  # type: ignore

try:
    from jarz_pos.utils.account_utils import get_pos_cash_account
except Exception:
    def get_pos_cash_account(pos_profile: str, company: str) -> str:  # type: ignore
        # Fallback: try to resolve a Cash account roughly matching the profile name
        acc = frappe.db.get_value(
            "Account",
            {"company": company, "parent_account": ["like", "%Cash In Hand%"], "account_name": ["like", f"%{pos_profile}%"], "is_group": 0},
            "name",
        )
        if acc:
            return acc
        # last resort: company's default cash account
        return frappe.get_cached_value("Company", company, "default_cash_account") or "Cash"

try:
    from jarz_pos.api.notifications import notify_invoice_reassignment
except Exception:
    def notify_invoice_reassignment(*args, **kwargs):  # type: ignore
        return None


_ALLOWED_TRANSFER_STATES = {"received", "in progress", "ready"}


def _current_user_allowed_profiles() -> List[str]:
    """Return POS Profiles the current user can manage.

    Rules:
    - If user has role System Manager or POS Manager, return all active POS Profiles.
    - Else, return POS Profiles linked via child table POS Profile User.
    """
    user = frappe.session.user
    roles = set([r.get("role") for r in frappe.get_all("Has Role", filters={"parent": user}, fields=["role"])])
    try:
        if {"System Manager", "POS Manager"} & roles:
            return frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name") or []
    except Exception:
        pass
    try:
        linked = frappe.get_all("POS Profile User", filters={"user": user}, pluck="parent") or []
        if not linked:
            return []
        return frappe.get_all("POS Profile", filters={"name": ["in", linked], "disabled": 0}, pluck="name") or []
    except Exception:
        return []


def _get_state_field_options() -> List[str]:
    """Return list of Sales Invoice state options without reading Custom Field doc.
    Prefers 'custom_sales_invoice_state', falls back to legacy names.
    """
    try:
        meta = frappe.get_meta("Sales Invoice")
        for field_name in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            field = meta.get_field(field_name)
            if field and getattr(field, 'options', None):
                options = [opt.strip() for opt in field.options.split('\n') if opt.strip()]
                if options:
                    return options
        return []
    except Exception:
        return []


@frappe.whitelist(allow_guest=False)
def get_manager_dashboard_summary(company: Optional[str] = None) -> Dict[str, Any]:
    """Return accessible branches (POS Profiles) and their cash balances.

    Args:
        company: Optional company filter. If omitted, uses the single company of latest POS invoice or the user's default company.
    Returns:
        { success, branches: [ { name, title, cash_account, balance } ], total_balance }
    """
    profiles = _current_user_allowed_profiles()
    if not profiles:
        return {"success": True, "branches": [], "total_balance": 0.0}

    # Try to get company if not given
    if not company:
        try:
            # Latest POS SI company
            row = frappe.get_all("Sales Invoice", filters={"is_pos": 1}, fields=["company"], order_by="creation desc", limit=1)
            if row:
                company = row[0]["company"]
        except Exception:
            company = None
    balances: List[Dict[str, Any]] = []
    total = 0.0
    for p in profiles:
        try:
            cash_acc = get_pos_cash_account(p, company) if company else None
            bal = 0.0
            if cash_acc and get_balance_on:
                try:
                    bal = float(get_balance_on(account=cash_acc, date=frappe.utils.nowdate(), company=company))  # type: ignore
                except Exception:
                    bal = 0.0
            balances.append({
                "name": p,
                "title": p,
                "cash_account": cash_acc,
                "balance": bal,
            })
            total += bal
        except Exception:
            balances.append({"name": p, "title": p, "cash_account": None, "balance": 0.0})
    return {"success": True, "branches": balances, "total_balance": total}


@frappe.whitelist(allow_guest=False)
def get_manager_orders(branch: Optional[str] = None, state: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    """Return a recent feed of POS invoices for selected branch or for all accessible branches.

    Args:
      branch: POS Profile name; when omitted or 'all', includes all accessible profiles.
      limit: Max invoices to return (default 200).
    Returns:
      { success, invoices: [ ... ] }
    """
    limit = max(1, min(int(limit or 200), 500))
    allowed = _current_user_allowed_profiles()
    if not allowed:
        return {"success": True, "invoices": []}

    profiles = allowed
    if branch and branch.lower() != "all":
        if branch in allowed:
            profiles = [branch]
        else:
            # No access to requested branch
            return {"success": True, "invoices": []}

    # Prefer filtering by custom_kanban_profile; fallback to pos_profile
    try:
        si_meta = frappe.get_meta("Sales Invoice")
        branch_filter_field = "custom_kanban_profile" if si_meta.get_field("custom_kanban_profile") else "pos_profile"
    except Exception:
        branch_filter_field = "pos_profile"

    fields = [
        "name", "customer", "customer_name", "posting_date", "posting_time", "grand_total", "net_total",
        "status", branch_filter_field, "custom_sales_invoice_state", "sales_invoice_state",
    ]
    # Build filters
    filters: Dict[str, Any] = {
        branch_filter_field: ["in", profiles],
        "docstatus": 1,
        "is_pos": 1,
    }
    # Optional state filter
    try:
        state_field = "custom_sales_invoice_state" if frappe.get_meta("Sales Invoice").get_field("custom_sales_invoice_state") else (
            "sales_invoice_state" if frappe.get_meta("Sales Invoice").get_field("sales_invoice_state") else None
        )
    except Exception:
        state_field = None
    if state and state.lower() != "all" and state_field:
        # Map to canonical case from options (case-insensitive)
        try:
            options = _get_state_field_options()
            match = next((opt for opt in options if opt.lower() == state.lower()), None)
            canonical = match or state
        except Exception:
            canonical = state
        filters[state_field] = canonical

    rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=fields,
        order_by="posting_date desc, posting_time desc",
        limit=limit,
    )
    # Normalize payload
    invs: List[Dict[str, Any]] = []
    for r in rows:
        invs.append({
            "name": r.get("name"),
            "customer": r.get("customer"),
            "customer_name": r.get("customer_name") or r.get("customer"),
            "posting_date": str(r.get("posting_date")),
            "posting_time": str(r.get("posting_time")),
            "grand_total": float(r.get("grand_total") or 0),
            "net_total": float(r.get("net_total") or 0),
            "status": r.get("custom_sales_invoice_state") or r.get("sales_invoice_state") or r.get("status"),
            "branch": r.get(branch_filter_field),
        })
    return {"success": True, "invoices": invs}


@frappe.whitelist(allow_guest=False)
def get_manager_states() -> Dict[str, Any]:
    """Return available Sales Invoice states (same list used by Kanban columns)."""
    try:
        states = _get_state_field_options()
        return {"success": True, "states": states}
    except Exception as e:
        return {"success": False, "error": str(e), "states": []}


@frappe.whitelist(allow_guest=False)
def update_invoice_branch(invoice_id: str, new_branch: str) -> Dict[str, Any]:
    """Update the invoice's branch linkage by setting custom_kanban_profile.

    Rules:
    - Only for submitted POS invoices (docstatus=1 and is_pos=1).
    - new_branch must be in current user's allowed POS Profiles.
    - Field custom_kanban_profile must exist; pos_profile and kanban profile are both updated.
    """
    try:
        if not invoice_id or not new_branch:
            return {"success": False, "error": "Missing invoice_id or new_branch"}
        allowed = _current_user_allowed_profiles()
        if new_branch not in allowed:
            return {"success": False, "error": "Not allowed to assign to this branch"}
        inv = frappe.get_doc("Sales Invoice", invoice_id)
        if int(inv.get("docstatus") or 0) != 1 or int(inv.get("is_pos") or 0) != 1:
            return {"success": False, "error": "Only submitted POS invoices can be reassigned"}
        meta = frappe.get_meta("Sales Invoice")
        if not meta.get_field("custom_kanban_profile"):
            return {"success": False, "error": "custom_kanban_profile field not found on Sales Invoice"}

        current_state = (
            inv.get("custom_sales_invoice_state")
            or inv.get("sales_invoice_state")
            or inv.get("custom_state")
            or inv.get("state")
            or "Received"
        )

        if str(current_state).strip().lower() not in _ALLOWED_TRANSFER_STATES:
            return {
                "success": False,
                "error": "Invoice can only be transferred when state is Received, In Progress, or Ready",
            }

        state_fields: List[str] = []
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                state_fields.append(candidate)

        updates: Dict[str, Any] = {"custom_kanban_profile": new_branch}
        if meta.get_field("pos_profile"):
            updates["pos_profile"] = new_branch
        for field in state_fields:
            updates[field] = "Received"

        for field, value in {
            "custom_acceptance_status": "Pending",
            "custom_accepted_by": None,
            "custom_accepted_on": None,
        }.items():
            if meta.get_field(field):
                updates[field] = value

        frappe.db.set_value("Sales Invoice", inv.name, updates, update_modified=True)
        inv.reload()

        try:
            notify_invoice_reassignment(inv, new_branch)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "notify_invoice_reassignment failed during transfer")
        try:
            frappe.db.commit()
        except Exception:
            pass
        return {
            "success": True,
            "invoice_id": invoice_id,
            "new_branch": new_branch,
            "new_state": "Received",
        }
    except Exception as e:
        frappe.log_error(f"Update Invoice Branch Error: {str(e)}", "Manager API")
        return {"success": False, "error": str(e)}

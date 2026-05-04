"""Manager Dashboard APIs for Branch Live Feed.

Endpoints:
- get_manager_dashboard_summary: list accessible POS Profiles with cash account and current balance.
- get_manager_orders: recent invoices feed filtered by branch (POS Profile) or all, optional by state.
- get_manager_states: return available Sales Invoice state options (same as Kanban columns).
- update_cancelled_invoice_status_fields: update limited workflow fields on cancelled Sales Invoices.
- update_invoice_branch: reassign a submitted Sales Invoice by changing custom_kanban_profile only.
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional, Union
import frappe
from frappe import _
from jarz_pos.constants import ACCOUNTS, ROLES, WS_EVENTS

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
            {"company": company, "parent_account": ["like", f"%{ACCOUNTS.CASH_IN_HAND}%"], "account_name": ["like", f"%{pos_profile}%"], "is_group": 0},
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


# Allowed states for invoice transfer (normalized: lowercase, no extra spaces)
# These match the actual field values: "Received", "In Progress", "Ready"
# Note: "recieved" (misspelled) included for backward compatibility with existing data
_ALLOWED_TRANSFER_STATES = {"received", "recieved", "in progress", "ready", "preparing"}


def _current_user_allowed_profiles() -> List[str]:
    """Return POS Profiles the current user can manage.

    Rules:
    - If user has role System Manager or POS Manager, return all active POS Profiles.
    - Else, return POS Profiles linked via child table POS Profile User.
    """
    user = frappe.session.user
    roles = set([r.get("role") for r in frappe.get_all("Has Role", filters={"parent": user}, fields=["role"])])
    try:
        if ROLES.ADMIN & roles:
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


def _ensure_manager_dashboard_access() -> None:
    """Ensure the current user has JARZ Manager, Line Manager, or admin-level role for dashboard access."""
    roles = set(frappe.get_roles())
    allowed = ROLES.ADMIN | {"JARZ Manager", "JARZ line manager", ROLES.JARZ_LINE_MANAGER}
    if not roles.intersection(allowed):
        frappe.throw(_("Not permitted: Manager Dashboard access required"), frappe.PermissionError)


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


def _state_key(value: Optional[str]) -> Optional[str]:
    """Normalize a state label into the websocket state-key format."""
    if not value:
        return None
    return str(value).strip().lower().replace(" ", "_")


def _match_option(value: Optional[str], options: List[str]) -> Optional[str]:
    """Return the canonical option that matches value case-insensitively."""
    cleaned_value = str(value or "").strip()
    if not cleaned_value:
        return None
    for option in options:
        if option.lower() == cleaned_value.lower():
            return option
    return None


def _get_acceptance_field_options() -> List[str]:
    """Return Acceptance Status select options from Sales Invoice metadata."""
    try:
        field = frappe.get_meta("Sales Invoice").get_field("custom_acceptance_status")
        if field and getattr(field, "options", None):
            return [opt.strip() for opt in field.options.split("\n") if opt.strip()]
    except Exception:
        pass
    return []


def _publish_invoice_reassignment_refresh(
    invoice: Any,
    *,
    old_branch: Optional[str],
    new_branch: str,
    old_state: Optional[str],
    new_state: str,
) -> None:
    """Broadcast a board-refresh event for cross-session Kanban convergence."""
    invoice_summary = {
        "name": getattr(invoice, "name", None),
        "customer": invoice.get("customer"),
        "customer_name": invoice.get("customer_name"),
        "grand_total": invoice.get("grand_total"),
        "status": invoice.get("status"),
        "posting_date": str(invoice.get("posting_date")) if invoice.get("posting_date") else None,
        "posting_time": str(invoice.get("posting_time")) if invoice.get("posting_time") else None,
        "pos_profile": invoice.get("pos_profile"),
        "kanban_profile": new_branch,
    }
    payload = {
        "event": "invoice_reassigned",
        "invoice_id": getattr(invoice, "name", None),
        "old_profile": old_branch,
        "new_profile": new_branch,
        "old_state": old_state,
        "new_state": new_state,
        "old_state_key": None,
        "new_state_key": _state_key(new_state),
        "pos_profile": invoice.get("pos_profile"),
        "kanban_profile": new_branch,
        "acceptance_status": invoice.get("custom_acceptance_status"),
        "updated_by": frappe.session.user,
        "timestamp": frappe.utils.now(),
        "force_refresh": True,
        "invoice": invoice_summary,
    }
    try:
        frappe.publish_realtime(WS_EVENTS.INVOICE_STATE_CHANGE, payload, user="*")
        frappe.publish_realtime(WS_EVENTS.KANBAN_UPDATE, payload, user="*")
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Invoice reassignment realtime publish failed for {getattr(invoice, 'name', None)}",
        )


@frappe.whitelist(allow_guest=False)
def get_manager_dashboard_summary(company: Optional[str] = None) -> Dict[str, Any]:
    """Return accessible branches (POS Profiles) and their cash balances.

    Args:
        company: Optional company filter. If omitted, uses the single company of latest POS invoice or the user's default company.
    Returns:
        { success, branches: [ { name, title, cash_account, balance } ], total_balance }
    """
    _ensure_manager_dashboard_access()
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
    _ensure_manager_dashboard_access()
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
    _ensure_manager_dashboard_access()
    try:
        states = _get_state_field_options()
        return {"success": True, "states": states}
    except Exception as e:
        return {"success": False, "error": str(e), "states": []}


@frappe.whitelist(allow_guest=False)
def update_cancelled_invoice_status_fields(
    invoice_id: str,
    sales_invoice_state: Optional[str] = None,
    acceptance_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Update selected workflow fields on a cancelled Sales Invoice.

    This keeps cancelled documents immutable in general while allowing managers
    to correct the two Jarz workflow fields that still matter operationally.
    """
    _ensure_manager_dashboard_access()

    invoice_id = (invoice_id or "").strip()
    requested_state = (sales_invoice_state or "").strip()
    requested_acceptance = (acceptance_status or "").strip()

    if not invoice_id:
        return {"success": False, "error": "invoice_id is required"}
    if not requested_state and not requested_acceptance:
        return {"success": False, "error": "At least one field update is required"}

    try:
        inv = frappe.get_doc("Sales Invoice", invoice_id)
        frappe.has_permission("Sales Invoice", doc=inv, ptype="write", throw=True)

        if int(inv.get("docstatus") or 0) != 2:
            return {"success": False, "error": "Only cancelled Sales Invoices can be updated with this action"}

        meta = frappe.get_meta("Sales Invoice")
        updates: Dict[str, Any] = {}
        update_fragments: List[str] = []

        if requested_state:
            state_options = _get_state_field_options()
            canonical_state = _match_option(requested_state, state_options) if state_options else requested_state
            if not canonical_state:
                return {"success": False, "error": f"Invalid Sales Invoice State: {requested_state}"}

            state_fields = [
                field_name
                for field_name in ("custom_sales_invoice_state", "sales_invoice_state")
                if meta.get_field(field_name)
            ]
            if not state_fields:
                return {"success": False, "error": "Sales Invoice State field was not found on Sales Invoice"}

            if any(str(inv.get(field_name) or "").strip() != canonical_state for field_name in state_fields):
                for field_name in state_fields:
                    updates[field_name] = canonical_state
                update_fragments.append(f"Sales Invoice State = {canonical_state}")

        if requested_acceptance:
            acceptance_options = _get_acceptance_field_options()
            canonical_acceptance = _match_option(requested_acceptance, acceptance_options) if acceptance_options else requested_acceptance
            if not canonical_acceptance:
                return {"success": False, "error": f"Invalid Acceptance Status: {requested_acceptance}"}

            if not meta.get_field("custom_acceptance_status"):
                return {"success": False, "error": "Acceptance Status field was not found on Sales Invoice"}

            current_acceptance = str(inv.get("custom_acceptance_status") or "").strip()
            if current_acceptance != canonical_acceptance:
                updates["custom_acceptance_status"] = canonical_acceptance
                if meta.get_field("custom_accepted_by"):
                    updates["custom_accepted_by"] = frappe.session.user if canonical_acceptance.lower() == "accepted" else None
                if meta.get_field("custom_accepted_on"):
                    updates["custom_accepted_on"] = frappe.utils.now_datetime() if canonical_acceptance.lower() == "accepted" else None
                update_fragments.append(f"Acceptance Status = {canonical_acceptance}")

        if not updates:
            return {
                "success": True,
                "invoice_id": inv.name,
                "sales_invoice_state": inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state"),
                "acceptance_status": inv.get("custom_acceptance_status"),
                "no_change": True,
            }

        frappe.db.set_value("Sales Invoice", inv.name, updates, update_modified=True)
        inv.reload()

        try:
            inv.add_comment(
                "Edit",
                f"Cancelled invoice fields updated by {frappe.session.user}: {'; '.join(update_fragments)}",
            )
        except Exception:
            pass

        return {
            "success": True,
            "invoice_id": inv.name,
            "sales_invoice_state": inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state"),
            "acceptance_status": inv.get("custom_acceptance_status"),
            "accepted_by": inv.get("custom_accepted_by"),
            "accepted_on": inv.get("custom_accepted_on"),
        }
    except frappe.PermissionError:
        raise
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "update_cancelled_invoice_status_fields failed")
        return {"success": False, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def update_invoice_branch(invoice_id: str, new_branch: str) -> Dict[str, Any]:
    """Reassign a submitted POS invoice by updating custom_kanban_profile.

    This is the only supported post-submit branch transfer path.

    Rules:
    - Only for submitted POS invoices (docstatus=1 and is_pos=1).
    - The target POS Profile must exist, be enabled, and be allowed for the current user.
    - Only custom_kanban_profile and transfer-related Kanban workflow fields are updated.
    - pos_profile remains unchanged after submit.
    - new_branch must be in current user's allowed POS Profiles.
    - The reassignment touches modified and emits a realtime refresh event for other sessions.
    """
    _ensure_manager_dashboard_access()
    try:
        frappe.logger().info(f"Transfer invoice request: {invoice_id} -> {new_branch}")
        
        if not invoice_id:
            return {"success": False, "error": "invoice_id is required"}
        if not new_branch:
            return {"success": False, "error": "new_branch is required"}

        if not frappe.db.exists("Sales Invoice", invoice_id):
            return {"success": False, "error": f"Sales Invoice {invoice_id} was not found"}

        if not frappe.db.exists("POS Profile", new_branch):
            return {"success": False, "error": f"Target POS Profile {new_branch} was not found"}

        if int(frappe.db.get_value("POS Profile", new_branch, "disabled") or 0) == 1:
            return {"success": False, "error": f"Target POS Profile {new_branch} is disabled"}

        allowed = _current_user_allowed_profiles()
        if new_branch not in allowed:
            return {"success": False, "error": f"Not allowed to assign invoices into branch {new_branch}"}

        inv = frappe.get_doc("Sales Invoice", invoice_id)
        
        frappe.logger().info(f"Invoice docstatus: {inv.get('docstatus')}, is_pos: {inv.get('is_pos')}")
        
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
        current_branch = inv.get("custom_kanban_profile") or inv.get("pos_profile")

        frappe.logger().info(f"Current state: '{current_state}'")
        frappe.logger().info(
            f"Invoice transfer validated: invoice={inv.name}, user={frappe.session.user}, old_branch={current_branch}, new_branch={new_branch}, state={current_state}"
        )
        
        # Normalize the state for comparison (strip and lowercase)
        normalized_state = str(current_state).strip().lower()
        
        frappe.logger().info(f"Normalized state: '{normalized_state}', Allowed: {_ALLOWED_TRANSFER_STATES}")
        
        # Only allow transfer from Received, In Progress, or Ready states
        if normalized_state not in _ALLOWED_TRANSFER_STATES:
            frappe.log_error(
                f"Invoice {invoice_id} transfer blocked. State: '{current_state}' (normalized: '{normalized_state}'). Allowed: {_ALLOWED_TRANSFER_STATES}",
                "Invoice Transfer State Check"
            )
            return {
                "success": False,
                "error": f"Invoice can only be transferred when state is Received, In Progress, or Ready. Current state: {current_state}",
            }

        state_fields: List[str] = []
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                state_fields.append(candidate)

        # Only update custom_kanban_profile, NOT pos_profile
        # pos_profile is read-only after invoice submission and cannot be changed
        updates: Dict[str, Any] = {"custom_kanban_profile": new_branch}
        
        # Reset to Received state when transferring
        target_received = "Received"
        try:
            options = _get_state_field_options() or []
            # Prefer exact option match (case-insensitive) for Received / Recieved
            for opt in options:
                if opt.strip().lower() in {"received", "recieved"}:
                    target_received = opt.strip()
                    break
        except Exception:
            pass
        for field in state_fields:
            updates[field] = target_received

        # Reset acceptance status
        for field, value in {
            "custom_acceptance_status": "Pending",
            "custom_accepted_by": None,
            "custom_accepted_on": None,
        }.items():
            if meta.get_field(field):
                updates[field] = value

        # Handle delivery time: try to find closest matching period in new POS profile
        current_time_from = inv.get("custom_delivery_time_from")
        current_time_to = inv.get("custom_delivery_time_to") 
        current_delivery_date = inv.get("custom_delivery_date")
        
        if current_time_from and meta.get_field("custom_delivery_time_from"):
            try:
                # Get delivery periods from new POS profile
                new_profile_doc = frappe.get_doc("POS Profile", new_branch)
                delivery_periods = new_profile_doc.get("custom_delivery_periods") or []
                
                if delivery_periods:
                    # Find closest matching period based on time_from
                    from datetime import datetime, time
                    current_time = datetime.strptime(str(current_time_from), "%H:%M:%S").time() if isinstance(current_time_from, str) else current_time_from
                    
                    closest_period = None
                    min_diff = float('inf')
                    
                    for period in delivery_periods:
                        period_from = period.get("time_from")
                        if period_from:
                            period_time = datetime.strptime(str(period_from), "%H:%M:%S").time() if isinstance(period_from, str) else period_from
                            # Calculate time difference in minutes
                            diff = abs((datetime.combine(datetime.today(), current_time) - 
                                      datetime.combine(datetime.today(), period_time)).total_seconds() / 60)
                            if diff < min_diff:
                                min_diff = diff
                                closest_period = period
                    
                    if closest_period:
                        updates["custom_delivery_time_from"] = closest_period.get("time_from")
                        updates["custom_delivery_time_to"] = closest_period.get("time_to")
                        if meta.get_field("custom_delivery_duration"):
                            updates["custom_delivery_duration"] = closest_period.get("duration")
                        if meta.get_field("custom_delivery_slot_label"):
                            raw_label = closest_period.get("label") or ""
                            if raw_label:
                                updates["custom_delivery_slot_label"] = raw_label
                            else:
                                tf = closest_period.get("time_from") or ""
                                tt = closest_period.get("time_to") or ""
                                try:
                                    from datetime import datetime as _dt
                                    tf_ampm = _dt.strptime(tf.split(".")[0], "%H:%M:%S").strftime("%I:%M %p") if tf else tf
                                    tt_ampm = _dt.strptime(tt.split(".")[0], "%H:%M:%S").strftime("%I:%M %p") if tt else tt
                                    updates["custom_delivery_slot_label"] = f"{tf_ampm} - {tt_ampm}"
                                except Exception:
                                    updates["custom_delivery_slot_label"] = f"{tf} - {tt}"
            except Exception as e:
                frappe.log_error(f"Error updating delivery time during transfer: {str(e)}", "Invoice Transfer")

        # Use flags to bypass validation and permission checks for submitted invoices
        frappe.flags.ignore_permissions = True
        frappe.flags.ignore_validate = True
        
        try:
            for field, value in updates.items():
                frappe.db.set_value("Sales Invoice", inv.name, field, value, update_modified=True)
            frappe.db.commit()
        except Exception as e:
            frappe.log_error(f"Error setting values during transfer: {str(e)}\nUpdates: {updates}", "Invoice Transfer")
            frappe.db.rollback()
            return {"success": False, "error": f"Failed to update invoice fields: {str(e)}"}
        finally:
            frappe.flags.ignore_permissions = False
            frappe.flags.ignore_validate = False

        inv.reload()

        try:
            _publish_invoice_reassignment_refresh(
                inv,
                old_branch=current_branch,
                new_branch=new_branch,
                old_state=current_state,
                new_state=target_received,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Invoice reassignment realtime publish failed during transfer")

        try:
            notify_invoice_reassignment(inv, new_branch)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "notify_invoice_reassignment failed during transfer")
        try:
            frappe.db.commit()
        except Exception:
            pass
        frappe.logger().info(
            f"Invoice transfer completed: invoice={inv.name}, user={frappe.session.user}, old_branch={current_branch}, new_branch={new_branch}, old_state={current_state}, new_state={target_received}"
        )
        return {
            "success": True,
            "invoice_id": invoice_id,
            "new_branch": new_branch,
            "new_state": target_received,
        }
    except Exception as e:
        frappe.logger().error(f"Update Invoice Branch Error: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Manager API - Update Invoice Branch")
        return {"success": False, "error": str(e)}

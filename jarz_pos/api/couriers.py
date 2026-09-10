"""Jarz POS – Courier workflow API endpoints.

This module exposes courier-related operations using the refactored
delivery handling services.
"""

from __future__ import annotations

import hmac
import json

import frappe

from jarz_pos.services.delivery_handling import (
    mark_courier_outstanding as _mark_courier_outstanding,
    pay_delivery_expense as _pay_delivery_expense,
    courier_delivery_expense_only as _courier_delivery_expense_only,
    get_courier_balances as _get_courier_balances,
    settle_courier as _settle_courier,
    settle_delivery_party as _settle_delivery_party,
    settle_courier_for_invoice as _settle_courier_for_invoice,
    handle_out_for_delivery_paid as _handle_out_for_delivery_paid,
    handle_out_for_delivery_transition as _handle_out_for_delivery_transition,
    settle_single_invoice_paid as _settle_single_invoice_paid,
    settle_courier_collected_payment as _settle_courier_collected_payment,
    change_payment_collection_method as _change_payment_collection_method,
    deliver_online_unconfirmed as _deliver_online_unconfirmed,
    deliver_credit_on_account as _deliver_credit_on_account,
    list_unconfirmed_online_orders as _list_unconfirmed_online_orders,
    confirm_online_payment as _confirm_online_payment,
    convert_online_order_to_cod as _convert_online_order_to_cod,
    get_unsettle_preview as _get_unsettle_preview,
    unsettle_courier_settlement as _unsettle_courier_settlement,
    list_recent_courier_settlements as _list_recent_courier_settlements,
)
from jarz_pos.services.delivery_party import create_delivery_party as _create_delivery_party
from jarz_pos.utils.invoice_utils import normalize_woo_order_id
from jarz_pos.api.invoices import pay_invoice as _pay_invoice  # reuse payment creation
from jarz_pos.services import delivery_handling as _delivery_services
from jarz_pos.services.settlement_strategies import (
    dispatch_settlement as _dispatch_settlement,
    _is_online_intent as _is_online_intent,
    _is_credit_intent as _is_credit_intent,
)
from jarz_pos.utils.account_utils import (
    get_freight_expense_account,
    get_pos_cash_account,
    validate_account_exists,
)
from jarz_pos.utils.courier_visibility import (
    filter_available_couriers,
    get_visible_pos_profiles,
)
from frappe.utils import now_datetime, get_datetime
from jarz_pos.constants import DELIVERY_GROUPS, ROLES
from jarz_pos.utils.access_control import (
    ensure_open_shift,
    ensure_open_shift_for_invoice,
    ensure_profile_scoped_invoice_access,
)


# ---------------------------------------------------------------------------
# Entry guards
#
# This module is a thin dispatch layer over ``services.delivery_handling``, which
# makes it the natural choke point for the two questions every courier action
# must answer first: is this order in one of my branches, and is that branch
# open? Both guards run before any document is touched.
# ---------------------------------------------------------------------------


def _invoice_branch_row(invoice_name: str):
    """Cheap branch lookup — avoids loading the whole invoice just to gate it."""
    name = (invoice_name or "").strip()
    if not name:
        frappe.throw(frappe._("invoice_name is required"))
    row = frappe.db.get_value(
        "Sales Invoice",
        name,
        ["name", "custom_kanban_profile", "pos_profile"],
        as_dict=True,
    )
    if not row:
        frappe.throw(frappe._("Sales Invoice {0} was not found").format(name))
    return row


def _guard_invoice_action(
    invoice_name: str,
    *,
    action_label: str,
    require_shift: bool = True,
) -> None:
    """Branch-scope, and optionally shift-gate, an invoice-level courier action."""
    row = _invoice_branch_row(invoice_name)
    ensure_profile_scoped_invoice_access(row, action_label=action_label)
    if require_shift:
        ensure_open_shift_for_invoice(row, action_label=action_label)


def _guard_branch_action(
    pos_profile: str | None,
    *,
    action_label: str,
    require_shift: bool = True,
) -> None:
    """Branch-scope, and optionally shift-gate, a profile-level courier action."""
    profile = str(pos_profile or "").strip()
    if not profile:
        return
    ensure_profile_scoped_invoice_access(
        frappe._dict({"custom_kanban_profile": profile}),
        action_label=action_label,
    )
    if require_shift:
        ensure_open_shift(profile, action_label=action_label)


# ---------------------------------------------------------------------------
# Public, whitelisted functions
# ---------------------------------------------------------------------------


@frappe.whitelist()  # type: ignore[attr-defined]
def mark_courier_outstanding(invoice_name: str, courier: str | None = None, party_type: str | None = None, party: str | None = None, partner_fee=None):
    _guard_invoice_action(invoice_name, action_label="recording courier outstanding")
    # ``partner_fee`` rides in as the shipping override: for a partner rider it is the
    # ONLY accepted source of the delivery cost (the service refuses to guess from our
    # own area rates), and for an ordinary courier it stays None and changes nothing.
    return _mark_courier_outstanding(
        invoice_name, courier, party_type, party, shipping_override=partner_fee
    )


# ---------------------------------------------------------------------------
# InstaPay / Mobile Wallet — unpaid online payment assurance & reconciliation
# ---------------------------------------------------------------------------


def _guard_dispatch(invoice_name: str, *, shortage_approved=False, shortage_reason: str | None = None):
    """The pre-dispatch rules every Out-for-Delivery entry point shares.

    Refuses a second dispatch of an order that already left (the repeat took the
    paid path and accrued the courier's fee twice), then applies the sub-territory,
    pending-shipping-request and stock-shortage gates that only the plain state
    endpoint used to enforce. Returns the loaded invoice for the caller's use.
    """
    inv = frappe.get_doc("Sales Invoice", invoice_name)
    _delivery_services.assert_not_already_dispatched(inv)
    _delivery_services.enforce_dispatch_gates(
        inv, shortage_approved=shortage_approved, shortage_reason=shortage_reason
    )
    return inv


@frappe.whitelist()  # type: ignore[attr-defined]
def deliver_online_unconfirmed(invoice_name: str, pos_profile: str, party_type: str | None = None, party: str | None = None, partner_fee=None, shortage_approved=False, shortage_reason: str | None = None):
    """Move an unpaid online-intent (InstaPay/Mobile Wallet) order Out for Delivery
    while it stays honestly Unpaid (no receivable move, no Payment Entry).

    The courier (party_type/party) is recorded and the freight accrued to them;
    settlement of the customer's money happens later on the manager reconciliation
    screen.
    """
    _guard_invoice_action(invoice_name, action_label="dispatching an order")
    _guard_dispatch(invoice_name, shortage_approved=shortage_approved, shortage_reason=shortage_reason)
    return _deliver_online_unconfirmed(
        invoice_name, pos_profile, party_type, party, partner_fee=partner_fee
    )


@frappe.whitelist()  # type: ignore[attr-defined]
def deliver_credit_on_account(invoice_name: str, pos_profile: str, party_type: str | None = None, party: str | None = None, partner_fee=None, shortage_approved=False, shortage_reason: str | None = None):
    """Move an order taken ON ACCOUNT Out for Delivery while it stays Unpaid.

    Same guards as every other dispatch entry point (branch scope, open shift,
    no double dispatch, stock/pin/territory gates). What it does NOT do is touch
    the customer's money: no Payment Entry, no move to Courier Outstanding, and
    no "Awaiting Payment" stamp — a credit order is not waiting for a transfer,
    and stamping it would arm the hourly escalation alarm. The courier is still
    accrued (and settleable for) his freight.
    """
    _guard_invoice_action(invoice_name, action_label="dispatching an order")
    _guard_dispatch(invoice_name, shortage_approved=shortage_approved, shortage_reason=shortage_reason)
    return _deliver_credit_on_account(
        invoice_name, pos_profile, party_type, party, partner_fee=partner_fee
    )


@frappe.whitelist()  # type: ignore[attr-defined]
def list_unconfirmed_online_orders(pos_profile: str | None = None):
    """List submitted orders awaiting online payment confirmation (scoped to accessible profiles)."""
    return _list_unconfirmed_online_orders(pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def confirm_online_payment(invoice_name: str, pos_profile: str, reference_no: str | None = None, receipt_name: str | None = None):
    """Manager confirms the bank/wallet transfer: creates a Receive PE (DR Bank/Instapay,
    CR Debtors) that marks the invoice Paid. Idempotent."""
    _guard_invoice_action(invoice_name, action_label="confirming an online payment")
    return _confirm_online_payment(invoice_name, pos_profile, reference_no, receipt_name)


@frappe.whitelist()  # type: ignore[attr-defined]
def convert_online_order_to_cod(invoice_name: str, pos_profile: str, party_type: str | None = None, party: str | None = None):
    """Convert an unconfirmed online order into a cash-on-delivery courier order."""
    _guard_invoice_action(invoice_name, action_label="converting an order to cash on delivery")
    return _convert_online_order_to_cod(invoice_name, pos_profile, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def pay_delivery_expense(invoice_name: str, pos_profile: str):
    _guard_invoice_action(invoice_name, action_label="paying a delivery expense")
    return _pay_delivery_expense(invoice_name, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def courier_delivery_expense_only(invoice_name: str, courier: str, party_type: str | None = None, party: str | None = None):
    _guard_invoice_action(invoice_name, action_label="paying a courier expense")
    return _courier_delivery_expense_only(invoice_name, courier, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def get_courier_balances(pos_profile: str | None = None):
    """Unsettled courier balances for the caller's branch(es).

    ``pos_profile`` narrows to one branch (and is access-checked); omitting it
    returns every branch the caller is assigned to. Neither form can reach a
    branch the caller is not a member of — the scope is applied server-side, so
    an older client that sends nothing is scoped too.
    """
    return _get_courier_balances(pos_profile=pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def get_couriers(pos_profile: str | None = None):
    """Get list of active couriers visible for the requested POS profile."""
    return get_active_couriers(pos_profile=pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier(courier: str | None = None, pos_profile: str | None = None, party_type: str | None = None, party: str | None = None):
    """Backward-compatible settlement API.

    Preferred: pass party_type and party to settle unified delivery party.
    Fallback: pass courier (legacy label) which will settle legacy rows only.
    """
    _guard_branch_action(pos_profile, action_label="settling a courier")
    if party_type and party:
        return _settle_delivery_party(party_type=party_type, party=party, pos_profile=pos_profile)
    if courier:
        return _settle_courier(courier, pos_profile)
    frappe.throw("Provide either party_type & party or courier")


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_delivery_party(party_type: str, party: str, pos_profile: str | None = None):
    _guard_branch_action(pos_profile, action_label="settling a delivery party")
    return _settle_delivery_party(party_type=party_type, party=party, pos_profile=pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def settle_courier_for_invoice(invoice_name: str, pos_profile: str | None = None):
    """Settle the courier position for a SINGLE invoice.

    Scope is this invoice only. Internally this picks between the same two single-invoice
    settlement paths the POS app uses (settle_courier_collected_payment /
    settle_single_invoice_paid) by the sign of get_invoice_settlement_preview's net_amount.

    To settle a courier's ENTIRE outstanding balance in one journal entry, call
    settle_delivery_party() (or legacy settle_courier()) instead — until 2026-08-01 this
    endpoint did that despite its name.
    """
    _guard_invoice_action(invoice_name, action_label="settling a courier for this order")
    return _settle_courier_for_invoice(invoice_name, pos_profile)


@frappe.whitelist()  # type: ignore[attr-defined]
def get_active_couriers(pos_profile: str | None = None):
    """Return active delivery parties visible for the current user's POS profiles.

    Output rows have shape:
      {"party_type": "Employee"|"Supplier", "party": name, "display_name": label}
    """
    visible_profiles = get_visible_pos_profiles(requested_pos_profile=pos_profile)
    if not visible_profiles:
        return []

    out = []

    def _row_value(row, fieldname: str):
        if isinstance(row, dict):
            return row.get(fieldname)
        return getattr(row, fieldname, None)

    # Utility: check if a DocType has a given column in DB
    def _has_column(doctype: str, column: str) -> bool:
        try:
            return bool(frappe.db.has_column(doctype, column))
        except Exception:
            return False
    # Resolve group names: prefer Jarz POS Settings, fallback to constants
    _emp_grp_name = DELIVERY_GROUPS.EMPLOYEE_GROUP
    _sup_grp_name = DELIVERY_GROUPS.SUPPLIER_GROUP
    try:
        from jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings import get_jarz_settings
        s = get_jarz_settings()
        if s and s.delivery_employee_group:
            _emp_grp_name = s.delivery_employee_group
        if s and s.delivery_supplier_group:
            _sup_grp_name = s.delivery_supplier_group
    except Exception:
        pass
    # Employees in Employee Group
    emp_group = frappe.db.get_value("Employee Group", {"employee_group_name": _emp_grp_name}, "name")
    if emp_group:
        try:
            eg_doc = frappe.get_doc("Employee Group", emp_group)
            # Try common child table fieldnames first
            potential_tables = [
                "employees", "members", "employee_list", "employee_members",
                "employee_group_items", "employee_details",
            ]
            employee_names: set[str] = set()
            found_any = False
            for key in potential_tables:
                rows = eg_doc.get(key)
                if isinstance(rows, list) and rows:
                    for r in rows:
                        # child rows may have 'employee' link; fallback to 'employee_id'
                        emp = (r.get("employee") or r.get("employee_id") or "").strip()
                        if emp:
                            employee_names.add(emp)
                    found_any = True
                    break
            # If not found under known keys, scan any child list for 'employee' key
            if not found_any:
                data = eg_doc.as_dict() or {}
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict) and "employee" in v[0]:
                        for r in v:
                            emp = (r.get("employee") or r.get("employee_id") or "").strip()
                            if emp:
                                employee_names.add(emp)
                        found_any = True
                        break
            if employee_names:
                emp_fields = ["name", "employee_name"]
                if _has_column("Employee", "branch"):
                    emp_fields.append("branch")
                if _has_column("Employee", "status"):
                    emp_fields.append("status")
                if _has_column("Employee", "custom_delivery_partner"):
                    emp_fields.append("custom_delivery_partner")
                emps = frappe.get_all(
                    "Employee",
                    fields=emp_fields,
                    filters={"name": ["in", list(employee_names)]},
                )
                out.extend({
                    "party_type": "Employee",
                    "party": _row_value(e, "name"),
                    "display_name": (_row_value(e, "employee_name") or _row_value(e, "name")),
                    "branch": _row_value(e, "branch"),
                    "delivery_partner": _row_value(e, "custom_delivery_partner") or None,
                    "status": _row_value(e, "status"),
                } for e in emps)
        except Exception as err:
            frappe.log_error(f"Failed to read Employee Group members: {err}", "Jarz POS get_active_couriers")
    # Suppliers in Supplier Group
    sup_group = frappe.db.get_value("Supplier Group", {"supplier_group_name": _sup_grp_name}, "name")
    if sup_group:
        sup_fields = ["name", "supplier_name"]
        if _has_column("Supplier", "branch"):
            sup_fields.append("branch")
        if _has_column("Supplier", "disabled"):
            sup_fields.append("disabled")
        if _has_column("Supplier", "custom_delivery_partner"):
            sup_fields.append("custom_delivery_partner")
        sups = frappe.get_all("Supplier", fields=sup_fields, filters={"supplier_group": sup_group})
        out.extend({
            "party_type": "Supplier",
            "party": _row_value(s, "name"),
            "display_name": (_row_value(s, "supplier_name") or _row_value(s, "name")),
            "branch": _row_value(s, "branch"),
            "delivery_partner": _row_value(s, "custom_delivery_partner") or None,
            "disabled": _row_value(s, "disabled"),
        } for s in sups)
    return filter_available_couriers(out, visible_profiles=visible_profiles)


@frappe.whitelist()  # type: ignore[attr-defined]
def handle_out_for_delivery_paid(invoice_name: str, courier: str, settlement: str, pos_profile: str, party_type: str | None = None, party: str | None = None, shortage_approved=False, shortage_reason: str | None = None):
    # 'courier' kept only for backward compatibility; underlying service ignores legacy Courier DocType
    _guard_invoice_action(invoice_name, action_label="dispatching an order")
    _guard_dispatch(invoice_name, shortage_approved=shortage_approved, shortage_reason=shortage_reason)
    return _handle_out_for_delivery_paid(invoice_name, courier, settlement, pos_profile, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
def handle_out_for_delivery_transition(invoice_name: str, courier: str, mode: str, pos_profile: str, idempotency_token: str | None = None, party_type: str | None = None, party: str | None = None, shortage_approved=False, shortage_reason: str | None = None):
    # 'courier' kept only for backward compatibility; underlying service ignores legacy Courier DocType
    _guard_invoice_action(invoice_name, action_label="dispatching an order")
    _guard_dispatch(invoice_name, shortage_approved=shortage_approved, shortage_reason=shortage_reason)
    return _handle_out_for_delivery_transition(invoice_name, courier, mode, pos_profile, idempotency_token, party_type, party)


@frappe.whitelist()  # type: ignore[attr-defined]
@frappe.whitelist(allow_guest=False)
def settle_single_invoice_paid(invoice_name: str, pos_profile: str, party_type: str, party: str):
    """Settle a paid invoice's courier shipping fee individually (one-by-one settlement).

    Creates JE (DR Creditors [party] / CR Cash) and settles or creates Courier Transaction.
    Returns: { success, invoice, journal_entry, shipping_amount, party_type, party, courier_transactions }
    """
    frappe.logger().info(f"API settle_single_invoice_paid CALLED: invoice={invoice_name}, pos_profile={pos_profile}, party_type={party_type}, party={party}")
    _guard_invoice_action(invoice_name, action_label="settling this order")
    return _settle_single_invoice_paid(invoice_name, pos_profile, party_type, party)

@frappe.whitelist(allow_guest=False)
def settle_courier_collected_payment(invoice_name: str, pos_profile: str, party_type: str, party: str):
    """Settle a courier collected payment.

    This function processes the collected payment for the courier.
    Returns: { success, invoice, payment_details }
    """
    frappe.logger().info(f"API settle_courier_collected_payment CALLED: invoice={invoice_name}, pos_profile={pos_profile}, party_type={party_type}, party={party}")
    _guard_invoice_action(invoice_name, action_label="settling a collected payment")
    return _settle_courier_collected_payment(invoice_name, pos_profile, party_type, party)


def _ensure_collection_change_access() -> None:
    roles = {str(role or "").strip() for role in (frappe.get_roles() or []) if str(role or "").strip()}
    allowed = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER
    if not roles.intersection(allowed):
        frappe.throw("Not permitted: Manager access required", frappe.PermissionError)


@frappe.whitelist(allow_guest=False)
def change_payment_collection_method(
    invoice_name: str,
    new_method: str,
    pos_profile: str,
    party_type: str | None = None,
    party: str | None = None,
    reference_no: str | None = None,
    reference_date: str | None = None,
    receipt_name: str | None = None,
    notes: str | None = None,
    idempotency_token: str | None = None,
):
    """Manager-only collection-method switch for customer-unpaid courier orders.

    The DISPATCH-STATE gate lives one layer down, in
    ``services/delivery_handling.change_payment_collection_method``: it refuses
    anything whose ``custom_sales_invoice_state`` is not Out for Delivery or
    Delivered, normalising case and underscores as it goes. Deliberately not
    duplicated here — two copies of a money gate drift, and the one that drifts is
    always the one nobody is reading. It matters most for the credit shape, which
    has no ``Awaiting Payment`` stamp to gate it implicitly: a credit order still in
    Recieved would otherwise fall through to ``unpaid_online_cash_at_branch`` and
    post DR branch cash / CR Debtors for goods still in the kitchen. The kanban card
    matches the same states (``api/kanban._COLLECTION_CHANGE_STATES``) so the action
    is not offered where the server would refuse it.
    """
    try:
        _ensure_collection_change_access()
        inv = frappe.get_doc("Sales Invoice", (invoice_name or "").strip())
        frappe.has_permission("Sales Invoice", "write", doc=inv, throw=True)
        ensure_profile_scoped_invoice_access(
            inv, action_label="changing the collection method"
        )
        ensure_open_shift_for_invoice(
            inv, action_label="changing the collection method"
        )
        result = _change_payment_collection_method(
            invoice_name=invoice_name,
            new_method=new_method,
            pos_profile=pos_profile,
            party_type=party_type,
            party=party,
            reference_no=reference_no,
            reference_date=reference_date,
            receipt_name=receipt_name,
            notes=notes,
            idempotency_token=idempotency_token,
        )
        return {"success": True, "data": result}
    except frappe.PermissionError:
        raise
    except frappe.ValidationError:
        raise
    except Exception:
        frappe.log_error(frappe.get_traceback(), "change_payment_collection_method failed")
        raise


@frappe.whitelist()  # type: ignore[attr-defined]
def create_delivery_party(
    party_type: str,
    name: str | None = None,
    phone: str | None = None,
    pos_profile: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    delivery_partner: str | None = None,
):
    """Create a new delivery party (Employee or Supplier) and return unified structure.

    Either provide (first_name & last_name) or a combined name.
    If delivery_partner is provided, sets custom_delivery_partner on the created record.
    Returns: {party_type, party, display_name, phone}
    """
    try:
        delivery_partner = (delivery_partner or '').strip() or None
        # Validate POS profile is active
        if pos_profile:
            from jarz_pos.utils.validation_utils import assert_pos_profile_enabled
            assert_pos_profile_enabled(pos_profile)
        # Validate delivery_partner permission via POS profile
        if delivery_partner and pos_profile:
            try:
                has_field = bool(frappe.get_meta('POS Profile').get_field('custom_allow_delivery_partner'))
                if has_field:
                    allowed = frappe.db.get_value('POS Profile', pos_profile, 'custom_allow_delivery_partner')
                    if not allowed:
                        frappe.throw('This POS Profile is not allowed to assign delivery partners.')
            except frappe.exceptions.ValidationError:
                raise
            except Exception:
                pass  # field doesn't exist yet, allow through

        frappe.logger().info(f"create_delivery_party called with: party_type={party_type}, name={name}, first_name={first_name}, last_name={last_name}, phone={phone}, pos_profile={pos_profile}, delivery_partner={delivery_partner}")
        # Use pos_profile as branch name per requirement
        result = _create_delivery_party(
            party_type=party_type,
            name=name,
            phone=phone,
            branch=pos_profile,
            first_name=first_name,
            last_name=last_name,
        )

        # Set delivery partner on the created record if provided
        if delivery_partner and result and result.get('party'):
            try:
                doc = frappe.get_doc(result['party_type'], result['party'])
                if doc.meta.get_field('custom_delivery_partner'):
                    doc.custom_delivery_partner = delivery_partner
                    doc.save(ignore_permissions=True)
                    result['delivery_partner'] = delivery_partner
            except Exception as dp_err:
                frappe.logger().warning(f"Failed to set delivery_partner on {result.get('party')}: {dp_err}")

        frappe.logger().info(f"create_delivery_party successful: {result}")
        return result
    except frappe.exceptions.ValidationError:
        raise
    except Exception as e:
        frappe.logger().error(f"create_delivery_party failed: {str(e)}")
        frappe.logger().error(frappe.get_traceback())
        # Return user-friendly error instead of generic 409
        frappe.throw(f"Failed to create courier: {str(e)}")


@frappe.whitelist()  # type: ignore[attr-defined]
def get_delivery_partners_list():
    """Return list of active Delivery Partner records for dropdown selection."""
    partners = frappe.get_all(
        'Delivery Partner',
        filters={'is_active': 1},
        fields=['name', 'partner_name'],
        order_by='partner_name asc',
    )
    return partners


# ---------------------------------------------------------------------------
# Two-step settlement (Preview + Confirm) – server-driven, atomic on confirm
# ---------------------------------------------------------------------------

def _seconds_since(ts_str: str | None) -> int | None:
    if not ts_str:
        return None
    try:
        dt = get_datetime(ts_str)
        if not dt:
            return None
        return int((now_datetime() - dt).total_seconds())
    except Exception:
        return None


def _latest_payment_info(inv_name: str) -> dict | None:
    try:
        refs = frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Sales Invoice", "reference_name": inv_name},
            pluck="parent",
        )
        if not refs:
            return None
        rows = frappe.get_all(
            "Payment Entry",
            filters={"name": ["in", refs], "docstatus": 1, "payment_type": "Receive"},
            fields=["name", "creation", "posting_date", "posting_time", "modified"],
            order_by="creation desc",
            limit=1,
        )
        return rows[0] if rows else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Preview tokens — the single-use permits that let a confirm post money
# ---------------------------------------------------------------------------

def _log_preview_cache_failure(which: str) -> None:
    """Record a preview-cache read failure without ever becoming the new 500.

    The read is deliberately degraded to "no usable permit", but silently
    swallowing it means a genuine cache misconfiguration — wrong Redis URL,
    cache container down, auth failure — presents to every cashier as
    "Preview expired, reopen the dialog", forever, with no server-side signal
    that anything is wrong. Nothing would distinguish that from an ordinary
    stale token.

    Wrapped because `frappe.log_error` can itself raise (see the logging notes
    in this repo), and a logging failure must not resurrect the crash this
    handler exists to prevent.
    """
    try:
        frappe.log_error(frappe.get_traceback(), f"{which} preview cache read failed")
    except Exception:  # noqa: BLE001
        pass


#: Compare-and-delete, executed inside Redis so it cannot interleave.
#:
#: A plain GET-then-DELETE (what both preview flows used to do) is
#: check-then-act: two callers holding the same token both pass the GET before
#: either DELETEs, and both go on to post. GETDEL would fix the atomicity but
#: destroys the value whatever was presented, so anyone submitting a stale or
#: wrong token would invalidate the legitimate one. Comparing against the exact
#: bytes we read and deleting only on a match gives single-use without that
#: side effect: of two callers with one token exactly one gets 1 back, and a
#: wrong token changes nothing.
_PREVIEW_TOKEN_CONSUME_LUA = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "redis.call('DEL', KEYS[1]); return 1 else return 0 end"
)

#: Lifetime of a settlement preview token, matching UNSETTLE_PREVIEW_TTL.
SETTLE_PREVIEW_TTL = 180


def _settle_preview_key(token: str):
    """Redis key holding the preview payload for *token*.

    Keyed on the token rather than on the invoice — unlike
    :func:`_unsettle_preview_key`. Settling is the routine cashier action and
    the dialog is opened constantly, often from two devices looking at the same
    order; invalidating an open dialog's permit because someone else glanced at
    the invoice would turn a fix into a daily "Preview expired". Each token is
    independently short-lived and single-use, and ``confirm_settlement`` still
    has to get past the branch, shift and dispatch guards plus the invoice's own
    state, so coexisting previews are not coexisting permits to double-post.

    ``make_key`` applies the same ``<db_name>|`` namespace prefix the pickling
    helpers apply, keeping this inside the site's own cache namespace. The value
    is written and read with raw Redis commands rather than those helpers,
    because only raw commands can consume it atomically — and because ``hget``
    consults ``frappe.local.cache`` first, which can return a token already
    consumed earlier in the same request.
    """
    # Normalised here rather than at each call site so peek and spend cannot
    # address different keys for the same presented token.
    return frappe.cache().make_key(f"jarz_pos:settle_preview:{str(token or '').strip()}")


def _mint_settle_preview_token(data: dict) -> str:
    """Store *data* under a fresh single-use token and return the token."""
    token = frappe.generate_hash(length=32)
    # One SET carrying its own expiry. The previous shape was `hset` followed by
    # `expire(cache_key, 180)` — but `hset` namespaces the key through `make_key`
    # while the inherited `expire` does not, so that call set a TTL on a key that
    # did not exist and silently returned False. The token it was meant to expire
    # had NO expiry at all: verified against real Redis on 2026-09-07, `expire()`
    # returned False and `TTL` on the real key was -1. A permit to post money was
    # immortal, replayable long after the state it described had changed.
    frappe.cache().set(
        _settle_preview_key(token), json.dumps(data, sort_keys=True), ex=SETTLE_PREVIEW_TTL
    )
    return token


def _peek_settle_preview_token(token: str):
    """Validate the preview token WITHOUT spending it.

    Returns ``(payload, raw)`` when the token is live, else ``(None, None)``.
    ``raw`` is the exact stored bytes and is what
    :func:`_spend_settle_preview_token` compares against — so the pair form a
    compare-and-swap across the validation that runs between them.

    Reading before spending is deliberate: the caller needs the previewed
    amounts to decide anything, and a request refused for a fixable reason
    (wrong invoice, closed shift) should leave the token usable rather than
    forcing a re-preview.
    """
    presented = str(token or "").strip()
    if not presented:
        return None, None

    # Any cache failure here means "no usable permit", never a 500 on the
    # cashier's confirm.
    #
    # The specific case this exists for is the DEPLOY WINDOW, not a Redis
    # outage. The previous shape stored this payload as a Redis HASH under the
    # exact same key bytes (`hset` namespaced through `make_key`, which is what
    # `_settle_preview_key` still produces), so a token minted seconds before
    # the workers restart is a hash that this `GET` hits with
    # `WRONGTYPE Operation against a key holding the wrong kind of value`.
    # Unhandled, that surfaced as an opaque 500 on the routine settle path for
    # every cashier holding an open dialog across the restart. Degrading to
    # "expired or invalid" instead sends them back through re-preview, which is
    # the recovery they already understand and which the caller already
    # handles. Fails closed either way: no permit, no posting.
    try:
        raw = frappe.cache().get(_settle_preview_key(presented))
    except Exception:
        _log_preview_cache_failure("settle")
        return None, None
    if raw is None:
        return None, None

    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return None, None
    if not isinstance(payload, dict):
        return None, None

    return payload, raw


def _spend_settle_preview_token(token: str, raw) -> bool:
    """Atomically consume the token whose exact bytes are *raw*.

    True only for the caller that actually removed it; anyone whose value
    changed in the meantime must be refused.
    """
    return bool(
        frappe.cache().eval(_PREVIEW_TOKEN_CONSUME_LUA, 1, _settle_preview_key(token), raw)
    )


@frappe.whitelist()  # type: ignore[attr-defined]
def generate_settlement_preview(invoice: str, party_type: str | None = None, party: str | None = None, mode: str = "pay_now", recent_payment_seconds: int = 30):
    """Produce a settlement preview and mint a short-lived token to be used on confirmation.

    Branch-scoped since 2026-08-19. This endpoint and :func:`confirm_settlement`
    are the pair the mobile client drives for the whole out-for-delivery
    pay-now flow, and neither carried any guard at all: no role, no branch, no
    shift. Every *other* invoice-level action in this module goes through
    :func:`_guard_invoice_action`; these two were simply missed, so any logged-in
    POS user could mint a preview against any branch's invoice and then settle
    it. The shift requirement is deliberately left off here — a preview posts
    nothing, and refusing to *look* before the drawer is open would be a
    behaviour change rather than a fix. ``confirm_settlement``, which posts, does
    require one.

    Returns:
      {
        invoice, party_type, party, mode,
        order_amount, shipping_amount, net_amount,
        is_unpaid_effective, last_payment_seconds,
        preview_token, expires_in
      }
    """
    if not invoice:
        frappe.throw("invoice is required")

    _guard_invoice_action(
        invoice, action_label="preview a settlement", require_shift=False
    )

    inv = frappe.get_doc("Sales Invoice", invoice)
    if inv.docstatus != 1:
        frappe.throw("Invoice must be submitted")

    # derive shipping via service helper (territory-aware in your services)
    try:
        shipping = float(_delivery_services._get_delivery_expense_amount(inv) or 0.0)  # type: ignore[attr-defined]
    except Exception:
        shipping = 0.0

    outstanding = float(inv.outstanding_amount or 0)
    status_l = (inv.status or "").strip().lower()

    last_pe = _latest_payment_info(inv.name)
    last_pe_seconds = _seconds_since(last_pe["creation"]) if last_pe else None

    # Effective unpaid when truly unpaid OR payment is too recent (within threshold)
    unpaid_status = status_l in {"unpaid", "overdue", "partially paid", "partly paid"}
    is_unpaid_effective = (outstanding > 0.009) or unpaid_status or (last_pe_seconds is not None and last_pe_seconds <= int(recent_payment_seconds))

    # Order amount = what the courier is holding / owes for this invoice. The authoritative
    # source is the Courier Transaction accrued at Out-for-Delivery — the SAME rows the batch
    # settlement (settle_delivery_party) and get_courier_balances aggregate. Reading it here
    # keeps the single-invoice preview's amounts AND its collect/pay label identical to the
    # batch "Settle All" path.
    #
    # This is the fix for the settle-later (COD / cash) divergence: when an order is dispatched
    # "settle later", its receivable is moved to Courier Outstanding, so the invoice looks
    # "Paid" (outstanding == 0) even though the courier still owes (order_amount - shipping).
    # The old heuristic (`grand_total if is_unpaid_effective else 0`) therefore zeroed the order
    # amount for exactly these orders, dropping the shipping deduction from a real order amount
    # and flipping "collect from courier" into "pay courier" — while the batch path (which reads
    # the Courier Transaction) still showed the correct positive "collect" balance.
    settlement_ct_filters = {"reference_invoice": inv.name, "status": ["!=", "Settled"]}
    if party_type and party:
        settlement_ct_filters.update({"party_type": party_type, "party": party})
    settlement_ct_rows = frappe.get_all(
        "Courier Transaction",
        filters=settlement_ct_filters,
        fields=["amount", "shipping_amount", "party_type", "party", "partner_fee", "is_partner_order"],
        order_by="creation desc",
        limit=1,
    )
    settlement_ct = settlement_ct_rows[0] if settlement_ct_rows else None

    if settlement_ct is not None:
        # Trust the recorded transaction (what the courier actually collected / owes).
        order_amount = float(settlement_ct.get("amount") or 0)
        ct_shipping = float(settlement_ct.get("shipping_amount") or 0)
        if ct_shipping > 0:
            # Prefer the shipping accrued on the CT so single & batch net the exact same value.
            shipping = ct_shipping
        # Adopt the CT's party when the caller did not specify one.
        if not (party_type and party):
            party_type = settlement_ct.get("party_type") or party_type
            party = settlement_ct.get("party") or party
    else:
        # No transaction yet (preview generated before OFD): anticipate the courier collection
        # from the invoice's paid/unpaid status, as before.
        order_amount = float(inv.grand_total or 0) if is_unpaid_effective else 0.0

    # Online-intent unpaid orders (InstaPay/Mobile Wallet) collect NOTHING from the courier —
    # the customer pays online and a manager confirms the transfer separately. Zero the courier
    # cash-collection amounts so the preview never implies the courier is liable.
    is_online_unconfirmed = bool(is_unpaid_effective and _is_online_intent(inv))
    # A credit / on-account order collects nothing from the courier either, and for
    # the same reason: the money is not travelling with him. Kept as its own flag
    # because the two are NOT the same downstream — an online order is awaiting a
    # transfer, a credit order is a 30-day receivable on the customer — but the
    # courier's cash position is identically zero, and without this a preview taken
    # BEFORE dispatch (no Courier Transaction yet) would fall into the branch above
    # and tell the branch to collect the whole invoice from the rider.
    is_credit_on_account = bool(is_unpaid_effective and _is_credit_intent(inv))
    if is_online_unconfirmed or is_credit_on_account:
        order_amount = 0.0

    # Detect delivery partner mode
    _dp = None
    if party_type and party:
        _dp_field = "custom_delivery_partner"
        try:
            _dp = frappe.db.get_value(party_type, party, _dp_field)
        except Exception:
            pass
    is_partner_order = bool(_dp)

    # What the partner charges for the trip is NOT deducted from the cash the branch
    # collects — the rider hands over every pound and his company bills us weekly.
    # It lives on the transaction's ``partner_fee``, never on ``shipping_amount``,
    # which is why the net below is the full amount.
    partner_fee = 0.0
    if is_partner_order:
        if settlement_ct is not None:
            partner_fee = float(settlement_ct.get("partner_fee") or 0)
        if partner_fee <= 0:
            partner_fee = float(getattr(inv, "custom_shipping_expense", 0) or 0)
        # Nothing is withheld from the rider, so the branch collects the whole order.
        shipping = 0.0
        net_amount = order_amount
    else:
        net_amount = order_amount - shipping

    # Online-intent unpaid and credit / on-account orders: no courier cash
    # collection at all.
    if is_online_unconfirmed or is_credit_on_account:
        net_amount = 0.0

    # include resolved party if not provided – from any existing CT linked to invoice
    if not (party_type and party):
        existing_party = frappe.get_all(
            "Courier Transaction",
            filters={
                "reference_invoice": inv.name,
                "party_type": ["not in", [None, ""]],
                "party": ["not in", [None, ""]],
            },
            fields=["party_type", "party"],
            limit=1,
        )
        if existing_party:
            party_type = existing_party[0].get("party_type")
            party = existing_party[0].get("party")

    token = _mint_settle_preview_token(
        {
            "invoice": inv.name,
            "party_type": party_type,
            "party": party,
            "mode": mode,
            "order_amount": order_amount,
            "shipping_amount": shipping,
            "net_amount": net_amount,
            "is_unpaid_effective": is_unpaid_effective,
            "last_payment_seconds": last_pe_seconds,
            "is_partner_order": is_partner_order,
            "delivery_partner": _dp,
            "partner_fee": partner_fee,
            "is_online_unconfirmed": is_online_unconfirmed,
            "is_credit_on_account": is_credit_on_account,
        }
    )

    return {
        "invoice": inv.name,
        "woo_order_id": normalize_woo_order_id(inv.get("woo_order_id")),
        "party_type": party_type,
        "party": party,
        "mode": mode,
        "order_amount": order_amount,
        "shipping_amount": shipping,
        "net_amount": net_amount,
        "is_unpaid_effective": is_unpaid_effective,
        "last_payment_seconds": last_pe_seconds,
        "preview_token": token,
        "expires_in": SETTLE_PREVIEW_TTL,
        "is_partner_order": is_partner_order,
        "delivery_partner": _dp,
        "partner_fee": partner_fee,
        # The cashier must type the partner's own price for this address before the
        # order can go out — our area rates do not map onto the partner's zones, so
        # there is deliberately no default to fall back on. Already dispatched
        # orders (a transaction exists) keep the fee they were sent with.
        "requires_partner_fee": bool(is_partner_order and settlement_ct is None),
        "is_online_unconfirmed": is_online_unconfirmed,
        # Lets the client label the dispatch honestly ("on account — nothing to
        # collect") instead of showing a zero it cannot explain.
        "is_credit_on_account": is_credit_on_account,
    }


@frappe.whitelist()  # type: ignore[attr-defined]
def confirm_settlement(invoice: str, preview_token: str, mode: str, pos_profile: str | None = None, party_type: str | None = None, party: str | None = None, payment_mode: str = "Cash", partner_fee=None, shortage_approved=False, shortage_reason: str | None = None):
    """Confirm a previously previewed settlement atomically.

    If preview indicated unpaid and mode==pay_now, creates a Payment Entry, then performs
    Out For Delivery transition using unified delivery party details. All inside one transaction.

    ``shortage_approved`` / ``shortage_reason`` carry the operator's stock-shortage
    approval, exactly as ``api.kanban.update_invoice_state`` takes it: this is the
    entry point for every courier dispatch, and until 2026-09-05 it applied none of
    the pre-dispatch gates.
    """
    if not invoice:
        frappe.throw("invoice is required")
    if not preview_token:
        frappe.throw("preview_token is required")

    # Branch-scoped and shift-gated since 2026-08-19. This posts Payment Entries
    # and Journal Entries through _dispatch_settlement below, and carried no
    # guard of any kind — no role, no branch, no shift — while being the exact
    # endpoint the mobile client calls to settle an order. The preview token is
    # not a substitute: it is minted by generate_settlement_preview, which was
    # equally unguarded, so a caller could simply mint their own.
    _guard_invoice_action(invoice, action_label="confirm a settlement")
    _guard_dispatch(invoice, shortage_approved=shortage_approved, shortage_reason=shortage_reason)

    data, raw_token = _peek_settle_preview_token(preview_token)
    if not data:
        frappe.throw("Preview expired or invalid. Please reopen the dialog.")
    if data.get("invoice") != invoice:
        frappe.throw("Preview does not match invoice. Please reopen the dialog.")

    # adopt party from preview if not provided
    party_type = party_type or data.get("party_type")
    party = party or data.get("party")

    # Build a non-empty courier label for legacy 'courier' arg required by services layer
    def _courier_label(pt: str | None, p: str | None) -> str:
        pt = (pt or "").strip()
        p = (p or "").strip()
        if not p:
            return "Courier"
        try:
            if pt == "Employee":
                return frappe.db.get_value("Employee", p, "employee_name") or p
            if pt == "Supplier":
                return frappe.db.get_value("Supplier", p, "supplier_name") or p
        except Exception:
            pass
        return p

    # Spend the permit BEFORE posting anything. This used to be a delete after
    # the commit — check-then-act, so two callers holding one token both passed
    # the read and both went on to settle. The compare-and-delete runs inside
    # Redis, so exactly one of them gets True here and the rest are refused
    # before any money moves.
    #
    # The guards and the invoice match have already run, but this is NOT the
    # same as "a token is only burned by a caller that is about to post" — an
    # earlier version of this comment claimed that and it is false.
    # `_dispatch_settlement` below still refuses for user-fixable reasons, most
    # concretely `_require_partner_fee` ("Enter the partner's delivery cost"),
    # and a lock-wait timeout or deadlock lands in the same place. The savepoint
    # rollback restores MariaDB; it does not put the Redis key back, so those
    # callers lose their permit.
    #
    # That is survivable only because no client retries with the same token:
    # the Kanban board and the invoice card both re-run the preview on every
    # attempt. If a same-token retry is ever added, re-mint here in the failure
    # path rather than relying on this note.
    if not _spend_settle_preview_token(preview_token, raw_token):
        frappe.throw("Preview already used or expired. Please reopen the dialog.")

    try:
        frappe.db.savepoint("confirm_settlement")
        # Map preview mode to our strategy mode keys
        strat_mode = "now" if (mode or data.get("mode")) in {"pay_now", "now"} else "later"
        # Use separated strategies to perform the correct accounting and CT/JE actions
        # The partner's own price for this trip, typed by the cashier off the
        # partner's app. Falls back to the previewed value so a client that already
        # showed the figure does not have to echo it back.
        effective_partner_fee = partner_fee
        if effective_partner_fee is None and data.get("is_partner_order"):
            effective_partner_fee = data.get("partner_fee")
        res = _dispatch_settlement(
            inv_name=invoice,
            mode=strat_mode,
            pos_profile=pos_profile,
            payment_type=payment_mode,
            party_type=party_type,
            party=party,
            partner_fee=effective_partner_fee,
        )

        frappe.db.commit()

        base = {
            "success": True,
            "invoice": invoice,
            "mode": mode,
            "order_amount": data.get("order_amount"),
            "shipping_amount": data.get("shipping_amount"),
            "net_amount": data.get("net_amount"),
            "is_unpaid_effective": data.get("is_unpaid_effective"),
            "party_type": party_type,
            "party": party,
            "is_partner_order": data.get("is_partner_order", False),
            "delivery_partner": data.get("delivery_partner"),
            "partner_fee": effective_partner_fee,
        }
        base.update({k: v for k, v in (res or {}).items() if k not in base})
        return base
    except Exception as e:
        frappe.db.rollback(save_point="confirm_settlement")
        frappe.log_error(frappe.get_traceback(), "confirm_settlement failed")
        raise


# ---------------------------------------------------------------------------
# Un-settle — reverse a posted courier settlement (Preview + Commit)
#
# Before this pair, the only way to undo a courier settlement was a developer
# piping a hand-written Python script into a production bench console — most
# recently on 2026-09-02, when five Nasr City Courier Transactions were
# settled from the wrong branch till. See
# ``jarz_pos.services.delivery_handling`` (the "Un-settle" section) for the
# accounting: a REVERSING Journal Entry is posted, the original is never
# touched, and every Courier Transaction the original settled flips back to
# Unsettled.
#
# Reversing money is manager-tier — mirrors ``_ensure_collection_change_access``
# immediately above it in this module, the closest existing "a manager undoes
# a courier money decision" gate. Branch scope is resolved from the
# settlement itself (never from the caller — see
# ``services.delivery_handling.resolve_settlement_branch``) and then checked
# with the same ``_guard_branch_action`` every other branch-wide courier
# action in this module uses.
# ---------------------------------------------------------------------------


#: Settlement reversal shipped DARK from 944bf02 until 2026-09-07, held back by
#: this flag while three defects were fixed. All three were invisible to the
#: mocked suite, which was green throughout — so none of them was allowed to be
#: closed by another mocked test. Each was reproduced and then re-checked against
#: a real database, real Redis and a second real connection
#: (``tests/test_settlement_reversal_real_db.py``, now in the CI array):
#:
#: 1. DEAD QUERY. ``list_recent_courier_settlements`` filtered ``journal_entry``
#:    with ``["not in", ["", None]]``, which Frappe renders as
#:    ``IFNULL(journal_entry,'') NOT IN ('', NULL)`` — UNKNOWN, never TRUE, for
#:    every row. Confirmed against production: the old filter returned 0 rows
#:    where the fixed one returns rows out of the 36 Courier Transactions that
#:    carry a journal entry, so nothing could ever be listed to reverse. Now
#:    ``["is", "set"]``. The mocked test had ASSERTED the broken shape.
#: 2. DENY-LIST. The "is this a settlement?" test excluded one tag
#:    (PARTNER_FEE_ACCRUAL) and admitted everything else that lands in
#:    ``Courier Transaction.journal_entry`` — including the collection-change
#:    entries, which really do sit on Settled rows because a settle path that
#:    nets to zero closes the row without clearing the field. Reversing one
#:    would erase the record of a customer's online payment and re-create a
#:    receivable against a courier holding nothing. The production audit found
#:    14 such entries already attached to Settled rows — 14 of the 22 journal
#:    entries the dialog would have offered. Now an ALLOW-list
#:    (``SETTLEMENT_JE_TAG_TYPES``), which required tagging the batch
#:    settlement; legacy untagged batch entries are recognised by their remark
#:    shape so history stays reversible.
#: 3. NO ISOLATION. ``FOR UPDATE`` on the Journal Entry serialized two callers
#:    but did not isolate them: under MariaDB's REPEATABLE READ the guards after
#:    the lock were plain SELECTs served from the snapshot opened at the
#:    transaction's first read, so the second caller re-read the state that told
#:    it to proceed. Reproduced with two connections — a plain read still
#:    reported the settlement reversible after the other transaction committed
#:    the flip. The decisive re-read is now a locking read.
#:
#: The production audit also asked whether the tag test failed OPEN for legacy
#: untagged partner fee accruals: it does not — there are zero of them.
UNSETTLE_RELEASED = True


def _ensure_unsettle_released() -> None:
    """Refuse the reversal endpoints while the feature is held back.

    Enforced here rather than only in the client: these endpoints take an
    arbitrary Journal Entry name, so hiding the button would leave the
    defects reachable by anyone who can call the API.
    """
    if not UNSETTLE_RELEASED:
        frappe.throw(
            "Reversing a courier settlement is not available yet. It is held "
            "back pending verification; reverse it from the backend for now.",
            frappe.ValidationError,
        )


#: Lifetime of an un-settle preview token, matching generate_settlement_preview.
UNSETTLE_PREVIEW_TTL = 180

#: The reversal consumes its token with the same compare-and-delete the
#: settlement preview uses — see :data:`_PREVIEW_TOKEN_CONSUME_LUA` for why a
#: GET-then-DELETE cannot give single-use and why GETDEL is the wrong fix.
_UNSETTLE_TOKEN_CONSUME_LUA = _PREVIEW_TOKEN_CONSUME_LUA


def _unsettle_preview_key(journal_entry: str):
    """Redis key holding the ONE live preview token for *journal_entry*.

    Keyed on the JOURNAL ENTRY rather than on the token, which is what makes a
    new preview invalidate every earlier one for the same settlement. Keying on
    the token (the previous shape) meant every call to
    :func:`get_unsettle_preview` minted an ADDITIONAL independently-valid permit
    to reverse the same entry, and they all coexisted.

    ``make_key`` applies the same ``<db_name>|`` namespace prefix
    ``frappe.cache().hset`` applies, so this stays inside the site's own cache
    namespace. The value is written and read with raw Redis commands rather than
    the pickling helpers, because only raw commands can consume it atomically —
    and because ``hget`` consults ``frappe.local.cache`` first, which can return
    a token already consumed earlier in the same request.
    """
    return frappe.cache().make_key(f"jarz_pos:unsettle_preview:{journal_entry}")


def _mint_unsettle_preview_token(journal_entry: str, pos_profile: str) -> str:
    """Store a fresh single-use token for *journal_entry*, replacing any prior one."""
    token = frappe.generate_hash(length=32)
    payload = json.dumps({"token": token, "pos_profile": pos_profile}, sort_keys=True)
    # One SET carrying its own expiry. The previous shape was `hset` followed by
    # `expire(cache_key, 180)` — but `hset` namespaces the key through `make_key`
    # while the inherited `expire` does not, so that call set a TTL on a key that
    # did not exist and silently returned False. The token it was meant to expire
    # had NO expiry at all and stayed valid indefinitely.
    frappe.cache().set(_unsettle_preview_key(journal_entry), payload, ex=UNSETTLE_PREVIEW_TTL)
    return token


def _peek_unsettle_preview_token(journal_entry: str, token: str):
    """Validate the preview token WITHOUT spending it.

    Returns ``(payload, raw)`` when the token is live and matches, else
    ``(None, None)``. ``raw`` is the exact stored bytes, and is what
    :func:`_spend_unsettle_preview_token` compares against — so the pair form a
    compare-and-swap across the guards that run between them.

    Reading before spending is deliberate: the branch guards need the branch out
    of the payload, and a caller refused for a fixable reason (their shift is not
    open yet) should still be holding a usable token afterwards rather than
    having to re-preview.
    """
    key = _unsettle_preview_key(journal_entry)
    # Same deploy-window hazard as the settle path, and worse here: the old
    # shape wrote a HASH at these exact key bytes AND its TTL never applied, so
    # a stale hash does not age out — it survives until a new preview SETs over
    # it. `GET` against a hash raises WRONGTYPE, which unhandled was a 500 on a
    # manager reversing a wrong-branch settlement. Degrade to "no permit".
    try:
        raw = frappe.cache().get(key)
    except Exception:
        _log_preview_cache_failure("unsettle")
        return None, None
    if raw is None:
        return None, None

    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return None, None
    if not isinstance(payload, dict):
        return None, None

    stored = str(payload.get("token") or "")
    presented = str(token or "")
    if not stored or not presented or not hmac.compare_digest(stored, presented):
        return None, None

    return payload, raw


def _spend_unsettle_preview_token(journal_entry: str, raw) -> bool:
    """Atomically consume the token whose exact bytes are *raw*.

    True only for the caller that actually removed it. Anyone whose value
    changed in the meantime — because another caller spent it, or because a
    newer preview replaced it — gets False and must be refused.
    """
    return bool(
        frappe.cache().eval(
            _UNSETTLE_TOKEN_CONSUME_LUA, 1, _unsettle_preview_key(journal_entry), raw
        )
    )


def _ensure_unsettle_access() -> None:
    _ensure_unsettle_released()
    roles = {str(role or "").strip() for role in (frappe.get_roles() or []) if str(role or "").strip()}
    allowed = ROLES.ADMIN | ROLES.LINE_MANAGER_TIER
    if not roles.intersection(allowed):
        frappe.throw(
            "Not permitted: Manager access required to reverse a courier settlement",
            frappe.PermissionError,
        )


@frappe.whitelist()  # type: ignore[attr-defined]
def get_unsettle_preview(journal_entry: str):
    """Preview reversing a posted courier settlement. Read-only — posts nothing.

    Mints a short-lived ``preview_token`` (3 minutes, the same window
    :func:`generate_settlement_preview` uses) that :func:`unsettle_courier_settlement`
    must be called with — the same server-driven preview/confirm pairing every
    other money-moving courier action in this module follows.

    Returns:
      {
        journal_entry, company, pos_profile, posting_date, title, user_remark,
        courier_transactions: [...], reversal_lines: [...],
        already_reversed, reversal_journal_entry,
        preview_token, expires_in
      }
    """
    _ensure_unsettle_access()

    journal_entry = (journal_entry or "").strip()
    if not journal_entry:
        frappe.throw("journal_entry is required")

    data = _get_unsettle_preview(journal_entry)

    branch = data.get("pos_profile")
    if not branch:
        frappe.throw(
            f"Could not determine which branch Journal Entry {journal_entry} belongs to; "
            "refusing to reverse it without a resolvable branch."
        )
    # The branch is derived from the settlement itself, never taken from the
    # caller — this only checks that the CALLER is scoped to it. A preview
    # posts nothing, so (like generate_settlement_preview) no shift is
    # required to look.
    _guard_branch_action(branch, action_label="reversing a courier settlement", require_shift=False)

    if data.get("already_reversed"):
        frappe.throw(
            f"Journal Entry {journal_entry} was already reversed by "
            f"{data.get('reversal_journal_entry')}."
        )

    token = _mint_unsettle_preview_token(journal_entry, branch)

    return {**data, "preview_token": token, "expires_in": UNSETTLE_PREVIEW_TTL}


@frappe.whitelist()  # type: ignore[attr-defined]
def unsettle_courier_settlement(journal_entry: str, preview_token: str, reason: str | None = None):
    """Confirm and perform the reversal previewed by :func:`get_unsettle_preview`.

    Posts a reversing Journal Entry and flips every Courier Transaction the
    original settlement closed back to Unsettled. Never touches the original
    entry. Refuses outright — rather than posting a second reversing entry —
    if this settlement was already reversed.
    """
    _ensure_unsettle_access()

    journal_entry = (journal_entry or "").strip()
    if not journal_entry:
        frappe.throw("journal_entry is required")
    if not preview_token:
        frappe.throw("preview_token is required")

    # Validate now, spend just before the reversal. There is no separate
    # journal_entry match to make any more — the key IS the journal entry, so a
    # token minted for another settlement is simply not found here.
    cached, raw_token = _peek_unsettle_preview_token(journal_entry, preview_token)
    if not cached:
        frappe.throw("Preview expired or invalid. Please reopen the dialog.")

    branch = cached.get("pos_profile")
    if not branch:
        frappe.throw(
            f"Could not determine which branch Journal Entry {journal_entry} belongs to; "
            "refusing to reverse it without a resolvable branch."
        )
    # Posts money, so (like confirm_settlement) the branch must actually be open.
    _guard_branch_action(branch, action_label="reversing a courier settlement", require_shift=True)

    # Spend the token BEFORE the reversal, not after, and spend it INSIDE Redis.
    # Both halves matter. Consuming up front closes the window in which two
    # holders of one token both reach the service call; doing it as a
    # compare-and-delete closes the narrower window in which they both pass a
    # plain check before either deletes. Whoever loses this is refused here,
    # having posted nothing.
    if not _spend_unsettle_preview_token(journal_entry, raw_token):
        frappe.throw(
            "This preview was already used, or a newer one replaced it. "
            "Please reopen the dialog."
        )

    # The service function owns its own savepoint/commit/rollback (mirroring every
    # other settlement builder in that module) — this wrapper does not double it.
    try:
        result = _unsettle_courier_settlement(journal_entry, pos_profile=branch, reason=reason)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "unsettle_courier_settlement failed")
        # The token is already consumed at this point, so a bare re-raise would
        # leave the caller stuck retrying with a preview_token that can never
        # work again. Tell them the clean path explicitly.
        frappe.throw(
            f"Reversing Journal Entry {journal_entry} failed and the preview token has "
            "been consumed. Please reopen the reversal dialog to generate a new preview "
            "and try again.",
            exc=e,
        )

    return result


def _coerce_include_reversed(value) -> bool:
    """Tolerant truthiness for ``include_reversed``.

    Called both from Python (a real ``bool``) and over HTTP, where a query
    string or form field arrives as text — ``int()`` alone chokes on
    ``"true"``/``"false"``, which is exactly the shape a browser or a REST
    client sends for a boolean toggle.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


@frappe.whitelist()  # type: ignore[attr-defined]
def list_recent_settlements(
    pos_profile: str | None = None,
    limit: int = 50,
    include_reversed: bool = False,
    days: int = 90,
):
    """List recent courier settlements so a manager can FIND one to reverse.

    Reversal (:func:`unsettle_courier_settlement`) is keyed on a Journal Entry
    NAME, which nothing on a phone ever surfaces — ``get_courier_balances`` only
    ever describes UNSETTLED positions, and every settle call site discards the
    ``journal_entry`` its own response carries. This is the server-side list
    that replaces the mobile client's on-device settlement cache, which cannot
    help the exact case this exists for: on 2026-09-02 five Nasr City Courier
    Transactions were settled from the wrong branch till, and whoever needs to
    reverse that may not be on the device that did it.

    Gated with the SAME manager-tier check :func:`unsettle_courier_settlement`
    itself uses — someone who cannot reverse a settlement has no reason to
    browse this list. Branch-scoped exactly like :func:`get_courier_balances`:
    ``pos_profile`` narrows to one branch (and is access-checked), omitting it
    returns every branch the caller is assigned to, and neither form can reach
    a branch the caller is not a member of.

    ``include_reversed`` defaults to False, so the list shows only what can
    still be acted on; pass True to include already-reversed settlements for
    audit.

    ``days`` bounds the scan to a posting-date window (default 90, clamped to
    1..365 by the service). It is forwarded rather than dropped: the service
    has always taken it, but this wrapper did not accept it, so every caller —
    including the mobile dialog — was hard-pinned to 90 days with no way to
    look further back at an older settlement.

    Returns a list of rows, newest first:
      {
        journal_entry, posting_date, party_type, party, display_name,
        pos_profile, net_amount, transaction_count,
        already_reversed, reversal_journal_entry
      }
    """
    _ensure_unsettle_access()
    return _list_recent_courier_settlements(
        pos_profile=pos_profile,
        limit=limit,
        include_reversed=_coerce_include_reversed(include_reversed),
        days=days,
    )
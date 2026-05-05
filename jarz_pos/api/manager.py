"""Manager Dashboard APIs for Branch Live Feed.

Endpoints:
- get_manager_dashboard_summary: list accessible POS Profiles with cash account and current balance.
- get_manager_orders: recent invoices feed filtered by branch (POS Profile) or all, optional by state.
- get_manager_states: return available Sales Invoice state options (same as Kanban columns).
- update_cancelled_invoice_status_fields: update limited workflow fields on cancelled Sales Invoices.
- update_invoice_branch: reassign a submitted Sales Invoice by changing custom_kanban_profile only.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
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

try:
    from jarz_pos.utils.invoice_utils import format_invoice_data
except Exception:
    def format_invoice_data(invoice_doc):  # type: ignore
        return {"name": getattr(invoice_doc, "name", None)}

try:
    from jarz_pos.services.invoice_creation import create_pos_invoice as _create_amendment_invoice
except Exception:
    _create_amendment_invoice = None  # type: ignore


# Allowed states for invoice transfer (normalized: lowercase, no extra spaces)
# These match the actual field values: "Received", "In Progress", "Ready"
# Note: "recieved" (misspelled) included for backward compatibility with existing data
_ALLOWED_TRANSFER_STATES = {"received", "recieved", "in progress", "ready", "preparing"}
_ALLOWED_AMENDMENT_STATES = _ALLOWED_TRANSFER_STATES


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


def _resolve_pos_profile_warehouse(pos_profile_name: str) -> str:
    """Resolve the stock source warehouse for a POS Profile."""
    warehouse = (frappe.db.get_value("POS Profile", pos_profile_name, "warehouse") or "").strip()
    if not warehouse:
        raise frappe.ValidationError(_(f"Target POS Profile {pos_profile_name} has no warehouse configured."))
    if not frappe.db.exists("Warehouse", warehouse):
        raise frappe.ValidationError(_(f"Configured warehouse {warehouse} for POS Profile {pos_profile_name} was not found."))
    return warehouse


def _validate_transfer_target_warehouse(inv: Any, target_warehouse: str) -> None:
    """Ensure the target warehouse is compatible with the Sales Invoice company."""
    invoice_company = str(inv.get("company") or "").strip()
    warehouse_company = str(frappe.db.get_value("Warehouse", target_warehouse, "company") or "").strip()
    if invoice_company and warehouse_company and invoice_company != warehouse_company:
        raise frappe.ValidationError(_(f"Target warehouse {target_warehouse} belongs to a different company."))


def _get_transfer_stock_rows(inv: Any) -> List[Any]:
    """Return invoice rows whose warehouse must follow branch reassignment."""
    stock_rows: List[Any] = []
    item_stock_cache: Dict[str, bool] = {}

    for row in list(getattr(inv, "items", []) or []):
        item_code = str(getattr(row, "item_code", "") or "").strip()
        if not item_code:
            continue
        if item_code not in item_stock_cache:
            item_stock_cache[item_code] = bool(int(frappe.db.get_value("Item", item_code, "is_stock_item") or 0))
        if item_stock_cache[item_code]:
            stock_rows.append(row)

    return stock_rows


def _find_submitted_delivery_notes(invoice_name: str) -> List[str]:
    """Return submitted Delivery Notes already linked to the Sales Invoice."""
    rows = frappe.get_all(
        "Delivery Note Item",
        filters={"against_sales_invoice": invoice_name, "docstatus": 1},
        pluck="parent",
        limit_page_length=20,
    ) or []
    return sorted({row for row in rows if row})


def _find_submitted_payment_entries(invoice_name: str) -> List[str]:
    """Return submitted Payment Entries already linked to the Sales Invoice."""
    ref_rows = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice_name,
            "parenttype": "Payment Entry",
        },
        pluck="parent",
        limit_page_length=20,
    ) or []
    payment_entry_names = sorted({row for row in ref_rows if row})
    if not payment_entry_names:
        return []

    submitted = frappe.get_all(
        "Payment Entry",
        filters={"name": ["in", payment_entry_names], "docstatus": 1},
        pluck="name",
        limit_page_length=20,
    ) or []
    return sorted({row for row in submitted if row})


def _find_courier_transactions(invoice_name: str) -> List[str]:
    """Return courier transactions already linked to the Sales Invoice."""
    try:
        rows = frappe.get_all(
            "Courier Transaction",
            filters={"reference_invoice": invoice_name},
            pluck="name",
            limit_page_length=20,
        ) or []
    except Exception:
        rows = []
    return sorted({row for row in rows if row})


def _find_sales_partner_transactions(invoice_name: str) -> List[str]:
    """Return Sales Partner Transaction rows already linked to the Sales Invoice."""
    try:
        rows = frappe.get_all(
            "Sales Partner Transactions",
            filters={"reference_invoice": invoice_name},
            pluck="name",
            limit_page_length=20,
        ) or []
    except Exception:
        rows = []
    return sorted({row for row in rows if row})


def _find_submitted_journal_entries(invoice_name: str) -> List[str]:
    """Return submitted Journal Entries that already settled against the invoice."""
    journal_entry_names = set()

    try:
        title_rows = frappe.get_all(
            "Journal Entry",
            filters={"docstatus": 1, "title": ["like", f"%{invoice_name}%"]},
            pluck="name",
            limit_page_length=20,
        ) or []
        journal_entry_names.update(row for row in title_rows if row)
    except Exception:
        pass

    try:
        remark_rows = frappe.get_all(
            "Journal Entry",
            filters={"docstatus": 1, "user_remark": ["like", f"%{invoice_name}%"]},
            pluck="name",
            limit_page_length=20,
        ) or []
        journal_entry_names.update(row for row in remark_rows if row)
    except Exception:
        pass

    try:
        ref_rows = frappe.get_all(
            "Journal Entry Account",
            filters={
                "reference_type": "Sales Invoice",
                "reference_name": invoice_name,
                "parenttype": "Journal Entry",
            },
            pluck="parent",
            limit_page_length=20,
        ) or []
        ref_names = sorted({row for row in ref_rows if row})
        if ref_names:
            submitted = frappe.get_all(
                "Journal Entry",
                filters={"name": ["in", ref_names], "docstatus": 1},
                pluck="name",
                limit_page_length=20,
            ) or []
            journal_entry_names.update(row for row in submitted if row)
    except Exception:
        pass

    return sorted(journal_entry_names)


def _get_active_delivery_trip_name(inv: Any) -> Optional[str]:
    """Return the linked delivery trip when it is still operationally active."""
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()
    trip_name = str(getattr(inv, "custom_delivery_trip", "") or inv.get("custom_delivery_trip") or "").strip()
    if not trip_name and invoice_name:
        try:
            linked_trips = frappe.get_all(
                "Delivery Trip Invoice",
                filters={"invoice": invoice_name},
                pluck="parent",
                limit_page_length=5,
            ) or []
            trip_name = next((row for row in linked_trips if row), "")
        except Exception:
            trip_name = ""
    if not trip_name:
        return None

    try:
        trip_status = str(frappe.db.get_value("Delivery Trip", trip_name, "status") or "").strip()
    except Exception:
        return trip_name

    if not trip_status or trip_status != "Completed":
        return trip_name
    return None


def get_invoice_hard_mutation_blocker(inv: Any) -> Optional[Dict[str, Any]]:
    """Return the first downstream artifact that blocks cancel/amend mutations."""
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()
    if not invoice_name:
        return None

    delivery_notes = _find_submitted_delivery_notes(invoice_name)
    if delivery_notes:
        return {
            "mutation_block_code": "delivery_note_exists",
            "mutation_block_reason": _("This invoice already has a submitted Delivery Note and must use a corrective workflow."),
            "delivery_notes": delivery_notes,
        }

    active_trip = _get_active_delivery_trip_name(inv)
    if active_trip:
        return {
            "mutation_block_code": "delivery_trip_exists",
            "mutation_block_reason": _("This invoice is already linked to an active delivery trip and cannot be changed from this workflow."),
            "delivery_trip": active_trip,
        }

    courier_transactions = _find_courier_transactions(invoice_name)
    if courier_transactions:
        return {
            "mutation_block_code": "courier_transaction_exists",
            "mutation_block_reason": _("This invoice already has courier settlement artifacts and cannot be changed from this workflow."),
            "courier_transactions": courier_transactions,
        }

    sales_partner_transactions = _find_sales_partner_transactions(invoice_name)
    if sales_partner_transactions:
        return {
            "mutation_block_code": "sales_partner_transaction_exists",
            "mutation_block_reason": _("This invoice already has sales partner settlement artifacts and cannot be changed from this workflow."),
            "sales_partner_transactions": sales_partner_transactions,
        }

    journal_entries = _find_submitted_journal_entries(invoice_name)
    if journal_entries:
        return {
            "mutation_block_code": "journal_entry_exists",
            "mutation_block_reason": _("This invoice already has settlement journal entries and cannot be changed from this workflow."),
            "journal_entries": journal_entries,
        }

    return None


def get_invoice_amendment_eligibility(inv: Any) -> Dict[str, Any]:
    """Return whether a submitted POS invoice can start the ERP-first amendment flow."""
    invoice_name = str(getattr(inv, "name", None) or inv.get("name") or "").strip()
    current_state = str(inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "").strip()
    normalized_state = current_state.lower()

    def _blocked(code: str, reason: str, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "can_amend": False,
            "amendment_block_code": code,
            "amendment_block_reason": reason,
        }
        payload.update(extra)
        return payload

    if not invoice_name:
        return _blocked("invoice_missing", _("Invoice was not found."))

    if int(inv.get("docstatus") or 0) != 1:
        return _blocked("invoice_not_submitted", _("Only submitted invoices can be amended."))

    if int(inv.get("is_return") or 0):
        return _blocked("return_invoice", _("Return invoices cannot be amended from this workflow."))

    if normalized_state not in _ALLOWED_AMENDMENT_STATES:
        return _blocked(
            "state_not_supported",
            _("This invoice can only be amended before dispatch while it is still in an operational prep state."),
        )

    mutation_blocker = get_invoice_hard_mutation_blocker(inv)
    if mutation_blocker:
        return _blocked(
            mutation_blocker.get("mutation_block_code") or "mutation_blocked",
            mutation_blocker.get("mutation_block_reason") or _("This invoice cannot be changed from this workflow."),
            **{
                key: value
                for key, value in mutation_blocker.items()
                if key not in {"mutation_block_code", "mutation_block_reason"}
            },
        )

    return {
        "can_amend": True,
        "amendment_block_code": None,
        "amendment_block_reason": None,
    }


def _derive_required_delivery_datetime(inv: Any) -> Optional[str]:
    """Derive the delivery start datetime from the invoice's stored slot fields."""
    delivery_date = str(inv.get("custom_delivery_date") or "").strip()
    delivery_time_from = str(inv.get("custom_delivery_time_from") or "").strip()
    if not delivery_date or not delivery_time_from:
        return None
    normalized_time = delivery_time_from if len(delivery_time_from) > 5 else f"{delivery_time_from}:00"
    return f"{delivery_date} {normalized_time}"


def _derive_delivery_end_datetime(inv: Any) -> Optional[str]:
    """Derive the delivery end datetime from the invoice's duration metadata."""
    start_text = _derive_required_delivery_datetime(inv)
    if not start_text:
        return None

    raw_duration = inv.get("custom_delivery_duration")
    if raw_duration in (None, ""):
        return None

    try:
        start_dt = frappe.utils.get_datetime(start_text)
        if isinstance(raw_duration, str) and ":" in raw_duration:
            parts = [int(part or 0) for part in raw_duration.split(":")]
            while len(parts) < 3:
                parts.append(0)
            duration_seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            duration_seconds = int(float(raw_duration or 0))
        if duration_seconds <= 0:
            return None
        return frappe.utils.add_to_date(start_dt, seconds=duration_seconds, as_string=True)
    except Exception:
        return None


@contextmanager
def _temporary_invoice_creation_form_context(
    *,
    required_delivery_datetime: Optional[str] = None,
    delivery_end_datetime: Optional[str] = None,
) -> Any:
    """Temporarily seed form_dict so invoice creation keeps the chosen slot duration."""
    previous_form_dict = getattr(frappe, "form_dict", None)
    next_form_dict = frappe._dict(dict(previous_form_dict or {}))
    if required_delivery_datetime:
        next_form_dict["required_delivery_datetime"] = required_delivery_datetime
    if delivery_end_datetime:
        next_form_dict["delivery_end_datetime"] = delivery_end_datetime
    frappe.form_dict = next_form_dict
    try:
        yield
    finally:
        frappe.form_dict = previous_form_dict


def _build_invoice_amendment_request_id(
    *,
    invoice_id: str,
    cart_json: Any,
    pos_profile_name: Optional[str],
    customer_name: Optional[str],
    shipping_address_name: Optional[str],
    required_delivery_datetime: Optional[str],
    delivery_end_datetime: Optional[str],
    sales_partner: Optional[str],
    payment_type: Optional[str],
    pickup: Union[bool, int, str, None],
    payment_method: Optional[str],
    provided_idempotency_key: Optional[str] = None,
) -> str:
    """Build a stable idempotency key for amendment retries of the same payload."""
    provided = str(provided_idempotency_key or "").strip()
    if provided:
        return provided

    try:
        normalized_cart = frappe.parse_json(cart_json) if isinstance(cart_json, str) else cart_json
    except Exception:
        normalized_cart = cart_json

    payload = {
        "invoice_id": invoice_id,
        "cart": normalized_cart,
        "pos_profile_name": pos_profile_name,
        "customer_name": customer_name,
        "shipping_address_name": shipping_address_name,
        "required_delivery_datetime": required_delivery_datetime,
        "delivery_end_datetime": delivery_end_datetime,
        "sales_partner": sales_partner,
        "payment_type": payment_type,
        "pickup": _is_truthy_flag(pickup),
        "payment_method": payment_method,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return f"amd-{invoice_id}-{digest[:16]}"


def _find_existing_amendment_invoice(source_invoice_id: str) -> Optional[str]:
    """Return the existing replacement invoice for a cancelled source invoice when present."""
    try:
        rows = frappe.get_all(
            "Sales Invoice",
            filters={"amended_from": source_invoice_id, "docstatus": ["!=", 2]},
            pluck="name",
            order_by="creation desc",
            limit_page_length=1,
        ) or []
    except Exception:
        rows = []
    return rows[0] if rows else None


def _add_invoice_audit_comment(invoice_name: str, comment: str) -> None:
    """Add a best-effort audit comment to an invoice."""
    if not comment:
        return
    try:
        frappe.get_doc("Sales Invoice", invoice_name).add_comment("Comment", comment)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Invoice amendment audit comment failed for {invoice_name}")


def _mark_source_invoice_as_amended(
    source_invoice_name: str,
    *,
    replacement_invoice_name: str,
    request_id: str,
    initiated_by: str,
) -> None:
    """Persist structured amendment metadata on the superseded source invoice."""
    meta = frappe.get_meta("Sales Invoice")
    reason_text = (
        f"Superseded by {replacement_invoice_name} through POS amendment flow. "
        f"Request ID: {request_id}. Initiated by: {initiated_by}."
    )
    updates: Dict[str, Any] = {}
    if meta.get_field("custom_cancellation_type"):
        updates["custom_cancellation_type"] = "Amended"
    if meta.get_field("custom_cancellation_reason"):
        updates["custom_cancellation_reason"] = reason_text
    if updates:
        frappe.db.set_value("Sales Invoice", source_invoice_name, updates, update_modified=False)


def _build_invoice_amendment_response(
    *,
    request_id: str,
    source_invoice_name: str,
    replacement_invoice_name: str,
    cancelled_payment_entries: Optional[List[str]] = None,
    already_processed: bool = False,
) -> Dict[str, Any]:
    """Return the stable API response for a completed amendment orchestration."""
    replacement_invoice = frappe.get_doc("Sales Invoice", replacement_invoice_name)
    return {
        "success": True,
        "request_id": request_id,
        "source_invoice_id": source_invoice_name,
        "replacement_invoice_id": replacement_invoice_name,
        "cancelled_payment_entries": cancelled_payment_entries or [],
        "already_processed": already_processed,
        "invoice": format_invoice_data(replacement_invoice),
    }


def _run_invoice_amendment_job(
    *,
    invoice_id: str,
    request_id: str,
    cart_json: Any,
    customer_name: Optional[str] = None,
    shipping_address_name: Optional[str] = None,
    pos_profile_name: Optional[str] = None,
    required_delivery_datetime: Optional[str] = None,
    delivery_end_datetime: Optional[str] = None,
    sales_partner: Optional[str] = None,
    payment_type: Optional[str] = None,
    pickup: Union[bool, int, str, None] = None,
    payment_method: Optional[str] = None,
    initiated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Queueable job that supersedes a submitted invoice and recreates it from the POS payload."""
    if _create_amendment_invoice is None:
        frappe.throw(_("Invoice amendment service is unavailable."))

    logger = frappe.logger("jarz_pos.api.manager", allow_site=frappe.local.site)
    source_invoice = frappe.get_doc("Sales Invoice", invoice_id)
    existing_replacement = _find_existing_amendment_invoice(invoice_id)
    if existing_replacement:
        return _build_invoice_amendment_response(
            request_id=request_id,
            source_invoice_name=invoice_id,
            replacement_invoice_name=existing_replacement,
            already_processed=True,
        )

    eligibility = get_invoice_amendment_eligibility(source_invoice)
    if not eligibility.get("can_amend"):
        return {
            "success": False,
            "request_id": request_id,
            "error": eligibility.get("amendment_block_reason") or _("Invoice amendment is blocked."),
            "amendment_block_code": eligibility.get("amendment_block_code"),
        }

    cancelled_payment_entries: List[str] = []
    effective_customer_name = (customer_name or source_invoice.get("customer") or "").strip() or "Walking Customer"
    effective_shipping_address_name = (
        shipping_address_name
        or source_invoice.get("shipping_address_name")
        or source_invoice.get("customer_address")
        or None
    )
    effective_pos_profile = (
        pos_profile_name
        or source_invoice.get("custom_kanban_profile")
        or source_invoice.get("pos_profile")
        or ""
    ).strip()
    effective_sales_partner = (sales_partner if sales_partner is not None else source_invoice.get("sales_partner") or None)
    effective_payment_method = (
        payment_method if payment_method is not None else source_invoice.get("custom_payment_method") or None
    )
    effective_pickup = _is_truthy_flag(pickup) or _is_truthy_flag(source_invoice.get("custom_is_pickup"))
    effective_required_delivery_datetime = required_delivery_datetime or _derive_required_delivery_datetime(source_invoice)
    effective_delivery_end_datetime = delivery_end_datetime or _derive_delivery_end_datetime(source_invoice)
    woo_order_id = source_invoice.get("woo_order_id")
    initiated_by = (initiated_by or frappe.session.user or "Unknown User").strip()

    save_point = f"invoice_amendment_{hashlib.sha1(request_id.encode('utf-8')).hexdigest()[:10]}"
    try:
        frappe.db.savepoint(save_point)
    except Exception:
        save_point = ""

    try:
        payment_entries = _find_submitted_payment_entries(invoice_id)
        for payment_entry_name in payment_entries:
            payment_entry = frappe.get_doc("Payment Entry", payment_entry_name)
            if int(payment_entry.get("docstatus") or 0) != 1:
                continue
            payment_entry.flags.ignore_permissions = True
            payment_entry.cancel()
            cancelled_payment_entries.append(payment_entry.name)

        source_invoice.flags.ignore_permissions = True
        source_invoice.flags.ignore_woo_outbound = True
        source_invoice.cancel()

        with _temporary_invoice_creation_form_context(
            required_delivery_datetime=effective_required_delivery_datetime,
            delivery_end_datetime=effective_delivery_end_datetime,
        ):
            creation_result = _create_amendment_invoice(
                cart_json,
                effective_customer_name,
                effective_pos_profile,
                None,
                effective_required_delivery_datetime,
                effective_shipping_address_name,
                effective_sales_partner,
                payment_type,
                effective_pickup,
                effective_payment_method,
                amended_from=invoice_id,
                woo_order_id=woo_order_id,
            )

        replacement_invoice_name = (
            creation_result.get("invoice_name")
            or creation_result.get("name")
            or ""
        )
        if not replacement_invoice_name:
            frappe.throw(_("Invoice amendment did not return the replacement invoice name."))

        _mark_source_invoice_as_amended(
            invoice_id,
            replacement_invoice_name=replacement_invoice_name,
            request_id=request_id,
            initiated_by=initiated_by,
        )
        _add_invoice_audit_comment(
            invoice_id,
            (
                f"Invoice amended by {initiated_by}. Superseded by {replacement_invoice_name}. "
                f"Request ID: {request_id}."
            ),
        )
        _add_invoice_audit_comment(
            replacement_invoice_name,
            (
                f"Created as amendment of {invoice_id} by {initiated_by}. "
                f"Request ID: {request_id}."
            ),
        )

        logger.info(
            {
                "event": "invoice_amendment_completed",
                "source_invoice": invoice_id,
                "replacement_invoice": replacement_invoice_name,
                "request_id": request_id,
                "cancelled_payment_entries": cancelled_payment_entries,
            }
        )
        return _build_invoice_amendment_response(
            request_id=request_id,
            source_invoice_name=invoice_id,
            replacement_invoice_name=replacement_invoice_name,
            cancelled_payment_entries=cancelled_payment_entries,
        )
    except Exception as exc:
        if save_point:
            frappe.db.rollback(save_point=save_point)
        logger.error(
            {
                "event": "invoice_amendment_failed",
                "source_invoice": invoice_id,
                "request_id": request_id,
                "error": str(exc),
            }
        )
        frappe.log_error(frappe.get_traceback(), "submit_invoice_amendment failed")
        return {"success": False, "request_id": request_id, "error": str(exc)}


@frappe.whitelist(allow_guest=False)
def submit_invoice_amendment(
    invoice_id: str,
    cart_json: Any,
    customer_name: Optional[str] = None,
    shipping_address_name: Optional[str] = None,
    pos_profile_name: Optional[str] = None,
    required_delivery_datetime: Optional[str] = None,
    delivery_end_datetime: Optional[str] = None,
    sales_partner: Optional[str] = None,
    payment_type: Optional[str] = None,
    pickup: Union[bool, int, str, None] = None,
    payment_method: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Supersede a submitted invoice and recreate it from the edited POS cart payload."""
    _ensure_manager_dashboard_access()

    invoice_id = (invoice_id or "").strip()
    if not invoice_id:
        return {"success": False, "error": "invoice_id is required"}
    if not cart_json:
        return {"success": False, "error": "cart_json is required"}

    source_invoice = frappe.get_doc("Sales Invoice", invoice_id)
    frappe.has_permission("Sales Invoice", doc=source_invoice, ptype="write", throw=True)

    existing_replacement = _find_existing_amendment_invoice(invoice_id)
    request_id = _build_invoice_amendment_request_id(
        invoice_id=invoice_id,
        cart_json=cart_json,
        pos_profile_name=pos_profile_name,
        customer_name=customer_name,
        shipping_address_name=shipping_address_name,
        required_delivery_datetime=required_delivery_datetime or _derive_required_delivery_datetime(source_invoice),
        delivery_end_datetime=delivery_end_datetime or _derive_delivery_end_datetime(source_invoice),
        sales_partner=sales_partner if sales_partner is not None else source_invoice.get("sales_partner"),
        payment_type=payment_type,
        pickup=pickup,
        payment_method=payment_method if payment_method is not None else source_invoice.get("custom_payment_method"),
        provided_idempotency_key=idempotency_key,
    )
    if existing_replacement:
        return _build_invoice_amendment_response(
            request_id=request_id,
            source_invoice_name=invoice_id,
            replacement_invoice_name=existing_replacement,
            already_processed=True,
        )

    if int(source_invoice.get("docstatus") or 0) != 1:
        return {"success": False, "request_id": request_id, "error": "Only submitted invoices can be amended"}

    eligibility = get_invoice_amendment_eligibility(source_invoice)
    if not eligibility.get("can_amend"):
        return {
            "success": False,
            "request_id": request_id,
            "error": eligibility.get("amendment_block_reason") or _("Invoice amendment is blocked."),
            "amendment_block_code": eligibility.get("amendment_block_code"),
        }

    return frappe.enqueue(
        "jarz_pos.api.manager._run_invoice_amendment_job",
        queue="short",
        timeout=1200,
        now=True,
        job_id=request_id,
        invoice_id=invoice_id,
        request_id=request_id,
        cart_json=cart_json,
        customer_name=customer_name,
        shipping_address_name=shipping_address_name,
        pos_profile_name=pos_profile_name,
        required_delivery_datetime=required_delivery_datetime,
        delivery_end_datetime=delivery_end_datetime,
        sales_partner=sales_partner,
        payment_type=payment_type,
        pickup=pickup,
        payment_method=payment_method,
        initiated_by=frappe.session.user,
    )


def _get_invoice_warehouse_mismatches(inv: Any, expected_warehouse: str) -> List[Dict[str, str]]:
    """Return stock rows whose warehouse no longer matches the operational branch."""
    mismatches: List[Dict[str, str]] = []
    for row in _get_transfer_stock_rows(inv):
        row_warehouse = str(getattr(row, "warehouse", "") or "").strip()
        if row_warehouse != expected_warehouse:
            mismatches.append(
                {
                    "row_name": str(getattr(row, "name", "") or "").strip(),
                    "item_code": str(getattr(row, "item_code", "") or "").strip(),
                    "warehouse": row_warehouse or "blank",
                }
            )
    return mismatches


def _is_truthy_flag(value: Union[bool, int, str, None]) -> bool:
    """Normalize common truthy flag inputs from whitelisted method arguments."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


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
def get_invoice_warehouse_alignment_report(
    company: Optional[str] = None,
    branch: Optional[str] = None,
    limit: Union[int, str] = 100,
) -> Dict[str, Any]:
    """List submitted POS invoices whose item warehouses no longer match the operational branch."""
    _ensure_manager_dashboard_access()
    frappe.has_permission("Sales Invoice", throw=True)

    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.ADMIN):
        frappe.throw(_("Not permitted: administrator access required"), frappe.PermissionError)

    try:
        limit_value = max(1, min(int(limit or 100), 500))
    except Exception:
        limit_value = 100

    filters: Dict[str, Any] = {"docstatus": 1, "is_pos": 1}
    if company:
        filters["company"] = company

    report_rows = frappe.get_all(
        "Sales Invoice",
        filters=filters,
        fields=["name", "company", "customer", "posting_date", "custom_kanban_profile", "pos_profile"],
        order_by="modified desc",
        limit_page_length=limit_value,
    ) or []

    misaligned_invoices: List[Dict[str, Any]] = []
    for row in report_rows:
        invoice_name = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
        if not invoice_name:
            continue

        inv = frappe.get_doc("Sales Invoice", invoice_name)
        operational_profile = inv.get("custom_kanban_profile") or inv.get("pos_profile")
        if branch and operational_profile != branch:
            continue

        submitted_delivery_notes = _find_submitted_delivery_notes(inv.name)
        if submitted_delivery_notes:
            continue

        if not operational_profile:
            misaligned_invoices.append(
                {
                    "invoice_id": inv.name,
                    "company": inv.get("company"),
                    "customer": inv.get("customer"),
                    "operational_profile": None,
                    "target_warehouse": None,
                    "delivery_notes": [],
                    "issue": "Invoice has no operational POS Profile configured.",
                    "mismatches": [],
                }
            )
            continue

        try:
            expected_warehouse = _resolve_pos_profile_warehouse(operational_profile)
            _validate_transfer_target_warehouse(inv, expected_warehouse)
        except frappe.ValidationError as validation_error:
            misaligned_invoices.append(
                {
                    "invoice_id": inv.name,
                    "company": inv.get("company"),
                    "customer": inv.get("customer"),
                    "operational_profile": operational_profile,
                    "target_warehouse": None,
                    "delivery_notes": [],
                    "issue": str(validation_error),
                    "mismatches": [],
                }
            )
            continue

        mismatches = _get_invoice_warehouse_mismatches(inv, expected_warehouse)
        if mismatches:
            misaligned_invoices.append(
                {
                    "invoice_id": inv.name,
                    "company": inv.get("company"),
                    "customer": inv.get("customer"),
                    "operational_profile": operational_profile,
                    "target_warehouse": expected_warehouse,
                    "delivery_notes": [],
                    "issue": "Invoice item warehouses do not match the operational branch warehouse.",
                    "mismatches": mismatches,
                }
            )

    return {"success": True, "count": len(misaligned_invoices), "invoices": misaligned_invoices}


@frappe.whitelist(allow_guest=False)
def repair_invoice_warehouse_alignment(
    company: Optional[str] = None,
    branch: Optional[str] = None,
    limit: Union[int, str] = 100,
    apply_changes: Union[bool, int, str, None] = False,
) -> Dict[str, Any]:
    """Dry-run or repair misaligned submitted invoices before Delivery Note creation."""
    _ensure_manager_dashboard_access()
    frappe.has_permission("Sales Invoice", throw=True)

    roles = set(frappe.get_roles())
    if not roles.intersection(ROLES.ADMIN):
        frappe.throw(_("Not permitted: administrator access required"), frappe.PermissionError)

    report = get_invoice_warehouse_alignment_report(company=company, branch=branch, limit=limit)
    apply_mode = _is_truthy_flag(apply_changes)
    if not apply_mode:
        report["mode"] = "dry_run"
        return report

    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    meta = frappe.get_meta("Sales Invoice")

    for entry in report.get("invoices", []):
        invoice_id = entry.get("invoice_id")
        if not invoice_id:
            continue

        inv = frappe.get_doc("Sales Invoice", invoice_id)
        operational_profile = inv.get("custom_kanban_profile") or inv.get("pos_profile")
        if not operational_profile:
            skipped.append({"invoice_id": invoice_id, "issue": "Invoice has no operational POS Profile configured."})
            continue

        if _find_submitted_delivery_notes(inv.name):
            skipped.append({
                "invoice_id": invoice_id,
                "issue": "Invoice already has a submitted Delivery Note; branch transfer is no longer allowed.",
            })
            continue

        try:
            target_warehouse = _resolve_pos_profile_warehouse(operational_profile)
            _validate_transfer_target_warehouse(inv, target_warehouse)
            mismatches = _get_invoice_warehouse_mismatches(inv, target_warehouse)
            if not mismatches:
                skipped.append({"invoice_id": invoice_id, "issue": "Invoice is already aligned."})
                continue

            source_warehouses = sorted({mismatch.get("warehouse") for mismatch in mismatches if mismatch.get("warehouse")})
            for row in _get_transfer_stock_rows(inv):
                current_warehouse = str(getattr(row, "warehouse", "") or "").strip()
                if current_warehouse != target_warehouse:
                    frappe.db.set_value("Sales Invoice Item", row.name, "warehouse", target_warehouse, update_modified=False)
                    row.warehouse = target_warehouse

            if meta.get_field("set_warehouse"):
                frappe.db.set_value("Sales Invoice", inv.name, "set_warehouse", target_warehouse, update_modified=True)

            try:
                source_warehouse_label = ", ".join(source_warehouses) if source_warehouses else "none"
                inv.add_comment(
                    "Edit",
                    f"Warehouse alignment repair moved item warehouses from {source_warehouse_label} to {target_warehouse} for active kanban profile {operational_profile} by {frappe.session.user}.",
                )
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Invoice warehouse alignment repair comment failed")

            frappe.db.commit()
            applied.append(
                {
                    "invoice_id": inv.name,
                    "operational_profile": operational_profile,
                    "target_warehouse": target_warehouse,
                    "repaired_rows": len(mismatches),
                }
            )
        except Exception as exc:
            frappe.db.rollback()
            skipped.append({"invoice_id": invoice_id, "issue": str(exc)})

    return {
        "success": True,
        "mode": "apply",
        "count": report.get("count", 0),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
    }


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
        frappe.has_permission("Sales Invoice", doc=inv, ptype="write", throw=True)
        
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

        existing_delivery_notes = _find_submitted_delivery_notes(inv.name)
        if existing_delivery_notes:
            return {
                "success": False,
                "error": "Invoice already has a submitted Delivery Note; branch transfer is no longer allowed.",
            }

        try:
            target_warehouse = _resolve_pos_profile_warehouse(new_branch)
            _validate_transfer_target_warehouse(inv, target_warehouse)
        except frappe.ValidationError as validation_error:
            return {"success": False, "error": str(validation_error)}

        stock_rows = _get_transfer_stock_rows(inv)
        source_warehouses = sorted({str(getattr(row, "warehouse", "") or "").strip() for row in stock_rows if str(getattr(row, "warehouse", "") or "").strip()})

        state_fields: List[str] = []
        for candidate in ["custom_sales_invoice_state", "sales_invoice_state", "custom_state", "state"]:
            if meta.get_field(candidate):
                state_fields.append(candidate)

        # Only update custom_kanban_profile, NOT pos_profile
        # pos_profile is read-only after invoice submission and cannot be changed
        updates: Dict[str, Any] = {"custom_kanban_profile": new_branch}
        if meta.get_field("set_warehouse"):
            updates["set_warehouse"] = target_warehouse
        
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
            for row in stock_rows:
                if not getattr(row, "name", None):
                    raise frappe.ValidationError(_(f"Invoice row for item {getattr(row, 'item_code', '?')} is missing a name and cannot be moved."))
                current_warehouse = str(getattr(row, "warehouse", "") or "").strip()
                if current_warehouse != target_warehouse:
                    frappe.db.set_value("Sales Invoice Item", row.name, "warehouse", target_warehouse, update_modified=False)
                    row.warehouse = target_warehouse
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
            source_warehouse_label = ", ".join(source_warehouses) if source_warehouses else "none"
            inv.add_comment(
                "Edit",
                f"Invoice transferred from {current_branch} to {new_branch}. Item warehouses moved from {source_warehouse_label} to {target_warehouse} by {frappe.session.user}.",
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Invoice transfer audit comment failed")

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
            "target_warehouse": target_warehouse,
        }
    except Exception as e:
        frappe.logger().error(f"Update Invoice Branch Error: {str(e)}")
        frappe.log_error(frappe.get_traceback(), "Manager API - Update Invoice Branch")
        return {"success": False, "error": str(e)}

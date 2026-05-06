"""
Jarz POS - Notification and polling API endpoints
Alternative to websocket-based notifications for mobile clients
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import frappe
from frappe import _
from jarz_pos.constants import QUERY_LIMITS, STATUS, WS_EVENTS, ROLES

# Firebase Admin SDK for modern FCM V1 API
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    frappe.log_error("firebase-admin package not installed. FCM notifications disabled.", "FCM Import Warning")


@frappe.whitelist(allow_guest=False)
def get_recent_invoices(minutes: int = 5) -> Dict[str, Any]:
    """
    Get Sales Invoices created/modified in the last N minutes.
    Mobile app can poll this endpoint instead of relying on websockets.
    
    Args:
        minutes: Look back this many minutes for new/updated invoices
        
    Returns:
        Dict with new invoices and updated invoices
    """
    try:
        # Calculate cutoff time
        cutoff = frappe.utils.add_to_date(frappe.utils.now(), minutes=-minutes)
        
        # Get newly created invoices
        new_invoices = frappe.get_all(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "creation": [">=", cutoff]
            },
            fields=[
                "name", "customer", "customer_name", "territory", "posting_date",
                "grand_total", "net_total", "custom_sales_invoice_state", 
                "sales_invoice_state", "status", "creation", "modified"
            ],
            order_by="creation desc",
            limit=QUERY_LIMITS.NOTIFICATIONS
        )
        
        # Get recently modified invoices (state changes, etc.)
        modified_invoices = frappe.get_all(
            "Sales Invoice", 
            filters={
                "docstatus": 1,
                "modified": [">=", cutoff],
                "creation": ["<", cutoff]  # Don't double-count new ones
            },
            fields=[
                "name", "customer", "customer_name", "territory", "posting_date",
                "grand_total", "net_total", "custom_sales_invoice_state",
                "sales_invoice_state", "status", "creation", "modified"
            ],
            order_by="modified desc",
            limit=QUERY_LIMITS.NOTIFICATIONS
        )
        
        # Format response
        response = {
            "success": True,
            "timestamp": frappe.utils.now(),
            "cutoff_time": cutoff,
            "minutes_checked": minutes,
            "new_invoices": [],
            "modified_invoices": [],
            "total_count": len(new_invoices) + len(modified_invoices)
        }
        
        # Process new invoices
        for inv in new_invoices:
            state = inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "Received"
            response["new_invoices"].append({
                "name": inv.name,
                "customer_name": inv.customer_name or inv.customer,
                "territory": inv.territory or "",
                "posting_date": str(inv.posting_date),
                "grand_total": float(inv.grand_total or 0),
                "net_total": float(inv.net_total or 0),
                "status": state,
                "doc_status": inv.status,
                "creation": str(inv.creation),
                "event_type": "new_invoice"
            })
        
        # Process modified invoices
        for inv in modified_invoices:
            state = inv.get("custom_sales_invoice_state") or inv.get("sales_invoice_state") or "Received"
            response["modified_invoices"].append({
                "name": inv.name,
                "customer_name": inv.customer_name or inv.customer,
                "territory": inv.territory or "",
                "posting_date": str(inv.posting_date),
                "grand_total": float(inv.grand_total or 0),
                "net_total": float(inv.net_total or 0),
                "status": state,
                "doc_status": inv.status,
                "modified": str(inv.modified),
                "event_type": "invoice_updated"
            })
            
        return response
        
    except Exception as e:
        frappe.log_error(f"Notifications API error: {str(e)}", "Notifications API")
        return {
            "success": False,
            "error": str(e),
            "timestamp": frappe.utils.now()
        }


@frappe.whitelist(allow_guest=False) 
def check_for_updates(last_check: Optional[str] = None) -> Dict[str, Any]:
    """
    Check for any invoice updates since the last check time.
    More efficient than get_recent_invoices for frequent polling.
    
    Args:
        last_check: ISO timestamp of last check (optional)
        
    Returns:
        Dict with has_updates flag and summary data
    """
    try:
        # Default to 5 minutes ago if no last_check provided
        if last_check:
            try:
                cutoff = frappe.utils.get_datetime(last_check)
            except:
                cutoff = frappe.utils.add_to_date(frappe.utils.now(), minutes=-5)
        else:
            cutoff = frappe.utils.add_to_date(frappe.utils.now(), minutes=-5)
        
        # Quick count of new/modified invoices
        new_count = frappe.db.count(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "creation": [">=", cutoff]
            }
        )
        
        modified_count = frappe.db.count(
            "Sales Invoice",
            filters={
                "docstatus": 1,
                "modified": [">=", cutoff],
                "creation": ["<", cutoff]
            }
        )
        
        has_updates = (new_count + modified_count) > 0
        
        return {
            "success": True,
            "has_updates": has_updates,
            "new_count": new_count,
            "modified_count": modified_count,
            "total_updates": new_count + modified_count,
            "last_check": str(cutoff),
            "current_time": frappe.utils.now(),
            "message": f"Found {new_count} new and {modified_count} modified invoices" if has_updates else "No updates found"
        }
        
    except Exception as e:
        frappe.log_error(f"Check updates error: {str(e)}", "Notifications API") 
        return {
            "success": False,
            "error": str(e),
            "timestamp": frappe.utils.now()
        }


@frappe.whitelist(allow_guest=True)  # Allow guest for debugging
def test_websocket_emission() -> Dict[str, Any]:
    """
    Test endpoint to manually emit websocket events for debugging.
    Emits both new invoice and state change events.
    """
    try:
        timestamp = frappe.utils.now()
        test_invoice_id = f"TEST-NOTIFY-{frappe.utils.now_datetime().strftime('%H%M%S')}"
        
        # Emit new invoice event
        new_invoice_payload = {
            "name": test_invoice_id,
            "customer_name": "Test Customer",
            "total": 100.0,
            "grand_total": 120.0,
            "status": "Received",
            "sales_invoice_state": "Received",
            "posting_date": str(frappe.utils.today()),
            "posting_time": str(frappe.utils.nowtime()),
            "pos_profile": "Test Profile",
            "timestamp": timestamp,
            "test_event": True
        }
        
        frappe.publish_realtime(WS_EVENTS.NEW_INVOICE, new_invoice_payload, user="*")
        
        # Emit state change event (triggers kanban refresh)
        state_change_payload = {
            "event": WS_EVENTS.INVOICE_STATE_CHANGE,
            "invoice_id": test_invoice_id,
            "old_state_key": None,  # Null old_state triggers refresh
            "new_state_key": "received",
            "old_state": None,
            "new_state": "Received",
            "timestamp": timestamp,
            "test_event": True
        }
        
        frappe.publish_realtime(WS_EVENTS.INVOICE_STATE_CHANGE, state_change_payload, user="*")
        frappe.publish_realtime(WS_EVENTS.KANBAN_UPDATE, state_change_payload, user="*")
        
        # Also emit a generic test event
        frappe.publish_realtime(WS_EVENTS.TEST_EVENT, {"message": "Test websocket emission", "timestamp": timestamp}, user="*")
        
        return {
            "success": True,
            "message": "Test websocket events emitted successfully",
            "events_sent": [
                WS_EVENTS.NEW_INVOICE,
                WS_EVENTS.INVOICE_STATE_CHANGE,
                WS_EVENTS.KANBAN_UPDATE,
                WS_EVENTS.TEST_EVENT,
            ],
            "test_invoice_id": test_invoice_id,
            "timestamp": timestamp
        }
        
    except Exception as e:
        frappe.log_error(f"Test websocket emission error: {str(e)}", "Notifications API")
        return {
            "success": False,
            "error": str(e),
            "timestamp": frappe.utils.now()
        }


@frappe.whitelist(allow_guest=False)
def get_websocket_debug_info() -> Dict[str, Any]:
    """
    Get debugging information about websocket configuration and status.
    """
    try:
        import os
        
        # Get site config
        site_config = frappe.get_site_config()
        
        return {
            "success": True,
            "site_name": frappe.local.site,
            "socketio_port": site_config.get("socketio_port", "Not configured"),
            "redis_socketio": site_config.get("redis_socketio", "Not configured"),
            "developer_mode": site_config.get("developer_mode", False),
            "user": frappe.session.user,
            "timestamp": frappe.utils.now(),
            "available_events": [
                WS_EVENTS.NEW_INVOICE,
                WS_EVENTS.INVOICE_STATE_CHANGE,
                WS_EVENTS.KANBAN_UPDATE,
                WS_EVENTS.OUT_FOR_DELIVERY_TRANSITION,
                WS_EVENTS.COURIER_OUTSTANDING,
            ]
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "timestamp": frappe.utils.now()
        }


# ---------------------------------------------------------------------------
# Mobile push registration & alert lifecycle
# ---------------------------------------------------------------------------

MAX_FCM_TOKENS_PER_BATCH = 500
DEFAULT_WALK_IN_CUSTOMER = "Walk-in"
DEFAULT_NEW_ORDER_TITLE = "New Order"
DEFAULT_ITEM_LABEL = "Item"


def _initialize_firebase_app() -> bool:
    """Initialize Firebase Admin SDK if not already initialized."""
    if not FIREBASE_AVAILABLE:
        return False
    
    try:
        # Check if already initialized
        firebase_admin.get_app()
        return True
    except ValueError:
        # Not initialized, try to initialize
        pass
    
    # Try to load service account from config
    service_account_path = frappe.local.conf.get("fcm_service_account_path")
    service_account_json = frappe.local.conf.get("fcm_service_account")
    
    try:
        if service_account_path:
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            return True
        elif service_account_json:
            if isinstance(service_account_json, str):
                service_account_json = json.loads(service_account_json)
            cred = credentials.Certificate(service_account_json)
            firebase_admin.initialize_app(cred)
            return True
        else:
            frappe.log_error("No Firebase service account configured in site_config", "FCM Init Failed")
            return False
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Firebase initialization failed")
        return False


def _disable_token(token: str) -> None:
    """Disable a Jarz Mobile Device token when FCM reports it as invalid."""
    devices = frappe.get_all(
        "Jarz Mobile Device",
        filters={"token": token},
        fields=["name", "enabled"],
    )
    if not devices:
        return

    for row in devices:
        docname = row.get("name")
        if not docname:
            continue

        doc = frappe.get_doc("Jarz Mobile Device", docname)
        if not doc.enabled:
            continue

        # Bypass duplicate-token validation by updating directly in DB
        frappe.db.set_value(
            "Jarz Mobile Device",
            docname,
            "enabled",
            0,
            update_modified=False,
        )
        frappe.logger().info(f"Disabled stale FCM token {token} for device {docname}")


def _get_mobile_device_rows_by_token(token: str) -> List[Dict[str, Any]]:
    return frappe.get_all(
        "Jarz Mobile Device",
        filters={"token": token},
        fields=["name", "user", "enabled", "modified"],
        order_by="enabled desc, modified desc",
    )


def _is_enabled_mobile_device(row: Dict[str, Any]) -> bool:
    try:
        return int(row.get("enabled") or 0) == 1
    except (TypeError, ValueError):
        return False


def _select_mobile_device_row(rows: Sequence[Dict[str, Any]], user: str) -> Optional[Dict[str, Any]]:
    if not rows:
        return None

    for candidates in (
        [row for row in rows if row.get("user") == user and _is_enabled_mobile_device(row)],
        [row for row in rows if _is_enabled_mobile_device(row)],
        [row for row in rows if row.get("user") == user],
        list(rows),
    ):
        if candidates:
            return candidates[0]

    return None


def _prune_duplicate_mobile_device_rows(rows: Sequence[Dict[str, Any]], keep_name: str) -> None:
    for row in rows:
        docname = row.get("name")
        if not docname or docname == keep_name:
            continue

        try:
            frappe.delete_doc(
                "Jarz Mobile Device",
                docname,
                ignore_permissions=True,
                force=True,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Failed pruning duplicate mobile device row {docname}",
            )


def _update_mobile_device_row(docname: str, payload: Dict[str, Any]) -> Any:
    values = dict(payload)
    values["last_seen"] = frappe.utils.now_datetime()
    frappe.db.set_value("Jarz Mobile Device", docname, values, update_modified=True)
    return frappe.get_doc("Jarz Mobile Device", docname)


def _upsert_mobile_device(payload: Dict[str, Any]) -> Any:
    token = payload["token"]
    user = payload["user"]
    rows = _get_mobile_device_rows_by_token(token)
    selected = _select_mobile_device_row(rows, user)

    if selected:
        _prune_duplicate_mobile_device_rows(rows, selected["name"])
        return _update_mobile_device_row(selected["name"], payload)

    doc = frappe.get_doc({"doctype": "Jarz Mobile Device", **payload})
    try:
        doc.insert(ignore_permissions=True)
        return doc
    except Exception:
        rows = _get_mobile_device_rows_by_token(token)
        selected = _select_mobile_device_row(rows, user)
        if not selected:
            raise

        _prune_duplicate_mobile_device_rows(rows, selected["name"])
        return _update_mobile_device_row(selected["name"], payload)

@frappe.whitelist(allow_guest=False)
def register_mobile_device(
    token: str,
    platform: Optional[str] = None,
    device_name: Optional[str] = None,
    app_version: Optional[str] = None,
    pos_profiles: Optional[str] = None,
) -> Dict[str, Any]:
    """Register or refresh an FCM token for the signed-in user."""

    token = (token or "").strip()
    if not token:
        frappe.throw("token is required")

    if len(token) > 2048:
        frappe.throw(_("FCM token is unexpectedly long"))

    user = frappe.session.user
    if not user or user == "Guest":
        frappe.throw("Authentication required to register device")

    try:
        payload = {
            "token": token,
            "user": user,
            "platform": (platform or "Android").title(),
            "device_name": device_name,
            "app_version": app_version,
            "enabled": 1,
            "pos_profiles": _normalise_pos_profile_payload(pos_profiles),
        }

        doc = _upsert_mobile_device(payload)

        return {"success": True, "device": doc.name}
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "register_mobile_device failed")
        frappe.throw(f"Unable to register device: {exc}")


@frappe.whitelist(allow_guest=False)
def register_device_token(
    token: str,
    platform: Optional[str] = None,
    device_name: Optional[str] = None,
    app_version: Optional[str] = None,
    pos_profiles: Optional[str] = None,
) -> Dict[str, Any]:
    """Backward-compatible alias for older mobile and startup callers."""

    return register_mobile_device(
        token=token,
        platform=platform,
        device_name=device_name,
        app_version=app_version,
        pos_profiles=pos_profiles,
    )


@frappe.whitelist(allow_guest=False)
def accept_invoice(invoice_name: str) -> Dict[str, Any]:
    """Backward-compatible alias for older accept endpoint callers."""

    return acknowledge_invoice(invoice_name)


@frappe.whitelist(allow_guest=False)
def acknowledge_invoice(invoice_name: str) -> Dict[str, Any]:
    """Mark an invoice alert as accepted by the current user."""

    if not invoice_name:
        frappe.throw("invoice_name is required")

    user = frappe.session.user
    doc = frappe.get_doc("Sales Invoice", invoice_name)

    if doc.docstatus != 1:
        frappe.throw("Invoice must be submitted before acknowledgement")

    _ensure_user_can_accept(doc, user)

    current_status = getattr(doc, "custom_acceptance_status", None) or "Pending"
    if current_status == "Accepted":
        return {
            "success": True,
            "already": True,
            "accepted_by": getattr(doc, "custom_accepted_by", None),
            "accepted_on": getattr(doc, "custom_accepted_on", None),
        }

    accepted_on = frappe.utils.now_datetime()
    frappe.db.set_value(
        "Sales Invoice",
        doc.name,
        {
            "custom_acceptance_status": "Accepted",
            "custom_accepted_by": user,
            "custom_accepted_on": accepted_on,
        },
        update_modified=True,
    )

    payload = _build_invoice_alert_payload(doc)
    payload.update({
        "acceptance_status": "Accepted",
        "requires_acceptance": False,
        "accepted_by": user,
        "accepted_on": accepted_on.isoformat(),
    })

    recipients = _resolve_recipients_for_payload(payload)
    _publish_invoice_accepted(payload, recipients)
    _push_invoice_accepted(payload, recipients)

    return {
        "success": True,
        "accepted_by": user,
        "accepted_on": accepted_on.isoformat(),
    }


@frappe.whitelist(allow_guest=False)
def get_pending_alerts() -> Dict[str, Any]:
    """Return unaccepted invoices for the caller's authorised POS profiles."""

    user = frappe.session.user
    profiles = _get_profiles_for_user(user)
    if not profiles:
        return {"success": True, "alerts": []}

    cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-12)
    rows = frappe.get_all(
        "Sales Invoice",
        filters={
            "docstatus": 1,
            "status": ["!=", "Cancelled"],
            "pos_profile": ["in", profiles],
            "custom_acceptance_status": ["in", [None, "", "Pending"]],
            "custom_sales_invoice_state": ["!=", "Cancelled"],
            "creation": [">", cutoff],
        },
        fields=["name"],
        order_by="creation asc",
        limit=QUERY_LIMITS.NOTIFICATIONS,
    )

    alerts: List[Dict[str, Any]] = []
    for row in rows:
        try:
            inv_doc = frappe.get_doc("Sales Invoice", row.name)
            payload = _build_invoice_alert_payload(inv_doc)
            if payload:
                alerts.append(payload)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Failed building alert payload for {row.name}")

    return {"success": True, "alerts": alerts}


def handle_invoice_submission(doc: Any) -> None:
    """Emit realtime and push notifications for a newly submitted invoice."""

    # Skip invoices without a POS profile (e.g. WooCommerce orders) —
    # they have no push-notification recipients.
    if not getattr(doc, "pos_profile", None):
        return

    try:
        frappe.logger().info(
            f"Invoice submit hook triggered for {getattr(doc, 'name', '?')} pos_profile={getattr(doc, 'pos_profile', None)}"
        )
        payload = _build_invoice_alert_payload(doc)
        if not payload:
            frappe.log_error(f"Empty payload for invoice {getattr(doc, 'name', '?')}", "Invoice Notification Skipped")
            return

        # Log the payload for debugging
        if frappe.conf.get("developer_mode"):
            frappe.msgprint(f"Invoice alert payload: requires_acceptance={payload.get('requires_acceptance')}, acceptance_status={payload.get('acceptance_status')}")

        recipients = _resolve_recipients_for_payload(payload)
        frappe.logger().info(
            f"Invoice notification payload ready invoice={payload.get('invoice_id')} recipients={len(recipients)} requires_acceptance={payload.get('requires_acceptance')}"
        )
        
        # Emit realtime notification
        _publish_invoice_alert(payload, recipients)
        
        # Send push notification
        _push_new_invoice(payload, recipients)
        
        frappe.log_error(
            f"Invoice {payload.get('invoice_id')} notification sent. "
            f"Requires acceptance: {payload.get('requires_acceptance')}, "
            f"Recipients: {len(recipients)}",
            "Invoice Notification Sent"
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "handle_invoice_submission failed")


def notify_invoice_reassignment(invoice: Union[str, Any], new_kanban_profile: str) -> None:
    """Re-issue invoice alerts for the new effective Kanban branch after reassignment.

    Args:
        invoice: Sales Invoice doc or name to notify for.
        new_kanban_profile: Effective Kanban profile that should receive the alert.

    The persisted Sales Invoice.pos_profile is not changed here. For legacy
    consumers, the emitted alert payload still mirrors the effective branch into
    both `pos_profile` and `kanban_profile`.
    """

    if not new_kanban_profile:
        return

    try:
        doc = invoice if not isinstance(invoice, str) else frappe.get_doc("Sales Invoice", invoice)
        if not doc:
            return

        original_profile = getattr(doc, "pos_profile", None)
        try:
            # Build the compatibility payload as if the invoice belongs to the new
            # branch, without mutating the persisted submitted POS Profile.
            setattr(doc, "pos_profile", new_kanban_profile)
            payload = _build_invoice_alert_payload(doc)
        finally:
            setattr(doc, "pos_profile", original_profile)

        if not payload:
            return

        payload["pos_profile"] = new_kanban_profile
        payload["kanban_profile"] = new_kanban_profile
        payload["acceptance_status"] = "Pending"
        payload["requires_acceptance"] = True

        recipients = _get_users_for_pos_profiles([new_kanban_profile])
        _publish_invoice_alert(payload, recipients)
        _push_new_invoice(payload, recipients)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"notify_invoice_reassignment failed for {getattr(invoice, 'name', invoice)}",
        )


def notify_invoice_cancellation(
    invoice: Union[str, Any],
    reason: str,
    *,
    notes: Optional[str] = None,
    credit_note: Optional[str] = None,
) -> None:
    """Notify relevant users that an invoice has been cancelled from the Kanban board."""

    try:
        if isinstance(invoice, str):
            doc = frappe.get_doc("Sales Invoice", invoice)
        else:
            doc = invoice

        payload = _build_invoice_alert_payload(doc) or {}
        payload.update(
            {
                "invoice_id": getattr(doc, "name", ""),
                "event": "invoice_cancelled",
                "reason": reason,
                "notes": notes or "",
                "credit_note": credit_note,
                "timestamp": frappe.utils.now_datetime().isoformat(),
                "sales_invoice_state": STATUS.CANCELLED,
                "cancelled_by": frappe.session.user,
            }
        )

        if not payload.get("pos_profile"):
            payload["pos_profile"] = getattr(doc, "pos_profile", None)
        if not payload.get("kanban_profile"):
            payload["kanban_profile"] = getattr(doc, "custom_kanban_profile", None)

        recipients = _resolve_recipients_for_payload(payload)
        target = recipients if recipients else "*"
        frappe.publish_realtime(WS_EVENTS.INVOICE_CANCELLED, payload, user=target)

        data_payload: Dict[str, str] = {
            "type": "invoice_cancelled",
            "invoice_id": payload.get("invoice_id", ""),
            "reason": reason,
            "pos_profile": payload.get("pos_profile", "") or "",
            "timestamp": payload.get("timestamp", frappe.utils.now_datetime().isoformat()),
            "sales_invoice_state": payload.get("sales_invoice_state", STATUS.CANCELLED),
        }
        if notes:
            data_payload["notes"] = notes
        if credit_note:
            data_payload["credit_note"] = credit_note

        tokens = _get_tokens_for_users(recipients)
        if tokens:
            _send_fcm_notifications(tokens, data_payload)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"notify_invoice_cancellation failed for {getattr(invoice, 'name', invoice)}",
        )


def _pick_display_text(*values: Any, fallback: str = "") -> str:
    for value in values:
        text = _safe_str(value).strip()
        if text:
            return text
    return fallback


def _format_total_display(value: Any) -> str:
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _format_qty_display(value: Any) -> str:
    try:
        qty = float(value or 0)
    except (TypeError, ValueError):
        return "0"

    if qty.is_integer():
        return str(int(qty))
    return f"{qty:g}"


def _parse_item_count(value: Any) -> int:
    try:
        return max(int(float(_safe_str(value) or 0)), 0)
    except (TypeError, ValueError):
        return 0


def _item_count_summary(item_count: Any) -> str:
    count = _parse_item_count(item_count)
    if count == 1:
        return "1 item"
    if count > 1:
        return f"{count} items"
    return "No items"


def _summarize_items(items: Sequence[Dict[str, Any]]) -> str:
    summary_parts: List[str] = []
    for item in list(items)[:5]:
        item_label = _pick_display_text(
            item.get("item_name"),
            item.get("item_code"),
            fallback=DEFAULT_ITEM_LABEL,
        )
        summary_parts.append(f"{item_label} x {_format_qty_display(item.get('qty', 0))}")

    if summary_parts:
        return ", ".join(summary_parts)
    return _item_count_summary(len(items))


def _enrich_invoice_display_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_items = payload.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    item_count = len(items) if isinstance(raw_items, list) else _parse_item_count(payload.get("item_count"))
    item_summary = _pick_display_text(
        payload.get("item_summary"),
        fallback=_summarize_items(items) if items else _item_count_summary(item_count),
    )
    customer_name = _pick_display_text(payload.get("customer_name"), fallback=DEFAULT_WALK_IN_CUSTOMER)
    branch_display = _pick_display_text(
        payload.get("branch_display"),
        payload.get("pos_profile"),
        payload.get("kanban_profile"),
    )
    total_display = _pick_display_text(
        payload.get("total_display"),
        fallback=_format_total_display(payload.get("grand_total")),
    )

    body_parts: List[str] = []
    if branch_display:
        body_parts.append(branch_display)
    body_parts.append(f"Total: {total_display}")
    if item_summary:
        body_parts.append(item_summary)

    payload.update(
        {
            "customer_name": customer_name,
            "branch_display": branch_display,
            "total_display": total_display,
            "item_count": item_count,
            "item_summary": item_summary,
            "title": _pick_display_text(
                payload.get("title"),
                fallback=f"{DEFAULT_NEW_ORDER_TITLE}: {customer_name}",
            ),
            "body": _pick_display_text(
                payload.get("body"),
                fallback=" | ".join(body_parts),
            ),
        }
    )
    return payload


def _resolve_notification_content(data_payload: Dict[str, str]) -> Tuple[str, str]:
    title = _pick_display_text(data_payload.get("title"))
    body = _pick_display_text(data_payload.get("body"))
    if title and body:
        return title, body

    if data_payload.get("type") == "new_invoice":
        display_payload = _enrich_invoice_display_fields(dict(data_payload))
        title = title or _pick_display_text(display_payload.get("title"), fallback=DEFAULT_NEW_ORDER_TITLE)
        body = body or _pick_display_text(display_payload.get("body"), fallback="Open Jarz POS for details")
        return title, body

    fallback_title = _pick_display_text(
        data_payload.get("customer_name"),
        _safe_str(data_payload.get("type")).replace("_", " ").title(),
        fallback="Jarz POS",
    )
    fallback_body = _pick_display_text(
        data_payload.get("reason"),
        data_payload.get("invoice_id"),
        fallback="Open Jarz POS for details",
    )
    return title or fallback_title, body or fallback_body


def _build_invoice_alert_payload(doc: Any) -> Dict[str, Any]:
    if not doc or not getattr(doc, "name", None):
        return {}

    invoice_id = getattr(doc, "name", "")
    pos_profile = getattr(doc, "pos_profile", None)
    if not pos_profile:
        return {}

    # Ensure acceptance fields are set BEFORE building payload
    _ensure_acceptance_defaults(doc)
    
    # Reload the document to get the latest acceptance status
    try:
        doc = frappe.get_doc("Sales Invoice", invoice_id)
    except Exception:
        pass  # Use the doc we have if reload fails

    customer = _pick_display_text(
        getattr(doc, "customer_name", None),
        fallback=DEFAULT_WALK_IN_CUSTOMER,
    )
    state = (
        getattr(doc, "custom_sales_invoice_state", None)
        or getattr(doc, "sales_invoice_state", None)
        or getattr(doc, "status", None)
        or "Received"
    )

    items: List[Dict[str, Any]] = []
    try:
        for row in getattr(doc, "items", [])[:15]:
            item_code = _pick_display_text(getattr(row, "item_code", None))
            items.append(
                {
                    "item_code": item_code,
                    "item_name": _pick_display_text(
                        getattr(row, "item_name", None),
                        item_code,
                        fallback=DEFAULT_ITEM_LABEL,
                    ),
                    "qty": float(getattr(row, "qty", 0) or 0),
                }
            )
    except Exception:
        pass

    acceptance_status = getattr(doc, "custom_acceptance_status", None) or "Pending"

    payload: Dict[str, Any] = {
        "invoice_id": invoice_id,
        "name": invoice_id,
        "customer_name": customer,
        "grand_total": float(getattr(doc, "grand_total", 0) or 0),
        "net_total": float(getattr(doc, "net_total", 0) or 0),
        "outstanding": float(getattr(doc, "outstanding_amount", 0) or 0),
        "sales_invoice_state": state,
        "posting_date": str(getattr(doc, "posting_date", "")),
        "posting_time": str(getattr(doc, "posting_time", "")),
        "pos_profile": pos_profile,
        "kanban_profile": getattr(doc, "custom_kanban_profile", None),
        "custom_is_pickup": bool(getattr(doc, "custom_is_pickup", 0)),
        "delivery_date": _safe_str(getattr(doc, "custom_delivery_date", None)),
        "delivery_time_from": _safe_str(getattr(doc, "custom_delivery_time_from", None)),
        "requires_acceptance": acceptance_status != "Accepted",
        "acceptance_status": acceptance_status or "Pending",
        "timestamp": frappe.utils.now_datetime().isoformat(),
        "items": items,
    }

    return _enrich_invoice_display_fields(payload)


def _resolve_recipients_for_payload(payload: Dict[str, Any]) -> List[str]:
    profiles: List[str] = []
    for key in ("pos_profile", "kanban_profile"):
        value = payload.get(key)
        if value and value not in profiles:
            profiles.append(value)

    return _get_users_for_pos_profiles(profiles)


def _ensure_acceptance_defaults(doc: Any) -> None:
    """Ensure acceptance status is set to Pending if not already set."""
    try:
        current_status = getattr(doc, "custom_acceptance_status", None)
        # If status is already set (Pending or Accepted), don't change it
        if current_status in ("Pending", "Accepted"):
            return
        
        # Set default to Pending for new invoices
        frappe.db.set_value(
            "Sales Invoice",
            doc.name,
            {
                "custom_acceptance_status": "Pending",
                "custom_accepted_by": None,
                "custom_accepted_on": None,
            },
            update_modified=False,
        )
        setattr(doc, "custom_acceptance_status", "Pending")
        setattr(doc, "custom_accepted_by", None)
        setattr(doc, "custom_accepted_on", None)
        
        if frappe.conf.get("developer_mode"):
            frappe.msgprint(f"Set acceptance status to Pending for {doc.name}")
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to set acceptance defaults for {getattr(doc, 'name', '?')}")


def _publish_invoice_alert(payload: Dict[str, Any], recipients: Sequence[str]) -> None:
    try:
        target = recipients if recipients else "*"
        frappe.publish_realtime(WS_EVENTS.NEW_INVOICE, payload, user=target)
        
        if frappe.conf.get("developer_mode"):
            frappe.msgprint(
                f"Published jarz_pos_new_invoice event for {payload.get('invoice_id')} "
                f"to {len(recipients) if recipients else 'all'} users"
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "publish_realtime jarz_pos_new_invoice failed")


def _publish_invoice_accepted(payload: Dict[str, Any], recipients: Sequence[str]) -> None:
    try:
        target = recipients if recipients else "*"
        frappe.publish_realtime(WS_EVENTS.INVOICE_ACCEPTED, payload, user=target)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "publish_realtime jarz_pos_invoice_accepted failed")


def _push_new_invoice(payload: Dict[str, Any], recipients: Sequence[str]) -> None:
    tokens = _get_tokens_for_users(recipients)
    if not tokens:
        try:
            frappe.logger().info(
                f"FCM skip: no tokens for new_invoice; recipients={len(recipients)}"
            )
        except Exception:
            pass
        return

    data = _prepare_invoice_data_payload("new_invoice", payload)
    try:
        msg = f"FCM send: new_invoice; recipients={len(recipients)}; tokens={len(tokens)}; invoice={payload.get('invoice_id')}"
        frappe.logger().info(msg)
        frappe.log_error(msg, "FCM Send Debug")
    except Exception:
        pass
    _send_fcm_notifications(tokens, data)


def _push_invoice_accepted(payload: Dict[str, Any], recipients: Sequence[str]) -> None:
    tokens = _get_tokens_for_users(recipients)
    if not tokens:
        return

    data = {
        "type": "invoice_accepted",
        "invoice_id": payload.get("invoice_id", ""),
        "accepted_by": payload.get("accepted_by", ""),
        "accepted_on": payload.get("accepted_on", ""),
    }
    try:
        frappe.logger().info(
            f"FCM send: invoice_accepted; recipients={len(recipients)}; tokens={len(tokens)}"
        )
    except Exception:
        pass
    _send_fcm_notifications(tokens, data)


def _prepare_invoice_data_payload(event_type: str, payload: Dict[str, Any]) -> Dict[str, str]:
    display_payload = _enrich_invoice_display_fields(dict(payload))
    timestamp = display_payload.get("timestamp") or frappe.utils.now_datetime().isoformat()
    data: Dict[str, str] = {
        "type": event_type,
        "invoice_id": display_payload.get("invoice_id", ""),
        "notification_id": display_payload.get("invoice_id", ""),
        "customer_name": display_payload.get("customer_name", ""),
        "pos_profile": display_payload.get("pos_profile", "") or "",
        "grand_total": str(display_payload.get("grand_total", 0)),
        "sales_invoice_state": display_payload.get("sales_invoice_state", ""),
        "timestamp": timestamp,
        "requires_acceptance": "1" if display_payload.get("requires_acceptance") else "0",
        "item_summary": display_payload.get("item_summary", ""),
        "branch_display": display_payload.get("branch_display", "") or "",
        "total_display": display_payload.get("total_display", "0.00"),
        "item_count": str(display_payload.get("item_count", 0)),
        "title": display_payload.get("title", "") or "",
        "body": display_payload.get("body", "") or "",
    }

    delivery_date = display_payload.get("delivery_date")
    if delivery_date:
        data["delivery_date"] = delivery_date
    delivery_time = display_payload.get("delivery_time_from")
    if delivery_time:
        data["delivery_time"] = delivery_time

    items_json = json.dumps(display_payload.get("items", []), default=str)
    data["items"] = items_json
    return data


def _send_fcm_notifications(tokens: Sequence[str], data_payload: Dict[str, str]) -> None:
    """Send FCM push notifications using Firebase Admin SDK (V1 API)."""
    if not _initialize_firebase_app():
        frappe.log_error("Firebase not initialized. Skipping FCM push.", "FCM Push Skipped")
        return
    
    if not tokens:
        return
    
    # Build the message
    try:
        msg_type = data_payload.get("type", "")
        if msg_type == "new_invoice":
            title, body = _resolve_notification_content(data_payload)
            notification = messaging.Notification(title=title, body=body)
            android_notification = messaging.AndroidNotification(
                sound='default',
                channel_id='jarz_order_alerts',
                tag=data_payload.get("invoice_id", "")
            )
        else:
            title, body = _resolve_notification_content(data_payload)
            notification = messaging.Notification(title=title, body=body)

            # Use shift channel for shift events, order alerts channel for everything else
            if msg_type in ("shift_started", "shift_ended"):
                android_channel_id = "jarz_shift_updates"
            else:
                android_channel_id = "jarz_order_alerts"

            android_notification = messaging.AndroidNotification(
                sound='default',
                channel_id=android_channel_id,
                tag=data_payload.get("invoice_id", "")
            )

        android_config_kwargs = {"priority": 'high'}
        if android_notification is not None:
            android_config_kwargs["notification"] = android_notification

        # Use shift channel for shift events, order alerts channel for everything else
        # Send to each token (Firebase Admin SDK doesn't support batch sends in the same way)
        # For better performance, we can use MulticastMessage
        messages = []
        for token in tokens[:MAX_FCM_TOKENS_PER_BATCH]:
            message_kwargs = {
                "data": data_payload,
                "android": messaging.AndroidConfig(**android_config_kwargs),
                "token": token,
            }
            if notification is not None:
                message_kwargs["notification"] = notification
            message = messaging.Message(**message_kwargs)
            messages.append(message)
        
        # Send all messages
        if messages:
            # Send individually for now (can batch with MulticastMessage if needed)
            for message in messages:
                try:
                    response = messaging.send(message)
                    frappe.logger().info(f"FCM message sent successfully: {response}")
                except Exception as send_err:
                    err_text = str(send_err)
                    frappe.log_error(f"Failed to send FCM to token: {err_text}", "FCM Send Error")
                    # Auto-disable tokens that are no longer valid
                    if any(code in err_text for code in ("NotRegistered", "registration-token-not-registered")):
                        try:
                            _disable_token(message.token)
                        except Exception:
                            frappe.log_error(frappe.get_traceback(), "FCM Token Disable Failed")
                    
    except Exception:
        frappe.log_error(frappe.get_traceback(), "FCM push failed")


def _chunk(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def _get_tokens_for_users(users: Sequence[str]) -> List[str]:
    if not users:
        return []

    rows = frappe.get_all(
        "Jarz Mobile Device",
        filters={"user": ["in", list(users)], "enabled": 1},
        fields=["token"],
    )
    tokens = [row.get("token") for row in rows if row.get("token")]
    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: List[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def _get_users_for_pos_profiles(profiles: Sequence[str]) -> List[str]:
    filtered = [p for p in profiles if p]
    if not filtered:
        return []

    try:
        rows = frappe.get_all(
            "POS Profile User",
            filters={"parent": ["in", filtered], "parenttype": "POS Profile"},
            fields=["user"],
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Failed to load POS Profile users")
        return []

    users: List[str] = []
    seen: set[str] = set()
    for row in rows:
        user = row.get("user")
        if user and user not in seen:
            seen.add(user)
            users.append(user)
    return users


def _get_profiles_for_user(user: str) -> List[str]:
    if not user or user == "Guest":
        return []

    rows = frappe.get_all(
        "POS Profile User",
        filters={"user": user, "parenttype": "POS Profile"},
        fields=["parent"],
    )

    profiles: List[str] = []
    seen: set[str] = set()
    for row in rows:
        parent = row.get("parent")
        if parent and parent not in seen:
            seen.add(parent)
            profiles.append(parent)
    return profiles


def _ensure_user_can_accept(doc: Any, user: str) -> None:
    if user in {ROLES.ADMINISTRATOR, ROLES.SYSTEM_MANAGER}:
        return

    authorised_users = _get_users_for_pos_profiles(
        [getattr(doc, "pos_profile", None), getattr(doc, "custom_kanban_profile", None)]
    )
    if user not in authorised_users:
        frappe.throw("You are not allowed to accept orders for this POS profile", frappe.PermissionError)


def _normalise_pos_profile_payload(pos_profiles: Optional[str]) -> Optional[str]:
    if not pos_profiles:
        return None
    try:
        if isinstance(pos_profiles, str):
            parsed = json.loads(pos_profiles)
        else:
            parsed = pos_profiles
        if isinstance(parsed, (list, tuple)):
            return json.dumps([str(p) for p in parsed])
    except Exception:
        return None
    return None


def _safe_str(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value)

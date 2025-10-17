"""
Jarz POS - Notification and polling API endpoints
Alternative to websocket-based notifications for mobile clients
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

import frappe
from frappe import _

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
            limit=50
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
            limit=50
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
        
        frappe.publish_realtime("jarz_pos_new_invoice", new_invoice_payload, user="*")
        
        # Emit state change event (triggers kanban refresh)
        state_change_payload = {
            "event": "jarz_pos_invoice_state_change",
            "invoice_id": test_invoice_id,
            "old_state_key": None,  # Null old_state triggers refresh
            "new_state_key": "received",
            "old_state": None,
            "new_state": "Received",
            "timestamp": timestamp,
            "test_event": True
        }
        
        frappe.publish_realtime("jarz_pos_invoice_state_change", state_change_payload, user="*")
        frappe.publish_realtime("kanban_update", state_change_payload, user="*")
        
        # Also emit a generic test event
        frappe.publish_realtime("test_event", {"message": "Test websocket emission", "timestamp": timestamp}, user="*")
        
        return {
            "success": True,
            "message": "Test websocket events emitted successfully",
            "events_sent": [
                "jarz_pos_new_invoice",
                "jarz_pos_invoice_state_change", 
                "kanban_update",
                "test_event"
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
                "jarz_pos_new_invoice",
                "jarz_pos_invoice_state_change",
                "kanban_update", 
                "jarz_pos_out_for_delivery_transition",
                "jarz_pos_courier_outstanding"
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
        docname = frappe.db.get_value("Jarz Mobile Device", {"token": token}, "name")
        payload = {
            "token": token,
            "user": user,
            "platform": (platform or "Android").title(),
            "device_name": device_name,
            "app_version": app_version,
            "enabled": 1,
            "pos_profiles": _normalise_pos_profile_payload(pos_profiles),
        }

        if docname:
            doc = frappe.get_doc("Jarz Mobile Device", docname)
            for field, value in payload.items():
                setattr(doc, field, value)
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.get_doc({"doctype": "Jarz Mobile Device", **payload})
            doc.insert(ignore_permissions=True)

        return {"success": True, "device": doc.name}
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "register_mobile_device failed")
        frappe.throw(f"Unable to register device: {exc}")


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
            "pos_profile": ["in", profiles],
            "custom_acceptance_status": ["in", [None, "", "Pending"]],
            "creation": [">", cutoff],
        },
        fields=["name"],
        order_by="creation asc",
        limit=50,
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

    try:
        payload = _build_invoice_alert_payload(doc)
        if not payload:
            return

        recipients = _resolve_recipients_for_payload(payload)
        _publish_invoice_alert(payload, recipients)
        _push_new_invoice(payload, recipients)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "handle_invoice_submission failed")


def _build_invoice_alert_payload(doc: Any) -> Dict[str, Any]:
    if not doc or not getattr(doc, "name", None):
        return {}

    invoice_id = getattr(doc, "name", "")
    pos_profile = getattr(doc, "pos_profile", None)
    if not pos_profile:
        return {}

    _ensure_acceptance_defaults(doc)

    customer = getattr(doc, "customer_name", None) or getattr(doc, "customer", "")
    state = (
        getattr(doc, "custom_sales_invoice_state", None)
        or getattr(doc, "sales_invoice_state", None)
        or getattr(doc, "status", None)
        or "Received"
    )

    items: List[Dict[str, Any]] = []
    try:
        for row in getattr(doc, "items", [])[:15]:
            items.append(
                {
                    "item_code": getattr(row, "item_code", None),
                    "item_name": getattr(row, "item_name", None) or getattr(row, "item_code", None),
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

    payload["item_summary"] = ", ".join(
        f"{item.get('item_name') or item.get('item_code')} × {item.get('qty', 0)}" for item in items[:5]
    )

    return payload


def _resolve_recipients_for_payload(payload: Dict[str, Any]) -> List[str]:
    profiles: List[str] = []
    for key in ("pos_profile", "kanban_profile"):
        value = payload.get(key)
        if value and value not in profiles:
            profiles.append(value)

    return _get_users_for_pos_profiles(profiles)


def _ensure_acceptance_defaults(doc: Any) -> None:
    try:
        if getattr(doc, "custom_acceptance_status", None):
            return
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
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Failed to set acceptance defaults for {getattr(doc, 'name', '?')}")


def _publish_invoice_alert(payload: Dict[str, Any], recipients: Sequence[str]) -> None:
    try:
        target = recipients if recipients else "*"
        frappe.publish_realtime("jarz_pos_new_invoice", payload, user=target)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "publish_realtime jarz_pos_new_invoice failed")


def _publish_invoice_accepted(payload: Dict[str, Any], recipients: Sequence[str]) -> None:
    try:
        target = recipients if recipients else "*"
        frappe.publish_realtime("jarz_pos_invoice_accepted", payload, user=target)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "publish_realtime jarz_pos_invoice_accepted failed")


def _push_new_invoice(payload: Dict[str, Any], recipients: Sequence[str]) -> None:
    tokens = _get_tokens_for_users(recipients)
    if not tokens:
        return

    data = _prepare_invoice_data_payload("new_invoice", payload)
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
    _send_fcm_notifications(tokens, data)


def _prepare_invoice_data_payload(event_type: str, payload: Dict[str, Any]) -> Dict[str, str]:
    data: Dict[str, str] = {
        "type": event_type,
        "invoice_id": payload.get("invoice_id", ""),
        "customer_name": payload.get("customer_name", ""),
        "pos_profile": payload.get("pos_profile", ""),
        "grand_total": str(payload.get("grand_total", 0)),
        "sales_invoice_state": payload.get("sales_invoice_state", ""),
        "timestamp": payload.get("timestamp", frappe.utils.now_datetime().isoformat()),
        "requires_acceptance": "1" if payload.get("requires_acceptance") else "0",
        "item_summary": payload.get("item_summary", ""),
    }

    delivery_date = payload.get("delivery_date")
    if delivery_date:
        data["delivery_date"] = delivery_date
    delivery_time = payload.get("delivery_time_from")
    if delivery_time:
        data["delivery_time"] = delivery_time

    items_json = json.dumps(payload.get("items", []), default=str)
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
        # Extract notification content from data
        title = data_payload.get("customer_name", "New Order")
        body = f"Total: {data_payload.get('grand_total', 0)}"
        if data_payload.get("item_summary"):
            body += f" • {data_payload.get('item_summary')}"
        
        # Send to each token (Firebase Admin SDK doesn't support batch sends in the same way)
        # For better performance, we can use MulticastMessage
        messages = []
        for token in tokens[:MAX_FCM_TOKENS_PER_BATCH]:
            message = messaging.Message(
                data=data_payload,
                android=messaging.AndroidConfig(
                    priority='high',
                    notification=messaging.AndroidNotification(
                        sound='default',
                        channel_id='jarz_order_alerts'
                    )
                ),
                token=token
            )
            messages.append(message)
        
        # Send all messages
        if messages:
            # Send individually for now (can batch with MulticastMessage if needed)
            for message in messages:
                try:
                    response = messaging.send(message)
                    frappe.logger().info(f"FCM message sent successfully: {response}")
                except Exception as send_err:
                    frappe.log_error(f"Failed to send FCM to token: {str(send_err)}", "FCM Send Error")
                    
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
    if user in {"Administrator", "System Manager"}:
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

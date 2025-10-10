"""
Jarz POS - Notification and polling API endpoints
Alternative to websocket-based notifications for mobile clients
"""

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import frappe
from frappe import _


@frappe.whitelist(allow_guest=False)
def get_recent_invoices(minutes: int = 5) -> dict[str, Any]:
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
        frappe.log_error(f"Notifications API error: {e!s}", "Notifications API")
        return {
            "success": False,
            "error": str(e),
            "timestamp": frappe.utils.now()
        }


@frappe.whitelist(allow_guest=False)
def check_for_updates(last_check: str | None = None) -> dict[str, Any]:
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
            except Exception:
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
        frappe.log_error(f"Check updates error: {e!s}", "Notifications API")
        return {
            "success": False,
            "error": str(e),
            "timestamp": frappe.utils.now()
        }


@frappe.whitelist(allow_guest=True)  # Allow guest for debugging
def test_websocket_emission() -> dict[str, Any]:
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
        frappe.log_error(f"Test websocket emission error: {e!s}", "Notifications API")
        return {
            "success": False,
            "error": str(e),
            "timestamp": frappe.utils.now()
        }


@frappe.whitelist(allow_guest=False)
def get_websocket_debug_info() -> dict[str, Any]:
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

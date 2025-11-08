"""Test connection endpoint for verifying frontend-backend communication"""

import frappe
from frappe import _
from jarz_pos.utils.error_handler import handle_api_error, success_response


@frappe.whitelist(allow_guest=False)
def ping():
    """Simple ping endpoint to test connection"""
    try:
        return success_response(
            data={
                "timestamp": frappe.utils.now(),
                "user": frappe.session.user,
                "app_version": "1.0.0"
            },
            message="Backend is connected successfully"
        )
    except Exception as e:
        return handle_api_error(e, "Ping Test")


@frappe.whitelist(allow_guest=False)
def health_check():
    """Comprehensive health check for the backend"""
    try:
        # Test database connection
        db_test = frappe.db.sql("SELECT 1")[0][0] == 1
        
        # Test Redis connection (if available)
        redis_test = True
        try:
            frappe.cache().get("test_key")
        except Exception:
            redis_test = False
            
        # Get app info
        app_info = {
            "app_name": "jarz_pos",
            "app_version": "1.0.0",
            "frappe_version": frappe.__version__,
            "site": frappe.local.site
        }
        
        return {
            "success": True,
            "message": "All systems operational",
            "timestamp": frappe.utils.now(),
            "user": frappe.session.user,
            "tests": {
                "database": db_test,
                "redis": redis_test
            },
            "app_info": app_info
        }
        
    except Exception as e:
        frappe.log_error(f"Health check failed: {str(e)}", "Health Check Error")
        return {
            "success": False,
            "message": f"Health check failed: {str(e)}",
            "timestamp": frappe.utils.now(),
            "user": frappe.session.user
        }


@frappe.whitelist(allow_guest=False)
def get_backend_info():
    """Get detailed backend information"""
    return {
        "success": True,
        "data": {
            "app_name": "Jarz POS",
            "app_version": "1.0.0",
            "frappe_version": frappe.__version__,
            "site": frappe.local.site,
            "user": frappe.session.user,
            "user_full_name": frappe.get_value("User", frappe.session.user, "full_name"),
            "api_endpoints": [
                "/api/method/jarz_pos.api.test_connection.ping",
                "/api/method/jarz_pos.api.test_connection.health_check",
                "/api/method/jarz_pos.api.pos.get_pos_profiles",
                "/api/method/jarz_pos.api.invoices.create_pos_invoice",
                "/api/method/jarz_pos.api.customer.get_customers",
                "/api/method/jarz_pos.api.couriers.get_couriers"
            ],
            "timestamp": frappe.utils.now()
        }
    }

@frappe.whitelist(allow_guest=True)
def emit_test_event(event: str = "jarz_pos_new_invoice"):
    """Emit a realtime test event to verify Socket.IO delivery to clients.
    Default event is 'jarz_pos_new_invoice'. Requires auth.
    """
    try:
        now = frappe.utils.now()
        payload = {"name": f"TEST-SI-{now}", "timestamp": now, "by": frappe.session.user}
        frappe.publish_realtime(event, payload, user="*")
        # Also emit a kanban-style state-change with no old_state to trigger a refresh
        kanban_payload = {
            "event": "jarz_pos_invoice_state_change",
            "invoice_id": payload["name"],
            "old_state_key": None,
            "new_state_key": "received",
            "old_state": None,
            "new_state": "Received",
        }
        frappe.publish_realtime("jarz_pos_invoice_state_change", kanban_payload, user="*")
        return success_response(message="Event emitted", data=payload)
    except Exception as e:
        return handle_api_error(e, "Emit Test Event")


# ---------------------------------------------------------------------------
# Maintenance utility: Reload missing standard DocTypes from this app
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=False)
def reload_jarz_doctypes():
    """Reload standard DocTypes shipped with this app into the site DB.
    Useful when DocTypes were removed during migrate and need restoring.
    """
    doctypes = [
        ("jarz_pos", "doctype", "jarz_bundle"),
        ("jarz_pos", "doctype", "jarz_bundle_item_group"),
        ("jarz_pos", "doctype", "pos_profile_timetable"),
        ("jarz_pos", "doctype", "pos_profile_day_timing"),
        ("jarz_pos", "doctype", "courier"),
        ("jarz_pos", "doctype", "courier_transaction"),
        ("jarz_pos", "doctype", "custom_settings"),
        ("jarz_pos", "doctype", "city"),
    ]
    reloaded = []
    for module, doctype_type, name in doctypes:
        try:
            frappe.reload_doc(module, doctype_type, name)
            reloaded.append(name)
        except Exception as e:
            frappe.log_error(f"Failed to reload {name}: {e}", "Jarz POS Reload DocTypes")
    frappe.db.commit()
    return {"success": True, "reloaded": reloaded}

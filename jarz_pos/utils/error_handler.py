"""Error handling utilities for consistent API responses"""

import frappe
from frappe import _
import traceback


def handle_api_error(e, context="API Error"):
    """Standardized error handling for API endpoints"""
    error_msg = str(e)
    
    # Log the full error with traceback
    frappe.log_error(
        message=f"Context: {context}\nError: {error_msg}\nTraceback: {traceback.format_exc()}",
        title=context
    )
    
    # Return standardized error response
    if frappe.local.response:
        frappe.local.response.http_status_code = 500
    
    return {
        "success": False,
        "error": True,
        "message": error_msg,
        "context": context,
        "timestamp": frappe.utils.now()
    }


def validate_required_fields(data, required_fields):
    """Validate that all required fields are present in the data"""
    missing_fields = []
    
    for field in required_fields:
        if field not in data or data[field] is None or data[field] == "":
            missing_fields.append(field)
    
    if missing_fields:
        error_msg = f"Missing required fields: {', '.join(missing_fields)}"
        frappe.throw(_(error_msg))
    
    return True


def success_response(data=None, message="Success"):
    """Standardized success response"""
    response = {
        "success": True,
        "error": False,
        "message": message,
        "timestamp": frappe.utils.now()
    }
    
    if data is not None:
        response["data"] = data
    
    return response


def validation_error_response(message):
    """Standardized validation error response"""
    if frappe.local.response:
        frappe.local.response.http_status_code = 400
    
    return {
        "success": False,
        "error": True,
        "error_type": "validation_error",
        "message": message,
        "timestamp": frappe.utils.now()
    }


def not_found_response(resource_type, resource_id):
    """Standardized not found error response"""
    if frappe.local.response:
        frappe.local.response.http_status_code = 404
    
    return {
        "success": False,
        "error": True,
        "error_type": "not_found",
        "message": f"{resource_type} with ID '{resource_id}' not found",
        "timestamp": frappe.utils.now()
    }


def permission_error_response(message="Insufficient permissions"):
    """Standardized permission error response"""
    if frappe.local.response:
        frappe.local.response.http_status_code = 403
    
    return {
        "success": False,
        "error": True,
        "error_type": "permission_error",
        "message": message,
        "timestamp": frappe.utils.now()
    }

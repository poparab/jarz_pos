"""API endpoints for POS Payment Receipt management."""

from __future__ import annotations
import frappe
import base64
import os
from typing import List, Dict, Any
from frappe import _
from frappe.exceptions import PermissionError as FrappePermissionError

from jarz_pos.constants import ROLES


RECEIPT_STATUS_UNCONFIRMED = "Unconfirmed"
RECEIPT_STATUS_CONFIRMED = "Confirmed"
RECEIPT_STATUS_CHANGED = "Changed"


def _normalize_receipt_method(method: str | None) -> str:
    normalized = str(method or "").strip().lower().replace(" ", "").replace("_", "")
    if normalized in {"instapay", "insta", "bank", "bankaccount"}:
        return "instapay"
    if normalized in {"wallet", "mobilewallet"}:
        return "wallet"
    if normalized in {"paymentgateway", "gateway", "card"}:
        return "payment_gateway"
    if normalized in {"cash", "cod", "cashondelivery"}:
        return "cash"
    return normalized


def mark_payment_receipts_changed_for_invoice(
    sales_invoice: str,
    *,
    payment_methods: list[str] | tuple[str, ...] | set[str] | None = None,
    receipt_name: str | None = None,
) -> list[str]:
    invoice_name = str(sales_invoice or "").strip()
    if not invoice_name:
        return []

    filters: dict[str, object] = {
        "sales_invoice": invoice_name,
        "status": ["!=", RECEIPT_STATUS_CHANGED],
    }
    if receipt_name:
        filters["name"] = str(receipt_name).strip()

    rows = frappe.get_all(
        "POS Payment Receipt",
        filters=filters,
        fields=["name", "payment_method"],
        order_by="creation desc",
    )
    if payment_methods:
        allowed_methods = {
            _normalize_receipt_method(method)
            for method in payment_methods
            if str(method or "").strip()
        }
        rows = [
            row for row in rows
            if _normalize_receipt_method(row.get("payment_method")) in allowed_methods
        ]

    changed_receipts: list[str] = []
    for row in rows:
        receipt = frappe.get_doc("POS Payment Receipt", row.get("name"))
        receipt.status = RECEIPT_STATUS_CHANGED
        receipt.save(ignore_permissions=True)
        changed_receipts.append(receipt.name)

    return changed_receipts


def ensure_uploaded_payment_receipt(
    receipt_name: str,
    *,
    sales_invoice: str,
    payment_method: str,
    amount: float,
) -> dict[str, Any]:
    normalized_name = str(receipt_name or "").strip()
    if not normalized_name:
        frappe.throw("Payment receipt is required")
    if not frappe.db.exists("POS Payment Receipt", normalized_name):
        frappe.throw("Payment receipt was not found")

    receipt = frappe.get_doc("POS Payment Receipt", normalized_name)
    if str(getattr(receipt, "sales_invoice", "") or "").strip() != str(sales_invoice or "").strip():
        frappe.throw("Payment receipt does not belong to this invoice")
    if str(getattr(receipt, "status", "") or "").strip() == RECEIPT_STATUS_CHANGED:
        frappe.throw("Changed payment receipts cannot be used")
    if _normalize_receipt_method(getattr(receipt, "payment_method", None)) != _normalize_receipt_method(payment_method):
        frappe.throw("Payment receipt method does not match the selected collection method")
    receipt_amount = float(getattr(receipt, "amount", 0) or 0)
    if abs(receipt_amount - float(amount or 0)) > 0.01:
        frappe.throw("Payment receipt amount does not match the order amount")

    image_url = str(
        getattr(receipt, "receipt_image_url", None)
        or getattr(receipt, "receipt_image", None)
        or ""
    ).strip()
    if not image_url:
        frappe.throw("Payment receipt must have an uploaded image")

    return {
        "name": receipt.name,
        "sales_invoice": str(getattr(receipt, "sales_invoice", "") or "").strip(),
        "payment_method": str(getattr(receipt, "payment_method", "") or "").strip(),
        "amount": receipt_amount,
        "status": str(getattr(receipt, "status", "") or "").strip(),
        "receipt_image_url": image_url,
    }


def _current_user_roles() -> set[str]:
    return {
        str(role or "").strip()
        for role in (frappe.get_roles(frappe.session.user) or [])
        if str(role or "").strip()
    }


def _has_payment_receipt_confirm_access(pos_profile: str | None = None) -> bool:
    roles = _current_user_roles()

    if ROLES.ADMIN.intersection(roles) or ROLES.JARZ_MANAGER in roles:
        return True

    if "JARZ line manager" not in roles and ROLES.JARZ_LINE_MANAGER not in roles:
        return False

    if not pos_profile:
        return True

    from jarz_pos.api.manager import _current_user_allowed_profiles

    allowed_profiles = {
        str(profile or "").strip()
        for profile in (_current_user_allowed_profiles() or [])
        if str(profile or "").strip()
    }
    if not allowed_profiles:
        return False

    return str(pos_profile or "").strip() in allowed_profiles


def _ensure_payment_receipt_confirm_access(pos_profile: str | None = None) -> None:
    if _has_payment_receipt_confirm_access(pos_profile):
        return

    frappe.throw(
        _("Only branch managers and above can confirm payment receipts."),
        FrappePermissionError,
    )


@frappe.whitelist()
def list_payment_receipts(pos_profile: str = None, status: str = None):
    """List payment receipts filtered by POS profile and status.
    
    Args:
        pos_profile: Filter by POS profile (optional)
        status: Filter by status: Unconfirmed/Confirmed/Changed (optional)
    
    Returns:
        list: List of payment receipt records
    """
    try:
        filters = {}
        
        if pos_profile:
            filters['pos_profile'] = pos_profile
        
        if status:
            filters['status'] = status
        else:
            filters['status'] = ['!=', RECEIPT_STATUS_CHANGED]
        
        # Get accessible POS profiles for the current user
        from jarz_pos.api.manager import _current_user_allowed_profiles
        accessible_profiles = _current_user_allowed_profiles()
        
        # If pos_profile not specified, filter by accessible profiles
        if not pos_profile and accessible_profiles:
            filters['pos_profile'] = ['in', accessible_profiles]
        
        receipts = frappe.get_all(
            'POS Payment Receipt',
            filters=filters,
            fields=[
                'name',
                'sales_invoice',
                'payment_method',
                'amount',
                'pos_profile',
                'status',
                'receipt_image',
                'receipt_image_url',
                'uploaded_by',
                'upload_date',
                'confirmed_by',
                'confirmed_date',
                'creation',
                'modified'
            ],
            order_by='creation desc'
        )
        
        # Get invoice details for each receipt
        for receipt in receipts:
            try:
                invoice = frappe.get_doc('Sales Invoice', receipt['sales_invoice'])
                receipt['customer_name'] = invoice.customer_name
                receipt['invoice_id'] = invoice.name
            except Exception:
                receipt['customer_name'] = 'Unknown'
                receipt['invoice_id'] = receipt['sales_invoice']

            receipt['can_confirm'] = _has_payment_receipt_confirm_access(
                receipt.get('pos_profile')
            )
        
        frappe.logger().info(f"Retrieved {len(receipts)} payment receipts")
        
        return receipts
    
    except Exception as e:
        frappe.logger().error(f"Failed to list payment receipts: {str(e)}")
        frappe.throw(f"Failed to list payment receipts: {str(e)}")


@frappe.whitelist()
def create_payment_receipt(sales_invoice: str, payment_method: str, amount: float, pos_profile: str):
    """Create a new payment receipt record.
    
    Args:
        sales_invoice: Sales Invoice name
        payment_method: Instapay or Mobile Wallet
        amount: Payment amount
        pos_profile: POS Profile name
    
    Returns:
        dict: Created receipt details
    """
    try:
        frappe.logger().info(f"Creating payment receipt for invoice {sales_invoice}")
        
        # Reuse only active receipts; changed receipts are audit history and should not block recreation.
        existing = frappe.get_all(
            'POS Payment Receipt',
            filters={
                'sales_invoice': sales_invoice,
                'status': ['!=', RECEIPT_STATUS_CHANGED],
            },
            fields=['name', 'payment_method'],
            order_by='creation desc',
            limit_page_length=20,
        )
        existing_name = next(
            (
                row.get('name')
                for row in existing
                if _normalize_receipt_method(row.get('payment_method')) == _normalize_receipt_method(payment_method)
            ),
            None,
        )

        if existing_name:
            frappe.logger().info(f"Receipt already exists: {existing_name}")
            return {
                'success': True,
                'receipt_name': existing_name,
                'message': 'Receipt already exists'
            }
        
        # Create new receipt
        receipt = frappe.get_doc({
            'doctype': 'POS Payment Receipt',
            'sales_invoice': sales_invoice,
            'payment_method': payment_method,
            'amount': amount,
            'pos_profile': pos_profile,
            'status': RECEIPT_STATUS_UNCONFIRMED,
            'uploaded_by': frappe.session.user
        })
        
        receipt.insert()
        frappe.db.commit()
        
        frappe.logger().info(f"Created payment receipt: {receipt.name}")
        
        return {
            'success': True,
            'receipt_name': receipt.name,
            'message': 'Receipt created successfully'
        }
    
    except Exception as e:
        frappe.logger().error(f"Failed to create payment receipt: {str(e)}")
        frappe.throw(f"Failed to create payment receipt: {str(e)}")


@frappe.whitelist()
def upload_receipt_image(receipt_name: str, image_data: str, filename: str):
    """Upload receipt image for a payment receipt.
    
    Args:
        receipt_name: POS Payment Receipt name
        image_data: Base64 encoded image data
        filename: Original filename
    
    Returns:
        dict: Upload result with file URL
    """
    try:
        frappe.logger().info(f"Uploading receipt image for {receipt_name}")
        
        # Get the receipt document
        receipt = frappe.get_doc('POS Payment Receipt', receipt_name)
        
        # Decode base64 image
        if ',' in image_data:
            # Remove data:image/...;base64, prefix if present
            image_data = image_data.split(',')[1]
        
        image_bytes = base64.b64decode(image_data)
        
        # Create file document
        file_doc = frappe.get_doc({
            'doctype': 'File',
            'file_name': filename,
            'is_private': 0,
            'content': image_bytes,
            'attached_to_doctype': 'POS Payment Receipt',
            'attached_to_name': receipt_name,
            'attached_to_field': 'receipt_image'
        })
        file_doc.save()
        
        # Update receipt with image
        receipt.receipt_image = file_doc.file_url
        receipt.receipt_image_url = file_doc.file_url
        receipt.upload_date = frappe.utils.now()
        receipt.uploaded_by = frappe.session.user
        receipt.save()
        
        frappe.db.commit()
        
        frappe.logger().info(f"Receipt image uploaded: {file_doc.file_url}")
        
        return {
            'success': True,
            'file_url': file_doc.file_url,
            'message': 'Image uploaded successfully'
        }
    
    except Exception as e:
        frappe.logger().error(f"Failed to upload receipt image: {str(e)}")
        frappe.throw(f"Failed to upload receipt image: {str(e)}")


@frappe.whitelist()
def confirm_receipt(receipt_name: str):
    """Confirm a payment receipt.
    
    Args:
        receipt_name: POS Payment Receipt name
    
    Returns:
        dict: Confirmation result
    """
    try:
        frappe.logger().info(f"Confirming receipt {receipt_name}")
        
        receipt = frappe.get_doc('POS Payment Receipt', receipt_name)
        _ensure_payment_receipt_confirm_access(getattr(receipt, 'pos_profile', None))

        if receipt.status == RECEIPT_STATUS_CHANGED:
            frappe.throw('Changed payment receipts cannot be confirmed')
        
        if receipt.status == RECEIPT_STATUS_CONFIRMED:
            return {
                'success': True,
                'message': 'Receipt already confirmed'
            }
        
        receipt.status = RECEIPT_STATUS_CONFIRMED
        receipt.confirmed_by = frappe.session.user
        receipt.confirmed_date = frappe.utils.now()
        receipt.save()
        
        frappe.db.commit()
        
        frappe.logger().info(f"Receipt confirmed: {receipt_name}")
        
        return {
            'success': True,
            'message': 'Receipt confirmed successfully'
        }
    
    except FrappePermissionError:
        raise
    except Exception as e:
        frappe.logger().error(f"Failed to confirm receipt: {str(e)}")
        frappe.throw(f"Failed to confirm receipt: {str(e)}")


@frappe.whitelist()
def get_accessible_pos_profiles():
    """Get list of POS profiles accessible to current user.
    
    Returns:
        list: List of POS profile names
    """
    try:
        from jarz_pos.api.manager import _current_user_allowed_profiles
        
        profile_names = _current_user_allowed_profiles()
        
        return profile_names
    
    except Exception as e:
        frappe.logger().error(f"Failed to get accessible profiles: {str(e)}")
        frappe.throw(f"Failed to get accessible profiles: {str(e)}")

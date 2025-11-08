"""API endpoints for POS Payment Receipt management."""

from __future__ import annotations
import frappe
import base64
import os
from typing import List, Dict, Any


@frappe.whitelist()
def list_payment_receipts(pos_profile: str = None, status: str = None):
    """List payment receipts filtered by POS profile and status.
    
    Args:
        pos_profile: Filter by POS profile (optional)
        status: Filter by status: Unconfirmed/Confirmed (optional)
    
    Returns:
        list: List of payment receipt records
    """
    try:
        filters = {}
        
        if pos_profile:
            filters['pos_profile'] = pos_profile
        
        if status:
            filters['status'] = status
        
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
        
        # Check if receipt already exists for this invoice and payment method
        existing = frappe.db.exists('POS Payment Receipt', {
            'sales_invoice': sales_invoice,
            'payment_method': payment_method
        })
        
        if existing:
            frappe.logger().info(f"Receipt already exists: {existing}")
            return {
                'receipt_name': existing,
                'message': 'Receipt already exists'
            }
        
        # Create new receipt
        receipt = frappe.get_doc({
            'doctype': 'POS Payment Receipt',
            'sales_invoice': sales_invoice,
            'payment_method': payment_method,
            'amount': amount,
            'pos_profile': pos_profile,
            'status': 'Unconfirmed',
            'uploaded_by': frappe.session.user
        })
        
        receipt.insert()
        frappe.db.commit()
        
        frappe.logger().info(f"Created payment receipt: {receipt.name}")
        
        return {
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
        
        if receipt.status == 'Confirmed':
            return {'message': 'Receipt already confirmed'}
        
        receipt.status = 'Confirmed'
        receipt.confirmed_by = frappe.session.user
        receipt.confirmed_date = frappe.utils.now()
        receipt.save()
        
        frappe.db.commit()
        
        frappe.logger().info(f"Receipt confirmed: {receipt_name}")
        
        return {'message': 'Receipt confirmed successfully'}
    
    except Exception as e:
        frappe.logger().error(f"Failed to confirm receipt: {str(e)}")
        frappe.throw(f"Failed to confirm receipt: {str(e)}")


@frappe.whitelist()
def get_accessible_pos_profiles():
    """Get list of POS profiles accessible to current user.
    
    Returns:
        dict: List of POS profiles with names and titles
    """
    try:
        from jarz_pos.api.manager import _current_user_allowed_profiles
        
        profile_names = _current_user_allowed_profiles()
        
        profiles = []
        for name in profile_names:
            profile = frappe.get_doc('POS Profile', name)
            profiles.append({
                'name': name,
                'title': profile.name  # You can customize this to show a better title
            })
        
        return profiles
    
    except Exception as e:
        frappe.logger().error(f"Failed to get accessible profiles: {str(e)}")
        frappe.throw(f"Failed to get accessible profiles: {str(e)}")

"""
Test API for Bundle and Delivery Implementation

This file contains test endpoints to verify the bundle processing
and delivery charges implementation works correctly.
"""

import json

import frappe


@frappe.whitelist()
def debug_bundle_data():
    """
    Debug current bundle data to understand the structure
    """
    try:
        # Get all bundles with their fields
        bundles = frappe.get_all("Jarz Bundle",
            fields=["name", "bundle_name", "bundle_price", "erpnext_item"],
            limit=10)

        # Get all bundle items
        bundle_items = frappe.get_all("Jarz Bundle Item Group",
            fields=["parent", "item_group", "quantity"],
            limit=20)

        # Check specific item from the log
        test_item = "lpiau127so"
        item_exists = frappe.db.exists("Item", test_item)
        bundle_with_item = frappe.get_all("Jarz Bundle",
            filters={"erpnext_item": test_item},
            fields=["name", "bundle_name", "bundle_price", "erpnext_item"])

        return {
            'success': True,
            'debug_data': {
                'total_bundles': len(bundles),
                'bundles': bundles,
                'total_bundle_items': len(bundle_items),
                'bundle_items': bundle_items,
                'test_item_check': {
                    'item_code': test_item,
                    'item_exists': item_exists,
                    'bundle_with_this_item': bundle_with_item
                }
            }
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }


@frappe.whitelist()
def test_bundle_processing():
    """
    Test the bundle processing functionality
    Returns bundle expansion and pricing calculations
    """
    try:
        from jarz_pos.services.bundle_processing import (
            process_bundle_for_invoice,
            validate_bundle_configuration_by_item,
        )

        # Get all bundles for testing
        bundles = frappe.get_all("Jarz Bundle",
            fields=["name", "bundle_name", "bundle_price", "erpnext_item"],
            limit=5)

        test_results = []

        for bundle in bundles:
            bundle_code = bundle['name']
            erpnext_item = bundle['erpnext_item']

            # Test validation using ERPNext item (the correct way)
            is_valid, validation_message, found_bundle_code = validate_bundle_configuration_by_item(erpnext_item)

            result = {
                'bundle_code': bundle_code,
                'bundle_name': bundle['bundle_name'],
                'bundle_price': bundle['bundle_price'],
                'erpnext_item': erpnext_item,
                'validation': {
                    'is_valid': is_valid,
                    'message': validation_message,
                    'found_bundle_code': found_bundle_code
                }
            }

            if is_valid and erpnext_item:
                try:
                    # Test bundle processing using ERPNext item (the correct way)
                    processed_items = process_bundle_for_invoice(erpnext_item, 1)
                    result['processed_items'] = processed_items
                    result['item_count'] = len(processed_items)

                    # Calculate totals
                    parent_total = sum(item.get('rate', 0) * item.get('qty', 0)
                                     for item in processed_items
                                     if item.get('is_bundle_parent'))

                    child_total_before_discount = sum(item.get('rate', 0) * item.get('qty', 0)
                                                    for item in processed_items
                                                    if item.get('is_bundle_child'))

                    child_total_after_discount = sum(
                        (item.get('rate', 0) * item.get('qty', 0)) *
                        (1 - item.get('discount_percentage', 0) / 100)
                        for item in processed_items
                        if item.get('is_bundle_child')
                    )

                    result['totals'] = {
                        'parent_total': parent_total,
                        'child_total_before_discount': child_total_before_discount,
                        'child_total_after_discount': child_total_after_discount,
                        'expected_total': bundle['bundle_price']
                    }

                except Exception as e:
                    result['processing_error'] = str(e)

            test_results.append(result)

        return {
            'success': True,
            'bundles_tested': len(test_results),
            'results': test_results
        }

    except Exception as e:
        frappe.log_error(f"Bundle test error: {e!s}", "Bundle Test")
        return {
            'success': False,
            'error': str(e)
        }


@frappe.whitelist()
def test_delivery_charges():
    """
    Test the delivery charges functionality
    Returns delivery account validation and tax calculation
    """
    try:
        from jarz_pos.utils.delivery_utils import get_delivery_account, validate_delivery_charges

        # Get default company
        companies = frappe.get_all("Company", fields=["name", "abbr"], limit=3)

        test_results = []

        for company in companies:
            company_name = company['name']
            company_abbr = company['abbr']

            result = {
                'company': company_name,
                'abbreviation': company_abbr
            }

            try:
                # Test delivery account lookup
                delivery_account = get_delivery_account(company_name)
                result['delivery_account'] = delivery_account
                result['account_found'] = True

                # Test account verification
                account_exists = frappe.db.exists("Account", delivery_account)
                result['account_exists'] = account_exists

                if account_exists:
                    account_doc = frappe.get_doc("Account", delivery_account)
                    result['account_details'] = {
                        'account_type': account_doc.account_type,
                        'is_group': account_doc.is_group,
                        'parent_account': account_doc.parent_account
                    }

            except Exception as e:
                result['account_error'] = str(e)
                result['account_found'] = False

            # Test delivery charge validation
            test_charges = [0, 10, 50, 100, -5, 15000]
            charge_validations = []

            for charge in test_charges:
                is_valid, message = validate_delivery_charges(charge)
                charge_validations.append({
                    'amount': charge,
                    'is_valid': is_valid,
                    'message': message
                })

            result['charge_validations'] = charge_validations
            test_results.append(result)

        return {
            'success': True,
            'companies_tested': len(test_results),
            'results': test_results
        }

    except Exception as e:
        frappe.log_error(f"Delivery test error: {e!s}", "Delivery Test")
        return {
            'success': False,
            'error': str(e)
        }


@frappe.whitelist()
def test_invoice_creation_with_bundle():
    """
    Test complete invoice creation with bundle items
    """
    try:
        # Get test data
        bundles = frappe.get_all("Jarz Bundle",
            fields=["name", "bundle_name", "bundle_price", "erpnext_item"],
            limit=1)

        if not bundles:
            return {
                'success': False,
                'error': 'No bundles found for testing'
            }

        bundle = bundles[0]
        if not bundle['erpnext_item']:
            return {
                'success': False,
                'error': f'Bundle {bundle["bundle_name"]} has no ERPNext item configured'
            }

        customers = frappe.get_all("Customer",
            fields=["name", "customer_name"],
            limit=1)

        if not customers:
            return {
                'success': False,
                'error': 'No customers found for testing'
            }

        pos_profiles = frappe.get_all("POS Profile",
            fields=["name"],
            limit=1)

        if not pos_profiles:
            return {
                'success': False,
                'error': 'No POS profiles found for testing'
            }

        # Prepare test cart with bundle using ERPNext item (correct way)
        test_cart = [{
            'item_code': bundle['erpnext_item'],  # Use ERPNext item, not bundle ID
            'qty': 1,
            'rate': bundle['bundle_price'],
            'is_bundle': True
        }]

        test_delivery_charges = 25.0

        # Call the invoice creation API
        from jarz_pos.services.invoice_creation import create_pos_invoice

        result = create_pos_invoice(
            cart_json=json.dumps(test_cart),
            customer_name=customers[0]['name'],
            pos_profile_name=pos_profiles[0]['name'],
            delivery_charges_json=json.dumps([{
                'charge_type': 'Delivery',
                'amount': test_delivery_charges
            }])
        )

        # If successful, get the invoice details
        if result.get('success') and result.get('invoice_name'):
            invoice_doc = frappe.get_doc("Sales Invoice", result['invoice_name'])

            # Analyze the invoice
            analysis = {
                'invoice_name': invoice_doc.name,
                'total_items': len(invoice_doc.items),
                'bundle_parents': len([item for item in invoice_doc.items if item.get('is_bundle_parent')]),
                'bundle_children': len([item for item in invoice_doc.items if item.get('is_bundle_child')]),
                'delivery_taxes': len([tax for tax in (invoice_doc.taxes or [])
                                     if 'delivery' in (tax.description or '').lower()]),
                'net_total': invoice_doc.net_total,
                'grand_total': invoice_doc.grand_total,
                'items_details': []
            }

            for item in invoice_doc.items:
                analysis['items_details'].append({
                    'item_code': item.item_code,
                    'qty': item.qty,
                    'rate': item.rate,
                    'amount': item.amount,
                    'discount_percentage': getattr(item, 'discount_percentage', 0),
                    'is_bundle_parent': item.get('is_bundle_parent'),
                    'is_bundle_child': item.get('is_bundle_child'),
                    'bundle_code': item.get('bundle_code'),
                    'parent_bundle': item.get('parent_bundle')
                })

            result['invoice_analysis'] = analysis

        return result

    except Exception as e:
        frappe.log_error(f"Invoice creation test error: {e!s}", "Invoice Test")
        return {
            'success': False,
            'error': str(e)
        }

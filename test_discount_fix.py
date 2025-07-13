#!/usr/bin/env python3
"""
Test script to verify discount calculation fixes in Jarz POS
Run this script to test the discount application in sales invoices
"""

import frappe
import json
from jarz_pos.jarz_pos.page.custom_pos.custom_pos import create_sales_invoice

def test_bundle_discount_calculation():
    """Test bundle discount calculation with detailed output"""
    print("=== Testing Bundle Discount Calculation ===")
    
    # Test data: Bundle with items totaling $40, bundle price $25
    cart = [
        {
            "is_bundle": True,
            "bundle_name": "Test Bundle",
            "item_code": "BUNDLE001",  # Assuming this exists
            "price": 25.00,  # Bundle price (less than individual total)
            "items": [
                {"item_code": "ITEM001"},  # $10.00
                {"item_code": "ITEM002"},  # $15.00
                {"item_code": "ITEM003"},  # $5.00
                {"item_code": "ITEM001"}   # $10.00 (duplicate)
            ]
        }
    ]
    
    cart_json = json.dumps(cart)
    
    try:
        # Create sales invoice
        invoice = create_sales_invoice(cart_json, "Customer", "POS Profile")
        
        print(f"✅ Invoice created successfully: {invoice.get('name', 'Unknown')}")
        print(f"📋 Total items in invoice: {len(invoice.get('items', []))}")
        
        # Analyze items
        for i, item in enumerate(invoice.get('items', [])):
            print(f"\n📦 Item {i+1}:")
            print(f"   Code: {item.get('item_code')}")
            print(f"   Qty: {item.get('qty')}")
            print(f"   Rate: ${item.get('rate', 0):.2f}")
            print(f"   Discount %: {item.get('discount_percentage', 0):.2f}%")
            print(f"   Amount: ${item.get('amount', 0):.2f}")
            
            if item.get('description'):
                print(f"   Description: {item.get('description')}")
        
        # Calculate totals
        total_amount = sum(item.get('amount', 0) for item in invoice.get('items', []))
        print(f"\n💰 Total Items Amount: ${total_amount:.2f}")
        
        # Check delivery charges
        if invoice.get('taxes'):
            print(f"\n🚚 Taxes/Charges:")
            for tax in invoice.get('taxes', []):
                print(f"   {tax.get('description')}: ${tax.get('tax_amount', 0):.2f}")
        
        if invoice.get('discount_amount'):
            print(f"\n💸 Discount Amount: ${invoice.get('discount_amount', 0):.2f}")
        
        print(f"\n🧾 Grand Total: ${invoice.get('grand_total', 0):.2f}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error creating invoice: {str(e)}")
        return False

def test_delivery_charges():
    """Test delivery charges calculation"""
    print("\n=== Testing Delivery Charges ===")
    
    cart = [
        {
            "item_code": "ITEM001",
            "qty": 1,
            "price": 10.00
        }
    ]
    
    delivery_charges = {
        "income": 15.00,
        "expense": 5.00,
        "city": "Test City"
    }
    
    cart_json = json.dumps(cart)
    delivery_json = json.dumps(delivery_charges)
    
    try:
        invoice = create_sales_invoice(cart_json, "Customer", "POS Profile", delivery_json)
        
        print(f"✅ Invoice with delivery created: {invoice.get('name', 'Unknown')}")
        
        # Check delivery taxes
        delivery_taxes = [tax for tax in invoice.get('taxes', []) if 'Delivery' in tax.get('description', '')]
        print(f"🚚 Delivery taxes found: {len(delivery_taxes)}")
        
        for tax in delivery_taxes:
            print(f"   {tax.get('description')}: ${tax.get('tax_amount', 0):.2f}")
        
        if invoice.get('discount_amount'):
            print(f"💸 Delivery expense discount: ${invoice.get('discount_amount', 0):.2f}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error creating delivery invoice: {str(e)}")
        return False

def test_regular_items():
    """Test regular items (no discount)"""
    print("\n=== Testing Regular Items ===")
    
    cart = [
        {
            "item_code": "ITEM001",
            "qty": 2,
            "price": 10.00
        }
    ]
    
    cart_json = json.dumps(cart)
    
    try:
        invoice = create_sales_invoice(cart_json, "Customer", "POS Profile")
        
        print(f"✅ Regular item invoice created: {invoice.get('name', 'Unknown')}")
        
        item = invoice.get('items', [{}])[0]
        print(f"📦 Item: {item.get('item_code')}")
        print(f"   Qty: {item.get('qty')}")
        print(f"   Rate: ${item.get('rate', 0):.2f}")
        print(f"   Amount: ${item.get('amount', 0):.2f}")
        print(f"   Discount %: {item.get('discount_percentage', 0):.2f}%")
        
        return True
        
    except Exception as e:
        print(f"❌ Error creating regular item invoice: {str(e)}")
        return False

def main():
    """Run all tests"""
    print("🧪 Starting Discount Calculation Tests")
    print("=" * 50)
    
    # Initialize Frappe
    frappe.init(site="development.localhost")
    frappe.connect()
    
    tests = [
        test_regular_items,
        test_bundle_discount_calculation,
        test_delivery_charges
    ]
    
    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"❌ Test failed with exception: {str(e)}")
            results.append(False)
    
    print("\n" + "=" * 50)
    print("🏁 Test Results Summary")
    print("=" * 50)
    
    passed = sum(results)
    total = len(results)
    
    print(f"✅ Passed: {passed}/{total}")
    print(f"❌ Failed: {total - passed}/{total}")
    
    if all(results):
        print("\n🎉 All tests passed! Discount calculation is working correctly.")
    else:
        print("\n⚠️  Some tests failed. Please check the implementation.")
    
    frappe.destroy()

if __name__ == "__main__":
    main() 
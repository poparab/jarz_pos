#!/usr/bin/env python3
"""
Script to set up test item prices for bundle testing
Run this script to create the necessary Item Price records for testing
"""

import frappe

def setup_test_prices():
    """Set up test item prices for bundle testing"""
    
    # Test items and their prices
    test_items = [
        {"item_code": "MANUAL-CHILD-1", "price": 50},
        {"item_code": "MANUAL-CHILD-2", "price": 50},
        {"item_code": "TEST-CHILD-1", "price": 50},
        {"item_code": "TEST-CHILD-2", "price": 50},
    ]
    
    price_list = "Standard Selling"
    
    for item_data in test_items:
        item_code = item_data["item_code"]
        price = item_data["price"]
        
        # Check if Item Price already exists
        existing_price = frappe.db.get_value("Item Price", {
            "item_code": item_code,
            "price_list": price_list
        })
        
        if not existing_price:
            # Create Item Price record
            item_price = frappe.get_doc({
                "doctype": "Item Price",
                "item_code": item_code,
                "price_list": price_list,
                "price_list_rate": price
            })
            item_price.insert()
            print(f"Created Item Price for {item_code}: {price}")
        else:
            print(f"Item Price already exists for {item_code}")
    
    print("Test prices setup complete!")

if __name__ == "__main__":
    setup_test_prices() 
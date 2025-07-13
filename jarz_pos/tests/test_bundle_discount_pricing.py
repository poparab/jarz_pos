"""
Test suite for the new ERPNext-compliant bundle pricing implementation using discount_amount
"""
import json
import frappe
import unittest
from jarz_pos.jarz_pos.page.custom_pos.custom_pos import create_sales_invoice


class TestBundleDiscountPricing(unittest.TestCase):
    
    def setUp(self):
        """Set up test data"""
        # Clear any existing test data
        self.cleanup_test_data()
        
        # Create test company if not exists
        if not frappe.db.exists("Company", "Test Company"):
            company = frappe.new_doc("Company")
            company.company_name = "Test Company"
            company.default_currency = "GBP"
            company.save()
        
        # Create test customer
        if not frappe.db.exists("Customer", "Test Bundle Customer"):
            customer = frappe.new_doc("Customer")
            customer.customer_name = "Test Bundle Customer"
            customer.save()
            
        # Create test price list
        if not frappe.db.exists("Price List", "Test Bundle Price List"):
            price_list = frappe.new_doc("Price List")
            price_list.price_list_name = "Test Bundle Price List"
            price_list.currency = "GBP"
            price_list.selling = 1  # Mark as selling price list
            price_list.save()
            
        # Create test warehouse
        if not frappe.db.exists("Warehouse", "Test Warehouse - TC"):
            warehouse = frappe.new_doc("Warehouse")
            warehouse.warehouse_name = "Test Warehouse"
            warehouse.company = "Test Company"
            warehouse.save()
            
        # Create test POS profile
        if not frappe.db.exists("POS Profile", "Test Bundle POS"):
            pos_profile = frappe.new_doc("POS Profile")
            pos_profile.name = "Test Bundle POS"
            pos_profile.company = "Test Company"
            pos_profile.selling_price_list = "Test Bundle Price List"
            pos_profile.currency = "GBP"
            pos_profile.warehouse = "Test Warehouse - TC"
            pos_profile.save()
            
        # Create test items with prices
        self.create_test_items()
        
    def create_test_items(self):
        """Create test items with stock and prices"""
        items_data = [
            {"item_code": "BUNDLE_PARENT", "item_name": "Bundle Parent Item", "price": 100.00},
            {"item_code": "CHILD_ITEM_1", "item_name": "Child Item 1", "price": 50.00},
            {"item_code": "CHILD_ITEM_2", "item_name": "Child Item 2", "price": 30.00},
            {"item_code": "CHILD_ITEM_3", "item_name": "Child Item 3", "price": 20.00}
        ]
        
        for item_data in items_data:
            # Create item
            if not frappe.db.exists("Item", item_data["item_code"]):
                item = frappe.new_doc("Item")
                item.item_code = item_data["item_code"]
                item.item_name = item_data["item_name"]
                item.standard_rate = item_data["price"]
                item.item_group = "All Item Groups"
                item.save()
                
            # Create item price
            if not frappe.db.exists("Item Price", {
                "item_code": item_data["item_code"], 
                "price_list": "Test Bundle Price List"
            }):
                item_price = frappe.new_doc("Item Price")
                item_price.item_code = item_data["item_code"]
                item_price.price_list = "Test Bundle Price List"
                item_price.price_list_rate = item_data["price"]
                item_price.save()
                
            # Add stock
            stock_entry = frappe.new_doc("Stock Entry")
            stock_entry.stock_entry_type = "Material Receipt"
            stock_entry.company = "Test Company"
            stock_entry.append("items", {
                "item_code": item_data["item_code"],
                "qty": 100,
                "basic_rate": item_data["price"],
                "t_warehouse": "Test Warehouse - TC"
            })
            stock_entry.save()
            stock_entry.submit()

    def test_bundle_pricing_calculation(self):
        """Test that bundle pricing calculations are correct"""
        
        # Test data: Bundle with total original price of 200 (100+50+30+20), 
        # but bundle price should be 150
        cart_data = [{
            "is_bundle": True,
            "item_code": "BUNDLE_PARENT",
            "bundle_name": "Test Bundle",
            "price": 150.00,  # Bundle price (total discount needed: 50)
            "items": [
                {"item_code": "CHILD_ITEM_1", "qty": 1},  # 50.00
                {"item_code": "CHILD_ITEM_2", "qty": 1},  # 30.00
                {"item_code": "CHILD_ITEM_3", "qty": 1}   # 20.00
            ]
        }]
        
        # Create sales invoice
        result = create_sales_invoice(
            cart_json=json.dumps(cart_data),
            customer_name="Test Bundle Customer",
            pos_profile_name="Test Bundle POS"
        )
        
        # Verify the invoice was created
        self.assertIsNotNone(result)
        self.assertIn("name", result)
        
        # Get the actual invoice
        invoice = frappe.get_doc("Sales Invoice", result["name"])
        
        # Verify we have 4 items (1 parent + 3 children)
        self.assertEqual(len(invoice.items), 4)
        
        # Calculate expected discounts
        total_original = 200.00  # 100 + 50 + 30 + 20
        total_discount = 50.00   # 200 - 150
        
        # Verify parent item
        parent_item = invoice.items[0]
        self.assertEqual(parent_item.item_code, "BUNDLE_PARENT")
        self.assertEqual(parent_item.rate, 100.00)
        expected_parent_discount = 50.00 * (100.00 / 200.00)  # 25.00
        self.assertAlmostEqual(parent_item.discount_amount, expected_parent_discount, places=2)
        self.assertAlmostEqual(parent_item.amount, 100.00 - expected_parent_discount, places=2)
        
        # Verify child items have proportional discounts
        child_items = invoice.items[1:]
        child_codes = ["CHILD_ITEM_1", "CHILD_ITEM_2", "CHILD_ITEM_3"]
        child_prices = [50.00, 30.00, 20.00]
        
        total_final_amount = 0
        for i, child_item in enumerate(child_items):
            self.assertEqual(child_item.item_code, child_codes[i])
            self.assertEqual(child_item.rate, child_prices[i])
            
            expected_discount = 50.00 * (child_prices[i] / 200.00)
            self.assertAlmostEqual(child_item.discount_amount, expected_discount, places=2)
            
            expected_final_amount = child_prices[i] - expected_discount
            self.assertAlmostEqual(child_item.amount, expected_final_amount, places=2)
            total_final_amount += child_item.amount
            
        # Verify total amount equals bundle price
        total_invoice_amount = sum(item.amount for item in invoice.items)
        self.assertAlmostEqual(total_invoice_amount, 150.00, places=2)
        
        print(f"✅ Bundle pricing test passed! Total amount: {total_invoice_amount}")

    def test_stock_ledger_entries(self):
        """Test that stock movements are properly recorded"""
        
        cart_data = [{
            "is_bundle": True,
            "item_code": "BUNDLE_PARENT",
            "bundle_name": "Stock Test Bundle",
            "price": 120.00,
            "items": [
                {"item_code": "CHILD_ITEM_1", "qty": 2},
                {"item_code": "CHILD_ITEM_2", "qty": 1}
            ]
        }]
        
        # Create sales invoice
        result = create_sales_invoice(
            cart_json=json.dumps(cart_data),
            customer_name="Test Bundle Customer",
            pos_profile_name="Test Bundle POS"
        )
        
        invoice = frappe.get_doc("Sales Invoice", result["name"])
        
        # Check stock ledger entries were created for all items
        stock_entries = frappe.get_all("Stock Ledger Entry", 
            filters={"voucher_no": invoice.name},
            fields=["item_code", "actual_qty"]
        )
        
        # Should have entries for parent + children
        expected_entries = {
            "BUNDLE_PARENT": -1,    # 1 parent item out
            "CHILD_ITEM_1": -2,     # 2 child items out  
            "CHILD_ITEM_2": -1      # 1 child item out
        }
        
        actual_entries = {entry["item_code"]: entry["actual_qty"] for entry in stock_entries}
        
        for item_code, expected_qty in expected_entries.items():
            self.assertIn(item_code, actual_entries)
            self.assertEqual(actual_entries[item_code], expected_qty)
            
        print(f"✅ Stock ledger test passed! Entries: {actual_entries}")

    def test_accounting_entries(self):
        """Test that GL entries are correctly created"""
        
        cart_data = [{
            "is_bundle": True,
            "item_code": "BUNDLE_PARENT", 
            "bundle_name": "Accounting Test Bundle",
            "price": 100.00,
            "items": [
                {"item_code": "CHILD_ITEM_1", "qty": 1}
            ]
        }]
        
        result = create_sales_invoice(
            cart_json=json.dumps(cart_data),
            customer_name="Test Bundle Customer", 
            pos_profile_name="Test Bundle POS"
        )
        
        invoice = frappe.get_doc("Sales Invoice", result["name"])
        
        # Check GL entries
        gl_entries = frappe.get_all("GL Entry",
            filters={"voucher_no": invoice.name},
            fields=["account", "debit", "credit"]
        )
        
        # Should have debit to customer and credit to income
        total_debits = sum(entry["debit"] for entry in gl_entries)
        total_credits = sum(entry["credit"] for entry in gl_entries)
        
        # Debits should equal credits
        self.assertAlmostEqual(total_debits, total_credits, places=2)
        
        # Total should equal bundle price
        self.assertAlmostEqual(total_debits, 100.00, places=2)
        
        print(f"✅ Accounting test passed! Debits: {total_debits}, Credits: {total_credits}")

    def cleanup_test_data(self):
        """Clean up test data"""
        # Cancel and delete any existing test invoices
        test_invoices = frappe.get_all("Sales Invoice", 
            filters={"customer": "Test Bundle Customer"},
            fields=["name", "docstatus"]
        )
        
        for invoice in test_invoices:
            if invoice["docstatus"] == 1:  # Submitted
                doc = frappe.get_doc("Sales Invoice", invoice["name"])
                doc.cancel()
            frappe.delete_doc("Sales Invoice", invoice["name"])

    def tearDown(self):
        """Clean up after tests"""
        self.cleanup_test_data()


if __name__ == "__main__":
    # Run specific test
    test = TestBundleDiscountPricing()
    test.setUp()
    
    print("🧪 Testing bundle pricing calculation...")
    test.test_bundle_pricing_calculation()
    
    print("🧪 Testing stock ledger entries...")
    test.test_stock_ledger_entries()
    
    print("🧪 Testing accounting entries...")
    test.test_accounting_entries()
    
    test.tearDown()
    print("✅ All tests completed successfully!") 
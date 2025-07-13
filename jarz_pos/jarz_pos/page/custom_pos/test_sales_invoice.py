import frappe
import json
import unittest
from frappe.tests.utils import FrappeTestCase
from jarz_pos.jarz_pos.page.custom_pos.custom_pos import create_sales_invoice


class TestSalesInvoiceCreation(FrappeTestCase):
    
    def setUp(self):
        """Set up test data"""
        # Create test company if not exists
        if not frappe.db.exists("Company", "Test Company"):
            company = frappe.get_doc({
                "doctype": "Company",
                "company_name": "Test Company",
                "abbr": "TC",
                "default_currency": "USD"
            })
            company.insert(ignore_permissions=True)
        
        # Create test customer if not exists
        if not frappe.db.exists("Customer", "Test Customer"):
            customer = frappe.get_doc({
                "doctype": "Customer",
                "customer_name": "Test Customer",
                "customer_type": "Individual"
            })
            customer.insert(ignore_permissions=True)
        
        # Create test items if not exist
        test_items = [
            {"item_code": "ITEM001", "item_name": "Test Item 1", "price": 10.00},
            {"item_code": "ITEM002", "item_name": "Test Item 2", "price": 15.00},
            {"item_code": "ITEM003", "item_name": "Test Item 3", "price": 5.00},
            {"item_code": "BUNDLE001", "item_name": "Bundle Parent", "price": 0.00}
        ]
        
        for item_data in test_items:
            if not frappe.db.exists("Item", item_data["item_code"]):
                item = frappe.get_doc({
                    "doctype": "Item",
                    "item_code": item_data["item_code"],
                    "item_name": item_data["item_name"],
                    "item_group": "Products",
                    "stock_uom": "Nos",
                    "is_stock_item": 1
                })
                item.insert(ignore_permissions=True)
        
        # Create price list if not exists
        if not frappe.db.exists("Price List", "Test Selling"):
            price_list = frappe.get_doc({
                "doctype": "Price List",
                "price_list_name": "Test Selling",
                "selling": 1
            })
            price_list.insert(ignore_permissions=True)
        
        # Create item prices
        for item_data in test_items:
            if not frappe.db.exists("Item Price", {"item_code": item_data["item_code"], "price_list": "Test Selling"}):
                item_price = frappe.get_doc({
                    "doctype": "Item Price",
                    "item_code": item_data["item_code"],
                    "price_list": "Test Selling",
                    "price_list_rate": item_data["price"]
                })
                item_price.insert(ignore_permissions=True)
        
        # Create POS Profile if not exists
        if not frappe.db.exists("POS Profile", "Test POS Profile"):
            pos_profile = frappe.get_doc({
                "doctype": "POS Profile",
                "name": "Test POS Profile",
                "company": "Test Company",
                "selling_price_list": "Test Selling",
                "currency": "USD"
            })
            pos_profile.insert(ignore_permissions=True)
        
        # Create freight account if not exists
        if not frappe.db.exists("Account", {"account_name": "Freight and Forwarding Charges", "company": "Test Company"}):
            account = frappe.get_doc({
                "doctype": "Account",
                "account_name": "Freight and Forwarding Charges",
                "company": "Test Company",
                "parent_account": "Indirect Expenses - TC",
                "account_type": "Expense Account",
                "is_group": 0
            })
            account.insert(ignore_permissions=True)
    
    def test_regular_item_invoice(self):
        """Test regular item invoice creation"""
        cart = [
            {
                "item_code": "ITEM001",
                "qty": 2,
                "price": 10.00
            }
        ]
        
        cart_json = json.dumps(cart)
        
        # Create sales invoice
        invoice = create_sales_invoice(cart_json, "Test Customer", "Test POS Profile")
        
        # Verify invoice creation
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice["customer"], "Test Customer")
        self.assertEqual(len(invoice["items"]), 1)
        
        # Verify item details
        item = invoice["items"][0]
        self.assertEqual(item["item_code"], "ITEM001")
        self.assertEqual(item["qty"], 2)
        self.assertEqual(item["rate"], 10.00)
        self.assertEqual(item["amount"], 20.00)
    
    def test_bundle_discount_calculation(self):
        """Test bundle discount calculation and application"""
        cart = [
            {
                "is_bundle": True,
                "bundle_name": "Test Bundle",
                "item_code": "BUNDLE001",
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
        
        # Create sales invoice
        invoice = create_sales_invoice(cart_json, "Test Customer", "Test POS Profile")
        
        # Verify invoice creation
        self.assertIsNotNone(invoice)
        
        # Should have 4 items: 1 parent bundle + 3 unique items
        self.assertEqual(len(invoice["items"]), 4)
        
        # Verify parent bundle item
        parent_item = invoice["items"][0]
        self.assertEqual(parent_item["item_code"], "BUNDLE001")
        self.assertEqual(parent_item["qty"], 1)
        self.assertEqual(parent_item["rate"], 0)
        self.assertEqual(parent_item["amount"], 0)
        
        # Calculate expected discount
        # Individual total: 10 + 15 + 5 + 10 = 40
        # Bundle price: 25
        # Discount: 40 - 25 = 15
        # Discount percentage: 15/40 * 100 = 37.5%
        expected_discount_percentage = 37.5
        
        # Verify individual items with discount
        bundle_items = invoice["items"][1:]  # Skip parent item
        
        # Find ITEM001 (should have qty 2 due to aggregation)
        item001 = next(item for item in bundle_items if item["item_code"] == "ITEM001")
        self.assertEqual(item001["qty"], 2)
        self.assertEqual(item001["rate"], 10.00)
        self.assertEqual(item001["discount_percentage"], expected_discount_percentage)
        # Expected amount: (10 * 2) - (20 * 0.375) = 20 - 7.5 = 12.5
        self.assertEqual(item001["amount"], 12.5)
        
        # Find ITEM002
        item002 = next(item for item in bundle_items if item["item_code"] == "ITEM002")
        self.assertEqual(item002["qty"], 1)
        self.assertEqual(item002["rate"], 15.00)
        self.assertEqual(item002["discount_percentage"], expected_discount_percentage)
        # Expected amount: 15 - (15 * 0.375) = 15 - 5.625 = 9.375
        self.assertEqual(item002["amount"], 9.375)
        
        # Find ITEM003
        item003 = next(item for item in bundle_items if item["item_code"] == "ITEM003")
        self.assertEqual(item003["qty"], 1)
        self.assertEqual(item003["rate"], 5.00)
        self.assertEqual(item003["discount_percentage"], expected_discount_percentage)
        # Expected amount: 5 - (5 * 0.375) = 5 - 1.875 = 3.125
        self.assertEqual(item003["amount"], 3.125)
        
        # Verify total bundle amount matches bundle price
        bundle_total = item001["amount"] + item002["amount"] + item003["amount"]
        self.assertEqual(bundle_total, 25.00)
    
    def test_bundle_no_discount(self):
        """Test bundle with no discount (bundle price >= individual total)"""
        cart = [
            {
                "is_bundle": True,
                "bundle_name": "No Discount Bundle",
                "item_code": "BUNDLE001", 
                "price": 40.00,  # Bundle price equals individual total
                "items": [
                    {"item_code": "ITEM001"},  # $10.00
                    {"item_code": "ITEM002"},  # $15.00
                    {"item_code": "ITEM003"},  # $5.00
                    {"item_code": "ITEM001"}   # $10.00 (duplicate)
                ]
            }
        ]
        
        cart_json = json.dumps(cart)
        
        # Create sales invoice
        invoice = create_sales_invoice(cart_json, "Test Customer", "Test POS Profile")
        
        # Verify no discount applied
        bundle_items = invoice["items"][1:]  # Skip parent item
        
        for item in bundle_items:
            self.assertEqual(item["discount_percentage"], 0)
            # Amount should equal rate * qty (no discount)
            expected_amount = item["rate"] * item["qty"]
            self.assertEqual(item["amount"], expected_amount)
    
    def test_delivery_charges(self):
        """Test delivery charges calculation"""
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
        
        # Create sales invoice
        invoice = create_sales_invoice(cart_json, "Test Customer", "Test POS Profile", delivery_json)
        
        # Verify delivery charges applied
        self.assertIsNotNone(invoice)
        
        # Should have taxes for delivery
        self.assertGreater(len(invoice["taxes"]), 0)
        
        # Verify delivery income tax
        delivery_income_tax = next(
            (tax for tax in invoice["taxes"] if tax["description"] == "Delivery to Test City"),
            None
        )
        self.assertIsNotNone(delivery_income_tax)
        self.assertEqual(delivery_income_tax["tax_amount"], 15.00)
        
        # Verify delivery expense tax
        delivery_expense_tax = next(
            (tax for tax in invoice["taxes"] if tax["description"] == "Delivery Expense - Test City"),
            None
        )
        self.assertIsNotNone(delivery_expense_tax)
        self.assertEqual(delivery_expense_tax["tax_amount"], -5.00)
        
        # Invoice-level discount_amount removed (delivery expense handled via negative tax row)
        self.assertIsNone(invoice.get("discount_amount"))
    
    def test_mixed_cart_calculation(self):
        """Test mixed cart with regular items, bundles, and delivery"""
        cart = [
            {
                "item_code": "ITEM001",
                "qty": 1,
                "price": 10.00
            },
            {
                "is_bundle": True,
                "bundle_name": "Mixed Bundle",
                "item_code": "BUNDLE001",
                "price": 18.00,  # Bundle price (less than 20 individual total)
                "items": [
                    {"item_code": "ITEM002"},  # $15.00
                    {"item_code": "ITEM003"}   # $5.00
                ]
            }
        ]
        
        delivery_charges = {
            "income": 10.00,
            "expense": 3.00,
            "city": "Mixed City"
        }
        
        cart_json = json.dumps(cart)
        delivery_json = json.dumps(delivery_charges)
        
        # Create sales invoice
        invoice = create_sales_invoice(cart_json, "Test Customer", "Test POS Profile", delivery_json)
        
        # Verify invoice structure
        self.assertIsNotNone(invoice)
        
        # Should have 4 items: 1 regular + 1 bundle parent + 2 bundle items
        self.assertEqual(len(invoice["items"]), 4)
        
        # Verify regular item (no discount)
        regular_item = next(item for item in invoice["items"] if item["item_code"] == "ITEM001")
        self.assertEqual(regular_item["discount_percentage"], 0)
        self.assertEqual(regular_item["amount"], 10.00)
        
        # Verify bundle items have discount
        # Bundle discount: (20 - 18) / 20 * 100 = 10%
        bundle_items = [item for item in invoice["items"] if item.get("description", "").startswith("Part of bundle")]
        
        for item in bundle_items:
            self.assertEqual(item["discount_percentage"], 10.0)
        
        # Verify delivery charges (via negative tax row only, no invoice-level discount)
        self.assertIsNone(invoice.get("discount_amount"))
        self.assertGreater(len(invoice["taxes"]), 0)
    
    def tearDown(self):
        """Clean up test data"""
        # Clean up created test records
        frappe.db.rollback()


if __name__ == "__main__":
    unittest.main() 
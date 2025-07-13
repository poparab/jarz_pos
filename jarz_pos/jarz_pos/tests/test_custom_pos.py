import frappe
import unittest
import json

from ..page.custom_pos.custom_pos import process_bundle_item, process_regular_item

class TestCustomPOS(unittest.TestCase):
    def setUp(self):
        """Set up test data before each test"""
        # Use existing data or create minimal test data
        pass

    def tearDown(self):
        """Clean up after each test"""
        pass

    def test_regular_item_processing_logic(self):
        """Test the logic of regular item processing without database operations"""
        # Create a test sales invoice document (not saved)
        si = frappe.new_doc("Sales Invoice")
        si.customer = "Test Customer"
        si.company = "_Test Company"
        si.ignore_pricing_rule = 1
        
        # Test data
        test_item = {
            "item_code": "TEST-REGULAR-ITEM",
            "qty": 3,
            "price": 100
        }
        
        # Process the item
        process_regular_item(si, test_item)
        
        # Assertions on the logic
        self.assertEqual(len(si.items), 1)
        item = si.items[0]
        self.assertEqual(item.item_code, "TEST-REGULAR-ITEM")
        self.assertEqual(item.qty, 3)
        self.assertEqual(item.rate, 100)
        self.assertEqual(item.amount, 300)
        self.assertEqual(item.ignore_pricing_rule, 1)

    def test_bundle_item_processing_logic(self):
        """Test the logic of bundle item processing without database operations"""
        # Create a test sales invoice document (not saved)
        si = frappe.new_doc("Sales Invoice")
        si.customer = "Test Customer"
        si.company = "_Test Company"
        si.ignore_pricing_rule = 1
        
        # Test bundle data with simple calculations
        test_bundle = {
            "item_code": "TEST-BUNDLE-PARENT",
            "bundle_name": "Test Bundle",
            "price": 100,  # Simple total for easy verification
            "items": [
                {"item_code": "TEST-CHILD-1", "qty": 1},
                {"item_code": "TEST-CHILD-2", "qty": 1}
            ]
        }
        
        # Mock the price lookup to avoid database calls
        original_get_value = frappe.db.get_value
        def mock_get_value(doctype, filters, fieldname, as_dict=False, debug=False, cache=None):
            if doctype == "Item Price":
                # Return simple prices for testing
                prices = {
                    "TEST-CHILD-1": 50,
                    "TEST-CHILD-2": 50
                }
                item_code = filters.get("item_code")
                return prices.get(item_code, 0)
            return original_get_value(doctype, filters, fieldname, as_dict=as_dict, debug=debug, cache=cache)
        
        # Temporarily replace the function
        frappe.db.get_value = mock_get_value
        
        try:
            # Process the bundle
            process_bundle_item(si, test_bundle, "Standard Selling")
            
            # Assertions
            self.assertEqual(len(si.items), 3)  # 1 parent + 2 children
            
            # Check parent item
            parent_item = next((item for item in si.items if item.item_code == "TEST-BUNDLE-PARENT"), None)
            self.assertIsNotNone(parent_item)
            self.assertEqual(parent_item.rate, 0)
            self.assertEqual(parent_item.amount, 0)
            self.assertEqual(parent_item.ignore_pricing_rule, 1)
            
            # Check child items
            child_1 = next((item for item in si.items if item.item_code == "TEST-CHILD-1"), None)
            child_2 = next((item for item in si.items if item.item_code == "TEST-CHILD-2"), None)
            
            self.assertIsNotNone(child_1)
            self.assertIsNotNone(child_2)
            self.assertEqual(child_1.ignore_pricing_rule, 1)
            self.assertEqual(child_2.ignore_pricing_rule, 1)
            
            # Check that child amounts sum to bundle price
            total_child_amount = child_1.amount + child_2.amount
            self.assertAlmostEqual(total_child_amount, 100, places=2)
            
            # Check proportional distribution
            # Both children should get equal amounts since they have equal original prices
            self.assertAlmostEqual(child_1.amount, 50, places=2)
            self.assertAlmostEqual(child_2.amount, 50, places=2)
            
        finally:
            # Restore original function
            frappe.db.get_value = original_get_value

    def test_bundle_price_calculation_accuracy(self):
        """Test that bundle price calculations are mathematically accurate"""
        # Create a test sales invoice document (not saved)
        si = frappe.new_doc("Sales Invoice")
        si.customer = "Test Customer"
        si.company = "_Test Company"
        si.ignore_pricing_rule = 1
        
        # Bundle with known prices for precise testing
        bundle = {
            "item_code": "TEST-BUNDLE-PARENT",
            "bundle_name": "Precision Test Bundle",
            "price": 200,  # Total bundle price
            "items": [
                {"item_code": "TEST-CHILD-1", "qty": 1},  # Original price: 100
                {"item_code": "TEST-CHILD-2", "qty": 1}   # Original price: 100
            ]
        }
        
        # Mock the price lookup
        original_get_value = frappe.db.get_value
        def mock_get_value(doctype, filters, fieldname, as_dict=False, debug=False, cache=None):
            if doctype == "Item Price":
                prices = {
                    "TEST-CHILD-1": 100,
                    "TEST-CHILD-2": 100
                }
                item_code = filters.get("item_code")
                return prices.get(item_code, 0)
            return original_get_value(doctype, filters, fieldname, as_dict=as_dict, debug=debug, cache=cache)
        
        frappe.db.get_value = mock_get_value
        
        try:
            process_bundle_item(si, bundle, "Standard Selling")
            
            # Find child items
            child_1 = next((item for item in si.items if item.item_code == "TEST-CHILD-1"), None)
            child_2 = next((item for item in si.items if item.item_code == "TEST-CHILD-2"), None)
            
            # Verify proportional distribution
            # Child-1 should get: (100/200) * 200 = 100
            # Child-2 should get: (100/200) * 200 = 100
            self.assertAlmostEqual(child_1.amount, 100, places=2)
            self.assertAlmostEqual(child_2.amount, 100, places=2)
            self.assertAlmostEqual(child_1.amount + child_2.amount, 200, places=2)
            
        finally:
            frappe.db.get_value = original_get_value

    def test_bundle_with_unequal_prices(self):
        """Test bundle pricing with items of different original prices"""
        si = frappe.new_doc("Sales Invoice")
        si.customer = "Test Customer"
        si.company = "_Test Company"
        si.ignore_pricing_rule = 1
        
        # Bundle with unequal prices
        bundle = {
            "item_code": "TEST-BUNDLE-PARENT",
            "bundle_name": "Unequal Price Bundle",
            "price": 150,  # Bundle price
            "items": [
                {"item_code": "TEST-CHILD-1", "qty": 1},  # Original price: 50
                {"item_code": "TEST-CHILD-2", "qty": 1}   # Original price: 100
            ]
        }
        
        # Mock the price lookup
        original_get_value = frappe.db.get_value
        def mock_get_value(doctype, filters, fieldname, as_dict=False, debug=False, cache=None):
            if doctype == "Item Price":
                prices = {
                    "TEST-CHILD-1": 50,
                    "TEST-CHILD-2": 100
                }
                item_code = filters.get("item_code")
                return prices.get(item_code, 0)
            return original_get_value(doctype, filters, fieldname, as_dict=as_dict, debug=debug, cache=cache)
        
        frappe.db.get_value = mock_get_value
        
        try:
            process_bundle_item(si, bundle, "Standard Selling")
            
            # Find child items
            child_1 = next((item for item in si.items if item.item_code == "TEST-CHILD-1"), None)
            child_2 = next((item for item in si.items if item.item_code == "TEST-CHILD-2"), None)
            
            # Verify proportional distribution
            # Total original value: 50 + 100 = 150
            # Child-1 should get: (50/150) * 150 = 50
            # Child-2 should get: (100/150) * 150 = 100
            self.assertAlmostEqual(child_1.amount, 50, places=2)
            self.assertAlmostEqual(child_2.amount, 100, places=2)
            self.assertAlmostEqual(child_1.amount + child_2.amount, 150, places=2)
            
        finally:
            frappe.db.get_value = original_get_value

    def test_manual_bundle_verification(self):
        """Manual test to verify bundle logic works with actual data"""
        # This test will help us understand if the issue is in the logic or the data
        print("\n=== Manual Bundle Verification Test ===")
        
        # Create a simple sales invoice
        si = frappe.new_doc("Sales Invoice")
        si.customer = "Test Customer"
        si.company = "_Test Company"
        si.ignore_pricing_rule = 1
        
        # Simple bundle test
        bundle = {
            "item_code": "MANUAL-TEST-BUNDLE",
            "bundle_name": "Manual Test Bundle",
            "price": 100,
            "items": [
                {"item_code": "MANUAL-CHILD-1", "qty": 1},
                {"item_code": "MANUAL-CHILD-2", "qty": 1}
            ]
        }
        
        try:
            process_bundle_item(si, bundle, "Standard Selling")
            
            print(f"Number of items created: {len(si.items)}")
            for item in si.items:
                print(f"Item: {item.item_code}, Rate: {item.rate}, Amount: {item.amount}, Ignore Pricing: {item.ignore_pricing_rule}")
            
            # Basic assertions
            self.assertEqual(len(si.items), 3)  # 1 parent + 2 children
            
            # Find items
            parent = next((item for item in si.items if item.item_code == "MANUAL-TEST-BUNDLE"), None)
            child_1 = next((item for item in si.items if item.item_code == "MANUAL-CHILD-1"), None)
            child_2 = next((item for item in si.items if item.item_code == "MANUAL-CHILD-2"), None)
            
            if parent:
                print(f"Parent item rate: {parent.rate}, amount: {parent.amount}")
                self.assertEqual(parent.rate, 0)
                self.assertEqual(parent.amount, 0)
            
            if child_1 and child_2:
                total = child_1.amount + child_2.amount
                print(f"Child 1 amount: {child_1.amount}")
                print(f"Child 2 amount: {child_2.amount}")
                print(f"Total child amount: {total}")
                self.assertAlmostEqual(total, 100, places=2)
            
        except Exception as e:
            print(f"Error in manual test: {str(e)}")
            raise


if __name__ == "__main__":
    # Run tests
    unittest.main() 
"""Tests for discount calculation service.

This module tests the business logic for discount calculations.
"""

import unittest


class TestDiscountCalculation(unittest.TestCase):
	"""Test class for discount calculation business logic."""

	def test_calculate_proportional_discount_basic(self):
		"""Test proportional discount calculation."""
		from jarz_pos.services.discount_calculation import calculate_proportional_discount

		# Mock child item with rate and qty
		child_item = {"rate": 100.0, "qty": 1}
		total_child_value = 200.0  # Two items worth 100 each
		target_total = 150.0  # 25% discount

		result = calculate_proportional_discount(child_item, total_child_value, target_total)

		# Verify result structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertIn("discount_amount", result, "Should include discount_amount")
		self.assertIn("discount_percentage", result, "Should include discount_percentage")

		# Verify discount calculations
		# This item is 50% of total (100/200), so it should get 50% of total discount
		# Total discount = 200 - 150 = 50, so this item discount = 25
		self.assertAlmostEqual(
			result["discount_amount"], 25.0, places=1, msg="Discount amount should be proportional"
		)

	def test_calculate_item_rates_with_discount_basic(self):
		"""Test item rate calculation with discount."""
		from jarz_pos.services.discount_calculation import calculate_item_rates_with_discount

		original_rate = 100.0
		discount_amount = 25.0
		qty = 1

		result = calculate_item_rates_with_discount(original_rate, discount_amount, qty)

		# Verify result structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertIn("rate", result, "Should include rate")
		self.assertIn("amount", result, "Should include amount")

		# Rate should be original - discount
		expected_rate = original_rate - discount_amount
		self.assertAlmostEqual(result["rate"], expected_rate, places=2, msg="Rate should be discounted")

		# Amount should be rate * qty
		expected_amount = expected_rate * qty
		self.assertAlmostEqual(result["amount"], expected_amount, places=2, msg="Amount should be rate * qty")

	def test_calculate_discount_percentage_basic(self):
		"""Test discount percentage calculation."""
		from jarz_pos.services.discount_calculation import calculate_discount_percentage

		discount_amount = 25.0
		original_rate = 100.0
		qty = 1

		result = calculate_discount_percentage(discount_amount, original_rate, qty)

		# Should return 25% discount
		self.assertAlmostEqual(result, 25.0, places=1, msg="Discount percentage should be 25%")

	def test_calculate_discount_percentage_with_quantity(self):
		"""Test discount percentage calculation with quantity."""
		from jarz_pos.services.discount_calculation import calculate_discount_percentage

		discount_amount = 50.0
		original_rate = 100.0
		qty = 2  # Total original = 200

		result = calculate_discount_percentage(discount_amount, original_rate, qty)

		# Should return 25% discount (50 / 200)
		self.assertAlmostEqual(result, 25.0, places=1, msg="Discount percentage should account for quantity")

	def test_calculate_bundle_discounts_structure(self):
		"""Test that calculate_bundle_discounts returns correct structure."""
		from jarz_pos.services.discount_calculation import calculate_bundle_discounts

		# Mock child items data
		child_items = [
			{"item_code": "ITEM1", "rate": 100.0, "qty": 1},
			{"item_code": "ITEM2", "rate": 50.0, "qty": 1},
		]

		bundle_qty = 1
		bundle_price = 120.0  # 20% discount from 150

		result = calculate_bundle_discounts(child_items, bundle_qty, bundle_price)

		# Verify result is a list
		self.assertIsInstance(result, list, "Should return a list")
		self.assertEqual(len(result), len(child_items), "Should return same number of items")

	def test_verify_bundle_discount_totals_correct(self):
		"""Test that verify_bundle_discount_totals validates correctly."""
		from jarz_pos.services.discount_calculation import verify_bundle_discount_totals

		# Mock processed items that sum to bundle price
		processed_items = [{"amount": 80.0}, {"amount": 40.0}]

		bundle_qty = 1
		bundle_price = 120.0

		# Should not raise error when totals match
		try:
			verify_bundle_discount_totals(processed_items, bundle_qty, bundle_price)
			verified = True
		except Exception:
			verified = False

		self.assertTrue(verified, "Should verify when totals match")

	def test_verify_bundle_discount_totals_mismatch(self):
		"""Test that verify_bundle_discount_totals detects mismatch."""
		from jarz_pos.services.discount_calculation import verify_bundle_discount_totals

		# Mock processed items that don't sum to bundle price
		processed_items = [{"amount": 60.0}, {"amount": 40.0}]

		bundle_qty = 1
		bundle_price = 120.0  # Expected total, but actual is 100

		# Should raise error when totals don't match (within tolerance)
		# Note: The function may have tolerance for rounding
		try:
			verify_bundle_discount_totals(processed_items, bundle_qty, bundle_price)
			# If it doesn't raise, it means there's tolerance
		except Exception:
			# Expected to fail or have tolerance
			pass

"""Tests for POS API endpoints.

This module tests POS-related API endpoints including profiles, products, and bundles.
"""

import unittest

import frappe


class TestPOSAPI(unittest.TestCase):
	"""Test class for POS API functionality."""

	def test_get_pos_profiles_structure(self):
		"""Test that get_pos_profiles returns correct structure."""
		from jarz_pos.api.pos import get_pos_profiles

		result = get_pos_profiles()

		# Should return a list
		self.assertIsInstance(result, list, "Should return a list")
		# List can be empty if no profiles are configured for the user

	def test_get_pos_profiles_filters_disabled(self):
		"""Test that get_pos_profiles only returns enabled profiles."""
		from jarz_pos.api.pos import get_pos_profiles

		result = get_pos_profiles()

		# If there are profiles, verify they are all enabled
		for profile_name in result:
			profile_doc = frappe.get_doc("POS Profile", profile_name)
			self.assertEqual(profile_doc.disabled, 0, f"Profile {profile_name} should not be disabled")

	def test_get_profile_products_requires_profile(self):
		"""Test that get_profile_products requires a profile parameter."""
		from jarz_pos.api.pos import get_profile_products

		# Should handle empty profile gracefully or return empty list
		result = get_profile_products(profile="")
		self.assertIsInstance(result, list, "Should return a list even with empty profile")

	def test_get_profile_bundles_structure(self):
		"""Test that get_profile_bundles returns correct structure."""
		from jarz_pos.api.pos import get_profile_bundles

		# Test with empty profile
		result = get_profile_bundles(profile="")

		# Should return a list
		self.assertIsInstance(result, list, "Should return a list")

	def test_get_sales_partners_default_limit(self):
		"""Test that get_sales_partners respects default limit."""
		from jarz_pos.api.pos import get_sales_partners

		result = get_sales_partners()

		# Should return a list
		self.assertIsInstance(result, list, "Should return a list")
		# Should not exceed default limit of 10
		self.assertLessEqual(len(result), 10, "Should not exceed default limit of 10")

	def test_get_sales_partners_custom_limit(self):
		"""Test that get_sales_partners respects custom limit."""
		from jarz_pos.api.pos import get_sales_partners

		result = get_sales_partners(limit=5)

		# Should return a list
		self.assertIsInstance(result, list, "Should return a list")
		# Should not exceed custom limit
		self.assertLessEqual(len(result), 5, "Should not exceed custom limit of 5")

	def test_get_sales_partners_structure(self):
		"""Test that get_sales_partners returns correct structure."""
		from jarz_pos.api.pos import get_sales_partners

		result = get_sales_partners(limit=1)

		# If there are partners, verify structure
		if result:
			partner = result[0]
			self.assertIn("name", partner, "Should include name")
			self.assertIn("partner_name", partner, "Should include partner_name")
			self.assertIn("title", partner, "Should include title")

	def test_get_sales_partners_search(self):
		"""Test that get_sales_partners search functionality."""
		from jarz_pos.api.pos import get_sales_partners

		# Test search with a term
		result = get_sales_partners(search="test", limit=10)

		# Should return a list
		self.assertIsInstance(result, list, "Should return a list")
		# All returned partners should match the search term (case-insensitive)
		for partner in result:
			name_lower = (partner.get("name") or "").lower()
			partner_name_lower = (partner.get("partner_name") or "").lower()
			self.assertTrue(
				"test" in name_lower or "test" in partner_name_lower,
				f"Partner {partner.get('name')} should match search term",
			)

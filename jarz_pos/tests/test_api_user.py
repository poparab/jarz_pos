"""Tests for user API endpoints.

This module tests the user-related API endpoints including user roles and permissions.
"""
import unittest
import frappe


class TestUserAPI(unittest.TestCase):
	"""Test class for User API functionality."""

	def test_get_current_user_roles(self):
		"""Test retrieving current user roles."""
		from jarz_pos.api.user import get_current_user_roles

		result = get_current_user_roles()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertIn("user", result, "Should include user key")
		self.assertIn("full_name", result, "Should include full_name key")
		self.assertIn("roles", result, "Should include roles key")
		self.assertIn("is_jarz_manager", result, "Should include is_jarz_manager key")

		# Verify data types
		self.assertIsInstance(result["roles"], list, "Roles should be a list")
		self.assertIsInstance(result["is_jarz_manager"], bool, "is_jarz_manager should be boolean")

		# Verify user is set
		self.assertEqual(result["user"], frappe.session.user, "User should match session user")

	def test_jarz_manager_role_detection(self):
		"""Test JARZ Manager role detection logic."""
		from jarz_pos.api.user import get_current_user_roles

		result = get_current_user_roles()

		# Check if role detection logic works
		has_manager_role = "JARZ Manager" in result["roles"]
		self.assertEqual(
			result["is_jarz_manager"],
			has_manager_role,
			"is_jarz_manager should match JARZ Manager role presence",
		)

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


class TestManagerHomeFlag(unittest.TestCase):
	"""User.custom_manager_home: the mobile app's revenue-dashboard home and
	manager bottom bar. Off unless switched on for a user."""

	def test_manager_home_is_a_bool_in_the_payload(self):
		from jarz_pos.api.user import get_current_user_roles

		result = get_current_user_roles()
		self.assertIn("manager_home", result)
		self.assertIsInstance(result["manager_home"], bool)

	def test_manager_home_follows_the_user_field(self):
		from jarz_pos.api.user import get_current_user_roles

		if not frappe.get_meta("User").has_field("custom_manager_home"):
			self.skipTest("custom_manager_home not migrated on this site")
		user = frappe.session.user
		before = frappe.db.get_value("User", user, "custom_manager_home")
		try:
			frappe.db.set_value("User", user, "custom_manager_home", 1, update_modified=False)
			self.assertIs(get_current_user_roles()["manager_home"], True)
			frappe.db.set_value("User", user, "custom_manager_home", 0, update_modified=False)
			self.assertIs(get_current_user_roles()["manager_home"], False)
		finally:
			frappe.db.set_value("User", user, "custom_manager_home", before or 0, update_modified=False)

	def test_unmigrated_site_answers_false(self):
		from unittest import mock

		from jarz_pos.api import user as user_api

		real_get_meta = frappe.get_meta
		# The real User meta, except that it has never heard of the field.
		# Frappe's own query layer reads metas too, so everything else on it
		# must behave normally.
		user_meta = mock.Mock(wraps=real_get_meta("User"))
		user_meta.has_field.side_effect = lambda fieldname: (
			False if fieldname == "custom_manager_home" else real_get_meta("User").has_field(fieldname)
		)

		def get_meta(doctype, *args, **kwargs):
			if doctype == "User":
				return user_meta
			return real_get_meta(doctype, *args, **kwargs)

		with mock.patch.object(user_api.frappe, "get_meta", side_effect=get_meta):
			result = user_api.get_current_user_roles()
		self.assertIs(result["manager_home"], False)

	def test_field_is_in_the_fixture_and_the_fixture_filter(self):
		import json
		import os

		from jarz_pos import hooks

		path = os.path.join(os.path.dirname(hooks.__file__), "fixtures", "custom_field.json")
		with open(path, encoding="utf-8") as fh:
			fields = {f["name"]: f for f in json.load(fh)}
		field = fields.get("User-custom_manager_home")
		self.assertIsNotNone(field)
		self.assertEqual(field["fieldtype"], "Check")
		self.assertEqual(field["default"], "0")
		self.assertEqual(field["dt"], "User")
		dts = next(
			flt[2]
			for fx in hooks.fixtures
			if isinstance(fx, dict) and fx.get("dt") == "Custom Field"
			for flt in fx.get("filters", [])
			if flt[0] == "dt"
		)
		self.assertIn("User", dts)

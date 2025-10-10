"""Tests for test connection API endpoints.

This module tests connectivity and health check endpoints.
"""

import unittest


class TestConnectionAPI(unittest.TestCase):
	"""Test class for connection and health check API functionality."""

	def test_ping(self):
		"""Test basic ping endpoint."""
		from jarz_pos.api.test_connection import ping

		result = ping()

		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("message", result, "Should include message")

	def test_health_check(self):
		"""Test comprehensive health check endpoint."""
		from jarz_pos.api.test_connection import health_check

		result = health_check()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("message", result, "Should include message")
		self.assertIn("timestamp", result, "Should include timestamp")
		self.assertIn("user", result, "Should include user")
		self.assertIn("tests", result, "Should include tests")
		self.assertIn("app_info", result, "Should include app_info")

		# Verify tests
		tests = result["tests"]
		self.assertIn("database", tests, "Should test database")
		self.assertIn("redis", tests, "Should test redis")
		self.assertTrue(tests["database"], "Database test should pass")

		# Verify app info
		app_info = result["app_info"]
		self.assertIn("app_name", app_info, "Should include app_name")
		self.assertIn("app_version", app_info, "Should include app_version")
		self.assertIn("frappe_version", app_info, "Should include frappe_version")
		self.assertEqual(app_info["app_name"], "jarz_pos", "App name should be jarz_pos")

	def test_get_backend_info(self):
		"""Test backend information endpoint."""
		from jarz_pos.api.test_connection import get_backend_info

		result = get_backend_info()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should return success=True")
		self.assertIn("data", result, "Should include data")

		# Verify data
		data = result["data"]
		self.assertIn("app_name", data, "Should include app_name")
		self.assertIn("app_version", data, "Should include app_version")
		self.assertIn("frappe_version", data, "Should include frappe_version")
		self.assertIn("site", data, "Should include site")
		self.assertIn("user", data, "Should include user")
		self.assertIn("api_endpoints", data, "Should include api_endpoints")
		self.assertIn("timestamp", data, "Should include timestamp")

		# Verify API endpoints list
		self.assertIsInstance(data["api_endpoints"], list, "api_endpoints should be a list")
		self.assertTrue(len(data["api_endpoints"]) > 0, "Should have at least one endpoint")

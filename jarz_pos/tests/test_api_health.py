"""Tests for health API endpoint.

This module tests the simple health check endpoint.
"""

import unittest


class TestHealthAPI(unittest.TestCase):
	"""Test class for health API functionality."""

	def test_ping(self):
		"""Test simple ping endpoint."""
		from jarz_pos.api.health import ping

		result = ping()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertIn("ok", result, "Should include ok key")
		self.assertTrue(result["ok"], "ok should be truthy")
		self.assertEqual(result.get("message"), "pong", "Message should be pong")

"""Tests for health API endpoint.

This module tests the simple health check endpoint.
"""
import unittest
import frappe


class TestHealthAPI(unittest.TestCase):
	"""Test class for health API functionality."""

	def test_ping(self):
		"""Test simple ping endpoint."""
		from jarz_pos.api.health import ping

		result = ping()

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertIn("status", result, "Should include status key")
		self.assertEqual(result["status"], "ok", "Status should be 'ok'")

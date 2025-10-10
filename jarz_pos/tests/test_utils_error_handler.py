"""Tests for error handler utilities.

This module tests error handling utility functions.
"""

import unittest


class TestErrorHandler(unittest.TestCase):
	"""Test class for error handler functionality."""

	def test_success_response_structure(self):
		"""Test success_response utility function."""
		from jarz_pos.utils.error_handler import success_response

		# Test with data
		result = success_response(data={"key": "value"})

		# Verify response structure
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertTrue(result.get("success"), "Should have success=True")
		self.assertIn("data", result, "Should include data")
		self.assertEqual(result["data"]["key"], "value", "Should include provided data")

	def test_success_response_with_message(self):
		"""Test success_response with custom message."""
		from jarz_pos.utils.error_handler import success_response

		result = success_response(message="Operation successful", data=None)

		# Verify response structure
		self.assertTrue(result.get("success"), "Should have success=True")
		self.assertIn("message", result, "Should include message")
		self.assertEqual(result["message"], "Operation successful", "Should include custom message")

	def test_handle_api_error_response(self):
		"""Test handle_api_error helper function."""
		from jarz_pos.utils.error_handler import handle_api_error

		try:
			raise ValueError("Test error")
		except ValueError as exc:
			result = handle_api_error(exc, context="Unit Test")

		self.assertIsInstance(result, dict, "Should return standardized error response")
		self.assertFalse(result.get("success"), "Should have success=False")
		self.assertTrue(result.get("error"), "Should flag error")
		self.assertEqual(result.get("context"), "Unit Test", "Should include context")

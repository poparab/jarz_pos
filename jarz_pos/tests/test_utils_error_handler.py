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

	def test_handle_api_error_decorator(self):
		"""Test handle_api_error decorator functionality."""
		from jarz_pos.utils.error_handler import handle_api_error

		# Create a test function that raises an error
		@handle_api_error
		def test_function_with_error():
			raise ValueError("Test error")

		# Call the function
		result = test_function_with_error()

		# Should return error response instead of raising
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertFalse(result.get("success"), "Should have success=False")
		self.assertIn("error", result, "Should include error message")

	def test_handle_api_error_success_case(self):
		"""Test handle_api_error with successful function."""
		from jarz_pos.utils.error_handler import handle_api_error

		# Create a test function that succeeds
		@handle_api_error
		def test_function_success():
			return {"data": "test"}

		# Call the function
		result = test_function_success()

		# Should return the original result
		self.assertIsInstance(result, dict, "Should return a dictionary")
		self.assertEqual(result.get("data"), "test", "Should return original data")

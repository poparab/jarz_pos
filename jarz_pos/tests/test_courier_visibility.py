import unittest
from types import SimpleNamespace
from unittest.mock import patch


class TestCourierVisibilityHelpers(unittest.TestCase):

	@patch("jarz_pos.utils.courier_visibility.frappe")
	def test_assert_courier_matches_pos_profile_rejects_cross_branch_assignment(self, mock_frappe):
		from jarz_pos.utils.courier_visibility import assert_courier_matches_pos_profile

		def throw_side_effect(message, *args, **kwargs):
			raise Exception(str(message))

		mock_frappe.throw.side_effect = throw_side_effect
		mock_frappe.db.exists.return_value = True

		def cached_value_side_effect(doctype, name, fieldname):
			if doctype == "Employee" and name == "EMP-1" and fieldname == "branch":
				return "Dokki"
			if doctype == "Employee" and name == "EMP-1" and fieldname == "status":
				return "Active"
			return None

		mock_frappe.get_cached_value.side_effect = cached_value_side_effect

		with self.assertRaisesRegex(Exception, "belongs to POS Profile Dokki, not Nasr city"):
			assert_courier_matches_pos_profile("Employee", "EMP-1", "Nasr city")

	@patch("jarz_pos.utils.courier_visibility.assert_pos_profile_enabled")
	@patch("jarz_pos.utils.courier_visibility.frappe")
	def test_resolve_assignment_pos_profile_rejects_requested_profile_mismatch(
		self,
		mock_frappe,
		mock_assert_enabled,
	):
		from jarz_pos.utils.courier_visibility import resolve_assignment_pos_profile

		def throw_side_effect(message, *args, **kwargs):
			raise Exception(str(message))

		mock_frappe.throw.side_effect = throw_side_effect
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.get_roles.return_value = ["System Manager"]

		invoice = SimpleNamespace(
			name="SINV-40",
			custom_kanban_profile="Nasr city",
			pos_profile="Dokki",
		)

		with self.assertRaisesRegex(Exception, "belongs to POS Profile Nasr city, not Dokki"):
			resolve_assignment_pos_profile(invoice, requested_pos_profile="Dokki")

		mock_assert_enabled.assert_any_call("Dokki")
		mock_assert_enabled.assert_any_call("Nasr city")

	@patch("jarz_pos.utils.courier_visibility.assert_pos_profile_enabled")
	@patch("jarz_pos.utils.courier_visibility.frappe")
	def test_assert_invoices_share_pos_profile_rejects_mixed_invoices(
		self,
		mock_frappe,
		mock_assert_enabled,
	):
		from jarz_pos.utils.courier_visibility import assert_invoices_share_pos_profile

		def throw_side_effect(message, *args, **kwargs):
			raise Exception(str(message))

		mock_frappe.throw.side_effect = throw_side_effect
		mock_frappe.session.user = "user@example.com"
		mock_frappe.get_roles.return_value = []
		mock_frappe.get_all.side_effect = [
			["Nasr city", "Dokki"],
			["Nasr city", "Dokki"],
		]

		invoices = [
			SimpleNamespace(name="SINV-50", custom_kanban_profile="Nasr city", pos_profile="Nasr city"),
			SimpleNamespace(name="SINV-51", custom_kanban_profile="Dokki", pos_profile="Dokki"),
		]

		with self.assertRaisesRegex(Exception, "same POS Profile"):
			assert_invoices_share_pos_profile(invoices)

		mock_assert_enabled.assert_not_called()
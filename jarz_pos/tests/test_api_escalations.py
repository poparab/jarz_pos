"""Tests for the unconfirmed-online-payment escalation feed.

Covers the permission gate on ``jarz_pos.api.escalations.list_unconfirmed_online_payment_escalations``,
its happy-path shape, and that both it and the hourly job
(``jarz_pos.tasks.escalate_unconfirmed_online_payments``) read the same threshold
through ``jarz_pos.tasks.get_unconfirmed_online_payment_alert_hours``.
"""

import unittest
from unittest.mock import MagicMock, patch


class TestEnsureEscalationAccess(unittest.TestCase):
	"""Role gate: admin / line-manager tier only, mirroring the confirm action
	on the same InstaPay Reconciliation screen."""

	def test_denies_plain_staff_role(self):
		from jarz_pos.api.escalations import _ensure_escalation_access

		mock_frappe = MagicMock()
		mock_frappe.PermissionError = PermissionError
		mock_frappe.throw.side_effect = PermissionError("Not permitted")

		with patch("jarz_pos.api.escalations.frappe", mock_frappe):
			mock_frappe.get_roles.return_value = ["Sales User"]
			with self.assertRaises(PermissionError):
				_ensure_escalation_access()

	def test_allows_jarz_manager(self):
		from jarz_pos.api.escalations import _ensure_escalation_access

		mock_frappe = MagicMock()

		with patch("jarz_pos.api.escalations.frappe", mock_frappe):
			mock_frappe.get_roles.return_value = ["JARZ Manager"]
			_ensure_escalation_access()  # should not raise
			mock_frappe.throw.assert_not_called()

	def test_allows_line_manager_alt_spelling(self):
		from jarz_pos.api.escalations import _ensure_escalation_access

		mock_frappe = MagicMock()

		with patch("jarz_pos.api.escalations.frappe", mock_frappe):
			mock_frappe.get_roles.return_value = ["JARZ line manager"]
			_ensure_escalation_access()  # should not raise
			mock_frappe.throw.assert_not_called()

	def test_allows_system_manager(self):
		from jarz_pos.api.escalations import _ensure_escalation_access

		mock_frappe = MagicMock()

		with patch("jarz_pos.api.escalations.frappe", mock_frappe):
			mock_frappe.get_roles.return_value = ["System Manager"]
			_ensure_escalation_access()  # should not raise
			mock_frappe.throw.assert_not_called()


class TestListUnconfirmedOnlinePaymentEscalations(unittest.TestCase):
	def test_denies_non_manager_caller(self):
		from jarz_pos.api.escalations import list_unconfirmed_online_payment_escalations

		mock_frappe = MagicMock()
		mock_frappe.PermissionError = PermissionError
		mock_frappe.throw.side_effect = PermissionError("Not permitted")
		mock_frappe.get_roles.return_value = ["Sales User"]

		with patch("jarz_pos.api.escalations.frappe", mock_frappe):
			with self.assertRaises(PermissionError):
				list_unconfirmed_online_payment_escalations()

		mock_frappe.get_all.assert_not_called()

	def test_happy_path_shape_and_threshold_reuse(self):
		from jarz_pos.api.escalations import list_unconfirmed_online_payment_escalations

		mock_frappe = MagicMock()
		mock_frappe.get_roles.return_value = ["JARZ Manager"]

		rows = [
			{
				"name": "ACC-SINV-0100",
				"customer": "CUST-1",
				"customer_name": "Jarz Test Customer",
				"grand_total": 250.0,
				"custom_payment_method": "InstaPay",
				"custom_ofd_unconfirmed_since": "2026-09-07 00:00:00",
				"custom_kanban_profile": "Nasr city",
				"pos_profile": "Nasr city",
				"custom_payment_confirmation_alerted": 1,
				"woo_order_id": 0,
			}
		]
		mock_frappe.get_all.return_value = rows

		with patch("jarz_pos.api.escalations.frappe", mock_frappe), \
				patch("jarz_pos.api.escalations.get_unconfirmed_online_payment_alert_hours", return_value=4), \
				patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Nasr city"]):
			result = list_unconfirmed_online_payment_escalations()

		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("threshold_hours"), 4)
		self.assertEqual(len(result.get("orders")), 1)

		order = result["orders"][0]
		self.assertEqual(order["invoice"], "ACC-SINV-0100")
		self.assertIsNone(order["woo_order_id"])  # 0 collapses to None
		self.assertEqual(order["customer"], "CUST-1")
		self.assertEqual(order["customer_name"], "Jarz Test Customer")
		self.assertEqual(order["branch"], "Nasr city")
		self.assertEqual(order["amount"], 250.0)
		self.assertEqual(order["payment_method"], "InstaPay")
		self.assertEqual(order["out_for_delivery_since"], "2026-09-07 00:00:00")
		self.assertIn("out_for_delivery_seconds", order)
		self.assertEqual(order["threshold_hours"], 4)
		self.assertTrue(order["already_alerted"])

		# Branch scoping: no explicit pos_profile -> filtered to the caller's accessible profiles.
		_, kwargs = mock_frappe.get_all.call_args
		self.assertEqual(kwargs["filters"]["custom_kanban_profile"], ["in", ["Nasr city"]])
		self.assertEqual(kwargs["filters"]["custom_payment_confirmation_status"], "Awaiting Payment")

	def test_explicit_pos_profile_narrows_filter(self):
		from jarz_pos.api.escalations import list_unconfirmed_online_payment_escalations

		mock_frappe = MagicMock()
		mock_frappe.get_roles.return_value = ["System Manager"]
		mock_frappe.get_all.return_value = []

		with patch("jarz_pos.api.escalations.frappe", mock_frappe), \
				patch("jarz_pos.api.escalations.get_unconfirmed_online_payment_alert_hours", return_value=6), \
				patch("jarz_pos.api.manager._current_user_allowed_profiles", return_value=["Nasr city", "Dokki"]):
			result = list_unconfirmed_online_payment_escalations(pos_profile="Dokki")

		self.assertEqual(result.get("orders"), [])
		_, kwargs = mock_frappe.get_all.call_args
		self.assertEqual(kwargs["filters"]["custom_kanban_profile"], "Dokki")


class TestSharedThresholdHelper(unittest.TestCase):
	"""The hourly job and the escalation feed must never disagree about the threshold."""

	def test_reads_configured_threshold_from_settings(self):
		from jarz_pos import tasks

		fake_settings = MagicMock()
		fake_settings.instapay_unconfirmed_alert_hours = 9

		with patch(
			"jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings.get_jarz_settings",
			return_value=fake_settings,
		):
			self.assertEqual(tasks.get_unconfirmed_online_payment_alert_hours(), 9)

	def test_falls_back_to_default_when_settings_missing_or_invalid(self):
		from jarz_pos import tasks

		fake_settings = MagicMock()
		fake_settings.instapay_unconfirmed_alert_hours = None

		with patch(
			"jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings.get_jarz_settings",
			return_value=fake_settings,
		):
			self.assertEqual(
				tasks.get_unconfirmed_online_payment_alert_hours(),
				tasks.DEFAULT_UNCONFIRMED_ONLINE_PAYMENT_ALERT_HOURS,
			)

	def test_falls_back_to_default_when_settings_lookup_raises(self):
		from jarz_pos import tasks

		with patch(
			"jarz_pos.doctype.jarz_pos_settings.jarz_pos_settings.get_jarz_settings",
			side_effect=Exception("boom"),
		):
			self.assertEqual(
				tasks.get_unconfirmed_online_payment_alert_hours(),
				tasks.DEFAULT_UNCONFIRMED_ONLINE_PAYMENT_ALERT_HOURS,
			)


if __name__ == "__main__":
	unittest.main()

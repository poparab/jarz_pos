"""Focused tests for Sales Invoice event handlers."""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestSalesInvoiceEvents(unittest.TestCase):
	"""Regression coverage for Kanban profile seeding behavior."""

	def test_sync_kanban_profile_keeps_draft_aligned_with_pos_profile(self):
		"""Draft invoices should still mirror POS Profile into the Kanban field."""
		from jarz_pos.events.sales_invoice import sync_kanban_profile

		doc = SimpleNamespace(docstatus=0, pos_profile="Dokki", custom_kanban_profile="Nasr city")

		sync_kanban_profile(doc)

		self.assertEqual(doc.custom_kanban_profile, "Dokki")

	def test_sync_kanban_profile_preserves_submitted_reassignment(self):
		"""Submitted invoices must keep the explicit Kanban reassignment field untouched."""
		from jarz_pos.events.sales_invoice import sync_kanban_profile

		doc = SimpleNamespace(docstatus=1, pos_profile="Dokki", custom_kanban_profile="Nasr city")

		sync_kanban_profile(doc)

		self.assertEqual(doc.custom_kanban_profile, "Nasr city")

	def test_mark_cancelled_invoice_workflow_fields_sets_cancelled_and_accepted(self):
		"""Every cancellation path should stamp Cancelled state and Accepted acceptance status."""
		from jarz_pos.events.sales_invoice import mark_cancelled_invoice_workflow_fields

		doc = SimpleNamespace(
			name="ACC-SINV-TEST-001",
			custom_sales_invoice_state="Ready",
			sales_invoice_state="Ready",
			custom_acceptance_status="Pending",
			custom_accepted_by=None,
			custom_accepted_on=None,
		)

		def _get_field(fieldname):
			if fieldname in {
				"custom_sales_invoice_state",
				"sales_invoice_state",
				"custom_acceptance_status",
				"custom_accepted_by",
				"custom_accepted_on",
			}:
				return object()
			return None

		meta = MagicMock()
		meta.get_field.side_effect = _get_field

		mock_frappe = MagicMock()
		mock_frappe.get_meta.return_value = meta
		mock_frappe.session.user = "manager@example.com"
		mock_frappe.utils.now_datetime.return_value = "2026-05-05 18:00:00"

		def _set_value(_doctype, _name, values, update_modified=False):
			for fieldname, value in values.items():
				setattr(doc, fieldname, value)

		mock_frappe.db.set_value.side_effect = _set_value

		with patch("jarz_pos.events.sales_invoice.frappe", mock_frappe):
			mark_cancelled_invoice_workflow_fields(doc)

		mock_frappe.db.set_value.assert_called_once()
		self.assertEqual(doc.custom_sales_invoice_state, "Cancelled")
		self.assertEqual(doc.sales_invoice_state, "Cancelled")
		self.assertEqual(doc.custom_acceptance_status, "Accepted")
		self.assertEqual(doc.custom_accepted_by, "manager@example.com")
		self.assertEqual(doc.custom_accepted_on, "2026-05-05 18:00:00")
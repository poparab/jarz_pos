"""Focused tests for Sales Invoice event handlers."""

import unittest
from types import SimpleNamespace


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
"""Access-tier tests for the invoice warehouse alignment endpoints.

Extracted from ``test_api_manager.py`` (which is not part of the backend CI
allowlist) because these two tests guard a permission change that must run in
CI: ``get_invoice_warehouse_alignment_report`` was widened to the
manager-dashboard tier, while ``repair_invoice_warehouse_alignment`` kept its
stricter ``ROLES.ADMIN`` gate. This module is self-contained and does not
import anything from ``test_api_manager``.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _FakeInvoice:
	"""Minimal Sales Invoice stub for manager API tests."""

	def __init__(self, **data):
		self._data = dict(data)
		self.name = self._data["name"]

	def __getattr__(self, key):
		if key in self._data:
			return self._data[key]
		raise AttributeError(key)

	def get(self, key, default=None):
		return self._data.get(key, default)

	def reload(self):
		return None


def _raise_frappe(message, exc=None, title=None):
	if exc and isinstance(exc, type) and issubclass(exc, Exception):
		raise exc(message)
	raise Exception(message)


def _alignment_frappe(profile_warehouse="Stores - Nasr city", warehouse_found="Stores - Nasr city"):
	"""A frappe mock answering the report's two set-based queries.

	INV-006 was taken at Dokki but moved to Nasr city on the Kanban, and its one
	stock row still points at the Dokki store.
	"""
	mock_frappe = MagicMock()
	mock_frappe.logger.return_value = MagicMock()
	mock_frappe.has_permission.return_value = True
	mock_frappe.PermissionError = PermissionError
	mock_frappe.throw.side_effect = _raise_frappe

	candidate = {
		"name": "INV-006",
		"company": "Jarz",
		"customer": "CUST-006",
		"posting_date": "2026-09-01",
		"grand_total": 123.45,
		"operational_profile": "Nasr city",
		"profile_found": "Nasr city",
		"profile_warehouse": profile_warehouse,
		"warehouse_found": warehouse_found,
		"warehouse_company": "Jarz" if warehouse_found else None,
	}
	stock_row = {"parent": "INV-006", "name": "SII-030", "item_code": "ITEM-001", "warehouse": "Stores - Dokki"}

	def _sql(query, params=None, as_dict=False, **kwargs):
		if "FROM `tabSales Invoice` si" in query:
			return [dict(candidate)]
		if "FROM `tabSales Invoice Item` sii" in query:
			return [dict(stock_row)]
		return []

	mock_frappe.db.sql.side_effect = _sql
	return mock_frappe


class TestWarehouseAlignmentAccess(unittest.TestCase):
	"""Tests for the manager/line-manager vs admin access tiers on the
	invoice warehouse alignment report and repair endpoints."""

	def test_get_invoice_warehouse_alignment_report_widened_to_line_manager_tier(self):
		"""Read-only report must be reachable by the manager-dashboard tier, not admins only.

		Regression guard for the mobile watchlist: this endpoint used to require
		``ROLES.ADMIN`` on top of ``_ensure_manager_dashboard_access``, which shut out
		a plain JARZ line manager. Only the read report was widened —
		``repair_invoice_warehouse_alignment`` keeps its stricter admin-only gate
		(see the sibling test below).
		"""
		from jarz_pos.api.manager import get_invoice_warehouse_alignment_report

		mock_frappe = _alignment_frappe()
		# Deliberately NOT an admin role — only the manager/line-manager tier.
		mock_frappe.get_roles.return_value = ["JARZ line manager"]

		# _ensure_manager_dashboard_access runs for REAL here (not patched away) so the
		# widened gate is actually exercised, not just assumed.
		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			result = get_invoice_warehouse_alignment_report(limit=25)

		self.assertTrue(result.get("success"))
		self.assertEqual(result.get("count"), 1)
		row = result["invoices"][0]
		self.assertEqual(row["invoice_id"], "INV-006")
		self.assertEqual(row["operational_profile"], "Nasr city")
		self.assertEqual(row["target_warehouse"], "Stores - Nasr city")
		self.assertEqual(row["actual_warehouses"], ["Stores - Dokki"])
		self.assertEqual(row["posting_date"], "2026-09-01")
		self.assertEqual(row["amount"], 123.45)
		self.assertEqual(
			row["mismatches"],
			[{"row_name": "SII-030", "item_code": "ITEM-001", "warehouse": "Stores - Dokki"}],
		)
		# Set-based: no per-invoice get_doc.
		mock_frappe.get_doc.assert_not_called()

	def test_branch_filter_and_limit_are_applied_in_sql(self):
		"""The branch condition must sit in the SQL, before LIMIT, not in a Python pass after it."""
		from jarz_pos.api.manager import get_invoice_warehouse_alignment_report

		mock_frappe = _alignment_frappe()
		mock_frappe.get_roles.return_value = ["JARZ line manager"]

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			get_invoice_warehouse_alignment_report(branch="Nasr city", limit=999)

		query, params = mock_frappe.db.sql.call_args_list[0][0][:2]
		self.assertIn("%(branch)s", query)
		self.assertIn("NOT EXISTS", query)
		self.assertLess(query.index("%(branch)s"), query.index("LIMIT"))
		self.assertEqual(params["branch"], "Nasr city")
		self.assertEqual(params["limit"], 500)  # clamped

	def test_profile_without_warehouse_is_reported_with_the_old_message(self):
		from jarz_pos.api.manager import get_invoice_warehouse_alignment_report

		mock_frappe = _alignment_frappe(profile_warehouse=None, warehouse_found=None)
		mock_frappe.get_roles.return_value = ["JARZ line manager"]

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			result = get_invoice_warehouse_alignment_report()

		row = result["invoices"][0]
		self.assertIsNone(row["target_warehouse"])
		self.assertEqual(row["issue"], "Target POS Profile Nasr city has no warehouse configured.")

	def test_repair_invoice_warehouse_alignment_still_requires_admin_tier(self):
		"""The bulk repair endpoint must stay stricter than the report: a line manager
		may read the watchlist but must not be able to rewrite warehouses."""
		from jarz_pos.api.manager import repair_invoice_warehouse_alignment

		mock_frappe = MagicMock()
		mock_frappe.logger.return_value = MagicMock()
		mock_frappe.get_roles.return_value = ["JARZ line manager"]
		mock_frappe.has_permission.return_value = True
		mock_frappe.PermissionError = PermissionError
		mock_frappe.throw.side_effect = _raise_frappe

		with patch("jarz_pos.api.manager.frappe", mock_frappe):
			with self.assertRaises(PermissionError):
				repair_invoice_warehouse_alignment(limit=25, apply_changes=1)


if __name__ == "__main__":
	unittest.main()

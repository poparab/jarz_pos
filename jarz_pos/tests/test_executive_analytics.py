"""Executive overview: branch scope (Feature: reports-overhaul review fixes).

* An unknown branch is refused up front, instead of every section swallowing it
  and the overview rendering as an all-zero branch.
* With a branch, B2B / B2C revenue come from the branch's P&L ``branches`` row;
  the P&L ``summary`` is company-wide.

Every loader is mocked, so no site data is needed.
"""

import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import executive_analytics as ea
from jarz_pos.api import financial_report as fr


def _pnl():
	return {
		"summary": {
			"sales": 10000.0,
			"gross_profit": 4000.0,
			"orders": 40,
			"net_profit": 1500.0,
			"net_margin_pct": 14.0,
			"b2b_revenue": 3000.0,
			"b2c_revenue": 7200.0,
		},
		"shipping": {"expense": 900.0, "income": 700.0, "net": -200.0},
		"data_quality": {"warnings": []},
		"branches": [
			{
				"branch": "Dokki",
				"orders": 10,
				"revenue": 2500.0,
				"cogs": 1000.0,
				"gross_profit": 1500.0,
				"shipping_income": 120.0,
				"b2b_revenue": 500.0,
				"b2c_revenue": 2100.0,
			},
		],
	}


class TestExecutiveBranchScope(unittest.TestCase):
	def _run(self, branch, pnl=None):
		pnl_mock = MagicMock(return_value=pnl if pnl is not None else _pnl())
		with ExitStack() as stack:
			stack.enter_context(patch.object(ea, "_ensure_jarz_manager"))
			stack.enter_context(patch.object(ea, "frappe", MagicMock()))
			stack.enter_context(
				patch.object(fr, "validate_branch", side_effect=lambda b: (b or "").strip() or None)
			)
			stack.enter_context(patch.object(fr, "get_profit_and_loss", pnl_mock))
			stack.enter_context(
				patch("jarz_pos.api.product_analytics.get_product_analytics", return_value={})
			)
			stack.enter_context(patch("jarz_pos.api.shipping_analytics.get_summary_kpis", return_value={}))
			stack.enter_context(patch("jarz_pos.api.shipping_analytics.get_alerts_data", return_value=[]))
			stack.enter_context(
				patch("jarz_pos.services.rfm_segmentation.get_segment_summary", return_value=[])
			)
			stack.enter_context(
				patch("jarz_pos.api.inventory_analytics.get_restock", return_value={"restock": []})
			)
			stack.enter_context(
				patch(
					"jarz_pos.services.demand_forecasting.build_alert_data",
					return_value={"critical": [], "watch_list": []},
				)
			)
			stack.enter_context(patch("jarz_pos.services.demand_forecasting.get_settings", return_value={}))
			out = ea.get_executive_overview(date_from="2026-09-01", date_to="2026-09-30", branch=branch)
		return out, pnl_mock

	def test_company_view_reads_the_summary(self):
		out, _ = self._run(None)
		self.assertEqual(out["kpis"]["b2b_revenue"], 3000.0)
		self.assertEqual(out["kpis"]["b2c_revenue"], 7200.0)

	def test_branch_view_reads_its_branch_row_not_the_company_summary(self):
		out, pnl_mock = self._run("Dokki")
		self.assertEqual(pnl_mock.call_args.kwargs["branch"], "Dokki")
		k = out["kpis"]
		self.assertEqual(k["b2b_revenue"], 500.0)
		self.assertEqual(k["b2c_revenue"], 2100.0)
		self.assertEqual(k["total_revenue"], 2500.0)
		self.assertEqual(k["total_orders"], 10)

	def test_branch_without_a_row_has_no_channel_revenue(self):
		pnl = _pnl()
		pnl["branches"] = []
		out, _ = self._run("Dokki", pnl=pnl)
		self.assertEqual(out["kpis"]["b2b_revenue"], 0.0)
		self.assertEqual(out["kpis"]["b2c_revenue"], 0.0)


class TestExecutiveRefusesUnknownBranch(unittest.TestCase):
	def test_unknown_branch_raises_before_any_section_runs(self):
		fake = MagicMock()
		fake.ValidationError = frappe.ValidationError

		def _throw(msg, exc=frappe.ValidationError, *a, **kw):
			raise exc(msg)

		fake.throw.side_effect = _throw
		fake.db.exists.return_value = False
		with (
			patch.object(ea, "_ensure_jarz_manager"),
			patch.multiple(fr, frappe=fake, _=lambda s, *a, **k: s),
			patch("jarz_pos.api.product_analytics.get_product_analytics") as product,
		):
			with self.assertRaises(frappe.ValidationError):
				ea.get_executive_overview(date_from="2026-09-01", date_to="2026-09-30", branch="Nowhere")
			with self.assertRaises(frappe.ValidationError):
				ea.get_executive_overview(branch=fr.NO_BRANCH_LABEL)
		product.assert_not_called()


if __name__ == "__main__":
	unittest.main()

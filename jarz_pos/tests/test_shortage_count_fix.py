"""Unit tests for the Production Board's shortage → miscount correction.

Same pattern as ``test_api_production``: pure ``unittest``, every resolver
patched, nothing reaches a database.
"""

import unittest
from datetime import datetime
from unittest.mock import patch

LINES = [{"item_code": "Strawberry Large", "bom_name": "BOM-SL-001", "item_qty": 34}]

ROLLUP = {
	"shortages": [
		{
			"item_code": "Label 330",
			"item_name": "Label 330",
			"uom": "Nos",
			"source_warehouse": "Raw Material - J",
			"required_qty": 34.0,
			"available_qty": 11.0,
			"missing_qty": 23.0,
			"reason": "insufficient_stock",
		},
		{
			"item_code": "strawberry mix",
			"item_name": "Strawberry mix",
			"uom": "Kg",
			"source_warehouse": "Raw Material - J",
			"required_qty": 1.36,
			"available_qty": 1.0,
			"missing_qty": 0.3604,
			"reason": "insufficient_stock",
		},
		{
			"item_code": "orphan",
			"source_warehouse": None,
			"missing_qty": 2.0,
			"reason": "missing_source_warehouse",
		},
	]
}


def _raise(msg, exc=None, *args, **kwargs):
	import frappe

	raise (exc or frappe.ValidationError)(msg)


class TestShortageCountFix(unittest.TestCase):
	def _patches(self, balances=None, roles=("Manufacturing Manager",), later_counts=None):
		balances = balances if balances is not None else {"Label 330": 11.0, "strawberry mix": 1.0}
		return [
			patch("jarz_pos.api.production._later_stock_counts", return_value=list(later_counts or [])),
			patch("jarz_pos.api.production._", new=lambda msg: msg),
			patch("jarz_pos.api.production.planning._resolve_default_company", return_value="Jarz Co"),
			patch("jarz_pos.api.production.planning.build_basket_rollup", return_value=ROLLUP),
			patch(
				"jarz_pos.api.manufacturing._resolve_now_datetime",
				return_value=datetime(2026, 11, 21, 15, 0, 0),
			),
			patch("jarz_pos.api.inventory_count._get_qty_as_of_map", return_value=balances),
			patch("jarz_pos.api.production.frappe.get_roles", return_value=list(roles)),
			patch("jarz_pos.api.production.frappe.throw", side_effect=_raise),
		]

	def _run(self, fn, *args, balances=None, roles=("Manufacturing Manager",), later_counts=None, **kwargs):
		patches = self._patches(balances, roles, later_counts)
		for p in patches:
			p.start()
		try:
			return fn(*args, **kwargs)
		finally:
			for p in reversed(patches):
				p.stop()

	def test_preview_counts_five_minutes_before_production_and_adds_the_shortfall(self):
		from jarz_pos.api import production

		plan = self._run(production.preview_shortage_count_fix, LINES, scheduled_at="2026-11-20 18:00:00")
		self.assertEqual(plan["production_at"], "2026-11-20 18:00:00")
		self.assertEqual(plan["count_at"], "2026-11-20 17:55:00")
		by_code = {r["item_code"]: r for r in plan["items"]}
		self.assertEqual(by_code["Label 330"]["counted_qty"], 34.0)
		# Rounded UP to 3 decimals so the batch is never short by a float hair.
		self.assertAlmostEqual(by_code["strawberry mix"]["missing_qty"], 0.361)
		self.assertAlmostEqual(by_code["strawberry mix"]["counted_qty"], 1.361)
		self.assertEqual([s["item_code"] for s in plan["skipped"]], ["orphan"])

	def test_undated_basket_counts_five_minutes_before_server_now(self):
		from jarz_pos.api import production

		plan = self._run(production.preview_shortage_count_fix, LINES)
		self.assertEqual(plan["count_at"], "2026-11-21 14:55:00")

	def test_a_batch_later_today_is_counted_now_not_in_the_future(self):
		from jarz_pos.api import production

		plan = self._run(production.preview_shortage_count_fix, LINES, scheduled_at="2026-11-21 18:00:00")
		self.assertEqual(plan["count_at"], "2026-11-21 15:00:00")

	def test_shortfall_is_added_to_the_balance_at_the_count_moment(self):
		from jarz_pos.api import production

		plan = self._run(
			production.preview_shortage_count_fix,
			LINES,
			balances={"Label 330": 15.0, "strawberry mix": 1.0},
		)
		by_code = {r["item_code"]: r for r in plan["items"]}
		self.assertEqual(by_code["Label 330"]["current_qty"], 15.0)
		self.assertEqual(by_code["Label 330"]["counted_qty"], 38.0)

	def test_operator_without_backdate_role_is_refused(self):
		import frappe

		from jarz_pos.api import production

		with self.assertRaises(frappe.PermissionError):
			self._run(production.preview_shortage_count_fix, LINES, roles=("Production Operator",))

	def test_post_requires_the_miscount_confirmation(self):
		import frappe

		from jarz_pos.api import production

		with patch("jarz_pos.api.inventory_count._reconcile_counts") as reco:
			with self.assertRaises(frappe.ValidationError):
				self._run(production.fix_shortage_by_count, LINES, confirm_miscount=0)
			reco.assert_not_called()

	def test_post_reconciles_each_warehouse_with_remarks(self):
		from jarz_pos.api import production

		with (
			patch(
				"jarz_pos.api.inventory_count._reconcile_counts",
				return_value={"ok": True, "stock_reconciliation": "MAT-RECO-1", "differences": 2},
			) as reco,
			# Wholesale: ``frappe.db`` / ``frappe.session`` are request-bound
			# proxies that cannot even be read to be patched outside a site.
			patch("jarz_pos.api.production.frappe") as mock_frappe,
		):
			mock_frappe.session.user = "owner@example.com"
			mock_frappe.db.get_value.return_value = "Owner"
			db = mock_frappe.db
			result = self._run(
				production.fix_shortage_by_count,
				LINES,
				scheduled_at="2026-11-20 18:00:00",
				confirm_miscount=1,
			)
		self.assertEqual(result["stock_reconciliations"], ["MAT-RECO-1"])
		reco.assert_called_once()
		args, kwargs = reco.call_args
		self.assertEqual(args[0], "Raw Material - J")
		self.assertEqual(args[1], "2026-11-20 17:55:00")
		self.assertEqual({ln["item_code"] for ln in args[2]}, {"Label 330", "strawberry mix"})
		self.assertTrue(kwargs["qty_as_of_posting"])
		self.assertIn("counted wrong", kwargs["remarks"])
		db.commit.assert_called_once()

	def test_count_can_be_dated_earlier_than_production(self):
		from jarz_pos.api import production

		plan = self._run(
			production.preview_shortage_count_fix,
			LINES,
			scheduled_at="2026-11-20 18:00:00",
			count_at="2026-11-18 09:30:00",
		)
		self.assertEqual(plan["count_at"], "2026-11-18 09:30:00")
		self.assertEqual(plan["latest_count_at"], "2026-11-20 17:55:00")
		self.assertEqual(plan["later_counts"], [])

	def test_count_dated_after_the_default_moment_is_refused(self):
		import frappe

		from jarz_pos.api import production

		with self.assertRaises(frappe.ValidationError):
			self._run(
				production.preview_shortage_count_fix,
				LINES,
				scheduled_at="2026-11-20 18:00:00",
				count_at="2026-11-20 17:59:00",
			)

	def test_post_refuses_a_date_before_a_later_stock_count(self):
		import frappe

		from jarz_pos.api import production

		later = [
			{
				"item_code": "Label 330",
				"warehouse": "Raw Material - J",
				"stock_reconciliation": "MAT-RECO-9",
				"posted_at": "2026-11-19 10:00:00",
			}
		]
		with patch("jarz_pos.api.inventory_count._reconcile_counts") as reco:
			with self.assertRaises(frappe.ValidationError):
				self._run(
					production.fix_shortage_by_count,
					LINES,
					scheduled_at="2026-11-20 18:00:00",
					count_at="2026-11-18 09:30:00",
					confirm_miscount=1,
					later_counts=later,
				)
			reco.assert_not_called()

	def test_post_uses_the_chosen_count_date(self):
		from jarz_pos.api import production

		with (
			patch(
				"jarz_pos.api.inventory_count._reconcile_counts",
				return_value={"ok": True, "stock_reconciliation": "MAT-RECO-2", "differences": 2},
			) as reco,
			patch("jarz_pos.api.production.frappe") as mock_frappe,
		):
			mock_frappe.session.user = "owner@example.com"
			mock_frappe.db.get_value.return_value = "Owner"
			self._run(
				production.fix_shortage_by_count,
				LINES,
				scheduled_at="2026-11-20 18:00:00",
				count_at="2026-11-18 09:30:00",
				confirm_miscount=1,
			)
		args, kwargs = reco.call_args
		self.assertEqual(args[1], "2026-11-18 09:30:00")
		self.assertIn("count dated 2026-11-18 09:30:00", kwargs["remarks"])


if __name__ == "__main__":
	unittest.main()

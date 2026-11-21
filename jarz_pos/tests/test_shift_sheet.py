"""Tests for api.shift.get_shift_sheet (the digital shift sales sheet).

Pins down: who may read it (opener, or a shift-monitor manager of that branch),
that the staff view hides totals (amounts_hidden) while still listing per-order
amounts, that the manager view sums totals, and that jar counting skips non-jar
and bundle header rows and groups flavours across sizes.

The DB readers are small ``_resolve_sheet_*`` helpers, patched here. Needs a
connected site (``frappe.init`` + ``frappe.connect``) because ``frappe.throw``
and ``_()`` are the real ones.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jarz_pos.api import shift

OPENING = {
	"name": "POS-OPEN-1",
	"user": "opener@example.com",
	"pos_profile": "Branch A",
	"period_start_date": "2026-11-20 10:00:00",
	"company": "Jarz",
}
CLOSING = {"name": "POS-CLOSE-1", "period_end_date": "2026-11-20 22:00:00"}


def _inv(name, total, **extra):
	row = {
		"name": name,
		"creation": "2026-11-20 11:00:00",
		"grand_total": total,
		"outstanding_amount": 0,
		"territory": "Maadi",
		"woo_order_id": 0,
		"custom_courier_party_type": None,
		"custom_courier_party": None,
		"custom_sales_invoice_state": "Delivered",
		"custom_payment_method": "Cash",
		"custom_is_pickup": 0,
	}
	row.update(extra)
	return row


JAR_ROWS = [
	{"parent": "SI-1", "item_code": "J-MAN-S", "item_name": "Mango Small", "item_group": "Small", "qty": 2},
	{"parent": "SI-1", "item_code": "J-MAN-M", "item_name": "Mango Medium", "item_group": "Medium", "qty": 1},
	{"parent": "SI-1", "item_code": "BOX", "item_name": "Gift Box", "item_group": "Packaging", "qty": 5},
	{"parent": "SI-2", "item_code": "BND-1", "item_name": "Mango Small Bundle", "item_group": "Small", "qty": 1},
	{"parent": "SI-2", "item_code": "J-MAN-S", "item_name": "Mango Small", "item_group": "Small", "qty": 3},
	{"parent": "SI-2", "item_code": "J-ORE-L", "item_name": "Oreo Large", "item_group": "Large", "qty": 1},
	{"parent": "SI-2", "item_code": "J-ORE-M", "item_name": "Oreo Meduim", "item_group": "Meduim", "qty": 2},
]


class TestShiftSheet(unittest.TestCase):
	def _run(self, user, *, manager=False, profiles=(), closing=CLOSING, opening=OPENING):
		patches = [
			patch.object(frappe, "session", SimpleNamespace(user=user)),
			patch.object(shift, "_resolve_sheet_opening", return_value=dict(opening)),
			patch.object(shift, "_resolve_sheet_closing", return_value=closing),
			patch.object(shift, "_sheet_caller_access", return_value=(manager, list(profiles))),
			patch.object(
				shift,
				"_resolve_sheet_invoices",
				return_value=[
					_inv("SI-1", 300, custom_courier_party_type="Employee", custom_courier_party="EMP-1"),
					_inv("SI-2", 500, custom_is_pickup=1, custom_courier_party="EMP-1", woo_order_id=77),
				],
			),
			patch.object(shift, "_resolve_sheet_jar_rows", return_value=JAR_ROWS),
			patch.object(shift, "_resolve_sheet_bundle_codes", return_value={"BND-1"}),
			patch.object(shift, "_resolve_sheet_shipping", return_value={"SI-1": 30.0}),
			patch.object(shift, "_resolve_sheet_courier_labels", return_value={("Employee", "EMP-1"): "Ali"}),
			patch.object(shift, "_resolve_sheet_payment_methods", return_value={"SI-2": "Instapay"}),
			patch.object(shift, "_resolve_sheet_area_labels", return_value={"Maadi": "المعادي"}),
			patch.object(
				shift,
				"_resolve_sheet_payouts",
				return_value=[
					{"kind": "expense", "name": "EXP-1", "label": "Ice", "amount": 40.0, "status": "Paid", "at": "x"},
					{"kind": "advance", "name": "EA-1", "label": "Ali", "amount": 60.0, "status": "Paid", "at": "y"},
				],
			),
			patch.object(
				shift,
				"_resolve_sheet_drawer",
				return_value={
					"opening_amount": 100.0,
					"expected_closing": 700.0,
					"actual_closing": 690.0,
					"difference": -10.0,
				},
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return shift.get_shift_sheet("POS-OPEN-1")

	def test_opener_ok_staff_view_hides_totals(self):
		sheet = self._run("opener@example.com")
		self.assertIsNone(sheet["totals"])
		self.assertEqual(sheet["amounts_hidden"], 1)
		self.assertFalse(sheet["is_manager_view"])
		self.assertEqual([o["grand_total"] for o in sheet["orders"]], [300.0, 500.0])
		self.assertFalse(sheet["is_open"])
		self.assertEqual(sheet["closing_entry"], "POS-CLOSE-1")

	def test_stranger_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run("stranger@example.com")

	def test_manager_outside_branch_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self._run("mgr@example.com", manager=True, profiles=["Branch B"])

	def test_manager_in_branch_gets_totals(self):
		sheet = self._run("mgr@example.com", manager=True, profiles=["Branch A"])
		self.assertEqual(sheet["amounts_hidden"], 0)
		self.assertTrue(sheet["is_manager_view"])
		totals = sheet["totals"]
		self.assertEqual(totals["orders"], 2)
		self.assertEqual(totals["jars"], 9)
		self.assertEqual(totals["grand_total"], 800.0)
		self.assertEqual(totals["delivery_fees"], 30.0)
		self.assertEqual(totals["payouts"], 100.0)
		self.assertEqual(totals["opening_amount"], 100.0)
		self.assertEqual(totals["expected_closing"], 700.0)
		self.assertEqual(totals["actual_closing"], 690.0)
		self.assertEqual(totals["difference"], -10.0)

	def test_opener_who_is_also_manager_sees_manager_view(self):
		sheet = self._run("opener@example.com", manager=True, profiles=[])
		self.assertTrue(sheet["is_manager_view"])
		self.assertIsNotNone(sheet["totals"])

	def test_open_shift_has_no_closing(self):
		sheet = self._run("opener@example.com", closing=None)
		self.assertTrue(sheet["is_open"])
		self.assertIsNone(sheet["closing_entry"])
		self.assertIsNone(sheet["closed_at"])

	def test_order_rows(self):
		orders = {o["name"]: o for o in self._run("opener@example.com")["orders"]}
		self.assertEqual(orders["SI-1"]["courier"], "Ali")
		self.assertEqual(orders["SI-1"]["delivery_fee"], 30.0)
		self.assertEqual(orders["SI-1"]["order_number"], "SI-1")
		self.assertEqual(orders["SI-1"]["area"], "المعادي")
		self.assertEqual(orders["SI-1"]["payment_method"], "Cash")
		self.assertTrue(orders["SI-2"]["is_pickup"])
		self.assertIsNone(orders["SI-2"]["courier"])
		self.assertEqual(orders["SI-2"]["order_number"], "77")
		self.assertEqual(orders["SI-2"]["payment_method"], "Instapay")

	def test_jars_exclude_non_jar_and_bundle_header_rows(self):
		orders = {o["name"]: o for o in self._run("opener@example.com")["orders"]}
		self.assertEqual(orders["SI-1"]["jars"], 3)  # 2 small + 1 medium, box ignored
		self.assertEqual(orders["SI-2"]["jars"], 6)  # header skipped: 3 + 1 + 2

	def test_flavour_matrix_grouping(self):
		matrix = self._run("opener@example.com")["jars_by_flavour"]
		self.assertEqual(
			matrix,
			[
				{"flavour": "Mango", "small": 5, "medium": 1, "large": 0},
				{"flavour": "Oreo", "small": 0, "medium": 2, "large": 1},
			],
		)

	def test_aggregate_helper_directly(self):
		per, matrix = shift._aggregate_sheet_jars(JAR_ROWS, {"BND-1"})
		self.assertEqual(per, {"SI-1": 3, "SI-2": 6})
		self.assertEqual([m["flavour"] for m in matrix], ["Mango", "Oreo"])


if __name__ == "__main__":
	unittest.main()

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
	{
		"parent": "SI-2",
		"item_code": "BND-1",
		"item_name": "Mango Small Bundle",
		"item_group": "Small",
		"qty": 1,
	},
	{"parent": "SI-2", "item_code": "J-MAN-S", "item_name": "Mango Small", "item_group": "Small", "qty": 3},
	{"parent": "SI-2", "item_code": "J-ORE-L", "item_name": "Oreo Large", "item_group": "Large", "qty": 1},
	{"parent": "SI-2", "item_code": "J-ORE-M", "item_name": "Oreo Meduim", "item_group": "Meduim", "qty": 2},
]


class TestShiftSheet(unittest.TestCase):
	def _run(
		self,
		user,
		*,
		manager=False,
		profiles=(),
		closing=CLOSING,
		opening=OPENING,
		invoices=None,
		dispatches=None,
		settlements=None,
		returns=None,
		drawer_paid=None,
		overdue=None,
		out_rows=None,
	):
		if dispatches is None:
			# Both default orders went out in the shift, so both are listed.
			dispatches = {"SI-1": "2026-11-20 11:30:00", "SI-2": "2026-11-20 11:45:00"}
		if out_rows is None:
			# Both default orders went out in the shift: their Delivery Notes
			# carry the same lines as the invoices.
			out_rows = JAR_ROWS
		if invoices is None:
			invoices = [
				_inv("SI-1", 300, custom_courier_party_type="Employee", custom_courier_party="EMP-1"),
				_inv("SI-2", 500, custom_is_pickup=1, custom_courier_party="EMP-1", woo_order_id=77),
			]
		patches = [
			patch.object(shift, "_resolve_sheet_dispatches", return_value=dispatches),
			patch.object(shift, "_resolve_sheet_drawer_payments", return_value=drawer_paid or {}),
			patch.object(shift, "_resolve_sheet_overdue", return_value=overdue or []),
			patch.object(shift, "_resolve_sheet_settlements", return_value=settlements or {}),
			patch.object(shift, "_resolve_sheet_returns", return_value=returns or []),
			patch.object(
				shift, "_resolve_sheet_cash_flow", return_value={"opening": 100.0, "expected": 700.0}
			),
			patch.object(frappe, "session", SimpleNamespace(user=user)),
			patch.object(shift, "_resolve_sheet_opening", return_value=dict(opening)),
			patch.object(shift, "_resolve_sheet_closing", return_value=closing),
			patch.object(shift, "_sheet_caller_access", return_value=(manager, list(profiles))),
			patch.object(
				shift,
				"_resolve_sheet_invoices",
				return_value=invoices,
			),
			patch.object(shift, "_resolve_sheet_jar_rows", return_value=JAR_ROWS),
			patch.object(shift, "_resolve_sheet_dispatched_jar_rows", return_value=out_rows),
			patch.object(shift, "_resolve_sheet_bundle_codes", return_value={"BND-1"}),
			patch.object(shift, "_resolve_sheet_shipping", return_value={"SI-1": 30.0}),
			patch.object(shift, "_resolve_sheet_courier_labels", return_value={("Employee", "EMP-1"): "Ali"}),
			patch.object(shift, "_resolve_sheet_payment_methods", return_value={"SI-2": "Instapay"}),
			patch.object(shift, "_resolve_sheet_area_labels", return_value={"Maadi": "المعادي"}),
			patch.object(
				shift,
				"_resolve_sheet_payouts",
				return_value=[
					{
						"kind": "expense",
						"name": "EXP-1",
						"label": "Ice",
						"amount": 40.0,
						"status": "Paid",
						"at": "x",
					},
					{
						"kind": "advance",
						"name": "EA-1",
						"label": "Ali",
						"amount": 60.0,
						"status": "Paid",
						"at": "y",
					},
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

	def test_default_rows_and_returns_is_a_list(self):
		sheet = self._run("opener@example.com")
		self.assertEqual(
			[o["reasons"] for o in sheet["orders"]],
			[["created", "out_for_delivery"], ["created", "out_for_delivery"]],
		)
		self.assertIsNone(sheet["orders"][0]["settled_at"])
		self.assertEqual(sheet["orders"][0]["settled_amount"], 0.0)
		self.assertIsNone(sheet["orders"][0]["paid_at"])
		self.assertEqual(sheet["orders"][0]["paid_amount"], 0.0)
		self.assertEqual(sheet["returns"], [])
		self.assertEqual(sheet["overdue"], [])

	def test_created_only_orders_are_not_listed_but_still_count_as_sales(self):
		# Both orders were taken in this shift but go out tomorrow: nothing
		# touched the drawer, so the list is empty, yet they are today's sales.
		sheet = self._run(
			"mgr@example.com", manager=True, profiles=["Branch A"], dispatches={}, out_rows=[]
		)
		self.assertEqual(sheet["orders"], [])
		totals = sheet["totals"]
		self.assertEqual(totals["orders"], 2)
		self.assertEqual(totals["jars"], 9)
		self.assertEqual(totals["grand_total"], 800.0)
		self.assertEqual(totals["listed_orders"], 0)
		# Nothing left the branch, so the stock table is empty.
		self.assertEqual(sheet["jars_by_flavour"], [])
		self.assertEqual(totals["jars_out"], 0)

	def test_flavour_matrix_is_what_left_the_branch(self):
		# Only an order created in an earlier shift went out in this one: its jars
		# are in the stock table, the two orders created here (not yet out) are not,
		# though they still count as this shift's sales.
		out_rows = [
			{"parent": "SI-OLD", "item_code": "J-ORE-M", "item_name": "Oreo Medium", "item_group": "Medium", "qty": 4},
			{"parent": "SI-OLD", "item_code": "BND-1", "item_name": "Mango Small Bundle", "item_group": "Small", "qty": 1},
		]
		sheet = self._run(
			"mgr@example.com", manager=True, profiles=["Branch A"], dispatches={}, out_rows=out_rows
		)
		self.assertEqual(sheet["jars_by_flavour"], [{"flavour": "Oreo", "small": 0, "medium": 4, "large": 0}])
		self.assertEqual(sheet["totals"]["jars_out"], 4)
		self.assertEqual(sheet["totals"]["jars"], 9)

	def test_cash_paid_into_drawer_lists_the_order(self):
		sheet = self._run(
			"mgr@example.com",
			manager=True,
			profiles=["Branch A"],
			dispatches={},
			drawer_paid={"SI-2": {"at": "2026-11-20 11:05:00", "amount": 500.0}},
		)
		self.assertEqual([o["name"] for o in sheet["orders"]], ["SI-2"])
		row = sheet["orders"][0]
		self.assertEqual(row["reasons"], ["created", "paid"])
		self.assertTrue(row["paid_at"].startswith("2026-11-20T11:05"))
		self.assertEqual(row["paid_amount"], 500.0)
		self.assertEqual(sheet["totals"]["paid_orders"], 1)
		self.assertEqual(sheet["totals"]["paid_amount"], 500.0)

	def test_overdue_is_passed_through_and_counted(self):
		item = {"name": "SI-LATE", "order_number": "123", "minutes_late": 90, "due_in_this_shift": True}
		sheet = self._run("mgr@example.com", manager=True, profiles=["Branch A"], overdue=[item])
		self.assertEqual(sheet["overdue"], [item])
		self.assertEqual(sheet["totals"]["overdue_orders"], 1)
		# Staff see the late list too (no totals).
		staff = self._run("opener@example.com", overdue=[item])
		self.assertEqual(staff["overdue"], [item])

	def _multi_reason_sheet(self):
		invoices = [
			_inv("SI-1", 300),
			# created in an earlier shift, dispatched and settled in this one
			_inv("SI-OLD", 200, creation="2026-11-19 20:00:00"),
			# created in an earlier shift, only dispatched here
			_inv("SI-OFD", 100, creation="2026-11-19 21:00:00"),
		]
		returns = [
			{
				"credit_note": "CN-1",
				"order": "SI-1",
				"order_number": "SI-1",
				"amount": 50.0,
				"refund_amount": 50.0,
				"refund_account": "Cash - A",
				"refund_kind": "cash",
				"at": "x",
			},
			{
				"credit_note": "CN-2",
				"order": "SI-9",
				"order_number": "9",
				"amount": 80.0,
				"refund_amount": 80.0,
				"refund_account": "Bank Account - J",
				"refund_kind": "bank",
				"at": "y",
			},
			{
				"credit_note": "CN-3",
				"order": "SI-8",
				"order_number": "8",
				"amount": 20.0,
				"refund_amount": 0.0,
				"refund_account": None,
				"refund_kind": "credit",
				"at": "z",
			},
		]
		return self._run(
			"mgr@example.com",
			manager=True,
			profiles=["Branch A"],
			invoices=invoices,
			dispatches={
				"SI-OLD": "2026-11-20 12:00:00",
				"SI-OFD": "2026-11-20 10:30:00",
				"SI-1": "2026-11-20 11:30:00",
			},
			settlements={"SI-OLD": {"at": "2026-11-20 13:00:00", "amount": 170.0}},
			returns=returns,
		)

	def test_reasons_order_and_sorting(self):
		sheet = self._multi_reason_sheet()
		by_name = {o["name"]: o for o in sheet["orders"]}
		self.assertEqual(by_name["SI-1"]["reasons"], ["created", "out_for_delivery"])
		self.assertEqual(by_name["SI-OLD"]["reasons"], ["out_for_delivery", "settled"])
		self.assertEqual(by_name["SI-OLD"]["settled_amount"], 170.0)
		self.assertTrue(by_name["SI-OLD"]["settled_at"].startswith("2026-11-20T13:00"))
		self.assertTrue(by_name["SI-OLD"]["out_for_delivery_at"].startswith("2026-11-20T12:00"))
		# earliest in-window moment: SI-OFD 10:30, SI-1 11:00 (created), SI-OLD 12:00
		self.assertEqual([o["name"] for o in sheet["orders"]], ["SI-OFD", "SI-1", "SI-OLD"])

	def test_totals_count_created_orders_only(self):
		totals = self._multi_reason_sheet()["totals"]
		self.assertEqual(totals["orders"], 1)
		self.assertEqual(totals["new_orders"], 1)
		self.assertEqual(totals["grand_total"], 300.0)
		self.assertEqual(totals["jars"], 3)  # SI-1 only; the other two have no jar rows
		self.assertEqual(totals["out_for_delivery_orders"], 3)
		self.assertEqual(totals["settled_orders"], 1)
		self.assertEqual(totals["settled_amount"], 170.0)
		self.assertEqual(totals["returns"], 3)
		self.assertEqual(totals["returns_amount"], 150.0)
		self.assertEqual(totals["cash_refunds"], 50.0)
		self.assertEqual(totals["bank_refunds"], 80.0)

	def test_staff_view_still_has_no_totals_but_has_returns(self):
		sheet = self._run("opener@example.com", returns=[{"credit_note": "CN-1"}])
		self.assertIsNone(sheet["totals"])
		self.assertEqual(len(sheet["returns"]), 1)


if __name__ == "__main__":
	unittest.main()


class TestShiftSheetDrawerRules(TestShiftSheet):
	"""Blind count on the server, and sales stay with the order's own branch."""

	def test_staff_response_carries_no_drawer_money(self):
		sheet = self._run(
			"opener@example.com",
			settlements={"SI-1": {"at": "2026-11-20 15:00:00", "amount": 300.0, "fee": 65.0}},
			drawer_paid={"SI-2": {"at": "2026-11-20 11:05:00", "amount": 500.0}},
		)
		for o in sheet["orders"]:
			self.assertEqual(o["paid_amount"], 0.0)
			self.assertEqual(o["courier_cash_in"], 0.0)
			self.assertEqual(o["courier_fee_paid"], 0.0)
			self.assertEqual(o["settled_amount"], 0.0)
		# The times stay: staff can see that it was paid, not how much.
		by_name = {o["name"]: o for o in sheet["orders"]}
		self.assertIn("paid", by_name["SI-2"]["reasons"])
		self.assertIsNotNone(by_name["SI-2"]["paid_at"])

	def test_other_branch_order_is_listed_but_not_a_sale_here(self):
		invoices = [
			_inv("SI-1", 300),
			# Taken at Branch A, moved to Branch B's board, paid into A's till.
			_inv("SI-MOVED", 400, custom_kanban_profile="Branch B", pos_profile="Branch A"),
		]
		sheet = self._run(
			"mgr@example.com",
			manager=True,
			profiles=["Branch A"],
			invoices=invoices,
			dispatches={"SI-1": "2026-11-20 11:30:00"},
			drawer_paid={"SI-MOVED": {"at": "2026-11-20 11:05:00", "amount": 400.0}},
		)
		by_name = {o["name"]: o for o in sheet["orders"]}
		self.assertEqual(by_name["SI-MOVED"]["reasons"], ["paid"])
		self.assertEqual(sheet["totals"]["orders"], 1)
		self.assertEqual(sheet["totals"]["grand_total"], 300.0)


class TestShiftSheetOverdue(unittest.TestCase):
	"""The real ``_resolve_sheet_overdue`` rules, with the DB reads faked."""

	START = "2026-11-20 10:00:00"
	END = "2026-11-20 22:00:00"

	def _row(self, name, delivery_date, time_from, duration=5400, **extra):
		row = {
			"name": name,
			"creation": "2026-11-19 12:00:00",
			"customer": "C-1",
			"customer_name": "Mona",
			"territory": None,
			"grand_total": 300,
			"outstanding_amount": 0,
			"woo_order_id": 0,
			"woo_order_number": name.replace("SI-", ""),
			"state": "Recieved",
			"payment_method": "Cash",
			"is_pickup": 0,
			"sub_territory": None,
			"delivery_date": delivery_date,
			"time_from": time_from,
			"duration": duration,
			"slot_label": None,
			"delivered_at": None,
		}
		row.update(extra)
		return row

	def _resolve(self, rows, logs=None, dns=None, is_open=False, end=None):
		def fake_sql(query, params=None, as_dict=False):
			if "FROM `tabSales Invoice` si" in query:
				return rows
			if "FROM `tabInvoice State Log`" in query:
				return [{"name": k, "at": v} for k, v in (logs or {}).items() if k in params["names"]]
			if "FROM `tabDelivery Note Item`" in query:
				return [{"name": k, "at": v} for k, v in (dns or {}).items() if k in params["names"]]
			raise AssertionError(query)

		patches = [
			patch.object(shift, "_sheet_has_column", return_value=True),
			patch.object(shift, "_resolve_sheet_area_labels", return_value={}),
			patch.object(frappe.db, "sql", side_effect=fake_sql),
			patch.object(frappe.db, "table_exists", return_value=True),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return shift._resolve_sheet_overdue("Branch A", self.START, end or self.END, is_open=is_open)

	def test_rules(self):
		rows = [
			# slot 14:00-15:30 today, never went out -> late, due this shift
			self._row("SI-LATE", "2026-11-20", "14:00:00"),
			# slot passed, but went out at 15:00 (in time for the shift) -> not listed
			self._row("SI-OUT", "2026-11-20", "14:00:00"),
			# slot yesterday, went out only after this shift ended -> listed, earlier shift
			self._row("SI-CARRIED", "2026-11-19", "16:00:00"),
			# rescheduled to 23:00 tonight: slot not passed yet -> not listed
			self._row("SI-RESCHED", "2026-11-20", "23:00:00"),
			# slot with no duration ends at its start
			self._row("SI-NODUR", "2026-11-20", "20:00:00", duration=0),
			# older than the 14-day lookback -> not listed
			self._row("SI-ANCIENT", "2026-11-01", "14:00:00"),
			# only a Delivery Note, before the end -> went out
			self._row("SI-DN", "2026-11-20", "12:00:00"),
		]
		late = self._resolve(
			rows,
			logs={"SI-OUT": "2026-11-20 15:00:00", "SI-CARRIED": "2026-11-21 13:00:00"},
			dns={"SI-DN": "2026-11-20 12:30:00"},
		)
		by_name = {o["name"]: o for o in late}
		self.assertEqual(set(by_name), {"SI-CARRIED", "SI-LATE", "SI-NODUR"})
		self.assertEqual([o["name"] for o in late], ["SI-CARRIED", "SI-LATE", "SI-NODUR"])
		self.assertTrue(by_name["SI-LATE"]["due_in_this_shift"])
		self.assertEqual(by_name["SI-LATE"]["minutes_late"], 390)  # 15:30 -> 22:00
		self.assertFalse(by_name["SI-CARRIED"]["due_in_this_shift"])
		self.assertTrue(by_name["SI-CARRIED"]["went_out_at"].startswith("2026-11-21T13:00"))
		self.assertTrue(by_name["SI-NODUR"]["deadline"].startswith("2026-11-20T20:00"))
		self.assertEqual(by_name["SI-LATE"]["order_number"], "LATE")
		self.assertEqual(by_name["SI-LATE"]["customer_name"], "Mona")

	def test_open_shift_trusts_current_out_state_and_hides_went_out(self):
		rows = [
			self._row("SI-NOLOG", "2026-11-20", "14:00:00", state="Out for Delivery"),
			self._row("SI-WAIT", "2026-11-20", "14:00:00"),
		]
		late = self._resolve(rows, is_open=True)
		self.assertEqual([o["name"] for o in late], ["SI-WAIT"])
		self.assertIsNone(late[0]["went_out_at"])


class TestShiftSheetDeliveryMoney(TestShiftSheet):
	"""Courier cost, courier cash and the delivery totals per order."""

	def test_courier_cost_and_cash_in(self):
		invoices = [
			_inv(
				"SI-1",
				300,
				custom_courier_party_type="Employee",
				custom_courier_party="EMP-1",
				custom_shipping_expense=65,
			),
			_inv(
				"SI-3",
				480,
				custom_courier_party_type="Employee",
				custom_courier_party="EMP-1",
				custom_shipping_expense=50,
			),
			_inv("SI-4", 320, sales_partner="Talabat", custom_shipping_expense=25),
			_inv("SI-5", 200, custom_shipping_expense=70, custom_sales_invoice_state="Received"),
			# Taken and not sent out: never listed.
			_inv("SI-6", 150, custom_shipping_expense=40, custom_sales_invoice_state="Received"),
		]
		dispatches = {
			"SI-1": "2026-11-20 12:00:00",
			"SI-3": "2026-11-20 12:30:00",
			"SI-4": "2026-11-20 13:00:00",
		}
		settlements = {
			"SI-1": {"at": "2026-11-20 15:00:00", "amount": 300.0, "fee": 65.0},
			"SI-3": {"at": "2026-11-20 15:00:00", "amount": 0.0, "fee": 50.0},
		}
		sheet = self._run(
			"mgr@example.com",
			manager=True,
			profiles=["Branch A"],
			invoices=invoices,
			dispatches=dispatches,
			settlements=settlements,
			# SI-5 was paid in cash at the counter but has not gone out yet.
			drawer_paid={"SI-5": {"at": "2026-11-20 11:10:00", "amount": 200.0}},
		)
		rows = {o["name"]: o for o in sheet["orders"]}
		self.assertNotIn("SI-6", rows)
		self.assertEqual(rows["SI-1"]["shipping_expense"], 65.0)
		self.assertEqual(rows["SI-1"]["courier_cash_in"], 235.0)
		self.assertEqual(rows["SI-1"]["courier_fee_paid"], 65.0)
		# Prepaid order: the branch paid the courier its fee.
		self.assertEqual(rows["SI-3"]["courier_cash_in"], -50.0)
		# Partner-delivered order: no courier of ours, no cost.
		self.assertEqual(rows["SI-4"]["shipping_expense"], 0.0)
		# Not sent out yet: the stored figure is only an estimate.
		self.assertIsNone(rows["SI-5"]["shipping_expense"])
		totals = sheet["totals"]
		self.assertEqual(totals["delivery_orders"], 3)
		self.assertEqual(totals["delivery_income"], 30.0)
		self.assertEqual(totals["delivery_cost"], 115.0)
		self.assertEqual(totals["delivery_net"], -85.0)
		self.assertEqual(totals["courier_cash_in"], 185.0)
		self.assertEqual(totals["courier_fees_paid"], 115.0)
		self.assertEqual(sheet["cash_flow"]["expected"], 700.0)

	def test_staff_view_has_no_cash_flow(self):
		sheet = self._run("opener@example.com")
		self.assertIsNone(sheet["cash_flow"])

	def test_pickup_costs_nothing(self):
		self.assertEqual(
			shift._sheet_shipping_expense({"custom_is_pickup": 1, "custom_shipping_expense": 40}, True), 0.0
		)


class TestShiftSheetCashClassifier(unittest.TestCase):
	TILL = "Branch A - J"
	CO = "Courier Outstanding - J"
	OS = "Cash Over Short - J"

	def _classify(self, vouchers, legs, pe=None, parties=None):
		types = {
			"Bank Account - J": {"account_type": "Bank", "root_type": "Asset"},
			"Cash - J": {"account_type": "Cash", "root_type": "Asset"},
			"Misc - J": {"account_type": "", "root_type": "Expense"},
			"Creditors - J": {"account_type": "Payable", "root_type": "Liability"},
			self.CO: {"account_type": "", "root_type": "Asset"},
			self.OS: {"account_type": "", "root_type": "Expense"},
		}
		return shift._classify_sheet_cash(
			vouchers, legs, pe or {}, types, self.TILL, self.CO, self.OS, parties or set()
		)

	def test_courier_settlement_split_into_orders_and_fees(self):
		vouchers = [{"voucher_type": "Journal Entry", "voucher_no": "JE-1", "amount": 2990.0, "title": "Ali"}]
		legs = {
			"JE-1": [
				{"account": self.TILL, "debit": 2990, "credit": 0},
				{
					"account": "Creditors - J",
					"debit": 250,
					"credit": 0,
					"party_type": "Supplier",
					"party": "Ali",
				},
				{"account": self.CO, "debit": 0, "credit": 3240},
			]
		}
		sums, lines = self._classify(vouchers, legs)
		self.assertEqual(sums["courier_orders"], 3240.0)
		self.assertEqual(sums["courier_fees"], -250.0)
		self.assertEqual(len(lines), 2)

	def test_kinds(self):
		vouchers = [
			{"voucher_type": "Payment Entry", "voucher_no": "PE-1", "amount": 240.0},
			{"voucher_type": "Payment Entry", "voucher_no": "PE-2", "amount": -660.0},
			{"voucher_type": "Payment Entry", "voucher_no": "PE-3", "amount": -300.0},
			{"voucher_type": "Journal Entry", "voucher_no": "JE-2", "amount": -3000.0},
			{"voucher_type": "Journal Entry", "voucher_no": "JE-3", "amount": -30.0},
			{"voucher_type": "Journal Entry", "voucher_no": "JE-4", "amount": 50.0},
			{"voucher_type": "Journal Entry", "voucher_no": "JE-5", "amount": -200.0},
		]
		legs = {
			"JE-2": [{"account": "Cash - J", "debit": 3000, "credit": 0}],
			"JE-3": [{"account": "Misc - J", "debit": 30, "credit": 0}],
			"JE-4": [{"account": self.OS, "debit": 0, "credit": 50}],
			"JE-5": [
				{
					"account": "Creditors - J",
					"debit": 200,
					"credit": 0,
					"party_type": "Supplier",
					"party": "InDrive",
				}
			],
		}
		pe = {
			"PE-1": {"payment_type": "Receive", "party_name": "Cust"},
			"PE-2": {"payment_type": "Pay", "reference_no": "RET-CN-1", "party_name": "Cust"},
			"PE-3": {"payment_type": "Pay", "party_name": "Belal"},
		}
		sums, _lines = self._classify(vouchers, legs, pe, {("Supplier", "InDrive")})
		self.assertEqual(sums["sales_cash"], 240.0)
		self.assertEqual(sums["refunds"], -660.0)
		self.assertEqual(sums["expenses"], -330.0)
		self.assertEqual(sums["handovers"], -3000.0)
		self.assertEqual(sums["courier_fees"], -200.0)
		# A previous close's over/short entry is already in the counted opening.
		self.assertEqual(sums["other"], 0.0)

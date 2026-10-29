"""Order-proposal accuracy log -- pure, no site.

Pins how a proposal is compared with the billed Sales Invoice (plain lines,
bundles, quantities, charges that are not lines) and how the scored rows roll up
into the accuracy summary.

Plain ``unittest.TestCase``; a stub ``frappe`` is registered when frappe is not
installed.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace

try:  # pragma: no cover - depends on the environment
	import frappe
except ImportError:  # pragma: no cover
	frappe = types.ModuleType("frappe")
	frappe.whitelist = lambda *a, **k: (a[0] if a and callable(a[0]) else (lambda fn: fn))
	frappe._ = lambda m: m
	frappe.local = SimpleNamespace()
	frappe.session = SimpleNamespace(user="Guest")
	frappe.db = SimpleNamespace()
	frappe.log_error = lambda *a, **k: None
	frappe.get_traceback = lambda *a, **k: ""
	sys.modules["frappe"] = frappe
	_utils = types.ModuleType("frappe.utils")
	_utils.get_datetime = lambda d=None: d
	_utils.now_datetime = lambda: datetime.datetime(2026, 10, 29, 12, 0)
	_utils.nowdate = lambda: "2026-10-29"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_order_log as log


def item(code, qty=1):
	return {"kind": "item", "item_code": code, "item_name": code, "qty": qty}


def bundle(code, picks, qty=1):
	return {
		"kind": "bundle",
		"bundle_id": "B-" + code,
		"item_code": code,
		"item_name": code,
		"qty": qty,
		"selections": {"g1": [{"item_code": c, "qty": q} for c, q in picks]},
		"complete": True,
	}


def row(code, qty=1):
	return {"item_code": code, "qty": qty}


def parent(code, bundle_code, qty=1):
	return {"item_code": code, "qty": qty, "is_bundle_parent": 1, "bundle_code": bundle_code}


def child(code, bundle_code, qty=1):
	return {"item_code": code, "qty": qty, "is_bundle_child": 1, "parent_bundle": bundle_code}


class CompareTests(unittest.TestCase):
	def test_exact_match(self):
		result = log.compare([item("Lotus", 2), item("Mango")], [row("Mango"), row("Lotus", 2)])
		self.assertTrue(result["exact_match"])
		self.assertEqual((result["lines_billed"], result["lines_matched"]), (2, 2))
		self.assertEqual(result["missing"], [])
		self.assertEqual(result["extra"], [])

	def test_missing_line(self):
		result = log.compare([item("Lotus")], [row("Lotus"), row("Mango")])
		self.assertFalse(result["exact_match"])
		self.assertEqual((result["lines_billed"], result["lines_matched"]), (2, 1))
		self.assertEqual(result["missing"], ["Mango x1"])
		self.assertEqual(result["extra"], [])

	def test_extra_line(self):
		result = log.compare([item("Lotus"), item("Oreo")], [row("Lotus")])
		self.assertFalse(result["exact_match"])
		self.assertEqual((result["lines_billed"], result["lines_matched"]), (1, 1))
		self.assertEqual(result["extra"], ["Oreo x1"])

	def test_quantity_mismatch_is_a_miss_and_an_extra(self):
		result = log.compare([item("Lotus", 2)], [row("Lotus", 3)])
		self.assertFalse(result["exact_match"])
		self.assertEqual(result["lines_matched"], 0)
		self.assertEqual(result["missing"], ["Lotus x3"])
		self.assertEqual(result["extra"], ["Lotus x2"])

	def test_repeated_plain_lines_merge(self):
		result = log.compare([item("Lotus"), item("Lotus")], [row("Lotus", 2)])
		self.assertTrue(result["exact_match"])

	def test_bundle_same_flavours_in_another_order(self):
		proposal = [bundle("TRIO", [("Choc", 2), ("Mango", 1)])]
		invoice = [
			parent("TRIO", "BNDL-TRIO"),
			child("Mango", "BNDL-TRIO", 1),
			child("Choc", "BNDL-TRIO", 2),
		]
		result = log.compare(proposal, invoice)
		self.assertTrue(result["exact_match"])
		self.assertEqual((result["lines_billed"], result["lines_matched"]), (1, 1))

	def test_bundle_picks_split_over_several_rows_merge(self):
		proposal = [bundle("TRIO", [("Choc", 1), ("Choc", 1), ("Mango", 1)])]
		invoice = [parent("TRIO", "B"), child("Choc", "B", 2), child("Mango", "B", 1)]
		self.assertTrue(log.compare(proposal, invoice)["exact_match"])

	def test_bundle_with_a_different_flavour_does_not_match(self):
		proposal = [bundle("TRIO", [("Choc", 3)])]
		invoice = [parent("TRIO", "B"), child("Choc", "B", 2), child("Mango", "B", 1)]
		result = log.compare(proposal, invoice)
		self.assertFalse(result["exact_match"])
		self.assertEqual(result["lines_matched"], 0)
		self.assertEqual(len(result["missing"]), 1)
		self.assertEqual(len(result["extra"]), 1)

	def test_two_bundles_billed_as_one_row_of_qty_two(self):
		proposal = [bundle("TRIO", [("Choc", 3)]), bundle("TRIO", [("Mango", 3)])]
		invoice = [parent("TRIO", "B", 2), child("Choc", "B", 3), child("Mango", "B", 3)]
		result = log.compare(proposal, invoice)
		self.assertTrue(result["exact_match"])
		self.assertEqual(result["lines_billed"], 1)

	def test_bundle_children_do_not_count_as_plain_lines(self):
		proposal = [bundle("TRIO", [("Choc", 3)]), item("Lotus")]
		invoice = [parent("TRIO", "B"), child("Choc", "B", 3), row("Lotus")]
		result = log.compare(proposal, invoice)
		self.assertTrue(result["exact_match"])
		self.assertEqual(result["lines_billed"], 2)

	def test_two_kinds_of_bundle_keep_their_own_flavours(self):
		proposal = [bundle("TRIO", [("Choc", 3)]), bundle("DUO", [("Mango", 2)])]
		invoice = [
			parent("TRIO", "B1"),
			child("Choc", "B1", 3),
			parent("DUO", "B2"),
			child("Mango", "B2", 2),
		]
		self.assertTrue(log.compare(proposal, invoice)["exact_match"])

	def test_charges_are_not_lines(self):
		invoice = [row("Lotus"), row("Shipping Income"), row("Delivery Fee"), row("", 1), row("Mango", 0)]
		result = log.compare([item("Lotus")], invoice)
		self.assertTrue(result["exact_match"])
		self.assertEqual(result["lines_billed"], 1)

	def test_empty_invoice_is_never_exact(self):
		result = log.compare([], [])
		self.assertFalse(result["exact_match"])
		self.assertEqual(result["lines_billed"], 0)

	def test_garbage_input_does_not_raise(self):
		result = log.compare([None, "x", {"kind": "item"}], [{"qty": "abc", "item_code": "Lotus"}])
		self.assertEqual(result["lines_matched"], 0)


class SummaryTests(unittest.TestCase):
	def test_empty(self):
		summary = log.summarise([])
		self.assertEqual(summary["proposals"], 0)
		self.assertIsNone(summary["exact_match_rate"])
		self.assertIsNone(summary["mean_line_recall"])
		self.assertEqual(summary["total_cost_usd"], 0)

	def test_rollup(self):
		rows = [
			# exact
			{
				"sales_invoice": "SI-1",
				"scored_at": "x",
				"exact_match": 1,
				"lines_billed": 2,
				"lines_matched": 2,
				"lines_proposed": 2,
				"cost_usd": 0.01,
			},
			# half read, one extra line
			{
				"sales_invoice": "SI-2",
				"scored_at": "x",
				"exact_match": 0,
				"lines_billed": 2,
				"lines_matched": 1,
				"lines_proposed": 4,
				"cost_usd": 0.02,
			},
			# scored, no invoice
			{"scored_at": "x", "lines_proposed": 3, "cost_usd": 0.03},
			# not scored yet
			{"lines_proposed": 1, "cost_usd": 0.04},
		]
		summary = log.summarise(rows)
		self.assertEqual(summary["proposals"], 4)
		self.assertEqual(summary["scored"], 3)
		self.assertEqual(summary["with_invoice"], 2)
		self.assertEqual(summary["exact_match"], 1)
		self.assertEqual(summary["exact_match_rate"], 0.5)
		self.assertEqual(summary["mean_line_recall"], 0.75)
		self.assertEqual(summary["mean_line_precision"], 0.625)
		self.assertAlmostEqual(summary["total_cost_usd"], 0.10)


if __name__ == "__main__":
	unittest.main()

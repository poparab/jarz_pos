from __future__ import annotations

from collections import defaultdict

import frappe
from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry as ERPNextStockEntry
from frappe import _
from frappe.utils import flt


class StockEntry(ERPNextStockEntry):
	def validate_fg_completed_qty(self):
		"""ERPNext 16.12's check, with a rounding tolerance instead of float ``!=``.

		ERPNext compares ``fg_completed_qty`` with ``fg_item_qty + process_loss_qty``
		exactly. Each side is rounded to the qty precision, but the float SUM of two
		rounded values often is not: 2.538 + 0.162 == 2.6999999999999997, not 2.7.
		So a Manufacture entry for a BOM with a process loss was refused for about
		one weight in four ("Since there is a process loss of 0.16200000000000037
		units ...", Chocolate ganache 6%, 2.7 Kg, 2026-11-26). The rule itself is
		kept: a real mismatch, larger than half the smallest unit, still throws.
		"""
		if self.purpose != "Manufacture":
			return

		fg_qty = defaultdict(float)
		for d in self.items:
			if d.is_finished_item:
				fg_qty[d.item_code] += flt(d.qty)

		if not fg_qty:
			return

		precision = frappe.get_precision("Stock Entry Detail", "qty")
		tolerance = 0.5 / (10 ** (precision or 0))
		fg_item = next(iter(fg_qty.keys()))
		fg_item_qty = flt(fg_qty[fg_item], precision)
		fg_completed_qty = flt(self.fg_completed_qty, precision)

		for d in self.items:
			if not fg_qty.get(d.item_code):
				continue

			if (fg_completed_qty - fg_item_qty) > 0:
				# Rounded, so 0.16200000000000037 is never stored or shown.
				self.process_loss_qty = flt(fg_completed_qty - fg_item_qty, precision)

			if not self.process_loss_qty:
				continue

			expected = flt(fg_item_qty, precision) + flt(self.process_loss_qty, precision)
			if abs(fg_completed_qty - expected) >= tolerance:
				frappe.throw(
					_(
						"Since there is a process loss of {0} units for the finished good {1}, you should reduce the quantity by {0} units for the finished good {1} in the Items Table."
					).format(frappe.bold(self.process_loss_qty), frappe.bold(d.item_code))
				)

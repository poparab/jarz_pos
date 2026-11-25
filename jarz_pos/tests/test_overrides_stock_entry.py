import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _entry(fg_completed_qty, fg_item_qty, process_loss_qty=0.0, purpose="Manufacture"):
	from jarz_pos.overrides.stock_entry import StockEntry

	se = StockEntry.__new__(StockEntry)
	se.__dict__.update(
		purpose=purpose,
		fg_completed_qty=fg_completed_qty,
		process_loss_qty=process_loss_qty,
		items=[
			SimpleNamespace(item_code="Cocoa", qty=1.0, is_finished_item=0),
			SimpleNamespace(item_code="Chocolate ganache", qty=fg_item_qty, is_finished_item=1),
		],
	)
	return se


class _Thrown(Exception):
	pass


class TestStockEntryProcessLossTolerance(unittest.TestCase):
	def _run(self, se):
		mock_frappe = MagicMock()
		mock_frappe.get_precision.return_value = 3
		mock_frappe.throw.side_effect = _Thrown
		mock_frappe.bold.side_effect = lambda v: v
		with patch("jarz_pos.overrides.stock_entry.frappe", mock_frappe), patch(
			"jarz_pos.overrides.stock_entry._", side_effect=lambda s: s
		):
			se.validate_fg_completed_qty()

	def test_float_sum_that_erpnext_refused_now_passes(self):
		# 2026-11-26: 2.7 Kg ganache at 6% loss; 2.538 + 0.162 == 2.6999999999999997.
		self.assertNotEqual(2.538 + 0.162, 2.7)
		se = _entry(2.7, 2.538, process_loss_qty=2.7 * 6 / 100)
		self._run(se)
		self.assertEqual(se.process_loss_qty, 0.162)

	def test_every_weight_at_common_loss_rates_passes(self):
		for pct in (2, 6, 3.5, 10):
			for grams in range(100, 10001, 7):
				fg = grams / 1000
				out = round(fg - fg * pct / 100, 3)
				with self.subTest(pct=pct, fg=fg):
					self._run(_entry(fg, out, process_loss_qty=fg * pct / 100))

	def test_real_mismatch_still_refused(self):
		# The entry books all 2.7 out yet still claims a 0.1 loss: 2.7 + 0.1 is
		# 0.1 Kg off, which is not rounding.
		se = _entry(2.7, 2.7, process_loss_qty=0.1)
		with self.assertRaises(_Thrown):
			self._run(se)

	def test_non_manufacture_untouched(self):
		se = _entry(2.7, 1.0, process_loss_qty=5, purpose="Material Transfer")
		self._run(se)
		self.assertEqual(se.process_loss_qty, 5)


if __name__ == "__main__":
	unittest.main()

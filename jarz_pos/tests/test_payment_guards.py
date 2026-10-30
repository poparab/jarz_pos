"""Payment Entry over-allocation guard.

Pure mocks: the module's ``frappe`` handle is replaced, so nothing here touches
the site. What is pinned, and why each matters:

* An allocation within the freshly locked outstanding passes; one piastre-plus
  over it is refused. This is the 2026-10-28 case: the second and third
  payments for one invoice must see the outstanding the first one left.
* The read is a LOCKING read (``FOR UPDATE``): a plain read answers from the
  transaction's older snapshot, which is exactly how the race got through.
* An entry with no invoice allocation takes no lock at all.
* Several rows for one invoice are summed, and invoices are locked in sorted
  order so two entries cannot deadlock each other.
* Returns (negative outstanding) and non-submitted references are left alone.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _Refused(Exception):
	pass


def _throw(msg, *args, **kwargs):
	raise _Refused(str(msg))


def _mock_frappe(rows_by_name):
	mock = MagicMock()
	mock.throw.side_effect = _throw

	def _sql(query, values=None, as_dict=False):
		row = rows_by_name.get(values[0])
		return [SimpleNamespace(**row)] if row else []

	mock.db.sql.side_effect = _sql
	return mock


def _pe(*refs):
	return {
		"references": [
			{"reference_doctype": d, "reference_name": n, "allocated_amount": a} for d, n, a in refs
		]
	}


class TestGuardInvoiceOverallocation(unittest.TestCase):
	def _run(self, doc, rows):
		from jarz_pos.services import payment_guards

		mock = _mock_frappe(rows)
		with patch.object(payment_guards, "frappe", mock), patch.object(payment_guards, "_", lambda s: s):
			payment_guards.guard_invoice_overallocation(doc)
		return mock

	def test_within_outstanding_passes(self):
		mock = self._run(
			_pe(("Purchase Invoice", "PI-1", 14128.0)),
			{"PI-1": {"outstanding_amount": 14128.06, "docstatus": 1}},
		)
		query = mock.db.sql.call_args[0][0]
		self.assertIn("FOR UPDATE", query)
		self.assertIn("`tabPurchase Invoice`", query)

	def test_second_payment_after_first_committed_is_refused(self):
		with self.assertRaises(_Refused):
			self._run(
				_pe(("Purchase Invoice", "PI-1", 14128.0)),
				{"PI-1": {"outstanding_amount": 0, "docstatus": 1}},
			)

	def test_boundary(self):
		self._run(
			_pe(("Sales Invoice", "SI-1", 100.01)), {"SI-1": {"outstanding_amount": 100.0, "docstatus": 1}}
		)
		with self.assertRaises(_Refused):
			self._run(
				_pe(("Sales Invoice", "SI-1", 100.02)),
				{"SI-1": {"outstanding_amount": 100.0, "docstatus": 1}},
			)

	def test_rows_for_one_invoice_are_summed(self):
		with self.assertRaises(_Refused):
			self._run(
				_pe(("Sales Invoice", "SI-1", 60), ("Sales Invoice", "SI-1", 60)),
				{"SI-1": {"outstanding_amount": 100, "docstatus": 1}},
			)

	def test_no_invoice_allocation_takes_no_lock(self):
		mock = self._run(_pe(("Sales Order", "SO-1", 500), ("Sales Invoice", "SI-1", 0)), {})
		mock.db.sql.assert_not_called()
		mock = self._run({"references": []}, {})
		mock.db.sql.assert_not_called()

	def test_locks_in_sorted_order(self):
		rows = {
			"SI-B": {"outstanding_amount": 50, "docstatus": 1},
			"SI-A": {"outstanding_amount": 50, "docstatus": 1},
			"PI-Z": {"outstanding_amount": 50, "docstatus": 1},
		}
		mock = self._run(
			_pe(
				("Sales Invoice", "SI-B", 10), ("Sales Invoice", "SI-A", 10), ("Purchase Invoice", "PI-Z", 10)
			),
			rows,
		)
		locked = [c[0][1][0] for c in mock.db.sql.call_args_list]
		self.assertEqual(locked, ["PI-Z", "SI-A", "SI-B"])

	def test_returns_and_unsubmitted_are_skipped(self):
		self._run(
			_pe(("Sales Invoice", "SI-RET", 80)), {"SI-RET": {"outstanding_amount": -80, "docstatus": 1}}
		)
		self._run(
			_pe(("Sales Invoice", "SI-DRAFT", 80)), {"SI-DRAFT": {"outstanding_amount": 0, "docstatus": 0}}
		)
		self._run(_pe(("Sales Invoice", "SI-GONE", 80)), {})


if __name__ == "__main__":
	unittest.main()

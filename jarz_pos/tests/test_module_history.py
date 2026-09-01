"""History endpoints for the modules that used to submit into a void.

Stock Transfer, Cash Transfer, Inventory Count and Shift each posted a document
and then had no way to show it again.  These tests pin the parts of the four
``list_*`` endpoints that are actual logic rather than a pass-through query:

* which parent documents a child-table filter is allowed to narrow to, and the
  difference between "no restriction" and "nothing matches";
* how a cash transfer is recognised, given that nothing marks one;
* the per-line delta a count reports;
* and that shift amounts stay manager-only.

They patch ``frappe.get_all`` / ``frappe.db`` rather than touching a site, which
is what the rest of the mock suite does and what lets this module run in CI
against the populated ``frontend`` clone without writing to it.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _dispatch(handlers, default=None):
	"""Build a ``frappe.get_all`` stand-in that answers per DocType.

	``handlers`` maps a DocType name to either a list of rows or a callable
	taking the keyword arguments of the call.
	"""

	def fake_get_all(doctype, *args, **kwargs):
		handler = handlers.get(doctype, default if default is not None else [])
		if callable(handler):
			return handler(**kwargs)
		return handler

	return fake_get_all


def _fake_db(count=0):
	"""A stand-in for ``frappe.db``.

	The whole object is replaced rather than an attribute patched on it:
	outside a site request ``frappe.db`` is an unbound Local proxy, so
	``patch.object(frappe.db, ...)`` raises before the test body runs.
	Swapping the module attribute works both with and without a site, and
	guarantees no query reaches a real database either way.
	"""
	return SimpleNamespace(
		count=lambda *args, **kwargs: count,
		get_single_value=lambda *args, **kwargs: "JARZ",
	)


class TestStockTransferHistory(unittest.TestCase):
	"""``jarz_pos.api.transfer.list_transfers``."""

	def _call(self, handlers, count=0, **kwargs):
		from jarz_pos.api import transfer

		with patch.object(transfer, "_ensure_transfer_access"), patch.object(
			transfer.frappe, "get_all", side_effect=_dispatch(handlers)
		), patch.object(transfer.frappe, "db", _fake_db(count)):
			return transfer.list_transfers(**kwargs)

	def test_unfiltered_call_does_not_restrict_by_name(self):
		"""With no warehouse and no search, every submitted transfer qualifies.

		The guard here is that ``allowed_parents`` stays ``None`` rather than
		collapsing to an empty set — an empty set means "nothing matches" and
		would return an empty history for the plain case.
		"""
		seen = {}

		def stock_entry(**kwargs):
			seen.update(kwargs.get("filters") or {})
			return [{
				"name": "STE-0001",
				"posting_date": "2026-09-01",
				"posting_time": "10:00:00",
				"total_outgoing_value": 100.0,
				"owner": "a@example.com",
				"creation": "2026-09-01 10:00:00",
				"remarks": None,
			}]

		result = self._call(
			{
				"Stock Entry": stock_entry,
				"Stock Entry Detail": [
					{"parent": "STE-0001", "item_code": "JAR-1", "item_name": "Jar",
					 "qty": 3.0, "uom": "Nos", "s_warehouse": "A", "t_warehouse": "B",
					 "basic_rate": 10.0, "amount": 30.0},
					{"parent": "STE-0001", "item_code": "JAR-2", "item_name": "Jar 2",
					 "qty": 2.0, "uom": "Nos", "s_warehouse": "A", "t_warehouse": "B",
					 "basic_rate": 10.0, "amount": 20.0},
				],
				"User": [{"name": "a@example.com", "full_name": "Ada"}],
			},
			count=1,
		)

		self.assertNotIn("name", seen, "an unfiltered call must not pin a name list")
		self.assertEqual(result["total"], 1)
		row = result["transfers"][0]
		self.assertEqual(row["source_warehouse"], "A")
		self.assertEqual(row["target_warehouse"], "B")
		self.assertEqual(row["total_qty"], 5.0)
		self.assertEqual(row["owner_name"], "Ada")
		self.assertEqual(len(row["items"]), 2)

	def test_warehouse_filter_with_no_match_returns_empty(self):
		"""No child row on that warehouse means no transfer, not every transfer."""
		result = self._call({"Stock Entry Detail": []}, count=99,
							source_warehouse="Nowhere")
		self.assertEqual(result, {"transfers": [], "total": 0})

	def test_search_matches_name_or_item_and_intersects_warehouse(self):
		"""Both narrowing paths apply together, not one overriding the other."""
		captured = {}

		def stock_entry(**kwargs):
			filters = kwargs.get("filters") or {}
			if filters.get("name", [None])[0] == "like":
				return [{"name": "STE-0001"}, {"name": "STE-0009"}]
			captured.update(filters)
			return []

		def detail(**kwargs):
			filters = kwargs.get("filters") or {}
			if filters.get("s_warehouse"):
				# Only 0001 and 0002 leave warehouse A.
				return [{"parent": "STE-0001"}, {"parent": "STE-0002"}]
			# Item search hits 0001 and 0009.
			return [{"parent": "STE-0001"}, {"parent": "STE-0009"}]

		self._call(
			{"Stock Entry": stock_entry, "Stock Entry Detail": detail},
			source_warehouse="A",
			search="jar",
		)
		self.assertEqual(captured.get("name"), ["in", ["STE-0001"]])

	def test_mixed_warehouses_report_no_single_route(self):
		"""A hand-made entry spanning warehouses has no one route to name."""
		result = self._call(
			{
				"Stock Entry": [{
					"name": "STE-0002", "posting_date": "2026-09-01",
					"posting_time": "10:00:00", "total_outgoing_value": 0.0,
					"owner": None, "creation": "2026-09-01 10:00:00", "remarks": None,
				}],
				"Stock Entry Detail": [
					{"parent": "STE-0002", "item_code": "X", "item_name": "X", "qty": 1.0,
					 "uom": "Nos", "s_warehouse": "A", "t_warehouse": "B",
					 "basic_rate": 0.0, "amount": 0.0},
					{"parent": "STE-0002", "item_code": "Y", "item_name": "Y", "qty": 1.0,
					 "uom": "Nos", "s_warehouse": "C", "t_warehouse": "B",
					 "basic_rate": 0.0, "amount": 0.0},
				],
			},
			count=1,
		)
		row = result["transfers"][0]
		self.assertIsNone(row["source_warehouse"])
		self.assertEqual(row["source_warehouses"], ["A", "C"])
		self.assertEqual(row["target_warehouse"], "B")


class TestCashTransferHistory(unittest.TestCase):
	"""``jarz_pos.api.cash_transfer.list_transfers``.

	``submit_transfer`` leaves no marker on the Journal Entry it writes, so the
	history recognises a transfer by shape.  These tests pin that shape.
	"""

	TRANSFERABLE = {"Cash - J", "Bank - J", "Dokki - J"}

	def _call(self, je_rows, journal_entries=None, **kwargs):
		from jarz_pos.api import cash_transfer

		journal_entries = journal_entries if journal_entries is not None else []

		def journal_entry(**call_kwargs):
			if call_kwargs.get("fields") == ["name"]:
				return [{"name": e["name"]} for e in journal_entries]
			return journal_entries

		handlers = {
			"Journal Entry Account": je_rows,
			"Journal Entry": journal_entry,
			"User": [],
			"Account": [{"name": n, "account_name": n.split(" - ")[0]}
						for n in sorted(self.TRANSFERABLE)],
		}
		with patch.object(cash_transfer, "_ensure_manager_access"), patch.object(
			cash_transfer, "_transferable_account_names",
			return_value=set(self.TRANSFERABLE)
		), patch.object(
			cash_transfer.frappe, "get_all", side_effect=_dispatch(handlers)
		), patch.object(cash_transfer.frappe, "db", _fake_db()), patch.object(
			cash_transfer.frappe.defaults, "get_user_default", return_value="JARZ"
		):
			return cash_transfer.list_transfers(**kwargs)

	def _two_leg_rows(self, credit_account, debit_account):
		return [
			{"parent": "JE-1", "account": credit_account, "idx": 1,
			 "debit_in_account_currency": 0.0, "credit_in_account_currency": 500.0},
			{"parent": "JE-1", "account": debit_account, "idx": 2,
			 "debit_in_account_currency": 500.0, "credit_in_account_currency": 0.0},
		]

	def test_two_leg_entry_between_cash_accounts_is_a_transfer(self):
		result = self._call(
			self._two_leg_rows("Cash - J", "Bank - J"),
			journal_entries=[{
				"name": "JE-1", "posting_date": "2026-09-01", "total_debit": 500.0,
				"user_remark": "drawer to bank", "owner": None,
				"creation": "2026-09-01 09:00:00", "voucher_type": "Journal Entry",
			}],
		)
		self.assertEqual(result["total"], 1)
		row = result["transfers"][0]
		self.assertEqual(row["from_account"], "Cash - J")
		self.assertEqual(row["to_account"], "Bank - J")
		self.assertEqual(row["amount"], 500.0)
		self.assertEqual(row["from_label"], "Cash")

	def test_entry_touching_a_non_transferable_account_is_excluded(self):
		"""The shift discrepancy entry is Cash -> Cash Over/Short, not a transfer.

		Both entries touch a cash account, so the candidate query returns both;
		only the second leg tells them apart.
		"""
		result = self._call(self._two_leg_rows("Cash - J", "Cash Over/Short - J"))
		self.assertEqual(result, {"transfers": [], "total": 0})

	def test_entry_with_more_than_two_legs_is_excluded(self):
		rows = self._two_leg_rows("Cash - J", "Bank - J")
		rows.append({
			"parent": "JE-1", "account": "Dokki - J", "idx": 3,
			"debit_in_account_currency": 10.0, "credit_in_account_currency": 0.0,
		})
		result = self._call(rows)
		self.assertEqual(result, {"transfers": [], "total": 0})

	def test_account_filter_matches_either_side(self):
		rows = self._two_leg_rows("Cash - J", "Bank - J")
		entries = [{
			"name": "JE-1", "posting_date": "2026-09-01", "total_debit": 500.0,
			"user_remark": None, "owner": None, "creation": "2026-09-01 09:00:00",
			"voucher_type": "Journal Entry",
		}]
		self.assertEqual(
			self._call(rows, journal_entries=entries, account="Bank - J")["total"], 1,
			"the receiving side must match",
		)
		self.assertEqual(
			self._call(rows, journal_entries=entries, account="Cash - J")["total"], 1,
			"the sending side must match",
		)
		self.assertEqual(
			self._call(rows, journal_entries=entries, account="Dokki - J"),
			{"transfers": [], "total": 0},
			"an uninvolved account must match nothing",
		)


class TestInventoryCountHistory(unittest.TestCase):
	"""``jarz_pos.api.inventory_count.list_reconciliations``."""

	def _call(self, handlers, count=0, **kwargs):
		from jarz_pos.api import inventory_count

		with patch.object(inventory_count, "_ensure_manager_access"), patch.object(
			inventory_count.frappe, "get_all", side_effect=_dispatch(handlers)
		), patch.object(inventory_count.frappe, "db", _fake_db(count)):
			return inventory_count.list_reconciliations(**kwargs)

	def test_line_delta_is_counted_minus_system(self):
		"""The stored row holds before and after; the operator wants the delta."""
		result = self._call(
			{
				"Stock Reconciliation": [{
					"name": "SR-1", "posting_date": "2026-09-01",
					"posting_time": "18:00:00", "set_warehouse": "Dokki - J",
					"difference_amount": -250.0, "company": "JARZ", "owner": None,
					"creation": "2026-09-01 18:00:00",
				}],
				"Stock Reconciliation Item": [
					{"parent": "SR-1", "item_code": "A", "item_name": "A",
					 "warehouse": "Dokki - J", "qty": 12.0, "current_qty": 15.0,
					 "valuation_rate": 10.0, "current_valuation_rate": 10.0,
					 "amount": 120.0, "current_amount": 150.0},
					{"parent": "SR-1", "item_code": "B", "item_name": "B",
					 "warehouse": "Dokki - J", "qty": 8.0, "current_qty": 5.0,
					 "valuation_rate": 10.0, "current_valuation_rate": 10.0,
					 "amount": 80.0, "current_amount": 50.0},
				],
			},
			count=1,
		)
		row = result["counts"][0]
		self.assertEqual([i["qty_difference"] for i in row["items"]], [-3.0, 3.0])
		self.assertEqual(row["item_count"], 2)
		self.assertEqual(row["increase_count"], 1)
		self.assertEqual(row["decrease_count"], 1)
		self.assertEqual(row["warehouse"], "Dokki - J")

	def test_warehouse_filter_unions_header_and_line_matches(self):
		"""`set_warehouse` is only set when every line agrees, so both are checked."""
		captured = {}

		def reconciliation(**kwargs):
			filters = kwargs.get("filters") or {}
			if isinstance(filters.get("set_warehouse"), str):
				return [{"name": "SR-HEADER"}]
			captured.update(filters)
			return []

		self._call(
			{
				"Stock Reconciliation": reconciliation,
				"Stock Reconciliation Item": [{"parent": "SR-LINE"}],
			},
			warehouse="Dokki - J",
		)
		self.assertEqual(captured.get("name"), ["in", ["SR-HEADER", "SR-LINE"]])

	def test_warehouse_with_no_match_returns_empty(self):
		result = self._call(
			{"Stock Reconciliation": [], "Stock Reconciliation Item": []},
			count=42,
			warehouse="Nowhere",
		)
		self.assertEqual(result, {"counts": [], "total": 0})


class TestShiftHistory(unittest.TestCase):
	"""``jarz_pos.api.shift.list_shifts``.

	The blind close in ``get_shift_summary`` withholds every figure from the
	closing cashier on purpose.  A history that handed the same person those
	figures afterwards would be a way around it, so the amounts are gated.
	"""

	OPENING = {
		"name": "POS-OPEN-1", "status": "Closed", "user": "cashier@example.com",
		"company": "JARZ", "pos_profile": "Dokki", "docstatus": 1,
		"period_start_date": "2026-09-01 09:00:00",
		"period_end_date": "2026-09-01 21:00:00",
		"creation": "2026-09-01 09:00:00",
	}
	CLOSING = {
		"name": "POS-CLOSE-1", "pos_opening_entry": "POS-OPEN-1",
		"posting_date": "2026-09-01", "period_end_date": "2026-09-01 21:00:00",
		"grand_total": 5000.0, "net_total": 4800.0, "total_quantity": 120.0,
		"status": "Submitted",
	}
	RECON = [
		{"parent": "POS-CLOSE-1", "mode_of_payment": "Cash", "opening_amount": 1000.0,
		 "expected_amount": 4000.0, "closing_amount": 3950.0, "difference": -50.0},
		{"parent": "POS-CLOSE-1", "mode_of_payment": "InstaPay", "opening_amount": 0.0,
		 "expected_amount": 1000.0, "closing_amount": 1000.0, "difference": 0.0},
	]

	def _call(self, *, is_manager, **kwargs):
		from jarz_pos.api import shift

		captured = {}

		def opening(**call_kwargs):
			captured.update(call_kwargs.get("filters") or {})
			return [dict(self.OPENING)]

		handlers = {
			"POS Opening Entry": opening,
			"POS Closing Entry": [dict(self.CLOSING)],
			"POS Closing Entry Detail": [dict(r) for r in self.RECON],
			"User": [{"name": "cashier@example.com", "full_name": "Sam"}],
		}
		with patch.object(shift, "_user_can_see_all_shifts", return_value=is_manager), \
				patch.object(shift.frappe, "get_all", side_effect=_dispatch(handlers)), \
				patch.object(shift.frappe, "db", _fake_db(1)), \
				patch.object(shift.frappe, "session", shift.frappe._dict(
					{"user": "cashier@example.com"})):
			return shift.list_shifts(**kwargs), captured

	def test_manager_sees_amounts_and_the_signed_variance(self):
		result, _ = self._call(is_manager=True)
		self.assertEqual(result["amounts_hidden"], 0)
		row = result["shifts"][0]
		self.assertEqual(row["grand_total"], 5000.0)
		self.assertEqual(row["difference"], -50.0, "the drawer was 50 short overall")
		self.assertEqual(len(row["payment_reconciliation"]), 2)
		self.assertEqual(row["closing_entry"], "POS-CLOSE-1")
		self.assertFalse(row["is_open"])

	def test_non_manager_gets_own_shifts_without_money(self):
		result, filters = self._call(is_manager=False)
		self.assertEqual(filters.get("user"), "cashier@example.com",
						 "a non-manager must be pinned to their own shifts")
		self.assertEqual(result["amounts_hidden"], 1)
		row = result["shifts"][0]
		self.assertNotIn("grand_total", row)
		self.assertNotIn("payment_reconciliation", row)
		self.assertNotIn("difference", row)
		self.assertEqual(row["pos_profile"], "Dokki")
		self.assertEqual(row["user_full_name"], "Sam")

	def test_manager_can_narrow_to_their_own_shifts(self):
		_, filters = self._call(is_manager=True, mine_only=1)
		self.assertEqual(filters.get("user"), "cashier@example.com")

	def test_manager_is_not_pinned_to_own_shifts_by_default(self):
		_, filters = self._call(is_manager=True)
		self.assertNotIn("user", filters)


if __name__ == "__main__":
	unittest.main()

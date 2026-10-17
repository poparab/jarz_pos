"""Board load speed: the fast path must show exactly the cards the old path showed.

Pure mock/unittest — no site data, no DB writes. One fake "world" (invoices,
customers, contacts, addresses, territories, courier rows) is answered both
through the per-call APIs the old path used (frappe.db.get_value / exists,
frappe.get_doc) and through the bulk frappe.get_all the fast path uses, so the
two paths read the same data and their boards can be compared card by card.

Why it exists: on production the unfiltered board built a full card for all
1,227 orders of the last 60 days and then discarded all but the newest
Delivered ones (5.3 s per load). The fast path cuts the finished-work columns
first and batches the per-order lookups; none of that may change a card.
"""

import copy
import datetime
import unittest
from unittest.mock import MagicMock, patch

import frappe

from jarz_pos.api import kanban
from jarz_pos.api.kanban import (
	_KANBAN_TRIM_MAX_CARDS,
	_format_address_row,
	_get_address_details_map,
	_get_unsettled_courier_invoice_set,
	_prefetch_customer_phones,
	_resolve_customer_phone,
	_rows_in_visible_window,
	get_kanban_invoices,
)
from jarz_pos.utils import invoice_utils

TODAY = datetime.date(2026, 10, 17)
CUTOFF = "2026-10-15"


def _d(days_ago: int) -> datetime.date:
	return TODAY - datetime.timedelta(days=days_ago)


class _NotFound(Exception):
	pass


class FakeWorld:
	"""A tiny in-memory database answering both the per-call and bulk reads."""

	def __init__(self):
		self.tables: dict[str, dict[str, dict]] = {
			"Customer": {},
			"Contact": {},
			"Address": {},
			"Territory": {},
			"Dynamic Link": {},
			"Courier Transaction": {},
		}
		self.invoices: list[frappe._dict] = []
		self.items: list[dict] = []
		self.taxes: list[dict] = []
		self.calls: list[tuple] = []

	# -- matching -----------------------------------------------------------
	@staticmethod
	def _match(row: dict, filters) -> bool:
		if not filters:
			return True
		for key, cond in filters.items():
			val = row.get(key)
			if isinstance(cond, (list, tuple)):
				op, arg = cond[0], cond[1]
				if op == "in":
					if val not in arg:
						return False
				elif op == "!=":
					# SQL: NULL != x is not true.
					if val is None or val == arg:
						return False
				elif op == "like":
					if not str(val or "").startswith(str(arg).rstrip("%")):
						return False
				else:
					raise AssertionError(f"unsupported op {op}")
			elif val != cond:
				return False
		return True

	def _rows(self, doctype):
		return [dict(r, name=n) for n, r in self.tables[doctype].items()]

	# -- bulk ---------------------------------------------------------------
	def get_all(self, doctype, filters=None, fields=None, pluck=None, limit=None, **kwargs):
		self.calls.append(("get_all", doctype, limit))
		if doctype == "Sales Invoice":
			return [frappe._dict(copy.deepcopy(r)) for r in self.invoices]
		if doctype == "Sales Invoice Item":
			rows = [r for r in self.items if self._match(r, filters)]
		elif doctype == "Sales Taxes and Charges":
			rows = [r for r in self.taxes if self._match(r, filters)]
		else:
			rows = [r for r in self._rows(doctype) if self._match(r, filters)]
		if pluck:
			return [r.get(pluck) for r in rows]
		out = []
		for r in rows:
			if fields:
				out.append(frappe._dict({f: r.get(f) for f in fields}))
			else:
				out.append(frappe._dict(r))
		return out

	# -- per call -----------------------------------------------------------
	def get_value(self, doctype, filters, fieldname="name", as_dict=False, **kwargs):
		self.calls.append(("get_value", doctype))
		if isinstance(filters, str):
			row = self.tables.get(doctype, {}).get(filters)
			row = dict(row, name=filters) if row is not None else None
		else:
			matches = [r for r in self._rows(doctype) if self._match(r, filters)]
			row = matches[0] if matches else None
		if row is None:
			return None
		if isinstance(fieldname, (list, tuple)):
			vals = {f: row.get(f) for f in fieldname}
			return frappe._dict(vals) if as_dict else tuple(vals.values())
		return row.get(fieldname)

	def exists(self, doctype, filters=None, **kwargs):
		self.calls.append(("exists", doctype))
		if isinstance(filters, str):
			return filters if filters in self.tables[doctype] else None
		for r in self._rows(doctype):
			if self._match(r, filters):
				return r["name"]
		return None

	def get_doc(self, doctype, name=None, *args, **kwargs):
		self.calls.append(("get_doc", doctype))
		row = self.tables.get(doctype, {}).get(name)
		if row is None:
			raise _NotFound(f"{doctype} {name} not found")
		doc = frappe._dict(dict(row, name=name))
		doc.as_dict = lambda: dict(row, name=name)
		return doc

	def db(self):
		db = MagicMock()
		db.get_value.side_effect = self.get_value
		db.exists.side_effect = self.exists
		db.has_column.return_value = True
		db.count.side_effect = lambda *a, **k: len(self.invoices)
		return db


class _Meta:
	def get_field(self, fieldname):
		return fieldname == "custom_kanban_profile"


def _invoice(name, state, posting_date, *, docstatus=1, customer="CUST-1", territory="T1",
	address="ADDR-1", modified=None, **extra):
	row = {
		"name": name,
		"customer": customer,
		"customer_name": f"Name {customer}",
		"territory": territory,
		"posting_date": posting_date,
		"posting_time": datetime.timedelta(hours=10),
		"creation": f"{posting_date} 10:00:00",
		"grand_total": 100.0,
		"net_total": 90.0,
		"total_taxes_and_charges": 10.0,
		"status": "Paid",
		"custom_sales_invoice_state": state,
		"sales_invoice_state": None,
		"sales_partner": None,
		"pos_profile": "Main",
		"custom_kanban_profile": "Main",
		"modified": modified or f"{posting_date} 12:00:00",
		"outstanding_amount": 0.0,
		"docstatus": docstatus,
		"is_return": 0,
		"custom_acceptance_status": "Accepted",
		"custom_payment_method": "Cash",
		"custom_delivery_date": posting_date,
		"custom_delivery_time_from": "14:00:00",
		"custom_delivery_duration": 3600,
		"shipping_address_name": address,
		"customer_address": None,
		"custom_shipping_expense": 0,
		"custom_sub_territory": None,
		"custom_delivery_trip": None,
		"custom_shipping_override": 0,
		"custom_shipping_override_status": None,
		"remarks": "",
		"woo_order_id": None,
	}
	row.update(extra)
	return row


def build_world() -> FakeWorld:
	w = FakeWorld()
	t = w.tables
	t["Territory"]["T1"] = {
		"territory_name": "Nasr City",
		"custom_territory_name_ar": "مدينة نصر",
		"shipping_expense": 40,
		"parent_territory": None,
	}
	t["Territory"]["T2"] = {
		"territory_name": "Maadi",
		"custom_territory_name_ar": "",
		"shipping_expense": 60,
		"parent_territory": None,
	}
	t["Territory"]["T2-SUB"] = {
		"territory_name": "Maadi Degla",
		"custom_territory_name_ar": "",
		"parent_territory": "T2",
	}
	t["Territory"]["EGNASR"] = {"territory_name": "Nasr", "custom_territory_name_ar": "نصر"}
	# Phones: one customer per lookup step, plus nobody-has-one.
	t["Customer"]["CUST-1"] = {"mobile_no": "0100", "phone": "0200"}
	t["Customer"]["CUST-2"] = {"mobile_no": None, "phone": "0222"}
	t["Customer"]["CUST-3"] = {"customer_primary_contact": "CON-3"}
	# Both numbers set: the lookup order decides which one the card shows.
	t["Contact"]["CON-3"] = {"mobile_no": "0313", "phone": "0333"}
	t["Customer"]["CUST-4"] = {"customer_primary_address": "ADDR-4"}
	t["Address"]["ADDR-4"] = {"address_line1": "x", "phone_no": "0444", "mobile_no": "0499"}
	t["Customer"]["CUST-5"] = {}
	t["Dynamic Link"]["DL-5"] = {
		"link_doctype": "Customer",
		"link_name": "CUST-5",
		"parenttype": "Contact",
		"parent": "CON-5",
	}
	t["Contact"]["CON-5"] = {"mobile_no": "0555"}
	t["Customer"]["CUST-6"] = {}
	# Primary contact points at a Contact that is gone: step 3 must not answer.
	t["Customer"]["CUST-7"] = {
		"customer_primary_contact": "CON-GONE",
		"customer_primary_address": "ADDR-7",
	}
	t["Address"]["ADDR-7"] = {"address_line1": "y", "phone": "0777"}
	# Addresses: plain, with line 2, with a Woo area code as city, missing one.
	t["Address"]["ADDR-1"] = {"address_line1": " 12  Abbas St ", "address_line2": None, "city": None}
	t["Address"]["ADDR-2"] = {"address_line1": "Villa 4", "address_line2": "Floor 2", "city": "EGNASR"}
	t["Address"]["ADDR-3"] = {"address_line1": "", "address_line2": "", "city": "Unknown City"}
	# Courier rows: one unsettled, one settled, one with no status at all.
	w.invoices_spec = []

	rows = []
	# Newest first, as the real query orders them.
	rows.append(_invoice("SI-A", "Received", _d(0), customer="CUST-1",
		custom_shipping_override=50, custom_shipping_override_status="Approved"))
	rows.append(_invoice("SI-B", "Received", _d(0), customer="CUST-2", address="ADDR-2"))
	rows.append(_invoice("SI-C", "Out for Delivery", _d(0), customer="CUST-3", territory="T2",
		custom_sub_territory="T2-SUB", custom_shipping_expense=75))
	for i in range(34):
		rows.append(_invoice(f"SI-DL{i:02d}", "Delivered", _d(i % 3), customer=f"CUST-{1 + i % 7}",
			modified=f"{_d(i % 3)} {10 + i % 10}:{i:02d}:00", address=["ADDR-1", "ADDR-2", "ADDR-3", "ADDR-GONE"][i % 4]))
	rows.append(_invoice("SI-H", "Cancelled", _d(1), docstatus=2, customer="CUST-4"))
	rows.append(_invoice("SI-C2", "Cancelled", _d(2), docstatus=2, customer="CUST-6"))
	rows.append(_invoice("SI-E", "In Progress", _d(5), customer="CUST-5", address=None))
	rows.append(_invoice("SI-R", "Ready", _d(6), customer="CUST-7"))
	rows.append(_invoice("SI-RT", "Returned", _d(9), customer="CUST-1"))
	rows.append(_invoice("SI-PK", "Received", _d(9), customer="CUST-2", custom_is_pickup=1))
	for i in range(20):
		rows.append(_invoice(f"SI-OLD{i:02d}", "Delivered", _d(10 + i), customer=f"CUST-{1 + i % 7}",
			custom_shipping_override=33, custom_shipping_override_status="Approved"))
	for i in range(10):
		rows.append(_invoice(f"SI-OC{i:02d}", "Cancelled", _d(10 + i), docstatus=2))
	w.invoices = [frappe._dict(r) for r in rows]

	for r in rows:
		for j in range(3):
			w.items.append({"parent": r["name"], "item_code": f"IT-{j}", "item_name": f"Item {j}",
				"qty": 1, "rate": 30, "amount": 30})
		w.taxes.append({"parent": r["name"], "parenttype": "Sales Invoice",
			"description": "Shipping Income (T1)", "tax_amount": 25})
	t["Courier Transaction"]["CT-1"] = {"reference_invoice": "SI-C", "status": "Unsettled"}
	t["Courier Transaction"]["CT-2"] = {"reference_invoice": "SI-DL00", "status": "Settled"}
	t["Courier Transaction"]["CT-3"] = {"reference_invoice": "SI-DL01", "status": None}
	t["Courier Transaction"]["CT-4"] = {"reference_invoice": "SI-OLD03", "status": "Unsettled"}
	return w


class _BoardHarness:
	"""Runs get_kanban_invoices against a FakeWorld with every outside read stubbed."""

	def __init__(self, world: FakeWorld):
		self.world = world
		self.spy_names: list[list[str]] = []

	def run(self, filters=None, legacy=False):
		w = self.world
		saved_cache = getattr(frappe.local, "cache", None)
		frappe.local.cache = {}
		conf = frappe._dict({"jarz_kanban_legacy_load": 1 if legacy else 0})

		def _spy(names):
			self.spy_names.append(list(names))
			return {}

		patches = [
			patch("frappe.db", w.db()),
			patch("frappe.conf", conf),
			patch("frappe.get_all", side_effect=w.get_all),
			patch("frappe.get_doc", side_effect=w.get_doc),
			patch("frappe.get_meta", return_value=_Meta()),
			patch("frappe.log_error"),
			patch("frappe.utils.today", return_value=str(TODAY)),
			patch.object(kanban, "_get_current_user_pos_profiles", return_value=["Main"]),
			patch.object(kanban, "_get_state_field_options",
				return_value=["Received", "In Progress", "Out for Delivery", "Delivered", "Cancelled", "Returned"]),
			patch.object(kanban, "_find_customer_search_matches", return_value=[]),
			patch.object(kanban, "invoice_pin_columns", return_value=[]),
			patch.object(kanban, "get_address_pin_map", return_value={}),
			patch.object(kanban, "_get_active_payment_receipt_map", side_effect=_spy),
			patch.object(kanban, "_get_invoice_note_counts", return_value={}),
			patch.object(kanban, "_get_invoice_latest_notes", return_value={}),
			patch.object(kanban, "_get_actual_payment_method_map", return_value={}),
			patch.object(kanban, "_get_unsettled_customer_amount_map", return_value={}),
		]
		for p in patches:
			p.start()
		try:
			return get_kanban_invoices(filters)
		finally:
			for p in reversed(patches):
				p.stop()
			frappe.local.cache = saved_cache


def _trim_cancelled(result):
	"""What the fast path adds on purpose: the Cancelled column's 2-day/30-card trim."""
	out = copy.deepcopy(result)
	cards = out["data"].get("cancelled") or []
	out["data"]["cancelled"] = [c for c in cards if c["posting_date"] >= CUTOFF][:_KANBAN_TRIM_MAX_CARDS]
	out["card_count"] = sum(len(v) for v in out["data"].values())
	return out


class TestFastBoardMatchesOldBoard(unittest.TestCase):
	def setUp(self):
		self.world = build_world()

	def test_unfiltered_board_is_card_for_card_identical(self):
		legacy = _BoardHarness(build_world()).run(legacy=True)
		fast = _BoardHarness(build_world()).run(legacy=False)
		self.assertTrue(legacy["success"], legacy)
		self.assertTrue(fast["success"], fast)
		self.assertEqual(fast, _trim_cancelled(legacy))

	def test_fast_board_builds_only_the_visible_cards(self):
		legacy_h = _BoardHarness(build_world())
		legacy_h.run(legacy=True)
		fast_h = _BoardHarness(build_world())
		fast = fast_h.run(legacy=False)
		built_legacy = set(legacy_h.spy_names[0])
		built_fast = set(fast_h.spy_names[0])
		self.assertEqual(len(built_legacy), len(self.world.invoices))
		self.assertNotIn("SI-OLD05", built_fast)
		self.assertNotIn("SI-OC03", built_fast)
		# Old but still-live work is always built.
		for name in ("SI-E", "SI-R", "SI-RT", "SI-PK"):
			self.assertIn(name, built_fast)
		shown = {c["name"] for cards in fast["data"].values() for c in cards}
		self.assertTrue(shown <= built_fast)
		# The counts describe every matching order, not just the built ones.
		self.assertEqual(fast["total_matching"], len(self.world.invoices))

	def test_trimmed_columns_keep_recent_cards_capped_at_30(self):
		fast = _BoardHarness(build_world()).run()
		delivered = fast["data"]["delivered"]
		self.assertEqual(len(delivered), _KANBAN_TRIM_MAX_CARDS)
		self.assertTrue(all(c["posting_date"] >= CUTOFF for c in delivered))
		self.assertEqual({c["name"] for c in fast["data"]["cancelled"]}, {"SI-H", "SI-C2"})
		self.assertEqual([c["name"] for c in fast["data"]["returned"]], ["SI-RT"])
		self.assertIn("SI-R", {c["name"] for c in fast["data"]["in_progress"]})

	def test_filtered_board_is_never_trimmed_and_matches_old(self):
		filters = {"customer": "CUST-1"}
		legacy = _BoardHarness(build_world()).run(filters, legacy=True)
		fast = _BoardHarness(build_world()).run(filters, legacy=False)
		self.assertEqual(fast, legacy)
		self.assertIn("SI-OLD00", {c["name"] for c in fast["data"]["delivered"]})
		self.assertIn("SI-OC00", {c["name"] for c in fast["data"]["cancelled"]})

	def test_off_switch_restores_the_untrimmed_cancelled_column(self):
		legacy = _BoardHarness(build_world()).run(legacy=True)
		self.assertEqual(len(legacy["data"]["cancelled"]), 12)

	def test_every_card_field_is_populated_from_the_right_lookup(self):
		fast = _BoardHarness(build_world()).run()
		cards = {c["name"]: c for col in fast["data"].values() for c in col}
		self.assertEqual(cards["SI-A"]["customer_phone"], "0100")
		self.assertEqual(cards["SI-B"]["customer_phone"], "0222")
		self.assertEqual(cards["SI-C"]["customer_phone"], "0313")
		self.assertEqual(cards["SI-H"]["customer_phone"], "0444")
		self.assertEqual(cards["SI-E"]["customer_phone"], "0555")
		self.assertEqual(cards["SI-C2"]["customer_phone"], "")
		# Step 2's Contact is gone, so step 3 (the address phone) answers - via
		# the one-by-one path, exactly as before.
		self.assertEqual(cards["SI-R"]["customer_phone"], "0777")
		self.assertEqual(cards["SI-A"]["full_address"], "12 Abbas St")
		self.assertEqual(cards["SI-B"]["full_address"], "Villa 4, Floor 2, نصر")
		self.assertEqual(cards["SI-E"]["full_address"], "")
		self.assertTrue(cards["SI-C"]["has_unsettled_courier_txn"])
		self.assertFalse(cards["SI-DL00"]["has_unsettled_courier_txn"])
		self.assertFalse(cards["SI-DL01"]["has_unsettled_courier_txn"])
		self.assertEqual(len(cards["SI-A"]["items"]), 3)
		self.assertEqual(cards["SI-C"]["sub_territory_display"], "Maadi Degla")
		self.assertTrue(cards["SI-C"]["has_sub_territories"])
		self.assertEqual(cards["SI-A"]["territory_name_ar"], "مدينة نصر")


class TestShippingExpenseDoesNotLeakBetweenCards(unittest.TestCase):
	def test_override_on_one_order_stays_on_that_order(self):
		"""SI-A's approved 50 override used to be written into the territory cache,
		so SI-B (same territory, no override) showed 50 instead of the 40 rate."""
		for legacy in (True, False):
			fast = _BoardHarness(build_world()).run(legacy=legacy)
			cards = {c["name"]: c for col in fast["data"].values() for c in col}
			self.assertEqual(cards["SI-A"]["shipping_expense"], 50.0)
			self.assertEqual(cards["SI-B"]["shipping_expense"], 40.0)
			self.assertEqual(cards["SI-C"]["shipping_expense"], 75.0)
			self.assertEqual(cards["SI-PK"]["shipping_expense"], 0.0)


class TestItemsAreNeverCapped(unittest.TestCase):
	def test_items_query_has_no_row_cap(self):
		world = build_world()
		_BoardHarness(world).run()
		limits = [c[2] for c in world.calls if c[:2] == ("get_all", "Sales Invoice Item")]
		self.assertEqual(limits, [0])


class TestBulkHelpersMatchTheOneByOneHelpers(unittest.TestCase):
	def _with_world(self, fn):
		w = build_world()
		saved_cache = getattr(frappe.local, "cache", None)
		frappe.local.cache = {}
		with patch("frappe.db", w.db()), patch("frappe.get_all", side_effect=w.get_all), patch(
			"frappe.get_doc", side_effect=w.get_doc
		), patch("frappe.log_error"):
			try:
				return fn(w)
			finally:
				frappe.local.cache = saved_cache

	def test_prefetched_phones_equal_the_one_by_one_answer(self):
		customers = [f"CUST-{i}" for i in range(1, 8)] + ["CUST-NOPE", ""]

		def one_by_one(_w):
			return {c: _resolve_customer_phone(c) for c in customers}

		def prefetched(_w):
			_prefetch_customer_phones(customers)
			return {c: _resolve_customer_phone(c) for c in customers}

		self.assertEqual(self._with_world(prefetched), self._with_world(one_by_one))

	def test_prefetch_leaves_unresolved_customers_to_the_full_path(self):
		def run(_w):
			_prefetch_customer_phones(["CUST-5", "CUST-6", "CUST-7", "CUST-1"])
			return dict(frappe.local.cache)

		cache = self._with_world(run)
		self.assertEqual(cache, {"cust_phone::CUST-1": "0100"})

	def test_prefetch_failure_changes_nothing(self):
		def run(w):
			with patch("frappe.get_all", side_effect=RuntimeError("db down")):
				_prefetch_customer_phones(["CUST-1"])
			return dict(frappe.local.cache)

		self.assertEqual(self._with_world(run), {})

	def test_address_map_equals_get_address_details(self):
		names = ["ADDR-1", "ADDR-2", "ADDR-3", "ADDR-4", "ADDR-GONE", None, ""]

		def bulk(_w):
			return _get_address_details_map(names)

		def one_by_one(_w):
			return {n: invoice_utils.get_address_details(n) for n in names if n}

		self.assertEqual(self._with_world(bulk), self._with_world(one_by_one))

	def test_format_address_row_matches_get_address_details(self):
		rows = [
			{"address_line1": "  a\tb ", "address_line2": " c ", "city": "EGNASR"},
			{"address_line1": None, "address_line2": None, "city": "Plain"},
			{"address_line1": "", "address_line2": "x", "city": ""},
			{"address_line1": ", lead", "address_line2": None, "city": None},
		]
		for i, row in enumerate(rows):
			w = build_world()
			w.tables["Address"][f"R{i}"] = row
			with patch("frappe.db", w.db()), patch("frappe.get_doc", side_effect=w.get_doc):
				self.assertEqual(
					_format_address_row(frappe._dict(row)),
					invoice_utils.get_address_details(f"R{i}"),
				)

	def test_courier_set_matches_exists(self):
		names = ["SI-C", "SI-DL00", "SI-DL01", "SI-OLD03", "SI-A"]

		def run(w):
			bulk = _get_unsettled_courier_invoice_set(names)
			one = {
				n
				for n in names
				if frappe.db.exists("Courier Transaction", {"reference_invoice": n, "status": ["!=", "Settled"]})
			}
			return bulk, one

		bulk, one = self._with_world(run)
		self.assertEqual(bulk, one)
		self.assertEqual(bulk, {"SI-C", "SI-OLD03"})

	def test_courier_set_failure_returns_none(self):
		with patch("frappe.get_all", side_effect=RuntimeError("boom")):
			self.assertIsNone(_get_unsettled_courier_invoice_set(["SI-1"]))


class TestVisibleWindow(unittest.TestCase):
	def test_only_old_finished_rows_are_dropped(self):
		rows = [
			frappe._dict(name="a", custom_sales_invoice_state="Delivered", posting_date=_d(3)),
			frappe._dict(name="b", custom_sales_invoice_state="Delivered", posting_date=_d(2)),
			frappe._dict(name="c", custom_sales_invoice_state="Cancelled", posting_date=_d(30)),
			frappe._dict(name="d", custom_sales_invoice_state="Returned", posting_date=_d(30)),
			frappe._dict(name="e", custom_sales_invoice_state="Ready", posting_date=_d(30)),
			frappe._dict(name="f", custom_sales_invoice_state=None, posting_date=_d(30)),
			frappe._dict(name="g", sales_invoice_state="Delivered", posting_date=_d(30)),
		]
		kept = _rows_in_visible_window(rows, ("delivered", "cancelled"), CUTOFF)
		self.assertEqual([r.name for r in kept], ["b", "d", "e", "f"])


if __name__ == "__main__":
	unittest.main()

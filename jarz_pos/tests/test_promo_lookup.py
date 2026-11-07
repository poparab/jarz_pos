"""Promo-code lookup for POS staff -- ``jarz_pos.api.promo.lookup_promo_code``.

Pins the response contract the Flutter client is written against: the status
precedence, the scope labels, the remaining-uses arithmetic, and the
``order_check`` block that evaluates the one looked-up code against a real
order through the unmodified promo engine (``evaluate_promo_codes``).

Site-less: a small in-memory ``frappe`` stand-in is patched into both the API
module and the promo engine, so the engine's own gates run for real. A stub
``frappe`` is registered when frappe is not installed.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

try:  # pragma: no cover - depends on the environment
	import frappe  # noqa: F401
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
	_utils.now_datetime = lambda: datetime.datetime(2026, 11, 7, 12, 0)
	_utils.nowdate = lambda: "2026-11-07"
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.api import promo as api
from jarz_pos.services import promo_codes as engine

NOW = datetime.datetime(2026, 11, 7, 12, 0)


class _Thrown(Exception):
	pass


class _Doc(SimpleNamespace):
	"""Document stand-in: attributes plus ``.get`` like frappe.model.Document."""

	def get(self, key, default=None):
		return getattr(self, key, default)


def _promo(name, **overrides):
	data = {
		"name": name,
		"code": name,
		"title": None,
		"enabled": 1,
		"discount_type": "Percentage",
		"discount_value": 10.0,
		"max_discount_amount": 0,
		"min_order_amount": 0,
		"min_on_whole_order": 0,
		"applies_to": "Whole Order",
		"item_scope": [],
		"company": None,
		"allow_pickup": 0,
		"channels": [],
		"stackable": 0,
		"priority": 0,
		"usage_limit": 0,
		"per_customer_limit": 0,
		"times_used": 0,
		"active_from": None,
		"active_to": None,
		"customer": None,
		"customer_phone": None,
		"woo_coupon_code": None,
		"woo_coupon_id": None,
		"description": None,
	}
	data.update(overrides)
	return _Doc(**data)


def _line(item_code, amount, item_group="Jars", qty=1):
	return SimpleNamespace(
		item_code=item_code,
		item_group=item_group,
		qty=qty,
		rate=amount / qty if qty else 0,
		price_list_rate=amount / qty if qty else 0,
		discount_percentage=0,
		amount=amount,
	)


def _invoice(name, **overrides):
	data = {
		"name": name,
		"company": None,
		"customer": "CUST-1",
		"customer_name": "Mona Ali",
		"contact_mobile": None,
		"pos_profile": "Zayed",
		"woo_order_id": 0,
		"docstatus": 1,
		"status": "Unpaid",
		"is_return": 0,
		"custom_is_pickup": 0,
		"custom_promo_codes": None,
		"custom_promo_discount_total": 0,
		"grand_total": 500.0,
		"items": [_line("LOTUS-L", 300.0), _line("TIRAMISU-L", 150.0)],
		"taxes": [SimpleNamespace(description="Shipping Income (Zayed)", tax_amount=50.0)],
		"creation": "2026-11-06 10:00:00",
	}
	data.update(overrides)
	return _Doc(**data)


def _ci(a, b):
	return a is not None and b is not None and str(a).lower() == str(b).lower()


class _FakeDB:
	def __init__(self, site):
		self.site = site

	def get_value(self, doctype, filters, fieldname=None, as_dict=False):
		if doctype == "Jarz Promo Code":
			(field_name, value), = filters.items()
			for promo in self.site.promos.values():
				if _ci(getattr(promo, field_name, None), value):
					return promo.name
			return None
		if doctype == "Item":
			return self.site.item_names.get(filters)
		if doctype == "Customer":
			customer = self.site.customers.get(filters)
			return customer.get(fieldname) if customer else None
		return None

	def exists(self, doctype, name):
		if doctype == "Sales Invoice":
			for inv_name in self.site.invoices:
				if _ci(inv_name, name):
					return inv_name
		return None

	def count(self, doctype, filters):
		return len(self.site.redemptions_matching(filters))


class _FakeSite:
	def __init__(self):
		self.promos: dict = {}
		self.invoices: dict = {}
		self.redemptions: list = []
		self.item_names: dict = {}
		self.customers: dict = {"CUST-1": {"customer_name": "Mona Ali", "mobile_no": "01000000001"}}
		self.db = _FakeDB(self)
		self.utils = SimpleNamespace(now_datetime=lambda: NOW, get_datetime=lambda v: v)
		self.get_doc_calls: list = []

	# -- frappe surface -----------------------------------------------------
	def _(self, msg):
		return msg

	def throw(self, msg, *a, **k):
		raise _Thrown(msg)

	def log_error(self, *a, **k):
		return None

	def get_traceback(self, *a, **k):
		return ""

	def get_cached_value(self, doctype, name, field_name):
		return None

	def get_doc(self, doctype, name):
		self.get_doc_calls.append((doctype, name))
		if doctype == "Jarz Promo Code":
			for promo in self.promos.values():
				if _ci(promo.name, name):
					return promo
			raise KeyError(name)
		if doctype == "Sales Invoice":
			return self.invoices[name]
		if doctype == "Customer":
			return _Doc(name=name, **self.customers.get(name, {}))
		raise KeyError(doctype)

	def get_all(self, doctype, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None):
		if doctype == "Jarz Promo Redemption":
			rows = self.redemptions_matching(filters)
			rows = sorted(rows, key=lambda r: r["creation"], reverse=True)
			return [dict(r) for r in rows[: limit_page_length or None]]
		if doctype == "Sales Invoice":
			if "name" in filters:
				names = filters["name"][1]
				return [
					{
						"name": inv.name,
						"customer_name": inv.customer_name,
						"posting_date": getattr(inv, "posting_date", None),
						"woo_order_id": inv.woo_order_id,
					}
					for inv in self.invoices.values()
					if inv.name in names
				]
			matches = [
				inv
				for inv in self.invoices.values()
				if inv.woo_order_id == filters["woo_order_id"]
				and inv.docstatus == filters["docstatus"]
				and int(inv.is_return or 0) == filters.get("is_return", 0)
			]
			matches.sort(key=lambda inv: inv.creation, reverse=True)
			if limit_page_length:
				matches = matches[:limit_page_length]
			return [inv.name for inv in matches] if pluck == "name" else matches
		return []

	# -- helpers ------------------------------------------------------------
	def redemptions_matching(self, filters):
		out = []
		for row in self.redemptions:
			ok = True
			for key, want in (filters or {}).items():
				have = row.get(key)
				if isinstance(want, list) and want and want[0] == "!=":
					ok = ok and have != want[1]
				else:
					ok = ok and have == want
			if ok:
				out.append(row)
		return out

	def redeem(self, code, invoice, *, status="Active", customer="CUST-X", creation="2026-11-01 10:00:00", woo=None):
		self.redemptions.append(
			{
				"promo_code": code,
				"sales_invoice": invoice,
				"customer": customer,
				"status": status,
				"creation": creation,
				"woo_order_id": woo,
			}
		)


class _Base(unittest.TestCase):
	def setUp(self):
		self.site = _FakeSite()
		for target in ("jarz_pos.api.promo.frappe", "jarz_pos.services.promo_codes.frappe"):
			patcher = mock.patch(target, self.site)
			patcher.start()
			self.addCleanup(patcher.stop)

	def lookup(self, code, order=None):
		return api.lookup_promo_code(code=code, order=order)


class TestFinding(_Base):
	def test_unknown_code_is_not_found_and_upper_cased(self):
		self.assertEqual(self.lookup(" nope10 "), {"found": False, "code": "NOPE10"})

	def test_empty_code_throws(self):
		with self.assertRaises(_Thrown):
			self.lookup("   ")

	def test_matches_woo_coupon_code_case_insensitively(self):
		self.site.promos["BACK42R4FV"] = _promo("BACK42R4FV", woo_coupon_code="welcome-back", woo_coupon_id="9911")
		out = self.lookup("Welcome-BACK")
		self.assertTrue(out["found"])
		self.assertEqual(out["code"], "BACK42R4FV")
		self.assertEqual(out["woo_coupon_code"], "welcome-back")
		self.assertTrue(out["on_website"])

	def test_matches_name_case_insensitively(self):
		self.site.promos["JARZ10"] = _promo("JARZ10")
		out = self.lookup("jarz10")
		self.assertTrue(out["found"])
		self.assertEqual(out["code"], "JARZ10")
		self.assertFalse(out["on_website"])
		self.assertIsNone(out["order_check"])

	def test_full_shape(self):
		self.site.promos["JARZ10"] = _promo(
			"JARZ10",
			title="Ten off",
			max_discount_amount=75,
			min_order_amount=300,
			min_on_whole_order=1,
			channels=[SimpleNamespace(channel="Woo"), SimpleNamespace(channel="flutter")],
			allow_pickup=1,
			stackable=1,
			per_customer_limit=1,
			active_from=datetime.datetime(2026, 11, 1, 9, 30),
			active_to=datetime.datetime(2026, 11, 30, 23, 59, 59),
			customer="CUST-1",
			customer_phone="01000000001",
		)
		out = self.lookup("JARZ10")
		expected_keys = {
			"found", "code", "title", "description", "status", "discount_type", "discount_value",
			"max_discount_amount", "min_order_amount", "min_on_whole_order", "applies_to", "scope",
			"channels", "allow_pickup", "stackable", "usage_limit", "times_used", "remaining_uses",
			"per_customer_limit", "active_from", "active_to", "customer", "customer_name",
			"customer_phone", "woo_coupon_code", "on_website", "recent_uses", "order_check",
		}
		self.assertEqual(set(out), expected_keys)
		self.assertEqual(out["title"], "Ten off")
		self.assertEqual(out["status"], "active")
		self.assertEqual(out["channels"], ["woo", "flutter"])
		self.assertEqual(out["active_from"], "2026-11-01 09:30:00")
		self.assertEqual(out["active_to"], "2026-11-30 23:59:59")
		self.assertEqual(out["customer_name"], "Mona Ali")
		self.assertTrue(out["min_on_whole_order"])
		self.assertTrue(out["allow_pickup"])
		self.assertEqual(out["max_discount_amount"], 75.0)
		self.assertEqual(out["per_customer_limit"], 1)
		self.assertEqual(out["scope"], [])


class TestStatus(_Base):
	def _status(self, **overrides):
		self.site.promos["P"] = _promo("P", **overrides)
		return self.lookup("P")["status"]

	def test_active(self):
		self.assertEqual(self._status(), "active")

	def test_disabled_wins_over_everything(self):
		self.assertEqual(self._status(enabled=0, active_to=NOW - datetime.timedelta(days=3)), "disabled")

	def test_not_started(self):
		self.assertEqual(self._status(active_from=NOW + datetime.timedelta(hours=1)), "not_started")

	def test_not_started_wins_over_expired(self):
		self.assertEqual(
			self._status(
				active_from=NOW + datetime.timedelta(days=1), active_to=NOW - datetime.timedelta(days=1)
			),
			"not_started",
		)

	def test_expired(self):
		self.assertEqual(self._status(active_to=NOW - datetime.timedelta(minutes=1)), "expired")

	def test_expired_wins_over_used_up(self):
		self.site.redeem("P", "SI-1")
		self.assertEqual(self._status(usage_limit=1, active_to=NOW - datetime.timedelta(days=1)), "expired")

	def test_used_up_counts_active_redemptions_not_the_stored_counter(self):
		self.site.redeem("P", "SI-1")
		self.site.redeem("P", "SI-2", status="Reversed")
		# Stored counter says 5, but only one redemption is Active.
		self.assertEqual(self._status(usage_limit=2, times_used=5), "active")
		self.site.redeem("P", "SI-3")
		self.assertEqual(self.lookup("P")["status"], "used_up")


class TestUsage(_Base):
	def test_unlimited_has_no_remaining(self):
		self.site.promos["P"] = _promo("P")
		self.site.redeem("P", "SI-1")
		out = self.lookup("P")
		self.assertEqual(out["usage_limit"], 0)
		self.assertEqual(out["times_used"], 1)
		self.assertIsNone(out["remaining_uses"])

	def test_remaining_uses_math(self):
		self.site.promos["P"] = _promo("P", usage_limit=5, times_used=99)
		self.site.redeem("P", "SI-1")
		self.site.redeem("P", "SI-2")
		self.site.redeem("P", "SI-3", status="Reversed")
		out = self.lookup("P")
		self.assertEqual(out["times_used"], 2)
		self.assertEqual(out["remaining_uses"], 3)

	def test_remaining_never_negative(self):
		self.site.promos["P"] = _promo("P", usage_limit=1)
		self.site.redeem("P", "SI-1")
		self.site.redeem("P", "SI-2")
		self.assertEqual(self.lookup("P")["remaining_uses"], 0)

	def test_recent_uses_newest_five(self):
		self.site.promos["P"] = _promo("P")
		self.site.invoices["SI-9"] = _invoice(
			"SI-9", woo_order_id=21471, customer_name="Sara", posting_date=datetime.date(2026, 11, 5)
		)
		for day in range(1, 8):
			self.site.redeem("P", f"SI-{day}", creation=f"2026-11-0{day} 10:00:00", customer="CUST-1")
		self.site.redeem("P", "SI-9", creation="2026-11-08 09:00:00", status="Reversed", woo="21471")
		uses = self.lookup("P")["recent_uses"]
		self.assertEqual(len(uses), 5)
		self.assertEqual(
			uses[0],
			{"invoice": "SI-9", "woo_order_id": 21471, "customer_name": "Sara", "date": "2026-11-05", "status": "Reversed"},
		)
		# No invoice row: falls back to the redemption's customer and creation date.
		self.assertEqual(
			uses[1],
			{"invoice": "SI-7", "woo_order_id": None, "customer_name": "Mona Ali", "date": "2026-11-07", "status": "Active"},
		)
		self.assertEqual([u["invoice"] for u in uses], ["SI-9", "SI-7", "SI-6", "SI-5", "SI-4"])


class TestScope(_Base):
	def test_specific_items_lists_item_names(self):
		self.site.item_names = {"LOTUS-L": "Lotus Large Jar"}
		self.site.promos["P"] = _promo(
			"P",
			applies_to="Specific Items",
			item_scope=[
				SimpleNamespace(scope_type="Item", item_code="LOTUS-L", item_group=None),
				SimpleNamespace(scope_type="Item", item_code="NO-NAME", item_group=None),
				SimpleNamespace(scope_type="Item Group", item_code=None, item_group="Ignored"),
			],
		)
		out = self.lookup("P")
		self.assertEqual(out["applies_to"], "Specific Items")
		self.assertEqual(
			out["scope"],
			[
				{"type": "Item", "name": "LOTUS-L", "label": "Lotus Large Jar"},
				{"type": "Item", "name": "NO-NAME", "label": "NO-NAME"},
			],
		)

	def test_item_groups_label_is_the_group(self):
		self.site.promos["P"] = _promo(
			"P",
			applies_to="Item Groups",
			item_scope=[SimpleNamespace(scope_type="Item Group", item_code=None, item_group="Large Jars")],
		)
		self.assertEqual(
			self.lookup("P")["scope"], [{"type": "Item Group", "name": "Large Jars", "label": "Large Jars"}]
		)

	def test_whole_order_has_no_scope(self):
		self.site.promos["P"] = _promo(
			"P", item_scope=[SimpleNamespace(scope_type="Item", item_code="LOTUS-L", item_group=None)]
		)
		self.assertEqual(self.lookup("P")["scope"], [])


class TestOrderCheck(_Base):
	def test_percentage_accepted_replaces_current_promo(self):
		self.site.promos["JARZ10"] = _promo("JARZ10", discount_value=10)
		# 450 of lines + 50 shipping - 30 existing promo = 470.
		self.site.invoices["ACC-SINV-2026-19393"] = _invoice(
			"ACC-SINV-2026-19393",
			woo_order_id=21471,
			grand_total=470.0,
			custom_promo_codes='["OLD30"]',
			custom_promo_discount_total=30.0,
		)
		check = self.lookup("jarz10", order="21471")["order_check"]
		self.assertEqual(check["invoice"], "ACC-SINV-2026-19393")
		self.assertEqual(check["woo_order_id"], 21471)
		self.assertEqual(check["customer_name"], "Mona Ali")
		self.assertEqual(check["docstatus"], 1)
		self.assertEqual(check["invoice_status"], "Unpaid")
		self.assertEqual(check["current_total"], 470.0)
		self.assertEqual(check["current_codes"], ["OLD30"])
		self.assertEqual(check["current_promo_discount"], 30.0)
		self.assertEqual(check["delivery_amount"], 50.0)
		self.assertTrue(check["accepted"])
		self.assertIsNone(check["reason"])
		self.assertEqual(check["discount_amount"], 45.0)
		self.assertFalse(check["free_delivery"])
		self.assertEqual(check["new_total"], 455.0)  # 470 + 30 - 45

	def test_free_delivery_removes_the_shipping_row(self):
		self.site.promos["SHIPFREE"] = _promo("SHIPFREE", discount_type="Free Delivery", discount_value=0)
		self.site.invoices["SI-1"] = _invoice("SI-1", grand_total=500.0)
		check = self.lookup("SHIPFREE", order="SI-1")["order_check"]
		self.assertTrue(check["accepted"])
		self.assertTrue(check["free_delivery"])
		self.assertEqual(check["discount_amount"], 0.0)
		self.assertEqual(check["new_total"], 450.0)

	def test_rejected_code_leaves_the_total(self):
		self.site.promos["BIG"] = _promo("BIG", min_order_amount=1000)
		self.site.invoices["SI-1"] = _invoice("SI-1", grand_total=470.0, custom_promo_discount_total=30.0)
		check = self.lookup("BIG", order="SI-1")["order_check"]
		self.assertFalse(check["accepted"])
		self.assertIn("minimum", check["reason"])
		self.assertEqual(check["discount_amount"], 0.0)
		self.assertEqual(check["new_total"], 470.0)

	def test_channel_follows_the_before_validate_hook(self):
		# No woo_order_id and no order channel -> "desk", as apply_promo_codes_before_validate.
		self.site.promos["WOOONLY"] = _promo("WOOONLY", channels=[SimpleNamespace(channel="woo")])
		self.site.invoices["SI-1"] = _invoice("SI-1")
		check = self.lookup("WOOONLY", order="SI-1")["order_check"]
		self.assertFalse(check["accepted"])
		self.assertEqual(check["reason"], "not valid for channel 'desk'")
		self.site.invoices["SI-2"] = _invoice("SI-2", woo_order_id=777)
		self.assertTrue(self.lookup("WOOONLY", order="777")["order_check"]["accepted"])

	def test_pos_app_order_evaluates_as_flutter(self):
		# create_pos_invoice evaluates POS-app orders as "flutter" and stamps
		# custom_order_channel, so the lookup must use it.
		self.site.promos["APPONLY"] = _promo("APPONLY", channels=[SimpleNamespace(channel="flutter")])
		self.site.invoices["SI-3"] = _invoice("SI-3", custom_order_channel="Flutter")
		self.assertTrue(self.lookup("APPONLY", order="SI-3")["order_check"]["accepted"])
		self.site.invoices["SI-4"] = _invoice("SI-4")
		check = self.lookup("APPONLY", order="SI-4")["order_check"]
		self.assertFalse(check["accepted"])
		self.assertEqual(check["reason"], "not valid for channel 'desk'")

	def test_pickup_order_rejects_a_delivery_only_code(self):
		self.site.promos["P"] = _promo("P", allow_pickup=0)
		self.site.invoices["SI-1"] = _invoice("SI-1", custom_is_pickup=1)
		check = self.lookup("P", order="SI-1")["order_check"]
		self.assertFalse(check["accepted"])
		self.assertEqual(check["reason"], "not valid for pickup orders")

	def test_own_redemption_does_not_use_up_a_single_use_code(self):
		self.site.promos["ONCE"] = _promo("ONCE", usage_limit=1, discount_type="Fixed Amount", discount_value=40)
		self.site.invoices["SI-1"] = _invoice(
			"SI-1", grand_total=460.0, custom_promo_codes='["ONCE"]', custom_promo_discount_total=40.0
		)
		self.site.redeem("ONCE", "SI-1")
		out = self.lookup("ONCE", order="SI-1")
		self.assertEqual(out["status"], "used_up")
		check = out["order_check"]
		self.assertTrue(check["accepted"])
		self.assertEqual(check["discount_amount"], 40.0)
		self.assertEqual(check["new_total"], 460.0)

	def test_woo_number_prefers_submitted_then_draft(self):
		self.site.promos["P"] = _promo("P")
		self.site.invoices["SI-OLD"] = _invoice("SI-OLD", woo_order_id=500, docstatus=2, creation="2026-11-01")
		self.site.invoices["SI-DRAFT"] = _invoice("SI-DRAFT", woo_order_id=500, docstatus=0, creation="2026-11-03")
		self.site.invoices["SI-LIVE"] = _invoice("SI-LIVE", woo_order_id=500, docstatus=1, creation="2026-11-02")
		self.site.invoices["SI-RET"] = _invoice(
			"SI-RET", woo_order_id=500, docstatus=1, is_return=1, creation="2026-11-04"
		)
		self.assertEqual(self.lookup("P", order="#500")["order_check"]["invoice"], "SI-LIVE")
		del self.site.invoices["SI-LIVE"]
		self.assertEqual(self.lookup("P", order="500")["order_check"]["invoice"], "SI-DRAFT")

	def test_order_not_found(self):
		self.site.promos["P"] = _promo("P")
		self.assertEqual(
			self.lookup("P", order=" 99999 ")["order_check"], {"error": "order_not_found", "order": "99999"}
		)
		self.assertEqual(
			self.lookup("P", order="ACC-SINV-NOPE")["order_check"],
			{"error": "order_not_found", "order": "ACC-SINV-NOPE"},
		)

	def test_never_saves_the_invoice(self):
		self.site.promos["P"] = _promo("P")
		inv = _invoice("SI-1")
		inv.save = mock.Mock()
		inv.db_set = mock.Mock()
		self.site.invoices["SI-1"] = inv
		self.lookup("P", order="SI-1")
		inv.save.assert_not_called()
		inv.db_set.assert_not_called()
		self.assertEqual(inv.grand_total, 500.0)
		self.assertEqual(len(inv.taxes), 1)

	def test_new_total_never_negative(self):
		self.site.promos["P"] = _promo("P", discount_type="Fixed Amount", discount_value=1000)
		# Odd invoice: header discounts outside the promo engine already took it near 0.
		self.site.invoices["SI-1"] = _invoice("SI-1", grand_total=5.0)
		check = self.lookup("P", order="SI-1")["order_check"]
		self.assertTrue(check["accepted"])
		self.assertEqual(check["discount_amount"], 450.0)
		self.assertEqual(check["new_total"], 0.0)


if __name__ == "__main__":
	unittest.main()

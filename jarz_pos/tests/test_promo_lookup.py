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
		if doctype == "Jarz Promo Code" and isinstance(filters, str):
			promo = self.site.promos.get(filters)
			return getattr(promo, fieldname, None) if promo else None
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
		self.territories: dict = {}
		self.can_read = True

	def has_permission(self, doctype, ptype="read", doc=None, **kwargs):
		return self.can_read

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
		if doctype == "Territory":
			return _Doc(name=name, **self.territories[name])
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
		self.branch_ok = True

		def _branch(doc):
			if not self.branch_ok:
				raise _Thrown("not your branch")

		patcher = mock.patch("jarz_pos.api.promo._ensure_branch_access", _branch)
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


class TestOrderAccessAndDelivery(_Base):
	def test_order_without_read_permission_is_not_shown(self):
		self.site.promos["P"] = _promo("P", discount_type="Percentage", discount_value=10)
		self.site.invoices["SI-1"] = _invoice("SI-1", woo_order_id=900)
		self.site.can_read = False
		check = self.lookup("P", order="900")["order_check"]
		self.assertEqual(check, {"error": "not_permitted", "order": "900"})

	def test_order_from_another_branch_is_not_shown(self):
		self.site.promos["P"] = _promo("P", discount_type="Percentage", discount_value=10)
		self.site.invoices["SI-1"] = _invoice("SI-1")
		self.branch_ok = False
		check = self.lookup("P", order="SI-1")["order_check"]
		self.assertEqual(check["error"], "not_permitted")
		self.assertNotIn("customer_name", check)
		self.assertNotIn("current_total", check)

	def test_replacing_a_free_delivery_code_puts_the_fee_back(self):
		# Current code FREESHIP already removed the 50 shipping row: 450 total.
		self.site.promos["FREESHIP"] = _promo("FREESHIP", discount_type="Free Delivery", discount_value=0)
		self.site.promos["P10"] = _promo("P10", discount_type="Percentage", discount_value=10)
		self.site.territories["Zayed"] = {"shipping_income": 50.0}
		self.site.invoices["SI-1"] = _invoice(
			"SI-1", grand_total=450.0, taxes=[], territory="Zayed", custom_promo_codes='["FREESHIP"]'
		)
		check = self.lookup("P10", order="SI-1")["order_check"]
		self.assertTrue(check["accepted"])
		self.assertEqual(check["waived_delivery"], 50.0)
		self.assertEqual(check["discount_amount"], 45.0)
		self.assertEqual(check["new_total"], 455.0)  # 450 + 50 back - 45

	def test_order_delivery_override_wins_over_territory_fee(self):
		self.site.promos["FREESHIP"] = _promo("FREESHIP", discount_type="Free Delivery", discount_value=0)
		self.site.promos["F20"] = _promo("F20", discount_type="Fixed Amount", discount_value=20)
		self.site.territories["Zayed"] = {"shipping_income": 50.0}
		self.site.invoices["SI-1"] = _invoice(
			"SI-1", grand_total=450.0, taxes=[], territory="Zayed",
			custom_delivery_income=70.0, custom_promo_codes='["FREESHIP"]',
		)
		check = self.lookup("F20", order="SI-1")["order_check"]
		self.assertEqual(check["waived_delivery"], 70.0)
		self.assertEqual(check["new_total"], 500.0)  # 450 + 70 - 20

	def test_free_delivery_code_on_a_free_delivery_order_keeps_the_total(self):
		self.site.promos["FREESHIP"] = _promo("FREESHIP", discount_type="Free Delivery", discount_value=0)
		self.site.territories["Zayed"] = {"shipping_income": 50.0}
		self.site.invoices["SI-1"] = _invoice(
			"SI-1", grand_total=450.0, taxes=[], territory="Zayed", custom_promo_codes='["FREESHIP"]'
		)
		check = self.lookup("FREESHIP", order="SI-1")["order_check"]
		self.assertTrue(check["accepted"])
		self.assertEqual(check["new_total"], 450.0)

	def test_no_fee_restored_without_a_current_free_delivery_code(self):
		self.site.promos["P10"] = _promo("P10", discount_type="Percentage", discount_value=10)
		self.site.territories["Zayed"] = {"shipping_income": 50.0}
		self.site.invoices["SI-1"] = _invoice("SI-1", grand_total=450.0, taxes=[], territory="Zayed", custom_is_pickup=1)
		check = self.lookup("P10", order="SI-1")["order_check"]
		self.assertEqual(check["waived_delivery"], 0.0)


class TestLookupPromoCodeShapeUnchanged(_Base):
	"""``lookup_promo_code`` now splices the shared ``_code_summary_base``;
	its keys, their order and values must be exactly what they were."""

	def test_key_order_and_values(self):
		self.site.promos["JARZ10"] = _promo(
			"JARZ10",
			title="Ten off",
			usage_limit=3,
			per_customer_limit=1,
			customer="CUST-1",
			customer_phone="01000000001",
			woo_coupon_code="jarz10",
			woo_coupon_id="77",
			active_to=datetime.datetime(2026, 11, 30, 23, 59, 59),
		)
		self.site.redeem("JARZ10", "SI-1", customer="CUST-1")
		out = self.lookup("jarz10")
		self.assertEqual(
			list(out),
			[
				"found", "code", "title", "description", "status", "discount_type", "discount_value",
				"max_discount_amount", "min_order_amount", "min_on_whole_order", "applies_to", "scope",
				"channels", "allow_pickup", "stackable", "usage_limit", "times_used", "remaining_uses",
				"per_customer_limit", "active_from", "active_to", "customer", "customer_name",
				"customer_phone", "woo_coupon_code", "on_website", "recent_uses", "order_check",
			],
		)
		self.assertEqual(out["code"], "JARZ10")
		self.assertEqual(out["status"], "active")
		self.assertEqual(out["usage_limit"], 3)
		self.assertEqual(out["times_used"], 1)
		self.assertEqual(out["remaining_uses"], 2)
		self.assertEqual(out["customer"], "CUST-1")
		self.assertEqual(out["customer_name"], "Mona Ali")
		self.assertEqual(out["customer_phone"], "01000000001")
		self.assertTrue(out["on_website"])
		self.assertEqual(out["woo_coupon_code"], "jarz10")
		self.assertNotIn("personal", out)
		self.assertNotIn("customer_times_used", out)


# ---------------------------------------------------------------------------
# lookup_customer_promos (feature promo-customer-lookup)
# ---------------------------------------------------------------------------

LATER = NOW + datetime.timedelta(days=30)
EARLIER = NOW - datetime.timedelta(days=3)

_SUMMARY_KEYS = {
	"code", "title", "description", "status", "personal", "discount_type", "discount_value",
	"max_discount_amount", "min_order_amount", "min_on_whole_order", "applies_to", "scope", "channels",
	"allow_pickup", "stackable", "usage_limit", "times_used", "remaining_uses", "per_customer_limit",
	"customer_times_used", "customer_remaining_uses", "active_from", "active_to", "on_website",
	"woo_coupon_code",
}
_ENTRY_KEYS = {
	"customer", "customer_name", "phone", "order_count", "last_order_date", "last_woo_order_id",
	"active_codes", "public_codes", "inactive_codes", "used_codes",
}


def _strip_phone(value):
	# Same separators the endpoint strips in SQL before its LIKE prefilter.
	return "".join(ch for ch in str(value or "") if ch not in " -()./")


def _rows_match(row, filters):
	for key, want in (filters or {}).items():
		have = row.get(key)
		if isinstance(want, (list, tuple)):
			op, arg = want[0], want[1]
			if op == "in":
				ok = have in arg
			elif op == "is":
				is_set = bool(str(have).strip()) if have is not None else False
				ok = is_set if arg == "set" else not is_set
			elif op == "!=":
				ok = have != arg
			else:
				raise AssertionError(f"unsupported filter op {op!r}")
		else:
			ok = have == want
		if not ok:
			return False
	return True


class _CustomerDB(_FakeDB):
	def get_value(self, doctype, filters, fieldname=None, as_dict=False):
		self.site.get_value_log.append(doctype)
		return super().get_value(doctype, filters, fieldname, as_dict)

	def sql(self, query, values=None, as_dict=False):
		return self.site.run_sql(" ".join(query.split()), values or {})


class _CustomerSite(_FakeSite):
	"""``_FakeSite`` plus customers, contacts and the SQL the lookup runs."""

	def __init__(self):
		super().__init__()
		self.db = _CustomerDB(self)
		self.customers = {}
		self.contacts: list = []  # (customer, phone)
		self.sql_log: list = []
		self.get_all_log: list = []
		self.get_value_log: list = []

	# -- fixtures -------------------------------------------------------------
	def add_customer(self, name, customer_name=None, mobile_no=None, disabled=0):
		self.customers[name] = {"customer_name": customer_name or name, "mobile_no": mobile_no, "disabled": disabled}

	def add_order(self, name, customer, posting_date, *, woo=0, docstatus=1, is_return=0, grand_total=500.0):
		self.invoices[name] = _invoice(
			name,
			customer=customer,
			posting_date=posting_date,
			woo_order_id=woo,
			docstatus=docstatus,
			is_return=is_return,
			grand_total=grand_total,
			creation=datetime.datetime.combine(posting_date, datetime.time(10, 0)),
		)

	def redeem(self, code, invoice, *, status="Active", customer="CUST-X", creation="2026-11-01 10:00:00", woo=None, discount=0.0):
		super().redeem(code, invoice, status=status, customer=customer, creation=creation, woo=woo)
		self.redemptions[-1]["discount_applied"] = discount

	def clear_logs(self):
		self.sql_log.clear()
		self.get_all_log.clear()
		self.get_value_log.clear()
		self.get_doc_calls.clear()

	# -- frappe.db.sql --------------------------------------------------------
	def _orders(self, customer):
		return [
			inv
			for inv in self.invoices.values()
			if inv.customer == customer and inv.docstatus == 1 and not int(inv.is_return or 0)
		]

	def run_sql(self, q, values):
		self.sql_log.append(q)
		if "FROM `tabContact Phone` cp" in q:
			tail = values["pattern"].strip("%")
			return [{"customer": c, "phone": p} for c, p in self.contacts if tail in _strip_phone(p)]
		if "FROM `tabCustomer` WHERE" in q:
			tail = values["pattern"].strip("%")
			return [
				{"name": n, "mobile_no": c["mobile_no"]}
				for n, c in self.customers.items()
				if tail in _strip_phone(c["mobile_no"])
			]
		if "c.customer_name LIKE" in q:
			needle = values["like"].strip("%").replace("\\", "").lower()
			hits = [
				n for n, c in self.customers.items() if needle in n.lower() or needle in c["customer_name"].lower()
			]

			def _recent(n):
				dates = [inv.posting_date for inv in self._orders(n)]
				last = max(dates) if dates else None
				return (last is None, -last.toordinal() if last else 0, n)

			return [{"name": n} for n in sorted(hits, key=_recent)[: values["limit"]]]
		if "FROM `tabDynamic Link` dl" in q:
			return [{"customer": c, "phone": p} for c, p in self.contacts if c in values["customers"]]
		if "AS order_count" in q:
			out = []
			for n in values["customers"]:
				orders = self._orders(n)
				if orders:
					out.append(
						{
							"customer": n,
							"order_count": len(orders),
							"last_order_date": max(i.posting_date for i in orders),
							"last_creation": max(i.creation for i in orders),
							"last_woo_order_id": max(int(i.woo_order_id or 0) for i in orders),
						}
					)
			return out
		if "`tabJarz Promo Redemption`" in q:
			per_customer = "customers" in values
			counts: dict = {}
			for r in self.redemptions:
				if r["status"] != "Active" or r["promo_code"] not in values["codes"]:
					continue
				if per_customer and r["customer"] not in values["customers"]:
					continue
				key = (r["promo_code"], r["customer"]) if per_customer else (r["promo_code"],)
				counts[key] = counts.get(key, 0) + 1
			if per_customer:
				return [{"promo_code": k[0], "customer": k[1], "n": v} for k, v in counts.items()]
			return [{"promo_code": k[0], "n": v} for k, v in counts.items()]
		raise AssertionError(f"unexpected SQL: {q}")

	# -- frappe.get_all -------------------------------------------------------
	def get_all(self, doctype, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None, **kwargs):
		self.get_all_log.append(doctype)
		if doctype == "Customer":
			rows = [r for r in ({"name": n, **c} for n, c in self.customers.items()) if _rows_match(r, filters)]
			if pluck:
				return [r[pluck] for r in rows]
			return [{f: r.get(f) for f in fields} for r in rows]
		if doctype == "Jarz Promo Code":
			return [
				{f: getattr(p, f, None) for f in fields} for p in self.promos.values() if _rows_match(vars(p), filters)
			]
		if doctype in ("Jarz Promo Code Item", "Jarz Promotion Rule Channel"):
			field = "item_scope" if doctype == "Jarz Promo Code Item" else "channels"
			out = []
			for p in self.promos.values():
				for child in getattr(p, field) or []:
					row = {"parent": p.name, "parenttype": "Jarz Promo Code", "parentfield": field, **vars(child)}
					if _rows_match(row, filters):
						out.append({f: row.get(f) for f in fields})
			return out
		if doctype == "Item":
			return [
				{"name": n, "item_name": label}
				for n, label in self.item_names.items()
				if _rows_match({"name": n}, filters)
			]
		if doctype == "Jarz Promo Redemption":
			rows = sorted(
				(r for r in self.redemptions if _rows_match(r, filters)), key=lambda r: r["creation"], reverse=True
			)
			return [dict(r) for r in rows[: limit_page_length or None]]
		if doctype == "Sales Invoice" and filters and "name" in filters:
			return [
				{
					"name": inv.name,
					"customer_name": inv.customer_name,
					"posting_date": getattr(inv, "posting_date", None),
					"woo_order_id": inv.woo_order_id,
					"grand_total": inv.grand_total,
				}
				for inv in self.invoices.values()
				if inv.name in filters["name"][1]
			]
		return super().get_all(
			doctype, filters=filters, fields=fields, pluck=pluck, order_by=order_by, limit_page_length=limit_page_length
		)


class _CustomerBase(unittest.TestCase):
	def setUp(self):
		self.site = _CustomerSite()
		for target in ("jarz_pos.api.promo.frappe", "jarz_pos.services.promo_codes.frappe"):
			patcher = mock.patch(target, self.site)
			patcher.start()
			self.addCleanup(patcher.stop)

	def find(self, query):
		return api.lookup_customer_promos(query=query)

	@staticmethod
	def entry(out, customer):
		for row in out["customers"]:
			if row["customer"] == customer:
				return row
		raise AssertionError(f"{customer!r} not in {[c['customer'] for c in out['customers']]}")

	@staticmethod
	def codes(rows):
		return [row["code"] for row in rows]


class TestCustomerLookupQuery(_CustomerBase):
	def test_too_short_query_throws(self):
		for query in (None, "", "   ", "ab", " a1 "):
			with self.assertRaises(_Thrown, msg=repr(query)):
				self.find(query)

	def test_phone_mode_vs_name_mode(self):
		self.site.add_customer("Amal Bedross", mobile_no="01225495533")
		phone_queries = (
			"0122 549-5533",
			"(+20) 122 549 5533",
			"+201225495533",
			"٠١٢٢٥٤٩٥٥٣٣",  # Arabic-Indic digits
		)
		for query in phone_queries:
			out = self.find(query)
			self.assertEqual(out["match_type"], "phone", query)
			self.assertEqual([c["customer"] for c in out["customers"]], ["Amal Bedross"], query)
		for query in ("12345", "Amal", "Amal 0122", "  amal  "):
			self.assertEqual(self.find(query)["match_type"], "name", query)
		self.assertEqual(self.find("  Amal ")["query"], "Amal")
		self.assertEqual([c["customer"] for c in self.find("Amal")["customers"]], ["Amal Bedross"])

	def test_phone_matches_every_stored_format_and_contact_phones(self):
		self.site.add_customer("CUST-A", "Amal", mobile_no="+20 122 549 5533")
		self.site.add_customer("CUST-B", "Basma", mobile_no=None)
		self.site.add_customer("CUST-C", "Camilia", mobile_no="201225495533")
		self.site.add_customer("CUST-D", "Dina", mobile_no="01000000009")
		self.site.contacts = [("CUST-B", "0122-549-5533"), ("GHOST", "01225495533")]

		out = self.find("01225495533")
		self.assertEqual(out["match_type"], "phone")
		self.assertEqual(sorted(c["customer"] for c in out["customers"]), ["CUST-A", "CUST-B", "CUST-C"])
		# No mobile_no: the first contact phone is shown.
		self.assertEqual(self.entry(out, "CUST-B")["phone"], "0122-549-5533")
		self.assertEqual(self.entry(out, "CUST-A")["phone"], "+20 122 549 5533")

		# Fewer full digits: any part of the number matches - its end, its middle,
		# or its start as staff type it with the leading 0.
		for partial_query in ("549 5533", "549553", "0122549", "0122549553"):
			partial = self.find(partial_query)
			self.assertEqual(
				sorted(c["customer"] for c in partial["customers"]),
				["CUST-A", "CUST-B", "CUST-C"],
				partial_query,
			)
		# Digits that are not in the number match nothing.
		self.assertEqual(self.find("777888")["customers"], [])

	def test_phone_mode_orders_by_most_recent_order_and_reports_stats(self):
		self.site.add_customer("CUST-A", "Amal", mobile_no="01225495533")
		self.site.add_customer("CUST-B", "Basma", mobile_no="+201225495533")
		self.site.add_customer("CUST-C", "Camilia", mobile_no="201225495533")
		self.site.add_order("SI-A1", "CUST-A", datetime.date(2026, 11, 1))
		self.site.add_order("SI-C1", "CUST-C", datetime.date(2026, 11, 3), woo=21000)
		self.site.add_order("SI-C2", "CUST-C", datetime.date(2026, 11, 5))
		self.site.add_order("SI-C3", "CUST-C", datetime.date(2026, 11, 6), is_return=1)
		self.site.add_order("SI-C4", "CUST-C", datetime.date(2026, 11, 7), docstatus=0)

		out = self.find("01225495533")
		self.assertEqual([c["customer"] for c in out["customers"]], ["CUST-C", "CUST-A", "CUST-B"])
		c = self.entry(out, "CUST-C")
		self.assertEqual(c["order_count"], 2)
		self.assertEqual(c["last_order_date"], "2026-11-05")
		self.assertEqual(c["last_woo_order_id"], 21000)
		a = self.entry(out, "CUST-A")
		self.assertEqual((a["order_count"], a["last_order_date"], a["last_woo_order_id"]), (1, "2026-11-01", None))
		b = self.entry(out, "CUST-B")
		self.assertEqual((b["order_count"], b["last_order_date"], b["last_woo_order_id"]), (0, None, None))

	def test_name_mode_matches_name_and_docname_most_recent_order_first(self):
		self.site.add_customer("Amal Bedross", mobile_no="01225495533")
		self.site.add_customer("Amal Hassan", mobile_no="01000000002")
		self.site.add_customer("KAMAL-001", "Karim", disabled=1)
		self.site.add_customer("Sara", mobile_no="01000000003")
		self.site.add_order("SI-1", "Amal Bedross", datetime.date(2026, 11, 1))
		self.site.add_order("SI-2", "Amal Hassan", datetime.date(2026, 11, 6))
		out = self.find("amal")
		self.assertEqual(out["match_type"], "name")
		self.assertFalse(out["truncated"])
		self.assertEqual([c["customer"] for c in out["customers"]], ["Amal Hassan", "Amal Bedross", "KAMAL-001"])
		self.assertEqual(self.entry(out, "KAMAL-001")["customer_name"], "Karim")

	def test_name_mode_caps_at_ten_and_flags_truncated(self):
		for i in range(1, 11):
			self.site.add_customer(f"Sara {i:02d}")
		out = self.find("Sara")
		self.assertEqual(len(out["customers"]), 10)
		self.assertFalse(out["truncated"])
		self.site.add_customer("Sara 11")
		out = self.find("Sara")
		self.assertEqual(len(out["customers"]), 10)
		self.assertTrue(out["truncated"])

	def test_no_match_is_an_empty_list(self):
		self.site.add_customer("Amal Bedross", mobile_no="01225495533")
		self.assertEqual(
			self.find("Nobody"), {"query": "Nobody", "match_type": "name", "truncated": False, "customers": []}
		)
		self.assertEqual(
			self.find("01999999999"),
			{"query": "01999999999", "match_type": "phone", "truncated": False, "customers": []},
		)


class TestCustomerLookupCodes(_CustomerBase):
	def test_personal_code_matched_by_customer_link(self):
		self.site.add_customer("Amal Bedross", mobile_no="01225495533")
		self.site.add_customer("Zed", mobile_no="01000000077")
		self.site.promos["BACK1"] = _promo("BACK1", customer="Amal Bedross", active_to=LATER)
		self.site.promos["ZED1"] = _promo("ZED1", customer="Zed", customer_phone="01000000077")
		amal = self.entry(self.find("Bedross"), "Amal Bedross")
		self.assertEqual(self.codes(amal["active_codes"]), ["BACK1"])
		self.assertTrue(amal["active_codes"][0]["personal"])
		self.assertEqual(amal["public_codes"], [])
		self.assertEqual(amal["inactive_codes"], [])

	def test_personal_code_matched_by_phone_key_only(self):
		# Woo forked a duplicate Customer; the code is locked to the duplicate
		# but carries the phone, so the engine accepts it for this customer too.
		self.site.add_customer("Amal Bedross", mobile_no=None)
		self.site.add_customer("Amal Woo Dup", mobile_no="01009998888")
		self.site.contacts = [("Amal Bedross", "01225495533")]
		self.site.promos["DUP1"] = _promo("DUP1", customer="Amal Woo Dup", customer_phone="+201225495533")
		self.site.promos["PH2"] = _promo("PH2", customer=None, customer_phone="0122 549 5533")
		self.site.promos["OTHER"] = _promo("OTHER", customer_phone="01000000099")

		amal = self.entry(self.find("Bedross"), "Amal Bedross")
		self.assertEqual(sorted(self.codes(amal["active_codes"])), ["DUP1", "PH2"])
		self.assertTrue(all(code["personal"] for code in amal["active_codes"]))

		# Phone mode: a real Customer owns the key, so no pseudo entry.
		out = self.find("01225495533")
		self.assertEqual([c["customer"] for c in out["customers"]], ["Amal Bedross"])
		self.assertEqual(sorted(self.codes(out["customers"][0]["active_codes"])), ["DUP1", "PH2"])

	def test_pseudo_customer_for_phone_locked_code_without_customer(self):
		self.site.promos["WB1"] = _promo("WB1", customer=None, customer_phone="01117778888", active_to=LATER)
		self.site.promos["WB2"] = _promo(
			"WB2", customer="GONE-CUST", customer_phone="+201117778888", active_to=EARLIER
		)
		self.site.promos["PUB"] = _promo("PUB")
		for query in ("0111 777 8888", "7778888"):
			out = self.find(query)
			self.assertEqual(out["match_type"], "phone")
			self.assertEqual(len(out["customers"]), 1, query)
			pseudo = out["customers"][0]
			self.assertEqual(set(pseudo), _ENTRY_KEYS)
			self.assertIsNone(pseudo["customer"])
			self.assertIsNone(pseudo["customer_name"])
			self.assertEqual(pseudo["phone"], "01117778888")
			self.assertEqual(
				(pseudo["order_count"], pseudo["last_order_date"], pseudo["last_woo_order_id"]), (0, None, None)
			)
			self.assertEqual(self.codes(pseudo["active_codes"]), ["WB1"])
			self.assertEqual(self.codes(pseudo["inactive_codes"]), ["WB2"])
			self.assertEqual(self.codes(pseudo["public_codes"]), ["PUB"])
			self.assertEqual(pseudo["used_codes"], [])
		# Name mode never invents pseudo customers.
		self.assertEqual(self.find("GONE")["customers"], [])

	def test_pseudo_entry_follows_real_customers(self):
		self.site.add_customer("Amal Bedross", mobile_no="01225495533")
		self.site.promos["WB1"] = _promo("WB1", customer_phone="01117495533")
		out = self.find("495533")
		self.assertEqual([c["customer"] for c in out["customers"]], ["Amal Bedross", None])
		self.assertEqual(self.codes(out["customers"][1]["active_codes"]), ["WB1"])
		self.assertEqual(out["customers"][0]["active_codes"], [])

	def test_active_vs_inactive_split_and_order(self):
		self.site.add_customer("Mona Ali", mobile_no="01000000001")
		mine = {"customer": "Mona Ali"}
		self.site.promos.update(
			{
				"ACT1": _promo("ACT1", active_to=NOW + datetime.timedelta(days=10), **mine),
				"ACT2": _promo("ACT2", active_to=NOW + datetime.timedelta(days=2), **mine),
				"OPEN": _promo("OPEN", **mine),
				"SOON": _promo("SOON", active_from=NOW + datetime.timedelta(days=1), **mine),
				"EXP1": _promo("EXP1", active_to=NOW - datetime.timedelta(days=1), **mine),
				"EXP2": _promo("EXP2", active_to=NOW - datetime.timedelta(days=5), **mine),
				"USED": _promo("USED", usage_limit=1, active_to=NOW + datetime.timedelta(days=5), **mine),
				"OFF": _promo("OFF", enabled=0, active_to=NOW + datetime.timedelta(days=20), **mine),
			}
		)
		self.site.redeem("USED", "SI-U", customer="Mona Ali")
		mona = self.entry(self.find("Mona"), "Mona Ali")
		self.assertEqual(self.codes(mona["active_codes"]), ["ACT2", "ACT1", "OPEN", "SOON"])
		self.assertEqual([c["status"] for c in mona["active_codes"]], ["active", "active", "active", "not_started"])
		self.assertEqual(self.codes(mona["inactive_codes"]), ["OFF", "USED", "EXP1", "EXP2"])
		self.assertEqual(
			[c["status"] for c in mona["inactive_codes"]], ["disabled", "used_up", "expired", "expired"]
		)
		used = mona["inactive_codes"][1]
		self.assertEqual((used["times_used"], used["remaining_uses"], used["customer_times_used"]), (1, 0, 1))

	def test_inactive_codes_capped_at_ten(self):
		self.site.add_customer("Mona Ali")
		for i in range(12):
			self.site.promos[f"OLD{i:02d}"] = _promo(
				f"OLD{i:02d}", customer="Mona Ali", active_to=NOW - datetime.timedelta(days=i + 1)
			)
		mona = self.entry(self.find("Mona"), "Mona Ali")
		self.assertEqual(self.codes(mona["inactive_codes"]), [f"OLD{i:02d}" for i in range(10)])

	def test_public_code_hidden_once_the_customer_used_their_share(self):
		self.site.add_customer("Mona Ali", mobile_no="01000000001")
		self.site.add_customer("Sara Adel", mobile_no="01000000002")
		self.site.add_customer("Zed", mobile_no="01000000077")
		self.site.promos.update(
			{
				"PUB1": _promo("PUB1", per_customer_limit=1),
				"PUB2": _promo("PUB2"),
				"PUBX": _promo("PUBX", active_to=EARLIER),
				"PUBOFF": _promo("PUBOFF", enabled=0),
				"LOCKED": _promo("LOCKED", customer="Zed"),
			}
		)
		self.site.redeem("PUB1", "SI-1", customer="Mona Ali")
		self.site.redeem("PUB1", "SI-2", customer="Sara Adel", status="Reversed")

		mona = self.entry(self.find("Mona"), "Mona Ali")
		self.assertEqual(self.codes(mona["public_codes"]), ["PUB2"])
		pub2 = mona["public_codes"][0]
		self.assertFalse(pub2["personal"])
		self.assertIsNone(pub2["customer_remaining_uses"])

		sara = self.entry(self.find("Sara"), "Sara Adel")
		self.assertEqual(self.codes(sara["public_codes"]), ["PUB1", "PUB2"])
		pub1 = sara["public_codes"][0]
		self.assertEqual((pub1["times_used"], pub1["customer_times_used"], pub1["customer_remaining_uses"]), (1, 0, 1))

	def test_used_codes_rows_include_reversed(self):
		self.site.add_customer("Mona Ali", mobile_no="01000000001")
		self.site.add_customer("Sara Adel", mobile_no="01000000002")
		self.site.promos["BACK1"] = _promo("BACK1", customer="Mona Ali")
		self.site.promos["PUB1"] = _promo("PUB1")
		self.site.add_order("SI-1", "Mona Ali", datetime.date(2026, 10, 31), woo=20240, grand_total=510.0)
		self.site.add_order("SI-2", "Mona Ali", datetime.date(2026, 11, 2), grand_total=300.0)
		self.site.redeem("BACK1", "SI-1", customer="Mona Ali", creation="2026-10-31 12:00:00", discount=90.0)
		self.site.redeem(
			"PUB1", "SI-2", customer="Mona Ali", status="Reversed", creation="2026-11-02 12:00:00", discount=30.0
		)
		self.site.redeem(
			"PUB1", "SI-GONE", customer="Mona Ali", creation="2026-11-03 09:00:00", woo="21999", discount=25.0
		)
		self.site.redeem("PUB1", "SI-9", customer="Sara Adel", creation="2026-11-04 09:00:00")

		mona = self.entry(self.find("Mona"), "Mona Ali")
		self.assertEqual(
			mona["used_codes"],
			[
				{
					"code": "PUB1", "invoice": "SI-GONE", "woo_order_id": 21999, "date": "2026-11-03",
					"status": "Active", "discount": 25.0, "order_total": None,
				},
				{
					"code": "PUB1", "invoice": "SI-2", "woo_order_id": None, "date": "2026-11-02",
					"status": "Reversed", "discount": 30.0, "order_total": 300.0,
				},
				{
					"code": "BACK1", "invoice": "SI-1", "woo_order_id": 20240, "date": "2026-10-31",
					"status": "Active", "discount": 90.0, "order_total": 510.0,
				},
			],
		)

	def test_used_codes_capped_at_twenty(self):
		self.site.add_customer("Mona Ali")
		self.site.promos["PUB1"] = _promo("PUB1")
		for i in range(25):
			self.site.redeem("PUB1", f"SI-{i}", customer="Mona Ali", creation=f"2026-10-{i + 1:02d} 10:00:00")
		used = self.entry(self.find("Mona"), "Mona Ali")["used_codes"]
		self.assertEqual(len(used), 20)
		self.assertEqual(used[0]["invoice"], "SI-24")

	def test_code_summary_shape_scope_and_channels(self):
		self.site.add_customer("Mona Ali", mobile_no="01000000001")
		self.site.item_names = {"LOTUS-L": "Lotus Large Jar"}
		self.site.promos["BACK1"] = _promo(
			"BACK1",
			customer="Mona Ali",
			title="Win-back one",
			discount_value=15,
			max_discount_amount=100,
			min_order_amount=400,
			applies_to="Specific Items",
			item_scope=[SimpleNamespace(scope_type="Item", item_code="LOTUS-L", item_group=None)],
			channels=[SimpleNamespace(channel="Woo"), SimpleNamespace(channel="flutter")],
			usage_limit=1,
			per_customer_limit=1,
			active_from=datetime.datetime(2026, 10, 31, 0, 3, 4),
			active_to=datetime.datetime(2026, 11, 30, 0, 3, 4),
			woo_coupon_code="back1",
			woo_coupon_id="555",
		)
		self.site.clear_logs()
		out = self.find("Mona")
		mona = self.entry(out, "Mona Ali")
		self.assertEqual(set(mona), _ENTRY_KEYS)
		code = mona["active_codes"][0]
		self.assertEqual(set(code), _SUMMARY_KEYS)
		self.assertEqual(code["scope"], [{"type": "Item", "name": "LOTUS-L", "label": "Lotus Large Jar"}])
		self.assertEqual(code["channels"], ["woo", "flutter"])
		self.assertEqual(code["active_from"], "2026-10-31 00:03:04")
		self.assertEqual(code["active_to"], "2026-11-30 00:03:04")
		self.assertEqual((code["remaining_uses"], code["customer_remaining_uses"]), (1, 1))
		self.assertTrue(code["on_website"])
		self.assertEqual(code["woo_coupon_code"], "back1")
		# Batched: no per-code Item lookups and no per-code get_doc.
		self.assertNotIn("Item", self.site.get_value_log)
		self.assertEqual(self.site.get_doc_calls, [])

	def test_query_count_does_not_grow_with_customers(self):
		def add(i):
			name = f"Nour {i}"
			self.site.add_customer(name, mobile_no=f"0100000{i:04d}")
			self.site.promos[f"N{i}A"] = _promo(f"N{i}A", customer=name)
			self.site.promos[f"N{i}B"] = _promo(f"N{i}B", customer_phone=f"+20100000{i:04d}")
			self.site.add_order(f"SI-N{i}", name, datetime.date(2026, 11, 1), woo=1000 + i)
			self.site.redeem(f"N{i}A", f"SI-N{i}", customer=name)

		def calls():
			self.site.clear_logs()
			out = self.find("Nour")
			return len(out["customers"]), len(self.site.sql_log) + len(self.site.get_all_log) + len(
				self.site.get_value_log
			)

		add(1)
		add(2)
		few, few_calls = calls()
		for i in range(3, 9):
			add(i)
		many, many_calls = calls()
		self.assertEqual((few, many), (2, 8))
		self.assertEqual(few_calls, many_calls)

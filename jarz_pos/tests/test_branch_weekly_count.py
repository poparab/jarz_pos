"""Tests for the Branch Weekly Count API (api/branch_weekly_count.py).

What they pin down:

* the week is Saturday to Friday, and the due date is the Friday;
* the count is blind: the sheet and a non-approver's view carry no quantities
  the system holds, and a submit that skips any sheet item is refused;
* a staff submit waits as Pending Approval, moves no stock, and pushes the
  branch's approvers; an approver's submit posts straight away at ``counted_at``;
* approve posts at ``counted_at`` too, only from Pending Approval, only for an
  approver of that branch; reject needs a reason;
* the sheet is the count profile's items plus the warehouse's Bin items, minus
  the profile's Exclude exceptions;
* the side-menu queue is absent (not zero) for a caller who cannot approve.

Runs without a site: frappe is replaced by a small fake, exactly as the other
unit tests in this folder do. Run with ``python -m unittest`` from the app root
(``PYTHONPATH=. python -m unittest jarz_pos.tests.test_branch_weekly_count``).
"""

import json
import sys
import types
import unittest
from contextlib import ExitStack
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _install_fake_frappe():
	"""Give the fake frappe whatever this module needs, whichever test installed it first."""
	existing = sys.modules.get("frappe")
	if existing is not None and getattr(existing, "__file__", None):
		return  # the real thing

	frappe = existing or types.ModuleType("frappe")
	utils = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")

	def default(obj, name, value):
		if not hasattr(obj, name):
			setattr(obj, name, value)

	def fake_whitelist(*args, **kwargs):
		def decorator(func):
			return func

		if args and callable(args[0]) and len(args) == 1 and not kwargs:
			return args[0]
		return decorator

	def fake_throw(message, exc=Exception):
		raise exc(message)

	default(frappe, "_", lambda message: message)
	default(frappe, "_dict", dict)
	default(frappe, "whitelist", fake_whitelist)
	default(frappe, "throw", fake_throw)
	default(frappe, "ValidationError", type("ValidationError", (Exception,), {}))
	default(frappe, "PermissionError", type("PermissionError", (Exception,), {}))
	default(frappe, "DoesNotExistError", type("DoesNotExistError", (Exception,), {}))
	default(frappe, "get_all", lambda *a, **k: [])
	default(frappe, "get_doc", lambda *a, **k: SimpleNamespace())
	default(frappe, "get_cached_doc", lambda *a, **k: SimpleNamespace())
	default(frappe, "get_roles", lambda *a, **k: [])
	default(frappe, "enqueue", lambda *a, **k: None)
	default(frappe, "get_traceback", lambda *a, **k: "")
	default(frappe, "log_error", lambda *a, **k: None)
	default(frappe, "session", SimpleNamespace(user="staff@jarz.test"))
	default(frappe, "defaults", SimpleNamespace(get_user_default=lambda *a, **k: None))
	if not hasattr(frappe, "db"):
		frappe.db = SimpleNamespace()
	for attr, fn in {
		"count": lambda *a, **k: 0,
		"exists": lambda *a, **k: None,
		"get_value": lambda *a, **k: None,
		"get_single_value": lambda *a, **k: None,
		"sql": lambda *a, **k: [],
		"commit": lambda *a, **k: None,
	}.items():
		default(frappe.db, attr, fn)
	default(utils, "cint", lambda value: int(value or 0))
	default(utils, "getdate", lambda value: value)
	default(utils, "strip_html", lambda value: str(value))
	default(utils, "now_datetime", lambda: datetime(2026, 10, 20, 13, 5, 0))
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	frappe.utils = utils


_install_fake_frappe()

import frappe

from jarz_pos.api import approvals
from jarz_pos.api import branch_weekly_count as bwc
from jarz_pos.constants import ROLES, STATUS

TUESDAY = datetime(2026, 10, 20, 13, 5, 0)
FRIDAY = datetime(2026, 10, 23, 9, 0, 0)
WEEK_START = "2026-10-17"

SHEET = [
	{
		"item_code": "JAR-L-TIR",
		"item_name": "Tiramisu Large",
		"item_group": "Large",
		"stock_uom": "Nos",
		"image": None,
	},
	{
		"item_code": "JAR-S-TIR",
		"item_name": "Tiramisu Small",
		"item_group": "Small",
		"stock_uom": "Nos",
		"image": "/files/x.png",
	},
	{
		"item_code": "BAG-1",
		"item_name": "Paper Bag",
		"item_group": "Consumable",
		"stock_uom": "Nos",
		"image": None,
	},
]
FULL_LINES = [
	{"item_code": "JAR-L-TIR", "qty": 3},
	{"item_code": "JAR-S-TIR", "qty": 2},
	{"item_code": "BAG-1", "qty": 10},
]
BIN_QTY = {"JAR-L-TIR": 5.0, "JAR-S-TIR": 2.0, "BAG-1": 10.0}


class _FakeDoc:
	"""Stands in for a Branch Weekly Count Document."""

	def __init__(self, values=None, **fields):
		merged = {**(values or {}), **fields}
		items = merged.pop("items", [])
		self.__dict__.update(merged)
		self.name = fields.get("name", "BWC-2026-00001")
		self.items = [SimpleNamespace(**row) if isinstance(row, dict) else row for row in items]
		self.inserted = False
		self.saved = False

	def insert(self, **kwargs):
		self.inserted = True
		return self

	def save(self, **kwargs):
		self.saved = True
		return self

	def get(self, key, default=None):
		return getattr(self, key, default)


def _pending_doc(**overrides):
	fields = {
		"name": "BWC-2026-00007",
		"pos_profile": "Dokki",
		"warehouse": "Dokki - J",
		"week_start": date(2026, 10, 17),
		"status": STATUS.PENDING_APPROVAL,
		"counted_by": "staff@jarz.test",
		"counted_by_name": "Yehia",
		"counted_at": datetime(2026, 10, 19, 20, 15, 0),
		"approved_by_name": None,
		"approved_at": None,
		"rejection_reason": None,
		"total_items": 2,
		"differences_count": 1,
		"stock_reconciliation": None,
		"items": [
			{
				"item_code": "JAR-L-TIR",
				"item_name": "Tiramisu Large",
				"item_group": "Large",
				"stock_uom": "Nos",
				"counted_qty": 3.0,
				"system_qty": 5.0,
				"difference": -2.0,
			},
			{
				"item_code": "BAG-1",
				"item_name": "Paper Bag",
				"item_group": "Consumable",
				"stock_uom": "Nos",
				"counted_qty": 10.0,
				"system_qty": 10.0,
				"difference": 0.0,
			},
			{
				"item_code": "JAR-S-TIR",
				"item_name": "Tiramisu Small",
				"item_group": "Small",
				"stock_uom": "Nos",
				"counted_qty": 4.0,
				"system_qty": 2.0,
				"difference": 2.0,
			},
		],
	}
	fields.update(overrides)
	return _FakeDoc(**fields)


class _Base(unittest.TestCase):
	"""Common patches: a fixed clock, a signed-in user, a branch the user can use."""

	user = "staff@jarz.test"
	approver = False
	now = TUESDAY

	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.reconcile = MagicMock(
			return_value={"ok": True, "stock_reconciliation": "MAT-RECO-1", "differences": 1}
		)
		self.enqueue = MagicMock(return_value=True)
		self.committed = MagicMock()
		self.patch(bwc, "_now", return_value=self.now)
		self.patch(bwc.frappe, "session", new=SimpleNamespace(user=self.user), create=True)
		self.patch(bwc, "is_count_approver", side_effect=lambda user=None: self.approver)
		self.patch(bwc, "listed_count_approvers", return_value=None)
		self.patch(bwc, "get_allowed_pos_profiles_for_user", return_value=["Dokki"])
		self.patch(bwc, "_full_name", side_effect=lambda user: f"Name of {user}")
		self.patch(bwc, "_reconcile_counts", new=self.reconcile)
		self.patch(bwc, "_enqueue_push", new=self.enqueue)
		self.patch(bwc.frappe.db, "commit", new=self.committed, create=True)
		self.patch(bwc.frappe.db, "sql", return_value=[], create=True)

	def patch(self, target, attr, **kwargs):
		return self.stack.enter_context(patch.object(target, attr, **kwargs))

	def refusal(self, func, *args, **kwargs):
		"""The ``[code]`` a call is refused with, and the exception type."""
		with self.assertRaises(Exception) as ctx:
			func(*args, **kwargs)
		message = str(ctx.exception)
		self.assertTrue(message.startswith("["), message)
		return message[1 : message.index("]")], ctx.exception


class TestWeekMath(unittest.TestCase):
	def test_saturday_starts_its_own_week(self):
		self.assertEqual(bwc.week_start_for(date(2026, 10, 17)), date(2026, 10, 17))

	def test_friday_belongs_to_the_week_that_started_six_days_earlier(self):
		self.assertEqual(bwc.week_start_for(date(2026, 10, 23)), date(2026, 10, 17))

	def test_sunday_is_the_day_after_the_week_started(self):
		self.assertEqual(bwc.week_start_for(date(2026, 10, 18)), date(2026, 10, 17))

	def test_midweek_and_the_week_before(self):
		self.assertEqual(bwc.week_start_for(date(2026, 10, 20)), date(2026, 10, 17))
		self.assertEqual(bwc.week_start_for(date(2026, 10, 16)), date(2026, 10, 10))
		self.assertEqual(bwc.week_start_for(date(2026, 10, 24)), date(2026, 10, 24))

	def test_due_date_is_the_friday(self):
		start, due = bwc._week_bounds(date(2026, 10, 20))
		self.assertEqual((start, due), (date(2026, 10, 17), date(2026, 10, 23)))
		self.assertEqual(due.weekday(), 4)


class TestStatus(_Base):
	def _status(self, *, now, counts, approved_last_week=(), live=True):
		self.patch(bwc, "_now", return_value=now)
		self.patch(bwc, "_counting_live_by", return_value=live)
		self.patch(
			bwc,
			"_branch_rows",
			return_value=[{"name": n, "warehouse": f"{n} - J", "company": "Jarz"} for n in "ABCD"],
		)
		self.patch(bwc, "_member_profiles", return_value={"A", "B", "C"})
		self.patch(bwc, "_latest_counts", return_value=counts)
		self.patch(bwc, "_profiles_approved_in_week", return_value=set(approved_last_week))
		self.patch(bwc, "count_pending_in_scope", return_value=2)
		self.patch(bwc, "get_allowed_pos_profiles_for_user", return_value=list("ABCD"))
		return bwc.get_weekly_count_status()

	def _counts(self):
		return {
			"B": {
				"name": "BWC-1",
				"status": STATUS.PENDING_APPROVAL,
				"counted_at": datetime(2026, 10, 19, 9, 5),
			},
			"C": {"name": "BWC-2", "status": STATUS.APPROVED},
			"D": {"name": "BWC-3", "status": STATUS.REJECTED, "rejection_reason": "recount"},
		}

	def test_week_header_and_days_left(self):
		result = self._status(now=TUESDAY, counts={})
		self.assertEqual(
			(result["week_start"], result["due_date"], result["today"], result["days_left"]),
			("2026-10-17", "2026-10-23", "2026-10-20", 3),
		)

	def test_states_and_membership(self):
		result = self._status(now=TUESDAY, counts=self._counts())
		by_name = {b["pos_profile"]: b for b in result["branches"]}
		self.assertEqual(
			{n: b["state"] for n, b in by_name.items()},
			{"A": "due", "B": "pending_approval", "C": "done", "D": "rejected"},
		)
		self.assertEqual([by_name[n]["is_member"] for n in "ABCD"], [True, True, True, False])
		self.assertIsNone(by_name["A"]["count"])
		self.assertEqual(by_name["B"]["count"]["counted_at"], "2026-10-19 09:05:00")
		self.assertEqual(by_name["D"]["count"]["rejection_reason"], "recount")

	def test_last_day_only_for_due_or_rejected_on_the_friday(self):
		friday = self._status(now=FRIDAY, counts=self._counts())
		self.assertEqual(
			{b["pos_profile"]: b["last_day"] for b in friday["branches"]},
			{"A": True, "B": False, "C": False, "D": True},
		)
		self.assertEqual(friday["days_left"], 0)
		tuesday = self._status(now=TUESDAY, counts=self._counts())
		self.assertFalse(any(b["last_day"] for b in tuesday["branches"]))

	def test_missed_last_week_means_no_approved_count(self):
		result = self._status(now=TUESDAY, counts={}, approved_last_week=["A", "C"])
		self.assertEqual(
			{b["pos_profile"]: b["missed_last_week"] for b in result["branches"]},
			{"A": False, "B": True, "C": False, "D": True},
		)

	def test_launch_week_never_shows_missed(self):
		result = self._status(now=TUESDAY, counts={}, approved_last_week=[], live=False)
		self.assertFalse(any(b["missed_last_week"] for b in result["branches"]))

	def test_pending_count_is_for_approvers_only(self):
		self.assertEqual(self._status(now=TUESDAY, counts={})["pending_approval_count"], 0)
		self.assertFalse(self._status(now=TUESDAY, counts={})["is_approver"])
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		result = self._status(now=TUESDAY, counts={})
		self.assertTrue(result["is_approver"])
		self.assertEqual(result["pending_approval_count"], 2)


class TestSheet(_Base):
	def _item(self, code, name, group):
		return {
			"name": code,
			"item_name": name,
			"item_group": group,
			"stock_uom": "Nos",
			"image": None,
		}

	def test_items_are_profile_items_plus_bin_items_minus_exclusions(self):
		asked = {}
		catalogue = {
			"JAR-L-TIR": self._item("JAR-L-TIR", "Tiramisu Large", "Large"),
			"JAR-M-TIR": self._item("JAR-M-TIR", "Tiramisu Medium", "Medium"),
			"BAG-1": self._item("BAG-1", "Paper Bag", "Consumable"),
			"JAR-OLD": self._item("JAR-OLD", "Retired Large", "Large"),
		}

		def fake_get_all(doctype, **kwargs):
			if doctype == "Bin":
				asked["bin_filters"] = kwargs["filters"]
				return ["JAR-L-TIR", "BAG-1", "JAR-OLD"]
			asked["item_filters"] = kwargs["filters"]
			return [catalogue[c] for c in kwargs["filters"]["name"][1] if c in catalogue]

		self.patch(bwc, "_resolve_count_item_codes", return_value=["JAR-L-TIR", "JAR-M-TIR"])
		self.patch(bwc, "_excluded_item_codes", return_value={"JAR-OLD"})
		self.patch(bwc.frappe, "get_all", side_effect=fake_get_all)

		items = bwc._sheet_items("Dokki - J")

		self.assertEqual(asked["bin_filters"], {"warehouse": "Dokki - J"})
		self.assertEqual(asked["item_filters"]["name"], ["in", ["BAG-1", "JAR-L-TIR", "JAR-M-TIR"]])
		self.assertEqual(
			{k: v for k, v in asked["item_filters"].items() if k != "name"},
			{"is_stock_item": 1, "disabled": 0, "has_variants": 0},
		)
		self.assertEqual(sorted(i["item_code"] for i in items), ["BAG-1", "JAR-L-TIR", "JAR-M-TIR"])

	def test_no_count_profile_still_counts_what_the_warehouse_holds(self):
		self.patch(bwc, "_resolve_count_item_codes", return_value=None)
		self.patch(bwc, "_excluded_item_codes", return_value=set())

		def fake_get_all(doctype, **kwargs):
			if doctype == "Bin":
				return ["BAG-1"]
			return [self._item("BAG-1", "Paper Bag", "Consumable")]

		self.patch(bwc.frappe, "get_all", side_effect=fake_get_all)
		self.assertEqual([i["item_code"] for i in bwc._sheet_items("Dokki - J")], ["BAG-1"])

	def test_empty_warehouse_is_an_empty_sheet(self):
		self.patch(bwc, "_resolve_count_item_codes", return_value=[])
		self.patch(bwc, "_excluded_item_codes", return_value=set())
		self.patch(bwc.frappe, "get_all", return_value=[])
		self.assertEqual(bwc._sheet_items("Dokki - J"), [])

	def test_sections_follow_size_order_then_alphabetical_and_flavours_line_up(self):
		def item(code, name, group):
			return {
				"item_code": code,
				"item_name": name,
				"item_group": group,
				"stock_uom": "Nos",
				"image": None,
			}

		sections = bwc._build_sections(
			[
				item("c1", "Paper Bag", "Consumable"),
				item("s2", "Tiramisu Small", "Small"),
				item("l2", "Tiramisu Large", "Large"),
				item("l1", "Brownie Large", "Large"),
				item("m1", "Brownie Medium", "Medium"),
				item("b1", "Cold Brew", "Beverages"),
				item("s1", "Brownie Small", "Small"),
			]
		)
		self.assertEqual(
			[s["key"] for s in sections], ["Large", "Medium", "Small", "Beverages", "Consumable"]
		)
		self.assertEqual([i["item_code"] for i in sections[0]["items"]], ["l1", "l2"])
		self.assertEqual([i["item_code"] for i in sections[2]["items"]], ["s1", "s2"])
		self.assertEqual(sections[0]["title"], "Large")

	def test_sheet_is_blind(self):
		self.patch(bwc, "_require_branch", return_value={"name": "Dokki", "warehouse": "Dokki - J"})
		self.patch(bwc, "_sheet_items", return_value=SHEET)
		self.patch(bwc, "_latest_counts", return_value={})
		result = bwc.get_weekly_count_sheet("Dokki")
		self.assertEqual(result["state"], "due")
		self.assertEqual(result["total_items"], 3)
		self.assertEqual(result["week_start"], WEEK_START)
		self.assertEqual(result["due_date"], "2026-10-23")
		for section in result["sections"]:
			for entry in section["items"]:
				self.assertEqual(set(entry), {"item_code", "item_name", "stock_uom", "image"})
		self.assertNotIn("system_qty", json.dumps(result))

	def test_sheet_shows_the_status_card_state_when_already_counted(self):
		self.patch(bwc, "_require_branch", return_value={"name": "Dokki", "warehouse": "Dokki - J"})
		self.patch(bwc, "_sheet_items", return_value=SHEET)
		self.patch(
			bwc,
			"_latest_counts",
			return_value={"Dokki": {"name": "BWC-9", "status": STATUS.PENDING_APPROVAL}},
		)
		result = bwc.get_weekly_count_sheet("Dokki")
		self.assertEqual(result["state"], "pending_approval")
		self.assertEqual(result["count"]["name"], "BWC-9")


class TestRequireBranch(_Base):
	def test_a_branch_outside_the_callers_scope_is_refused(self):
		code, exc = self.refusal(bwc._require_branch, "Nasr city", self.user)
		self.assertEqual(code, "no_access")
		self.assertIsInstance(exc, frappe.PermissionError)

	def test_a_branch_without_a_warehouse_is_refused(self):
		self.patch(bwc.frappe.db, "get_value", return_value={"name": "Dokki", "warehouse": None}, create=True)
		code, _exc = self.refusal(bwc._require_branch, "Dokki", self.user)
		self.assertEqual(code, "no_warehouse")


class TestSubmit(_Base):
	def setUp(self):
		super().setUp()
		self.docs = []

		def fake_get_doc(values, *args, **kwargs):
			doc = _FakeDoc(values)
			self.docs.append(doc)
			return doc

		self.patch(
			bwc,
			"_require_branch",
			return_value={"name": "Dokki", "warehouse": "Dokki - J", "company": "Jarz"},
		)
		self.patch(bwc, "_sheet_items", return_value=SHEET)
		self.patch(bwc, "_get_bin_qty_map", return_value=BIN_QTY)
		self.patch(bwc.frappe, "get_all", return_value=[])
		self.patch(bwc.frappe.db, "get_value", return_value=None, create=True)
		self.patch(bwc.frappe, "get_doc", side_effect=fake_get_doc)

	def submit(self, lines=None, **kwargs):
		return bwc.submit_weekly_count("Dokki", FULL_LINES if lines is None else lines, **kwargs)

	def test_staff_count_waits_for_approval_and_moves_no_stock(self):
		result = self.submit(client_request_id="req-1")

		self.reconcile.assert_not_called()
		self.assertEqual(result["status"], STATUS.PENDING_APPROVAL)
		self.assertFalse(result["auto_approved"])
		self.assertIsNone(result["stock_reconciliation"])
		self.assertEqual(result["total_items"], 3)
		# Blind: the counter is not told how many lines were off.
		self.assertIsNone(result["differences_count"])
		doc = self.docs[0]
		self.assertTrue(doc.inserted)
		self.assertEqual(doc.status, STATUS.PENDING_APPROVAL)
		self.assertEqual(doc.week_start, WEEK_START)
		self.assertEqual(doc.counted_by, self.user)
		self.assertEqual(doc.client_request_id, "req-1")
		self.assertEqual(doc.differences_count, 1)
		self.assertEqual(doc.counted_at, "2026-10-20 13:05:00")
		self.assertEqual(
			{r.item_code: (r.counted_qty, r.system_qty, r.difference) for r in doc.items},
			{"JAR-L-TIR": (3.0, 5.0, -2.0), "JAR-S-TIR": (2.0, 2.0, 0.0), "BAG-1": (10.0, 10.0, 0.0)},
		)
		self.enqueue.assert_called_once_with(
			"notify_branch_count_approval_required", doc.name, "branch_count_approval"
		)
		self.committed.assert_called()

	def test_lines_may_be_a_json_string(self):
		result = self.submit(json.dumps(FULL_LINES))
		self.assertEqual(result["status"], STATUS.PENDING_APPROVAL)

	def test_approver_count_posts_immediately_at_counted_at(self):
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)

		result = self.submit()

		self.reconcile.assert_called_once()
		warehouse, posting, lines = self.reconcile.call_args.args
		self.assertEqual((warehouse, posting), ("Dokki - J", "2026-10-20 13:05:00"))
		self.assertEqual(self.reconcile.call_args.kwargs, {"qty_as_of_posting": True})
		self.assertEqual(
			sorted((ln["item_code"], ln["qty"]) for ln in lines),
			[("BAG-1", 10.0), ("JAR-L-TIR", 3.0), ("JAR-S-TIR", 2.0)],
		)
		self.assertEqual(result["status"], STATUS.APPROVED)
		self.assertTrue(result["auto_approved"])
		self.assertEqual(result["stock_reconciliation"], "MAT-RECO-1")
		self.assertEqual(result["differences_count"], 1)
		doc = self.docs[0]
		self.assertEqual(doc.status, STATUS.APPROVED)
		self.assertEqual(doc.approved_by, self.user)
		self.assertEqual(doc.stock_reconciliation, "MAT-RECO-1")
		self.assertEqual(doc.approved_at, "2026-10-20 13:05:00")
		# Nobody is asked to approve what an approver already approved.
		self.enqueue.assert_not_called()

	def test_a_failed_posting_leaves_no_count_behind(self):
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		self.reconcile.side_effect = frappe.ValidationError("Negative stock")
		with self.assertRaises(frappe.ValidationError):
			self.submit()
		self.assertEqual(self.docs, [])

	def test_missing_items_are_refused_and_named(self):
		code, exc = self.refusal(self.submit, [{"item_code": "JAR-L-TIR", "qty": 3}])
		self.assertEqual(code, "missing_items")
		self.assertIn("Paper Bag", str(exc))
		self.assertIn("Tiramisu Small", str(exc))
		self.assertNotIn("Tiramisu Large", str(exc))
		self.assertEqual(self.docs, [])
		self.reconcile.assert_not_called()

	def test_an_empty_submit_is_missing_everything(self):
		code, _exc = self.refusal(self.submit, [])
		self.assertEqual(code, "missing_items")

	def test_items_not_on_the_sheet_are_refused(self):
		code, exc = self.refusal(self.submit, [*FULL_LINES, {"item_code": "SNEAKY", "qty": 1}])
		self.assertEqual(code, "unknown_items")
		self.assertIn("SNEAKY", str(exc))

	def test_bad_quantities_are_refused(self):
		for bad in (-1, "abc", None, True, float("nan"), float("inf"), ""):
			lines = [{"item_code": "JAR-L-TIR", "qty": bad}, *FULL_LINES[1:]]
			code, _exc = self.refusal(self.submit, lines)
			self.assertEqual(code, "invalid_qty", repr(bad))

	def test_zero_is_a_valid_count(self):
		lines = [{"item_code": "JAR-L-TIR", "qty": 0}, *FULL_LINES[1:]]
		self.assertEqual(self.submit(lines)["status"], STATUS.PENDING_APPROVAL)

	def test_a_line_listed_twice_is_refused(self):
		code, _exc = self.refusal(self.submit, [*FULL_LINES, {"item_code": "BAG-1", "qty": 1}])
		self.assertEqual(code, "invalid_qty")

	def test_unreadable_lines_are_refused(self):
		code, _exc = self.refusal(self.submit, "not json")
		self.assertEqual(code, "invalid_qty")

	def test_a_branch_with_nothing_to_count_is_refused(self):
		self.patch(bwc, "_sheet_items", return_value=[])
		code, _exc = self.refusal(self.submit, [])
		self.assertEqual(code, "no_items")

	def test_a_second_count_while_one_is_pending_is_refused(self):
		self.patch(bwc.frappe, "get_all", return_value=[{"name": "BWC-1", "status": STATUS.PENDING_APPROVAL}])
		code, _exc = self.refusal(self.submit)
		self.assertEqual(code, "already_pending")
		self.assertEqual(self.docs, [])

	def test_a_second_count_after_an_approved_one_is_refused(self):
		self.patch(bwc.frappe, "get_all", return_value=[{"name": "BWC-1", "status": STATUS.APPROVED}])
		code, _exc = self.refusal(self.submit)
		self.assertEqual(code, "already_done")

	def test_the_week_check_looks_only_at_live_counts_of_this_week(self):
		get_all = self.patch(bwc.frappe, "get_all", return_value=[])
		self.submit()
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["pos_profile"], "Dokki")
		self.assertEqual(filters["week_start"], WEEK_START)
		self.assertEqual(filters["status"], ["in", [STATUS.PENDING_APPROVAL, STATUS.APPROVED]])

	def test_a_rejected_count_does_not_block_a_recount(self):
		# Rejected is not in the live statuses the check looks for, so the query returns nothing.
		self.assertEqual(self.submit()["status"], STATUS.PENDING_APPROVAL)

	def test_a_retry_returns_the_same_count_without_a_second_one(self):
		get_value = self.patch(
			bwc.frappe.db,
			"get_value",
			return_value={
				"name": "BWC-2026-00003",
				"status": STATUS.PENDING_APPROVAL,
				"auto_approved": 0,
				"stock_reconciliation": None,
				"differences_count": 4,
				"total_items": 3,
			},
			create=True,
		)
		result = self.submit(client_request_id="req-1")
		self.assertEqual(result["name"], "BWC-2026-00003")
		self.assertIsNone(result["differences_count"])
		self.assertEqual(self.docs, [])
		self.reconcile.assert_not_called()
		self.enqueue.assert_not_called()
		filters = get_value.call_args.args[1]
		self.assertEqual(filters["client_request_id"], "req-1")
		self.assertEqual(filters["counted_by"], self.user)

	def test_a_retry_never_echoes_a_rejected_count(self):
		# The lookup skips Rejected, so the same request id after a rejection
		# records a new count instead of handing the rejected one back.
		get_value = self.patch(bwc.frappe.db, "get_value", return_value=None, create=True)
		result = self.submit(client_request_id="req-1")
		self.assertEqual(get_value.call_args.args[1]["status"], ["!=", STATUS.REJECTED])
		self.assertEqual(result["status"], STATUS.PENDING_APPROVAL)
		self.assertEqual(len(self.docs), 1)

	def test_branch_access_is_checked_first(self):
		self.patch(
			bwc,
			"_require_branch",
			side_effect=frappe.PermissionError("[no_access] nope"),
		)
		code, exc = self.refusal(self.submit)
		self.assertEqual(code, "no_access")
		self.assertIsInstance(exc, frappe.PermissionError)
		self.assertEqual(self.docs, [])

	def test_the_branch_row_is_locked_before_the_week_is_checked(self):
		order = []
		self.patch(bwc.frappe.db, "sql", side_effect=lambda *a, **k: order.append("lock") or [], create=True)
		self.patch(bwc.frappe, "get_all", side_effect=lambda *a, **k: order.append("check") or [])
		self.submit()
		self.assertEqual(order[:2], ["lock", "check"])


class TestDecide(_Base):
	def setUp(self):
		super().setUp()
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		self.user = "boss@jarz.test"
		self.patch(bwc.frappe, "session", new=SimpleNamespace(user=self.user), create=True)
		self.doc = _pending_doc()
		self.patch(bwc.frappe.db, "exists", return_value="BWC-2026-00007", create=True)
		self.get_doc = self.patch(bwc.frappe, "get_doc", side_effect=lambda *a, **k: self.doc)

	def test_approve_posts_at_counted_at_and_stamps_the_count(self):
		result = bwc.approve_weekly_count("BWC-2026-00007")

		self.reconcile.assert_called_once()
		warehouse, posting, lines = self.reconcile.call_args.args
		self.assertEqual((warehouse, posting), ("Dokki - J", "2026-10-19 20:15:00"))
		# A late approval is judged against the stock at counted_at, not today's Bin.
		self.assertEqual(self.reconcile.call_args.kwargs, {"qty_as_of_posting": True})
		self.assertEqual(
			sorted((ln["item_code"], ln["qty"]) for ln in lines),
			[("BAG-1", 10.0), ("JAR-L-TIR", 3.0), ("JAR-S-TIR", 4.0)],
		)
		self.assertEqual(
			result,
			{
				"ok": True,
				"name": "BWC-2026-00007",
				"status": STATUS.APPROVED,
				"stock_reconciliation": "MAT-RECO-1",
			},
		)
		self.assertEqual(self.doc.status, STATUS.APPROVED)
		self.assertEqual(self.doc.approved_by, self.user)
		self.assertEqual(self.doc.stock_reconciliation, "MAT-RECO-1")
		self.assertTrue(self.doc.saved)

	def test_the_row_is_locked_while_deciding(self):
		bwc.approve_weekly_count("BWC-2026-00007")
		self.assertTrue(self.get_doc.call_args.kwargs.get("for_update"))

	def test_approve_with_no_differences_has_no_reconciliation(self):
		self.reconcile.return_value = {
			"ok": True,
			"stock_reconciliation": None,
			"message": "No differences found",
			"differences": 0,
		}
		result = bwc.approve_weekly_count("BWC-2026-00007")
		self.assertIsNone(result["stock_reconciliation"])
		self.assertEqual(self.doc.status, STATUS.APPROVED)

	def test_approve_refuses_a_count_that_is_not_pending(self):
		for status in (STATUS.APPROVED, STATUS.REJECTED):
			self.doc = _pending_doc(status=status)
			code, _exc = self.refusal(bwc.approve_weekly_count, "BWC-2026-00007")
			self.assertEqual(code, "not_pending")
		self.reconcile.assert_not_called()

	def test_nobody_decides_on_their_own_count(self):
		self.doc = _pending_doc(counted_by=self.user)
		for call in (
			lambda: bwc.approve_weekly_count("BWC-2026-00007"),
			lambda: bwc.reject_weekly_count("BWC-2026-00007", "why"),
		):
			code, exc = self.refusal(call)
			self.assertEqual(code, "own_count")
			self.assertIsInstance(exc, frappe.PermissionError)
		self.reconcile.assert_not_called()
		self.assertEqual(self.doc.status, STATUS.PENDING_APPROVAL)

	def test_approve_needs_an_approver(self):
		type(self).approver = False
		code, exc = self.refusal(bwc.approve_weekly_count, "BWC-2026-00007")
		self.assertEqual(code, "no_access")
		self.assertIsInstance(exc, frappe.PermissionError)
		self.reconcile.assert_not_called()

	def test_approve_needs_access_to_that_branch(self):
		self.patch(bwc, "get_allowed_pos_profiles_for_user", return_value=["Nasr city"])
		code, exc = self.refusal(bwc.approve_weekly_count, "BWC-2026-00007")
		self.assertEqual(code, "no_access")
		self.assertIsInstance(exc, frappe.PermissionError)
		self.reconcile.assert_not_called()
		self.assertEqual(self.doc.status, STATUS.PENDING_APPROVAL)

	def test_approve_of_a_missing_count_is_not_found(self):
		self.patch(bwc.frappe.db, "exists", return_value=None, create=True)
		code, _exc = self.refusal(bwc.approve_weekly_count, "BWC-NOPE")
		self.assertEqual(code, "not_found")

	def test_a_failed_posting_leaves_the_count_pending(self):
		self.reconcile.side_effect = frappe.ValidationError("Negative stock")
		with self.assertRaises(frappe.ValidationError):
			bwc.approve_weekly_count("BWC-2026-00007")
		self.assertEqual(self.doc.status, STATUS.PENDING_APPROVAL)
		self.assertFalse(self.doc.saved)

	def test_reject_needs_a_reason(self):
		for reason in ("", "   ", None):
			code, _exc = self.refusal(bwc.reject_weekly_count, "BWC-2026-00007", reason)
			self.assertEqual(code, "reason_required")
		self.assertEqual(self.doc.status, STATUS.PENDING_APPROVAL)

	def test_reject_marks_the_count_and_tells_the_counter(self):
		result = bwc.reject_weekly_count("BWC-2026-00007", "  Large jars look low, recount  ")
		self.assertEqual(result, {"ok": True, "name": "BWC-2026-00007", "status": STATUS.REJECTED})
		self.assertEqual(self.doc.status, STATUS.REJECTED)
		self.assertEqual(self.doc.rejection_reason, "Large jars look low, recount")
		self.assertTrue(self.doc.saved)
		self.reconcile.assert_not_called()
		self.enqueue.assert_called_once_with(
			"notify_branch_count_rejected", "BWC-2026-00007", "branch_count_rejected"
		)

	def test_reject_only_from_pending(self):
		self.doc = _pending_doc(status=STATUS.APPROVED)
		code, _exc = self.refusal(bwc.reject_weekly_count, "BWC-2026-00007", "why")
		self.assertEqual(code, "not_pending")
		self.enqueue.assert_not_called()

	def test_reject_needs_an_approver_with_branch_access(self):
		self.patch(bwc, "get_allowed_pos_profiles_for_user", return_value=["Nasr city"])
		code, _exc = self.refusal(bwc.reject_weekly_count, "BWC-2026-00007", "why")
		self.assertEqual(code, "no_access")
		type(self).approver = False
		code, _exc = self.refusal(bwc.reject_weekly_count, "BWC-2026-00007", "why")
		self.assertEqual(code, "no_access")


class TestReads(_Base):
	def setUp(self):
		super().setUp()
		self.doc = _pending_doc()
		self.patch(bwc.frappe.db, "exists", return_value="BWC-2026-00007", create=True)
		self.patch(bwc.frappe, "get_doc", side_effect=lambda *a, **k: self.doc)

	def test_a_non_approver_never_sees_system_quantities(self):
		result = bwc.get_weekly_count("BWC-2026-00007")
		self.assertFalse(result["can_approve"])
		self.assertIsNone(result["differences_count"])
		for line in result["lines"]:
			self.assertIsNone(line["system_qty"])
			self.assertIsNone(line["difference"])
			self.assertIsNotNone(line["counted_qty"])
		# Section order, not difference order.
		self.assertEqual([ln["item_code"] for ln in result["lines"]], ["JAR-L-TIR", "JAR-S-TIR", "BAG-1"])

	def test_an_approver_sees_quantities_biggest_difference_first(self):
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		self.doc = _pending_doc(counted_by="someone.else@jarz.test")
		result = bwc.get_weekly_count("BWC-2026-00007")
		self.assertTrue(result["can_approve"])
		self.assertEqual(result["differences_count"], 1)
		self.assertEqual([ln["item_code"] for ln in result["lines"]], ["JAR-L-TIR", "JAR-S-TIR", "BAG-1"])
		self.assertEqual(result["lines"][0]["system_qty"], 5.0)
		self.assertEqual(result["lines"][0]["difference"], -2.0)
		self.assertEqual(result["counted_at"], "2026-10-19 20:15:00")
		self.assertEqual(result["week_start"], WEEK_START)

	def test_can_approve_only_while_pending(self):
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		self.doc = _pending_doc(status=STATUS.APPROVED, counted_by="someone.else@jarz.test")
		self.assertFalse(bwc.get_weekly_count("BWC-2026-00007")["can_approve"])

	def test_an_approver_cannot_approve_their_own_count(self):
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		self.doc = _pending_doc(counted_by=self.user)
		self.assertFalse(bwc.get_weekly_count("BWC-2026-00007")["can_approve"])

	def test_an_approver_without_the_branch_cannot_read_it(self):
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		self.patch(bwc, "get_allowed_pos_profiles_for_user", return_value=["Nasr city"])
		code, _exc = self.refusal(bwc.get_weekly_count, "BWC-2026-00007")
		self.assertEqual(code, "no_access")

	def test_staff_cannot_read_a_colleagues_count(self):
		self.doc = _pending_doc(counted_by="someone.else@jarz.test")
		code, _exc = self.refusal(bwc.get_weekly_count, "BWC-2026-00007")
		self.assertEqual(code, "no_access")

	def test_list_for_staff_is_limited_to_their_own_counts(self):
		get_all = self.patch(bwc.frappe, "get_all", return_value=[_pending_doc().__dict__])
		result = bwc.list_weekly_counts(status=STATUS.PENDING_APPROVAL, limit="500")
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["counted_by"], self.user)
		self.assertEqual(filters["pos_profile"], ["in", ["Dokki"]])
		self.assertEqual(filters["status"], STATUS.PENDING_APPROVAL)
		self.assertEqual(get_all.call_args.kwargs["limit"], 200)
		self.assertIsNone(result["counts"][0]["differences_count"])

	def test_list_for_an_approver_covers_the_branch_and_shows_differences(self):
		type(self).approver = True
		self.addCleanup(setattr, type(self), "approver", False)
		get_all = self.patch(bwc.frappe, "get_all", return_value=[_pending_doc().__dict__])
		result = bwc.list_weekly_counts()
		self.assertNotIn("counted_by", get_all.call_args.kwargs["filters"])
		self.assertEqual(result["counts"][0]["differences_count"], 1)
		self.assertEqual(result["counts"][0]["week_start"], WEEK_START)

	def test_list_with_a_branch_outside_the_scope_is_empty(self):
		get_all = self.patch(bwc.frappe, "get_all", return_value=[])
		self.assertEqual(bwc.list_weekly_counts(pos_profile="Nasr city"), {"counts": []})
		get_all.assert_not_called()


class TestApprover(_Base):
	def test_roles_that_approve(self):
		self.stack.close()  # undo the is_count_approver stub from _Base.setUp
		no_list = patch.object(bwc, "listed_count_approvers", return_value=None)
		no_list.start()
		self.addCleanup(no_list.stop)
		for role in (
			ROLES.JARZ_LINE_MANAGER,
			ROLES.JARZ_LINE_MANAGER_ALT,
			ROLES.JARZ_MANAGER,
			"Stock Manager",
		):
			with patch.object(bwc.frappe, "get_roles", return_value=[role], create=True):
				self.assertTrue(bwc.is_count_approver("someone@jarz.test"), role)
		with patch.object(bwc.frappe, "get_roles", return_value=["Sales User"], create=True):
			self.assertFalse(bwc.is_count_approver("someone@jarz.test"))
		self.assertTrue(bwc.is_count_approver("Administrator"))

	def test_the_list_alone_decides_once_anyone_is_on_it(self):
		self.stack.close()
		listed = {"kareem@jarz.test", "reem@jarz.test"}
		with (
			patch.object(bwc, "listed_count_approvers", return_value=listed),
			patch.object(bwc.frappe, "get_roles", return_value=[ROLES.JARZ_LINE_MANAGER], create=True),
		):
			self.assertTrue(bwc.is_count_approver("kareem@jarz.test"))
			self.assertFalse(bwc.is_count_approver("belal@jarz.test"))  # line manager, not listed
			self.assertTrue(bwc.is_count_approver("Administrator"))

	def test_a_list_of_only_disabled_users_approves_nobody(self):
		self.stack.close()
		with patch.object(bwc, "listed_count_approvers", return_value=set()):
			self.assertFalse(bwc.is_count_approver("someone@jarz.test"))

	def test_listed_approvers_reads_the_settings_table_and_drops_disabled_users(self):
		def get_all(doctype, **kwargs):
			if doctype == "Jarz Weekly Count Approver":
				self.assertEqual(
					kwargs["filters"],
					{"parenttype": "Jarz POS Settings", "parentfield": "weekly_count_approvers"},
				)
				return ["kareem@jarz.test", "gone@jarz.test"]
			if doctype == "User":
				return ["kareem@jarz.test"]
			return []

		self.stack.close()
		with patch.object(bwc.frappe, "get_all", side_effect=get_all, create=True):
			self.assertEqual(bwc.listed_count_approvers(), {"kareem@jarz.test"})

	def test_an_empty_or_unmigrated_list_falls_back_to_roles(self):
		self.stack.close()
		with patch.object(bwc.frappe, "get_all", return_value=[], create=True):
			self.assertIsNone(bwc.listed_count_approvers())
		with patch.object(bwc.frappe, "get_all", side_effect=RuntimeError("no table"), create=True):
			self.assertIsNone(bwc.listed_count_approvers())

	def test_a_listed_approver_covers_every_enabled_branch(self):
		self.patch(bwc, "listed_count_approvers", return_value={"kareem@jarz.test"})
		get_all = self.patch(bwc.frappe, "get_all", return_value=["Dokki", "Nasr city", "Zayed"], create=True)
		self.assertEqual(bwc.count_scope("kareem@jarz.test"), ["Dokki", "Nasr city", "Zayed"])
		get_all.assert_called_once_with("POS Profile", filters={"disabled": 0}, pluck="name")
		self.assertEqual(bwc.count_scope("staff@jarz.test"), ["Dokki"])

	def test_a_listed_approver_is_a_member_only_of_their_own_branches(self):
		self.patch(bwc, "listed_count_approvers", return_value={"kareem@jarz.test"})
		self.patch(bwc, "user_has_global_profile_access", return_value=False)
		self.patch(bwc.frappe, "get_all", return_value=["Dokki"], create=True)
		self.assertEqual(bwc._member_profiles("kareem@jarz.test", ["Dokki", "Zayed"]), {"Dokki"})
		self.assertEqual(bwc._member_profiles("staff@jarz.test", ["Dokki", "Zayed"]), {"Dokki", "Zayed"})

	def test_pending_count_with_no_branch_is_zero_not_all(self):
		count = self.patch(bwc.frappe.db, "count", return_value=99, create=True)
		self.patch(bwc, "get_allowed_pos_profiles_for_user", return_value=[])
		self.assertEqual(bwc.count_pending_in_scope(), 0)
		count.assert_not_called()

	def test_pending_count_is_limited_to_the_scope(self):
		count = self.patch(bwc.frappe.db, "count", return_value=4, create=True)
		self.assertEqual(bwc.count_pending_in_scope(), 4)
		count.assert_called_once_with(
			"Branch Weekly Count",
			filters={"status": STATUS.PENDING_APPROVAL, "pos_profile": ["in", ["Dokki"]]},
		)


class TestEnqueuePush(unittest.TestCase):
	def _run(self, suppressed):
		fake_notifications = SimpleNamespace(outbound_alerts_suppressed=lambda context="": suppressed)
		enqueue = MagicMock()
		with (
			patch.dict(sys.modules, {"jarz_pos.api.notifications": fake_notifications}),
			patch.object(bwc.frappe, "enqueue", enqueue, create=True),
		):
			queued = bwc._enqueue_push("notify_branch_count_rejected", "BWC-1", "branch_count_rejected")
		return queued, enqueue

	def test_queues_after_commit_on_the_short_queue(self):
		queued, enqueue = self._run(suppressed=False)
		self.assertTrue(queued)
		enqueue.assert_called_once_with(
			"jarz_pos.api.notifications.notify_branch_count_rejected",
			queue="short",
			enqueue_after_commit=True,
			count_name="BWC-1",
		)

	def test_a_test_run_queues_nothing(self):
		queued, enqueue = self._run(suppressed=True)
		self.assertFalse(queued)
		enqueue.assert_not_called()

	def test_a_broken_queue_never_fails_the_count(self):
		fake_notifications = SimpleNamespace(outbound_alerts_suppressed=lambda context="": False)
		with (
			patch.dict(sys.modules, {"jarz_pos.api.notifications": fake_notifications}),
			patch.object(bwc.frappe, "enqueue", side_effect=RuntimeError("redis down"), create=True),
		):
			self.assertFalse(bwc._enqueue_push("notify_branch_count_rejected", "BWC-1", "x"))


class TestApprovalsQueue(unittest.TestCase):
	def test_the_queue_is_registered_under_its_wire_key(self):
		self.assertEqual(approvals.Q_BRANCH_COUNTS, "branch_counts")
		self.assertIn(("branch_counts", approvals._branch_counts_queue), approvals._QUEUES)

	def test_absent_for_a_caller_who_cannot_approve(self):
		with patch.object(bwc, "is_count_approver", return_value=False):
			self.assertIsNone(approvals._branch_counts_queue())

	def test_counts_pending_in_the_callers_scope_for_an_approver(self):
		with (
			patch.object(bwc, "is_count_approver", return_value=True),
			patch.object(bwc, "count_pending_in_scope", return_value=3),
		):
			self.assertEqual(approvals._branch_counts_queue(), {"count": 3})

	def test_it_shows_up_in_get_pending_approvals(self):
		with (
			patch.object(bwc, "is_count_approver", return_value=True),
			patch.object(bwc, "count_pending_in_scope", return_value=2),
			patch.object(approvals, "_QUEUES", [("branch_counts", approvals._branch_counts_queue)]),
		):
			result = approvals.get_pending_approvals()
		self.assertEqual(result["total"], 2)
		self.assertEqual(result["queues"], [{"key": "branch_counts", "count": 2}])


try:
	from jarz_pos.api import notifications
except Exception:  # needs a live frappe site (firebase, redis) -- skipped where it cannot import
	notifications = None


@unittest.skipIf(notifications is None, "jarz_pos.api.notifications needs a frappe site to import")
class TestBranchCountNotifications(unittest.TestCase):
	def _row(self, **overrides):
		row = {
			"pos_profile": "Dokki",
			"counted_by": "staff@jarz.test",
			"counted_by_name": "Yehia",
			"differences_count": 4,
			"status": STATUS.PENDING_APPROVAL,
			"rejection_reason": "Recount the large jars",
		}
		row.update(overrides)
		return row

	def _patch_count_row(self, row):
		"""Serve `row` for the Branch Weekly Count lookup only.

		A blanket get_value mock also answers frappe's own lookups: under
		`bench run-tests` the system time zone cache is cold, so now_datetime()
		reads System Settings through get_value, gets the count row back and
		raises. Every other doctype goes to the real (or stubbed) get_value.
		"""
		original = getattr(notifications.frappe.db, "get_value", None)

		def get_value(doctype, *args, **kwargs):
			if doctype == "Branch Weekly Count":
				return row
			return original(doctype, *args, **kwargs) if original else None

		return patch.object(notifications.frappe.db, "get_value", side_effect=get_value, create=True)

	def test_both_types_use_the_approvals_channel(self):
		self.assertIn("branch_count_approval", notifications.APPROVAL_NOTIFICATION_TYPES)
		self.assertIn("branch_count_rejected", notifications.APPROVAL_NOTIFICATION_TYPES)

	def test_approval_push_goes_to_the_branch_approvers_without_the_counter(self):
		send = MagicMock(return_value={"ok": True, "status": "sent"})
		recipients = MagicMock(return_value=["boss@jarz.test"])
		with (
			patch.object(notifications, "outbound_alerts_suppressed", return_value=False),
			self._patch_count_row(self._row()),
			patch.object(notifications, "_branch_count_approver_recipients", recipients),
			patch.object(notifications, "_send_branch_count_push", send),
		):
			result = notifications.notify_branch_count_approval_required("BWC-1")

		recipients.assert_called_once_with("Dokki", "staff@jarz.test")
		args = send.call_args.args
		self.assertEqual(args[0], ["boss@jarz.test"])
		data = args[1]
		self.assertEqual(data["type"], "branch_count_approval")
		self.assertEqual(data["count_name"], "BWC-1")
		self.assertEqual(data["pos_profile"], "Dokki")
		self.assertEqual(data["title"], "جرد أسبوعي بانتظار الموافقة")
		self.assertEqual(data["body"], "Yehia · Dokki · 4 اختلاف")
		self.assertEqual(result["recipients"], 1)

	def test_nothing_is_sent_for_a_count_already_decided(self):
		send = MagicMock()
		with (
			patch.object(notifications, "outbound_alerts_suppressed", return_value=False),
			patch.object(
				notifications.frappe.db,
				"get_value",
				return_value=self._row(status=STATUS.APPROVED),
				create=True,
			),
			patch.object(notifications, "_send_branch_count_push", send),
		):
			result = notifications.notify_branch_count_approval_required("BWC-1")
		self.assertEqual(result["status"], "skipped_not_pending")
		send.assert_not_called()

	def test_nothing_is_sent_during_a_test_run(self):
		send = MagicMock()
		with (
			patch.object(notifications, "outbound_alerts_suppressed", return_value=True),
			patch.object(notifications, "_send_branch_count_push", send),
		):
			self.assertEqual(
				notifications.notify_branch_count_approval_required("BWC-1")["status"],
				"suppressed_test_run",
			)
			self.assertEqual(
				notifications.notify_branch_count_rejected("BWC-1")["status"], "suppressed_test_run"
			)
		send.assert_not_called()

	def test_rejection_push_goes_to_the_counter_with_the_reason(self):
		send = MagicMock(return_value={"ok": True, "status": "sent"})
		with (
			patch.object(notifications, "outbound_alerts_suppressed", return_value=False),
			self._patch_count_row(self._row(status=STATUS.REJECTED)),
			patch.object(notifications, "_send_branch_count_push", send),
		):
			notifications.notify_branch_count_rejected("BWC-1")
		args = send.call_args.args
		self.assertEqual(args[0], ["staff@jarz.test"])
		self.assertEqual(args[1]["type"], "branch_count_rejected")
		self.assertEqual(args[1]["title"], "الجرد الأسبوعي مرفوض — أعد العد")
		self.assertEqual(args[1]["body"], "Recount the large jars")
		self.assertEqual(args[1]["count_name"], "BWC-1")

	def test_recipients_are_approvers_of_that_branch_not_the_counter(self):
		def fake_get_all(doctype, **kwargs):
			if doctype == "POS Profile User":
				return ["branch.lead@jarz.test"]
			if doctype == "Has Role":
				return ["owner@jarz.test"]
			return []

		holders = [
			"branch.lead@jarz.test",
			"other.branch@jarz.test",
			"owner@jarz.test",
			"staff@jarz.test",
		]
		with (
			patch.object(bwc, "listed_count_approvers", return_value=None),
			patch.object(notifications, "_get_users_with_roles", return_value=holders),
			patch.object(notifications.frappe, "get_all", side_effect=fake_get_all, create=True),
		):
			result = notifications._branch_count_approver_recipients("Dokki", "staff@jarz.test")
		self.assertEqual(result, ["branch.lead@jarz.test", "owner@jarz.test"])

	def test_with_a_list_every_listed_approver_is_pushed_for_any_branch(self):
		roles = MagicMock()
		with (
			patch.object(bwc, "listed_count_approvers", return_value={"kareem@jarz.test", "reem@jarz.test"}),
			patch.object(notifications, "_get_users_with_roles", roles),
		):
			self.assertEqual(
				notifications._branch_count_approver_recipients("Zayed", "reem@jarz.test"),
				["kareem@jarz.test"],
			)
		roles.assert_not_called()


if __name__ == "__main__":
	unittest.main()

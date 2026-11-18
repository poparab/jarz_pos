"""Approve a moderator-override line as a Brand Fact (Feature: chat-approve-override-fact) -- mocked, no site.

Pins: the source marker round-trips; approved indexes are grouped per draft;
only a manager may approve; a bad line index is refused; the fact is created
Approved with who/when/source; a second approval of the same line creates
nothing; the draft shape carries ``approved_conflicts``.
"""

import datetime
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

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
	_utils.getdate = lambda d=None: d
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_common as cc
from jarz_pos.services import chat_fact_approval as fa


class _Denied(Exception):
	pass


def _throw(message, exc=None, *a, **k):
	raise _Denied(str(message))


def _row(**kw):
	class Row(dict):
		__getattr__ = dict.get

	return Row(**kw)


DRAFT_ROW = _row(
	name="DRAFT-9",
	conversation="CHAT-1",
	fact_conflicts="The fruit is real but blended → only real fruit is approved\n150 → Trio is 165",
	moderator_hint="tell her it's real fruit but blended",
)
META = SimpleNamespace(get_field=lambda f: SimpleNamespace(options="Product\nIngredients and claims\nOther"))


class MarkerTest(unittest.TestCase):
	def test_marker_round_trips(self):
		marker = fa.source_marker("DRAFT-9", 1)
		self.assertEqual(marker, "Moderator override DRAFT-9 #1")
		self.assertEqual(fa.parse_marker(marker + ": a → b"), ("DRAFT-9", 1))
		self.assertIsNone(fa.parse_marker("Approved Facts Registry (17 Oct 2026)"))
		self.assertIsNone(fa.parse_marker(None))

	def test_approved_indexes_grouped_and_sorted(self):
		rows = [
			{"source": "Moderator override D1 #2: x"},
			{"source": "Moderator override D1 #0: y"},
			{"source": "Moderator override D2 #1: z"},
			{"source": "Moderator override D1 #0: again"},
			{"source": "something else"},
		]
		with mock.patch.object(frappe, "get_all", return_value=rows, create=True):
			self.assertEqual(fa.approved_indexes(["D1", "D3"]), {"D1": [0, 2]})
		self.assertEqual(fa.approved_indexes([]), {})


class ApproveTest(unittest.TestCase):
	def run_approve(self, *, manager=True, existing=(), index=0, **kw):
		inserted = []

		def get_doc(data):
			doc = SimpleNamespace(**data, name="FACT-1")
			doc.insert = lambda **k: inserted.append(data)
			return doc

		def get_all(doctype, **k):
			if k.get("pluck") == "name":
				return list(existing)
			return [{"source": f"Moderator override DRAFT-9 #{index}: x"}] if (inserted or existing) else []

		with (
			mock.patch.object(fa, "can_approve", return_value=manager),
			mock.patch.object(frappe, "throw", side_effect=_throw, create=True),
			mock.patch.object(frappe, "PermissionError", Exception, create=True),
			mock.patch.object(frappe, "DoesNotExistError", Exception, create=True),
			mock.patch.object(
				frappe, "db", SimpleNamespace(get_value=lambda *a, **k: DRAFT_ROW), create=True
			),
			mock.patch.object(frappe, "get_all", side_effect=get_all, create=True),
			mock.patch.object(frappe, "get_doc", side_effect=get_doc, create=True),
			mock.patch.object(frappe, "get_meta", return_value=META, create=True),
			mock.patch.object(frappe, "session", SimpleNamespace(user="owner@jarz"), create=True),
			mock.patch.object(frappe, "utils", SimpleNamespace(getdate=lambda d: d.date()), create=True),
			mock.patch.object(cc, "now", return_value=datetime.datetime(2026, 11, 18, 19, 0)),
			mock.patch.object(cc, "full_name", return_value="Abdelrahman Mamdouh"),
			mock.patch.object(fa, "_clear_draft_context") as cleared,
		):
			out = fa.approve(
				"DRAFT-9",
				index,
				kw.get("topic", "Real fruit, blended"),
				kw.get("fact_en", "Fillings made with real fruit, blended"),
				kw.get("fact_ar", ""),
				kw.get("category", "Ingredients and claims"),
			)
		return out, inserted, cleared

	def test_creates_an_approved_fact_with_its_source(self):
		out, inserted, cleared = self.run_approve()
		self.assertEqual(len(inserted), 1)
		fact = inserted[0]
		self.assertEqual(fact["doctype"], "Jarz Brand Fact")
		self.assertEqual(fact["status"], "Approved")
		self.assertEqual(fact["topic"], "Real fruit, blended")
		self.assertEqual(fact["category"], "Ingredients and claims")
		self.assertEqual(fact["approved_by"], "Abdelrahman Mamdouh")
		self.assertEqual(str(fact["approved_on"]), "2026-11-18")
		self.assertTrue(
			fact["source"].startswith("Moderator override DRAFT-9 #0: The fruit is real but blended")
		)
		self.assertIn("CHAT-1", fact["notes"])
		self.assertEqual(out, {"fact": "FACT-1", "draft": "DRAFT-9", "approved_conflicts": [0]})
		cleared.assert_called_once()

	def test_unknown_category_falls_back_to_other(self):
		_out, inserted, _c = self.run_approve(category="Nonsense")
		self.assertEqual(inserted[0]["category"], "Other")

	def test_second_approval_creates_nothing(self):
		out, inserted, cleared = self.run_approve(existing=["FACT-OLD"])
		self.assertEqual(inserted, [])
		self.assertEqual(out["fact"], "FACT-OLD")
		cleared.assert_not_called()

	def test_only_a_manager(self):
		with self.assertRaises(_Denied):
			self.run_approve(manager=False)

	def test_bad_index_and_empty_text_refused(self):
		with self.assertRaises(_Denied):
			self.run_approve(index=5)
		with self.assertRaises(_Denied):
			self.run_approve(topic="  ")
		with self.assertRaises(_Denied):
			self.run_approve(fact_en="", fact_ar="")


class DraftShapeTest(unittest.TestCase):
	def test_draft_carries_approved_conflicts(self):
		row = _row(name="DRAFT-9", fact_conflicts="a → b\nc → d", status="Pending")
		with (
			mock.patch.object(fa, "approved_indexes", return_value={"DRAFT-9": [1]}) as looked,
			mock.patch.object(cc, "_item_name", return_value=None),
		):
			shape = cc.serialize_draft(row)
		self.assertEqual(shape["fact_conflicts"], ["a → b", "c → d"])
		self.assertEqual(shape["approved_conflicts"], [1])
		looked.assert_called_once_with(["DRAFT-9"])

	def test_no_lookup_without_conflicts(self):
		with (
			mock.patch.object(fa, "approved_indexes") as looked,
			mock.patch.object(cc, "_item_name", return_value=None),
		):
			shape = cc.serialize_draft(_row(name="DRAFT-1", status="Pending"))
		self.assertEqual(shape["approved_conflicts"], [])
		looked.assert_not_called()


if __name__ == "__main__":
	unittest.main()

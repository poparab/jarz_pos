"""Chat customer linking by typed phone, and the nightly customer notes -- pure, no site.

Pins:

* a mobile typed in a chat is found in the spellings customers use (010…,
  +2010…, 2010…, spaced, Arabic-Indic digits) and nothing else (prices, order
  numbers, landlines);
* the notes call gets every chat and the latest orders, and a NONE answer or an
  over-long one is stored as nothing / trimmed on a line break.

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

from jarz_pos.services import chat_ingest as ing
from jarz_pos.services import chat_summary as s


class TypedPhonesTest(unittest.TestCase):
	def test_spellings(self):
		self.assertEqual(ing.typed_phones("رقمي 01121480676"), ["01121480676"])
		self.assertEqual(ing.typed_phones("+201121480676"), ["+201121480676"])
		self.assertEqual(ing.typed_phones("0112 148 0676"), ["01121480676"])
		self.assertEqual(ing.typed_phones("0112-148-0676"), ["01121480676"])
		self.assertEqual(ing.typed_phones("٠١١٢١٤٨٠٦٧٦"), ["01121480676"])
		self.assertEqual(ing.typed_phones("1121480676"), ["1121480676"])

	def test_several_numbers_in_order_without_repeats(self):
		text = "ده رقمي 01121480676 وده رقم ماما 01001234567 وتاني 01121480676"
		self.assertEqual(ing.typed_phones(text), ["01121480676", "01001234567"])

	def test_not_phones(self):
		for text in ("الحساب 480 جنيه", "اوردر رقم 19816", "0233445566", "013121480676", "", None):
			self.assertEqual(ing.typed_phones(text), [], text)

	def test_no_link_without_a_number(self):
		self.assertEqual(ing.link_by_typed_phone("عايز اطلب"), {})


class SummaryInputTest(unittest.TestCase):
	def test_order_line(self):
		line = s.order_line("2026-07-22", [("Jarz Signature Trio", 1.0), ("Lotus Large", 2.0), ("", 1.0)])
		self.assertEqual(line, "2026-07-22: Jarz Signature Trio, Lotus Large x2")
		self.assertEqual(s.order_line("2026-07-22", []), "2026-07-22: (no items)")

	def test_build_input_has_every_chat_and_the_orders(self):
		text = s.build_input(
			[("Messenger", ["Customer: عايز جار لوتس"]), ("WhatsApp", [])],
			["2026-07-22: Lotus Large x2"],
			17,
		)
		self.assertIn("## Messenger chat (oldest first)\nCustomer: عايز جار لوتس", text)
		self.assertIn("## WhatsApp chat (oldest first)\n(no text messages)", text)
		self.assertIn("## Orders (17 in total, latest 1 shown)\n2026-07-22: Lotus Large x2", text)

	def test_build_input_without_orders(self):
		self.assertIn("(no orders on record)", s.build_input([], [], 0))


class CleanSummaryTest(unittest.TestCase):
	def test_none_is_empty(self):
		for text in ("NONE", " none. ", "", None):
			self.assertEqual(s.clean_summary(text), "")

	def test_long_notes_cut_on_a_line(self):
		notes = "\n".join(f"- point {i} " + "x" * 150 for i in range(10))
		out = s.clean_summary(notes)
		self.assertLessEqual(len(out), s.SUMMARY_MAX_CHARS)
		self.assertTrue(out.endswith("x"))
		self.assertEqual(out.count("\n") + 1, len(out.splitlines()))

	def test_prompt_forbids_personal_data(self):
		self.assertIn("Never include phone numbers", s.SYSTEM_PROMPT)
		self.assertIn("NONE", s.SYSTEM_PROMPT)


if __name__ == "__main__":
	unittest.main()

"""Staff notes on a chat (Feature: chat-staff-notes) -- pure parts, no site.

Pins: a note is trimmed and capped; the AI block prints notes oldest first,
flags open vs handled, masks names, and is empty with no notes; the prompt
rules make saved replies the first choice and let staff notes outrank facts.
"""

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
	_utils.getdate = lambda d=None: d
	sys.modules["frappe.utils"] = _utils
	frappe.utils = _utils

from jarz_pos.services import chat_notes as cn


class TestCleanNote(unittest.TestCase):
	def test_trims_and_collapses(self):
		self.assertEqual(cn.clean_note("  hello   there \n\n\n next  "), "hello there\n\nnext")

	def test_caps_length(self):
		self.assertEqual(len(cn.clean_note("x" * 5000)), cn.MAX_NOTE_CHARS)

	def test_empty(self):
		self.assertEqual(cn.clean_note(None), "")
		self.assertEqual(cn.clean_note("  \n "), "")


class TestPromptNotes(unittest.TestCase):
	def test_empty(self):
		self.assertEqual(cn.render_prompt_notes([]), "")

	def test_oldest_first_with_state(self):
		rows = [  # newest first, as load_rows returns them
			{"note": "Give her free delivery", "resolved": 0},
			{"note": "Moderator was rude, apologise", "resolved": 1},
		]
		text = cn.render_prompt_notes(rows)
		self.assertIn("- [handled] Moderator was rude, apologise", text)
		self.assertIn("- [OPEN] Give her free delivery", text)
		self.assertLess(text.index("rude"), text.index("free delivery"))
		self.assertIn("rule 17", text)
		self.assertIn("Never mention the notes", text)

	def test_mask_applied(self):
		text = cn.render_prompt_notes([{"note": "Mona is VIP", "resolved": 0}], lambda t: t.replace("Mona", "[name]"))
		self.assertIn("[name] is VIP", text)
		self.assertNotIn("Mona", text)

	def test_caps_count(self):
		rows = [{"note": f"n{i}", "resolved": 0} for i in range(cn.PROMPT_NOTES + 5)]
		self.assertEqual(cn.render_prompt_notes(rows).count("- [OPEN]"), cn.PROMPT_NOTES)


class TestPromptRules(unittest.TestCase):
	def test_saved_replies_first_and_notes_outrank(self):
		from jarz_pos.services import chat_drafts as d

		p = d.SYSTEM_PROMPT
		self.assertIn("9. Saved replies first.", p)
		self.assertIn("Do not rephrase a saved reply", p)
		self.assertIn("each staff note on this chat", p)


if __name__ == "__main__":
	unittest.main()

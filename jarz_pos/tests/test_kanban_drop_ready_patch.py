"""``Patches.v1_9.kanban_drop_ready_state`` with the database mocked out.

Pure ``unittest``: no site, no DB, no ``erpnext.tests.utils``.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from jarz_pos.Patches.v1_9 import kanban_drop_ready_state as patch_mod

_COLUMNS = ("custom_sales_invoice_state", "sales_invoice_state")


def _fake_frappe(ready_rows, columns=_COLUMNS, log_doctype_exists=True):
	"""A frappe whose SELECT returns ``ready_rows`` until the UPDATE has run."""
	fake = MagicMock()
	fake.db.has_column.side_effect = lambda dt, col: col in columns
	fake.db.exists.return_value = log_doctype_exists
	fake.utils.now_datetime.return_value = "2026-10-13 02:00:00"
	state = {"updated": False}

	def sql(query, values=None):
		if query.lstrip().upper().startswith("SELECT"):
			return [] if state["updated"] else list(ready_rows)
		state["updated"] = True
		return None

	fake.db.sql.side_effect = sql
	return fake


def _updates(fake):
	return [c for c in fake.db.sql.call_args_list if c.args[0].lstrip().upper().startswith("UPDATE")]


RECORD = "jarz_pos.services.state_log.record_state_change"


class TestConversion(unittest.TestCase):
	def test_every_ready_row_is_converted_in_both_columns(self):
		fake = _fake_frappe([("SINV-1", "Dokki"), ("SINV-2", "Nasr city")])
		with patch.object(patch_mod, "frappe", fake), patch(RECORD):
			patch_mod.execute()
		updates = _updates(fake)
		self.assertEqual(len(updates), 2)
		for call in updates:
			self.assertEqual(call.args[1], ("In Progress", "Ready"))
		self.assertIn("`custom_sales_invoice_state`", updates[0].args[0])
		self.assertIn("`sales_invoice_state`", updates[1].args[0])

	def test_modified_is_never_touched(self):
		fake = _fake_frappe([("SINV-1", "Dokki")])
		with patch.object(patch_mod, "frappe", fake), patch(RECORD):
			patch_mod.execute()
		for call in _updates(fake):
			self.assertNotIn("modified", call.args[0].lower())

	def test_one_log_row_per_converted_invoice(self):
		fake = _fake_frappe([("SINV-1", "Dokki"), ("SINV-2", "Nasr city")])
		with patch.object(patch_mod, "frappe", fake), patch(RECORD) as rec:
			patch_mod.execute()
		self.assertEqual(rec.call_count, 2)
		first = rec.call_args_list[0]
		self.assertEqual(first.args, ("SINV-1", "Ready", "In Progress", "migration:drop_ready"))
		self.assertEqual(first.kwargs["user"], "Administrator")
		self.assertEqual(first.kwargs["pos_profile"], "Dokki")
		fake.db.commit.assert_called_once()

	def test_an_invoice_in_both_columns_is_logged_once(self):
		# The same invoice is returned by the SELECT of each column.
		fake = _fake_frappe([("SINV-1", "Dokki")])
		with patch.object(patch_mod, "frappe", fake), patch(RECORD) as rec:
			patch_mod.execute()
		self.assertEqual(rec.call_count, 1)

	def test_log_is_skipped_when_the_doctype_is_not_there_yet(self):
		fake = _fake_frappe([("SINV-1", "Dokki")], log_doctype_exists=False)
		with patch.object(patch_mod, "frappe", fake), patch(RECORD) as rec:
			patch_mod.execute()
		rec.assert_not_called()
		self.assertEqual(len(_updates(fake)), 2)

	def test_only_the_existing_column_is_touched(self):
		fake = _fake_frappe([("SINV-1", "Dokki")], columns=("custom_sales_invoice_state",))
		with patch.object(patch_mod, "frappe", fake), patch(RECORD):
			patch_mod.execute()
		updates = _updates(fake)
		self.assertEqual(len(updates), 1)
		self.assertIn("`custom_sales_invoice_state`", updates[0].args[0])

	def test_audit_note_failure_does_not_abort(self):
		fake = _fake_frappe([("SINV-1", "Dokki")])
		fake.log_error.side_effect = RuntimeError("log_error exploded")
		with patch.object(patch_mod, "frappe", fake), patch(RECORD):
			patch_mod.execute()
		fake.db.commit.assert_called_once()


class TestIdempotent(unittest.TestCase):
	def test_second_run_finds_nothing_and_changes_nothing(self):
		fake = _fake_frappe([("SINV-1", "Dokki")])
		with patch.object(patch_mod, "frappe", fake), patch(RECORD) as rec:
			patch_mod.execute()
			first_updates = len(_updates(fake))
			first_logs = rec.call_count
			patch_mod.execute()
		self.assertEqual(len(_updates(fake)), first_updates)
		self.assertEqual(rec.call_count, first_logs)
		self.assertEqual(fake.db.commit.call_count, 1)

	def test_no_ready_rows_means_no_writes(self):
		fake = _fake_frappe([])
		with patch.object(patch_mod, "frappe", fake), patch(RECORD) as rec:
			patch_mod.execute()
		self.assertEqual(_updates(fake), [])
		rec.assert_not_called()
		fake.db.commit.assert_not_called()

	def test_no_state_columns_is_a_no_op(self):
		fake = _fake_frappe([("SINV-1", "Dokki")], columns=())
		with patch.object(patch_mod, "frappe", fake):
			patch_mod.execute()
		fake.db.sql.assert_not_called()


class TestRegistration(unittest.TestCase):
	def test_patch_is_registered_in_post_model_sync(self):
		import jarz_pos

		path = os.path.join(os.path.dirname(os.path.abspath(jarz_pos.__file__)), "patches.txt")
		with open(path, "r", encoding="utf-8") as handle:
			text = handle.read()
		post = text.split("[post_model_sync]", 1)[1]
		self.assertIn("jarz_pos.Patches.v1_9.kanban_drop_ready_state", post)


if __name__ == "__main__":
	unittest.main()
